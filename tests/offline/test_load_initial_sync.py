"""Load / correctness-at-scale test for the initial download sync (Stage 7b-E3).

Drives SyncEngine.download_sync_cycle through a full initial index of a
~10,000-entry fake-Drive tree and proves the end state is complete and
uncorrupted — under a rate-limit storm (S1) and across a crash/resume (S2).

Two slow scenarios are marked @pytest.mark.load and excluded from the normal
offline run (run them with `-m load`). Tiny non-slow S0 guards run the SAME
helpers at N~=150 so harness/assertion regressions surface in the green loop.

Sleep neutralisation: _retry_transient backs off via _poll_event.wait().
The fixture replaces client._poll_event with a Mock whose wait/is_set return
False ("not cancelled, not shut down") so backoff is instant and deterministic
(mirrors tests/offline/test_fault_ratelimit.py).
"""

from __future__ import annotations

import hashlib
import os
import os.path as osp
import sqlite3
import time
import tracemalloc
from unittest.mock import Mock

import pytest

import bora.client as client_mod
from bora.client import GoogleDriveClient
from bora.config import remove_configuration
from bora.core import FullAccount
from bora.database.core import Database
from bora.exceptions import CancelledError
from bora.keyring import CredentialStorage
from bora.path_cache import PathIDCache
from bora.sync import SyncEngine
from bora.utils.appdirs import get_home_dir
from bora.utils.path import delete
from tests.fake_drive import FOLDER_MIME, ROOT_ID, FakeDrive, make_http_error

_STUB_ACCOUNT = FullAccount(
    account_id="account-id",
    display_name="Load Tester",
    email="load@example.com",
    profile_photo_url=None,
    disabled=False,
)

# Soft guards. Generous absolute ceilings documented as soft guards so a slow
# machine does not produce false failures. They catch hangs/leaks, NOT
# performance (the hard gate is correctness). Calibrated against a real, fully
# correct S1 run on this machine that took ~650s wall-clock — note the elapsed
# time is measured with tracemalloc tracing active, which inflates it well
# beyond the untraced runtime. The hang guard is set to ~3x that observation so
# a genuine deadlock still trips it while a slow-but-correct run does not.
_HANG_GUARD_SECONDS = 1800.0
_MEMORY_CEILING_BYTES = 512 * 1024 * 1024


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


# These slow tests use a FIXED sync dir + config name (not tmp_path) because the
# crash-resume scenarios reopen the same persistent index DB across SyncEngine
# instances. That makes them vulnerable to contamination: a run killed before
# teardown (e.g. a slow test aborted mid-flight, or run as a background job that
# is terminated) leaves both this dir AND a partial index/state DB under
# get_data_path("bora", "<config>*"), which the next run inherits ->
# FileExistsError at os.mkdir, or a wrong index_count from stale index rows.
_CONFIG_NAME = "load-initial-sync-config"
_LOCAL_DIR_NAME = "load_initial_sync_dir"


def _purge_load_state(local_dir: str) -> None:
    """Remove this fixture's sync dir + all config/state/index DB files.

    Safe to call when nothing exists (``delete`` returns rather than raises;
    ``remove_configuration`` only walks an already-created data dir). Used both
    defensively at setup and in teardown so a killed prior run cannot
    contaminate the next.
    """
    delete(local_dir)
    remove_configuration(_CONFIG_NAME)


@pytest.fixture
def sync_engine():
    local_dir = osp.join(get_home_dir(), _LOCAL_DIR_NAME)
    # Defensive pre-clean: purge any leftovers from a prior run that was killed
    # before its teardown (see the module note above). get_data_path("bora")
    # is created on demand, so remove_configuration's listdir is safe here.
    _purge_load_state(local_dir)
    os.mkdir(local_dir)

    # check_same_thread=False: apply_remote_changes uses a thread pool
    # (do_parallel) that touches the path cache from worker threads.
    cache = PathIDCache(Database(sqlite3.connect(":memory:", check_same_thread=False)))
    cache.root_id = ROOT_ID
    sync = SyncEngine(
        GoogleDriveClient(
            _CONFIG_NAME,
            CredentialStorage(_CONFIG_NAME),
            cache,
        )
    )
    sync.sync_path = local_dir
    sync.local_cursor = 0.0

    yield sync

    _purge_load_state(local_dir)


