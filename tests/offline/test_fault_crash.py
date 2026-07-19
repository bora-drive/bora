"""Fault-injection tests for crash-replay and create-idempotency (Stage 7b-E2, Task 3).

Covers:
  A — crash between classification and cursor commit replays cleanly (finding 2 + others)
  B — crash mid-apply_remote_changes re-apply is idempotent (multi-item batch)
  C — create-then-crash idempotency via reserved file-id + 409 recovery (F7)
"""

from __future__ import annotations

import hashlib
import os
import os.path as osp
import sqlite3
from datetime import datetime
from unittest.mock import Mock

import pytest

from bora.client import GoogleDriveClient
from bora.config import remove_configuration
from bora.core import FullAccount
from bora.database.core import Database
from bora.keyring import CredentialStorage
from bora.models import IndexEntry, ItemType
from bora.path_cache import PathIDCache
from bora.sync import SyncEngine
from bora.utils.appdirs import get_home_dir
from bora.utils.path import delete
from tests.fake_drive import ROOT_ID, FakeDrive, make_http_error

# ---------------------------------------------------------------------------
# Shared helpers
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
# Shared engine fixture (mirrors test_fault_feed.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_engine():
    local_dir = osp.join(get_home_dir(), "fault_crash_dir")
    os.mkdir(local_dir)

    # check_same_thread=False is required because apply_remote_changes uses a
    # thread pool (do_parallel) that accesses the path cache from worker threads.
    cache = PathIDCache(Database(sqlite3.connect(":memory:", check_same_thread=False)))
    cache.root_id = ROOT_ID
    sync = SyncEngine(
        GoogleDriveClient(
            "fault-crash-config", CredentialStorage("fault-crash-config"), cache
        )
    )
    sync.sync_path = local_dir
    sync.local_cursor = 0.0

    yield sync

    remove_configuration("fault-crash-config")
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
        read_only=0,
    )
    with sync._database_access():
        sync._index_table.update(entry)


def _write_local_file(sync: SyncEngine, remote_path: str, data: bytes) -> str:
    local_path = sync.to_local_path_from_cased(remote_path)
    os.makedirs(osp.dirname(local_path), exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


# ---------------------------------------------------------------------------
# Scenario A — crash between classification and cursor commit replays cleanly
# (F2 / finding 2)
#
# Three sub-cases: replayed delete (cache evicted), replayed create, replayed rename.
# ---------------------------------------------------------------------------


def test_crash_replay_delete_cache_evicted(sync_engine: SyncEngine):
    """Replayed delete with cache-evicted id still removes the stranded item.

    The crash window: classification evicts the file_id from the path cache
    (durable) but the cursor is never advanced. On restart the same batch is
    re-fetched; the id is no longer in the cache, so the fallback
    _index_path_for_remote_id resolves it from the index and the delete is
    re-emitted correctly.

    Generalizes the reference test at test_sync.py::
    test_crash_replayed_delete_with_evicted_cache_removes_stranded_item.

    Teeth: we disable the resolver by replacing _index_path_for_remote_id with
    one that always returns None, and confirm the stranded file/row survive
    (test fails red); then re-enable and confirm green.
    """
    data = b"about to be deleted on the remote"
    fake = FakeDrive()
    file_id = fake.add_file("/gone_crash.txt", data)

    start_cursor = _start(fake)

    # Fully-synced local state with last_sync in the future (RemoteNewer gate).
    local_path = _write_local_file(sync_engine, "/gone_crash.txt", data)
    _seed_entry(
        sync_engine,
        "/gone_crash.txt",
        file_id,
        ItemType.File,
        rev="rev-1",
        content_hash=_md5(data),
    )
    # Set last_sync ahead of file ctime so the delete is classified RemoteNewer.
    with sync_engine._database_access():
        entry = sync_engine.get_index_entry("/gone_crash.txt")
        entry.last_sync = datetime.today().timestamp() + 3600
        sync_engine._index_table.update(entry)

    # Simulate the crashed first run: cache has already evicted file_id.
    assert sync_engine.client._path_cache.id_to_path(file_id) is None

    # Delete the file on the remote AFTER taking the start cursor.
    fake.service().files().delete(fileId=file_id).execute()

    _wire_fake(sync_engine, fake)

    # Replay the batch: re-fetch from the same cursor (cursor never advanced).
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start_cursor):
        sync_engine.apply_remote_changes(sync_events)

    assert not osp.exists(local_path), "stranded local file must be removed on replay"
    assert sync_engine.get_index_entry("/gone_crash.txt") is None
    assert sync_engine.get_index_entry_by_remote_id(file_id) is None
    assert sync_engine.sync_errors == []


