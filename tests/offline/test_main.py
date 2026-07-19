import os
import sqlite3
from unittest.mock import PropertyMock, patch

import pytest
import requests

import bora.main
from bora.constants import GITHUB_RELEASES_API
from bora.core import FolderMetadata, ListFolderResult
from bora.exceptions import NotLinkedError
from bora.main import Bora
from bora.models import IndexEntry, ItemType
from bora.path_cache import PathIDCache
from bora.path_codec import encode_path, fold_human, fold_path


def test_check_for_updates(m: Bora) -> None:
    # get current releases from GitHub

    resp = requests.get(GITHUB_RELEASES_API)

    try:
        resp.raise_for_status()
    except Exception:
        # rate limit etc, connection error, etc
        return

    data = resp.json()

    previous_release = data[1]["tag_name"].lstrip("v")
    latest_stable_release = data[0]["tag_name"].lstrip("v")

    # check that no update is offered from current (newest) version

    bora.main.__version__ = latest_stable_release

    update_res = m.check_for_updates()

    assert update_res.latest_release == latest_stable_release
    assert not update_res.update_available
    assert update_res.release_notes == ""

    # check that update is offered from previous release

    bora.main.__version__ = previous_release

    update_res = m.check_for_updates()

    assert update_res.latest_release == latest_stable_release
    assert update_res.update_available
    assert update_res.release_notes != ""


def test_not_linked_error(m: Bora) -> None:
    with pytest.raises(NotLinkedError):
        m.get_metadata("/test")


def test_bora_wires_path_cache_into_client(m: Bora) -> None:
    """Stage 3: the client must receive a PathIDCache from Bora."""
    assert isinstance(m.client._path_cache, PathIDCache)


def test_shutdown_daemon_calls_client_close(m: Bora) -> None:
    """Stage 5: shutdown_daemon must call client.close() before stop_sync()
    so that any in-flight change-feed poll is aborted before workers are joined."""
    call_order: list[str] = []

    with (
        patch.object(m.client, "close", side_effect=lambda: call_order.append("close")),
        patch.object(m, "stop_sync", side_effect=lambda: call_order.append("stop_sync")),
        patch.object(m._client_db, "close", side_effect=lambda: call_order.append("db_close")),
    ):
        m.shutdown_daemon()

    assert "close" in call_order, "client.close() was not called"
    assert "stop_sync" in call_order, "stop_sync() was not called"
    assert call_order.index("close") < call_order.index("stop_sync"), (
        "client.close() must be called before stop_sync()"
    )


def test_shutdown_daemon_closes_sync_db(m: Bora) -> None:
    """shutdown_daemon must close the SyncEngine's index DB connection --
    otherwise it is only ever closed by GC at an arbitrary later time
    (ResourceWarning: unclosed database)."""
    with patch.object(m, "stop_sync"):
        m.shutdown_daemon()

    with pytest.raises(sqlite3.ProgrammingError):
        m.sync._db.execute("SELECT 1")


def test_shutdown_daemon_closes_sync_db_after_stop_sync(m: Bora) -> None:
    """The sync DB must only be closed AFTER stop_sync() has joined the sync
    workers -- they read and write the index while running."""
    call_order: list[str] = []

    with (
        patch.object(m, "stop_sync", side_effect=lambda: call_order.append("stop_sync")),
        patch.object(m.sync, "close", side_effect=lambda: call_order.append("sync_close")),
    ):
        m.shutdown_daemon()

    assert "sync_close" in call_order, "sync.close() was not called"
    assert call_order.index("stop_sync") < call_order.index("sync_close"), (
        "sync.close() must be called after stop_sync()"
    )


# ==== excluded-items pipeline: fold_human form throughout =============================
#
# excluded_items entries are stored in fold_human form (decoded human path,
# per-segment NFC(casefold(NFC))). Every comparison against the stored entries
# must use the same form; every call into engine APIs (index, sync errors,
# client, download queue) must use the encoded virtual-path form.


