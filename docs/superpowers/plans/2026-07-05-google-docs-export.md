# Google Docs Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sync native Google Workspace files (Docs, Sheets, Slides) as one-way, read-only local mirrors — `.md`/`.docx`, `.xlsx`, `.pptx` — that auto-update whenever the source document changes server-side and never upload back.

**Architecture:** Native docs are currently *filtered out* at five seams by `native_docs.py`. This feature (a) opens those seams for **exportable** types only (Docs/Sheets/Slides; Forms/Sites/Drawings/shortcuts stay filtered), (b) **projects the export filename** (`Report` → `Report.docx`) as the canonical `remote_path` at the three `compose_path` chokepoints so the index, `PathIDCache` key, and on-disk name all agree while the raw Drive name survives only in `PathIDCache.name`, (c) downloads via `files().export_media` instead of `get_media`, computing a local md5 for tamper detection, (d) marks mirrors read-only on disk and `download_only` in the index so the upload path never round-trips them, and (e) uses the Drive `version` field (not `headRevisionId`, which native docs lack) as the change signal.

**Tech Stack:** Python 3, `google-api-python-client` (Drive v3), custom SQLite ORM (`bora.database.orm`), pytest with a `FakeDrive` test double (`tests/fake_drive.py`), mypy + flake8.

## Global Constraints

- Package is `bora`; source under `src/bora/`, tests under `tests/offline/`. Run tests with `python -m pytest tests/offline/... -v` (benchmarks auto-disabled via `addopts = "--benchmark-disable"`).
- **mypy and flake8 must stay at baseline parity** — no new errors. Run `python -m mypy src/bora` and `python -m flake8 src/bora` after each task.
- Adding a **config** key needs **no `CONF_VERSION` bump** (`config/main.py:88`, note 3). Do NOT bump it.
- The index/history/hash_cache tables are a **derived cache** — the sanctioned response to a schema change is drop-and-reindex, never `ALTER TABLE` (`main.py:1538` policy comment).
- Exportable mime map (canonical, define once in `native_docs.py`):
  - `application/vnd.google-apps.document` → Office `docx`, Markdown `text/markdown`/`.md`
  - `application/vnd.google-apps.spreadsheet` → `xlsx` (Office mime `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`)
  - `application/vnd.google-apps.presentation` → `pptx` (Office mime `application/vnd.openxmlformats-officedocument.presentationml.presentation`)
  - Word Office mime: `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- Config defaults (both **ON**): `export_google_docs = True`, `export_google_docs_markdown = True`.
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit. Frequent commits.
- End every commit message with the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- **`src/bora/native_docs.py`** (modify) — the classifier. Gains the export-mime map, `is_exportable` / `is_unsupported_native`, and the naming helper `export_display_name`. Sole owner of "which natives export and to what name."
- **`src/bora/export.py`** (create) — small value object `ExportOptions(enabled: bool, markdown: bool)` plus a pure `project_leaf_name(raw_name, mime, opts) -> str` used at every `compose_path` chokepoint. Keeps the projection logic in one testable place; the three call sites just call it.
- **`src/bora/client.py`** (modify) — `download()` export branch; open `get_metadata` / `list_folder_iterator` / `_bootstrap_recursive` / `_scoped_recursive` for exportables; thread `ExportOptions`; add `version` to reads.
- **`src/bora/changes.py`** (modify) — `FILE_FIELDS` gains `version`; `_emit_apply` passes exportables through and projects the leaf name; `iter_remote_changes` threads `ExportOptions`.
- **`src/bora/path_cache.py`** (modify) — `populate_from_listing` projects the leaf name (keeping raw `name`); `move` and `resolve_path` preserve the raw name instead of re-deriving it from the projected path.
- **`src/bora/core.py`** (modify) — `to_file_metadata` sets `rev = str(version)` for exportables and marks the metadata as an export (new `FileMetadata.export_mime` field).
- **`src/bora/models.py`** (modify) — `IndexEntry.download_only` column; `SyncEvent` carries `download_only`; `from_metadata` maps it.
- **`src/bora/database/orm.py`** (modify) — schema-drift guard in `Manager` init.
- **`src/bora/sync.py`** (modify) — set read-only + `download_only` after export download; suppress upload for `download_only`; conflict-copy-on-edit for `download_only`.
- **`src/bora/config/main.py`** (modify) — two new `sync` defaults.
- **`src/bora/main.py`** (modify) — `Bora` facade properties for the toggles.
- **`src/bora/cli/cli_settings.py`** + **`cli_main.py`** (modify) — `export` command group.
- **`tests/fake_drive.py`** (modify) — model native docs (`version`, no md5, no headRevisionId) and `files().export_media`.

---

## Task 1: Export classifier & naming in `native_docs.py`

**Files:**
- Modify: `src/bora/native_docs.py`
- Test: `tests/offline/test_native_docs.py`

**Interfaces:**
- Produces:
  - `EXPORT_MIME_MAP: dict[str, tuple[str, str]]` — native mime → `(office_mime, office_ext)`, e.g. `"...document" → ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx")`.
  - `MARKDOWN_MIME = "text/markdown"`, `MARKDOWN_EXT = "md"`.
  - `is_exportable(mime: str | None) -> bool` — True only for the three keys of `EXPORT_MIME_MAP`.
  - `is_unsupported_native(mime: str | None) -> bool` — native (per `is_google_native`) but not exportable.
  - `export_target(mime: str, markdown: bool) -> tuple[str, str]` — returns `(export_mime, ext)`; for the Document mime with `markdown=True` returns `(MARKDOWN_MIME, MARKDOWN_EXT)`, else the Office pair.
  - `export_display_name(raw_name: str, mime: str, markdown: bool) -> str` — `f"{raw_name}.{ext}"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/offline/test_native_docs.py`:

