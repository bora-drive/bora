# Design: Google Docs Export (one-way, read-only)

**Date:** 2026-07-05
**Status:** Approved — ready for implementation plan
**Scope:** Export native Google Workspace files (Docs, Sheets, Slides) to local
`.docx` / `.xlsx` / `.pptx` (or `.md` for Docs), as one-way (download-only)
read-only mirrors that auto-update whenever the server-side document changes.

---

## 1. Problem & goal

Bora (the maestral → Google Drive port) currently **skips all native Google
Workspace files everywhere**. They have no downloadable bytes, no `md5Checksum`,
and cannot be synced as ordinary files, so `native_docs.py` filters them out at
five points (metadata, listing, bootstrap, changes feed, restore).

Goal: instead of skipping them, **export** them to their Office equivalents on
disk, keep those local files **read-only** and **download-only** (they must
never be uploaded back), and **re-export automatically** whenever the underlying
Google document is edited server-side.

Mapping:

| Native mime | Export format (default) | Extension |
|---|---|---|
| `application/vnd.google-apps.document` | `text/markdown` **or** `docx` | `.md` / `.docx` |
| `application/vnd.google-apps.spreadsheet` | `xlsx` | `.xlsx` |
| `application/vnd.google-apps.presentation` | `pptx` | `.pptx` |

Out of scope (still filtered, unchanged): Forms, Sites, Drawings, shortcuts, and
any other native type without a clean Office/Markdown export.

---

## 2. Key decisions (from brainstorming)

1. **Foundation = a `download_only` index flag (approach A).** Bora has no
   download-only concept today. A per-item flag on the index entry is the only
   mechanism that cleanly expresses "this local file mirrors a server object and
   must never round-trip." Rejected alternatives: auto-`.mignore` (extension-
   based, would wrongly suppress genuine user Office files) and a content-hash
   identity trick (breaks on edit, muddies conflict detection).
2. **Edit model = conflict-copy.** If a user edits a local export, rename their
   file to `… (conflicted copy).<ext>` (and make *that* a normal uploadable
   file), then re-export the server version under the original read-only name.
3. **Naming = append native extension**, with disambiguation on collision.
4. **Markdown toggle = a format selector for Google Docs, not a companion.**
   When on, Docs export as `.md` (replacing `.docx`); Sheets/Slides unaffected.
   **One local file per remote object** — no companion files, no two-rows-per-
   object schema problem.
5. **Both toggles default ON.** Out of the box: Docs → `.md`, Sheets → `.xlsx`,
   Slides → `.pptx`.

---

## 3. Components & seam

Four existing modules change; no new subsystem.

### 3.1 `native_docs.py` — classifier + naming

- `EXPORT_MIME_MAP`: native mime → `(export_mime, extension)` for the Office
  formats; a parallel one-entry Markdown map for Docs (`text/markdown`, `.md`).
- `is_exportable(mime)` — Docs / Sheets / Slides.
- `is_unsupported_native(mime)` — Forms / Sites / Drawings / shortcuts / other
  natives; these stay filtered exactly as today.
- `export_target(name, mime, markdown_enabled)` → `(export_mime, filename)`.
  For a Doc with `markdown_enabled=True` returns `text/markdown` + `<name>.md`;
  otherwise the Office mime + `<name>.<ext>`.

The existing `is_google_native()` is retained where "any native" is the right
question (e.g. the upload/restore guards), and callers that need the finer
distinction switch to `is_exportable` / `is_unsupported_native`.

### 3.2 `client.py` — export download branch

- `download()` (`src/bora/client.py:708`) currently raises `UnsupportedFileError`
  for native docs. Add an **export branch**: for exportables, call
  `files().export_media(fileId=..., mimeType=export_mime)` (instead of
  `files().get_media`, which does not work for native docs), stream to the
  `.bora-part` sidecar, then `os.replace` (atomic) as today. No server
  `md5Checksum` to verify against — see §4.
