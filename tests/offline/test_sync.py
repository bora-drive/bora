import hashlib
import os
import os.path as osp
import sqlite3
from datetime import datetime
from queue import Queue
from unittest.mock import Mock

import pytest

from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.core import FileMetadata
from cumulus.database.core import Database
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


# ==== Upload-side conflict signal (cluster A regression) =============================
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
