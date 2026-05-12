# Stage 2 — Auth & onboarding

**Status:** Design (awaiting review)
**Date:** 2026-05-12
**Stage of:** `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` §5.2
**Inputs:** the staged-port plan, `docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md` Q1, the current stub state of `src/cumulus/client.py` / `keyring.py` / `cli/cli_core.py` / `main.py` / `exceptions.py` / `errorhandling.py`, REVIEW2 entries B1 and B3.

---

## 1. Purpose

Stand up the auth & onboarding surface so the user can run `cumulus auth link`
against a Google account, the daemon persists OAuth credentials in the system
keyring, every later stage's API call carries valid auth, and revoked-grant
errors propagate cleanly to a re-link prompt instead of crashing background
threads.

Stage 2 does not implement any Drive functionality beyond the single
`about.get` call needed to validate credentials and capture account identity.
Read and write clients are Stage 3 and Stage 4.

## 2. Scope

### 2.1 In scope

- **Phase 2 — Google Cloud project setup.** A "Cumulus" project with the
  Drive API enabled and a **Desktop app** OAuth client (this client type is
  what makes Google accept any `http://localhost:<port>` redirect URI
  without exact-match registration, including the random ports
  `run_local_server` may bind on different developers' machines).
  The OAuth client also has `http://localhost:8080` explicitly registered
  to support the Shape C paste-redirect fallback. Ship `client_secret.json`
  at `src/cumulus/resources/client_secret.json`, loaded via
  `importlib.resources`, with a `CUMULUS_CLIENT_SECRET_PATH` env-var
  override. The Google Cloud project ships in **Testing** mode (B2
  verification deferred to Stage 8). The 100-test-user cap and 7-day
  refresh-token expiry are documented as known limitations cleared by
  Stage 8.
- **Phase 3 — auth API.** Final-form `get_auth_url`, `link(code | refresh_token
  | access_token)`, `unlink()`, `linked` property in `GoogleDriveClient`.
  Credential persistence and refresh-on-401 wired through.
- **B1 — headless-friendly UX.** Default flow is
  `InstalledAppFlow.run_local_server(port=8080)` running **in the CLI
  process**. SSH-tunnel users get a documented
  `ssh -L 8080:localhost:8080` recipe. A paste-the-redirect-URL fallback
  (`cumulus auth link --no-local-server`) handles headless-no-tunnel; it
  requires `OAUTHLIB_INSECURE_TRANSPORT=1` in the daemon environment.
- **B3 — token-revocation propagation.** `RefreshError` is mapped to
  `TokenRevokedError` in `convert_api_errors`. A `PersistingCredentials`
  wrapper writes refreshed credentials back to the keyring after every
  successful refresh. Background-thread `AuthError` surfaces as a re-link
  prompt via the existing manager wiring (Stage 2 adds a test that asserts
  this; it does not extend the wiring unless the test reveals a gap).
- **Drive scope.** `https://www.googleapis.com/auth/drive` only. Account
  identity from `about.get(fields="user(emailAddress,displayName,permissionId,photoLink)")`:
  store `permissionId` as `account_id` in config; surface `email` and
  `displayName` in the user-visible `FullAccount`.
- **`core.py` `FullAccount` shape adaptation.** Drop Dropbox-only fields
  (`team`, `team_member_id`, `locale`, `country`, `account_type`,
  `email_verified`, `root_info`) that have no analogue in Drive; keep
  `account_id`, `display_name`, `email`, `profile_photo_url`, `disabled` plus
  `SpaceUsage`. The exact field list lands in the plan, not the spec.

### 2.2 Out of scope

- **B2 OAuth verification submission.** Stage 8.
- **Drive read/write API methods** beyond the single `about.get` call. Stage
  3 (read) and Stage 4 (write).
- **HTTP error mapping** (401/403/404/429/5xx from `HttpError`). Stage 3
  owns the read-side error policy; Stage 4 owns write-side. Stage 2 owns
  only the auth-library exception surface (`RefreshError`,
  `GoogleAuthError`).
- **Transient-refresh-failure retry** (network blip mid-refresh). Stage 3
  alongside the 429/5xx retry policy.
- **GUI re-link prompt UX.** `maestral-qt` and `maestral-cocoa` are out of
  scope for the v1 port (master design §3).
- **In-memory fake-Drive (E1).** Stage 7.
- **Tests under `tests/linked/`.** Stage 3+; nothing for Stage 2 to
  exercise there.

## 3. Architecture

### 3.1 Module layout

```
src/cumulus/
  oauth.py            NEW — Flow construction, persistence-aware Credentials
                            wrapper, client_secret.json loading
  client.py           MODIFIED — replace get_auth_url/link/unlink/linked stubs;
                                 add the "load creds at startup" code path;
                                 add get_account_info via about.get
  keyring.py          MODIFIED — no API change; extend docstring to note
                                 that the stored string holds the
                                 Credentials.to_json() blob
  resources/
    client_secret.json     NEW — bundled OAuth client credentials
  cli/
    cli_core.py       MODIFIED — rewrite link_dialog for Shape A loopback;
                                 add --no-local-server flag for Shape C
                                 fallback
  errorhandling.py    MODIFIED — convert_api_errors maps RefreshError to
                                 TokenRevokedError and other auth-library
                                 exceptions to AuthError
  daemon.py           MODIFIED — set OAUTHLIB_INSECURE_TRANSPORT=1 in the
                                 daemon process environment
  core.py             MINOR — FullAccount adapted to Drive's "about" payload
tests/offline/
  test_oauth.py       NEW
  test_client_auth.py NEW (or auth coverage added to test_client.py)
  test_cli.py         MODIFIED — auth_link command scenarios
```

### 3.2 Dependency direction

`oauth.py` depends on `keyring.py` (read/write the stored blob) and `core.py`
(constants only). `client.py` depends on `oauth.py` and `errorhandling.py`.
`cli/cli_core.py` depends on `client.py` via the `Maestral` / `MaestralProxy`
facade — never on `oauth.py` directly. No cycles.

### 3.3 Why a new `oauth.py` rather than inlining into `client.py`

1. `client.py` is already the largest module in the package and grows
   significantly in Stages 3–4; isolating the OAuth flow + persistence
   logic keeps each module under ~600 lines.
2. The auth code is the only part of the system that knows how to talk to
   the OAuth endpoint (`oauth2.googleapis.com`) vs. the Drive API endpoint
   (`www.googleapis.com/drive`). The two are meaningfully different
   concerns.
3. It makes the mock surface for unit tests crisp — tests mock `oauth.py`,
   never need to mock `client.py`'s Drive calls.

### 3.4 Why `keyring.py` doesn't get an API change

The existing `CredentialStorage.token` property takes and returns an
arbitrary string. The Google `Credentials.to_json()` blob is a string.
No code change is needed in `keyring.py` itself; the change is documentary.
The Dropbox-flavored `token_access_type` config field is silently
retired (read but ignored); the actual config-schema sweep belongs to
Stage 6.

## 4. The OAuth module — `src/cumulus/oauth.py`

Pure helpers around `google_auth_oauthlib` and `google.oauth2.credentials`.
No daemon state held here — state lives on `GoogleDriveClient` instances.

### 4.1 Public surface

```python
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

def load_client_config() -> dict:
    """Resolve client_secret.json from CUMULUS_CLIENT_SECRET_PATH or the
    bundled resource. Raises AuthError on missing/malformed."""

def run_local_flow(open_browser: bool = True, port: int = 8080) -> Credentials:
    """Used by the CLI process (not the daemon). Runs
    InstalledAppFlow.run_local_server with port=8080 (predictable for
    SSH-tunnelling via 'ssh -L 8080:localhost:8080 <host>'). Raises
    AuthError with an actionable message if the port is already bound —
    the user can free the port or fall back to --no-local-server."""

def make_paste_flow() -> Flow:
    """Construct a Flow with redirect_uri='http://localhost:8080' for the
    Shape C fallback. Caller holds the Flow object for the matching
    exchange."""

class PersistingCredentials(google.oauth2.credentials.Credentials):
    """Credentials subclass whose refresh() also persists to the keyring."""

def credentials_from_refresh_token(refresh_token: str) -> Credentials:
    """Build a Credentials seeded only with a refresh_token (and the bundled
    client_id/secret). Useful for tests, CI, and the link(refresh_token=...)
    path."""

def load_credentials(cred_storage: CredentialStorage) -> PersistingCredentials | None:
    """Read keyring blob, deserialize, return None if missing/malformed."""

def save_credentials(cred_storage: CredentialStorage,
                     creds: Credentials,
                     account_id: str) -> None:
    """Serialize Credentials.to_json() and hand to cred_storage.save_creds."""

def wrap_persisting(creds: Credentials,
                    cred_storage: CredentialStorage,
                    account_id: str) -> PersistingCredentials:
    """Upgrade a plain Credentials into a PersistingCredentials bound to a
    specific cred_storage and account_id."""
```

### 4.2 `load_client_config` resolution order

```python
def load_client_config() -> dict:
    override = os.environ.get("CUMULUS_CLIENT_SECRET_PATH")
    if override:
        with open(override) as f:
            return json.load(f)
    return json.loads(
        importlib.resources.files("cumulus.resources")
        .joinpath("client_secret.json")
        .read_text()
    )
```

If neither path resolves to a readable, well-formed JSON object, raise
`AuthError("Client credentials not found", ...)`. The bundled file is part
of the distribution contract; missing it is a packaging defect, not a
runtime user error, but we want a clear error message either way.

### 4.3 `PersistingCredentials` — refresh persistence

```python
class PersistingCredentials(Credentials):
    def __init__(self, *args, cred_storage=None, account_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._cred_storage = cred_storage
        self._account_id = account_id

    def refresh(self, request):
        super().refresh(request)
        # On success the parent updated self.token / self.expiry.
        # Persist; RefreshError is allowed to propagate unhandled.
        save_credentials(self._cred_storage, self, self._account_id)
```

This is the entire "persist refreshed creds" half of B3.
`googleapiclient.discovery.build(credentials=creds)` calls `creds.refresh()`
automatically on 401, so our override piggybacks on that without any wrapper
at every API-call site. `RefreshError` propagates; the API-call site's
`convert_api_errors` maps it to `TokenRevokedError` (§6).

### 4.4 Storage format

Stored verbatim in the keyring as the JSON string produced by
`Credentials.to_json()`:

```json
{"token": "...", "refresh_token": "...", "token_uri": "...",
 "client_id": "...", "client_secret": "...", "scopes": ["..."],
 "expiry": "..."}
```

`client_secret` redundancy with the bundled `client_secret.json` is
acceptable — there is no new exposure (the file ships in the source tree
already) and the round-trip is lossless. The Stage 8 D5 credential-rotation
note will record that stored blobs from a prior `client_secret.json` keep
working because their embedded `client_secret` is what's used at refresh
time.

`load_credentials` deserializes via `Credentials.from_authorized_user_info`
and upgrades the result via `wrap_persisting` so the returned object
already persists on refresh.

## 5. Daemon-side `client.py` auth API

### 5.1 `linked` property

```python
@property
def linked(self) -> bool:
    if self._credentials is None:
        self._credentials = oauth.load_credentials(self._cred_storage)
    return (
        self._credentials is not None
        and self._credentials.refresh_token is not None
    )
```

Cached after first call. The `refresh_token is not None` clause is
load-bearing: a credential blob with only an access_token can't survive its
first expiry and is functionally not linked.

### 5.2 `get_auth_url()` — Shape C fallback only

```python
def get_auth_url(self) -> str:
    flow = oauth.make_paste_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent"
    )
    self._pending_paste_flow = _PasteFlowState(flow=flow, created_at=now())
    return auth_url
```

`access_type="offline"` is what asks for a refresh_token in the first place.
`prompt="consent"` forces the consent screen on re-link, which is what
guarantees Google returns a refresh_token (Google omits it on subsequent
grants without re-consent).

`_PasteFlowState` is a daemon-process-memory dataclass holding the `Flow`
(it carries the PKCE verifier between the URL call and the exchange call)
plus a `created_at` for the 10-minute expiry. Only one flow at a time;
calling `get_auth_url` again replaces the prior pending flow. Cleared on
success, on `unlink`, and on expiry.

### 5.3 `link(code | refresh_token | access_token)`

```python
def link(self, code=None, refresh_token=None, access_token=None) -> int:
    try:
        if refresh_token is not None:
            creds = oauth.credentials_from_refresh_token(refresh_token)
            creds.refresh(Request())  # validate it
        elif code is not None:
            state = self._pending_paste_flow
            self._pending_paste_flow = None
            if state is None or (now() - state.created_at) > timedelta(minutes=10):
                return 1
            state.flow.fetch_token(code=code)
            creds = state.flow.credentials
        elif access_token is not None:
            creds = Credentials(token=access_token, scopes=oauth.DRIVE_SCOPES)
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

Return-code contract preserved (0 / 1 / 2) so the existing CLI dialog's
branching logic doesn't need to change.

`_fetch_account_info_for_creds(creds)` does one Drive
`about.get(fields="user(emailAddress,displayName,permissionId,photoLink),storageQuota")`
call. It validates the credentials work for Drive and supplies the
`account_id` (= `permissionId`). The returned `FullAccount` is cached and
served by subsequent `get_account_info()` calls.

### 5.4 `unlink()`

```python
def unlink(self) -> None:
    creds = self._credentials
    if creds is not None and creds.refresh_token:
        try:
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": creds.refresh_token},
                timeout=self._timeout,
            )
        except CONNECTION_ERRORS:
            pass  # local cleanup still proceeds; caller logs via main.py
    self._credentials = None
    self._pending_paste_flow = None
    self._cached_account_info = None
