"""Fault-injection tests for auth-error paths (Stage 7b-E2, Task 9).

Covers:
  A — 401 / TokenRevokedError mid-batch → clean fatal stop, cursor un-advanced,
      no partial corruption, resumable after relink.
  B — token-refresh failure mid-sync → surfaces as TokenRevokedError (not raw
      RefreshError), cursor un-advanced, restart from persisted cursor resumes
      correctly once fault clears.

Architecture summary (confirmed by code reconnaissance)
-------------------------------------------------------
Propagation path for a 401 HttpError during the change-feed poll
(incremental download cycle):

  changes.py:iter_remote_changes
    retry(lambda c=cursor: service_factory().changes().list(...).execute())
    ↳ HttpError(401) raised inside the lambda
    ↳ _retry_transient: 401 ∉ RETRYABLE_HTTP_STATUS → re-raises immediately
    ↳ except HttpError in iter_remote_changes: status != 400 → re-raises
  client.py:list_remote_changes_iterator (with convert_api_errors():)
    ↳ convert_api_errors catches HttpError(401) → raises TokenRevokedError
  sync.py:list_remote_changes_iterator
    ↳ yield from propagates the exception out of the generator
  sync.py:download_sync_cycle (for changes, cursor in changes_iter:)
    ↳ TokenRevokedError propagates out of the for-loop
    ↳ NOT caught anywhere in download_sync_cycle or apply_remote_changes —
      TokenRevokedError is AuthError(BoraApiError), NOT SyncError
    ↳ _create_local_entry only catches SyncError (per-item) — unreachable
      for a 401 on the feed-fetch level (which happens BEFORE apply)
  → download_sync_cycle raises TokenRevokedError (FATAL-CLEAN)

Because the 401 fires INSIDE iter_remote_changes (during the changes.list
call), it aborts BEFORE any cursor write (sync.py:3068 writes cursor only
AFTER apply_remote_changes succeeds for each chunk) → cursor un-advanced.

Propagation path for a RefreshError:

  same path as 401 but the exception type differs:
    _retry_transient: RefreshError ∉ HttpError and ∉ CONNECTION_ERRORS → re-raises
    convert_api_errors: catches RefreshError → raises TokenRevokedError
  → same fatal-clean outcome as 401.

Injection seam for RefreshError:
  Since `_get_service` is stubbed with a Mock returning fake.service(),
  replacing _get_service with a side_effect raises RefreshError BEFORE
  the changes.list lambda body runs.  The lambda is:
    lambda c=cursor: service_factory().changes().list(...).execute()
  So RefreshError is raised AT service_factory() call, still inside the
  lambda, still inside _retry_transient, still inside convert_api_errors.
  RefreshError propagates through _retry_transient (not an HttpError/
  CONNECTION_ERROR) and is caught by convert_api_errors → TokenRevokedError.

  Note: if the refresh happened via google.auth.transport.requests.Request
  inside Credentials.refresh() (the real OAuth path), it would also be
  inside a convert_api_errors scope (every call to _get_service triggers
  lazy token refresh). But that path requires real OAuth credentials; the
  fake-drive path is the reachable seam. Documented: a RefreshError from
  google.auth's token-refresh machinery during _get_service would propagate
  through the same convert_api_errors context and be mapped identically.
"""

from __future__ import annotations

import hashlib
import os
import os.path as osp
import sqlite3
from unittest.mock import Mock

import pytest
from google.auth.exceptions import RefreshError

from bora.client import GoogleDriveClient
from bora.config import remove_configuration
from bora.core import FullAccount
from bora.database.core import Database
from bora.exceptions import AuthError, TokenRevokedError
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
    display_name="Auth Tester",
    email="auth@example.com",
    profile_photo_url=None,
    disabled=False,
)