```python
from bora.native_docs import (
    EXPORT_MIME_MAP,
    export_display_name,
    export_target,
    is_exportable,
    is_unsupported_native,
)

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"


@pytest.mark.parametrize("mime", [DOC, SHEET, SLIDES])
def test_exportable_types(mime):
    assert is_exportable(mime) is True
    assert is_unsupported_native(mime) is False


@pytest.mark.parametrize(
    "mime",
    [
        "application/vnd.google-apps.drawing",
        "application/vnd.google-apps.form",
        "application/vnd.google-apps.site",
        GOOGLE_SHORTCUT_MIME_TYPE,
    ],
)
def test_unsupported_natives(mime):
    assert is_exportable(mime) is False
    assert is_unsupported_native(mime) is True


def test_folder_and_regular_are_neither():
    for mime in (GOOGLE_FOLDER_MIME_TYPE, "image/png", None):
        assert is_exportable(mime) is False
        assert is_unsupported_native(mime) is False


def test_export_target_office():
    assert export_target(SHEET, markdown=True) == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    )
    assert export_target(SLIDES, markdown=False)[1] == "pptx"


def test_export_target_markdown_only_for_docs():
    assert export_target(DOC, markdown=True) == ("text/markdown", "md")
    assert export_target(DOC, markdown=False)[1] == "docx"
    # markdown flag never affects sheets/slides
    assert export_target(SHEET, markdown=True)[1] == "xlsx"


def test_export_display_name():
    assert export_display_name("Report", DOC, markdown=False) == "Report.docx"
    assert export_display_name("Report", DOC, markdown=True) == "Report.md"
    assert export_display_name("Q3 Budget", SHEET, markdown=True) == "Q3 Budget.xlsx"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_native_docs.py -q`
Expected: FAIL — `ImportError: cannot import name 'EXPORT_MIME_MAP'`.

- [ ] **Step 3: Implement in `src/bora/native_docs.py`**

Add after the existing constants (after line 43) and export the new names in `__all__`:

```python
MARKDOWN_MIME = "text/markdown"
MARKDOWN_EXT = "md"

# native mime -> (office export mime, extension without dot)
EXPORT_MIME_MAP: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pptx",
    ),
}
_DOCUMENT_MIME = "application/vnd.google-apps.document"


def is_exportable(mime_type: str | None) -> bool:
    """True for the native types Bora can export (Docs, Sheets, Slides)."""
    return mime_type in EXPORT_MIME_MAP


def is_unsupported_native(mime_type: str | None) -> bool:
    """True for native types that are neither folders nor exportable
    (Forms, Sites, Drawings, shortcuts, ...): still skipped entirely."""
    return is_google_native(mime_type) and not is_exportable(mime_type)


def export_target(mime_type: str, markdown: bool) -> tuple[str, str]:
    """Return (export_mime, extension) for an exportable *mime_type*.
    Markdown is offered only for Google Docs; Sheets/Slides ignore it."""
    if markdown and mime_type == _DOCUMENT_MIME:
        return (MARKDOWN_MIME, MARKDOWN_EXT)
    return EXPORT_MIME_MAP[mime_type]


def export_display_name(raw_name: str, mime_type: str, markdown: bool) -> str:
    """Project the on-disk / canonical remote leaf name for an export,
    e.g. 'Report' -> 'Report.docx' (or '.md' for a Doc with markdown on)."""
    _, ext = export_target(mime_type, markdown)
    return f"{raw_name}.{ext}"
```

Add `"EXPORT_MIME_MAP"`, `"MARKDOWN_MIME"`, `"MARKDOWN_EXT"`, `"is_exportable"`, `"is_unsupported_native"`, `"export_target"`, `"export_display_name"` to `__all__` (line 30).

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/offline/test_native_docs.py -q`
Expected: PASS. Then `python -m mypy src/bora/native_docs.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/bora/native_docs.py tests/offline/test_native_docs.py
git commit -m "feat(export): add exportable-native classifier and naming helpers"
```

---

## Task 2: `ExportOptions` and the projection helper (`export.py`)

**Files:**
- Create: `src/bora/export.py`
- Test: `tests/offline/test_export.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ExportOptions: enabled: bool = False; markdown: bool = False` with a classmethod `disabled()` and `from_config(conf) -> ExportOptions`.
  - `project_leaf_name(raw_name: str, mime: str | None, opts: ExportOptions) -> str` — if `opts.enabled and is_exportable(mime)` return `export_display_name(raw_name, mime, opts.markdown)`, else `raw_name` unchanged. This is the single function called at all three `compose_path` chokepoints.

- [ ] **Step 1: Write the failing test** — `tests/offline/test_export.py`:

```python
from __future__ import annotations

from bora.export import ExportOptions, project_leaf_name

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
PNG = "image/png"
FOLDER = "application/vnd.google-apps.folder"


def test_disabled_never_projects():
    opts = ExportOptions(enabled=False, markdown=True)
    assert project_leaf_name("Report", DOC, opts) == "Report"


def test_enabled_projects_exportables():
    opts = ExportOptions(enabled=True, markdown=False)
    assert project_leaf_name("Report", DOC, opts) == "Report.docx"
    assert project_leaf_name("Q3", SHEET, opts) == "Q3.xlsx"


def test_markdown_only_docs():
    opts = ExportOptions(enabled=True, markdown=True)
    assert project_leaf_name("Report", DOC, opts) == "Report.md"
    assert project_leaf_name("Q3", SHEET, opts) == "Q3.xlsx"


def test_non_exportables_untouched():
    opts = ExportOptions(enabled=True, markdown=True)
    assert project_leaf_name("photo", PNG, opts) == "photo"
    assert project_leaf_name("dir", FOLDER, opts) == "dir"
    assert project_leaf_name("x", None, opts) == "x"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_export.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bora.export'`.

- [ ] **Step 3: Implement `src/bora/export.py`**

```python
"""One-way export of native Google Workspace files to Office/Markdown.

A native Google Doc has no file extension in Drive; when export is enabled we
give it one on disk (``Report`` -> ``Report.docx``). To keep the sync index,
the ``PathIDCache`` key, and the on-disk name in agreement, the *projected*
leaf name is used as the canonical ``remote_path`` everywhere; the raw Drive
name survives only in ``PathIDCache.name`` (used for id-keyed API calls).
:func:`project_leaf_name` is the single projection applied at the three
``compose_path`` chokepoints (changes feed, folder listing, cache rebuild).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .native_docs import export_display_name, is_exportable

if TYPE_CHECKING:
    from .config.main import UserConfig


@dataclass(frozen=True)
class ExportOptions:
    enabled: bool = False
    markdown: bool = False

    @classmethod
    def disabled(cls) -> "ExportOptions":
        return cls(enabled=False, markdown=False)

    @classmethod
    def from_config(cls, conf: "UserConfig") -> "ExportOptions":
        return cls(
            enabled=bool(conf.get("sync", "export_google_docs")),
            markdown=bool(conf.get("sync", "export_google_docs_markdown")),
        )


def project_leaf_name(raw_name: str, mime: str | None, opts: ExportOptions) -> str:
    """Return the on-disk / canonical leaf name for *raw_name*.

    Unchanged unless export is enabled and *mime* is exportable, in which case
    the native extension is appended (``.md`` for a Doc when markdown is on).
    """
    if opts.enabled and is_exportable(mime):
        return export_display_name(raw_name, mime, opts.markdown)
    return raw_name
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/offline/test_export.py -q`
Expected: PASS. `python -m mypy src/bora/export.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/bora/export.py tests/offline/test_export.py
git commit -m "feat(export): add ExportOptions and project_leaf_name projection helper"
```

