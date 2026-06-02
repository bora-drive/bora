"""Offline unit tests for cumulus.writes._WriteHelper.

The Drive API is mocked at the client._get_service() boundary, same as
test_client.py. Each test injects a chained MagicMock service.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, Mock, patch

import pytest

from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.database.core import Database
from cumulus.exceptions import (
    BadInputError,
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