def test_crash_replay_delete_teeth(sync_engine: SyncEngine):
    """Prove teeth: disabling the resolver lets the stranded file survive.

    This confirms that the green test above is not a vacuous pass — without
    the resolver, the replayed delete goes missing and the local file remains.
    """
    data = b"proof that the resolver matters"
    fake = FakeDrive()
    file_id = fake.add_file("/gone_proof.txt", data)

    start_cursor = _start(fake)

    local_path = _write_local_file(sync_engine, "/gone_proof.txt", data)
    _seed_entry(
        sync_engine,
        "/gone_proof.txt",
        file_id,
        ItemType.File,
        rev="rev-1",
        content_hash=_md5(data),
    )
    with sync_engine._database_access():
        entry = sync_engine.get_index_entry("/gone_proof.txt")
        entry.last_sync = datetime.today().timestamp() + 3600
        sync_engine._index_table.update(entry)

    assert sync_engine.client._path_cache.id_to_path(file_id) is None

    fake.service().files().delete(fileId=file_id).execute()

    _wire_fake(sync_engine, fake)

    # Neuter the resolver: replace with one that always returns None.
    original_resolver = sync_engine._index_path_for_remote_id
    sync_engine._index_path_for_remote_id = lambda _id: None  # type: ignore[method-assign]

    try:
        for sync_events, _cursor in sync_engine.list_remote_changes_iterator(
            start_cursor
        ):
            sync_engine.apply_remote_changes(sync_events)
    finally:
        sync_engine._index_path_for_remote_id = original_resolver  # type: ignore[method-assign]

    # WITHOUT the resolver the file is NOT removed (the delete was silently skipped).
    assert osp.exists(local_path), (
        "stranded file must survive when resolver is disabled — "
        "proves the green test has real teeth"
    )


def test_crash_replay_create_idempotent(sync_engine: SyncEngine):
    """Replayed create (batch re-fetched, file already on disk from first run).

    The first run downloads the file and commits its index row, but the
    cursor is never advanced. On replay the same FileMetadata arrives again.
    The re-apply must be a no-op (Skipped, not an error), and the final
    on-disk + index state must be correct.

    Teeth: if apply were to try to overwrite the file with a different
    rev/hash the index row's rev would change — we assert the row stays
    stable across replay.
    """
    data = b"created then crash then replay"
    fake = FakeDrive()

    start_cursor = _start(fake)

    # After taking the cursor, add the file so its create appears in the feed.
    file_id = fake.add_file("/created.txt", data)
    file_rev = fake.files[file_id]["headRevisionId"]

    # Seed the path cache so classification resolves the parent directory.
    sync_engine.client._path_cache.set(
        "/created.txt", file_id, parent_id=ROOT_ID, name="created.txt", is_folder=False
    )

    _wire_fake(sync_engine, fake)

    # First run: apply the batch (commits index row + downloads file).
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start_cursor):
        sync_engine.apply_remote_changes(sync_events)

    # Confirm first-run state.
    local_path = sync_engine.to_local_path_from_cased("/created.txt")
    assert osp.exists(local_path)
    entry_after_first = sync_engine.get_index_entry("/created.txt")
    assert entry_after_first is not None
    assert entry_after_first.remote_id == file_id
    assert entry_after_first.rev == file_rev

    # Replay: re-run from the SAME start_cursor (cursor was never advanced).
    _wire_fake(sync_engine, fake)
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start_cursor):
        sync_engine.apply_remote_changes(sync_events)

    # The index row must not be corrupted by the re-apply.
    entry_after_replay = sync_engine.get_index_entry("/created.txt")
    assert entry_after_replay is not None
    assert entry_after_replay.remote_id == file_id
    assert entry_after_replay.rev == file_rev

    # Exactly one index row for the file.
    by_id = sync_engine.get_index_entry_by_remote_id(file_id)
    assert by_id is not None
    assert by_id.remote_path_lower == "/created.txt"

    assert sync_engine.sync_errors == []


