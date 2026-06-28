# Stage 8 — Docs + Release (rebrand to Bora) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the Google-Drive port from a working branch to a releasable, single-named project ("Bora"): fix the two W0 release blockers, rename `cumulus`/`Maestral` → `bora`/`Bora`, remove backward-compat aliases, fix and build the docs, and write the release checklist.

**Architecture:** Land the two correctness fixes first on the known-green tree (account-state persistence; cut the stubbed `sharelink`), then perform the mechanical rename in three reviewable passes (package path → app identity → class names), then alias removal, then docs/packaging/checklist. The full offline test suite (`pytest tests/offline -m "not load"`) is the regression gate after every code task.

**Tech Stack:** Python 3.10+, setuptools/pyproject, Pyro5 daemon, google-api-python-client, click CLI, pytest, Sphinx + sphinx-autoapi (furo theme).

## Global Constraints

Copied verbatim from the spec (`docs/superpowers/specs/2026-06-28-stage-8-docs-release-design.md`). Every task implicitly includes these.

- Product/brand name: **Bora**; CLI command/console-script: `bora`; top-level package: `bora`; distribution `name`: `bora`.
- Main class: `Bora` (was `Maestral`); daemon proxy: `BoraProxy` (was `MaestralProxy`).
- `APP_NAME = "Bora"`; `BUNDLE_ID = "tools.bora.app"`; `CONFIG_DIR_NAME = "bora"`.
- Test env vars: `BORA_TEST_REFRESH_TOKEN`, `BORA_TEST_ACCESS_TOKEN`, `BORA_AUTH_PORT`, `BORA_CLIENT_SECRET_PATH` (was `CUMULUS_*`); shell-completion var becomes `BORA_COMPLETE` automatically from the prog name.
- GUI extras: `bora-qt`, `bora-cocoa`. Icon resource: `bora.png`. Homepage URL: `https://bora.tools`. Version: `0.1.0`.
- **Preserve as attribution (do NOT scrub):** `LICENSE.txt` © 2018 Sam Schott; README "fork of Maestral" + link to `https://github.com/samschott/maestral`; accurate technical-comparison comments (e.g. "Dropbox used a long-poll; Drive polls").
- **Do NOT rewrite history:** leave `docs/superpowers/specs/` and `docs/superpowers/plans/` Stage 0–7 records under their then-names. Only shipped/forward-looking surfaces are renamed.
- Drive sharing API is **deferred** to post-v0.1.0 (do not implement `sharelink`).
- Regression gate after each code task: `pytest tests/offline -m "not load"` green; `mypy src/` and `flake8 src/` parity with the pre-stage baseline. Per the project's convention the controller (not a subagent) runs the suite; a smoke-test account must NOT be linked while it runs (currently none is).

---

## File Structure

**Touched by the correctness fixes (Tasks 1–2):**
- `src/cumulus/client.py` — persist account state; remove shared-link stub methods.
- `src/cumulus/main.py` — remove shared-link forwarder methods.
- `src/cumulus/cli/cli_core.py` — remove `sharelink` command group.
- `src/cumulus/cli/cli_main.py` — unregister `sharelink`.
- `tests/offline/test_client_auth.py` — new account-state test.
- `tests/offline/test_cli.py` — remove sharelink CLI tests.
- `tests/linked/unit/test_client.py` — remove shared-link client tests (linked suite, not in offline gate).

**Touched by the rename (Tasks 3–5):** effectively the whole tree (`src/cumulus/` → `src/bora/`, all of `tests/`, `pyproject.toml`, `docs/conf.py`, `docs/index.rst`).

**Touched by docs/housekeeping/packaging (Tasks 6–11):** `docs/conf.py`, `docs/index.rst`, `README.md`, `CHANGELOG.md`, `PLAN.md`, `TODO.md`, new `docs/setup.md`, new `docs/differences.md`, new `docs/release-checklist.md`, new `packaging/bora.desktop`.

---

## Task 1: Persist account info to state (W0.5 blocker 2)

Fix: `status` / `auth status` / `config-files` show `--` because the Drive port never writes `email`/`type`/`usage` to the `account` state section. Persist them when account info is fetched.

**Files:**
- Modify: `src/cumulus/client.py` (method `_fetch_account_info_for_creds`, ~line 536; imports near top)
- Test: `tests/offline/test_client_auth.py`

**Interfaces:**
- Consumes: existing `client._state` (a `CumulusState`, has `.set(section, option, value)` and `.get(section, option)`); `_fake_about_response()` test helper; `client` / `cred_storage` fixtures; `_link_to_perm_x(client, cred_storage, monkeypatch)` helper — all already in `test_client_auth.py`.
- Produces: after `get_account_info()` / `get_space_usage()`, the `account` state section contains keys `email`, `display_name`, `type` (`"personal"`), `usage` (formatted string), `usage_type` (`"individual"`).

- [ ] **Step 1: Write the failing test**

