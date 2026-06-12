import hashlib
import os
import os.path as osp
import sqlite3
from datetime import datetime
from queue import Queue
from unittest.mock import Mock

import pytest
from pathspec import PathSpec

from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.core import FileMetadata, FullAccount
from cumulus.database.core import Database
from cumulus.exceptions import DataChangedError
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
from cumulus.path_codec import encode_path, fold_path
from cumulus.sync import ActivityNode, ActivityTree, Conflict, SyncEngine
from cumulus.utils.appdirs import get_home_dir
from cumulus.utils.path import delete
from tests.fake_drive import ROOT_ID, FakeDrive

EVENT1 = SyncEvent(
    remote_path="/d0/file1.txt",
    direction=SyncDirection.Up,
    status=SyncStatus.Syncing,
    local_path="/d0/file1.txt",
    remote_path_lower="/d0/file1.txt",
    change_type=ChangeType.Added,
    completed=0,
    size=10,
    item_type=ItemType.File,
    sync_time=datetime.today(),
)
EVENT2 = SyncEvent(
    remote_path="/d0/file2.txt",
    direction=SyncDirection.Up,
    status=SyncStatus.Syncing,
    local_path="/d0/file2.txt",
    remote_path_lower="/d0/file2.txt",
    change_type=ChangeType.Added,
    completed=0,
    size=10,
    item_type=ItemType.File,
    sync_time=datetime.today(),
)
EVENT2_FAILED = SyncEvent(
    remote_path="/d0/file2.txt",
    direction=SyncDirection.Up,
    status=SyncStatus.Failed,
    local_path="/d0/file2.txt",
    remote_path_lower="/d0/file2.txt",
    change_type=ChangeType.Added,
    completed=0,
    size=10,
    item_type=ItemType.File,
    sync_time=datetime.today(),
)


def test_activity_tree_add_single() -> None:
    tree = ActivityTree()
    tree.add(EVENT1)

    assert_in_tree(tree, EVENT1)
    assert_tree_integrity(tree)


def test_activity_tree_add_multiple() -> None:
    tree = ActivityTree()
    tree.add(EVENT1)
    tree.add(EVENT2)

    assert_in_tree(tree, EVENT1)
    assert_in_tree(tree, EVENT2)

    assert_tree_integrity(tree)


def test_activity_tree_remove_single() -> None:
    tree = ActivityTree()
    tree.add(EVENT1)
    tree.remove(EVENT1)

    assert EVENT1 not in tree.sync_events
    assert len(tree.children) == 0
    assert not tree.has_path(EVENT1.remote_path)

    assert_tree_integrity(tree)


def test_activity_tree_remove_multiple() -> None:
    tree = ActivityTree()
    tree.add(EVENT1)
    tree.add(EVENT2)

    tree.remove(EVENT1)

    assert_in_tree(tree, EVENT2)
    assert_not_in_tree(tree, EVENT1)

    tree.remove(EVENT2)

    assert_not_in_tree(tree, EVENT2)
    assert len(tree.children) == 0

    assert_tree_integrity(tree)


def test_activity_tree_failed_replaced() -> None:
    tree = ActivityTree()
    tree.add(EVENT1)
    tree.add(EVENT2_FAILED)

    assert_in_tree(tree, EVENT1)
    assert_in_tree(tree, EVENT2_FAILED)

    tree.add(EVENT2)

    assert_in_tree(tree, EVENT2)
    assert_not_in_tree(tree, EVENT2_FAILED)

    assert_tree_integrity(tree)


def assert_in_tree(tree: ActivityTree, event: SyncEvent) -> None:
    assert event in tree.sync_events
    assert tree.has_path(event.remote_path)

    parts = event.remote_path.lstrip("/").split("/")

    node = tree

    for part in parts:
        assert event in node.children[part].sync_events
        node = node.children[part]

    assert tree.get_node(event.remote_path) is node


def assert_not_in_tree(tree: ActivityTree, event: SyncEvent) -> None:
    # Traverse tree, ensure that the event is not attached to any node.
    queue = Queue()
    queue.put(tree)

    while not queue.empty():
        node = queue.get()
        assert event not in node.sync_events

        for child in node.children.values():
            queue.put(child)


def assert_tree_integrity(node: ActivityNode) -> None:
    if node.parent:
        # The parent node should contain all of this node's sync events.
        assert node.sync_events.issubset(node.parent.sync_events)
        # Empty nodes with parent should be removed (apart from the root node).
        assert len(node.sync_events) > 0, node

    # Recurse.
    for child in node.children.values():
        assert_tree_integrity(child)


# ==== Conflict resolution under Drive-shaped values ==================================
#
# After Stages 1-4 of the Google Drive port:
#   * IndexEntry.rev / SyncEvent.rev carry Drive's ``headRevisionId``.
#   * IndexEntry.content_hash / SyncEvent.content_hash carry the MD5 checksum
#     (``md5Checksum``) that ``content_hash()`` now computes locally.
#
# These tests prove that ``_check_download_conflict`` and ``_check_requires_upload``
# behave correctly once rev == headRevisionId and content_hash == MD5. They use a
# minimal offline SyncEngine fixture with a real on-disk sync folder so that
# ``get_local_hash`` reads actual bytes and ``_ctime_newer_than_last_sync`` reads
# actual ctimes.


@pytest.fixture
def sync_engine():
    """Offline SyncEngine with an in-memory DB and a real local sync folder."""
    local_dir = osp.join(get_home_dir(), "conflict_dummy_dir")
    os.mkdir(local_dir)

    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = "ROOT"
    sync = SyncEngine(
        GoogleDriveClient("test-config", CredentialStorage("test-config"), cache)
    )
    sync.sync_path = local_dir
    # Pin local_cursor low so get_last_sync is governed by per-entry last_sync.
    sync.local_cursor = 0.0

    yield sync

    remove_configuration("test-config")
    delete(local_dir)


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _write_local_file(sync: SyncEngine, remote_path: str, data: bytes) -> str:
    """Write ``data`` to the local path mirroring ``remote_path``; return local path."""
    local_path = sync.to_local_path_from_cased(remote_path)
    os.makedirs(osp.dirname(local_path), exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


def _seed_index_entry(
    sync: SyncEngine,
    remote_path: str,
    rev: str,
    content_hash: str | None,
    last_sync: float,
) -> None:
    """Insert an IndexEntry for ``remote_path`` directly via the engine's index API."""
    remote_path_lower = remote_path.lower()
    entry = IndexEntry(
        remote_path_cased=remote_path,
        remote_path_lower=remote_path_lower,
        remote_id="id:seed",
        item_type=ItemType.File,
        last_sync=last_sync,
        rev=rev,
        content_hash=content_hash,
        symlink_target=None,
    )
    with sync._database_access():
        sync._index_table.update(entry)


def _download_event(
    sync: SyncEngine,
    remote_path: str,
    rev: str | None,
    content_hash: str | None,
    change_type: ChangeType = ChangeType.Modified,
) -> SyncEvent:
    """Build a download SyncEvent with the given remote rev / content_hash."""
    return SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id="id:remote",
        local_path=sync.to_local_path_from_cased(remote_path),
        rev=rev,
        content_hash=content_hash,
        symlink_target=None,
        change_type=change_type,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )


