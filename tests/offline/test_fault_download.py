"""Fault-injection tests for the download path (Stage 7b-E2, Task 6).

Covers:
  A — truncated ``get_media`` bytes → MD5 mismatch → DataCorruptionError,
      sidecar cleaned up, target file not committed.
  B — wrong advertised md5 (bytes correct, checksum wrong) → DataCorruptionError,
      same cleanup guarantees; teeth test confirms the md5-comparison mechanism
      is load-bearing (wrong_md5 mode + clean recovery after checksum restore).
  C — mid-stream fault on the 2nd chunk of a multi-chunk download → per-chunk
      _retry_transient recovers transparently; final file matches original MD5.

Sleep neutralisation
--------------------
``_retry_transient`` backs off by calling ``self._poll_event.wait(sleep_for)``.
Returning False from ``wait`` means "not cancelled, proceed".  We monkeypatch
``client._poll_event.wait`` to ``lambda t: False`` and ``is_set`` to
``lambda: False`` so retries are instant with no real delay.
"""

from __future__ import annotations

import hashlib
import os.path as osp
import sqlite3
from unittest.mock import Mock

import pytest

from bora.client import GoogleDriveClient, _DOWNLOAD_CHUNK_SIZE
from bora.config import remove_configuration
from bora.database.core import Database
from bora.exceptions import DataCorruptionError
from bora.keyring import CredentialStorage
from bora.path_cache import PathIDCache
from tests.fake_drive import ROOT_ID, FakeDrive, make_timeout

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_CONFIG = "test-config-fault-download"


def _make_cache() -> PathIDCache:
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = ROOT_ID
    return c


@pytest.fixture
def cof():
    """Client-on-fake with neutralised retry sleep and ensure_root_id."""
    fake = FakeDrive()
    cache = _make_cache()
    client = GoogleDriveClient(_CONFIG, CredentialStorage(_CONFIG), cache)
    client._get_service = lambda: fake.service()
    client._ensure_root_id = Mock()
    # Neutralise retry sleep: wait() returning False means "not cancelled".
    client._poll_event = Mock()
    client._poll_event.is_set = Mock(return_value=False)
    client._poll_event.wait = Mock(return_value=False)
    yield client, fake
    remove_configuration(_CONFIG)


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


# ===========================================================================
# Scenario A — truncated bytes → DataCorruptionError, no committed file
# ===========================================================================


def test_truncated_download_raises_and_leaves_no_file(cof, tmp_path):
    """Truncated get_media → DataCorruptionError; target + sidecar are cleaned up."""
    client, fake = cof

    fid = fake.add_file("/big.bin", content=b"z" * 4096)
    client._path_cache.set("/big.bin", fid, ROOT_ID, "big.bin", False)
    fake.corrupt_download(fid, mode="truncate")

    local = str(tmp_path / "big.bin")
    with pytest.raises(DataCorruptionError):
        client.download("/big.bin", local)

    assert not osp.exists(local), "target file must not exist after corruption"
    assert not osp.exists(local + ".bora-part"), "sidecar must be cleaned up"


def test_truncated_download_teeth(cof, tmp_path):
    """Teeth: bypass the MD5 check → truncated file IS committed.

    This proves the above test is sensitive to the integrity check: without
    the check, a truncated download silently overwrites the target path.

    We bypass the check by aligning the advertised md5Checksum with the
    truncated content (i.e. making a 'consistent but wrong' server response)
    so that ``if expected: if hasher.hexdigest() != expected: raise`` is never
    triggered.
    """
    client, fake = cof

    content = b"z" * 4096
    fid = fake.add_file("/big.bin", content=content)
    client._path_cache.set("/big.bin", fid, ROOT_ID, "big.bin", False)

    # Arm truncation: get_media will serve content[:-1].
    fake.corrupt_download(fid, mode="truncate")

    # Make the advertised md5 match the TRUNCATED content so the check passes.
    # This simulates "no integrity guard" — any discrepancy goes undetected.
    truncated = content[:-1]
    fake.files[fid]["md5Checksum"] = _md5(truncated)

    local = str(tmp_path / "big.bin")
    # No DataCorruptionError — the truncated file is committed.
    client.download("/big.bin", local)

    # The corrupted (short) file landed on disk.
    assert osp.exists(local)
    with open(local, "rb") as fh:
        on_disk = fh.read()
    assert on_disk == truncated, "teeth: truncated bytes were committed without the check"


# ===========================================================================
# Scenario B — wrong advertised md5 → DataCorruptionError, no committed file
# ===========================================================================


def test_flipped_byte_download_raises_and_leaves_no_file(cof, tmp_path):
    """Flipped-byte get_media → DataCorruptionError; same cleanup guarantees."""
    client, fake = cof

    fid = fake.add_file("/flip.bin", content=b"A" * 4096)
    client._path_cache.set("/flip.bin", fid, ROOT_ID, "flip.bin", False)
    fake.corrupt_download(fid, mode="flip")

    local = str(tmp_path / "flip.bin")
    with pytest.raises(DataCorruptionError):
        client.download("/flip.bin", local)

    assert not osp.exists(local), "target file must not exist after integrity failure"
    assert not osp.exists(local + ".bora-part"), "sidecar must be cleaned up"


