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


from cumulus.exceptions import NotLinkedError


def test_get_service_raises_not_linked_when_unlinked(client):
    with pytest.raises(NotLinkedError):
        client._get_service()


def test_get_service_builds_drive_v3_with_credentials(
    client, cred_storage, monkeypatch
):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    from cumulus import oauth
    base = oauth.credentials_from_refresh_token("rt-1")
    oauth.save_credentials(cred_storage, base, account_id="perm-1")

    fake_service = mock.Mock(name="drive_v3_service")
    with mock.patch("cumulus.client.build", return_value=fake_service) as m_build:
        result = client._get_service()
    assert result is fake_service
    m_build.assert_called_once()
    assert m_build.call_args.args[:2] == ("drive", "v3")
    assert m_build.call_args.kwargs["credentials"] is client._credentials
    assert m_build.call_args.kwargs["cache_discovery"] is False


from cumulus import oauth as _oauth
from cumulus.core import FullAccount, PersonalSpaceUsage


def _fake_about_response():
    return {
        "user": {
            "displayName": "Alice Example",
            "emailAddress": "alice@example.com",
            "permissionId": "perm-12345",
            "photoLink": "https://lh3.example.com/photo.jpg",
        },
        "storageQuota": {
            "usage": "1024000",
            "limit": "16106127360",
        },
    }