Add to `tests/offline/test_client_auth.py` (after `test_get_space_usage`):

```python
def test_fetch_account_info_persists_state(client, cred_storage, monkeypatch):
    """Account email/type/usage must be written to the `account` state section
    so the CLI (status / auth status / config-files) can display them."""
    _link_to_perm_x(client, cred_storage, monkeypatch)
    assert client.linked is True
    fake_service = mock.MagicMock()
    fake_service.about().get().execute.return_value = _fake_about_response()

    with mock.patch("cumulus.client.build", return_value=fake_service):
        client.get_account_info()
        client.get_space_usage()

    assert client._state.get("account", "email") == "alice@example.com"
    assert client._state.get("account", "display_name") == "Alice Example"
    assert client._state.get("account", "type") == "personal"
    usage = client._state.get("account", "usage")
    assert usage and "used" in usage
    assert client._state.get("account", "usage_type") == "individual"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/offline/test_client_auth.py::test_fetch_account_info_persists_state -v`
Expected: FAIL — `assert None == 'alice@example.com'` (state key not written).

- [ ] **Step 3: Add the `natural_size` import**

At the top of `src/cumulus/client.py`, with the other `from .` imports, add:

```python
from .utils import natural_size
```

(If a `from .utils import ...` line already exists, append `natural_size` to it instead.)

- [ ] **Step 4: Persist state in `_fetch_account_info_for_creds`**

In `src/cumulus/client.py`, locate the end of `_fetch_account_info_for_creds`, just before `return FullAccount(...)`. The method already has `user = response.get("user", {})` and `quota = response.get("storageQuota", {})` and sets `self._cached_space_usage`. Immediately before the `return FullAccount(...)` add:

```python
        # Persist account info to the `account` state section so the CLI
        # (status / auth status / config-files) can display it. Drive has no
        # Basic/Pro/Business tiers, so type is always "personal".
        used = int(quota.get("usage", 0))
        allocated = int(quota.get("limit", 0))
        if allocated:
            usage_str = f"{used / allocated:.1%} of {natural_size(allocated)} used"
        else:
            usage_str = f"{natural_size(used)} used"
        self._state.set("account", "email", user.get("emailAddress", ""))
        self._state.set("account", "display_name", user.get("displayName", ""))
        self._state.set("account", "type", "personal")
        self._state.set("account", "usage", usage_str)
        self._state.set("account", "usage_type", "individual")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/offline/test_client_auth.py::test_fetch_account_info_persists_state -v`
Expected: PASS.

- [ ] **Step 6: Run the client-auth + CLI-info test modules**

Run: `pytest tests/offline/test_client_auth.py tests/offline/test_cli.py -q`
Expected: all pass (no regression in caching/space-usage tests).

- [ ] **Step 7: Commit**

