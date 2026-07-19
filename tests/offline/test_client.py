"""Offline unit tests for bora.client.GoogleDriveClient.

Drive API calls are mocked at the `_get_service()` boundary: each test
builds a MagicMock service and assigns `client._get_service` to a Mock
returning it.

Round-trip tests (marked with the ``fake_drive`` fixture) use the
in-memory FakeDrive in place of the real API so the full client
translation code is exercised with realistic stateful responses.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from unittest.mock import MagicMock, Mock

import pytest

from bora.client import GoogleDriveClient, _MAX_RETRY_ATTEMPTS
from bora.config import remove_configuration
from bora.core import FileMetadata, FolderMetadata, WriteMode
from bora.database.core import Database
from bora.export import ExportOptions
from bora.keyring import CredentialStorage
from bora.path_cache import PathIDCache
from tests.fake_drive import ROOT_ID

_CONFIG = "test-config-client-offline"
_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


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
    from bora.exceptions import CancelledError

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


def test_retry_honors_retry_after_header(client, make_http_error):
    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    call = Mock(side_effect=[make_http_error(429, retry_after="7"), "ok"])
    assert client._retry_transient(call) == "ok"
    # The backoff sleep must use the Retry-After value, not the default.
    client._poll_event.wait.assert_called_once_with(7.0)


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
    from bora.core import FileMetadata

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
    from bora.core import FolderMetadata

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
    from bora.exceptions import NotFoundError

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
    from bora.core import FileMetadata

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


def test_get_metadata_file_uses_cached_cased_path_not_fold(client, cache):
    """7c-7: path_display must carry the cache's true cased path, not the
    casefolded lookup input. Seed a cased path, look it up via a casefolded
    variant (ß casefolds to ss), and assert the returned metadata preserves
    the original casing — otherwise the item materializes locally under the
    wrong name."""
    from bora.core import FileMetadata
    from bora.path_codec import fold_path

    _seed(cache, "/Straße.txt", "F7", "Straße.txt")
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "F7", "name": "Straße.txt", "mimeType": "text/plain",
        "parents": ["ROOT"], "modifiedTime": "2026-04-30T00:00:00Z",
        "size": "5", "md5Checksum": "h", "trashed": False,
    }
    client._get_service = Mock(return_value=service)

    meta = client.get_metadata("/strasse.txt")
    assert isinstance(meta, FileMetadata)
    assert meta.path_display == "/Straße.txt"
    assert meta.path_lower == fold_path("/Straße.txt")


def test_get_metadata_folder_uses_cached_cased_path_not_fold(client, cache):
    """7c-7: same as the file case, for the folder branch."""
    from bora.core import FolderMetadata

    _seed(cache, "/Straße", "D7", "Straße", is_folder=True)
    service = fake_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "D7", "name": "Straße",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["ROOT"], "trashed": False,
    }
    client._get_service = Mock(return_value=service)

    meta = client.get_metadata("/strasse")
    assert isinstance(meta, FolderMetadata)
    assert meta.path_display == "/Straße"


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
    from bora.exceptions import NotFoundError

    client._get_service = Mock(side_effect=AssertionError("no API call"))
    with pytest.raises(NotFoundError):
        client.list_revisions("/missing")


# ==== list_folder_iterator (non-recursive) ================================

def test_list_folder_non_recursive_yields_entries(client, cache):
    from bora.core import FileMetadata, FolderMetadata

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
    from bora.exceptions import NotFoundError

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
    changes.getStartPageToken call plus paginated files.list calls.

    ``_bootstrap_recursive`` also calls ``list_shared_roots()`` (a second,
    separate ``files.list`` paging loop) right before it populates the path
    cache, so one trailing empty page is appended to satisfy that extra
    ``execute()`` call after the ambient-listing pages are exhausted.
    """
    service = fake_service()
    service.changes.return_value.getStartPageToken.return_value.execute.return_value = {
        "startPageToken": start_token
    }
    service.files.return_value.list.return_value.execute.side_effect = (
        list(files_pages) + [{"files": []}]
    )
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

    with caplog.at_level(logging.INFO, logger="bora.client"):
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
    monkeypatch.setattr("bora.client.MediaIoBaseDownload", FakeDownloader)
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
    from bora.exceptions import UnsupportedFileError

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
    from bora.exceptions import DataCorruptionError

    monkeypatch.setattr("bora.client.MediaIoBaseDownload", FakeDownloader)
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
    assert not (tmp_path / "a.bin.bora-part").exists()


def test_download_cache_miss_raises(client, tmp_path):
    from bora.exceptions import NotFoundError

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

    monkeypatch.setattr("bora.client.MediaIoBaseDownload", FailingDownloader)
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
    assert not (tmp_path / "a.bin.bora-part").exists()


