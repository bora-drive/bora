import hashlib
import os
import os.path as osp
import sqlite3
from datetime import datetime, timezone
from queue import Queue
from unittest.mock import Mock

import pytest
from pathspec import PathSpec
from watchdog.events import FileMovedEvent

from bora.client import GoogleDriveClient
from bora.config import remove_configuration
from bora.core import (
    DeletedMetadata,
    FileMetadata,
    FolderMetadata,
    FullAccount,
    ListFolderResult,
)
from bora.database.core import Database
from bora.exceptions import DataChangedError
from bora.export import ExportOptions
from bora.keyring import CredentialStorage
from bora.models import (
    ChangeType,
    IndexEntry,
    ItemType,
    SyncDirection,
    SyncEvent,
    SyncStatus,
)
from bora.path_cache import SHARED_WITH_ME_ID, PathIDCache
from bora.path_codec import encode_path, fold_path
from bora.sync import ActivityNode, ActivityTree, Conflict, SyncEngine
from bora.utils.appdirs import get_home_dir
from bora.utils.path import delete
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
    read_only: int = 0,
    remote_id: str = "id:seed",
    symlink_target: str | None = None,
) -> None:
    """Insert an IndexEntry for ``remote_path`` directly via the engine's index API."""
    remote_path_lower = remote_path.lower()
    entry = IndexEntry(
        remote_path_cased=remote_path,
        remote_path_lower=remote_path_lower,
        remote_id=remote_id,
        item_type=ItemType.File,
        last_sync=last_sync,
        rev=rev,
        content_hash=content_hash,
        symlink_target=symlink_target,
        read_only=read_only,
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


def _symlink_event(
    sync: SyncEngine,
    remote_path: str,
    target: str,
    rev: str | None = "r1",
) -> SyncEvent:
    """Build a download SyncEvent for a Drive shortcut, carrying a
    ``symlink_target`` (mirrors ``_download_event``, but for shortcuts a
    shortcut has no content_hash of its own -- the fetch is a local symlink
    write, not a download)."""
    return SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id="id:remote-shortcut",
        local_path=sync.to_local_path_from_cased(remote_path),
        rev=rev,
        content_hash=None,
        symlink_target=target,
        change_type=ChangeType.Added,
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


def test_conflict_first_download_with_no_remote_hash_is_not_identical(
    sync_engine: SyncEngine,
):
    """A brand-new exportable native doc (Google Doc/Sheet/Slide) has no
    server-side content_hash -- Drive provides no MD5 for exports -- and, on
    its very first download, no local file exists yet either. Both being
    None must NOT be read as "content hashes match, so items are identical":
    that misclassification would skip the download outright and the file
    would never be created.

    No index entry is seeded, so ``local_rev`` is None and the top ``event.rev
    == local_rev`` branch does not short-circuit (event.rev is a real Drive
    version string, not None) -- this exercises the hash-comparison branch
    directly. Regression test for a bug found while implementing Task 7
    (read-only export mirrors)."""
    remote_path = "/new_doc.docx"
    event = _download_event(
        sync_engine,
        remote_path,
        rev="VERSION-1",
        content_hash=None,
        change_type=ChangeType.Added,
    )

    assert sync_engine._check_download_conflict(event) is not Conflict.Identical


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
    from bora.exceptions import NotFoundError

    remote_path = "/brand_new.txt"
    data = b"never uploaded before"
    local_path = _write_local_file(sync_engine, remote_path, data)

    def _raise_not_found(path):
        raise NotFoundError(
            "Not found",
            f"No item at '{path}' is known to Bora.",
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
        read_only=0,
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
            read_only=0,
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
        read_only=0,
    )
    with sync._database_access():
        sync._index_table.update(entry)


def test_update_index_from_remote_metadata_evicts_stale_same_remote_id(
    sync_engine: SyncEngine,
):
    """update_index_from_remote_metadata must uphold the one-remote_id ->
    one-path invariant: writing a row for a remote_id that is already indexed
    at a DIFFERENT path evicts the stale row instead of inserting a duplicate
    that violates the UNIQUE(remote_id) constraint (DatabaseError crash).

    Regression: the download path records the read_only bit via this method
    before update_index_from_sync_event runs, so an occupied-target / stale-
    cache remote move (stale old-path row still present) crashed here."""
    data = b"content"
    file_id = "file-1"
    # Stale row: same remote_id indexed at the OLD path.
    _seed_entry(
        sync_engine, "/a.txt", file_id, ItemType.File, rev="rev-1",
        content_hash=_md5(data),
    )

    md = FileMetadata(
        name="b.txt", path_lower="/b.txt", path_display="/b.txt", id=file_id,
        modified=datetime.now(timezone.utc), rev="rev-1", size=len(data),
        symlink_target=None, shared=False, modified_by=None,
        is_downloadable=True, content_hash=_md5(data), can_edit=True,
    )
    # Must not raise DatabaseError (UNIQUE(remote_id)).
    sync_engine.update_index_from_remote_metadata(md)

    assert sync_engine.get_index_entry("/a.txt") is None
    new_entry = sync_engine.get_index_entry("/b.txt")
    assert new_entry is not None and new_entry.remote_id == file_id
    with sync_engine._database_access():
        rows = sync_engine._index_table.select_sql("WHERE remote_id = ?", file_id)
    assert len(rows) == 1


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


def test_remote_move_with_occupied_target_evicts_stale_index_and_conflicts(
    sync_engine: SyncEngine,
):
    """If a foreign local item already occupies the target of a remote move,
    the engine must NOT re-key the index onto the occupant: that would adopt
    the occupant into the moved file's sync identity (equal revs => Skipped)
    and silently overwrite the remote file with the occupant's content on the
    next local scan. Instead the downstream conflict logic conflict-copies the
    occupant and downloads the moved file at the new path.

    Post-7c-2: the stale index row at the OLD path is evicted when the new-path
    row is inserted, so the moved file's id is indexed at exactly one path — no
    duplicate remote_id row (finding C). The on-disk copy at the old path is
    left as an orphan that the next local scan re-uploads as a new file
    (accepted residual; see
    docs/superpowers/plans/2026-06-15-stage-7c-p0-deviations.md §4). The key
    safety property — the index is never re-keyed onto the occupant, so the
    remote is never overwritten with the occupant's content — is asserted via
    the new-path content + Conflict status below."""
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

    # 7c-2: the stale /a.txt index row is evicted when the new /b.txt row is
    # inserted, so the moved file's id resolves to exactly ONE path (/b.txt) —
    # no duplicate remote_id row. The on-disk /a.txt copy remains as an orphan
    # (re-uploaded on the next local scan; accepted residual). Crucially the
    # index was NOT re-keyed onto the occupant: /b.txt carries the moved file's
    # content (asserted below), not the occupant's.
    assert osp.isfile(local_old)  # on-disk orphan; self-heals on next scan
    assert sync_engine.get_index_entry("/a.txt") is None
    new_entry = sync_engine.get_index_entry("/b.txt")
    assert new_entry is not None
    assert new_entry.remote_id == file_id
    assert new_entry.content_hash == _md5(data)
    with sync_engine._database_access():
        rows = sync_engine._index_table.select_sql("WHERE remote_id = ?", file_id)
    assert len(rows) == 1, (
        "exactly one row must hold the moved id, got "
        f"{[r.remote_path_lower for r in rows]}"
    )

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


def test_remote_folder_rename_with_same_batch_stale_child_echo(sync_engine: SyncEngine):
    """A poll batch can carry BOTH a folder rename (/old -> /renamed) and a
    stale echo of a child's own earlier change, resolved at the child's
    PRE-rename path (/old/kid.txt). Apply order is folders-then-files, so the
    folder rename re-keys the whole subtree first (child now at
    /renamed/kid.txt), then the stale child event fires on the same child id.
    The engine must NOT undo the subtree re-key: the child must end up indexed
    only at /renamed/kid.txt, never resurrected at the stale /old/kid.txt
    (cluster-E final-review finding 5, reproduced live)."""
    data = b"kid content unchanged by the rename"
    fake = FakeDrive()
    # Drive's current truth after the rename: /renamed and /renamed/kid.txt.
    dir_id = fake.add_folder("/renamed")
    kid_id = fake.add_file("/renamed/kid.txt", data)
    kid_rev = fake.files[kid_id]["headRevisionId"]
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._path_cache.set(
        "/renamed", dir_id, parent_id=ROOT_ID, name="renamed", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/renamed/kid.txt", kid_id, parent_id=dir_id, name="kid.txt", is_folder=False
    )
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )

    # Fully synced PRE-rename state: /old/kid.txt on disk + index at /old.
    _write_local_file(sync_engine, "/old/kid.txt", data)
    _seed_entry(
        sync_engine, "/old", dir_id, ItemType.Folder, rev="folder", content_hash="folder"
    )
    _seed_entry(
        sync_engine, "/old/kid.txt", kid_id, ItemType.File, rev=kid_rev,
        content_hash=_md5(data),
    )

    # Apply order within the batch: folders before files.
    folder_event = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.Folder,
        sync_time=datetime.today().timestamp(),
        remote_path="/renamed",
        remote_path_lower="/renamed",
        remote_id=dir_id,
        local_path=sync_engine.to_local_path_from_cased("/renamed"),
        rev="folder",
        content_hash="folder",
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )
    # Stale echo of the child, resolved at its PRE-rename path.
    stale_child_event = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path="/old/kid.txt",
        remote_path_lower="/old/kid.txt",
        remote_id=kid_id,
        local_path=sync_engine.to_local_path_from_cased("/old/kid.txt"),
        rev=kid_rev,
        content_hash=_md5(data),
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=len(data),
        completed=0,
    )

    sync_engine._create_local_entry(folder_event)
    sync_engine._create_local_entry(stale_child_event)

    # Disk: the subtree lives at /renamed; nothing resurrected at /old.
    assert osp.isfile(sync_engine.to_local_path_from_cased("/renamed/kid.txt"))
    assert not osp.exists(sync_engine.to_local_path_from_cased("/old"))
    assert not osp.exists(sync_engine.to_local_path_from_cased("/old/kid.txt"))

    # Index: child indexed only at /renamed/kid.txt, never at the stale path.
    assert sync_engine.get_index_entry("/old/kid.txt") is None
    assert sync_engine.get_index_entry("/old") is None
    kid_entry = sync_engine.get_index_entry("/renamed/kid.txt")
    assert kid_entry is not None
    assert kid_entry.remote_id == kid_id

    # Exactly one index row holds the child id (no duplicate at the stale path).
    by_id = sync_engine.get_index_entry_by_remote_id(kid_id)
    assert by_id is not None
    assert by_id.remote_path_lower == "/renamed/kid.txt"

    assert sync_engine.sync_errors == []