---

## Task 3: `version` field & export metadata in reads (`changes.py`, `core.py`)

**Files:**
- Modify: `src/bora/changes.py` (`FILE_FIELDS`, line 54)
- Modify: `src/bora/core.py` (`FileMetadata`, `to_file_metadata`)
- Test: `tests/offline/test_changes.py` (or a small `tests/offline/test_core_metadata.py`)

**Interfaces:**
- Produces:
  - `FILE_FIELDS` now includes `version`.
  - `FileMetadata.export_mime: str | None` (new field, default `None`) — set when the item is an exportable native doc; consumed by `client.download` to pick `export_media` + mime, and by sync to know an item is a download-only mirror.
  - `to_file_metadata(file, path, export_opts)` — when `export_opts.enabled and is_exportable(mime)`: `rev = str(file["version"])`, `content_hash = None`, `size = None`, `export_mime = export_target(mime, export_opts.markdown)[0]`. Otherwise unchanged (`rev = headRevisionId`, `export_mime = None`).

**Consumes:** `ExportOptions`, `is_exportable`, `export_target` from Tasks 1–2.

- [ ] **Step 1: Write the failing test** — `tests/offline/test_core_metadata.py`:

```python
from __future__ import annotations

from bora.core import to_file_metadata
from bora.export import ExportOptions

DOC = "application/vnd.google-apps.document"


def _doc_record():
    return {
        "id": "docid",
        "name": "Report",
        "mimeType": DOC,
        "modifiedTime": "2026-07-05T00:00:00.000Z",
        "version": "42",
        "md5Checksum": None,
        "headRevisionId": None,
        "size": None,
        "trashed": False,
    }


def test_export_metadata_uses_version_as_rev():
    md = to_file_metadata(_doc_record(), "/Report.docx", ExportOptions(True, False))
    assert md.rev == "42"
    assert md.content_hash is None
    assert md.export_mime == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_binary_metadata_unchanged():
    rec = {
        "id": "b", "name": "a.bin", "mimeType": "application/octet-stream",
        "modifiedTime": "2026-07-05T00:00:00.000Z", "version": "9",
        "md5Checksum": "abc", "headRevisionId": "rev9", "size": "3",
        "trashed": False,
    }
    md = to_file_metadata(rec, "/a.bin", ExportOptions(True, False))
    assert md.rev == "rev9"
    assert md.export_mime is None
    assert md.content_hash == "abc"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_core_metadata.py -q`
Expected: FAIL — `to_file_metadata()` takes no `export_opts` arg / `FileMetadata` has no `export_mime`.

- [ ] **Step 3: Implement**

In `src/bora/changes.py:54`, add `version` to `FILE_FIELDS`:

```python
FILE_FIELDS = (
    "id, name, parents, mimeType, modifiedTime, size, md5Checksum, version, "
    "shared, headRevisionId, trashed, lastModifyingUser(permissionId)"
)
```

In `src/bora/core.py`: add `export_mime: str | None = None` to the `FileMetadata` dataclass (place it with the other optional fields; keep it last so existing positional constructions are unaffected). Then update `to_file_metadata`:

```python
def to_file_metadata(
    file: dict[str, Any],
    path: str,
    export_opts: "ExportOptions" | None = None,
) -> FileMetadata:
    from .export import ExportOptions
    from .native_docs import export_target, is_exportable

    opts = export_opts or ExportOptions.disabled()
    mime = file.get("mimeType")
    export_mime: str | None = None
    if opts.enabled and is_exportable(mime):
        rev = str(file["version"])
        content_hash = None
        size = None
        export_mime = export_target(mime, opts.markdown)[0]
    else:
        rev = file.get("headRevisionId")
        content_hash = file.get("md5Checksum")
        size = int(file["size"]) if file.get("size") is not None else None
    # ... build and return FileMetadata(..., rev=rev, content_hash=content_hash,
    #     size=size, export_mime=export_mime) preserving all existing fields.
```

Adjust to the real current body of `to_file_metadata` (keep every field it already sets; only `rev`/`content_hash`/`size`/`export_mime` change). Every existing caller passing no `export_opts` keeps today's behavior.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/offline/test_core_metadata.py tests/offline/test_changes.py -q`
Expected: PASS. `python -m mypy src/bora/core.py src/bora/changes.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/bora/changes.py src/bora/core.py tests/offline/test_core_metadata.py
git commit -m "feat(export): add Drive version field and export_mime metadata projection"
```

---

## Task 4: Thread `ExportOptions` + projection through listing & change feed

**Files:**
- Modify: `src/bora/client.py` — `get_metadata` (582), `list_folder`/`list_folder_iterator` (910/935/952), `_bootstrap_recursive` (1040), `_scoped_recursive` (1149), `get_latest_cursor`
- Modify: `src/bora/changes.py` — `iter_remote_changes`, `_emit_apply` (202)
- Modify: `src/bora/path_cache.py` — `populate_from_listing` (336/406), `move` (247/278), `resolve_path` (300/321)
- Test: `tests/offline/test_path_cache.py`, `tests/offline/test_changes.py`, `tests/offline/test_client.py`

**Interfaces:**
- Consumes: `ExportOptions`, `project_leaf_name`, `is_exportable`, `is_unsupported_native`, and the `export_opts`-aware `to_file_metadata` from earlier tasks.
- `GoogleDriveClient` gains an `export_opts: ExportOptions` attribute (set in `__init__` from the sync engine / manager; defaults to `ExportOptions.disabled()`). All listing methods that today take `include_non_downloadable_files: bool = False` instead derive it from `self.export_opts.enabled` **for exportables only** — but keep the explicit flag for load-test callers; simplest: keep the flag and also pass `self.export_opts` where projection is needed.