def _bypass_link_checks():
    """exclude/include_items guard on a linked account and a sync dir; neither
    exists in the offline harness, and neither is needed by the code under test.

    Also stubs ``Bora.pending_link`` to False: in production, exclude/include_items
    only reach their body after ``_check_linked()`` has confirmed the account is
    linked, so ``Bora.sync_path`` (which returns "" while ``pending_link`` is
    True) always sees a real path there. Without this, offline tests that never
    actually link an account would see ``self.sync_path`` silently collapse to
    "" inside ``_remove_after_excluded``, masking real local-path handling."""
    return (
        patch.object(Bora, "_check_linked"),
        patch.object(Bora, "_check_sync_dir"),
        patch.object(Bora, "pending_link", new_callable=PropertyMock, return_value=False),
    )


def test_include_items_casefold_divergent_name(m: Bora) -> None:
    """exclude → include round-trip for a casefold-divergent name.

    'Straße' is stored as '/strasse' (fold_human: casefold maps ß → ss), but
    normalize() yields '/straße' (lower keeps ß). If include_items queries
    with the normalize() form, re-inclusion is a silent no-op."""
    m.sync.excluded_items = ["/Straße"]
    assert m.sync.excluded_items == {"/strasse"}

    check_linked, check_sync_dir, no_pending_link = _bypass_link_checks()
    with check_linked, check_sync_dir, no_pending_link:
        m.include_items("/Straße")

    assert m.sync.excluded_items == set()


def test_include_items_literal_percent_name(m: Bora) -> None:
    """A name containing a literal percent-escape ('a%20b') must survive the
    round-trip: it is stored verbatim in fold_human form and must not be
    unquoted by any comparison along the way."""
    m.sync.excluded_items = ["/a%20b"]
    assert m.sync.excluded_items == {"/a%20b"}

    check_linked, check_sync_dir, no_pending_link = _bypass_link_checks()
    with check_linked, check_sync_dir, no_pending_link:
        m.include_items("/a%20b")

    assert m.sync.excluded_items == set()


def test_exclude_items_removes_index_entries_by_fold_path_key(m: Bora) -> None:
    """_remove_after_excluded must key index housekeeping by the fold_path
    (encoded) form — the index's remote_path_lower — not the fold_human form."""
    encoded = encode_path("/Ördner/datei.txt")
    folded = fold_path(encoded)  # '/%C3%B6rdner/datei.txt'
    entry = IndexEntry(
        remote_path_cased=encoded,
        remote_path_lower=folded,
        remote_id="id:seed",
        item_type=ItemType.File,
        last_sync=0.0,
        rev="rev-1",
        content_hash=None,
        symlink_target=None,
        read_only=0,
    )
    with m.sync._database_access():
        m.sync._index_table.update(entry)

    check_linked, check_sync_dir, no_pending_link = _bypass_link_checks()
    with check_linked, check_sync_dir, no_pending_link:
        m.exclude_items("/Ördner")

    assert m.sync.excluded_items == {"/ördner"}
    assert m.sync.get_index_entry(folded) is None, (
        "index entry keyed by fold_path form was not removed on exclusion"
    )


