"""Fault-injection tests for write paths (Stage 7b-E2, Task 5).

Covers:
  A — resumable upload: first next_chunk faults (timeout) → _retry_transient
      retries → exactly ONE file with correct md5, no truncation or duplicate.
  B — remove_batch: one fileId faults mid-batch with a 500 → the other N-1
      items are trashed correctly; result list has exactly one error slot for
      the failing id.
  C — move / make_dir: injected 503s (count=2) → _retry_transient converges;
      retry is bounded (count=_MAX_RETRY_ATTEMPTS+1 > max → raises).

Sleep neutralisation
--------------------
``_retry_transient`` backs off by calling ``self._poll_event.wait(sleep_for)``.
Returning False from wait means "not cancelled, proceed".  We monkeypatch
``client._poll_event.wait`` to ``lambda t: False`` so retries are instant.
"""

from __future__ import annotations

import hashlib
import sqlite3
from unittest.mock import Mock

import pytest

from bora.client import GoogleDriveClient, _MAX_RETRY_ATTEMPTS
from bora.config import remove_configuration
from bora.core import WriteMode
from bora.writes import _SIMPLE_UPLOAD_THRESHOLD
from bora.database.core import Database
from bora.exceptions import BoraApiError
from bora.keyring import CredentialStorage
from bora.path_cache import PathIDCache
from tests.fake_drive import (
    ROOT_ID,
    FakeDrive,
    make_conn_reset,
    make_http_error,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_CONFIG = "test-config-fault-writes"


def _make_cache() -> PathIDCache:
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = ROOT_ID
    return c


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


@pytest.fixture
def cof():
    """Minimal client-on-fake: real GoogleDriveClient wired to FakeDrive.

    sleep neutralised: _poll_event.wait returns False so _retry_transient
    doesn't actually sleep between retries.
    """
    fake = FakeDrive()
    cache = _make_cache()
    client = GoogleDriveClient(_CONFIG, CredentialStorage(_CONFIG), cache)
    client._get_service = lambda: fake.service()
    # Neutralise ensure-root-id network call; cache already seeded.
    client._ensure_root_id = Mock()
    # Neutralise retry sleep: wait() returning False means "not cancelled".
    # is_set() must return False so the upload loop does not abort immediately.
    client._poll_event = Mock()
    client._poll_event.is_set = Mock(return_value=False)
    client._poll_event.wait = Mock(return_value=False)
    yield client, fake
    remove_configuration(_CONFIG)


# ===========================================================================
# Scenario A — resumable upload: mid-chunk fault → retry → ONE correct file
# ===========================================================================


def test_resumable_fault_first_chunk_retries_to_single_correct_file(
    cof, tmp_path
):
    """Arm a connection-reset on the first next_chunk() call for files.create.

    The fake's _ResumableRequest calls _maybe_fail("files.create", ...) at
    the top of every next_chunk() invocation.  fail_next("files.create", ...)
    fires before the step counter advances, so the retry restarts at step 0
    and the upload completes cleanly.

    Assert: fake.files has exactly ONE non-ROOT entry; its md5 matches the
    local file; it is not trashed.
    """
    client, fake = cof

    size = _SIMPLE_UPLOAD_THRESHOLD + 1  # just over threshold -> resumable path
    data = b"x" * size
    local = tmp_path / "big.bin"
    local.write_bytes(data)
    expected_md5 = _md5(data)

    # Arm: the very first next_chunk call faults with a connection reset.
    fake.fail_next("files.create", make_conn_reset(), count=1)

    result = client._writes.upload(str(local), "/big.bin", write_mode=WriteMode.Add)

    # Exactly one non-ROOT file in the fake store.
    non_root = {fid: rec for fid, rec in fake.files.items() if fid != ROOT_ID}
    assert len(non_root) == 1, (
        f"Expected exactly 1 file; got {len(non_root)}: "
        + str(list(non_root.keys()))
    )

    fid, rec = next(iter(non_root.items()))
    assert rec["md5Checksum"] == expected_md5, (
        f"md5 mismatch: got {rec['md5Checksum']!r}, expected {expected_md5!r}"
    )
    assert rec["size"] == size
    assert rec["trashed"] is False
    assert result.id == fid
    assert result.content_hash == expected_md5


def test_resumable_fault_first_chunk_teeth(cof, tmp_path):
    """Teeth: arm count=_MAX_RETRY_ATTEMPTS so every attempt faults.

    After exhausting retries _retry_transient re-raises; the upload must fail
    and no file must be created in fake.files.  This confirms that the success
    in the above test was actually produced by the retry path, not by the
    fault silently not firing.
    """
    client, fake = cof

    size = _SIMPLE_UPLOAD_THRESHOLD + 1  # just over threshold -> resumable path
    data = b"z" * size
    local = tmp_path / "big2.bin"
    local.write_bytes(data)

    # Arm all attempts — every next_chunk call at step 0 faults.
    fake.fail_next("files.create", make_conn_reset(), count=_MAX_RETRY_ATTEMPTS)

    # After exhausting retries, _retry_transient re-raises the CONNECTION_ERROR;
    # convert_api_errors then wraps it as bora.exceptions.ConnectionError.
    from bora.exceptions import ConnectionError as _BoraConnError
    with pytest.raises(_BoraConnError):
        client._writes.upload(str(local), "/big2.bin", write_mode=WriteMode.Add)

    non_root = {fid: rec for fid, rec in fake.files.items() if fid != ROOT_ID}
    assert len(non_root) == 0, (
        "No file should have been created when all retries were exhausted"
    )


# ===========================================================================
# Scenario B — remove_batch: one fileId faults → siblings not lost
# ===========================================================================


def _seed_files(client: GoogleDriveClient, fake: FakeDrive, paths: list[str]):
    """Add files to fake + path cache; return list of (path, file_id) pairs."""
    pairs = []
    for p in paths:
        name = p.lstrip("/")
        fid = fake.add_file(p, b"content")
        client._path_cache.set(p, fid, parent_id=ROOT_ID, name=name, is_folder=False)
        pairs.append((p, fid))
    return pairs


def test_remove_batch_one_fault_sibling_items_trashed(cof):
    """Seed 4 files; arm a 500 on one fileId inside the batch.

    Assert:
    - result list slot for target_id contains a BoraApiError (not trashed).
    - all other 3 slots contain successful metadata objects.
    - the 3 non-target files are trashed in fake.files.
    - the target file is NOT trashed in fake.files.
    """
    client, fake = cof

    paths = ["/a.txt", "/b.txt", "/c.txt", "/d.txt"]
    pairs = _seed_files(client, fake, paths)

    # Target the second file (index 1).
    target_path, target_id = pairs[1]

    # Arm: next files.update whose fileId == target_id raises 500.
    fake.fail_when(
        "files.update",
        lambda **kw: kw.get("fileId") == target_id,
        make_http_error(500),
    )

    entries = [(p, None) for p, _ in pairs]
    results = client._writes.remove_batch(entries)

    # Result list length matches input.
    assert len(results) == 4

    # Target slot is an error.
    assert isinstance(results[1], BoraApiError), (
        f"Expected BoraApiError at slot 1; got {type(results[1]).__name__}"
    )

    # Non-target slots are success metadata.
    for i, (path, fid) in enumerate(pairs):
        if i == 1:
            continue
        assert not isinstance(results[i], BoraApiError), (
            f"Slot {i} should be success metadata; got {type(results[i]).__name__}"
        )

    # Non-target files are trashed in the fake.
    for i, (path, fid) in enumerate(pairs):
        if i == 1:
            assert fake.files[fid]["trashed"] is False, (
                f"Target {fid} must NOT be trashed"
            )
        else:
            assert fake.files[fid]["trashed"] is True, (
                f"Non-target {fid} at path {path!r} must be trashed"
            )


def test_remove_batch_one_fault_sibling_teeth(cof):
    """Teeth: arm a 500 on ALL four files.

    All four result slots should be BoraApiError instances and no files
    are trashed.  This confirms the per-slot error routing is real.
    """
    client, fake = cof

    paths = ["/a.txt", "/b.txt", "/c.txt", "/d.txt"]
    pairs = _seed_files(client, fake, paths)

    # Arm all four with 500s (remaining=4 so each call to files.update faults).
    fake.fail_next("files.update", make_http_error(500), count=4)

    entries = [(p, None) for p, _ in pairs]
    results = client._writes.remove_batch(entries)

    # All four slots must be errors.
    for i, r in enumerate(results):
        assert isinstance(r, BoraApiError), (
            f"Expected BoraApiError at slot {i}; got {type(r).__name__}"
        )

    # No files trashed.
    for path, fid in pairs:
        assert fake.files[fid]["trashed"] is False, (
            f"File {fid} at {path!r} must not be trashed when its batch call faulted"
        )


# ===========================================================================
# Scenario C — move / make_dir: 5xx → _retry_transient converges
# ===========================================================================


def test_move_503_count2_converges(cof):
    """Arm 2 consecutive 503s on files.update → move converges after 2 retries.

    Assert: the file has the new name in fake.files; exactly ONE file exists
    (no duplicate created).
    """
    client, fake = cof

    fid = fake.add_file("/foo.txt", b"content")
    client._path_cache.set(
        "/foo.txt", fid, parent_id=ROOT_ID, name="foo.txt", is_folder=False
    )

    # Arm 2 transient 503s before the successful call.
    fake.fail_next("files.update", make_http_error(503), count=2)

    result = client._writes.move("/foo.txt", "/bar.txt")

    # Rename applied correctly.
    assert fake.files[fid]["name"] == "bar.txt"
    # Cache updated.
    assert client._path_cache.path_to_id("/foo.txt") is None
    assert client._path_cache.path_to_id("/bar.txt") == fid
    # Only one non-ROOT file.
    non_root = [r for r in fake.files.values() if r["id"] != ROOT_ID]
    assert len(non_root) == 1
    assert result.id == fid


def test_move_503_exceeds_max_retries_raises_teeth(cof):
    """Teeth: arm _MAX_RETRY_ATTEMPTS+1 × 503s → _retry_transient
    exhausts all attempts and raises; confirms retries are bounded.
    """
    from googleapiclient.errors import HttpError as _HttpError

    client, fake = cof

    fid = fake.add_file("/foo.txt", b"content")
    client._path_cache.set(
        "/foo.txt", fid, parent_id=ROOT_ID, name="foo.txt", is_folder=False
    )

    fake.fail_next("files.update", make_http_error(503), count=_MAX_RETRY_ATTEMPTS + 1)

    with pytest.raises((BoraApiError, _HttpError)):
        client._writes.move("/foo.txt", "/bar.txt")

    # File name unchanged — move never committed.
    assert fake.files[fid]["name"] == "foo.txt"


def test_make_dir_503_count2_converges(cof):
    """Arm 2 consecutive 503s on files.create → make_dir converges.

    Also arms the conflict-probe files.list to return empty (no pre-existing
    folder) via the fake's default behaviour (no matching files).

    Assert: exactly ONE folder under ROOT with the correct name; cache seeded.
    """
    client, fake = cof

    # Arm 2 transient 503s on files.create (the folder create, not list probe).
    fake.fail_next("files.create", make_http_error(503), count=2)

    result = client._writes.make_dir("/newdir")

    # Exactly one non-ROOT entry, a folder with correct name.
    non_root = [r for r in fake.files.values() if r["id"] != ROOT_ID]
    assert len(non_root) == 1
    assert non_root[0]["name"] == "newdir"
    assert non_root[0]["mimeType"] == "application/vnd.google-apps.folder"
    assert non_root[0]["trashed"] is False

    # Cache updated.
    assert client._path_cache.path_to_id("/newdir") == result.id


def test_make_dir_503_exceeds_max_retries_raises_teeth(cof):
    """Teeth: arm _MAX_RETRY_ATTEMPTS+1 × 503s on files.create → make_dir raises
    after exhausting retries, proving the retry loop is bounded and not infinite.
    """
    from googleapiclient.errors import HttpError as _HttpError

    client, fake = cof

    fake.fail_next("files.create", make_http_error(503), count=_MAX_RETRY_ATTEMPTS + 1)

    with pytest.raises((BoraApiError, _HttpError)):
        client._writes.make_dir("/newdir")

    # No folder was created in the fake.
    non_root = [r for r in fake.files.values() if r["id"] != ROOT_ID]
    assert len(non_root) == 0, (
        f"Expected no folders created after exhausted retries; got {len(non_root)}"
    )
