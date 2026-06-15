# Stage 7b-E3 — 10k-File Initial-Sync Load Test: Deviations & Findings

**Date:** 2026-06-15
**Plan:** `docs/superpowers/plans/2026-06-15-stage-7b-e3-load-test.md`
**Spec:** `docs/superpowers/specs/2026-06-15-stage-7b-e3-load-test-design.md`
**Branch:** `google-drive-port`. Pre-stage HEAD: `ca33544` (the E3 design-spec commit). Execution: subagent-driven-development (fresh implementer per task + two-stage spec/quality review per task; the two slow 10k scenarios were run by the controller out-of-band because each takes ~11–14 min and would stall an implementer subagent).

## Headline

- **Zero production changes.** `git diff ca33544 HEAD -- src/` is empty. Pure test-addition + fake-infra stage, exactly as anticipated by spec §2 (E3 is tests-only). No §8 triage fix was needed — both 10k scenarios passed the hard correctness gate on the first correct run.
- **No findings / no defects.** The 10k initial sync indexed completely and uncorrupted under both a rate-limit storm and a crash/resume. Nothing was xfail-deferred.
- **+8 tests, +1 recurring fake-Drive fault hook, +1 pytest marker.** 6 non-slow tests added to the normal offline suite; 2 slow `load`-marked scenarios run on explicit invocation.

## Per-task summary

| Task | Files | Tests | Outcome |
|---|---|---|---|
| 1 | `tests/fake_drive.py`, `tests/offline/test_fake_drive.py` | +3 | `fail_every(key, n, exc)` recurring fault hook + `_consume_fail` extension. Review nits applied: `n >= 1` `ValueError` guard (+ its test) and a single-rule-per-key docstring note. No defects. |
| 2 | `pyproject.toml`, `tests/offline/test_load_initial_sync.py` | +1 | Module scaffold: `load` marker, `sync_engine` fixture, O(N) `build_tree` (direct `FakeDrive._insert`), `_assert_complete_index`, clean-run `test_build_tree_shape`. No defects. |
| 3 | `tests/offline/test_load_initial_sync.py` | +2 | S1 rate-limit storm. `_run_storm` + non-slow `test_s0_storm_small` + slow `test_s1_storm_load`. Review nits applied (assert-message wording, `_peak` comment). No defects. |
| 4 | `tests/offline/test_load_initial_sync.py` | +2 | S2 crash/resume idempotent re-index. `_run_crash_resume` + non-slow `test_s0_crash_resume_small` + slow `test_s2_crash_resume_load`. Review nit applied (cased-path lookup in the partial-state integrity loop). No defects. |
| 5 | this file | — | Gate + deviations. |

Commits: `f505cfe`, `2539306` (Task 1); `1cf7f55` (Task 2); `599b48b`, `ce624a0` (Task 3); `820cacc`, `20e4488` (Task 4).

## Gate evidence

- **Zero production changes:** `git diff ca33544 HEAD -- src/` → empty (0 lines).
- **flake8** (`python -m flake8 --extend-ignore=E203,E501,E704,W503,H306 tests/fake_drive.py tests/offline/test_fake_drive.py tests/offline/test_load_initial_sync.py`): CLEAN.
  - Note: the project configures flake8 via `[tool.flake8]` in `pyproject.toml` using the `flake8-pyproject` plugin, which is **not installed in this VM**. A bare `python -m flake8` therefore ignores the pyproject `ignore` list and reports pre-existing E203/E501 across the repo. The explicit `--extend-ignore` invocation above reproduces the project's configured ignore list and is the correct gate command here.
