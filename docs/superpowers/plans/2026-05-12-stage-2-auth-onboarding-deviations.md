# Stage 2 — Plan deviations & manual smoke test

Records two things, both required by the staged-port workflow:
1. Deviations the implementer made from the plan during execution
   (with rationale).
2. The result of the manual smoke test required by master design §7.

## Plan deviations

- **Task 2 fix-up (commit `a79fdd8`):** Code-quality review caught four
  issues in the initial Task 2 commit (`334985f`): (a) `ModuleNotFoundError`
  from a missing `cumulus.resources` package wasn't caught and would escape
  as an unhandled exception; (b) the test suite didn't cover the
  bundled-resource-missing path, which the spec explicitly called out as
  required coverage; (c) a `PermissionError` (subclass of `OSError`) on
  the override path was misrouted to the "malformed" error branch; (d)
  `open(override)` didn't specify `encoding="utf-8"`. All four addressed
  in `a79fdd8` with no further regressions. The exception handler
  ordering became: `(FileNotFoundError, ModuleNotFoundError)` →
  "not found", `json.JSONDecodeError` → "malformed", `OSError` → "could
  not read".

- **Task 8 scope creep + fix-up (commits `efd2789` then `4a6b02b`):** The
  initial Task 8 implementer modified `tests/offline/test_errorhandling.py`
  to wrap the pre-existing Dropbox-based tests in `try/except` and
  `if _DROPBOX_AVAILABLE:` guards so the file would collect. The plan and
  project rules forbid touching the pre-existing broken test files (they
  belong to Stages 3/5/6). The follow-up commit `4a6b02b` reverted those
  changes and moved the new Google auth tests to a dedicated file
  `tests/offline/test_errorhandling_auth.py`. The legacy file is back to
  its broken-collection state, which is intentional. Also: commit
  `efd2789` used `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`
  instead of the project-standard `Claude Opus 4.7 (1M context)` trailer
  — a one-off mismatch that is now part of the permanent git history; the
  follow-up `4a6b02b` uses the correct trailer.

- **Task 10 dead-import cleanup (commit `9155d4d`):** Stubbing the body
  of `manager._update_path_root` to `raise NotImplementedError(...)` left
  nine imports orphaned at the top of `manager.py`
  (`TemporaryDirectory`, `TeamRootInfo`, `UserRootInfo`, `removeprefix`,
  `delete`, `is_child`, `is_equal_or_child`, `move`, `normalize`). The
  Task 10 implementer left them in place; a follow-up commit removed them
  as a small chore. No behavior change.

- **Task 16 test deviation:** The plan's test snippet used
  `fake_flow.credentials = fake_creds` to inject credentials into a mocked
  `Flow` object. `Flow.credentials` is a read-only `@property`, so direct
  attribute assignment raises `AttributeError`. The implementer used
  `mock.patch.object(type(fake_flow), "credentials", new_callable=mock.PropertyMock, return_value=fake_creds)`
  instead. Test-correctness fix only; production code is unchanged.

- **Task 20 test approach:** The plan provided two acceptable approaches
  for testing the `auth_link` dispatch path: invoking via
  `auth_link.callback(...)` directly, or falling back to `CliRunner`. The
  implementer used the `CliRunner` fallback (explicitly authorized by the
  plan) because the `@inject_proxy` decorator made direct callback
  invocation awkward. A `_make_proxy_mock()` test helper was added to keep
  the tests readable.

- **Task 21 outcome:** The pre-existing generic `except Exception:` branch
  in `manager._handle_sync_thread_errors` already handles `AuthError` /
  `TokenRevokedError` correctly via `getattr(err, "title", ...)` — no
  explicit `except AuthError:` branch was needed. Stage 2 added the test
  (`tests/offline/test_manager_auth.py`) to pin the behavior but did NOT
  modify `manager.py`.

- **Task 1 follow-up recommendation (not yet addressed):** The code-quality
  reviewer for Task 1 recommended adding a `.gitignore` comment marking
  `src/cumulus/resources/client_secret.json` as intentionally tracked, so
  future contributors don't accidentally commit replacement credentials.
  Recorded here as a Stage-8-release-prep item rather than addressing
  mid-stage.

## Deferred minor items (not blocking Stage 2 completion)

- `datetime.utcnow()` is used at four call sites (oauth.py refresh test
  helper, client.py `get_auth_url` and `link(code=...)` paths, test file).
  Python 3.12 emits a deprecation warning; future Python will raise. The
  spec snippet used `datetime.utcnow()` so this was carried through
  faithfully. Replace with `datetime.now(timezone.utc)` before the port
  ships.

- A handful of test-file imports landed mid-file rather than at the top of
  their respective modules (`tests/offline/test_oauth.py`,
  `tests/offline/test_client_auth.py`, `tests/offline/test_cli.py`). PEP 8
  violation only, no functional impact.

- `_PasteFlowState.flow: "Any"` (quoted) in `client.py` instead of
  `flow: Any` (unquoted). Equivalent at runtime under
  `from __future__ import annotations`.

- The `from cumulus.cli.cli_core import auth_link` import in the Task 20
  tests is unused (the tests invoke via CliRunner). Harmless leftover.

- Task 7's `OSError` catch in `run_local_flow` is broader than the
  "port-in-use" error message implies — a non-`EADDRINUSE` socket error
  would surface the misleading message. The plan permits the broad catch;
  narrowing or rewording the message can wait for Stage 3 error-handling.

## Manual smoke test

Run against a real Google account (`cumulus@arko.net`, project `cumulus`)
on 2026-05-12, on the exe.dev Linux VM. All 5 items passed; three Stage 2
fix-up commits landed during execution (recorded above the checklist).

