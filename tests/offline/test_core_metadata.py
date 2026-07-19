from __future__ import annotations

from bora.changes import to_file_metadata
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