def test_download_updates_sync_event(client, cache, tmp_path, monkeypatch):
    monkeypatch.setattr("bora.client.MediaIoBaseDownload", FakeDownloader)
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


def test_download_by_file_id_skips_path_lookup(client, cache, tmp_path, monkeypatch):
    """When file_id is given, path_to_id must not be called and the Drive id
    used for both metadata get and get_media must be the supplied id."""
    import hashlib as _hashlib2

    monkeypatch.setattr("bora.client.MediaIoBaseDownload", FakeDownloader)

    # Seed the cache with a DIFFERENT id so we can prove path_to_id was NOT used.
    _seed(cache, "/whatever.txt", "WRONG_ID", "whatever.txt")

    data = b"file content by id"
    meta = {
        "id": "GIVEN_ID", "name": "whatever.txt",
        "mimeType": "application/octet-stream",
        "parents": ["ROOT"], "trashed": False, "size": str(len(data)),
        "md5Checksum": _hashlib2.md5(data).hexdigest(), "version": 7,
    }
    service = _download_service(meta, data)
    client._get_service = Mock(return_value=service)

    # Monkeypatch path_to_id to record if it was called (returns the wrong id).
    path_to_id_calls = []
    original_path_to_id = cache.path_to_id

    def recording_path_to_id(path):
        path_to_id_calls.append(path)
        return original_path_to_id(path)

    monkeypatch.setattr(cache, "path_to_id", recording_path_to_id)

    dest = tmp_path / "whatever.txt"
    result = client.download("/whatever.txt", str(dest), file_id="GIVEN_ID")

    # path_to_id must NOT have been called.
    assert path_to_id_calls == [], "path_to_id was called despite file_id being given"

    # The Drive fileId used for both get and get_media must be "GIVEN_ID".
    get_call_kwargs = service.files.return_value.get.call_args[1]
    assert get_call_kwargs["fileId"] == "GIVEN_ID", (
        f"get() was called with fileId={get_call_kwargs['fileId']!r}, expected 'GIVEN_ID'"
    )

    get_media_call_kwargs = service.files.return_value.get_media.call_args[1]
    assert get_media_call_kwargs["fileId"] == "GIVEN_ID", (
        f"get_media() was called with fileId={get_media_call_kwargs['fileId']!r}, expected 'GIVEN_ID'"
    )

    # The file was written correctly.
    assert dest.read_bytes() == data
    assert result.id == "GIVEN_ID"


def test_throttle_down_sleeps_when_limit_set(client, monkeypatch):
    slept = []
    monkeypatch.setattr("bora.client.time.sleep", lambda s: slept.append(s))
    client.bandwidth_limit_down = 1000  # bytes/sec
    client._throttle_down(2000)
    assert slept == [2.0]


def test_throttle_down_noop_when_no_limit(client, monkeypatch):
    slept = []
    monkeypatch.setattr("bora.client.time.sleep", lambda s: slept.append(s))
    client.bandwidth_limit_down = 0
    client._throttle_down(2000)
    assert slept == []


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


def test_wait_400_raises_cursor_reset_not_bad_input(client, make_http_error):
    """A 400 HttpError from changes.list must raise CursorResetError, not
    BadInputError.  The mapping depends on try placement inside
    convert_api_errors and outside _retry_transient; this test pins that.

    Note: CursorResetError and BadInputError are both direct subclasses of
    BoraApiError with no parent-child relationship, so
    pytest.raises(CursorResetError) already rules out BadInputError."""
    from bora.exceptions import CursorResetError

    client._poll_event = Mock()
    client._poll_event.wait.return_value = False  # timed out normally
    service = fake_service()
    service.changes.return_value.list.return_value.execute.side_effect = (
        make_http_error(400, "badRequest")
    )
    client._get_service = Mock(return_value=service)

    with pytest.raises(CursorResetError):
        client.wait_for_remote_changes("STALE_CURSOR", timeout=1)


def test_wait_403_raises_insufficient_permissions(client, make_http_error):
    """A non-400 HttpError must propagate through convert_api_errors normally
    and not be swallowed by the CursorResetError branch."""
    from bora.exceptions import InsufficientPermissionsError

    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    service = fake_service()
    service.changes.return_value.list.return_value.execute.side_effect = (
        make_http_error(403, "forbidden")
    )
    client._get_service = Mock(return_value=service)

    with pytest.raises(InsufficientPermissionsError):
        client.wait_for_remote_changes("CURSOR", timeout=1)


