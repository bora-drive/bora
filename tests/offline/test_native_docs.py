"""Unit tests for bora.native_docs."""

from __future__ import annotations

import pytest

from bora.native_docs import (
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


def test_filter_native_docs_keeps_items_without_mimetype():
    items = [{"id": "1"}, {"id": "2", "mimeType": "application/vnd.google-apps.document"}]
    kept = filter_native_docs(items)
    assert [it["id"] for it in kept] == ["1"]


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