def test_apply_remote_move_does_not_skip_when_dest_parent_is_indexed(
    sync_engine: SyncEngine,
):
    """The finding-5 stale-skip must NOT over-fire: a move whose destination
    parent is a live remote location (present in the index) is a genuine move,
    even if that parent dir is merely missing on disk. Only a destination whose
    parent has left the namespace counts as a stale echo. _apply_remote_move
    must return False (fall through) here, not True (skip)."""
    data = b"reparented content"
    fake = FakeDrive()
    dir_id = fake.add_folder("/dir")
    file_id = fake.add_file("/dir/b.txt", data)
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )

    # Local /a.txt on disk; parent /dir is INDEXED but never created on disk,
    # so the on-disk move to /dir/b.txt silently no-ops (missing dest parent).
    _write_local_file(sync_engine, "/a.txt", data)
    _seed_entry(
        sync_engine, "/dir", dir_id, ItemType.Folder, rev="folder", content_hash="folder"
    )
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

    # Indexed dest parent => genuine move, not a stale echo: do not skip.
    assert sync_engine._apply_remote_move(event) is False


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


def test_crash_replayed_delete_with_evicted_cache_removes_stranded_item(
    sync_engine: SyncEngine,
) -> None:
    """End-to-end crash-replay (cluster-E finding 2): the path cache mutates
    durably as ``iter_remote_changes`` classifies, but the engine cursor only
    advances once the whole batch is applied. A hard crash mid-batch therefore
    re-fetches a delete whose fileId the crashed run already evicted from the
    cache — yet the index row (and the local file) still survive. The engine's
    index-by-remote_id fallback must re-resolve that delete so the replayed
    batch removes the stranded local file and index row instead of silently
    no-op'ing and letting the cursor advance past an unapplied deletion.

    Drives the real client iterator -> engine resolver -> apply path.
    """
    data = b"about to be deleted on the remote"
    fake = FakeDrive()
    file_id = fake.add_file("/gone.txt", data)
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )

    # Snapshot the cursor while /gone.txt still exists, then delete it remotely.
    start_cursor = (
        fake.service().changes().getStartPageToken().execute()["startPageToken"]
    )

    # Fully-synced local state survives the crash: file on disk + index row with
    # a last_sync AFTER the file's ctime, so the replayed delete is classified
    # RemoteNewer (not suppressed as a local-newer conflict).
    local_path = _write_local_file(sync_engine, "/gone.txt", data)
    _seed_entry(
        sync_engine,
        "/gone.txt",
        file_id,
        ItemType.File,
        rev="rev-1",
        content_hash=_md5(data),
    )
    with sync_engine._database_access():
        entry = sync_engine.get_index_entry("/gone.txt")
        entry.last_sync = datetime.today().timestamp() + 3600
        sync_engine._index_table.update(entry)

    # The crashed run already evicted /gone.txt from the path cache (durable),
    # so the cache no longer knows file_id. The deletion was never applied and
    # the cursor never advanced, so the index row above still maps id -> path.
    assert sync_engine.client._path_cache.id_to_path(file_id) is None

    fake.service().files().delete(fileId=file_id).execute()

    # Replay the batch end-to-end: real client iterator -> resolver -> apply.
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start_cursor):
        sync_engine.apply_remote_changes(sync_events)

    assert not osp.exists(local_path), "stranded local file must be removed on replay"
    assert sync_engine.get_index_entry("/gone.txt") is None
    assert sync_engine.get_index_entry_by_remote_id(file_id) is None
    assert sync_engine.sync_errors == []


# ==== Cluster E final review: _clean_remote_changes must preserve move-out deletes ====
#
# The change feed is id-keyed: at most one entry per fileId per batch. The only way
# two entries reach the same path_lower in one batch is a move-out + reuse — a
# DeletedMetadata for the item that vacated path P, then a create for a DIFFERENT
# item placed onto P. The builder already orders these delete-before-create. The
# old _clean_remote_changes dropped the delete unless the item type changed, so a
# folder->folder reuse silently kept the departing folder's children and index rows
# and never indexed the new folder. The delete must now always survive.


def _modified_now() -> datetime:
    return datetime.today()


def _file_md(path: str, file_id: str, data: bytes) -> FileMetadata:
    return FileMetadata(
        name=osp.basename(path),
        path_lower=path.lower(),
        path_display=path,
        id=file_id,
        modified=_modified_now(),
        rev="rev-1",
        size=len(data),
        symlink_target=None,
        shared=False,
        modified_by=None,
        is_downloadable=True,
        content_hash=_md5(data),
    )


def _folder_md(path: str, folder_id: str) -> FolderMetadata:
    return FolderMetadata(
        name=osp.basename(path),
        path_lower=path.lower(),
        path_display=path,
        id=folder_id,
        shared=False,
    )


def _deleted_md(path: str) -> DeletedMetadata:
    return DeletedMetadata(
        name=osp.basename(path),
        path_lower=path.lower(),
        path_display=path,
    )


def test_clean_remote_changes_folder_reuse_preserves_delete(sync_engine: SyncEngine):
    """Folder F at /p (with child /p/kid.txt) is replaced in one batch by a
    DIFFERENT folder G at /p. The feed is [DeletedMetadata(/p), FolderMetadata(/p,
    id=G)]. _clean_remote_changes must keep the delete (so F's subtree is removed)
    BEFORE the surviving FolderMetadata — the old code dropped it (no type change)."""
    # Index + disk for the departing folder F and its child.
    _seed_entry(
        sync_engine, "/p", "id-F", ItemType.Folder, rev="folder", content_hash="folder"
    )
    _seed_entry(
        sync_engine,
        "/p/kid.txt",
        "id-kid",
        ItemType.File,
        rev="rev-kid",
        content_hash=_md5(b"kid"),
    )
    _write_local_file(sync_engine, "/p/kid.txt", b"kid")

    res = ListFolderResult(
        entries=[_deleted_md("/p"), _folder_md("/p", "id-G")],
        has_more=False,
        cursor="c",
    )

    cleaned = sync_engine._clean_remote_changes(res)

    # The delete for the departing folder must survive, ahead of the create.
    types = [type(e) for e in cleaned.entries]
    assert DeletedMetadata in types, (
        "move-out delete for the reused folder path was dropped"
    )
    del_idx = next(
        i for i, e in enumerate(cleaned.entries) if isinstance(e, DeletedMetadata)
    )
    fol_idx = next(
        i for i, e in enumerate(cleaned.entries) if isinstance(e, FolderMetadata)
    )
    assert del_idx < fol_idx, "delete must be emitted before the create"
    # The surviving create is the NEW folder G.
    assert cleaned.entries[fol_idx].id == "id-G"


def test_clean_remote_changes_file_reuse_keeps_delete_then_file(sync_engine: SyncEngine):
    """File reuse regression guard: [DeletedMetadata(/p), FileMetadata(/p, id=G)]
    must keep the delete then the new file (file->file converged before, but the
    delete is now preserved consistently — no regression)."""
    _seed_entry(
        sync_engine, "/p", "id-F", ItemType.File, rev="rev-f", content_hash=_md5(b"old")
    )

    res = ListFolderResult(
        entries=[_deleted_md("/p"), _file_md("/p", "id-G", b"new")],
        has_more=False,
        cursor="c",
    )

    cleaned = sync_engine._clean_remote_changes(res)

    types = [type(e) for e in cleaned.entries]
    assert DeletedMetadata in types
    del_idx = next(
        i for i, e in enumerate(cleaned.entries) if isinstance(e, DeletedMetadata)
    )
    file_idx = next(
        i for i, e in enumerate(cleaned.entries) if isinstance(e, FileMetadata)
    )
    assert del_idx < file_idx
    assert cleaned.entries[file_idx].id == "id-G"


def test_clean_remote_changes_single_event_unchanged(sync_engine: SyncEngine):
    """A path with a single event passes through untouched."""
    res = ListFolderResult(
        entries=[_file_md("/solo.txt", "id-1", b"data")],
        has_more=False,
        cursor="c",
    )

    cleaned = sync_engine._clean_remote_changes(res)

    assert len(cleaned.entries) == 1
    assert isinstance(cleaned.entries[0], FileMetadata)
    assert cleaned.entries[0].id == "id-1"


def test_clean_remote_changes_type_change_still_splits(sync_engine: SyncEngine):
    """The existing type-change case (folder vacated, file created at /p) still
    yields delete-then-create — now a subcase of the delete-preserving branch."""
    _seed_entry(
        sync_engine, "/p", "id-F", ItemType.Folder, rev="folder", content_hash="folder"
    )

    res = ListFolderResult(
        entries=[_deleted_md("/p"), _file_md("/p", "id-G", b"data")],
        has_more=False,
        cursor="c",
    )

    cleaned = sync_engine._clean_remote_changes(res)

    types = [type(e) for e in cleaned.entries]
    assert DeletedMetadata in types
    del_idx = next(
        i for i, e in enumerate(cleaned.entries) if isinstance(e, DeletedMetadata)
    )
    file_idx = next(
        i for i, e in enumerate(cleaned.entries) if isinstance(e, FileMetadata)
    )
    assert del_idx < file_idx


