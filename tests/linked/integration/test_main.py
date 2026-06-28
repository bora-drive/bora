"""Linked integration tests for the Bora main API — ported to Google Drive (Stage 7a, D6).

Tests run against a real Google account when BORA_TEST_REFRESH_TOKEN is set;
they skip cleanly when the variable is absent.

Dropbox-specific features dropped:
  - test_get_file_diff / test_get_file_diff_local / test_get_file_diff_not_found /
    test_get_file_diff_unsupported_ext / test_get_file_diff_unsupported_content /
    test_get_file_diff_unsupported_content_local:
      The Dropbox version used a low-level `dbx.files_upload(content, rev=md.rev)`
      call to create specific revisions.  The Drive implementation uses
      `client.upload()` with WriteMode.Overwrite; revisions are Drive revision IDs.
      The get_file_diff tests are ported to use the high-level `client.upload()` /
      `client.list_revisions()` API to obtain rev IDs before calling get_file_diff.
      Flagged for E2.
  - Sharing tests (test_share_dir_*, test_sharedlink_*): sharing is an MVP stub.
    Dropped — covered by test_client.py::test_sharing_stubs_raise.

Uncertain / flagged for E2 real-account run:
  - test_get_file_diff and variants (rev IDs obtained via list_revisions)
  - test_restore (same approach)
  - test_file_status (error path depends on local PathError detection)
"""

from __future__ import annotations

import os
import os.path as osp
import subprocess
import sys
import tempfile
import time

import pytest

from bora.constants import IDLE, FileStatus
from bora.core import FileMetadata, WriteMode
from bora.exceptions import (
    InotifyError,
    NotFoundError,
    SyncError,
    UnsupportedFileTypeForDiff,
)
from bora.main import Bora
from bora.utils.integration import get_inotify_limits

from tests.linked.conftest import requires_account, wait_until


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_file_with_content(
    m: Bora, remote_path: str, content: str | bytes
) -> FileMetadata:
    """Upload content to remote_path via the Drive client; return FileMetadata."""
    if isinstance(content, bytes):
        suffix = osp.splitext(remote_path)[1] or ".bin"
        mode = "wb"
    else:
        suffix = osp.splitext(remote_path)[1] or ".txt"
        mode = "w"

    with tempfile.NamedTemporaryFile(mode=mode, suffix=suffix, delete=False) as tf:
        tf.write(content)
        tf_path = tf.name

    try:
        md = m.client.upload(
            tf_path,
            remote_path,
            write_mode=WriteMode.Overwrite,
        )
    finally:
        os.unlink(tf_path)

    assert isinstance(md, FileMetadata)
    return md


def wait_for_idle(m: Bora, cycles: int = 6) -> None:
    """Blocks until Bora instance is idle for at least ``cycles`` sync cycles."""

    count = 0

    while count < cycles:
        if m.sync.busy():
            # Wait until we can acquire the sync lock => we are idle.
            m.sync.sync_lock.acquire()
            m.sync.sync_lock.release()
            count = 0
        else:
            time.sleep(1)
            count += 1


# ---------------------------------------------------------------------------
# Status / properties
# ---------------------------------------------------------------------------


@requires_account
def test_status_properties(m: Bora) -> None:
    assert not m.pending_link
    assert not m.pending_sync_folder

    assert m.status == IDLE
    assert m.running
    assert m.connected
    assert not m.paused
    assert not m.sync_errors
    assert not m.fatal_errors

    m._root_logger.info("test message")
    assert m.status == "test message"


@requires_account
def test_file_status(m: Bora) -> None:
    # test synced Drive folder
    file_status = m.get_file_status(m.sync_path)
    assert file_status == FileStatus.Synced.value

    # test synced file
    local_path = m.to_local_path("/test.txt")
    with open(local_path, "w") as f:
        f.write("new")

    wait_for_idle(m)

    file_status = m.get_file_status(local_path)
    assert file_status == FileStatus.Synced.value

    # test case sensitivity
    file_status = m.get_file_status(m.to_local_path("/test.txt".upper()))
    assert file_status == FileStatus.Unwatched.value

    # test unwatched outside of drive
    file_status = m.get_file_status("/url/local")
    assert file_status == FileStatus.Unwatched.value

    # test unwatched non-existent
    file_status = m.get_file_status("/this is not a folder")
    assert file_status == FileStatus.Unwatched.value

    # test unwatched when paused
    m.stop_sync()
    wait_for_idle(m)

    file_status = m.get_file_status(m.sync_path)
    assert file_status == FileStatus.Unwatched.value

    m.start_sync()
    wait_for_idle(m)

    # test error status — a path with bytes outside the file system encoding is
    # invalid on Drive (which requires utf-8). Backslash, the Dropbox-invalid
    # character, is a valid name character on Drive/Linux and produces no error.
    invalid_local_folder = m.to_local_path("/test_folder_\udce4")
    os.mkdir(os.fsencode(invalid_local_folder))
    wait_for_idle(m)

    file_status = m.get_file_status(invalid_local_folder)
    file_status_parent = m.get_file_status(m.sync_path)

    assert file_status == FileStatus.Error.value
    assert file_status_parent == FileStatus.Error.value


