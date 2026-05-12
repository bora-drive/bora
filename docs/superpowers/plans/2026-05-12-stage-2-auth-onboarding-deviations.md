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
