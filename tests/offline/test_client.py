"""Offline unit tests for cumulus.client.GoogleDriveClient.

Drive API calls are mocked at the `_get_service()` boundary: each test
builds a MagicMock service and assigns `client._get_service` to a Mock
returning it.
"""

from __future__ import annotations

import logging
import sqlite3
from unittest.mock import MagicMock, Mock

import pytest

from cumulus.client import GoogleDriveClient, _MAX_RETRY_ATTEMPTS
from cumulus.config import remove_configuration
from cumulus.database.core import Database
from cumulus.keyring import CredentialStorage
from cumulus.path_cache import PathIDCache

_CONFIG = "test-config-client-offline"


@pytest.fixture
def cache() -> PathIDCache:
    """Fresh in-memory path cache with the sync root pre-seeded."""
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = "ROOT"
    return c


@pytest.fixture
def client(cache: PathIDCache):
    cred = CredentialStorage(_CONFIG)
    cl = GoogleDriveClient(_CONFIG, cred, cache)
    yield cl
    cred.delete_creds()
    remove_configuration(_CONFIG)


def fake_service() -> MagicMock:
    """A MagicMock standing in for a built Drive v3 service."""
    return MagicMock(name="drive-service")


# ==== constructor =========================================================

def test_constructor_stores_path_cache(client, cache):
    assert client._path_cache is cache


def test_close_sets_poll_event(client):
    assert client._poll_event.is_set() is False
    client.close()
    assert client._poll_event.is_set() is True


# ==== _retry_transient ====================================================

def test_retry_returns_on_first_success(client):
    call = Mock(return_value="ok")
    assert client._retry_transient(call) == "ok"
    assert call.call_count == 1


def test_retry_retries_then_succeeds(client, make_http_error):
    client._poll_event = Mock()
    client._poll_event.wait.return_value = False  # no shutdown
    call = Mock(side_effect=[make_http_error(503), make_http_error(503), "ok"])
    assert client._retry_transient(call) == "ok"
    assert call.call_count == 3


def test_retry_does_not_retry_non_retryable(client, make_http_error):
    # A 404 is not transient — it must propagate unchanged (the caller's
    # convert_api_errors maps it later).
    from googleapiclient.errors import HttpError

    call = Mock(side_effect=make_http_error(404))
    with pytest.raises(HttpError):
        client._retry_transient(call)
    assert call.call_count == 1


def test_retry_exhausts_and_raises_last(client, make_http_error):
    from googleapiclient.errors import HttpError

    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    call = Mock(side_effect=[make_http_error(500)] * 10)
    with pytest.raises(HttpError):
        client._retry_transient(call)
    assert call.call_count == _MAX_RETRY_ATTEMPTS


def test_retry_retries_rate_limited_403(client, make_http_error):
    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    call = Mock(side_effect=[make_http_error(403, "rateLimitExceeded"), "ok"])
    assert client._retry_transient(call) == "ok"
    assert call.call_count == 2


def test_retry_does_not_retry_permission_403(client, make_http_error):
    from googleapiclient.errors import HttpError

    call = Mock(side_effect=make_http_error(403, "insufficientFilePermissions"))
    with pytest.raises(HttpError):
        client._retry_transient(call)
    assert call.call_count == 1


def test_retry_aborts_on_shutdown(client, make_http_error):
    from cumulus.exceptions import CancelledError

    client._poll_event = Mock()
    client._poll_event.wait.return_value = True  # shutdown during backoff
    call = Mock(side_effect=make_http_error(503))
    with pytest.raises(CancelledError):
        client._retry_transient(call)


def test_retry_retries_connection_error(client):
    import requests

    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    call = Mock(side_effect=[requests.exceptions.ConnectionError("net"), "ok"])
    assert client._retry_transient(call) == "ok"
    assert call.call_count == 2


# ==== _ensure_root_id =====================================================

def test_ensure_root_id_resolves_and_caches(client):
    client._path_cache.root_id = None  # force resolution
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "ROOT_XYZ"
    }
    client._get_service = Mock(return_value=service)

    assert client._ensure_root_id() == "ROOT_XYZ"
    assert client._path_cache.root_id == "ROOT_XYZ"
    # Second call must not hit the service again.
    assert client._ensure_root_id() == "ROOT_XYZ"
    assert service.files.return_value.get.call_count == 1