@requires_account
def test_move_dropbox_folder(m: Bora) -> None:
    new_dir_short = "~/New GoogleDrive"
    new_dir = osp.realpath(osp.expanduser(new_dir_short))

    m.move_sync_directory(new_dir_short)
    assert osp.isdir(new_dir)
    assert m.sync_path == new_dir

    wait_for_idle(m)

    # assert that sync was resumed after moving folder
    assert m.running


@requires_account
def test_move_dropbox_folder_to_itself(m: Bora) -> None:
    m.move_sync_directory(m.sync_path)

    # assert that sync is still running
    assert m.running


@requires_account
def test_move_dropbox_folder_to_existing(m: Bora, tmp_path) -> None:
    new_dir = tmp_path / "new_dbx_dir"
    new_dir.mkdir()

    with pytest.raises(FileExistsError):
        m.move_sync_directory(str(new_dir))

    # assert that sync is still running
    assert m.running


# ---------------------------------------------------------------------------
# Selective sync
# ---------------------------------------------------------------------------


@requires_account
def test_exclude_items(m: Bora) -> None:
    """
    Tests :meth:`Bora.exclude_item`, :meth:`Bora.include_item`,
    :meth:`Bora.excluded_status` and :meth:`Bora.excluded_items`.
    """
    dbx_dirs = [
        "/selective_sync_test_folder",
        "/independent_folder",
        "/selective_sync_test_folder/subfolder_0",
        "/selective_sync_test_folder/subfolder_1",
    ]

    local_dirs = [m.to_local_path(remote_path) for remote_path in dbx_dirs]

    # create folder structure
    for path in local_dirs:
        os.mkdir(path)

    wait_for_idle(m)

    # exclude "/selective_sync_test_folder" from sync
    m.exclude_items(
        "/selective_sync_test_folder",
        "/selective_sync_test_folder/subfolder_0",
    )
    wait_for_idle(m)

    # check that local items have been deleted
    assert not osp.exists(m.to_local_path("/selective_sync_test_folder"))

    # check that `Bora.excluded_items` only contains top-level folder
    assert m.excluded_items == {"/selective_sync_test_folder"}

    # check that `Bora.excluded_status` returns the correct values
    assert m.excluded_status("") == "partially excluded"
    assert m.excluded_status("/independent_folder") == "included"

    for remote_path in dbx_dirs:
        if remote_path != "/independent_folder":
            assert m.excluded_status(remote_path) == "excluded"


@requires_account
def test_include_items(m: Bora) -> None:
    # create remote folder structure and exclude from sync
    m.exclude_items("/selective_sync_test_folder")
    dbx_dirs = [
        "/selective_sync_test_folder",
        "/independent_folder",
        "/selective_sync_test_folder/subfolder_0",
        "/selective_sync_test_folder/subfolder_1",
    ]

    for path in dbx_dirs:
        m.sync.client.make_dir(path)

    wait_for_idle(m)

    # include folder in sync, check that it worked
    m.include_items("/selective_sync_test_folder")
    wait_for_idle(m)

    assert osp.exists(m.to_local_path("/selective_sync_test_folder"))
    assert "/selective_sync_test_folder" not in m.excluded_items

    for remote_path in dbx_dirs:
        assert m.excluded_status(remote_path) == "included"

    # check for fatal errors
    assert not m.fatal_errors


