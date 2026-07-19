"""This module defines the main API which is exposed to the CLI or GUI."""

from __future__ import annotations

import asyncio
import difflib
import gc
import logging
import mimetypes

# system imports
import os
import os.path as osp
import random
import shutil
import sqlite3
import tempfile
import time
from asyncio import AbstractEventLoop, Future
from datetime import datetime, timezone
from typing import Any, Collection, Iterator, Sequence

# external imports
import requests
from packaging.version import Version
from watchdog.events import DirDeletedEvent, FileDeletedEvent

try:
    from systemd import journal
except ImportError:
    journal = None

# local imports
from . import __version__
from .changes import parse_drive_time
from .client import GoogleDriveClient
from .config import BoraConfig, BoraState, validate_config_name
from .constants import (
    CONNECTING,
    DEFAULT_CONFIG_NAME,
    GITHUB_RELEASES_API,
    IDLE,
    IS_LINUX,
    IS_MACOS,
    PAUSED,
    FileStatus,
)
from .core import (
    FileMetadata,
    FullAccount,
    Metadata,
    PersonalSpaceUsage,
    UpdateCheckResult,
)
from .database.core import Database
from .errorhandling import CONNECTION_ERRORS, convert_api_errors
from .exceptions import (
    BusyError,
    KeyringAccessError,
    BoraApiError,
    NoSyncDirError,
    NotFoundError,
    NotLinkedError,
    UnsupportedFileTypeForDiff,
    UpdateCheckError,
)
from .keyring import CredentialStorage
from .logging import (
    LOG_FMT_SHORT,
    AwaitableHandler,
    CachedHandler,
    scoped_logger,
    setup_logging,
)
from .manager import SyncManager
from .models import SyncErrorEntry, SyncEvent, SyncStatus
from .notify import BoraDesktopNotifier
from .path_cache import PathIDCache
from .sync import SyncDirection, SyncEngine, pf_repr
from .utils import get_newer_version
from .utils.appdirs import get_cache_path, get_data_path
from .path_codec import decode_path, encode_path, fold_human, fold_path
from .utils.path import (
    delete,
    is_child,
    is_equal_or_child,
    isdir,
    to_existing_unnormalized_path,
)

__all__ = ["Bora"]


def _sql_add_column(db: Database, table: str, column: str, affinity: str) -> None:
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {affinity};")
    except sqlite3.OperationalError:
        # column already exists
        pass


def _sql_drop_table(db: Database, table: str) -> None:
    try:
        db.execute(f"DROP TABLE {table};")
    except sqlite3.OperationalError:
        # table does not exist
        pass


# ======================================================================================
# Main API
# ======================================================================================


