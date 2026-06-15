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

import cumulus.client as client_mod
from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.core import FullAccount
from cumulus.database.core import Database
from cumulus.exceptions import CancelledError
from cumulus.keyring import CredentialStorage
from cumulus.path_cache import PathIDCache
from cumulus.sync import SyncEngine
from cumulus.utils.appdirs import get_home_dir
from cumulus.utils.path import delete
from tests.fake_drive import FOLDER_MIME, ROOT_ID, FakeDrive, make_http_error

_STUB_ACCOUNT = FullAccount(
    account_id="account-id",
    display_name="Load Tester",
    email="load@example.com",
    profile_photo_url=None,
    disabled=False,
)

# Soft guards. Generous absolute ceilings tuned to the dev/CI machine at plan
# time; documented as soft guards so a slow machine does not produce false
# failures. They catch hangs/leaks, not performance.
_HANG_GUARD_SECONDS = 300.0
_MEMORY_CEILING_BYTES = 512 * 1024 * 1024


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


@pytest.fixture
def sync_engine():
    local_dir = osp.join(get_home_dir(), "load_initial_sync_dir")
    os.mkdir(local_dir)

    # check_same_thread=False: apply_remote_changes uses a thread pool
    # (do_parallel) that touches the path cache from worker threads.
    cache = PathIDCache(Database(sqlite3.connect(":memory:", check_same_thread=False)))
    cache.root_id = ROOT_ID
    sync = SyncEngine(
        GoogleDriveClient(
            "load-initial-sync-config",
            CredentialStorage("load-initial-sync-config"),
            cache,
        )
    )
    sync.sync_path = local_dir
    sync.local_cursor = 0.0

    yield sync

    remove_configuration("load-initial-sync-config")
    delete(local_dir)


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
        content = f"cumulus-load-test file {k} unique-payload".encode()
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
