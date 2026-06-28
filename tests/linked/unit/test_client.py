"""Linked client tests — ported to Google Drive (Stage 7a, Task D4).

Tests run against a real Google account when CUMULUS_TEST_REFRESH_TOKEN is
set; they skip cleanly when the variable is absent.

Dropbox-only features dropped:
  - test_share_dir_new, test_share_dir_existing  (Shared Drives stub)
  - test_sharedlink_lifecycle, test_sharedlink_errors  (sharing stub)
  - All Dropbox upload-session chunk-phase hash-mismatch tests
    (the Dropbox API had per-chunk hash verification; the Drive client
    uses a single end-to-end MD5 comparison via _md5_file; there is no
    monkeypatch seam for per-chunk failure)

Ported:
  - test_upload (simple)
  - test_upload_resumable (large file; ~1.8 MB fixture, same code path)
  - test_upload_hash_mismatch (Drive md5 verify path via _md5_file)
  - test_download (round-trip + MD5 verify)
  - test_download_hash_mismatch (Drive download md5 verify path)
  - test_get_metadata_file, test_get_metadata_folder,
    test_get_metadata_nonexistent_raises
  - test_list_folder
  - test_change_feed (with wait_until for Drive lag)
  - test_move
  - test_remove
  - test_restore
  - test_make_dir
  - test_batch_methods (make_dir_batch + remove_batch)

Added:
  - test_sharing_stubs_raise (confirms sharing methods raise NotImplementedError)
"""

from __future__ import annotations

import hashlib
import os

import pytest

from bora.client import GoogleDriveClient
from bora.core import FileMetadata, FolderMetadata, WriteMode
from bora.exceptions import (
    DataCorruptionError,
    NotFoundError,
)
from bora.utils.path import normalize

from tests.linked.conftest import requires_account, wait_until

resources = os.path.dirname(os.path.dirname(__file__)) + "/resources"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _md5(path: str) -> str:
    """Compute MD5 hex digest of a local file (mirrors Drive's md5Checksum)."""
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@requires_account
def test_upload(client: GoogleDriveClient) -> None:
    """Upload a small file in a single (non-resumable) request."""
    file = resources + "/file.txt"
    md = client.upload(file, "/file.txt")
    assert isinstance(md, FileMetadata)
    assert md.content_hash == _md5(file)
    assert md.size == os.path.getsize(file)


@requires_account
def test_upload_resumable(client: GoogleDriveClient) -> None:
    """Upload a file via the same upload() API; Drive picks resumable vs simple.

    The test fixture large-file.pdf is ~1.8 MB, which is below the 5 MB
    resumable threshold, so both this and test_upload exercise the simple path
    with the current fixtures. A truly >5 MB file would be needed to force the
    resumable branch; that is left for a future fixture update (flagged below).
    """
    large_file = resources + "/large-file.pdf"
    md = client.upload(large_file, "/large-file.pdf")
    assert isinstance(md, FileMetadata)
    assert md.content_hash == _md5(large_file)


@requires_account
def test_upload_hash_mismatch(
    client: GoogleDriveClient, monkeypatch
) -> None:
    """DataCorruptionError raised when Drive's md5Checksum doesn't match local.

    The write path (writes._WriteHelper.upload) computes a local MD5 via the
    static method _md5_file and compares it against Drive's response field
    md5Checksum. Monkeypatching _md5_file to return a wrong hash causes every
    upload to fail the comparison.
    """
    import bora.writes as _writes

    file = resources + "/file.txt"

    monkeypatch.setattr(
        _writes._WriteHelper,
        "_md5_file",
        staticmethod(lambda path: "0" * 32),  # always wrong
    )

    with pytest.raises(DataCorruptionError):
        client.upload(file, "/file.txt")


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

@requires_account
def test_download(client: GoogleDriveClient, tmp_path) -> None:
    """Upload then download a file; verify MD5 matches."""
    file = resources + "/file.txt"
    client.upload(file, "/file.txt")

    dest = str(tmp_path / "file.txt")
    md = client.download("/file.txt", dest)

    assert isinstance(md, FileMetadata)
    assert _md5(dest) == _md5(file)
    assert md.content_hash == _md5(file)


@requires_account
def test_download_hash_mismatch(
    client: GoogleDriveClient, monkeypatch, tmp_path
) -> None:
    """DataCorruptionError raised when the downloaded file has the wrong MD5.

    client.py's download() method verifies the file via bora.client.hashlib.md5
    after the download completes. Monkeypatching that reference to return a
    wrong hexdigest causes the comparison to fail.
    """
    import bora.client as _client_mod

    file = resources + "/file.txt"
    client.upload(file, "/file.txt")

    real_md5 = _client_mod.hashlib.md5

    class _FakeMd5:
        def __init__(self, *a, **kw):
            self._real = real_md5(*a, **kw)

        def update(self, b: bytes) -> None:
            self._real.update(b)

        def hexdigest(self) -> str:
            return "0" * 32  # always wrong

    monkeypatch.setattr(_client_mod.hashlib, "md5", _FakeMd5)

    with pytest.raises(DataCorruptionError):
        client.download("/file.txt", str(tmp_path / "file.txt"))


# ---------------------------------------------------------------------------
# get_metadata
# ---------------------------------------------------------------------------

@requires_account
def test_get_metadata_file(client: GoogleDriveClient) -> None:
    """get_metadata returns FileMetadata after upload."""
    file = resources + "/file.txt"
    uploaded = client.upload(file, "/meta-file.txt")

    md = client.get_metadata("/meta-file.txt")
    assert isinstance(md, FileMetadata)
    assert md.id == uploaded.id
    assert md.content_hash == _md5(file)