- [x] **1. Default flow (Shape A).** `CUMULUS_AUTH_PORT=18080 cumulus auth link`
  with an SSH tunnel `ssh -L 18080:localhost:18080 <vm>` from the user's
  laptop. Auth URL printed; user opened in laptop browser; consent
  approved; loopback captured the redirect via the tunnel; result:
  `✓ Linked to cumulus@arko.net`. Two issues surfaced and were fixed
  during this step (see "Smoke-test discoveries" below).
- [x] **2. Relink with Shape C (`--no-local-server`).** User ran
  `cumulus auth link -r --no-local-server` in an interactive SSH session
  (background mode crashes the survey-based prompt with a TTY error —
  a Stage 2 limitation noted but not in scope to fix). Pasted the full
  redirect URL back; parser extracted the code; result:
  `✓ Linked to cumulus@arko.net`. Validates `_paste_redirect_dialog`,
  `_parse_redirect_url_or_code` on a full URL with `code=` param, the
  daemon-side `_PasteFlowState` lifecycle, the `link(code=...)` inline
  branch (Task 16), and crucially the `OAUTHLIB_INSECURE_TRANSPORT=1`
  daemon env var (Task 9) — without it the `http://localhost:8080`
  redirect_uri would have been rejected at exchange time.
- [x] **3. Persistence across restarts.** Fresh Python process
  (`python3 -c "from cumulus.client import GoogleDriveClient; ..."`)
  loaded the persisted keyring blob, googleapiclient auto-refreshed
  the access token, returned correct `account_id=17820054878047320812`,
  `email=cumulus@arko.net`, `display_name=Cumulus Tester`, and storage
  quota (29 GB / 166 GB). `cumulus auth status` partially worked
  (Remote ID populated correctly; Email/Account-type blank because
  the CLI reads from `state["account"]["email"]` which Stage 2 does
  not write — same gap as `_report_link_result` pre-fix; Stage 3+
  concern).
- [x] **4. Token revocation.** User revoked at
  https://myaccount.google.com/permissions. Smoke harness
  (`/tmp/smoke_step4.py` — uses `convert_api_errors` around
  `c.get_account_info()` after nulling `c._credentials.token` to force
  a refresh) raised:
  ```
  PASS: TokenRevokedError raised
    title: Authentication revoked
    message: Your Google account has been disconnected. Run 'cumulus auth link -r' to re-link.
  ```
  Full B3 chain verified (`PersistingCredentials.refresh` propagating
  `RefreshError`, `convert_api_errors` mapping it, exception class and
  message match spec).
- [x] **5. Unlink.** `cumulus auth unlink -Y` output:
  `✓ Credentials removed`, `✓ Unlinked Maestral.` Post-state verified:
  config `auth/account_id` reset to `''`, `auth/keyring` reset to
  `automatic`, `cred_storage.account_id` returns `None`, `cred_storage.keyring`
  is `None`, `client.linked` returns `False`. Best-effort token revoke
  in `client.unlink()` was attempted (token already revoked in step 4 —
  any response is acceptable).

Stage 2 is done per master design §7.

## Smoke-test discoveries (fixed inline during smoke testing 2026-05-12)

Three Stage 2 bugs surfaced during smoke testing and were fixed before
declaring the stage complete:

- **`webbrowser.open()` raises on headless systems (commit `8cb0c8c`):**
  `_local_server_dialog` called `oauth.run_local_flow(port=8080)` with
  default `open_browser=True`. On the headless exe.dev VM, no browser
  is installed, so `webbrowser.open()` raised `webbrowser.Error("could
  not locate runnable browser")`, which the dialog's broad
  `except Exception` swallowed as "Invalid authorization, please try
  again." — without ever showing the user the auth URL. The dialog
  already instructs users to open the URL in their own browser (the
  SSH-tunnel pattern), so launching `webbrowser` on the CLI host was
  never useful. Fix: pass `open_browser=False` unconditionally; the
  Google library's `authorization_prompt_message` still prints the URL.

- **exe.dev auto-proxy probes port 8080 and trips CSRF check
  (commit `40f37f2`):** As soon as Shape A bound the local server on
  port 8080, exe.dev's HTTP-forwarding feature (which auto-proxies any
  port below 9999) made an unauthenticated GET to the loopback URL.
  google-auth-oauthlib's WSGI handler treated it as the OAuth callback,
  compared the absent/wrong `state` parameter to the one we registered,
  and raised `(mismatching_state) CSRF Warning!` — completing the flow
  with a failure before the user could even open the URL in their
  browser. Fix: add `CUMULUS_AUTH_PORT` env var so users can choose a
  port outside the auto-probe range (we used 18080 for the smoke test,
  with SSH tunnel `ssh -L 18080:localhost:18080`). Default stays at
  8080 for environments without auto-probing.

- **Circular import in `keyring.py` (commit `246391a`):** Task 4 of
  the original plan added `from .keyring import CredentialStorage` to
  `oauth.py` (so `save_credentials` / `load_credentials` could be
  typed). `keyring.py` already had `from .cli import output` at the
  top, which loads `cli_core`, which (after Task 18) loads `oauth.py`,
  which tries to load `keyring.py` — circular. The cycle happens to
  work when the `cumulus` CLI entry point drives the import order,
  but any consumer that imports `cumulus.keyring` directly (the
  daemon, ad-hoc scripts, our step 3 smoke harness) hit
  `ImportError: cannot import name 'CredentialStorage' from partially
  initialized module 'cumulus.keyring'`. Fix: move `from .cli import
  output` inside the two methods that use it (`save_creds`,
  `delete_creds`) so it's lazy-imported.

All three fixes are localized, tested (213 offline tests still pass
unchanged), and don't alter the public surface of any module. None of
them invalidate prior task reviews — they're stage-completion fix-ups
exposed only by end-to-end smoke testing.
