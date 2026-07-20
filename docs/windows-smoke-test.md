# Windows Smoke Test

Manual checklist for the "Windows smoke test" item on the [release
checklist](release-checklist.md) — Bora has never been run on Windows, so
this is a from-scratch install-and-sync pass on a real Windows machine. It
is not automated and is not part of CI.

Run every command exactly as written in **PowerShell**. Each step lists the
command(s) and the expected result. Fill in the results table at the end as
you go, and stop to capture a traceback (paste it into the table) if any
step fails.

---

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

Expected: `0.1.0`.

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

When prompted `Would you like to sync all folders?`, answer `y` (this is a
fresh/test account, so there's no reason to exclude anything for the smoke
test).

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