def test_ensure_root_id_short_circuits_when_set(client):
    # cache.root_id was pre-seeded to "ROOT" by the fixture.
    client._get_service = Mock(side_effect=AssertionError("should not be called"))
    assert client._ensure_root_id() == "ROOT"


# ==== get_metadata ========================================================

def _seed(cache, path, file_id, name, parent="ROOT", is_folder=False):
    cache.set(path, file_id, parent, name, is_folder)


def test_get_metadata_returns_file_metadata(client, cache):
    from cumulus.core import FileMetadata

    _seed(cache, "/report.pdf", "F1", "report.pdf")
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "F1", "name": "report.pdf", "mimeType": "application/pdf",
        "parents": ["ROOT"], "modifiedTime": "2026-04-30T00:00:00Z",
        "size": "10", "md5Checksum": "h", "version": 2, "trashed": False,
    }
    client._get_service = Mock(return_value=service)

    meta = client.get_metadata("/report.pdf")
    assert isinstance(meta, FileMetadata)
    assert meta.id == "F1"


def test_get_metadata_returns_folder_metadata(client, cache):
    from cumulus.core import FolderMetadata

    _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "D1", "name": "Documents",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["ROOT"], "trashed": False,
    }
    client._get_service = Mock(return_value=service)

    meta = client.get_metadata("/Documents")
    assert isinstance(meta, FolderMetadata)


def test_get_metadata_returns_none_for_native_doc(client, cache):
    _seed(cache, "/Notes", "G1", "Notes")
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "G1", "name": "Notes",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["ROOT"], "trashed": False,
    }
    client._get_service = Mock(return_value=service)

    assert client.get_metadata("/Notes") is None


def test_get_metadata_raises_not_found_on_cache_miss(client):
    from cumulus.exceptions import NotFoundError

    client._get_service = Mock(side_effect=AssertionError("should not call API"))
    with pytest.raises(NotFoundError):
        client.get_metadata("/no-such-file")


def test_get_metadata_trashed_returns_none(client, cache):
    _seed(cache, "/old.pdf", "F9", "old.pdf")
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "F9", "name": "old.pdf", "mimeType": "application/pdf",
        "parents": ["ROOT"], "trashed": True,
    }
    client._get_service = Mock(return_value=service)

    assert client.get_metadata("/old.pdf", include_deleted=False) is None


def test_get_metadata_trashed_with_include_deleted_returns_metadata(client, cache):
    from cumulus.core import FileMetadata

    _seed(cache, "/old.pdf", "F9", "old.pdf")
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "F9", "name": "old.pdf", "mimeType": "application/pdf",
        "parents": ["ROOT"], "trashed": True, "version": 1, "size": "5",
    }
    client._get_service = Mock(return_value=service)

    meta = client.get_metadata("/old.pdf", include_deleted=True)
    assert isinstance(meta, FileMetadata)
    assert meta.id == "F9"


# ==== list_revisions / restore ============================================

def test_list_revisions_returns_newest_first(client, cache):
    _seed(cache, "/report.pdf", "F1", "report.pdf")
    service = fake_service()
    service.revisions.return_value.list.return_value.execute.return_value = {
        "revisions": [
            {"id": "r1", "modifiedTime": "2026-01-01T00:00:00Z", "size": "10"},
            {"id": "r2", "modifiedTime": "2026-02-01T00:00:00Z", "size": "20"},
        ]
    }
    client._get_service = Mock(return_value=service)

    revs = client.list_revisions("/report.pdf")
    assert [r.rev for r in revs] == ["r2", "r1"]


def test_list_revisions_respects_limit(client, cache):
    _seed(cache, "/report.pdf", "F1", "report.pdf")
    service = fake_service()
    service.revisions.return_value.list.return_value.execute.return_value = {
        "revisions": [
            {"id": f"r{i}", "modifiedTime": "2026-01-01T00:00:00Z", "size": "1"}
            for i in range(20)
        ]
    }
    client._get_service = Mock(return_value=service)

    revs = client.list_revisions("/report.pdf", limit=5)
    assert len(revs) == 5


