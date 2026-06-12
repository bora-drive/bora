import os
import os.path as osp
from threading import Event

import pytest

from cumulus import oauth
from cumulus.exceptions import CursorResetError, NoSyncDirError
from cumulus.main import Maestral
from cumulus.utils.appdirs import get_home_dir
from cumulus.utils.path import delete


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_linked(m: Maestral) -> None:
    """Store a valid (offline) refresh-token blob so that m.client.linked is
    True without hitting a real OAuth server."""
    creds = oauth.credentials_from_refresh_token("rt-offline-test")
    oauth.save_credentials(m.cred_storage, creds, "perm-test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pending_flags_on_fresh_instance(m: Maestral) -> None:
    """A freshly constructed Maestral instance has no linked account and no
    sync folder configured, so both pending_link and pending_sync_folder
    must be True."""
    assert m.pending_link is True
    assert m.pending_sync_folder is True


def test_create_sync_directory_creates_dir_and_clears_pending_flag(
    m: Maestral, tmp_path
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
    m: Maestral,
) -> None:
    """ensure_sync_folder_present must raise NoSyncDirError when the
    configured sync directory does not exist on disk."""
    absent = osp.join(get_home_dir(), "does_not_exist_xyz_cumulus_test")
    m.sync.sync_path = absent  # bypass _check_linked; set directly

    with pytest.raises(NoSyncDirError):
        m.sync.ensure_sync_folder_present()


def test_ensure_sync_folder_present_passes_when_dir_exists(
    m: Maestral, tmp_path
) -> None:
    """ensure_sync_folder_present must not raise when the sync folder is
    present on disk."""
    local_dir = str(tmp_path / "SyncRoot")
    os.makedirs(local_dir)
    m.sync.sync_path = local_dir  # bypass _check_linked; set directly

    # Should complete without raising.
    m.sync.ensure_sync_folder_present()


def test_cursor_reset_triggers_reindex_and_autostart(m: Maestral, tmp_path) -> None:
    """CursorResetError from the download worker must reset sync state and
    schedule a restart, not stop permanently (review F5)."""
    # Point sync_path at a real dir so stop() does not trip on missing folder.
    local_dir = str(tmp_path / "SyncRoot")
    os.makedirs(local_dir)
    m.sync.sync_path = local_dir

    # Pre-seed a non-empty cursor so we can observe it being cleared.
    m.sync.remote_cursor = "some-token"

    manager = m.manager
    running = manager.running
    autostart = manager.autostart

    with manager._handle_sync_thread_errors(running, autostart):
        raise CursorResetError("Sync cursor reset", "token expired")

    assert m.sync.remote_cursor == ""        # full re-index scheduled
    assert manager.autostart.is_set()       # sync will resume