```

The revoke call is what makes server-side unlink actually take effect —
without it the refresh_token stays valid on Google's side and could be
reused if leaked. Best-effort: a failed revoke logs in `Maestral.unlink`
(existing path) and does not block local cleanup. Keyring + config
cleanup happens in `Maestral.unlink`, which already wires `delete_creds`
and `_conf.cleanup`.

### 5.5 Credential loading at daemon startup

`__init__` does **not** eagerly load credentials. The first access of
`self.linked` (or `_get_service()`) triggers lazy
`oauth.load_credentials(...)`. Matches the existing keyring-laziness
pattern; avoids blocking daemon startup on keyring unlock.

`_get_service()` is a small helper used by every Drive API method in
Stage 3+:

```python
def _get_service(self):
    if not self.linked:
        raise NotLinkedError("Not linked", "Run 'cumulus auth link'.")
    return build(
        "drive", "v3",
        credentials=self._credentials,
        cache_discovery=False,
    )
```

Stage 2 only uses `_get_service()` inside `_fetch_account_info_for_creds`,
but it's the auth surface every later stage builds on, so it lands here.

## 6. CLI flow & error mapping

### 6.1 Rewritten `auth_link` command

The existing `auth_link` command in `cli/cli_core.py` is rewritten with two
new helpers replacing the old `link_dialog`:

```python
@auth.command(name="link")
@click.option("--relink", "-r", is_flag=True)
@click.option("--no-local-server", is_flag=True,
              help="Use paste-the-redirect-URL flow (for headless users "
                   "without SSH port-forwarding).")
