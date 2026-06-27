# Stage 7c — P0 Production-Defect Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three confirmed production defects guarded by `strict=True` xfails (occupied-target duplicate `remote_id` row, no-UNIQUE-guard stale orphan, persistent-400 unbounded reindex spin) and remove their xfail marks.

**Architecture:** Three independent defects, fixed across `sync.py`, `models.py`, and `manager.py`. The first two (7c-1 + 7c-2) are coupled — "B feeds C" — and close together via three coordinated changes: (a) a *narrowed* conflict-semantic fix in `_check_download_conflict` so an occupied-target folder move conflict-copies the untracked occupant instead of silently adopting it; (b) an app-level eviction in `update_index_from_sync_event` that removes any pre-existing index row holding the same `remote_id` before inserting the new one (collapses the stale orphan); (c) a schema `UNIQUE` constraint on `IndexEntry.remote_id` as a defense-in-depth backstop. The third (7c-3) adds a cross-restart circuit-breaker counter to `SyncManager._handle_sync_thread_errors`.

**Tech Stack:** Python 3.12, SQLite via the project's hand-rolled ORM (`src/cumulus/database/orm.py`), pytest offline suite with `tests.fake_drive.FakeDrive`.

---

## ⚠️ Design notes — read before starting (deviations from the 7c spec's one-line sketches)

The spec (`docs/superpowers/specs/2026-06-15-stage-7c-deferred-backlog.md`) proposes one-line fixes. Reconnaissance found two refinements that the implementer **must** apply, or the fix will be wrong:

1. **7c-1 must be narrowed.** The spec says "an existing local folder with **no** index row at the event path must return `Conflict.Conflict`." Taken literally this breaks the legitimate **first-sync merge** case (user already has an identical folder locally before linking; the remote folder event arrives with no index row and the engine *should* adopt it as `Identical`). The correct trigger is the *occupied-target move*: return `Conflict` only when the event's `remote_id` is **already tracked at a different index path** (i.e. `_apply_remote_move` just bailed because the target was occupied). When the id is not yet in the index, keep returning `Identical` (first-sync merge).

2. **7c-1 + 7c-2 need the eviction, not just `UNIQUE`.** The conflict-semantic fix conflict-copies the occupant but leaves the stale `/src` row, so the count-of-rows acceptance test still sees two rows. The `UNIQUE` constraint alone would turn the second insert into an `IntegrityError` (a crash), not "one row." The piece that actually collapses to one row is an **app-level eviction** in `update_index_from_sync_event`: before inserting, delete any *other* row with the same `remote_id`. The schema `UNIQUE` constraint is then a pure backstop (it only takes effect on a freshly-built DB anyway — `CREATE TABLE IF NOT EXISTS` does not alter existing tables; acceptable because this is pre-release, all dev/test DBs are fresh or `:memory:`).

3. **The two non-xfail duplicate-row tests break at seed time.** `test_duplicate_remote_id_rows_no_crash` and `test_duplicate_remote_id_rows_no_dangling_stale_row` both seed two rows with the same `remote_id` directly via `_seed_entry`. Once `UNIQUE` exists, the *second* seed raises `IntegrityError`. Both tests must be rewritten (Task 4) to assert the new reality: duplicates can no longer be created.

4. **7c-3's engine-level xfail test cannot observe a manager counter.** `test_scenario_b_persistent_400_is_bounded` (in `test_fault_cursor.py`) simulates the reset loop at the engine level and never touches `SyncManager`. A manager-side counter is invisible to it. Delete that engine-level xfail test and add a faithful manager-level test (Task 5) that drives `_handle_sync_thread_errors` directly, mirroring the existing `test_cursor_reset_triggers_reindex_and_autostart`.

**Definition of done (from the spec):** all three `strict=True` xfails removed because the defects are fixed (verified RED-before-GREEN), full offline suite green, mypy/flake8 at baseline parity, a deviations file written.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `src/cumulus/sync.py` | `_check_download_conflict` (7c-1), `update_index_from_sync_event` (eviction) | Modify |
| `src/cumulus/models.py` | `IndexEntry.remote_id` schema (7c-2 `UNIQUE`) | Modify |
| `src/cumulus/manager.py` | `_handle_sync_thread_errors` + `__init__` circuit-breaker (7c-3) | Modify |
| `tests/offline/test_fault_divergence.py` | 7c-1 occupant-preservation test; remove 2 xfails; rewrite 2 duplicate tests | Modify |
| `tests/offline/test_fault_cursor.py` | Delete the engine-level persistent-400 xfail test | Modify |
| `tests/offline/test_manager.py` | New manager-level circuit-breaker test (7c-3) | Modify |
| `docs/superpowers/plans/2026-06-15-stage-7c-p0-deviations.md` | Execution deviations / evidence | Create (Task 7) |