```bash
git add src/cumulus/client.py tests/offline/test_client_auth.py
git commit -m "fix(8-w0.5): persist account email/type/usage to state (status display)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Cut the `sharelink` command group (W0.5 blocker 1)

`sharelink create|revoke|list` are stubs that raise uncaught `NotImplementedError`. Remove the CLI group and the underlying client/main shared-link methods. Leave `share_dir` (guarded by `is_team_space`, hardcoded `False`, so unreachable) and the `SharedLinkMetadata` dataclass (reserved for the deferred sharing API) in place.

**Files:**
- Modify: `src/cumulus/cli/cli_main.py:5,35` (import + registration)
- Modify: `src/cumulus/cli/cli_core.py:14` (import) and the `sharelink` group block (~lines 472–552)
- Modify: `src/cumulus/main.py:55` (import) and methods `create_shared_link`/`revoke_shared_link`/`list_shared_links` (~1382–1450)
- Modify: `src/cumulus/client.py:65` (import) and methods `create_shared_link`/`revoke_shared_link`/`list_shared_links` (~1286–1325)
- Test: `tests/offline/test_cli.py` (remove sharelink tests); `tests/linked/unit/test_client.py` (remove shared-link tests)

**Interfaces:**
- Produces: `bora`/`cumulus --help` no longer lists `sharelink`; no public `create_shared_link`/`revoke_shared_link`/`list_shared_links` on `Bora`/`GoogleDriveClient`. `SharedLinkMetadata` (core.py) and `share_dir` (client.py) remain defined but unreferenced by the CLI.

- [ ] **Step 1: Write the failing test (sharelink is gone from the CLI)**

Add to `tests/offline/test_cli.py`:

```python
def test_sharelink_command_removed():
    """sharelink is cut for v0.1.0 (Drive sharing deferred); it must not be
    registered, so invoking it errors as an unknown command."""
    from click.testing import CliRunner
    from cumulus.cli.cli_main import main

    result = CliRunner().invoke(main, ["sharelink", "list"])
    assert result.exit_code != 0
    assert "No such command 'sharelink'" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/offline/test_cli.py::test_sharelink_command_removed -v`
Expected: FAIL — `sharelink` is still registered (no "No such command").

- [ ] **Step 3: Unregister the group in `cli_main.py`**

In `src/cumulus/cli/cli_main.py`:
- Line 5: remove `sharelink` from `from .cli_core import auth, gui, pause, resume, sharelink, start, stop` → `from .cli_core import auth, gui, pause, resume, start, stop`.
- Line 35: delete the whole line `main.add_command(sharelink, section="Core Commands")`.

- [ ] **Step 4: Remove the group from `cli_core.py`**

In `src/cumulus/cli/cli_core.py`:
- Line 14: change `from ..core import FolderMetadata, SharedLinkMetadata` → `from ..core import FolderMetadata` (`FolderMetadata` is still used at line 264; `SharedLinkMetadata` is not after removal).
- Delete the entire `@click.group(help="Create and manage shared links.")` block: the `sharelink` group definition and its three subcommands `sharelink_create`, `sharelink_revoke`, `sharelink_list` (from the `@click.group` decorator above `def sharelink()` through the end of `sharelink_list`). Verify nothing below it depends on those symbols.

- [ ] **Step 5: Remove the forwarders in `main.py`**

In `src/cumulus/main.py`:
- Delete the methods `create_shared_link` (~1382), `revoke_shared_link` (~1420), and `list_shared_links` (~1434) in full.
- Line 55: remove `SharedLinkMetadata,` from the `from .core import (...)` block (confirm it has no other use in `main.py` first with a grep; if it does, keep it).

- [ ] **Step 6: Remove the stub methods in `client.py`**

In `src/cumulus/client.py`:
- Delete `create_shared_link` (~1286), `revoke_shared_link` (~1308), and `list_shared_links` (~1315) in full.
- Line 65: remove `SharedLinkMetadata,` from the `from .core import (...)` block (confirm no other use in `client.py`; `share_dir` returns `FolderMetadata`, not `SharedLinkMetadata`).

- [ ] **Step 7: Remove now-dead sharelink tests**

- In `tests/offline/test_cli.py`: delete any test exercising `sharelink create/revoke/list` (search the file for `sharelink`). Keep the new `test_sharelink_command_removed`.
- In `tests/linked/unit/test_client.py`: delete tests calling `create_shared_link`/`revoke_shared_link`/`list_shared_links` (search for `shared_link`).

- [ ] **Step 8: Run the test to verify it passes + check imports**

Run:
```bash
pytest tests/offline/test_cli.py::test_sharelink_command_removed -v
python -c "import cumulus.cli.cli_main, cumulus.main, cumulus.client, cumulus.cli.cli_core"
flake8 src/cumulus/cli/cli_core.py src/cumulus/main.py src/cumulus/client.py src/cumulus/cli/cli_main.py
```
Expected: test PASS; import OK; flake8 clean (no unused-import F401 for `SharedLinkMetadata`).

- [ ] **Step 9: Run the offline gate**

Run: `pytest tests/offline -m "not load" -q`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add src/cumulus/ tests/offline/test_cli.py tests/linked/unit/test_client.py
git commit -m "feat(8-w0.5): cut sharelink command group for v0.1.0 (sharing deferred)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Rename the package `cumulus` → `bora` (W1a)

Mechanical move of the package path and all imports. Class names and `APP_NAME`/`CONFIG_DIR_NAME` stay as-is in this task (Tasks 4–5 handle those) to keep the diff purely about the import path.

**Files:** `src/cumulus/` → `src/bora/`; every `import cumulus` / `from cumulus` in `src/` and `tests/`; `pyproject.toml`.

**Interfaces:**
- Produces: top-level package `bora`; console script `bora = "bora.cli:main"`; distribution `name = "bora"`. After this task, `from bora... import ...` is the only valid form.

- [ ] **Step 1: Move the package directory**

```bash
cd /home/exedev/maestral
git mv src/cumulus src/bora
rm -rf src/cumulus.egg-info src/maestral.egg-info
```

- [ ] **Step 2: Rewrite imports across source and tests**

```bash
grep -rl --include='*.py' -e 'cumulus' src/ tests/ \
  | xargs sed -i -E 's/\bfrom cumulus\b/from bora/g; s/\bimport cumulus\b/import bora/g; s/"cumulus\./"bora./g; s/'\''cumulus\./'\''bora./g; s/patch\("cumulus\./patch("bora./g'
```

Then sweep remaining bare module references (e.g. `cumulus.client.build` in `mock.patch` strings, `cumulus.cli:main`):

```bash
grep -rl --include='*.py' -e 'cumulus' src/ tests/ | xargs sed -i 's/cumulus\./bora./g'
```

- [ ] **Step 3: Update `pyproject.toml`**

In `pyproject.toml` change:
- `name = "cumulus"` → `name = "bora"`
- `[project.scripts]` `cumulus = "cumulus.cli:main"` → `bora = "bora.cli:main"`
- `[project.optional-dependencies] gui`: `cumulus-qt...` → `bora-qt...`, `cumulus-cocoa...` → `bora-cocoa...`
- `dev = ["bump2version", "cumulus[lint,test]"]` → `"bora[lint,test]"`
- `[tool.setuptools.package-data]` key `cumulus = [...]` → `bora = [...]`
- `urls = { Homepage = "https://github.com/user/cumulus" }` → `urls = { Homepage = "https://bora.tools" }`

(The icon `data-files` path still says `maestral.png` — Task 4 renames the file. Leave it for now.)

- [ ] **Step 4: Reinstall the package in editable mode**

```bash
pip install -e . -q
which bora && bora --version
```
Expected: `bora` resolves on PATH; prints `0.1.0`. (The old `cumulus` script is gone.)

- [ ] **Step 5: Verify no `cumulus` imports remain**

```bash
grep -rn --include='*.py' -e '\bcumulus\b' src/ tests/ || echo "CLEAN"
```
Expected: `CLEAN` (or only string-literal residues to fix). If any import remains, fix it and re-run.

- [ ] **Step 6: Run the offline gate**

Run: `pytest tests/offline -m "not load" -q`
Expected: all pass. (`CUMULUS_*` env vars and `APP_NAME`/`CONFIG_DIR_NAME` are still the old values; that's fine — Task 4 changes them.)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(8-w1a): rename package cumulus -> bora (path + imports)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: App identity — constants, env vars, resource, strings (W1b)

**Files:** `src/bora/constants.py`; `src/bora/config/main.py`; every `CUMULUS_*` env var in `src/bora/` and `tests/`; `src/bora/resources/maestral.png` → `bora.png`; `pyproject.toml`; `docs/conf.py`; user-facing strings; `src/bora/cli/cli_core.py:461` (`dbid`); `src/bora/models.py:110` (docstring); `tests/README.md`.

**Interfaces:**
- Produces: `APP_NAME="Bora"`, `BUNDLE_ID="tools.bora.app"`, `CONFIG_DIR_NAME="bora"`; env vars `BORA_*`; icon `bora.png`.

- [ ] **Step 1: Update identity constants**

In `src/bora/constants.py`: `APP_NAME = "Cumulus"` → `"Bora"`; `BUNDLE_ID = "com.cumulus.app"` → `"tools.bora.app"`.
In `src/bora/config/main.py`: `CONFIG_DIR_NAME = "cumulus"` → `"bora"`. Also update the two bootstrap calls in `src/bora/config/__init__.py` that pass the literal `"cumulus"` to `get_conf_path`/`get_data_path` → `"bora"`.

- [ ] **Step 2: Rename the env vars**

```bash
cd /home/exedev/maestral
grep -rl --include='*.py' -e 'CUMULUS_' src/ tests/ \
  | xargs sed -i 's/CUMULUS_/BORA_/g'
```
This covers `CUMULUS_TEST_REFRESH_TOKEN`, `CUMULUS_TEST_ACCESS_TOKEN`, `CUMULUS_AUTH_PORT`, `CUMULUS_CLIENT_SECRET_PATH`, and the click `CUMULUS_COMPLETE` (now derived as `BORA_COMPLETE`). Update `tests/README.md` references too:
```bash
sed -i 's/CUMULUS_/BORA_/g' tests/README.md
```

- [ ] **Step 3: Rename the icon resource and references**

```bash
git mv src/bora/resources/maestral.png src/bora/resources/bora.png
```
- `pyproject.toml`: `[tool.setuptools.data-files]` `"src/cumulus/resources/maestral.png"` → `"src/bora/resources/bora.png"` (the `src/cumulus` part may already be `src/bora` from Task 3 — fix both the dir and the filename).
- `src/bora/constants.py`: `APP_ICON_PATH = resource_path("bora.resources", "maestral.png")...` → `"bora.png"`.
- `docs/conf.py`: `html_logo = "maestral.png"` → `"bora.png"` (Task 6 handles the rest of conf.py; this keeps the icon rename atomic).

- [ ] **Step 4: Replace user-facing product strings**

Replace `Maestral`/`Cumulus` with `Bora` only in user-visible strings and product docstrings within `src/bora/` (NOT the preserved attribution per Global Constraints). Find them:
```bash
grep -rn --include='*.py' -e 'Maestral' -e 'Cumulus' src/bora/ | grep -v 'class Maestral\|MaestralProxy'
```
Edit each hit that is a product reference (e.g. `keyring.py` "Please relink Maestral...", `notify.py` "Desktop notification emitter for Maestral", `daemon.py`/`main.py`/`autostart.py` docstrings, `cli_core.py` "Run 'cumulus auth link' to authorize Cumulus."). Leave `class Maestral`/`MaestralProxy` (Task 5) and any "fork of Maestral"/upstream-link/technical-comparison comments.

- [ ] **Step 5: W0 minor cleanups**

- `src/bora/cli/cli_core.py` (~line 461): the `dbid` variable/label — rename the local variable to `remote_id` and keep the displayed label "Remote ID".
- `src/bora/models.py` (~line 110): remove/replace the docstring reference `:class:`dropbox.files.Metadata`` (it points at a removed class) — reword to describe the local `Metadata` model.

- [ ] **Step 6: Reinstall, import smoke, run the gate**

```bash
pip install -e . -q
python -c "import bora; from bora.constants import APP_NAME, BUNDLE_ID; print(APP_NAME, BUNDLE_ID)"
pytest tests/offline -m "not load" -q
```
Expected: prints `Bora tools.bora.app`; suite green. If any test hardcodes `"cumulus"` config dir / `CUMULUS_` env / `maestral.png`, fix it to the new value.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(8-w1b): app identity -> Bora (constants, env vars, icon, strings)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Rename the `Maestral` / `MaestralProxy` classes → `Bora` / `BoraProxy` (W1c)

**Files:** `src/bora/main.py` (`class Maestral`), `src/bora/daemon.py` (`class MaestralProxy`, `__all__`, RPC), `src/bora/cli/*` (`m: Maestral` hints, `Bora(config_name)` factory), `src/bora/sync.py` (docstring examples), and any other reference.

**Interfaces:**
- Consumes: nothing new.
- Produces: public class `Bora` (`bora.main.Bora`) and `BoraProxy` (`bora.daemon.BoraProxy`). Daemon RPC object name updated consistently so client/daemon still bind.

- [ ] **Step 1: Inventory the references**

```bash
cd /home/exedev/maestral
grep -rn --include='*.py' -e '\bMaestralProxy\b' -e '\bMaestral\b' src/ tests/
```
Confirm every hit is the class identifier (not preserved prose). Note any Pyro RPC registration that uses the literal name (e.g. `expose`/`register`/object-id strings).

- [ ] **Step 2: Rename `MaestralProxy` → `BoraProxy` first (longer name, avoids partial overlap)**

```bash
grep -rl --include='*.py' -e 'MaestralProxy' src/ tests/ | xargs sed -i 's/\bMaestralProxy\b/BoraProxy/g'
```

- [ ] **Step 3: Rename `Maestral` → `Bora`**

```bash
grep -rl --include='*.py' -e '\bMaestral\b' src/ tests/ | xargs sed -i 's/\bMaestral\b/Bora/g'
```

- [ ] **Step 4: Check Pyro daemon name registration**

Open `src/bora/daemon.py` and verify the daemon registers/serves under a name derived from the class or a constant, and that `BoraProxy` connects to the same name. If a literal object-id string (e.g. `"maestral.<config>"` or similar) is used for the Pyro URI, update both the server `register`/`Daemon` side and the proxy `Proxy(...)` URI consistently. Grep:
```bash
grep -rn -e 'PYRO\|Proxy(\|register(\|objectId\|uri\|socket_path\|\.sock' src/bora/daemon.py | head -30
```
Update any name that embeds the old brand so server and proxy still match.

- [ ] **Step 5: Reinstall, import smoke, daemon-test, full gate**

```bash
pip install -e . -q
python -c "from bora.main import Bora; from bora.daemon import BoraProxy; print(Bora.__name__, BoraProxy.__name__)"
pytest tests/offline/test_daemon.py -q
pytest tests/offline -m "not load" -q
```
Expected: prints `Bora BoraProxy`; daemon tests pass; full suite green.

- [ ] **Step 6: Final residual scan over shipped surface**

```bash
grep -rn --include='*.py' -e 'Maestral' -e 'Cumulus' -e '\bcumulus\b' src/ tests/ | grep -v 'fork of Maestral\|samschott/maestral'
```
Expected: empty, or only preserved attribution / technical-comparison comments. Fix any stray identifier.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(8-w1c): rename Maestral/MaestralProxy classes -> Bora/BoraProxy

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Remove backward-compat aliases (W2)

**Files:** `src/bora/client.py` (`DropboxClient` alias), `src/bora/exceptions.py` (`Dropbox*Error`/`NoDropboxDirError` aliases; `dbx_path`/`dbx_path_from` compat params), and any call-sites using those names.

**Interfaces:**
- Produces: only canonical names remain — `GoogleDriveClient`, `ServerError`, `ConnectionError`, `NoSyncDirError`, `AuthError`, and the canonical `*_path` error attribute.

- [ ] **Step 1: Find all uses of the aliases**

```bash
cd /home/exedev/maestral
grep -rn --include='*.py' -e '\bDropboxClient\b' -e '\bDropboxServerError\b' -e '\bDropboxConnectionError\b' -e '\bNoDropboxDirError\b' -e '\bDropboxAuthError\b' -e '\bdbx_path\b' -e '\bdbx_path_from\b' src/ tests/
```

- [ ] **Step 2: Migrate call-sites to canonical names**

For each hit outside the alias definitions, replace with the canonical name:
```bash
grep -rl --include='*.py' -e 'DropboxClient' src/ tests/ | xargs sed -i 's/\bDropboxClient\b/GoogleDriveClient/g'
grep -rl --include='*.py' -e 'DropboxServerError' src/ tests/ | xargs sed -i 's/\bDropboxServerError\b/ServerError/g'
grep -rl --include='*.py' -e 'DropboxConnectionError' src/ tests/ | xargs sed -i 's/\bDropboxConnectionError\b/ConnectionError/g'
grep -rl --include='*.py' -e 'NoDropboxDirError' src/ tests/ | xargs sed -i 's/\bNoDropboxDirError\b/NoSyncDirError/g'
grep -rl --include='*.py' -e 'DropboxAuthError' src/ tests/ | xargs sed -i 's/\bDropboxAuthError\b/AuthError/g'
```
For `dbx_path`/`dbx_path_from`: inspect `exceptions.py` to find the canonical property/param name they alias (the non-`dbx_` `*_path`), and replace usages with that. Do NOT blind-sed these — edit by hand to avoid hitting unrelated `*_path` names.

- [ ] **Step 3: Delete the alias definitions**

In `src/bora/client.py`: delete the `DropboxClient = GoogleDriveClient` line and its TODO comment.
In `src/bora/exceptions.py`: delete the four `Dropbox*` / `NoDropboxDirError` module-level alias assignments, and remove the `dbx_path`/`dbx_path_from` backward-compat params/properties on the API-error class (and their `# Backward compat` markers).

- [ ] **Step 4: Type-check, import smoke, full gate**

```bash
pip install -e . -q
python -c "import bora.client, bora.exceptions"
grep -rn -e 'Dropbox\(Client\|ServerError\|ConnectionError\|AuthError\)\|NoDropboxDirError\|dbx_path' src/ tests/ || echo "CLEAN"
mypy src/bora -q
pytest tests/offline -m "not load" -q
```
Expected: import OK; `CLEAN`; mypy passes (catches any missed call-site); suite green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(8-w2): remove Dropbox backward-compat aliases; migrate call-sites

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Fix Sphinx config + index, build docs (W3a)

**Files:** `docs/conf.py`, `docs/index.rst`.

**Interfaces:**
- Produces: a Sphinx build that resolves the `bora` autoapi tree with no broken references.

- [ ] **Step 1: Install the docs extra**

```bash
pip install -e ".[docs]" -q
```

- [ ] **Step 2: Fix `docs/conf.py`**

- `autoapi_dirs = ["../src/maestral"]` → `["../src/bora"]`.
- `project = "Maestral"` → `"Bora"`; `title`/`html_title` "Maestral API Documentation" → "Bora API Documentation".
- Author/copyright lines: set the project author to Bora while keeping the upstream attribution elsewhere; do not delete the Sam Schott credit if conf.py carries it — adjust the displayed project name only.
- `html_logo` is already `bora.png` (Task 4). Confirm.
- Remove the dead `"dropbox": ("https://dropbox-sdk-python...", None)` entry from `intersphinx_mapping`.

- [ ] **Step 3: Fix `docs/index.rst`**

- Title "Maestral Developer Documentation" → "Bora Developer Documentation".
- All `autoapi/maestral/*/index` (and any `autoapi/cumulus/*`) toctree paths → `autoapi/bora/*/index`.
- Docstring/example block: `from maestral.main import Maestral` → `from bora.main import Bora`; replace "Dropbox" with "Google Drive" in the surrounding prose.

- [ ] **Step 4: Build the docs and check for broken references**

```bash
cd /home/exedev/maestral/docs
sphinx-build -b html -n -W --keep-going . _build/html 2>&1 | tee /tmp/sphinx-build.log | tail -30
```
Expected: build completes. Review `/tmp/sphinx-build.log` for `WARNING: ... reference target not found` or missing-autoapi warnings. Fix any `bora` autoapi path that didn't resolve. (If `-W` is too strict because of pre-existing upstream docstring warnings unrelated to the rename, drop `-W` but still grep the log for "maestral"/"cumulus"/"reference target not found" and ensure none remain.)

- [ ] **Step 5: Commit**

```bash
cd /home/exedev/maestral
git add docs/conf.py docs/index.rst
git commit -m "docs(8-w3): fix Sphinx config + index for bora autoapi; build clean

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: User-facing docs — README, setup guide, differences (W3b)

**Files:** `README.md`; new `docs/setup.md`; new `docs/differences.md`.

**Interfaces:**
- Produces: README and two guides describing Bora-on-Google-Drive; the differences doc records the 180 s polling-latency note and "shared links not yet supported."

- [ ] **Step 1: Rewrite `README.md`**

Rewrite to describe Bora:
- Title "Bora" (subtitle may keep "a Google Drive sync client, forked from Maestral").
- Keep the "fork of [Maestral](https://github.com/samschott/maestral)" attribution paragraph.
- Replace Dropbox-specific warnings/prose with Google Drive equivalents ("Never sync a local folder with both the official Google Drive client and Bora", "...location for the Bora folder").
- Install: `pip install bora` / `pip install 'bora[gui]'`; CLI examples use `bora start|stop|status|ls|excluded|...`.
- Remove the dead Dropbox docker image (`maestraldbx/maestral`) and the `maestral-dropbox` screenshot links (drop or replace with a placeholder note).
- Fix the API-docs links (Bora docs; Google Drive API instead of Dropbox API).

Verify no unintended residue:
```bash
grep -n -i -e 'dropbox' -e 'maestral' -e 'cumulus' README.md | grep -v -i 'fork of\|samschott/maestral'
```
Expected: empty (only the attribution lines remain).

- [ ] **Step 2: Write `docs/setup.md` (Google Cloud + OAuth setup)**

Create `docs/setup.md` covering: creating a Google Cloud project, enabling the Drive API, creating an OAuth client (Desktop app), the consent-screen "Testing" mode and the **add-yourself-as-a-test-user** workaround until the app is verified (ties to checklist B2), and where Bora reads `client_secret.json`. Reference the headless port-forward pattern for `bora auth link` over SSH.

- [ ] **Step 3: Write `docs/differences.md` (user-facing differences)**

Create `docs/differences.md`: a concise "Differences from Dropbox / Maestral upstream" for users, summarizing the behavioral deltas and linking the dev reference `docs/superpowers/references/dropbox-vs-drive-differences.md`. MUST include:
- **Polling latency (D6):** "changes made on drive.google.com appear locally up to ~3 minutes later" (180 s poll; no long-poll equivalent on Drive).
- **Shared links:** not yet supported in v0.1.0 (deferred); `sharelink` is intentionally absent.
- Path/case semantics and any other deltas already documented in the reference doc.

- [ ] **Step 4: Optionally include the new pages in the toctree**

If `docs/index.rst` has a user-guide toctree, add `setup` and `differences` entries; rebuild to confirm they render:
```bash
cd docs && sphinx-build -b html -n . _build/html 2>&1 | grep -i -e 'warning' -e 'differences' -e 'setup' | tail -20; cd ..
```
Expected: the two pages build with no new broken-reference warnings.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/setup.md docs/differences.md docs/index.rst
git commit -m "docs(8-w3): rewrite README; add setup + differences guides (Bora/Drive)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Housekeeping — PLAN/TODO sweep, fresh CHANGELOG (W4)

**Files:** `PLAN.md`, `TODO.md`, `CHANGELOG.md`.

- [ ] **Step 1: Fix the stale polling claim and name residue in PLAN.md/TODO.md**

- `PLAN.md` §3.2: change the "30s polling" claim to 180 s (match `DEFAULT_POLL_INTERVAL = 180`).
- Sweep forward-looking name residue in `PLAN.md` and `TODO.md`:
```bash
grep -n -i -e 'cumulus' -e '\bmaestral\b' PLAN.md TODO.md | grep -v -i 'fork of\|samschott'
```
Update each forward-looking/shipped reference to Bora. (Do not edit historical record framing if a line is explicitly describing past state; reword product references.)

- [ ] **Step 2: Replace CHANGELOG with a fresh v0.1.0**

Delete the upstream Maestral history and write a new `CHANGELOG.md`:

```markdown
# Changelog

## v0.1.0 — Initial release (Bora, Google Drive)

Bora is a Google Drive sync client for Linux and macOS, forked from
[Maestral](https://github.com/samschott/maestral) and re-targeted from the
Dropbox API to the Google Drive API.

### Added
- Google Drive sync engine (initial full sync, change-feed polling, conflict
  resolution, selective sync, revisions/restore).
- Google OAuth linking (`bora auth link`), including a headless paste-redirect
  fallback.

### Changed
- Renamed the project to **Bora** (CLI command `bora`).
- Remote-change detection polls every 180 s (Google Drive has no long-poll
  equivalent), so remote edits appear locally within ~3 minutes.

### Known limitations
- Shared links are not yet supported (no `sharelink` command).
- Shared Drives (team drives) are not supported.
- The OAuth app is unverified; add yourself as a test user (see `docs/setup.md`).
```

```bash
git rm --quiet CHANGELOG.md 2>/dev/null; printf '%s' "<paste content above>" > CHANGELOG.md
```
(Practically: overwrite `CHANGELOG.md` with the new content via the editor; the point is the upstream history is gone.)

- [ ] **Step 3: Commit**

```bash
git add PLAN.md TODO.md CHANGELOG.md
git commit -m "docs(8-w4): fresh v0.1.0 CHANGELOG; fix 180s polling claim; name sweep

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Packaging polish (W5)

**Files:** `pyproject.toml` (add `oauthlib`); new `packaging/bora.desktop`.

- [ ] **Step 1: Add the missing direct dependency**

In `pyproject.toml` `dependencies`, add `"oauthlib"` (imported directly at `src/bora/client.py` `from oauthlib...`; currently only transitive). Keep the list alphabetical-ish per existing style.

- [ ] **Step 2: Add a Linux desktop entry**

Create `packaging/bora.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Bora
Comment=Google Drive sync client
Exec=bora gui
Icon=bora
Terminal=false
Categories=Network;FileTransfer;
```

(systemd user units are generated dynamically by `autostart.py`; no static unit template is needed — note this in the release checklist.)

- [ ] **Step 3: Clean-venv install verification**

```bash
cd /home/exedev/maestral
python -m venv /tmp/bora-venv
/tmp/bora-venv/bin/pip install -q . 2>&1 | tail -5
/tmp/bora-venv/bin/bora --version
/tmp/bora-venv/bin/bora --help | head -5
/tmp/bora-venv/bin/python -c "import bora; print('import OK')"
rm -rf /tmp/bora-venv
```
Expected: install succeeds (no missing-dependency error — `oauthlib` now declared), `bora --version` prints `0.1.0`, help renders, import OK.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml packaging/bora.desktop
git commit -m "build(8-w5): declare oauthlib dep; add Linux .desktop entry; verify clean install

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Release checklist doc (W6)

**Files:** new `docs/release-checklist.md`.

- [ ] **Step 1: Write the checklist**

Create `docs/release-checklist.md` capturing the manual / off-VM steps:

```markdown
# Bora v0.1.0 Release Checklist

Manual steps that must be performed by a human / off this VM.

## Pre-release
- [ ] **OAuth verification (B2):** submit the Google OAuth app for verification.
      Until it clears, the consent screen is in Testing mode — document the
      add-yourself-as-a-test-user workaround (see `docs/setup.md`) for
      contributors and early users.
- [ ] **Credential rotation policy (D5):** decide and document the policy for
      rotating the shipped `client_secret.json` (release note + deprecation
      timeline) so an old shipped credential can be retired without poisoning
      the project's abuse quota. The current secret is committed in the repo
      (`src/bora/resources/client_secret.json`); confirm it is restricted to
      the intended scopes/project before public release.
- [ ] **Windows smoke test:** install and run a basic link + sync on Windows
      (out of reach from the Linux dev VM).
- [ ] **Real-account end-to-end smoke:** link a live Google account
      (`bora auth link`), sync a small tree, verify status/usage display now
      shows the account (W0.5 fix). NOTE: the rename changed the on-disk config
      identity (`bora`), so any pre-rename `cumulus` link must be re-created.

## Cut the release
- [ ] Confirm the offline suite is green and `pip install .` works in a clean
      venv (see Stage 8 plan Task 10).
- [ ] Bump the version if needed (`bump2version`), currently `0.1.0`.
- [ ] Build artifacts: `python -m build` → sdist + wheel in `dist/`.
- [ ] (Optional) upload to PyPI; create a GitHub release with the CHANGELOG
      v0.1.0 notes.

## Post-release
- [ ] Backlog: implement the Drive sharing API and re-introduce `sharelink`
      (deferred from v0.1.0 — see the Stage 8 spec §4.1).
- [ ] Backlog: Shared Drives (team drive) support.
```

- [ ] **Step 2: Final full-stage verification**

```bash
cd /home/exedev/maestral
pytest tests/offline -m "not load" -q
mypy src/bora -q
flake8 src/bora
grep -rn --include='*.py' -e '\bMaestral\b' -e '\bCumulus\b' -e '\bcumulus\b' src/ tests/ | grep -v 'fork of\|samschott/maestral' || echo "RENAME CLEAN"
```
Expected: suite green; mypy/flake8 clean; `RENAME CLEAN`.

- [ ] **Step 3: Commit**

```bash
git add docs/release-checklist.md
git commit -m "docs(8-w6): release checklist (OAuth verify, rotation, Windows, publish)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-review notes (coverage vs spec)

- W0.5 blocker 2 → Task 1; W0.5 blocker 1 → Task 2. ✓
- W1 (rename) → Tasks 3 (path), 4 (identity/env/icon/strings/minor W0 items), 5 (classes). ✓
- W2 (alias removal) → Task 6. ✓
- W3 (docs) → Tasks 7 (Sphinx config + build) and 8 (README/setup/differences, incl. D6 latency note). ✓
- W4 (housekeeping, fresh CHANGELOG, 180 s) → Task 9. ✓
- W5 (oauthlib dep, .desktop, clean install) → Task 10. ✓
- W6 (release checklist, incl. B2/D5/Windows/publish/real-account, deferred sharing) → Task 11. ✓
- Preserved attribution & "don't rewrite history" enforced via Global Constraints + the residual-scan greps (which exclude `fork of`/`samschott/maestral`). ✓