@click.option("--refresh-token", "refresh_token", default=None, hidden=True)
@click.option("--access-token", "access_token", default=None, hidden=True)
@existing_config_option
def auth_link(m, relink, no_local_server, refresh_token, access_token):
    if not m.pending_link and not relink:
        echo("Already linked. Use '-r' to relink.")
        return
    if refresh_token or access_token:
        res = m.link(refresh_token=refresh_token, access_token=access_token)
    elif no_local_server:
        res = _paste_redirect_dialog(m)
    else:
        res = _local_server_dialog(m)
    _report_link_result(m, res)
```

**`_local_server_dialog(m)` — default Shape A path.** Runs
`oauth.run_local_flow()` *in the CLI process*. The CLI opens the browser
and captures the loopback redirect; daemon never sees the auth URL. On
success the resulting `Credentials` has a `refresh_token` — extract it and
call `m.link(refresh_token=refresh_token)`. Before opening the browser,
the CLI prints:

```
A browser will open to authorize Cumulus to access your Google Drive.
If you're on a headless server, run on your laptop first:
    ssh -L 8080:localhost:8080 <your-host>
```

**`_paste_redirect_dialog(m)` — Shape C fallback.** Calls
`m.get_auth_url()`, prints the URL plus instructions ("after authorizing,
your browser will fail to connect to a `localhost` URL — **copy that URL
from the address bar** and paste it here"), reads the pasted URL or code,
and calls `m.link(code=parsed_code)`. A small `_parse_redirect_url_or_code`
helper handles both shapes — full `http://localhost:8080/?code=4/...` URLs
and bare codes.

