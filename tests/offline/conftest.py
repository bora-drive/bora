import json
import logging
import os
import os.path as osp
import sqlite3

import httplib2
import pytest
from googleapiclient.errors import HttpError

from cumulus.client import DropboxClient
from cumulus.config import list_configs, remove_configuration
from cumulus.database.core import Database
from cumulus.path_cache import PathIDCache
from cumulus.daemon import Stop, stop_cumulus_daemon_process
from cumulus.fsevents import Observer
from cumulus.keyring import CredentialStorage
from cumulus.main import Maestral
from cumulus.sync import SyncEngine
from cumulus.utils.appdirs import get_home_dir
from cumulus.utils.path import delete


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


@pytest.fixture
def sync():
    local_dir = osp.join(get_home_dir(), "dummy_dir")
    os.mkdir(local_dir)

    sync = SyncEngine(DropboxClient("test-config", CredentialStorage("test-config"), _make_path_cache()))
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
    yield DropboxClient(
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