This task is the heart of the projection. Implement in three coordinated edits, each with its own test-first cycle. Do them in this sub-order and commit once at the end.

### 4a — `path_cache` projection & raw-name preservation

- [ ] **Step 1: Failing test** in `tests/offline/test_path_cache.py`:

```python
from bora.export import ExportOptions

DOC = "application/vnd.google-apps.document"


def test_populate_projects_export_leaf(tmp_path):
    cache = _new_cache(tmp_path)  # follow existing test helper in this file
    root = cache.root_id
    items = [
        {"id": "d1", "name": "Report", "mimeType": DOC, "parents": [root]},
    ]
    cache.populate_from_listing(items, export_opts=ExportOptions(True, False))
    assert cache.path_to_id("/Report.docx") == "d1"
    assert cache.get_entry("d1").name == "Report"  # raw name preserved


def test_move_preserves_raw_name(tmp_path):
    cache = _new_cache(tmp_path)
    root = cache.root_id
    cache.populate_from_listing(
        [{"id": "d1", "name": "Report", "mimeType": DOC, "parents": [root]}],
        export_opts=ExportOptions(True, False),
    )
    cache.move("/Report.docx", "/Renamed.docx")
    assert cache.get_entry("d1").name == "Report"  # NOT "Renamed.docx"
```

Match `_new_cache`/fixtures to whatever `tests/offline/test_path_cache.py` already uses; if the file constructs the cache differently, mirror that.

- [ ] **Step 2: Run — expect FAIL** (`populate_from_listing` has no `export_opts`; projection missing).

Run: `python -m pytest tests/offline/test_path_cache.py -q`

- [ ] **Step 3: Implement**

`populate_from_listing(self, items, export_opts: ExportOptions | None = None)` — at the leaf-name computation (`path_cache.py:406`), replace the raw name in the *path* with the projection while keeping `name=raw_name`:

```python
from .export import ExportOptions, project_leaf_name
opts = export_opts or ExportOptions.disabled()
...
projected = project_leaf_name(raw_name, cid_mime, opts)   # cid_mime from the item
encoded_path = compose_path(parent_path, projected)
plan.append(PathCacheEntry(file_id=cid, remote_path=encoded_path,
                           name=raw_name, ...))  # name stays RAW
```
(The item's mime is available in the loop; if `populate_from_listing` doesn't currently carry mime per item, thread it from the raw dict.)

`move` (`path_cache.py:278-284`): stop recomputing `name` from the projected leaf. Preserve the existing row's `name`:

```python
# BEFORE: new_leaf_raw = decode_name(new_path.rsplit("/", 1)[-1])
existing = self.get_entry(file_id)
new_leaf_raw = existing.name if existing is not None else decode_name(...)
```

`resolve_path` (`path_cache.py:321`): it rebuilds a path from `name`; for exportable entries this would yield `/Report` and diverge from the stored `remote_path`. Simplest correct fix — return the stored `remote_path` column directly instead of rebuilding from `name` when they differ. Prefer the stored `remote_path` (it already carries the projection).

- [ ] **Step 4: Run — expect PASS.**

### 4b — `_emit_apply` passes exportables through with projection

- [ ] **Step 1: Failing test** in `tests/offline/test_changes.py` — a Doc change under export produces a projected APPLY entry, and Forms/shortcuts are still dropped:

```python
from bora.export import ExportOptions
DOC = "application/vnd.google-apps.document"
FORM = "application/vnd.google-apps.form"


def test_exportable_doc_emitted_with_projection(...):
    # build a change dict for a Doc "Report" in an in-scope parent, then:
    entries = list(iter_remote_changes(changes, path_cache,
                                       export_opts=ExportOptions(True, False)))
    paths = [e.path_display for e in entries]
    assert "/Report.docx" in paths


def test_form_still_dropped(...):
    entries = list(iter_remote_changes(changes, path_cache,
                                       export_opts=ExportOptions(True, False)))
    assert all(not e.path_display.endswith("form-name") for e in entries)
```

Flesh out the change-dict construction to match existing `test_changes.py` helpers.

- [ ] **Step 2: Run — expect FAIL** (`iter_remote_changes` has no `export_opts`; Docs dropped).

- [ ] **Step 3: Implement** in `src/bora/changes.py`:

Thread `export_opts: ExportOptions | None = None` through `iter_remote_changes` → `_emit_apply`. In `_emit_apply` (line 224-227) change the native gate:

```python
opts = export_opts or ExportOptions.disabled()
mime = file.get("mimeType")
if is_unsupported_native(mime):
    return True                     # drop Forms/Sites/Drawings/shortcuts
if is_google_native(mime) and not (opts.enabled and is_exportable(mime)):
    return True                     # export disabled -> drop exportables too
```

At the `compose_path` site (line 234) project the leaf:

```python
projected = project_leaf_name(file["name"], mime, opts)
new_path = compose_path(parent_path, projected)
```

Keep `path_cache.set(new_path, file["id"], parent_id, file["name"], is_folder)` — cache key = projected `new_path`, `name` = raw `file["name"]`. Pass `export_opts` into the `to_file_metadata(file, new_path, export_opts=opts)` call so `rev`/`export_mime` are set.

- [ ] **Step 4: Run — expect PASS.**

### 4c — client listing/metadata opened for exportables + projection

