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


def test_maestral_api_error_uses_remote_path():
    """MaestralApiError should accept remote_path instead of dbx_path."""
    from maestral.exceptions import MaestralApiError

    err = MaestralApiError(
        "title",
        "msg",
        remote_path="/test",
        remote_path_from="/old",
        local_path="/local/test",
        local_path_from="/local/old",
    )
    assert err.remote_path == "/test"
    assert err.remote_path_from == "/old"
    assert err.local_path == "/local/test"
    assert err.local_path_from == "/local/old"


def test_maestral_api_error_dbx_path_compat():
    """Old dbx_path kwargs should still work as aliases."""
    from maestral.exceptions import MaestralApiError

    err = MaestralApiError("title", "msg", dbx_path="/compat", dbx_path_from="/old")
    assert err.remote_path == "/compat"
    assert err.remote_path_from == "/old"
    # Old attribute names should also work
    assert err.dbx_path == "/compat"
    assert err.dbx_path_from == "/old"


def test_sync_event_has_remote_path_field():
    """SyncEvent should use remote_path instead of dbx_path."""
    from maestral.models import SyncEvent

    assert hasattr(SyncEvent, 'remote_path')
    assert hasattr(SyncEvent, 'remote_path_lower')
    assert hasattr(SyncEvent, 'remote_path_from')
    assert hasattr(SyncEvent, 'remote_path_from_lower')


def test_index_entry_has_remote_path_field():
    """IndexEntry should use remote_path instead of dbx_path."""
    from maestral.models import IndexEntry

    assert hasattr(IndexEntry, 'remote_path_lower')
    assert hasattr(IndexEntry, 'remote_path_cased')
    assert hasattr(IndexEntry, 'remote_id')


def test_sync_error_entry_has_remote_path_field():
    """SyncErrorEntry should use remote_path instead of dbx_path."""
    from maestral.models import SyncErrorEntry

    assert hasattr(SyncErrorEntry, 'remote_path')
    assert hasattr(SyncErrorEntry, 'remote_path_lower')
    assert hasattr(SyncErrorEntry, 'remote_path_from')
    assert hasattr(SyncErrorEntry, 'remote_path_from_lower')


def test_core_metadata_has_remote_fields():
    """Core Metadata dataclasses should not reference 'dbx'."""
    from maestral.core import FileMetadata, FolderMetadata, ListFolderResult

    # These are dataclasses with path_lower, path_display etc - no dbx prefix.
    # Just verify they still work.
    import datetime
    fm = FileMetadata(
        name="test.txt",
        path_lower="/test.txt",
        path_display="/test.txt",
        id="id:123",
        client_modified=datetime.datetime.now(),
        server_modified=datetime.datetime.now(),
        rev="abc",
        size=100,
        symlink_target=None,
        shared=False,
        modified_by=None,
        is_downloadable=True,
        content_hash="abc123",
    )
    assert fm.name == "test.txt"


def test_pyproject_has_no_dropbox_dependency():
    """pyproject.toml should not list dropbox as a dependency."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    deps = data["project"]["dependencies"]
    for dep in deps:
        assert not dep.startswith("dropbox"), f"Found dropbox dependency: {dep}"


def test_pyproject_has_google_dependencies():
    """pyproject.toml should list google API dependencies."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    deps = " ".join(data["project"]["dependencies"])
    assert "google-api-python-client" in deps
    assert "google-auth" in deps
    assert "google-auth-oauthlib" in deps


def test_pyproject_description_not_dropbox():
    """Project description should not mention Dropbox."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    desc = data["project"]["description"]
    assert "Dropbox" not in desc
    assert "Google Drive" in desc or "sync" in desc.lower()
