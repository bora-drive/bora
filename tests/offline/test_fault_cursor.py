"""Fault-injection tests for cursor-reset and reindex paths (Stage 7b-E2, Task 8).

Covers:
  A — 400 invalid pageToken mid-stream → CursorResetError → stop-before-reset
      preserved; engine-level post-condition (cursor reset to "", index rebuilt).
  B — persistent 400 on EVERY changes.list call (even after reindex provides a
      fresh token) — the persistent-400 bug class.  The circuit-breaker for this
      is a manager-level concern (bora.manager.MAX_CURSOR_RESETS, 7c-3); its
      acceptance tests live in tests/offline/test_manager.py.

Architecture summary (confirmed by reconnaissance)
---------------------------------------------------
CursorResetError is raised by:
  • cumulus/changes.py:334 — inside iter_remote_changes when changes.list → 400
  • cumulus/client.py:1238 — inside wait_for_remote_changes when changes.list → 400

It is caught ONLY by:
  • cumulus/manager.py:561 — _handle_sync_thread_errors (a context manager)
    Action: self.stop() → self.sync.reset_sync_state() → self.autostart.set()
    reset_sync_state() clears ALL sync state: index, history, errors, hash table,
    and resets _state to defaults which sets cursor → "" (the config default).

The SyncEngine itself does NOT catch CursorResetError — it propagates up.
download_sync_cycle() takes the incremental path when remote_cursor != "" and
the bootstrap path (list_folder_iterator → _bootstrap_recursive) when == "".

Reindex observable post-condition:
  • remote_cursor == ""  (the reset sentinel)
  • did_finish_indexing == False  (set by download_sync_cycle on cursor=="")
  • A subsequent download_sync_cycle() with remote_cursor=="" performs bootstrap
    (getStartPageToken + files.list), sets a fresh cursor, did_finish_indexing=True.

Stop-before-reset:
  download_sync_cycle() advances remote_cursor ONLY after apply_remote_changes
  succeeds.  A 400 aborts inside iter_remote_changes (before any cursor write)
  so the cursor is never partially advanced — the state is either fully pre-reset
  (old cursor still stored) or fully re-indexed (new cursor stored), never torn.

Persistent-400 finding:
  The manager catches CursorResetError → resets state (cursor to "") → sets
  autostart.  The connection_monitor() then calls start() again.  If changes.list
  ALSO returns 400 with the FRESH token from the next bootstrap, the cycle repeats
  indefinitely: bootstrap → 400 → reset → autostart → bootstrap → 400 → …
  This is bounded by a manager-level circuit-breaker that counts consecutive
  CursorResetError cycles across the manager's start() calls and gives up after
  MAX_CURSOR_RESETS (7c-3); see tests/offline/test_manager.py.
"""

from __future__ import annotations

import os
import os.path as osp
import sqlite3
from unittest.mock import Mock

import pytest

from bora.client import GoogleDriveClient
from bora.config import remove_configuration
from bora.core import FullAccount
from bora.database.core import Database
from bora.exceptions import CursorResetError
from bora.keyring import CredentialStorage
from bora.models import IndexEntry, ItemType
from bora.path_cache import PathIDCache
from bora.sync import SyncEngine
from bora.utils.appdirs import get_home_dir
from bora.utils.path import delete
from tests.fake_drive import ROOT_ID, FakeDrive, make_http_error

# ---------------------------------------------------------------------------
# Shared constants / stubs
# ---------------------------------------------------------------------------

_STUB_ACCOUNT = FullAccount(
    account_id="account-id",
    display_name="Cursor Tester",
    email="cursor@example.com",
    profile_photo_url=None,
    disabled=False,
)

_CONFIG = "test-config-fault-cursor"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_engine():
    """SyncEngine wired to an in-memory DB with check_same_thread=False.

    Mirrors the pattern in test_fault_crash.py.
    """
    local_dir = osp.join(get_home_dir(), "fault_cursor_dir")
    os.mkdir(local_dir)

    cache = PathIDCache(
        Database(sqlite3.connect(":memory:", check_same_thread=False))
    )
    cache.root_id = ROOT_ID
    sync = SyncEngine(
        GoogleDriveClient(_CONFIG, CredentialStorage(_CONFIG), cache)
    )
    sync.sync_path = local_dir
    sync.local_cursor = 0.0

    yield sync

    remove_configuration(_CONFIG)
    delete(local_dir)