def test_wait_true_on_empty_page_with_next_token(client):
    """Drive may return an empty changes page WITH a nextPageToken (elided
    entries); that still means 'changes pending' (review F12).

    The poll gate must not return False on such a response — it would
    stall the download cycle indefinitely because the cursor only advances
    inside download_sync_cycle, not inside wait_for_remote_changes."""
    client._poll_event = Mock()
    client._poll_event.wait.return_value = False  # timed out normally
    service = fake_service()
    service.changes.return_value.list.return_value.execute.return_value = {
        "changes": [],
        "nextPageToken": "t2",
    }
    client._get_service = Mock(return_value=service)

    assert client.wait_for_remote_changes("t1", timeout=0) is True


def test_wait_true_on_non_empty_changes(client):
    """Non-empty changes page always returns True (symmetric baseline)."""
    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    service = fake_service()
    service.changes.return_value.list.return_value.execute.return_value = {
        "changes": [{"fileId": "F1"}],
    }
    client._get_service = Mock(return_value=service)

    assert client.wait_for_remote_changes("t1", timeout=0) is True


def test_wait_false_on_fully_empty_final_page(client):
    """A final page ({newStartPageToken, no changes, no nextPageToken})
    must return False — no changes are pending."""
    client._poll_event = Mock()
    client._poll_event.wait.return_value = False
    service = fake_service()
    service.changes.return_value.list.return_value.execute.return_value = {
        "newStartPageToken": "NEXT",
    }
    client._get_service = Mock(return_value=service)

    assert client.wait_for_remote_changes("t1", timeout=0) is False


# ==== list_remote_changes(_iterator) ======================================

def test_list_remote_changes_iterator_delegates(client, cache):
    from bora.core import FileMetadata

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

    from bora.exceptions import NotFoundError

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


# ==== writes.py wiring + delegators =======================================

def test_client_constructs_write_helper(client):
    from bora.writes import _WriteHelper

    assert isinstance(client._writes, _WriteHelper)
    assert client._writes._client is client


def test_upload_delegates_to_helper(client, monkeypatch):
    captured = {}

    def fake_upload(local_path, remote_path, *, write_mode, update_rev,
                    autorename, sync_event):
        captured.update(
            local_path=local_path, remote_path=remote_path,
            write_mode=write_mode, update_rev=update_rev,
            autorename=autorename, sync_event=sync_event,
        )
        return "sentinel-result"

    monkeypatch.setattr(client._writes, "upload", fake_upload)
    from bora.core import WriteMode

    out = client.upload(
        "/local/a.txt", "/a.txt",
        write_mode=WriteMode.Overwrite, update_rev="rev1",
        autorename=True, sync_event=None,
    )
    assert out == "sentinel-result"
    assert captured == {
        "local_path": "/local/a.txt", "remote_path": "/a.txt",
        "write_mode": WriteMode.Overwrite, "update_rev": "rev1",
        "autorename": True, "sync_event": None,
    }


def test_remove_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "remove",
        lambda remote_path, parent_rev=None: captured.update(
            remote_path=remote_path, parent_rev=parent_rev,
        ) or "sentinel",
    )
    assert client.remove("/a.txt", parent_rev="r1") == "sentinel"
    assert captured == {"remote_path": "/a.txt", "parent_rev": "r1"}


def test_move_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "move",
        lambda remote_path, new_path, *, autorename=False: captured.update(
            remote_path=remote_path, new_path=new_path, autorename=autorename,
        ) or "sentinel",
    )
    assert client.move("/a.txt", "/b/a.txt", autorename=True) == "sentinel"
    assert captured == {"remote_path": "/a.txt", "new_path": "/b/a.txt", "autorename": True}


def test_make_dir_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "make_dir",
        lambda remote_path, *, autorename=False: captured.update(
            remote_path=remote_path, autorename=autorename,
        ) or "sentinel",
    )
    assert client.make_dir("/foo", autorename=True) == "sentinel"
    assert captured == {"remote_path": "/foo", "autorename": True}


def test_make_dir_batch_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "make_dir_batch",
        lambda remote_paths: captured.update(paths=list(remote_paths)) or ["s"],
    )
    assert client.make_dir_batch(["/a", "/b"]) == ["s"]
    assert captured == {"paths": ["/a", "/b"]}


def test_remove_batch_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "remove_batch",
        lambda entries, batch_size=100: captured.update(
            entries=list(entries), batch_size=batch_size,
        ) or ["s"],
    )
    assert client.remove_batch([("/a", None), ("/b", "r")]) == ["s"]
    assert captured == {"entries": [("/a", None), ("/b", "r")], "batch_size": 100}


def test_restore_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "restore",
        lambda remote_path, rev: captured.update(
            remote_path=remote_path, rev=rev,
        ) or "sentinel",
    )
    assert client.restore("/a.txt", "rev5") == "sentinel"
    assert captured == {"remote_path": "/a.txt", "rev": "rev5"}