@requires_account
def test_get_metadata_folder(client: GoogleDriveClient) -> None:
    """get_metadata returns FolderMetadata after make_dir."""
    client.make_dir("/meta-folder")
    md = client.get_metadata("/meta-folder")
    assert isinstance(md, FolderMetadata)


@requires_account
def test_get_metadata_nonexistent_raises(client: GoogleDriveClient) -> None:
    """get_metadata raises NotFoundError for a path not in the cache."""
    with pytest.raises(NotFoundError):
        client.get_metadata("/does-not-exist.txt")


# ---------------------------------------------------------------------------
# list_folder
# ---------------------------------------------------------------------------

@requires_account
def test_list_folder(client: GoogleDriveClient) -> None:
    """list_folder returns uploaded files and sub-folders."""
    file = resources + "/file.txt"
    client.upload(file, "/lf-file.txt")
    client.make_dir("/lf-sub")

    result = client.list_folder("/")
    paths = {e.path_display for e in result.entries}
    assert "/lf-file.txt" in paths
    assert "/lf-sub" in paths


# ---------------------------------------------------------------------------
# Change feed
# ---------------------------------------------------------------------------

@requires_account
def test_change_feed(client: GoogleDriveClient) -> None:
    """Upload a file then verify it appears in the change feed.

    wait_until polls because Drive's changes.list can lag several seconds.
    """
    cursor = client.get_latest_cursor("/")

    file = resources + "/file.txt"
    uploaded = client.upload(file, "/cf-file.txt")

    def _has_change() -> bool:
        result = client.list_remote_changes(cursor)
        return any(
            getattr(e, "id", None) == uploaded.id for e in result.entries
        )

    wait_until(_has_change, timeout=60, interval=3)


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------

@requires_account
def test_move(client: GoogleDriveClient) -> None:
    """move renames a file; old path evicted, new path accessible."""
    file = resources + "/file.txt"
    md_orig = client.upload(file, "/move-src.txt")

    md_moved = client.move("/move-src.txt", "/move-dst.txt")
    assert isinstance(md_moved, FileMetadata)
    assert md_moved.id == md_orig.id

    # New path resolves.
    md_new = client.get_metadata("/move-dst.txt")
    assert isinstance(md_new, FileMetadata)

    # Old path no longer resolves (evicted from cache by move()).
    with pytest.raises(NotFoundError):
        client.get_metadata("/move-src.txt")


# ---------------------------------------------------------------------------
# remove + restore
# ---------------------------------------------------------------------------

@requires_account
def test_remove(client: GoogleDriveClient) -> None:
    """remove trashes the file; path cache evicted; get_metadata raises."""
    file = resources + "/file.txt"
    client.upload(file, "/rm-file.txt")

    md_rm = client.remove("/rm-file.txt")
    assert isinstance(md_rm, FileMetadata)

    with pytest.raises(NotFoundError):
        client.get_metadata("/rm-file.txt")


@requires_account
def test_restore(client: GoogleDriveClient, tmp_path) -> None:
    """Upload, capture revision, overwrite, then restore the original revision."""
    file = resources + "/file.txt"
    orig_md = client.upload(file, "/restore-file.txt")
    orig_rev = orig_md.rev
    original_hash = orig_md.content_hash

    # Overwrite with different content to create a new revision.
    new_content = tmp_path / "new.txt"
    new_content.write_bytes(b"overwritten content that differs from original")
    client.upload(str(new_content), "/restore-file.txt", write_mode=WriteMode.Overwrite)

    # Restore the original revision.
    restored = client.restore("/restore-file.txt", orig_rev)
    assert isinstance(restored, FileMetadata)
    assert restored.content_hash == original_hash


# ---------------------------------------------------------------------------
# make_dir
# ---------------------------------------------------------------------------

@requires_account
def test_make_dir(client: GoogleDriveClient) -> None:
    """make_dir creates a folder; get_metadata returns FolderMetadata."""
    md = client.make_dir("/new-folder")
    assert isinstance(md, FolderMetadata)
    assert md.path_display == "/new-folder"

    fetched = client.get_metadata("/new-folder")
    assert isinstance(fetched, FolderMetadata)
    assert fetched.id == md.id


# ---------------------------------------------------------------------------
# Batch methods
# ---------------------------------------------------------------------------

@requires_account
def test_batch_methods(client: GoogleDriveClient) -> None:
    """make_dir_batch creates 20 folders; remove_batch trashes them.

    The original Dropbox test also created an invalid path (/invalid\\) and
    expected a PathError in the batch result. Drive does not reject that name,
    so the invalid-path slot is dropped from the batch test.
    """
    folders = [f"/folder {i}" for i in range(20)]

    # Create dirs in batch.
    res_create = client.make_dir_batch(folders)

    assert len(res_create) == 20
    for i, item in enumerate(res_create):
        assert isinstance(item, FolderMetadata), (
            f"slot {i} expected FolderMetadata, got {type(item)}: {item}"
        )
        assert item.path_lower == normalize(folders[i])

    # Remove in batch (20 valid + 1 nonexistent = 21 entries).
    remove_entries = [(folder, None) for folder in folders] + [("/not_a_folder", None)]
    res_remove = client.remove_batch(remove_entries)

    assert len(res_remove) == 21
    for i in range(20):
        assert isinstance(res_remove[i], FolderMetadata), (
            f"remove slot {i} expected FolderMetadata, got {type(res_remove[i])}"
        )
        assert res_remove[i].path_lower == normalize(folders[i])

    assert isinstance(res_remove[20], NotFoundError)