def test_wrong_md5_download_raises_then_clean_recovery_teeth(cof, tmp_path):
    """Teeth: integrity-check mechanism is load-bearing (exercised via wrong_md5 mode).

    ``wrong_md5`` mode leaves the served bytes correct but mutates the
    record's ``md5Checksum`` to a known-wrong value.  This isolates the
    MD5 *comparison* as the detection mechanism: the check must fire even
    when bytes are valid, purely because the advertised checksum differs.

    Phase 1 — wrong_md5 armed: DataCorruptionError is raised and no file is
    committed, proving the integrity guard is load-bearing.

    Phase 2 — checksum restored: the correct md5 is written back to the
    record and a clean download of the same file succeeds, confirming the
    error was caused solely by the checksum mismatch and not by byte damage.
    """
    client, fake = cof

    content = b"A" * 4096
    fid = fake.add_file("/flip.bin", content=content)
    client._path_cache.set("/flip.bin", fid, ROOT_ID, "flip.bin", False)

    # wrong_md5 mode: bytes correct, advertised md5 wrong.
    fake.corrupt_download(fid, mode="wrong_md5")

    local = str(tmp_path / "flip.bin")
    with pytest.raises(DataCorruptionError):
        client.download("/flip.bin", local)

    # Now restore the correct md5 — download should succeed.
    fake.files[fid]["md5Checksum"] = _md5(content)
    local2 = str(tmp_path / "flip2.bin")
    client.download("/flip.bin", local2)
    assert osp.exists(local2)
    with open(local2, "rb") as fh:
        assert fh.read() == content


# ===========================================================================
# Scenario C — mid-stream timeout on 2nd chunk → retry recovers
# ===========================================================================


def test_mid_chunk_timeout_retries_and_produces_correct_file(cof, tmp_path):
    """Multi-chunk download: 2nd chunk faults with timeout; _retry_transient recovers.

    A file larger than _DOWNLOAD_CHUNK_SIZE (8MB) causes MediaIoBaseDownload
    to issue multiple range requests.  Arming fail_on_call("files.get_media", 2, ...)
    injects a Timeout into the 2nd chunk's http.request() call.

    _retry_transient wraps each downloader.next_chunk() call.  When the Timeout
    propagates out of next_chunk(), _retry_transient catches it (Timeout is in
    CONNECTION_ERRORS), sleeps (neutralised by the mock), and retries the same
    next_chunk() call.  MediaIoBaseDownload is stateful: it reissues the same
    range request for the 2nd chunk.  The retry succeeds and the download
    completes.

    Assert: final file exists, has the correct MD5, no sidecar is left.
    """
    client, fake = cof

    # Content spans at least 2 chunks.
    size = _DOWNLOAD_CHUNK_SIZE + 1024
    content = b"X" * size
    expected_md5 = _md5(content)

    fid = fake.add_file("/multi.bin", content=content)
    client._path_cache.set("/multi.bin", fid, ROOT_ID, "multi.bin", False)

    # Fault the 2nd chunk request only.
    fake.fail_on_call("files.get_media", 2, make_timeout())

    local = str(tmp_path / "multi.bin")
    result = client.download("/multi.bin", local)

    # File committed correctly.
    assert osp.exists(local), "file must exist after successful download with mid-chunk retry"
    assert not osp.exists(local + ".bora-part"), "no sidecar should remain"

    with open(local, "rb") as fh:
        on_disk = fh.read()
    assert _md5(on_disk) == expected_md5, "on-disk md5 must match original content"
    assert result.content_hash == expected_md5


def test_mid_chunk_timeout_teeth(cof, tmp_path):
    """Teeth: exhaust ALL retry attempts on every chunk → BoraApiError raised.

    This confirms the above test's success came from the retry recovering, not
    from the fault silently not firing.

    Arm fail_next("files.get_media", timeout, count=_MAX_RETRY_ATTEMPTS) so
    every chunk request faults.  After exhausting _MAX_RETRY_ATTEMPTS the
    client must raise (wrapped as a Bora connection error), and no file is
    committed.
    """
    from bora.client import _MAX_RETRY_ATTEMPTS
    from bora.exceptions import ConnectionError as _BoraConnError

    client, fake = cof

    size = _DOWNLOAD_CHUNK_SIZE + 1024
    content = b"Y" * size
    fid = fake.add_file("/multi.bin", content=content)
    client._path_cache.set("/multi.bin", fid, ROOT_ID, "multi.bin", False)

    # Exhaust all retries: every chunk request faults.
    fake.fail_next("files.get_media", make_timeout(), count=_MAX_RETRY_ATTEMPTS)

    local = str(tmp_path / "multi.bin")
    with pytest.raises(_BoraConnError):
        client.download("/multi.bin", local)

    assert not osp.exists(local), "no file should be committed when all retries exhausted"
    assert not osp.exists(local + ".bora-part"), "sidecar must be cleaned up on error"
