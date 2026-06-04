"""Offline unit tests for cumulus.writes._WriteHelper.

Two layers of coverage:
  1. Mock-service tests (original): inject a chained MagicMock at
     client._get_service(); assert Drive call shape and cache write-through.
  2. FakeDrive integration tests (new, Task B4): use client_on_fake fixture
     so writes round-trip through the in-memory fake; assert state via
     fake.files[...] and via client.get_metadata.
"""

from __future__ import annotations

import hashlib
import os as _os
import sqlite3
import tempfile
from unittest.mock import MagicMock, Mock, patch

import pytest

from cumulus.changes import parse_drive_time
from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.core import FileMetadata, WriteMode
from cumulus.database.core import Database
from cumulus.exceptions import (
    BadInputError,
    CumulusApiError,
    DataChangedError,
    NotFoundError,
)
from cumulus.keyring import CredentialStorage
from cumulus.path_cache import PathIDCache

_CONFIG = "test-config-writes-offline"


@pytest.fixture
def cache() -> PathIDCache:
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = "ROOT"
    return c


@pytest.fixture
def client(cache: PathIDCache):
    cred = CredentialStorage(_CONFIG)
    cl = GoogleDriveClient(_CONFIG, cred, cache)
    # Disable the network-touching ensure-root-id step: tests pre-seed ROOT.
    cl._ensure_root_id = Mock()
    yield cl
    cred.delete_creds()
    remove_configuration(_CONFIG)


def fake_service() -> MagicMock:
    return MagicMock(name="drive-service")


# ==== make_dir ============================================================

def test_make_dir_creates_folder_and_updates_cache(client, cache):
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new",
        "name": "foo",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    result = client._writes.make_dir("/foo")

    # Drive call shape
    create = svc.files.return_value.create
    create.assert_called_once()
    body = create.call_args.kwargs["body"]
    assert body == {
        "name": "foo",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["ROOT"],
    }
    # Cache write-through
    assert cache.path_to_id("/foo") == "F-new"
    # Return value
    assert result.id == "F-new"
    assert result.name == "foo"


def test_make_dir_decodes_name_segment(client, cache):
    # path_codec encodes '/' in names as %2F; the upload/mkdir side
    # must decode before sending to Drive.
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-x", "name": "a/b",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.make_dir("/a%2Fb")

    body = svc.files.return_value.create.call_args.kwargs["body"]
    assert body["name"] == "a/b"


def test_make_dir_missing_parent_raises_not_found(client):
    client._get_service = Mock(return_value=fake_service())
    with pytest.raises(NotFoundError):
        client._writes.make_dir("/nope/foo")


def test_make_dir_autorename_single_collision(client, cache):
    cache.set("/foo", "F-existing", parent_id="ROOT", name="foo", is_folder=True)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "foo (1)",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    result = client._writes.make_dir("/foo", autorename=True)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    # Drive gets the decoded name.
    assert body["name"] == "foo (1)"
    # Cache and path_display use the encoded form.
    assert cache.path_to_id("/foo %281%29") == "F-new"
    assert result.path_display == "/foo %281%29"
    assert result.name == "foo (1)"


def test_make_dir_autorename_multi_collision(client, cache):
    cache.set("/foo", "F1", parent_id="ROOT", name="foo", is_folder=True)
    cache.set("/foo %281%29", "F2", parent_id="ROOT", name="foo (1)", is_folder=True)
    cache.set("/foo %282%29", "F3", parent_id="ROOT", name="foo (2)", is_folder=True)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "foo (3)",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    result = client._writes.make_dir("/foo", autorename=True)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    # Drive gets the decoded name.
    assert body["name"] == "foo (3)"
    # Cache and path_display use the encoded form.
    assert cache.path_to_id("/foo %283%29") == "F-new"
    assert result.path_display == "/foo %283%29"


def test_make_dir_collision_without_autorename_creates_duplicate_name(client, cache):
    # When autorename=False, Drive allows duplicate names. The cache
    # has an existing entry but the call goes through unchanged.
    cache.set("/foo", "F1", parent_id="ROOT", name="foo", is_folder=True)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "foo",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.make_dir("/foo", autorename=False)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    assert body["name"] == "foo"
    # PathIDCache.set() rejects case-conflicting writes (different IDs at the
    # same folded path). The existing entry wins and the old ID is retained.
    # This is expected: the caller should use autorename=True to avoid this.
    assert cache.path_to_id("/foo") == "F1"


def test_make_dir_autorename_bounded_at_999(client, cache):
    # Seed 1000 collisions to force overflow.
    # Collision entries must be stored under encoded paths so the probe in
    # _autorename_unique (which works in encoded namespace) finds them.
    cache.set("/foo", "F0", parent_id="ROOT", name="foo", is_folder=True)
    for n in range(1, 1000):
        encoded_path = f"/foo %28{n}%29"
        cache.set(encoded_path, f"F{n}", parent_id="ROOT", name=f"foo ({n})", is_folder=True)
    client._get_service = Mock(return_value=fake_service())

    with pytest.raises(BadInputError):
        client._writes.make_dir("/foo", autorename=True)


def test_make_dir_counts_request(client, cache):
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "foo",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)
    client._count_request = Mock()

    client._writes.make_dir("/foo")

    client._count_request.assert_called_once()


# ==== remove ==============================================================

def test_remove_trashes_by_default(client, cache):
    cache.set("/foo.txt", "F1", parent_id="ROOT", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "foo.txt",
        "mimeType": "application/pdf", "trashed": True,
    }
    client._get_service = Mock(return_value=svc)

    result = client._writes.remove("/foo.txt")

    update = svc.files.return_value.update
    update.assert_called_once()
    assert update.call_args.kwargs["fileId"] == "F1"
    assert update.call_args.kwargs["body"] == {"trashed": True}
    # files.delete must NOT have been called.
    svc.files.return_value.delete.assert_not_called()
    # Cache write-through
    assert cache.path_to_id("/foo.txt") is None
    assert result.id == "F1"