@requires_account
def test_include_items_nested(m: Bora) -> None:
    """Tests special cases of nested selected sync changes."""
    # create remote folder structure and exclude from sync
    m.exclude_items("/selective_sync_test_folder")
    dbx_dirs = [
        "/selective_sync_test_folder",
        "/independent_folder",
        "/selective_sync_test_folder/subfolder_0",
        "/selective_sync_test_folder/subfolder_1",
    ]

    for path in dbx_dirs:
        m.sync.client.make_dir(path)

    wait_for_idle(m)

    # test including a folder inside "/selective_sync_test_folder",
    # "/selective_sync_test_folder" should become included itself but
    # its other children will still be excluded
    m.include_items("/selective_sync_test_folder/subfolder_0")
    wait_for_idle(m)

    assert "/selective_sync_test_folder" not in m.excluded_items
    assert "/selective_sync_test_folder/subfolder_1" in m.excluded_items
    assert osp.exists(m.to_local_path("/selective_sync_test_folder/subfolder_0"))
    assert not osp.exists(m.to_local_path("/selective_sync_test_folder/subfolder_1"))

    # check for fatal errors
    assert not m.fatal_errors


@requires_account
def test_selective_sync_global(m: Bora) -> None:
    """Test :meth:`Bora.exclude_items` to change all items at once."""
    dbx_dirs = [
        "/selective_sync_test_folder",
        "/independent_folder",
        "/selective_sync_test_folder/subfolder_0",
        "/selective_sync_test_folder/subfolder_1",
    ]

    local_dirs = [m.to_local_path(remote_path) for remote_path in dbx_dirs]

    # create folder structure
    for path in local_dirs:
        os.mkdir(path)

    wait_for_idle(m)

    # exclude "/selective_sync_test_folder" and one child from sync
    m.excluded_items = {
        "/selective_sync_test_folder",
        "/selective_sync_test_folder/subfolder_0",
    }
    wait_for_idle(m)

    # check that local items have been deleted
    assert not osp.exists(m.to_local_path("/selective_sync_test_folder"))

    # check that `Bora.excluded_items` has been updated correctly
    assert m.excluded_items == {"/selective_sync_test_folder"}

    # exclude only child folder from sync, check that it worked
    m.excluded_items = {"/selective_sync_test_folder/subfolder_0"}
    wait_for_idle(m)

    assert osp.exists(m.to_local_path("/selective_sync_test_folder"))
    assert osp.exists(m.to_local_path("/selective_sync_test_folder/subfolder_1"))
    assert m.excluded_items == {"/selective_sync_test_folder/subfolder_0"}

    # check for fatal errors
    assert not m.fatal_errors


# ---------------------------------------------------------------------------
# File diff
# NOTE [E2]: These tests obtain rev IDs via client.list_revisions, then call
# get_file_diff with those IDs. The Drive rev IDs are strings (version resource
# IDs), not Dropbox hash-like strings.
# ---------------------------------------------------------------------------


@requires_account
def test_get_file_diff(m: Bora) -> None:
    """Tests diffing two remote revisions of a file."""
    remote_path = "/test.txt"

    _create_file_with_content(m, remote_path, "old")
    # Give Drive a moment to create a distinct revision.
    time.sleep(2)
    _create_file_with_content(m, remote_path, "new")

    revisions = m.list_revisions(remote_path, limit=10)
    assert len(revisions) >= 2, "Expected at least two revisions"

    # list_revisions returns most-recent first.
    new_rev = revisions[0].rev
    old_rev = revisions[1].rev

    diff = m.get_file_diff(remote_path, old_rev, new_rev)

    assert diff[2] == "@@ -1 +1 @@\n"
    assert diff[3] == "-old"
    assert diff[4] == "+new"


@requires_account
def test_get_file_diff_local(m: Bora) -> None:
    """Tests diffing a remote revision against the local file."""
    remote_path = "/test.txt"
    local_path = m.to_local_path(remote_path)

    m.stop_sync()
    wait_for_idle(m)

    _create_file_with_content(m, remote_path, "old")
    revisions = m.list_revisions(remote_path, limit=10)
    assert len(revisions) >= 1
    old_rev = revisions[0].rev

    with open(local_path, "w") as f:
        f.write("new")

    diff = m.get_file_diff(remote_path, old_rev, None)

    assert diff[2] == "@@ -1 +1 @@\n"
    assert diff[3] == "-old"
    assert diff[4] == "+new"


@requires_account
def test_get_file_diff_not_found(m: Bora) -> None:
    """Tests that get_file_diff raises NotFoundError for a non-existent rev."""
    remote_path = "/test.txt"

    _create_file_with_content(m, remote_path, "new")
    revisions = m.list_revisions(remote_path, limit=10)
    new_rev = revisions[0].rev

    with pytest.raises(NotFoundError):
        m.get_file_diff(remote_path, "this-rev-does-not-exist-000000", new_rev)