`_report_link_result(m, res)` is the existing 0/1/2 → user-facing-message
branching extracted into a helper for reuse between the two dialog paths.

### 6.2 Daemon-environment requirement for Shape C

The daemon process must have `OAUTHLIB_INSECURE_TRANSPORT=1` set for the
paste-redirect token exchange to succeed. Without it, `oauthlib` rejects
the HTTP `redirect_uri=http://localhost:8080` at exchange time with
`InsecureTransportError` (spike Q1 confirmed). Stage 2 sets this
unconditionally in `daemon.py:start_cumulus_daemon` before any other
imports of oauthlib. It only relaxes oauthlib's `localhost`-over-HTTP rule
and has no security consequence for the live HTTPS Drive API calls.

### 6.3 Error mapping — `errorhandling.py`

The `convert_api_errors` decorator gets two new branches:

```python
except google.auth.exceptions.RefreshError as exc:
    raise TokenRevokedError(
        "Authentication revoked",
        "Your Google account has been disconnected. "
        "Run 'cumulus auth link -r' to re-link.",
    ) from exc

except google.auth.exceptions.GoogleAuthError as exc:
    raise AuthError("Authentication error", str(exc)) from exc
```

`RefreshError` is the only auth-library exception that means *the user
must intervene* — the rest (`DefaultCredentialsError`, etc.) are programmer
errors and map to a generic `AuthError`. HTTP `HttpError` mappings (401 /
403 / 404 / 429 / 5xx) belong to Stage 3; Stage 2 owns only the
auth-library exception surface.

