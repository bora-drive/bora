# Stage 7a follow-up — Green the linked integration suite

> **For agentic workers:** this is a debugging/fix plan, not a greenfield TDD plan. Tasks are "establish baseline → fix by root-cause category → re-verify". Use checkbox (`- [ ]`) tracking. Execute against a **real Google test account** (throwaway `cumulus-test@arko.net` or equivalent).

**Goal:** Take the ported `tests/linked/integration/` suite (sync engine + manager) from its current partial state to fully green against a real Google Drive account.

**Status going in:** Stage 7a is code-complete; the fake-Drive, offline suite (470 passing), and **real-account *unit* client tests (16/16 passing)** are all validated. The **integration** suite is the remaining work: it was ported *blind* (no account at port time) and a first real-account run found **47 failures / 17 passed / 2 skipped** (32-minute run). One systematic cause (stale lock-exclusion path) has since been fixed; the true residual must be re-measured (Task 1).

**Context this builds on:**
- Spec: `docs/superpowers/specs/2026-06-04-stage-7a-test-infrastructure-design.md`
- Stage 7a plan + deviations: `docs/superpowers/plans/2026-06-04-stage-7a-test-infrastructure{,-deviations}.md`
- The integration suite lives in `tests/linked/integration/{test_sync.py,test_main.py,conftest.py}`; shared harness in `tests/linked/{conftest.py,lock.py}`.

---

## How to run (read first)

The suite needs a real account and a refresh token in the environment:

```bash
cd /home/exedev/maestral
export OAUTHLIB_INSECURE_TRANSPORT=1
export CUMULUS_TEST_REFRESH_TOKEN='<mint a fresh one>'   # see below
python -m pytest tests/linked/integration -q -p no:benchmark
```

- **The refresh token expires after ~7 days** (OAuth client is in Testing mode). It will almost certainly be dead next session — re-mint it. The minting helper was written at `/tmp/mint_test_token.py` (ephemeral — **recreate or commit it to `scripts/` as a first step**). It runs the project's paste flow (`cumulus.oauth.make_paste_flow`), prints an auth URL, you approve in a browser signed into the **test account** (NOT the `cumulus@arko.net` smoke account), paste back the `localhost:8080?code=…` redirect URL, and it prints the refresh token + the linked account email.
- The account must be a registered **test user** on the OAuth consent screen of the project owning `src/cumulus/resources/client_secret.json`.
- The account is a **throwaway** — the integration `m` fixture's `clean_drive_dir` trashes everything in the account root at each test's setup/teardown. That's expected.
- **A full integration run is ~32 minutes.** Iterate on subsets (`pytest "tests/linked/integration/test_sync.py::test_name"`) while fixing; do full runs only to confirm. Capture full output to a file — do **not** pipe through `tail` (it truncates tracebacks). Use `--tb=short --show-capture=no` for readable tracebacks; logs are very verbose otherwise.

---

## What is already known (confirmed this session)

**Validated, do not touch:** offline suite (470), real-account unit client tests (`tests/linked/unit/test_client.py` 16/16 — upload simple+resumable, download+md5, get_metadata, list_folder, change feed, move, remove, restore, make_dir, batch all PASS live). The client/writes/changes primitives work against real Drive. The integration failures are in the higher-level sync-engine/manager orchestration and (mostly) in the **test code**, not the primitives.

**Fixed already (commit `d0e03c6`):** `test_sync.py` had a stale `LOCK_PATH = "/test.lock"` (Dropbox-era); the real Drive lock sentinel is `.cumulus-test.lock`, so `assert_local_tree` never excluded it and the lock file polluted every exact-tree comparison. Now tracks `LOCK_NAME` from conftest. This already flipped `test_folder_tree_local` green; it likely clears a sizeable fraction of the 47 — **Task 1 re-measures**.

**Confirmed production bug (NOT a test bug):** `src/cumulus/main.py:971` `get_file_diff` calls `self.client.get_metadata(f"rev:{old_rev}", include_deleted=True)`. The `rev:…` path form is a leftover **Dropbox-ism**; Drive's `get_metadata` is path-cache-based and raises `NotFoundError`. Breaks `test_get_file_diff` and its 5 variants. This is a Stage 5/6 carryover — see Task 4 for the scope decision.

