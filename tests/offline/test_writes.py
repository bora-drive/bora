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
    assert body["name"] == "foo (1)"
    assert cache.path_to_id("/foo (1)") == "F-new"
    assert result.name == "foo (1)"


def test_make_dir_autorename_multi_collision(client, cache):
    cache.set("/foo", "F1", parent_id="ROOT", name="foo", is_folder=True)
    cache.set("/foo (1)", "F2", parent_id="ROOT", name="foo (1)", is_folder=True)
    cache.set("/foo (2)", "F3", parent_id="ROOT", name="foo (2)", is_folder=True)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "foo (3)",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.make_dir("/foo", autorename=True)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    assert body["name"] == "foo (3)"


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
    cache.set("/foo", "F0", parent_id="ROOT", name="foo", is_folder=True)
    for n in range(1, 1000):
        cache.set(f"/foo ({n})", f"F{n}", parent_id="ROOT", name=f"foo ({n})", is_folder=True)
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
