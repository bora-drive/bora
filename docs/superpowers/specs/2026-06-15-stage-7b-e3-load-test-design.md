# Stage 7b-E3 — 10k-File Initial-Sync Load Test (design)

**Date:** 2026-06-15
**Branch:** `google-drive-port`
**Predecessor:** 7b-E2 fault-injection complete (Task 9 `5c954e3`, gate `4c4c7a4`);
offline suite GREEN (633 passed / 3 xfailed / 0 failed, `--benchmark-disable`),
mypy at baseline parity, zero production changes in E2.
**Master-plan reference:** `2026-04-30-staged-port-plan-design.md` §5.7, item **E3**.

## 1. Context & decomposition

Stage 7b bundles three deliverables, split into three sequential sub-stages
each with its own spec → plan → execute cycle (per the 2026-06-14 brainstorm):

1. **7b-E2** — fault-injection tests *(complete)*
2. **7b-E3** — 10k-file initial-sync load test *(this spec)*
3. **7b-deferred** — the cluster-E items punted to 7b *(future spec)*

This document covers **7b-E3 only**.

## 2. Goal

A **hermetic, in-memory 10k-file initial-sync load test** that drives the
cumulus sync engine through a full initial indexing of a ~10,000-entry Google
Drive tree against the fake-Drive, under a **rate-limit storm**, and proves the
sync completes **correctly at scale**. This validates the rate-limit-backoff
redesign from REVIEW issue 3 — which is now "no client-side rate budget;
`_retry_transient`'s exponential backoff absorbs 429 / rate-limit-403"
(client.py:99-106) — by exercising that backoff path hundreds of times across a
real 10k-entry sync and asserting the end state is complete and uncorrupted.

The test's **center of gravity is correctness-at-scale**, not throughput
benchmarking. Performance is bounded only by soft sanity guards (a generous
hang-guard timeout and a generous absolute memory ceiling); the hard gate is
correctness.

**Non-goals:**
- Throughput / wall-clock performance benchmarking (no perf regression gate;
  the existing `test_clean_local_events::test_performance` benchmark is a
  separate, local-event-cleaning microbenchmark and is untouched here).
- A real-account run. E3 is fake-Drive only; a real-account smoke procedure is
  *documented* and *deferred* to Stage-8 release smoke (§9).
- The 7b-deferred bucket items, and any server-side-eventual-consistency faults
  that cannot be modelled in-memory.
- Any new product behavior. E3 is tests only. If the load test exposes a
  genuine defect, it is fixed under the §8 triage protocol; otherwise zero
  production changes are expected (as in E2).

## 3. Approach

**One dedicated module, a shared tree-builder, two scenario tests, plus a tiny
non-slow harness-guard** (brainstorm approach A).

- New file `tests/offline/test_load_initial_sync.py`, following the E2
  fault-module conventions (the `sync_engine` fixture + `_wire_fake` +
  `_seed_entry` / `_write_local_file` helpers + the `_poll_event` Mock
  sleep-neutralization established across `test_fault_*.py`).
- A `build_tree(...)` helper bulk-seeds the fake-Drive into a realistic nested
  shape and returns the expected state for assertions.
- Two slow-marked tests at N=10k — **S1 rate-limit storm** and **S2 crash/resume
  idempotent re-index** — kept as separate, independently-debuggable functions.
- **One tiny non-slow variant at N≈150** exercising the same code paths so
  harness/assertion regressions surface in the normal green loop, not only at
  the gate.

Rejected alternatives: a parametrized `N × fault-profile` scale matrix (the
crash/resume flow does not parametrize cleanly against the storm flow or share
assertions — same reason E2 rejected a scenario-matrix harness), and folding
the load tests into `test_clean_local_events.py` / an E2 fault module (mixes an
end-to-end sync load test into modules scoped to something else).

## 4. Real code surfaces under test

- **`src/cumulus/sync.py`** — `download_sync_cycle` (the initial-index driver:
  empty-cursor reset, chunked apply, `indexing_counter` / `did_finish_indexing`
  state, per-chunk `gc.collect()`, final cursor commit), `apply_remote_changes`
  (thread-pool classification + `do_parallel` folder/file/delete application),
  `_create_local_entry` (per-item download + index write).
