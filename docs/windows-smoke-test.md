# Windows Support Status

**Bora v0.1.0 does not support Windows.** It cannot be installed and run
end-to-end on Windows today — not "untested," but actively broken by
POSIX-only code paths that trigger on the very first command. This was
found during review of an earlier draft of this checklist, which had
assumed a working install and walked through link/sync steps without
verifying that any of it could run on Windows at all.

This matches the project's own declared platform support: `pyproject.toml`
describes Bora as "Open-source Google Drive sync client for macOS and
Linux," carries only the `Operating System :: Unix` classifier (no
Windows classifier), and the CLI's own `--help` text
(`src/bora/cli/cli_main.py:23`) says `"File sync client for Linux and
macOS."` Windows was never a supported target; it just hadn't been
explicitly ruled out in the release checklist until now.

## Verified blockers

1. **`src/bora/utils/path.py:8` and `:21`** unconditionally `import fcntl`
   and `import xattr` at module scope. Both are POSIX-only (`fcntl` isn't
   available on Windows Python at all; `xattr` has no Windows wheel).
   `path.py` is imported early in the CLI's dependency chain, so even
   `bora --version` — the very first smoke-test step — would fail with
   `ModuleNotFoundError` before any Bora-specific code runs.
   `src/bora/daemon.py:10` has the same unconditional `import fcntl` (used
   for a file lock at line ~254-260), an independent instance of the same
   problem in the daemon process itself.

2. **`xattr` is an unconditional runtime dependency** in `pyproject.toml`
   (`dependencies = [..., "xattr", ...]`, no platform marker), with no
   Windows wheel on PyPI. Contrast with `rubicon-objc`, two lines above it,
   which correctly carries `;sys_platform=='darwin'`. Depending on what
   `pip` resolves, `pip install` itself may fail on Windows before the code
   in point 1 is ever reached.

3. **`src/bora/utils/appdirs.py`** — every path-resolution function
   (`get_conf_path`, `get_data_path`, `get_cache_path`, `get_log_path`,
   `get_autostart_path`, `get_runtime_path`) branches on
   `platform.system() == "Darwin"` / `"Linux"` and does `else: raise
   RuntimeError("Platform not supported")` for anything else, including
   Windows. These functions are called from `src/bora/config/main.py`,
   `src/bora/config/__init__.py`, `src/bora/daemon.py`,
   `src/bora/main.py`, `src/bora/sync.py`, and `src/bora/autostart.py` —
   i.e. every config-touching or state-touching command. Even if points 1
   and 2 were fixed, virtually every command would still die here.

Any one of these is fatal on its own; all three exist independently, so
partial fixes (e.g. only patching `appdirs.py`) would still leave a
non-functional Windows build.

## What a Windows port would need

- **Platform-gate the POSIX-only imports/deps** in `path.py` and
  `daemon.py` — move `fcntl`/`xattr` behind `if not IS_WINDOWS` (or
  equivalent) imports with a Windows-native replacement for whatever they
  back (file locking, extended attributes), and add a `;sys_platform !=
  'win32'` marker to `xattr` in `pyproject.toml` (matching the existing
  `rubicon-objc` pattern) plus a Windows-only substitute dependency if one
  is needed.
- **Add a Windows branch to `appdirs.py`** — real Windows paths (e.g.
  `%APPDATA%`/`%LOCALAPPDATA%` via `os.environ` rather than XDG variables)
  for all six path functions, in place of the current `raise
  RuntimeError`.
- **Audit `daemon`/`fsevents`/`autostart`/`notify` for platform gates.**
  Findings from this pass:
  - `src/bora/fsevents/__init__.py` **already has a working Windows
    fallback** — it picks `InotifyObserver` on Linux, `FSEventsObserver` on
    Darwin, and otherwise falls back to its own `OrderedPollingObserver`
    (`src/bora/fsevents/polling.py`), which is plain `watchdog` polling and
    has no platform-specific code. So file-change detection is not a
    blocker for a Windows port.
  - `src/bora/utils/shortcuts.py` **already has a Windows path**: it tries
    a real `os.symlink` first and falls back to writing a minimal `.lnk`
    file (`write_lnk`) on `OSError`, which is the documented, expected
    outcome on Windows without Developer Mode/elevation. This piece is
    ready.
  - `src/bora/autostart.py` has **no Windows backend** —
    `SupportedImplementations` only enumerates `systemd`, `launchd`, and
    `xdg_desktop`; a Windows port would need e.g. a Scheduled Task or
    Startup-folder shortcut backend. Not a blocker for basic sync, but
    needed for feature parity.
  - `src/bora/notify.py` depends on `desktop-notifier`, which is
    cross-platform (Windows Toast support included) — no gate needed here.
  - `src/bora/daemon.py` beyond the `fcntl` import above uses `Pyro5` for
    the CLI↔daemon RPC; this is pure Python and should be fine on Windows
    once the `fcntl` lock is replaced, but hasn't been verified beyond that.