def test_conflict_equal_rev_is_local_newer_or_identical(sync_engine: SyncEngine):
    """Most load-bearing assertion: equal headRevisionId => skip the download.

    Echo suppression depends on this: a remote change we already have (same
    headRevisionId in our index) must not overwrite local state.
    """
    remote_path = "/equal_rev.txt"
    data = b"local and remote agree"
    _write_local_file(sync_engine, remote_path, data)
    _seed_index_entry(
        sync_engine,
        remote_path,
        rev="REV-HEAD-1",
        content_hash=_md5(data),
        last_sync=0.0,
    )

    # Remote event carries the SAME headRevisionId as our index.
    event = _download_event(
        sync_engine, remote_path, rev="REV-HEAD-1", content_hash="REMOTE-DIFFERENT-HASH"
    )

    assert (
        sync_engine._check_download_conflict(event)
        is Conflict.LocalNewerOrIdentical
    )


def test_conflict_equal_hash_is_identical(sync_engine: SyncEngine):
    """Different headRevisionId but identical MD5 => Identical (no real change)."""
    remote_path = "/equal_hash.txt"
    data = b"identical content, new revision id"
    _write_local_file(sync_engine, remote_path, data)
    _seed_index_entry(
        sync_engine,
        remote_path,
        rev="REV-OLD",
        content_hash=_md5(data),
        last_sync=0.0,
    )

    # Different rev, but the remote MD5 equals the local file's MD5.
    event = _download_event(
        sync_engine, remote_path, rev="REV-NEW", content_hash=_md5(data)
    )

    assert sync_engine._check_download_conflict(event) is Conflict.Identical


def test_conflict_remote_newer_when_no_local_changes(sync_engine: SyncEngine):
    """ctime older than last_sync + differing hashes => RemoteNewer.

    We force "no unsynced local changes" by seeding last_sync far in the future,
    so the local file's ctime is necessarily older than last_sync.
    """
    remote_path = "/remote_newer.txt"
    local_data = b"local synced content"
    _write_local_file(sync_engine, remote_path, local_data)
    future = datetime.today().timestamp() + 10_000_000
    _seed_index_entry(
        sync_engine,
        remote_path,
        rev="REV-OLD",
        content_hash=_md5(local_data),
        last_sync=future,
    )

    # Remote changed: new rev, different content hash.
    event = _download_event(
        sync_engine,
        remote_path,
        rev="REV-NEW",
        content_hash=_md5(b"remote changed content"),
    )

    assert sync_engine._check_download_conflict(event) is Conflict.RemoteNewer


def test_conflict_both_changed_is_conflict(sync_engine: SyncEngine):
    """Both sides changed (no upload error) => Conflict.

    Local has unsynced changes (ctime newer than last_sync=0) and the remote
    hash differs, with no recorded upload error.
    """
    remote_path = "/both_changed.txt"
    local_data = b"local unsynced edit"
    _write_local_file(sync_engine, remote_path, local_data)
    _seed_index_entry(
        sync_engine,
        remote_path,
        rev="REV-OLD",
        content_hash=_md5(b"previously synced content"),
        last_sync=0.0,
    )

    event = _download_event(
        sync_engine,
        remote_path,
        rev="REV-NEW",
        content_hash=_md5(b"remote unsynced edit"),
    )

    assert sync_engine._check_download_conflict(event) is Conflict.Conflict


def test_requires_upload_skips_when_remote_hash_matches(
    sync_engine: SyncEngine, monkeypatch
):
    """_check_requires_upload returns False when remote MD5 == local content_hash."""
    remote_path = "/no_upload.txt"
    data = b"content already on remote"
    local_path = _write_local_file(sync_engine, remote_path, data)
    md5 = _md5(data)

    md_remote = FileMetadata(
        name="no_upload.txt",
        path_lower=remote_path.lower(),
        path_display=remote_path,
        id="id:remote",
        modified=datetime.today(),
        rev="REV-REMOTE",
        size=len(data),
        symlink_target=None,
        shared=False,
        modified_by=None,
        is_downloadable=True,
        content_hash=md5,
    )
    monkeypatch.setattr(
        sync_engine.client, "get_metadata", lambda path: md_remote
    )

    event = SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id="id:remote",
        local_path=local_path,
        rev=None,
        content_hash=md5,
        symlink_target=None,
        change_type=ChangeType.Modified,
        status=SyncStatus.Queued,
        size=len(data),
        completed=0,
    )

    assert sync_engine._check_requires_upload(event) is False


def test_requires_upload_true_when_remote_hash_differs(
    sync_engine: SyncEngine, monkeypatch
):
    """_check_requires_upload returns True when remote MD5 differs from local."""
    remote_path = "/needs_upload.txt"
    data = b"new local content"
    local_path = _write_local_file(sync_engine, remote_path, data)

    md_remote = FileMetadata(
        name="needs_upload.txt",
        path_lower=remote_path.lower(),
        path_display=remote_path,
        id="id:remote",
        modified=datetime.today(),
        rev="REV-REMOTE",
        size=10,
        symlink_target=None,
        shared=False,
        modified_by=None,
        is_downloadable=True,
        content_hash=_md5(b"different remote content"),
    )
    monkeypatch.setattr(
        sync_engine.client, "get_metadata", lambda path: md_remote
    )

    event = SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id="id:remote",
        local_path=local_path,
        rev=None,
        content_hash=_md5(data),
        symlink_target=None,
        change_type=ChangeType.Modified,
        status=SyncStatus.Queued,
        size=len(data),
        completed=0,
    )

    assert sync_engine._check_requires_upload(event) is True