def test_create_shortcut_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "create_shortcut",
        lambda remote_path, target_remote_path, *, autorename=False: captured.update(
            remote_path=remote_path, target_remote_path=target_remote_path,
            autorename=autorename,
        ) or "sentinel",
    )
    assert client.create_shortcut("/link", "/Docs/report.pdf", autorename=True) == "sentinel"
    assert captured == {
        "remote_path": "/link", "target_remote_path": "/Docs/report.pdf",
        "autorename": True,
    }


def test_retarget_shortcut_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "retarget_shortcut",
        lambda remote_path, target_remote_path: captured.update(
            remote_path=remote_path, target_remote_path=target_remote_path,
        ) or "sentinel",
    )
    assert client.retarget_shortcut("/link", "/Docs/new.pdf") == "sentinel"
    assert captured == {"remote_path": "/link", "target_remote_path": "/Docs/new.pdf"}


# ==== FakeDrive round-trip tests ==========================================
# These tests wire the real GoogleDriveClient to the in-memory FakeDrive
# via the `client_on_fake` conftest fixture. They exercise real translation
# code (client.py / writes.py / changes.py) against stateful fake responses.


# ---- Step 1: new-file NotFoundError regression ---------------------------

def test_get_metadata_unknown_path_raises_notfound(client_on_fake):
    """Regression (Stage 5, 2ed6e33): get_metadata for a path the cache does
    not know raises NotFoundError — the upload-decision path keys off this to
    mean 'needs upload' rather than crashing.

    Verified against client.py lines 551-557: path_cache.path_to_id returns
    None for an unknown path, which triggers an explicit NotFoundError raise.
    """
    from bora.exceptions import NotFoundError

    cof = client_on_fake
    with pytest.raises(NotFoundError):
        cof.client.get_metadata("/does-not-exist.txt")


def test_check_requires_upload_for_new_file_returns_true(client_on_fake, tmp_path):
    """End-to-end regression for the Stage 5 fix: when a local file has no
    corresponding remote entry (path cache miss → NotFoundError), the
    upload-decision function _check_requires_upload returns True (upload
    required), rather than propagating the NotFoundError.

    This uses the real FakeDrive client (not a monkeypatch) so the
    NotFoundError is produced by the real get_metadata path-cache miss
    logic in client.py.
    """
    import os
    from datetime import datetime
    from bora.sync import SyncEngine
    from bora.models import (
        SyncEvent, SyncDirection, SyncStatus, ItemType, ChangeType
    )
    from bora.utils.appdirs import get_home_dir

    cof = client_on_fake

    # Build a minimal SyncEngine wired to the same client and a real local dir.
    local_dir = str(tmp_path)
    engine = SyncEngine(cof.client)
    engine.sync_path = local_dir

    # Write a local file so the event has realistic attributes.
    local_file = tmp_path / "new_file.txt"
    content = b"brand new content"
    local_file.write_bytes(content)
    local_md5 = hashlib.md5(content).hexdigest()

    # Build a synthetic added-file SyncEvent whose remote_path is completely
    # unknown to the path cache and the fake.
    remote_path = "/new_file.txt"
    event = SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=None,
        local_path=str(local_file),
        rev=None,
        content_hash=local_md5,
        symlink_target=None,
        change_type=ChangeType.Added,
        status=SyncStatus.Queued,
        size=len(content),
        completed=0,
    )

    # get_metadata("/new_file.txt") will raise NotFoundError because the path
    # cache has no entry for it.  _check_requires_upload must catch that and
    # return True (upload required).
    result = engine._check_requires_upload(event)
    assert result is True


# ---- Step 2: round-trip tests via the fake -------------------------------

def test_upload_then_get_metadata_shows_correct_fields(client_on_fake, tmp_path):
    """Upload a real local file, then get_metadata returns correct md5 / rev / size."""
    cof = client_on_fake

    content = b"round trip content"
    local_file = tmp_path / "up.txt"
    local_file.write_bytes(content)

    meta = cof.client.upload(str(local_file), "/up.txt", write_mode=WriteMode.Add)
    assert isinstance(meta, FileMetadata)
    assert meta.size == len(content)
    assert meta.content_hash == hashlib.md5(content).hexdigest()
    assert meta.rev  # non-empty revision ID

    # get_metadata should now resolve (upload writes through to cache).
    got = cof.client.get_metadata("/up.txt")
    assert isinstance(got, FileMetadata)
    assert got.size == len(content)
    assert got.content_hash == hashlib.md5(content).hexdigest()
    assert got.id == meta.id