### 6.4 Background-thread propagation

The existing `_handle_sync_thread_errors` in `manager.py` already catches
`AuthError` and pauses sync. Stage 2's task is **verify, not extend**: add
a test asserting that a `TokenRevokedError` raised from a mock background
API call pauses sync, sets the user-visible status to a re-link message,
and does not retry-loop the failed call. If the existing path doesn't do
all three, the missing wiring lands in Stage 2 (not Stage 5). Looking at
the existing skeleton this is likely already correct for `AuthError`
because Dropbox surfaces the same class, but the test pins it as a
contract.

## 7. Tests

### 7.1 `tests/offline/test_oauth.py` (new)

Mock OAuth HTTP endpoints with `responses`. No real network.

- **`load_client_config` resolution order.** Bundled file by default.
  `CUMULUS_CLIENT_SECRET_PATH` env var overrides. Missing-everywhere raises
  `AuthError` (not raw `FileNotFoundError`).
- **`PersistingCredentials.refresh` persists.** Mock token endpoint
  returns a refreshed access_token; assert `cred_storage.save_creds` is
  called with the JSON blob containing the new token.
- **`PersistingCredentials.refresh` propagates `RefreshError`.** Mock
  token endpoint returns 400 `invalid_grant`; assert raises
  `RefreshError`; assert `cred_storage.save_creds` was **not** called.
- **`load_credentials` round-trip.** Save a `Credentials` blob via
  `save_credentials`; `load_credentials` returns an equivalent
  `PersistingCredentials` with the same `refresh_token` / `scopes`.
- **`load_credentials` malformed-blob handling.** Keyring returns a
  truncated / non-JSON / wrong-shape string; returns `None`, does not
  raise.

### 7.2 `tests/offline/test_client_auth.py` (new) — or extending `test_client.py`

Mock `oauth.run_local_flow` and the `about.get` Drive call.

- `linked` is False when keyring is empty.
- `linked` is False when keyring blob lacks `refresh_token`.
- `linked` is True after `link(refresh_token=...)` happy path.
- `link(refresh_token=...)` happy path validates by calling `about.get`,
  stores credentials keyed by `permissionId`, sets `_cached_account_info`.
- `link(refresh_token=...)` rejects revoked token: mock token endpoint
  returns `invalid_grant`; assert returns `1`; assert no keyring write.