def test_requires_upload_true_when_remote_not_found(
    sync_engine: SyncEngine, monkeypatch
):
    """A brand-new local file has no remote counterpart, so the Google Drive
    client's ``get_metadata`` raises ``NotFoundError`` (the path is unknown to
    its cache). ``_check_requires_upload`` must treat that as "remote absent ->
    upload required" and return True, not let the error propagate.

    Regression test for the Stage 5 smoke-test finding: the offline suite
    previously only ever monkeypatched ``get_metadata`` to *return* a value, so
    the NotFoundError-raising path (which broke every new-file upload against a
    real Drive) was never exercised.
    """
    from cumulus.exceptions import NotFoundError

    remote_path = "/brand_new.txt"
    data = b"never uploaded before"
    local_path = _write_local_file(sync_engine, remote_path, data)

    def _raise_not_found(path):
        raise NotFoundError(
            "Not found",
            f"No item at '{path}' is known to Cumulus.",
            remote_path=path,
        )

    monkeypatch.setattr(sync_engine.client, "get_metadata", _raise_not_found)

    event = SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=None,
        local_path=local_path,
        rev=None,
        content_hash=_md5(data),
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=len(data),
        completed=0,
    )

    assert sync_engine._check_requires_upload(event) is True


def test_echo_suppression_records_headrevisionid_and_skips_download(
    sync_engine: SyncEngine,
):
    """The single most important Stage 5 assertion: after recording an upload's
    returned metadata into the index, the change-feed echo of that same write
    is skipped because its headRevisionId matches our index rev.

    This proves rev-based echo suppression -- NOT content-hash identity. The
    echo event deliberately carries a DIFFERENT content_hash from the local
    file, so the skip can only come from the rev match.
    """
    remote_path = "/echo.txt"
    data = b"freshly uploaded bytes"
    # The local file exists (mirrors a just-written file) but its bytes have a
    # different hash than the echo event carries, ruling out the Identical path.
    _write_local_file(sync_engine, remote_path, data)

    # Simulate the post-upload metadata the Drive write returns.
    md = FileMetadata(
        name="echo.txt",
        path_lower=remote_path.lower(),
        path_display=remote_path,
        id="id:echo",
        modified=datetime.today(),
        rev="HEADREVX",
        size=len(data),
        symlink_target=None,
        shared=False,
        modified_by=None,
        is_downloadable=True,
        content_hash=_md5(data),
    )

    # _on_local_file_modified records exactly this into the index.
    sync_engine.update_index_from_remote_metadata(md)
    assert sync_engine.get_local_rev(remote_path.lower()) == "HEADREVX"

    # The change feed re-delivers our own write: same headRevisionId, but a
    # different content_hash (so a hash match cannot be what suppresses it).
    echo = _download_event(
        sync_engine,
        remote_path,
        rev="HEADREVX",
        content_hash="A-DIFFERENT-REMOTE-HASH",
    )

    assert (
        sync_engine._check_download_conflict(echo)
        is Conflict.LocalNewerOrIdentical
    )


# ==== Cluster A conflict chain (upload signal + download check) ======================
#
# A modify-upload whose remote headRevisionId moved under us raises
# DataChangedError in writes.upload — the definitive "remote changed" conflict
# signal. The download side's "unresolved upload error => Conflict" branch in
# _check_download_conflict relies on a recorded Up sync error for the path; if
# the upload side swallows the signal, conflict detection degrades to a
# timing-sensitive ctime heuristic and the local edit can be overwritten
# without a conflicted copy.


def _local_modified_event(
    sync: SyncEngine, remote_path: str, content_hash: str
) -> SyncEvent:
    """Build a local file-modified upload SyncEvent."""
    return SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id="id:remote",
        local_path=sync.to_local_path_from_cased(remote_path),
        rev=None,
        content_hash=content_hash,
        symlink_target=None,
        change_type=ChangeType.Modified,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )


def test_upload_rev_mismatch_records_conflict_not_silent_skip(
    sync_engine: SyncEngine,
):
    """A modify-upload whose remote rev moved must surface a conflict signal.

    Regression for the cluster-A data-loss bug: the rev-mismatch
    DataChangedError was swallowed as SyncStatus.Skipped with no sync error
    recorded, so the download side never created a conflicted copy and the
    remote change overwrote the local edit.
    """
    remote_path = "/conflict_up.txt"

    # FakeDrive holds the file at a NEWER head revision ("rev-1", assigned by
    # the fake) than our index records below.
    fake = FakeDrive()
    file_id = fake.add_file(remote_path, content=b"remote content 2")
    assert fake.files[file_id]["headRevisionId"] != "REV-OLD"
    sync_engine.client._get_service = Mock(return_value=fake.service())
    # Point the path cache at the fake file so the upload resolves it.
    sync_engine.client._path_cache.set(
        remote_path,
        file_id,
        parent_id=ROOT_ID,
        name="conflict_up.txt",
        is_folder=False,
    )

    # Index + local disk reflect an OLDER rev with locally-modified content.
    local_data = b"local edit the user wants to keep"
    _write_local_file(sync_engine, remote_path, local_data)
    _seed_index_entry(
        sync_engine,
        remote_path,
        rev="REV-OLD",
        content_hash=_md5(b"original content"),
        last_sync=0.0,
    )

    event = _local_modified_event(
        sync_engine, remote_path, content_hash=_md5(local_data)
    )

    # Drive the full upload wrapper (it catches SyncError and records it).
    sync_engine._create_remote_entry(event)

    # The conflict signal MUST be recorded for the path on the Up direction, so
    # the download side will create the conflicted copy instead of overwriting.
    up_errors = sync_engine.sync_errors_for_path(
        remote_path.lower(), direction=SyncDirection.Up
    )
    assert (
        len(up_errors) > 0
    ), "rev-mismatch upload silently skipped — local edit would be lost"

    # The event must also be marked Failed (set by the `except SyncError`
    # branch in _create_remote_entry) — guards against a regression where an
    # error is recorded but the event is still Done/Skipped and errors get
    # cleared.
    assert event.status == SyncStatus.Failed


