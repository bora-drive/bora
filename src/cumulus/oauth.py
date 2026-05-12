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

from .exceptions import AuthError

__all__ = ["DRIVE_SCOPES", "load_client_config"]

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