**Path-encoding lead (needs Task 2 resolution):** `path_codec.encode_name` URL-encodes *all* non-unreserved chars (e.g. `ó` → `%C3%B3`), so `FileMetadata.path_display` is the encoded virtual path. The download-direction failures (`'/f%C3%B3lder' not found locally`, nested `'/test_folder/sub_folder_2/sub_sub_file.txt' not found locally`, `different content for "/test_file.txt"`) center on how the **test helpers** (`assert_synced`/`assert_local_tree` in `test_sync.py`) map `path_display` to a local path via `m.to_local_path(...)`. Determine whether `to_local_path` decodes the encoding (if so the helpers are fine and the bug is elsewhere — e.g. nested-tree download timing) or not (helpers must decode, or it's a real `to_local_path` bug). The nested-file and content-mismatch cases may also be **timing** (`wait_for_idle`/`wait_until` not waiting long enough for deep trees / change-feed propagation) rather than encoding — verify before assuming.

---

## The 47 pre-fix failures, grouped by *hypothesized* cause

Re-measure in Task 1 — the lock fix (`d0e03c6`) likely already cleared several of these (every test that ends in an `assert_local_tree`/`assert_synced` exact-tree comparison was vulnerable to lock pollution).

**A. Path-encoding / download-direction (Task 2):**
`test_file_lifecycle[fólder]`, `test_folder_tree_remote`, `test_unicode_decomposed`, `test_unicode_conflict`, `test_case_change_local`, `test_case_change_remote`, `test_case_conflict` — likely the encoded-`path_display`-vs-local-FS helper issue and/or unicode normalization.

**B. Conflict-copy / replace-semantics naming (Task 3):**
`test_file_conflict_modified`, `test_file_conflict_created`, `test_selective_sync_conflict`, `test_remote_file_replaced_by_folder{,_and_unsynced_local_changes}`, `test_remote_folder_replaced_by_file{,_and_unsynced_local_changes}`, `test_local_folder_replaced_by_file{,_and_unsynced_remote_changes}`, `test_local_file_replaced_by_folder{,_and_unsynced_remote_changes}`, `test_move_to_existing_file`, `test_local_and_remote_creation_with_{equal,different}_content` — the Dropbox→Drive conflict-copy naming differs (`_local_cc_filename` / Drive autorename vs Dropbox server naming). Several were flagged at port time (see `test_sync.py` module docstring "flagged for E2").

**C. Generic exact-tree / lifecycle (likely largely fixed by `d0e03c6` — verify in Task 1):**
`test_file_lifecycle[test_file.txt]` (now shows "different content" — investigate: stale state vs. real), `test_local_indexing`, `test_mignore`, `test_excluded_folder_cleared_on_deletion`, `test_local_file_folder_deleted_event`, `test_parallel_deletion_when_paused`, `test_local_deletion_during_upload`, `test_rapid_local_changes`, `test_rapid_remote_changes`, `test_invalid_pending_download`, `test_out_of_order_indexing`.

**D. Local-FS error-injection (Task 5):**
`test_unix_permissions` (FileNotFound), `test_local_path_error`, `test_local_indexing_error`, `test_local_permission_error`, `test_long_path_error`, `test_symlink_error`, `test_symlink_indexing_error` — these inject local filesystem error conditions; may need Drive-shaped error mapping and/or are sensitive to the VM's filesystem (permissions, path-length, symlink support). Some may be environment-specific (note/skip with rationale if so, don't force-green).

**E. Manager / main (Task 3 or its own pass):**
`test_file_status` (`'unwatched' == 'error'` — the test's error-state setup doesn't translate to Drive), `test_restore` (assertion).

**F. Production bug (Task 4):** `test_get_file_diff{,_local,_unsupported_ext,_unsupported_content,_unsupported_content_local}` — the `main.py` `rev:` bug above.

---

## Tasks

### Task 1: Re-establish the true post-lock-fix baseline
- [ ] Mint a fresh `CUMULUS_TEST_REFRESH_TOKEN` (see "How to run"); confirm it links `cumulus-test@arko.net` (or the chosen throwaway), not the smoke account.
- [ ] Run the full integration suite capturing COMPLETE output to a file (no `tail`):
  `python -m pytest tests/linked/integration --tb=short --show-capture=no -p no:benchmark > /tmp/int_baseline.txt 2>&1`
