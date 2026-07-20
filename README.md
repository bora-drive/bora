# Bora

A light-weight, open-source Google Drive sync client for macOS and Linux, forked from Maestral.

## About

Bora is a fork of [Maestral](https://github.com/samschott/maestral), an open-source
file sync client written in Python, adapted to sync Google Drive.

Bora supports syncing multiple Google Drive accounts and excluding local files from
sync with a ".mignore" file.

The focus on "simple" file syncing comes with real advantages: on macOS, the Bora app
bundle is lightweight and uses minimal memory. Exact memory usage depends on the size of
your synced folder and can be further reduced when running without a GUI.

Bora uses the public Google Drive API. It skips uploading or downloading a file if it
already exists with the same content locally or in the cloud (verified via MD5 checksum).

## Warning

- Never sync a local folder with both the official Google Drive client and Bora at the
  same time.
- Network drives and some external hard drives are not supported as locations for the
  Bora folder.

## Installation

On all platforms, you can install Bora as a Python package from PyPI.

For more detailed information on installation, setup, and system requirements, see the
[documentation](docs/setup.md).

### Python package using PyPI

It is recommended to install Bora inside a virtual environment:

```console
$ python3 -m venv bora-venv
$ source bora-venv/bin/activate
(bora-venv)$ pip install bora
```

If you intend to use the graphical user interface, specify the `gui` extra during
installation. This will install the system-tray frontend:

```console
(bora-venv)$ pip install 'bora[gui]'
```

## Usage

Run `bora gui` in the command line (or open the Bora app on macOS) to start Bora with a
graphical user interface. On its first run, Bora will guide you through linking and
configuring your Google Drive account and will then start syncing.

### Command line usage

After installation, Bora is available as `bora` in your command prompt. Run
`bora --help` for a full list of available commands. The most important are:

- `bora gui`: Start the Bora GUI; creates a sync daemon if one is not already running.
- `bora start|stop`: Start or stop the sync daemon.
- `bora pause|resume`: Pause or resume syncing.
- `bora autostart -Y|-N`: Configure the daemon to start on login.
- `bora status`: Show the current sync status.
- `bora filestatus LOCAL_PATH`: Show the sync status of an individual file or folder.
- `bora excluded add|remove|list`: Manage excluded folders.
- `bora ls DRIVE_PATH`: List the contents of a directory on Google Drive.
- `bora notify snooze N`: Snooze desktop notifications for N minutes.
- `bora auth link|unlink|status`: Manage account authentication.

Bora supports syncing multiple Google Drive accounts by running multiple instances with
different configuration files. Pass `--config-name` to `bora start` or `bora gui` to
select a named configuration (an existing one will be used, or a new one will be
created):

```console
$ bora start --config-name="personal"
$ bora start --config-name="work"
```

This starts two Bora instances syncing a personal and a work account respectively.
Configs are automatically cleared when unlinking an account. List all config files with
`bora config-files`:

```console
$ bora config-files

Config name  Account               Path
bora         user@gmail.com        ~/Library/Application Support/bora/bora.ini
work         user@mycorp.org       ~/Library/Application Support/bora/work.ini
```

A full reference for the CLI is available in the [user guide](docs/userguide.md).

## Differences from Maestral / Dropbox

Because Bora syncs Google Drive rather than Dropbox, a few behaviors differ from
upstream Maestral. The most notable:

- **Polling latency:** changes made on drive.google.com may take up to ~3 minutes to
  appear locally (Google Drive has no long-poll API; Bora polls every 180 seconds).
- **Native Google Docs/Sheets/Slides** have no byte content on the Drive API; they are
  exported as read-only Office/Markdown mirrors (configurable with `bora export`).
  Forms, Sites and Drawings are skipped.
- **Shared links** are available through the Python API (Drive `anyone`-reader
  permissions); there is no `bora sharelink` CLI command in v0.1.0.

See [docs/differences.md](docs/differences.md) for the full list.

## Contribute

There are multiple areas that could use help — from adding CLI commands to packaging for
additional platforms. Look out for issues marked "good first issue" or "help wanted".

Relevant resources:

- [Bora API docs](docs/index.rst) (build with Sphinx from `docs/`)
- [Google Drive API docs](https://developers.google.com/drive/api/reference/rest/v3)
- [Google Drive Python client docs](https://googleapis.github.io/google-api-python-client/docs/dyn/drive_v3.html)

[CONTRIBUTING.md](CONTRIBUTING.md) contains detailed information on expected code style
and test format.

## System requirements

- macOS 10.15 Catalina or higher, or Linux
- Python 3.10 or higher
- For the system tray icon on Linux:
  - [gnome-shell-extension-appindicator](https://github.com/ubuntu/gnome-shell-extension-appindicator)
    on GNOME 3.26 and higher