class Bora:
    """The public API

    All methods and properties return objects or raise exceptions which can safely be
    serialized, i.e., pure Python types. The only exception are instances of
    :exc:`bora.exceptions.BoraApiError`: they need to be registered explicitly
    with the serpent serializer which is used for communication to frontends.

    Sync errors and fatal errors which occur in the sync threads can be read with the
    properties :attr:`sync_errors` and :attr:`fatal_errors`, respectively.

    :Example:

        First create an instance with a new config_name. In this example, we choose
        "private" to sync a private remote account. Then link the created config to an
        existing remote account and set up the local sync folder. If successful,
        invoke :meth:`start_sync` to start syncing.

        >>> from bora.main import Bora
        >>> m = Bora(config_name='private')
        >>> url = m.get_auth_url()  # get token from authorization website
        >>> print(f'Please go to {url} to retrieve a authorization token.')
        >>> token = input('Enter auth token: ')
        >>> res = m.link(token)
        >>> if res == 0:
        ...     m.create_sync_directory('~/MyDrive')
        ...     m.start_sync()

    :param config_name: Name of bora configuration to run. Must not contain any
        whitespace. If the given config file does exist, it will be created.
    :param log_to_stderr: If ``True``, Bora will print log messages to stderr.
        When started as a systemd services, this can result in duplicate log messages
        in the systemd journal. Defaults to ``False``.
    :param event_loop: Event loop to use for any features that require an asyncio event
        loop. If not given, those features will be disabled. This currently only affects
        desktop notifications.
    :param shutdown_future: Feature to set a result when shutdown is complete. Used to
        inform the caller if an API client calls :method:`shutdown_daemon`. The event
        loop associated with the Future must be the same as ``event_loop``.
    """

    _external_log_handlers: Sequence[logging.Handler]
    _log_handler_status_longpoll: AwaitableHandler
    _log_handler_info_cache: CachedHandler
    _log_handler_error_cache: CachedHandler

    def __init__(
        self,
        config_name: str = DEFAULT_CONFIG_NAME,
        log_to_stderr: bool = False,
        event_loop: AbstractEventLoop | None = None,
        shutdown_future: Future[bool] | None = None,
    ) -> None:
        # Check system compatibility.
        self._check_system_compatibility()

        self._loop = event_loop
        self._config_name = validate_config_name(config_name)
        self._conf = BoraConfig(self.config_name)
        self._state = BoraState(self.config_name)
        self._logger = scoped_logger(__name__, self.config_name)
        self.cred_storage = CredentialStorage(self.config_name)

        # Set up logging.
        self._log_to_stderr = log_to_stderr
        self._root_logger = scoped_logger("bora", self.config_name)
        self._root_logger.setLevel(min(self.log_level, logging.INFO))
        self._root_logger.handlers.clear()
        self._setup_logging_external()
        self._setup_logging_internal()

        # Run update scripts after init of loggers and config / state.
        self._check_and_run_post_update_scripts()

        # Set up desktop notifier using event loop.
        self._dn: BoraDesktopNotifier | None = None
        if self._loop:
            self._dn = BoraDesktopNotifier(self._config_name, self._loop)

        # Set up sync infrastructure.
        #
        # Stage 3 gives the read client its own DB-backed path cache.
        # The SyncEngine still opens its own connection to the same DB
        # file; Stage 5's sync-engine port consolidates the two onto a
        # single shared Database/PathIDCache.
        self._client_db_path = get_data_path(
            "bora", f"{self.config_name}.db"
        )
        self._client_db = Database(
            sqlite3.connect(self._client_db_path, check_same_thread=False)
        )
        self._path_cache = PathIDCache(self._client_db)
        self.client = GoogleDriveClient(
            self.config_name,
            self.cred_storage,
            self._path_cache,
            bandwidth_limit_up=self.bandwidth_limit_up,
            bandwidth_limit_down=self.bandwidth_limit_down,
        )
        self.sync = SyncEngine(self.client, self._dn)
        self.manager = SyncManager(self.sync, self._dn)

        # Create a future which will return once `shutdown_daemon` is called.
        # This can be used by an event loop to wait until bora has been stopped.
        if shutdown_future and not shutdown_future.get_loop() is self._loop:
            raise RuntimeError("'shutdown_future' must use the passed event loop.")

        self.shutdown_future = shutdown_future

    @staticmethod
    def _check_system_compatibility() -> None:
        if not (IS_MACOS or IS_LINUX):
            raise RuntimeError("Only macOS and Linux are supported")

    def _setup_logging_external(self) -> None:
        """
        Sets up logging to external channels:
          * Log files.
          * The systemd journal, if started by systemd.
          * The systemd notify status, if started by systemd.
          * Stderr, if requested.
        """
        self._external_log_handlers = setup_logging(
            self.config_name, stderr=self._log_to_stderr
        )

    def _setup_logging_internal(self) -> None:
        """Sets up logging to internal info and error caches."""
        # Log to cached handlers for status and error APIs.
        self._log_handler_info_cache = CachedHandler(maxlen=1)
        self._log_handler_info_cache.setFormatter(LOG_FMT_SHORT)
        self._log_handler_info_cache.setLevel(logging.INFO)
        self._root_logger.addHandler(self._log_handler_info_cache)

        self._log_handler_error_cache = CachedHandler()
        self._log_handler_error_cache.setFormatter(LOG_FMT_SHORT)
        self._log_handler_error_cache.setLevel(logging.ERROR)
        self._root_logger.addHandler(self._log_handler_error_cache)

        self._log_handler_status_longpoll = AwaitableHandler(max_unblock_per_second=1)
        self._log_handler_status_longpoll.setFormatter(LOG_FMT_SHORT)
        self._log_handler_status_longpoll.setLevel(logging.INFO)
        self._root_logger.addHandler(self._log_handler_status_longpoll)

    @property
    def version(self) -> str:
        """Returns the current Bora version."""
        return __version__

    def get_auth_url(self) -> str:
        """
        Returns a URL to authorize access to a remote account. To link a remote
        account, retrieve an authorization code from the URL and link Bora by
        calling :meth:`link` with the provided code.

        :returns: URL to retrieve an authorization code.
        """
        return self.client.get_auth_url()

    def link(
        self,
        code: str | None = None,
        refresh_token: str | None = None,
        access_token: str | None = None,
    ) -> int:
        """
        Links Bora with a remote account using the given authorization code. The
        code will be exchanged for an access token and a refresh token with the remote
        servers. The refresh token will be stored for future usage as documented in the
        :mod:`oauth` module. Supported keyring backends are, in order of preference:

            * macOS Keychain
            * Any keyring implementing the SecretService Dbus specification
            * KWallet
            * Gnome Keyring
            * Plain text storage

        For testing, it is also possible to directly provide a long-lived refresh token
        or a short-lived access token. Note that the tokens must be issued for Bora,
        with the required scopes, and will be validated with remote servers as part of
        this call.

        :param code: Authorization code.
        :param refresh_token: Optionally, instead of an authorization code, directly
            provide a refresh token.
        :param access_token: Optionally, instead of an authorization code or a refresh
            token, directly provide an access token. Note that access tokens are
            short-lived.
        :returns: 0 on success, 1 for an invalid token and 2 for connection errors.
        """
        return self.client.link(
            code=code, refresh_token=refresh_token, access_token=access_token
        )

    def unlink(self) -> None:
        """
        Unlinks the configured remote account but leaves all downloaded files in place.
        All syncing metadata will be removed as well. Connection and API errors will be
        handled silently but the access key will always be removed from the
        user's PC.

        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()
        self.stop_sync()

        try:
            self.client.unlink()
        except (ConnectionError, BoraApiError):
            self._logger.debug("Could not invalidate token with the remote", exc_info=True)
        except KeyringAccessError:
            self._logger.debug("Could not remove token from keyring", exc_info=True)

        try:
            self.cred_storage.delete_creds()
        except KeyringAccessError:
            self._logger.debug("Could not remove token from keyring", exc_info=True)

        # Clean up config and state files.
        self._conf.cleanup()
        self._state.cleanup()
        self.sync.reset_sync_state()
        self.sync.reload_cached_config()

        self._logger.info("Unlinked remote account.")

    # ==== Methods to access config and saved state ====================================

    @property
    def config_name(self) -> str:
        """The selected configuration."""
        return self._config_name

    def set_conf(self, section: str, name: str, value: Any) -> None:
        """
        Sets a configuration option.

        :param section: Name of section in config file.
        :param name: Name of config option.
        :param value: Config value. May be any type accepted by :obj:`ast.literal_eval`.
        """
        self._conf.set(section, name, value)

    def get_conf(self, section: str, name: str) -> Any:
        """
        Gets a configuration option.

        :param section: Name of section in config file.
        :param name: Name of config option.
        :returns: Config value. May be any type accepted by :obj:`ast.literal_eval`.
        """
        return self._conf.get(section, name)

    def set_state(self, section: str, name: str, value: Any) -> None:
        """
        Sets a state value.

        :param section: Name of section in state file.
        :param name: Name of state variable.
        :param value: State value. May be any type accepted by :obj:`ast.literal_eval`.
        """
        self._state.set(section, name, value)

    def get_state(self, section: str, name: str) -> Any:
        """
        Gets a state value.

        :param section: Name of section in state file.
        :param name: Name of state variable.
        :returns: State value. May be any type accepted by :obj:`ast.literal_eval`.
        """
        return self._state.get(section, name)

    # ==== Getters / setters for config with side effects ==============================

    @property
    def sync_path(self) -> str:
        """
        Returns the path to the local sync folder (read only). This will be an empty
        string if not sync folder has been set up yet. Use
        :meth:`create_sync_directory` or :meth:`move_sync_directory` to set or
        change the sync directory location instead.

        :raises NotLinkedError: if no remote account is linked.
        """
        if self.pending_link:
            return ""
        else:
            return self.sync.sync_path

    @property
    def excluded_items(self) -> set[str]:
        """
        The list of files and folders excluded by selective sync.

        Any changes to this list will be applied immediately if we have already
        performed the initial sync. Paths which have been added to the list will be
        deleted from the local drive and paths which have been removed will be
        downloaded.

        If the initial was not yet performed, changes will only be saved for the initial
        sync.

        Use :meth:`exclude_item` and :meth:`include_item` to add or remove individual
        items from selective sync.
        """
        if self.pending_link:
            return set()
        else:
            return self.sync.excluded_items

    @excluded_items.setter
    def excluded_items(self, items: Collection[str]) -> None:
        """Setter: excluded_items"""
        excluded_items = self.sync.clean_excluded_items_list(items)
        old_excluded_items = self.excluded_items

        added_excluded_items = excluded_items - old_excluded_items
        added_included_items = old_excluded_items - excluded_items

        has_changes = len(added_excluded_items) > 0 or len(added_included_items) > 0

        if has_changes:
            if self.sync.sync_lock.acquire(blocking=False):
                try:
                    self.sync.excluded_items = excluded_items

                    if self.pending_first_download:
                        return

                    # Apply changes.
                    for path in added_excluded_items:
                        self._logger.info("Excluded %s", path)
                        self._remove_after_excluded(path)

                    for path in added_included_items:
                        # ``path`` is in fold_human form (the stored form of
                        # ``excluded_items``); engine APIs take the encoded
                        # virtual-path form.
                        if not self.sync.is_excluded_by_user(
                            fold_path(encode_path(path))
                        ):
                            self._logger.info("Included %s", path)
                            self.manager.download_queue.put(encode_path(path))

                    self._logger.info(IDLE)

                finally:
                    self.sync.sync_lock.release()

            else:
                raise BusyError(
                    "Cannot set excluded items", "Please try again when idle."
                )

    @property
    def log_level(self) -> int:
        """Log level for log files, stderr and the systemd journal."""
        return self._conf.get("app", "log_level")

    @log_level.setter
    def log_level(self, level: int) -> None:
        """Setter: log_level."""
        self._root_logger.setLevel(min(level, logging.INFO))
        for handler in self._external_log_handlers:
            handler.setLevel(level)
        self._conf.set("app", "log_level", level)

    @property
    def notification_snooze(self) -> float:
        """Snooze time for desktop notifications in minutes. Defaults to 0 if
        notifications are not snoozed."""
        if not self._dn:
            raise RuntimeError("Desktop notifications require an event loop")
        return self._dn.snoozed

    @notification_snooze.setter
    def notification_snooze(self, minutes: float) -> None:
        """Setter: notification_snooze."""
        if not self._dn:
            raise RuntimeError("Desktop notifications require an event loop")
        self._dn.snoozed = minutes

    @property
    def notification_level(self) -> int:
        """Level for desktop notifications. See :mod:`notify` for level definitions."""
        if not self._dn:
            raise RuntimeError("Desktop notifications require an event loop")
        return self._dn.notify_level

    @notification_level.setter
    def notification_level(self, level: int) -> None:
        """Setter: notification_level."""
        if not self._dn:
            raise RuntimeError("Desktop notifications require an event loop")
        self._dn.notify_level = level

    @property
    def bandwidth_limit_down(self) -> float:
        """Maximum download bandwidth to use in bytes per second."""
        return self._conf.get("app", "bandwidth_limit_down")

    @bandwidth_limit_down.setter
    def bandwidth_limit_down(self, value: float) -> None:
        """Setter: bandwidth_limit_down."""
        self.client.bandwidth_limit_down = value
        self._conf.set("app", "bandwidth_limit_down", value)

    @property
    def bandwidth_limit_up(self) -> float:
        """Maximum download bandwidth to use in bytes per second."""
        return self._conf.get("app", "bandwidth_limit_up")

    @bandwidth_limit_up.setter
    def bandwidth_limit_up(self, value: float) -> None:
        """Setter: bandwidth_limit_up."""
        self.client.bandwidth_limit_up = value
        self._conf.set("app", "bandwidth_limit_up", value)

    @property
    def export_google_docs(self) -> bool:
        """Whether to export native Google Docs, Sheets and Slides on download."""
        return bool(self._conf.get("sync", "export_google_docs"))

    @export_google_docs.setter
    def export_google_docs(self, value: bool) -> None:
        """Setter: export_google_docs."""
        self._conf.set("sync", "export_google_docs", bool(value))

    @property
    def export_google_docs_markdown(self) -> bool:
        """Whether to export Google Docs to Markdown instead of .docx."""
        return bool(self._conf.get("sync", "export_google_docs_markdown"))

    @export_google_docs_markdown.setter
    def export_google_docs_markdown(self, value: bool) -> None:
        """Setter: export_google_docs_markdown."""
        self._conf.set("sync", "export_google_docs_markdown", bool(value))

    # ==== State information  ==========================================================

    def status_change_longpoll(self, timeout: float | None = 60) -> bool:
        """
        Blocks until there is a change in status or until a timeout occurs.

        This method can be used by frontends to wait for status changes without constant
        polling. Status changes are for example transitions from syncing to idle or
        vice-versa, new errors, or connection status changes.

        Will unblock at most once per second.

        :param timeout: Maximum time to block before returning, even if there is no
            status change.
        :returns: Whether there was a status change within the timeout.

        .. versionadded:: 1.3.0
        """
        return self._log_handler_status_longpoll.wait_for_emit(timeout)

    @property
    def pending_link(self) -> bool:
        """Whether Bora is linked to a remote account (read only). This will block
        until the user's keyring is unlocked to load the saved auth token."""
        return not self.client.linked

    @property
    def pending_sync_folder(self) -> bool:
        """Whether a local sync directory has been configured (read only). This will
        not check if the configured directory actually exists, starting the sync may
        still raise a :exc:`bora.exceptions.NoSyncDirError`."""
        return not self.sync.sync_path

    @property
    def pending_first_download(self) -> bool:
        """Whether the initial download has already started (read only)."""
        return self.sync.local_cursor == 0 or self.sync.remote_cursor == ""

    @property
    def paused(self) -> bool:
        """Whether syncing is paused by the user (read only). Use :meth:`start_sync` and
        :meth:`stop_sync` to start and stop syncing, respectively."""
        return not self.manager.autostart.is_set() and not self.sync.busy()

    @property
    def running(self) -> bool:
        """Whether sync threads are running (read only). This is similar to
        :attr:`paused` but also returns False if syncing is paused because we cannot
        connect to remote servers.."""
        return self.manager.running.is_set() or self.sync.busy()

    @property
    def connected(self) -> bool:
        """Whether remote servers can be reached (read only)."""
        if self.pending_link:
            return False
        else:
            return self.manager.connected

    @property
    def status(self) -> str:
        """The last status message (read only). This can be displayed as information to
        the user but should not be relied on otherwise."""
        if self.paused:
            return PAUSED
        elif not self.connected:
            return CONNECTING
        else:
            return self._log_handler_info_cache.get_last_message()

    @property
    def sync_errors(self) -> list[SyncErrorEntry]:
        """
        A list of current sync errors as dicts (read only). This list is populated by
        the sync threads. The following keys will always be present but may contain
        empty values: "type", "inherits", "title", "traceback", "title", "message",
        "local_path", "remote_path".

        :raises NotLinkedError: if no remote account is linked.
        """
        return self.sync.sync_errors

    @property
    def fatal_errors(self) -> list[BoraApiError]:
        """
        Returns a list of fatal errors as dicts (read only). This does not include lost
        internet connections or file sync errors which only emit warnings and are
        tracked and cleared separately. Errors listed here must be acted upon for
        Bora to continue syncing.

        The following keys will always be present but may contain empty values: "type",
        "inherits", "title", "traceback", "title", and "message".s

        This list is populated from all log messages with level ERROR or higher that
        have ``exc_info`` attached.
        """
        errors: list[BoraApiError] = []

        for r in self._log_handler_error_cache.cached_records:
            if r.exc_info:
                err = r.exc_info[1]
                if isinstance(err, BoraApiError):
                    errors.append(err)

        return errors

    def clear_fatal_errors(self) -> None:
        """
        Manually clears all fatal errors. This should be used after they have been
        resolved by the user through the GUI or CLI.
        """
        self._log_handler_error_cache.clear()

    @property
    def account_profile_pic_path(self) -> str:
        """
        The path of the current account's profile picture (read only). There may not be
        an actual file at that path if the user did not set a profile picture or the
        picture has not yet been downloaded.
        """
        return get_cache_path("bora", f"{self._config_name}_profile_pic.jpeg")

    def get_file_status(self, local_path: str) -> str:
        """
        Returns the sync status of a file or folder. The returned status is recursive
        for folders.

        * "uploading" if any file inside the folder is being uploaded.
        * "downloading" if any file inside the folder is being downloaded.
        * "error" if any item inside the folder failed to sync and none are currently
          being uploaded or downloaded.
        * "up to date" if all items are successfully synced.
        * "unwatched" if syncing is paused or for items outside the sync directory.

        .. versionadded:: 1.4.4
           Recursive behavior. Previous versions would return "up to date" for a folder,
           even if some contained files would be syncing.

        :param local_path: Path to file on the local drive. May be relative to the
            current working directory.
        :returns: String indicating the sync status. Can be 'uploading', 'downloading',
            'up to date', 'error', or 'unwatched'.
        """
        if not self.running:
            return FileStatus.Unwatched.value

        local_path = osp.realpath(local_path)

        try:
            remote_path_cased = self.sync.to_remote_path(local_path)
        except ValueError:
            return FileStatus.Unwatched.value

        # Find any sync activity for the local path.
        node = self.sync.activity.get_node(remote_path_cased)

        if not node:
            # Always return synced for the root folder in the absense of sync activity.
            if remote_path_cased == "/":
                return FileStatus.Synced.value

            # Check if the path is in our index. If yes, it is fully synced, otherwise
            # it is unwatched.
            if self.sync.get_index_entry_for_local_path(local_path):
                return FileStatus.Synced.value

            return FileStatus.Unwatched.value

        # Return effective status of item and its children. Syncing items take
        # precedence over Failed which take precedence over Synced. Note that Up and
        # Down are mutually exclusive because they are performed in alternating cycles.
        file_status = FileStatus.Synced

        for event in node.sync_events:
            if event.status is SyncStatus.Syncing:
                if event.direction is SyncDirection.Up:
                    return FileStatus.Uploading.value
                elif event.direction is SyncDirection.Down:
                    return FileStatus.Downloading.value
            elif event.status is SyncStatus.Failed:
                file_status = FileStatus.Error

        return file_status.value

    def get_activity(self, limit: int | None = 100) -> list[SyncEvent]:
        """
        Returns the current upload / download activity.

        :param limit: Maximum number of items to return. If None, all entries will be
            returned.
        :returns: A lists of all sync events currently queued for or being uploaded or
            downloaded with the events the furthest up in the queue coming first.
        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()
        return list(self.sync.activity.sync_events)[:limit]

    def get_history(
        self, remote_path: str | None = None, limit: int | None = 100
    ) -> list[SyncEvent]:
        """
        Returns the historic upload / download activity. Up to 1,000 sync events are
        kept in the database. Any events which occurred before the interval specified by
        the ``keep_history`` config value are discarded.

        :param remote_path: If given, show sync history for the specified remote path only.
        :param limit: Maximum number of items to return. If None, all entries will be
            returned.
        :returns: A lists of all sync events since ``keep_history`` sorted by time with
            the oldest event first.
        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()
        events = self.sync.get_history(remote_path=remote_path)
        return events[-limit:] if limit else events

    def get_account_info(self) -> FullAccount:
        """
        Returns the account information from the remote and returns it as a dictionary.

        :returns: remote account information.
        :raises AuthError: in case of an invalid access token.
        :raises ServerError: for internal server errors.
        :raises ConnectionError: if the connection to the remote fails.
        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()
        return self.client.get_account_info()

    def get_space_usage(self) -> PersonalSpaceUsage:
        """
        Gets the space usage from the remote and returns it as a dictionary.

        :returns: remote space usage information.
        :raises AuthError: in case of an invalid access token.
        :raises ServerError: for internal server errors.
        :raises ConnectionError: if the connection to the remote fails.
        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()
        return self.client.get_space_usage()

    # ==== Control methods for front ends ==============================================

    def get_profile_pic(self) -> str | None:
        """
        Attempts to download the user's profile picture from the remote. The picture is
        saved in Bora's cache directory for retrieval when there is no internet
        connection. Check :attr:`account_profile_pic_path` for cached profile pics.

        :returns: Path to saved profile picture or ``None`` if no profile picture was
            downloaded.
        :raises AuthError: in case of an invalid access token.
        :raises ServerError: for internal server errors.
        :raises ConnectionError: if the connection to the remote fails.
        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()

        account_info = self.client.get_account_info()

        if account_info.profile_photo_url:
            with convert_api_errors():
                res = requests.get(account_info.profile_photo_url)
                with open(self.account_profile_pic_path, "wb") as f:
                    f.write(res.content)
            return self.account_profile_pic_path
        else:
            self._delete_old_profile_pics()
            return None

    def get_metadata(self, remote_path: str) -> Metadata | None:
        """
        Returns metadata for a file or folder on the remote.

        :param remote_path: Path to file or folder on the remote.
        :returns: Item metadata as dict. See :class:`Metadata` for
            keys and values.
        :raises NotFoundError: if there is nothing at the given path.
        :raises AuthError: in case of an invalid access token.
        :raises ServerError: for internal server errors.
        :raises ConnectionError: if the connection to the remote fails.
        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()
        return self.client.get_metadata(remote_path)

    def list_folder(
        self,
        remote_path: str,
        recursive: bool = False,
        include_deleted: bool = False,
        include_mounted_folders: bool = True,
        include_non_downloadable_files: bool = False,
    ) -> list[Metadata]:
        """
        List all items inside the folder given by ``remote_path``. Keyword arguments are
        passed on the Drive API call :meth:`client.GoogleDriveClient.list_folder`.

        :param remote_path: Path to folder on the remote.
        :param recursive: If true, the list folder operation will be applied recursively
            to all subfolders and the response will contain contents of all subfolders.
        :param include_deleted: If true, the results will include entries for files and
            folders that used to exist but were deleted.
        :param bool include_mounted_folders: If true, the results will include
            entries under mounted folders which includes app folder, shared
            folder and team folder.
        :param bool include_non_downloadable_files: If true, include files that
            are not downloadable, i.e. Google Docs.
        :raises NotFoundError: if there is nothing at the given path.
        :raises NotAFolderError: if the given path refers to a file.
        :raises AuthError: in case of an invalid access token.
        :raises ServerError: for internal server errors.
        :raises ConnectionError: if the connection to the remote fails.
        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()

        res = self.client.list_folder(
            remote_path,
            recursive=recursive,
            include_deleted=include_deleted,
            include_mounted_folders=include_mounted_folders,
            include_non_downloadable_files=include_non_downloadable_files,
        )
        return res.entries

    def list_folder_iterator(
        self,
        remote_path: str,
        recursive: bool = False,
        include_deleted: bool = False,
        include_mounted_folders: bool = True,
        limit: int | None = None,
        include_non_downloadable_files: bool = False,
    ) -> Iterator[list[Metadata]]:
        """
        Returns an iterator over items inside the folder given by ``remote_path``. Keyword
        arguments are passed on the client call
        :meth:`client.GoogleDriveClient.list_folder_iterator`. Each iteration will yield a
        list of approximately 500 entries, depending on the number of entries returned
        by an individual API call.

        :param remote_path: Path to folder on the remote.
        :param recursive: If true, the list folder operation will be applied recursively
            to all subfolders and the response will contain contents of all subfolders.
        :param include_deleted: If true, the results will include entries for files and
            folders that used to exist but were deleted.
        :param bool include_mounted_folders: If true, the results will include
            entries under mounted folders which includes app folder, shared
            folder and team folder.
        :param Nullable[int] limit: The maximum number of results to return per
            request. Note: This is an approximate number and there can be
            slightly more entries returned in some cases.
        :param bool include_non_downloadable_files: If true, include files that
            are not downloadable, i.e. Google Docs.
        :returns: Iterator over list of Item metadata as dicts. See
            :class:`Metadata` for keys and values.
        :raises NotFoundError: if there is nothing at the given path.
        :raises NotAFolderError: if the given path refers to a file.
        :raises AuthError: in case of an invalid access token.
        :raises ServerError: for internal server errors.
        :raises ConnectionError: if the connection to the remote fails.
        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()

        res_iter = self.client.list_folder_iterator(
            remote_path,
            recursive=recursive,
            include_deleted=include_deleted,
            include_mounted_folders=include_mounted_folders,
            limit=limit,
            include_non_downloadable_files=include_non_downloadable_files,
        )

        for res in res_iter:
            yield res.entries
            del res
            gc.collect()

    def list_revisions(self, remote_path: str, limit: int = 10) -> list[FileMetadata]:
        """
        List revisions of old files at the given path ``remote_path``. This will also
        return revisions if the file has already been deleted.

        :param remote_path: Path to file on the remote.
        :param limit: Maximum number of revisions to list.
        :returns: List of File metadata as dicts. See
            :class:`Metadata` for keys and values.
        :raises NotFoundError: if there never was a file at the given path.
        :raises IsAFolderError: if the given path refers to a folder
        :raises AuthError: in case of an invalid access token.
        :raises ServerError: for internal server errors.
        :raises ConnectionError: if the connection to the remote fails.
        """
        self._check_linked()
        return self.client.list_revisions(remote_path, limit=limit)

    def get_file_diff(
        self, remote_path: str, old_rev: str, new_rev: str | None = None
    ) -> list[str]:
        """
        Compare two revisions of a text file using Python's difflib. The versions will be
        downloaded to temporary files. If new_rev is None, the old revision will be
        compared to the corresponding local file, if any.

        Unlike Dropbox, Google Drive addresses a revision by ``(file_id,
        revision_id)`` rather than a globally-unique rev string, so the file's
        path is required to resolve the file id shared by all its revisions.

        :param remote_path: Encoded virtual path of the file to diff.
        :param old_rev: Identifier of old revision.
        :param new_rev: Identifier of new revision, or ``None`` to compare the
            old revision against the local file.
        :returns: Diff as a list of strings (lines).
        :raises NotFoundError: if the path or a revision is unknown.
        :raises UnsupportedFileTypeForDiff: if the file type is not supported.
        :raises UnsupportedFileTypeForDiff: if file content could not be decoded.
        :raises BoraApiError: if the file could not be read for any other reason.
        """

        def str_from_date(d: datetime) -> str:
            """Convert 'modified' metadata to string in local timezone"""
            tz_date = d.replace(tzinfo=timezone.utc).astimezone()
            return tz_date.strftime("%d %b %Y at %H:%M")

        # All revisions of a file share its file id; resolve it once. Raises
        # NotFoundError if the path is unknown to the cache.
        file_id = self.client._writes._resolve_file_id(remote_path)

        remote_path_cased = self.sync.correct_case(remote_path)
        local_path = self.sync.to_local_path(remote_path)

        def download_rev(rev: str) -> tuple[list[str], datetime]:
            """
            Download a revision to a tmp file, read it, and return its content
            plus the revision's modification time.
            """
            tmp_path = self.client._writes._download_revision_to_tempfile(file_id, rev)
            try:
                try:
                    with convert_api_errors(remote_path_cased, tmp_path):
                        with open(tmp_path) as f:
                            content = f.readlines()
                except UnicodeDecodeError:
                    raise UnsupportedFileTypeForDiff(
                        "Failed to decode the file",
                        "Only UTF-8 plain text files are currently supported.",
                    )
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            with convert_api_errors(remote_path_cased):
                meta = self.client._retry_transient(
                    lambda: self.client._get_service()
                    .revisions()
                    .get(fileId=file_id, revisionId=rev, fields="modifiedTime")
                    .execute()
                )
            return content, parse_drive_time(meta.get("modifiedTime"))

        # Check if a diff is possible from the file type.
        # If mime is None, proceed because most files without
        # an extension are just text files.
        mime, _ = mimetypes.guess_type(remote_path_cased)
        if mime is not None and not mime.startswith("text/"):
            raise UnsupportedFileTypeForDiff(
                f"Bad file type: '{mime}'", "Only files of type 'text/*' are supported."
            )

        if new_rev:
            content_new, modified_new = download_rev(new_rev)
            date_str_new = str_from_date(modified_new)
        else:
            # Use the local file if new_rev is None.
            new_rev = "local version"
            try:
                with convert_api_errors(
                    remote_path=remote_path_cased, local_path=local_path
                ):
                    mtime = time.localtime(osp.getmtime(local_path))
                    date_str_new = time.strftime("%d %b %Y at %H:%M", mtime)

                    with open(local_path) as f:
                        content_new = f.readlines()

            except UnicodeDecodeError:
                raise UnsupportedFileTypeForDiff(
                    "Failed to decode the file",
                    "Only UTF-8 plain text files are currently supported.",
                )

        content_old, modified_old = download_rev(old_rev)
        date_str_old = str_from_date(modified_old)

        return list(
            difflib.unified_diff(
                content_old,
                content_new,
                fromfile=f"{remote_path_cased} ({old_rev})",
                tofile=f"{remote_path_cased} ({new_rev})",
                fromfiledate=date_str_old,
                tofiledate=date_str_new,
            )
        )

    def restore(self, remote_path: str, rev: str) -> FileMetadata:
        """
        Restore an old revision of a file.

        :param remote_path: The path to save the restored file.
        :param rev: The revision to restore. Old revisions can be listed with
            :meth:`list_revisions`.
        :returns: Metadata of the returned file. See :class:`FileMetadata`
            for keys and values.
        :raises AuthError: in case of an invalid access token.
        :raises ServerError: for internal server errors.
        :raises ConnectionError: if the connection to the remote fails.
        """
        self._check_linked()
        self._logger.info(f"Restoring '{remote_path} to {rev}'")
        res = self.client.restore(remote_path, rev)
        self._logger.info(f"Restored '{remote_path} to {rev}'")
        self._logger.info(IDLE)
        return res

    def _delete_old_profile_pics(self) -> None:
        for file in os.listdir(get_cache_path("bora")):
            if file.startswith(f"{self._config_name}_profile_pic"):
                try:
                    os.unlink(osp.join(get_cache_path("bora"), file))
                except OSError:
                    pass

    def rebuild_index(self) -> None:
        """
        Rebuilds the rev file by comparing remote with local files and updating rev
        numbers from the remote server. Files are compared by their content hashes and
        conflicting copies are created if the contents differ. File changes during the
        rebuild process will be queued and uploaded once rebuilding has completed.

        Rebuilding will be performed asynchronously and errors can be accessed through
        :attr:`sync_errors` or :attr:`bora_errors`.

        :raises NotLinkedError: if no remote account is linked.
        :raises NoSyncDirError: if local sync folder is not set up.
        """
        self._check_linked()
        self._check_sync_dir()

        self.manager.rebuild_index()

    def start_sync(self) -> None:
        """
        Creates syncing threads and starts syncing.

        :raises NotLinkedError: if no remote account is linked.
        :raises NoSyncDirError: if local sync folder is not set up.
        """
        self._check_linked()
        self._check_sync_dir()

        self.manager.start()

    def stop_sync(self) -> None:
        """
        Stops all syncing threads if running. Call :meth:`start_sync` to restart
        syncing.
        """
        self.manager.stop()

    def reset_sync_state(self) -> None:
        """
        Resets the sync index and state. Only call this to clean up leftover state
        information if the remote was improperly unlinked (e.g., auth token has been
        manually deleted). Otherwise, leave state management to Bora.

        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()
        self.manager.reset_sync_state()

    def exclude_items(self, *remote_paths: str) -> None:
        """
        Excludes files or folders from sync and deletes it locally. It is safe to call
        this method with items which have already been excluded.

        :param remote_paths: remote paths of items to exclude.
        :raises NotFoundError: if there is nothing at the given path.
        :raises ConnectionError: if the connection to the remote fails.
        :raises AuthError: in case of an invalid access token.
        :raises ServerError: for internal server errors.
        :raises ConnectionError: if the connection to the remote fails.
        :raises NotLinkedError: if no remote account is linked.
        :raises NoSyncDirError: if local sync folder is not set up.
        """
        self._check_linked()
        self._check_sync_dir()

        remote_paths_lower = {fold_human(p.rstrip("/")) for p in remote_paths}

        # ---- input cleanup -----------------------------------------------------------
        excluded_items_old = self.sync.excluded_items
        excluded_items_new = self.sync.clean_excluded_items_list(
            excluded_items_old | remote_paths_lower
        )

        excluded_items_added = excluded_items_new - excluded_items_old

        if len(excluded_items_added) == 0:
            return

        # ---- apply changes -----------------------------------------------------------
        if self.sync.sync_lock.acquire(blocking=False):
            try:
                self.sync.excluded_items = excluded_items_new

                for remote_path_lower in excluded_items_added:
                    self._remove_after_excluded(remote_path_lower)
                    self._logger.info("Excluded %s", remote_path_lower)

                self._logger.info(IDLE)
            finally:
                self.sync.sync_lock.release()

        else:
            raise BusyError("Cannot add excluded items", "Please try again when idle.")

    def _remove_after_excluded(self, remote_path_lower: str) -> None:
        # ``remote_path_lower`` is in fold_human form (decoded, casefolded) — the
        # stored form of ``excluded_items``. The index and sync-error tables are
        # keyed by the fold_path form (encoded). Re-encoding the fold_human form
        # yields exactly that key: fold_path(encode_path(fold_human(p))) ==
        # fold_path(encode_path(p)), because decode_path ∘ encode_path == id and
        # fold_human is idempotent.
        remote_path_fold = fold_path(encode_path(remote_path_lower))

        # Perform housekeeping.
        self.sync.remove_node_from_index(remote_path_fold)
        self.sync.clear_sync_errors_for_path(remote_path_fold, recursive=True)

        # Remove item from local drive. Local paths use the decoded (human)
        # form, so the fold_human key is the right one here.
        local_path_uncased = f"{self.sync_path}{remote_path_lower}"

        try:
            local_path = to_existing_unnormalized_path(local_path_uncased)
        except FileNotFoundError:
            return

        event_cls = DirDeletedEvent if isdir(local_path) else FileDeletedEvent
        with self.manager.sync.fs_events.ignore(event_cls(local_path)):
            delete(local_path)

    def include_items(self, *remote_paths: str) -> None:
        """
        Includes files or folders in sync and downloads it in the background. It is safe
        to call this method with items which have already been included, they will not
        be downloaded again.

        If the path lies inside an excluded folder, all its immediate parents will be
        included. Other children of the excluded folder will remain excluded.

        If any children of remote_path were excluded, they will now be included.

        Any downloads will be carried out by the sync threads. Errors during the
        download can be accessed through :attr:`sync_errors` or :attr:`bora_errors`.

        :param remote_paths: remote paths of items to include.
        :raises NotFoundError: if there is nothing at the given path.
        :raises AuthError: in case of an invalid access token.
        :raises ServerError: for internal server errors.
        :raises ConnectionError: if the connection to the remote fails.
        :raises NotLinkedError: if no remote account is linked.
        :raises NoSyncDirError: if local sync folder is not set up.
        """
        self._check_linked()
        self._check_sync_dir()

        remote_paths_lower = {fold_human(p.rstrip("/")) for p in remote_paths}

        # ---- input validation --------------------------------------------------------
        excluded_items = self.sync.excluded_items
        # ``remote_paths_lower`` is in fold_human form, the stored form of
        # ``excluded_items``; ``is_excluded_by_user`` takes the encoded
        # fold_path form.
        newly_included_items = {
            p
            for p in remote_paths_lower
            if self.sync.is_excluded_by_user(fold_path(encode_path(p)))
        }
        if len(newly_included_items) == 0:
            return

        # ---- update excluded items list ----------------------------------------------
        excluded_items.difference_update(newly_included_items)

        # Find parent folders that should also be newly included.
        for remote_path_lower in newly_included_items.copy():
            for folder in excluded_items.copy():
                # Include all parents which are required to download remote_path_lower.
                if is_child(remote_path_lower, folder):
                    # Remove parent folder from excluded list.
                    excluded_items.discard(folder)
                    # Re-add their children (except parents of remote_path_lower).
                    # The client speaks encoded virtual paths: encode the
                    # fold_human entry on the way in, and decode the metadata's
                    # fold_path ``path_lower`` back to fold_human on the way out
                    # so the stored entries stay comparable.
                    for res in self.client.list_folder_iterator(encode_path(folder)):
                        for entry in res.entries:
                            entry_path_lower = decode_path(entry.path_lower)
                            if not is_equal_or_child(
                                remote_path_lower, entry_path_lower
                            ):
                                excluded_items.add(entry_path_lower)

                    # Add the parent to the download list.
                    newly_included_items.add(folder)

                # Include all children of remote_path_lower.
                if is_child(folder, remote_path_lower):
                    excluded_items.discard(folder)

        # ---- download items from the remote ---------------------------------------------
        if self.sync.sync_lock.acquire(blocking=False):
            self._logger.debug("Excluded items old: %s", pf_repr(self.excluded_items))
            self._logger.debug("Excluded items new: %s", pf_repr(excluded_items))
            try:
                self.sync.excluded_items = excluded_items
                for remote_path_lower in newly_included_items:
                    self._logger.info("Included '%s'", remote_path_lower)
                    # The download queue carries encoded virtual paths.
                    self.manager.download_queue.put(encode_path(remote_path_lower))
            finally:
                self.sync.sync_lock.release()

        else:
            raise BusyError("Cannot include item", "Please try again when idle.")

    def excluded_status(self, remote_path: str) -> str:
        """
        Returns 'excluded', 'partially excluded' or 'included'. This function will not
        check if the item actually exists on the remote.

        :param remote_path: Path to item on the remote.
        :returns: Excluded status.
        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()

        # excluded_items are stored in fold_human form (decoded, casefolded).
        # Apply the same fold to the caller-supplied path before comparing.
        remote_path_lower = fold_human(remote_path.rstrip("/"))

        if any(is_equal_or_child(remote_path_lower, f) for f in self.sync.excluded_items):
            return "excluded"
        elif any(is_child(f, remote_path_lower) for f in self.sync.excluded_items):
            return "partially excluded"
        else:
            return "included"

    def move_sync_directory(self, new_path: str) -> None:
        """
        Sets the local sync directory. This moves all local files to the new location
        and resumes syncing afterwards.

        :param new_path: Full path to local sync folder. "~" will be expanded to the
            user's home directory.
        :raises OSError: if moving the directory fails.
        :raises NotLinkedError: if no remote account is linked.
        :raises NoSyncDirError: if local sync folder is not set up.
        """
        self._check_linked()
        self._check_sync_dir()

        self._logger.info("Moving sync folder...")

        old_path = self.sync.sync_path
        new_path = osp.realpath(osp.expanduser(new_path))

        try:
            if osp.samefile(old_path, new_path):
                self._logger.info(f'sync folder moved to "{new_path}"')
                return
        except FileNotFoundError:
            pass

        if osp.exists(new_path):
            raise FileExistsError(f'Path "{new_path}" already exists.')

        # Pause syncing.
        was_syncing = self.running
        self.stop_sync()

        if osp.isdir(old_path):
            # Will also create ancestors of new_path if required.
            shutil.move(old_path, new_path)
        else:
            os.makedirs(new_path)

        # Update config file and client.
        self.sync.sync_path = new_path

        self._logger.info(f'sync folder moved to "{new_path}"')

        # Resume syncing.
        if was_syncing:
            self.start_sync()

    def create_sync_directory(self, path: str) -> None:
        """
        Creates a new sync directory. Only call this during setup.

        :param path: Full path to local sync folder. "~" will be expanded to the
            user's home directory.
        :raises OSError: if creation fails.
        :raises NotLinkedError: if no remote account is linked.
        """
        self._check_linked()

        # Pause syncing.
        resume = False
        if self.running:
            self.stop_sync()
            resume = True

        # Perform housekeeping.
        path = osp.realpath(osp.expanduser(path))
        self.manager.reset_sync_state()

        # Create new folder.
        os.makedirs(path, exist_ok=True)

        # Update config file and client.
        self.sync.sync_path = path

        # Resume syncing.
        if resume:
            self.start_sync()

    # ==== Utility methods for front ends ==============================================

    def to_local_path(self, remote_path: str) -> str:
        """
        Converts a path relative to the sync folder to a correctly cased local file
        system path.

        :param remote_path: Path relative to remote root.
        :returns: Corresponding path on local hard drive.
        :raises NotLinkedError: if no remote account is linked.
        :raises NoSyncDirError: if local sync folder is not set up.
        """

        self._check_linked()
        self._check_sync_dir()

        return self.sync.to_local_path(remote_path)

    def check_for_updates(self) -> UpdateCheckResult:
        """
        Checks if an update is available.

        :returns: A dictionary with information about the latest release with the fields
            'update_available' (bool), 'latest_release' (str), 'release_notes' (str)
            and 'error' (str or None).
        :raises UpdateCheckError: if checking for an update fails.
        """
        current_version = __version__.lstrip("v")
        update_release_notes = ""

        try:
            resp = requests.get(GITHUB_RELEASES_API)
            resp.raise_for_status()

            data = resp.json()

            releases = []
            release_notes = []

            # Remove? The GitHub API already returns sorted entries.
            data.sort(key=lambda x: Version(x["tag_name"]), reverse=True)

            for item in data:
                v = item["tag_name"].lstrip("v")
                if not Version(v).is_prerelease:
                    releases.append(v)
                    release_notes.append("### {tag_name}\n\n{body}".format(**item))

            new_version = get_newer_version(current_version, releases)

            if new_version:
                # closest_release == current_version if current_version appears in the
                # release list. Otherwise closest_release < current_version
                closest_release = next(
                    v for v in releases if Version(v) <= Version(current_version)
                )
                closest_release_idx = releases.index(closest_release)

                update_release_notes_list = release_notes[0:closest_release_idx]
                update_release_notes = "\n".join(update_release_notes_list)
        except CONNECTION_ERRORS:
            raise UpdateCheckError(
                "Could not check for updates",
                "No internet connection. Please try again later.",
            )
        except Exception as e:
            raise UpdateCheckError(
                "Could not check for updates",
                f"Unable to retrieve information: {e}",
            )

        return UpdateCheckResult(
            update_available=bool(new_version),
            latest_release=new_version or current_version,
            release_notes=update_release_notes,
        )

    def shutdown_daemon(self) -> None:
        """
        Stop syncing and notify anyone monitoring ``shutdown_future`` that we are done.
        """
        # Signal the client's change-feed poll to abort so any blocked download
        # worker can wake and exit before stop_sync() tries to join it.
        # Precise cancel-race handling between manager workers and the client poll
        # is finalised in Stage 6 (manager rewrite); Stage 5 just ensures the
        # poll is signalled here.
        self.client.close()
        self.stop_sync()
        self._client_db.close()

        if self.shutdown_future and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.shutdown_future.set_result, True)

        self._logger.info("Shutting down")

    # ==== Verifiers ===================================================================

    def _check_linked(self) -> None:
        if not self.client.linked:
            raise NotLinkedError(
                "No remote account linked",
                "Please link an account using the GUI or CLI.",
            )

    def _check_sync_dir(self) -> None:
        if self.pending_sync_folder:
            raise NoSyncDirError(
                "No local sync directory",
                "Please set up a local sync directory using the GUI or CLI.",
            )

    # ==== Housekeeping on update  =====================================================

    def _check_and_run_post_update_scripts(self) -> None:
        """
        Runs post-update scripts if necessary.

        Legacy Dropbox-era migrations have been removed. This is a fresh fork.
        """
        self._state.set("app", "updated_scripts_completed", __version__)

        self._conf.remove_deprecated_options()
        self._state.remove_deprecated_options()

    def _update_from_pre_v1_2_1(self) -> None:
        raise RuntimeError("Cannot upgrade from version before v1.2.1")

    def _update_from_pre_v1_3_2(self) -> None:
        if self._conf.get("app", "keyring") == "keyring.backends.OS_X.Keyring":
            self._logger.info("Migrating keyring after update from pre v1.3.2")
            self._conf.set("app", "keyring", "keyring.backends.macOS.Keyring")

    def _update_from_pre_v1_4_8(self) -> None:
        # Migrate config and state keys to new sections.
        self._logger.info("Migrating config after update from pre v1.4.8")

        mapping = {
            "path": {"old": "main", "new": "sync"},
            "excluded_items": {"old": "main", "new": "sync"},
            "keyring": {"old": "app", "new": "auth"},
            "account_id": {"old": "account", "new": "auth"},
        }

        for key, sections in mapping.items():
            if self._conf.has_option(sections["old"], key):
                value = self._conf.get(sections["old"], key)
                self._conf.set(sections["new"], key, value)

        self._logger.info("Migrating state after update from pre v1.4.8")

        mapping = {
            "token_access_type": {"old": "account", "new": "auth"},
        }

        for key, sections in mapping.items():
            if self._state.has_option(sections["old"], key):
                value = self._state.get(sections["old"], key)
                self._state.set(sections["new"], key, value)

    def _update_from_pre_v1_6_0(self) -> None:
        # DB-schema policy (Stage 6, D2): the index / history / hash_cache
        # tables are a DERIVED CACHE of Drive state, not a system of record.
        # There is deliberately no schema-version stamp and no migration
        # framework -- on any schema change we DROP these tables and re-derive
        # them from Drive by reindexing. Re-indexing is cheap and always
        # available, so blind table-drop here is intentional, not data loss.
        self._logger.info("Scheduling reindex after update from pre v1.6.0")

        db_path = get_data_path("bora", f"{self.config_name}.db")
        connection = sqlite3.connect(db_path, check_same_thread=False)
        db = Database(connection)

        _sql_drop_table(db, "hash_cache")
        _sql_drop_table(db, "'index'")
        _sql_drop_table(db, "'history'")

        self._state.reset_to_defaults("sync")

        db.close()

    # ==== Periodic async jobs =========================================================

    def __repr__(self) -> str:
        email = self._state.get("account", "email")
        account_type = self._state.get("account", "type")

        return (
            f"<{self.__class__.__name__}(config={self._config_name!r}, "
            f"account=({email!r}, {account_type!r}))>"
        )


async def sleep_rand(target: float, jitter: float = 60) -> None:
    await asyncio.sleep(target + random.random() * jitter)
