"""Google-native (Workspace) document detection and filtering.

Google Docs, Sheets, Slides, etc. have special MIME types under
``application/vnd.google-apps.*``. They have no downloadable bytes, no
``md5Checksum``, and cannot be synced as regular files.

Cumulus's MVP strategy: **skip** these items entirely. They are filtered out
of change-feed results and listing results before the sync engine sees them.
A future version may export them to Office or PDF formats.

Filter points (see REVIEW.md Issue #5):
  1. ``client.list_folder_iterator`` / ``list_folder`` — strip from results
  2. ``client.list_remote_changes_iterator`` — strip from change entries
  3. ``client.get_metadata`` — return ``None`` for native docs
  4. ``client.download`` — raise an error if somehow reached
  5. ``path_cache.populate_from_listing`` — already skips non-folder native
     types because they are excluded from the listing
  6. ``sync.py`` change processing — secondary guard in ``_apply_remote_changes``
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "GOOGLE_NATIVE_MIME_TYPES",
    "GOOGLE_FOLDER_MIME_TYPE",
    "is_google_native",
    "is_google_folder",
    "filter_native_docs",
]

# https://developers.google.com/drive/api/guides/mime-types
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

GOOGLE_NATIVE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.google-apps.document",       # Google Docs
        "application/vnd.google-apps.spreadsheet",    # Google Sheets
        "application/vnd.google-apps.presentation",   # Google Slides
        "application/vnd.google-apps.drawing",        # Google Drawings
        "application/vnd.google-apps.form",           # Google Forms
        "application/vnd.google-apps.site",           # Google Sites
        "application/vnd.google-apps.jam",            # Google Jamboard
        "application/vnd.google-apps.script",         # Google Apps Script
        "application/vnd.google-apps.map",            # Google My Maps
        "application/vnd.google-apps.shortcut",       # Drive shortcuts
        "application/vnd.google-apps.drive-sdk",      # 3rd-party shortcuts
    }
)
"""MIME types for Google-native items that cannot be downloaded as raw bytes.
Does **not** include ``application/vnd.google-apps.folder``."""


def is_google_native(mime_type: str | None) -> bool:
    """Return ``True`` if *mime_type* is a non-downloadable Google-native type.

    Folders return ``False`` — they are handled normally by the sync engine.
    """
    return mime_type in GOOGLE_NATIVE_MIME_TYPES


def is_google_folder(mime_type: str | None) -> bool:
    """Return ``True`` if *mime_type* is the Google Drive folder type."""
    return mime_type == GOOGLE_FOLDER_MIME_TYPE


def filter_native_docs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of *items* with Google-native documents removed.

    Folders and regular (binary) files are kept.  This is the primary filter
    that should be applied to ``files.list`` and ``changes.list`` results
    before they reach the sync engine.
    """
    return [
        item
        for item in items
        if not is_google_native(item.get("mimeType"))
    ]