@requires_account
def test_get_file_diff_unsupported_ext(m: Bora) -> None:
    """Tests file diffs for unsupported file types."""

    remote_path = "/test.pdf"
    _create_file_with_content(m, remote_path, "old")
    time.sleep(2)
    _create_file_with_content(m, remote_path, "new")

    revisions = m.list_revisions(remote_path, limit=10)
    assert len(revisions) >= 2
    new_rev = revisions[0].rev
    old_rev = revisions[1].rev

    with pytest.raises(UnsupportedFileTypeForDiff):
        m.get_file_diff(remote_path, old_rev, new_rev)


@requires_account
def test_get_file_diff_unsupported_content(m: Bora) -> None:
    """Tests file diffs for binary content with .txt extension."""

    remote_path = "/test.txt"
    # Upload a compiled c file header with .txt extension.
    _create_file_with_content(m, remote_path, b"\xcf\xfa\xed\xfe\x07")
    time.sleep(2)
    _create_file_with_content(m, remote_path, "new")

    revisions = m.list_revisions(remote_path, limit=10)
    assert len(revisions) >= 2
    new_rev = revisions[0].rev
    old_rev = revisions[1].rev

    with pytest.raises(UnsupportedFileTypeForDiff):
        m.get_file_diff(remote_path, old_rev, new_rev)


@requires_account
def test_get_file_diff_unsupported_content_local(m: Bora) -> None:
    """Tests file diff for binary local file content."""
    remote_path = "/test.txt"
    local_path = m.to_local_path(remote_path)

    m.stop_sync()
    wait_for_idle(m)

    _create_file_with_content(m, remote_path, "old")
    revisions = m.list_revisions(remote_path, limit=10)
    old_rev = revisions[0].rev

    with open(local_path, "wb") as f:
        f.write("möglich".encode("cp273"))

    with pytest.raises(UnsupportedFileTypeForDiff):
        m.get_file_diff(remote_path, old_rev, None)


# ---------------------------------------------------------------------------
# Restore
# NOTE [E2]: Drive rev IDs are opaque strings obtained from list_revisions.
# ---------------------------------------------------------------------------


@requires_account
def test_restore(m: Bora) -> None:
    """Tests restoring an old revision."""

    remote_path = "/file.txt"
    local_path = m.to_local_path(remote_path)

    # create a local file and sync it
    with open(local_path, "w") as f:
        f.write("old content")

    wait_for_idle(m)

    old_md = m.client.get_metadata(remote_path)
    assert isinstance(old_md, FileMetadata)
    old_rev = old_md.rev

    # modify the file and sync it
    with open(local_path, "w") as f:
        f.write("new content")

    wait_for_idle(m)

    new_md = m.client.get_metadata(remote_path)
    assert isinstance(new_md, FileMetadata)
    assert new_md.content_hash == m.sync.get_local_hash(local_path)

    # restore the old rev

    try:
        m.restore(remote_path, old_rev)
    except SyncError as exc:
        # catch all error for restore in progress, raise otherwise
        if "in progress" not in exc.title:
            raise

    # Restore uploads the old revision as a new revision; the engine then
    # downloads it back. That round-trips through the change feed, so poll the
    # local file for the restored content rather than asserting once.
    def _restored() -> bool:
        try:
            with open(local_path) as f:
                return f.read() == "old content"
        except OSError:
            return False

    wait_until(_restored, timeout=90)


@requires_account
def test_restore_failed(m: Bora) -> None:
    """Tests restoring a non-existing revision."""

    with pytest.raises(NotFoundError):
        m.restore("/restored-file", "this-rev-does-not-exist-000000")


# ---------------------------------------------------------------------------
# inotify
# ---------------------------------------------------------------------------


@requires_account
@pytest.mark.skipif(sys.platform != "linux", reason="inotify specific test")
@pytest.mark.skipif(os.getenv("CI", False) is False, reason="Only running on CI")
def test_inotify_error(m: Bora) -> None:
    max_user_watches, max_user_instances, _ = get_inotify_limits()

    try:
        subprocess.check_call(["sudo", "sysctl", "-w", "fs.inotify.max_user_watches=1"])
    except subprocess.CalledProcessError:
        return

    try:
        m.stop_sync()
        wait_for_idle(m)

        # create some folders for us to watch
        os.mkdir(m.sync_path + "/folder 1")
        os.mkdir(m.sync_path + "/folder 2")
        os.mkdir(m.sync_path + "/folder 3")

        m.start_sync()

        assert len(m.fatal_errors) == 1
        assert isinstance(m.fatal_errors[0], InotifyError)

    finally:
        subprocess.check_call(
            ["sudo", "sysctl", "-w", f"fs.inotify.max_user_watches={max_user_watches}"]
        )