def test_clean_remote_changes_path_ends_deleted_keeps_single_delete(
    sync_engine: SyncEngine,
):
    """If the path ends deleted (last_event is a DeletedMetadata), keep a single
    delete — no spurious create."""
    _seed_entry(
        sync_engine, "/p", "id-F", ItemType.File, rev="rev-f", content_hash=_md5(b"old")
    )

    res = ListFolderResult(
        entries=[_file_md("/p", "id-F", b"old"), _deleted_md("/p")],
        has_more=False,
        cursor="c",
    )

    cleaned = sync_engine._clean_remote_changes(res)

    assert len(cleaned.entries) == 1
    assert isinstance(cleaned.entries[0], DeletedMetadata)


def test_ensure_shared_with_me_dir_creates_it(sync_engine: SyncEngine) -> None:
    shared = os.path.join(sync_engine.sync_path, "Shared with me")
    assert not os.path.exists(shared)
    sync_engine.ensure_shared_with_me_dir_present()
    assert os.path.isdir(shared)


def test_ensure_shared_with_me_dir_idempotent(sync_engine: SyncEngine) -> None:
    sync_engine.ensure_shared_with_me_dir_present()
    sync_engine.ensure_shared_with_me_dir_present()  # no raise on second call
    assert os.path.isdir(os.path.join(sync_engine.sync_path, "Shared with me"))


def test_get_remote_item_creates_shared_with_me_mount(
    sync_engine: SyncEngine,
) -> None:
    """Regression for the review gap on Task 5: ``get_remote_item`` is an
    alternate download entry point (invoked from manager.py's startup/resume/
    retry loops) that runs BEFORE ``download_sync_cycle``. Without its own
    call to ``ensure_shared_with_me_dir_present``, downloading a shared item
    whose local "/Shared with me" mount is absent used to blow up:
    ``_ensure_parent`` cannot materialize the virtual-root parent (it has no
    remote metadata), so the final ``move()`` into place raised
    ``FileNotFoundError`` because "Shared with me" itself doesn't exist on
    disk.

    This drives a real end-to-end single-file download (FakeDrive-backed
    ``client.download`` -> MD5-verified content on disk) through
    ``get_remote_item`` for a file directly under "/Shared with me", rather
    than mocking/spying on ``ensure_shared_with_me_dir_present`` -- so it
    proves the fix actually unblocks the on-disk write it gates, not just
    that the method got called.

    (A shared *folder* with a child would exercise ``_get_remote_folder`` /
    ``apply_remote_changes`` instead, but that path fans out through a
    thread pool (``do_parallel``) and the fixture's in-memory sqlite
    connections aren't safe to touch from worker threads -- an unrelated,
    pre-existing fixture limitation. The file path below still calls
    ``_create_local_entry`` synchronously and exercises the exact
    ``_ensure_parent`` / mount-materialization gap the fix closes.)
    """
    shared_dir = os.path.join(sync_engine.sync_path, "Shared with me")
    # Defensive: the fixture never creates this dir, but assert + remove any
    # pre-existing one so the test is a true regression check regardless of
    # fixture changes.
    if os.path.exists(shared_dir):
        delete(shared_dir)
    assert not os.path.exists(shared_dir)

    fake = FakeDrive()
    # A file shared directly (not inside a shared folder) -- a real "Shared
    # with me" shape, and its remote parent is the virtual root itself, so
    # ``_ensure_parent`` walks straight into the mount-materialization gap.
    content = b"shared file content"
    file_id = fake._insert("report.txt", "OTHER_ROOT", "text/plain", content)
    fake.files[file_id]["sharedWithMe"] = True
    fake.files[file_id]["ownedByMe"] = False
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )
    sync_engine.client._path_cache.set(
        "/Shared with me/report.txt",
        file_id,
        parent_id=SHARED_WITH_ME_ID,
        name="report.txt",
        is_folder=False,
    )

    success = sync_engine.get_remote_item("/Shared with me/report.txt")

    assert success, "download of shared file must succeed"
    assert os.path.isdir(shared_dir), "mount point must be created"
    local_file = os.path.join(shared_dir, "report.txt")
    assert os.path.isfile(local_file)
    with open(local_file, "rb") as f:
        assert f.read() == content


# ==== Task 6: download_only index column threaded from export_mime metadata =