def test_list_folder_reflects_seeded_tree(client_on_fake):
    """list_folder / list_folder_iterator reflects a fake-seeded tree."""
    cof = client_on_fake
    fake = cof.fake
    cache = cof.client._path_cache

    # Seed the fake and populate the path cache so list_folder_iterator can
    # resolve the parent folder ID.
    folder_id = fake.add_folder("/docs")
    cache.set("/docs", folder_id, ROOT_ID, "docs", True)

    file_id_a = fake.add_file("/docs/a.txt", content=b"alpha")
    cache.set("/docs/a.txt", file_id_a, folder_id, "a.txt", False)

    folder_id_sub = fake.add_folder("/docs/sub")
    cache.set("/docs/sub", folder_id_sub, folder_id, "sub", True)

    result = cof.client.list_folder("/docs")
    ids = {e.id for e in result.entries}
    assert file_id_a in ids
    assert folder_id_sub in ids

    # Iterator form: one page, has_more=False.
    pages = list(cof.client.list_folder_iterator("/docs"))
    assert len(pages) == 1
    assert pages[0].has_more is False
    assert {e.id for e in pages[0].entries} == ids


def test_move_then_get_metadata_at_new_path(client_on_fake, tmp_path):
    """move updates the path cache and get_metadata resolves at the new path."""
    cof = client_on_fake
    fake = cof.fake
    cache = cof.client._path_cache

    # Upload a file first (creates it in fake + seeds cache).
    content = b"movable"
    local_file = tmp_path / "orig.txt"
    local_file.write_bytes(content)
    meta = cof.client.upload(str(local_file), "/orig.txt", write_mode=WriteMode.Add)

    # Seed destination parent (root is already known as ROOT_ID).
    dest_folder_id = fake.add_folder("/dest")
    cache.set("/dest", dest_folder_id, ROOT_ID, "dest", True)

    # Move the file.
    moved = cof.client.move("/orig.txt", "/dest/orig.txt")
    assert isinstance(moved, (FileMetadata, FolderMetadata))
    assert moved.id == meta.id

    # get_metadata at new path must work.
    got = cof.client.get_metadata("/dest/orig.txt")
    assert isinstance(got, FileMetadata)
    assert got.id == meta.id

    # Old path must no longer resolve.
    from bora.exceptions import NotFoundError
    with pytest.raises(NotFoundError):
        cof.client.get_metadata("/orig.txt")


def test_remove_then_metadata_reflects_deletion(client_on_fake, tmp_path):
    """remove trashes the item; subsequent get_metadata raises NotFoundError
    (path evicted from cache), and the underlying Drive record is trashed.
    Also confirms that a separate fresh client seeing the trashed record
    via the change feed emits a DeletedMetadata entry."""
    from bora.core import DeletedMetadata
    from bora.exceptions import NotFoundError

    cof = client_on_fake
    fake = cof.fake
    cache = cof.client._path_cache

    # Upload a file.
    content = b"disposable"
    local_file = tmp_path / "del.txt"
    local_file.write_bytes(content)
    meta = cof.client.upload(str(local_file), "/del.txt", write_mode=WriteMode.Add)
    file_id = meta.id

    # Capture the cursor BEFORE the remove so we can replay the change.
    cursor_before_remove = str(fake._seq)

    # Re-seed the cache with the file so a second change-feed consumer can
    # find the path (simulating a second client that has the path in its cache
    # but hasn't processed the trash event yet).
    cache.set("/del.txt", file_id, ROOT_ID, "del.txt", False)

    # Remove it (trashes the item and evicts from cache).
    cof.client.remove("/del.txt")

    # 1. The Drive record is now trashed=True; the cache no longer has the path.
    #    get_metadata should raise NotFoundError (cache miss after eviction).
    with pytest.raises(NotFoundError):
        cof.client.get_metadata("/del.txt")

    # 2. The underlying fake record has trashed=True.
    assert fake.files[file_id]["trashed"] is True

    # 3. A fresh client (same fake, re-seeded cache) processes the change feed
    #    and produces a DeletedMetadata for the trashed item.
    import sqlite3 as _sqlite3
    fresh_cache = PathIDCache(
        __import__("bora.database.core", fromlist=["Database"]).Database(
            _sqlite3.connect(":memory:")
        )
    )
    fresh_cache.root_id = ROOT_ID
    fresh_cache.set("/del.txt", file_id, ROOT_ID, "del.txt", False)

    fresh_client = GoogleDriveClient(
        "test-config-fresh", CredentialStorage("test-config-fresh"), fresh_cache
    )
    fresh_client._get_service = lambda: fake.service()

    changes_result = fresh_client.list_remote_changes(cursor_before_remove)
    deleted_paths = {
        e.path_display for e in changes_result.entries
        if isinstance(e, DeletedMetadata)
    }
    assert "/del.txt" in deleted_paths, (
        f"Expected DeletedMetadata for /del.txt; got entries: {changes_result.entries}"
    )

    # Cleanup the fresh client config.
    from bora.config import remove_configuration as _rm
    _rm("test-config-fresh")