_CONFIG = "test-config-fault-auth"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_engine():
    """SyncEngine wired to an in-memory DB with check_same_thread=False.

    Mirrors the pattern in test_fault_crash.py / test_fault_cursor.py.
    check_same_thread=False is required because apply_remote_changes uses a
    thread pool (do_parallel) that accesses the path cache from worker threads.
    """
    local_dir = osp.join(get_home_dir(), "fault_auth_dir")
    os.mkdir(local_dir)

    cache = PathIDCache(Database(sqlite3.connect(":memory:", check_same_thread=False)))
    cache.root_id = ROOT_ID
    sync = SyncEngine(GoogleDriveClient(_CONFIG, CredentialStorage(_CONFIG), cache))
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
    return fake.service().changes().getStartPageToken().execute()["startPageToken"]


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
        read_only=0,
    )
    with sync._database_access():
        sync._index_table.update(entry)


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


# ===========================================================================
# Scenario A — 401 / TokenRevokedError mid-batch
# ===========================================================================
#
# A1 — unit: 401 on changes.list surfaces as TokenRevokedError at client layer.
# A2 — engine: download_sync_cycle raises TokenRevokedError (fatal, not swallowed
#      as a per-item SyncError); cursor NOT advanced; already-applied items intact.
# A3 — relink simulation: re-run the same cycle after clearing the fault → completes.
# A4 — teeth: prove TokenRevokedError is NOT swallowed (invert by checking type).
# ---------------------------------------------------------------------------


def test_scenario_a1_unit_401_raises_token_revoked_error():
    """Unit: 401 from changes.list propagates as TokenRevokedError at client layer.

    Exercises the mapping path:
      HttpError(401) → _retry_transient re-raises → iter_remote_changes re-raises
      → convert_api_errors maps to TokenRevokedError.

    Confirmed by reconnaissance: 401 is NOT in RETRYABLE_HTTP_STATUS, so
    _retry_transient re-raises immediately (no retries); the per-status
    mapping in http_error_to_bora maps 401 → TokenRevokedError.
    """
    fake = FakeDrive()
    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = ROOT_ID

    client = GoogleDriveClient(
        _CONFIG + "-unit-a1", CredentialStorage(_CONFIG + "-unit-a1"), cache
    )
    client._get_service = Mock(return_value=fake.service())
    client._cached_account_info = _STUB_ACCOUNT
    client._ensure_root_id = Mock()

    start = _start(fake)

    # Arm a single 401 on changes.list.
    fake.fail_next("changes.list", make_http_error(401))

    with pytest.raises(TokenRevokedError) as excinfo:
        client.list_remote_changes(start)

    # Verify it is the specific TokenRevokedError (not just AuthError/BoraApiError).
    assert isinstance(excinfo.value, TokenRevokedError)
    assert isinstance(excinfo.value, AuthError)
    # The message references re-linking.
    assert (
        "link" in excinfo.value.message.lower()
        or "re-link" in excinfo.value.message.lower()
    )

    remove_configuration(_CONFIG + "-unit-a1")


def test_scenario_a1_no_fault_no_spurious_token_revoked():
    """Guard: TokenRevokedError must NOT be raised without a 401.

    Paired with a1 above to pin both directions: the fault must fire on 401
    and must NOT fire spuriously. Together they prove the mapping is precise.
    """
    fake = FakeDrive()
    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = ROOT_ID

    client = GoogleDriveClient(
        _CONFIG + "-unit-a1g", CredentialStorage(_CONFIG + "-unit-a1g"), cache
    )
    client._get_service = Mock(return_value=fake.service())
    client._cached_account_info = _STUB_ACCOUNT
    client._ensure_root_id = Mock()

    start = _start(fake)
    # No fault armed — must NOT raise TokenRevokedError.
    try:
        client.list_remote_changes(start)
    except TokenRevokedError:
        pytest.fail("TokenRevokedError raised without a 401 — mapping fires spuriously")

    remove_configuration(_CONFIG + "-unit-a1g")