- [ ] **Step 1: Failing test** in `tests/offline/test_client.py` — with export on, a native Doc is listed as `Report.docx` and `get_metadata("/Report.docx")` returns file metadata (not `None`); a Form is still absent. (Requires the FakeDrive export support from Task 8 — if executing strictly in order, write these assertions and mark the test `xfail(strict=False)` until Task 8 lands, then flip. Alternatively land Task 8's FakeDrive changes first; see note in Task 8.)

- [ ] **Step 2: Run — expect FAIL/xfail.**

- [ ] **Step 3: Implement** in `src/bora/client.py`:

- Add `self.export_opts: ExportOptions = ExportOptions.disabled()` in `__init__`; add a setter or make it a public attribute the sync engine assigns.
- `get_metadata` (617-620): replace `if is_google_native(mime): return None` with:
  ```python
  if is_unsupported_native(mime):
      return None
  if is_google_native(mime) and not self.export_opts.enabled:
      return None
  ...
  return to_file_metadata(file, cased_path, export_opts=self.export_opts)
  ```
- `list_folder_iterator` (1024): change the skip to
  ```python
  if is_unsupported_native(mime) or (
      is_google_native(mime) and not self.export_opts.enabled
  ):
      continue
  child_path = compose_path(remote_path, project_leaf_name(file["name"], mime, self.export_opts))
  ...
  entries.append(to_file_metadata(file, child_path, export_opts=self.export_opts))
  ```
- `_bootstrap_recursive` (1091-1099) and `_scoped_recursive` (1194): same gate — keep exportables when `export_opts.enabled`, drop `is_unsupported_native` always. `populate_from_listing(filtered, export_opts=self.export_opts)`.
- Keep the existing `include_non_downloadable_files` parameter for the load-test callers, but OR it with `self.export_opts.enabled` where it gates exportables.

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit (whole Task 4)**

```bash
git add src/bora/client.py src/bora/changes.py src/bora/path_cache.py \
        tests/offline/test_client.py tests/offline/test_changes.py tests/offline/test_path_cache.py
git commit -m "feat(export): project export leaf name across listing, change feed and path cache"
```

---

## Task 5: `download()` export branch (`client.py`)

**Files:**
- Modify: `src/bora/client.py` — `download` (708)
- Modify: `tests/fake_drive.py` — add `export_media` (can also be done as the first half of Task 8; if so, do Task 8's FakeDrive step before this test)
- Test: `tests/offline/test_client.py`

**Interfaces:**
- Consumes: `FileMetadata.export_mime`, `export_target`, `is_exportable`.
- Produces: `download()` for an exportable file streams `files().export_media(fileId, mimeType=export_mime)` to the `.bora-part` sidecar, computes the md5 of the exported bytes, `os.replace`s into place, and returns a `FileMetadata` whose `content_hash` = that md5, `rev` = `str(version)`, `export_mime` set, `path_display` = the passed (already-projected) `remote_path`, and `size` = exported byte count.

- [ ] **Step 1: Failing test** in `tests/offline/test_client.py`:

```python
def test_download_exports_native_doc(client_with_export, fake, tmp_path):
    # fake native Doc "Report" with content b"exported-bytes" for export mime docx
    file_id = fake.add_native(name="Report", kind="document")
    fake.set_export_bytes(file_id, "application/vnd.openxmlformats-"
                          "officedocument.wordprocessingml.document", b"DOCX")
    dest = str(tmp_path / "Report.docx")
    md = client_with_export.download("/Report.docx", dest, file_id=file_id)
    assert open(dest, "rb").read() == b"DOCX"
    import hashlib
    assert md.content_hash == hashlib.md5(b"DOCX").hexdigest()
    assert md.export_mime is not None
```

- [ ] **Step 2: Run — expect FAIL** (download raises `UnsupportedFileError`).

- [ ] **Step 3: Implement** — replace the raise at `client.py:750-757` with an export branch:

```python
mime = meta_file.get("mimeType")
if is_unsupported_native(mime):
    raise UnsupportedFileError(...)                      # keep for Forms/Sites
if is_exportable(mime) and self.export_opts.enabled:
    export_mime, _ext = export_target(mime, self.export_opts.markdown)
    request = self._get_service().files().export_media(
        fileId=file_id, mimeType=export_mime
    )
    hasher = hashlib.md5()
    with open(tmp_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=_DOWNLOAD_CHUNK_SIZE)
        done = False
        while not done:
            status, done = self._retry_transient(lambda: downloader.next_chunk())
            if sync_event is not None and status is not None:
                sync_event.completed = status.resumable_progress
    with open(tmp_path, "rb") as fh:                      # hash the exported bytes
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(chunk)
    os.replace(tmp_path, local_path)
    md = to_file_metadata(meta_file, remote_path, export_opts=self.export_opts)
    md.content_hash = hasher.hexdigest()
    md.size = os.path.getsize(local_path)
    return md
# else: existing get_media path (unchanged), for binary files
```

There is **no server md5 to verify** for exports (the existing `DataCorruptionError` md5 check applies only to the `get_media` path — leave it there). Ensure `hashlib` and `os` are imported (they are used elsewhere in the module already).

- [ ] **Step 4: Run — expect PASS.** `python -m mypy src/bora/client.py`.

- [ ] **Step 5: Commit**

```bash
git add src/bora/client.py tests/offline/test_client.py tests/fake_drive.py
git commit -m "feat(export): export native docs via export_media with local md5"
```

---

## Task 6: `download_only` column + schema-drift guard

**Files:**
- Modify: `src/bora/models.py` — `IndexEntry` (473); `SyncEvent` fields + `from_metadata` (290); the two `IndexEntry(...)` construction sites in `sync.py` (`update_index_from_sync_event` 1067, `update_index_from_remote_metadata` 1109)
- Modify: `src/bora/database/orm.py` — `Manager` init schema-drift guard
- Test: `tests/offline/test_models.py` (or existing), `tests/offline/test_orm.py` (create if absent)

**Interfaces:**
- Produces:
  - `IndexEntry.download_only = Column(SqlBool())` (nullable; absent/`None` ⇒ treated as `False`). Use the existing boolean SQL type (`SqlBool` / whatever `bora.database.types` provides; check how `shared`/booleans are stored elsewhere — if none, store as `SqlInt` 0/1 and expose a `bool` property).
  - `SyncEvent.download_only` column + `from_metadata` sets it `True` when `md.export_mime is not None`.
  - Schema-drift guard: on `Manager` init, if `PRAGMA table_info(<table>)` column set ≠ `{c.name for c in model.__columns__}`, `DROP TABLE` then let `create_table_if_not_exists` recreate.

- [ ] **Step 1: Failing test** — `tests/offline/test_orm.py`:

```python
def test_schema_drift_drops_and_recreates(tmp_path):
    # 1. create a table with an OLD column set (simulate pre-feature DB)
    # 2. open a Manager for the model that now has an EXTRA column
    # 3. assert the table was dropped+recreated (row count 0, new column present)
    ...
```

And in `tests/offline/test_models.py` a round-trip test that an `IndexEntry` with `download_only=True` persists and reads back `True`, and a legacy row (column absent in a hand-made table) reads back as `False`/`None`-coerced.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**

`models.py`: add to `IndexEntry` after `symlink_target`:
```python
download_only = Column(SqlInt())  # 1 = exported read-only mirror; None/0 = normal
```
Add a property:
```python
@property
def is_download_only(self) -> bool:
    return bool(self.download_only)
```
Add matching `download_only` column to `SyncEvent`; in `from_metadata` set `download_only = 1 if getattr(md, "export_mime", None) else None`. Update the two `IndexEntry(...)` constructors in `sync.py` to pass `download_only=event.download_only` (and the remote-metadata variant to derive from `md.export_mime`).

`database/orm.py` — in `Manager.__init__` (near line 243, before/after `create_table_if_not_exists`):
```python
def _guard_schema(self) -> None:
    rows = self.db.execute(f"PRAGMA table_info({self.table_name});").fetchall()
    if not rows:
        return  # fresh DB; create_table will make it
    existing = {r[1] for r in rows}
    expected = {c.name for c in self.model.__columns__}
    if existing != expected:
        self.db.executescript(f"DROP TABLE IF EXISTS {self.table_name};")
```
Call `self._guard_schema()` immediately before `create_table_if_not_exists()`. Because these tables are a derived cache, dropping is safe; the empty index triggers the existing remote bootstrap on next sync. (Match the real `Database` execute API in `bora.database`.)

- [ ] **Step 4: Run — expect PASS.** Run the full offline suite to confirm no regression from the new column:

Run: `python -m pytest tests/offline -q`
Expected: PASS (same pass/deselect counts as baseline, plus the new tests).

- [ ] **Step 5: Commit**

```bash
git add src/bora/models.py src/bora/database/orm.py src/bora/sync.py \
        tests/offline/test_orm.py tests/offline/test_models.py
git commit -m "feat(export): add download_only index column with schema-drift guard"
```

---

## Task 7: Read-only + `download_only` on export download (`sync.py`)

**Files:**
- Modify: `src/bora/sync.py` — `_on_remote_file` (3695), after the `move` into place (~3799) and index write (3801)
- Test: `tests/offline/test_sync.py`

**Interfaces:**
- Consumes: `SyncEvent.download_only`, `FileMetadata.export_mime`.
- Produces: after an export download lands, the local file is `chmod 0o444` (read-only) and the index entry has `download_only=1`. Normal (binary) downloads are unchanged.

- [ ] **Step 1: Failing test** in `tests/offline/test_sync.py` — drive a full download cycle for a native Doc through the FakeDrive with export on; assert the local `Report.docx` exists, is read-only (`not os.access(path, os.W_OK)` / mode `& 0o222 == 0`), and `get_index_entry("/report.docx").is_download_only is True`. Reuse the existing sync-test harness in this file (the `sync` fixture wired to a FakeDrive).

- [ ] **Step 2: Run — expect FAIL** (file writable; `download_only` unset).

- [ ] **Step 3: Implement** in `_on_remote_file`, in the non-symlink branch after the `move(...)` at 3793-3799 and before/around `update_index_from_sync_event(event)` at 3801:

```python
if event.download_only:
    with self.fs_events.ignore(FileModifiedEvent(event.local_path), recursive=False):
        try:
            os.chmod(event.local_path, 0o444)
        except OSError as err:
            self._logger.debug("Cannot set read-only: %s", err)
```
`event.download_only` is already set by `SyncEvent.from_metadata` (Task 6) because `md.export_mime` is set by `download` (Task 5). `update_index_from_sync_event` already persists `download_only` (Task 6). Ensure the `chmod` happens inside an `fs_events.ignore` so the permission change doesn't register as a local modification.

- [ ] **Step 4: Run — expect PASS.** `python -m pytest tests/offline/test_sync.py -q`.

- [ ] **Step 5: Commit**

```bash
git add src/bora/sync.py tests/offline/test_sync.py
git commit -m "feat(export): mark exported mirrors read-only and download-only on disk"
```

---

## Task 8: FakeDrive native-doc + export_media support (test infra)

> Note: parts of this are needed by Tasks 4c/5/7. If executing in strict order, land this task's FakeDrive additions first (they're test-only, no production risk) or move it to position 4.5. It is listed here as its own reviewable unit.

**Files:**
- Modify: `tests/fake_drive.py`
- Test: `tests/offline/test_fake_drive.py` (create — a couple of sanity tests)

**Interfaces:**
- Produces on `FakeDrive`:
  - `add_native(self, name, kind, parent="/", version="1") -> str` where `kind ∈ {"document","spreadsheet","presentation","form"}`; sets `mimeType` to the matching `application/vnd.google-apps.*`, `md5Checksum=None`, `headRevisionId=None`, `version=version`.
  - `set_export_bytes(self, file_id, export_mime, content: bytes)` — registers bytes served by `files().export_media`.
  - `_record_change` bumps `version` (string int increment) for native docs on edit, so change-feed tests can simulate a server-side edit.
  - `service().files().export_media(fileId=..., mimeType=...)` returns a media request whose `next_chunk()` streams the registered bytes (mirror the existing `get_media` `_MediaRequest`).

- [ ] **Step 1: Sanity test** — `tests/offline/test_fake_drive.py`: add a native doc, register export bytes, call `service.files().export_media(...).execute()`-style read via the same `_read_media` helper, assert the bytes round-trip; assert `add_native` sets `md5Checksum=None`, `version="1"`.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** — extend `FakeDrive` following the existing `add_file`/`_insert`/`_MediaRequest` patterns (fake_drive.py:244-370). Add an `_export_bytes: dict[tuple[str, str], bytes]` map keyed by `(file_id, export_mime)`. Add `export_media` to the fake `files()` resource returning a media request that serves `self._export_bytes[(file_id, mime)]`. Make `version` participate in `_project`/`_parse_field_keys` so `files.get`/`files.list` return it.

- [ ] **Step 4: Run — expect PASS.** Then re-run any Task 4c/5/7 tests that depended on this (flip any temporary `xfail`).

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(export): model native docs and export_media in FakeDrive"
```

---

## Task 9: Suppress upload + conflict-copy-on-edit for `download_only` (`sync.py`)

**Files:**
- Modify: `src/bora/sync.py` — local-event filter in `apply_local_changes` (~2016-2018) and a new edit-detection branch
- Test: `tests/offline/test_sync.py`

**Interfaces:**
- Consumes: `IndexEntry.is_download_only`, `_local_cc_filename` (3686), `generate_cc_name`, the reindex/rescan helpers already used at 3762.
- Produces:
  - A local change to a `download_only` file is **never uploaded**.
  - If the change actually modified content (local md5 ≠ indexed `content_hash`), the engine renames the local file to a conflicted-copy name, **clears** `download_only` on the renamed file (so the user's edited copy becomes a normal, uploadable file), and re-exports the server version to the original read-only name on the next download pass (queue a rescan / re-download of the original `remote_path`).
  - A pure metadata touch (e.g. the `chmod` itself, or mtime change with identical hash) is ignored — no conflict.

- [ ] **Step 1: Failing tests** in `tests/offline/test_sync.py`:
  1. `test_download_only_never_uploads`: export a Doc, then simulate a local content change; run the local-change apply; assert no upload SyncEvent was produced for that path and the server file is unchanged.
  2. `test_download_only_edit_makes_conflict_copy`: same setup, edited content differs; assert a `… (… conflicted copy …).docx` file appears, that it is writable and **not** `download_only`, and that the original path is queued for re-export (index still `download_only` for the original / a rescan was scheduled).

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**

In `apply_local_changes` at the filter (`sync.py:2016-2018`), add a `download_only` branch **before** the existing exclusion `continue`:

```python
for event in sync_events:
    if self.is_excluded(event.local_path) or self.is_mignore(event):
        continue

    entry = self.get_index_entry(event.remote_path_lower)
    if entry is not None and entry.is_download_only:
        self._handle_download_only_local_change(event, entry)
        continue
    ...
```

Add the handler near `_local_cc_filename` (after 3693):

```python
def _handle_download_only_local_change(self, event: SyncEvent, entry: IndexEntry) -> None:
    """A read-only export was changed locally. Never upload it. If the content
    actually differs from the last export, preserve the user's copy as a
    conflicted copy (a normal, uploadable file) and re-export the server
    version to the original read-only name."""
    if event.is_deleted:
        # User deleted the mirror; re-export on next download pass.
        self.rescan(event.local_path)
        return
    local_hash = self.get_local_hash(event.local_path)
    if local_hash == entry.content_hash:
        return  # touch only (e.g. the chmod); nothing to do
    cc_path = self._local_cc_filename(event.local_path, None)
    with self.fs_events.ignore(FileMovedEvent(event.local_path, cc_path)):
        move(event.local_path, cc_path, raise_error=True)
    try:
        os.chmod(cc_path, 0o644)  # user's copy is editable again
    except OSError:
        pass
    # cc_path is a brand-new local file with no index entry -> normal upload path
    self.rescan(cc_path)
    # Force re-export of the original: drop its index rev so the next download
    # cycle re-downloads it (rev mismatch), or rescan the (now-absent) original.
    self.rescan(event.local_path)
    self._logger.info("Preserved local edit of read-only export as %s", cc_path)
```

Match the exact `move`, `rescan`, and `fs_events.ignore` signatures already used in `_on_remote_file` (3749-3762). The key correctness point: the conflicted copy must end up with **no** `download_only` index entry (it's a new path → new normal entry), and the original path must be re-fetched from the server. If `rescan(event.local_path)` alone doesn't re-trigger a download for the original (because the server `rev` is unchanged), instead delete the original's index `rev` via a targeted helper so `_check_download_conflict` sees a mismatch — verify which mechanism actually re-exports in the test and keep the one that does.

- [ ] **Step 4: Run — expect PASS.** `python -m pytest tests/offline/test_sync.py -q`.

- [ ] **Step 5: Commit**

```bash
git add src/bora/sync.py tests/offline/test_sync.py
git commit -m "feat(export): suppress upload and conflict-copy local edits of read-only mirrors"
```

---

## Task 10: Config toggles + wiring `ExportOptions` into client

**Files:**
- Modify: `src/bora/config/main.py` — `sync` defaults (38-45)
- Modify: `src/bora/sync.py` / `src/bora/manager.py` — build `ExportOptions.from_config` and assign `client.export_opts`; re-bootstrap on enable
- Test: `tests/offline/test_config.py` (or existing config test), `tests/offline/test_sync.py`

**Interfaces:**
- Produces: `sync.export_google_docs = True`, `sync.export_google_docs_markdown = True`; `client.export_opts` reflects them; toggling calls `ExportOptions.from_config`.

- [ ] **Step 1: Failing test** — assert defaults are present and `True`:

```python
def test_export_defaults_present(...):
    conf = _load_default_config(...)
    assert conf.get("sync", "export_google_docs") is True
    assert conf.get("sync", "export_google_docs_markdown") is True
```
And a sync-level test that `client.export_opts.enabled` is `True` when the config is default.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**

`config/main.py:38-45` add two keys (NO `CONF_VERSION` bump — additive):
```python
    "sync": {
        "path": "",
        "excluded_items": [],
        "max_cpu_percent": 20.0,
        "keep_history": 60 * 60 * 24 * 7,
        "upload": True,
        "download": True,
        "export_google_docs": True,           # export native Docs/Sheets/Slides
        "export_google_docs_markdown": True,  # Docs -> .md (else .docx)
    },
```
Where the sync engine constructs / owns the `GoogleDriveClient` (see `manager.py:205`/`sync.py:615` config-read pattern), set:
```python
self.client.export_opts = ExportOptions.from_config(self._conf)
```
and thread `self.client.export_opts` into `iter_remote_changes(..., export_opts=self.client.export_opts)` at the change-cycle call site, and into `list_folder_iterator(..., include_non_downloadable_files=self.client.export_opts.enabled)` at bootstrap. When the toggle transitions off→on (setter), schedule a reindex (same mechanism the schema-drift guard relies on: drop/rebuild index or reset the sync cursor) so previously-skipped natives are pulled in.

- [ ] **Step 4: Run — expect PASS.** Full suite: `python -m pytest tests/offline -q`.

- [ ] **Step 5: Commit**

```bash
git add src/bora/config/main.py src/bora/sync.py src/bora/manager.py tests/offline/test_config.py
git commit -m "feat(export): add export config toggles (default on) and wire into client"
```

---

## Task 11: `Bora` facade properties + CLI `export` group

**Files:**
- Modify: `src/bora/main.py` — properties `export_google_docs`, `export_google_docs_markdown` (getter/setter over `_conf`)
- Modify: `src/bora/cli/cli_settings.py` — new `export` group
- Modify: `src/bora/cli/cli_main.py` — register the group
- Test: `tests/offline/` CLI test (mirror an existing `cli_settings` test if present) or a `main` facade test

**Interfaces:**
- Consumes: config keys from Task 10.
- Produces: `m.export_google_docs` / `m.export_google_docs_markdown` bool properties; `bora export docs [on|off]` and `bora export markdown [on|off]` (or get/set) CLI commands.

- [ ] **Step 1: Failing test** — a facade test: set `m.export_google_docs = False`, assert `conf.get("sync","export_google_docs") is False`; and a `CliRunner` test invoking `export docs off` then `export docs` echoes the state. Model on any existing `cli_settings` test.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**

In `main.py`, add properties (mirror `bandwidth_limit_up`):
```python
@property
def export_google_docs(self) -> bool:
    return bool(self._conf.get("sync", "export_google_docs"))

@export_google_docs.setter
def export_google_docs(self, value: bool) -> None:
    self._conf.set("sync", "export_google_docs", bool(value))

# ...and the same pair for export_google_docs_markdown
```

In `cli_settings.py`, copy the `bandwidth_limit` group shape (149-190) into an `export` group with two `on/off`-or-show subcommands using `@inject_proxy(fallback=True, existing_config=True)`, `ok(...)` on set and `echo(...)` on show. Register in `cli_main.py` alongside the others:
```python
from .cli_settings import autostart, bandwidth_limit, excluded, export, notify
...
main.add_command(export, section="Settings")
```

- [ ] **Step 4: Run — expect PASS.** `python -m pytest tests/offline -q`; `python -m flake8 src/bora`; `python -m mypy src/bora`.

- [ ] **Step 5: Commit**

```bash
git add src/bora/main.py src/bora/cli/cli_settings.py src/bora/cli/cli_main.py tests/offline
git commit -m "feat(export): expose export toggles on Bora facade and CLI"
```

---

## Task 12: Full-cycle integration test + live spot-check

**Files:**
- Test: `tests/offline/test_sync.py` (integration) and manual live verification
- Docs: update `TODO.md` / `PLAN.md` breadcrumbs noting the feature landed

**Interfaces:** none new — this is verification.

- [ ] **Step 1: Integration test (FakeDrive, end-to-end)** — one test that:
  1. adds a native Doc, Sheet, Slides via FakeDrive with export on;
  2. runs a full download cycle; asserts `Report.md` (markdown default on), `Book.xlsx`, `Deck.pptx` exist, all read-only, all `download_only` in the index;
  3. bumps the Doc's `version` (server edit) and re-runs the cycle; asserts the mirror was re-exported (content updated) and stays read-only;
  4. flips `export_google_docs_markdown` off, re-runs; asserts the Doc mirror becomes `Report.docx` (old `.md` mirror removed / renamed by `remote_id`);
  5. asserts a Form added to FakeDrive never appears on disk.

- [ ] **Step 2: Run — expect PASS.** `python -m pytest tests/offline/test_sync.py -q`.

- [ ] **Step 3: Full gate**

Run:
```bash
python -m pytest tests/offline -q
python -m mypy src/bora
python -m flake8 src/bora
```
Expected: offline suite green (baseline pass count + new tests, same deselect count), mypy + flake8 at baseline parity.

- [ ] **Step 4: Live spot-check** (cumulus-test account, per prior-stage practice — controller runs this, not a subagent). Link the test account, create/point at a real Google Doc, Sheet, and Slides:
  - Confirm `Report.md`, `<sheet>.xlsx`, `<slides>.pptx` appear, are read-only, and open correctly.
  - Edit the Google Doc in the web UI; confirm the local mirror auto-updates within a poll interval and stays read-only.
  - Confirm no `.md`/`.docx`/`.xlsx`/`.pptx` is ever uploaded back (no new Drive file appears).
  - Toggle markdown off via `bora export markdown off`; confirm the Doc re-exports as `.docx`.
  - **Memory caution:** do not run `pytest tests/offline` while the smoke-test account is linked (keyring collision wipes the linked account — see memory `feedback_keyring_test_collision`).

- [ ] **Step 5: Commit**

```bash
git add tests/offline/test_sync.py TODO.md PLAN.md
git commit -m "test(export): end-to-end export cycle integration test; docs"
```

---

## Self-Review (completed during authoring)

**Spec coverage:** every spec section maps to a task — classifier/naming (§3.1 → T1), projection (§ path-identity → T2/T4), export download (§3.2 → T5), change signal `version` (§4 → T3), `download_only` + schema-drift (§3.4 → T6), read-only (§5 → T7), upload-suppression + conflict-copy (§5 → T9), markdown toggle (§6 → T1/T3/T5/T10), config/CLI (§7 → T10/T11), error handling (§7 → T5 keeps `UnsupportedFileError` for Forms; `exportSizeLimitExceeded` rides existing `convert_api_errors`/SyncError path — call out in T5 review), testing (§8 → every task + T12). FakeDrive infra (T8) is an enabling task the spec implies.

**Type consistency:** `ExportOptions(enabled, markdown)`, `project_leaf_name(raw_name, mime, opts)`, `export_target(mime, markdown) -> (mime, ext)`, `export_display_name(raw_name, mime, markdown)`, `FileMetadata.export_mime`, `IndexEntry.download_only` / `.is_download_only`, `SyncEvent.download_only` are used consistently across tasks.

**Ordering caveat (flagged for the executor):** Tasks 4c, 5, and 7 exercise FakeDrive export support built in Task 8. Land Task 8's FakeDrive additions first (they are test-only) or reorder Task 8 to position 4.5. This is noted at the top of Tasks 5 and 8.

**Open risk to verify empirically in T12:** that the Drive `version` field is present and monotonically increments on native-doc edits (design assumption replacing `headRevisionId`). If a real edit does not bump `version` as expected, fall back to `modifiedTime` as the `rev` source — a one-line change in `to_file_metadata` (T3).