def test_download_via_fake_writes_correct_bytes(client_on_fake, tmp_path):
    """Download via real MediaIoBaseDownload writes correct bytes and md5."""
    cof = client_on_fake
    fake = cof.fake
    cache = cof.client._path_cache

    content = b"download me please"
    file_id = fake.add_file("/dl.txt", content=content)
    cache.set("/dl.txt", file_id, ROOT_ID, "dl.txt", False)

    dest = tmp_path / "dl.txt"
    result = cof.client.download("/dl.txt", str(dest))

    assert dest.read_bytes() == content
    assert isinstance(result, FileMetadata)
    assert result.id == file_id
    assert result.content_hash == hashlib.md5(content).hexdigest()


# ==== Task 17: create/retarget Drive shortcuts (M3) =======================

def test_create_shortcut_sets_target(client_on_fake):
    client, fake = client_on_fake.client, client_on_fake.fake
    # Seed a target file and its cache entry.
    fake.add_folder("/Docs")
    fake.add_file("/Docs/report.pdf", content=b"x")
    # Bootstrap the cache so path_to_id resolves /Docs and /Docs/report.pdf.
    list(client.list_folder_iterator("/", recursive=True))
    result = client.create_shortcut("/Docs/link", "/Docs/report.pdf")
    assert result.symlink_target == "report.pdf"
    assert result.is_downloadable is False
    # The created Drive object is a shortcut pointing at the target's id.
    created = fake.get_by_path("/Docs/link")
    assert created["mimeType"] == "application/vnd.google-apps.shortcut"
    assert created["shortcutDetails"]["targetId"] == fake.id_of("/Docs/report.pdf")
    # Cache write-through so a following retarget/remove resolves the shortcut.
    assert client._path_cache.path_to_id("/Docs/link") == created["id"]


def test_retarget_shortcut_updates_target_id(client_on_fake):
    client, fake = client_on_fake.client, client_on_fake.fake
    fake.add_folder("/Docs")
    fake.add_file("/Docs/report.pdf", content=b"x")
    fake.add_file("/Docs/other.pdf", content=b"y")
    list(client.list_folder_iterator("/", recursive=True))
    client.create_shortcut("/Docs/link", "/Docs/report.pdf")

    result = client.retarget_shortcut("/Docs/link", "/Docs/other.pdf")

    assert result.symlink_target == "other.pdf"
    updated = fake.get_by_path("/Docs/link")
    assert updated["shortcutDetails"]["targetId"] == fake.id_of("/Docs/other.pdf")


# ==== list_folder_iterator (recursive scoped — review F1) =================

def test_recursive_list_of_subfolder_is_scoped(client_on_fake, fake_drive):
    """list_folder(path, recursive=True) for a non-root path must return only
    that subtree and must NOT rebuild the path cache (review F1)."""
    cof = client_on_fake
    cache = cof.client._path_cache

    sub_id = fake_drive.add_folder("/sub")
    inner_id = fake_drive.add_file("/sub/inner.txt", content=b"x")
    out_id = fake_drive.add_file("/outside.txt", content=b"y")

    # Bootstrap once so the cache knows everything.
    cof.client.list_folder("/", recursive=True)

    result = cof.client.list_folder("/sub", recursive=True)
    paths = {md.path_display for md in result.entries}
    assert paths == {"/sub/inner.txt"}, paths

    # Cache must still know the out-of-subtree file (no clear+rebuild).
    assert cache.path_to_id("/outside.txt") == out_id
    assert cache.path_to_id("/sub") == sub_id
    assert cache.path_to_id("/sub/inner.txt") == inner_id


def test_recursive_list_of_root_still_bootstraps(client_on_fake, fake_drive):
    """recursive=True on '/' keeps the bootstrap semantics (initial sync)."""
    cof = client_on_fake
    fake_drive.add_file("/a.txt", content=b"a")
    result = cof.client.list_folder("/", recursive=True)
    assert {md.path_display for md in result.entries} == {"/a.txt"}


def test_scoped_recursive_writes_new_items_through_to_cache(client_on_fake, fake_drive):
    """Items created on Drive after bootstrap are discovered by a scoped
    listing and become resolvable in the path cache."""
    fake_drive.add_folder("/sub")
    client_on_fake.client.list_folder("/", recursive=True)
    new_file = fake_drive.add_file("/sub/late.txt", content=b"z")
    nested = fake_drive.add_folder("/sub/nested")
    deep = fake_drive.add_file("/sub/nested/deep.txt", content=b"d")

    result = client_on_fake.client.list_folder("/sub", recursive=True)

    cache = client_on_fake.client._path_cache
    assert cache.path_to_id("/sub/late.txt") == new_file
    assert cache.path_to_id("/sub/nested") == nested
    assert cache.path_to_id("/sub/nested/deep.txt") == deep
    assert {md.path_display for md in result.entries} == {
        "/sub/late.txt", "/sub/nested", "/sub/nested/deep.txt",
    }


