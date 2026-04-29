# type: ignore
# flake8: noqa

import errno

import pytest
from dropbox import exceptions
from dropbox.async_ import *
from dropbox.auth import *
from dropbox.common import *
from dropbox.files import *
from dropbox.sharing import *
from dropbox.users import *

from cumulus.errorhandling import (
    dropbox_to_cumulus_error,
    get_lookup_error_msg,
    get_session_lookup_error_msg,
    get_write_error_msg,
    os_to_cumulus_error,
)
from cumulus.exceptions import (
    BadInputError,
    ConflictError,
    CursorResetError,
    DataCorruptionError,
    AuthError as MaestralAuthError,
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
)
from cumulus.exceptions import PathRootError as MPRE
from cumulus.exceptions import (
    RestrictedContentError,
    SharedLinkError,
    SyncError,
    TokenExpiredError,
    TokenRevokedError,
    UnsupportedFileError,
)


@pytest.mark.parametrize(
    "number,cumulus_exc",
    [
        (errno.EPERM, InsufficientPermissionsError),
        (errno.ENOENT, NotFoundError),
        (errno.EEXIST, ConflictError),
        (errno.EISDIR, IsAFolderError),
        (errno.ENOTDIR, NotAFolderError),
        (errno.ENAMETOOLONG, PathError),
        (errno.EINVAL, PathError),
        (errno.EFBIG, FileSizeError),
        (errno.ENOSPC, InsufficientSpaceError),
        (errno.EFAULT, FileReadError),
        (errno.ENOMEM, OutOfMemoryError),
    ],
)
def test_os_to_cumulus_error(number: int, cumulus_exc):
    os_error = OSError(number, "error")
    converted = os_to_cumulus_error(os_error)
    assert isinstance(converted, cumulus_exc)


@pytest.mark.parametrize(
    "error,cumulus_exc",
    [
        (LookupError.malformed_path(None), PathError),
        (LookupError.not_file, IsAFolderError),
        (LookupError.not_folder, NotAFolderError),
        (LookupError.not_found, NotFoundError),
        (LookupError.restricted_content, RestrictedContentError),
        (LookupError.unsupported_content_type, UnsupportedFileError),
        (LookupError.locked, InsufficientPermissionsError),
    ],
)
def test_get_lookup_error_msg(error, cumulus_exc):
    text, err_cls = get_lookup_error_msg(error)
    assert err_cls is cumulus_exc


@pytest.mark.parametrize(
    "error,cumulus_exc",
    [
        (WriteError.conflict(WriteConflictError.file), FileConflictError),
        (WriteError.conflict(WriteConflictError.folder), FolderConflictError),
        (WriteError.conflict(WriteConflictError.file_ancestor), FileConflictError),
        (WriteError.disallowed_name, PathError),
        (WriteError.insufficient_space, InsufficientSpaceError),
        (WriteError.malformed_path(None), PathError),
        (WriteError.no_write_permission, InsufficientPermissionsError),
        (WriteError.team_folder, SyncError),
        (WriteError.too_many_write_operations, SyncError),
    ],
)
def test_get_write_error_msg(error, cumulus_exc):
    text, err_cls = get_write_error_msg(error)
    assert err_cls is cumulus_exc


@pytest.mark.parametrize(
    "error,cumulus_exc",
    [
        (UploadSessionLookupError.closed, SyncError),
        (
            UploadSessionLookupError.incorrect_offset(UploadSessionOffsetError(20)),
            DataCorruptionError,
        ),
        (UploadSessionLookupError.not_closed, SyncError),
        (UploadSessionLookupError.not_found, SyncError),
        (UploadSessionLookupError.too_large, FileSizeError),
        (UploadSessionAppendError.content_hash_mismatch, DataCorruptionError),
    ],
)
def test_get_session_lookup_error_msg(error, cumulus_exc):
    text, err_cls = get_session_lookup_error_msg(error)
    assert err_cls is cumulus_exc


