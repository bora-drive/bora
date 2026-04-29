import logging
import os
import os.path as osp

import pytest

from cumulus.client import DropboxClient
from cumulus.config import list_configs, remove_configuration
from cumulus.daemon import Stop, stop_cumulus_daemon_process
from cumulus.fsevents import Observer
from cumulus.keyring import CredentialStorage
from cumulus.main import Maestral
from cumulus.sync import SyncEngine
from cumulus.utils.appdirs import get_home_dir
from cumulus.utils.path import delete


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

    sync = SyncEngine(DropboxClient("test-config", CredentialStorage("test-config")))
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
    yield DropboxClient("test-config", CredentialStorage("test-config"))
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
