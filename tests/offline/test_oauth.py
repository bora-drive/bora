from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from bora import oauth
from bora.exceptions import AuthError


def _write_client_secret(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test-client.apps.googleusercontent.com",
                    "client_secret": "test-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost:8080"],
                }
            }
        )
    )


def test_load_client_config_uses_bundled_resource_by_default(monkeypatch):
    monkeypatch.delenv("BORA_CLIENT_SECRET_PATH", raising=False)
    cfg = oauth.load_client_config()
    assert "installed" in cfg
    assert "client_id" in cfg["installed"]


def test_load_client_config_env_override(monkeypatch, tmp_path):
    override = tmp_path / "alt_client_secret.json"
    _write_client_secret(override)
    monkeypatch.setenv("BORA_CLIENT_SECRET_PATH", str(override))

    cfg = oauth.load_client_config()
    assert cfg["installed"]["client_id"] == "test-client.apps.googleusercontent.com"


def test_load_client_config_missing_override_raises_auth_error(monkeypatch, tmp_path):
    missing = tmp_path / "nope.json"
    monkeypatch.setenv("BORA_CLIENT_SECRET_PATH", str(missing))

    with pytest.raises(AuthError):
        oauth.load_client_config()


def test_load_client_config_malformed_override_raises_auth_error(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("BORA_CLIENT_SECRET_PATH", str(bad))

    with pytest.raises(AuthError):
        oauth.load_client_config()


def test_load_client_config_missing_bundled_resource_raises_auth_error(monkeypatch):
    monkeypatch.delenv("BORA_CLIENT_SECRET_PATH", raising=False)

    def _raise(*_a, **_kw):
        raise ModuleNotFoundError("No module named 'bora.resources'")

    monkeypatch.setattr("bora.oauth.files", _raise)
    with pytest.raises(AuthError):
        oauth.load_client_config()


def test_credentials_from_refresh_token_uses_bundled_client(monkeypatch):
    monkeypatch.delenv("BORA_CLIENT_SECRET_PATH", raising=False)
    creds = oauth.credentials_from_refresh_token("test-refresh-token")
    assert creds.refresh_token == "test-refresh-token"
    assert creds.client_id  # comes from client_secret.json
    assert creds.client_secret
    assert creds.token_uri == "https://oauth2.googleapis.com/token"
    assert creds.scopes == oauth.DRIVE_SCOPES
    assert creds.token is None  # access token absent until first refresh


from bora.config import remove_configuration
from bora.keyring import CredentialStorage


@pytest.fixture
def cred_storage():
    storage = CredentialStorage("test-config-oauth")
    yield storage
    storage.delete_creds()
    remove_configuration("test-config-oauth")


def test_save_and_load_credentials_round_trip(monkeypatch, cred_storage):
    monkeypatch.delenv("BORA_CLIENT_SECRET_PATH", raising=False)
    original = oauth.credentials_from_refresh_token("rt-abc")

    oauth.save_credentials(cred_storage, original, account_id="perm-123")

    assert cred_storage.account_id == "perm-123"
    loaded = oauth.load_credentials(cred_storage)
    assert loaded is not None
    assert loaded.refresh_token == "rt-abc"
    assert loaded.scopes == oauth.DRIVE_SCOPES
    assert loaded.client_id == original.client_id


def test_load_credentials_returns_none_when_empty(cred_storage):
    assert oauth.load_credentials(cred_storage) is None


def test_load_credentials_returns_none_on_malformed_blob(cred_storage):
    cred_storage.save_creds("perm-x", "{not valid json")
    assert oauth.load_credentials(cred_storage) is None


def test_load_credentials_returns_none_on_wrong_shape(cred_storage):
    cred_storage.save_creds("perm-x", json.dumps({"something_else": 1}))
    assert oauth.load_credentials(cred_storage) is None


def _mark_refreshed(self, request):
    # Simulate google-auth's parent refresh() success path:
    # mutate self.token and self.expiry as a real refresh would.
    from datetime import datetime, timedelta

    self.token = "refreshed-access-token"
    self.expiry = datetime.utcnow() + timedelta(hours=1)


def test_persisting_credentials_refresh_persists(monkeypatch, cred_storage):
    monkeypatch.delenv("BORA_CLIENT_SECRET_PATH", raising=False)
    base = oauth.credentials_from_refresh_token("rt-abc")
    wrapped = oauth.wrap_persisting(base, cred_storage, account_id="perm-456")

    monkeypatch.setattr(Credentials, "refresh", _mark_refreshed)
    wrapped.refresh(request=mock.Mock())

    # Persisted blob carries the refreshed access token.
    assert cred_storage.account_id == "perm-456"
    reloaded = oauth.load_credentials(cred_storage)
    assert reloaded is not None
    assert reloaded.token == "refreshed-access-token"


def test_persisting_credentials_refresh_does_not_persist_on_error(
    monkeypatch, cred_storage
):
    monkeypatch.delenv("BORA_CLIENT_SECRET_PATH", raising=False)
    base = oauth.credentials_from_refresh_token("rt-abc")
    wrapped = oauth.wrap_persisting(base, cred_storage, account_id="perm-456")

    def _raise(self, request):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", _raise)

    with pytest.raises(RefreshError):
        wrapped.refresh(request=mock.Mock())

    # Keyring untouched.
    assert cred_storage.account_id is None
    assert cred_storage.token is None


def test_load_credentials_returned_blob_can_be_wrapped(monkeypatch, cred_storage):
    monkeypatch.delenv("BORA_CLIENT_SECRET_PATH", raising=False)
    base = oauth.credentials_from_refresh_token("rt-abc")
    oauth.save_credentials(cred_storage, base, account_id="perm-789")
    loaded = oauth.load_credentials(cred_storage)
    assert loaded is not None
    wrapped = oauth.wrap_persisting(loaded, cred_storage, account_id="perm-789")
    assert isinstance(wrapped, oauth.PersistingCredentials)
    assert wrapped.refresh_token == "rt-abc"


def test_make_paste_flow_authorization_url_shape(monkeypatch):
    monkeypatch.delenv("BORA_CLIENT_SECRET_PATH", raising=False)
    flow = oauth.make_paste_flow()

    auth_url, _state = flow.authorization_url(access_type="offline", prompt="consent")

    assert auth_url.startswith("https://accounts.google.com/o/oauth2/")
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8080" in auth_url
    assert "access_type=offline" in auth_url
    assert "prompt=consent" in auth_url
    # auth/drive scope encoded into the URL.
    assert "drive" in auth_url


def test_run_local_flow_returns_credentials(monkeypatch):
    monkeypatch.delenv("BORA_CLIENT_SECRET_PATH", raising=False)
    fake_creds = mock.Mock(spec=Credentials)
    fake_flow = mock.Mock()
    fake_flow.run_local_server = mock.Mock(return_value=fake_creds)

    with mock.patch(
        "bora.oauth.InstalledAppFlow.from_client_config",
        return_value=fake_flow,
    ):
        result = oauth.run_local_flow(port=8080)

    assert result is fake_creds
    fake_flow.run_local_server.assert_called_once()
    kwargs = fake_flow.run_local_server.call_args.kwargs
    assert kwargs.get("port") == 8080


def test_run_local_flow_port_in_use_raises_auth_error(monkeypatch):
    monkeypatch.delenv("BORA_CLIENT_SECRET_PATH", raising=False)
    fake_flow = mock.Mock()
    fake_flow.run_local_server = mock.Mock(
        side_effect=OSError(98, "Address already in use")
    )

    with mock.patch(
        "bora.oauth.InstalledAppFlow.from_client_config",
        return_value=fake_flow,
    ):
        with pytest.raises(AuthError) as excinfo:
            oauth.run_local_flow(port=8080)
    assert "8080" in str(excinfo.value)
    assert "--no-local-server" in str(excinfo.value)
