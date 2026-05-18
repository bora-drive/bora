"""Unit tests for cumulus.changes."""

from __future__ import annotations

from datetime import datetime, timezone

from cumulus.changes import (
    parse_drive_time,
    to_file_metadata,
    to_folder_metadata,
)
from cumulus.core import FileMetadata, FolderMetadata

SAMPLE_FILE = {
    "id": "F1",
    "name": "report.pdf",
    "parents": ["ROOT"],
    "mimeType": "application/pdf",
    "modifiedTime": "2026-04-30T04:32:17.043Z",
    "size": "2048",
    "md5Checksum": "abc123",
    "shared": False,
    "version": 7,
    "trashed": False,
    "lastModifyingUser": {"permissionId": "perm-9"},
}

SAMPLE_FOLDER = {
    "id": "D1",
    "name": "Documents",
    "parents": ["ROOT"],
    "mimeType": "application/vnd.google-apps.folder",
    "shared": True,
}


def test_parse_drive_time_round_trips():
    dt = parse_drive_time("2026-04-30T04:32:17.043Z")
    assert dt == datetime(2026, 4, 30, 4, 32, 17, 43000, tzinfo=timezone.utc)


def test_parse_drive_time_handles_none():
    dt = parse_drive_time(None)
    assert dt.tzinfo is timezone.utc


def test_to_file_metadata_maps_all_fields():
    meta = to_file_metadata(SAMPLE_FILE, "/report.pdf")
    assert isinstance(meta, FileMetadata)
    assert meta.name == "report.pdf"
    assert meta.path_display == "/report.pdf"
    assert meta.id == "F1"
    assert meta.rev == "7"
    assert meta.size == 2048
    assert meta.content_hash == "abc123"
    assert meta.symlink_target is None
    assert meta.is_downloadable is True
    assert meta.modified_by == "perm-9"
    assert meta.client_modified == meta.server_modified


def test_to_file_metadata_tolerates_missing_optional_fields():
    bare = {"id": "F2", "name": "x.bin", "mimeType": "application/octet-stream"}
    meta = to_file_metadata(bare, "/x.bin")
    assert meta.size == 0
    assert meta.content_hash == ""
    assert meta.modified_by is None


def test_to_file_metadata_handles_null_version():
    meta = to_file_metadata({"id": "F3", "name": "f.bin", "version": None}, "/f.bin")
    assert meta.rev == ""


def test_to_folder_metadata_maps_fields():
    meta = to_folder_metadata(SAMPLE_FOLDER, "/Documents")
    assert isinstance(meta, FolderMetadata)
    assert meta.name == "Documents"
    assert meta.path_display == "/Documents"
    assert meta.id == "D1"
    assert meta.shared is True


# ==== classify ============================================================

import sqlite3

from cumulus.changes import ChangeDecision, classify
from cumulus.database.core import Database
from cumulus.path_cache import PathIDCache


def _cache() -> PathIDCache:
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = "ROOT"
    return c


def test_classify_removed_true_is_delete():
    change = {"removed": True, "fileId": "F1"}
    assert classify(change, _cache()) is ChangeDecision.DELETE


def test_classify_trashed_is_delete():
    cache = _cache()
    change = {"removed": False, "fileId": "F1",
              "file": {"id": "F1", "name": "a", "parents": ["ROOT"],
                       "trashed": True}}
    assert classify(change, cache) is ChangeDecision.DELETE


def test_classify_move_out_is_delete():
    cache = _cache()
    cache.set("/a.pdf", "F1", "ROOT", "a.pdf", False)
    change = {"removed": False, "fileId": "F1",
              "file": {"id": "F1", "name": "a.pdf",
                       "parents": ["OUTSIDE"], "trashed": False}}
    assert classify(change, cache) is ChangeDecision.DELETE


def test_classify_never_in_scope_is_out_of_scope():
    cache = _cache()
    change = {"removed": False, "fileId": "F9",
              "file": {"id": "F9", "name": "x", "parents": ["OUTSIDE"],
                       "trashed": False}}
    assert classify(change, cache) is ChangeDecision.OUT_OF_SCOPE


def test_classify_in_scope_is_apply():
    cache = _cache()
    change = {"removed": False, "fileId": "F1",
              "file": {"id": "F1", "name": "a.pdf", "parents": ["ROOT"],
                       "trashed": False}}
    assert classify(change, cache) is ChangeDecision.APPLY


def test_classify_missing_file_body_is_unknown():
    cache = _cache()
    change = {"removed": False, "fileId": "F1", "file": None}
    assert classify(change, cache) is ChangeDecision.UNKNOWN_FILE


# ==== iter_remote_changes =================================================

from unittest.mock import MagicMock

from cumulus.changes import iter_remote_changes
from cumulus.core import DeletedMetadata, FileMetadata, FolderMetadata


def _passthrough_retry(call):
    """A retry helper that just calls through (no real retrying)."""
    return call()


