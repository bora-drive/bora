# Differences from Dropbox / Maestral Upstream

Bora is a port of [Maestral](https://github.com/samschott/maestral) from Dropbox to
Google Drive. Most day-to-day syncing feels the same, but Google Drive's API has a
number of behavioral differences that are visible to end users. This page summarises
the user-relevant ones.

For the full technical catalogue (including port adaptations and internal
implementation notes) see
[docs/superpowers/references/dropbox-vs-drive-differences.md](superpowers/references/dropbox-vs-drive-differences.md).

---

## Polling latency (~3 minutes)

**This is the biggest practical difference.** Google Drive does not offer a long-poll
or webhook API equivalent to Dropbox's `list_folder/longpoll`. Bora must periodically
ask the Drive API "are there new changes?" on a fixed schedule.

The default polling interval is **180 seconds**. This means:

- Changes you make on [drive.google.com](https://drive.google.com) (or from another
  device) will appear in your local Bora folder **up to ~3 minutes later**.
- Changes you make locally are uploaded immediately (no polling delay on the upload
  path — only the remote-to-local direction is affected).

There is no workaround short of reducing the poll interval, which would increase API
quota consumption.

---

## Shared links — not supported in v0.1.0

The `sharelink` command group is intentionally absent from Bora v0.1.0. Generating or
revoking shared links is deferred to a future release. Attempting to invoke a sharing
subcommand will return an error.

---

## Shared Drives (Team Drives) — not supported

Bora syncs **My Drive only**. Shared Drives (formerly "Team Drives") are separate root
namespaces in the Drive API and are not included in the sync scope.

---

## Native Google Docs, Sheets, and Slides

Files with a `application/vnd.google-apps.*` MIME type — Google Docs, Sheets, Slides,
Forms, Drawings, and Sites — have no downloadable byte content via the Drive API and
no checksum, so they cannot be synced as regular files.

By default, Bora **exports** Google Docs, Sheets, and Slides to read-only local
mirror files (`.md` or `.docx` for Docs, `.xlsx` for Sheets, `.pptx` for Slides).
These mirrors are one-way: local edits to them are never uploaded. See the
[Google Docs export section of the user guide](userguide.md#exporting-google-docs-sheets-and-slides)
for details and how to turn this off.

Other native types (Forms, Drawings, Sites) are **skipped** and never mirrored to your
local folder. Drive Shortcuts are handled separately — see the symlink section below.

---

## Path and filename semantics

### Slash and special characters in names

Google Drive allows `/`, backslash, and most control characters as literal characters
inside a filename (only the null byte is truly forbidden). Dropbox rejected these.

Bora percent-encodes unsafe characters when mapping a Drive filename to a local path,
so you should never see a literal `/` in a synced filename on disk. The on-disk name is
human-readable; encoding is transparent.

### Unicode normalization

Dropbox normalised file names to NFC (composed) Unicode. Drive does not; a file
uploaded with NFD (decomposed) bytes keeps those bytes. Bora preserves the remote name
as-is, so a file created on Drive with decomposed characters will appear locally with
those same bytes rather than being silently recomposed.

### Case sensitivity

Google Drive is case-preserving but not strictly case-sensitive (two files named
`Foo.txt` and `foo.txt` can coexist). Bora's index uses a case-folded key internally
to detect conflicts, but the on-disk name preserves the original casing from Drive.

---

## Conflicted-copy naming

When Bora detects a write conflict (both the local and remote copies changed since the
last sync), it creates a conflicted copy locally. The name format is:

```
<original-name> (<owner>'s conflicted copy <YYYY-MM-DD>).<ext>
```

Dropbox resolved conflicts server-side and used a different naming convention. Bora
handles conflict copies locally.

---

## Symlinks and Drive shortcuts

Google Drive has no native symlink type, but it does have **shortcuts** (small pointer
files created with "Add shortcut to Drive"). Bora maps the two onto each other:

- **Local to remote:** a symlink in the sync folder whose target resolves to a path
  *inside* the sync folder is uploaded as a Drive shortcut pointing at the synced
  target. Retargeting the symlink retargets the existing shortcut in place; deleting
  the symlink deletes the shortcut.
- **Remote to local:** a Drive shortcut whose target is synced is materialised locally
  as a symlink with a relative target. On Windows, where creating real symlinks
  usually requires Developer Mode or admin rights, Bora falls back to writing a
  minimal `<name>.lnk` shortcut file instead.

Limitations:

- A symlink whose target lies *outside* the sync folder (or is otherwise not synced)
  cannot be represented as a Drive shortcut and is reported as a sync error.
- Replacing an existing regular file or folder with a symlink at the same path is not
  supported and is reported as a sync error.
- A remote shortcut whose target is excluded from sync (or otherwise not synced) is
  skipped with a warning.
- A shortcut pointing at another shortcut is not followed (only one hop is resolved).
- Shortcuts inside view-only shared folders cannot be created, retargeted, or deleted,
  because that would modify a folder you have no write access to.

---

## Revision identifiers

Dropbox used a globally unique `rev` string per file version. Drive revision IDs are
per-file (not globally unique); the current version of a file is identified by its
`headRevisionId`. This is transparent to most users but matters if you interact with
the `bora revs` command or the Python API directly.
