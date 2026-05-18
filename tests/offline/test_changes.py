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


def test_to_folder_metadata_maps_fields():
    meta = to_folder_metadata(SAMPLE_FOLDER, "/Documents")
    assert isinstance(meta, FolderMetadata)
    assert meta.name == "Documents"
    assert meta.path_display == "/Documents"
    assert meta.id == "D1"
    assert meta.shared is True