def test_index_records_download_only_from_metadata(
    sync_engine: SyncEngine,
) -> None:
    """update_index_from_remote_metadata must derive IndexEntry.download_only
    from the metadata's export_mime field, so exported native docs
    (Google Docs/Sheets/Slides) are flagged as read-only local mirrors."""
    remote_path_lower = "/doc.gdoc"
    md = FileMetadata(
        name="doc.gdoc",
        path_lower=remote_path_lower,
        path_display="/doc.gdoc",
        id="D1",
        modified=datetime.today(),
        rev="r1",
        size=1,
        symlink_target=None,
        shared=False,
        modified_by=None,
        is_downloadable=True,
        content_hash=None,
        can_edit=True,
        export_mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    sync_engine.update_index_from_remote_metadata(md)

    entry = sync_engine.get_index_entry(remote_path_lower)
    assert entry is not None
    assert entry.is_download_only is True


def test_index_records_download_only_false_without_export_mime(
    sync_engine: SyncEngine,
) -> None:
    """An ordinary binary file (export_mime unset) must not be flagged
    download_only."""
    remote_path_lower = "/plain.txt"
    md = FileMetadata(
        name="plain.txt",
        path_lower=remote_path_lower,
        path_display="/plain.txt",
        id="P1",
        modified=datetime.today(),
        rev="r1",
        size=1,
        symlink_target=None,
        shared=False,
        modified_by=None,
        is_downloadable=True,
        content_hash="h",
        can_edit=True,
    )

    sync_engine.update_index_from_remote_metadata(md)

    entry = sync_engine.get_index_entry(remote_path_lower)
    assert entry is not None
    assert entry.is_download_only is False


def test_index_records_download_only_false_for_folder(
    sync_engine: SyncEngine,
) -> None:
    """Folders are never exportable native docs, so download_only must be
    falsy for FolderMetadata regardless of can_edit."""
    remote_path_lower = "/afolder"
    md = FolderMetadata(
        name="afolder",
        path_lower=remote_path_lower,
        path_display="/afolder",
        id="F1",
        shared=False,
        can_edit=True,
    )

    sync_engine.update_index_from_remote_metadata(md)

    entry = sync_engine.get_index_entry(remote_path_lower)
    assert entry is not None
    assert entry.is_download_only is False


def test_sync_event_from_metadata_sets_download_only_from_export_mime(
    sync_engine: SyncEngine,
) -> None:
    """SyncEvent.from_metadata must set download_only truthy iff the
    remote metadata's export_mime is set, and update_index_from_sync_event
    (the seam used by the real download-apply path) must thread that value
    through to the resulting IndexEntry."""
    remote_path_lower = "/exported.gsheet"
    md = FileMetadata(
        name="exported.gsheet",
        path_lower=remote_path_lower,
        path_display="/exported.gsheet",
        id="S1",
        modified=datetime.today(),
        rev="r1",
        size=1,
        symlink_target=None,
        shared=True,
        modified_by="tester",
        is_downloadable=True,
        content_hash=None,
        can_edit=True,
        export_mime="text/csv",
    )

    event = SyncEvent.from_metadata(md, sync_engine)
    assert event.download_only == 1

    sync_engine.update_index_from_sync_event(event)

    entry = sync_engine.get_index_entry(remote_path_lower)
    assert entry is not None
    assert entry.is_download_only is True


# ==== Task 7: read_only index column threaded from can_edit metadata =========


def test_index_records_read_only_from_metadata(
    sync_engine: SyncEngine,
) -> None:
    """update_index_from_remote_metadata must derive IndexEntry.read_only
    from the metadata's can_edit flag, so the upload gate (Task 9) has a
    local source of truth without a remote fetch."""
    # Top-level path so correct_case's ancestor lookup hits the "/" base case
    # instead of requiring a linked client to resolve a parent's casing.
    remote_path_lower = "/v.pdf"
    md = FileMetadata(
        name="v.pdf",
        path_lower=remote_path_lower,
        path_display="/v.pdf",
        id="V1",
        modified=datetime.today(),
        rev="r1",
        size=1,
        symlink_target=None,
        shared=True,
        modified_by=None,
        is_downloadable=True,
        content_hash="h",
        can_edit=False,
    )

    sync_engine.update_index_from_remote_metadata(md)

    entry = sync_engine.get_index_entry(remote_path_lower)
    assert entry is not None
    assert entry.is_read_only is True


def test_readonly_file_download_clears_write_bits(
    sync_engine: SyncEngine,
) -> None:
    """_apply_read_only must clear write bits on a downloaded view-only
    shared file, wrapped in fs_events.ignore so the chmod isn't echoed back
    as a local change (Task 8)."""
    import stat as _stat

    local = osp.join(sync_engine.sync_path, "Shared with me", "v.txt")
    os.makedirs(osp.dirname(local), exist_ok=True)
    with open(local, "w") as f:
        f.write("x")

    sync_engine._apply_read_only(local, is_dir=False)

    mode = _stat.S_IMODE(os.lstat(local).st_mode)
    assert not (mode & 0o222)  # no write bits


def test_apply_read_only_dir_clears_write_bits(sync_engine: SyncEngine) -> None:
    """_apply_read_only(is_dir=True) uses 0o555 so the directory stays
    traversable/listable but not writable."""
    import stat as _stat

    local = osp.join(sync_engine.sync_path, "Shared with me", "d")
    os.makedirs(local, exist_ok=True)

    sync_engine._apply_read_only(local, is_dir=True)

    mode = _stat.S_IMODE(os.lstat(local).st_mode)
    assert not (mode & 0o222)
    assert mode & 0o111  # still traversable


def test_restore_writable_reinstates_write_bit(sync_engine: SyncEngine) -> None:
    """_restore_writable must OR in the owner write bit before the engine
    overwrites/deletes a read-only local file."""
    import stat as _stat

    local = osp.join(sync_engine.sync_path, "Shared with me", "v.txt")
    os.makedirs(osp.dirname(local), exist_ok=True)
    with open(local, "w") as f:
        f.write("x")
    os.chmod(local, 0o444)

    sync_engine._restore_writable(local)

    mode = _stat.S_IMODE(os.lstat(local).st_mode)
    assert mode & 0o200


def test_restore_writable_missing_file_is_noop(sync_engine: SyncEngine) -> None:
    """_restore_writable must not raise if the local target no longer
    exists (e.g. already removed by a prior remote-delete replay)."""
    local = osp.join(sync_engine.sync_path, "Shared with me", "missing.txt")

    sync_engine._restore_writable(local)  # should not raise


def test_restore_writable_recurses_into_nested_read_only_dirs(
    sync_engine: SyncEngine,
) -> None:
    """_restore_writable must restore write permission on EVERY directory in
    a subtree, not just the top-level path (review gap 1). A shared folder
    has every nested level independently chmod'd 0o555 by _apply_read_only,
    so a shallow restore leaves nested dirs read-only and a subsequent
    shutil.rmtree of the subtree raises PermissionError on the first
    read-only nested directory, leaving the item stuck."""
    import shutil
    import stat as _stat

    top = osp.join(sync_engine.sync_path, "Shared with me", "folder")
    nested = osp.join(top, "sub", "subsub")
    os.makedirs(nested, exist_ok=True)
    with open(osp.join(nested, "file.txt"), "w") as f:
        f.write("x")

    # Simulate what _apply_read_only(is_dir=True) does at every level of a
    # downloaded read-only shared folder tree.
    for dirpath, _dirnames, _filenames in os.walk(top, topdown=False):
        os.chmod(dirpath, 0o555)
    os.chmod(top, 0o555)

    sync_engine._restore_writable(top)

    for dirpath, _dirnames, _filenames in os.walk(top):
        mode = _stat.S_IMODE(os.lstat(dirpath).st_mode)
        assert mode & 0o200, f"{dirpath} should be writable after restore"

    # The whole point: deleting the subtree must now succeed without
    # raising PermissionError on a still-read-only nested directory.
    shutil.rmtree(top)
    assert not os.path.exists(top)


def test_restore_writable_single_file_still_uses_owner_write_bit(
    sync_engine: SyncEngine,
) -> None:
    """Non-directory targets keep the existing narrow restore (mode | 0o200)
    -- the recursive directory-walk logic must not be applied to files."""
    import stat as _stat

    local = osp.join(sync_engine.sync_path, "Shared with me", "solo.txt")
    os.makedirs(osp.dirname(local), exist_ok=True)
    with open(local, "w") as f:
        f.write("x")
    os.chmod(local, 0o444)

    sync_engine._restore_writable(local)

    mode = _stat.S_IMODE(os.lstat(local).st_mode)
    assert mode & 0o200
    assert not (mode & 0o111)  # exec bits untouched, unlike the dir path


def test_is_read_only_event_reads_index_entry(sync_engine: SyncEngine) -> None:
    """_is_read_only_event must consult the index entry at
    event.remote_path_lower rather than any flag on the SyncEvent itself."""
    remote_path = "/v.pdf"
    entry = IndexEntry(
        remote_path_cased=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id="id:ro",
        item_type=ItemType.File,
        last_sync=0.0,
        rev="r1",
        content_hash="h",
        symlink_target=None,
        read_only=1,
    )
    with sync_engine._database_access():
        sync_engine._index_table.update(entry)

    event = _download_event(sync_engine, remote_path, "r1", "h")

    assert sync_engine._is_read_only_event(event) is True


def test_is_read_only_event_false_when_no_index_entry(
    sync_engine: SyncEngine,
) -> None:
    """_is_read_only_event must return False (not raise) when there is no
    index entry yet for the event's path."""
    event = _download_event(sync_engine, "/nope.pdf", "r1", "h")

    assert sync_engine._is_read_only_event(event) is False


# ==== Task 9: gate uploads on the read_only index flag ======================


def test_read_only_local_change_is_skipped_not_uploaded(
    sync_engine: SyncEngine,
) -> None:
    """_create_remote_entry must gate local mutations to a view-only shared
    item: the index row is read-only, so the event is marked Skipped and the
    upload handler (_on_local_file_modified) must never be invoked."""
    remote_path = "/shared with me/v.txt"
    _seed_index_entry(
        sync_engine,
        remote_path,
        rev="r1",
        content_hash=_md5(b"remote content"),
        last_sync=0.0,
        read_only=1,
    )
    event = _local_modified_event(sync_engine, remote_path, _md5(b"local edit"))

    called = []
    sync_engine._on_local_file_modified = lambda e: called.append(e)  # type: ignore[method-assign]

    result = sync_engine._create_remote_entry(event)

    assert result.status == SyncStatus.Skipped
    assert called == []


def test_editable_local_change_is_not_gated(sync_engine: SyncEngine) -> None:
    """A local change to an item with no index entry (i.e. not read-only)
    must fall through to the normal upload path unchanged."""
    remote_path = "/shared with me/editable.txt"
    event = _local_modified_event(sync_engine, remote_path, _md5(b"local edit"))

    called = []
    sync_engine._on_local_file_modified = lambda e: (  # type: ignore[method-assign]
        called.append(e) or SyncStatus.Done
    )

    result = sync_engine._create_remote_entry(event)

    assert result.status == SyncStatus.Done
    assert called == [event]


def test_read_only_gate_discards_activity_and_clears_sync_errors(
    sync_engine: SyncEngine,
) -> None:
    """Review fix: the read-only gate in _create_remote_entry used to `return
    event` immediately after marking it Skipped, bypassing the cleanup tail
    that every other exit path reaches (clear_sync_errors_from_event +
    activity.discard). That leaked a permanent activity-tree entry for every
    gated event (ActivityTree.add only auto-removes Failed events, never
    Skipped) and meant a pre-existing sync error for a path that later became
    read-only could never be cleared, since every future local event to that
    path just hits the gate again.

    This mirrors the reviewer's repro: the caller (apply_local_changes) adds
    the event to sync_engine.activity *before* calling _create_remote_entry,
    and a prior sync error may already be recorded for the path.
    """
    remote_path = "/shared with me/stuck.txt"
    _seed_index_entry(
        sync_engine,
        remote_path,
        rev="r1",
        content_hash=_md5(b"remote content"),
        last_sync=0.0,
        read_only=1,
    )

    # Seed a pre-existing sync error for the path (e.g. left over from before
    # the item became read-only, or from a prior failed attempt).
    err = DataChangedError(
        "Data changed", "stale error", remote_path=remote_path
    )
    sync_engine._handle_sync_error(err, direction=SyncDirection.Up)
    assert sync_engine.has_sync_errors() is True

    event = _local_modified_event(sync_engine, remote_path, _md5(b"local edit"))

    # Mirror apply_local_changes: the caller registers the event in the
    # activity tree before handing it to _create_remote_entry.
    sync_engine.activity.add(event)
    assert event in sync_engine.activity.sync_events

    result = sync_engine._create_remote_entry(event)

    assert result.status == SyncStatus.Skipped

    # The event must be dropped from the in-memory activity tree, not leaked
    # forever.
    assert event not in sync_engine.activity.sync_events

    # The stale sync error for this path must be cleared, exactly as it would
    # be on the normal (non-gated) success path.
    assert sync_engine.has_sync_errors() is False


# ==== I1 fix: read_only must actually reach the index on the download path ==
#
# Blocker from the whole-branch review: update_index_from_sync_event (the
# ONLY writer on the download apply path -- _on_remote_file/_on_remote_folder)
# could not derive read_only from can_edit because SyncEvent carried no such
# field, so it always wrote read_only=0 (or preserved a prior row). Only
# update_index_from_remote_metadata (called from upload/move handlers, never
# from download) derived it correctly. Net effect: a view-only shared item
# downloaded via the real sync path landed WRITABLE and its upload gate never
# armed. These tests drive the REAL download path end-to-end (FakeDrive ->
# client.get_metadata/download -> SyncEvent.from_metadata ->
# _create_local_entry/_on_remote_file -> update_index_from_sync_event) rather
# than seeding read_only by hand, so they actually exercise the seam that was
# broken.


def test_view_only_shared_file_download_sets_read_only_and_gates_upload(
    sync_engine: SyncEngine,
) -> None:
    """End-to-end regression for the I1 blocker (file variant).

    Before the fix: SyncEvent had no can_edit/read_only field, so
    update_index_from_sync_event always wrote read_only=0 for a freshly
    downloaded item, _is_read_only_event returned False, _apply_read_only
    never ran (file stayed writable), and a subsequent local edit was NOT
    gated by _create_remote_entry -- it fell through to
    _on_local_file_modified, i.e. would have been uploaded and hit a Drive
    403 (the retry-storm scenario spec Sec5 exists to prevent).
    """
    import stat as _stat

    shared_dir = os.path.join(sync_engine.sync_path, "Shared with me")
    if os.path.exists(shared_dir):
        delete(shared_dir)

    fake = FakeDrive()
    content = b"view only content"
    file_id = fake._insert("viewonly.txt", "OTHER_ROOT", "text/plain", content)
    fake.files[file_id]["sharedWithMe"] = True
    fake.files[file_id]["ownedByMe"] = False
    fake.files[file_id]["capabilities"] = {"canEdit": False}
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._cached_account_info = FullAccount(
        account_id="account-id",
        display_name="Offline Tester",
        email="offline@example.com",
        profile_photo_url=None,
        disabled=False,
    )
    remote_path = "/Shared with me/viewonly.txt"
    sync_engine.client._path_cache.set(
        remote_path,
        file_id,
        parent_id=SHARED_WITH_ME_ID,
        name="viewonly.txt",
        is_folder=False,
    )

    # Real download path: get_metadata -> SyncEvent.from_metadata ->
    # _create_local_entry -> _on_remote_file -> update_index_from_sync_event.
    success = sync_engine.get_remote_item(remote_path)
    assert success, "download of view-only shared file must succeed"

    local_file = os.path.join(shared_dir, "viewonly.txt")
    assert os.path.isfile(local_file)
    with open(local_file, "rb") as f:
        assert f.read() == content

    # (1) The downloaded local file must have its write bits cleared.
    mode = _stat.S_IMODE(os.lstat(local_file).st_mode)
    assert not (mode & 0o222), "view-only download must be written read-only"

    # Index row backing the gate must also be read-only (belt and braces --
    # this is what _is_read_only_event actually consults).
    entry = sync_engine.get_index_entry(remote_path.lower())
    assert entry is not None
    assert entry.is_read_only is True

    # (2) A subsequent local-modified event for this path must be gated:
    # _create_remote_entry must skip it without ever invoking the upload
    # handler. Restore the write bit first to simulate an edit actually
    # landing on disk (e.g. a race, or a filesystem that ignores the chmod).
    os.chmod(local_file, 0o644)
    with open(local_file, "wb") as f:
        f.write(b"local edit attempt")

    event = _local_modified_event(
        sync_engine, remote_path, _md5(b"local edit attempt")
    )

    called = []
    sync_engine._on_local_file_modified = lambda e: called.append(e)  # type: ignore[method-assign]

    result = sync_engine._create_remote_entry(event)

    assert result.status == SyncStatus.Skipped
    assert called == []


def test_view_only_shared_folder_download_sets_read_only_dir(
    sync_engine: SyncEngine,
) -> None:
    """Folder variant of the above: _on_remote_folder must also derive
    read_only from can_edit via the real from_metadata ->
    update_index_from_sync_event seam, chmod'ing the directory 0o555.

    A full end-to-end drive through get_remote_item/_get_remote_folder would
    fan out through a thread pool (do_parallel), and this fixture's
    in-memory sqlite connection isn't safe to touch from worker threads --
    the same pre-existing fixture limitation noted in
    test_get_remote_item_creates_shared_with_me_mount. This instead builds
    the event via the real SyncEvent.from_metadata(md, sync_engine) (the same
    call get_remote_item/_get_remote_folder make) and drives it straight into
    the real _on_remote_folder, which is the actual code path under test for
    the read_only seam.
    """
    import stat as _stat

    os.makedirs(
        os.path.join(sync_engine.sync_path, "Shared with me"), exist_ok=True
    )

    remote_path = "/Shared with me/ViewOnlyFolder"
    # Seed the parent's casing/index row so _ensure_parent and correct_case()
    # resolve "/Shared with me" locally instead of requiring a linked client.
    _seed_index_entry(
        sync_engine, "/Shared with me", rev="folder", content_hash=None,
        last_sync=0.0, remote_id="SR",
    )

    md = FolderMetadata(
        name="ViewOnlyFolder",
        path_lower=remote_path.lower(),
        path_display=remote_path,
        id="FOLDER-VIEWONLY",
        shared=True,
        can_edit=False,
    )

    event = SyncEvent.from_metadata(md, sync_engine)
    status = sync_engine._on_remote_folder(event)

    assert status == SyncStatus.Done
    assert os.path.isdir(event.local_path)

    mode = _stat.S_IMODE(os.lstat(event.local_path).st_mode)
    assert not (mode & 0o222), "view-only shared folder must be written read-only"
    assert mode & 0o111, "folder must remain traversable/listable"

    entry = sync_engine.get_index_entry(remote_path.lower())
    assert entry is not None
    assert entry.is_read_only is True


# ==== Task 14: download path materializes shortcuts via the platform writer ==


def test_symlink_download_uses_platform_writer(
    sync_engine: SyncEngine, monkeypatch
) -> None:
    """_on_remote_file's symlink branch must call write_platform_symlink
    (imported at module scope in sync.py so Windows can fall back to a
    .lnk), not a bare os.symlink."""
    calls: list[tuple[str, str]] = []
    import bora.sync as sync_mod

    def fake_write_platform_symlink(target: str, link_path: str) -> str:
        calls.append((target, link_path))
        # _on_remote_file os.lstat()s the path this function returns (to
        # feed _save_local_hash's inode/mtime), so the fake still has to
        # leave something on disk and return its path -- this verifies
        # write_platform_symlink was called with the right args without
        # depending on its real (unpatched) POSIX-symlink behavior.
        os.symlink(target, link_path)
        return link_path

    monkeypatch.setattr(
        sync_mod, "write_platform_symlink", fake_write_platform_symlink
    )

    remote_path = "/Shared with me/link"
    local_path = sync_engine.to_local_path_from_cased(remote_path)
    # The parent "Shared with me" mount is normally materialized by
    # ensure_shared_with_me_dir_present() before a download cycle runs;
    # _ensure_parent cannot create it (it's a virtual root with no remote
    # metadata), so create it directly here -- mirrors the pattern used for
    # other direct _on_remote_file-adjacent tests (e.g. _write_local_file).
    os.makedirs(osp.dirname(local_path), exist_ok=True)

    event = _symlink_event(sync_engine, remote_path, target="../Docs/report.pdf")

    status = sync_engine._on_remote_file(event)

    assert status == SyncStatus.Done
    assert calls == [("../Docs/report.pdf", local_path)]
    assert os.path.islink(local_path)
    assert os.readlink(local_path) == "../Docs/report.pdf"


# ==== Task 15: upload guard relaxed for known shortcut symlinks (M2) ========


def test_local_symlink_matching_known_shortcut_is_skipped(
    sync_engine: SyncEngine,
) -> None:
    """A local symlink whose path matches a KNOWN shortcut (an index row with
    a symlink_target) is silently ignored on upload -- M2 is download-only for
    shortcuts; local edits/deletes are not propagated until M3."""
    remote_path = "/Shared with me/link"
    _seed_index_entry(
        sync_engine,
        remote_path,
        rev="r1",
        content_hash=None,
        last_sync=0.0,
        remote_id="SL",
        symlink_target="../Docs/report.pdf",
    )

    event = _local_modified_event(sync_engine, remote_path, content_hash="unused")
    event.symlink_target = "../Docs/report.pdf"

    status = sync_engine._on_local_file_modified(event)

    assert status == SyncStatus.Skipped


def test_unknown_local_symlink_still_errors(sync_engine: SyncEngine) -> None:
    """A local symlink with no matching index row (or one that is not itself
    a symlink) is still unsupported and must raise SymlinkError, as before."""
    from bora.exceptions import SymlinkError

    remote_path = "/nope/link"
    event = _local_modified_event(sync_engine, remote_path, content_hash="unused")
    event.symlink_target = "to_nowhere"

    with pytest.raises(SymlinkError):
        sync_engine._on_local_file_modified(event)


# ==== Task 17: create/retarget Drive shortcuts from local symlinks (M3) =====
#
# Extends the Task 15 symlink branch: a brand-new local symlink (no index
# row) whose target maps to a known Drive item now creates a shortcut instead
# of raising SymlinkError; a known shortcut whose target TEXT changed is
# retargeted in place; a known shortcut whose text is unchanged remains the
# Task 15 no-op (see test_local_symlink_matching_known_shortcut_is_skipped
# above). A known shortcut whose new target does not map to a synced item
# raises SymlinkError, same as the brand-new-unmappable case -- reconciled in
# Task 18 (Finding 2): both "left local, target unmappable" cases now surface
# the same sync error/notice instead of one erroring and the other silently
# skipping with just a debug log.


def _make_shortcut_metadata(remote_path: str, file_id: str, target_rel: str) -> FileMetadata:
    return FileMetadata(
        name=osp.basename(remote_path),
        path_lower=remote_path.lower(),
        path_display=remote_path,
        id=file_id,
        modified=datetime.today(),
        rev="",
        size=0,
        symlink_target=target_rel,
        shared=False,
        modified_by=None,
        is_downloadable=False,
        content_hash="",
    )


def test_local_symlink_new_target_creates_shortcut(
    sync_engine: SyncEngine, monkeypatch
) -> None:
    """A brand-new local symlink (no index row) whose target resolves to a
    known Drive item calls client.create_shortcut and indexes the result --
    replacing the old blanket SymlinkError for this mappable case."""
    remote_path = "/Shared with me/link"
    target_remote = "/Docs/report.pdf"
    sync_engine.client._path_cache.set(
        target_remote, "F-target", parent_id="F-docs", name="report.pdf",
        is_folder=False,
    )
    # Seed the parent's casing in the index so update_index_from_remote_metadata's
    # correct_case() resolves "/Shared with me" locally instead of hitting the
    # (unlinked, in this test) network client.
    _seed_index_entry(
        sync_engine, "/Shared with me", rev="folder", content_hash=None,
        last_sync=0.0, remote_id="SR",
    )

    event = _local_modified_event(sync_engine, remote_path, content_hash="unused")
    event.symlink_target = "../Docs/report.pdf"

    md_new = _make_shortcut_metadata(remote_path, "F-shortcut", "../Docs/report.pdf")
    calls = []
    monkeypatch.setattr(
        sync_engine.client, "create_shortcut",
        lambda rp, trp, **kw: calls.append((rp, trp)) or md_new,
    )

    status = sync_engine._on_local_file_modified(event)

    assert status == SyncStatus.Done
    assert calls == [(remote_path, target_remote)]
    entry = sync_engine._index_table.get(remote_path.lower())
    assert entry is not None
    assert entry.is_symlink
    assert entry.remote_id == "F-shortcut"


def test_local_symlink_known_shortcut_retargets_on_target_change(
    sync_engine: SyncEngine, monkeypatch
) -> None:
    """A KNOWN shortcut (index row with is_symlink + remote_id) whose local
    symlink text changed calls client.retarget_shortcut with the new target,
    not create_shortcut."""
    remote_path = "/Shared with me/link"
    new_target_remote = "/Docs/other.pdf"
    _seed_index_entry(
        sync_engine, remote_path, rev="r1", content_hash=None, last_sync=0.0,
        remote_id="SL", symlink_target="../Docs/report.pdf",
    )
    _seed_index_entry(
        sync_engine, "/Shared with me", rev="folder", content_hash=None,
        last_sync=0.0, remote_id="SR",
    )
    sync_engine.client._path_cache.set(
        new_target_remote, "F-other", parent_id="F-docs", name="other.pdf",
        is_folder=False,
    )

    event = _local_modified_event(sync_engine, remote_path, content_hash="unused")
    event.symlink_target = "../Docs/other.pdf"

    md_new = _make_shortcut_metadata(remote_path, "SL", "../Docs/other.pdf")
    calls = []
    monkeypatch.setattr(
        sync_engine.client, "retarget_shortcut",
        lambda rp, trp: calls.append((rp, trp)) or md_new,
    )
    monkeypatch.setattr(
        sync_engine.client, "create_shortcut",
        lambda *a, **kw: pytest.fail("create_shortcut must not be called for a known shortcut"),
    )

    status = sync_engine._on_local_file_modified(event)

    assert status == SyncStatus.Done
    assert calls == [(remote_path, new_target_remote)]
    entry = sync_engine._index_table.get(remote_path.lower())
    assert entry is not None
    assert entry.symlink_target == "../Docs/other.pdf"


def test_local_symlink_known_shortcut_unmappable_new_target_errors(
    sync_engine: SyncEngine, monkeypatch
) -> None:
    """A known shortcut whose target text changed to something that does not
    map to a synced Drive item raises SymlinkError (Task 18, Finding 2): the
    item is left local and surfaced as a sync error/notice, the same uniform
    behaviour as a brand-new unmappable symlink
    (test_unknown_local_symlink_still_errors) -- not a silent Skip."""
    from bora.exceptions import SymlinkError

    remote_path = "/Shared with me/link"
    _seed_index_entry(
        sync_engine, remote_path, rev="r1", content_hash=None, last_sync=0.0,
        remote_id="SL", symlink_target="../Docs/report.pdf",
    )

    event = _local_modified_event(sync_engine, remote_path, content_hash="unused")
    event.symlink_target = "../Docs/nope.pdf"  # never seeded in the path cache

    calls = []
    monkeypatch.setattr(
        sync_engine.client, "retarget_shortcut",
        lambda rp, trp: calls.append((rp, trp)),
    )

    with pytest.raises(SymlinkError):
        sync_engine._on_local_file_modified(event)

    assert calls == []


def test_local_symlink_known_shortcut_non_ascii_target_unchanged_is_skipped(
    sync_engine: SyncEngine, monkeypatch
) -> None:
    """Regression: entry.symlink_target is the ENCODED relpath (as stored by
    create_shortcut/retarget_shortcut in writes.py), while event.symlink_target
    is the raw (decoded) os.readlink() text. For a non-ASCII target name these
    differ textually even when nothing changed -- comparing them directly
    misclassifies an unchanged shortcut as "changed" and retargets it (and
    burns a quota-consuming files.update call) on every scan cycle. The
    no-op check must compare like-for-like (both encoded)."""
    remote_path = "/Shared with me/link"
    target_remote = "/Docs/café.pdf"  # café.pdf
    encoded_target_remote = encode_path(target_remote)
    assert encoded_target_remote == "/Docs/caf%C3%A9.pdf"

    # entry.symlink_target as actually stored by create_shortcut/retarget_shortcut:
    # posixpath.relpath(ENCODED target, ENCODED own dirname).
    _seed_index_entry(
        sync_engine, remote_path, rev="r1", content_hash=None, last_sync=0.0,
        remote_id="SL", symlink_target="../Docs/caf%C3%A9.pdf",
    )
    _seed_index_entry(
        sync_engine, "/Shared with me", rev="folder", content_hash=None,
        last_sync=0.0, remote_id="SR",
    )
    sync_engine.client._path_cache.set(
        encoded_target_remote, "F-target", parent_id="F-docs", name="caf%C3%A9.pdf",
        is_folder=False,
    )

    event = _local_modified_event(sync_engine, remote_path, content_hash="unused")
    # Raw readlink() text is human/decoded and UNCHANGED from what produced
    # the stored shortcut above -- this must be a no-op.
    event.symlink_target = "../Docs/café.pdf"

    calls = []
    monkeypatch.setattr(
        sync_engine.client, "retarget_shortcut",
        lambda rp, trp: calls.append((rp, trp)),
    )
    monkeypatch.setattr(
        sync_engine.client, "create_shortcut",
        lambda *a, **kw: pytest.fail("create_shortcut must not be called for a known shortcut"),
    )

    status = sync_engine._on_local_file_modified(event)

    assert status == SyncStatus.Skipped
    assert calls == []


def test_local_symlink_replacing_existing_non_symlink_row_errors_not_duplicate(
    sync_engine: SyncEngine, monkeypatch
) -> None:
    """Regression: if a NON-symlink index row (a previously-synced regular
    file/folder) already exists at this path, a local symlink now appearing
    there must NOT call create_shortcut -- that would create a same-name
    duplicate/orphan item on Drive next to the original. It must fall through
    to the existing (pre-Task-17) SymlinkError, even when the symlink's target
    does map to a known Drive item."""
    from bora.exceptions import SymlinkError

    remote_path = "/Shared with me/link"
    target_remote = "/Docs/report.pdf"

    # Existing REGULAR (non-symlink) index row at this path.
    _seed_index_entry(
        sync_engine, remote_path, rev="r1", content_hash="deadbeef", last_sync=0.0,
        remote_id="F-old",
    )
    _seed_index_entry(
        sync_engine, "/Shared with me", rev="folder", content_hash=None,
        last_sync=0.0, remote_id="SR",
    )
    sync_engine.client._path_cache.set(
        target_remote, "F-target", parent_id="F-docs", name="report.pdf",
        is_folder=False,
    )

    event = _local_modified_event(sync_engine, remote_path, content_hash="unused")
    event.symlink_target = "../Docs/report.pdf"

    calls = []
    monkeypatch.setattr(
        sync_engine.client, "create_shortcut",
        lambda *a, **kw: calls.append(a) or pytest.fail(
            "create_shortcut must not be called; it would duplicate the "
            "existing non-symlink item on Drive"
        ),
    )

    with pytest.raises(SymlinkError):
        sync_engine._on_local_file_modified(event)

    assert calls == []


# ==== Task 16: local symlink deletion propagates to the Drive shortcut (M3) ============
#
# A shortcut is an ordinary index row (with a ``symlink_target``) plus a
# path-cache entry -- there is no separate "shortcut" storage. _on_local_deleted
# is generic: it resolves the remote item by path via ``client.get_metadata``
# and, absent a mismatch, deletes it via the path-based ``client.remove``. It
# carries no symlink-specific guard, so deleting the local symlink should
# already propagate to a remote shortcut delete with no production change.


def _local_deleted_event(
    sync: SyncEngine, remote_path: str, remote_id: str = "SL"
) -> SyncEvent:
    """Build a local-deleted upload SyncEvent for a (now-vanished) symlink."""
    return SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=remote_id,
        local_path=sync.to_local_path_from_cased(remote_path),
        rev=None,
        content_hash=None,
        symlink_target=None,
        change_type=ChangeType.Removed,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )


def test_delete_local_symlink_deletes_remote_shortcut(
    sync_engine: SyncEngine, monkeypatch
) -> None:
    """Deleting a local shortcut symlink must delete the corresponding Drive
    shortcut via the path-based ``client.remove`` (Task 16 / M3)."""
    remote_path = "/Shared with me/link"

    _seed_index_entry(
        sync_engine,
        remote_path,
        rev="r1",
        content_hash=None,
        last_sync=0.0,
        remote_id="SL",
        symlink_target="../Docs/report.pdf",
    )

    # Seed the path cache so a path-based resolution (e.g. inside client.remove
    # in production) would succeed, mirroring the suite's other deletion tests.
    sync_engine.client._path_cache.set(
        remote_path, "SL", parent_id="SR", name="link", is_folder=False
    )

    md_remote = FileMetadata(
        name="link",
        path_lower=remote_path.lower(),
        path_display=remote_path,
        id="SL",
        modified=datetime.today(),
        rev="r1",
        size=0,
        symlink_target="../Docs/report.pdf",
        shared=True,
        modified_by=None,
        is_downloadable=False,
        content_hash="",
    )
    monkeypatch.setattr(
        sync_engine.client, "get_metadata", lambda path, **kwargs: md_remote
    )

    calls = []
    # remove() is path-based: (remote_path, parent_rev=None).
    monkeypatch.setattr(
        sync_engine.client,
        "remove",
        lambda p, parent_rev=None: calls.append(p),
    )

    event = _local_deleted_event(sync_engine, remote_path, remote_id="SL")

    status = sync_engine._on_local_deleted(event)

    assert status == SyncStatus.Done
    assert calls == [remote_path]