- [ ] Produce the real residual failure list with one-line root cause per test. Re-bucket against categories A–F above (drop the ones the lock fix already fixed).
- [ ] Commit the residual list as an update to this plan (or a short notes file).

### Task 2: Resolve the path-encoding / download-direction failures (category A)
- [ ] Read `src/cumulus/main.py`/`sync.py` `to_local_path` and `to_remote_path*`: does the round trip decode `encode_name` output for the local FS? Write a tiny offline assertion (against the fake or pure functions) proving the intended contract.
- [ ] If the **test helpers** (`assert_synced`/`assert_local_tree`) misuse the encoded `path_display`, fix the helpers (decode for local-FS comparison). If `to_local_path` itself is wrong, that is a **production bug** — STOP, document it, and route to Task 4's scope decision.
- [ ] Separately rule out timing: confirm `wait_for_idle`/`wait_until` budgets are adequate for nested trees and change-feed lag before concluding "not found" is an encoding bug.
- [ ] Re-run category-A tests as a subset until green.

### Task 3: Fix conflict-copy / replace-semantics + manager assertions (categories B, E)
- [ ] For each conflict/replace test, compare the Dropbox-era expected name against the Drive engine's actual conflict-copy/autorename name (`_local_cc_filename`, `generate_cc_name`) and update the assertion to the Drive scheme. The `test_sync.py` module docstring already flags several of these.
- [ ] Fix `test_file_status` (determine how to produce a real `FileStatus.Error` state under Drive) and `test_restore`.
- [ ] Re-run subsets until green.

### Task 4: Production-bug scope decision — `get_file_diff` (category F)
- [ ] `main.py:971` `get_file_diff` uses the Dropbox `rev:` path convention. Decide scope: this is Stage 5/6 functionality, not test infra. Options: (i) fix it here (use `client.list_revisions` + revision download by `file_id`+`revisionId`, which `writes.py::_download_revision_to_tempfile` already does) and re-point the 6 tests; or (ii) defer to a Stage 5/6 follow-up, mark the 6 `test_get_file_diff*` tests `xfail` with a tracking reference, and keep the suite green.
- [ ] Whichever path: the 6 tests must end either passing (if fixed) or `xfail` (if deferred), not failing. Record the decision.

### Task 5: Local-FS error-injection tests (category D)
- [ ] Triage each: real Drive error-mapping gap → fix; environment-specific (VM permissions/path-length/symlink support) → guard with an appropriate `skipif`/`xfail` and a clear reason (do NOT force-green a test the environment can't support). The original Dropbox versions are the reference for intent.
- [ ] Re-run subset.

### Task 6: Final verification + close the gate
- [ ] Full run green (or green-modulo-documented-`xfail`/`skip`):
  `python -m pytest tests/linked -q -p no:benchmark` → unit 16 + integration all pass/xfail/skip-with-reason; 0 unexpected failures.
- [ ] Confirm offline still green (`pytest tests/offline -q`) and clean skip without creds (`unset CUMULUS_TEST_REFRESH_TOKEN; pytest tests/linked -q` → all skipped, 0 errors).
- [ ] Update `docs/superpowers/plans/2026-06-04-stage-7a-test-infrastructure-deviations.md` "Real-account gate" section: record PASS, the final counts, any `xfail`/`skip` with rationale, and any production bugs filed.
- [ ] Update project memory: Stage 7a **stage-complete**; note any deferred prod bugs (e.g. `get_file_diff`) for Stage 5/6 follow-up.

---

## Gotchas / notes for next session
- Don't pipe pytest through `tail` — it ate the tracebacks on the first run. Redirect full output to a file.
- `-p no:benchmark` avoids the benchmark plugin noise; logs are still very verbose — use `--show-capture=no`.
- The `m` fixture is function-scoped and does full clean+sync per test → each integration test is slow (tens of seconds). Subset aggressively while iterating.
- The session lock (`.cumulus-test.lock`) syncs into the mirror by design; assertions exclude it via `LOCK_PATH` (now correct). If you add new tree assertions, exclude it.
- Several tests were explicitly flagged "for E2" in the `test_sync.py` module docstring — start there for category B.
- Do not modify the validated layers (fake-Drive, `client.py`/`writes.py`/`changes.py`, offline tests, unit linked tests) unless a failure proves a real bug there (unlikely — they pass live).