- Beyond code changes, `pyproject.toml`'s description/classifiers and the
  CLI `--help` text would need updating once Windows is actually
  supported, plus real testing — this document's checklist below is the
  starting point for that, once the above is done.

---

# Checklist (for a future Windows port)

**This section does not apply to Bora v0.1.0 — do not run it against the
current release.** It is kept here, unexecuted, as the manual test plan to
run once the blockers above are fixed and a Windows port actually exists.
Steps and commands below were written against the current (Linux/macOS
only) codebase and will need re-verification against whatever the ported
code actually does — e.g. the install step's `pip install` may no longer
need a `xattr` workaround, and the config-path step may look different
once `appdirs.py` gains a real Windows branch.

Run every command exactly as written in **PowerShell**. Each step lists
the command(s) and the expected result. Fill in the results table at the
end as you go, and stop to capture a traceback (paste it into the table)
if any step fails.

## 0. Prerequisites

- Windows 10 or 11.
- [Python](https://www.python.org/downloads/windows/) 3.10 or newer (Bora's
  floor is `requires-python = ">=3.10"`) with the `py` launcher on `PATH`.
- [Git for Windows](https://git-scm.com/download/win).
- Access to the `indirect/bora` GitHub repo. **The repo is currently
  private**, so `pip install git+https://...` in step 2 will prompt for
  Git credentials — have a GitHub Personal Access Token (with `repo` scope)
  ready to use as the password (or embed it in the URL as shown below).
- A Google account you can add as an OAuth test user (see step 3) or that
  already has access to the linked project's test-user list.

```powershell
py --version
```

Expected: `Python 3.10.x` or newer.

---

## 1. Create a virtual environment and install Bora

```powershell
py -m venv bora-venv
bora-venv\Scripts\Activate.ps1
```

Expected: the prompt gains a `(bora-venv)` prefix.

> If `Activate.ps1` is blocked by execution policy, run PowerShell as the
> current user with `Set-ExecutionPolicy -Scope Process -ExecutionPolicy
> Bypass` first, then re-run the activation command.

Install the tagged release:

```powershell
pip install git+https://github.com/indirect/bora.git@v0.1.0
```

**As of this writing the `v0.1.0` tag does not exist yet.** If the command
above fails with something like `fatal: couldn't find remote ref v0.1.0` or
`ERROR: ... Could not find a tag or branch 'v0.1.0'`, fall back to the tip
of `main`:

```powershell
pip install git+https://github.com/indirect/bora.git@main
```

Either command will prompt for GitHub credentials (private repo) — use your
username and a Personal Access Token as the password. To avoid the prompt,
embed the token in the URL instead:

```powershell
pip install git+https://<YOUR_TOKEN>@github.com/indirect/bora.git@main
```

Verify the install:

```powershell
bora --version
```

Expected (once the port exists): `0.1.0`. **On the current, unported
codebase this will instead raise `ModuleNotFoundError` — see the status
section above.**

---

## 2. Link a Google account

Bora ships with bundled OAuth credentials (`client_secret.json`), so no
Google Cloud setup is needed to run this smoke test — see
[setup.md](setup.md) if you ever need to swap in your own credentials.

The OAuth consent screen's status matters here — check which of these two
states applies when you run this:

- **App still in "Testing" mode (the default today, tracked as release
  checklist item B2).** Only accounts explicitly added as test users can
  complete the OAuth flow at all; anyone else is blocked outright with a
  "hasn't completed the Google verification process" error page. Before
  linking, make sure the Google account you're using is on the test-user
  list — see [setup.md § Add yourself as a test user](setup.md#add-yourself-as-a-test-user),
  and ask whoever administers the `bora-500821` GCP project to add you if
  you don't have access yourself.
- **App has since been published to production (post-B2, but before full
  Google verification clears).** Any Google account can start the flow, but
  the browser will show a **"Google hasn't verified this app"** interstitial.
  Click **Advanced**, then **Go to Bora (unsafe)** to proceed.

Use a dedicated config name (`smoke`) so this doesn't touch any existing
Bora config on the machine:

```powershell
bora auth link -c smoke
```

Expected: Bora prints an authorization URL to the terminal (it does **not**
open a browser for you — copy/paste the URL yourself) and waits on a local
loopback server. Open the URL in a browser, sign in, work through whichever
consent-screen state applies above, and approve. The terminal should then
print:

```
[OK] Linked to <your-email>
```

Confirm:

```powershell
bora auth status -c smoke
```

Expected: prints `Email:`, `Account type:`, and `Remote ID:` for the linked
account.

---

## 3. Start syncing into a fresh folder

```powershell
bora start -c smoke
```

When prompted `Please choose a local sync folder:`, type:

```
~\BoraSmoke
```

(Bora expands `~` via Python's `os.path.expanduser`, which resolves to
`%USERPROFILE%\BoraSmoke` on Windows — i.e.
`C:\Users\<you>\BoraSmoke`.) The folder doesn't need to exist yet; Bora
creates it.

When prompted, the exact text is `Would you like sync all folders?`
(`src/bora/cli/cli_core.py:256` — note: no "to" in the prompt). Answer `y`
(this is a fresh/test account, so there's no reason to exclude anything for
the smoke test).

Expected: `[OK] Setup completed. Starting sync.` followed by
`Starting Bora...        [OK]`.

**Local → remote:** create a file locally and confirm it appears on the web.

```powershell
"hello from windows" | Out-File -FilePath "$env:USERPROFILE\BoraSmoke\hello.txt"
```

Expected: `hello.txt` appears in the Drive web UI (drive.google.com) under
the synced folder within a few seconds.

**Remote → local:** in the Drive web UI, create a new plain-text file (or
upload one) inside the synced folder.

Expected: the file appears under `%USERPROFILE%\BoraSmoke` locally within
about 3 minutes (Bora's remote-change polling defaults to a 180-second
interval, `DEFAULT_POLL_INTERVAL` in `src/bora/client.py`).

---

## 4. Native-doc export (Google Doc → Markdown mirror)

In the Drive web UI, inside the synced folder, create a new **Google Doc**
(**New → Google Docs**) and type some text into it.

Expected: within about 3 minutes, a corresponding `<doc name>.md` file
appears under `%USERPROFILE%\BoraSmoke` (Bora's default config exports
native Google Docs/Sheets/Slides, and exports Docs as Markdown rather than
`.docx`).

Confirm the mirror is read-only:

```powershell
Get-ItemProperty "$env:USERPROFILE\BoraSmoke\<doc name>.md" | Select-Object IsReadOnly
```

Expected: `IsReadOnly` is `True`. (Bora clears the write bits with
`os.chmod(path, 0o444)` on every exported native-doc mirror, regardless of
your Drive edit permissions on the source Doc, since the export is
one-way.) Also try editing and saving the file in Notepad — Windows should
refuse to save over the read-only file.

---

## 5. Remote Drive shortcut → `.lnk`

In the Drive web UI, right-click any existing file inside the synced
folder (e.g. `hello.txt`) and choose **Organize → Add shortcut**, adding the
shortcut into the same synced folder (or a subfolder of it).

Expected: within about 3 minutes, a `.lnk` file with a matching name
appears under `%USERPROFILE%\BoraSmoke`. (Bora tries a real symlink first
and only falls back to writing a minimal `.lnk` file when `os.symlink`
fails — which is the normal case on Windows without Developer Mode or an
elevated process; see `src/bora/utils/shortcuts.py`.) Double-clicking the
`.lnk` in Explorer should open the target file.

---

## 6. Status and shutdown

```powershell
bora status -c smoke
```

Expected: table showing the linked account/usage, `Status: ...`, and
`Sync errors: 0`.

```powershell
bora stop -c smoke
```

Expected: `Stopping Bora...        [OK]`.

---

## Results

Record the environment and outcome for each step. Attach full tracebacks
(paste as a fenced code block below the table, or link a gist) for any
`[FAIL]`.

| Field | Value |
|---|---|
| Windows version (`winver` / `[System.Environment]::OSVersion`) | |
| Python version (`py --version`) | |
| Bora version installed (`bora --version`) | |
| Install source (`@v0.1.0` tag or `@main` fallback) | |
| Date run | |
| Run by | |

| Step | Result (Pass/Fail) | Notes / traceback |
|---|---|---|
| 1. Install (venv + pip install + `bora --version`) | | |
| 2. `bora auth link` / `bora auth status` | | |
| 3a. `bora start` + local→remote file | | |
| 3b. Remote→local file (polling) | | |
| 4. Native-doc export → read-only `.md` mirror | | |
| 5. Remote shortcut → `.lnk` | | |
| 6. `bora status` (0 sync errors) / `bora stop` | | |
