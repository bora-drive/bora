# Stage 7c — P0 Defect-Fix Deviations & Gate Evidence

**Date executed:** 2026-06-27
**Branch:** `google-drive-port`
**Plan:** `docs/superpowers/plans/2026-06-15-stage-7c-p0-defects.md`
**Spec:** `docs/superpowers/specs/2026-06-15-stage-7c-deferred-backlog.md`
**Workflow:** subagent-driven-development (fresh implementer per task + two-stage spec/quality review), mirroring the cluster-E / 7b-E2 cycle.

This document records where execution deviated from the plan's literal snippets, the
test re-authoring forced by the new schema constraint, one code-review suggestion
that was investigated and **deliberately rejected**, the accepted residuals, and the
final green-gate evidence. It is the definition-of-done artifact for the P0 block.

## Commit map (Tasks 1–5)

| Task | Defect | Commit | Summary |
| --- | --- | --- | --- |
| 1 | 7c-1 (finding B) | `f84cdb7` | `_check_download_conflict` conflict-copies an untracked occupant on an occupied-target folder move |
| 2 | 7c-1/7c-2 | `13b3f4c` | evict stale same-`remote_id` rows in `update_index_from_sync_event`; remove finding-B strict xfail |
| 3 | 7c-2 (finding C) | `6033935` | `UNIQUE` constraint on `IndexEntry.remote_id` |
| 4 | 7c-2 | `e529a80` | rewrite the two duplicate-row tests for the UNIQUE reality; remove finding-C strict xfail |
| 4b | docs | `3a1f063` | refresh the finding-B test docstring to fixed-state reality (stale "strict xfail" prose) |
| 5 | 7c-3 (persistent-400) | `35e0faf` | circuit-breaker for persistent `CursorResetError`; delete engine-level persistent-400 xfail; add two manager-level tests |
| 6 | gate fix | `9184545` | rewrite the file-case occupied-target move test for stale-row eviction (gate-discovered); refresh stale Scenario B comment (see §2.3b) |

All three `@pytest.mark.xfail(strict=True)` guards are gone (`grep -rn "pytest.mark.xfail" tests/offline/` → no matches). Each removal was verified RED-before-GREEN: the strict xfail XPASSes (would fail CI) the moment its defect is fixed, forcing the mark's removal.

---

## 1. Design deviations from the spec's one-line sketches

These were anticipated in the plan's "Design notes" section and confirmed during execution.

### 1.1 — 7c-1 narrowed, not the spec's literal "no index row ⇒ Conflict"

The spec proposed: *"an existing local folder with **no** index row at the event path must return `Conflict.Conflict`."* Taken literally this breaks the legitimate **first-sync merge** (a user already has an identical folder locally before linking; the remote folder event arrives with no index row and the engine *should* adopt it as `Identical`).

**Implemented (Task 1):** divert to `Conflict.Conflict` only when the occupant is a folder (`content_hash == "folder"`), there is no index row at the event path (`local_rev is None`), **and** the event's `remote_id` is already tracked at a *different* index path (`get_index_entry_by_remote_id(event.remote_id)` returns an entry whose `remote_path_lower` differs). When the id is not yet indexed anywhere, control falls through to the existing `Conflict.Identical` (first-sync merge preserved). Verified by running `test_sync.py` (40 tests) with no first-sync-merge regression.

### 1.2 — eviction + UNIQUE, not UNIQUE alone

The spec's one-liner (add a `UNIQUE` constraint) is insufficient on its own: the conflict-semantic fix conflict-copies the occupant but leaves the stale `/src` row, so a row-count assertion still sees two rows; and a bare `UNIQUE` would turn the second insert into a crashing `IntegrityError`, not "one row."

**Implemented (Tasks 2 + 3):** an **app-level eviction** in `update_index_from_sync_event` deletes any *other* row holding the same `remote_id` at a different path before inserting (this is the piece that actually collapses to one row, and it works on existing databases). The schema `UNIQUE` constraint is a defense-in-depth backstop that converts any *residual* silent corruption into a detectable error.

