"""This module contains the Google Drive API client.

It will wrap calls to the Google Drive API via ``google-api-python-client``
and handle exceptions, resumable uploads/downloads, etc.

This is a stub — all methods raise :exc:`NotImplementedError`.
The full implementation will be built in Phases 3–5.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time

import requests
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterator,
    Sequence,
    TypeVar,
    overload,
)

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as _AuthRequest
from google.oauth2.credentials import Credentials as _Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from . import changes, oauth
from .changes import FILE_FIELDS, parse_drive_time, to_file_metadata, to_folder_metadata
from .config import CumulusState
from .native_docs import is_google_folder, is_google_native
from .path_cache import PathIDCache
from .path_codec import compose_path, fold_path
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
    RETRYABLE_403_REASONS,
    RETRYABLE_HTTP_STATUS,
    convert_api_errors,
    extract_http_reason,
)
from .exceptions import (
    CancelledError,
    CumulusApiError,
    DataCorruptionError,
    NotFoundError,
    NotLinkedError,
    UnsupportedFileError,
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

# Transient-error retry policy (Stage 3 spec §7.2).
_MAX_RETRY_ATTEMPTS = 5
_INITIAL_BACKOFF = 1.0   # seconds
_MAX_BACKOFF = 32.0      # seconds

# Page size for files.list calls.
_LIST_PAGE_SIZE = 1000

# Download chunk size — Google's recommended granularity for
# MediaIoBaseDownload.
_DOWNLOAD_CHUNK_SIZE = 8 * 1024 * 1024

T = TypeVar("T")


def _is_retryable_http_error(exc: HttpError) -> bool:
    """Whether an HttpError is a transient failure worth retrying."""
    status = exc.resp.status
    if status in RETRYABLE_HTTP_STATUS:
        return True
    if status == 403:
        return extract_http_reason(exc) in RETRYABLE_403_REASONS
    return False


def _retry_after_seconds(exc: HttpError) -> float | None:
    """Parse a numeric Retry-After header from an HttpError, if present.

    Returns ``None`` when the header is absent or is an HTTP-date form
    (we fall back to exponential backoff in that case)."""
    retry_after = exc.resp.get("retry-after")
    if not retry_after:
        return None
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        return None


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
    :param path_cache: Bidirectional Drive file-ID ↔ path cache.
    :param timeout: Request timeout in seconds.
    :param bandwidth_limit_up: Upload bandwidth limit in bytes/sec (0 = unlimited).
    :param bandwidth_limit_down: Download bandwidth limit in bytes/sec (0 = unlimited).
    """

    def __init__(
        self,
        config_name: str,
        cred_storage: Any,
        path_cache: PathIDCache,
        timeout: float = 100,
        session: Any | None = None,
        bandwidth_limit_up: float = 0,
        bandwidth_limit_down: float = 0,
    ) -> None:
        self.config_name = config_name
        self._cred_storage = cred_storage
        self._path_cache = path_cache
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

        # Shutdown signal: set on close()/unlink(), cleared on link().
        # Interrupts the change-feed poll sleep and retry backoff sleeps
        # so the daemon stops within ~1 ms instead of waiting them out.
        self._poll_event = threading.Event()

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

        Used only by the Shape C paste-the-redirect-URL fallback.
        After the user opens this URL and approves, they pass the resulting
        redirect URL (or just its 'code' parameter) back to link(code=...).
        """
        flow = oauth.make_paste_flow()
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent"
        )
        self._pending_paste_flow = _PasteFlowState(
            flow=flow, created_at=datetime.utcnow()
        )
        return auth_url

    def link(
        self,
        code: str | None = None,
        refresh_token: str | None = None,
        access_token: str | None = None,
    ) -> int:
        """Link with a Google account. Returns 0 / 1 / 2 (success / invalid /
        connection failure).
        """
        self._poll_event.clear()
        try:
            if refresh_token is not None:
                creds = oauth.credentials_from_refresh_token(refresh_token)
                creds.refresh(_AuthRequest())
            elif access_token is not None:
                creds = _Credentials(
                    token=access_token, scopes=list(oauth.DRIVE_SCOPES)
                )
            elif code is not None:
                state = self._pending_paste_flow
                self._pending_paste_flow = None
                if state is None:
                    return 1
                if (datetime.utcnow() - state.created_at) > timedelta(minutes=10):
                    return 1
                state.flow.fetch_token(code=code)
                creds = state.flow.credentials
            else:
                return 1

            account = self._fetch_account_info_for_creds(creds)
            oauth.save_credentials(self._cred_storage, creds, account.account_id)
            self._credentials = oauth.wrap_persisting(
                creds, self._cred_storage, account.account_id
            )
            self._cached_account_info = account
            return 0

        except RefreshError:
            return 1
        except CONNECTION_ERRORS:
            return 2

    def unlink(self) -> None:
        """Unlinks the configured Google account.

        Best-effort revokes the refresh token with Google so a leaked token
        can't be reused; failures to revoke do not block local-state cleanup.
        """
        creds = self._credentials
        if creds is not None and creds.refresh_token:
            try:
                requests.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": creds.refresh_token},
                    timeout=self._timeout,
                )
            except CONNECTION_ERRORS:
                logger.debug("Could not revoke token with Google", exc_info=True)
        self._credentials = None
        self._pending_paste_flow = None
        self._cached_account_info = None
        self._cached_space_usage = None
        self._poll_event.set()

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

    def _retry_transient(self, call: Callable[[], T]) -> T:
        """Run *call* and retry it on transient Google Drive errors.

        Retries 429 / 5xx / rate-limit-403 HttpErrors and connection
        errors with exponential backoff (1s → 32s) plus jitter, honoring
        a numeric ``Retry-After`` header when present. After
        ``_MAX_RETRY_ATTEMPTS`` it re-raises the last exception unchanged
        so the caller's ``convert_api_errors`` can map it.

        Raises :exc:`~cumulus.exceptions.CancelledError` if the client's
        ``_poll_event`` fires during a backoff sleep (daemon shutdown).
        """
        attempt = 0
        backoff = _INITIAL_BACKOFF
        while True:
            try:
                return call()
            except HttpError as exc:
                if (
                    not _is_retryable_http_error(exc)
                    or attempt >= _MAX_RETRY_ATTEMPTS - 1
                ):
                    raise
                sleep_for = _retry_after_seconds(exc)
                if sleep_for is None:
                    sleep_for = backoff + random.uniform(0, 0.5)
            except CONNECTION_ERRORS:
                if attempt >= _MAX_RETRY_ATTEMPTS - 1:
                    raise
                sleep_for = backoff + random.uniform(0, 0.5)
            if self._poll_event.wait(sleep_for):
                raise CancelledError(
                    "Sync cancelled",
                    "Google Drive request interrupted by daemon shutdown.",
                )
            backoff = min(backoff * 2, _MAX_BACKOFF)
            attempt += 1

    def _ensure_root_id(self) -> str:
        """Resolve the Drive 'root' alias to a concrete file ID and cache
        it on the path cache. Idempotent — one round-trip per daemon
        lifetime. Every method that hits the Drive API calls this first.
        """
        if self._path_cache.root_id is not None:
            return self._path_cache.root_id
        with convert_api_errors():
            resp = self._retry_transient(
                lambda: self._get_service()
                .files()
                .get(fileId="root", fields="id")
                .execute()
            )
        root_id = resp["id"]
        self._path_cache.root_id = root_id
        return root_id

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
        """Cleans up resources and signals any in-flight poll/retry sleep
        to abort (daemon shutdown)."""
        self._poll_event.set()

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

        :param remote_path: Encoded virtual path of the item.
        :param include_deleted: Whether to return data for trashed items.
        :returns: Metadata of the item, or ``None`` for Google-native
            documents (which cannot be synced) and for trashed items
            when *include_deleted* is ``False``.
        :raises NotFoundError: if *remote_path* is not known to the
            path cache.
        """
        self._ensure_root_id()
        file_id = self._path_cache.path_to_id(remote_path)
        if file_id is None:
            raise NotFoundError(
                "Not found",
                f"No item at '{remote_path}' is known to Cumulus.",
                remote_path=remote_path,
            )
        with convert_api_errors(remote_path=remote_path):
            file = self._retry_transient(
                lambda: self._get_service()
                .files()
                .get(fileId=file_id, fields=FILE_FIELDS)
                .execute()
            )
        self._count_request()
        if file.get("trashed") and not include_deleted:
            return None
        mime = file.get("mimeType")
        if is_google_native(mime):
            return None
        if is_google_folder(mime):
            return to_folder_metadata(file, remote_path)
        return to_file_metadata(file, remote_path)

    def list_revisions(
        self, remote_path: str, limit: int = 10
    ) -> list[FileMetadata]:
        """Lists revisions for the file at *remote_path*, most recent
        first, capped at *limit*.

        The Dropbox-era ``mode`` parameter is dropped — Drive's
        ``revisions.list`` only accepts a file ID.

        :raises NotFoundError: if *remote_path* is not in the path cache.
        """
        self._ensure_root_id()
        file_id = self._path_cache.path_to_id(remote_path)
        if file_id is None:
            raise NotFoundError(
                "Not found",
                f"No item at '{remote_path}' is known to Cumulus.",
                remote_path=remote_path,
            )
        raw: list[dict[str, Any]] = []
        page_token: str | None = None
        with convert_api_errors(remote_path=remote_path):
            while True:
                resp = self._retry_transient(
                    lambda pt=page_token: self._get_service()
                    .revisions()
                    .list(
                        fileId=file_id,
                        fields=(
                            "nextPageToken, revisions(id, modifiedTime, "
                            "size, md5Checksum, "
                            "lastModifyingUser(permissionId))"
                        ),
                        pageSize=100,
                        pageToken=pt,
                    )
                    .execute()
                )
                self._count_request()
                raw.extend(resp.get("revisions", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        # Drive returns revisions oldest-first; we want newest-first.
        raw.reverse()
        name = remote_path.rsplit("/", 1)[-1]
        path_lower = fold_path(remote_path)
        result: list[FileMetadata] = []
        # Each entry's `id` is the file ID (shared by all revisions); `rev`
        # holds the revision ID — that is the per-revision discriminator.
        # `shared` is left False here (revision listings don't carry it).
        for rev in raw[:limit]:
            modified = parse_drive_time(rev.get("modifiedTime"))
            last_user = rev.get("lastModifyingUser") or {}
            result.append(
                FileMetadata(
                    name=name,
                    path_lower=path_lower,
                    path_display=remote_path,
                    id=file_id,
                    client_modified=modified,
                    server_modified=modified,
                    rev=rev["id"],
                    size=int(rev.get("size", 0) or 0),
                    symlink_target=None,
                    shared=False,
                    modified_by=last_user.get("permissionId"),
                    is_downloadable=True,
                    content_hash=rev.get("md5Checksum", ""),
                )
            )
        return result

    def restore(self, remote_path: str, rev: str) -> FileMetadata:
        """Restore an old revision of a file.

        Not available until Stage 4 (write client) lands ``upload``:
        Google Drive has no single-call revision-restore, so restore is
        implemented as download-revision + re-upload, which needs the
        write client.
        """
        raise NotImplementedError(
            "restore() is not available until Stage 4 (write client) "
            "lands upload support."
        )

    # ---- Download / Upload -----------------------------------------------------------

    def download(
        self,
        remote_path: str,
        local_path: str,
        sync_event: SyncEvent | None = None,
    ) -> FileMetadata:
        """Downloads a file from Google Drive to *local_path*.

        Downloads to a ``.cumulus-part`` sidecar, verifies the MD5
        against Drive's ``md5Checksum``, then atomically renames into
        place. *sync_event*, if given, has ``.completed`` updated after
        each chunk.

        :raises NotFoundError: if *remote_path* is unknown to the cache.
        :raises UnsupportedFileError: if it resolves to a native doc.
        :raises DataCorruptionError: on an MD5 mismatch.
        """
        self._ensure_root_id()
        file_id = self._path_cache.path_to_id(remote_path)
        if file_id is None:
            raise NotFoundError(
                "Not found",
                f"No item at '{remote_path}' is known to Cumulus.",
                remote_path=remote_path,
            )
        tmp_path = local_path + ".cumulus-part"
        with convert_api_errors(remote_path=remote_path, local_path=local_path):
            meta_file = self._retry_transient(
                lambda: self._get_service()
                .files()
                .get(fileId=file_id, fields=FILE_FIELDS)
                .execute()
            )
            self._count_request()
            if is_google_native(meta_file.get("mimeType")):
                raise UnsupportedFileError(
                    "Cannot download file",
                    "Google-native documents (Docs, Sheets, Slides, "
                    "etc.) cannot be synced as files.",
                    remote_path=remote_path,
                    local_path=local_path,
                )
            request = self._get_service().files().get_media(fileId=file_id)
            with open(tmp_path, "wb") as fh:
                downloader = MediaIoBaseDownload(
                    fh, request, chunksize=_DOWNLOAD_CHUNK_SIZE
                )
                done = False
                while not done:
                    status, done = self._retry_transient(
                        lambda: downloader.next_chunk()
                    )
                    if sync_event is not None and status is not None:
                        sync_event.completed = status.resumable_progress
                    self._throttle_down(_DOWNLOAD_CHUNK_SIZE)
            self._count_request()

            expected = meta_file.get("md5Checksum")
            if expected:
                hasher = hashlib.md5()
                with open(tmp_path, "rb") as fh:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        hasher.update(block)
                if hasher.hexdigest() != expected:
                    os.remove(tmp_path)
                    raise DataCorruptionError(
                        "Download failed",
                        "The downloaded file is corrupt (MD5 mismatch). "
                        "Cumulus will retry.",
                        remote_path=remote_path,
                        local_path=local_path,
                    )
            os.replace(tmp_path, local_path)
        return to_file_metadata(meta_file, remote_path)

    def _throttle_down(self, nbytes: int) -> None:
        """Crude download bandwidth throttle: sleep proportionally to the
        configured limit. A no-op when no limit is set."""
        if self.bandwidth_limit_down > 0:
            time.sleep(nbytes / self.bandwidth_limit_down)

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
        """Lists the contents of a folder, exhausting all pages.

        See :meth:`list_folder_iterator` for parameter meaning.
        """
        pages = list(
            self.list_folder_iterator(
                remote_path,
                recursive=recursive,
                include_deleted=include_deleted,
                include_mounted_folders=include_mounted_folders,
                include_non_downloadable_files=include_non_downloadable_files,
            )
        )
        return self.flatten_results(pages)

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

        ``recursive=True`` (only meaningful for the sync root ``"/"``) is
        the initial-sync bootstrap: it enumerates the whole reachable
        subtree and atomically populates the path cache. See
        :meth:`_bootstrap_recursive`.

        ``recursive=False`` lists one folder's direct children;
        *remote_path* must already be known to the path cache.

        Google-native documents are filtered out unless
        *include_non_downloadable_files* is ``True``.

        ``include_mounted_folders`` and ``limit`` are accepted for interface
        compatibility with the sync engine; ``limit`` sizes the API page,
        and ``include_mounted_folders`` is currently a no-op (all children
        are listed regardless of mount status).
        """
        self._ensure_root_id()
        if recursive:
            yield from self._bootstrap_recursive(include_non_downloadable_files)
            return

        parent_id = self._path_cache.path_to_id(remote_path)
        if parent_id is None:
            raise NotFoundError(
                "Not found",
                f"No folder at '{remote_path}' is known to Cumulus.",
                remote_path=remote_path,
            )
        query = f"'{parent_id}' in parents"
        if not include_deleted:
            query += " and trashed=false"
        page_size = limit or _LIST_PAGE_SIZE
        page_token: str | None = None
        while True:
            with convert_api_errors(remote_path=remote_path):
                resp = self._retry_transient(
                    lambda pt=page_token: self._get_service()
                    .files()
                    .list(
                        q=query,
                        fields=f"nextPageToken, files({FILE_FIELDS})",
                        pageSize=page_size,
                        pageToken=pt,
                    )
                    .execute()
                )
            self._count_request()
            entries: list[Metadata] = []
            for file in resp.get("files", []):
                mime = file.get("mimeType")
                if is_google_native(mime) and not include_non_downloadable_files:
                    continue
                child_path = compose_path(remote_path, file["name"])
                if is_google_folder(mime):
                    entries.append(to_folder_metadata(file, child_path))
                else:
                    entries.append(to_file_metadata(file, child_path))
            page_token = resp.get("nextPageToken")
            yield ListFolderResult(
                entries=entries,
                has_more=bool(page_token),
                cursor=page_token or "",
            )
            if not page_token:
                break

    def _bootstrap_recursive(
        self, include_non_downloadable_files: bool = False
    ) -> Iterator[ListFolderResult]:
        """Initial-sync bootstrap: enumerate the whole Drive, populate
        the path cache atomically, and yield the result as pages.

        This is the *only* method that builds the path cache from
        scratch. It must be called only on initial sync — it clears and
        rebuilds the cache, discarding any change-feed deltas. See spec
        §5.5.
        """
        # Capture the change cursor BEFORE listing so a change that
        # lands mid-listing is not missed by the first poll cycle.
        with convert_api_errors():
            token_resp = self._retry_transient(
                lambda: self._get_service()
                .changes()
                .getStartPageToken()
                .execute()
            )
        start_token = token_resp["startPageToken"]
        self._count_request()

        # Page through every non-trashed item visible to the user.
        # The full flat listing is buffered in memory before processing;
        # acceptable for the v1 scale target (see spec §5.2 / §10).
        raw_items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            with convert_api_errors():
                resp = self._retry_transient(
                    lambda pt=page_token: self._get_service()
                    .files()
                    .list(
                        q="trashed=false",
                        fields=f"nextPageToken, files({FILE_FIELDS})",
                        pageSize=_LIST_PAGE_SIZE,
                        pageToken=pt,
                    )
                    .execute()
                )
            self._count_request()
            raw_items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        root_id = self._path_cache.root_id
        # Drop the sync root itself (it is implicit) and, unless asked
        # otherwise, Google-native docs. populate_from_listing keeps
        # folders regardless; is_google_native already excludes folders.
        filtered = [
            it
            for it in raw_items
            if it.get("id") != root_id
            and (
                include_non_downloadable_files
                or not is_google_native(it.get("mimeType"))
            )
        ]

        # Atomic populate. The cache BFS-filters to items reachable from
        # root_id; Computers/Backup, shared-with-me and orphans drop out.
        self._path_cache.populate_from_listing(filtered)
        indexed = self._path_cache.count()
        skipped = len(filtered) - indexed
        if skipped > 0:
            logger.info(
                "Indexed %d items from Google Drive; skipped %d items "
                "outside sync scope (Computers/Backup, shared-with-me, "
                "orphans, or duplicate names) — see debug log for detail.",
                indexed,
                skipped,
            )
        else:
            logger.info("Indexed %d items from Google Drive.", indexed)

        # Yield the in-scope items as pages. An item is in scope iff the
        # cache resolved a path for it. The final page carries the
        # change cursor captured above.
        in_scope = [
            it
            for it in filtered
            if self._path_cache.id_to_path(it["id"]) is not None
        ]
        if not in_scope:
            yield ListFolderResult(
                entries=[], has_more=False, cursor=start_token
            )
            return
        for start in range(0, len(in_scope), _LIST_PAGE_SIZE):
            chunk = in_scope[start : start + _LIST_PAGE_SIZE]
            is_last = start + _LIST_PAGE_SIZE >= len(in_scope)
            entries: list[Metadata] = []
            for file in chunk:
                # Safe to re-resolve: the cache is not mutated after
                # populate_from_listing completed above.
                path = self._path_cache.id_to_path(file["id"])
                if is_google_folder(file.get("mimeType")):
                    entries.append(to_folder_metadata(file, path))
                else:
                    entries.append(to_file_metadata(file, path))
            yield ListFolderResult(
                entries=entries,
                has_more=not is_last,
                cursor=start_token if is_last else "",
            )

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
