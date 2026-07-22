# Changelog

## Unreleased

### Fixed
- A dropped network connection no longer pauses syncing permanently.
- Unexportable files no longer retried on every sync cycle.
- Docs/Sheets/Slides over the 10 MB export limit now sync via a docs.google.com fallback.
- Files whose owner disabled downloading now appear as read-only .url shortcuts.
- A synced .url shortcut of your own that points at the same document is no
  longer deleted when that document becomes downloadable again.

### Changed
- Download-disabled files are now recognised from Drive metadata
  (`capabilities.canDownload`) before any download or export is attempted,
  instead of only after a refused request.

## v0.1.0 — Initial release (Bora, Google Drive)

Bora is a Google Drive sync client for macOS and Linux, forked from
[Maestral](https://github.com/samschott/maestral) and re-targeted from the
Dropbox API to the Google Drive API.

### Added
- Google Drive sync engine: initial full sync, change-feed polling, conflict
  resolution, selective sync (`.mignore`), file revisions and restore.
- Google OAuth account linking (`bora auth link`), including a headless
  paste-redirect / SSH port-forward fallback.
- Sharing support via Drive permissions: `create_shared_link` (idempotent
  `anyone`-reader link), `revoke_shared_link`, `list_shared_links`, and
  `share_dir` (folder + optional per-email role grant). These currently ship
  as `Client` methods only — there is no `bora sharelink`-style CLI command
  yet.
- Google Docs/Sheets/Slides export to Markdown/Office on download (Doc →
  `.md`/`.docx`, Sheet → `.xlsx`, Slides → `.pptx`), with `bora export
  docs on|off` / `bora export markdown on|off` toggles (both default on).
- "Shared with me" virtual `/Shared with me` root; view-only shared items are
  read-only on disk and never uploaded (an editor-access shared item syncs
  writable, per Drive's `can_edit`).
- Symlink ↔ Drive-shortcut mapping: local symlinks create/retarget Drive
  shortcuts, and remote shortcuts materialize as platform symlinks / `.lnk`.
- Upload bandwidth throttling (`_throttle_up`), matching the existing
  download-side throttle; simple ≤5 MB uploads are single-request and stay
  unthrottled by design.

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
- `bora ls` requested the wrong change-feed field (`sharedWithMe` instead of
  `sharedWithMeTime`), which broke the "Shared with me" projection.
- The local `/Shared with me` mount directory is no longer uploaded to Drive
  or synthesized as a create event by the while-inactive scan.
- Piped (non-tty) `bora ls` printed nothing because it re-iterated an
  already-exhausted `list_folder_iterator`.
- A server-side re-export of an exported Google Docs mirror used to spawn a
  spurious writable conflicted copy; `_check_download_conflict` now
  byte-compares an untouched mirror against the surviving index row's
  content hash, so an unedited mirror is treated as `RemoteNewer` instead
  (f15d19c). A follow-up fix (255bf37) adds `_adopt_untouched_mirror` as a
  fallback for the case where that index row doesn't survive an
  index rebuild — see `TODO.md` for the byte-stable-vs-byte-unstable nuance.
  For Office mirrors (`.xlsx`/`.pptx`), whose re-exports are zip containers
  that differ byte-for-byte on every export even when unchanged (per-entry
  zip timestamps), adoption now falls back to a zip-aware comparison of
  member names/CRCs/sizes (3677ca3) so these mirrors are still adopted
  instead of spawning a conflicted copy and an unnecessary upload.

### Known limitations
- Shared Drives (team drives) are not supported.
- The OAuth app's consent screen is published to "In production" but is
  still unverified by Google, so linking shows Google's "unverified app"
  warning (click Advanced → continue) and the app is capped at 100 total
  users. Full verification is deferred to the public release (see
  `docs/setup.md` and `docs/credential-rotation.md`).