def _changes_service(*pages):
    """Build a fake service whose changes.list returns the given pages."""
    service = MagicMock(name="drive-service")
    service.changes.return_value.list.return_value.execute.side_effect = pages
    return lambda: service


def test_iter_changes_create_writes_cache_and_yields():
    cache = _cache()
    page = {
        "newStartPageToken": "NEXT",
        "changes": [
            {"removed": False, "fileId": "F1",
             "file": {"id": "F1", "name": "a.pdf", "parents": ["ROOT"],
                      "mimeType": "application/pdf", "trashed": False,
                      "size": "1", "version": 1}},
        ],
    }
    results = list(iter_remote_changes(
        _changes_service(page), _passthrough_retry, cache, "CURSOR"
    ))
    assert len(results) == 1
    assert isinstance(results[0].entries[0], FileMetadata)
    assert cache.path_to_id("/a.pdf") == "F1"
    assert results[0].cursor == "NEXT"
    assert results[0].has_more is False


def test_iter_changes_delete_removes_from_cache():
    cache = _cache()
    cache.set("/a.pdf", "F1", "ROOT", "a.pdf", False)
    page = {
        "newStartPageToken": "NEXT",
        "changes": [{"removed": True, "fileId": "F1"}],
    }
    results = list(iter_remote_changes(
        _changes_service(page), _passthrough_retry, cache, "CURSOR"
    ))
    assert isinstance(results[0].entries[0], DeletedMetadata)
    assert cache.path_to_id("/a.pdf") is None


def test_iter_changes_rename_moves_cache_entry():
    cache = _cache()
    cache.set("/old.pdf", "F1", "ROOT", "old.pdf", False)
    page = {
        "newStartPageToken": "NEXT",
        "changes": [
            {"removed": False, "fileId": "F1",
             "file": {"id": "F1", "name": "new.pdf", "parents": ["ROOT"],
                      "mimeType": "application/pdf", "trashed": False,
                      "size": "1", "version": 2}},
        ],
    }
    list(iter_remote_changes(
        _changes_service(page), _passthrough_retry, cache, "CURSOR"
    ))
    assert cache.path_to_id("/old.pdf") is None
    assert cache.path_to_id("/new.pdf") == "F1"


def test_iter_changes_intra_batch_dependency():
    # Folder D1 created in entry 1; file F1 moved into D1 in entry 2.
    cache = _cache()
    page = {
        "newStartPageToken": "NEXT",
        "changes": [
            {"removed": False, "fileId": "D1",
             "file": {"id": "D1", "name": "Docs", "parents": ["ROOT"],
                      "mimeType": "application/vnd.google-apps.folder",
                      "trashed": False}},
            {"removed": False, "fileId": "F1",
             "file": {"id": "F1", "name": "a.pdf", "parents": ["D1"],
                      "mimeType": "application/pdf", "trashed": False,
                      "size": "1", "version": 1}},
        ],
    }
    results = list(iter_remote_changes(
        _changes_service(page), _passthrough_retry, cache, "CURSOR"
    ))
    assert cache.path_to_id("/Docs/a.pdf") == "F1"
    assert {type(e) for e in results[0].entries} == {FolderMetadata, FileMetadata}


def test_iter_changes_drops_native_doc():
    cache = _cache()
    page = {
        "newStartPageToken": "NEXT",
        "changes": [
            {"removed": False, "fileId": "G1",
             "file": {"id": "G1", "name": "Doc", "parents": ["ROOT"],
                      "mimeType": "application/vnd.google-apps.document",
                      "trashed": False}},
        ],
    }
    results = list(iter_remote_changes(
        _changes_service(page), _passthrough_retry, cache, "CURSOR"
    ))
    assert results[0].entries == []
    assert cache.get_entry("G1") is None


def test_iter_changes_drops_unknown_delete():
    cache = _cache()
    page = {
        "newStartPageToken": "NEXT",
        "changes": [{"removed": True, "fileId": "NEVER_SEEN"}],
    }
    results = list(iter_remote_changes(
        _changes_service(page), _passthrough_retry, cache, "CURSOR"
    ))
    assert results[0].entries == []


def test_iter_changes_paginates():
    cache = _cache()
    page1 = {
        "nextPageToken": "p2",
        "changes": [
            {"removed": False, "fileId": "F1",
             "file": {"id": "F1", "name": "a.pdf", "parents": ["ROOT"],
                      "mimeType": "application/pdf", "trashed": False,
                      "size": "1", "version": 1}},
        ],
    }
    page2 = {
        "newStartPageToken": "NEXT",
        "changes": [
            {"removed": False, "fileId": "F2",
             "file": {"id": "F2", "name": "b.pdf", "parents": ["ROOT"],
                      "mimeType": "application/pdf", "trashed": False,
                      "size": "1", "version": 1}},
        ],
    }
    results = list(iter_remote_changes(
        _changes_service(page1, page2), _passthrough_retry, cache, "CURSOR"
    ))
    assert len(results) == 2
    assert results[0].has_more is True
    assert results[0].cursor == "p2"
    assert results[1].has_more is False
    assert results[1].cursor == "NEXT"
