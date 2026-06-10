"""This module contains methods to convert OSErrors, Google auth-library
exceptions, and HTTP errors to instances of
:exc:`cumulus.exceptions.CumulusApiError`.
"""

from __future__ import annotations

import builtins
import contextlib
import errno
import json
import os
from typing import Any, Callable, Iterator, TypeVar, Union

import google.auth.exceptions
import requests
from googleapiclient.errors import HttpError

from .exceptions import (
    BadInputError,
    ConflictError,
    CursorResetError,
    DataChangedError,
    DataCorruptionError,
    AuthError,
    ConnectionError,
    ServerError,
    FileConflictError,
    FileReadError,
    FileSizeError,
    FolderConflictError,
    InsufficientPermissionsError,
    InsufficientSpaceError,
    InvalidDbidError,
    IsAFolderError,
    CumulusApiError,
    NotAFolderError,
    NotFoundError,
    OutOfMemoryError,
    PathError,
    RestrictedContentError,
    SharedLinkError,
    SymlinkError,
    SyncError,
    TokenExpiredError,
    TokenRevokedError,
    UnsupportedFileError,
)
from .utils.path import fs_max_lengths_for_path

__all__ = [
    "CONNECTION_ERRORS",
    "RETRYABLE_403_REASONS",
    "RETRYABLE_HTTP_STATUS",
    "extract_http_reason",
    "http_error_to_cumulus",
    "google_api_to_cumulus_error",
    "os_to_cumulus_error",
    "convert_api_errors",
]

CONNECTION_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.RetryError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    builtins.ConnectionError,
)

# 403 sub-reasons that mean "rate-limited, retry" rather than a hard
# permission failure. See the Stage 3 spec §6.1.
RETRYABLE_403_REASONS = frozenset(
    {
        "userRateLimitExceeded",
        "rateLimitExceeded",
        "sharingRateLimitExceeded",
    }
)

# HTTP status codes that are always transient and worth retrying.
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})

LocalError = Union[CumulusApiError, OSError]

FT = TypeVar("FT", bound=Callable[..., Any])


# ==== Conversion functions to generate error messages and types =======================


def extract_http_reason(exc: HttpError) -> str:
    """Pull the Drive sub-reason string (e.g. ``rateLimitExceeded``) from
    an :exc:`~googleapiclient.errors.HttpError`. Returns ``""`` when the
    error body has no machine-readable reason.
    """
    try:
        content = exc.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        data = json.loads(content)
    except (ValueError, AttributeError, TypeError):
        return ""
    error = data.get("error", {})
    errors = error.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return errors[0].get("reason", "")
    # Some Drive responses carry a top-level status string instead.
    status = error.get("status", "")
    return status if isinstance(status, str) else ""


def http_error_to_cumulus(
    status: int,
    reason: str,
    exc: HttpError,
    remote_path: str | None = None,
    local_path: str | None = None,
) -> CumulusApiError:
    """Map an HTTP status + Drive sub-reason to a Cumulus exception.

    See the Stage 3 spec §6.1 for the full table. Retryable statuses
    (429, 5xx, the rate-limit 403 reasons) only reach this function
    after ``client._retry_transient`` has exhausted its budget, so they
    map straight to a user-facing ``ConnectionError`` / ``ServerError``.
    """
    if status == 400:
        return BadInputError(
            "Bad request",
            "Cumulus made an invalid request to Google Drive.",
            remote_path=remote_path,
            local_path=local_path,
        )
    if status == 401:
        return TokenRevokedError(
            "Authentication revoked",
            "Your Google account has been disconnected. "
            "Run 'cumulus auth link -r' to re-link.",
        )
    if status == 403:
        if reason in RETRYABLE_403_REASONS:
            return ConnectionError(
                "Google Drive rate limit reached",
                "Too many requests to Google Drive. Cumulus will retry.",
            )
        if reason == "storageQuotaExceeded":
            return InsufficientSpaceError(
                "Cannot sync item",
                "Your Google Drive account is out of storage space.",
                remote_path=remote_path,
                local_path=local_path,
            )
        if reason == "dailyLimitExceeded":
            return ConnectionError(
                "Google Drive daily quota reached",
                "The daily Google Drive API quota for this app has been "
                "exhausted. Sync will resume once the quota resets.",
            )
        if reason == "cannotDownloadAbusiveFile":
            return RestrictedContentError(
                "Cannot download file",
                "Google flagged this file as malware or otherwise abusive "
                "and will not allow it to be downloaded.",
                remote_path=remote_path,
                local_path=local_path,
            )
        return InsufficientPermissionsError(
            "Insufficient permissions",
            "You do not have permission to access this item on Google Drive.",
            remote_path=remote_path,
            local_path=local_path,
        )
    if status == 404:
        return NotFoundError(
            "Not found",
            "The requested item does not exist on Google Drive.",
            remote_path=remote_path,
            local_path=local_path,
        )
    if status == 412:
        return DataChangedError(
            "Item changed on Google Drive",
            "The item changed on Drive since Cumulus last checked. "
            "Cumulus will reconcile on the next sync pass.",
            remote_path=remote_path,
            local_path=local_path,
        )
    if status == 429:
        return ConnectionError(
            "Google Drive rate limit reached",
            "Too many requests to Google Drive. Cumulus will retry.",
        )
    if status in (500, 502, 503, 504):
        return ServerError(
            "Google Drive server error",
            "Google Drive reported an internal error. Cumulus will retry.",
        )
    return CumulusApiError(
        f"Google Drive API error ({status})",
        str(exc),
        remote_path=remote_path,
        local_path=local_path,
    )