def _wire_fake(sync: SyncEngine, fake: FakeDrive) -> None:
    """Wire the fake service + neutralise the retry-backoff sleep."""
    sync.client._get_service = Mock(return_value=fake.service())
    sync.client._cached_account_info = _STUB_ACCOUNT
    # Instant, deterministic backoff: wait()->False (not cancelled),
    # is_set()->False (not shut down).
    sync.client._poll_event = Mock()
    sync.client._poll_event.wait = Mock(return_value=False)
    sync.client._poll_event.is_set = Mock(return_value=False)


def build_tree(
    fake: FakeDrive, n_folders: int, n_files: int, branching: int = 16
) -> tuple[set[str], dict[str, str]]:
    """Bulk-seed the fake-Drive into a nested tree, O(N).

    Folders form a balanced tree of fan-out *branching* (bounded depth). Files
    are distributed round-robin across every folder (and the root). Each file
    gets DISTINCT content so md5s differ and per-file integrity is checkable.

    Returns (expected_folder_paths, {file_path: md5}).
    """
    id_by_index = {0: ROOT_ID}
    path_by_index = {0: ""}  # root path "" -> children compose to "/name"
    expected_folders: set[str] = set()

    for f in range(1, n_folders + 1):
        parent_idx = (f - 1) // branching
        parent_id = id_by_index[parent_idx]
        parent_path = path_by_index[parent_idx]
        name = f"folder-{f}"
        path = f"{parent_path}/{name}"
        fid = fake._insert(name, parent_id, FOLDER_MIME, b"")
        id_by_index[f] = fid
        path_by_index[f] = path
        expected_folders.add(path)

    expected_files: dict[str, str] = {}
    folder_indices = list(id_by_index.keys())  # includes root index 0
    for k in range(n_files):
        fidx = folder_indices[k % len(folder_indices)]
        parent_id = id_by_index[fidx]
        parent_path = path_by_index[fidx]
        name = f"file-{k}.txt"
        path = f"{parent_path}/{name}"
        content = f"bora-load-test file {k} unique-payload".encode()
        fake._insert(name, parent_id, "text/plain", content)
        expected_files[path] = _md5(content)

    return expected_folders, expected_files


def _assert_complete_index(
    sync: SyncEngine,
    expected_folders: set[str],
    expected_files: dict[str, str],
) -> None:
    """Assert the index + on-disk state is complete and uncorrupted."""
    # 1) Exactly one index row per remote_id; total == folders + files.
    remote_ids: list[str] = []
    for entry in sync.iter_index():
        remote_ids.append(entry.remote_id)
    assert len(remote_ids) == len(set(remote_ids)), "duplicate remote_id index rows"
    assert sync.index_count() == len(expected_folders) + len(expected_files), (
        f"index_count {sync.index_count()} != "
        f"{len(expected_folders) + len(expected_files)} expected"
    )

    # 2) Every folder present in the index and on disk.
    for path in expected_folders:
        assert sync.get_index_entry(path.lower()) is not None, f"missing folder row {path}"
        assert osp.isdir(sync.to_local_path_from_cased(path)), f"missing folder dir {path}"

    # 3) Every file present in the index, on disk, with bytes matching its md5.
    for path, expected_md5 in expected_files.items():
        entry = sync.get_index_entry(path.lower())
        assert entry is not None, f"missing file row {path}"
        local = sync.to_local_path_from_cased(path)
        assert osp.isfile(local), f"missing file on disk {path}"
        with open(local, "rb") as fh:
            assert _md5(fh.read()) == expected_md5, f"corrupt content {path}"

    # 4) Indexing finished and the cursor advanced off the empty sentinel.
    assert sync._state.get("sync", "did_finish_indexing") is True
    assert sync._state.get("sync", "indexing_counter") == 0
    assert sync.remote_cursor != "", "cursor must advance to the real start-token"
    assert sync.sync_errors == []