def test_remove_missing_path_raises_not_found(client):
    client._get_service = Mock(return_value=fake_service())
    with pytest.raises(NotFoundError):
        client._writes.remove("/nope.txt")


def test_remove_parent_rev_match_succeeds(client, cache):
    cache.set("/foo.txt", "F1", parent_id="ROOT", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "headRevisionId": "REV-X", "mimeType": "application/pdf",
    }
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "foo.txt",
        "mimeType": "application/pdf", "trashed": True,
    }
    client._get_service = Mock(return_value=svc)

    client._writes.remove("/foo.txt", parent_rev="REV-X")

    svc.files.return_value.update.assert_called_once()


def test_remove_parent_rev_mismatch_raises_data_changed(client, cache):
    cache.set("/foo.txt", "F1", parent_id="ROOT", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "headRevisionId": "REV-NEW", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    with pytest.raises(DataChangedError):
        client._writes.remove("/foo.txt", parent_rev="REV-OLD")

    svc.files.return_value.update.assert_not_called()


def test_remove_parent_rev_on_folder_raises_bad_input(client, cache):
    cache.set("/foo", "D1", parent_id="ROOT", name="foo", is_folder=True)
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    with pytest.raises(BadInputError):
        client._writes.remove("/foo", parent_rev="anything")


def test_hard_delete_primitive(client, cache):
    cache.set("/foo.txt", "F1", parent_id="ROOT", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.delete.return_value.execute.return_value = None
    client._get_service = Mock(return_value=svc)

    client._writes._hard_delete("/foo.txt")

    svc.files.return_value.delete.assert_called_once_with(fileId="F1")
    assert cache.path_to_id("/foo.txt") is None


# ==== move ================================================================

def test_move_renames_in_place(client, cache):
    cache.set("/foo.txt", "F1", parent_id="ROOT", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "bar.txt", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.move("/foo.txt", "/bar.txt")

    update = svc.files.return_value.update
    body = update.call_args.kwargs
    assert body["fileId"] == "F1"
    assert body["body"] == {"name": "bar.txt"}
    # Same parent on both sides — no addParents / removeParents needed.
    assert "addParents" not in body or body["addParents"] in (None, "", "ROOT")
    assert "removeParents" not in body or body["removeParents"] in (None, "", "ROOT")
    # Cache write-through
    assert cache.path_to_id("/foo.txt") is None
    assert cache.path_to_id("/bar.txt") == "F1"


def test_move_changes_parent(client, cache):
    cache.set("/src", "DSRC", parent_id="ROOT", name="src", is_folder=True)
    cache.set("/dst", "DDST", parent_id="ROOT", name="dst", is_folder=True)
    cache.set("/src/foo.txt", "F1", parent_id="DSRC", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "foo.txt", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.move("/src/foo.txt", "/dst/foo.txt")

    body = svc.files.return_value.update.call_args.kwargs
    assert body["body"] == {"name": "foo.txt"}
    assert body["addParents"] == "DDST"
    assert body["removeParents"] == "DSRC"
    assert cache.path_to_id("/src/foo.txt") is None
    assert cache.path_to_id("/dst/foo.txt") == "F1"


def test_move_combined_rename_and_reparent(client, cache):
    cache.set("/src", "DSRC", parent_id="ROOT", name="src", is_folder=True)
    cache.set("/dst", "DDST", parent_id="ROOT", name="dst", is_folder=True)
    cache.set("/src/foo.txt", "F1", parent_id="DSRC", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "bar.txt", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.move("/src/foo.txt", "/dst/bar.txt")

    body = svc.files.return_value.update.call_args.kwargs
    assert body["body"] == {"name": "bar.txt"}
    assert body["addParents"] == "DDST"
    assert body["removeParents"] == "DSRC"


def test_move_missing_source_raises_not_found(client):
    client._get_service = Mock(return_value=fake_service())
    with pytest.raises(NotFoundError):
        client._writes.move("/nope.txt", "/dst.txt")


def test_move_missing_dest_parent_raises_not_found(client, cache):
    cache.set("/foo.txt", "F1", parent_id="ROOT", name="foo.txt", is_folder=False)
    client._get_service = Mock(return_value=fake_service())
    with pytest.raises(NotFoundError):
        client._writes.move("/foo.txt", "/no/such/dir/foo.txt")


def test_move_autorename_on_dest_collision(client, cache):
    cache.set("/src", "DSRC", parent_id="ROOT", name="src", is_folder=True)
    cache.set("/dst", "DDST", parent_id="ROOT", name="dst", is_folder=True)
    cache.set("/src/foo.txt", "F1", parent_id="DSRC", name="foo.txt", is_folder=False)
    cache.set("/dst/foo.txt", "Fexisting", parent_id="DDST", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "foo (1).txt", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.move("/src/foo.txt", "/dst/foo.txt", autorename=True)

    body = svc.files.return_value.update.call_args.kwargs
    # Drive gets the decoded name (no special chars to encode here, so equal).
    assert body["body"] == {"name": "foo (1).txt"}
    # Cache stores the encoded form. For "foo (1).txt", encoded form is
    # "foo %281%29.txt" (parens encode).
    assert cache.path_to_id("/dst/foo %281%29.txt") == "F1"


def test_move_decodes_new_name_segment(client, cache):
    cache.set("/foo.txt", "F1", parent_id="ROOT", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "a/b", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.move("/foo.txt", "/a%2Fb")

    body = svc.files.return_value.update.call_args.kwargs
    assert body["body"]["name"] == "a/b"


# ==== upload — simple (<=5MB) =============================================


def _write_tempfile(content: bytes) -> str:
    fd, path = tempfile.mkstemp()
    with _os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def test_upload_add_creates_new_file(client, cache):
    data = b"hello world"
    local = _write_tempfile(data)
    md5 = _md5_hex(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": md5,
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    result = client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Add)

    create = svc.files.return_value.create
    create.assert_called_once()
    body = create.call_args.kwargs["body"]
    assert body["name"] == "hello.txt"
    assert body["parents"] == ["ROOT"]
    assert "modifiedTime" in body
    assert cache.path_to_id("/hello.txt") == "F-new"
    assert result.id == "F-new"
    _os.unlink(local)


def test_upload_add_existing_without_autorename_raises_file_conflict(client, cache):
    cache.set("/hello.txt", "F-existing",
              parent_id="ROOT", name="hello.txt", is_folder=False)
    local = _write_tempfile(b"x")
    client._get_service = Mock(return_value=fake_service())
    from cumulus.core import WriteMode
    from cumulus.exceptions import FileConflictError

    with pytest.raises(FileConflictError):
        client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Add)
    _os.unlink(local)


def test_upload_add_existing_with_autorename_picks_unique_name(client, cache):
    cache.set("/hello.txt", "F-existing",
              parent_id="ROOT", name="hello.txt", is_folder=False)
    data = b"x"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello (1).txt",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Add, autorename=True)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    # Drive gets decoded name.
    assert body["name"] == "hello (1).txt"
    # Cache stores encoded form. encode_name("hello (1).txt") = "hello %281%29.txt"
    assert cache.path_to_id("/hello %281%29.txt") == "F-new"
    _os.unlink(local)


def test_upload_update_existing(client, cache):
    cache.set("/hello.txt", "F1",
              parent_id="ROOT", name="hello.txt", is_folder=False)
    data = b"new"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev2",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Update)

    svc.files.return_value.update.assert_called_once()
    assert svc.files.return_value.update.call_args.kwargs["fileId"] == "F1"
    # Path -> ID unchanged; cache should not be re-set (no-op for Update).
    assert cache.path_to_id("/hello.txt") == "F1"
    _os.unlink(local)


def test_upload_update_missing_raises_not_found(client, cache):
    local = _write_tempfile(b"x")
    client._get_service = Mock(return_value=fake_service())
    from cumulus.core import WriteMode

    with pytest.raises(NotFoundError):
        client._writes.upload(local, "/nope.txt", write_mode=WriteMode.Update)
    _os.unlink(local)


def test_upload_overwrite_creates_when_missing(client, cache):
    data = b"x"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Overwrite)

    svc.files.return_value.create.assert_called_once()
    _os.unlink(local)


def test_upload_overwrite_updates_when_existing(client, cache):
    cache.set("/hello.txt", "F1",
              parent_id="ROOT", name="hello.txt", is_folder=False)
    data = b"x"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev2",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Overwrite)

    svc.files.return_value.update.assert_called_once()
    _os.unlink(local)


def test_upload_md5_mismatch_raises_data_corruption(client, cache):
    data = b"hello"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": "0" * 32,  # deliberate mismatch
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode
    from cumulus.exceptions import DataCorruptionError

    with pytest.raises(DataCorruptionError):
        client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Add)
    _os.unlink(local)


def test_upload_missing_md5_warns_and_succeeds(client, cache, caplog):
    data = b"hello"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello.txt",
        "mimeType": "text/plain",
        # md5Checksum deliberately omitted
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="cumulus.writes"):
        result = client._writes.upload(
            local, "/hello.txt", write_mode=WriteMode.Add
        )
    assert result.id == "F-new"
    assert any("md5" in r.message.lower() for r in caplog.records)
    _os.unlink(local)


def test_upload_update_rev_mismatch_raises_data_changed(client, cache):
    cache.set("/hello.txt", "F1",
              parent_id="ROOT", name="hello.txt", is_folder=False)
    local = _write_tempfile(b"x")
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "headRevisionId": "rev-new",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    with pytest.raises(DataChangedError):
        client._writes.upload(
            local, "/hello.txt",
            write_mode=WriteMode.Update, update_rev="rev-old",
        )
    # Upload was never attempted.
    svc.files.return_value.update.assert_not_called()
    _os.unlink(local)


def test_upload_decodes_name_segment(client, cache):
    data = b"x"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "a/b",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    client._writes.upload(local, "/a%2Fb", write_mode=WriteMode.Add)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    # Drive name is decoded.
    assert body["name"] == "a/b"
    _os.unlink(local)


# ==== upload — resumable (>5MB) ===========================================

def _big_tempfile(size_bytes: int) -> str:
    """Sparse-ish big file for size-threshold tests. Content is the byte 'a'."""
    fd, path = tempfile.mkstemp()
    with _os.fdopen(fd, "wb") as f:
        f.write(b"a" * size_bytes)
    return path


def test_upload_simple_at_threshold(client, cache):
    # 5 MB exactly -> simple path (threshold is >, not >=).
    size = 5 * 1024 * 1024
    local = _big_tempfile(size)
    md5 = hashlib.md5(b"a" * size).hexdigest()
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "Fa", "name": "big.bin",
        "mimeType": "application/octet-stream",
        "md5Checksum": md5,
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(size), "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    # Patch MediaFileUpload to capture the resumable flag.
    with patch("cumulus.writes.MediaFileUpload") as mfu:
        mfu.return_value = Mock()
        client._writes.upload(local, "/big.bin", write_mode=WriteMode.Add)
    assert mfu.call_args.kwargs.get("resumable") is False
    _os.unlink(local)


def test_upload_resumable_above_threshold(client, cache):
    # 5 MB + 1 byte -> resumable path.
    size = 5 * 1024 * 1024 + 1
    local = _big_tempfile(size)
    md5 = hashlib.md5(b"a" * size).hexdigest()

    # next_chunk returns (status, None) twice then (status, response).
    progress_status = Mock()
    progress_status.resumable_progress = 0
    progress_status.total_size = size

    final_resp = {
        "id": "Fa", "name": "big.bin",
        "mimeType": "application/octet-stream",
        "md5Checksum": md5,
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(size), "headRevisionId": "rev1",
    }

    request = Mock()
    request.next_chunk = Mock(side_effect=[
        (progress_status, None),
        (progress_status, None),
        (progress_status, final_resp),
    ])

    svc = fake_service()
    svc.files.return_value.create.return_value = request
    client._get_service = Mock(return_value=svc)

    from cumulus.core import WriteMode
    with patch("cumulus.writes.MediaFileUpload") as mfu:
        mfu.return_value = Mock()
        client._writes.upload(local, "/big.bin", write_mode=WriteMode.Add)

    assert mfu.call_args.kwargs["resumable"] is True
    assert mfu.call_args.kwargs["chunksize"] == 8 * 1024 * 1024
    assert request.next_chunk.call_count == 3
    assert cache.path_to_id("/big.bin") == "Fa"
    _os.unlink(local)


def test_upload_resumable_storage_quota_exceeded(client, cache, make_http_error):
    size = 5 * 1024 * 1024 + 1
    local = _big_tempfile(size)

    request = Mock()
    request.next_chunk = Mock(side_effect=make_http_error(
        403, reason="storageQuotaExceeded",
    ))
    svc = fake_service()
    svc.files.return_value.create.return_value = request
    client._get_service = Mock(return_value=svc)
    # Disable retry sleep.
    client._retry_transient = lambda fn: fn()

    from cumulus.core import WriteMode
    from cumulus.exceptions import InsufficientSpaceError

    with patch("cumulus.writes.MediaFileUpload") as mfu:
        mfu.return_value = Mock()
        with pytest.raises(InsufficientSpaceError):
            client._writes.upload(local, "/big.bin", write_mode=WriteMode.Add)

    # Cache must NOT have been mutated.
    assert cache.path_to_id("/big.bin") is None
    _os.unlink(local)


def test_upload_resumable_progress_reported(client, cache):
    size = 5 * 1024 * 1024 + 1
    local = _big_tempfile(size)
    md5 = hashlib.md5(b"a" * size).hexdigest()
    final_resp = {
        "id": "Fa", "name": "big.bin",
        "mimeType": "application/octet-stream",
        "md5Checksum": md5,
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(size), "headRevisionId": "rev1",
    }
    s1 = Mock(); s1.resumable_progress = 4 * 1024 * 1024
    s2 = Mock(); s2.resumable_progress = size
    request = Mock()
    request.next_chunk = Mock(side_effect=[
        (s1, None), (s2, final_resp),
    ])
    svc = fake_service()
    svc.files.return_value.create.return_value = request
    client._get_service = Mock(return_value=svc)

    sync_event = Mock()
    from cumulus.core import WriteMode
    with patch("cumulus.writes.MediaFileUpload") as mfu:
        mfu.return_value = Mock()
        client._writes.upload(
            local, "/big.bin",
            write_mode=WriteMode.Add, sync_event=sync_event,
        )

    # Progress is reported via sync_event.completed = status.resumable_progress,
    # matching Stage 3 download's pattern (client.py line 714).
    assert sync_event.completed == size
    _os.unlink(local)


def test_upload_resumable_shutdown_aborts(client, cache):
    size = 5 * 1024 * 1024 + 1
    local = _big_tempfile(size)
    s = Mock(); s.resumable_progress = 1 * 1024 * 1024
    request = Mock()
    request.next_chunk = Mock(return_value=(s, None))
    svc = fake_service()
    svc.files.return_value.create.return_value = request
    client._get_service = Mock(return_value=svc)
    client._poll_event = Mock()
    # First .is_set() check: False (proceed). Second: True (abort).
    client._poll_event.is_set = Mock(side_effect=[False, True])

    from cumulus.core import WriteMode
    from cumulus.exceptions import CancelledError

    with patch("cumulus.writes.MediaFileUpload") as mfu:
        mfu.return_value = Mock()
        with pytest.raises(CancelledError):
            client._writes.upload(local, "/big.bin", write_mode=WriteMode.Add)
    _os.unlink(local)


# ==== make_dir_batch ======================================================

def _batch_request_double(responses_per_id: dict):
    """Build a fake BatchHttpRequest that, on execute(), invokes each
    registered callback with (request_id, response, exception)."""
    class _Batch:
        def __init__(self):
            self._calls = []

        def add(self, request, callback, request_id=None):
            self._calls.append((request_id, callback))

        def execute(self):
            for rid, cb in self._calls:
                resp_or_exc = responses_per_id.get(rid)
                if isinstance(resp_or_exc, Exception):
                    cb(rid, None, resp_or_exc)
                else:
                    cb(rid, resp_or_exc, None)

    return _Batch()


def test_make_dir_batch_creates_each_folder(client, cache):
    from cumulus.writes import _FOLDER_MIME
    svc = fake_service()
    responses = {
        "0": {"id": "DA", "name": "a", "mimeType": _FOLDER_MIME},
        "1": {"id": "DB", "name": "b", "mimeType": _FOLDER_MIME},
        "2": {"id": "DC", "name": "c", "mimeType": _FOLDER_MIME},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)

    result = client._writes.make_dir_batch(["/a", "/b", "/c"])

    assert [r.id for r in result] == ["DA", "DB", "DC"]
    assert cache.path_to_id("/a") == "DA"
    assert cache.path_to_id("/b") == "DB"
    assert cache.path_to_id("/c") == "DC"


def test_make_dir_batch_chunks_at_100(client, cache):
    from cumulus.writes import _FOLDER_MIME
    svc = fake_service()
    batches = []

    def new_batch(*a, **kw):
        # All 101 indices populated; only those in the chunk are invoked
        # via _Batch._calls anyway.
        responses = {str(i): {"id": f"D{i}", "name": str(i),
                              "mimeType": _FOLDER_MIME}
                     for i in range(101)}
        b = _batch_request_double(responses)
        batches.append(b)
        return b

    svc.new_batch_http_request = Mock(side_effect=new_batch)
    client._get_service = Mock(return_value=svc)

    paths = [f"/d{i}" for i in range(101)]
    result = client._writes.make_dir_batch(paths)

    # Two batch round-trips: sizes 100 + 1.
    assert len(batches) == 2
    assert len(batches[0]._calls) == 100
    assert len(batches[1]._calls) == 1
    assert len(result) == 101


def test_make_dir_batch_exactly_100_is_single_batch(client, cache):
    from cumulus.writes import _FOLDER_MIME
    svc = fake_service()
    batches = []

    def new_batch(*a, **kw):
        responses = {str(i): {"id": f"D{i}", "name": str(i),
                              "mimeType": _FOLDER_MIME}
                     for i in range(100)}
        b = _batch_request_double(responses)
        batches.append(b)
        return b

    svc.new_batch_http_request = Mock(side_effect=new_batch)
    client._get_service = Mock(return_value=svc)

    paths = [f"/d{i}" for i in range(100)]
    result = client._writes.make_dir_batch(paths)

    # Exactly one batch round-trip with 100 sub-requests.
    assert len(batches) == 1
    assert len(batches[0]._calls) == 100
    assert len(result) == 100


def test_make_dir_batch_handles_mixed_success_and_error(
    client, cache, make_http_error,
):
    from cumulus.writes import _FOLDER_MIME
    svc = fake_service()
    err = make_http_error(404, reason="notFound")
    responses = {
        "0": {"id": "DA", "name": "a", "mimeType": _FOLDER_MIME},
        "1": err,
        "2": {"id": "DC", "name": "c", "mimeType": _FOLDER_MIME},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)

    result = client._writes.make_dir_batch(["/a", "/b", "/c"])

    assert result[0].id == "DA"
    assert isinstance(result[1], CumulusApiError)
    assert result[2].id == "DC"
    # Only successful entries write through to the cache.
    assert cache.path_to_id("/a") == "DA"
    assert cache.path_to_id("/b") is None
    assert cache.path_to_id("/c") == "DC"


def test_make_dir_batch_counts_subrequests(client, cache):
    from cumulus.writes import _FOLDER_MIME
    svc = fake_service()
    responses = {
        "0": {"id": "DA", "name": "a", "mimeType": _FOLDER_MIME},
        "1": {"id": "DB", "name": "b", "mimeType": _FOLDER_MIME},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)
    client._count_request = Mock()

    client._writes.make_dir_batch(["/a", "/b"])

    # 2 sub-requests in 1 batch -> _count_request(2).
    client._count_request.assert_called_once_with(2)


# ==== remove_batch ========================================================

def test_remove_batch_trashes_each_entry(client, cache):
    cache.set("/a.txt", "FA", parent_id="ROOT", name="a.txt", is_folder=False)
    cache.set("/b.txt", "FB", parent_id="ROOT", name="b.txt", is_folder=False)
    svc = fake_service()
    responses = {
        "0": {"id": "FA", "name": "a.txt", "mimeType": "text/plain",
              "trashed": True},
        "1": {"id": "FB", "name": "b.txt", "mimeType": "text/plain",
              "trashed": True},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)

    # Capture each update call's kwargs so we can assert body shape.
    update_kwargs_seen = []
    def capture(**kw):
        update_kwargs_seen.append(kw)
        return Mock()
    svc.files.return_value.update.side_effect = capture

    result = client._writes.remove_batch([("/a.txt", None), ("/b.txt", None)])

    assert all(kw["body"] == {"trashed": True} for kw in update_kwargs_seen)
    assert {kw["fileId"] for kw in update_kwargs_seen} == {"FA", "FB"}
    assert cache.path_to_id("/a.txt") is None
    assert cache.path_to_id("/b.txt") is None
    assert len(result) == 2


def test_remove_batch_missing_entry_returns_error(client, cache):
    cache.set("/a.txt", "FA", parent_id="ROOT", name="a.txt", is_folder=False)
    svc = fake_service()
    responses = {
        "0": {"id": "FA", "name": "a.txt", "mimeType": "text/plain",
              "trashed": True},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)

    result = client._writes.remove_batch([("/a.txt", None), ("/nope.txt", None)])

    assert result[0].id == "FA"
    assert isinstance(result[1], NotFoundError)


def test_remove_batch_with_rev_runs_precondition(client, cache):
    cache.set("/a.txt", "FA", parent_id="ROOT", name="a.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "headRevisionId": "rev-good",
        "mimeType": "text/plain",
    }
    responses = {
        "0": {"id": "FA", "name": "a.txt", "mimeType": "text/plain",
              "trashed": True},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)

    client._writes.remove_batch([("/a.txt", "rev-good")])

    svc.files.return_value.get.assert_called_once()


def test_remove_batch_rev_mismatch_returns_data_changed(client, cache):
    cache.set("/a.txt", "FA", parent_id="ROOT", name="a.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "headRevisionId": "rev-new", "mimeType": "text/plain",
    }
    # No batch response needed — precheck fails before batch executes.
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double({})
    )
    client._get_service = Mock(return_value=svc)

    result = client._writes.remove_batch([("/a.txt", "rev-old")])

    assert isinstance(result[0], DataChangedError)
    # Cache untouched.
    assert cache.path_to_id("/a.txt") == "FA"


def test_remove_batch_counts_subrequests(client, cache):
    cache.set("/a.txt", "FA", parent_id="ROOT", name="a.txt", is_folder=False)
    cache.set("/b.txt", "FB", parent_id="ROOT", name="b.txt", is_folder=False)
    svc = fake_service()
    responses = {
        "0": {"id": "FA", "name": "a.txt", "mimeType": "text/plain",
              "trashed": True},
        "1": {"id": "FB", "name": "b.txt", "mimeType": "text/plain",
              "trashed": True},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)
    client._count_request = Mock()

    client._writes.remove_batch([("/a.txt", None), ("/b.txt", None)])

    # 2 sub-requests in 1 batch -> _count_request(2).
    # _count_request may also have been called during precondition gets,
    # but for entries without parent_rev, there are no pre-batch GETs.
    client._count_request.assert_called_once_with(2)


# ==== restore =============================================================

def test_restore_missing_path_raises_not_found(client):
    client._get_service = Mock(return_value=fake_service())
    with pytest.raises(NotFoundError):
        client._writes.restore("/nope.txt", "rev1")


def test_restore_native_doc_raises_unsupported(client, cache):
    cache.set("/notes.gdoc", "FN",
              parent_id="ROOT", name="notes.gdoc", is_folder=False)
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "mimeType": "application/vnd.google-apps.document",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.exceptions import UnsupportedFileError

    with pytest.raises(UnsupportedFileError):
        client._writes.restore("/notes.gdoc", "rev1")
    # No revision download attempted.
    svc.revisions.return_value.get_media.assert_not_called()


def test_restore_downloads_revision_and_reuploads(client, cache, monkeypatch):
    cache.set("/foo.txt", "F1",
              parent_id="ROOT", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "mimeType": "text/plain",
    }
    client._get_service = Mock(return_value=svc)

    captured_uploads = []

    def fake_upload(local_path, remote_path, *, write_mode, **kwargs):
        with open(local_path, "rb") as f:
            captured_uploads.append((remote_path, write_mode, f.read()))
        return FileMetadata(
            name="foo.txt",
            path_lower=remote_path.lower(),
            path_display=remote_path,
            id="F1",
            modified=parse_drive_time(None),
            rev="rev-new",
            size=3, symlink_target=None, shared=False,
            modified_by=None, is_downloadable=True,
            content_hash="abc",
        )

    monkeypatch.setattr(client._writes, "upload", fake_upload)

    # Patch the helper's revision-to-tempfile downloader to write a known payload.
    def fake_download(file_id, rev):
        import tempfile
        fd, p = tempfile.mkstemp()
        with _os.fdopen(fd, "wb") as f:
            f.write(b"OLD")
        return p

    monkeypatch.setattr(
        client._writes, "_download_revision_to_tempfile", fake_download,
    )

    result = client._writes.restore("/foo.txt", "rev-old")

    assert len(captured_uploads) == 1
    assert captured_uploads[0][0] == "/foo.txt"
    assert captured_uploads[0][1] is WriteMode.Update
    assert captured_uploads[0][2] == b"OLD"
    assert result.id == "F1"


def test_restore_cleans_up_tempfile_on_success(client, cache, monkeypatch):
    cache.set("/foo.txt", "F1",
              parent_id="ROOT", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "mimeType": "text/plain",
    }
    client._get_service = Mock(return_value=svc)

    tmp_paths: list[str] = []

    def fake_download(file_id, rev):
        import tempfile
        fd, p = tempfile.mkstemp()
        _os.close(fd)
        tmp_paths.append(p)
        return p

    monkeypatch.setattr(
        client._writes, "_download_revision_to_tempfile", fake_download
    )
    monkeypatch.setattr(
        client._writes, "upload",
        lambda *a, **kw: FileMetadata(
            name="foo.txt", path_lower="/foo.txt", path_display="/foo.txt",
            id="F1",
            modified=parse_drive_time(None),
            rev="x",
            size=0, symlink_target=None, shared=False, modified_by=None,
            is_downloadable=True, content_hash="",
        ),
    )

    client._writes.restore("/foo.txt", "rev-old")

    assert tmp_paths
    for p in tmp_paths:
        assert not _os.path.exists(p), f"tempfile {p} was not cleaned up"


def test_restore_cleans_up_tempfile_on_upload_exception(client, cache, monkeypatch):
    cache.set("/foo.txt", "F1",
              parent_id="ROOT", name="foo.txt", is_folder=False)
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "mimeType": "text/plain",
    }
    client._get_service = Mock(return_value=svc)

    tmp_paths: list[str] = []

    def fake_download(file_id, rev):
        import tempfile
        fd, p = tempfile.mkstemp()
        _os.close(fd)
        tmp_paths.append(p)
        return p

    def boom(*a, **kw):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(
        client._writes, "_download_revision_to_tempfile", fake_download
    )
    monkeypatch.setattr(client._writes, "upload", boom)

    with pytest.raises(RuntimeError):
        client._writes.restore("/foo.txt", "rev-old")

    assert tmp_paths
    for p in tmp_paths:
        assert not _os.path.exists(p)


# ==== modifiedTime on upload (Stage 5, echo suppression) ==================

def test_upload_create_sets_modified_time_from_local_mtime(client, cache):
    """Simple CREATE must send modifiedTime derived from the local file mtime."""
    import datetime as _dt

    data = b"hello world"
    local = _write_tempfile(data)
    # Pin a known local mtime so we can assert the exact RFC-3339 string.
    mtime = 1_700_000_000.0
    _os.utime(local, (mtime, mtime))
    expected = (
        _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": expected,
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Add)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    assert body["modifiedTime"] == expected
    _os.unlink(local)


def test_upload_update_sets_modified_time_from_local_mtime(client, cache):
    """Simple UPDATE must pass a body with modifiedTime alongside media_body."""
    import datetime as _dt

    cache.set("/hello.txt", "F1",
              parent_id="ROOT", name="hello.txt", is_folder=False)
    data = b"new"
    local = _write_tempfile(data)
    mtime = 1_700_000_500.0
    _os.utime(local, (mtime, mtime))
    expected = (
        _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": expected,
        "size": str(len(data)),
        "headRevisionId": "rev2",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Update)

    kwargs = svc.files.return_value.update.call_args.kwargs
    assert kwargs["fileId"] == "F1"
    assert kwargs["body"] == {"modifiedTime": expected}
    # media_body + fields must still be present.
    assert "media_body" in kwargs
    assert "fields" in kwargs
    _os.unlink(local)


# ===========================================================================
# Task B4 — FakeDrive integration tests
# These tests use client_on_fake (real GoogleDriveClient + in-memory FakeDrive)
# and assert state via fake.files[...] and via client.get_metadata.
# ===========================================================================

from tests.fake_drive import ROOT_ID, make_http_error as _make_http_error


# ---- upload via fake -------------------------------------------------------

def test_fake_upload_simple_creates_file_in_fake(client_on_fake, tmp_path):
    """Simple upload (<=5MB) round-trips through FakeDrive; fake.files shows
    the new file with correct md5 and size."""
    client = client_on_fake.client
    fake = client_on_fake.fake
    # Disable network-touching steps.
    client._ensure_root_id = Mock()

    data = b"hello from fake"
    local = tmp_path / "hello.txt"
    local.write_bytes(data)

    result = client._writes.upload(str(local), "/hello.txt", write_mode=WriteMode.Add)

    # The file was inserted into the fake's store.
    fid = result.id
    assert fid in fake.files
    rec = fake.files[fid]
    assert rec["name"] == "hello.txt"
    assert rec["md5Checksum"] == hashlib.md5(data).hexdigest()
    assert rec["size"] == len(data)
    assert rec["content"] == data
    assert rec["trashed"] is False

    # get_metadata agrees.
    # Seed the path cache so get_metadata can find the file.
    client._path_cache.set(
        "/hello.txt", fid, parent_id=ROOT_ID, name="hello.txt", is_folder=False
    )
    meta = client.get_metadata("/hello.txt")
    assert meta is not None
    assert meta.id == fid
    assert meta.content_hash == hashlib.md5(data).hexdigest()


def test_fake_upload_resumable_creates_file_in_fake(client_on_fake, tmp_path):
    """Resumable upload (>5MB) round-trips through FakeDrive._ResumableRequest."""
    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    # Write 5 MB + 1 byte to trigger the resumable path.
    size = 5 * 1024 * 1024 + 1
    data = b"x" * size
    local = tmp_path / "big.bin"
    local.write_bytes(data)

    result = client._writes.upload(str(local), "/big.bin", write_mode=WriteMode.Add)

    fid = result.id
    assert fid in fake.files
    rec = fake.files[fid]
    assert rec["md5Checksum"] == hashlib.md5(data).hexdigest()
    assert rec["size"] == size
    assert rec["trashed"] is False


# ---- move via fake --------------------------------------------------------

def test_fake_move_renames_file(client_on_fake):
    """move() renames a file: fake record gets new name, old path resolves to None."""
    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    fid = fake.add_file("/foo.txt", b"content")
    client._path_cache.set(
        "/foo.txt", fid, parent_id=ROOT_ID, name="foo.txt", is_folder=False
    )

    client._writes.move("/foo.txt", "/bar.txt")

    assert fake.files[fid]["name"] == "bar.txt"
    # Cache write-through.
    assert client._path_cache.path_to_id("/foo.txt") is None
    assert client._path_cache.path_to_id("/bar.txt") == fid


def test_fake_move_reparents_file(client_on_fake):
    """move() to a different parent updates the parents list in the fake record."""
    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    src_id = fake.add_folder("/src")
    dst_id = fake.add_folder("/dst")
    fid = fake.add_file("/src/foo.txt", b"data")

    client._path_cache.set("/src", src_id, parent_id=ROOT_ID, name="src", is_folder=True)
    client._path_cache.set("/dst", dst_id, parent_id=ROOT_ID, name="dst", is_folder=True)
    client._path_cache.set(
        "/src/foo.txt", fid, parent_id=src_id, name="foo.txt", is_folder=False
    )

    client._writes.move("/src/foo.txt", "/dst/foo.txt")

    rec = fake.files[fid]
    assert dst_id in rec["parents"]
    assert src_id not in rec["parents"]
    assert client._path_cache.path_to_id("/src/foo.txt") is None
    assert client._path_cache.path_to_id("/dst/foo.txt") == fid


# ---- remove via fake ------------------------------------------------------

def test_fake_remove_trashes_file(client_on_fake):
    """remove() sets trashed=True in the fake record and evicts the path from cache."""
    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    fid = fake.add_file("/foo.txt", b"hello")
    client._path_cache.set(
        "/foo.txt", fid, parent_id=ROOT_ID, name="foo.txt", is_folder=False
    )

    client._writes.remove("/foo.txt")

    assert fake.files[fid]["trashed"] is True
    assert client._path_cache.path_to_id("/foo.txt") is None


# ---- make_dir via fake ----------------------------------------------------

def test_fake_make_dir_creates_folder(client_on_fake):
    """make_dir() inserts a folder record in fake.files and updates the cache."""
    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    result = client._writes.make_dir("/newdir")

    fid = result.id
    assert fid in fake.files
    rec = fake.files[fid]
    assert rec["name"] == "newdir"
    from tests.fake_drive import FOLDER_MIME
    assert rec["mimeType"] == FOLDER_MIME
    assert rec["trashed"] is False
    # Cache write-through.
    assert client._path_cache.path_to_id("/newdir") == fid


# ---- make_dir_batch via fake ----------------------------------------------

def test_fake_make_dir_batch_creates_folders(client_on_fake):
    """make_dir_batch() via FakeDrive._Batch: each folder ends up in fake.files."""
    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    results = client._writes.make_dir_batch(["/alpha", "/beta", "/gamma"])

    assert len(results) == 3
    for r in results:
        assert not isinstance(r, CumulusApiError), f"unexpected error: {r}"
        assert r.id in fake.files
        assert fake.files[r.id]["trashed"] is False
    names = {fake.files[r.id]["name"] for r in results}
    assert names == {"alpha", "beta", "gamma"}


# ---- remove_batch via fake ------------------------------------------------

def test_fake_remove_batch_trashes_files(client_on_fake):
    """remove_batch() via FakeDrive._Batch: each file ends up with trashed=True."""
    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    fid_a = fake.add_file("/a.txt", b"aaa")
    fid_b = fake.add_file("/b.txt", b"bbb")
    client._path_cache.set("/a.txt", fid_a, parent_id=ROOT_ID, name="a.txt", is_folder=False)
    client._path_cache.set("/b.txt", fid_b, parent_id=ROOT_ID, name="b.txt", is_folder=False)

    results = client._writes.remove_batch([("/a.txt", None), ("/b.txt", None)])

    assert len(results) == 2
    for r in results:
        assert not isinstance(r, CumulusApiError), f"unexpected error: {r}"
    assert fake.files[fid_a]["trashed"] is True
    assert fake.files[fid_b]["trashed"] is True
    assert client._path_cache.path_to_id("/a.txt") is None
    assert client._path_cache.path_to_id("/b.txt") is None


# ---- restore via fake -----------------------------------------------------

def test_fake_restore_round_trips_content(client_on_fake, tmp_path):
    """restore() downloads the revision content then re-uploads it.

    The FakeDrive revisions.get_media returns the file's current content, so
    after restore the fake record should still have the same bytes (the content
    written by the initial upload), and the cache/metadata should be consistent.
    """
    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    # Seed a file with known content + a revision.
    data = b"original content for restore"
    fid = fake.add_file("/doc.txt", data)
    rev_id = fake.files[fid]["headRevisionId"]
    client._path_cache.set(
        "/doc.txt", fid, parent_id=ROOT_ID, name="doc.txt", is_folder=False
    )

    result = client._writes.restore("/doc.txt", rev_id)

    # After restore, the file should still be present and have the same content.
    assert result.id == fid
    rec = fake.files[fid]
    assert rec["content"] == data
    assert rec["trashed"] is False


# ---- error paths: quota and 412 via fake ----------------------------------

def test_fake_upload_quota_exceeded_raises_insufficient_space(client_on_fake, tmp_path):
    """Injecting a 403/storageQuotaExceeded via fail_next on 'files.create'
    causes upload() to raise InsufficientSpaceError (not the raw HttpError).

    Confirmed exception class: cumulus.exceptions.InsufficientSpaceError
    """
    from cumulus.exceptions import InsufficientSpaceError

    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    data = b"small file"
    local = tmp_path / "small.txt"
    local.write_bytes(data)

    fake.fail_next("files.create", _make_http_error(403, "storageQuotaExceeded"))

    with pytest.raises(InsufficientSpaceError):
        client._writes.upload(str(local), "/small.txt", write_mode=WriteMode.Add)

    # The cache must not have been updated (upload was aborted before commit).
    assert client._path_cache.path_to_id("/small.txt") is None


def test_fake_upload_resumable_quota_exceeded(client_on_fake, tmp_path):
    """Injecting storageQuotaExceeded into the resumable path (files.create
    key is used by _ResumableRequest) raises InsufficientSpaceError.

    Confirmed exception class: cumulus.exceptions.InsufficientSpaceError
    """
    from cumulus.exceptions import InsufficientSpaceError

    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    size = 5 * 1024 * 1024 + 1
    local = tmp_path / "big.bin"
    local.write_bytes(b"b" * size)

    fake.fail_next("files.create", _make_http_error(403, "storageQuotaExceeded"))

    with pytest.raises(InsufficientSpaceError):
        client._writes.upload(str(local), "/big.bin", write_mode=WriteMode.Add)

    assert client._path_cache.path_to_id("/big.bin") is None


def test_fake_remove_412_condition_not_met_raises_data_changed(client_on_fake):
    """Injecting a 412/conditionNotMet via fail_next on 'files.update'
    causes remove() to raise DataChangedError.

    Note: remove() without parent_rev does not check preconditions beforehand;
    the 412 from the update call itself is converted by convert_api_errors.

    Confirmed exception class: cumulus.exceptions.DataChangedError
    """
    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    fid = fake.add_file("/doc.txt", b"content")
    client._path_cache.set(
        "/doc.txt", fid, parent_id=ROOT_ID, name="doc.txt", is_folder=False
    )

    fake.fail_next("files.update", _make_http_error(412, "conditionNotMet"))

    with pytest.raises(DataChangedError):
        client._writes.remove("/doc.txt")

    # File should still be present (operation was aborted).
    assert fake.files[fid]["trashed"] is False


def test_fake_move_412_condition_not_met_raises_data_changed(client_on_fake):
    """Injecting a 412/conditionNotMet on move's files.update raises
    DataChangedError.

    Confirmed exception class: cumulus.exceptions.DataChangedError
    """
    client = client_on_fake.client
    fake = client_on_fake.fake
    client._ensure_root_id = Mock()

    fid = fake.add_file("/src.txt", b"data")
    client._path_cache.set(
        "/src.txt", fid, parent_id=ROOT_ID, name="src.txt", is_folder=False
    )

    fake.fail_next("files.update", _make_http_error(412, "conditionNotMet"))

    with pytest.raises(DataChangedError):
        client._writes.move("/src.txt", "/dst.txt")

    # Cache unchanged — move was aborted.
    assert client._path_cache.path_to_id("/src.txt") == fid