**`CREATE TABLE IF NOT EXISTS` caveat:** the `UNIQUE` constraint only takes effect on a **freshly-built** index DB — `CREATE TABLE IF NOT EXISTS` does not alter existing tables. This is acceptable because the project is pre-release (all dev/test DBs are fresh or `:memory:`) and the index is a derived cache rebuilt on version bump; the app-level eviction enforces uniqueness on any existing DB regardless of the schema.

---

## 2. Test re-authoring (forced by the new constraint)

### 2.1 — `IntegrityError` → `DatabaseError` translation (affects Tasks 3 & 4)

The plan's Task 3/4 snippets asserted `pytest.raises(sqlite3.IntegrityError)` around a `_seed_entry(...)` call. **That is wrong**: `_seed_entry` wraps its insert in `with sync._database_access():`, and `_database_access` (`src/cumulus/sync.py` ~L1726) deliberately catches `sqlite3.IntegrityError` and re-raises it as `cumulus.exceptions.DatabaseError`. So a duplicate insert through `_seed_entry` raises `DatabaseError`, not the raw sqlite3 error.

- **Task 3** added `test_remote_id_unique_constraint_rejects_duplicate`, which asserts the **raw** `sqlite3.IntegrityError` by *bypassing* the wrapper — it issues the duplicate via `sync_engine._index_table.update(duplicate)` directly. This is the precise test of the schema-level constraint.
- **Task 4** rewrote `test_duplicate_remote_id_rows_no_crash` → `test_duplicate_remote_id_rows_rejected_at_db`, which goes through `_seed_entry` and therefore asserts `pytest.raises(DatabaseError)`, plus a chained-cause assertion (`excinfo.value.__cause__ or excinfo.value.__context__` is a `sqlite3.IntegrityError`) — verified to hold at runtime because the `DatabaseError` is raised from inside the `except sqlite3.IntegrityError` handler (implicit `__context__` chaining), then asserts exactly one row survives.

### 2.2 — finding-C xfail replaced by a re-key test

`test_duplicate_remote_id_rows_no_dangling_stale_row` (the finding-C strict xfail) seeded two duplicate rows directly — impossible under the new constraint. Task 4 replaced it with `test_remote_id_move_via_create_rekeys_not_duplicates`, which drives the eviction+rekey through `update_index_from_sync_event` (not a direct seed), asserting one row at the new path, the old path gone, and `get_index_entry_by_remote_id` resolving unambiguously.

### 2.3b — file-case occupied-target test rewrite (discovered by the green gate)

