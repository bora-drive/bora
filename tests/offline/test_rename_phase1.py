"""Tests for Phase 1 renaming: exception classes should have backend-agnostic names.

These tests verify that the old Dropbox-prefixed names are replaced with
new generic names, and that the old names still work as aliases for
backward compatibility.
"""

import pytest


def test_server_error_exists():
    from maestral.exceptions import ServerError
    assert issubclass(ServerError, Exception)


def test_connection_error_exists():
    from maestral.exceptions import ConnectionError
    assert issubclass(ConnectionError, Exception)


def test_auth_error_exists():
    from maestral.exceptions import AuthError
    assert issubclass(AuthError, Exception)


def test_no_sync_dir_error_exists():
    from maestral.exceptions import NoSyncDirError
    assert issubclass(NoSyncDirError, Exception)


def test_old_names_still_importable():
    """Backward compat: old names should still be importable as aliases."""
    from maestral.exceptions import DropboxServerError, DropboxConnectionError
    from maestral.exceptions import DropboxAuthError, NoDropboxDirError
    from maestral.exceptions import ServerError, ConnectionError
    from maestral.exceptions import AuthError, NoSyncDirError

    assert DropboxServerError is ServerError
    assert DropboxConnectionError is ConnectionError
    assert DropboxAuthError is AuthError
    assert NoDropboxDirError is NoSyncDirError


def test_token_errors_inherit_from_auth_error():
    from maestral.exceptions import AuthError, TokenExpiredError, TokenRevokedError
    assert issubclass(TokenExpiredError, AuthError)
    assert issubclass(TokenRevokedError, AuthError)