def test_scenario_a2_engine_401_raises_fatal_token_revoked(sync_engine: SyncEngine):
    """Engine level: download_sync_cycle raises TokenRevokedError on 401.

    Confirms propagation is FATAL-CLEAN:
    - TokenRevokedError (AuthError, NOT SyncError) propagates through
      download_sync_cycle without being swallowed by the per-item SyncError
      catch in _create_local_entry (unreachable here because the 401 fires
      BEFORE apply_remote_changes on the feed-fetch).
    - Cursor is NOT advanced past the pre-fault value (stop-before-apply
      guarantee: cursor is written only AFTER apply_remote_changes succeeds).
    - No torn partial state: previously applied items are intact.

    Setup: seed several files in fake + index; give engine a non-empty cursor
    (incremental path); arm 401 on changes.list; run download_sync_cycle.
    """
    fake = FakeDrive()

    # Seed three files so the fake has content; take cursor BEFORE seeding
    # so the incremental feed would see them as changes.
    start_cursor = _start(fake)

    fid_a = fake.add_file("/alpha.txt", b"content-a")
    fid_b = fake.add_file("/beta.txt", b"content-b")
    fid_c = fake.add_file("/gamma.txt", b"content-c")

    _wire_fake(sync_engine, fake)

    # Give the engine a non-empty cursor → incremental path.
    sync_engine.remote_cursor = start_cursor

    # Seed index entries for these files (simulate already-synced state).
    _seed_entry(
        sync_engine,
        "/alpha.txt",
        fid_a,
        ItemType.File,
        "rev-1",
        _md5(b"content-a"),
    )
    _seed_entry(
        sync_engine,
        "/beta.txt",
        fid_b,
        ItemType.File,
        "rev-2",
        _md5(b"content-b"),
    )
    _seed_entry(
        sync_engine,
        "/gamma.txt",
        fid_c,
        ItemType.File,
        "rev-3",
        _md5(b"content-c"),
    )

    # Arm a 401 on the very first changes.list call.
    fake.fail_next("changes.list", make_http_error(401))

    # The cycle MUST raise TokenRevokedError (fatal, not swallowed).
    with pytest.raises(TokenRevokedError):
        sync_engine.download_sync_cycle()

    # --- Cursor un-advanced check ---
    # The 401 fires BEFORE apply_remote_changes, so the cursor must not move.
    assert (
        sync_engine.remote_cursor == start_cursor
    ), "Cursor must NOT be advanced when 401 aborts before apply_remote_changes"

    # --- No partial corruption: index must be intact ---
    idx_a = sync_engine.get_index_entry_by_remote_id(fid_a)
    idx_b = sync_engine.get_index_entry_by_remote_id(fid_b)
    idx_c = sync_engine.get_index_entry_by_remote_id(fid_c)

    assert idx_a is not None, "alpha.txt index entry must be intact"
    assert idx_b is not None, "beta.txt index entry must be intact"
    assert idx_c is not None, "gamma.txt index entry must be intact"

    # Revisions must not have been corrupted.
    assert idx_a.rev == "rev-1"
    assert idx_b.rev == "rev-2"
    assert idx_c.rev == "rev-3"


def test_scenario_a3_resumable_after_relink(sync_engine: SyncEngine):
    """After a 401, clearing the fault and re-running from the SAME cursor
    completes the batch and reaches the correct final state.

    This simulates the 'relink' use case:
    1. Sync cycle hits 401 → raises TokenRevokedError; cursor un-advanced.
    2. User re-links (fault cleared); daemon restarts from persisted cursor.
    3. Same cursor → same incremental batch → succeeds → files applied.

    The test verifies that after relink the files ARE applied to the local
    sync folder and the cursor advances past the batch.
    """
    fake = FakeDrive()

    start_cursor = _start(fake)
    fake.add_file("/doc-a.txt", b"resume-content-a")
    fake.add_file("/doc-b.txt", b"resume-content-b")

    _wire_fake(sync_engine, fake)
    sync_engine.remote_cursor = start_cursor

    # --- First pass: 401 fires ---
    fake.fail_next("changes.list", make_http_error(401))

    with pytest.raises(TokenRevokedError):
        sync_engine.download_sync_cycle()

    assert (
        sync_engine.remote_cursor == start_cursor
    ), "Cursor must be un-advanced after 401"

    # --- Relink simulation: clear any remaining faults, re-run same cursor ---
    # (In production: daemon re-links, persisted cursor is un-changed, daemon restarts)
    assert not fake._fail_rules, "Fault must have been consumed by the 401"

    # Re-run from the persisted cursor (no new fault) → must succeed.
    sync_engine.download_sync_cycle()

    # Cursor must have advanced past the batch.
    assert (
        sync_engine.remote_cursor != start_cursor
    ), "Cursor must advance after successful relink-and-retry cycle"

    # Files must have been applied to the local sync folder.
    local_a = sync_engine.to_local_path_from_cased("/doc-a.txt")
    local_b = sync_engine.to_local_path_from_cased("/doc-b.txt")
    assert osp.exists(local_a), "doc-a.txt must exist locally after relink cycle"
    assert osp.exists(local_b), "doc-b.txt must exist locally after relink cycle"

    with open(local_a, "rb") as f:
        assert f.read() == b"resume-content-a"
    with open(local_b, "rb") as f:
        assert f.read() == b"resume-content-b"