def test_delete_local_symlink_of_real_shortcut_skips_rev_check(
    sync_engine: SyncEngine, monkeypatch
) -> None:
    """A real Drive shortcut's index row carries rev="" (``create_shortcut``),
    while Drive/FakeDrive assign the shortcut a ``headRevisionId``. A rev-checked
    remove (``parent_rev=""``) compares ``headRevisionId != ""`` and raises
    ``DataChangedError``, so the delete would be skipped and the local symlink
    resurrected on the next download. The delete path must skip the rev-check for
    a symlink row (like it does for folders) and actually trash the shortcut.

    Unlike ``test_delete_local_symlink_deletes_remote_shortcut`` above, which
    monkeypatches ``client.remove`` and so never exercises the rev-check, this
    drives the REAL ``client.remove`` against FakeDrive.
    """
    remote_path = "/Shared with me/link"

    fake = FakeDrive()
    # A real Drive shortcut is a non-folder record, so FakeDrive gives it a
    # headRevisionId that can never equal the index row's rev="".
    sid = fake._insert("link", ROOT_ID, "application/vnd.google-apps.shortcut", b"")
    assert fake.files[sid]["headRevisionId"], "shortcut must have a headRevisionId"
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._path_cache.set(
        remote_path, sid, parent_id=ROOT_ID, name="link", is_folder=False
    )

    # Index row exactly as create_shortcut leaves it: rev="", is_symlink.
    _seed_index_entry(
        sync_engine, remote_path, rev="", content_hash=None, last_sync=0.0,
        remote_id=sid, symlink_target="../Docs/report.pdf",
    )

    # get_metadata drives the delete branch; the shortcut projects as a symlink
    # FileMetadata (matching the change feed / get_metadata projection).
    md_remote = _make_shortcut_metadata(remote_path, sid, "../Docs/report.pdf")
    monkeypatch.setattr(
        sync_engine.client, "get_metadata", lambda path, **kwargs: md_remote
    )

    event = _local_deleted_event(sync_engine, remote_path, remote_id=sid)

    status = sync_engine._on_local_deleted(event)

    assert status == SyncStatus.Done
    assert fake.files[sid]["trashed"] is True, (
        "shortcut delete must skip the rev-check and trash the Drive shortcut"
    )


