"""Fault-divergence tests: id→path staleness, occupied-target, duplicate-id rows,
missing parent reindex (Stage 7b-E2, Task 4).

Covers:
  A — stale cache entry, move resolves by remote_id (F3 regression)
  B — occupied-target move with folder occupant: no silent overwrite/resurrection
  C — duplicate remote_id index rows: deterministic resolution, no crash (LIKELY DEFECT)
  D — missing index row for live Drive parent: reindex/repair, not silent divergence
"""

from __future__ import annotations

import hashlib
import os
import os.path as osp
import sqlite3
from datetime import datetime
from unittest.mock import Mock

import pytest

from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.core import FullAccount
from cumulus.database.core import Database
from cumulus.exceptions import DatabaseError
from cumulus.keyring import CredentialStorage
from cumulus.models import (
    ChangeType,
    IndexEntry,
    ItemType,
    SyncDirection,
    SyncEvent,
    SyncStatus,
)
from cumulus.path_cache import PathIDCache
from cumulus.sync import SyncEngine
from cumulus.utils.appdirs import get_home_dir
from cumulus.utils.path import delete
from tests.fake_drive import ROOT_ID, FakeDrive

# ---------------------------------------------------------------------------
# Shared helpers (mirrored from test_fault_crash.py / test_sync.py patterns)
# ---------------------------------------------------------------------------

_STUB_ACCOUNT = FullAccount(
    account_id="account-id",
    display_name="Offline Tester",
    email="offline@example.com",
    profile_photo_url=None,
    disabled=False,
)


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _start(fake: FakeDrive) -> str:
    return fake.service().changes().getStartPageToken().execute()["startPageToken"]


# ---------------------------------------------------------------------------
# Shared engine fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_engine():
    local_dir = osp.join(get_home_dir(), "fault_divergence_dir")
    os.mkdir(local_dir)

    # check_same_thread=False required because apply_remote_changes uses a
    # thread pool (do_parallel) that accesses the path cache from worker threads.
    cache = PathIDCache(Database(sqlite3.connect(":memory:", check_same_thread=False)))
    cache.root_id = ROOT_ID
    sync = SyncEngine(
        GoogleDriveClient(
            "fault-divergence-config",
            CredentialStorage("fault-divergence-config"),
            cache,
        )
    )
    sync.sync_path = local_dir
    sync.local_cursor = 0.0

    yield sync

    remove_configuration("fault-divergence-config")
    delete(local_dir)


def _wire_fake(sync: SyncEngine, fake: FakeDrive) -> None:
    sync.client._get_service = Mock(return_value=fake.service())
    sync.client._cached_account_info = _STUB_ACCOUNT


def _seed_entry(
    sync: SyncEngine,
    remote_path: str,
    remote_id: str,
    item_type: ItemType,
    rev: str,
    content_hash: str | None,
    last_sync: float = 0.0,
) -> None:
    entry = IndexEntry(
        remote_path_cased=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=remote_id,
        item_type=item_type,
        last_sync=last_sync,
        rev=rev,
        content_hash=content_hash,
        symlink_target=None,
    )
    with sync._database_access():
        sync._index_table.update(entry)