def test_download_conflict_when_upload_error_present(sync_engine: SyncEngine):
    """With an unresolved Up error for the path, the remote change is a Conflict
    (which triggers conflicted-copy creation) — not a RemoteNewer overwrite.

    We seed ``last_sync`` in the future so that the ctime heuristic classifies
    the event as RemoteNewer (asserted as the baseline below); recording an Up
    error for the path must take precedence and force Conflict.
    """
    remote_path = "/conflict_down.txt"
    local_data = b"local edit"
    _write_local_file(sync_engine, remote_path, local_data)
    future = datetime.today().timestamp() + 10_000_000
    _seed_index_entry(
        sync_engine,
        remote_path,
        rev="REV-OLD",
        content_hash=_md5(b"original"),
        last_sync=future,
    )

    event = _download_event(
        sync_engine,
        remote_path,
        rev="REV-REMOTE-NEW",
        content_hash=_md5(b"remote content 2"),
    )

    # Baseline: with no recorded upload error, the heuristic says RemoteNewer
    # (the data-loss outcome this chain prevents).
    assert sync_engine._check_download_conflict(event) is Conflict.RemoteNewer

    # Simulate the upload having recorded an Up conflict error for this path.
    err = DataChangedError(
        "Item changed on Google Drive",
        "rev mismatch",
        remote_path=remote_path,
        local_path=sync_engine.to_local_path_from_cased(remote_path),
    )
    sync_engine._handle_sync_error(err, direction=SyncDirection.Up)

    # With the Up error recorded, the same event is a Conflict (conflicted copy).
    assert sync_engine._check_download_conflict(event) is Conflict.Conflict


# ==== Cluster B (B1): case-only local rename must not autorename remotely ===========
#
# A local case-only rename /folder -> /FOLDER goes up as client.move(...,
# autorename=True). The autorename collision probe is case-folded, so the
# folded destination equals the folded source and the probe used to find the
# item BEING MOVED itself — a false collision that renamed the folder to
# "FOLDER (1)" remotely and then mirrored the bogus conflict locally.


def test_local_case_only_folder_rename_does_not_autorename(sync_engine: SyncEngine):
    """A local case-only folder rename must arrive remotely as a plain rename."""
    fake = FakeDrive()
    folder_id = fake.add_folder("/folder")
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._path_cache.set(
        "/folder", folder_id, parent_id=ROOT_ID, name="folder", is_folder=True
    )

    # Index tracks the folder under its old casing.
    entry = IndexEntry(
        remote_path_cased="/folder",
        remote_path_lower="/folder",
        remote_id=folder_id,
        item_type=ItemType.Folder,
        last_sync=0.0,
        rev="folder",
        content_hash="folder",
        symlink_target=None,
    )
    with sync_engine._database_access():
        sync_engine._index_table.update(entry)

    # On disk the rename has already happened: only FOLDER exists.
    local_path_to = sync_engine.to_local_path_from_cased("/FOLDER")
    local_path_from = sync_engine.to_local_path_from_cased("/folder")
    os.mkdir(local_path_to)

    event = SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.Folder,
        sync_time=datetime.today().timestamp(),
        remote_path="/FOLDER",
        remote_path_lower="/folder",
        remote_path_from="/folder",
        remote_path_from_lower="/folder",
        remote_id=folder_id,
        local_path=local_path_to,
        local_path_from=local_path_from,
        rev=None,
        content_hash=None,
        symlink_target=None,
        change_type=ChangeType.Moved,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )

    sync_engine._create_remote_entry(event)

    # The remote folder kept its identity and took the new casing — and no
    # spurious "FOLDER (1)" sibling was created.
    assert fake.files[folder_id]["name"] == "FOLDER"
    assert "FOLDER (1)" not in [r["name"] for r in fake.files.values()]

    meta = sync_engine.client.get_metadata("/FOLDER")
    assert meta is not None
    assert meta.name == "FOLDER"

    # No bogus conflict was mirrored locally: the dir is still named FOLDER.
    assert osp.isdir(local_path_to)
    assert event.status == SyncStatus.Done


# ==== Cluster B (B2): remote case-only folder rename must re-case child index =======
#
# Drive's change feed delivers a SINGLE change for a renamed folder — unlike
# Dropbox, which emits delta entries for the folder AND all children. So when
# /folder is re-cased to /FOLDER remotely, _apply_case_change renames the local
# directory (taking the whole subtree with it on disk) and updates the folder's
# own index entry, but the children's index entries kept the stale cased path
# ("/folder/Subfolder"), diverging from the on-disk "/FOLDER/Subfolder".


def test_remote_case_only_folder_rename_recases_child_index(sync_engine: SyncEngine):
    """A remote case-only folder rename must update child index casing too."""
    fake = FakeDrive()
    # The remote rename has already happened: Drive holds /FOLDER/Subfolder.
    folder_id = fake.add_folder("/FOLDER")
    sub_id = fake.add_folder("/FOLDER/Subfolder")
    nested_id = fake.add_file("/FOLDER/Subfolder/nested.txt", b"nested")
    sync_engine.client._get_service = Mock(return_value=fake.service())
    # Path cache as the change feed leaves it (PathIDCache.move re-cases children).
    sync_engine.client._path_cache.set(
        "/FOLDER", folder_id, parent_id=ROOT_ID, name="FOLDER", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/FOLDER/Subfolder", sub_id, parent_id=folder_id, name="Subfolder", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/FOLDER/Subfolder/nested.txt",
        nested_id,
        parent_id=sub_id,
        name="nested.txt",
        is_folder=False,
    )

    # Index + disk reflect the OLD casing (fully synced before the remote rename).
    for cased, rid, item_type in [
        ("/folder", folder_id, ItemType.Folder),
        ("/folder/Subfolder", sub_id, ItemType.Folder),
        ("/folder/Subfolder/nested.txt", nested_id, ItemType.File),
    ]:
        entry = IndexEntry(
            remote_path_cased=cased,
            remote_path_lower=cased.lower(),
            remote_id=rid,
            item_type=item_type,
            last_sync=0.0,
            rev="folder",
            content_hash="folder",
            symlink_target=None,
        )
        with sync_engine._database_access():
            sync_engine._index_table.update(entry)
    os.makedirs(sync_engine.to_local_path_from_cased("/folder/Subfolder"))
    with open(
        sync_engine.to_local_path_from_cased("/folder/Subfolder/nested.txt"), "wb"
    ) as f:
        f.write(b"nested")

    # The change feed delivers ONE folder event, for the renamed folder itself.
    event = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.Folder,
        sync_time=datetime.today().timestamp(),
        remote_path="/FOLDER",
        remote_path_lower="/folder",
        remote_id=folder_id,
        local_path=sync_engine.to_local_path_from_cased("/FOLDER"),
        rev="folder",
        content_hash="folder",
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )

    sync_engine._create_local_entry(event)

    # On-disk directory renamed with the exact new casing (listdir, not stat,
    # so a case-insensitive file system cannot mask a stale name).
    assert "FOLDER" in os.listdir(sync_engine.sync_path)
    assert "folder" not in os.listdir(sync_engine.sync_path)
    assert "Subfolder" in os.listdir(osp.join(sync_engine.sync_path, "FOLDER"))

    # Index: the folder's own entry was re-cased ...
    parent_entry = sync_engine.get_index_entry("/folder")
    assert parent_entry is not None
    assert parent_entry.remote_path_cased == "/FOLDER"

    # ... and so was the child's (the B2 regression: it stayed "/folder/Subfolder").
    child_entry = sync_engine.get_index_entry("/folder/subfolder")
    assert child_entry is not None
    assert child_entry.remote_path_cased == "/FOLDER/Subfolder"

    # ... including grandchildren, not just direct children.
    grandchild_entry = sync_engine.get_index_entry("/folder/subfolder/nested.txt")
    assert grandchild_entry is not None
    assert grandchild_entry.remote_path_cased == "/FOLDER/Subfolder/nested.txt"

    # The case-conversion cache was refreshed alongside the index rows.
    assert sync_engine._case_conversion_cache.get("/folder/subfolder") == (
        "/FOLDER/Subfolder"
    )

    # The event terminated as a success, not Failed. The folder's content is
    # identical (rev matches the index), so after applying the case change
    # _on_remote_folder reports Skipped — which the engine treats as success
    # alongside Done.
    assert event.status == SyncStatus.Skipped