def test_moved_symlink_event_reads_target_from_destination(
    sync_engine: SyncEngine,
) -> None:
    """For a MOVED symlink, ``SyncEvent.from_file_system_event`` must read the
    symlink target from the destination (``to_path``), not ``src_path`` -- the
    old path no longer exists on disk after the move, so reading it drops
    ``symlink_target`` to None and the moved shortcut is misclassified as a
    regular file (``is_symlink`` False), breaking retarget/recognition.
    """
    src_local = sync_engine.to_local_path_from_cased("/orig_link")
    dest_local = sync_engine.to_local_path_from_cased("/moved_link")
    # Only the destination exists on disk (the move already happened); src is gone.
    os.symlink("../Docs/report.pdf", dest_local)

    event = FileMovedEvent(src_path=src_local, dest_path=dest_local)
    sync_event = SyncEvent.from_file_system_event(event, sync_engine)

    assert sync_event.change_type is ChangeType.Moved
    assert sync_event.symlink_target == "../Docs/report.pdf"


# ==== Task 18: gate reverse shortcut propagation on write access (M3) =======
#
# A view-only shared folder (index row with read_only=1) cannot accept
# create/retarget/delete of a Drive shortcut inside it, even though the
# shortcut's OWN index row (if any) may not exist yet or may not itself carry
# read_only=1. The generic item-level gate in _is_read_only_event only fires
# for a path that already has an index row; a brand-new local symlink has
# none, so it needs an explicit check against its PARENT's read-only flag.


def _local_symlink_create_event(
    sync: SyncEngine, remote_path: str, target: str
) -> SyncEvent:
    """Build a local upload SyncEvent for a brand-new local symlink."""
    event = _local_modified_event(sync, remote_path, content_hash="unused")
    event.symlink_target = target
    return event


def test_shortcut_create_skipped_in_readonly_folder(sync_engine: SyncEngine) -> None:
    """A brand-new local symlink inside a read-only (view-only) shared folder
    is skipped silently -- no create_shortcut call, no persisted sync error,
    no retry storm."""
    _seed_index_entry(
        sync_engine, "/shared with me/team", rev="folder", content_hash=None,
        last_sync=0.0, remote_id="SR", read_only=1,
    )

    created = []
    sync_engine.client.create_shortcut = lambda *a, **k: created.append(a)

    event = _local_symlink_create_event(
        sync_engine, "/Shared with me/Team/link", target="../report.pdf"
    )

    status = sync_engine._on_local_file_modified(event)

    assert status == SyncStatus.Skipped
    assert created == []  # no remote create attempted


def test_shortcut_retarget_skipped_in_readonly_folder(
    sync_engine: SyncEngine,
) -> None:
    """A KNOWN shortcut's retarget is also gated on the containing folder's
    write access, not just brand-new creates."""
    remote_path = "/Shared with me/Team/link"
    _seed_index_entry(
        sync_engine, "/shared with me/team", rev="folder", content_hash=None,
        last_sync=0.0, remote_id="SR", read_only=1,
    )
    _seed_index_entry(
        sync_engine, remote_path, rev="r1", content_hash=None, last_sync=0.0,
        remote_id="SL", symlink_target="../report.pdf",
    )

    retargeted = []
    sync_engine.client.retarget_shortcut = lambda *a, **k: retargeted.append(a)

    event = _local_symlink_create_event(
        sync_engine, remote_path, target="../other.pdf"
    )

    status = sync_engine._on_local_file_modified(event)

    assert status == SyncStatus.Skipped
    assert retargeted == []


def test_shortcut_delete_skipped_in_readonly_folder(sync_engine: SyncEngine) -> None:
    """Deleting a local shortcut symlink inside a read-only shared folder does
    not propagate to a remote delete -- the parent gate applies to delete just
    as it does to create/retarget (Task 16's generic delete path has no
    symlink-specific guard of its own)."""
    remote_path = "/Shared with me/Team/link"
    _seed_index_entry(
        sync_engine, "/shared with me/team", rev="folder", content_hash=None,
        last_sync=0.0, remote_id="SR", read_only=1,
    )
    _seed_index_entry(
        sync_engine, remote_path, rev="r1", content_hash=None, last_sync=0.0,
        remote_id="SL", symlink_target="../report.pdf",
    )

    removed = []
    sync_engine.client.remove = lambda *a, **k: removed.append(a)
    sync_engine.client.get_metadata = lambda *a, **k: pytest.fail(
        "get_metadata must not be called; the parent gate should skip before "
        "any remote call"
    )

    event = _local_deleted_event(sync_engine, remote_path, remote_id="SL")

    status = sync_engine._on_local_deleted(event)

    assert status == SyncStatus.Skipped
    assert removed == []


def test_non_symlink_delete_in_readonly_folder_not_gated_by_parent_check(
    sync_engine: SyncEngine, monkeypatch
) -> None:
    """The parent-read-only gate in _on_local_deleted is scoped to symlink
    (shortcut) rows -- a regular file's own read_only flag (populated from its
    real Drive canEdit) is what gates it, via _is_read_only_event upstream in
    _create_remote_entry, not this parent check. Confirms the new guard does
    not accidentally block ordinary deletes just because the parent folder
    happens to be read-only (e.g. a stale/incorrect parent flag)."""
    remote_path = "/Shared with me/Team/plain.txt"
    _seed_index_entry(
        sync_engine, "/shared with me/team", rev="folder", content_hash=None,
        last_sync=0.0, remote_id="SR", read_only=1,
    )
    _seed_index_entry(
        sync_engine, remote_path, rev="r1", content_hash="deadbeef", last_sync=0.0,
        remote_id="F1",
    )

    md_remote = FileMetadata(
        name="plain.txt",
        path_lower=remote_path.lower(),
        path_display=remote_path,
        id="F1",
        modified=datetime.today(),
        rev="r1",
        size=0,
        symlink_target=None,
        shared=True,
        modified_by=None,
        is_downloadable=True,
        content_hash="deadbeef",
    )
    monkeypatch.setattr(
        sync_engine.client, "get_metadata", lambda path, **kwargs: md_remote
    )

    calls = []
    monkeypatch.setattr(
        sync_engine.client, "remove", lambda p, parent_rev=None: calls.append(p)
    )

    event = _local_deleted_event(sync_engine, remote_path, remote_id="F1")

    status = sync_engine._on_local_deleted(event)

    assert status == SyncStatus.Done
    assert calls == [remote_path]


# ==== Task 7 (export): exported mirrors are chmod'd read-only on download ===
#
# client.download() exports natives (returns FileMetadata with export_mime +
# a locally-computed md5), SyncEvent.from_metadata sets download_only from
# md.export_mime, and update_index_from_sync_event persists it. This drives
# a full download cycle for a native Doc through the real
# client.download -> SyncEvent.from_metadata -> _on_remote_file seam and
# asserts the on-disk read-only bit + persisted download_only/content_hash.


_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def test_export_download_marks_file_read_only_and_download_only(
    sync_engine: SyncEngine,
) -> None:
    """A full download cycle for an exportable native Doc (export enabled)
    must land the exported bytes locally, chmod the local mirror read-only,
    and persist download_only=1 alongside the exported content's md5 in the
    index -- exercising the real _on_remote_file code path rather than
    hand-rolling the assertions.

    The seeded event's content_hash is the real pre-download ``None`` that
    ``to_file_metadata`` produces for exportable docs (Drive has no
    server-side hash for an export), exercising the true first-download path.
    This relies on the ``_check_download_conflict`` fix that stops a None
    remote hash from being misread as "identical" to a None local hash for a
    not-yet-downloaded path (see the preceding conflict-check fix commit)."""
    fake = FakeDrive()
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client.export_opts = ExportOptions(True, False)

    file_id = fake.add_native(name="Report", kind="document")
    content = b"exported docx bytes"
    fake.set_export_bytes(file_id, _DOCX_MIME, content)
    # SyncEvent.from_metadata (called internally by _on_remote_file with the
    # post-download metadata) falls back to sync_engine.client.account_info
    # for change_remote_id when md.shared is False, which requires a linked
    # account. Mark shared=True (mirrors the read-only-folder test's
    # approach below) so it takes the md.modified_by branch instead.
    fake.files[file_id]["shared"] = True

    local_path = sync_engine.to_local_path_from_cased("/Report.docx")
    event = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path="/Report.docx",
        remote_path_lower="/report.docx",
        remote_id=file_id,
        local_path=local_path,
        rev="0",
        content_hash=None,
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )

    status = sync_engine._on_remote_file(event)

    assert status == SyncStatus.Done
    assert os.path.isfile(local_path)
    with open(local_path, "rb") as f:
        assert f.read() == content

    mode = os.stat(local_path).st_mode
    assert mode & 0o222 == 0, "exported mirror must be written read-only"

    entry = sync_engine.get_index_entry("/report.docx")
    assert entry is not None
    assert entry.is_download_only is True
    assert entry.content_hash == _md5(content)


