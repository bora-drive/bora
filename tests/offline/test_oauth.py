from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cumulus import oauth
from cumulus.exceptions import AuthError


def _write_client_secret(path: Path) -> None:
    path.write_text(json.dumps({
        "installed": {
            "client_id": "test-client.apps.googleusercontent.com",
            "client_secret": "test-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost:8080"],
        }
    }))


def test_load_client_config_uses_bundled_resource_by_default(monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    cfg = oauth.load_client_config()
    assert "installed" in cfg
    assert "client_id" in cfg["installed"]


def test_load_client_config_env_override(monkeypatch, tmp_path):
    override = tmp_path / "alt_client_secret.json"
    _write_client_secret(override)
    monkeypatch.setenv("CUMULUS_CLIENT_SECRET_PATH", str(override))

    cfg = oauth.load_client_config()
    assert cfg["installed"]["client_id"] == "test-client.apps.googleusercontent.com"


def test_load_client_config_missing_override_raises_auth_error(monkeypatch, tmp_path):
    missing = tmp_path / "nope.json"
    monkeypatch.setenv("CUMULUS_CLIENT_SECRET_PATH", str(missing))

    with pytest.raises(AuthError):
        oauth.load_client_config()


def test_load_client_config_malformed_override_raises_auth_error(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("CUMULUS_CLIENT_SECRET_PATH", str(bad))

    with pytest.raises(AuthError):
        oauth.load_client_config()


def test_load_client_config_missing_bundled_resource_raises_auth_error(monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)

    def _raise(*_a, **_kw):
        raise ModuleNotFoundError("No module named 'cumulus.resources'")

    monkeypatch.setattr("cumulus.oauth.files", _raise)
    with pytest.raises(AuthError):
        oauth.load_client_config()


def test_credentials_from_refresh_token_uses_bundled_client(monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    creds = oauth.credentials_from_refresh_token("test-refresh-token")
    assert creds.refresh_token == "test-refresh-token"
    assert creds.client_id  # comes from client_secret.json
    assert creds.client_secret
    assert creds.token_uri == "https://oauth2.googleapis.com/token"
    assert creds.scopes == oauth.DRIVE_SCOPES
    assert creds.token is None  # access token absent until first refresh