def test_include_items_readds_children_in_fold_human_form(m: Bora) -> None:
    """Including a child of an excluded folder re-adds its siblings to
    excluded_items. The metadata's path_lower is in fold_path (encoded) form
    and must be decoded before insertion, and the client must be queried with
    the encoded form of the excluded folder."""
    m.sync.excluded_items = ["/Übergeordnet"]
    assert m.sync.excluded_items == {"/übergeordnet"}

    def _folder(human_path: str) -> FolderMetadata:
        encoded = encode_path(human_path)
        return FolderMetadata(
            name=human_path.rsplit("/", 1)[-1],
            path_lower=fold_path(encoded),
            path_display=encoded,
            id=f"id:{human_path}",
            shared=False,
        )

    listing = ListFolderResult(
        entries=[_folder("/Übergeordnet/Ördner (2)"), _folder("/Übergeordnet/Kind")],
        has_more=False,
        cursor="",
    )

    check_linked, check_sync_dir, no_pending_link = _bypass_link_checks()
    with (
        check_linked,
        check_sync_dir,
        no_pending_link,
        patch.object(
            m.client, "list_folder_iterator", return_value=iter([listing])
        ) as mock_list,
    ):
        m.include_items("/Übergeordnet/Kind")

    # The client speaks encoded virtual paths.
    mock_list.assert_called_once_with(encode_path("/übergeordnet"))
    # The sibling is re-added in fold_human (decoded) form, not as the dead
    # encoded entry '/übergeordnet/%c3%b6rdner %282%29'.
    assert m.sync.excluded_items == {"/übergeordnet/ördner (2)"}


def test_exclude_shared_with_me_subfolder_removes_index_and_local(
    m: Bora, tmp_path
) -> None:
    """Guard test: '/Shared with me/...' paths are ordinary paths as far as
    selective sync is concerned. No new config surface is needed for
    excluding a shared item — the existing exclude_items pipeline must prune
    both the index row and the local tree exactly as it does for any other
    remote path, via the same fold_path(encode_path(fold_human(...))) bridge
    documented on _remove_after_excluded."""
    m.sync.sync_path = str(tmp_path)

    remote_path = "/Shared with me/Team"
    local_path = os.path.join(m.sync.sync_path, "Shared with me", "Team")
    os.makedirs(local_path, exist_ok=True)

    encoded = encode_path(remote_path)
    folded = fold_path(encoded)
    entry = IndexEntry(
        remote_path_cased=encoded,
        remote_path_lower=folded,
        remote_id="id:shared-team",
        item_type=ItemType.Folder,
        last_sync=0.0,
        rev="rev-1",
        content_hash=None,
        symlink_target=None,
        read_only=1,
    )
    with m.sync._database_access():
        m.sync._index_table.update(entry)

    check_linked, check_sync_dir, no_pending_link = _bypass_link_checks()
    with check_linked, check_sync_dir, no_pending_link:
        m.exclude_items(remote_path)

    assert m.sync.excluded_items == {fold_human(remote_path)}
    assert not os.path.exists(local_path), (
        "local tree under 'Shared with me' was not deleted on exclusion"
    )
    assert m.sync.get_index_entry(folded) is None, (
        "index entry for the shared item was not removed on exclusion"
    )


def test_exclude_shared_with_me_root_disables_whole_feature(m: Bora, tmp_path) -> None:
    """Excluding the '/Shared with me' virtual root itself must be treated
    like excluding any other top-level folder: it disables sync of
    everything below it, and removes the local mount point along with any
    already-materialized children."""
    m.sync.sync_path = str(tmp_path)

    local_root = os.path.join(m.sync.sync_path, "Shared with me")
    local_child = os.path.join(local_root, "Team")
    os.makedirs(local_child, exist_ok=True)

    encoded = encode_path("/Shared with me/Team")
    folded = fold_path(encoded)
    entry = IndexEntry(
        remote_path_cased=encoded,
        remote_path_lower=folded,
        remote_id="id:shared-team",
        item_type=ItemType.Folder,
        last_sync=0.0,
        rev="rev-1",
        content_hash=None,
        symlink_target=None,
        read_only=1,
    )
    with m.sync._database_access():
        m.sync._index_table.update(entry)

    check_linked, check_sync_dir, no_pending_link = _bypass_link_checks()
    with check_linked, check_sync_dir, no_pending_link:
        m.exclude_items("/Shared with me")

    assert m.sync.excluded_items == {"/shared with me"}
    assert not os.path.exists(local_root), (
        "'Shared with me' mount point was not removed when the root was excluded"
    )
    assert m.sync.get_index_entry(folded) is None, (
        "index entry for the shared child was not removed when the root was excluded"
    )
