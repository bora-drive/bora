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

def test_ensure_root_id_resolves_and_caches(cache):
    cred = CredentialStorage(_CONFIG + "-root")
    cl = GoogleDriveClient(_CONFIG + "-root", cred, cache)
    cache.root_id = None  # force resolution
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "ROOT_XYZ"
    }
    cl._get_service = Mock(return_value=service)

    assert cl._ensure_root_id() == "ROOT_XYZ"
    assert cache.root_id == "ROOT_XYZ"
    # Second call must not hit the service again.
    assert cl._ensure_root_id() == "ROOT_XYZ"
    assert service.files.return_value.get.call_count == 1

    cred.delete_creds()
    remove_configuration(_CONFIG + "-root")


def test_ensure_root_id_short_circuits_when_set(client):
    # cache.root_id was pre-seeded to "ROOT" by the fixture.
    client._get_service = Mock(side_effect=AssertionError("should not be called"))
    assert client._ensure_root_id() == "ROOT"