---

## Task 1: 7c-1 — `_check_download_conflict` conflict-copies an occupied-target folder occupant

**Files:**
- Modify: `src/cumulus/sync.py:3380-3389` (the `Identical` branch of `_check_download_conflict`)
- Test: `tests/offline/test_fault_divergence.py`

This task makes the engine **conflict-copy** an untracked folder occupant instead of silently adopting it. It does **not** yet collapse the row count (that is Task 2). The test here asserts data-safety (occupant preserved), independent of row count.

- [ ] **Step 1: Write the failing test** — append to `tests/offline/test_fault_divergence.py`:

```python
def test_occupied_target_folder_move_conflict_copies_occupant(sync_engine: SyncEngine):
    """7c-1: a remote folder moved onto a path occupied by an UNTRACKED local
    folder must conflict-copy the occupant, not silently adopt it.

    After the apply: a "conflicted copy" of the occupant must exist on disk
    (the user's data is preserved) and the moved folder's id must be indexed
    at the new path.  (Row-count collapse is covered by Task 2.)
    """
    fake = FakeDrive()
    dir_id = fake.add_folder("/dst_cc")
    _wire_fake(sync_engine, fake)
    sync_engine.client._path_cache.set(
        "/dst_cc", dir_id, parent_id=ROOT_ID, name="dst_cc", is_folder=True
    )

    # Moved folder's OLD location, tracked in the index.
    _seed_entry(
        sync_engine, "/src_cc", dir_id, ItemType.Folder, rev="folder", content_hash="folder"
    )
    _write_local_dir(sync_engine, "/src_cc")
    # Untracked occupant at the move target.
    _write_local_dir(sync_engine, "/dst_cc")

    move_event = _make_move_event(
        sync_engine, "/dst_cc", dir_id, ItemType.Folder, "folder", "folder"
    )
    sync_engine._create_local_entry(move_event)

    # Occupant preserved as a conflicted copy (the user's folder is not lost).
    sync_root = sync_engine.sync_path
    cc_dirs = [
        name for name in os.listdir(sync_root)
        if "conflicted copy" in name and osp.isdir(osp.join(sync_root, name))
    ]
    assert cc_dirs, (
        "occupant must be preserved as a conflicted copy, not silently adopted; "
        f"sync root contents: {os.listdir(sync_root)}"
    )

    # The moved folder is indexed at the new path.
    new_entry = sync_engine.get_index_entry("/dst_cc")
    assert new_entry is not None and new_entry.remote_id == dir_id
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/offline/test_fault_divergence.py::test_occupied_target_folder_move_conflict_copies_occupant -v`
Expected: FAIL — no `conflicted copy` directory is created (the occupant is currently adopted via the `Identical` path).

- [ ] **Step 3: Apply the narrowed conflict-semantic fix**

In `src/cumulus/sync.py`, replace the `Identical` branch of `_check_download_conflict` (currently lines ~3380-3389):

```python
        elif (
            event.content_hash == self.get_local_hash(event.local_path)
            and event.symlink_target == local_symlink_target
        ):
            # Content hashes are equal, therefore items are identical. Folders will
            # have a content hash of 'folder'.
            self._logger.debug(
                'Equal content hashes for "%s": no conflict', event.remote_path
            )
            return Conflict.Identical
```

with:

```python
        elif (
            event.content_hash == self.get_local_hash(event.local_path)
            and event.symlink_target == local_symlink_target
        ):
            # Content hashes are equal, therefore items are identical. Folders will
            # have a content hash of 'folder'.
            if event.content_hash == "folder" and local_rev is None and event.remote_id:
                # An untracked local folder occupies this path. A folder's
                # content hash is the constant sentinel "folder", so "equal
                # hashes" here means nothing more than "a folder exists". If the
                # event's remote_id is ALREADY indexed at a DIFFERENT path, this
                # is an occupied-target move (_apply_remote_move bailed because
                # the target was occupied): the occupant is NOT the moved folder,
                # so adopting it would create a duplicate remote_id row (finding
                # B) and silently swallow the user's folder. Conflict-copy it
                # instead. A genuine first-sync merge — where the id is not yet
                # indexed anywhere — falls through to Identical below.
                other = self.get_index_entry_by_remote_id(event.remote_id)
                if other is not None and other.remote_path_lower != event.remote_path_lower:
                    self._logger.debug(
                        'Untracked folder occupies move target "%s" (id=%s indexed '
                        'at "%s"): conflict-copy occupant',
                        event.remote_path,
                        event.remote_id,
                        other.remote_path_lower,
                    )
                    return Conflict.Conflict
            self._logger.debug(
                'Equal content hashes for "%s": no conflict', event.remote_path
            )
            return Conflict.Identical
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/offline/test_fault_divergence.py::test_occupied_target_folder_move_conflict_copies_occupant -v`
Expected: PASS.

- [ ] **Step 5: Run the surrounding suite to check for first-sync-merge regressions**

Run: `python -m pytest tests/offline/test_sync.py tests/offline/test_load_initial_sync.py -q --benchmark-disable`
Expected: PASS (no regression — the narrowed condition leaves the first-sync `Identical` path untouched because the id is not yet indexed during a merge).

- [ ] **Step 6: Commit**

```bash
git add src/cumulus/sync.py tests/offline/test_fault_divergence.py
git commit -m "fix(7c-1): conflict-copy untracked occupant on occupied-target folder move"
```

---

## Task 2: 7c-1/7c-2 — evict stale same-`remote_id` rows in `update_index_from_sync_event`; remove the occupied-target xfail

**Files:**
- Modify: `src/cumulus/sync.py:1043-1058` (`update_index_from_sync_event`)
- Test: `tests/offline/test_fault_divergence.py` (remove the xfail on `test_occupied_target_folder_move_creates_duplicate_id_row`)

This collapses the stale `/src` row so the occupied-target move ends with exactly one row, flipping the existing strict xfail to a pass.

- [ ] **Step 1: Confirm the existing guard currently xfails**

Run: `python -m pytest tests/offline/test_fault_divergence.py::test_occupied_target_folder_move_creates_duplicate_id_row -v`
Expected: XFAIL (strict) — two rows share `dir_id`.

- [ ] **Step 2: Add the eviction**

In `src/cumulus/sync.py`, in `update_index_from_sync_event`, replace the create/update block (currently lines ~1045-1058):

```python
            if event.change_type is not ChangeType.Removed:
                # Create or update entry.
                entry = IndexEntry(
                    remote_path_cased=event.remote_path,
                    remote_path_lower=remote_path_lower,
                    remote_id=event.remote_id,
                    item_type=event.item_type,
                    last_sync=self._get_ctime(event.local_path),
                    rev=event.rev,
                    content_hash=event.content_hash,
                    symlink_target=event.symlink_target,
                )

                self._index_table.update(entry)
```

with:

```python
            if event.change_type is not ChangeType.Removed:
                # Evict any OTHER index row already holding this remote_id at a
                # different path before inserting. Drive emits a single change at
                # the new path for a moved item; an occupied-target / failed-move
                # fallback (_apply_remote_move returning without re-keying) can
                # leave a stale orphan row at the old path with the same
                # remote_id. Inserting the new row without this eviction creates
                # duplicate remote_id rows (findings B/C); get_index_entry_by_
                # remote_id is first-wins with no ORDER BY, so the orphan can be
                # returned for later moves. The UNIQUE schema constraint backstops
                # this on fresh databases; evicting here keeps existing databases
                # self-consistent too.
                if event.remote_id:
                    for stale in self._index_table.select(
                        MatchQuery(IndexEntry.remote_id, event.remote_id)
                    ):
                        if stale.remote_path_lower != remote_path_lower:
                            self._index_table.delete_primary_key(
                                stale.remote_path_lower
                            )

                # Create or update entry.
                entry = IndexEntry(
                    remote_path_cased=event.remote_path,
                    remote_path_lower=remote_path_lower,
                    remote_id=event.remote_id,
                    item_type=event.item_type,
                    last_sync=self._get_ctime(event.local_path),
                    rev=event.rev,
                    content_hash=event.content_hash,
                    symlink_target=event.symlink_target,
                )

                self._index_table.update(entry)
```

(`MatchQuery` and `IndexEntry` are already imported in `sync.py`.)

- [ ] **Step 3: Remove the xfail mark and tighten the guard**