def test_list_revisions_paginates(client, cache):
    _seed(cache, "/report.pdf", "F1", "report.pdf")
    service = fake_service()
    service.revisions.return_value.list.return_value.execute.side_effect = [
        {"revisions": [{"id": "r1", "modifiedTime": "2026-01-01T00:00:00Z",
                        "size": "1"}], "nextPageToken": "p2"},
        {"revisions": [{"id": "r2", "modifiedTime": "2026-02-01T00:00:00Z",
                        "size": "1"}]},
    ]
    client._get_service = Mock(return_value=service)

    revs = client.list_revisions("/report.pdf")
    assert {r.rev for r in revs} == {"r1", "r2"}
    # The second call must forward the nextPageToken from page 1.
    calls = service.revisions.return_value.list.call_args_list
    assert calls[1].kwargs["pageToken"] == "p2"


def test_list_revisions_cache_miss_raises(client):
    from cumulus.exceptions import NotFoundError

    client._get_service = Mock(side_effect=AssertionError("no API call"))
    with pytest.raises(NotFoundError):
        client.list_revisions("/missing")


def test_restore_raises_not_implemented(client):
    with pytest.raises(NotImplementedError):
        client.restore("/report.pdf", "r1")


# ==== list_folder_iterator (non-recursive) ================================

def test_list_folder_non_recursive_yields_entries(client, cache):
    from cumulus.core import FileMetadata, FolderMetadata

    _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
    service = fake_service()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [
            {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
             "parents": ["D1"], "trashed": False, "size": "1"},
            {"id": "D2", "name": "Sub",
             "mimeType": "application/vnd.google-apps.folder",
             "parents": ["D1"], "trashed": False},
        ]
    }
    client._get_service = Mock(return_value=service)

    pages = list(client.list_folder_iterator("/Documents"))
    entries = pages[0].entries
    assert len(entries) == 2
    assert any(isinstance(e, FileMetadata) for e in entries)
    assert any(isinstance(e, FolderMetadata) for e in entries)


def test_list_folder_non_recursive_filters_native_docs(client, cache):
    _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
    service = fake_service()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [
            {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
             "parents": ["D1"], "trashed": False, "size": "1"},
            {"id": "G1", "name": "Doc",
             "mimeType": "application/vnd.google-apps.document",
             "parents": ["D1"], "trashed": False},
        ]
    }
    client._get_service = Mock(return_value=service)

    entries = list(client.list_folder_iterator("/Documents"))[0].entries
    assert [e.id for e in entries] == ["F1"]


def test_list_folder_non_recursive_paginates(client, cache):
    _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
    service = fake_service()
    service.files.return_value.list.return_value.execute.side_effect = [
        {"files": [{"id": "F1", "name": "a", "mimeType": "application/pdf",
                    "parents": ["D1"], "trashed": False, "size": "1"}],
         "nextPageToken": "p2"},
        {"files": [{"id": "F2", "name": "b", "mimeType": "application/pdf",
                    "parents": ["D1"], "trashed": False, "size": "1"}]},
    ]
    client._get_service = Mock(return_value=service)

    pages = list(client.list_folder_iterator("/Documents"))
    assert len(pages) == 2
    assert pages[0].has_more is True
    assert pages[1].has_more is False
    # The second call must forward the nextPageToken from page 1.
    calls = service.files.return_value.list.call_args_list
    assert calls[1].kwargs["pageToken"] == "p2"


def test_list_folder_non_recursive_cache_miss_raises(client):
    from cumulus.exceptions import NotFoundError

    client._get_service = Mock(side_effect=AssertionError("no API call"))
    with pytest.raises(NotFoundError):
        list(client.list_folder_iterator("/missing-folder"))


