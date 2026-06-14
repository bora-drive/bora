# Stage 7b-E2 — Fault-Injection Suite: Deviations & Findings

**Date:** 2026-06-14
**Plan:** `docs/superpowers/plans/2026-06-14-stage-7b-e2-fault-injection.md`
**Spec:** `docs/superpowers/specs/2026-06-14-stage-7b-e2-fault-injection-design.md`
**Branch:** `google-drive-port`. Pre-stage HEAD: `74c6b65`. Execution: subagent-driven-development (implementer + spec/teeth review + code-quality review per task, with adversarial finding-verification on Task 4 & Task 8).

## Headline

- **Zero production changes this stage.** `git diff 74c6b65 HEAD -- src/` is empty. Every defect surfaced was captured as a `strict=True` xfail and escalated (per spec §8 triage), not fixed in-line. This is a pure test-addition + fake-infra stage.
- **+81 tests** (636 collected vs the 555 baseline). 3 `strict` xfails record confirmed production defects (see Findings).
- **3 confirmed production defects found** (Findings B, C, persistent-400), all adversarially verified by independent code-trace, all deferred by user decision (2026-06-14) to a dedicated post-gate TDD+review fix cycle — mirroring how cluster-E findings 2 & 5 were handled.

## Per-task summary

| Task | Module | Tests | Outcome |
|---|---|---|---|
| 1 | `tests/fake_drive.py` + `test_fake_drive.py` | +~12 | Fault-surface generalization: unified rule engine (`fail_next(count=,when=)`, `fail_after`, `fail_on_call`, `fail_when(predicate)`), `fail_kwargs` threaded to every resource `.execute()`, get_media fault hook, `make_timeout`/`make_conn_reset`. No defects. |
| 2 | `test_fault_feed.py` | 7 | Partial change-feed pages. All characterization (teeth-verified). No defects. |
| 3 | `test_fault_crash.py` | 8 | Crash-window / replay (F2/finding-2, F7). All characterization (teeth-verified). No defects. |
| 4 | `test_fault_divergence.py` | 7 pass + 2 xfail | Drive-vs-cache divergence. F3 + occupied-target + missing-parent green. **2 findings (B, C) xfailed.** |
| 5 | `test_fault_writes.py` | 8 | Write-client mid-flight (resumable upload, remove_batch, move/make_dir retry). No defects. |
| 6 | `test_fault_download.py` (+ corruption hook) | 6 (+4 hook) | Download integrity (truncate/flip/wrong-md5/mid-stream). No defects. |
| 7 | `test_fault_ratelimit.py` | 12 | Rate-limit/429 + quota (F14). dailyLimitExceeded-not-retried confirmed correct. No defects. |
| 8 | `test_fault_cursor.py` | 6 pass + 1 xfail | Cursor-invalidation (F5). 400→CursorResetError→reindex green. **1 finding (persistent-400) xfailed.** |
| 9 | `test_fault_auth.py` | 12 | Auth/token (F13). 401/RefreshError→TokenRevokedError fatal-clean propagation confirmed. No defects. |
| 10 | — (this gate) | — | Lint/mypy/collection green; full-suite run pending keyring-safe execution (see Gate section). |

## Findings (confirmed production defects — deferred to a dedicated fix cycle)