# ==== Cluster C: out-of-order remote change application ==============================
#
# Drive's change feed does NOT guarantee parent-before-child ordering (see
# docs/superpowers/references/dropbox-vs-drive-differences.md §14). When remote
# changes are applied children-before-parent via _create_local_entry,
# _ensure_parent must sync the missing parent on demand so that every item ends
# up both on disk and in the index, with no event failing.


def test_out_of_order_children_before_parent_all_created(sync_engine: SyncEngine):
    """Applying remote events children-first must still create the whole tree."""
    fake = FakeDrive()
    parent_id = fake.add_folder("/parent")
    child1_id = fake.add_folder("/parent/child_1")
    child2_id = fake.add_file("/parent/child_2", b"content")
    sync_engine.client._get_service = Mock(return_value=fake.service())
    # from_metadata reads account_info for non-shared files; keep it offline.
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )

    # Path cache as iter_remote_changes leaves it once the feed has delivered
    # all three items (its fixpoint resolves children against their parent).
    sync_engine.client._path_cache.set(
        "/parent", parent_id, parent_id=ROOT_ID, name="parent", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/parent/child_1", child1_id, parent_id=parent_id, name="child_1", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/parent/child_2",
        child2_id,
        parent_id=parent_id,
        name="child_2",
        is_folder=False,
    )

    # Apply the children BEFORE their parent — the reversed order the linked
    # test produces and the order Drive's feed is allowed to deliver.
    for remote_path in ["/parent/child_2", "/parent/child_1", "/parent"]:
        md = sync_engine.client.get_metadata(remote_path)
        assert md is not None
        event = SyncEvent.from_metadata(md, sync_engine)
        sync_engine._create_local_entry(event)
        assert event.status in (
            SyncStatus.Done,
            SyncStatus.Skipped,
        ), f"applying {remote_path} ended as {event.status}"

    # All three items exist locally and are indexed.
    for remote_path in ["/parent", "/parent/child_1", "/parent/child_2"]:
        local_path = sync_engine.to_local_path_from_cased(remote_path)
        assert osp.exists(local_path), f"{remote_path} not created locally"
        assert (
            sync_engine.get_index_entry(remote_path.lower()) is not None
        ), f"{remote_path} missing from index"

    # The downloaded file carries the remote content.
    with open(sync_engine.to_local_path_from_cased("/parent/child_2"), "rb") as f:
        assert f.read() == b"content"

    assert sync_engine.sync_errors == []


# ==== Cluster E (F3): remote moves must move the local item by remote_id ============
#
# Drive's change feed emits a SINGLE entry at the NEW path for a remote rename
# or reparent — no DeletedMetadata for the old path and no per-child entries
# for folders. The engine only deleted local items on DeletedMetadata, so a
# remote move used to leave the old local file (or whole folder tree) and its
# IndexEntry behind forever. _apply_remote_move must detect the move via
# remote_id, move the local item, and re-key the index subtree.


def _seed_entry(
    sync: SyncEngine,
    remote_path: str,
    remote_id: str,
    item_type: ItemType,
    rev: str,
    content_hash: str | None,
) -> None:
    entry = IndexEntry(
        remote_path_cased=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=remote_id,
        item_type=item_type,
        last_sync=0.0,
        rev=rev,
        content_hash=content_hash,
        symlink_target=None,
    )
    with sync._database_access():
        sync._index_table.update(entry)


def test_remote_file_rename_moves_local_file(sync_engine: SyncEngine):
    """A remote rename (same id, new path) must move the local file and
    re-key the index — not leave the old copy on disk forever (review F3)."""
    data = b"file content unchanged by the rename"
    fake = FakeDrive()
    file_id = fake.add_file("/b.txt", data)
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._path_cache.set(
        "/b.txt", file_id, parent_id=ROOT_ID, name="b.txt", is_folder=False
    )
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )

    # Fully synced state before the remote rename: local /a.txt + index entry.
    local_old = _write_local_file(sync_engine, "/a.txt", data)
    _seed_entry(
        sync_engine, "/a.txt", file_id, ItemType.File, rev="rev-1", content_hash=_md5(data)
    )

    # The change feed delivers ONE file event, at the new path, same id and rev.
    event = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path="/b.txt",
        remote_path_lower="/b.txt",
        remote_id=file_id,
        local_path=sync_engine.to_local_path_from_cased("/b.txt"),
        rev="rev-1",
        content_hash=_md5(data),
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=len(data),
        completed=0,
    )

    sync_engine._create_local_entry(event)

    # The old local copy is gone, the new path holds the content.
    assert not osp.exists(local_old)
    local_new = sync_engine.to_local_path_from_cased("/b.txt")
    assert osp.isfile(local_new)
    with open(local_new, "rb") as f:
        assert f.read() == data

    # The index was re-keyed: old path gone, new path holds the remote id.
    assert sync_engine.get_index_entry("/a.txt") is None
    new_entry = sync_engine.get_index_entry("/b.txt")
    assert new_entry is not None
    assert new_entry.remote_id == file_id

    assert event.status in (SyncStatus.Done, SyncStatus.Skipped)
    assert sync_engine.sync_errors == []