def test_build_tree_shape(sync_engine: SyncEngine):
    """A clean initial sync of a small tree indexes every entry correctly.

    Guards build_tree + _assert_complete_index in the normal green loop.
    """
    fake = FakeDrive()
    expected_folders, expected_files = build_tree(fake, n_folders=20, n_files=80)
    _wire_fake(sync_engine, fake)

    sync_engine.download_sync_cycle()

    _assert_complete_index(sync_engine, expected_folders, expected_files)


# ---------------------------------------------------------------------------
# Scenario S1 — initial sync under a rate-limit storm
# ---------------------------------------------------------------------------


def _run_storm(
    sync: SyncEngine,
    fake: FakeDrive,
    expected_folders: set[str],
    expected_files: dict[str, str],
    *,
    list_every: int = 5,
    media_every: int = 7,
) -> None:
    """Arm a recurring 429/403 storm on list + download, run the full initial
    sync, and assert the hard correctness gate plus that the storm fired."""
    list_rule = fake.fail_every("files.list", list_every, make_http_error(429))
    media_rule = fake.fail_every(
        "files.get_media", media_every, make_http_error(403, "userRateLimitExceeded")
    )

    _wire_fake(sync, fake)
    sync.download_sync_cycle()

    _assert_complete_index(sync, expected_folders, expected_files)

    # The storm must actually have hit the backoff path.
    total_fires = list_rule["fires"] + media_rule["fires"]
    assert total_fires > 0, "storm never fired — backoff path was not exercised"
    # And the neutralised backoff sleep was invoked at least once per fire.
    assert sync.client._poll_event.wait.call_count >= total_fires, (
        "at least one backoff wait() per injected fault"
    )


def test_s0_storm_small(sync_engine: SyncEngine):
    """Non-slow S0 guard for the storm scenario (N~=150)."""
    fake = FakeDrive()
    expected_folders, expected_files = build_tree(fake, n_folders=30, n_files=120)
    _run_storm(sync_engine, fake, expected_folders, expected_files)


@pytest.mark.load
def test_s1_storm_load(sync_engine: SyncEngine):
    """S1 (slow, N=10k): full initial sync under a rate-limit storm completes
    correctly. Soft guards: hang-guard wall-clock + tracemalloc peak ceiling."""
    fake = FakeDrive()
    expected_folders, expected_files = build_tree(
        fake, n_folders=2000, n_files=8000
    )

    tracemalloc.start()
    start = time.monotonic()
    _run_storm(sync_engine, fake, expected_folders, expected_files)
    elapsed = time.monotonic() - start
    # _peak spans the whole run including _assert_complete_index, which md5-reads
    # every file — so it is the harness+engine peak, a conservative leak guard,
    # not a pure engine-memory figure.
    _peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    # Soft guards: catch hangs/leaks, not performance.
    assert elapsed < _HANG_GUARD_SECONDS, (
        f"initial sync took {elapsed:.1f}s (> {_HANG_GUARD_SECONDS}s hang guard)"
    )
    assert _peak < _MEMORY_CEILING_BYTES, (
        f"peak python allocation {_peak / 1e6:.0f}MB exceeded the "
        f"{_MEMORY_CEILING_BYTES / 1e6:.0f}MB ceiling — possible O(N) leak"
    )


# ---------------------------------------------------------------------------
# Scenario S2 — crash / resume idempotent re-index
# ---------------------------------------------------------------------------