- **`src/cumulus/client.py`** — `list_folder_iterator` (full recursive listing:
  `files.list` pagination, full in-memory buffer, path-cache populate, paged
  yield with cursor on the final page only), `download` / `get_media`,
  `_retry_transient` (the backoff under test).
- **`src/cumulus/path_cache.py`** — `populate_from_listing`, `id_to_path` at
  10k-entry scale.
- **`tests/fake_drive.py`** — paginated `files.list`, `get_media`, seeding
  helpers, and the fault hooks (extended minimally if needed — §6).

## 5. Mechanics that shape the assertions

Two behaviors of the initial-index path are load-bearing for the design and are
asserted **as they actually behave**, not as an idealized model:

### 5.1 Cursor semantics — initial index does not checkpoint a mid-stream offset
`list_folder_iterator` yields `cursor=""` for every page **except the last**,
which yields the real `startPageToken` (client.py:1116-1132). Therefore
`download_sync_cycle` keeps `remote_cursor == ""` until the entire index
completes, and the empty-cursor branch (sync.py:3041-3044) resets
`indexing_counter` / `did_finish_indexing` on every entry while the cursor is
still empty. Consequence: a **mid-index crash restarts the full listing on
resume** — there is no resume-from-offset for the initial full index. S2 thus
validates **idempotent re-indexing after interruption** (already-applied items
are Skipped / no-op on the re-run), which is the real, correct behavior — not
resume-from-offset.

### 5.2 The full listing is buffered in memory by design
`list_folder_iterator` buffers the entire flat listing before processing
(client.py:1050-1052, "acceptable for the v1 scale target"). Memory is therefore
~O(N) at the listing layer **by design**. A "sublinear memory" assertion would
be wrong. The memory soft-bound (§7) is a **generous absolute ceiling** that
catches a runaway/leak (O(N) duplication beyond the one expected buffer, or
unbounded apply-layer accumulation) — not a sublinearity claim. The chunked
apply + `gc.collect()` in `download_sync_cycle` keeps the apply layer flat *on
top of* that one buffer.