def test_remote_folder_rename_moves_local_tree(sync_engine: SyncEngine):
    """A remote folder rename must take the whole local subtree and its
    index rows with it (Drive emits no per-child change entries)."""
    # Fully synced state before the remote rename: /old/kid.txt on disk + index.
    _write_local_file(sync_engine, "/old/kid.txt", b"kid")
    _seed_entry(
        sync_engine, "/old", "id-dir", ItemType.Folder, rev="folder", content_hash="folder"
    )
    _seed_entry(
        sync_engine,
        "/old/kid.txt",
        "id-kid",
        ItemType.File,
        rev="rev-kid",
        content_hash=_md5(b"kid"),
    )

    # The change feed delivers ONE folder event, at the new path, same id.
    event = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.Folder,
        sync_time=datetime.today().timestamp(),
        remote_path="/new",
        remote_path_lower="/new",
        remote_id="id-dir",
        local_path=sync_engine.to_local_path_from_cased("/new"),
        rev="folder",
        content_hash="folder",
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )

    sync_engine._create_local_entry(event)

    # The whole subtree moved on disk; nothing remains at the old path.
    assert osp.isdir(sync_engine.to_local_path_from_cased("/new"))
    assert osp.isfile(sync_engine.to_local_path_from_cased("/new/kid.txt"))
    assert not osp.exists(sync_engine.to_local_path_from_cased("/old"))

    # The index subtree was re-keyed with it.
    assert sync_engine.get_index_entry("/old") is None
    assert sync_engine.get_index_entry("/old/kid.txt") is None
    dir_entry = sync_engine.get_index_entry("/new")
    assert dir_entry is not None
    assert dir_entry.remote_id == "id-dir"
    kid_entry = sync_engine.get_index_entry("/new/kid.txt")
    assert kid_entry is not None
    assert kid_entry.remote_id == "id-kid"

    assert event.status in (SyncStatus.Done, SyncStatus.Skipped)
    assert sync_engine.sync_errors == []


def test_remote_reparent_into_unsynced_parent_moves_local_file(
    sync_engine: SyncEngine,
):
    """A remote reparent whose new parent folder is not yet synced locally must
    sync the parent on demand and still move the local file (no stale copy)."""
    data = b"reparented content"
    fake = FakeDrive()
    dir_id = fake.add_folder("/dir")
    file_id = fake.add_file("/dir/b.txt", data)
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._path_cache.set(
        "/dir", dir_id, parent_id=ROOT_ID, name="dir", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/dir/b.txt", file_id, parent_id=dir_id, name="b.txt", is_folder=False
    )
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )

    # Fully synced state before the remote reparent: local /a.txt only.
    local_old = _write_local_file(sync_engine, "/a.txt", data)
    _seed_entry(
        sync_engine, "/a.txt", file_id, ItemType.File, rev="rev-1", content_hash=_md5(data)
    )

    event = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path="/dir/b.txt",
        remote_path_lower="/dir/b.txt",
        remote_id=file_id,
        local_path=sync_engine.to_local_path_from_cased("/dir/b.txt"),
        rev="rev-1",
        content_hash=_md5(data),
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=len(data),
        completed=0,
    )

    sync_engine._create_local_entry(event)

    # The old copy moved into the on-demand-created parent folder.
    assert not osp.exists(local_old)
    assert osp.isdir(sync_engine.to_local_path_from_cased("/dir"))
    local_new = sync_engine.to_local_path_from_cased("/dir/b.txt")
    assert osp.isfile(local_new)
    with open(local_new, "rb") as f:
        assert f.read() == data

    # Index: parent synced, old key gone, new key holds the remote id.
    assert sync_engine.get_index_entry("/dir") is not None
    assert sync_engine.get_index_entry("/a.txt") is None
    new_entry = sync_engine.get_index_entry("/dir/b.txt")
    assert new_entry is not None
    assert new_entry.remote_id == file_id

    assert event.status in (SyncStatus.Done, SyncStatus.Skipped)
    assert sync_engine.sync_errors == []


def test_remote_move_with_occupied_target_keeps_index_and_conflicts(
    sync_engine: SyncEngine,
):
    """If a foreign local item already occupies the target of a remote move,
    the engine must NOT re-key the index onto the occupant: that would adopt
    the occupant into the moved file's sync identity (equal revs => Skipped),
    silently overwrite the remote file with the occupant's content on the next
    local scan, and resurrect the old path as a new upload. Instead, leave
    disk and index untouched so the downstream conflict logic conflicted-copies
    the occupant and downloads the moved file at the new path."""
    data = b"moved file content"
    occupant_data = b"foreign occupant created offline"
    fake = FakeDrive()
    file_id = fake.add_file("/b.txt", data)
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._path_cache.set(
        "/b.txt", file_id, parent_id=ROOT_ID, name="b.txt", is_folder=False
    )
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )

    # Fully synced state before the remote rename: local /a.txt + index entry.
    local_old = _write_local_file(sync_engine, "/a.txt", data)
    _seed_entry(
        sync_engine, "/a.txt", file_id, ItemType.File, rev="rev-1", content_hash=_md5(data)
    )

    # A DIFFERENT local item (no index row) occupies the move target.
    local_new = _write_local_file(sync_engine, "/b.txt", occupant_data)

    # The change feed delivers ONE file event, at the new path, same id and rev.
    event = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path="/b.txt",
        remote_path_lower="/b.txt",
        remote_id=file_id,
        local_path=local_new,
        rev="rev-1",
        content_hash=_md5(data),
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=len(data),
        completed=0,
    )

    sync_engine._create_local_entry(event)

    # The old path is untouched: disk copy still there, index row NOT re-keyed.
    assert osp.isfile(local_old)
    old_entry = sync_engine.get_index_entry("/a.txt")
    assert old_entry is not None
    assert old_entry.remote_id == file_id

    # The occupant was moved to a conflicted copy in the same folder.
    folder = osp.dirname(local_new)
    cc_paths = [
        osp.join(folder, name)
        for name in os.listdir(folder)
        if "conflicted copy" in name
    ]
    assert len(cc_paths) == 1, f"expected one conflicted copy in {os.listdir(folder)}"
    with open(cc_paths[0], "rb") as f:
        assert f.read() == occupant_data

    # The moved file was downloaded at the new path.
    assert osp.isfile(local_new)
    with open(local_new, "rb") as f:
        assert f.read() == data

    assert event.status is SyncStatus.Conflict


# ==== Cluster E (F9): dead Dropbox-era except clauses in local-event handlers ==========
#
# client.remove raises DataChangedError on a rev mismatch (writes.py).
# _on_local_deleted used to catch PathError (unreachable), so a
# delete-vs-remote-change conflict escaped as a recorded Up error and
# replayed on every restart. The fix skips the remote delete and
# re-downloads the newer remote file instead.


