# Cluster B+C — Case-Only Renames & Out-of-Order Change Application — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. REQUIRED for each fix: superpowers:systematic-debugging — these mechanisms are NOT yet pinned to a confirmed root cause; do Phase 1 (reproduce + pin) before writing any fix.

**Goal:** Make case-only renames (local and remote) and out-of-order remote change application converge correctly, removing three Stage-5 sync-engine xfails.

**Architecture:** Three related sync-engine ordering/identity bugs, none involving data loss:
- **B1 — local case-only rename** (`test_case_change_local`, xfail at test_sync.py:304): a live `/folder` → `/FOLDER` rename arrives from watchdog as a Dir/FileMovedEvent and is missed by the move handling, so `get_metadata("/folder")` afterward raises `NotFoundError`. Note the **while-stopped** variant (`test_case_change_local_while_stopped`, :340) passes — it goes through `_get_local_changes_while_inactive()` which decomposes the rename into create+delete — so the gap is specifically `_on_local_moved` (sync.py:2374) handling a case-only directory rename.
- **B2 — remote case-only rename** (`test_case_change_remote`, xfail at test_sync.py:376): a remote `/folder` → `/FOLDER` rename is not reflected locally; `get_metadata("/folder")` raises `NotFoundError`. This is the change-feed/download apply path (`_on_remote_folder`, sync.py:3668, and `correct_case`, sync.py:1287) not handling a name that differs only by case under the case-fold key.
- **C — out-of-order application** (`test_out_of_order_indexing`, xfail at test_sync.py:1434): applying remote changes children-before-parents via `_create_local_entry`/`_ensure_parent` leaves a child (`/parent/child_2`) uncreated. Related to the Drive change-feed's lack of parent-before-child ordering (see [dropbox-vs-drive-differences](../references/dropbox-vs-drive-differences.md) §14).

**Tech Stack:** Python, pytest, offline `SyncEngine` + `FakeDrive` (tests/offline), linked real-account suite (tests/linked/integration). Prefer deterministic offline reproductions; use the linked test only for final confirmation.

---

## File structure

- **Modify** `src/cumulus/sync.py` — `_on_local_moved` (B1, ~2374), `_on_remote_folder` / `correct_case` / `_correct_case_helper` (B2, ~1287/3668), `_create_local_entry` / `_ensure_parent` (C, ~3474/3518).
- **Create/extend** `tests/offline/test_sync.py` — deterministic regression tests for each.
- **Modify** `tests/linked/integration/test_sync.py` — remove each xfail once green.

---

### Task 1 (B1): Pin the local case-only-rename mechanism

**Files:** investigation only — `src/cumulus/sync.py:2374` (`_on_local_moved`), `tests/linked/integration/test_sync.py:305`.

- [ ] **Step 1: Reproduce and capture the decision path**

Run `test_case_change_local` with `--runxfail` against the real account in tmux, with the debug log isolated:
`: > /home/exedev/.cache/cumulus/cumulus-test-config-integration.log; python -m pytest "tests/linked/integration/test_sync.py::test_case_change_local" --runxfail -q`
Then grep the log for `/folder`, `/FOLDER`, `moved`, `correct_case`, `NotFoundError`.
Expected output: confirm whether `_on_local_moved` receives the Dir-moved event and what it does with a source/dest that are case-fold-equal (same folded key).

- [ ] **Step 2: Read `_on_local_moved` and the move-to-remote path**

Identify why a case-only move is not propagated: likely the folded source and destination keys are equal, so the move is treated as a no-op (the path cache keys by folded path) and the remote `name` is never updated via `files.update(name=...)`. Write the pinned mechanism into this plan as a one-paragraph note before proceeding.

- [ ] **Step 3: Decide the fix shape**

Likely fix: when a move's source and destination differ only by case (folded keys equal but cased names differ), still issue the remote rename (`client.move` / `files.update(name=...)`) and update the cache's cased name, rather than short-circuiting as a no-op. Record the exact approach here.

---

### Task 2 (B1): Deterministic test + fix

**Files:** `tests/offline/test_sync.py`, `src/cumulus/sync.py` (`_on_local_moved`).

- [ ] **Step 1: Write the failing test**

Wire `FakeDrive` into the offline `sync_engine` (pattern from tests/offline/test_writes.py). Seed `/folder` (folder) in the fake + index + on disk, then drive `_on_local_moved` with a Dir-moved event from `/folder` to `/FOLDER` and assert the fake's stored `name` becomes `FOLDER` and `get_metadata` resolves the item under the new casing.

