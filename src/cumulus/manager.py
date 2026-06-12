"""This module contains the classes to coordinate sync threads."""

from __future__ import annotations

import ctypes
import errno
import gc

# system imports
import os
import time
from contextlib import contextmanager
from functools import wraps
from queue import Empty, Queue
from threading import Event, RLock, Thread
from typing import Any, Callable, Generic, Iterator, TypeVar

from typing_extensions import Concatenate, ParamSpec

# local imports
from . import __url__, notify
from .client import API_HEALTH_URL
from .config import CumulusConfig, CumulusState, PersistentMutableSet
from .config.user import UserConfig
from .constants import CONNECTED, CONNECTING, DISCONNECTED, IDLE, PAUSED, SYNCING
from .exceptions import (
    CancelledError,
    ConnectionError,
    CursorResetError,
    InotifyError,
    CumulusApiError,
    NoSyncDirError,
    ServerError,
)
from .fsevents import Observer, ObserverType
from .logging import scoped_logger
from .notify import CumulusDesktopNotifier
from .sync import SyncEngine
from .utils.integration import check_connection, get_inotify_limits

__all__ = ["SyncManager"]

GOOGLE_API_HOSTNAME = API_HEALTH_URL


P = ParamSpec("P")
T = TypeVar("T")

malloc_trim: Callable[[int], None]

try:
    libc = ctypes.CDLL("libc.so.6")
    malloc_trim = libc.malloc_trim
except (OSError, AttributeError):

    def malloc_trim(pad: int) -> None:
        pass


def _free_memory() -> None:
    """Give back memory"""
    gc.collect()
    malloc_trim(0)


class PersistentQueue(Generic[T]):
    def __init__(self, conf: UserConfig, section: str, option: str) -> None:
        self._lock = RLock()
        self._queue: Queue[T] = Queue()
        self._persistent: PersistentMutableSet[T] = PersistentMutableSet(
            conf, section, option
        )

        for item in self._persistent:
            self._queue.put(item)

    def qsize(self) -> int:
        return self._queue.qsize()

    def has_pending(self) -> bool:
        return not self._queue.empty()

    def put(self, item: T) -> None:
        with self._lock:
            self._persistent.add(item)
            self._queue.put(item)

    def get(self, block: bool = True, timeout: int | None = None) -> T:
        return self._queue.get(block, timeout)

    def join(self) -> None:
        self._queue.join()

    def task_done(self, item: T) -> None:
        with self._lock:
            self._persistent.discard(item)
            self._queue.task_done()

    def __contains__(self, entry: Any) -> bool:
        return entry in self._persistent


