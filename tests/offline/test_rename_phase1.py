"""Tests for Phase 1 renaming: exception classes should have backend-agnostic names.

These tests verify that the old Dropbox-prefixed names are replaced with
new generic names. The transitional backward-compat aliases (DropboxClient,
Dropbox*Error, dbx_path) were removed in Stage 8.
"""

def test_server_error_exists():
    from bora.exceptions import ServerError
    assert issubclass(ServerError, Exception)


def test_connection_error_exists():
    from bora.exceptions import ConnectionError
    assert issubclass(ConnectionError, Exception)


def test_auth_error_exists():
    from bora.exceptions import AuthError
    assert issubclass(AuthError, Exception)


def test_no_sync_dir_error_exists():
    from bora.exceptions import NoSyncDirError
    assert issubclass(NoSyncDirError, Exception)


def test_token_errors_inherit_from_auth_error():
    from bora.exceptions import AuthError, TokenExpiredError, TokenRevokedError
    assert issubclass(TokenExpiredError, AuthError)
    assert issubclass(TokenRevokedError, AuthError)


def test_bora_api_error_uses_remote_path():
    """BoraApiError should accept remote_path instead of dbx_path."""
    from bora.exceptions import BoraApiError

    err = BoraApiError(
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


def test_sync_event_has_remote_path_field():
    """SyncEvent should use remote_path instead of dbx_path."""
    from bora.models import SyncEvent

    assert hasattr(SyncEvent, 'remote_path')
    assert hasattr(SyncEvent, 'remote_path_lower')
    assert hasattr(SyncEvent, 'remote_path_from')
    assert hasattr(SyncEvent, 'remote_path_from_lower')


def test_index_entry_has_remote_path_field():
    """IndexEntry should use remote_path instead of dbx_path."""
    from bora.models import IndexEntry

    assert hasattr(IndexEntry, 'remote_path_lower')
    assert hasattr(IndexEntry, 'remote_path_cased')
    assert hasattr(IndexEntry, 'remote_id')


def test_sync_error_entry_has_remote_path_field():
    """SyncErrorEntry should use remote_path instead of dbx_path."""
    from bora.models import SyncErrorEntry

    assert hasattr(SyncErrorEntry, 'remote_path')
    assert hasattr(SyncErrorEntry, 'remote_path_lower')
    assert hasattr(SyncErrorEntry, 'remote_path_from')
    assert hasattr(SyncErrorEntry, 'remote_path_from_lower')


def test_core_metadata_has_remote_fields():
    """Core Metadata dataclasses should not reference 'dbx'."""
    from bora.core import FileMetadata, FolderMetadata, ListFolderResult

    # These are dataclasses with path_lower, path_display etc - no dbx prefix.
    # Just verify they still work.
    import datetime
    fm = FileMetadata(
        name="test.txt",
        path_lower="/test.txt",
        path_display="/test.txt",
        id="id:123",
        modified=datetime.datetime.now(),
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


def test_constants_no_dropbox_app_key():
    """constants should not have a DROPBOX_APP_KEY."""
    from bora import constants
    assert not hasattr(constants, 'DROPBOX_APP_KEY')


def test_exceptions_docstrings_no_dropbox():
    """Exception docstrings should not reference Dropbox."""
    import bora.exceptions as exc
    for name in dir(exc):
        obj = getattr(exc, name)
        if isinstance(obj, type) and issubclass(obj, Exception):
            doc = obj.__doc__ or ""
            assert "Dropbox" not in doc, f"{name}.__doc__ still mentions Dropbox"


def test_package_importable_as_bora():
    """The package should be importable as 'bora'."""
    import bora
    assert hasattr(bora, '__version__')


def test_bora_cli_entry_point():
    """The CLI entry point should be 'bora'."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    scripts = data["project"]["scripts"]
    assert "bora" in scripts
    assert "bora.cli" in scripts["bora"]


def test_bora_constants():
    """APP_NAME should be Bora."""
    from bora.constants import APP_NAME
    assert APP_NAME == "Bora"


def test_bora_config_dir():
    """Config directory should use 'bora' not 'maestral'."""
    from bora.config.main import CONFIG_DIR_NAME
    assert CONFIG_DIR_NAME == "bora"
