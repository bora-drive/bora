import logging
import os
import time

import pytest

import bora.manager
from bora.config import remove_configuration
from bora.exceptions import AuthError, NotFoundError
from bora.main import Maestral
from bora.utils.appdirs import get_home_dir
from bora.utils.path import delete, generate_cc_name

from tests.linked.conftest import _make_client

fsevents_logger = logging.getLogger("fsevents")
fsevents_logger.setLevel(logging.DEBUG)


def pytest_addoption(parser):
    parser.addoption("--fs-observer", action="store", default="auto", dest="OBSERVER")


def pytest_collection_modifyitems(items):
    """Auto-retry the linked integration tests to absorb Google Drive's
    change-feed timing variance.

    Drive has no change-feed long-poll and ``changes.list`` lags a variable few
    seconds behind a mutation, so these end-to-end tests are inherently timing
    flaky against a real account: a different handful of timing-sensitive tests
    fails on any given run even though each passes the large majority of the
    time. ``pytest-rerunfailures`` re-runs a failed test (with a fresh fixture,
    i.e. a clean account + engine each attempt); only a test that fails *every*
    attempt counts as a failure. Genuinely deterministic gaps (e.g. the Stage-5
    conflict-resolution cases) are separately marked ``xfail`` and are not
    rescued by reruns.
    """
    flaky = pytest.mark.flaky(reruns=2, reruns_delay=5)
    for item in items:
        if "flaky" not in item.keywords:
            item.add_marker(flaky)


class RemoteMutator:
    """A second Drive client (its own path cache) for test-side remote
    mutations.

    The engine and the tests share one ``GoogleDriveClient`` (``m.client``)
    and therefore one path cache. Mutating the remote through it corrupts the
    engine's view: ``client.remove`` evicts the cache entry, so when the
    engine later processes the deletion from its change feed,
    ``changes._emit_delete`` can no longer resolve the ``fileId`` and the
    local copy is never removed; ``upload``/``make_dir`` pre-populate cache
    entries the engine has not yet synced. A *real* remote change comes from
    another device whose mutations never touch this engine's cache.

    Routing every test-side mutation through a separate client reproduces
    that: the engine learns of the change only via its change feed, against
    its own intact cache. The mutator refreshes its own cache (a recursive
    listing) before each op so path resolution never depends on prior state.
    """

    def __init__(self, client) -> None:
        self._c = client

    def _refresh(self) -> None:
        self._c.list_folder("/", recursive=True)

    def upload(self, local_path, remote_path, **kwargs):
        self._refresh()
        return self._c.upload(local_path, remote_path, **kwargs)

    def make_dir(self, remote_path, **kwargs):
        self._refresh()
        return self._c.make_dir(remote_path, **kwargs)

    def move(self, from_path, to_path, **kwargs):
        self._refresh()
        return self._c.move(from_path, to_path, **kwargs)

    def remove(self, remote_path):
        self._refresh()
        try:
            return self._c.remove(remote_path)
        except NotFoundError:
            return None


def _force_delete_tree(path: str) -> None:
    """Delete a local sync folder, restoring permissions first.

    The permission tests chmod files/folders to ``0o000``; a plain delete then
    fails with PermissionError and the sync folder leaks into the next run
    (``generate_cc_name`` increments to "(test runner 1)", "(... 2)", ...).
    Restore writable permissions across the tree before deleting.
    """
    if not os.path.exists(path):
        return
    for root, dirs, files in os.walk(path, topdown=True):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), 0o700)
            except OSError:
                pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    delete(path)


def clean_drive_dir(m: Maestral, lock_name: str) -> None:
    """Remove all items at the Drive root except the lock sentinel file.

    Deletes by ``file_id`` via the raw Drive service rather than
    ``client.remove`` (which resolves the path through the path cache).
    Items created out-of-band — e.g. by ``create_remote_tree`` calling
    ``m.client.upload`` directly — or with percent-encoded names may not be
    in this instance's cache, so a path-based remove silently raises
    ``NotFoundError`` and leaks the item into the next test. The ``file_id``
    from the listing is always valid, so this purge is reliable.
    """
    try:
        result = m.client.list_folder("/", recursive=False)
    except NotFoundError:
        return
    service = m.client._get_service()
    for entry in result.entries:
        # Skip the lock sentinel so the session lock is preserved.
        if getattr(entry, "name", None) == lock_name:
            continue
        file_id = getattr(entry, "id", None)
        if not file_id:
            continue
        try:
            service.files().delete(fileId=file_id).execute()
        except Exception:
            # Cleanup must never fail a test; the item may already be gone.
            pass