def test_local_delete_of_remotely_changed_file_redownloads(sync_engine: SyncEngine):
    """Deleting locally while the remote changed must skip the remote delete,
    drop the index entry, and restore the newer remote file — not loop a
    failing delete on every restart (review F9)."""
    remote_path = "/f.txt"
    new_content = b"new remote content"

    # Set up FakeDrive with the file at a new rev.
    fake = FakeDrive()
    file_id = fake.add_file(remote_path, content=new_content)
    new_rev = fake.files[file_id]["headRevisionId"]
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._path_cache.set(
        remote_path, file_id, parent_id=ROOT_ID, name="f.txt", is_folder=False
    )
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )

    # Index reflects the STALE rev — simulating a locally-deleted file whose
    # remote counterpart has already been updated since last sync.
    stale_rev = "REV-STALE"
    assert stale_rev != new_rev
    _seed_entry(sync_engine, remote_path, file_id, ItemType.File, rev=stale_rev, content_hash=_md5(b"old content"))

    # The local file is absent (user deleted it locally).
    local_path = sync_engine.to_local_path_from_cased(remote_path)
    assert not osp.exists(local_path)

    # Build a local-deleted SyncEvent.
    event = SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=file_id,
        local_path=local_path,
        rev=stale_rev,
        content_hash=None,
        symlink_target=None,
        change_type=ChangeType.Removed,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )

    sync_engine._create_remote_entry(event)

    # Remote file must NOT have been trashed — rev mismatch should skip delete.
    assert not fake.files[file_id]["trashed"], "remote file was trashed despite rev mismatch"

    # No lingering Up error for the path (was the bug: DataChangedError became a SyncError).
    up_errors = sync_engine.sync_errors_for_path(remote_path.lower(), direction=SyncDirection.Up)
    assert len(up_errors) == 0, f"unexpected Up errors: {up_errors}"

    # The newer remote file must have been restored locally.
    assert osp.exists(local_path), "remote file was not re-downloaded after delete conflict"

    # Index must be re-keyed to the new rev (not the stale one, not absent).
    entry = sync_engine.get_index_entry(remote_path)
    assert entry is not None, "index entry missing after redownload"
    assert entry.rev == new_rev, f"index rev {entry.rev!r} != new_rev {new_rev!r}"


# ==== Cluster E (F10): _on_local_moved DataChangedError branch ========================
#
# client.remove raises DataChangedError when the destination already exists with a
# newer rev than what our index recorded.  Before 8b91c3f the except clause caught
# FileConflictError (unreachable), so DataChangedError would propagate and be
# recorded as an Up error — effectively trashing a file we no longer own.  The fix
# catches DataChangedError and falls through so move(..., autorename=True) conflict-
# renames around the still-live destination.


def test_local_move_to_remotely_changed_destination_conflict_renames(
    sync_engine: SyncEngine,
):
    """Moving a.txt → b.txt locally when the remote b.txt changed since last
    sync must NOT trash b.txt; the source must arrive on remote under an
    autorename suffix and the stale b.txt index entry must be removed
    (review F10 — DataChangedError branch in _on_local_moved)."""
    src_data = b"source file content"
    dst_new_content = b"destination changed remotely"

    # Set up FakeDrive: b.txt exists with a new rev; a.txt is the move source.
    fake = FakeDrive()
    b_id = fake.add_file("/b.txt", content=dst_new_content)
    b_new_rev = fake.files[b_id]["headRevisionId"]
    a_id = fake.add_file("/a.txt", content=src_data)
    a_rev = fake.files[a_id]["headRevisionId"]
    sync_engine.client._get_service = Mock(return_value=fake.service())

    # Path cache reflects both files.
    sync_engine.client._path_cache.set(
        "/a.txt", a_id, parent_id=ROOT_ID, name="a.txt", is_folder=False
    )
    sync_engine.client._path_cache.set(
        "/b.txt", b_id, parent_id=ROOT_ID, name="b.txt", is_folder=False
    )
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )

    # Index: /a.txt at its current rev; /b.txt at a STALE rev so remove() raises
    # DataChangedError (rev precondition mismatch).
    _seed_entry(
        sync_engine, "/a.txt", a_id, ItemType.File, rev=a_rev, content_hash=_md5(src_data)
    )
    _seed_entry(
        sync_engine,
        "/b.txt",
        b_id,
        ItemType.File,
        rev="rev-stale",
        content_hash=_md5(b"old b content"),
    )

    # Local disk: the user moved a.txt → b.txt; a.txt is gone, b.txt holds the
    # moved content.
    local_path_from = sync_engine.to_local_path_from_cased("/a.txt")
    local_path_to = _write_local_file(sync_engine, "/b.txt", src_data)

    event = SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path="/b.txt",
        remote_path_lower="/b.txt",
        remote_path_from="/a.txt",
        remote_path_from_lower="/a.txt",
        remote_id=a_id,
        local_path=local_path_to,
        local_path_from=local_path_from,
        rev=a_rev,
        content_hash=_md5(src_data),
        symlink_target=None,
        change_type=ChangeType.Moved,
        status=SyncStatus.Queued,
        size=len(src_data),
        completed=0,
    )

    sync_engine._create_remote_entry(event)

    # Remote /b.txt must NOT have been trashed.
    assert not fake.files[b_id]["trashed"], "remote b.txt was trashed despite rev mismatch"

    # The move source must have arrived remotely under an autorename suffix
    # (autorename=True gives "b (1).txt" since "b.txt" is already occupied).
    remote_names = [r["name"] for r in fake.files.values() if not r["trashed"]]
    assert any(
        "b" in name and name != "b.txt" for name in remote_names
    ), f"no autorename sibling found; remote names: {remote_names}"

    # The stale /b.txt index entry must have been removed (replaced by the
    # conflict-renamed entry from _update_index_recursive).
    b_entry = sync_engine.get_index_entry("/b.txt")
    assert b_entry is None, (
        "stale /b.txt index entry was not removed after conflict rename; "
        f"entry: {b_entry}"
    )

    # No lingering Up error for /b.txt.
    up_errors = sync_engine.sync_errors_for_path("/b.txt", direction=SyncDirection.Up)
    assert len(up_errors) == 0, f"unexpected Up errors for /b.txt: {up_errors}"


# ==== Task 10 (F8): mignore / excluded_items match decoded (human-readable) paths =====
#
# Before the fix: patterns were matched against percent-encoded virtual paths, so
# any name containing non-ASCII or common punctuation (e.g. 'Ördner', '(1)', '#')
# silently failed to be ignored/excluded.
# After the fix: match against decode_path(remote_path) in human-readable form.


