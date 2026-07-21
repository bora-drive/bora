#!/usr/bin/env python3
"""Mint a Google Drive refresh token for the bora linked-test account.

Headless-friendly paste flow (Shape C): prints an authorization URL, you
approve it in a browser signed into the DEDICATED TEST ACCOUNT, then paste
back the localhost redirect URL (which will fail to load — that's expected).
The script exchanges the code, prints the refresh token, and confirms which
account it actually linked so you don't accidentally mint against the smoke
account.

Run from the repo root:

    python scripts/mint_test_token.py

Then:

    export BORA_TEST_REFRESH_TOKEN='<the printed value>'
    python -m pytest tests/linked -q

The bundled OAuth client's consent screen is published to "In production"
(unverified as of 2026-07-21), so the minted token no longer expires on a
7-day cycle. Re-run this script if the account changes, the token is revoked,
or the client is rotated (see docs/credential-rotation.md) — not on a routine
schedule.
"""

import os
import sys
from urllib.parse import parse_qs, urlparse

# The http loopback redirect_uri makes oauthlib refuse the exchange unless we
# explicitly allow insecure (http) transport. Same flag the daemon sets.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

from bora import oauth  # noqa: E402


def parse_code(value: str) -> str:
    """Accept the full pasted redirect URL or a bare code; return the code."""
    value = value.strip()
    if value.startswith(("http://", "https://")):
        codes = parse_qs(urlparse(value).query).get("code", [])
        return codes[0] if codes else ""
    return value


def main() -> int:
    flow = oauth.make_paste_flow()
    url, _state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="false"
    )

    print("\n1) Open this URL in a browser signed into the DEDICATED TEST")
    print("   account (bora-test@arko.net):\n")
    print(f"   {url}\n")
    print("2) Approve the Google Drive access request.")
    print("3) Your browser will then try to load a 'localhost:8080?code=...'")
    print("   URL and FAIL to connect — that is expected. Copy that URL from")
    print("   the address bar.\n")

    raw = input("Paste the redirected URL (or just the code): ")
    code = parse_code(raw)
    if not code:
        print("\nERROR: no authorization code found in that input.", file=sys.stderr)
        return 1

    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 - surface the real reason
        print(f"\nERROR: token exchange failed: {exc}", file=sys.stderr)
        return 1

    creds = flow.credentials
    refresh_token = getattr(creds, "refresh_token", None)
    if not refresh_token:
        print(
            "\nERROR: no refresh token returned. Re-run and make sure you "
            "approve the consent screen (the script already requests "
            "access_type=offline + prompt=consent).",
            file=sys.stderr,
        )
        return 1

    # Confirm which account we actually linked, so a wrong-account mint is
    # caught before it's exported.
    email = "<unknown>"
    try:
        from googleapiclient.discovery import build

        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        about = service.about().get(fields="user(emailAddress)").execute()
        email = about.get("user", {}).get("emailAddress", "<unknown>")
    except Exception as exc:  # noqa: BLE001
        print(f"\n(warning: could not confirm account email: {exc})", file=sys.stderr)

    print("\n" + "=" * 70)
    print(f"Linked account: {email}")
    if email not in ("bora-test@arko.net", "<unknown>"):
        print("\n!! WARNING: this is not the dedicated test account")
        print("   (bora-test@arko.net). Do NOT use this token for the")
        print("   linked test suite.")
    print("\nRefresh token (export this):\n")
    print(f"  export BORA_TEST_REFRESH_TOKEN='{refresh_token}'")
    print("=" * 70 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
