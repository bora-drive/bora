"""OAuth2 helpers for Google Drive auth.

This module owns: client_secret.json loading, Flow construction, the
Credentials.to_json() round-trip, and a PersistingCredentials wrapper
that writes refreshed tokens back to the keyring after every refresh.

No daemon state is held here — state lives on GoogleDriveClient instances.
"""

from __future__ import annotations

import json
import os
from importlib.resources import files
from typing import Any

from google.oauth2.credentials import Credentials

from .exceptions import AuthError
from .keyring import CredentialStorage

__all__ = [
    "DRIVE_SCOPES",
    "load_client_config",
    "credentials_from_refresh_token",
    "save_credentials",
    "load_credentials",
    "PersistingCredentials",
    "wrap_persisting",
]

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def load_client_config() -> dict[str, Any]:
    """Resolve client_secret.json from CUMULUS_CLIENT_SECRET_PATH (if set)
    or the bundled package resource. Raises AuthError on missing/malformed.
    """
    override = os.environ.get("CUMULUS_CLIENT_SECRET_PATH")
    try:
        if override:
            with open(override, encoding="utf-8") as f:
                return json.load(f)
        return json.loads(
            files("cumulus.resources").joinpath("client_secret.json").read_text()
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise AuthError(
            "Client credentials not found",
            f"Expected client_secret.json at: {override or '<bundled resource>'}. "
            "This is a Cumulus packaging defect — please file a bug.",
        ) from exc
    except json.JSONDecodeError as exc:
        raise AuthError(
            "Client credentials are malformed",
            f"Could not parse client_secret.json: {exc}",
        ) from exc
    except OSError as exc:
        raise AuthError(
            "Could not read client credentials",
            f"Could not read client_secret.json: {exc}",
        ) from exc


def credentials_from_refresh_token(refresh_token: str) -> Credentials:
    """Build a Credentials seeded only with a refresh_token plus the bundled
    client_id/secret. The returned Credentials has token=None; the caller
    should call .refresh(Request()) to obtain an access token (and to
    validate that the refresh token is still valid)."""
    cfg = load_client_config()["installed"]
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=cfg["token_uri"],
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        scopes=list(DRIVE_SCOPES),
    )


def save_credentials(
    cred_storage: CredentialStorage,
    creds: Credentials,
    account_id: str,
) -> None:
    """Serialize Credentials.to_json() and persist via cred_storage."""
    cred_storage.save_creds(account_id, creds.to_json())


def load_credentials(cred_storage: CredentialStorage) -> Credentials | None:
    """Read the keyring blob and deserialize.
    Returns None if no blob is stored or if the blob is malformed.
    Returns a plain Credentials; pass through wrap_persisting() to upgrade
    to a PersistingCredentials before handing to googleapiclient.
    """
    blob = cred_storage.token
    if not blob:
        return None
    try:
        info = json.loads(blob)
        return Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))
    except (ValueError, json.JSONDecodeError):
        return None


class PersistingCredentials(Credentials):
    """Credentials subclass whose refresh() also persists to the keyring.

    googleapiclient calls .refresh() automatically on 401; subclassing it
    gives us a single hook to write the refreshed token back without
    every API call site needing a wrapper.
    """

    def __init__(
        self,
        *args,
        cred_storage: CredentialStorage | None = None,
        account_id: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._cred_storage = cred_storage
        self._account_id = account_id

    def refresh(self, request) -> None:  # type: ignore[no-untyped-def]
        super().refresh(request)
        # Parent either mutated self.token/self.expiry or raised RefreshError
        # (which we let propagate). On success, persist.
        if self._cred_storage is not None and self._account_id is not None:
            save_credentials(self._cred_storage, self, self._account_id)


def wrap_persisting(
    creds: Credentials,
    cred_storage: CredentialStorage,
    account_id: str,
) -> PersistingCredentials:
    """Upgrade a plain Credentials into a PersistingCredentials bound to a
    specific cred_storage and account_id. Field-for-field copy."""
    return PersistingCredentials(
        token=creds.token,
        refresh_token=creds.refresh_token,
        token_uri=creds.token_uri,
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        scopes=creds.scopes,
        expiry=creds.expiry,
        cred_storage=cred_storage,
        account_id=account_id,
    )