```python
def test_local_case_only_rename_propagates_to_remote(sync_engine):
    """A /folder -> /FOLDER local rename must update the remote name, not no-op."""
    # ... seed FakeDrive folder + index + on-disk dir as /folder (see test_writes.py
    #     for the FakeDrive wiring and tests/offline/test_sync.py helpers) ...
    event = _dir_moved_event(sync_engine, "/folder", "/FOLDER")  # build per existing patterns
    sync_engine._on_local_moved(event)
    md = sync_engine.client.get_metadata("/FOLDER")
    assert md is not None and md.name == "FOLDER"
```

> The implementer fills the seeding to match FakeDrive's API; the assertion (remote name becomes `FOLDER`, resolvable under the new casing) is the contract.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/offline/test_sync.py::test_local_case_only_rename_propagates_to_remote -v`
Expected: FAIL (rename is short-circuited; remote `name` stays `folder`).

- [ ] **Step 3: Implement the minimal fix in `_on_local_moved`**

Per the Task 1 decision: detect case-only moves (folded keys equal, cased basenames differ) and still issue the remote rename + cache cased-name update.

- [ ] **Step 4: Run to verify it passes; run the offline sync suite**

Run: `python -m pytest tests/offline/test_sync.py -v`
Expected: new test PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add tests/offline/test_sync.py src/cumulus/sync.py
git commit -m "fix(cluster-b): propagate local case-only renames to the remote name"
```

---

### Task 3 (B2): Pin + fix the remote case-only-rename apply

**Files:** `src/cumulus/sync.py` (`_on_remote_folder` ~3668, `correct_case`/`_correct_case_helper` ~1287/1330), `tests/offline/test_sync.py`, `tests/linked/integration/test_sync.py:377`.

- [ ] **Step 1: Reproduce and pin**

Run `test_case_change_remote` with `--runxfail`; capture the log. Determine why a remote `/folder` → `/FOLDER` change-feed entry does not re-case the local folder (candidate: the apply path keys by folded path, finds the existing entry equal, and skips the local rename; or `correct_case` returns the stale casing). Write the pinned mechanism here.

- [ ] **Step 2: Write the failing offline test**

Seed `/folder` synced (fake + index + disk). Build a download SyncEvent for the same item with `remote_path="/FOLDER"` (same folded key, new casing) and apply it via `_create_local_entry`. Assert the on-disk directory is renamed to `FOLDER` and the index cased path is updated.

```python
def test_remote_case_only_rename_recases_local(sync_engine):
    # ... seed /folder synced ...
    event = _download_event(sync_engine, "/FOLDER", rev="folder", content_hash="folder",
                            change_type=ChangeType.Moved)  # or the type the feed delivers
    sync_engine._create_local_entry(event)
    assert os.path.isdir(sync_engine.to_local_path_from_cased("/FOLDER"))
    assert sync_engine.get_index_entry("/folder").remote_path_cased == "/FOLDER"
```

- [ ] **Step 3: Run (fail) → implement fix → run (pass)**

Fix likely in `_on_remote_folder` / case-correction: when the incoming cased name differs from the stored cased name under an equal folded key, perform a local rename and update the index cased path.
Run: `python -m pytest tests/offline/test_sync.py -v` → all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/offline/test_sync.py src/cumulus/sync.py
git commit -m "fix(cluster-b): apply remote case-only renames to the local folder + index"
```

---

### Task 4 (C): Pin + fix out-of-order application

**Files:** `src/cumulus/sync.py` (`_create_local_entry` ~3474, `_ensure_parent` ~3518), `tests/offline/test_sync.py`, `tests/linked/integration/test_sync.py:1434`.

- [ ] **Step 1: Pin the mechanism**

`_ensure_parent` (sync.py:3518-3546) already syncs a missing parent before a child by calling `get_metadata` + `_on_remote_folder`. Reproduce `test_out_of_order_indexing` with `--runxfail` and determine why `/parent/child_2` is left uncreated when changes apply children-first: candidates — (a) `_ensure_parent` resolves the parent but the second child's own apply is skipped because an earlier failed/short-circuited pass marked state inconsistently; (b) `get_metadata` for the not-yet-cached parent raises `NotFoundError` and the child is dropped rather than retried. Write the pinned mechanism here.

- [ ] **Step 2: Write the failing offline test**

Apply a batch of download events in reverse order (`/parent/child_2`, `/parent/child_1`, `/parent`) and assert all three exist locally and in the index afterward.

