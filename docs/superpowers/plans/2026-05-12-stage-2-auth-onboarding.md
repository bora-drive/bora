# Stage 2 — Auth & onboarding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the OAuth flow so a user can run `cumulus auth link`, the daemon stores Google credentials in the system keyring, every later stage's API call carries valid auth, and revoked grants surface as a re-link prompt instead of a stack trace.

**Architecture:** A new pure module `src/cumulus/oauth.py` holds Flow construction, the `Credentials.to_json()` round-trip, and a `PersistingCredentials` subclass that writes refreshed tokens back to the keyring on every refresh. `src/cumulus/client.py` replaces its `linked`/`get_auth_url`/`link`/`unlink` stubs to call into `oauth.py` and stores the loaded `Credentials` lazily on first access. The CLI process runs `InstalledAppFlow.run_local_server` itself (Shape A) and hands the resulting refresh token over Pyro5 to the daemon's `link(refresh_token=...)`; a `--no-local-server` paste-the-redirect-URL fallback covers headless-no-tunnel cases (Shape C). `convert_api_errors` maps `google.auth.exceptions.RefreshError` to the existing `TokenRevokedError`, which the manager's existing exception handler already turns into a stopped sync + user-visible notification.

**Tech Stack:** `google-auth>=2.23.0`, `google-auth-oauthlib>=1.1.0`, `google-api-python-client>=2.100.0`, `keyring`, `click`, `Pyro5` (all already in `pyproject.toml`); `pytest` + `unittest.mock` for tests (no new test deps).

**Reading order for the implementer:** the design spec is at `docs/superpowers/specs/2026-05-12-stage-2-auth-onboarding-design.md`. Read it before starting — every task below references a numbered section of that spec.

---

## Task 1: Bootstrap — Google Cloud project & bundled `client_secret.json`

**Files:**
- Create: `src/cumulus/resources/client_secret.json` (committed)
- Modify: `pyproject.toml` (package-data inclusion)

**This task is partly manual** — only a human with a Google account can create the GCP project. The agent should execute the code/config changes and pause for the human to drop in the JSON file before proceeding.

