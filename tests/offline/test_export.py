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