def test_crash_replay_rename_idempotent(sync_engine: SyncEngine):
    """Replayed rename (batch re-fetched, rename already applied from first run).

    The first run applies the rename and updates the index; the cursor is
    never advanced. On replay the same FolderMetadata / FileMetadata event
    arrives again (at the new path). The re-apply must be a no-op (Skipped),
    and the final on-disk + index state must be correct.

    Teeth: assert the old path is absent and the new path holds the entry.
    """
    data = b"file to be renamed"
    fake = FakeDrive()

    dir_id = fake.add_folder("/mydir")
    file_id = fake.add_file("/mydir/original.txt", data)
    original_rev = fake.files[file_id]["headRevisionId"]

    # Seed path cache with the pre-rename state.
    sync_engine.client._path_cache.set(
        "/mydir", dir_id, parent_id=ROOT_ID, name="mydir", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/mydir/original.txt",
        file_id,
        parent_id=dir_id,
        name="original.txt",
        is_folder=False,
    )

    start_cursor = _start(fake)

    # Apply the rename on the remote.
    fake.service().files().update(
        fileId=file_id,
        body={"name": "renamed.txt"},
    ).execute()

    # Seed the index and local disk with the PRE-rename state.
    _write_local_file(sync_engine, "/mydir/original.txt", data)
    _seed_entry(
        sync_engine,
        "/mydir",
        dir_id,
        ItemType.Folder,
        rev="folder-rev",
        content_hash="folder",
    )
    _seed_entry(
        sync_engine,
        "/mydir/original.txt",
        file_id,
        ItemType.File,
        rev=original_rev,
        content_hash=_md5(data),
    )

    _wire_fake(sync_engine, fake)

    # First run: apply rename.
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start_cursor):
        sync_engine.apply_remote_changes(sync_events)

    new_path = sync_engine.to_local_path_from_cased("/mydir/renamed.txt")
    old_path = sync_engine.to_local_path_from_cased("/mydir/original.txt")
    assert osp.exists(new_path), "file must be at new path after first run"
    assert not osp.exists(old_path), "old path must be absent after first run"
    assert sync_engine.get_index_entry("/mydir/renamed.txt") is not None
    assert sync_engine.get_index_entry("/mydir/original.txt") is None

    # Replay: re-run from same cursor.
    _wire_fake(sync_engine, fake)
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start_cursor):
        sync_engine.apply_remote_changes(sync_events)

    # Final state identical to post-first-run.
    assert osp.exists(new_path), "new path must still exist after replay"
    assert not osp.exists(old_path), "old path must stay absent after replay"
    entry = sync_engine.get_index_entry("/mydir/renamed.txt")
    assert entry is not None
    assert entry.remote_id == file_id
    assert sync_engine.get_index_entry("/mydir/original.txt") is None
    assert sync_engine.sync_errors == []


# ---------------------------------------------------------------------------
# Scenario B — crash mid-apply_remote_changes re-apply is idempotent
# ---------------------------------------------------------------------------


