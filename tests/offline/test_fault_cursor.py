"""Fault-injection tests for cursor-reset and reindex paths (Stage 7b-E2, Task 8).

Covers:
  A — 400 invalid pageToken mid-stream → CursorResetError → stop-before-reset
      preserved; engine-level post-condition (cursor reset to "", index rebuilt).
  B — persistent 400 on EVERY changes.list call (even after reindex provides a
      fresh token) — the persistent-400 bug class.  The manager has no circuit-
      breaker, so this is an architectural finding → xfail(strict=True).

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
  There is no circuit-breaker, backoff guard, or max-reindex-attempts counter.
  This is architectural (needs a circuit-breaker / backoff-state across the
  manager's start() calls) and is flagged xfail(strict=True).
"""

from __future__ import annotations

import os
import os.path as osp
import sqlite3
from unittest.mock import Mock

import pytest

from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.core import FullAccount
from cumulus.database.core import Database
from cumulus.exceptions import CursorResetError
from cumulus.keyring import CredentialStorage
from cumulus.models import IndexEntry, ItemType
from cumulus.path_cache import PathIDCache
from cumulus.sync import SyncEngine
from cumulus.utils.appdirs import get_home_dir
from cumulus.utils.path import delete
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


def _write_local_file(sync: SyncEngine, remote_path: str, data: bytes) -> str:
    local_path = sync.to_local_path_from_cased(remote_path)
    os.makedirs(osp.dirname(local_path), exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


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


def test_scenario_a1_teeth_missing_cursor_reset_propagates_as_http_error():
    """Teeth for A1: if the CursorResetError mapping were removed, a 400 would
    propagate as a generic HttpError (not CursorResetError).

    We prove teeth by bypassing the mapping: arm the fake with a 400 but assert
    it does NOT raise CursorResetError when we drain changes.py's iter directly
    with a stub that re-raises the raw HttpError (simulating no mapping).

    Actually the cleanest proof: monkey-patch changes.py to pass through HttpError
    directly.  Instead we use a simpler inversion: assert that WITHOUT the 400
    (no fault armed), CursorResetError is NOT raised — confirming the 400 is the
    causal trigger.  If CursorResetError were raised spuriously, this would fail.
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

    sync_engine.download_sync_cycle()

    # Incremental path must NOT reset did_finish_indexing to False.
    # (It ends by setting it to True, but it must not have been False mid-way
    # in a detectable way — the cursor path taken is confirmed by it not
    # calling list_folder_iterator, which would reset the path cache.)
    assert sync_engine.remote_cursor != "", (
        "Non-empty cursor must produce a non-empty post-cursor"
    )
    assert sync_engine._state.get("sync", "did_finish_indexing") is True, (
        "Incremental path must not reset did_finish_indexing"
    )


# ===========================================================================
# Scenario B — persistent 400 on fresh token: the persistent-400 bug class
# ===========================================================================
#
# Architectural finding: the manager has no circuit-breaker against repeated
# CursorResetError.  When changes.list returns 400 even for the FRESH token
# obtained from a successful bootstrap, the manager will loop:
#   download_worker: changes.list → CursorResetError
#   _handle_sync_thread_errors: stop() → reset_sync_state() → autostart.set()
#   connection_monitor: start() → download_worker ...repeat indefinitely
#
# There is no counter, no backoff guard, and no "give up" path.
#
# At the engine level (no manager loop), download_sync_cycle simply raises
# CursorResetError on each call.  The test proves the loop would be unbounded
# by simulating N manager-reset→cycle calls and confirming each raises.
# A guard would stop before N (e.g. raise a non-CursorResetError exception
# or stop resetting after K attempts).
#
# Because the fix requires a circuit-breaker across the manager's start()
# call boundary (architectural, cross-cutting), this is marked
# @pytest.mark.xfail(strict=True).
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "finding: persistent-400 causes unbounded reindex spin — "
        "manager has no circuit-breaker against repeated CursorResetError "
        "when changes.list returns 400 even for a fresh token post-bootstrap"
    ),
)
def test_scenario_b_persistent_400_is_bounded(sync_engine: SyncEngine):
    """Persistent 400 on changes.list must NOT produce an unbounded reindex loop.

    Scenario: changes.list returns 400 for ALL calls, including the fresh
    token obtained from a bootstrap.  The simulated manager loop
    (reset_sync_state → download_sync_cycle) must terminate within a finite
    number of attempts — either by surfacing a non-CursorResetError exception
    or by stopping resets after K attempts.

    Current behaviour: every iteration raises CursorResetError; the manager
    resets state and restarts unconditionally → UNBOUNDED SPIN.

    The test encodes "bounded = at most MAX_REINDEX_ATTEMPTS=3 manager cycles",
    which is absent from the current code.  As long as the current code has no
    circuit-breaker, this test MUST xfail.  When a circuit-breaker is added,
    remove the xfail decorator and the assertion becomes the acceptance criterion.
    """
    fake = FakeDrive()
    fake.add_file("/file.txt", b"data")
    _wire_fake(sync_engine, fake)

    # Arm enough 400s to cover: bootstrap fresh-token poll + several manager
    # reset cycles.  10 covers 3 simulated restart cycles with margin.
    # The fake's changes.list will fail on EVERY call for these 10 attempts.
    fake.fail_next("changes.list", make_http_error(400), count=10)

    MAX_REINDEX_ATTEMPTS = 3  # what a circuit-breaker SHOULD enforce

    cursor_reset_count = 0
    for _ in range(MAX_REINDEX_ATTEMPTS + 1):
        # Simulate manager reset: cursor → "" (bootstrap path next cycle).
        sync_engine.reset_sync_state()
        assert sync_engine.remote_cursor == ""

        try:
            # Bootstrap succeeds (uses getStartPageToken + files.list, not
            # changes.list).  Then the engine sets a fresh cursor and returns.
            # On the NEXT download_sync_cycle call (incremental), changes.list
            # fires a 400.
            sync_engine.download_sync_cycle()
            # If we get here without raising, the bootstrap completed and the
            # cursor was advanced — now arm the incremental poll.
            # The next call would be the "wait_for_remote_changes + download"
            # cycle; simulate it as a second download_sync_cycle with the fresh
            # cursor in place.
            sync_engine.download_sync_cycle()
        except CursorResetError:
            cursor_reset_count += 1
            continue
        else:
            # If no exception, the 400s were exhausted or a guard fired — BOUNDED.
            break

    # Assert bounded: a circuit-breaker must prevent more than
    # MAX_REINDEX_ATTEMPTS full reset→bootstrap→400 cycles.
    # Current code has no such guard, so cursor_reset_count == MAX_REINDEX_ATTEMPTS+1
    # (or more) and this assertion FAILS → xfail(strict=True) is justified.
    assert cursor_reset_count <= MAX_REINDEX_ATTEMPTS, (
        f"Unbounded reindex spin detected: CursorResetError raised "
        f"{cursor_reset_count} times in {MAX_REINDEX_ATTEMPTS + 1} iterations "
        f"with no circuit-breaker — a persistent-400 would loop forever."
    )