def test_list_folder_flattens(client, cache):
    _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
    service = fake_service()
    service.files.return_value.list.return_value.execute.side_effect = [
        {"files": [{"id": "F1", "name": "a", "mimeType": "application/pdf",
                    "parents": ["D1"], "trashed": False, "size": "1"}],
         "nextPageToken": "p2"},
        {"files": [{"id": "F2", "name": "b", "mimeType": "application/pdf",
                    "parents": ["D1"], "trashed": False, "size": "1"}]},
    ]
    client._get_service = Mock(return_value=service)

    result = client.list_folder("/Documents")
    assert {e.id for e in result.entries} == {"F1", "F2"}


# ==== list_folder_iterator (recursive bootstrap) ==========================

def _bootstrap_service(files_pages, start_token="TOK1"):
    """Build a fake service for the recursive bootstrap: a
    changes.getStartPageToken call plus paginated files.list calls."""
    service = fake_service()
    service.changes.return_value.getStartPageToken.return_value.execute.return_value = {
        "startPageToken": start_token
    }
    service.files.return_value.list.return_value.execute.side_effect = files_pages
    return service


def test_bootstrap_populates_cache_and_yields(client, cache):
    cache.root_id = "ROOT"
    pages = [{
        "files": [
            {"id": "D1", "name": "Docs",
             "mimeType": "application/vnd.google-apps.folder",
             "parents": ["ROOT"], "trashed": False},
            {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
             "parents": ["D1"], "trashed": False, "size": "1"},
        ]
    }]
    client._get_service = Mock(return_value=_bootstrap_service(pages))

    result_pages = list(client.list_folder_iterator("/", recursive=True))
    ids = {e.id for page in result_pages for e in page.entries}
    assert ids == {"D1", "F1"}
    # Cache populated for path resolution.
    assert cache.path_to_id("/Docs") == "D1"
    assert cache.path_to_id("/Docs/a.pdf") == "F1"
    # Last page carries the change cursor captured before listing.
    assert result_pages[-1].cursor == "TOK1"
    assert result_pages[-1].has_more is False


def test_bootstrap_excludes_native_docs(client, cache):
    cache.root_id = "ROOT"
    pages = [{
        "files": [
            {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
             "parents": ["ROOT"], "trashed": False, "size": "1"},
            {"id": "G1", "name": "Doc",
             "mimeType": "application/vnd.google-apps.document",
             "parents": ["ROOT"], "trashed": False},
        ]
    }]
    client._get_service = Mock(return_value=_bootstrap_service(pages))

    result_pages = list(client.list_folder_iterator("/", recursive=True))
    ids = {e.id for page in result_pages for e in page.entries}
    assert ids == {"F1"}
    assert cache.get_entry("G1") is None


def test_bootstrap_drops_items_outside_root(client, cache, caplog):
    cache.root_id = "ROOT"
    pages = [{
        "files": [
            {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
             "parents": ["ROOT"], "trashed": False, "size": "1"},
            # Parent OUTSIDE_ROOT is not reachable from ROOT.
            {"id": "F2", "name": "shared.pdf", "mimeType": "application/pdf",
             "parents": ["OUTSIDE_ROOT"], "trashed": False, "size": "1"},
        ]
    }]
    client._get_service = Mock(return_value=_bootstrap_service(pages))

    with caplog.at_level(logging.INFO, logger="cumulus.client"):
        result_pages = list(client.list_folder_iterator("/", recursive=True))
    ids = {e.id for page in result_pages for e in page.entries}
    assert ids == {"F1"}
    assert cache.get_entry("F2") is None
    assert any("skipped" in rec.message for rec in caplog.records)


def test_bootstrap_fetches_all_pages(client, cache):
    cache.root_id = "ROOT"
    pages = [
        {"files": [{"id": "F1", "name": "a", "mimeType": "application/pdf",
                    "parents": ["ROOT"], "trashed": False, "size": "1"}],
         "nextPageToken": "p2"},
        {"files": [{"id": "F2", "name": "b", "mimeType": "application/pdf",
                    "parents": ["ROOT"], "trashed": False, "size": "1"}]},
    ]
    client._get_service = Mock(return_value=_bootstrap_service(pages))

    result_pages = list(client.list_folder_iterator("/", recursive=True))
    ids = {e.id for page in result_pages for e in page.entries}
    assert ids == {"F1", "F2"}
    assert result_pages[-1].cursor == "TOK1"
    assert result_pages[-1].has_more is False