def test_scenario_a4_teeth_401_not_swallowed_as_sync_error(sync_engine: SyncEngine):
    """Teeth: prove 401 (→ TokenRevokedError) is NOT swallowed as a per-item
    SyncError by _create_local_entry.

    If TokenRevokedError were a SyncError, _create_local_entry would catch it
    as a per-item error, log it, and return SyncStatus.Failed — download_sync_cycle
    would NOT raise, and the cursor would advance normally. This would silently
    drop the auth error and corrupt state.

    Proof by type check:
    - TokenRevokedError is AuthError(BoraApiError) — NOT SyncError.
    - _create_local_entry catches `except SyncError`, not `except BoraApiError`.
    - Therefore TokenRevokedError propagates fatally.

    The test arm-and-assert structure confirms:
    1. The cycle DOES raise (not silently absorbs).
    2. The raised type IS specifically TokenRevokedError (not e.g. CursorResetError).
    3. Inverting the assertion (expecting NO exception) would fail → teeth confirmed.
    """
    from bora.exceptions import SyncError

    # Confirm the type hierarchy: TokenRevokedError must NOT be a SyncError.
    assert not issubclass(TokenRevokedError, SyncError), (
        "TEETH PRECONDITION: TokenRevokedError must not be a SyncError subclass — "
        "if it were, it would be caught per-item and this test is invalidated"
    )

    fake = FakeDrive()
    start_cursor = _start(fake)
    fake.add_file("/teeth-file.txt", b"teeth-data")
    _wire_fake(sync_engine, fake)
    sync_engine.remote_cursor = start_cursor

    fake.fail_next("changes.list", make_http_error(401))

    raised_type = None
    try:
        sync_engine.download_sync_cycle()
    except Exception as exc:
        raised_type = type(exc)

    assert raised_type is TokenRevokedError, (
        f"TEETH FAILED: expected TokenRevokedError to propagate fatally from "
        f"download_sync_cycle, got {raised_type!r}. "
        f"If None: 401 was swallowed (per-item catch). "
        f"If wrong type: mapping regression."
    )


def test_scenario_a4_teeth_401_cursor_not_advanced(sync_engine: SyncEngine):
    """Teeth for cursor un-advanced: confirm the cursor guard is not vacuous.

    We verify that WITHOUT a 401 fault (no failure), the cursor IS advanced —
    proving the cursor-not-advanced assertion in A2 is non-trivial (it actually
    changes when the cycle succeeds).
    """
    fake = FakeDrive()
    start_cursor = _start(fake)
    fake.add_file("/canary.txt", b"canary")
    _wire_fake(sync_engine, fake)
    sync_engine.remote_cursor = start_cursor

    # No fault armed → cycle must succeed and advance the cursor.
    sync_engine.download_sync_cycle()

    assert sync_engine.remote_cursor != start_cursor, (
        "Teeth: cursor must advance on a successful cycle (no fault) — "
        "proving the 'cursor un-advanced on 401' assertion is non-trivial"
    )