- `link(refresh_token=...)` returns `2` on connection error.
- `link(code=...)` requires a prior `get_auth_url`: calling with no
  pending flow returns `1`.
- `link(code=...)` rejects an expired pending flow: `created_at` is
  11 min ago; returns `1`.
- `unlink` clears in-memory state and best-effort revokes. Mock the
  revoke endpoint; assert called. Mock it to fail; assert `unlink` still
  clears local state.

### 7.3 `tests/offline/test_cli.py` (extend)

- **Default flow:** mock `oauth.run_local_flow` to return a fake
  `Credentials`; invoke `cumulus auth link`; assert `m.link(refresh_token=...)`
  is called with the refresh_token from the fake credentials.
- **`--no-local-server` flow:** mock `m.get_auth_url`; pipe a fake
  redirect URL on stdin; assert `m.link(code=...)` is called with the
  parsed code value. Test both input shapes — full
  `http://localhost:8080/?code=...` URL and bare code.

### 7.4 Manual smoke test (master design §7)

Recorded as a checklist file alongside the stage plan; not in CI. Run once
against the real Google account when implementation is complete:

1. `cumulus auth link` (Shape A) — browser opens, consent shown, "Linked
   to <email>" message.
2. `cumulus auth link -r --no-local-server` — relink via Shape C
   paste-the-redirect-URL.
3. Restart the daemon; verify it loads credentials and does not prompt
   for re-link.
4. From `accounts.google.com` security settings, revoke the Cumulus
   app's access.
5. Trigger an API call (any path that invokes `_get_service` — for
   Stage 2 alone this can be a synthesized call from a test harness or
   manual `python -c`). Verify the error surfaces as "re-link needed"
   status rather than a stack trace.
6. `cumulus auth unlink` — verify keyring cleared and config's
   `auth/account_id` empty.

### 7.5 Definition of "Stage 2 done"

1. This spec committed under `docs/superpowers/specs/`.
2. Implementation plan committed under `docs/superpowers/plans/`.
3. All code merged to `google-drive-port`.
4. New offline tests pass; existing offline tests still pass. The
   pre-existing test failures in `test_manager.py`, `test_main.py`,
   `test_event_handler.py`, `test_daemon.py` remain Stage 3/5/6 work and
   are not Stage 2's responsibility — Stage 2 must not regress beyond
   that baseline.
5. Manual smoke test §7.4 completed; result recorded in the plan's
   deviations file.

## 8. Risk register

| Risk | Mitigation |
|---|---|
| Testing-mode 7-day refresh-token expiry surprises early users | Document prominently in README + setup guide; Stage 8 (B2) verification clears the limitation |
| `client_secret.json` shipped in repo confuses contributors as "secret" | Setup guide explains that for installed-app clients Google does not treat client_secret as confidential |
| `OAUTHLIB_INSECURE_TRANSPORT=1` set in daemon environment misread as a vulnerability | Inline comment at the set-site explains it only relaxes localhost-over-HTTP for the OAuth-library callback; HTTPS Drive calls are unaffected |
| Daemon-side paste-flow state grows stale on idle daemons | Single-flow-at-a-time; 10-minute expiry; cleared on `unlink` |
| google-auth library version drift breaks `Credentials.to_json()` shape | Pinned minimum version in `pyproject.toml` already; round-trip test asserts shape stability |
| Local port 8080 already bound when running `cumulus auth link` | Catch `OSError` from `run_local_server`, raise `AuthError("Port 8080 in use", "Free the port or run 'cumulus auth link --no-local-server'.")` |
| GUI packages break silently from `Maestral.get_auth_url` / `link` semantic shift | Out of scope per master plan §3; the GUI compat decision is Stage 5 (D4) |

## 9. Open questions

None blocking. The following will be resolved during implementation, not
deferred:

- Exact `_report_link_result` message wording (cosmetic; lands in plan).
- Whether `core.py` field removal happens in this stage or as part of a
  cross-cutting `models.py` sweep in Stage 5. Default: in this stage,
  because the surface is touched here regardless.

## 10. Next step

Invoke the `superpowers:writing-plans` skill to draft the Stage 2
implementation plan under `docs/superpowers/plans/2026-05-12-stage-2-auth-onboarding.md`.
