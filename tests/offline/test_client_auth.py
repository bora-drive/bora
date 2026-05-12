from __future__ import annotations

import json
from unittest import mock

import pytest

from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.keyring import CredentialStorage


@pytest.fixture
def cred_storage():
    storage = CredentialStorage("test-config-client")
    yield storage
    storage.delete_creds()
    remove_configuration("test-config-client")


@pytest.fixture
def client(cred_storage):
    yield GoogleDriveClient("test-config-client", cred_storage)


def test_linked_is_false_when_keyring_empty(client):
    assert client.linked is False


def test_linked_is_false_when_blob_lacks_refresh_token(client, cred_storage):
    cred_storage.save_creds("perm-x", json.dumps({
        "token": "access-only",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    }))
    assert client.linked is False


def test_linked_caches_loaded_credentials(client, cred_storage, monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    from cumulus import oauth
    base = oauth.credentials_from_refresh_token("rt-1")
    oauth.save_credentials(cred_storage, base, account_id="perm-1")

    # First access loads.
    assert client.linked is True
    # Hot-path: don't re-read keyring.
    with mock.patch.object(oauth, "load_credentials") as m_load:
        _ = client.linked
        m_load.assert_not_called()