The full-offline-suite gate (Task 6) surfaced **one** test the plan did not anticipate: `tests/offline/test_sync.py::test_remote_move_with_occupied_target_keeps_index_and_conflicts` (the **file** analog of §2.3's folder sibling). It asserted the old `/a.txt` index row *survives* an occupied-target file move — the pre-7c duplicate-`remote_id` behavior the Task 2 eviction now eliminates. Task 2's implementer never ran `test_sync.py` (it only ran the divergence file), so this was caught only at the gate — which is exactly the gate's purpose.

Rewritten (commit `9184545`, renamed to `test_remote_move_with_occupied_target_evicts_stale_index_and_conflicts`) to assert the fixed behavior: the old row is evicted, the moved file's id resolves to exactly one row at `/b.txt`, and — crucially — `/b.txt`'s index row carries the **moved file's** content hash (`_md5(data)`), the occupant is conflict-copied, and `event.status is Conflict`. The original guard's real concern (never re-key onto the occupant → never overwrite the remote with the occupant's content) is now asserted **more** explicitly than before. An independent adversarial review confirmed the rewrite is not masking a regression by injecting a genuine re-key-onto-occupant bug and confirming the test still fails. The now-stale "Scenario B" comment block in `test_fault_divergence.py` (which described the fixed finding B as a live defect and referenced the old test name) was refreshed in the same commit.

### 2.3 — sibling-test rewrite (Task 2, unplanned but necessary)

`test_occupied_target_folder_move_no_rekey_of_old_row` previously asserted the **old `/src_folder` row must survive** — which *is* the finding-B buggy behavior the eviction now eliminates. Once the eviction landed, that assertion would fail. It was rewritten to assert the corrected post-fix semantics (exactly one row, re-keyed to `/dst_folder`, `/src_folder` evicted, occupant conflict-copied). Its "teeth" twin `test_occupied_target_folder_move_no_rekey_teeth` manipulates the DB directly (not via `_create_local_entry`), so the eviction never runs in it; left untouched. Spec-reviewer judged this a strengthening, not scope creep.

### 2.4 — engine-level persistent-400 xfail deleted in favour of manager-level tests

The spec's guard `test_scenario_b_persistent_400_is_bounded` (in `test_fault_cursor.py`) *simulated* the manager restart cycle at the engine level and could never observe a `SyncManager` counter. Task 5 deleted it and added two faithful manager-level tests in `test_manager.py` that drive the real `_handle_sync_thread_errors` context manager:
- `test_persistent_cursor_reset_trips_circuit_breaker` — the first `MAX_CURSOR_RESETS` resets re-arm autostart; the next trips the breaker (no autostart, ERROR notification).
- `test_cursor_reset_counter_resets_on_healthy_cycle` — a healthy exception-free pass clears the counter via the `else:` clause.

A structural note: the plan's snippet placed the `else:` clause right after `except CursorResetError:`, which is a `SyntaxError` because `_handle_sync_thread_errors` has further `except (ConnectionError, ServerError)` and `except Exception` clauses. The `else:` was correctly placed at the end of the try/except chain (after `except Exception`).

---

## 3. Rejected code-review suggestion (investigated, deliberately not applied)

The Task 5 code-quality reviewer raised a Minor suggestion: reset `self._cursor_reset_count = 0` inside `SyncManager.start()` so a same-process manual resume gets a fresh retry budget.

**Rejected after tracing the control flow.** `connection_monitor` calls `self.start()` exactly when `autostart.is_set()` (`manager.py:393-394`), and the **within-budget** `CursorResetError` path *re-arms* `autostart` to schedule the reindex restart. Therefore `start()` is the same code path the autostart-driven reindex loop runs through. Resetting the counter in `start()` would zero it on every reindex restart, so the counter could never accumulate and the circuit-breaker would **never trip** — defeating the entire fix.

The current design is correct precisely because the counter is reset **only** on a healthy (exception-free) worker pass via the `else:` clause — the true "feed recovered" signal. A manual resume after a trip behaves acceptably: if the feed is fixed, the first healthy cycle resets the counter; if it is still 400ing, re-stopping immediately is the desired outcome, consistent with the user-facing "Please restart syncing" message.

---

## 4. Accepted residuals

- **On-disk orphan at the old path (occupied-target move).** On the occupied-target move, the index is corrected immediately (one row at the new path), but the now-orphaned item *on disk* at the old path is left for the next local scan to reconcile. For a **folder** occupant this self-heals quietly. For a **file** the orphan at the old path has no index row, so the next local scan treats it as a new local file and **re-uploads it as a duplicate** in the remote ("resurrection"). This is more user-visible than the folder case (a spurious duplicate file appears in Drive) but is **not data loss** — the moved file is correct at the new path and the occupant is safely conflict-copied. It is the accepted cost of eliminating the duplicate-`remote_id` defect (finding C: nondeterministic resolution with potential spurious *delete*), which is the more severe failure. **Recommended follow-up (out of 7c scope):** make the occupied-target download fallback delete the source file on disk at the old path once the move completes (the file moved away from it), eliminating the resurrection — tracked as a candidate for the next touch of the move/download path; deferred because deleting local files in the fallback path is higher-risk and warrants its own TDD cycle.
- **Stale folder on disk at the old path** — see above; consistent with the pre-existing `_apply_remote_move` occupied-branch comment.
- **`UNIQUE` only on fresh DBs** — see §1.2 caveat; mitigated by the app-level eviction.
- **"Consecutive" cursor-reset counting** — a non-cursor error (ConnectionError/ServerError/generic) between two `CursorResetError`s neither increments nor resets the counter; only a healthy worker pass resets it. This is intentional: a transient connection blip is not evidence the change feed recovered, so it should not re-arm the breaker's budget.

---

## 5. Spec-item status updates

- **7c-1 (finding B)** → **FIXED** (`f84cdb7` + `13b3f4c`).
- **7c-2 (finding C)** → **FIXED** (`13b3f4c` eviction + `6033935` UNIQUE + `e529a80` tests).
- **7c-3 (persistent-400)** → **FIXED** (`35e0faf`).
- **7c-10 (transient duplicate `remote_id` rows)** → **CLOSED** by the 7c-1/7c-2 work, as the spec anticipated.
- P1 (7c-4 root_id persistence; 7c-5 unshare; 7c-6 headless paste-redirect) and P2 (7c-7…7c-9) remain **deferred** per the spec; out of scope for this P0 block.

---

## 6. Final gate evidence

- **Offline suite (excluding load tests):** `python -m pytest tests/offline --ignore=tests/offline/test_load_initial_sync.py -q --benchmark-disable` → **642 passed, 0 failed, 0 xfailed, 0 errors** (259.94s). This is the authoritative green result for everything Stage 7c touched; vs. the 7b-E3 baseline (639 passed / 3 xfailed) the 3 strict xfails are gone (2 flipped to passing, 1 deleted and replaced by 2 manager-level tests) and the gate-fix test (`9184545`) passes.
  - **Gate-discovered regression (now fixed):** the first full-suite run reported `1 failed` — `test_remote_move_with_occupied_target_keeps_index_and_conflicts` — root-caused to the file analog of the §2.3 folder sibling and fixed in `9184545` (see §2.3b). Re-run green.
  - **Load tests (`test_load_initial_sync.py`, 5 tests):** all pass on clean state, exercising the new eviction path during bulk initial sync (including the 10k storm + crash-resume variants). Getting there hit two layers of **pre-existing test-infra fragility**, both traced to a Task-1 implementer subagent that ran these slow tests in the background and was killed when it ended its turn (the controller, not subagents, must run multi-minute tests):
    1. `FileExistsError` at setup — the fixture uses a fixed `get_home_dir()`-relative `local_dir` and `os.mkdir` with no defensive pre-clean, so a leftover directory from the killed run blocked startup. Cleared via the project's own `delete()` cleanup util.
    2. After clearing, the first full-file run reported `test_build_tree_shape` failing its `index_count == 100` assertion. Root cause: the index DB is a **persistent file** at `get_data_path("cumulus", "<config>.db")` (file-based by design, required by the crash-resume tests), and the killed run had left a **partial index DB**. `test_build_tree_shape` runs first and opened the contaminated DB, inheriting extra rows. Its teardown then deleted the DB, so the other 4 tests — and the test in isolation — passed. Confirmed NOT a 7c defect by: (a) isolated pass, (b) the 3 fast tests passing together on clean state with `test_build_tree_shape` first, (c) the 4 storm/crash tests passing even in the contaminated run, (d) no leftover `*load-initial-sync*` artifacts after teardown. A clean full-file re-run confirms all 5 green. **Recommended test-infra follow-up (out of 7c scope):** make the fixture defensively pre-clean both `local_dir` and the data-path `.db` at setup (e.g. `delete()` before `os.mkdir`) so a killed prior run can't contaminate the next.
- **mypy:** `python -m mypy src/cumulus` → **20 errors in 6 files** — parity with the pre-7c baseline (commit `5626e4e`); the changes added no new annotations needing relaxation.
- **flake8:** `python -m flake8 src/cumulus/sync.py src/cumulus/models.py src/cumulus/manager.py` → **no new violations**. The project config (`pyproject.toml [tool.flake8]`) ignores `E203,E501,E704,W503,H306`. The two remaining non-E501 findings (`manager.py:10 F401 'os' unused`; `sync.py W293 blank-line whitespace`) are **pre-existing** at base commit `5626e4e` (the W293 line merely shifted 1483→1503 from Task 2's added lines).
- **xfail check:** `grep -rn "pytest.mark.xfail" tests/offline/` → no matches; all three strict xfails removed.

**Definition of done:** ✅ all three `strict=True` xfails removed (defects fixed, RED-before-GREEN verified per task), offline suite green, mypy/flake8 at baseline parity, this deviations file written.