def _wire_fake(sync: SyncEngine, fake: FakeDrive) -> None:
    """Point the client at *fake* and stub the account-info cache."""
    sync.client._get_service = Mock(return_value=fake.service())
    sync.client._cached_account_info = _STUB_ACCOUNT


def _start(fake: FakeDrive) -> str:
    """Return the current startPageToken from the fake."""
    return (
        fake.service().changes().getStartPageToken().execute()["startPageToken"]
    )


def _seed_entry(
    sync: SyncEngine,
    remote_path: str,
    remote_id: str,
    item_type: ItemType,
    rev: str,
    content_hash: str | None,
    last_sync: float = 0.0,
) -> None:
    entry = IndexEntry(
        remote_path_cased=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=remote_id,
        item_type=item_type,
        last_sync=last_sync,
        rev=rev,
        content_hash=content_hash,
        symlink_target=None,
    )
    with sync._database_access():
        sync._index_table.update(entry)


# ===========================================================================
# Scenario A — 400 invalid pageToken mid-stream
# ===========================================================================
#
# Three sub-tests:
#   A1 — unit: client.list_remote_changes_iterator raises CursorResetError
#   A2 — engine: download_sync_cycle raises CursorResetError; stop-before-reset
#        is preserved (cursor NOT advanced to a stale/partial value)
#   A3 — teeth: neuter the CursorResetError raise in iter_remote_changes by
#        patching changes.list to raise via convert_api_errors path instead.
#        Actually: teeth is provided by A2 itself — see docstring.
# ---------------------------------------------------------------------------


def test_scenario_a1_unit_400_raises_cursor_reset_error():
    """Unit: 400 from changes.list propagates as CursorResetError.

    The fake's changes.list is armed to raise once.  client.list_remote_changes
    wraps list_remote_changes_iterator.  The 400 must surface as CursorResetError
    (not BadInputError / HttpError) — this is the F5 mapping confirmed at the
    client layer, separate from the changes.py-level test in test_changes.py.

    Test level: client (below the engine), confirming the client wiring is live.
    """
    fake = FakeDrive()
    cache = PathIDCache(
        Database(sqlite3.connect(":memory:"))
    )
    cache.root_id = ROOT_ID

    client = GoogleDriveClient(_CONFIG + "-unit", CredentialStorage(_CONFIG + "-unit"), cache)
    client._get_service = Mock(return_value=fake.service())
    client._cached_account_info = _STUB_ACCOUNT
    client._ensure_root_id = Mock()

    # Obtain a valid cursor so we take the incremental (non-bootstrap) path.
    start = _start(fake)

    # Arm a single 400 on changes.list.
    fake.fail_next("changes.list", make_http_error(400))

    with pytest.raises(CursorResetError):
        # list_remote_changes exhausts list_remote_changes_iterator internally.
        client.list_remote_changes(start)

    remove_configuration(_CONFIG + "-unit")


def test_scenario_a1_no_fault_no_spurious_cursor_reset():
    """Guard: CursorResetError must NOT be raised when no 400 fault is armed.

    This is a necessary (not sufficient) condition for the A1 mapping.  The
    sufficient condition — that a 400 DOES raise CursorResetError — is covered
    by ``test_scenario_a1_unit_400_raises_cursor_reset_error`` above, which is
    the true teeth for the F5 mapping.

    If CursorResetError were raised spuriously (e.g. on every call regardless
    of the status code), that GREEN test would still pass but this guard would
    fail, exposing a false positive in the GREEN test.  Together the pair pins
    both directions of the mapping.
    """
    fake = FakeDrive()
    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = ROOT_ID

    client = GoogleDriveClient(
        _CONFIG + "-teeth-a1", CredentialStorage(_CONFIG + "-teeth-a1"), cache
    )
    client._get_service = Mock(return_value=fake.service())
    client._cached_account_info = _STUB_ACCOUNT
    client._ensure_root_id = Mock()

    start = _start(fake)
    # No fault armed — must NOT raise CursorResetError.
    try:
        client.list_remote_changes(start)
    except CursorResetError:
        pytest.fail(
            "CursorResetError raised without a 400 — mapping fires spuriously"
        )

    remove_configuration(_CONFIG + "-teeth-a1")


