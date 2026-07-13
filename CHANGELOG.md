# Changelog

## v0.1.0 — Initial release (Bora, Google Drive)

Bora is a Google Drive sync client for macOS and Linux, forked from
[Maestral](https://github.com/samschott/maestral) and re-targeted from the
Dropbox API to the Google Drive API.

### Added
- Google Drive sync engine: initial full sync, change-feed polling, conflict
  resolution, selective sync (`.mignore`), file revisions and restore.
- Google OAuth account linking (`bora auth link`), including a headless
  paste-redirect / SSH port-forward fallback.

### Changed
- Renamed the project to **Bora** (CLI command `bora`).
- Remote-change detection polls every 180 seconds (Google Drive has no
  long-poll equivalent), so remote edits appear locally within ~3 minutes.

### Fixed
- macOS daemon startup: the daemon subprocess is spawned with
  `PYTHONOPTIMIZE=2`, under which AppKit was no longer loaded incidentally
  before `rubicon.objc.eventloop` imports. That import resolves the AppKit class
  `NSEvent` at load time, so it failed with "ObjC Class NSEvent couldn't be
  found" and the daemon died before creating its Pyro socket. AppKit is now
  loaded explicitly first. (Surfaced by CI on macOS + Python 3.14.)

### Known limitations
- Shared links are not supported yet (no `sharelink` command).
- Shared Drives (team drives) are not supported.
- The OAuth app is unverified; add yourself as a test user (see `docs/setup.md`).