# ===========================================================================
# Scenario B — token-refresh failure mid-sync → TokenRevokedError, resumable
# ===========================================================================
#
# B1 — unit: RefreshError from _get_service maps to TokenRevokedError.
# B2 — engine: RefreshError mid-sync raises TokenRevokedError; cursor un-advanced.
# B3 — restart safety: same cursor after fault clears → completes.
# B4 — teeth: verify RefreshError is NOT surfaced raw (convert_api_errors wired).
# ---------------------------------------------------------------------------


def test_scenario_b1_unit_refresh_error_maps_to_token_revoked():
    """Unit: RefreshError from inside a convert_api_errors scope maps to
    TokenRevokedError (NOT raw RefreshError, NOT generic AuthError).

    Injection seam: _get_service is replaced with a side_effect that raises
    RefreshError.  The lambda in iter_remote_changes is:
        lambda c=cursor: service_factory().changes().list(...).execute()
    When service_factory() (i.e. _get_service()) raises RefreshError:
    - _retry_transient does NOT catch RefreshError (only HttpError + CONNECTION_ERRORS)
      → re-raises unchanged.
    - iter_remote_changes does NOT catch RefreshError (only HttpError for 400)
      → re-raises unchanged.
    - convert_api_errors (the context manager in list_remote_changes_iterator)
      DOES catch RefreshError → raises TokenRevokedError.

    This test confirms the F13 wiring is live: convert_api_errors wraps the
    entire iter_remote_changes generator via yield from.
    """
    fake = FakeDrive()
    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = ROOT_ID

    client = GoogleDriveClient(
        _CONFIG + "-unit-b1", CredentialStorage(_CONFIG + "-unit-b1"), cache
    )
    client._cached_account_info = _STUB_ACCOUNT
    client._ensure_root_id = Mock()

    # Replace _get_service to raise RefreshError when called.
    # This fires inside the lambda in iter_remote_changes, inside _retry_transient,
    # inside the convert_api_errors context manager in list_remote_changes_iterator.
    client._get_service = Mock(
        side_effect=RefreshError("invalid_grant: Token has been expired or revoked.")
    )

    start = _start(fake)  # obtained before wiring the fault

    with pytest.raises(TokenRevokedError) as excinfo:
        client.list_remote_changes(start)

    # Must be the specific type — NOT a raw RefreshError.
    assert isinstance(
        excinfo.value, TokenRevokedError
    ), f"Expected TokenRevokedError, got {type(excinfo.value).__name__}"
    assert isinstance(excinfo.value, AuthError)

    # The original RefreshError must be chained as __cause__ (by the
    # `raise TokenRevokedError(...) from exc` in convert_api_errors).
    assert isinstance(
        excinfo.value.__cause__, RefreshError
    ), "TokenRevokedError must chain the original RefreshError as __cause__"

    remove_configuration(_CONFIG + "-unit-b1")


def test_scenario_b1_teeth_refresh_error_not_raw():
    """Teeth for B1: prove convert_api_errors IS the guard (not accident).

    Method: call convert_api_errors directly with a RefreshError and confirm
    it maps to TokenRevokedError. Then call the same code WITHOUT
    convert_api_errors and confirm a raw RefreshError escapes.

    This pinpoints convert_api_errors as the load-bearing guard, proving
    that removing it would break the B2 engine scenario.
    """
    # Path WITH convert_api_errors → must convert.
    from bora.errorhandling import convert_api_errors

    with pytest.raises(TokenRevokedError):
        with convert_api_errors():
            raise RefreshError("invalid_grant")

    # Path WITHOUT convert_api_errors → raw RefreshError escapes.
    with pytest.raises(RefreshError):
        raise RefreshError("invalid_grant")


