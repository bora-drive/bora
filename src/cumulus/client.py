"""This module contains the Google Drive API client.

It will wrap calls to the Google Drive API via ``google-api-python-client``
and handle exceptions, resumable uploads/downloads, etc.

This is a stub — all methods raise :exc:`NotImplementedError`.
The full implementation will be built in Phases 3–5.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Iterator,
    Sequence,
    overload,
)

from googleapiclient.discovery import build

from . import oauth
from .config import CumulusState
from .core import (
    Account,
    AccountType,
    DeletedMetadata,
    FileMetadata,
    FolderMetadata,
    FullAccount,
    LinkAccessLevel,
    LinkAudience,
    LinkPermissions,
    ListFolderResult,
    ListSharedLinkResult,
    Metadata,
    PersonalSpaceUsage,
    RootInfo,
    SharedLinkMetadata,
    SpaceUsage,
    Team,
    TeamRootInfo,
    UserRootInfo,
    WriteMode,
)
from .errorhandling import (
    CONNECTION_ERRORS,
    convert_api_errors,
)
from .exceptions import (
    CumulusApiError,
    NotLinkedError,
)

if TYPE_CHECKING:
    from .models import SyncEvent

__all__ = ["GoogleDriveClient"]

logger = logging.getLogger(__name__)

# Google Drive API health-check URL (used by manager for connectivity checks).
API_HEALTH_URL = "https://www.googleapis.com"

# Google Drive default per-user quota: 12,000 queries per day.
# Warn at 80% to give the user a chance to slow down.
_DAILY_QUOTA = 12_000
_QUOTA_WARN_THRESHOLD = int(_DAILY_QUOTA * 0.80)  # 9,600
_QUOTA_CRITICAL_THRESHOLD = int(_DAILY_QUOTA * 0.95)  # 11,400

# Default polling interval for remote change detection (seconds).
# Dropbox used a long-poll; Google Drive has no equivalent, so we poll.
# 180 s keeps daily polling cost to ~480 requests, leaving headroom for
# file operations within the 12k/day quota.
DEFAULT_POLL_INTERVAL = 180


def _not_implemented() -> None:
    raise NotImplementedError(
        "Google Drive client not yet implemented. See PLAN.md Phases 3–5."
    )


@dataclass
class _PasteFlowState:
    """Holds the in-flight google_auth_oauthlib.flow.Flow object between
    get_auth_url() and link(code=...). Implementation details:

    - Single flow at a time per client; calling get_auth_url() replaces
      any prior pending state.
    - Cleared on link() success, on unlink(), and after a 10-minute
      expiry (auth codes from Google expire on roughly that scale).
    """
    flow: "Any"  # google_auth_oauthlib.flow.Flow
    created_at: datetime


class GoogleDriveClient:
    """Client for the Google Drive API.

    This client defines methods to create, move, modify, and delete files and
    folders on Google Drive and to download files from Google Drive.

    All Google API exceptions, :exc:`OSError` from the local file system, and
    connection errors will be converted to
    :exc:`cumulus.exceptions.CumulusApiError`.

    :param config_name: Name of the config to use.
    :param cred_storage: Credential storage instance.
    :param timeout: Request timeout in seconds.
    :param bandwidth_limit_up: Upload bandwidth limit in bytes/sec (0 = unlimited).
    :param bandwidth_limit_down: Download bandwidth limit in bytes/sec (0 = unlimited).
    """

    def __init__(
        self,
        config_name: str,
        cred_storage: Any,
        timeout: float = 100,
        session: Any | None = None,
        bandwidth_limit_up: float = 0,
        bandwidth_limit_down: float = 0,
    ) -> None:
        self.config_name = config_name
        self._cred_storage = cred_storage
        self._state = CumulusState(config_name)
        self._timeout = timeout
        self._cached_account_info: FullAccount | None = None
        self._cached_space_usage: PersonalSpaceUsage | None = None

        # Throttling config.
        self.bandwidth_limit_up = bandwidth_limit_up
        self.bandwidth_limit_down = bandwidth_limit_down

        # Request quota tracking.
        self._request_count = 0
        self._request_day: int = 0  # day-of-year when counter was last reset
        self._warned_at_threshold = False
        self._warned_at_critical = False

        # Auth state.
        self._credentials: oauth.PersistingCredentials | None = None
        self._pending_paste_flow: _PasteFlowState | None = None

    # ---- Request quota tracking -----------------------------------------------------

    def _count_request(self, n: int = 1) -> None:
        """Increment the daily request counter and warn if approaching limits.

        Call this for every Google Drive API request (or pass *n* for batch
        requests that consume multiple quota units).
        """
        today = time.localtime().tm_yday
        if today != self._request_day:
            # New day — reset counter.
            self._request_count = 0
            self._request_day = today
            self._warned_at_threshold = False
            self._warned_at_critical = False

        self._request_count += n

        if (
            self._request_count >= _QUOTA_CRITICAL_THRESHOLD
            and not self._warned_at_critical
        ):
            logger.warning(
                "Google Drive API quota nearly exhausted: %d / %d requests today. "
                "Sync operations may start failing.",
                self._request_count,
                _DAILY_QUOTA,
            )
            self._warned_at_critical = True
        elif (
            self._request_count >= _QUOTA_WARN_THRESHOLD
            and not self._warned_at_threshold
        ):
            logger.warning(
                "Google Drive API quota usage is high: %d / %d requests today. "
                "Consider reducing sync frequency.",
                self._request_count,
                _DAILY_QUOTA,
            )
            self._warned_at_threshold = True

    @property
    def request_count(self) -> int:
        """Number of API requests made today."""
        return self._request_count

    # ---- Auth / linking --------------------------------------------------------------

    @property
    def linked(self) -> bool:
        """Whether the client is linked to a Google account."""
        if self._credentials is None:
            loaded = oauth.load_credentials(self._cred_storage)
            if loaded is not None and loaded.refresh_token is not None:
                account_id = self._cred_storage.account_id or ""
                self._credentials = oauth.wrap_persisting(
                    loaded, self._cred_storage, account_id
                )
        return (
            self._credentials is not None
            and self._credentials.refresh_token is not None
        )

    def get_auth_url(self) -> str:
        """Returns a URL to authorize access to a Google Drive account.

        :returns: URL to retrieve an authorization code.
        """
        _not_implemented()
        return ""  # unreachable

    def link(
        self,
        code: str | None = None,
        refresh_token: str | None = None,
        access_token: str | None = None,
    ) -> int:
        """Links with a Google account using the given authorization code.

        :param code: Authorization code.
        :param refresh_token: Optionally provide a refresh token directly.
        :param access_token: Optionally provide a short-lived access token.
        :returns: 0 on success, 1 for an invalid token, 2 for connection errors.
        """
        _not_implemented()
        return 1  # unreachable

    def unlink(self) -> None:
        """Unlinks the Google account and deletes stored credentials."""
        _not_implemented()

    # ---- Service builder -------------------------------------------------------------

    def _get_service(self) -> Any:
        """Build a fresh Drive v3 service object using cached credentials.

        Every API call in Stages 3+ goes through this helper. cache_discovery
        is disabled because the discovery cache is unsafe to share across
        thread/process boundaries and is just disk I/O we don't need.
        """
        if not self.linked:
            raise NotLinkedError(
                "Not linked",
                "Run 'cumulus auth link' to authorize Cumulus.",
            )
        return build(
            "drive", "v3",
            credentials=self._credentials,
            cache_discovery=False,
        )

    # ---- Properties ------------------------------------------------------------------

    @property
    def account_info(self) -> FullAccount:
        """Returns cached account info.  Use :meth:`get_account_info` to refresh."""
        if not self._cached_account_info:
            return self.get_account_info()
        return self._cached_account_info

    @property
    def is_team_space(self) -> bool:
        """Whether the user is using a Shared Drive. Always ``False`` for MVP."""
        return False

    # ---- Session management ----------------------------------------------------------

    def close(self) -> None:
        """Cleans up resources."""
        pass

    def __enter__(self) -> GoogleDriveClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ---- Account operations ----------------------------------------------------------

    @overload
    def get_account_info(self, dbid: None = None) -> FullAccount: ...

    @overload
    def get_account_info(self, dbid: str) -> Account: ...

    def get_account_info(self, dbid: str | None = None) -> Account:
        """Returns the current account info. The dbid param is preserved for
        compat with the Maestral interface but is ignored for Drive (the
        API only exposes the linked user, never arbitrary accounts)."""
        if self._cached_account_info is None:
            self._cached_account_info = self._fetch_account_info_for_creds(
                self._credentials
            )
        return self._cached_account_info

    def get_space_usage(self) -> PersonalSpaceUsage:
        """Returns space usage of the currently linked account."""
        if self._cached_space_usage is None:
            # Refresh both account info and space usage in one round-trip.
            self._cached_account_info = None
            self.get_account_info()
        assert self._cached_space_usage is not None  # set as side effect of _fetch
        return self._cached_space_usage

    def _fetch_account_info_for_creds(self, creds: Any) -> FullAccount:
        """Validate credentials by issuing one about.get call, returning the
        account info. Also populates self._cached_space_usage as a side effect
        (the about.get response carries both).

        Builds a one-shot service rather than going through _get_service
        because link() may be calling us with credentials not yet stored
        in self._credentials.
        """
        service = build(
            "drive", "v3", credentials=creds, cache_discovery=False
        )
        response = service.about().get(
            fields=(
                "user(emailAddress,displayName,permissionId,photoLink),"
                "storageQuota(usage,limit)"
            )
        ).execute()
        user = response.get("user", {})
        quota = response.get("storageQuota", {})

        self._cached_space_usage = PersonalSpaceUsage(
            used=int(quota.get("usage", 0)),
            allocated=int(quota.get("limit", 0)),
            team_usage=None,
        )
        return FullAccount(
            account_id=user.get("permissionId", ""),
            display_name=user.get("displayName", ""),
            email=user.get("emailAddress", ""),
            profile_photo_url=user.get("photoLink"),
            disabled=False,
        )

    # ---- Metadata operations ---------------------------------------------------------

    def get_metadata(
        self, remote_path: str, include_deleted: bool = False
    ) -> Metadata | None:
        """Gets metadata for an item on Google Drive.

        :param remote_path: Path of file or folder.
        :param include_deleted: Whether to return data for trashed items.
        :returns: Metadata of item at the given path or ``None`` if not found.
            Returns ``None`` for Google-native documents (Docs, Sheets, etc.)
            since they cannot be synced as regular files.

        .. note:: Filter point for native docs (see :mod:`cumulus.native_docs`).
        """
        _not_implemented()
        return None  # unreachable

    def list_revisions(
        self, remote_path: str, mode: str = "path", limit: int = 10
    ) -> list[FileMetadata]:
        """Lists all file revisions for the given file.

        :param remote_path: Path to file.
        :param mode: Must be 'path' or 'id'.
        :param limit: Maximum number of revisions to list.
        :returns: File revision history.
        """
        _not_implemented()
        return []  # unreachable

    def restore(self, remote_path: str, rev: str) -> FileMetadata:
        """Restore an old revision of a file.

        :param remote_path: The path to save the restored file.
        :param rev: The revision to restore.
        :returns: Metadata of restored file.
        """
        _not_implemented()
        return FileMetadata("", "", "", "", None, None, "", 0, None, False, False, None)  # type: ignore[call-arg]  # unreachable

    # ---- Download / Upload -----------------------------------------------------------

    def download(
        self,
        remote_path: str,
        local_path: str,
        sync_event: SyncEvent | None = None,
    ) -> FileMetadata:
        """Downloads a file from Google Drive to a local path.

        :param remote_path: Path to file on Google Drive (or version identifier).
        :param local_path: Path to local download destination.
        :param sync_event: If given, updated with download progress.
        :returns: Metadata of downloaded item.
        :raises: :class:`~cumulus.exceptions.SyncError` if *remote_path*
            points to a Google-native document (Docs, Sheets, etc.).

        .. note:: Filter point for native docs (see :mod:`cumulus.native_docs`).
        """
        _not_implemented()
        return FileMetadata("", "", "", "", None, None, "", 0, None, False, False, None)  # type: ignore[call-arg]  # unreachable

    def upload(
        self,
        local_path: str,
        remote_path: str,
        write_mode: WriteMode = WriteMode.Add,
        update_rev: str | None = None,
        autorename: bool = False,
        sync_event: SyncEvent | None = None,
    ) -> FileMetadata:
        """Uploads a local file to Google Drive.

        :param local_path: Path to local file.
        :param remote_path: Path on Google Drive.
        :param write_mode: Write mode (Add, Update, Overwrite).
        :param update_rev: Expected revision for conditional update.
        :param autorename: Whether to auto-rename on conflict.
        :param sync_event: If given, updated with upload progress.
        :returns: Metadata of uploaded item.
        """
        _not_implemented()
        return FileMetadata("", "", "", "", None, None, "", 0, None, False, False, None)  # type: ignore[call-arg]  # unreachable

    # ---- File / folder operations ----------------------------------------------------

    def remove(
        self, remote_path: str, parent_rev: str | None = None
    ) -> FileMetadata | FolderMetadata:
        """Removes a file or folder.

        :param remote_path: Path to item on Google Drive.
        :param parent_rev: If given, only delete if rev matches (files only).
        :returns: Metadata of deleted item.
        """
        _not_implemented()
        return FolderMetadata("", "", "", "", False)  # type: ignore[call-arg]  # unreachable

    def remove_batch(
        self, entries: Sequence[tuple[str, str | None]], batch_size: int = 100
    ) -> list[FileMetadata | FolderMetadata | CumulusApiError]:
        """Deletes multiple items in a batch.

        .. note:: Implementation should use Google Drive's batch endpoint
           (``POST https://www.googleapis.com/batch/drive/v3``) to group up
           to 100 delete requests per HTTP round-trip.  This is critical for
           staying within the 12k requests/day quota.  Each sub-request in a
           batch counts as one quota unit, but the single HTTP call avoids
           per-request overhead and latency.

        :param entries: List of (path, rev) tuples.
        :param batch_size: Number of items per batch (max 100 for Google).
        :returns: List of metadata or errors.
        """
        _not_implemented()
        return []  # unreachable

    def move(
        self, remote_path: str, new_path: str, autorename: bool = False
    ) -> FileMetadata | FolderMetadata:
        """Moves / renames a file or folder.

        :param remote_path: Current path.
        :param new_path: New path.
        :param autorename: Whether to auto-rename on conflict.
        :returns: Metadata of moved item.
        """
        _not_implemented()
        return FolderMetadata("", "", "", "", False)  # type: ignore[call-arg]  # unreachable

    def make_dir(self, remote_path: str, autorename: bool = False) -> FolderMetadata:
        """Creates a folder.

        :param remote_path: Path of folder.
        :param autorename: Whether to auto-rename on conflict.
        :returns: Metadata of created folder.
        """
        _not_implemented()
        return FolderMetadata("", "", "", "", False)  # type: ignore[call-arg]  # unreachable

    def make_dir_batch(
        self, remote_paths: list[str]
    ) -> list[FolderMetadata | CumulusApiError]:
        """Creates multiple folders in a batch.

        .. note:: Implementation should use Google Drive's batch endpoint
           (up to 100 requests per batch) to minimize quota consumption.

        :param remote_paths: List of folder paths.
        :returns: List of metadata or errors.
        """
        _not_implemented()
        return []  # unreachable

    def share_dir(
        self, remote_path: str, force_async: bool = False
    ) -> FolderMetadata | None:
        """Shares a folder.  No-op for MVP (Shared Drives not supported).

        :param remote_path: Path of folder.
        :param force_async: Ignored.
        :returns: Metadata of shared folder or ``None``.
        """
        _not_implemented()
        return None  # unreachable

    # ---- Change detection / sync feed ------------------------------------------------

    def get_latest_cursor(
        self, remote_path: str, include_non_downloadable_files: bool = False
    ) -> str:
        """Gets the latest change token for the given folder and subfolders.

        :param remote_path: Path of folder.
        :param include_non_downloadable_files: Whether to include non-downloadable files.
        :returns: The latest change token (``startPageToken`` for Google Drive).
        """
        _not_implemented()
        return ""  # unreachable

    def list_folder(
        self,
        remote_path: str,
        recursive: bool = False,
        include_deleted: bool = False,
        include_mounted_folders: bool = True,
        include_non_downloadable_files: bool = False,
    ) -> ListFolderResult:
        """Lists the contents of a folder.

        :param remote_path: Path of folder.
        :param recursive: Whether to list recursively.
        :param include_deleted: Whether to include trashed items.
        :param include_mounted_folders: Whether to include shared folders.
        :param include_non_downloadable_files: Whether to include non-downloadable files.
        :returns: Content of given folder.
        """
        _not_implemented()
        return ListFolderResult([], False, "")  # type: ignore[call-arg]  # unreachable

    def list_folder_iterator(
        self,
        remote_path: str,
        recursive: bool = False,
        include_deleted: bool = False,
        include_mounted_folders: bool = True,
        limit: int | None = None,
        include_non_downloadable_files: bool = False,
    ) -> Iterator[ListFolderResult]:
        """Lists folder contents as a paginated iterator.

        :param remote_path: Path of folder.
        :param recursive: Whether to list recursively.
        :param include_deleted: Whether to include trashed items.
        :param include_mounted_folders: Whether to include shared folders.
        :param limit: Approximate max results per page.
        :param include_non_downloadable_files: Whether to include non-downloadable files.
        :returns: Iterator over pages of folder content.  Google-native
            documents (Docs, Sheets, etc.) should be filtered out of results
            unless *include_non_downloadable_files* is ``True``.

        .. note:: Filter point for native docs (see :mod:`cumulus.native_docs`).
        """
        _not_implemented()
        return iter([])  # unreachable

    def wait_for_remote_changes(
        self, last_cursor: str, timeout: int = DEFAULT_POLL_INTERVAL
    ) -> bool:
        """Waits for remote changes since ``last_cursor``.

        Google Drive has no long-poll endpoint.  Instead, we sleep for
        :data:`DEFAULT_POLL_INTERVAL` seconds (180 s), then check
        ``changes.list`` for new changes.  This keeps polling cost to
        ~480 requests/day, well within the 12k daily quota.

        :param last_cursor: Last change token (``startPageToken``).
        :param timeout: Seconds to sleep before polling.  Defaults to
            :data:`DEFAULT_POLL_INTERVAL`.
        :returns: ``True`` if changes are available, ``False`` otherwise.
        """
        # TODO (Phase 5g): Implement actual polling via changes.list.
        # Sleep, then query changes.list with pageToken=last_cursor.
        # Return True if the response contains any change entries.
        _not_implemented()
        return False  # unreachable

    def list_remote_changes(self, last_cursor: str) -> ListFolderResult:
        """Lists changes since ``last_cursor``.

        :param last_cursor: Last change token.
        :returns: Remote changes since given token.
        """
        _not_implemented()
        return ListFolderResult([], False, "")  # type: ignore[call-arg]  # unreachable

    def list_remote_changes_iterator(
        self, last_cursor: str
    ) -> Iterator[ListFolderResult]:
        """Lists remote changes as a paginated iterator.

        Google-native documents (Docs, Sheets, etc.) must be stripped from
        results before yielding.  Use :func:`cumulus.native_docs.filter_native_docs`
        or check :func:`cumulus.native_docs.is_google_native` per item.

        :param last_cursor: Last change token.
        :returns: Iterator over pages of remote changes.
        """
        _not_implemented()
        return iter([])  # unreachable

    # ---- Sharing ---------------------------------------------------------------------

    def create_shared_link(
        self,
        remote_path: str,
        visibility: LinkAudience = LinkAudience.Public,
        access_level: LinkAccessLevel = LinkAccessLevel.Viewer,
        allow_download: bool | None = None,
        password: str | None = None,
        expires: datetime | None = None,
    ) -> SharedLinkMetadata:
        """Creates a shared link for the given path.

        :param remote_path: Path to file or folder to share.
        :param visibility: Visibility of the shared link.
        :param access_level: Access level granted with the link.
        :param allow_download: Whether to allow downloads.
        :param password: Optional password protection.
        :param expires: Optional expiry time.
        :returns: Metadata for shared link.
        """
        _not_implemented()
        return SharedLinkMetadata("", "", "", None, None)  # type: ignore[call-arg]  # unreachable

    def revoke_shared_link(self, url: str) -> None:
        """Revokes a shared link.

        :param url: URL to revoke.
        """
        _not_implemented()

    def list_shared_links(
        self, remote_path: str | None = None
    ) -> list[SharedLinkMetadata]:
        """Lists all shared links for a given path.

        :param remote_path: Path to file or folder.
        :returns: Shared links for the path.
        """
        _not_implemented()
        return []  # unreachable

    # ---- Utilities -------------------------------------------------------------------

    @staticmethod
    def flatten_results(results: list[ListFolderResult]) -> ListFolderResult:
        """Flattens paginated listing results into a single result.

        :param results: List of results to flatten.
        :returns: Combined result with cursor from the last page.
        """
        if not results:
            raise ValueError("Expected at least one result to flatten.")

        entries = []
        for r in results:
            entries.extend(r.entries)

        return ListFolderResult(
            entries=entries,
            has_more=False,
            cursor=results[-1].cursor,
        )


# Backward-compat alias: sync.py and main.py still reference this name.
# TODO: Update all call-sites to use GoogleDriveClient, then remove this alias.
DropboxClient = GoogleDriveClient