All three were independently adversarially verified by code-trace (not test artifacts) and captured as `@pytest.mark.xfail(strict=True)` regression guards that will fail CI the moment the defect is fixed (forcing the mark's removal).

### Finding B — folder-vs-folder occupied-target move creates a duplicate `remote_id` index row
- **Test:** `test_fault_divergence.py::test_occupied_target_folder_move_creates_duplicate_id_row` (xfail strict).
- **Mechanism:** When a remote folder is moved onto a path already occupied by an untracked local folder, `_apply_remote_move` correctly bails (returns False), but `_check_download_conflict` then compares `content_hash "folder" == "folder"` and returns `Conflict.Identical` — it never checks whether an index row actually exists at the event path. `update_index_from_sync_event` inserts a **second** index row with the same `remote_id` (stale orphan at old path + new row at new path); the occupant is silently adopted instead of conflict-copied (contradicting `_apply_remote_move`'s own docstring). This is a **normal sync operation**, no test injection.
- **Root cause:** `_check_download_conflict` returning `Identical` for an untracked folder occupant (the `"folder" == "folder"` branch doesn't require a matching index entry to exist).
- **Fix (proposed):** in `_check_download_conflict`, an existing local folder with NO index row at the event path must be `Conflict.Conflict` (conflict-copy), not `Identical` — consistent with file-occupant behavior.

### Finding C — `get_index_entry_by_remote_id` has no uniqueness guard → stale orphan after move
- **Test:** `test_fault_divergence.py::test_duplicate_remote_id_rows_no_dangling_stale_row` (xfail strict). (`test_duplicate_remote_id_rows_no_crash` passes — it does not crash.)
- **Mechanism:** No `UNIQUE` constraint on `remote_id` (`models.py:491`, `index=True` only). `get_index_entry_by_remote_id` does `rows[0]` (first-wins, no ORDER BY). After a move, `_apply_remote_move` re-keys only the selected row, leaving the duplicate as a **stale orphan** that a later `get_index_entry_by_remote_id` can return — breaking subsequent moves and causing a spurious local delete on the next scan.
- **B feeds C in production:** Finding B is the operational path that *creates* duplicate `remote_id` rows with no test injection, making C's hazard reachable in normal operation.
- **Fix (proposed):** schema `UNIQUE` on `remote_id` — low-cost because the index is a derived cache rebuilt on version bump (no migration framework); converts silent corruption into a detectable error. Pair with the Finding B semantic fix for full closure.

### Finding (persistent-400) — unbounded reindex spin on a persistently-400 change feed
- **Test:** `test_fault_cursor.py::test_scenario_b_persistent_400_is_bounded` (xfail strict).
- **Mechanism:** `CursorResetError` (from `changes.list`→400) is caught only at `manager.py:561` (`_handle_sync_thread_errors`), which does `stop()` + `sync.reset_sync_state()` (cursor→"") + `autostart.set()`; `connection_monitor` then restarts. If the feed 400s for *every* token (including fresh ones from bootstrap), the manager loops: bootstrap → fresh cursor → 400 → reset → restart → … with **no circuit-breaker / max-attempts / backoff anywhere in `manager.py`**. Steady state is an infinite *slow* spin (~10s period, throttled only by `connection_check_interval`), never terminating.
- **Severity note:** the fix is **small and scoped** (not architectural in the redesign sense): a cross-restart counter on `SyncManager` (instance var) incremented in `_handle_sync_thread_errors`'s `except CursorResetError` and a give-up-after-N (stop without re-autostart + surface an error), reset on a healthy cycle. ~10-15 lines in `manager.py` (`__init__` + the except branch + a reset path).
- **Test faithfulness:** the xfail test *simulates* the manager restart cycle (bounded `range(MAX+1)` loop calling `reset_sync_state` + `download_sync_cycle`) rather than running the real manager thread loop; the simulation was verified faithful to the real catch→reset→restart→400 flow.

## Behaviors confirmed CORRECT (characterization, all teeth-verified)

These were potential defect sites that turned out correct; the tests pin them as regression guards (each proved non-vacuous by neutering the relevant production guard → test goes red → revert):

- **F2/finding-2 crash-replay:** evicted-cache delete re-resolved via `_index_path_for_remote_id`; create/rename replays idempotent (Task 3).
- **F7 idempotent create:** reserved-id + 409-fetch prevents duplicate on create-then-crash (Task 3, Task 5).
- **F3 move-by-remote_id:** stale-cache move resolves by id, not path (Task 4).
- **Change-feed all-pages-buffered crash-safety:** page-N fault never advances cursor past unapplied changes; finding-5 stale-echo skip holds across a real page break; fail_after redelivery idempotent (Task 2).
- **Download integrity:** truncate/flip/wrong-md5 all raise `DataCorruptionError`, leave no committed file or sidecar; mid-stream chunk fault transparently retried by `_retry_transient` + `MediaIoBaseDownload` range-reissue (Task 6).
- **F14 quota model:** 429 + `userRateLimitExceeded`/`rateLimitExceeded` retried & bounded; `dailyLimitExceeded` NOT retried (surfaces immediately as ConnectionError — confirmed correct); `storageQuotaExceeded` → `InsufficientSpaceError` (fatal); `_request_count` is a pure diagnostic with no client-side per-day budget (Task 7).
- **F5 cursor reset:** 400→CursorResetError→manager reindex (cursor→"") with stop-before-reset atomicity (cursor never partially advances) (Task 8).
- **F13 auth:** 401 and RefreshError both → `TokenRevokedError` (with `__cause__` chain), propagate FATAL-CLEAN through `download_sync_cycle` (not swallowed as per-item SyncError; `download_sync_cycle` has zero `except` handlers), cursor un-advanced, resumable after relink (Task 9).

## Plan-vs-implementation deltas

- **Task 1 exception factory:** `make_timeout()` returns `requests.exceptions.Timeout` (not `socket.timeout`) because `errorhandling.py` `CONNECTION_ERRORS` catches the former, not the latter — so retry-path tests exercise the real retry classifier.
- **Task 1 fail_after wiring:** the plan wired `fail_after` at create/delete; review extended it uniformly to all resource `.execute()` sites (notably `files.update` and `changes.list`, which Tasks 5 & 2 needed). `fail_kwargs` broadened to full call kwargs to prevent future predicate footguns.
- **`check_same_thread=False`:** the engine-driving fault modules set this on the in-memory sqlite path-cache connection because `apply_remote_changes` touches the cache from `do_parallel` worker threads.
- **Retry-sleep neutralization:** write/download/ratelimit modules mock `client._poll_event.wait`/`is_set` to make `_retry_transient` backoff instant and deterministic.
- **xfail form:** all findings use the `@pytest.mark.xfail(strict=True)` decorator form (a Task-4 review fix replaced an initial imperative `pytest.xfail()` form whose docstrings falsely claimed strictness; strict-guard XPASS behavior was verified).

## Tracking / backlog (non-blocking, for a future housekeeping pass)

- **`cof`-style sleep-neutralized fixture + `_seed_entry`/`_write_local_file`/`_wire_fake` boilerplate is now duplicated across ~5 fault modules.** Deferred deliberately (the existing conftest `client_on_fake`/`sync` fixtures aren't sleep-neutralized and use distinct config names to avoid collision). Candidate for a conftest factory / pytest plugin after Stage 7b, once the fixture contract is proven stable.
- **Task 9 `test_scenario_b4_teeth_401_vs_refresh_parity`** is weak teeth (trivially satisfied once A/B mappings hold) — first candidate to drop if test count is ever trimmed. `test_scenario_b1_teeth_refresh_error_not_raw` partially overlaps `test_errorhandling_auth.py`.
- **Unguarded `remove_configuration`** in a few standalone unit tests (no `try/finally`) — a config could leak if the body raises before teardown; same pattern as existing fault modules. A create/teardown fixture would fix it.
- **(final-review nits, non-blocking)** `test_fault_feed.py:36` hardcodes `_PAGE_SIZE = 100` instead of importing `changes._CHANGE_PAGE_SIZE` (comment keeps them in sync; would silently break the two-page-forcing logic if the constant changes). `test_fault_cursor.py` A2-teeth uses a `"sentinel-9999"` pageToken that would `ValueError` on `int()` if the armed fault ever failed to fire (safe today since the fail-check precedes `int()`; a valid-but-unused integer token would be sturdier). `_STUB_ACCOUNT` display-name/email fields differ cosmetically across modules (no assertion depends on them).

## Gate (Task 10) status

- **flake8:** CLEAN on all new test files + `fake_drive.py` (checked F/E402/E7/E9/C codes; project ignores `E203,E501,E704,W503,H306`).
- **mypy:** baseline parity — **trivially satisfied** since `git diff src/` is empty (zero production changes). The 20 pre-existing `src/cumulus` errors are the known baseline (unchanged from `d1990ad`).
- **Test collection:** 636 tests collected (vs 555 baseline = +81), clean.
- **Full-suite RUN:** ✅ **GREEN** — `pytest tests/offline --benchmark-disable -p no:randomly`: **633 passed, 3 xfailed, 0 failed, 0 unexpected skips** in 263s (4m23s). The 3 xfails are exactly the deferred findings (B, C, persistent-400), all reported as XFAIL (defects still present — strict guards correct). `test_performance` correctness assertion ran (via `--benchmark-disable`), hence 0 skips. 633 passed + 3 xfailed = 636 vs the 555 baseline (+81 net).
- **Keyring safety:** a REAL Google account was linked under `smoke-test` (`account_id=17820054878047320812`, `PlaintextKeyring` backend) during the run. Per the test-discipline guard (memory `feedback_keyring_test_collision`), its credentials were backed up to `~/.cumulus-smoke-test-cred-backup.json` (token verified == live) and the full keyring file copied to `~/.cumulus-keyring-backup.cfg` (both mode 0600, outside the repo) BEFORE the run. Post-run verification: the smoke-test account survived intact (account_id present, token unchanged) — no restore needed. Backups retained as insurance.

## Commits (this stage)

`d2ab19c`, `4e7c5b6`, `ad5eb18` (Task 1) · `5bd24d0`, `0f3d310`, `f6c2355` (Task 2) · `7e4174d` (Task 3) · `25bfdd5`, `14c091c` (Task 4) · `60ee51a`, `f2cbee4` (Task 5) · `25eca59`, `769ec94`, `3c0a729` (Task 6) · `8b71b09`, `7f55160` (Task 7) · `3dcd6ff`, `7d56cfd` (Task 8) · `5c954e3` (Task 9).
