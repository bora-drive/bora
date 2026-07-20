# User Guide

Bora is an open-source Google Drive sync client for Linux and macOS. It runs as a
background daemon that keeps a local folder and your Google Drive ("My Drive" plus a
"Shared with me" mount point) in sync, and is controlled entirely from the command
line. This guide walks through everyday usage; for behavioral differences from
Dropbox/Maestral, see [Differences from Dropbox](differences.md).

---

## Installation

Bora requires Linux or macOS. It is recommended to install Bora inside a virtual
environment:

```console
$ python3 -m venv bora-venv
$ source bora-venv/bin/activate
(bora-venv)$ pip install bora
```

If you want a system-tray frontend, install with the `gui` extra:

```console
(bora-venv)$ pip install 'bora[gui]'
```

After installation, the `bora` command is available in your shell. Optionally, install
shell completions with `bora completion bash|zsh|fish`.

---

## Linking your Google account

Link Bora to a Google Drive account:

```console
$ bora auth link
```

Bora prints a Google authorization URL and starts a small local server to receive the
OAuth redirect. Open the URL in a browser, sign in, and grant access; the flow
completes automatically. The resulting credentials are stored in your system keyring
(macOS Keychain, GNOME Keyring, or equivalent).

Useful variations:

- `bora auth link --no-local-server` — use the paste-the-redirect-URL flow instead of
  the loopback server (useful for headless setups without SSH port-forwarding; see
  the [setup guide](setup.md) for the SSH port-forwarding alternative).
- `bora auth link --relink` — re-authorize the already-linked account while keeping
  the sync state.

Check or revoke the link at any time:

```console
$ bora auth status   # show the linked account
$ bora auth unlink   # revoke credentials and remove them from the keyring
```

If you are running from source or want to use your own Google Cloud OAuth client, see
the [Google Cloud & OAuth setup guide](setup.md).

---

## Choosing the sync folder

You do not need to pick a folder before starting: the first `bora start` walks you
through linking (if still pending) and then asks for a local sync folder location,
suggesting `~/Sync (Bora)` by default. If the folder already exists you can choose to
**replace** it or **merge** its existing content into the sync.

To relocate the sync folder later, let Bora move it so the index stays intact:

```console
$ bora move-dir ~/new/location
```

---

## Starting and stopping the daemon

```console
$ bora start    # start the sync daemon (background)
$ bora stop     # stop the daemon
$ bora status   # connection, sync activity, and any sync errors
```

`bora start -f` runs the daemon in the foreground (add `-v` to also print log messages
to stderr), which is handy for debugging or for running under a service manager.

While the daemon is running you can temporarily suspend syncing:

```console
$ bora pause
$ bora resume
```

To start Bora automatically on login (systemd on Linux, launchd on macOS):

```console
$ bora autostart -Y   # enable; use -N to disable
```

Note that changes made remotely can take up to ~3 minutes to appear locally because
the Drive API must be polled; uploads happen immediately. See
[Differences from Dropbox](differences.md) for details.

---

## Selective sync

During the first-run dialog you can choose which top-level folders to sync. Afterwards,
manage exclusions with the `excluded` command group. Paths are given relative to the
Drive root:

```console
$ bora excluded list
$ bora excluded add "/Photos" "/Old projects"
$ bora excluded remove "/Photos"
```

Excluding an item removes it locally (it stays on Drive); removing it from the list
downloads it again. Removing a path nested inside an excluded folder includes its
parent folder as well (but no other items inside it).

Additionally, a `.mignore` file at the top level of the sync folder can list
gitignore-style patterns for **local** items that should never be uploaded. Patterns
are matched against the human-readable path relative to the sync folder.

---

## Exporting Google Docs, Sheets, and Slides

Native Google documents have no downloadable byte content and cannot be synced as
regular files. By default, Bora instead **exports** them to local mirror files:

| Native type   | Exported as                          |
| ------------- | ------------------------------------ |
| Google Docs   | `.md` (Markdown) — or `.docx` if Markdown export is off |
| Google Sheets | `.xlsx`                              |
| Google Slides | `.pptx`                              |

A Doc named `Report` therefore appears locally as `Report.md` (or `Report.docx`).
Forms, Drawings, and Sites cannot be exported and are always skipped.

The mirrors are **read-only and one-way**: the exported bytes are not the document's
own content, so Bora never uploads them back to Drive.

- If you edit a mirror anyway, your edit is preserved as a normal, writable
  conflicted copy, and the read-only mirror is re-exported the next time the document
  changes on Drive (or after `bora rebuild-index`).
- Deleting or renaming a mirror locally does **not** delete the document on Drive.