- `get_metadata` (`:582`), `list_folder_iterator` (`:952`),
  `_bootstrap_recursive` (`:1040`): stop dropping **exportables** when the export
  toggle is on. Reuse the already-threaded `include_non_downloadable_files` flag
  (currently always `False`) — export mode sets it `True`. Unsupported natives
  are still dropped.

### 3.3 `changes.py` — changes feed

- `_emit_apply` (`src/bora/changes.py:226`) currently returns `True` (drops the
  entry) for any native. When export is on, **exportables pass through** and are
  emitted as normal APPLY events; unsupported natives are still dropped.

### 3.4 `sync.py` / `models.py` — the flag & wiring

- **`models.py`:** add `download_only` (nullable bool `Column`, treated as
  `False` when absent) to `IndexEntry` (`src/bora/models.py:473`). The custom ORM
  only does `CREATE TABLE IF NOT EXISTS` — it will **not** add a column to an
  existing `.db`, and the per-version `_update_from_pre_v*` hooks are dead code
  (no dispatcher) on this unreleased `0.1.0` fork. The correct, version-
  independent mechanism is a **schema-drift guard** at DB-manager init: compare
  `PRAGMA table_info` against `model.__columns__` for the derived-cache tables
  (`index`, `history`, `hash_cache`); on any mismatch, **drop** them so
  `CREATE TABLE` recreates them and the existing empty-index bootstrap re-derives
  them from Drive. This honors the documented "derived cache → drop-and-reindex
  on schema change" policy (`main.py:1538` comment) without a migration
  framework, and covers fresh installs, existing dev DBs, and the test account.
- **Download path** (`_on_remote_file`, `src/bora/sync.py:3695`): for exportables,
  resolve the export target, download via the export branch, set `download_only=
  True` on the index entry, and apply read-only perms (§5).
- **Upload suppression** (local-event path, ~`src/bora/sync.py:2017`): skip
  queuing any upload for a local file whose index entry is `download_only`. This
  — not the file perms — is the real guard against round-tripping.

---

## 4. Data flow & change detection

Native docs have **no `md5Checksum`, no `size`, and no `headRevisionId`**
(that field is documented as binary-content-only). They **do** have the Drive
`version` field (a monotonically-increasing integer that changes on every
server-side edit) and `modifiedTime`.

- **Change signal = the Drive `version` field.** Add `version` to `FILE_FIELDS`
  (`changes.py:54`). For exportables, project `rev = str(version)`; binary files
  keep `rev = headRevisionId` (unchanged). On each remote change `rev` differs →
  re-export. This drives `_check_download_conflict` unchanged
  (`src/bora/sync.py:3368`: `if event.rev == local_rev → identical`), so
  exportables ride the existing path with `rev` as the identity key. Using
  `version` also satisfies the "Rev required to update index" guard
  (`sync.py:1029`), which an empty `headRevisionId` would violate.
- **`content_hash` = md5 of the exported bytes, computed locally** after export.
  It is *not* a server hash (there is none). Its only job is to let the
  local-scan machinery detect user tampering: an edited local file → md5 ≠
  indexed `content_hash` → conflict-copy path (§5). Division of labour:
  `rev` answers "did the server change," local md5 answers "did the user edit."
- Export writes to the same `.bora-part` sidecar → `os.replace`, as today.

**Determinism caveat:** Google's export bytes are not guaranteed byte-identical
across identical `rev`s. Because re-download is gated on `rev` (not hash), a
non-deterministic export does **not** cause spurious re-downloads. The local md5
is recomputed and re-stored on every export, so it always reflects the bytes
actually on disk — tamper detection compares the *current* disk file against the
*last exported* bytes, which is correct.

---

## 5. Read-only, edit/conflict, and collisions

- **Read-only:** after `os.replace`, strip write bits (`chmod 0o444`, honoring
  umask for group/other). Human-facing "don't edit me" signal only.