def test_scoped_recursive_skips_fold_conflicted_entry(client_on_fake, fake_drive):
    """A Drive file whose case-folded name collides with an already-cached
    entry must not appear in results, and the cache must still map the
    folded path to the original file id."""
    fake_drive.add_folder("/sub")
    orig_id = fake_drive.add_file("/sub/Report.pdf", content=b"original")
    # Bootstrap so /sub/Report.pdf is in the cache.
    client_on_fake.client.list_folder("/", recursive=True)

    # Add a second file whose name folds to the same key as Report.pdf.
    fake_drive.add_file("/sub/report.pdf", content=b"conflict",
                        literal_name="report.pdf")

    result = client_on_fake.client.list_folder("/sub", recursive=True)

    cache = client_on_fake.client._path_cache
    # The colliding entry must be absent from results.
    result_paths = {md.path_display for md in result.entries}
    assert "/sub/report.pdf" not in result_paths
    # The cache must still resolve the folded key to the original id.
    assert cache.path_to_id("/sub/Report.pdf") == orig_id


def test_list_shared_roots_returns_shared_items(client_on_fake) -> None:
    """list_shared_roots() queries sharedWithMe=true and returns the raw
    Drive file dicts for shared attach points (spec §2 belt-and-suspenders
    seed source)."""
    client, fake = client_on_fake.client, client_on_fake.fake
    fake.add_shared_root("Team Docs", shared_with_me=True)
    roots = client.list_shared_roots()
    assert any(r["name"] == "Team Docs" and r.get("sharedWithMe") for r in roots)


# ==== Task 4c: export projection through listing / metadata ===============


def test_export_opts_defaults_disabled(client):
    assert client.export_opts == ExportOptions.disabled()
    assert client.export_opts.enabled is False


def test_get_metadata_exportable_doc_when_export_enabled(client_on_fake):
    """With export enabled a native Doc resolves at its projected path and
    get_metadata returns FileMetadata carrying the export identity."""
    cof = client_on_fake
    fake = cof.fake
    cof.client.export_opts = ExportOptions(True, False)
    fid = fake.add_native("Report", "document", parent="/")
    fake.set_export_bytes(fid, _DOCX_MIME, b"docx-bytes")
    # Bootstrap populates the cache with the projected leaf name.
    list(cof.client.list_folder_iterator("/", recursive=True))

    meta = cof.client.get_metadata("/Report.docx")
    assert meta is not None
    assert isinstance(meta, FileMetadata)
    assert meta.id == fid
    assert meta.path_display == "/Report.docx"
    assert meta.export_mime == _DOCX_MIME
    assert meta.name == "Report"  # raw Drive name preserved


def test_get_metadata_exportable_doc_none_when_disabled(client_on_fake):
    """Export disabled -> a native Doc still returns None (unchanged)."""
    cof = client_on_fake
    fake = cof.fake
    fid = fake.add_native("Report", "document", parent="/")
    cof.client._path_cache.set("/Report", fid, ROOT_ID, "Report", False)
    assert cof.client.get_metadata("/Report") is None


def test_list_folder_projects_exportable_when_enabled(client_on_fake):
    """list_folder lists a native Doc as Report.docx when export is on."""
    cof = client_on_fake
    fake = cof.fake
    cof.client.export_opts = ExportOptions(True, False)
    folder = fake.add_folder("/docs")
    cof.client._path_cache.set("/docs", folder, ROOT_ID, "docs", True)
    fid = fake.add_native("Report", "document", parent="/docs")

    result = cof.client.list_folder("/docs")
    by_path = {e.path_display: e for e in result.entries}
    assert "/docs/Report.docx" in by_path
    md = by_path["/docs/Report.docx"]
    assert md.id == fid
    assert md.export_mime == _DOCX_MIME
    assert md.name == "Report"


def test_list_folder_drops_form_when_export_enabled(client_on_fake):
    """A Form is unsupported-native: dropped whether or not export is on."""
    cof = client_on_fake
    fake = cof.fake
    cof.client.export_opts = ExportOptions(True, False)
    folder = fake.add_folder("/docs")
    cof.client._path_cache.set("/docs", folder, ROOT_ID, "docs", True)
    fake.add_native("Survey", "form", parent="/docs")
    fake.add_file("/docs/a.txt", content=b"x")

    paths = {e.path_display for e in cof.client.list_folder("/docs").entries}
    assert "/docs/a.txt" in paths
    assert not any("Survey" in p for p in paths)