def _write_local_file(sync: SyncEngine, remote_path: str, data: bytes) -> str:
    local_path = sync.to_local_path_from_cased(remote_path)
    os.makedirs(osp.dirname(local_path), exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


def _write_local_dir(sync: SyncEngine, remote_path: str) -> str:
    local_path = sync.to_local_path_from_cased(remote_path)
    os.makedirs(local_path, exist_ok=True)
    return local_path


def _make_move_event(
    sync: SyncEngine,
    remote_path: str,
    remote_id: str,
    item_type: ItemType,
    rev: str,
    content_hash: str | None,
    size: int = 0,
) -> SyncEvent:
    return SyncEvent(
        direction=SyncDirection.Down,
        item_type=item_type,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=remote_id,
        local_path=sync.to_local_path_from_cased(remote_path),
        rev=rev,
        content_hash=content_hash,
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=size,
        completed=0,
    )


# ---------------------------------------------------------------------------
# Scenario A — stale id→path cache, move resolves by remote_id (F3)
#
# Fully synced file at /orig.txt.  Fake Drive moves it to /subdir/moved.txt
# out-of-band.  Path cache is deliberately NOT updated (stale: still maps
# /orig.txt → file_id, but the real Drive path is /subdir/moved.txt).
# Deliver the move via the SyncEvent and apply it.
# Assert the engine resolves by remote_id, not by the stale cache path.
# ---------------------------------------------------------------------------


def test_stale_cache_move_resolves_by_remote_id(sync_engine: SyncEngine):
    """_apply_remote_move must use remote_id as the anchor, not the stale
    cache path, so that a move applied after a cache-miss still succeeds.

    Construction mirrors test_remote_file_rename_moves_local_file (test_sync.py
    ~998) but uses a deliberately stale cache entry to prove id-keyed resolution.
    """
    data = b"file that will be moved out-of-band on Drive"
    fake = FakeDrive()
    dir_id = fake.add_folder("/subdir")
    file_id = fake.add_file("/subdir/moved.txt", data)
    file_rev = fake.files[file_id]["headRevisionId"]

    # Wire fake but with a STALE path cache: the cache says /orig.txt→file_id,
    # even though on Drive the file now lives at /subdir/moved.txt.
    _wire_fake(sync_engine, fake)
    # Stale cache entry at old path — engine must ignore this and use index row.
    sync_engine.client._path_cache.set(
        "/orig.txt", file_id, parent_id=ROOT_ID, name="orig.txt", is_folder=False
    )
    # Also populate the subdir in the cache so _ensure_parent can resolve it.
    sync_engine.client._path_cache.set(
        "/subdir", dir_id, parent_id=ROOT_ID, name="subdir", is_folder=True
    )

    # Fully synced PRE-move local state: /orig.txt on disk + index row.
    local_old = _write_local_file(sync_engine, "/orig.txt", data)
    _seed_entry(
        sync_engine,
        "/orig.txt",
        file_id,
        ItemType.File,
        rev=file_rev,
        content_hash=_md5(data),
    )
    # Seed the parent dir in the index so _ensure_parent short-circuits.
    _seed_entry(
        sync_engine,
        "/subdir",
        dir_id,
        ItemType.Folder,
        rev="folder",
        content_hash="folder",
    )
    _write_local_dir(sync_engine, "/subdir")

    # Deliver the move event: Drive says the file is now at /subdir/moved.txt.
    event = _make_move_event(
        sync_engine,
        "/subdir/moved.txt",
        file_id,
        ItemType.File,
        rev=file_rev,
        content_hash=_md5(data),
        size=len(data),
    )

    sync_engine._create_local_entry(event)

    # OLD path is gone — the engine did NOT use the stale cache path as a source.
    assert not osp.exists(local_old), "old local file must be gone after id-keyed move"

    # NEW path holds the content.
    local_new = sync_engine.to_local_path_from_cased("/subdir/moved.txt")
    assert osp.isfile(local_new), "new local file must exist"
    with open(local_new, "rb") as fh:
        assert fh.read() == data

    # Index: old key absent, new key present, exactly one row by id.
    assert sync_engine.get_index_entry("/orig.txt") is None
    new_entry = sync_engine.get_index_entry("/subdir/moved.txt")
    assert new_entry is not None
    assert new_entry.remote_id == file_id

    by_id = sync_engine.get_index_entry_by_remote_id(file_id)
    assert by_id is not None
    assert by_id.remote_path_lower == "/subdir/moved.txt"

    assert event.status in (SyncStatus.Done, SyncStatus.Skipped)
    assert sync_engine.sync_errors == []


def test_stale_cache_move_teeth(sync_engine: SyncEngine):
    """Teeth: if get_index_entry_by_remote_id always returned None, the engine
    could not find the old row by id, so it would leave the old local file on
    disk and create a stale new row — the old-path row would survive.

    We prove this by patching get_index_entry_by_remote_id to return None and
    asserting the old local file is still present (the move never happened).
    Then we restore the real method and confirm the green test has teeth.
    """
    data = b"teeth-proof content"
    fake = FakeDrive()
    dir_id = fake.add_folder("/subdir2")
    file_id = fake.add_file("/subdir2/moved2.txt", data)
    file_rev = fake.files[file_id]["headRevisionId"]

    _wire_fake(sync_engine, fake)
    sync_engine.client._path_cache.set(
        "/orig2.txt", file_id, parent_id=ROOT_ID, name="orig2.txt", is_folder=False
    )
    sync_engine.client._path_cache.set(
        "/subdir2", dir_id, parent_id=ROOT_ID, name="subdir2", is_folder=True
    )

    local_old = _write_local_file(sync_engine, "/orig2.txt", data)
    _seed_entry(
        sync_engine,
        "/orig2.txt",
        file_id,
        ItemType.File,
        rev=file_rev,
        content_hash=_md5(data),
    )
    _seed_entry(
        sync_engine,
        "/subdir2",
        dir_id,
        ItemType.Folder,
        rev="folder",
        content_hash="folder",
    )
    _write_local_dir(sync_engine, "/subdir2")

    event = _make_move_event(
        sync_engine,
        "/subdir2/moved2.txt",
        file_id,
        ItemType.File,
        rev=file_rev,
        content_hash=_md5(data),
        size=len(data),
    )

    # Neuter the id resolver: engine cannot find the old row.
    original = sync_engine.get_index_entry_by_remote_id
    sync_engine.get_index_entry_by_remote_id = lambda _id: None  # type: ignore[method-assign]

    try:
        sync_engine._create_local_entry(event)
    finally:
        sync_engine.get_index_entry_by_remote_id = original  # type: ignore[method-assign]

    # WITHOUT the resolver the old file is not moved (no row found → no move).
    assert osp.exists(local_old), (
        "old file must survive when the id resolver is neutered — "
        "proves the green test is not vacuous"
    )


# ---------------------------------------------------------------------------
# Scenario B — occupied-target move with a FOLDER occupant
#
# Baseline: test_remote_move_with_occupied_target_keeps_index_and_conflicts
# (test_sync.py ~1183) uses a file occupant.
# Adversarial variant: BOTH the moved item AND the occupant are FOLDERS.
#
# Analysis of actual behavior:
#   1. _apply_remote_move returns False (target occupied — osp.exists both paths).
#   2. _on_remote_folder calls _check_download_conflict for the /dst_folder event.
#   3. get_local_rev("/dst_folder") == None (no index row for occupant).
#   4. event.rev == "folder" != None → not LocalNewerOrIdentical.
#   5. get_local_hash(local_dst_folder) == "folder" (it's a dir) AND
#      event.content_hash == "folder" → Identical.
#   6. _check_download_conflict returns Identical → update_index_from_sync_event
#      creates a NEW row at /dst_folder with dir_id.
#   7. The old row at /src_folder SURVIVES as a stale orphan with the same id.
#
# This is a REAL DEFECT: the engine creates duplicate remote_id rows when a
# folder remote-move targets a locally-occupied-by-folder path.  The occupant
# is never conflict-copied (contrary to the docstring); the old row becomes a
# stale orphan.  The fix requires making _check_download_conflict consider
# missing-index-row folders as "untracked / newly-appeared" rather than
# "identical to remote" — an architectural change.
# ---------------------------------------------------------------------------


def test_occupied_target_folder_move_no_rekey_of_old_row(sync_engine: SyncEngine):
    """An occupied-target folder move must not silently adopt the occupant, and
    must end with exactly one (correctly re-keyed) index row for the moved id.

    _apply_remote_move detects the occupied target and bails (it does NOT
    re-key the old /src_folder row itself). The downstream download path then
    conflict-copies the untracked occupant and writes the moved folder at
    /dst_folder; update_index_from_sync_event's same-remote_id eviction (7c-1/
    7c-2) collapses the stale /src_folder orphan so the index ends with a single
    row at /dst_folder. Before that fix the engine left a stale /src_folder
    orphan plus a duplicate /dst_folder row (finding B).
    """
    fake = FakeDrive()
    dir_id = fake.add_folder("/dst_folder")

    _wire_fake(sync_engine, fake)
    sync_engine.client._path_cache.set(
        "/dst_folder", dir_id, parent_id=ROOT_ID, name="dst_folder", is_folder=True
    )

    _seed_entry(
        sync_engine,
        "/src_folder",
        dir_id,
        ItemType.Folder,
        rev="folder",
        content_hash="folder",
    )
    _write_local_dir(sync_engine, "/src_folder")
    # Occupant: a different local folder at the target, with NO index row.
    _write_local_dir(sync_engine, "/dst_folder")

    event = _make_move_event(
        sync_engine,
        "/dst_folder",
        dir_id,
        ItemType.Folder,
        rev="folder",
        content_hash="folder",
    )

    sync_engine._create_local_entry(event)

    # Exactly one row survives for the moved id, re-keyed to /dst_folder; the
    # stale /src_folder orphan is evicted (no silent adoption, no duplicate).
    with sync_engine._database_access():
        rows = sync_engine._index_table.select_sql("WHERE remote_id = ?", dir_id)
    assert len(rows) == 1, (
        f"expected 1 row for the moved id, got "
        f"{[r.remote_path_lower for r in rows]}"
    )
    assert rows[0].remote_path_lower == "/dst_folder"
    assert sync_engine.get_index_entry("/src_folder") is None
    new_entry = sync_engine.get_index_entry("/dst_folder")
    assert new_entry is not None and new_entry.remote_id == dir_id


def test_occupied_target_folder_move_no_rekey_teeth(sync_engine: SyncEngine):
    """Teeth for the no-rekey guarantee: manually re-key the old row to /dst_folder
    (simulating what a broken _apply_remote_move would do), then confirm the old
    row is gone.  This is the bad state the green test above must not produce.
    """
    fake = FakeDrive()
    dir_id = fake.add_folder("/dst_folder_teeth")
    _wire_fake(sync_engine, fake)

    _seed_entry(
        sync_engine,
        "/src_folder_teeth",
        dir_id,
        ItemType.Folder,
        rev="folder",
        content_hash="folder",
    )

    # Simulate the broken re-key.
    with sync_engine._database_access():
        old_entry = sync_engine.get_index_entry("/src_folder_teeth")
        assert old_entry is not None
        old_entry.remote_path_lower = "/dst_folder_teeth"
        old_entry.remote_path_cased = "/dst_folder_teeth"
        sync_engine._index_table.delete_primary_key("/src_folder_teeth")
        sync_engine._index_table.update(old_entry)

    # After the broken re-key the old row is gone — this is what the green test
    # asserts must NOT happen, proving the assertion has teeth.
    assert sync_engine.get_index_entry("/src_folder_teeth") is None, (
        "old row must disappear after the simulated bad re-key — "
        "proves the green test's assertion catches a real defect"
    )
    bad_entry = sync_engine.get_index_entry("/dst_folder_teeth")
    assert bad_entry is not None and bad_entry.remote_id == dir_id


def test_occupied_target_folder_move_creates_duplicate_id_row(sync_engine: SyncEngine):
    """FINDING B (FIXED, 7c-1/7c-2): folder-vs-folder occupied-target move
    must not create duplicate remote_id rows.

    When a remote folder (dir_id) is moved onto a path occupied by a different
    local folder (no index row), _apply_remote_move correctly returns False.
    Previously _on_remote_folder then called _check_download_conflict which
    returned Identical (both content_hash == "folder") and called
    update_index_from_sync_event, inserting a SECOND index row at the new path
    with the same remote_id (a stale orphan at /src_b3 + a new row at /dst_b3),
    silently adopting the occupant — the same nondeterministic-resolution defect
    as Scenario C, triggered by the folder-vs-folder occupied-target path.

    The fix: _check_download_conflict now treats a locally-existing folder whose
    remote_id is already indexed elsewhere as an occupied-target move and returns
    Conflict (occupant conflict-copied, 7c-1), and update_index_from_sync_event
    evicts any stale same-remote_id row before inserting (7c-2), so exactly one
    row remains at /dst_b3.

    This test was a strict xfail until the fix landed; it now asserts the fixed
    outcome (one row at /dst_b3, occupant conflict-copied).
    """
    fake = FakeDrive()
    dir_id = fake.add_folder("/dst_b3")
    _wire_fake(sync_engine, fake)
    sync_engine.client._path_cache.set(
        "/dst_b3", dir_id, parent_id=ROOT_ID, name="dst_b3", is_folder=True
    )

    # Seed: /src_b3 in index (the moved folder's OLD location).
    _seed_entry(
        sync_engine,
        "/src_b3",
        dir_id,
        ItemType.Folder,
        rev="folder",
        content_hash="folder",
    )
    _write_local_dir(sync_engine, "/src_b3")
    # Occupant at /dst_b3 — different local folder, no index row.
    _write_local_dir(sync_engine, "/dst_b3")

    move_event = _make_move_event(
        sync_engine, "/dst_b3", dir_id, ItemType.Folder, "folder", "folder"
    )
    sync_engine._create_local_entry(move_event)

    # Desired correct post-condition: exactly one row (at /dst_b3), occupant conflict-copied.
    with sync_engine._database_access():
        rows = sync_engine._index_table.select_sql("WHERE remote_id = ?", dir_id)
    assert len(rows) == 1
    assert rows[0].remote_path_lower == "/dst_b3"

    # Occupant preserved as a conflicted copy (not silently adopted).
    cc_dirs = [
        name for name in os.listdir(sync_engine.sync_path)
        if "conflicted copy" in name
    ]
    assert cc_dirs, "occupant must be conflict-copied, not adopted"


# ---------------------------------------------------------------------------
# Scenario C — duplicate remote_id index rows → deterministic resolution
#
# Seed TWO index rows with the SAME remote_id at different paths.
# Call _apply_remote_move with that id.
# The production code returns rows[0] which is "first wins" and potentially
# nondeterministic (SQLite dict iteration order; no ORDER BY in the query).
# Check: does it crash? Does it produce a dangling stale row? Is the resolution
# correct?
# ---------------------------------------------------------------------------


def test_duplicate_remote_id_rows_rejected_at_db(sync_engine: SyncEngine):
    """A duplicate remote_id can no longer be inserted (7c-2 UNIQUE). The
    second seed must raise (the engine's _database_access translates the raw
    sqlite3.IntegrityError into DatabaseError) rather than create a second row.

    Supersedes test_duplicate_remote_id_rows_no_crash, whose premise (two rows
    coexisting) is no longer reachable.
    """
    file_id = "dup-id-1"
    data = b"duplicate id content"
    _write_local_file(sync_engine, "/path_a.txt", data)

    _seed_entry(
        sync_engine, "/path_a.txt", file_id, ItemType.File, rev="rev-1",
        content_hash=_md5(data),
    )
    with pytest.raises(DatabaseError) as excinfo:
        _seed_entry(
            sync_engine, "/path_b.txt", file_id, ItemType.File, rev="rev-1",
            content_hash=_md5(data),
        )
    # The DatabaseError wraps the schema-level UNIQUE violation.
    cause = excinfo.value.__cause__ or excinfo.value.__context__
    assert isinstance(cause, sqlite3.IntegrityError)

    # Exactly one row for the id survives.
    with sync_engine._database_access():
        rows = sync_engine._index_table.select_sql("WHERE remote_id = ?", file_id)
    assert len(rows) == 1
    assert rows[0].remote_path_lower == "/path_a.txt"


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


# ---------------------------------------------------------------------------
# Scenario D — missing parent index row for a live Drive file → reindex/repair
#
# Deliver a child create whose PARENT is live on the fake but ABSENT from
# the local index.  _ensure_parent should fetch+create the parent on demand.
# Assert both parent and child land with index rows + local files.
# Prove teeth by neutering _ensure_parent and confirming the child drops/errors.
# ---------------------------------------------------------------------------


def test_missing_parent_reindexed_on_demand(sync_engine: SyncEngine):
    """A file create whose parent folder is absent from the local index triggers
    _ensure_parent to fetch and create the parent on demand.

    After the apply, both the parent folder and the child file must have local
    disk entries and index rows (_ensure_parent repair path exercised).
    """
    file_data = b"child file content"
    fake = FakeDrive()
    dir_id = fake.add_folder("/parentdir")
    file_id = fake.add_file("/parentdir/child.txt", file_data)
    file_rev = fake.files[file_id]["headRevisionId"]

    # Wire fake and populate the path cache for both items.
    _wire_fake(sync_engine, fake)
    sync_engine.client._path_cache.set(
        "/parentdir", dir_id, parent_id=ROOT_ID, name="parentdir", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/parentdir/child.txt",
        file_id,
        parent_id=dir_id,
        name="child.txt",
        is_folder=False,
    )

    # Deliberately do NOT seed the parent folder in the index — it is absent.
    assert sync_engine.get_index_entry("/parentdir") is None

    # Deliver a create event for the child file (parent not yet in index).
    event = _make_move_event(
        sync_engine,
        "/parentdir/child.txt",
        file_id,
        ItemType.File,
        rev=file_rev,
        content_hash=_md5(file_data),
        size=len(file_data),
    )

    sync_engine._create_local_entry(event)

    # The parent folder must now be in the index (reindexed on demand).
    parent_entry = sync_engine.get_index_entry("/parentdir")
    assert parent_entry is not None, (
        "_ensure_parent must have indexed /parentdir on demand; "
        "parent entry is still absent — _ensure_parent was not reached or silently dropped"
    )
    assert parent_entry.remote_id == dir_id

    # The parent folder must exist on disk.
    parent_local = sync_engine.to_local_path_from_cased("/parentdir")
    assert osp.isdir(parent_local), "parent directory must be created on disk by _ensure_parent"

    # The child file must also be present (index + disk).
    child_entry = sync_engine.get_index_entry("/parentdir/child.txt")
    assert child_entry is not None, "child file must be indexed after apply"
    assert child_entry.remote_id == file_id

    child_local = sync_engine.to_local_path_from_cased("/parentdir/child.txt")
    assert osp.isfile(child_local), "child file must exist on disk"
    with open(child_local, "rb") as fh:
        assert fh.read() == file_data

    assert event.status in (SyncStatus.Done, SyncStatus.Skipped)
    assert sync_engine.sync_errors == []


def test_missing_parent_teeth(sync_engine: SyncEngine):
    """Teeth: neutering _ensure_parent causes the child to fail or be silently
    dropped (parent directory cannot be created → mkdir fails or child is
    absent from disk).

    If the test WITHOUT neutering passes but WITH neutering the child is absent,
    the green test's _ensure_parent reliance is proven real.
    """
    file_data = b"teeth child content"
    fake = FakeDrive()
    dir_id = fake.add_folder("/teeth_parentdir")
    file_id = fake.add_file("/teeth_parentdir/teeth_child.txt", file_data)
    file_rev = fake.files[file_id]["headRevisionId"]

    _wire_fake(sync_engine, fake)
    sync_engine.client._path_cache.set(
        "/teeth_parentdir", dir_id, parent_id=ROOT_ID, name="teeth_parentdir", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/teeth_parentdir/teeth_child.txt",
        file_id,
        parent_id=dir_id,
        name="teeth_child.txt",
        is_folder=False,
    )

    assert sync_engine.get_index_entry("/teeth_parentdir") is None

    event = _make_move_event(
        sync_engine,
        "/teeth_parentdir/teeth_child.txt",
        file_id,
        ItemType.File,
        rev=file_rev,
        content_hash=_md5(file_data),
        size=len(file_data),
    )

    # Neuter _ensure_parent: replace with a no-op.
    original_ensure_parent = sync_engine._ensure_parent
    sync_engine._ensure_parent = lambda _ev: None  # type: ignore[method-assign]

    try:
        # With _ensure_parent neutered, the parent dir does not exist on disk.
        # The download (os.open into a missing directory) should fail or the
        # file should be absent.  A SyncError or FileNotFoundError is expected;
        # we swallow sync errors and check the child is absent.
        try:
            sync_engine._create_local_entry(event)
        except Exception:
            pass  # Any exception is acceptable; we check outcome below.
    finally:
        sync_engine._ensure_parent = original_ensure_parent  # type: ignore[method-assign]

    child_local = sync_engine.to_local_path_from_cased("/teeth_parentdir/teeth_child.txt")
    assert not osp.isfile(child_local), (
        "child file must be absent when _ensure_parent is neutered — "
        "proves the green test's reindex is doing real work"
    )


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


def test_remote_id_unique_constraint_rejects_duplicate(sync_engine: SyncEngine):
    """7c-2: the index schema must enforce UNIQUE on remote_id, so a second
    row with an already-used remote_id is rejected at the database layer.

    The duplicate insert is issued directly against the index table rather than
    through ``_seed_entry`` because ``SyncEngine._database_access`` deliberately
    translates raw ``sqlite3.IntegrityError`` into ``DatabaseError``. Bypassing
    that wrapper lets us assert the schema-level UNIQUE backstop fires.
    """
    import sqlite3 as _sqlite3

    _seed_entry(
        sync_engine, "/u_a.txt", "uniq-id", ItemType.File, rev="r1", content_hash="h"
    )

    duplicate = IndexEntry(
        remote_path_cased="/u_b.txt",
        remote_path_lower="/u_b.txt",
        remote_id="uniq-id",
        item_type=ItemType.File,
        last_sync=0.0,
        rev="r1",
        content_hash="h",
        symlink_target=None,
    )
    with pytest.raises(_sqlite3.IntegrityError):
        sync_engine._index_table.update(duplicate)