def _make_dir_upload_event(sync: SyncEngine, human_path: str) -> SyncEvent:
    """Build a minimal upload SyncEvent for a directory at ``human_path``."""
    encoded = encode_path(human_path)
    folded = fold_path(encoded)
    return SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.Folder,
        sync_time=datetime.today().timestamp(),
        remote_path=encoded,
        remote_path_lower=folded,
        remote_id=None,
        local_path=sync.to_local_path_from_cased(encoded),
        local_path_from=None,
        rev=None,
        content_hash=None,
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )


def test_mignore_matches_non_ascii_dir_name(sync_engine: SyncEngine) -> None:
    """_is_mignore_path must match user-written .mignore patterns against
    the decoded (human-readable) path, not the percent-encoded virtual path."""
    # Pattern written by user in plain text: the folder name as they see it.
    sync_engine._mignore_rules = PathSpec.from_lines(
        "gitwildmatch", ["Ördner", "*.tmp"]
    )
    event = _make_dir_upload_event(sync_engine, "/Ördner")
    # is_mignore also checks get_local_rev; because no index entry exists, the
    # condition is met (rev is None → not in index → True for second condition).
    assert sync_engine._is_mignore_path(event.remote_path, is_dir=event.is_directory)


def test_mignore_matching_is_case_sensitive(sync_engine: SyncEngine) -> None:
    """mignore matching must be case-sensitive (upstream-maestral parity, deliberate).

    A pattern 'Ördner/' must NOT match a folder whose name differs only in case
    ('ördner'), and a pattern 'photos/' must NOT match 'Photos'.
    """
    # Pattern with uppercase Ö — should not match lowercase ö variant.
    sync_engine._mignore_rules = PathSpec.from_lines(
        "gitwildmatch", ["Ördner/", "photos/"]
    )
    lower_event = _make_dir_upload_event(sync_engine, "/ördner")
    assert not sync_engine._is_mignore_path(
        lower_event.remote_path, is_dir=lower_event.is_directory
    ), "case-insensitive mismatch: 'Ördner/' pattern must not match '/ördner'"

    upper_event = _make_dir_upload_event(sync_engine, "/Photos")
    assert not sync_engine._is_mignore_path(
        upper_event.remote_path, is_dir=upper_event.is_directory
    ), "case-insensitive mismatch: 'photos/' pattern must not match '/Photos'"


def test_mignore_matches_punctuation_in_name(sync_engine: SyncEngine) -> None:
    """Parentheses and other common punctuation must survive the mignore match."""
    sync_engine._mignore_rules = PathSpec.from_lines(
        "gitwildmatch", ["Report (1).txt"]
    )
    encoded = encode_path("/Report (1).txt")
    folded = fold_path(encoded)
    event = SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=encoded,
        remote_path_lower=folded,
        remote_id=None,
        local_path=sync_engine.to_local_path_from_cased(encoded),
        local_path_from=None,
        rev=None,
        content_hash=None,
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )
    assert sync_engine._is_mignore_path(event.remote_path, is_dir=event.is_directory)


def test_excluded_items_match_non_ascii_name(sync_engine: SyncEngine) -> None:
    """is_excluded_by_user must handle non-ASCII names in excluded_items
    and must match against the fold_path'd remote_path_lower."""
    # Set via the public setter — triggers clean_excluded_items_list.
    sync_engine.excluded_items = ["/Ördner"]

    # A file inside the excluded directory must be caught.
    assert sync_engine.is_excluded_by_user(
        fold_path(encode_path("/Ördner/inner.txt"))
    )
    # The directory itself must be caught.
    assert sync_engine.is_excluded_by_user(fold_path(encode_path("/Ördner")))
    # A sibling must NOT be caught.
    assert not sync_engine.is_excluded_by_user(fold_path(encode_path("/Other")))


def test_excluded_items_match_punctuation_in_name(sync_engine: SyncEngine) -> None:
    """Parentheses, ampersands and similar chars must not break exclusion matching."""
    sync_engine.excluded_items = ["/Reports (2024)"]
    assert sync_engine.is_excluded_by_user(
        fold_path(encode_path("/Reports (2024)/Q1.pdf"))
    )
    assert not sync_engine.is_excluded_by_user(fold_path(encode_path("/Reports")))


def test_excluded_items_stale_config_form_matches(sync_engine: SyncEngine) -> None:
    """Stale config entries stored in the old normalize()-form (NFC lowercase
    plain text) must still match after the fold_human migration."""
    # Simulate a stale config entry: old code stored normalize('/Ördner') = '/ördner'
    # (plain decoded lowercase, not fold_human form — which is identical for this
    # example, so we test a case where they'd differ: a path with a percent sign).
    # The key insight: fold_human('/a%b') == '/a%b' (% is not special in human paths).
    # A stale entry '/a%b' stored literally must still match.
    sync_engine._excluded_items = {"/ördner"}  # bypass setter, inject stale form
    assert sync_engine.is_excluded_by_user(fold_path(encode_path("/Ördner")))


def test_excluded_items_cleaned_at_config_load(sync_engine: SyncEngine) -> None:
    """Configs written before the fold_human migration can hold normalize()-form
    entries. For casefold-divergent names these never match again: the stale
    entry for 'Straße' is '/straße' (NFC + lower) while the engine compares
    against '/strasse' (fold_human: casefold maps ß → ss). The list must be
    canonicalised when it is loaded from the config."""
    sync_engine._conf.set("sync", "excluded_items", ["/straße"])
    sync_engine.reload_cached_config()

    assert sync_engine._excluded_items == {"/strasse"}
    assert sync_engine.is_excluded_by_user(fold_path(encode_path("/Straße")))


def test_apply_remote_changes_prunes_deleted_excluded_item(
    sync_engine: SyncEngine,
) -> None:
    """A remotely deleted item that is excluded by the user must be pruned from
    excluded_items. Entries are stored in fold_human form while
    event.remote_path_lower is in fold_path (encoded) form — the comparison
    must decode the event side, mirroring is_excluded_by_user."""
    sync_engine.excluded_items = ["/Ördner"]

    encoded = encode_path("/Ördner")
    event = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.Folder,
        sync_time=datetime.today().timestamp(),
        remote_path=encoded,
        remote_path_lower=fold_path(encoded),
        remote_id="id:deleted",
        local_path=sync_engine.to_local_path_from_cased(encoded),
        rev=None,
        content_hash=None,
        symlink_target=None,
        change_type=ChangeType.Removed,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )

    sync_engine.apply_remote_changes([event])

    assert sync_engine.excluded_items == set()