# ==== download ============================================================

import hashlib as _hashlib


class FakeDownloader:
    """Stand-in for googleapiclient.http.MediaIoBaseDownload. Writes the
    bytes stashed on the request mock to the file handle in one chunk."""

    def __init__(self, fh, request, chunksize=None):
        self._fh = fh
        self._data = request._fake_data
        self._done = False

    def next_chunk(self):
        self._fh.write(self._data)
        self._done = True
        status = Mock(resumable_progress=len(self._data))
        return status, True


def _download_service(file_meta, file_bytes):
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = file_meta
    media_request = Mock()
    media_request._fake_data = file_bytes
    service.files.return_value.get_media.return_value = media_request
    return service


def test_download_writes_file_and_verifies_md5(client, cache, tmp_path, monkeypatch):
    monkeypatch.setattr("cumulus.client.MediaIoBaseDownload", FakeDownloader)
    _seed(cache, "/a.bin", "F1", "a.bin")
    data = b"hello world"
    meta = {
        "id": "F1", "name": "a.bin", "mimeType": "application/octet-stream",
        "parents": ["ROOT"], "trashed": False, "size": str(len(data)),
        "md5Checksum": _hashlib.md5(data).hexdigest(), "version": 1,
    }
    client._get_service = Mock(return_value=_download_service(meta, data))

    dest = tmp_path / "a.bin"
    result = client.download("/a.bin", str(dest))
    assert dest.read_bytes() == data
    assert result.id == "F1"


def test_download_native_doc_raises(client, cache, tmp_path):
    from cumulus.exceptions import UnsupportedFileError

    _seed(cache, "/Doc", "G1", "Doc")
    meta = {
        "id": "G1", "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["ROOT"], "trashed": False,
    }
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = meta
    client._get_service = Mock(return_value=service)

    with pytest.raises(UnsupportedFileError):
        client.download("/Doc", str(tmp_path / "Doc"))


def test_download_md5_mismatch_raises_and_cleans_up(client, cache, tmp_path, monkeypatch):
    from cumulus.exceptions import DataCorruptionError

    monkeypatch.setattr("cumulus.client.MediaIoBaseDownload", FakeDownloader)
    _seed(cache, "/a.bin", "F1", "a.bin")
    data = b"hello world"
    meta = {
        "id": "F1", "name": "a.bin", "mimeType": "application/octet-stream",
        "parents": ["ROOT"], "trashed": False, "size": str(len(data)),
        "md5Checksum": "deadbeef" * 4, "version": 1,  # wrong hash
    }
    client._get_service = Mock(return_value=_download_service(meta, data))

    dest = tmp_path / "a.bin"
    with pytest.raises(DataCorruptionError):
        client.download("/a.bin", str(dest))
    assert not dest.exists()
    assert not (tmp_path / "a.bin.cumulus-part").exists()


def test_download_cache_miss_raises(client, tmp_path):
    from cumulus.exceptions import NotFoundError

    client._get_service = Mock(side_effect=AssertionError("no API call"))
    with pytest.raises(NotFoundError):
        client.download("/missing", str(tmp_path / "x"))


def test_download_cleans_up_sidecar_on_error(client, cache, tmp_path, monkeypatch):
    from googleapiclient.errors import HttpError

    class FailingDownloader:
        def __init__(self, fh, request, chunksize=None):
            pass

        def next_chunk(self):
            raise RuntimeError("network died mid-download")

    monkeypatch.setattr("cumulus.client.MediaIoBaseDownload", FailingDownloader)
    _seed(cache, "/a.bin", "F1", "a.bin")
    meta = {
        "id": "F1", "name": "a.bin", "mimeType": "application/octet-stream",
        "parents": ["ROOT"], "trashed": False, "size": "5",
        "md5Checksum": "x", "version": 1,
    }
    client._get_service = Mock(return_value=_download_service(meta, b"data"))

    dest = tmp_path / "a.bin"
    with pytest.raises(RuntimeError):
        client.download("/a.bin", str(dest))
    assert not dest.exists()
    assert not (tmp_path / "a.bin.cumulus-part").exists()