# ==== Task 9 (export): local edits of read-only mirrors never upload ========
#
# A native doc is exported to a read-only local mirror (download_only=1 in the
# index). Local changes to that mirror must NEVER be uploaded (an upload would
# overwrite the live Google Doc with its own exported bytes). If the content
# actually diverged, the user's edit is preserved as a normal writable
# conflicted copy and the server version is re-exported to the original name.


def _export_native_doc(
    sync_engine: SyncEngine,
    name: str = "Report",
    content: bytes = b"exported docx bytes",
) -> tuple[FakeDrive, str, str, str, bytes]:
    """Run a real export-download cycle so the local mirror, its read-only
    bit and its download_only index entry all exist (mirrors
    test_export_download_marks_file_read_only_and_download_only). Returns
    (fake, file_id, remote_path, local_path, content)."""
    fake = FakeDrive()
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client.export_opts = ExportOptions(True, False)

    file_id = fake.add_native(name=name, kind="document")
    fake.set_export_bytes(file_id, _DOCX_MIME, content)
    # shared=True routes from_metadata down the md.modified_by branch (no
    # linked account needed offline) -- see the setup test above.
    fake.files[file_id]["shared"] = True

    remote_path = f"/{name}.docx"
    local_path = sync_engine.to_local_path_from_cased(remote_path)
    event = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=file_id,
        local_path=local_path,
        rev="0",
        content_hash=None,
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )
    assert sync_engine._on_remote_file(event) == SyncStatus.Done
    return fake, file_id, remote_path, local_path, content


def test_download_only_never_uploads(sync_engine: SyncEngine) -> None:
    """A local file event on a read-only export mirror must never produce an
    upload: apply_local_changes returns no result for it and the backing
    Google Doc on the server is left untouched (no files.update ran, so its
    version counter and empty content are unchanged). A metadata-only touch
    (here: making the mirror writable without changing bytes, e.g. the
    read-only chmod being echoed) is ignored outright -- no conflicted copy."""
    fake, file_id, remote_path, local_path, content = _export_native_doc(
        sync_engine
    )

    # Simulate a metadata touch: user flips the read-only bit off, bytes
    # unchanged. The local content hash still equals the indexed export hash.
    os.chmod(local_path, 0o644)
    event = _local_modified_event(
        sync_engine, remote_path, content_hash=_md5(content)
    )

    results = sync_engine.apply_local_changes([event])

    # Nothing was uploaded for the mirror.
    assert all(r.remote_path_lower != remote_path.lower() for r in results)
    # The native doc on the server is untouched: an upload would have called
    # files.update (bumping the native doc's version and writing docx bytes
    # into the Doc). version stays "1" and content stays empty.
    assert fake.files[file_id]["version"] == "1"
    assert fake.files[file_id]["content"] == b""
    # A pure touch leaves the mirror in place -- no conflicted copy spawned.
    siblings = os.listdir(osp.dirname(local_path))
    assert not any("conflicted copy" in n for n in siblings), siblings
    # Index entry is untouched (still the read-only mirror).
    entry = sync_engine.get_index_entry(remote_path.lower())
    assert entry is not None and entry.is_download_only is True


def test_download_only_edit_makes_conflict_copy(sync_engine: SyncEngine) -> None:
    """When the mirror's content genuinely diverges from the last export, the
    user's edit is preserved as a normal, writable, NON-download_only
    conflicted copy, and the original path is dropped from the index so the
    next download pass re-exports the untouched server version to the original
    read-only name."""
    fake, file_id, remote_path, local_path, content = _export_native_doc(
        sync_engine
    )
    directory = osp.dirname(local_path)

    # User edits the mirror: make it writable and change the bytes.
    edited = b"the user's own local edits"
    os.chmod(local_path, 0o644)
    with open(local_path, "wb") as f:
        f.write(edited)

    event = _local_modified_event(
        sync_engine, remote_path, content_hash=_md5(edited)
    )
    results = sync_engine.apply_local_changes([event])

    # Never uploaded to the native doc.
    assert all(r.remote_path_lower != remote_path.lower() for r in results)
    assert fake.files[file_id]["version"] == "1"
    assert fake.files[file_id]["content"] == b""

    # A conflicted copy preserving the user's edit appears.
    cc_names = [
        n
        for n in os.listdir(directory)
        if "conflicted copy" in n and n.endswith(".docx")
    ]
    assert len(cc_names) == 1, os.listdir(directory)
    cc_path = osp.join(directory, cc_names[0])
    with open(cc_path, "rb") as f:
        assert f.read() == edited
    # The preserved copy is a normal, writable file...
    assert os.stat(cc_path).st_mode & 0o200, "conflicted copy must be writable"
    # ...and carries NO download_only index entry (it is a brand-new path).
    cc_remote_lower = sync_engine.to_remote_path_lower(cc_path)
    assert sync_engine.get_index_entry(cc_remote_lower) is None

    # The original path was dropped from the index so the next download pass
    # re-exports the server version (rather than being skipped as "identical").
    assert sync_engine.get_index_entry(remote_path.lower()) is None

    # Prove the re-export actually happens: replay the download event for the
    # original path. With the stale index entry gone this is NOT skipped as
    # LocalNewerOrIdentical -- the untouched server bytes land back at the
    # original read-only name with download_only restored.
    reexport = SyncEvent(
        direction=SyncDirection.Down,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=file_id,
        local_path=local_path,
        rev="0",
        content_hash=None,
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )
    assert sync_engine._on_remote_file(reexport) == SyncStatus.Done
    with open(local_path, "rb") as f:
        assert f.read() == content
    assert os.stat(local_path).st_mode & 0o222 == 0, "re-export must be read-only"
    reexported = sync_engine.get_index_entry(remote_path.lower())
    assert reexported is not None and reexported.is_download_only is True


# ==== Task 10 (export): config toggles wired into client.export_opts ========
#
# sync.export_google_docs / sync.export_google_docs_markdown (config/main.py,
# default True/True) drive client.export_opts via ExportOptions.from_config.
# reload_cached_config is the existing "config changed underneath us, refresh
# cached values" hook (see test_excluded_items_cleaned_at_config_load above);
# it now also refreshes client.export_opts and, on an off -> on transition,
# reuses the sanctioned drop-and-rebuild mechanism (reset_sync_state, the
# same one the Task 6 schema-drift guard relies on) to schedule a reindex so
# previously-skipped native docs are picked up.


def test_export_opts_enabled_by_default_from_config(sync_engine: SyncEngine) -> None:
    """A freshly constructed SyncEngine wires client.export_opts from the
    default config, which is on for both keys."""
    assert sync_engine.client.export_opts.enabled is True
    assert sync_engine.client.export_opts.markdown is True


def test_reload_cached_config_picks_up_export_toggle(sync_engine: SyncEngine) -> None:
    """Editing the config directly (bypassing any setter) and calling
    reload_cached_config -- the established pattern for picking up an
    external config change -- must update client.export_opts, mirroring how
    excluded_items is re-read from a raw config edit."""
    sync_engine._conf.set("sync", "export_google_docs", False)
    sync_engine._conf.set("sync", "export_google_docs_markdown", False)
    sync_engine.reload_cached_config()

    assert sync_engine.client.export_opts.enabled is False
    assert sync_engine.client.export_opts.markdown is False

    sync_engine._conf.set("sync", "export_google_docs", True)
    sync_engine.reload_cached_config()

    assert sync_engine.client.export_opts.enabled is True


def test_export_toggle_off_to_on_schedules_reindex(sync_engine: SyncEngine) -> None:
    """Turning export on from off must schedule a full reindex (the same
    drop-and-rebuild mechanism reset_sync_state / the schema-drift guard
    use) so previously-skipped native docs are pulled in on the next sync
    cycle, rather than being permanently stuck as "already seen"."""
    # Start from export disabled, with some existing sync state/index data
    # that a reindex must clear.
    sync_engine._conf.set("sync", "export_google_docs", False)
    sync_engine.reload_cached_config()
    assert sync_engine.client.export_opts.enabled is False

    _seed_index_entry(
        sync_engine, "/Report", rev="0", content_hash="deadbeef", last_sync=1.0
    )
    sync_engine.remote_cursor = "SOME-CURSOR"
    assert sync_engine.index_count() == 1

    # Flip the toggle on.
    sync_engine._conf.set("sync", "export_google_docs", True)
    sync_engine.reload_cached_config()

    assert sync_engine.client.export_opts.enabled is True
    # reset_sync_state ran: index cleared and remote cursor reset, forcing a
    # full reindex on the next sync cycle.
    assert sync_engine.index_count() == 0
    assert sync_engine.remote_cursor == ""


def test_export_toggle_on_to_off_does_not_reindex(sync_engine: SyncEngine) -> None:
    """Turning export off must not clear the index / reset the cursor --
    only enabling export needs to pick up previously-skipped items."""
    assert sync_engine.client.export_opts.enabled is True  # default: on

    _seed_index_entry(
        sync_engine, "/Report", rev="0", content_hash="deadbeef", last_sync=1.0
    )
    sync_engine.remote_cursor = "SOME-CURSOR"

    sync_engine._conf.set("sync", "export_google_docs", False)
    sync_engine.reload_cached_config()

    assert sync_engine.client.export_opts.enabled is False
    assert sync_engine.index_count() == 1
    assert sync_engine.remote_cursor == "SOME-CURSOR"

