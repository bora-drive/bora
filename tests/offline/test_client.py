"""Offline unit tests for cumulus.client.GoogleDriveClient.

Drive API calls are mocked at the `_get_service()` boundary: each test
builds a MagicMock service and assigns `client._get_service` to a Mock
returning it.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, Mock

import pytest

from cumulus.client import GoogleDriveClient
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