Control the feature from the CLI (run without an argument to print the current
setting; changes take effect immediately):

```console
$ bora export docs on|off       # export native Docs/Sheets/Slides at all
$ bora export markdown on|off   # export Docs to Markdown instead of .docx
```

Turning export on schedules a full reindex so existing documents are picked up.
Turning it off stops exports and updates; existing mirror files stay on disk. The
corresponding config keys are `export_google_docs` and `export_google_docs_markdown`
in the `[sync]` section of the config file.

---

## Bandwidth limits

Upload and download rates can be capped independently, in MB/sec (`0` means
unlimited). Run without an argument to print the current limit; changes take effect
immediately:

```console
$ bora bandwidth-limit up 5
$ bora bandwidth-limit down 10
$ bora bandwidth-limit up 0    # remove the upload limit
```

---

## "Shared with me"

Items that other people share with you do not live in "My Drive"; Bora mounts them
under a virtual folder at the top of your sync folder:

```
<sync folder>/Shared with me/
```

- Shared items you can edit sync in both directions like any other file.
- **View-only** shared items are mirrored read-only locally (files `r--r--r--`,
  folders `r-xr-xr-x`). Local changes to view-only items are never uploaded; they are
  silently skipped.
- Shared items can be excluded from sync like any other folder, e.g.
  `bora excluded add "/Shared with me/Big dataset"`.

---

## Symlinks and Drive shortcuts

Bora maps local symlinks and Drive shortcuts onto each other:

- A local symlink pointing at a target **inside** the sync folder is uploaded as a
  Drive shortcut to that target; retargeting or deleting the symlink retargets or
  deletes the shortcut.
- A Drive shortcut whose target is synced is materialised locally as a symlink (with
  a relative target). On Windows, where creating symlinks usually requires elevated
  rights, Bora writes a `<name>.lnk` shortcut file instead.

Symlinks pointing outside the sync folder cannot be represented on Drive and are
reported as sync errors. See
[Differences from Dropbox](differences.md#symlinks-and-drive-shortcuts) for the full
list of edge cases.

---

## CLI tour

Run `bora --help` for the full listing and `bora COMMAND --help` for details on any
command. All commands accept `-c`/`--config-name` to operate on a named configuration,
which lets you run several accounts side by side (each with its own daemon, sync
folder, and settings):

```console
$ bora start -c work
$ bora status -c work
```

Use `bora config-files` to list all configured accounts (add `--clean` to remove
config files without a linked account).

### Core

| Command       | Description                                     |
| ------------- | ----------------------------------------------- |
| `bora start`  | Start the sync daemon (first run guides setup). |
| `bora stop`   | Stop the sync daemon.                           |
| `bora gui`    | Run the GUI if installed.                       |
| `bora pause`  | Pause syncing.                                  |
| `bora resume` | Resume syncing.                                 |
| `bora auth`   | `link`, `unlink`, and `status` for the account. |

### Information

| Command                | Description                                        |
| ---------------------- | -------------------------------------------------- |
| `bora status`          | Daemon status, activity, and sync errors.          |
| `bora filestatus PATH` | Sync status of a local file or folder.             |
| `bora activity`        | Live view of all items being synced.               |
| `bora history`         | Recent sync history.                               |
| `bora ls [-l] [-d] PATH` | List a remote directory (long format, deleted items). |
| `bora config-files`    | List all configured accounts.                      |

### Settings

| Command                | Description                                              |
| ---------------------- | -------------------------------------------------------- |
| `bora autostart`       | Start the daemon on login (`-Y` enable / `-N` disable).  |
| `bora excluded`        | `list`, `add`, `remove` selective-sync exclusions.       |
| `bora notify`          | Desktop notifications: `level ERROR\|SYNCISSUE\|FILECHANGE`, `snooze MINUTES`. |
| `bora bandwidth-limit` | `up` / `down` transfer limits in MB/sec.                 |
| `bora export`          | `docs` / `markdown` Google Docs export toggles.          |

### Maintenance

| Command                  | Description                                              |
| ------------------------ | -------------------------------------------------------- |
| `bora move-dir PATH`     | Move the local sync folder.                              |
| `bora rebuild-index`     | Rebuild the sync index (re-downloads metadata; may take a while). |
| `bora revs PATH`         | List old revisions of a file.                            |
| `bora diff PATH`         | Compare two revisions of a file.                         |
| `bora restore PATH`      | Restore a previous version of a file (`-v REV`).         |
| `bora log`               | `show`, `clear`, or set the `level` of the log.          |
| `bora config`            | `get` / `set` / `show` raw config values (prefer the Settings commands where one exists). |
| `bora completion SHELL`  | Generate a shell completion script (bash/zsh/fish).      |