def _run_crash_resume(
    sync: SyncEngine,
    fake: FakeDrive,
    expected_folders: set[str],
    expected_files: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_size: int,
    crash_after_chunks: int,
) -> None:
    """Interrupt the initial index after K applied chunks (on a non-final page,
    so cursor is still ""), then resume and assert idempotent completion."""
    # Lower the bootstrap page size so the interruption lands mid-listing.
    monkeypatch.setattr(client_mod, "_LIST_PAGE_SIZE", page_size)

    _wire_fake(sync, fake)

    # Interrupt after `crash_after_chunks` applied chunks via _cancel_requested.
    real_apply = sync.apply_remote_changes
    state = {"chunks": 0}

    def counting_apply(events):
        result = real_apply(events)
        state["chunks"] += 1
        if state["chunks"] == crash_after_chunks:
            sync._cancel_requested.set()
        return result

    monkeypatch.setattr(sync, "apply_remote_changes", counting_apply)

    with pytest.raises(CancelledError):
        sync.download_sync_cycle()

    # --- Partial state: cursor still "", indexing unfinished. ---
    assert sync.remote_cursor == "", "mid-index crash must NOT advance the cursor"
    assert sync._state.get("sync", "did_finish_indexing") is False
    partial_ids = [e.remote_id for e in sync.iter_index()]
    assert len(partial_ids) == len(set(partial_ids)), "duplicate rows after crash"
    assert sync.index_count() > 0, "some entries must have been applied before crash"
    assert sync.index_count() < len(expected_folders) + len(expected_files), (
        "crash must leave a PARTIAL index"
    )
    # Files applied so far must already be intact on disk. Look up by the
    # cased path (the key-space expected_files uses) so the integrity check
    # cannot silently skip an applied file under a casing mismatch.
    for entry in sync.iter_index():
        path = entry.remote_path_cased
        expected_md5 = expected_files.get(path)
        if expected_md5 is not None:
            local = sync.to_local_path_from_cased(path)
            assert osp.isfile(local)
            with open(local, "rb") as fh:
                assert _md5(fh.read()) == expected_md5

    # --- Resume: restore the real apply, clear cancel, re-enter the cycle. ---
    monkeypatch.setattr(sync, "apply_remote_changes", real_apply)
    sync._cancel_requested.clear()
    _wire_fake(sync, fake)
    sync.download_sync_cycle()

    # --- Final state: complete, correct, no duplication, no corruption. ---
    _assert_complete_index(sync, expected_folders, expected_files)


def test_s0_crash_resume_small(sync_engine: SyncEngine, monkeypatch):
    """Non-slow S0 guard for crash/resume (N~=150, page_size 50 -> 3 pages)."""
    fake = FakeDrive()
    expected_folders, expected_files = build_tree(fake, n_folders=30, n_files=120)
    _run_crash_resume(
        sync_engine, fake, expected_folders, expected_files, monkeypatch,
        page_size=50, crash_after_chunks=1,
    )


@pytest.mark.load
def test_s2_crash_resume_load(sync_engine: SyncEngine, monkeypatch):
    """S2 (slow, N=10k): crash mid-index, then resume; the re-index is
    idempotent and the final state is complete and uncorrupted. With page_size
    1000 the 10k tree is ~10 pages; crash after 3 leaves a non-final partial."""
    fake = FakeDrive()
    expected_folders, expected_files = build_tree(
        fake, n_folders=2000, n_files=8000
    )

    tracemalloc.start()
    start = time.monotonic()
    _run_crash_resume(
        sync_engine, fake, expected_folders, expected_files, monkeypatch,
        page_size=1000, crash_after_chunks=3,
    )
    elapsed = time.monotonic() - start
    # _peak spans the whole run including _assert_complete_index, which md5-reads
    # every file — so it is the harness+engine peak, a conservative leak guard,
    # not a pure engine-memory figure.
    _peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert elapsed < _HANG_GUARD_SECONDS, (
        f"crash/resume sync took {elapsed:.1f}s (> {_HANG_GUARD_SECONDS}s hang guard)"
    )
    assert _peak < _MEMORY_CEILING_BYTES, (
        f"peak python allocation {_peak / 1e6:.0f}MB exceeded the "
        f"{_MEMORY_CEILING_BYTES / 1e6:.0f}MB ceiling — possible O(N) leak"
    )