```python
def test_out_of_order_children_before_parent_all_created(sync_engine):
    # ... seed FakeDrive with /parent, /parent/child_1, /parent/child_2 ...
    for rp in ["/parent/child_2", "/parent/child_1", "/parent"]:
        sync_engine._create_local_entry(_download_event(sync_engine, rp, rev="...", content_hash="..."))
    for rp in ["/parent", "/parent/child_1", "/parent/child_2"]:
        assert os.path.exists(sync_engine.to_local_path_from_cased(rp))
        assert sync_engine.get_index_entry(rp.lower()) is not None
```

- [ ] **Step 3: Run (fail) → implement fix → run (pass)**

Fix likely in `_ensure_parent`/`_create_local_entry`: ensure a child whose parent is not yet present is reliably created after the parent is materialized (e.g., resolve the parent from the fake/cache rather than only `get_metadata`, or re-queue the child instead of dropping it on a transient parent miss). Keep the change minimal and within the existing `_tree_traversal` lock.
Run: `python -m pytest tests/offline/test_sync.py -v` → all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/offline/test_sync.py src/cumulus/sync.py
git commit -m "fix(cluster-c): create children applied before their parent"
```

---

### Task 5: Live confirmation + remove xfails

**Files:** `tests/linked/integration/test_sync.py` (xfails at :304, :376, :1434).

- [ ] **Step 1: Remove the three xfail markers.**

- [ ] **Step 2: Run each test ~6× against the real account (tmux; not concurrent with `tests/offline`).**

Run: loop `test_case_change_local`, `test_case_change_remote`, `test_out_of_order_indexing`.
Expected: all green, no failures.

- [ ] **Step 3: Full linked integration run.**

Run: `python -m pytest tests/linked/integration -q -ra`
Expected: the three xfails are gone; zero unexpected failures.

- [ ] **Step 4: Commit**

```bash
git add tests/linked/integration/test_sync.py
git commit -m "test(cluster-bc): un-xfail case-rename + out-of-order tests verified live"
```

---

## Self-review notes
- **Discipline:** each fix is gated on a pinned mechanism (Tasks 1/3/4 Step 1) — no fix before root cause, per systematic-debugging.
- **Placeholders:** the offline test bodies intentionally defer FakeDrive seeding detail to the implementer (the exact fake API is in tests/offline/test_writes.py / test_fake_drive.py); each test's *assertion* is fully specified and is the contract.
- **No data loss:** unlike cluster A, these are convergence/identity bugs; none silently discard user content.

---

## Execution notes (2026-06-10)

### Task 1 findings (B1 pin)

**Pinned mechanism.** The xfail reason ("delivered by watchdog as a FileMovedEvent and missed by the dir-move handling") is **refuted** by the live run. Watchdog delivers the rename correctly as a `DirMovedEvent(src_path='.../folder', dest_path='.../FOLDER', is_directory=True)` and the engine builds the right SyncEvent — the log shows `Filtered dir moved events: [<SyncEvent(direction=SyncDirection.Up, change_type=ChangeType.Moved, item_type=ItemType.Folder, remote_path=/FOLDER, remote_path_from=/folder)>]` followed by `Moving folders...` / `Moving /folder`. `_on_local_moved` (src/cumulus/sync.py:2373) then calls `self.client.move(remote_path_from, event.remote_path, autorename=True)` (sync.py:2430). The bug is in the client-side autorename emulation: `DriveWrites.move` (src/cumulus/writes.py:576) calls `_autorename_unique(new_parent_path, raw_new_name)` (writes.py:589), and `_autorename_unique` (writes.py:118-136) probes the destination with a bare `cache.path_to_id(parent_path + "/" + encoded_name)`, which is **case-folded** (path_cache.py:121-136, "lookup is case-insensitive (uses remote_path_fold)"). For a case-only rename `/folder → /FOLDER` the folded destination key equals the folded source key, so the probe hits *the very item being moved* and falsely reports a collision; autorename then picks `FOLDER (1)` and issues `files.update(name="FOLDER (1)")`. Back in the sync engine, `_handle_upload_conflict` (sync.py:2743) sees `md_new.name ("FOLDER (1)") != basename(event.local_path) ("FOLDER")` and mirrors the bogus conflict locally — log: `Upload conflict: renamed "/FOLDER" to "/FOLDER %281%29"` (09:00:11) — renaming the local dir to `FOLDER (1)` and removing the `/folder` index node. The remote now holds `FolderMetadata(name='FOLDER (1)', path_lower='/folder %281%29', path_display='/FOLDER %281%29')` (seen in the next change-feed fetch at 09:00:17), so the test's `m.client.get_metadata("/folder")` raises NotFoundError / the `md.name == "FOLDER"` assertion can never hold. The pattern is identical in all 3 pytest attempts (rerun blocks at log lines ~354/359, ~734, ~1067). Notably `path_cache.move` itself already solves this exact problem — it passes the moving item's id to `has_case_conflict(new_path, moving_id)` (path_cache.py:243-246), and `has_case_conflict` (path_cache.py:153-169) explicitly does not treat the same `file_id` as a conflict — `_autorename_unique` just lacks the same self-exclusion. The while-stopped variant passes because `_get_local_changes_while_inactive()` never goes through `client.move`/`_autorename_unique` on this path.

**Fix shape (proposal only).** Minimal fix in src/cumulus/writes.py: give `_autorename_unique` an `exclude_id: str | None = None` parameter and make its probe ignore entries whose `file_id == exclude_id` (e.g. replace `cache.path_to_id(p) is None` with `cache.has_case_conflict(p, exclude_id) is None`, mirroring path_cache.move); in `DriveWrites.move`, pass the already-resolved `file_id` (writes.py:583) as `exclude_id`. Then a case-only rename issues `files.update(name="FOLDER")` on the same fileId, and `cache.move("/folder", "/FOLDER")` already accepts the self-case-change (writes.py:618 / path_cache.py:246). Echo risk is low: after the move, `_update_index_recursive(md_to_new)` (sync.py:2455) records `/FOLDER` (incl. `remote_path_cased`), so when the rename comes back on the change feed `_on_remote_folder` first runs `_apply_case_change` (sync.py:3774), which no-ops because `entry.remote_path_cased == event.remote_path`, and then `_check_download_conflict`/`os.mkdir → FileExistsError → Skipped` (sync.py:3719-3725) make re-application harmless — same suppression path every successful upload already relies on. Residual checks for the fixer: (a) the upload path also calls `_autorename_unique` with a folded `existing_id` probe (writes.py:162-174) — a case-only *file* rename uploaded as Add+autorename could hit the same false self-collision; pass `existing_id` there too if it's the same item; (b) verify `_check_download_conflict` behaves for folders (no rev) on the echo.

**Saved log:** /tmp/cluster-b1-pin-run1.log (one pytest invocation, 3 attempt blocks, all reproducing; test ran 138.85s, 1 failed + 2 rerun).

### Task 2 outcome (B1 fix — commit 9643a2f)

Fix landed as proposed: `_autorename_unique(parent_path, encoded_name, exclude_id=None)` probes via `has_case_conflict(probe, exclude_id)`; `DriveWrites.move` passes the resolved `file_id`. Upload/make_dir call sites verified correct WITHOUT exclude_id (create paths — no self to exclude; the engine routes case-only renames to Update mode or the move path; spec reviewer traced both). 4 new offline tests (writes-level folder/file keep-name + genuine-collision guard; sync-level via `_create_remote_entry`); TDD-red verified by reverting writes.py; offline suite 476 green.

**Follow-up (Important, pre-existing, NOT fixed here):** case-only *file* renames at the sync level self-destruct before reaching the fixed probe — `_on_local_moved`'s replace-at-destination block (sync.py:2414-2426) looks up `get_index_entry(event.remote_path_lower)`; for `/file.txt → /FILE.TXT` the lower keys are equal so it finds the file being moved itself and `client.remove(..., parent_rev=...)` trashes it before `client.move` runs; the move then NotFoundErrors → rescan → re-upload as a NEW file (no content loss, but Drive identity/revision history lost). Faithful port of upstream's block; folder renames unaffected (the linked test is folder-based). Candidate fix: skip the remove when `local_entry.remote_id == event.remote_id` or when `event.remote_path_from_lower == event.remote_path_lower`.

### Task 3 findings (B2 pin + fix)

**Pinned mechanism.** The xfail reason ("get_metadata('/folder') raises NotFoundError") is **stale** — `PathIDCache.path_to_id` is case-insensitive (path_cache.py:121-136, lookup by `remote_path_fold`) and the change-feed's `_emit_apply` already calls `path_cache.move(old_path, new_path)` (src/cumulus/changes.py:213), whose prefix substitution re-cases children's cached paths (path_cache.py:262-279). The remote→local apply itself also works: all live runs show `Renamed ".../folder" to ".../FOLDER"` from `_apply_case_change` (run1 log lines 175/422/672), so the on-disk subtree and the folder's OWN index entry get the new casing. The real failure is in the **sync index for children**: Drive's change feed delivers a SINGLE change for the renamed folder — run2 log shows only `FolderMetadata(name='FOLDER', path_lower='/folder', path_display='/FOLDER')` in "Remote changes:" — unlike Dropbox, which emits delta entries for every child of a moved folder. `_apply_case_change` (src/cumulus/sync.py:3774) updated only the one entry (`entry.remote_path_cased = event.remote_path`), and `_check_download_conflict` returns LocalNewerOrIdentical for the folder echo (`Equal revs for "/FOLDER"` → Skipped, sync.py:3286/3687), so no other code path ever touches the child rows. Result: index has `/folder/Subfolder` (stale cased) while disk has `/FOLDER/Subfolder`. Pytest run2 pins it exactly: `AssertionError: casing on drive does not match index` at tests/linked/integration/test_sync.py:1589 with diff `- .../FOLDER/Subfolder` vs `+ .../folder/Subfolder` (saved: /tmp/cluster-b2-pin-run2-pytest.out; logs /tmp/cluster-b2-pin-run1.log, /tmp/cluster-b2-pin-run2.log).

**Fix.** In `_apply_case_change` (src/cumulus/sync.py): when the re-cased entry is a folder, select the subtree via `PathTreeQuery(IndexEntry.remote_path_lower, event.remote_path_lower)` (folded keys are unchanged — the rename is case-only) and rewrite each child's `remote_path_cased` by prefix substitution `old_path_cased + "/" → event.remote_path + "/"`, also refreshing `_case_conversion_cache` for each child so stale cached casings cannot re-pollute later `correct_case` calls. ~20 lines, inside the existing `_database_access()` block; no restructuring.

**TDD.** New offline test `test_remote_case_only_folder_rename_recases_child_index` (tests/offline/test_sync.py, FakeDrive-wired, drives `_create_local_entry` with the single folder event the feed produces). Red reproduced the pinned mechanism exactly (`'/folder/Subfolder' == '/FOLDER/Subfolder'` assertion failure with disk + parent entry already correct); green after fix. Targeted offline suites: tests/offline/test_sync.py + test_writes.py + test_path_cache.py = 124 passed.

**Live confirmation.** `test_case_change_remote --runxfail` PASS 2× (37.23s, 35.92s — first-try passes, no assert_synced retries). Passing log saved: /tmp/cluster-b2-postfix-run.log. xfail marker left in place for Task 5.

### Task 4 findings (C pin + fix)

**Pinned mechanism.** Both plan suspects were wrong, and so is the xfail reason — the sync engine handles children-before-parent correctly. The live failure is **test-harness timing**: the test fetches the change feed once, immediately after `create_remote_tree`, but Drive's `changes.list` lags a few seconds behind mutations (docs §14; same lag `assert_synced`'s docstring documents). In all 3 attempt blocks of /tmp/cluster-c-pin-run1.log (cursors 7276/7284/7293) the manual fetch returned ONLY `[FolderMetadata(name='parent', path_lower='/parent', ...)]` — `child_1`/`child_2` never appeared in any "Remote changes:" listing, so the reversed application had nothing to reverse: only `Created local folder "/parent"` (log lines 106-114, 205-213, 304-312). With sync stopped, nothing ever re-polls, and `_assert_synced_once` fails on the children missing locally. The engine-side ordering path was never exercised in the failing runs.

**Engine verified correct (offline).** New offline test `test_out_of_order_children_before_parent_all_created` (tests/offline/test_sync.py, FakeDrive-wired: path cache populated as `iter_remote_changes`' fixpoint leaves it, index/disk empty, events applied child_2 → child_1 → parent via `_create_local_entry`): GREEN against current code — `_ensure_parent` (sync.py:3518) syncs the missing parent on demand. Test has teeth: temporarily neutering `_ensure_parent` makes it fail exactly as the stale xfail reason describes (children dropped, parent missing). Note `get_metadata`-NotFoundError (plan candidate b) is unreachable for feed-delivered events: `iter_remote_changes`' fixpoint only emits a child entry after its parent resolved in the path cache (changes.py:286-295).

**Fix.** Harness, not engine: tests/linked/integration/test_sync.py `test_out_of_order_indexing` now polls `list_remote_changes_iterator` via `wait_until` (cursor threaded across polls) until the feed has delivered all three created paths, THEN reverses and applies. No src/cumulus changes needed for C.

**Live confirmation.** 3× PASS with `--runxfail` (26.73s / 27.73s / 27.08s vs 6:46 triple-fail before). Post-fix log /tmp/cluster-c-postfix-run.log proves the reversed path now exercises the engine live: `Parent folder /parent is not in index. Syncing it now before syncing any children.` → child_2, child_1 created, parent skip-deduped. Offline suites (test_sync + test_writes + test_path_cache): 125 passed. xfail marker left in place for Task 5.