@pytest.mark.parametrize(
    "error,cumulus_exc",
    [
        (RelocationError.cant_copy_shared_folder, SyncError),
        (RelocationError.cant_move_folder_into_itself, ConflictError),
        (RelocationError.cant_move_shared_folder, SyncError),
        (RelocationError.cant_nest_shared_folder, SyncError),
        (RelocationError.cant_transfer_ownership, SyncError),
        (RelocationError.duplicated_or_nested_paths, SyncError),
        (RelocationError.from_lookup(LookupError.not_found), NotFoundError),
        (RelocationError.from_write(WriteError.team_folder), SyncError),
        (RelocationError.to(WriteError.team_folder), SyncError),
        (RelocationError.insufficient_quota, InsufficientSpaceError),
        (RelocationError.internal_error, ServerError),
        (RelocationError.too_many_files, SyncError),
        (
            RelocationError.cant_move_into_family(MoveIntoFamilyError.is_shared_folder),
            SyncError,
        ),
        (RelocationError.cant_move_into_family(MoveIntoFamilyError.other), SyncError),
        (RelocationError.other, CumulusApiError),
        (CreateFolderError.path(WriteError.team_folder), SyncError),
        (DeleteError.path_lookup(LookupError.not_found), NotFoundError),
        (DeleteError.path_write(WriteError.team_folder), SyncError),
        (DeleteError.too_many_files, SyncError),
        (DeleteError.too_many_write_operations, SyncError),
        (DeleteError.other, CumulusApiError),
        (
            UploadError.path(
                UploadWriteFailed(reason=WriteError.team_folder, upload_session_id="")
            ),
            SyncError,
        ),
        (UploadError.properties_error, CumulusApiError),
        (UploadError.content_hash_mismatch, DataCorruptionError),
        (UploadError.other, CumulusApiError),
        (
            UploadSessionStartError.concurrent_session_close_not_allowed,
            CumulusApiError,
        ),
        (UploadSessionStartError.concurrent_session_data_not_allowed, CumulusApiError),
        (UploadSessionStartError.other, CumulusApiError),
        (
            UploadSessionFinishError.lookup_failed(UploadSessionLookupError.not_found),
            CumulusApiError,
        ),
        (UploadSessionFinishError.path(WriteError.team_folder), SyncError),
        (UploadSessionFinishError.properties_error, CumulusApiError),
        (UploadSessionFinishError.too_many_write_operations, SyncError),
        (UploadSessionFinishError.content_hash_mismatch, DataCorruptionError),
        (UploadSessionFinishError.other, CumulusApiError),
        (UploadSessionLookupError.too_large, FileSizeError),
        (UploadSessionLookupError.other, CumulusApiError),
        (DownloadError.path(LookupError.not_found), NotFoundError),
        (DownloadError.unsupported_file, UnsupportedFileError),
        (DownloadError.other, CumulusApiError),
        (ListFolderError.path(LookupError.not_found), NotFoundError),
        (ListFolderError.other, CumulusApiError),
        (ListFolderContinueError.path(LookupError.not_found), NotFoundError),
        (ListFolderContinueError.reset, CursorResetError),
        (ListFolderContinueError.other, CumulusApiError),
        (ListFolderLongpollError.reset, CursorResetError),
        (ListFolderLongpollError.other, CumulusApiError),
        (PollError.internal_error, ServerError),
        (PollError.other, CumulusApiError),
        (ListRevisionsError.path(LookupError.not_found), NotFoundError),
        (ListRevisionsError.other, CumulusApiError),
        (RestoreError.invalid_revision, NotFoundError),
        (RestoreError.path_lookup(LookupError.not_found), NotFoundError),
        (RestoreError.path_write(WriteError.team_folder), SyncError),
        (RestoreError.in_progress, SyncError),
        (RestoreError.other, CumulusApiError),
        (GetMetadataError.path(LookupError.not_found), NotFoundError),
        (GetAccountError.no_account, InvalidDbidError),
        (GetAccountError.other, CumulusApiError),
        (CreateSharedLinkWithSettingsError.access_denied, InsufficientPermissionsError),
        (CreateSharedLinkWithSettingsError.email_not_verified, SharedLinkError),
        (CreateSharedLinkWithSettingsError.path(LookupError.not_found), NotFoundError),
        (
            CreateSharedLinkWithSettingsError.settings_error(
                SharedLinkSettingsError.invalid_settings
            ),
            SharedLinkError,
        ),
        (
            CreateSharedLinkWithSettingsError.shared_link_already_exists(
                SharedLinkAlreadyExistsMetadata.other
            ),
            SharedLinkError,
        ),
        (RevokeSharedLinkError("shared_link_not_found"), NotFoundError),
        (
            RevokeSharedLinkError("shared_link_access_denied"),
            InsufficientPermissionsError,
        ),
        (RevokeSharedLinkError("shared_link_malformed"), SharedLinkError),
        (RevokeSharedLinkError("other"), CumulusApiError),
        (ListSharedLinksError.path(LookupError.not_found), NotFoundError),
        (ListSharedLinksError.reset, SharedLinkError),
        (ListSharedLinksError.other, CumulusApiError),
        (ShareFolderError.no_permission, InsufficientPermissionsError),
        (ShareFolderError.disallowed_shared_link_policy, InsufficientPermissionsError),
        (
            ShareFolderError.team_policy_disallows_member_policy,
            InsufficientPermissionsError,
        ),
        (ShareFolderError.email_unverified, CumulusApiError),
        (ShareFolderError.bad_path(SharePathError.is_file), FileConflictError),
        (ShareFolderError.bad_path(SharePathError.inside_shared_folder), SyncError),
        (ShareFolderError.bad_path(SharePathError.contains_shared_folder), SyncError),
        (ShareFolderError.bad_path(SharePathError.contains_app_folder), SyncError),
        (ShareFolderError.bad_path(SharePathError.contains_team_folder), SyncError),
        (ShareFolderError.bad_path(SharePathError.is_app_folder), SyncError),
        (ShareFolderError.bad_path(SharePathError.inside_app_folder), SyncError),
        (ShareFolderError.bad_path(SharePathError.is_public_folder), SyncError),
        (ShareFolderError.bad_path(SharePathError.inside_public_folder), SyncError),
        (
            ShareFolderError.bad_path(
                SharePathError.already_shared(SharedFolderMetadata())
            ),
            FolderConflictError,
        ),
        (ShareFolderError.bad_path(SharePathError.invalid_path), SyncError),
        (ShareFolderError.bad_path(SharePathError.is_osx_package), SyncError),
        (ShareFolderError.bad_path(SharePathError.inside_osx_package), SyncError),
        (ShareFolderError.bad_path(SharePathError.is_vault), SyncError),
        (ShareFolderError.bad_path(SharePathError.is_vault_locked), SyncError),
        (ShareFolderError.bad_path(SharePathError.is_family), SyncError),
        (ShareFolderError.other, CumulusApiError),
    ],
)
def test_dropbox_api_to_cumulus_error(error, cumulus_exc):
    converted = dropbox_to_cumulus_error(exceptions.ApiError("", error, "", ""))
    assert isinstance(converted, cumulus_exc), f"{error} was incorrectly converted"


@pytest.mark.parametrize(
    "exception,cumulus_exc",
    [
        (exceptions.AuthError("", AuthError.expired_access_token), TokenExpiredError),
        (exceptions.AuthError("", AuthError.invalid_access_token), TokenRevokedError),
        (exceptions.AuthError("", AuthError.user_suspended), MaestralAuthError),
        (
            exceptions.AuthError(
                "", AuthError.missing_scope(TokenScopeError(required_scope="test"))
            ),
            InsufficientPermissionsError,
        ),
        (exceptions.AuthError("", AuthError.other), CumulusApiError),
        (exceptions.BadInputError("", ""), BadInputError),
        (exceptions.InternalServerError("", "", ""), ServerError),
        (exceptions.PathRootError("", PathRootError.no_permission), MPRE),
        (exceptions.PathRootError("", PathRootError.invalid_root(RootInfo())), MPRE),
        (exceptions.PathRootError("", PathRootError.other), MPRE),
    ],
)
def test_dropbox_to_cumulus_error(exception, cumulus_exc):
    converted = dropbox_to_cumulus_error(exception)
    assert isinstance(converted, cumulus_exc)