def test_scenario_a2_engine_400_raises_cursor_reset_stop_before_reset(
    sync_engine: SyncEngine,
):
    """Engine level: download_sync_cycle raises CursorResetError on 400.

    Stop-before-reset is verified: the remote_cursor must NOT be advanced
    beyond the last-good value when the 400 fires.  The engine only writes the
    cursor AFTER apply_remote_changes succeeds (sync.py ~3068); a 400 inside
    iter_remote_changes aborts before any cursor write, so the stored cursor
    must be exactly the value we set before arming the fault.

    Setup: give the engine a non-empty cursor (so it takes the incremental
    path, not bootstrap), seed an index row, then arm a 400.
    """
    fake = FakeDrive()

    # Seed a file so the fake has something to list if the incremental feed
    # ever succeeds.
    file_id = fake.add_file("/existing.txt", b"hello")
    _wire_fake(sync_engine, fake)

    # Advance the fake's cursor so the engine's cursor matches a real token.
    start_cursor = _start(fake)

    # Give the engine a non-empty cursor → incremental path.
    sync_engine.remote_cursor = start_cursor
    assert sync_engine.remote_cursor == start_cursor, (
        "Precondition: cursor must be non-empty to take incremental path"
    )

    # Seed a matching index entry so the engine looks consistent.
    _seed_entry(
        sync_engine,
        remote_path="/existing.txt",
        remote_id=file_id,
        item_type=ItemType.File,
        rev="rev-1",
        content_hash=None,
    )

    # Arm one 400 on changes.list — fires when the incremental feed polls.
    fake.fail_next("changes.list", make_http_error(400))

    # download_sync_cycle must raise CursorResetError (not absorb it).
    with pytest.raises(CursorResetError):
        sync_engine.download_sync_cycle()

    # --- Stop-before-reset check ---
    # The engine must NOT have advanced the cursor past start_cursor.
    # A torn state would be: cursor advanced to some mid-batch value while
    # changes were only partially applied.
    assert sync_engine.remote_cursor == start_cursor, (
        "Stop-before-reset violated: cursor was advanced despite 400 abort"
    )


def test_scenario_a2_teeth_cursor_not_reset_by_engine(sync_engine: SyncEngine):
    """Teeth for A2: CursorResetError must propagate up; the engine must NOT
    swallow it and must NOT reset the cursor itself (reset is the manager's job).

    Proof: arm 400, run cycle, verify the cursor is still the pre-fault value.
    If the engine were to swallow CursorResetError and reset the cursor, the
    assert in A2 would pass for the wrong reason.  Here we additionally confirm
    that NO exception being raised would be a failure (i.e. the fault must fire).
    """
    fake = FakeDrive()
    _wire_fake(sync_engine, fake)

    # Give the engine a well-known cursor.
    sentinel_cursor = "sentinel-9999"
    sync_engine.remote_cursor = sentinel_cursor

    fake.fail_next("changes.list", make_http_error(400))

    raised = False
    try:
        sync_engine.download_sync_cycle()
    except CursorResetError:
        raised = True

    assert raised, (
        "Teeth failed: download_sync_cycle did not raise CursorResetError — "
        "the fault did not fire or the exception was swallowed"
    )
    # Cursor must still be the sentinel — the engine does NOT reset it.
    assert sync_engine.remote_cursor == sentinel_cursor, (
        "Teeth: engine must NOT reset the cursor — that is the manager's job"
    )


