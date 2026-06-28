"""
This module defines Maestral's error classes. It should be kept free of memory heavy
imports.

All errors inherit from :exc:`CumulusApiError` which has title and message attributes
to display the error to the user. Errors which are related to syncing a specific
file or folder inherit from :exc:`SyncError`, a subclass of :exc:`CumulusApiError`.
"""

from __future__ import annotations


class CumulusApiError(Exception):
    """Base class for Maestral errors

    CumulusApiError provides attributes that can be used to generate human-readable
    error messages and metadata regarding affected file paths (if any).

    Errors originating from the remote API or the 'local API' both inherit from
    CumulusApiError.

    :param title: A short description of the error type. This can be used in a CLI or
        GUI to give a short error summary.
    :param message: A more verbose description which can include instructions on how to
        proceed to fix the error.
    :param remote_path: Remote path of the item that caused the error.
    :param remote_path_from: Remote origin path of the item that caused the error.
        This should be set for instance when error occurs when moving an item.
    :param local_path: Local path of the item that caused the error.
    :param local_path_from: Local origin path of the item that caused the error.
        This should be set for instance when error occurs when moving an item.
    """

    def __init__(
        self,
        title: str,
        message: str = "",
        remote_path: str | None = None,
        remote_path_from: str | None = None,
        local_path: str | None = None,
        local_path_from: str | None = None,
        # Backward compat aliases:
        dbx_path: str | None = None,
        dbx_path_from: str | None = None,
    ) -> None:
        super().__init__(f"{title}. {message}")
        self.title = title
        self.message = message
        self.remote_path = remote_path or dbx_path
        self.remote_path_from = remote_path_from or dbx_path_from
        self.local_path = local_path
        self.local_path_from = local_path_from

    @property
    def dbx_path(self) -> str | None:
        """Backward compat alias for remote_path."""
        return self.remote_path

    @dbx_path.setter
    def dbx_path(self, value: str | None) -> None:
        self.remote_path = value

    @property
    def dbx_path_from(self) -> str | None:
        """Backward compat alias for remote_path_from."""
        return self.remote_path_from

    @dbx_path_from.setter
    def dbx_path_from(self, value: str | None) -> None:
        self.remote_path_from = value

    def __str__(self) -> str:
        return f"{self.title}. {self.message}"


# ==== regular sync errors =============================================================


class SyncError(CumulusApiError):
    """Base class for recoverable sync issues."""


class DataCorruptionError(SyncError):
    """Raised when data is corrupted in transit during upload or download."""


class DataChangedError(SyncError):
    """Raised when file changes during upload."""


class InsufficientPermissionsError(SyncError):
    """Raised when accessing a file or folder fails due to insufficient permissions,
    both locally and on the remote."""


class InsufficientSpaceError(SyncError):
    """Raised when the remote account or local drive has insufficient storage space."""


class PathError(SyncError):
    """Raised when there is an issue with the provided file or folder path such as
    invalid characters or a too long file name."""


class NotFoundError(SyncError):
    """Raised when a file or folder is requested but does not exist."""


class ConflictError(SyncError):
    """Raised when trying to create a file or folder which already exists."""


class FileConflictError(ConflictError):
    """Raised when trying to create a file which already exists."""


class FolderConflictError(SyncError):
    """Raised when trying to create or folder which already exists."""


class IsAFolderError(SyncError):
    """Raised when a file is required but a folder is provided."""


class NotAFolderError(SyncError):
    """Raised when a folder is required but a file is provided."""


class ServerError(SyncError):
    """Raised in case of internal server errors."""


DropboxServerError = ServerError  # backward compat


class RestrictedContentError(SyncError):
    """Raised when trying to sync restricted content, for instance when adding a file
    with a DMCA takedown notice to a public folder."""


class UnsupportedFileError(SyncError):
    """Raised when this file type cannot be downloaded but only exported. This is the
    case for certain cloud-native file types."""


class FileSizeError(SyncError):
    """Raised when attempting to upload a file larger than 350 GB in an upload session
    or larger than 150 MB in a single upload. Also raised when attempting to download a
    file with a size that exceeds file system's limit."""


class FileReadError(SyncError):
    """Raised when reading a local file failed."""


class SymlinkError(SyncError):
    """Raised when we cannot sync a symlink."""


# ==== errors which are not related to a specific sync event ===========================


class ConnectionError(CumulusApiError):
    """Raised when the connection to the remote server fails."""


DropboxConnectionError = ConnectionError  # backward compat


class CancelledError(CumulusApiError):
    """Raised when syncing is cancelled by the user."""


class NotLinkedError(CumulusApiError):
    """Raised when no remote account is linked."""


class InvalidDbidError(CumulusApiError):
    """Raised when the given ID does not correspond to an existing account."""


class KeyringAccessError(CumulusApiError):
    """Raised when retrieving a saved auth token from the user keyring fails."""


class NoSyncDirError(CumulusApiError):
    """Raised when the local sync folder cannot be found."""


NoDropboxDirError = NoSyncDirError  # backward compat


class CacheDirError(CumulusApiError):
    """Raised when creating the cache directory fails."""


class InotifyError(CumulusApiError):
    """Raised when the local sync folder is too large to monitor with inotify."""


class OutOfMemoryError(CumulusApiError):
    """Raised when there is insufficient memory to complete an operation."""


class DatabaseError(CumulusApiError):
    """Raised when reading or writing to the database fails."""


class AuthError(CumulusApiError):
    """Raised when authentication fails."""


DropboxAuthError = AuthError  # backward compat


class TokenExpiredError(AuthError):
    """Raised when authentication fails because the user's token has expired."""


class TokenRevokedError(AuthError):
    """Raised when authentication fails because the user's token has been revoked."""


class CursorResetError(CumulusApiError):
    """Raised when the cursor used for a longpoll or list-folder request has been
    invalidated. The server will very rarely invalidate a cursor. If this happens, a new
    cursor for the respective folder has to be obtained through files_list_folder. This
    may require re-syncing the entire sync folder."""


class BadInputError(CumulusApiError):
    """Raised when an API request is made with bad input. This should not happen
    during syncing but only in case of manual API calls."""


class BusyError(CumulusApiError):
    """Raised when trying to perform an action which is only possible in the idle
    state and we cannot block or queue the job."""


class UnsupportedFileTypeForDiff(CumulusApiError):
    """Raised when a diff for an unsupported file type was issued."""


class SharedLinkError(CumulusApiError):
    """Raised when creating a shared link fails."""


class PathRootError(CumulusApiError):
    """Raised when making an API call with an invalid path root header."""


class UpdateCheckError(CumulusApiError):
    """Raised when checking for updates fails."""