### 5.3 Deterministic backoff
`_retry_transient` backs off via `self._poll_event.wait(sleep_for)`
(client.py:420). With hundreds of injected faults, real 1s→32s sleeps would make
the run take hours. E3 reuses the E2 neutralization: `client._poll_event` is a
`Mock` whose `wait` and `is_set` return `False` ("not cancelled, not shut
down") — backoff becomes instant and deterministic with no behavior change.

## 6. Components

| Component | Role |
|-----------|------|
| `test_load_initial_sync.py` | New module; the two scenarios + harness-guard. |
| `sync_engine` fixture | In-memory `PathIDCache` (`check_same_thread=False`), real temp `sync_path`, `_wire_fake`, `_poll_event` Mock neutralization. Mirrors `test_fault_crash.py`. |
| `build_tree(fake, n_folders, n_files)` | Bulk-seeds the fake-Drive into a nested tree (folders spread across a bounded depth; tiny **distinct** file contents so md5s differ and per-file integrity is checkable). Returns the expected `{path: md5}` (files) and folder set for assertions. |
| Recurring-fault arming | The storm needs "fail every Nth call" on `files.list` and `files.get_media`. Existing hooks (`fail_next` / `fail_after` / `fail_on_call` / `fail_when`) are one-shot / single-predicate. If a stateful `fail_when` predicate is awkward, add a **minimal** recurring hook (e.g. `fail_every(key, n, exc)`) to `fake_drive.py` plus a focused test of the hook. Decided at plan time; smallest viable addition. |

## 7. Scenarios & assertions

### S1 — initial sync under rate-limit storm (slow; N=10k)
1. `build_tree` ~2k folders + ~8k tiny files.
2. Arm recurring 429 / rate-limit-403 on every Nth `files.list` page-fetch **and**
   every Nth `get_media` download.
3. `sync.download_sync_cycle()` from empty cursor.
4. **Assert (hard gate):**
   - all ~10k index rows present at correct paths; exactly one row per
     `remote_id` (no duplicate rows);
   - every file downloaded to disk with bytes matching its expected md5;
   - folder hierarchy on disk matches the seeded tree;
   - `did_finish_indexing` is True, `indexing_counter` reset to 0,
     `remote_cursor` advanced to the real start-token (no longer `""`);
   - the storm actually fired (injected-fault counter > 0) — proving the
     backoff path was exercised, not silently skipped.

### S2 — crash / resume idempotent re-index (slow; N=10k)
1. `build_tree` ~2k folders + ~8k tiny files.
2. `sync.download_sync_cycle()`, interrupted after K applied pages (injected
   fault or `_cancel_requested`).
3. **Assert partial state:** `remote_cursor` still `""`, `did_finish_indexing`
   False, a partial-but-consistent index (no duplicate rows, no corrupt files
   among those applied).
4. Re-enter `sync.download_sync_cycle()`.
5. **Assert (hard gate):** final index complete & correct (same checks as S1);
   already-applied items Skipped / no-op (not duplicated, not re-corrupted);
   previously-downloaded files intact; `did_finish_indexing` True.

### S0 — harness guard (non-slow; N≈150)
A tiny run of **both** scenario shapes at N≈150 with the same assertions, in the
normal `tests/offline` suite, so harness/assertion regressions surface fast.

## 8. Defect-triage protocol (carried from E2)

Each scenario is a "try hard to break it" run. If it exposes a genuine defect in
the sync engine / client / path cache:
- **Small, clearly-correct fix** → fix in-task (TDD red→green), keep the test as
  a regression guard, record in the deviations file.
- **Large or behavior-changing fix** → stop, escalate to the user with a finding
  note; on the user's decision either fix-now or capture as
  `@pytest.mark.xfail(strict=True)` with the finding documented and a
  fix-now/defer decision recorded. (Mirrors the E2 §8 protocol; bounds
  scope-growth from fixes.)

## 9. Deliverables & gate

- `tests/offline/test_load_initial_sync.py` with S0 (non-slow), S1, S2 (slow).
- A registered `load` (slow) pytest marker; the 10k tests excluded from the
  normal `tests/offline` run and invoked explicitly. Invocation documented.
- Any `fake_drive.py` fault-hook addition (minimal) with its own focused test.
- **Gate:** flake8 CLEAN on new/changed test files; mypy at baseline parity
  (zero production changes unless a §8 fix is made); the normal offline suite
  GREEN with S0 included (no regression vs the 636-test E2 baseline); the slow
  S1/S2 run GREEN on explicit invocation, with the run command, pass count, and
  any injected-fault counts recorded.
- A deviations file
  (`docs/superpowers/plans/2026-06-15-stage-7b-e3-load-test-deviations.md`)
  recording plan-vs-impl deltas, any defect (fixed-in-task vs xfail-deferred
  with the user's decision), final pass counts, the slow-run invocation/result,
  and the **documented real-account smoke procedure** deferred to Stage-8.
- **Test-discipline guards (carried from E2 / cluster-E):** never run
  `tests/linked` (needs a real account); never run `tests/offline` while a real
  smoke account is linked (keyring collision); the slow load run is part of the
  E3 gate, not the per-task green loop.

## 10. Real-account smoke (documented, deferred)

Out of E3 scope to execute; documented in the deviations file for Stage-8
release smoke: provision ~N files in the smoke test account, link, run an
initial sync, and verify all entries indexed + downloaded with correct content
and no duplication — following the keyring-backup guard (back up the smoke
account's credentials and keyring file outside the repo, mode 0600, before any
offline-suite interaction; verify the account survives intact afterward).

## 11. Open risks

- **Fault realism.** In-memory faults model Drive's *responses* (429 /
  rate-limit-403), not server-side eventual consistency, revision merging, or
  cross-request races — inherently unmodellable in-memory, out of E3 scope, and
  routed to real-account smoke.
- **Soft-bound calibration.** The timeout and memory ceilings are sized to catch
  hangs/leaks with generous headroom, not to gate performance; they are tuned to
  the CI/dev machine at plan time and documented as soft guards so a slow
  machine does not produce false failures.
- **Scope growth from fixes.** Bounded by the §8 triage protocol (escalate large
  fixes rather than absorb them silently).