In `tests/offline/test_fault_divergence.py`, delete the entire `@pytest.mark.xfail(...)` decorator above `test_occupied_target_folder_move_creates_duplicate_id_row` (the `strict=True` block whose reason starts `"finding B: ..."`). Then add an occupant-preservation assertion at the end of that test body, after the existing `assert rows[0].remote_path_lower == "/dst_b3"`:

```python
    # Occupant preserved as a conflicted copy (not silently adopted).
    cc_dirs = [
        name for name in os.listdir(sync_engine.sync_path)
        if "conflicted copy" in name
    ]
    assert cc_dirs, "occupant must be conflict-copied, not adopted"
```

- [ ] **Step 4: Run the (now un-xfailed) guard to verify it passes**

Run: `python -m pytest tests/offline/test_fault_divergence.py::test_occupied_target_folder_move_creates_duplicate_id_row -v`
Expected: PASS (exactly one row at `/dst_b3`; conflicted copy present).

- [ ] **Step 5: Run scenario-A move tests to confirm no re-key regression**

Run: `python -m pytest tests/offline/test_fault_divergence.py -k "stale_cache or missing_parent" -v`
Expected: PASS (normal moves still re-key exactly one row; eviction's `!= remote_path_lower` guard never touches the row being updated).

- [ ] **Step 6: Commit**

```bash
git add src/cumulus/sync.py tests/offline/test_fault_divergence.py
git commit -m "fix(7c-1/7c-2): evict stale same-remote_id rows on index update; remove finding-B xfail"
```

---

## Task 3: 7c-2 — add the `UNIQUE` constraint on `IndexEntry.remote_id`

**Files:**
- Modify: `src/cumulus/models.py:491`

The schema-level backstop. Because `update` inserts via `save()` (a plain `INSERT`), a duplicate `remote_id` now raises `sqlite3.IntegrityError` instead of silently corrupting the index.

- [ ] **Step 1: Write the failing test** — append to `tests/offline/test_fault_divergence.py`:

```python
def test_remote_id_unique_constraint_rejects_duplicate(sync_engine: SyncEngine):
    """7c-2: the index schema must enforce UNIQUE on remote_id, so a second
    row with an already-used remote_id is rejected at the database layer."""
    import sqlite3 as _sqlite3

    _seed_entry(
        sync_engine, "/u_a.txt", "uniq-id", ItemType.File, rev="r1", content_hash="h"
    )
    with pytest.raises(_sqlite3.IntegrityError):
        _seed_entry(
            sync_engine, "/u_b.txt", "uniq-id", ItemType.File, rev="r1", content_hash="h"
        )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/offline/test_fault_divergence.py::test_remote_id_unique_constraint_rejects_duplicate -v`
Expected: FAIL — no `IntegrityError` raised (no constraint yet); the second seed succeeds.

- [ ] **Step 3: Add the constraint**

In `src/cumulus/models.py`, change line 491 from:

```python
    remote_id = NonNullColumn(SqlString(), index=True)
```

to:

```python
    remote_id = NonNullColumn(SqlString(), unique=True, index=True)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/offline/test_fault_divergence.py::test_remote_id_unique_constraint_rejects_duplicate -v`
Expected: PASS (the in-memory DB is fresh, so `CREATE TABLE` includes `UNIQUE`).

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/models.py tests/offline/test_fault_divergence.py
git commit -m "fix(7c-2): add UNIQUE constraint on IndexEntry.remote_id"
```

---

## Task 4: 7c-2 — rewrite the two duplicate-row tests that can no longer seed duplicates; remove the finding-C xfail

**Files:**
- Modify: `tests/offline/test_fault_divergence.py` (`test_duplicate_remote_id_rows_no_crash`, `test_duplicate_remote_id_rows_no_dangling_stale_row`)

Both tests seed two same-`remote_id` rows directly; with Task 3's constraint the second seed raises at setup time. Their premise (duplicates can exist) is now false, so they are re-authored to assert that duplicates are rejected and that the engine's create path re-keys rather than duplicating.

- [ ] **Step 1: Confirm both tests now error at seed time**

Run: `python -m pytest tests/offline/test_fault_divergence.py -k duplicate_remote_id_rows -v`
Expected: `test_duplicate_remote_id_rows_no_crash` ERRORs/FAILs at the second `_seed_entry`; `test_duplicate_remote_id_rows_no_dangling_stale_row` is XFAIL (its body raises at seed, which a strict xfail still counts as xfail). This proves they need rewriting.

- [ ] **Step 2: Replace `test_duplicate_remote_id_rows_no_crash`**

Replace the whole `def test_duplicate_remote_id_rows_no_crash(...)` body with:

```python
def test_duplicate_remote_id_rows_rejected_at_db(sync_engine: SyncEngine):
    """A duplicate remote_id can no longer be inserted (7c-2 UNIQUE). The
    second seed must raise IntegrityError rather than create a second row.

    Supersedes test_duplicate_remote_id_rows_no_crash, whose premise (two rows
    coexisting) is no longer reachable.
    """
    import sqlite3 as _sqlite3

    file_id = "dup-id-1"
    data = b"duplicate id content"
    _write_local_file(sync_engine, "/path_a.txt", data)

    _seed_entry(
        sync_engine, "/path_a.txt", file_id, ItemType.File, rev="rev-1",
        content_hash=_md5(data),
    )
    with pytest.raises(_sqlite3.IntegrityError):
        _seed_entry(
            sync_engine, "/path_b.txt", file_id, ItemType.File, rev="rev-1",
            content_hash=_md5(data),
        )

    # Exactly one row for the id survives.
    with sync_engine._database_access():
        rows = sync_engine._index_table.select_sql("WHERE remote_id = ?", file_id)
    assert len(rows) == 1
    assert rows[0].remote_path_lower == "/path_a.txt"
```

- [ ] **Step 3: Replace `test_duplicate_remote_id_rows_no_dangling_stale_row` and drop its xfail**

Delete the `@pytest.mark.xfail(...)` decorator (the `strict=True` block whose reason starts `"finding C: ..."`) and replace the whole `def test_duplicate_remote_id_rows_no_dangling_stale_row(...)` with a test that proves the *engine* create path re-keys via eviction instead of leaving a dangling orphan:

```python
def test_remote_id_move_via_create_rekeys_not_duplicates(sync_engine: SyncEngine):
    """7c-2 closure: applying a create/move event for an id already indexed at
    another path re-keys (one row), leaving no dangling orphan.

    Supersedes the finding-C xfail. The stale-orphan defect is closed by the
    update_index_from_sync_event eviction (Task 2) plus the UNIQUE backstop
    (Task 3): get_index_entry_by_remote_id can no longer return a wrong row.
    """
    file_id = "dup-id-2"
    data = b"rekey not duplicate"

    _write_local_file(sync_engine, "/dup_a.txt", data)
    _seed_entry(
        sync_engine, "/dup_a.txt", file_id, ItemType.File, rev="rev-1",
        content_hash=_md5(data),
    )

    # Apply an index update for the same id at a new path (as the download path
    # does after writing the file). Eviction must drop the /dup_a.txt row.
    event = _make_move_event(
        sync_engine, "/dup_c.txt", file_id, ItemType.File, rev="rev-1",
        content_hash=_md5(data), size=len(data),
    )
    _write_local_file(sync_engine, "/dup_c.txt", data)
    sync_engine.update_index_from_sync_event(event)

    with sync_engine._database_access():
        rows_after = sync_engine._index_table.select_sql("WHERE remote_id = ?", file_id)
    assert len(rows_after) == 1, (
        f"expected 1 row, got {[r.remote_path_lower for r in rows_after]}"
    )
    assert sync_engine.get_index_entry("/dup_c.txt") is not None
    assert sync_engine.get_index_entry("/dup_a.txt") is None
    # Resolution by id is now unambiguous.
    by_id = sync_engine.get_index_entry_by_remote_id(file_id)
    assert by_id is not None and by_id.remote_path_lower == "/dup_c.txt"
```

- [ ] **Step 4: Run the rewritten tests**

Run: `python -m pytest tests/offline/test_fault_divergence.py -k "duplicate or rekey or unique" -v`
Expected: PASS (no xfail, no error). Confirm no remaining `xfail` markers in the file:

Run: `grep -n "xfail" tests/offline/test_fault_divergence.py`
Expected: no output.

- [ ] **Step 5: Run the whole divergence file**

Run: `python -m pytest tests/offline/test_fault_divergence.py -q --benchmark-disable`
Expected: all PASS, 0 xfail, 0 xpass.

- [ ] **Step 6: Commit**

```bash
git add tests/offline/test_fault_divergence.py
git commit -m "test(7c-2): rewrite duplicate-row tests for UNIQUE constraint; remove finding-C xfail"
```

---

## Task 5: 7c-3 — circuit-breaker for persistent `CursorResetError` in `SyncManager`

**Files:**
- Modify: `src/cumulus/manager.py` (`__init__` near line 127; `_handle_sync_thread_errors` lines ~553-589)
- Test: `tests/offline/test_manager.py`
- Modify: `tests/offline/test_fault_cursor.py` (delete the engine-level xfail test)

A cross-restart counter caps how many consecutive `CursorResetError`-driven reindex cycles the manager will perform. After `MAX_CURSOR_RESETS`, it stops without re-arming autostart and surfaces a visible error. Any healthy pass through the worker resets the counter.

- [ ] **Step 1: Write the failing test** — append to `tests/offline/test_manager.py`:

```python
def test_persistent_cursor_reset_trips_circuit_breaker(m: Maestral, tmp_path) -> None:
    """7c-3: repeated CursorResetError must NOT loop forever. After
    MAX_CURSOR_RESETS consecutive resets the manager gives up: it stops without
    re-arming autostart and raises a visible (ERROR-level) notification."""
    local_dir = str(tmp_path / "SyncRoot")
    os.makedirs(local_dir)
    m.sync.sync_path = local_dir

    manager = m.manager
    running = manager.running
    autostart = manager.autostart
    manager.desktop_notifier = Mock()

    from cumulus.manager import MAX_CURSOR_RESETS

    # The first MAX_CURSOR_RESETS resets schedule a restart (autostart set).
    for _ in range(MAX_CURSOR_RESETS):
        manager.autostart.clear()
        with manager._handle_sync_thread_errors(running, autostart):
            raise CursorResetError("Sync cursor reset", "token expired")
        assert manager.autostart.is_set(), "reset within budget must re-arm autostart"

    # The next reset trips the breaker: give up, no autostart.
    manager.autostart.clear()
    with manager._handle_sync_thread_errors(running, autostart):
        raise CursorResetError("Sync cursor reset", "token expired")

    assert not manager.autostart.is_set(), (
        "circuit-breaker must NOT re-arm autostart after the budget is exhausted"
    )
    error_calls = [
        c for c in manager.desktop_notifier.notify.call_args_list
        if c.kwargs.get("level") == notify.ERROR
    ]
    assert error_calls, "breaker must surface a visible ERROR notification"


def test_cursor_reset_counter_resets_on_healthy_cycle(m: Maestral, tmp_path) -> None:
    """7c-3: a healthy (exception-free) pass through the error handler clears
    the reset counter, so transient resets never accumulate to a trip."""
    local_dir = str(tmp_path / "SyncRoot2")
    os.makedirs(local_dir)
    m.sync.sync_path = local_dir

    manager = m.manager
    running = manager.running
    autostart = manager.autostart
    manager.desktop_notifier = Mock()

    from cumulus.manager import MAX_CURSOR_RESETS

    # Exhaust the budget minus one, then a healthy cycle clears it.
    for _ in range(MAX_CURSOR_RESETS):
        with manager._handle_sync_thread_errors(running, autostart):
            raise CursorResetError("Sync cursor reset", "token expired")
    with manager._handle_sync_thread_errors(running, autostart):
        pass  # healthy cycle resets the counter

    # A further reset is again treated as within-budget (autostart re-armed).
    manager.autostart.clear()
    with manager._handle_sync_thread_errors(running, autostart):
        raise CursorResetError("Sync cursor reset", "token expired")
    assert manager.autostart.is_set(), (
        "counter must reset on a healthy cycle, so this reset is within budget"
    )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/offline/test_manager.py -k "circuit_breaker or counter_resets" -v`
Expected: FAIL — `ImportError`/`AttributeError` for `MAX_CURSOR_RESETS` (does not exist yet), and the breaker behaviour is absent.

- [ ] **Step 3: Add the module constant**

In `src/cumulus/manager.py`, add near the top-level constants (after the imports, before the `SyncManager` class):

```python
MAX_CURSOR_RESETS = 3
"""Maximum consecutive CursorResetError-driven reindex cycles before the
manager gives up. A permanently-400ing change feed would otherwise loop
bootstrap -> 400 -> reset -> restart forever (7c-3)."""
```

- [ ] **Step 4: Initialise the counter**

In `SyncManager.__init__`, alongside the existing event setup (near `self.autostart = Event()` at line ~127), add:

```python
        self._cursor_reset_count = 0
```

- [ ] **Step 5: Add the breaker logic**

In `_handle_sync_thread_errors`, replace the existing structure (currently lines ~556-589) so the `CursorResetError` branch counts and trips, and a clean pass resets the counter. Replace:

```python
        try:
            yield
        except CancelledError:
            # Shutdown will be handled externally.
            running.clear()
        except CursorResetError:
            self._logger.warning(
                "Google Drive change token expired; rebuilding the index."
            )
            if self.desktop_notifier:
                self.desktop_notifier.notify(
                    "Rebuilding index",
                    "Cumulus was offline too long and will re-index "
                    "Google Drive. Sync resumes automatically.",
                    level=notify.INFO,
                )
            self.stop()
            try:
                self.sync.reset_sync_state()
            finally:
                self.autostart.set()
```

with:

```python
        try:
            yield
        except CancelledError:
            # Shutdown will be handled externally.
            running.clear()
        except CursorResetError:
            self._cursor_reset_count += 1
            if self._cursor_reset_count > MAX_CURSOR_RESETS:
                # Circuit-breaker: the change feed has returned a cursor-reset
                # error for MAX_CURSOR_RESETS consecutive reindex cycles,
                # including freshly-bootstrapped tokens. Re-arming autostart
                # would loop bootstrap -> 400 -> reset -> restart forever
                # (7c-3). Give up: stop without autostart and surface a visible
                # error so the user can act.
                self._logger.error(
                    "Google Drive change feed keeps rejecting the sync token "
                    "after %s reindex attempts; giving up.",
                    self._cursor_reset_count,
                )
                if self.desktop_notifier:
                    self.desktop_notifier.notify(
                        "Sync stopped",
                        "Cumulus could not re-index Google Drive after repeated "
                        "attempts. Please restart syncing.",
                        level=notify.ERROR,
                    )
                self.stop()
                return
            self._logger.warning(
                "Google Drive change token expired; rebuilding the index."
            )
            if self.desktop_notifier:
                self.desktop_notifier.notify(
                    "Rebuilding index",
                    "Cumulus was offline too long and will re-index "
                    "Google Drive. Sync resumes automatically.",
                    level=notify.INFO,
                )
            self.stop()
            try:
                self.sync.reset_sync_state()
            finally:
                self.autostart.set()
        else:
            # A healthy pass through the worker clears the cursor-reset
            # circuit-breaker; only *consecutive* resets accumulate.
            self._cursor_reset_count = 0
```

(`notify` is already imported in `manager.py` — confirm with `grep -n "import notify\|from . import" src/cumulus/manager.py` if unsure.)

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `python -m pytest tests/offline/test_manager.py -k "circuit_breaker or counter_resets" -v`
Expected: PASS.

- [ ] **Step 7: Confirm the existing cursor-reset test still passes (the first reset is unchanged)**

Run: `python -m pytest tests/offline/test_manager.py::test_cursor_reset_triggers_reindex_and_autostart -v`
Expected: PASS (the first reset still resets state + arms autostart with an INFO notification).

- [ ] **Step 8: Delete the engine-level persistent-400 xfail test**

In `tests/offline/test_fault_cursor.py`, delete the entire `@pytest.mark.xfail(...)` decorator **and** the `def test_scenario_b_persistent_400_is_bounded(...)` function below it (the whole Scenario B section). It is superseded by the manager-level tests in Step 1, which exercise the real circuit-breaker rather than an engine-level simulation. Leave the Scenario B explanatory comment block in place but update its closing lines to point to `test_manager.py`, or remove the block — either is fine; do not leave a dangling `xfail`.

Run: `grep -n "xfail" tests/offline/test_fault_cursor.py`
Expected: no output.

- [ ] **Step 9: Run the cursor fault file**

Run: `python -m pytest tests/offline/test_fault_cursor.py -q --benchmark-disable`
Expected: all PASS, 0 xfail.

- [ ] **Step 10: Commit**

```bash
git add src/cumulus/manager.py tests/offline/test_manager.py tests/offline/test_fault_cursor.py
git commit -m "fix(7c-3): circuit-breaker for persistent CursorResetError; remove persistent-400 xfail"
```

---

## Task 6: Full-suite green gate + static-analysis parity

**Files:** none (verification only)

- [ ] **Step 1: Confirm no strict-xfail guards remain for the three defects**

Run: `grep -rn "xfail" tests/offline/ | grep -iE "finding b|finding c|persistent-400|persistent 400|duplicate remote_id|circuit-breaker"`
Expected: no output (all three removed).

- [ ] **Step 2: Run the full offline suite**

Run: `python -m pytest tests/offline -q --benchmark-disable`
Expected: all PASS. Record the exact counts (e.g. `NNN passed, X deselected`). No `xpassed`, no new `xfailed` beyond any pre-existing unrelated ones. Compare against the last-known green baseline noted in the 7b-E3 gate evidence; any delta must be explained in Task 7.

- [ ] **Step 3: mypy parity**

Run: `python -m mypy src/cumulus 2>&1 | tail -5`
Expected: error count unchanged vs the baseline recorded in the 7b-E3 deviations file (the changes add no new annotations needing relaxation).

- [ ] **Step 4: flake8 parity**

Run: `python -m flake8 src/cumulus/sync.py src/cumulus/models.py src/cumulus/manager.py`
Expected: no new violations (line length, unused imports, etc.).

- [ ] **Step 5: Commit any formatting touch-ups only if needed**

```bash
git add -A
git commit -m "chore(7c): static-analysis parity touch-ups"
```

(Skip if nothing changed.)

---

## Task 7: Write the Stage 7c deviations / evidence document

**Files:**
- Create: `docs/superpowers/plans/2026-06-15-stage-7c-p0-deviations.md`

- [ ] **Step 1: Write the deviations file** capturing, with complete prose (no placeholders):
  - The two design deviations from the spec's one-line sketches (narrowed 7c-1 condition; eviction + UNIQUE rather than UNIQUE alone) and *why* (first-sync-merge preservation; row-count collapse vs. IntegrityError).
  - The test re-authoring: two duplicate-row tests rewritten (premise invalidated by UNIQUE), the finding-C xfail replaced by a re-key test, the engine-level persistent-400 xfail deleted in favour of two manager-level tests.
  - The accepted residual: on the occupied-target move, the stale folder *on disk* at the old path self-heals on the next local scan (index is now correct immediately); consistent with the pre-existing `_apply_remote_move` occupied-branch comment.
  - The `CREATE TABLE IF NOT EXISTS` caveat: the `UNIQUE` constraint only applies to freshly-built index DBs; acceptable pre-release, and the app-level eviction enforces uniqueness on any existing DB regardless.
  - Final gate evidence: exact offline pass/xfail counts, mypy/flake8 parity lines, and the commit hashes for Tasks 1-5.
  - Updated status for spec items 7c-1, 7c-2, 7c-3 → FIXED; note 7c-10 (transient duplicates) closed by this work.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-06-15-stage-7c-p0-deviations.md
git commit -m "docs(7c): P0 defect-fix deviations + gate evidence"
```

---

## Out of scope for this plan (tracked, not done here)

Per the spec §2-§3, the **P1** loose ends (7c-4 `root_id` persistence decision; 7c-5 unshare scenario; 7c-6 headless paste-redirect re-validation) and **P2** residuals (7c-7 `path_display` casefold; 7c-8 temp-file leak; 7c-9 `remote_path_lower` naming) are deferred. 7c-5/7c-6 need a live / 2-account setup and should batch with the next linked spot-check session; the rest are opportunistic, to be folded into the next touch of the relevant file. A follow-up plan (or direct execution) covers them once this P0 block is merged. Stage 8 (alias removal, naming sweep, docstrings, sharing API) remains separate per spec §5.

---

## Self-Review

- **Spec coverage (P0 block):** 7c-1 → Tasks 1-2; 7c-2 → Tasks 2-4; 7c-3 → Task 5; definition-of-done (xfails removed, suite green, static parity, deviations doc) → Tasks 6-7. P1/P2 explicitly deferred above.
- **All three strict xfails removed:** `test_occupied_target_folder_move_creates_duplicate_id_row` (Task 2), `test_duplicate_remote_id_rows_no_dangling_stale_row` (Task 4), `test_scenario_b_persistent_400_is_bounded` (Task 5, deleted).
- **Type/name consistency:** `MatchQuery`, `IndexEntry`, `Conflict`, `sqlite3` already imported in `sync.py`; `notify` already imported in `manager.py`; `MAX_CURSOR_RESETS` defined in Task 5 Step 3 and referenced by tests in Step 1 and the breaker in Step 5; `_cursor_reset_count` initialised (Step 4) before use (Step 5).
- **No placeholders:** every code step shows complete code; every run step states an expected result.
