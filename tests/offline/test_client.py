"""Offline unit tests for cumulus.client.GoogleDriveClient.

Drive API calls are mocked at the `_get_service()` boundary: each test
builds a MagicMock service and assigns `client._get_service` to a Mock
returning it.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, Mock

import pytest

from cumulus.client import GoogleDriveClient, _MAX_RETRY_ATTEMPTS
from cumulus.config import remove_configuration
from cumulus.database.core import Database
from cumulus.keyring import CredentialStorage
from cumulus.path_cache import PathIDCache

_CONFIG = "test-config-client-offline"


@pytest.fixture
def cache() -> PathIDCache:
    """Fresh in-memory path cache with the sync root pre-seeded."""
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = "ROOT"
    return c


@pytest.fixture
def client(cache: PathIDCache):
    cred = CredentialStorage(_CONFIG)
    cl = GoogleDriveClient(_CONFIG, cred, cache)
    yield cl
    cred.delete_creds()
    remove_configuration(_CONFIG)


def fake_service() -> MagicMock:
    """A MagicMock standing in for a built Drive v3 service."""
    return MagicMock(name="drive-service")


# ==== constructor =========================================================

def test_constructor_stores_path_cache(client, cache):
    assert client._path_cache is cache


def test_close_sets_poll_event(client):
    assert client._poll_event.is_set() is False
    client.close()
    assert client._poll_event.is_set() is True


# ==== _retry_transient ====================================================

def test_retry_returns_on_first_success(client):
    call = Mock(return_value="ok")
    assert client._retry_transient(call) == "ok"
    assert call.call_count == 1


def test_retry_retries_then_succeeds(client, make_http_error):
    client._poll_event = Mock()
    client._poll_event.wait.return_value = False  # no shutdown
    call = Mock(side_effect=[make_http_error(503), make_http_error(503), "ok"])
    assert client._retry_transient(call) == "ok"
    assert call.call_count == 3


def test_retry_does_not_retry_non_retryable(client, make_http_error):
    # A 404 is not transient — it must propagate unchanged (the caller's
    # convert_api_errors maps it later).
    from googleapiclient.errors import HttpError

    call = Mock(side_effect=make_http_error(404))
    with pytest.raises(HttpError):
        client._retry_transient(call)
    assert call.call_count == 1


def test_retry_exhausts_and_raises_last(client, make_http_error):
    from googleapiclient.errors import HttpError

    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    call = Mock(side_effect=[make_http_error(500)] * 10)
    with pytest.raises(HttpError):
        client._retry_transient(call)
    assert call.call_count == _MAX_RETRY_ATTEMPTS


def test_retry_retries_rate_limited_403(client, make_http_error):
    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    call = Mock(side_effect=[make_http_error(403, "rateLimitExceeded"), "ok"])
    assert client._retry_transient(call) == "ok"
    assert call.call_count == 2


def test_retry_does_not_retry_permission_403(client, make_http_error):
    from googleapiclient.errors import HttpError

    call = Mock(side_effect=make_http_error(403, "insufficientFilePermissions"))
    with pytest.raises(HttpError):
        client._retry_transient(call)
    assert call.call_count == 1


def test_retry_aborts_on_shutdown(client, make_http_error):
    from cumulus.exceptions import CancelledError

    client._poll_event = Mock()
    client._poll_event.wait.return_value = True  # shutdown during backoff
    call = Mock(side_effect=make_http_error(503))
    with pytest.raises(CancelledError):
        client._retry_transient(call)


def test_retry_retries_connection_error(client):
    import requests

    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    call = Mock(side_effect=[requests.exceptions.ConnectionError("net"), "ok"])
    assert client._retry_transient(call) == "ok"
    assert call.call_count == 2


# ==== _ensure_root_id =====================================================

def test_ensure_root_id_resolves_and_caches(client):
    client._path_cache.root_id = None  # force resolution
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "ROOT_XYZ"
    }
    client._get_service = Mock(return_value=service)

    assert client._ensure_root_id() == "ROOT_XYZ"
    assert client._path_cache.root_id == "ROOT_XYZ"
    # Second call must not hit the service again.
    assert client._ensure_root_id() == "ROOT_XYZ"
    assert service.files.return_value.get.call_count == 1


def test_ensure_root_id_short_circuits_when_set(client):
    # cache.root_id was pre-seeded to "ROOT" by the fixture.
    client._get_service = Mock(side_effect=AssertionError("should not be called"))
    assert client._ensure_root_id() == "ROOT"


# ==== get_metadata ========================================================

def _seed(cache, path, file_id, name, parent="ROOT", is_folder=False):
    cache.set(path, file_id, parent, name, is_folder)


def test_get_metadata_returns_file_metadata(client, cache):
    from cumulus.core import FileMetadata

    _seed(cache, "/report.pdf", "F1", "report.pdf")
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "F1", "name": "report.pdf", "mimeType": "application/pdf",
        "parents": ["ROOT"], "modifiedTime": "2026-04-30T00:00:00Z",
        "size": "10", "md5Checksum": "h", "version": 2, "trashed": False,
    }
    client._get_service = Mock(return_value=service)

    meta = client.get_metadata("/report.pdf")
    assert isinstance(meta, FileMetadata)
    assert meta.id == "F1"


def test_get_metadata_returns_folder_metadata(client, cache):
    from cumulus.core import FolderMetadata

    _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "D1", "name": "Documents",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["ROOT"], "trashed": False,
    }
    client._get_service = Mock(return_value=service)

    meta = client.get_metadata("/Documents")
    assert isinstance(meta, FolderMetadata)


def test_get_metadata_returns_none_for_native_doc(client, cache):
    _seed(cache, "/Notes", "G1", "Notes")
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "G1", "name": "Notes",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["ROOT"], "trashed": False,
    }
    client._get_service = Mock(return_value=service)

    assert client.get_metadata("/Notes") is None


def test_get_metadata_raises_not_found_on_cache_miss(client):
    from cumulus.exceptions import NotFoundError

    client._get_service = Mock(side_effect=AssertionError("should not call API"))
    with pytest.raises(NotFoundError):
        client.get_metadata("/no-such-file")


def test_get_metadata_trashed_returns_none(client, cache):
    _seed(cache, "/old.pdf", "F9", "old.pdf")
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "F9", "name": "old.pdf", "mimeType": "application/pdf",
        "parents": ["ROOT"], "trashed": True,
    }
    client._get_service = Mock(return_value=service)

    assert client.get_metadata("/old.pdf", include_deleted=False) is None


def test_get_metadata_trashed_with_include_deleted_returns_metadata(client, cache):
    from cumulus.core import FileMetadata

    _seed(cache, "/old.pdf", "F9", "old.pdf")
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "F9", "name": "old.pdf", "mimeType": "application/pdf",
        "parents": ["ROOT"], "trashed": True, "version": 1, "size": "5",
    }
    client._get_service = Mock(return_value=service)

    meta = client.get_metadata("/old.pdf", include_deleted=True)
    assert isinstance(meta, FileMetadata)
    assert meta.id == "F9"


# ==== list_revisions / restore ============================================

def test_list_revisions_returns_newest_first(client, cache):
    _seed(cache, "/report.pdf", "F1", "report.pdf")
    service = fake_service()
    service.revisions.return_value.list.return_value.execute.return_value = {
        "revisions": [
            {"id": "r1", "modifiedTime": "2026-01-01T00:00:00Z", "size": "10"},
            {"id": "r2", "modifiedTime": "2026-02-01T00:00:00Z", "size": "20"},
        ]
    }
    client._get_service = Mock(return_value=service)

    revs = client.list_revisions("/report.pdf")
    assert [r.rev for r in revs] == ["r2", "r1"]


def test_list_revisions_respects_limit(client, cache):
    _seed(cache, "/report.pdf", "F1", "report.pdf")
    service = fake_service()
    service.revisions.return_value.list.return_value.execute.return_value = {
        "revisions": [
            {"id": f"r{i}", "modifiedTime": "2026-01-01T00:00:00Z", "size": "1"}
            for i in range(20)
        ]
    }
    client._get_service = Mock(return_value=service)

    revs = client.list_revisions("/report.pdf", limit=5)
    assert len(revs) == 5


def test_list_revisions_paginates(client, cache):
    _seed(cache, "/report.pdf", "F1", "report.pdf")
    service = fake_service()
    service.revisions.return_value.list.return_value.execute.side_effect = [
        {"revisions": [{"id": "r1", "modifiedTime": "2026-01-01T00:00:00Z",
                        "size": "1"}], "nextPageToken": "p2"},
        {"revisions": [{"id": "r2", "modifiedTime": "2026-02-01T00:00:00Z",
                        "size": "1"}]},
    ]
    client._get_service = Mock(return_value=service)

    revs = client.list_revisions("/report.pdf")
    assert {r.rev for r in revs} == {"r1", "r2"}
    # The second call must forward the nextPageToken from page 1.
    calls = service.revisions.return_value.list.call_args_list
    assert calls[1].kwargs["pageToken"] == "p2"


def test_list_revisions_cache_miss_raises(client):
    from cumulus.exceptions import NotFoundError

    client._get_service = Mock(side_effect=AssertionError("no API call"))
    with pytest.raises(NotFoundError):
        client.list_revisions("/missing")


def test_restore_raises_not_implemented(client):
    with pytest.raises(NotImplementedError):
        client.restore("/report.pdf", "r1")