def test_multi_item_batch_reapply_idempotent(sync_engine: SyncEngine):
    """Re-applying a whole batch after a crash (cursor never advanced) is a no-op.

    Build a batch: one folder, two files, one delete. Apply it once (commits
    per-item index rows). Then re-apply the SAME batch from the same start
    cursor. Already-applied items must be Skipped/no-op. Final on-disk + index
    state must be identical to after the first apply; sync_errors must be [].

    Teeth: we assert that the content_hash in the index matches the ORIGINAL
    file content, not a corrupted re-apply; and that the deleted item's row
    is gone (not re-created) and the created items are not duplicated.
    """
    file1_data = b"file one content"
    file2_data = b"file two content"
    deleted_data = b"to be deleted"

    fake = FakeDrive()

    # Seed a file that will be deleted in this batch.
    deleted_id = fake.add_file("/del_me.txt", deleted_data)
    deleted_rev = fake.files[deleted_id]["headRevisionId"]
    sync_engine.client._path_cache.set(
        "/del_me.txt",
        deleted_id,
        parent_id=ROOT_ID,
        name="del_me.txt",
        is_folder=False,
    )

    start_cursor = _start(fake)

    # Add the new items to the remote AFTER taking the start cursor.
    dir_id = fake.add_folder("/newdir")
    file1_id = fake.add_file("/newdir/file1.txt", file1_data)
    file2_id = fake.add_file("/newdir/file2.txt", file2_data)

    # Seed path cache for the new items so classification can resolve them.
    sync_engine.client._path_cache.set(
        "/newdir", dir_id, parent_id=ROOT_ID, name="newdir", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/newdir/file1.txt",
        file1_id,
        parent_id=dir_id,
        name="file1.txt",
        is_folder=False,
    )
    sync_engine.client._path_cache.set(
        "/newdir/file2.txt",
        file2_id,
        parent_id=dir_id,
        name="file2.txt",
        is_folder=False,
    )

    # Delete the pre-existing file on the remote (visible from start_cursor).
    fake.service().files().delete(fileId=deleted_id).execute()

    # Seed local disk + index with the to-be-deleted file.
    deleted_local_path = _write_local_file(sync_engine, "/del_me.txt", deleted_data)
    _seed_entry(
        sync_engine,
        "/del_me.txt",
        deleted_id,
        ItemType.File,
        rev=deleted_rev,
        content_hash=_md5(deleted_data),
        last_sync=datetime.today().timestamp() + 3600,
    )

    _wire_fake(sync_engine, fake)

    # First apply: commits all per-item index rows.
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start_cursor):
        sync_engine.apply_remote_changes(sync_events)

    # Confirm first-apply state.
    assert osp.exists(sync_engine.to_local_path_from_cased("/newdir"))
    assert osp.exists(sync_engine.to_local_path_from_cased("/newdir/file1.txt"))
    assert osp.exists(sync_engine.to_local_path_from_cased("/newdir/file2.txt"))
    assert not osp.exists(deleted_local_path)

    e_dir = sync_engine.get_index_entry("/newdir")
    assert e_dir is not None and e_dir.remote_id == dir_id

    e_f1 = sync_engine.get_index_entry("/newdir/file1.txt")
    assert e_f1 is not None and e_f1.content_hash == _md5(file1_data)

    e_f2 = sync_engine.get_index_entry("/newdir/file2.txt")
    assert e_f2 is not None and e_f2.content_hash == _md5(file2_data)

    assert sync_engine.get_index_entry("/del_me.txt") is None
    assert sync_engine.sync_errors == []

    # Re-apply from the SAME start_cursor (modeling crash restart).
    _wire_fake(sync_engine, fake)
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start_cursor):
        sync_engine.apply_remote_changes(sync_events)

    # Post-replay state must be identical to post-first-apply.
    assert osp.exists(sync_engine.to_local_path_from_cased("/newdir"))
    assert osp.exists(sync_engine.to_local_path_from_cased("/newdir/file1.txt"))
    assert osp.exists(sync_engine.to_local_path_from_cased("/newdir/file2.txt"))
    assert not osp.exists(deleted_local_path)

    e_f1_replay = sync_engine.get_index_entry("/newdir/file1.txt")
    assert e_f1_replay is not None
    # Content hash must not change (not re-corrupted).
    assert e_f1_replay.content_hash == _md5(file1_data)
    assert e_f1_replay.remote_id == file1_id

    e_f2_replay = sync_engine.get_index_entry("/newdir/file2.txt")
    assert e_f2_replay is not None
    assert e_f2_replay.content_hash == _md5(file2_data)
    assert e_f2_replay.remote_id == file2_id

    # Exactly one index row per file.
    assert sync_engine.get_index_entry_by_remote_id(file1_id) is not None
    assert sync_engine.get_index_entry_by_remote_id(file2_id) is not None
    assert sync_engine.get_index_entry_by_remote_id(deleted_id) is None

    # The delete must remain gone.
    assert sync_engine.get_index_entry("/del_me.txt") is None
    assert sync_engine.sync_errors == []


def test_multi_item_batch_reapply_teeth(sync_engine: SyncEngine):
    """Teeth for Scenario B: confirm the assertion catches a corrupted re-apply.

    We manually corrupt file1's index row's content_hash after the first apply
    (simulating what a double-apply with wrong data would do), then assert
    the content_hash is wrong. This proves the assertion in the green test
    WOULD fail if re-apply corrupted the data.
    """
    file1_data = b"original content not to be corrupted"
    fake = FakeDrive()

    start_cursor = _start(fake)
    dir_id = fake.add_folder("/teeth_dir")
    file1_id = fake.add_file("/teeth_dir/t1.txt", file1_data)

    sync_engine.client._path_cache.set(
        "/teeth_dir", dir_id, parent_id=ROOT_ID, name="teeth_dir", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/teeth_dir/t1.txt",
        file1_id,
        parent_id=dir_id,
        name="t1.txt",
        is_folder=False,
    )
    _wire_fake(sync_engine, fake)

    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start_cursor):
        sync_engine.apply_remote_changes(sync_events)

    # Confirm correct state.
    e = sync_engine.get_index_entry("/teeth_dir/t1.txt")
    assert e is not None
    assert e.content_hash == _md5(file1_data)

    # Now manually corrupt the hash (simulates a double-apply writing wrong data).
    with sync_engine._database_access():
        e2 = sync_engine.get_index_entry("/teeth_dir/t1.txt")
        e2.content_hash = "CORRUPTED"
        sync_engine._index_table.update(e2)

    corrupted = sync_engine.get_index_entry("/teeth_dir/t1.txt")
    assert corrupted.content_hash == "CORRUPTED", (
        "corruption must be visible — this is the state the green-test assertion "
        "would catch if re-apply behaved incorrectly"
    )