def _link_to_perm_x(client, cred_storage, monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    base = _oauth.credentials_from_refresh_token("rt-1")
    _oauth.save_credentials(cred_storage, base, account_id="perm-12345")


def test_fetch_account_info_for_creds_returns_full_account(
    client, cred_storage, monkeypatch
):
    _link_to_perm_x(client, cred_storage, monkeypatch)
    # Trigger lazy load so client._credentials is populated.
    assert client.linked is True
    fake_service = mock.MagicMock()
    fake_service.about().get().execute.return_value = _fake_about_response()
    with mock.patch("cumulus.client.build", return_value=fake_service):
        account = client._fetch_account_info_for_creds(client._credentials)

    assert isinstance(account, FullAccount)
    assert account.account_id == "perm-12345"
    assert account.email == "alice@example.com"
    assert account.display_name == "Alice Example"
    assert account.profile_photo_url == "https://lh3.example.com/photo.jpg"
    assert account.disabled is False


def test_get_account_info_caches(client, cred_storage, monkeypatch):
    _link_to_perm_x(client, cred_storage, monkeypatch)
    fake_service = mock.MagicMock()
    fake_service.about().get().execute.return_value = _fake_about_response()

    with mock.patch("cumulus.client.build", return_value=fake_service):
        a1 = client.get_account_info()
        a2 = client.get_account_info()

    # Second call must NOT issue another about().get().execute().
    assert a1 is a2


def test_get_space_usage(client, cred_storage, monkeypatch):
    _link_to_perm_x(client, cred_storage, monkeypatch)
    fake_service = mock.MagicMock()
    fake_service.about().get().execute.return_value = _fake_about_response()

    with mock.patch("cumulus.client.build", return_value=fake_service):
        usage = client.get_space_usage()

    assert isinstance(usage, PersonalSpaceUsage)
    assert usage.used == 1024000
    assert usage.allocated == 16106127360
    assert usage.team_usage is None


from google.auth.exceptions import RefreshError


def test_link_with_refresh_token_happy_path(client, cred_storage, monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)

    # Mock Credentials.refresh to succeed without HTTP.
    def _mark_refreshed(self, request):
        self.token = "fresh-access-token"
    monkeypatch.setattr(_oauth.Credentials, "refresh", _mark_refreshed)

    fake_service = mock.MagicMock()
    fake_service.about().get().execute.return_value = _fake_about_response()
    with mock.patch("cumulus.client.build", return_value=fake_service):
        res = client.link(refresh_token="rt-abc")

    assert res == 0
    assert client.linked is True
    assert client._cached_account_info is not None
    assert client._cached_account_info.account_id == "perm-12345"
    # account_id stored in config; refresh_token persisted in keyring.
    assert cred_storage.account_id == "perm-12345"
    blob = json.loads(cred_storage.token)
    assert blob["refresh_token"] == "rt-abc"


def test_link_with_refresh_token_rejects_revoked_token(
    client, cred_storage, monkeypatch
):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    def _raise(self, request):
        raise RefreshError("invalid_grant")
    monkeypatch.setattr(_oauth.Credentials, "refresh", _raise)

    res = client.link(refresh_token="rt-bad")

    assert res == 1
    assert client.linked is False
    assert cred_storage.account_id is None


def test_link_with_refresh_token_returns_2_on_connection_error(
    client, cred_storage, monkeypatch
):
    import requests as _req
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    def _raise(self, request):
        raise _req.exceptions.ConnectionError("boom")
    monkeypatch.setattr(_oauth.Credentials, "refresh", _raise)

    res = client.link(refresh_token="rt-no-net")
    assert res == 2
    assert client.linked is False


def test_link_with_no_arguments_returns_1(client):
    assert client.link() == 1


def test_link_with_access_token_happy_path(client, cred_storage, monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    fake_service = mock.MagicMock()
    fake_service.about().get().execute.return_value = _fake_about_response()
    with mock.patch("cumulus.client.build", return_value=fake_service):
        res = client.link(access_token="at-abc")

    assert res == 0
    assert client._cached_account_info is not None
    # Note: access_token has no refresh_token, so linked() returns False on
    # reload. Verify the in-process state was set correctly though.
    assert client._cached_account_info.account_id == "perm-12345"


from datetime import datetime, timedelta


def test_get_auth_url_creates_pending_flow(client, monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    url = client.get_auth_url()
    assert url.startswith("https://accounts.google.com/o/oauth2/")
    assert client._pending_paste_flow is not None
    assert "access_type=offline" in url
    assert "prompt=consent" in url


def test_link_with_code_uses_pending_flow(client, cred_storage, monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    client.get_auth_url()
    fake_flow = client._pending_paste_flow.flow
    fake_flow.fetch_token = mock.Mock()  # short-circuit token exchange
    fake_creds = _oauth.credentials_from_refresh_token("rt-from-paste")
    fake_creds.token = "fresh-access-from-paste"
    # Flow.credentials is a read-only property; use PropertyMock to stub it.
    with mock.patch.object(type(fake_flow), "credentials",
                           new_callable=mock.PropertyMock,
                           return_value=fake_creds):
        fake_service = mock.MagicMock()
        fake_service.about().get().execute.return_value = _fake_about_response()
        with mock.patch("cumulus.client.build", return_value=fake_service):
            res = client.link(code="auth-code-from-redirect")

    assert res == 0
    fake_flow.fetch_token.assert_called_once_with(code="auth-code-from-redirect")
    assert client._pending_paste_flow is None
    assert client.linked is True


def test_link_with_code_returns_1_when_no_pending_flow(client):
    assert client._pending_paste_flow is None
    res = client.link(code="any-code")
    assert res == 1


def test_link_with_code_returns_1_on_expired_pending_flow(client, monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    client.get_auth_url()
    # Force the state to look stale.
    client._pending_paste_flow.created_at = datetime.utcnow() - timedelta(minutes=11)
    res = client.link(code="too-late")
    assert res == 1
    assert client._pending_paste_flow is None


def test_get_auth_url_replaces_prior_pending_flow(client, monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    client.get_auth_url()
    first = client._pending_paste_flow
    client.get_auth_url()
    assert client._pending_paste_flow is not first


def test_unlink_clears_credentials_and_revokes(client, cred_storage, monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    base = _oauth.credentials_from_refresh_token("rt-to-revoke")
    _oauth.save_credentials(cred_storage, base, account_id="perm-1")
    # Force the client to populate _credentials.
    assert client.linked is True

    with mock.patch("cumulus.client.requests.post") as m_post:
        client.unlink()

    assert client._credentials is None
    assert client._cached_account_info is None
    m_post.assert_called_once()
    args, kwargs = m_post.call_args
    assert args[0] == "https://oauth2.googleapis.com/revoke"
    assert kwargs["params"] == {"token": "rt-to-revoke"}


def test_unlink_tolerates_revoke_failure(client, cred_storage, monkeypatch):
    monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
    base = _oauth.credentials_from_refresh_token("rt-to-revoke")
    _oauth.save_credentials(cred_storage, base, account_id="perm-1")
    assert client.linked is True

    import requests as _req
    with mock.patch(
        "cumulus.client.requests.post",
        side_effect=_req.exceptions.ConnectionError("offline"),
    ):
        # Must not raise.
        client.unlink()

    assert client._credentials is None


def test_unlink_when_not_linked_is_a_noop(client):
    assert client.linked is False
    with mock.patch("cumulus.client.requests.post") as m_post:
        client.unlink()
    m_post.assert_not_called()
