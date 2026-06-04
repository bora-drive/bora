import os
from unittest import mock

import pytest

from cumulus.core import AccountType, FullAccount
from cumulus.exceptions import NoSyncDirError
from cumulus.main import Maestral
from cumulus.utils.appdirs import get_home_dir
from cumulus.utils.path import delete, generate_cc_name


def fake_linked(m: Maestral, account_info: FullAccount) -> None:
    m.client.get_account_info = mock.Mock(return_value=account_info)  # type: ignore
    m.cred_storage.save_creds("account_id", "1234")


def verify_folder_structure(root: str, structure: dict) -> None:
    for name, children in structure.items():
        path = os.path.join(root, name)
        assert os.path.exists(path)

        verify_folder_structure(path, children)


def create_folder_structure(root: str, structure: dict) -> None:
    for name, children in structure.items():
        path = os.path.join(root, name)
        os.makedirs(path)

        create_folder_structure(path, children)


account_info = FullAccount(
    account_id="",
    display_name="",
    email="",
    profile_photo_url=None,
    disabled=False,
)

# NOTE: All four path-root / team-namespace migration tests that previously
# lived here (test_migrate_path_root_user_to_team, _team_to_user,
# _team_to_team, _error) have been deleted.  They exercised
# check_and_update_path_root() / migrate_path_root(), which were removed in
# Stage 6 when all Dropbox path-root / team-namespace code was dropped from
# manager.py.  There is nothing left to test in this file for now; new Drive-
# specific manager tests will be added in later stages.
