import logging
import os
import sqlite3

import pytest

from bora.client import GoogleDriveClient
from bora.config import remove_configuration
from bora.database.core import Database
from bora.exceptions import AuthError, NotFoundError
from bora.keyring import CredentialStorage
from bora.path_cache import PathIDCache

from tests.linked.conftest import TEST_CONFIG, _make_client

fsevents_logger = logging.getLogger("fsevents")
fsevents_logger.setLevel(logging.DEBUG)


def clean_drive_dir(c: GoogleDriveClient, lock_name: str) -> None:
    """Remove all items at the Drive root except the lock sentinel file."""
    try:
        result = c.list_folder("/", recursive=False)
    except NotFoundError:
        return
    for entry in result.entries:
        # Skip the lock sentinel so the session lock is preserved.
        if getattr(entry, "name", None) == lock_name:
            continue
        try:
            c.remove(entry.path_lower)
        except NotFoundError:
            pass


@pytest.fixture
def client(test_lock):
    """Returns a GoogleDriveClient instance linked to the test account."""
    config_name = "bora-test-config-unit"

    c, cred_storage = _make_client(config_name)

    refresh_token = os.environ["BORA_TEST_REFRESH_TOKEN"]
    access_token = os.environ.get("BORA_TEST_ACCESS_TOKEN")
    res = c.link(refresh_token=refresh_token, access_token=access_token)

    if res == 1:
        raise AuthError("Invalid token")
    elif res == 2:
        raise ConnectionError("Could not connect to Google Drive")
    elif res > 0:
        raise RuntimeError(f"[error {res}] linking failed")

    clean_drive_dir(c, test_lock._name)
    yield c
    clean_drive_dir(c, test_lock._name)

    remove_configuration(config_name)
    cred_storage.delete_creds()