- **mypy** (`python -m mypy src/cumulus`): 20 errors in 6 files — **identical to the pre-E3 baseline**. Parity holds by construction: src is byte-for-byte unchanged since `ca33544` (the src diff is empty), so the mypy result cannot differ from baseline. The new test files are not in the mypy target set (consistent with prior stages' "src baseline parity").
- **Normal offline suite** (`python -m pytest tests/offline -m "not load" --benchmark-disable -q`): GREEN — **639 passed, 3 xfailed, 2 deselected** (the two `load` scenarios) in ~313s. This is the E2 baseline (633 passed / 3 xfailed) **+6 non-slow E3 tests**; the 3 inherited xfails are unchanged.
- **Slow load run** (`python -m pytest tests/offline/test_load_initial_sync.py -m load --benchmark-disable -q`): both scenarios GREEN, run individually by the controller:
  - **S1 storm** (`test_s1_storm_load`, 2000 folders + 8000 files): PASS. Wall-clock 663.9s (a first correct run measured 650.1s; both well under the recalibrated 1800s hang guard). Storm fired **1335** times — `files.list` faults=2, `files.get_media` faults=1333 (instrumented run) — proving the `_retry_transient` backoff path was exercised hundreds of times across the 10k sync. Hard correctness gate (`_assert_complete_index`) passed: all 10k rows present, one row per `remote_id`, every file md5-verified on disk, folder hierarchy correct, `did_finish_indexing` True, cursor advanced off `""`. Peak python allocation under the 512MB ceiling.
  - **S2 crash/resume** (`test_s2_crash_resume_load`, 2000 folders + 8000 files, page_size 1000, crash after 3 applied pages): PASS in 818.9s (~13m38s, under the 1800s guard). Partial-state gate held after the injected crash (cursor still `""`, `did_finish_indexing` False, partial non-duplicated index, applied files intact on disk); resume re-indexed from scratch idempotently to a complete, uncorrupted final state. Peak allocation under the 512MB ceiling.

## Soft-guard calibration (spec §11)

- `_HANG_GUARD_SECONDS`: planned at 300.0; **recalibrated by the controller to 1800.0** after a real, fully-correct S1 run took ~650s wall-clock. The 300s value produced a false failure on a *correct* run — exactly the slow-machine false-failure the spec warned about. The elapsed time is measured with `tracemalloc` tracing active, which inflates it well beyond the untraced runtime; 1800s ≈ 2.7–3× the observed runtime, so a genuine deadlock still trips the guard while a slow-but-correct run does not. This is a documented soft guard, not a performance gate (the hard gate is correctness). Rationale is also in the code comment at the constants.
- `_MEMORY_CEILING_BYTES`: 512MB, unchanged. Both 10k runs passed under it. This is a generous absolute leak ceiling (catches O(N) duplication beyond the one expected in-memory listing buffer per spec §5.2), not a sublinearity claim — the bootstrap buffers the full flat listing in memory by design.

## Plan-vs-impl deltas

1. **Task 1 gained a third test + a guard** (`fail_every` rejects `n < 1` with `ValueError`) from the code-quality review, beyond the two tests the plan specified. Cheap robustness; no behavior change for valid use.
2. **Hang guard 300 → 1800** (above). The plan wrote 300.0 as a plan-time guess and explicitly reserved recalibration for the controller (plan Task 3 Step 4 / spec §11).
3. **Review-nit polish** applied across Tasks 3–4 (assert-message wording to match the `>=` operator; a comment clarifying `_peak` spans the md5-readback; cased-path lookup so the partial-state integrity check can't silently skip a file). All behavior-equivalent for the all-lowercase load-test data.
4. **Operational note:** the slow tests share one fixture dir (`~/load_initial_sync_dir`) and config (`load-initial-sync-config`). A killed slow run leaves that dir and the on-disk index DB behind, which surfaces as a `FileExistsError` at fixture setup or a stale `index_count` on the next run. Recovery: `rm -rf ~/load_initial_sync_dir` + `remove_configuration("load-initial-sync-config")`. The two slow tests cannot run concurrently (shared dir/config).

## Test-discipline guards observed

- Never ran `tests/linked` (needs a real account).
- Did not run `tests/offline` while any real smoke account was linked (no smoke account was linked during this stage; keyring-collision guard respected).
- The slow `load` run is part of the E3 gate, not the per-task green loop (the green loop uses `-m "not load"`).

## Real-account smoke (documented, deferred to Stage-8 release smoke)

Out of E3 scope to execute (spec §10). In-memory faults model Drive's *responses* (429 / rate-limit-403), not server-side eventual consistency, revision merging, or cross-request races — inherently unmodellable in-memory and routed to real-account smoke. Procedure for Stage-8:

1. **Back up first.** Before any offline-suite interaction, back up the smoke account's credentials and keyring file OUTSIDE the repo, mode `0600` (the keyring-backup guard — a keyring collision can wipe the linked account; the backup is the recovery path).
2. Provision ~N files in the smoke test account (a few thousand small files in a nested tree is sufficient to exercise multi-page listing + bulk download).
3. Link the account and run an initial sync.
4. Verify all entries indexed **and** downloaded: one index row per `remote_id`, every file's on-disk bytes match Drive's `md5Checksum`, folder hierarchy matches, no duplication.
5. After the run, verify the smoke account survives intact; restore from the backup if the keyring collision damaged it.