- [ ] **Step 1: Human creates the GCP project (procedure below)**

  Procedure for the human running this plan:
  1. Open https://console.cloud.google.com → "New Project" → name "Cumulus".
  2. Project dashboard → "APIs & Services" → "Library" → search "Google Drive API" → Enable.
  3. "APIs & Services" → "OAuth consent screen" → User Type = External → Create. Fill App name = "Cumulus", User support email = your email, Developer email = your email. Save. (Skip Scopes step for now; we'll claim `auth/drive` directly in code.) Add yourself as a Test User. Publishing status stays "Testing" (B2 verification is Stage 8).
  4. "APIs & Services" → "Credentials" → "Create Credentials" → "OAuth client ID" → Application type = **Desktop app** → Name = "Cumulus desktop client" → Create.
  5. Click the new client → "Add URI" under "Authorized redirect URIs" → add `http://localhost:8080`. Save.
  6. Download the JSON for that OAuth client → save as `src/cumulus/resources/client_secret.json` in the repo.

  The Desktop-app client type is what makes Google accept any `http://localhost:<port>` redirect URI without exact-match registration (this covers the Shape A `run_local_server(port=8080)` case if the port shifts in future, and any contributor's local development). The explicit `localhost:8080` registration is what Shape C (paste-the-redirect-URL) needs.

- [ ] **Step 2: Verify file shape**

  Run:
  ```bash
  python -c "import json; d = json.load(open('src/cumulus/resources/client_secret.json')); assert 'installed' in d; assert 'client_id' in d['installed']; assert 'client_secret' in d['installed']; print('ok')"
  ```
  Expected output: `ok`.

  If this fails, the downloaded file is from the wrong client type (Web vs Desktop). Re-download as Desktop app.

- [ ] **Step 3: Add to `pyproject.toml` package-data**

  In `pyproject.toml`, add this section if it doesn't already exist, or extend the existing `[tool.setuptools.package-data]` block:

  ```toml
  [tool.setuptools.package-data]
  cumulus = ["resources/*.json", "resources/*.png", "py.typed"]
  ```

  This ensures `client_secret.json` ships in the installed wheel.

- [ ] **Step 4: Verify the file is reachable via `importlib.resources`**

  Run:
  ```bash
  python -c "from importlib.resources import files; p = files('cumulus.resources').joinpath('client_secret.json'); assert p.is_file(); print('ok')"
  ```
  Expected output: `ok`.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/resources/client_secret.json pyproject.toml
  git commit -m "feat(stage-2): bundle Google OAuth client_secret.json"
  ```

---

## Task 2: `oauth.py` — `load_client_config` resolution order

**Spec ref:** §4.2.

**Files:**
- Create: `src/cumulus/oauth.py`
- Test: `tests/offline/test_oauth.py`

- [ ] **Step 1: Write the failing tests**

  Create `tests/offline/test_oauth.py`:

  ```python
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
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py -v
  ```
  Expected: `ImportError: cannot import name 'oauth' from 'cumulus'` or similar.

- [ ] **Step 3: Create `src/cumulus/oauth.py` with `load_client_config`**

  ```python
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
              with open(override) as f:
                  return json.load(f)
          return json.loads(
              files("cumulus.resources").joinpath("client_secret.json").read_text()
          )
      except FileNotFoundError as exc:
          raise AuthError(
              "Client credentials not found",
              f"Expected client_secret.json at: {override or '<bundled resource>'}. "
              "This is a Cumulus packaging defect — please file a bug.",
          ) from exc
      except (json.JSONDecodeError, OSError) as exc:
          raise AuthError(
              "Client credentials are malformed",
              f"Could not parse client_secret.json: {exc}",
          ) from exc
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py -v
  ```
  Expected: 4 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/oauth.py tests/offline/test_oauth.py
  git commit -m "feat(stage-2/oauth): load_client_config with env-var override"
  ```

---

## Task 3: `oauth.py` — `credentials_from_refresh_token`

**Spec ref:** §4.1 (public surface).

**Files:**
- Modify: `src/cumulus/oauth.py`
- Test: `tests/offline/test_oauth.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_oauth.py`:

  ```python
  def test_credentials_from_refresh_token_uses_bundled_client(monkeypatch):
      monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
      creds = oauth.credentials_from_refresh_token("test-refresh-token")
      assert creds.refresh_token == "test-refresh-token"
      assert creds.client_id  # comes from client_secret.json
      assert creds.client_secret
      assert creds.token_uri == "https://oauth2.googleapis.com/token"
      assert creds.scopes == oauth.DRIVE_SCOPES
      assert creds.token is None  # access token absent until first refresh
  ```

- [ ] **Step 2: Run test to verify it fails**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py::test_credentials_from_refresh_token_uses_bundled_client -v
  ```
  Expected: `AttributeError: module 'cumulus.oauth' has no attribute 'credentials_from_refresh_token'`.

- [ ] **Step 3: Add `credentials_from_refresh_token`**

  Append to `src/cumulus/oauth.py`:

  ```python
  from google.oauth2.credentials import Credentials


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
  ```

  Update `__all__`: `__all__ = ["DRIVE_SCOPES", "load_client_config", "credentials_from_refresh_token"]`.

- [ ] **Step 4: Run test to verify it passes**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py -v
  ```
  Expected: 5 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/oauth.py tests/offline/test_oauth.py
  git commit -m "feat(stage-2/oauth): credentials_from_refresh_token"
  ```

---

## Task 4: `oauth.py` — `save_credentials` / `load_credentials` round-trip

**Spec ref:** §4.4 storage format.

**Files:**
- Modify: `src/cumulus/oauth.py`
- Test: `tests/offline/test_oauth.py`

The tests use the real `CredentialStorage` (with cleanup) to exercise the keyring round-trip end-to-end. The pattern matches `tests/offline/test_keyring.py`.

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_oauth.py`:

  ```python
  from cumulus.config import remove_configuration
  from cumulus.keyring import CredentialStorage


  @pytest.fixture
  def cred_storage():
      storage = CredentialStorage("test-config-oauth")
      yield storage
      storage.delete_creds()
      remove_configuration("test-config-oauth")


  def test_save_and_load_credentials_round_trip(monkeypatch, cred_storage):
      monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
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
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py -v -k "round_trip or returns_none"
  ```
  Expected: `AttributeError: module 'cumulus.oauth' has no attribute 'save_credentials'`.

- [ ] **Step 3: Add `save_credentials` and `load_credentials`**

  Append to `src/cumulus/oauth.py`:

  ```python
  import json as _json

  from .keyring import CredentialStorage


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
          info = _json.loads(blob)
          return Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))
      except (ValueError, _json.JSONDecodeError):
          return None
  ```

  Update `__all__` accordingly.

  *(Note: `Credentials.from_authorized_user_info` raises `ValueError` for wrong-shape dicts; we catch both that and `JSONDecodeError`.)*

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py -v
  ```
  Expected: 9 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/oauth.py tests/offline/test_oauth.py
  git commit -m "feat(stage-2/oauth): save/load credentials round-trip via keyring"
  ```

---

## Task 5: `oauth.py` — `PersistingCredentials` + `wrap_persisting`

**Spec ref:** §4.3.

**Files:**
- Modify: `src/cumulus/oauth.py`
- Test: `tests/offline/test_oauth.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_oauth.py`:

  ```python
  from unittest import mock

  from google.auth.exceptions import RefreshError


  def _mark_refreshed(self, request):
      # Simulate google-auth's parent refresh() success path:
      # mutate self.token and self.expiry as a real refresh would.
      from datetime import datetime, timedelta
      self.token = "refreshed-access-token"
      self.expiry = datetime.utcnow() + timedelta(hours=1)


  def test_persisting_credentials_refresh_persists(monkeypatch, cred_storage):
      monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
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
      monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
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
      monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
      base = oauth.credentials_from_refresh_token("rt-abc")
      oauth.save_credentials(cred_storage, base, account_id="perm-789")
      loaded = oauth.load_credentials(cred_storage)
      assert loaded is not None
      wrapped = oauth.wrap_persisting(loaded, cred_storage, account_id="perm-789")
      assert isinstance(wrapped, oauth.PersistingCredentials)
      assert wrapped.refresh_token == "rt-abc"
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py -v -k "persisting or wrapped"
  ```
  Expected: `AttributeError: module 'cumulus.oauth' has no attribute 'wrap_persisting'`.

- [ ] **Step 3: Add `PersistingCredentials` and `wrap_persisting`**

  Append to `src/cumulus/oauth.py`:

  ```python
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
  ```

  Update `__all__` to include `PersistingCredentials` and `wrap_persisting`.

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py -v
  ```
  Expected: 12 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/oauth.py tests/offline/test_oauth.py
  git commit -m "feat(stage-2/oauth): PersistingCredentials writes back on refresh"
  ```

---

## Task 6: `oauth.py` — `make_paste_flow` for Shape C fallback

**Spec ref:** §5.2.

**Files:**
- Modify: `src/cumulus/oauth.py`
- Test: `tests/offline/test_oauth.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_oauth.py`:

  ```python
  def test_make_paste_flow_authorization_url_shape(monkeypatch):
      monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
      flow = oauth.make_paste_flow()

      auth_url, _state = flow.authorization_url(
          access_type="offline", prompt="consent"
      )

      assert auth_url.startswith("https://accounts.google.com/o/oauth2/")
      assert "redirect_uri=http%3A%2F%2Flocalhost%3A8080" in auth_url
      assert "access_type=offline" in auth_url
      assert "prompt=consent" in auth_url
      # auth/drive scope encoded into the URL.
      assert "drive" in auth_url
  ```

- [ ] **Step 2: Run test to verify it fails**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py::test_make_paste_flow_authorization_url_shape -v
  ```
  Expected: `AttributeError`.

- [ ] **Step 3: Add `make_paste_flow`**

  Append to `src/cumulus/oauth.py`:

  ```python
  from google_auth_oauthlib.flow import Flow

  PASTE_REDIRECT_URI = "http://localhost:8080"


  def make_paste_flow() -> Flow:
      """Construct a Flow with redirect_uri=http://localhost:8080 for the
      Shape C paste-the-redirect-URL fallback.

      The caller (GoogleDriveClient) holds the returned Flow object between
      get_auth_url() and link(code=...) — it carries PKCE state that the
      exchange needs."""
      flow = Flow.from_client_config(
          load_client_config(),
          scopes=list(DRIVE_SCOPES),
      )
      flow.redirect_uri = PASTE_REDIRECT_URI
      return flow
  ```

  Update `__all__`.

- [ ] **Step 4: Run test to verify it passes**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py -v
  ```
  Expected: 13 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/oauth.py tests/offline/test_oauth.py
  git commit -m "feat(stage-2/oauth): make_paste_flow for Shape C fallback"
  ```

---

## Task 7: `oauth.py` — `run_local_flow` for Shape A

**Spec ref:** §4.1 `run_local_flow`, §8 risk row "Port 8080 in use".

**Files:**
- Modify: `src/cumulus/oauth.py`
- Test: `tests/offline/test_oauth.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_oauth.py`:

  ```python
  def test_run_local_flow_returns_credentials(monkeypatch):
      monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
      fake_creds = mock.Mock(spec=Credentials)
      fake_flow = mock.Mock()
      fake_flow.run_local_server = mock.Mock(return_value=fake_creds)

      with mock.patch(
          "cumulus.oauth.InstalledAppFlow.from_client_config",
          return_value=fake_flow,
      ):
          result = oauth.run_local_flow(port=8080)

      assert result is fake_creds
      fake_flow.run_local_server.assert_called_once()
      kwargs = fake_flow.run_local_server.call_args.kwargs
      assert kwargs.get("port") == 8080


  def test_run_local_flow_port_in_use_raises_auth_error(monkeypatch):
      monkeypatch.delenv("CUMULUS_CLIENT_SECRET_PATH", raising=False)
      fake_flow = mock.Mock()
      fake_flow.run_local_server = mock.Mock(
          side_effect=OSError(98, "Address already in use")
      )

      with mock.patch(
          "cumulus.oauth.InstalledAppFlow.from_client_config",
          return_value=fake_flow,
      ):
          with pytest.raises(AuthError) as excinfo:
              oauth.run_local_flow(port=8080)
      assert "8080" in str(excinfo.value)
      assert "--no-local-server" in str(excinfo.value)
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py -v -k "run_local_flow"
  ```
  Expected: `AttributeError`.

- [ ] **Step 3: Add `run_local_flow`**

  Append to `src/cumulus/oauth.py`:

  ```python
  from google_auth_oauthlib.flow import InstalledAppFlow


  def run_local_flow(open_browser: bool = True, port: int = 8080) -> Credentials:
      """Used by the CLI process (not the daemon). Runs
      InstalledAppFlow.run_local_server with port=8080 by default —
      predictable for 'ssh -L 8080:localhost:8080 <host>'.

      Raises AuthError if the port is already bound; the user can free the
      port or fall back to `cumulus auth link --no-local-server`.
      """
      flow = InstalledAppFlow.from_client_config(
          load_client_config(),
          scopes=list(DRIVE_SCOPES),
      )
      try:
          return flow.run_local_server(
              port=port,
              open_browser=open_browser,
              access_type="offline",
              prompt="consent",
          )
      except OSError as exc:
          raise AuthError(
              f"Port {port} in use",
              f"Could not bind localhost:{port} for the OAuth callback ({exc}). "
              "Either free the port and retry, or run "
              "'cumulus auth link --no-local-server' to use the paste-the-URL flow.",
          ) from exc
  ```

  Update `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_oauth.py -v
  ```
  Expected: 15 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/oauth.py tests/offline/test_oauth.py
  git commit -m "feat(stage-2/oauth): run_local_flow wrapper with port-bound error mapping"
  ```

---

## Task 8: `errorhandling.py` — map `RefreshError` and `GoogleAuthError`

**Spec ref:** §6.3.

**Files:**
- Modify: `src/cumulus/errorhandling.py`
- Test: `tests/offline/test_errorhandling.py` (new or extend if exists)

- [ ] **Step 1: Check if `tests/offline/test_errorhandling.py` exists; create a base if not**

  Run:
  ```bash
  ls tests/offline/test_errorhandling.py 2>/dev/null || echo "missing"
  ```

  If missing, create the file with:

  ```python
  from __future__ import annotations

  import pytest
  ```

  (We won't pre-test the existing pre-existing collection errors; this stage only adds the new tests below.)

- [ ] **Step 2: Add the failing tests**

  Append to `tests/offline/test_errorhandling.py`:

  ```python
  from google.auth.exceptions import GoogleAuthError, RefreshError

  from cumulus.errorhandling import convert_api_errors
  from cumulus.exceptions import AuthError, TokenRevokedError


  def test_convert_api_errors_maps_refresh_error_to_token_revoked():
      with pytest.raises(TokenRevokedError) as excinfo:
          with convert_api_errors():
              raise RefreshError("invalid_grant")
      assert "revoked" in excinfo.value.title.lower()
      assert isinstance(excinfo.value, AuthError)


  def test_convert_api_errors_maps_other_googleautherror_to_authError():
      class _Other(GoogleAuthError):
          pass

      with pytest.raises(AuthError) as excinfo:
          with convert_api_errors():
              raise _Other("something else broke")

      # Should NOT be a TokenRevokedError specifically — only the catch-all.
      assert not isinstance(excinfo.value, TokenRevokedError)
  ```

- [ ] **Step 3: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_errorhandling.py -v
  ```
  Expected: the RefreshError leaks out, no mapping in place.

- [ ] **Step 4: Add the mapping branches**

  In `src/cumulus/errorhandling.py`, modify the `convert_api_errors` body. The function currently is:

  ```python
  @contextlib.contextmanager
  def convert_api_errors(
      remote_path: str | None = None, local_path: str | None = None
  ) -> Iterator[None]:
      try:
          yield
      # TODO: Phase 6 — catch googleapiclient.errors.HttpError and
      # google.auth.exceptions here and convert via google_api_to_cumulus_error.
      except CONNECTION_ERRORS:
          ...
      except OSError as exc:
          ...
  ```

  Insert two new branches **between** `try ... yield` and `except CONNECTION_ERRORS`. Order matters because `RefreshError` is a subclass of `GoogleAuthError` — the more specific branch must come first:

  ```python
      try:
          yield
      except google.auth.exceptions.RefreshError as exc:
          raise TokenRevokedError(
              "Authentication revoked",
              "Your Google account has been disconnected. "
              "Run 'cumulus auth link -r' to re-link.",
          ) from exc
      except google.auth.exceptions.GoogleAuthError as exc:
          raise AuthError("Authentication error", str(exc)) from exc
      except CONNECTION_ERRORS:
          ...
  ```

  Add at the top of `errorhandling.py`:

  ```python
  import google.auth.exceptions
  ```

  Also remove the outdated `# TODO: Phase 6 ...` comment that referenced `google.auth.exceptions`, since this stage now handles the auth-library subset (HttpError mapping is still Stage 3).

- [ ] **Step 5: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_errorhandling.py -v
  ```
  Expected: 2 tests pass.

- [ ] **Step 6: Commit**

  ```bash
  git add src/cumulus/errorhandling.py tests/offline/test_errorhandling.py
  git commit -m "feat(stage-2/errors): map RefreshError to TokenRevokedError"
  ```

---

## Task 9: `daemon.py` — set `OAUTHLIB_INSECURE_TRANSPORT=1`

**Spec ref:** §6.2.

**Files:**
- Modify: `src/cumulus/daemon.py`

- [ ] **Step 1: Add the env-var-set at module load time**

  In `src/cumulus/daemon.py`, immediately after the existing `import os` (around line 14) but **before** any other cumulus imports, add:

  ```python
  # Allow OAuth callbacks over plain HTTP for localhost (used by Shape C
  # paste-the-redirect-URL fallback only). This only relaxes oauthlib's
  # localhost-over-HTTP rule for redirect_uri matching at token-exchange
  # time; live Drive API calls remain HTTPS-only. Set unconditionally so
  # the env var is in place before any oauthlib-using module imports.
  os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
  ```

  `setdefault` preserves an explicit user-set value (some debugging scenarios may want `0`).

- [ ] **Step 2: Verify the env var is set when daemon module imports**

  Run:
  ```bash
  python -c "import os; os.environ.pop('OAUTHLIB_INSECURE_TRANSPORT', None); import cumulus.daemon; assert os.environ['OAUTHLIB_INSECURE_TRANSPORT'] == '1'; print('ok')"
  ```
  Expected: `ok`.

- [ ] **Step 3: Commit**

  ```bash
  git add src/cumulus/daemon.py
  git commit -m "feat(stage-2/daemon): set OAUTHLIB_INSECURE_TRANSPORT for Shape C fallback"
  ```

---

## Task 10: `core.py` — `FullAccount` field cleanup for Drive shape

**Spec ref:** §2.1 last bullet.

**Files:**
- Modify: `src/cumulus/core.py`
- Test: `tests/offline/test_core_models.py` (new, small)

Drive's `about.get(fields="user(emailAddress,displayName,permissionId,photoLink)")` returns:
- `permissionId` (stable account ID)
- `emailAddress`
- `displayName`
- `photoLink`

Plus `storageQuota.usage` / `storageQuota.limit` for `SpaceUsage`.

Drop the Dropbox-only fields from `FullAccount`: `country`, `locale`, `team`, `team_member_id`, `account_type`, `root_info`. Drop `email_verified` from the parent `Account` (Drive doesn't expose it). Keep: `account_id`, `display_name`, `email`, `profile_photo_url`, `disabled`.

- [ ] **Step 1: Write the failing test**

  Create `tests/offline/test_core_models.py`:

  ```python
  from __future__ import annotations

  from dataclasses import fields

  from cumulus.core import Account, FullAccount


  def test_account_field_set_matches_drive_about_response():
      field_names = {f.name for f in fields(Account)}
      assert field_names == {
          "account_id",
          "display_name",
          "email",
          "profile_photo_url",
          "disabled",
      }


  def test_full_account_drops_dropbox_only_fields():
      field_names = {f.name for f in fields(FullAccount)}
      assert "country" not in field_names
      assert "locale" not in field_names
      assert "team" not in field_names
      assert "team_member_id" not in field_names
      assert "account_type" not in field_names
      assert "root_info" not in field_names
      # but the Account base fields stay:
      assert {
          "account_id",
          "display_name",
          "email",
          "profile_photo_url",
          "disabled",
      }.issubset(field_names)
  ```

- [ ] **Step 2: Run test to verify it fails**

  Run:
  ```bash
  pytest tests/offline/test_core_models.py -v
  ```
  Expected: fails on `email_verified` still present and Dropbox-only fields still present.

- [ ] **Step 3: Trim `Account` and `FullAccount` in `src/cumulus/core.py`**

  Replace the `Account` and `FullAccount` dataclasses with:

  ```python
  @dataclass
  class Account:
      """Represents the user's account"""

      account_id: str
      """Unique account ID (Drive permissionId)"""
      display_name: str
      """The user's name for display purposes"""
      email: str
      """The user's email address"""
      profile_photo_url: str | None
      """A URL to the user's photo, or None"""
      disabled: bool
      """Whether the account is disabled"""


  @dataclass
  class FullAccount(Account):
      """Represents the user's account and sync information.

      For Drive there is no team or root-namespace concept; FullAccount is
      currently just Account plus a placeholder for future Drive-specific
      fields (e.g. domain when shared-drive support lands)."""
      pass
  ```

- [ ] **Step 4: Adjust any callers that read removed fields**

  Run:
  ```bash
  grep -rn "\.team\b\|\.locale\b\|\.country\b\|\.account_type\b\|\.email_verified\b\|\.root_info\b\|\.team_member_id\b" src/cumulus/ --include="*.py" | grep -v "team_space\|team\.py"
  ```

  For each match, evaluate:
  - If the read is in test code or in code already broken by the Dropbox→Drive migration: leave it; it's in a pre-existing test failure file.
  - If it's in live src/cumulus code (manager.py, sync.py, etc.), remove or replace with a TODO comment that the field is no longer present (Stage 5 sync-engine pass will replace the call site).

  Likely callsites (verify with grep above):
  - `src/cumulus/main.py` — any `account.team` references in legacy code paths.

  For each live callsite that no longer compiles, replace with `None` or remove the conditional branch entirely (since these are dead Dropbox-only paths). Document each removal in a one-line code comment if not obvious. Re-run grep above until clean.

- [ ] **Step 5: Run test to verify it passes**

  Run:
  ```bash
  pytest tests/offline/test_core_models.py -v
  ```
  Expected: 2 tests pass.

  Also verify no offline tests regress:
  ```bash
  pytest tests/offline/ -v --ignore=tests/offline/test_client.py --ignore=tests/offline/test_main.py --ignore=tests/offline/test_manager.py --ignore=tests/offline/test_daemon.py --ignore=tests/offline/test_event_handler.py --ignore=tests/offline/test_errorhandling.py
  ```

  These ignore flags exclude the pre-existing test failures (recorded in the project memory file). `test_errorhandling.py` is excluded only because we're modifying it in this stage and don't want to retest yet.

- [ ] **Step 6: Commit**

  ```bash
  git add src/cumulus/core.py tests/offline/test_core_models.py src/cumulus/main.py
  # (and any other modified callsite files)
  git commit -m "feat(stage-2/core): drop Dropbox-only Account fields for Drive shape"
  ```

---

## Task 11: `client.py` — `linked` property + lazy credential load

**Spec ref:** §5.1.

**Files:**
- Modify: `src/cumulus/client.py`
- Create: `tests/offline/test_client_auth.py`

- [ ] **Step 1: Write the failing tests**

  Create `tests/offline/test_client_auth.py`:

  ```python
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
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v
  ```
  Expected: fails because `linked` still calls `_not_implemented()`.

- [ ] **Step 3: Implement `linked` and the `_credentials` cache**

  In `src/cumulus/client.py`:

  1. Add `from . import oauth` near the other cumulus imports.
  2. In `__init__`, add `self._credentials: oauth.PersistingCredentials | None = None` and `self._pending_paste_flow: _PasteFlowState | None = None` (the dataclass goes in client.py too — see below).
  3. Add a module-level dataclass below the constants block (around line 78):

  ```python
  from dataclasses import dataclass
  from datetime import datetime

  @dataclass
  class _PasteFlowState:
      flow: Any  # google_auth_oauthlib.flow.Flow
      created_at: datetime
  ```

  4. Replace the `linked` property with:

  ```python
  @property
  def linked(self) -> bool:
      """Whether the client is linked to a Google account."""
      if self._credentials is None:
          loaded = oauth.load_credentials(self._cred_storage)
          if loaded is not None and loaded.refresh_token is not None:
              account_id = self._cred_storage.account_id or ""
              self._credentials = oauth.wrap_persisting(
                  loaded, self._cred_storage, account_id
              )
      return (
          self._credentials is not None
          and self._credentials.refresh_token is not None
      )
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v
  ```
  Expected: 3 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client_auth.py
  git commit -m "feat(stage-2/client): linked property with lazy credential load"
  ```

---

## Task 12: `client.py` — `_get_service()` helper

**Spec ref:** §5.5.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client_auth.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_client_auth.py`:

  ```python
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
      _, kwargs = m_build.call_args
      assert m_build.call_args.args[:2] == ("drive", "v3")
      assert kwargs["credentials"] is client._credentials
      assert kwargs["cache_discovery"] is False
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v -k "get_service"
  ```
  Expected: `_get_service` not defined yet.

- [ ] **Step 3: Add `_get_service` and the `build` import**

  In `src/cumulus/client.py`:

  1. Add to imports:

  ```python
  from googleapiclient.discovery import build
  ```

  2. After the `# ---- Account operations ---- ` block divider (around line 236), add:

  ```python
  # ---- Service builder -------------------------------------------------------------

  def _get_service(self):
      """Build a fresh Drive v3 service object using cached credentials.

      Every API call in Stages 3+ goes through this helper. cache_discovery
      is disabled because the discovery cache is unsafe to share across
      thread/process boundaries and is just disk I/O we don't need.
      """
      if not self.linked:
          raise NotLinkedError(
              "Not linked",
              "Run 'cumulus auth link' to authorize Cumulus.",
          )
      return build(
          "drive", "v3",
          credentials=self._credentials,
          cache_discovery=False,
      )
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v
  ```
  Expected: 5 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client_auth.py
  git commit -m "feat(stage-2/client): _get_service helper builds Drive v3 service"
  ```

---

## Task 13: `client.py` — `get_account_info` / `get_space_usage` / `_fetch_account_info_for_creds`

**Spec ref:** §5.3 (`_fetch_account_info_for_creds`).

This is the single Drive API call Stage 2 needs (`about.get`). Used both during `link(...)` to validate and to surface account info for `pending_link`/`get_account_info`.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client_auth.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_client_auth.py`:

  ```python
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
      fake_service = mock.MagicMock()
      fake_service.about().get().execute.return_value = _fake_about_response()
      with mock.patch.object(client, "_get_service", return_value=fake_service):
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

      with mock.patch.object(client, "_get_service", return_value=fake_service):
          a1 = client.get_account_info()
          a2 = client.get_account_info()

      # Second call must NOT issue another about().get().execute().
      assert a1 is a2


  def test_get_space_usage(client, cred_storage, monkeypatch):
      _link_to_perm_x(client, cred_storage, monkeypatch)
      fake_service = mock.MagicMock()
      fake_service.about().get().execute.return_value = _fake_about_response()

      with mock.patch.object(client, "_get_service", return_value=fake_service):
          usage = client.get_space_usage()

      assert isinstance(usage, PersonalSpaceUsage)
      assert usage.used == 1024000
      assert usage.allocated == 16106127360
      assert usage.team_usage is None
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v -k "fetch_account or account_info or space_usage"
  ```
  Expected: methods still raise `NotImplementedError`.

- [ ] **Step 3: Implement the three methods**

  In `src/cumulus/client.py`:

  Replace the existing `get_account_info`/`get_space_usage` stubs (they currently call `_not_implemented()`) with:

  ```python
  def _fetch_account_info_for_creds(self, creds) -> FullAccount:
      """Validate credentials by issuing one about.get call, returning the
      account info. Used both by link() and by get_account_info()."""
      # Build a one-shot service rather than going through _get_service —
      # link() may be calling us with credentials not yet stored in self.
      from googleapiclient.discovery import build as _build
      service = _build(
          "drive", "v3", credentials=creds, cache_discovery=False
      )
      response = service.about().get(
          fields="user(emailAddress,displayName,permissionId,photoLink),"
                 "storageQuota(usage,limit)"
      ).execute()
      user = response.get("user", {})
      quota = response.get("storageQuota", {})

      # Cache the quota onto the client (cheap; lets get_space_usage reuse).
      self._cached_space_usage = PersonalSpaceUsage(
          used=int(quota.get("usage", 0)),
          allocated=int(quota.get("limit", 0)),
          team_usage=None,
      )
      return FullAccount(
          account_id=user.get("permissionId", ""),
          display_name=user.get("displayName", ""),
          email=user.get("emailAddress", ""),
          profile_photo_url=user.get("photoLink"),
          disabled=False,
      )


  def get_account_info(self, dbid: str | None = None) -> FullAccount:
      """Returns the current account info. The dbid param is preserved for
      compat with the Maestral interface but is ignored for Drive (the API
      only exposes the linked user, never arbitrary accounts).
      """
      if self._cached_account_info is None:
          self._cached_account_info = self._fetch_account_info_for_creds(
              self._credentials
          )
      return self._cached_account_info


  def get_space_usage(self) -> PersonalSpaceUsage:
      if getattr(self, "_cached_space_usage", None) is None:
          # Force a refresh via get_account_info.
          self._cached_account_info = None
          self.get_account_info()
      return self._cached_space_usage
  ```

  Also add the `_cached_space_usage` initialization to `__init__`:

  ```python
  self._cached_space_usage: PersonalSpaceUsage | None = None
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v
  ```
  Expected: 8 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client_auth.py
  git commit -m "feat(stage-2/client): about.get-backed account info + space usage"
  ```

---

## Task 14: `client.py` — `link(refresh_token=...)` happy + error paths

**Spec ref:** §5.3.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client_auth.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_client_auth.py`:

  ```python
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
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v -k "link_with"
  ```

- [ ] **Step 3: Implement `link()` core**

  In `src/cumulus/client.py`, replace the `link` stub with:

  ```python
  def link(
      self,
      code: str | None = None,
      refresh_token: str | None = None,
      access_token: str | None = None,
  ) -> int:
      """Link with a Google account. Returns 0 / 1 / 2 (success / invalid /
      connection failure).
      """
      from google.auth.exceptions import RefreshError
      from google.auth.transport.requests import Request as _Request

      try:
          if refresh_token is not None:
              creds = oauth.credentials_from_refresh_token(refresh_token)
              creds.refresh(_Request())
          elif access_token is not None:
              # Bare access-token path: useful for CI/tests; cannot survive
              # token expiry but enough to validate the round-trip.
              from google.oauth2.credentials import Credentials as _Credentials
              creds = _Credentials(
                  token=access_token, scopes=list(oauth.DRIVE_SCOPES)
              )
          elif code is not None:
              # Shape C paste-the-redirect-URL fallback. Implemented in Task 16.
              return self._link_with_paste_code(code)
          else:
              return 1

          account = self._fetch_account_info_for_creds(creds)
          oauth.save_credentials(self._cred_storage, creds, account.account_id)
          self._credentials = oauth.wrap_persisting(
              creds, self._cred_storage, account.account_id
          )
          self._cached_account_info = account
          return 0

      except RefreshError:
          return 1
      except CONNECTION_ERRORS:
          return 2

  def _link_with_paste_code(self, code: str) -> int:
      raise NotImplementedError  # Task 16
  ```

  Add to the imports (top of file): `from .errorhandling import CONNECTION_ERRORS` is already there.

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v
  ```
  Expected: 12 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client_auth.py
  git commit -m "feat(stage-2/client): link(refresh_token=...) happy + error paths"
  ```

---

## Task 15: `client.py` — `link(access_token=...)` validation

**Spec ref:** §5.3.

The previous task already wired `access_token` to a stub Credentials. This task adds a happy-path test for it (validation passes through `_fetch_account_info_for_creds` already).

**Files:**
- Test: `tests/offline/test_client_auth.py`

- [ ] **Step 1: Add the test**

  Append to `tests/offline/test_client_auth.py`:

  ```python
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
  ```

- [ ] **Step 2: Run test to verify it passes**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py::test_link_with_access_token_happy_path -v
  ```
  Expected: passes.

- [ ] **Step 3: Commit**

  ```bash
  git add tests/offline/test_client_auth.py
  git commit -m "test(stage-2/client): link(access_token=...) round-trip"
  ```

---

## Task 16: `client.py` — `get_auth_url()` + `link(code=...)` Shape C pair

**Spec ref:** §5.2, §5.3.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client_auth.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_client_auth.py`:

  ```python
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
      fake_flow.credentials = fake_creds

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
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v -k "auth_url or link_with_code or pending_flow"
  ```

- [ ] **Step 3: Implement `get_auth_url` and the `code=` branch of `link`**

  In `src/cumulus/client.py`:

  1. Replace the `get_auth_url` stub with:

  ```python
  def get_auth_url(self) -> str:
      flow = oauth.make_paste_flow()
      auth_url, _ = flow.authorization_url(
          access_type="offline", prompt="consent"
      )
      self._pending_paste_flow = _PasteFlowState(
          flow=flow, created_at=datetime.utcnow()
      )
      return auth_url
  ```

  2. Replace the `_link_with_paste_code` stub with the real body, plus refactor `link` to inline the code-path so the return-code mapping (RefreshError → 1, CONNECTION_ERRORS → 2) applies uniformly:

  ```python
  def link(
      self,
      code: str | None = None,
      refresh_token: str | None = None,
      access_token: str | None = None,
  ) -> int:
      from google.auth.exceptions import RefreshError
      from google.auth.transport.requests import Request as _Request

      try:
          if refresh_token is not None:
              creds = oauth.credentials_from_refresh_token(refresh_token)
              creds.refresh(_Request())
          elif access_token is not None:
              from google.oauth2.credentials import Credentials as _Credentials
              creds = _Credentials(
                  token=access_token, scopes=list(oauth.DRIVE_SCOPES)
              )
          elif code is not None:
              state = self._pending_paste_flow
              self._pending_paste_flow = None
              if state is None:
                  return 1
              if (datetime.utcnow() - state.created_at) > timedelta(minutes=10):
                  return 1
              state.flow.fetch_token(code=code)
              creds = state.flow.credentials
          else:
              return 1

          account = self._fetch_account_info_for_creds(creds)
          oauth.save_credentials(self._cred_storage, creds, account.account_id)
          self._credentials = oauth.wrap_persisting(
              creds, self._cred_storage, account.account_id
          )
          self._cached_account_info = account
          return 0

      except RefreshError:
          return 1
      except CONNECTION_ERRORS:
          return 2
  ```

  Delete the `_link_with_paste_code` placeholder.

  Add `from datetime import timedelta` to the imports.

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v
  ```
  Expected: 18 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client_auth.py
  git commit -m "feat(stage-2/client): get_auth_url + link(code=...) Shape C pair"
  ```

---

## Task 17: `client.py` — `unlink()` with best-effort token revoke

**Spec ref:** §5.4.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client_auth.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_client_auth.py`:

  ```python
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
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v -k "unlink"
  ```

- [ ] **Step 3: Implement `unlink`**

  In `src/cumulus/client.py`, replace the `unlink` stub with:

  ```python
  def unlink(self) -> None:
      """Unlinks the configured Google account. Best-effort revokes the
      refresh token with Google so a leaked token can't be reused; failures
      to revoke do not block local-state cleanup."""
      import requests as _requests
      creds = self._credentials
      if creds is not None and creds.refresh_token:
          try:
              _requests.post(
                  "https://oauth2.googleapis.com/revoke",
                  params={"token": creds.refresh_token},
                  timeout=self._timeout,
              )
          except CONNECTION_ERRORS:
              logger.debug("Could not revoke token with Google", exc_info=True)
      self._credentials = None
      self._pending_paste_flow = None
      self._cached_account_info = None
      self._cached_space_usage = None
  ```

  Add `import requests` near the top of the file (it's already used elsewhere in the cumulus codebase).

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_client_auth.py -v
  ```
  Expected: 21 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client_auth.py
  git commit -m "feat(stage-2/client): unlink with best-effort token revoke"
  ```

---

## Task 18: CLI — `_local_server_dialog` (Shape A default)

**Spec ref:** §6.1.

**Files:**
- Modify: `src/cumulus/cli/cli_core.py`
- Test: `tests/offline/test_cli.py` (extend)

- [ ] **Step 1: Add the failing test**

  Append to `tests/offline/test_cli.py`:

  ```python
  from unittest import mock

  from click.testing import CliRunner

  from cumulus.cli.cli_core import _local_server_dialog


  def test_local_server_dialog_invokes_run_local_flow_and_passes_refresh_token():
      m = mock.Mock()
      m.config_name = "test-config"
      m.link = mock.Mock(return_value=0)

      fake_creds = mock.Mock()
      fake_creds.refresh_token = "rt-from-loopback"

      with mock.patch(
          "cumulus.cli.cli_core.oauth.run_local_flow",
          return_value=fake_creds,
      ):
          res = _local_server_dialog(m)

      m.link.assert_called_once_with(refresh_token="rt-from-loopback")
      assert res == 0


  def test_local_server_dialog_returns_1_when_creds_lack_refresh_token():
      m = mock.Mock()
      m.config_name = "test-config"
      m.link = mock.Mock()

      fake_creds = mock.Mock()
      fake_creds.refresh_token = None  # consent skipped; refresh_token missing.

      with mock.patch(
          "cumulus.cli.cli_core.oauth.run_local_flow",
          return_value=fake_creds,
      ):
          res = _local_server_dialog(m)

      assert res == 1
      m.link.assert_not_called()
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_cli.py -v -k "local_server_dialog"
  ```

- [ ] **Step 3: Implement `_local_server_dialog` and the `oauth` import**

  In `src/cumulus/cli/cli_core.py`:

  1. Add to imports:

  ```python
  from .. import oauth
  from ..output import echo, info, ok, warn  # if not already imported
  ```

  *(echo/info/ok/warn may already be imported via `.output`. Check existing imports and avoid duplicates.)*

  2. Add the helper near the existing `link_dialog`:

  ```python
  def _local_server_dialog(m: "MaestralProxy | Maestral") -> int:
      """Shape A: run the loopback server in the CLI process, then hand the
      resulting refresh_token to the daemon's link(refresh_token=...).

      Returns the same 0/1/2 contract as Maestral.link.
      """
      info("A browser will open to authorize Cumulus to access your Google Drive.")
      info("If you're on a headless server, run on your laptop first:")
      info("    ssh -L 8080:localhost:8080 <your-host>")
      info("Then re-run 'cumulus auth link' here.")

      try:
          creds = oauth.run_local_flow(port=8080)
      except Exception as exc:  # AuthError already; surface message and bail.
          warn(str(exc))
          return 1

      if not getattr(creds, "refresh_token", None):
          warn(
              "Authorization completed but no refresh token was returned. "
              "Re-run with -r and approve the consent screen, or use "
              "--no-local-server."
          )
          return 1

      return m.link(refresh_token=creds.refresh_token)
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_cli.py -v -k "local_server_dialog"
  ```
  Expected: 2 tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/cli/cli_core.py tests/offline/test_cli.py
  git commit -m "feat(stage-2/cli): _local_server_dialog for Shape A loopback flow"
  ```

---

## Task 19: CLI — `_paste_redirect_dialog` + `_parse_redirect_url_or_code`

**Spec ref:** §6.1.

**Files:**
- Modify: `src/cumulus/cli/cli_core.py`
- Test: `tests/offline/test_cli.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_cli.py`:

  ```python
  from cumulus.cli.cli_core import (
      _paste_redirect_dialog,
      _parse_redirect_url_or_code,
  )


  def test_parse_redirect_url_extracts_code_from_full_redirect():
      url = "http://localhost:8080/?code=4%2F0AY0e-g7XYZ&state=abc&scope=drive"
      assert _parse_redirect_url_or_code(url) == "4/0AY0e-g7XYZ"


  def test_parse_redirect_url_extracts_code_from_bare_code():
      assert _parse_redirect_url_or_code("4/0AY0e-g7XYZ") == "4/0AY0e-g7XYZ"


  def test_parse_redirect_url_strips_whitespace():
      assert _parse_redirect_url_or_code("  4/0AY0e-g7XYZ  ") == "4/0AY0e-g7XYZ"


  def test_parse_redirect_url_returns_empty_on_no_code():
      assert _parse_redirect_url_or_code("http://localhost:8080/?error=denied") == ""
      assert _parse_redirect_url_or_code("") == ""


  def test_paste_redirect_dialog_calls_link_with_parsed_code(monkeypatch):
      m = mock.Mock()
      m.get_auth_url = mock.Mock(return_value="https://accounts.google.com/auth")
      m.link = mock.Mock(return_value=0)

      # Simulate user pasting the full redirect URL.
      monkeypatch.setattr(
          "cumulus.cli.cli_core.prompt",
          lambda _: "http://localhost:8080/?code=4/abc&state=x",
      )

      res = _paste_redirect_dialog(m)
      m.link.assert_called_once_with(code="4/abc")
      assert res == 0


  def test_paste_redirect_dialog_returns_1_on_empty_parse(monkeypatch):
      m = mock.Mock()
      m.get_auth_url = mock.Mock(return_value="https://accounts.google.com/auth")
      m.link = mock.Mock()

      monkeypatch.setattr("cumulus.cli.cli_core.prompt", lambda _: "")

      res = _paste_redirect_dialog(m)
      m.link.assert_not_called()
      assert res == 1
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_cli.py -v -k "parse_redirect or paste_redirect"
  ```

- [ ] **Step 3: Implement `_parse_redirect_url_or_code` and `_paste_redirect_dialog`**

  In `src/cumulus/cli/cli_core.py`, add:

  ```python
  from urllib.parse import parse_qs, urlparse


  def _parse_redirect_url_or_code(value: str) -> str:
      """Accept either the full redirect URL pasted from the browser address
      bar (e.g. 'http://localhost:8080/?code=4/abc&state=x') or a bare code
      ('4/abc'). Returns the bare code, or '' on failure."""
      value = value.strip()
      if not value:
          return ""
      if value.startswith("http://") or value.startswith("https://"):
          parsed = urlparse(value)
          codes = parse_qs(parsed.query).get("code", [])
          return codes[0] if codes else ""
      return value


  def _paste_redirect_dialog(m: "MaestralProxy | Maestral") -> int:
      """Shape C: ask the daemon for an auth URL, prompt the user to open it,
      then paste back the redirected URL (or just the code). The daemon
      consumes the code via link(code=...).
      """
      url = m.get_auth_url()
      info("Open this URL in any browser to authorize Cumulus:")
      info(f"    {url}")
      info(
          "After approving, your browser will fail to connect to a "
          "localhost URL — that's expected. Copy the URL from your "
          "browser's address bar and paste it below."
      )

      raw = prompt("Paste the redirected URL (or just the code)")
      code = _parse_redirect_url_or_code(raw)
      if not code:
          warn("Could not find an authorization code in that input.")
          return 1
      return m.link(code=code)
  ```

  `prompt` is already imported from `.dialogs`.

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_cli.py -v
  ```
  Expected: previous tests still pass + 6 new tests pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/cli/cli_core.py tests/offline/test_cli.py
  git commit -m "feat(stage-2/cli): _paste_redirect_dialog for Shape C fallback"
  ```

---

## Task 20: CLI — rewire `auth_link` to split on `--no-local-server`

**Spec ref:** §6.1.

**Files:**
- Modify: `src/cumulus/cli/cli_core.py`
- Test: `tests/offline/test_cli.py`

- [ ] **Step 1: Add the failing tests**

  Append to `tests/offline/test_cli.py`:

  ```python
  from cumulus.cli.cli_core import auth_link


  def test_auth_link_uses_local_server_dialog_by_default(monkeypatch):
      m_proxy = mock.Mock()
      m_proxy.pending_link = True
      m_proxy.config_name = "test-cli"

      called = []

      def _fake_local(m):
          called.append("local")
          return 0

      def _fake_paste(m):
          called.append("paste")
          return 0

      monkeypatch.setattr("cumulus.cli.cli_core._local_server_dialog", _fake_local)
      monkeypatch.setattr("cumulus.cli.cli_core._paste_redirect_dialog", _fake_paste)
      monkeypatch.setattr(
          "cumulus.cli.cli_core.inject_proxy",
          lambda **_: (lambda f: f),  # bypass the daemon-process injection.
      )

      # The decorator was already applied; we invoke the underlying function.
      auth_link.callback(m_proxy, relink=False, no_local_server=False,
                         refresh_token=None, access_token=None)

      assert called == ["local"]


  def test_auth_link_uses_paste_redirect_when_flag_set(monkeypatch):
      m_proxy = mock.Mock()
      m_proxy.pending_link = True
      m_proxy.config_name = "test-cli"

      called = []
      monkeypatch.setattr(
          "cumulus.cli.cli_core._local_server_dialog",
          lambda m: called.append("local") or 0,
      )
      monkeypatch.setattr(
          "cumulus.cli.cli_core._paste_redirect_dialog",
          lambda m: called.append("paste") or 0,
      )

      auth_link.callback(m_proxy, relink=False, no_local_server=True,
                         refresh_token=None, access_token=None)

      assert called == ["paste"]
  ```

  *(If `auth_link.callback` proves awkward because of decorator chaining, fall back to invoking via `CliRunner` and `--no-local-server` flag — the test goal is the dispatch path, not the decorator-detail.)*

- [ ] **Step 2: Run tests to verify they fail**

  Run:
  ```bash
  pytest tests/offline/test_cli.py -v -k "auth_link"
  ```

- [ ] **Step 3: Rewrite `auth_link`**

  In `src/cumulus/cli/cli_core.py`, replace the existing `auth_link` (lines around 333-365) with:

  ```python
  @auth.command(name="link", help="Link a new remote account.")
  @click.option(
      "--relink", "-r", is_flag=True, default=False,
      help="Relink to the existing account. Keeps the sync state.",
  )
  @click.option(
      "--no-local-server", is_flag=True, default=False,
      help="Use the paste-the-redirect-URL flow instead of the loopback "
           "server. Useful for headless setups without SSH port-forwarding.",
  )
  @click.option(
      "--refresh-token", "refresh_token", default=None, hidden=True,
  )
  @click.option(
      "--access-token", "access_token", default=None, hidden=True,
  )
  @inject_proxy(fallback=True, existing_config=False)
  @convert_api_errors
  def auth_link(
      m: Maestral,
      relink: bool,
      no_local_server: bool,
      refresh_token: str | None,
      access_token: str | None,
  ) -> None:
      if not m.pending_link and not relink:
          echo(
              "Cumulus is already linked. Use '-r' to relink to the same "
              "account or specify a new config name with '-c'."
          )
          return

      if refresh_token or access_token:
          res = m.link(refresh_token=refresh_token, access_token=access_token)
      elif no_local_server:
          res = _paste_redirect_dialog(m)
      else:
          res = _local_server_dialog(m)

      _report_link_result(m, res)


  def _report_link_result(m: "MaestralProxy | Maestral", res: int) -> None:
      if res == 0:
          email = m.get_state("account", "email") or "your account"
          ok(f"Linked to {email}")
      elif res == 1:
          warn("Invalid authorization, please try again.")
      elif res == 2:
          warn("Could not connect to Google, please try again.")
  ```

  Delete the old `link_dialog` function entirely (it has been fully replaced by `_local_server_dialog` + `_paste_redirect_dialog`). Then update the `start` command's `startup_dialog` (around line 189 of cli_core.py) to call the new flow:

  ```python
      if m.pending_link:
          # Default to Shape A (loopback). The user can still re-run
          # 'cumulus auth link --no-local-server' explicitly if needed.
          res = _local_server_dialog(m)
          _report_link_result(m, res)
          if res != 0:
              return
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run:
  ```bash
  pytest tests/offline/test_cli.py -v
  ```
  Expected: all auth-related CLI tests pass; previous tests still pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/cli/cli_core.py tests/offline/test_cli.py
  git commit -m "feat(stage-2/cli): split auth link on --no-local-server, drop old link_dialog"
  ```

---

## Task 21: Manager — verify `_handle_sync_thread_errors` handles `AuthError`

**Spec ref:** §6.4.

The existing handler has a generic `except Exception` branch that calls `self.stop()` and notifies. We need a test pinning this for `TokenRevokedError` specifically, so a future refactor doesn't accidentally lose B3 wiring.

**Files:**
- Create: `tests/offline/test_manager_auth.py`
- Modify (only if test reveals a gap): `src/cumulus/manager.py`

- [ ] **Step 1: Write the failing/pinning test**

  Create `tests/offline/test_manager_auth.py`:

  ```python
  """B3 verification: a TokenRevokedError raised inside a background sync
  thread must stop the manager and surface a user-visible message.
  """

  from __future__ import annotations

  from threading import Event
  from unittest import mock

  import pytest

  from cumulus.exceptions import TokenRevokedError


  def test_handle_sync_thread_errors_stops_on_token_revoked():
      """Pins the existing manager behavior: TokenRevokedError stops the
      manager and triggers the notifier. If this test starts failing because
      of a manager refactor, add an explicit branch — don't loosen the test.
      """
      from cumulus.manager import SyncManager

      # Minimal SyncManager stub: real construction requires SyncEngine.
      mgr = SyncManager.__new__(SyncManager)
      mgr._logger = mock.Mock()
      mgr.desktop_notifier = mock.Mock()
      mgr.stop = mock.Mock()
      mgr.autostart = Event()

      with mgr._handle_sync_thread_errors(running=Event(), autostart=Event()):
          raise TokenRevokedError(
              "Authentication revoked", "Run 'cumulus auth link -r' to re-link."
          )

      mgr.stop.assert_called_once()
      mgr.desktop_notifier.notify.assert_called_once()
      # The user-facing title must mention auth.
      call_args = mgr.desktop_notifier.notify.call_args
      title = call_args.args[0] if call_args.args else call_args.kwargs.get("title", "")
      assert "auth" in title.lower() or "revok" in title.lower()
  ```

- [ ] **Step 2: Run the test**

  Run:
  ```bash
  pytest tests/offline/test_manager_auth.py -v
  ```

  **If the test passes:** the existing wiring is sufficient. Skip Step 3, proceed to Step 4.

  **If the test fails:** the existing wiring drops the title. Add an explicit branch in `_handle_sync_thread_errors`:

- [ ] **Step 3 (conditional): Add explicit AuthError branch in manager**

  In `src/cumulus/manager.py:756-779`, insert before the generic `except Exception`:

  ```python
          except AuthError as err:
              self._logger.error(err.title, exc_info=True)
              if self.desktop_notifier:
                  self.desktop_notifier.notify(
                      err.title, err.message, level=notify.ERROR
                  )
              self.stop()
              # Do NOT set autostart — re-link requires user action.
  ```

  Add `from .exceptions import AuthError` to manager.py imports.

  Re-run the test from Step 2 until it passes.

- [ ] **Step 4: Commit**

  ```bash
  git add tests/offline/test_manager_auth.py src/cumulus/manager.py
  git commit -m "test(stage-2/manager): pin TokenRevokedError handling in background threads"
  ```

  *(If no manager change was required, the commit is test-only.)*

---

## Task 22: Manual smoke-test checklist + plan deviations file

**Spec ref:** §7.4 and §7.5.

**Files:**
- Create: `docs/superpowers/plans/2026-05-12-stage-2-auth-onboarding-deviations.md`

This mirrors the Stage 1 pattern of recording plan deviations and the manual smoke test results alongside the plan.

- [ ] **Step 1: Create the deviations file with the smoke-test checklist**

  Create `docs/superpowers/plans/2026-05-12-stage-2-auth-onboarding-deviations.md`:

  ```markdown
  # Stage 2 — Plan deviations & manual smoke test

  This file records two things, both required by the staged-port workflow:
  1. Any deviations the implementer made from the plan during execution
     (with rationale).
  2. The result of the manual smoke test required by master design §7.

  ## Plan deviations

  _Append entries here as tasks are completed. Format:_
  > **Task N (commit `<sha>`):** what changed from the plan and why.

  ## Manual smoke test

  Run this once Tasks 1–21 are merged. Tick each item only after confirming
  the expected behavior.

  - [ ] **1. Default flow (Shape A).** Run `cumulus auth link`. Browser
    opens, consent screen shows "Cumulus" as the app, "Linked to <email>"
    message appears. Cumulus is linked.
  - [ ] **2. Relink with Shape C (`--no-local-server`).** Run
    `cumulus auth link -r --no-local-server`. The CLI prints a URL; open
    it, approve, copy the failed-redirect URL from your browser address
    bar, paste it back into the CLI. "Linked to <email>" appears.
  - [ ] **3. Persistence across restarts.** Stop the daemon
    (`cumulus stop` or kill the process); start a fresh session
    (`cumulus start`). The daemon does NOT prompt for re-link.
  - [ ] **4. Token revocation.** Visit
    https://myaccount.google.com/permissions → find "Cumulus" → Remove
    Access. Within ~5 minutes (next change-feed poll once Stage 3 lands —
    for Stage 2 standalone, force an API call manually:
    `python -c "from cumulus.main import Maestral; m = Maestral('default'); print(m.client.get_account_info())"`).
    Expected: a `TokenRevokedError` surfaces as a user-visible "Re-link
    needed" notification. The daemon stops; no retry loop.
  - [ ] **5. Unlink.** Run `cumulus auth unlink`. Confirm. Verify the
    keyring entry is gone (`secret-tool lookup ...` on Linux, or
    `security find-internet-password -s Cumulus` on macOS). Verify
    config's `auth/account_id` is empty.

  Once all five are ticked, Stage 2 is done per master design §7.
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add docs/superpowers/plans/2026-05-12-stage-2-auth-onboarding-deviations.md
  git commit -m "docs(stage-2): plan-deviations and manual smoke-test checklist"
  ```

---

## Final task: Stage 2 readiness check

- [ ] **Step 1: Full offline test sweep**

  Run:
  ```bash
  pytest tests/offline/ -v \
      --ignore=tests/offline/test_client.py \
      --ignore=tests/offline/test_main.py \
      --ignore=tests/offline/test_manager.py \
      --ignore=tests/offline/test_daemon.py \
      --ignore=tests/offline/test_event_handler.py
  ```

  Expected: all Stage 1 and Stage 2 tests pass. The ignored files are
  recorded as pre-existing failures from the Dropbox→Drive migration and
  remain Stage 3/5/6 work.

- [ ] **Step 2: Run the manual smoke test from Task 22**

  Tick each item in the deviations file as you confirm it.

- [ ] **Step 3: Stage completion checklist (master design §7)**

  - [x] Spec committed (Task 0 — `docs/superpowers/specs/2026-05-12-stage-2-auth-onboarding-design.md`).
  - [x] Plan committed (this file).
  - [ ] All code merged to `google-drive-port`.
  - [ ] All new offline tests pass; existing offline tests pass-or-deferred.
  - [ ] Manual smoke test §7.4 completed; results recorded in the deviations file.