def test_scenario_a3_reindex_post_condition_after_manager_reset(
    sync_engine: SyncEngine,
):
    """Engine level: after manager-style reset (cursor→""), next
    download_sync_cycle takes the bootstrap path and completes successfully.

    This verifies the full reindex post-condition:
      1. cursor reset to ""
      2. did_finish_indexing reset to False
      3. bootstrap (list_folder_iterator) enumerates the Drive
      4. did_finish_indexing → True, cursor → a non-empty token

    This is what _handle_sync_thread_errors achieves by calling reset_sync_state()
    followed by autostart, which triggers download_sync_cycle() via start().
    We simulate it inline without the manager thread machinery.
    """
    fake = FakeDrive()

    # Seed some files in the fake so bootstrap has something to enumerate.
    fake.add_file("/doc.txt", b"content")
    fake.add_file("/img.png", b"pixels")

    _wire_fake(sync_engine, fake)

    # Simulate manager reset: reset all sync state (cursor → "", index cleared).
    sync_engine.reset_sync_state()

    assert sync_engine.remote_cursor == "", (
        "Precondition: reset_sync_state must clear cursor to ''"
    )
    assert sync_engine._state.get("sync", "did_finish_indexing") is False, (
        "Precondition: did_finish_indexing must be False after reset"
    )

    # Now run the bootstrap cycle — must NOT raise.
    sync_engine.download_sync_cycle()

    # Post-condition: cursor is a non-empty token (fresh from getStartPageToken).
    assert sync_engine.remote_cursor != "", (
        "Bootstrap must store a fresh non-empty cursor"
    )
    # did_finish_indexing must be True after a complete bootstrap.
    assert sync_engine._state.get("sync", "did_finish_indexing") is True, (
        "Bootstrap must set did_finish_indexing=True on completion"
    )


def test_scenario_a3_teeth_bootstrap_requires_empty_cursor(sync_engine: SyncEngine):
    """Teeth for A3: confirm the bootstrap path is taken ONLY when cursor is "".

    We set cursor to a non-empty value, run download_sync_cycle successfully,
    and assert it took the incremental (not bootstrap) path by checking that
    did_finish_indexing was NOT reset to False mid-cycle.  If the engine
    always took the bootstrap path regardless, this would fail.
    """
    fake = FakeDrive()
    fake.add_file("/canary.txt", b"canary")
    _wire_fake(sync_engine, fake)

    # Give a non-empty cursor so the incremental path is taken.
    start = _start(fake)
    sync_engine.remote_cursor = start

    # Pre-set did_finish_indexing=True to detect if bootstrap resets it.
    sync_engine._state.set("sync", "did_finish_indexing", True)

    # Spy: list_folder_iterator is called ONLY on the bootstrap path.
    # If the incremental path is taken it must never be called.
    lfi_calls: list[object] = []
    real_lfi = sync_engine.client.list_folder_iterator
    sync_engine.client.list_folder_iterator = (  # type: ignore[method-assign]
        lambda *a, **kw: (lfi_calls.append((a, kw)), real_lfi(*a, **kw))[-1]
    )

    sync_engine.download_sync_cycle()

    # Incremental path: list_folder_iterator must NOT be called, and the
    # post-conditions (non-empty cursor + did_finish_indexing=True) must hold.
    assert lfi_calls == [], (
        "Incremental path must not call list_folder_iterator"
    )
    assert sync_engine.remote_cursor != "", (
        "Non-empty cursor must produce a non-empty post-cursor"
    )
    assert sync_engine._state.get("sync", "did_finish_indexing") is True, (
        "Incremental path must not reset did_finish_indexing"
    )


# ===========================================================================
# Scenario B — persistent 400 on a fresh token (persistent-400 spin)
# ===========================================================================
#
# The engine-level xfail test that used to live here has been removed.  The
# persistent-400 circuit-breaker is a manager-level concern (it spans the
# manager's start()/reset/autostart loop, not a single engine cycle), so its
# acceptance criteria now live as real, passing tests in
# tests/offline/test_manager.py:
#   - test_persistent_cursor_reset_trips_circuit_breaker
#   - test_cursor_reset_counter_resets_on_healthy_cycle
# See bora.manager.MAX_CURSOR_RESETS (7c-3).
# ===========================================================================