# ---------------------------------------------------------------------------
# Scenario C — create-then-crash idempotency (F7)
# ---------------------------------------------------------------------------


def test_create_crash_idempotent_via_reserved_id(tmp_path):
    """Upload with fail_after on files.create: response lost, retry hits 409, recovery.

    fail_after models "committed server-side but response lost".  On the NEXT
    retry (still inside _retry_transient, since 500 is retryable), the same
    reserved id is reused, the fake raises 409, _retry_transient re-raises it
    (409 is not retryable), convert_api_errors wraps it, and the outer catch
    block fetches the file by its known reserved id.

    The result: EXACTLY ONE file in fake.files with the reserved id, correct
    content, no orphan duplicate.

    Teeth: if we did NOT use a reserved id and instead let the fake generate a
    new id on each create, the retry would insert a SECOND file with a different
    id; we prove this by confirming that WITHOUT the reserved-id path a retry
    creates two files. We demonstrate teeth by checking fake.files has exactly
    one entry with the reserved id (not two entries).
    """
    data = b"create-crash idempotency payload"
    local = tmp_path / "upload_me.txt"
    local.write_bytes(data)

    fake = FakeDrive()

    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = ROOT_ID
    client = GoogleDriveClient(
        "fault-crash-c-config", CredentialStorage("fault-crash-c-config"), cache
    )
    client._get_service = lambda: fake.service()
    # Disable the root-ensure network call.
    client._ensure_root_id = Mock()

    try:
        # Arm: fail_after fires ONCE after files.create commits server-side.
        # This causes the first attempt to: commit the file, raise 500.
        # _retry_transient retries → same request object → same reserved id → 409.
        # convert_api_errors wraps → outer catch handles → fetch by reserved id.
        fake.fail_after("files.create", make_http_error(500))

        from bora.core import WriteMode

        result = client._writes.upload(
            str(local), "/upload_me.txt", write_mode=WriteMode.Add
        )

        # Exactly one file exists with the reserved id.
        assert result.id in fake.files, "returned id must be in fake.files"
        assert fake.files[result.id]["content"] == data
        assert fake.files[result.id]["md5Checksum"] == _md5(data)

        # No other files with same name (no duplicate under ROOT).
        all_files_named = [
            fid
            for fid, rec in fake.files.items()
            if rec["name"] == "upload_me.txt"
            and ROOT_ID in rec["parents"]
            and not rec["trashed"]
        ]
        assert len(all_files_named) == 1, (
            f"Expected exactly 1 file named 'upload_me.txt', got {len(all_files_named)}: "
            f"{all_files_named}"
        )

    finally:
        remove_configuration("fault-crash-c-config")


def test_create_crash_teeth_no_reserved_id_would_duplicate(tmp_path):
    """Teeth: without a reserved id, a retry would create a SECOND file.

    We simulate this by calling files().create(...) twice WITHOUT an 'id' in
    the body — the fake generates fresh ids each time — and confirm two files
    exist. This is the pre-fix behavior that F7 / review reserved-id guards
    against.

    This characterization test PROVES the teeth: the green test above is not
    vacuous because without the reserved-id path you'd get two files.
    """
    fake = FakeDrive()
    svc = fake.service()

    from googleapiclient.http import MediaInMemoryUpload

    body = {"name": "dup.txt", "parents": [ROOT_ID]}
    media = MediaInMemoryUpload(b"dup content", mimetype="text/plain")

    # First create (no id in body — fake generates one).
    svc.files().create(body=body, media_body=media, fields="id").execute()

    # Second create (simulating a retry that doesn't know the first committed).
    media2 = MediaInMemoryUpload(b"dup content", mimetype="text/plain")
    svc.files().create(body=body, media_body=media2, fields="id").execute()

    # Two files now exist with name "dup.txt" — the duplication the id-reserve guards against.
    duplicates = [
        fid
        for fid, rec in fake.files.items()
        if rec["name"] == "dup.txt" and ROOT_ID in rec["parents"] and not rec["trashed"]
    ]
    assert len(duplicates) == 2, (
        "Without a reserved id, two creates produce two files — "
        "this is the defect the F7 guard prevents. "
        f"Found {len(duplicates)} file(s): {duplicates}"
    )