def test_list_folder_drops_exportable_when_disabled(client_on_fake):
    """Export disabled -> native Docs are filtered out, byte-identical to
    today's behavior."""
    cof = client_on_fake
    fake = cof.fake
    folder = fake.add_folder("/docs")
    cof.client._path_cache.set("/docs", folder, ROOT_ID, "docs", True)
    fake.add_native("Report", "document", parent="/docs")
    fake.add_file("/docs/a.txt", content=b"x")

    paths = {e.path_display for e in cof.client.list_folder("/docs").entries}
    assert "/docs/a.txt" in paths
    assert not any("Report" in p for p in paths)


def test_bootstrap_projects_exportable_and_keeps_shortcut(client_on_fake):
    """Recursive bootstrap projects exportables (export on) while leaving the
    shortcut->symlink flow untouched (shortcuts still cached/listed)."""
    cof = client_on_fake
    fake = cof.fake
    cof.client.export_opts = ExportOptions(True, False)
    target = fake.add_file("/target.txt", content=b"x")
    fake._insert("Link", ROOT_ID, "application/vnd.google-apps.shortcut", b"")
    fake.add_native("Report", "document", parent="/")

    list(cof.client.list_folder_iterator("/", recursive=True))
    cache = cof.client._path_cache
    assert cache.path_to_id("/Report.docx") is not None
    # Shortcut is still indexed by the bootstrap (symlink flow unaffected).
    assert cache.path_to_id("/Link") is not None
    assert target is not None


# ==== Task 5: download() export branch =====================================


def test_download_exports_native_doc(client_on_fake, tmp_path):
    """Export enabled: downloading an exportable Doc streams export_media
    bytes, hashes them locally, and returns the export identity."""
    cof = client_on_fake
    fake = cof.fake
    cof.client.export_opts = ExportOptions(True, False)
    file_id = fake.add_native(name="Report", kind="document")
    fake.set_export_bytes(file_id, _DOCX_MIME, b"DOCX")
    cof.client._path_cache.set("/Report.docx", file_id, ROOT_ID, "Report", False)

    dest = tmp_path / "Report.docx"
    md = cof.client.download("/Report.docx", str(dest), file_id=file_id)

    assert dest.read_bytes() == b"DOCX"
    assert not (tmp_path / "Report.docx.bora-part").exists()
    assert md.content_hash == hashlib.md5(b"DOCX").hexdigest()
    assert md.export_mime == _DOCX_MIME
    assert md.rev == "1"
    assert md.path_display == "/Report.docx"
    assert md.size == len(b"DOCX")


def test_download_exports_markdown_variant(client_on_fake, tmp_path):
    """Export enabled + markdown: a Doc is exported via text/markdown."""
    from bora.native_docs import MARKDOWN_MIME

    cof = client_on_fake
    fake = cof.fake
    cof.client.export_opts = ExportOptions(True, True)
    file_id = fake.add_native(name="Report", kind="document")
    fake.set_export_bytes(file_id, MARKDOWN_MIME, b"# Report\n")
    cof.client._path_cache.set("/Report.md", file_id, ROOT_ID, "Report", False)

    dest = tmp_path / "Report.md"
    md = cof.client.download("/Report.md", str(dest), file_id=file_id)

    assert dest.read_bytes() == b"# Report\n"
    assert md.content_hash == hashlib.md5(b"# Report\n").hexdigest()
    assert md.export_mime == MARKDOWN_MIME


def test_download_exportable_doc_raises_when_export_disabled(client_on_fake, tmp_path):
    """Export disabled: an exportable Doc still raises UnsupportedFileError,
    so behavior is unchanged when the feature is off."""
    from bora.exceptions import UnsupportedFileError

    cof = client_on_fake
    fake = cof.fake
    file_id = fake.add_native(name="Report", kind="document")
    cof.client._path_cache.set("/Report", file_id, ROOT_ID, "Report", False)

    dest = tmp_path / "Report"
    with pytest.raises(UnsupportedFileError):
        cof.client.download("/Report", str(dest), file_id=file_id)


def test_download_unsupported_native_raises_even_when_export_enabled(
    client_on_fake, tmp_path
):
    """A genuinely non-exportable native (Form) must still raise
    UnsupportedFileError via the is_unsupported_native branch, even with
    export enabled — pins that branch separately from the
    exportable-but-disabled branch (which a Docs mime would otherwise hit
    first, masking a swapped condition)."""
    from bora.exceptions import UnsupportedFileError

    cof = client_on_fake
    fake = cof.fake
    cof.client.export_opts = ExportOptions(True, False)
    file_id = fake.add_native(name="Survey", kind="form")
    cof.client._path_cache.set("/Survey", file_id, ROOT_ID, "Survey", False)

    dest = tmp_path / "Survey"
    with pytest.raises(UnsupportedFileError):
        cof.client.download("/Survey", str(dest), file_id=file_id)