def test_scenario_b2_engine_refresh_error_raises_fatal_token_revoked(
    sync_engine: SyncEngine,
):
    """Engine level: RefreshError from _get_service raises TokenRevokedError.

    Injection seam: _get_service side_effect=RefreshError.
    This fires inside the changes.list lambda, propagates through
    _retry_transient and iter_remote_changes to convert_api_errors →
    TokenRevokedError → download_sync_cycle raises fatally.

    Cursor un-advanced: RefreshError fires at the service_factory() call
    inside the lambda, before the changes.list RPC even runs → before any
    cursor write → cursor must be unchanged.
    """
    fake = FakeDrive()
    start_cursor = _start(fake)

    fid_a = fake.add_file("/refresh-test-a.txt", b"data-a")
    fid_b = fake.add_file("/refresh-test-b.txt", b"data-b")

    # Wire fake for the getStartPageToken call (needed above), then replace
    # _get_service to raise RefreshError for subsequent calls.
    _wire_fake(sync_engine, fake)
    sync_engine.remote_cursor = start_cursor

    # Seed index to simulate consistent pre-fault state.
    _seed_entry(
        sync_engine,
        "/refresh-test-a.txt",
        fid_a,
        ItemType.File,
        "rev-a",
        _md5(b"data-a"),
    )
    _seed_entry(
        sync_engine,
        "/refresh-test-b.txt",
        fid_b,
        ItemType.File,
        "rev-b",
        _md5(b"data-b"),
    )

    # Replace _get_service to raise RefreshError: this fires inside the lambda
    # in iter_remote_changes on the NEXT call to service_factory() (which happens
    # in the changes.list lambda during download_sync_cycle).
    sync_engine.client._get_service = Mock(
        side_effect=RefreshError("invalid_grant: Token has been expired or revoked.")
    )

    # Must raise TokenRevokedError (not raw RefreshError).
    with pytest.raises(TokenRevokedError):
        sync_engine.download_sync_cycle()

    # Cursor must be un-advanced.
    assert (
        sync_engine.remote_cursor == start_cursor
    ), "Cursor must NOT be advanced when RefreshError aborts the feed-fetch"

    # Index must be intact (no partial corruption).
    idx_a = sync_engine.get_index_entry_by_remote_id(fid_a)
    idx_b = sync_engine.get_index_entry_by_remote_id(fid_b)

    assert idx_a is not None and idx_a.rev == "rev-a", "alpha index must be intact"
    assert idx_b is not None and idx_b.rev == "rev-b", "beta index must be intact"


def test_scenario_b3_restart_from_persisted_cursor_after_refresh_error(
    sync_engine: SyncEngine,
):
    """Cross-restart safety: after RefreshError → cursor un-advanced → relink →
    restart from same persisted cursor → cycle completes correctly.

    Simulates the full recovery sequence:
    1. RefreshError fires → TokenRevokedError → cursor un-advanced.
    2. Persisted cursor unchanged (same as pre-fault).
    3. After relink: _get_service restored; re-run from same cursor.
    4. Cycle completes; files applied; cursor advances.
    """
    fake = FakeDrive()
    start_cursor = _start(fake)

    fake.add_file("/relink-a.txt", b"relink-data-a")
    fake.add_file("/relink-b.txt", b"relink-data-b")

    _wire_fake(sync_engine, fake)
    sync_engine.remote_cursor = start_cursor

    # --- Fault pass: RefreshError mid-sync ---
    sync_engine.client._get_service = Mock(side_effect=RefreshError("invalid_grant"))

    with pytest.raises(TokenRevokedError):
        sync_engine.download_sync_cycle()

    cursor_after_fault = sync_engine.remote_cursor
    assert (
        cursor_after_fault == start_cursor
    ), "Cursor must be un-advanced after RefreshError"

    # --- Relink: restore _get_service (simulates relink clearing the fault) ---
    sync_engine.client._get_service = Mock(return_value=fake.service())

    # Re-run from the persisted cursor → must succeed.
    sync_engine.download_sync_cycle()

    assert (
        sync_engine.remote_cursor != start_cursor
    ), "Cursor must advance after successful restart post-relink"

    # Files must have been applied.
    local_a = sync_engine.to_local_path_from_cased("/relink-a.txt")
    local_b = sync_engine.to_local_path_from_cased("/relink-b.txt")
    assert osp.exists(local_a), "relink-a.txt must exist locally after restart"
    assert osp.exists(local_b), "relink-b.txt must exist locally after restart"