def wait_for_idle(m: Maestral, cycles: int = 6) -> None:
    """Blocks until Maestral instance is idle for at least ``cycles`` sync cycles."""
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


@pytest.fixture
def m(pytestconfig, test_lock):
    """
    Returns a Maestral instance linked to the test account and syncing.
    """
    if pytestconfig.option.OBSERVER == "inotify":
        from watchdog.observers.inotify import InotifyObserver

        bora.manager.Observer = InotifyObserver
    elif pytestconfig.option.OBSERVER == "fsevents":
        from watchdog.observers.fsevents import FSEventsObserver

        bora.manager.Observer = FSEventsObserver
    elif pytestconfig.option.OBSERVER == "kqueue":
        from watchdog.observers.kqueue import KqueueObserver

        bora.manager.Observer = KqueueObserver
    elif pytestconfig.option.OBSERVER == "polling":
        from bora.fsevents.polling import OrderedPollingObserver

        bora.manager.Observer = OrderedPollingObserver

    config_name = "bora-test-config-integration"

    m = Maestral(config_name)
    m.log_level = logging.DEBUG
    m.sync.max_cpu_percent = 20 * 100

    refresh_token = os.environ["BORA_TEST_REFRESH_TOKEN"]
    access_token = os.environ.get("BORA_TEST_ACCESS_TOKEN")
    res = m.link(refresh_token=refresh_token, access_token=access_token)

    if res == 1:
        raise AuthError("Invalid token")
    elif res == 2:
        raise ConnectionError("Could not connect to Google Drive")
    elif res > 0:
        raise RuntimeError(f"[error {res}] linking failed")

    home = get_home_dir()
    local_drive_dir = generate_cc_name(home + "/GoogleDrive", suffix="test runner")
    m.create_sync_directory(local_drive_dir)

    # Production polls the Drive change feed every 180s (quota-driven) and has
    # no long-poll endpoint, so the remote-change worker sleeps the full
    # interval before fetching. That is far longer than the ``wait_for_idle``
    # window, so download-direction assertions would race the change feed and
    # spuriously see "not found locally". Tests need a snappy interval; shrink
    # it on this instance only (production keeps 180s). See greening notes.
    _orig_wait_remote = m.sync.wait_for_remote_changes
    m.sync.wait_for_remote_changes = (
        lambda last_cursor, timeout=2, _o=_orig_wait_remote: _o(last_cursor, timeout=2)
    )

    # A second, independent client (its own path cache) for test-side remote
    # mutations, so they reach the engine purely through its change feed —
    # like a real second device. Attached to ``m`` so the module-level
    # ``*_remote`` / ``create_remote_tree`` helpers can reach it without
    # threading a fixture through every test signature. See RemoteMutator.
    remote_config = "bora-test-config-remote"
    remote_client, remote_cred = _make_client(remote_config)
    rres = remote_client.link(refresh_token=refresh_token, access_token=access_token)
    if rres > 0:
        raise AuthError(f"linking remote mutator failed (code {rres})")
    m.test_remote = RemoteMutator(remote_client)

    clean_drive_dir(m, test_lock._name)
    m.start_sync()
    wait_for_idle(m)

    yield m

    m.stop_sync()
    clean_drive_dir(m, test_lock._name)
    wait_for_idle(m)
    _force_delete_tree(local_drive_dir)
    # A test may relocate the sync folder (e.g. test_move_dropbox_folder), in
    # which case the live sync_path differs from the original local_drive_dir;
    # delete it too so it does not leak into the next run as a FileExistsError.
    try:
        current_sync_path = m.sync_path
    except Exception:
        current_sync_path = None
    if current_sync_path and current_sync_path != local_drive_dir:
        _force_delete_tree(current_sync_path)

    remote_cred.delete_creds()
    remove_configuration(remote_config)
    remove_configuration(m.config_name)
    m.cred_storage.delete_creds()
