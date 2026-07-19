"""Google-native (Workspace) document detection and filtering.

Google Docs, Sheets, Slides, etc. have MIME types under
``application/vnd.google-apps.*``. They have no downloadable bytes, no
``md5Checksum``, and cannot be synced as regular files. Drive shortcuts
(``application/vnd.google-apps.shortcut``) are also non-syncable.

Bora's MVP strategy: **skip** these items entirely. They are filtered
out of change-feed results and listing results before the sync engine
sees them.

The predicate is a wildcard over the ``application/vnd.google-apps.``
prefix (excluding the folder type) rather than an enumerated set, so a
native MIME subtype Google ships in the future is handled without a
code change.

Filter points (see REVIEW.md Issue #5 and the Stage 3 spec §4.2):
  1. ``client.list_folder_iterator`` / ``list_folder`` — strip from results
  2. ``changes.iter_remote_changes`` — strip from change entries
  3. ``client.get_metadata`` — return ``None`` for native docs
  4. ``client.download`` — raise an error if somehow reached
  5. ``client.list_folder_iterator(recursive=True)`` — pre-filter before
     ``path_cache.populate_from_listing``
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "GOOGLE_NATIVE_PREFIX",
    "GOOGLE_FOLDER_MIME_TYPE",
    "GOOGLE_SHORTCUT_MIME_TYPE",
    "EXPORT_MIME_MAP",
    "MARKDOWN_MIME",
    "MARKDOWN_EXT",
    "is_google_native",
    "is_google_folder",
    "is_shortcut",
    "filter_native_docs",
    "is_exportable",
    "is_unsupported_native",
    "export_target",
    "export_display_name",
]

# https://developers.google.com/drive/api/guides/mime-types
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"

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


def is_google_native(mime_type: str | None) -> bool:
    """Return ``True`` if *mime_type* is a non-downloadable Google-native
    type.

    Any ``application/vnd.google-apps.*`` type qualifies **except** the
    folder type. Drive shortcuts qualify (they match the prefix and are
    not folders). Folders return ``False`` — they are handled normally
    by the sync engine.
    """
    if mime_type is None:
        return False
    return (
        mime_type.startswith(GOOGLE_NATIVE_PREFIX)
        and mime_type != GOOGLE_FOLDER_MIME_TYPE
    )


def is_google_folder(mime_type: str | None) -> bool:
    """Return ``True`` if *mime_type* is the Google Drive folder type.

    ``None`` safely returns ``False`` via the ``==`` comparison.
    """
    return mime_type == GOOGLE_FOLDER_MIME_TYPE


def is_shortcut(mime_type: str | None) -> bool:
    """Return ``True`` if *mime_type* is the Drive shortcut type.

    Shortcuts are already covered by :func:`is_google_native`; this
    helper exists for call sites that want to log or branch on shortcuts
    specifically.
    """
    return mime_type == GOOGLE_SHORTCUT_MIME_TYPE


def filter_native_docs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of *items* with Google-native documents removed.

    Folders and regular (binary) files are kept. This is the primary
    filter applied to ``files.list`` and ``changes.list`` results before
    they reach the sync engine.
    """
    return [
        item
        for item in items
        if not is_google_native(item.get("mimeType"))
    ]


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