@contextlib.contextmanager
def convert_api_errors(
    remote_path: str | None = None, local_path: str | None = None
) -> Iterator[None]:
    """A context manager that catches and re-raises instances of :exc:`OSError` and
    Google API exceptions as :exc:`cumulus.exceptions.CumulusApiError` or
    :exc:`ConnectionError`.

    :param remote_path: Remote path associated with the error.
    :param local_path: Local path associated with the error.
    """
    try:
        yield
    except google.auth.exceptions.RefreshError as exc:
        raise TokenRevokedError(
            "Authentication revoked",
            "Your Google account has been disconnected. "
            "Run 'cumulus auth link -r' to re-link.",
        ) from exc
    except google.auth.exceptions.GoogleAuthError as exc:
        raise AuthError("Authentication error", str(exc)) from exc
    except HttpError as exc:
        raise http_error_to_cumulus(
            exc.resp.status,
            extract_http_reason(exc),
            exc,
            remote_path,
            local_path,
        ) from exc
    except CONNECTION_ERRORS:
        raise ConnectionError(
            "Cannot connect to Google Drive",
            "Please check your internet connection and try again later.",
        )
    except OSError as exc:
        if exc.errno == errno.EPROTOTYPE:
            # Can occur on macOS, see https://bugs.python.org/issue33450.
            raise ConnectionError(
                "Cannot connect to Google Drive",
                "Please check your internet connection and try again later.",
            )
        else:
            raise os_to_cumulus_error(exc, remote_path, local_path)


def os_to_cumulus_error(
    exc: OSError, remote_path: str | None = None, local_path: str | None = None
) -> LocalError:
    """Converts a :exc:`OSError` to a :exc:`cumulus.exceptions.CumulusApiError` and
    tries to add a reasonably informative error title and message.

    :param exc: Original OSError.
    :param remote_path: Remote path associated with the error.
    :param local_path: Local path associated with the error.
    :returns: Converted exception.
    """
    title = "Could not sync file or folder"
    err_cls: type[CumulusApiError]

    if isinstance(exc, PermissionError):
        err_cls = InsufficientPermissionsError  # subclass of SyncError
        text = "Insufficient read or write permissions for this location."
    elif isinstance(exc, FileNotFoundError):
        err_cls = NotFoundError  # subclass of SyncError
        text = "The given path does not exist."
    elif isinstance(exc, FileExistsError):
        err_cls = ConflictError  # subclass of SyncError
        title = "Could not download file"
        text = "There already is an item at the given path."
    elif isinstance(exc, IsADirectoryError):
        err_cls = IsAFolderError  # subclass of SyncError
        title = "Could not create local file"
        text = "The given path refers to a folder."
    elif isinstance(exc, NotADirectoryError):
        err_cls = NotAFolderError  # subclass of SyncError
        title = "Could not create local folder"
        text = "The given path refers to a file."
    elif exc.errno == errno.ENAMETOOLONG:
        err_cls = PathError  # subclass of SyncError
        title = "Could not create local file"

        try:
            max_path, max_name = fs_max_lengths_for_path(local_path or "/")
        except RuntimeError:
            text = "The file name or path is too long."
        else:
            text = (
                "The file name is too long. File names and paths must be shorter "
                f"than {max_name} and {max_path} characters on your file system, "
                f"respectively."
            )
    elif exc.errno == errno.EINVAL:
        err_cls = PathError  # subclass of SyncError
        title = "Could not create local file"
        text = (
            "The file name contains characters which are not allowed on your file "
            "system. This could be for instance a colon or a trailing period."
        )
    elif exc.errno == errno.EFBIG:
        err_cls = FileSizeError  # subclass of SyncError
        title = "Could not download file"
        text = "The file size too large."
    elif exc.errno == errno.ELOOP:
        err_cls = SymlinkError  # subclass of SyncError
        title = "Cannot upload symlink"
        text = "Symlinks are not currently supported by the public remote API."
    elif exc.errno == errno.ENOSPC:
        err_cls = InsufficientSpaceError  # subclass of SyncError
        title = "Could not download file"
        text = "There is not enough space left on the selected drive."
    elif exc.errno == errno.ENOMEM:
        err_cls = OutOfMemoryError  # subclass of CumulusApiError
        text = "Out of memory. Please reduce the number of memory consuming processes."
    elif exc.errno is not None:
        err_cls = FileReadError
        text = f"Could not access file. Errno {exc.errno}: {os.strerror(exc.errno)}."
    else:
        err_cls = CumulusApiError
        text = str(exc)

    local_path = local_path or exc.filename

    cumulus_exc = err_cls(title, text, remote_path=remote_path, local_path=local_path)
    cumulus_exc.__cause__ = exc

    return cumulus_exc


def google_api_to_cumulus_error(
    exc: Exception,
    remote_path: str | None = None,
    local_path: str | None = None,
) -> CumulusApiError:
    """Converts a Google API exception to a :exc:`cumulus.exceptions.CumulusApiError`.

    .. note:: Not yet implemented. Will be completed in Phase 6.

    :param exc: Google API exception.
    :param remote_path: Remote path associated with the error.
    :param local_path: Local path associated with the error.
    :returns: Converted exception.
    """
    raise NotImplementedError(
        "Google API error handling not yet implemented (Phase 6)"
    )