class SyncManager:
    """Class to manage sync threads

    :param sync: The SyncEngine.
    :param desktop_notifier: Used to send desktop notifications for management-level
        events such as sync errors or other fatal errors.
    """

    download_queue: PersistentQueue[str]
    """Queue of remote paths which have been newly included in syncing."""

    def __init__(
        self, sync: SyncEngine, desktop_notifier: CumulusDesktopNotifier | None = None
    ) -> None:
        self.sync = sync
        self.desktop_notifier = desktop_notifier
        self._conf = CumulusConfig(self.sync.config_name)
        self._state = CumulusState(self.sync.config_name)
        self._logger = scoped_logger(__name__, self.sync.config_name)

        self._lock = RLock()

        self.running = Event()
        self.startup_completed = Event()
        self.autostart = Event()

        self.download_queue = PersistentQueue(self._state, "sync", "pending_downloads")

        self._startup_time = -1.0

        self.connection_check_interval = 10
        self.connected = False
        self._connection_helper_running = True
        self.connection_helper = Thread(
            target=self.connection_monitor,
            name="cumulus-connection-helper",
            daemon=True,
        )
        self.connection_helper.start()

        self.local_observer_thread: ObserverType | None = None

    def _with_lock(  # type:ignore[misc]
        fn: Callable[Concatenate[SyncManager, P], T],
    ) -> Callable[Concatenate[SyncManager, P], T]:
        @wraps(fn)
        def wrapper(__self: SyncManager, *args: P.args, **kwargs: P.kwargs) -> T:
            with __self._lock:
                return fn(__self, *args, **kwargs)

        return wrapper

    # ---- config and state ------------------------------------------------------------

    @property
    def idle_time(self) -> float:
        """
        Returns the idle time in seconds since the last file change or since startup if
        there haven't been any changes in our current session.
        """
        now = time.time()
        time_since_startup = now - self._startup_time
        time_since_last_sync = now - self.sync.last_change

        return min(time_since_startup, time_since_last_sync)

    # ---- control methods -------------------------------------------------------------

    @_with_lock
    def start(self) -> None:
        """Creates observer threads and starts syncing."""
        if self.running.is_set():
            return

        if not check_connection(GOOGLE_API_HOSTNAME, logger=self._logger):
            # Schedule autostart when connection becomes available.
            self.autostart.set()
            self._logger.info(CONNECTING)
            return

        # create a new set of events to let old threads die down
        self.running = Event()
        self.startup_completed = Event()

        self.startup_thread = Thread(
            target=self.startup_worker,
            daemon=True,
            args=(
                self.running,
                self.startup_completed,
                self.autostart,
            ),
            name="cumulus-sync-startup",
        )

        if self._conf.get("sync", "download"):
            self.download_thread = Thread(
                target=self.download_worker,
                daemon=True,
                args=(
                    self.running,
                    self.startup_completed,
                    self.autostart,
                ),
                name="cumulus-download",
            )
            self.download_thread_added_folder = Thread(
                target=self.download_worker_added_item,
                daemon=True,
                args=(
                    self.running,
                    self.startup_completed,
                    self.autostart,
                ),
                name="cumulus-folder-download",
            )

        enable_upload = self._conf.get("sync", "upload")
        enable_download = self._conf.get("sync", "download")

        if enable_upload:
            self.upload_thread = Thread(
                target=self.upload_worker,
                daemon=True,
                args=(
                    self.running,
                    self.startup_completed,
                    self.autostart,
                ),
                name="cumulus-upload",
            )

            if not self.local_observer_thread:
                try:
                    self.local_observer_thread = self._create_observer()
                except CumulusApiError as exc:
                    self._logger.error(exc.title, exc_info=True)
                    if self.desktop_notifier:
                        self.desktop_notifier.notify(
                            exc.title, exc.message, level=notify.ERROR
                        )
                    return

        self.running.set()
        self.autostart.set()

        if enable_upload:
            self.sync.fs_events.enable()
            self.upload_thread.start()

        if enable_download:
            self.download_thread.start()
            self.download_thread_added_folder.start()

        self.startup_thread.start()
        self._startup_time = time.time()

    def _create_observer(self) -> ObserverType:
        local_observer_thread = Observer(timeout=40)
        local_observer_thread.name = "cumulus-fsobserver"
        local_observer_thread.schedule(
            self.sync.fs_events, self.sync.sync_path, recursive=True
        )

        for emitter in local_observer_thread.emitters:
            # there should be only a single emitter thread
            emitter.name = "cumulus-fsemitter"

        try:
            local_observer_thread.start()
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EMFILE):
                try:
                    max_user_watches, max_user_instances, _ = get_inotify_limits()
                except OSError:
                    max_user_watches, max_user_instances = 2**18, 2**9

                url = f"{__url__}/docs/inotify-limits"

                if exc.errno == errno.ENOSPC:
                    n_new = max(2**19, 2 * max_user_watches)

                    raise InotifyError(
                        "Inotify limit reached",
                        "Changes to your sync folder cannot be monitored because it "
                        "contains too many items. Please increase "
                        f"fs.inotify.max_user_watches to {n_new}. See {url} for more "
                        "information.",
                    )

                else:
                    n_new = max(2**10, 2 * max_user_instances)

                    raise InotifyError(
                        "Inotify limit reached",
                        "Changes to your sync folder cannot be monitored because "
                        "there are too many activity inotify instances. Please "
                        f"increase fs.inotify.max_user_instances to {n_new}. See "
                        f"{url} for more information.",
                    )

            elif exc.errno in (errno.EPERM, errno.EACCES):
                raise InotifyError(
                    "Insufficient permissions to monitor local changes",
                    "Please check the permissions for your local sync folder",
                )

            elif exc.errno in (errno.ENOENT, errno.ENOTDIR):
                raise NoSyncDirError(
                    "sync folder missing",
                    "Please move the sync folder back to its original location "
                    "or restart Maestral to set up a new folder.",
                )
            else:
                raise CumulusApiError(
                    "Could not start watch of local directory",
                    exc.strerror or "Unknown error",
                )

        return local_observer_thread

    @_with_lock
    def stop(self) -> None:
        """Stops syncing and destroys worker threads."""
        if self.running.is_set():
            self._logger.info("Shutting down threads...")

        self.sync.fs_events.disable()
        self.running.clear()
        self.startup_completed.clear()
        self.autostart.clear()

        self.sync.cancel_sync()

        if self.local_observer_thread:
            self.local_observer_thread.stop()
            self.local_observer_thread = None

        self._logger.info(PAUSED)

    def reset_sync_state(self) -> None:
        """Resets all saved sync state. Settings are not affected."""
        if self.running.is_set():
            raise CumulusApiError(
                "Cannot reset sync state while syncing", "Please try again when idle."
            )
        self.sync.reset_sync_state()

    def rebuild_index(self) -> None:
        """
        Rebuilds the rev file by comparing remote with local files and updating rev
        numbers from the remote server. Files are compared by their content hashes and
        conflicting copies are created if the contents differ. File changes during the
        rebuild process will be queued and uploaded once rebuilding has completed.

        Rebuilding will be performed asynchronously.
        """
        self._logger.info("Rebuilding index...")

        was_running = self.running.is_set()

        self.stop()
        self.reset_sync_state()

        if was_running:
            self.start()

    # ---- thread methods --------------------------------------------------------------

    def connection_monitor(self) -> None:
        """
        Monitors the connection to remote servers. Pauses syncing when the connection
        is lost and resumes syncing when reconnected and syncing has not been paused by
        the user.
        """
        while self._connection_helper_running:
            connected = check_connection(GOOGLE_API_HOSTNAME)

            if connected != self.connected:
                # Log the status change.
                self._logger.info(CONNECTED if connected else CONNECTING)

            if connected:
                if not self.running.is_set() and self.autostart.is_set():
                    self.start()

            self.connected = connected

            time.sleep(self.connection_check_interval)

    def download_worker(
        self,
        running: Event,
        startup_completed: Event,
        autostart: Event,
    ) -> None:
        """
        Worker to sync changes of the remote with local folder.

        :param running: Event to shut down local file event handler and worker threads.
        :param startup_completed: Set when startup sync is completed.
        :param autostart: Set when syncing should automatically resume on connection.
        """
        startup_completed.wait()

        while running.is_set():
            with self._handle_sync_thread_errors(running, autostart):
                has_changes = self.sync.wait_for_remote_changes(self.sync.remote_cursor)

                if not running.is_set():
                    return

                self.sync.ensure_sync_folder_present()

                if has_changes:
                    self._logger.info(SYNCING)
                    self.sync.download_sync_cycle()
                    self._logger.info(IDLE)

                    self.sync.client.get_space_usage()

        _free_memory()

    def download_worker_added_item(
        self,
        running: Event,
        startup_completed: Event,
        autostart: Event,
    ) -> None:
        """
        Worker to download items which have been newly included in sync.

        :param running: Event to shut down local file event handler and worker threads.
        :param startup_completed: Set when startup sync is completed.
        :param autostart: Set when syncing should automatically resume on connection.
        """
        startup_completed.wait()

        while running.is_set():
            with self._handle_sync_thread_errors(running, autostart):
                try:
                    remote_path_lower = self.download_queue.get(timeout=40)
                except Empty:
                    continue

                if not running.is_set():
                    self.download_queue.put(remote_path_lower)
                    return

                with self.sync.sync_lock:
                    self.sync.get_remote_item(remote_path_lower)
                    self.download_queue.task_done(remote_path_lower)
                    self._logger.info(IDLE)

        _free_memory()

    def upload_worker(
        self,
        running: Event,
        startup_completed: Event,
        autostart: Event,
    ) -> None:
        """
        Worker to sync local changes to the remote.

        :param running: Event to shut down local file event handler and worker threads.
        :param startup_completed: Set when startup sync is completed.
        :param autostart: Set when syncing should automatically resume on connection.
        """
        startup_completed.wait()

        while running.is_set():
            with self._handle_sync_thread_errors(running, autostart):
                has_changes = self.sync.wait_for_local_changes()

                if not running.is_set():
                    return

                self.sync.ensure_sync_folder_present()

                if has_changes:
                    self._logger.info(SYNCING)
                    self.sync.upload_sync_cycle()
                    self._logger.info(IDLE)

        _free_memory()

    def startup_worker(
        self,
        running: Event,
        startup_completed: Event,
        autostart: Event,
    ) -> None:
        """
        Worker to sync local changes to the remote.

        :param running: Event to shut down local file event handler and worker threads.
        :param startup_completed: Set when startup sync is completed.
        :param autostart: Set when syncing should automatically resume on connection.
        """
        with self._handle_sync_thread_errors(running, autostart):
            # Fail early if sync folder disappeared.
            self.sync.ensure_sync_folder_present()

            # Reload mignore rules.
            self.sync.load_mignore_file()

            self.sync.client.get_space_usage()

            if not running.is_set():
                startup_completed.set()
                return

            # Retry failed downloads.
            if len(self.sync.download_errors) > 0:
                self._logger.info("Retrying failed syncs...")

            for error in list(self.sync.download_errors):
                self.sync.get_remote_item(error.remote_path_lower)

            # Resume interrupted downloads.
            if self.download_queue.qsize() > 0:
                self._logger.info("Resuming interrupted syncs...")

            while self.download_queue.has_pending():
                remote_path = self.download_queue.get()
                self.sync.get_remote_item(remote_path)
                self.download_queue.task_done(remote_path)

            if not running.is_set():
                startup_completed.set()
                return

            self.sync.download_sync_cycle()

            if not running.is_set():
                startup_completed.set()
                return

            if self._conf.get("sync", "upload"):
                self.sync.upload_local_changes_while_inactive()

            self._logger.info(IDLE)

        startup_completed.set()
        _free_memory()

    # ---- utilities -------------------------------------------------------------------

    @contextmanager
    def _handle_sync_thread_errors(
        self, running: Event, autostart: Event
    ) -> Iterator[None]:
        try:
            yield
        except CancelledError:
            # Shutdown will be handled externally.
            running.clear()
        except CursorResetError:
            self._logger.warning(
                "Google Drive change token expired; rebuilding the index."
            )
            if self.desktop_notifier:
                self.desktop_notifier.notify(
                    "Rebuilding index",
                    "Cumulus was offline too long and will re-index "
                    "Google Drive. Sync resumes automatically.",
                    level=notify.INFO,
                )
            self.stop()
            self.sync.reset_sync_state()
            self.autostart.set()
        except (ConnectionError, ServerError):
            self._logger.debug("Connection error", exc_info=True)
            self._logger.info(DISCONNECTED)
            self.stop()
            self.autostart.set()
            self._logger.info(CONNECTING)
        except Exception as err:
            title = getattr(err, "title", "Unexpected error")
            message = getattr(err, "message", "Please restart to continue syncing")
            self._logger.error(title, exc_info=True)
            if self.desktop_notifier:
                self.desktop_notifier.notify(title, message, level=notify.ERROR)
            self.stop()

    def __del__(self) -> None:
        try:
            self.stop()
            self._connection_helper_running = False
        except Exception:
            pass