def test_download_updates_sync_event(client, cache, tmp_path, monkeypatch):
    monkeypatch.setattr("cumulus.client.MediaIoBaseDownload", FakeDownloader)
    _seed(cache, "/a.bin", "F1", "a.bin")
    data = b"abcdef"
    meta = {
        "id": "F1", "name": "a.bin", "mimeType": "application/octet-stream",
        "parents": ["ROOT"], "trashed": False, "size": str(len(data)),
        "md5Checksum": _hashlib.md5(data).hexdigest(), "version": 1,
    }
    client._get_service = Mock(return_value=_download_service(meta, data))

    sync_event = Mock()
    client.download("/a.bin", str(tmp_path / "a.bin"), sync_event=sync_event)
    assert sync_event.completed == len(data)


# ==== get_latest_cursor ===================================================

def test_get_latest_cursor_returns_start_page_token(client):
    service = fake_service()
    service.changes.return_value.getStartPageToken.return_value.execute.return_value = {
        "startPageToken": "TOK99"
    }
    client._get_service = Mock(return_value=service)

    assert client.get_latest_cursor("/") == "TOK99"


def test_get_latest_cursor_ignores_remote_path(client):
    service = fake_service()
    service.changes.return_value.getStartPageToken.return_value.execute.return_value = {
        "startPageToken": "TOK99"
    }
    client._get_service = Mock(return_value=service)

    # Any remote_path must produce the same account-global token.
    assert client.get_latest_cursor("/Documents/Sub") == "TOK99"


# ==== wait_for_remote_changes =============================================

def test_wait_returns_true_when_changes_present(client):
    client._poll_event = Mock()
    client._poll_event.wait.return_value = False  # timed out normally
    service = fake_service()
    service.changes.return_value.list.return_value.execute.return_value = {
        "changes": [{"fileId": "F1"}]
    }
    client._get_service = Mock(return_value=service)

    assert client.wait_for_remote_changes("CURSOR", timeout=1) is True


def test_wait_returns_false_when_no_changes(client):
    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    service = fake_service()
    service.changes.return_value.list.return_value.execute.return_value = {
        "changes": []
    }
    client._get_service = Mock(return_value=service)

    assert client.wait_for_remote_changes("CURSOR", timeout=1) is False


def test_wait_returns_false_on_shutdown_without_polling(client):
    client._poll_event = Mock()
    client._poll_event.wait.return_value = True  # shutdown during sleep
    client._get_service = Mock(side_effect=AssertionError("must not poll"))

    assert client.wait_for_remote_changes("CURSOR", timeout=1) is False


# ==== list_remote_changes(_iterator) ======================================

def test_list_remote_changes_iterator_delegates(client, cache):
    from cumulus.core import FileMetadata

    service = fake_service()
    service.changes.return_value.list.return_value.execute.return_value = {
        "newStartPageToken": "NEXT",
        "changes": [
            {"removed": False, "fileId": "F1",
             "file": {"id": "F1", "name": "a.pdf", "parents": ["ROOT"],
                      "mimeType": "application/pdf", "trashed": False,
                      "size": "1", "version": 1}},
        ],
    }
    client._get_service = Mock(return_value=service)

    pages = list(client.list_remote_changes_iterator("CURSOR"))
    assert isinstance(pages[0].entries[0], FileMetadata)
    assert cache.path_to_id("/a.pdf") == "F1"


def test_list_remote_changes_iterator_converts_http_error(client, make_http_error):
    service = fake_service()
    service.changes.return_value.list.return_value.execute.side_effect = (
        make_http_error(404)
    )
    client._get_service = Mock(return_value=service)

    from cumulus.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        list(client.list_remote_changes_iterator("CURSOR"))


def test_list_remote_changes_flattens(client, cache):
    service = fake_service()
    service.changes.return_value.list.return_value.execute.return_value = {
        "newStartPageToken": "NEXT", "changes": [],
    }
    client._get_service = Mock(return_value=service)

    result = client.list_remote_changes("CURSOR")
    assert result.entries == []
    assert result.cursor == "NEXT"