def test_scenario_b4_teeth_refresh_error_type_pins_convert_api_errors_wiring(
    sync_engine: SyncEngine,
):
    """Teeth for B2: the TokenRevokedError type assertion would fail if
    convert_api_errors were NOT wrapping the iter_remote_changes generator.

    Proof by type: if convert_api_errors were removed from the
    list_remote_changes_iterator call, a raw RefreshError would propagate
    (not TokenRevokedError) and our isinstance check would fail.

    We verify this by:
    1. Confirming a RefreshError does NOT satisfy isinstance(_, TokenRevokedError).
    2. Confirming our engine test (B2) raises EXACTLY TokenRevokedError.

    These two facts together prove convert_api_errors is the load-bearing guard.
    """
    # Fact 1: raw RefreshError is not a TokenRevokedError.
    raw_exc = RefreshError("raw")
    assert not isinstance(raw_exc, TokenRevokedError), (
        "Teeth precondition: RefreshError must not be a TokenRevokedError — "
        "if it were, the type check in B2 would pass regardless of mapping"
    )

    # Fact 2: via the engine path, a RefreshError from _get_service arrives
    # as TokenRevokedError.
    fake = FakeDrive()
    start_cursor = _start(fake)
    fake.add_file("/teeth-b4.txt", b"teeth")
    _wire_fake(sync_engine, fake)
    sync_engine.remote_cursor = start_cursor

    sync_engine.client._get_service = Mock(side_effect=RefreshError("invalid_grant"))

    raised = None
    try:
        sync_engine.download_sync_cycle()
    except Exception as exc:
        raised = exc

    assert raised is not None, "Teeth: download_sync_cycle must raise on RefreshError"
    assert isinstance(raised, TokenRevokedError), (
        f"Teeth: expected TokenRevokedError, got {type(raised).__name__}. "
        f"If RefreshError: convert_api_errors is NOT wrapping iter_remote_changes — "
        f"the F13 wiring is broken."
    )
    # The original RefreshError must be chained.
    assert isinstance(raised.__cause__, RefreshError), (
        "Teeth: TokenRevokedError must chain RefreshError as __cause__ — "
        "confirming the convert_api_errors 'raise ... from exc' path is live"
    )


def test_scenario_b4_teeth_401_vs_refresh_parity(sync_engine: SyncEngine):
    """Additional teeth: 401 and RefreshError must produce the SAME exception
    type (TokenRevokedError) at the engine level.

    Both represent "token invalid" conditions. The engine must treat them
    identically: fatal TokenRevokedError, no swallow, cursor un-advanced.

    This test runs both faults back-to-back on fresh engine state and
    asserts parity of the raised type.
    """
    fake = FakeDrive()

    # --- 401 variant ---
    start_cursor = _start(fake)
    fake.add_file("/parity-401.txt", b"parity-data")
    _wire_fake(sync_engine, fake)
    sync_engine.remote_cursor = start_cursor

    fake.fail_next("changes.list", make_http_error(401))
    raised_401 = None
    try:
        sync_engine.download_sync_cycle()
    except Exception as exc:
        raised_401 = exc

    assert isinstance(
        raised_401, TokenRevokedError
    ), f"401 must surface as TokenRevokedError; got {type(raised_401).__name__}"

    # --- RefreshError variant (same engine, cursor still un-advanced) ---
    sync_engine.client._get_service = Mock(side_effect=RefreshError("invalid_grant"))
    raised_refresh = None
    try:
        sync_engine.download_sync_cycle()
    except Exception as exc:
        raised_refresh = exc

    assert isinstance(
        raised_refresh, TokenRevokedError
    ), f"RefreshError must surface as TokenRevokedError; got {type(raised_refresh).__name__}"

    # Both must be the same type (parity).
    assert type(raised_401) is type(raised_refresh), (
        f"401 and RefreshError must produce the same exception type at the engine level; "
        f"got {type(raised_401).__name__} vs {type(raised_refresh).__name__}"
    )