- **Upload suppression:** the `download_only` flag (not perms) prevents upload.
- **Edit detected** (local md5 ≠ indexed `content_hash` — only possible if the
  user cleared read-only and saved):
  1. Rename the user's file → `<name> (conflicted copy).<ext>`.
  2. Clear `download_only` on the renamed file so it becomes a normal,
     uploadable binary — the user keeps and can sync their edits.
  3. Re-export the server version to `<name>.<ext>`, read-only, `download_only`.
  Reuses Bora's Stage-7c conflict-copy + evict machinery.
- **Name collision** (a genuine binary `Report.docx` already exists in the same
  folder, not an export): the export takes a disambiguated, stable name —
  `Report (Google Doc).docx` — recorded in the index so it persists across
  re-exports.

---

## 6. The Markdown toggle (Google Docs only)

- `export_google_docs_markdown = True` (default): Google **Docs** export as
  `.md` via `text/markdown`. When `False`, Docs export as `.docx`.
- Sheets always `.xlsx`; Slides always `.pptx` — the toggle does not affect them.
- **One local file per remote object.** No companion files. Flipping the toggle
  changes the export target of Docs; the plan must handle the transition
  (an existing `.docx` export whose toggle flips to markdown should be replaced
  by `.md` on next re-export, and vice versa — treated as a rename of the
  download-only mirror keyed by `remote_id`).

---

## 7. Config, CLI, error handling

- **Config** (`src/bora/config/main.py`, `sync` section — additive keys, no
  `CONF_VERSION` bump):
  - `export_google_docs: bool = True`
  - `export_google_docs_markdown: bool = True`
- **CLI:** extend `src/bora/cli/cli_settings.py` following the `bandwidth_limit`
  group pattern; expose the toggles on the `Bora` facade in `src/bora/main.py`.
- **Enabling/disabling at runtime:** turning export **on** triggers a
  re-bootstrap that pulls in previously-skipped natives. Turning it **off**
  should stop exporting and (design choice for the plan) leave existing exports
  in place rather than mass-deleting.
- **Error handling:**
  - `export_media` can fail on large docs (`exportSizeLimitExceeded`, ~10 MB /
    50 MB depending on format) → surface a per-item `SyncError`, skip the item,
    do **not** wedge the sync cycle.
  - Unsupported natives (Forms/Sites/Drawings) stay filtered — never surfaced.
  - Transient export errors ride the existing retry/circuit-breaker paths.

---

## 8. Testing

- **`native_docs`:** classification (`is_exportable` / `is_unsupported_native`),
  `export_target` naming incl. markdown toggle and extension mapping.
- **`client.download`:** export branch with a mocked `export_media` for each of
  the three types + the markdown variant; verify atomic replace.
- **Sync engine:**
  - export → local file is read-only + `download_only` set + **no upload queued**.
  - `rev` change → re-export overwrites the local mirror.
  - user edit (md5 mismatch) → conflict-copy + re-export.
  - name collision → disambiguated stable name.
  - markdown toggle flip → `.docx` mirror replaced by `.md` (rename by
    `remote_id`).
  - export toggle **off** → natives filtered again; existing exports untouched.
- **Live spot-check** on the cumulus-test account: a real Doc (→ `.md` and,
  with toggle off, `.docx`), Sheet (→ `.xlsx`), Slides (→ `.pptx`); edit the
  Google Doc server-side and confirm the local mirror auto-updates; confirm the
  mirror is read-only and never re-uploaded.

---

## 9. Phasing

**Single phase.** Markdown export for Docs uses the *identical* `export_media`
seam as the Office formats — only the `mimeType` (`text/markdown`) and extension
(`.md`) differ. Deferring it would ship a no-op default (Docs would export as
`.docx` while the markdown default is `True`). It is therefore implemented
inline: the naming helper and export branch handle the markdown target from the
start, and the `export_google_docs_markdown` toggle is live on first release.
The toggle-flip transition (an existing `.docx` mirror whose toggle flips to
markdown is replaced by `.md`, treated as a rename of the download-only mirror
keyed by `remote_id`) is covered by the change-detection path, since flipping
the toggle changes the projected `remote_path` for the Doc.
