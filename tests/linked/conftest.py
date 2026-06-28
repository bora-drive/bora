import os
import sqlite3
import time

import pytest

from bora.client import GoogleDriveClient
from bora.config import remove_configuration
from bora.database.core import Database
from bora.exceptions import AuthError
from bora.keyring import CredentialStorage
from bora.path_cache import PathIDCache

from .lock import DriveTestLock

TEST_CONFIG = "cumulus-test-config"
LOCK_NAME = ".cumulus-test.lock"

_HAS_CREDS = bool(os.environ.get("CUMULUS_TEST_REFRESH_TOKEN"))
requires_account = pytest.mark.skipif(
    not _HAS_CREDS, reason="set CUMULUS_TEST_REFRESH_TOKEN to run linked tests"
)


def wait_until(predicate, timeout=60, interval=2):
    """Poll *predicate* until truthy or *timeout* seconds elapse.

    Drive's changes.list lags a few seconds behind a mutation, so linked
    assertions must poll rather than assert once."""
    deadline = timeout
    while deadline > 0:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
        deadline -= interval
    raise AssertionError(f"condition not met within {timeout}s")


def _make_client(config_name: str) -> tuple[GoogleDriveClient, CredentialStorage]:
    """Build a GoogleDriveClient with an in-memory path cache for linked tests."""
    cred_storage = CredentialStorage(config_name)
    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    client = GoogleDriveClient(config_name, cred_storage, cache)
    return client, cred_storage


@pytest.fixture(scope="session", autouse=True)
def test_lock():
    if not _HAS_CREDS:
        pytest.skip("no CUMULUS_TEST_REFRESH_TOKEN")
    c, cred_storage = _make_client(TEST_CONFIG)
    refresh_token = os.environ["CUMULUS_TEST_REFRESH_TOKEN"]
    access_token = os.environ.get("CUMULUS_TEST_ACCESS_TOKEN")
    res = c.link(refresh_token=refresh_token, access_token=access_token)
    if res > 0:
        raise AuthError(f"linking test account failed (code {res})")
    lock = DriveTestLock(c, LOCK_NAME)
    if not lock.acquire():
        raise TimeoutError("Could not acquire Drive test lock")
    yield lock
    lock.release()
    remove_configuration(TEST_CONFIG)
    cred_storage.delete_creds()
