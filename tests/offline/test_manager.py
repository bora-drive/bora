import os
import os.path as osp
from unittest.mock import Mock

import pytest

from bora import notify, oauth
from bora.exceptions import CursorResetError, NoSyncDirError
from bora.main import Bora
from bora.utils.appdirs import get_home_dir

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_linked(m: Bora) -> None:
    """Store a valid (offline) refresh-token blob so that m.client.linked is
    True without hitting a real OAuth server."""
    creds = oauth.credentials_from_refresh_token("rt-offline-test")
    oauth.save_credentials(m.cred_storage, creds, "perm-test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pending_flags_on_fresh_instance(m: Bora) -> None:
    """A freshly constructed Bora instance has no linked account and no
    sync folder configured, so both pending_link and pending_sync_folder
    must be True."""
    assert m.pending_link is True
    assert m.pending_sync_folder is True


def test_create_sync_directory_creates_dir_and_clears_pending_flag(
    m: Bora, tmp_path
) -> None:
    """create_sync_directory should create the local directory on disk and
    update sync_path so that pending_sync_folder becomes False."""
    _fake_linked(m)
    assert m.pending_link is False, "linking precondition failed"

    target = str(tmp_path / "MyDrive")
    m.create_sync_directory(target)

    assert osp.isdir(target), "directory was not created on disk"
    assert m.sync.sync_path == osp.realpath(target)
    assert m.pending_sync_folder is False


def test_ensure_sync_folder_present_raises_when_dir_missing(
    m: Bora,
) -> None:
    """ensure_sync_folder_present must raise NoSyncDirError when the
    configured sync directory does not exist on disk."""
    absent = osp.join(get_home_dir(), "does_not_exist_xyz_bora_test")
    m.sync.sync_path = absent  # bypass _check_linked; set directly

    with pytest.raises(NoSyncDirError):
        m.sync.ensure_sync_folder_present()


def test_ensure_sync_folder_present_passes_when_dir_exists(m: Bora, tmp_path) -> None:
    """ensure_sync_folder_present must not raise when the sync folder is
    present on disk."""
    local_dir = str(tmp_path / "SyncRoot")
    os.makedirs(local_dir)
    m.sync.sync_path = local_dir  # bypass _check_linked; set directly

    # Should complete without raising.
    m.sync.ensure_sync_folder_present()


def test_cursor_reset_triggers_reindex_and_autostart(m: Bora, tmp_path) -> None:
    """CursorResetError from the download worker must reset sync state and
    schedule a restart, not stop permanently (review F5)."""
    local_dir = str(tmp_path / "SyncRoot")
    os.makedirs(local_dir)
    m.sync.sync_path = local_dir

    # Pre-seed a non-empty cursor so we can observe it being cleared.
    m.sync.remote_cursor = "some-token"

    manager = m.manager
    running = manager.running
    autostart = manager.autostart

    # Attach a mock desktop notifier so the notification code path actually
    # executes (a None notifier would silently skip it).
    manager.desktop_notifier = Mock()

    with manager._handle_sync_thread_errors(running, autostart):
        raise CursorResetError("Sync cursor reset", "token expired")

    assert m.sync.remote_cursor == ""  # full re-index scheduled
    assert manager.autostart.is_set()  # sync will resume
    manager.desktop_notifier.notify.assert_called_once()
    assert manager.desktop_notifier.notify.call_args.kwargs["level"] == notify.INFO


def test_persistent_cursor_reset_trips_circuit_breaker(m: Bora, tmp_path) -> None:
    """7c-3: repeated CursorResetError must NOT loop forever. After
    MAX_CURSOR_RESETS consecutive resets the manager gives up: it stops without
    re-arming autostart and raises a visible (ERROR-level) notification."""
    local_dir = str(tmp_path / "SyncRoot")
    os.makedirs(local_dir)
    m.sync.sync_path = local_dir

    manager = m.manager
    running = manager.running
    autostart = manager.autostart
    manager.desktop_notifier = Mock()

    from bora.manager import MAX_CURSOR_RESETS

    # The first MAX_CURSOR_RESETS resets schedule a restart (autostart set).
    for _ in range(MAX_CURSOR_RESETS):
        manager.autostart.clear()
        with manager._handle_sync_thread_errors(running, autostart):
            raise CursorResetError("Sync cursor reset", "token expired")
        assert manager.autostart.is_set(), "reset within budget must re-arm autostart"

    # The next reset trips the breaker: give up, no autostart.
    manager.autostart.clear()
    with manager._handle_sync_thread_errors(running, autostart):
        raise CursorResetError("Sync cursor reset", "token expired")

    assert (
        not manager.autostart.is_set()
    ), "circuit-breaker must NOT re-arm autostart after the budget is exhausted"
    error_calls = [
        c
        for c in manager.desktop_notifier.notify.call_args_list
        if c.kwargs.get("level") == notify.ERROR
    ]
    assert error_calls, "breaker must surface a visible ERROR notification"


def test_cursor_reset_counter_resets_on_healthy_cycle(m: Bora, tmp_path) -> None:
    """7c-3: a healthy (exception-free) pass through the error handler clears
    the reset counter, so transient resets never accumulate to a trip."""
    local_dir = str(tmp_path / "SyncRoot2")
    os.makedirs(local_dir)
    m.sync.sync_path = local_dir

    manager = m.manager
    running = manager.running
    autostart = manager.autostart
    manager.desktop_notifier = Mock()

    from bora.manager import MAX_CURSOR_RESETS

    # Exhaust the budget minus one, then a healthy cycle clears it.
    for _ in range(MAX_CURSOR_RESETS):
        with manager._handle_sync_thread_errors(running, autostart):
            raise CursorResetError("Sync cursor reset", "token expired")
    with manager._handle_sync_thread_errors(running, autostart):
        pass  # healthy cycle resets the counter

    # A further reset is again treated as within-budget (autostart re-armed).
    manager.autostart.clear()
    with manager._handle_sync_thread_errors(running, autostart):
        raise CursorResetError("Sync cursor reset", "token expired")
    assert (
        manager.autostart.is_set()
    ), "counter must reset on a healthy cycle, so this reset is within budget"
