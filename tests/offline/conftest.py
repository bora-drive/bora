import dataclasses
import json
import logging
import sqlite3

import httplib2
import pytest
from googleapiclient.errors import HttpError

from bora.client import GoogleDriveClient
from bora.config import list_configs, remove_configuration
from bora.core import AccountType, FullAccount
from bora.database.core import Database
from bora.path_cache import PathIDCache
from bora.daemon import Stop, stop_cumulus_daemon_process
from bora.fsevents import Observer
from bora.keyring import CredentialStorage
from bora.main import Maestral
from bora.sync import SyncEngine
from bora.utils.path import delete
from tests.fake_drive import ROOT_ID, FakeDrive


def _make_path_cache() -> PathIDCache:
    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = "ROOT"
    return cache


@pytest.fixture
def m():
    m = Maestral("test-config")
    m.log_level = logging.DEBUG
    yield m
    remove_configuration(m.config_name)


_STUB_ACCOUNT = FullAccount(
    account_id="fake-account-id",
    display_name="Fake User",
    email="fake@example.com",
    profile_photo_url=None,
    disabled=False,
)


@pytest.fixture
def sync(tmp_path_factory):
    local_dir = str(tmp_path_factory.mktemp("sync_dir"))

    fake = FakeDrive()
    cache = _make_path_cache()
    cache.root_id = ROOT_ID
    client = GoogleDriveClient("test-config", CredentialStorage("test-config"), cache)
    # Wire the fake Drive service so no real Google API calls are made.
    client._get_service = lambda: fake.service()
    # Pre-populate cached account info to short-circuit _fetch_account_info_for_creds,
    # which calls build() directly (bypassing _get_service) and would hit google.auth.default().
    client._cached_account_info = _STUB_ACCOUNT

    sync = SyncEngine(client)
    sync.fs_events.enable()
    sync.sync_path = local_dir

    observer = Observer()
    observer.schedule(sync.fs_events, sync.sync_path, recursive=True)
    observer.start()

    yield sync

    observer.stop()
    observer.join()

    remove_configuration("test-config")
    delete(sync.sync_path)


@pytest.fixture
def client():
    yield GoogleDriveClient(
        "test-config", CredentialStorage("test-config"), _make_path_cache()
    )
    remove_configuration("test-config")


@pytest.fixture
def config_name(prefix: str = "test-config"):
    i = 0
    config_name = f"{prefix}-{i}"

    while config_name in list_configs():
        i += 1
        config_name = f"{prefix}-{i}"

    yield config_name

    res = stop_cumulus_daemon_process(config_name)

    if res is Stop.Failed:
        raise RuntimeError("Could not stop test daemon")

    remove_configuration(config_name)


@pytest.fixture
def make_http_error():
    """Factory fixture: build a googleapiclient HttpError with a given
    status code and optional Drive sub-reason string."""

    def _make(status: int, reason: str = "", retry_after: str | None = None):
        headers = {"status": status}
        if retry_after is not None:
            headers["retry-after"] = retry_after
        resp = httplib2.Response(headers)
        body: dict = {"error": {"code": status, "message": "test error"}}
        if reason:
            body["error"]["errors"] = [{"reason": reason, "message": "test error"}]
        return HttpError(resp, json.dumps(body).encode("utf-8"))

    return _make


@pytest.fixture
def fake_drive():
    return FakeDrive()


@dataclasses.dataclass
class ClientOnFake:
    client: GoogleDriveClient
    fake: FakeDrive


@pytest.fixture
def client_on_fake(fake_drive):
    cache = _make_path_cache()
    cache.root_id = ROOT_ID
    client = GoogleDriveClient(
        "test-config", CredentialStorage("test-config"), cache
    )
    client._get_service = lambda: fake_drive.service()
    yield ClientOnFake(client=client, fake=fake_drive)
    remove_configuration("test-config")
