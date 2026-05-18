"""Unit tests for cumulus.native_docs."""

from __future__ import annotations

import pytest

from cumulus.native_docs import (
    GOOGLE_FOLDER_MIME_TYPE,
    GOOGLE_SHORTCUT_MIME_TYPE,
    filter_native_docs,
    is_google_folder,
    is_google_native,
    is_shortcut,
)

KNOWN_NATIVE = [
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.drawing",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.jam",
    "application/vnd.google-apps.script",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.drive-sdk",
]


@pytest.mark.parametrize("mime", KNOWN_NATIVE)
def test_known_native_types_match(mime):
    assert is_google_native(mime) is True


def test_folder_is_not_native():
    assert is_google_native(GOOGLE_FOLDER_MIME_TYPE) is False


def test_future_native_type_matches_via_wildcard():
    # A type Google has not shipped yet must still be treated as native.
    assert is_google_native("application/vnd.google-apps.notebook") is True


def test_regular_file_is_not_native():
    assert is_google_native("image/png") is False
    assert is_google_native("application/pdf") is False


def test_none_and_empty_do_not_crash():
    assert is_google_native(None) is False
    assert is_google_native("") is False


def test_is_google_folder():
    assert is_google_folder(GOOGLE_FOLDER_MIME_TYPE) is True
    assert is_google_folder("image/png") is False
    assert is_google_folder(None) is False


def test_is_shortcut():
    assert is_shortcut(GOOGLE_SHORTCUT_MIME_TYPE) is True
    assert is_shortcut("application/vnd.google-apps.document") is False
    assert is_shortcut(None) is False


def test_filter_native_docs_keeps_folders_and_files():
    items = [
        {"id": "1", "mimeType": "image/png"},
        {"id": "2", "mimeType": GOOGLE_FOLDER_MIME_TYPE},
        {"id": "3", "mimeType": "application/vnd.google-apps.document"},
        {"id": "4", "mimeType": "application/vnd.google-apps.shortcut"},
    ]
    kept = filter_native_docs(items)
    assert [it["id"] for it in kept] == ["1", "2"]
