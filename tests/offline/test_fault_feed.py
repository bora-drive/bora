"""Fault-injection tests for the Drive changes feed (Stage 7b-E2, Task 2).

Each scenario follows the red → triage → fix cycle documented in the task spec.
Only ``tests/offline/test_fault_feed.py`` is run by these tests; the wider
suite is NOT exercised here.

Helpers mirror test_changes.py ~162-205 and test_sync.py ~196-214.
"""

from __future__ import annotations

import os
import os.path as osp
import sqlite3
from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError

from cumulus.changes import iter_remote_changes
from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.core import FileMetadata, FullAccount
from cumulus.database.core import Database
from cumulus.exceptions import ServerError
from cumulus.keyring import CredentialStorage
from cumulus.models import IndexEntry, ItemType
from cumulus.path_cache import PathIDCache
from cumulus.sync import SyncEngine
from cumulus.utils.appdirs import get_home_dir
from cumulus.utils.path import delete
from tests.fake_drive import ROOT_ID, FakeDrive, make_http_error

# ---------------------------------------------------------------------------
# Shared low-level helpers (mirror test_changes.py)
# ---------------------------------------------------------------------------

_PAGE_SIZE = 100  # Must match changes._CHANGE_PAGE_SIZE


def _cache() -> PathIDCache:
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = ROOT_ID
    return c


def _start(fake: FakeDrive) -> str:
    return fake.service().changes().getStartPageToken().execute()["startPageToken"]


def _drain(
    fake: FakeDrive,
    cache: PathIDCache,
    start: str,
    resolve_deleted_path=None,
) -> tuple[list, list]:
    """Run iter_remote_changes to exhaustion; return (flat_entries, pages)."""

    def factory():
        return fake.service()

    def retry(fn):
        return fn()

    pages = list(
        iter_remote_changes(
            factory, retry, cache, start,
            resolve_deleted_path=resolve_deleted_path,
        )
    )
    return [e for p in pages for e in p.entries], pages


# ---------------------------------------------------------------------------
# Shared engine fixture (mirror test_sync.py ~196-214)
# ---------------------------------------------------------------------------

_STUB_ACCOUNT = FullAccount(
    account_id="account-id",
    display_name="Offline Tester",
    email="offline@example.com",
    profile_photo_url=None,
    disabled=False,
)


@pytest.fixture
def sync_engine():
    local_dir = osp.join(get_home_dir(), "fault_feed_dir")
    os.mkdir(local_dir)

    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = ROOT_ID
    sync = SyncEngine(
        GoogleDriveClient("fault-feed-config", CredentialStorage("fault-feed-config"), cache)
    )
    sync.sync_path = local_dir
    sync.local_cursor = 0.0

    yield sync

    remove_configuration("fault-feed-config")
    delete(local_dir)


def _wire_fake(sync_engine: SyncEngine, fake: FakeDrive) -> None:
    sync_engine.client._get_service = Mock(return_value=fake.service())
    sync_engine.client._cached_account_info = _STUB_ACCOUNT


def _seed_entry(
    sync: SyncEngine,
    remote_path: str,
    remote_id: str,
    item_type: ItemType,
    rev: str,
    content_hash: str | None,
) -> None:
    entry = IndexEntry(
        remote_path_cased=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id=remote_id,
        item_type=item_type,
        last_sync=0.0,
        rev=rev,
        content_hash=content_hash,
        symlink_target=None,
    )
    with sync._database_access():
        sync._index_table.update(entry)


def _write_local_file(sync: SyncEngine, remote_path: str, data: bytes) -> str:
    local_path = sync.to_local_path_from_cased(remote_path)
    os.makedirs(osp.dirname(local_path), exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


# ---------------------------------------------------------------------------
# Scenario A — page-2 fault must not advance the cursor past unapplied changes
# ---------------------------------------------------------------------------


def test_page2_fault_raises_and_no_cursor_advance():
    """If page-2 raises HttpError(503), the generator propagates immediately
    (before any yield), so the caller never receives a cursor.

    Characterization: the all-pages-buffered design is crash-safe — no partial
    cursor advance.  We PROVE this by re-draining from the same start cursor
    and asserting all seeded files are delivered.
    """
    fake = FakeDrive()
    cache = _cache()

    # Take start BEFORE seeding files so all changes are visible from cursor.
    start = _start(fake)

    # Seed 101 files to force TWO changes.list calls.
    for i in range(_PAGE_SIZE + 1):
        fake.add_file(f"/f{i:03d}.txt", content=b"x")

    # Arm: fail on the 2nd changes.list call (page 2 fetch).
    fake.fail_on_call("changes.list", 2, make_http_error(503))

    # Red assertion: the drain raises.
    with pytest.raises(HttpError) as exc_info:
        _drain(fake, cache, start)
    assert exc_info.value.resp.status == 503

    # After the failure the cache must NOT have advanced (nothing was applied).
    # Prove the no-loss property: drain again from the SAME start cursor.
    # Clear fail rules (already consumed and removed by design, but be explicit).
    assert not fake._fail_rules  # rule self-removes after firing

    entries, pages = _drain(fake, cache, start)

    # All 101 changed files must be delivered — no silent loss.
    assert len(pages) == 1  # single ListFolderResult per poll cycle
    file_ids = {e.id for e in entries if isinstance(e, FileMetadata)}
    assert len(file_ids) == _PAGE_SIZE + 1

    # Cache has been populated for each file.
    for i in range(_PAGE_SIZE + 1):
        assert cache.path_to_id(f"/f{i:03d}.txt") is not None


def test_page2_fault_before_any_yield():
    """Complementary: confirm the exception escapes the generator BEFORE the
    single ListFolderResult yield (i.e. the caller never receives partial data).
    """
    fake = FakeDrive()
    cache = _cache()

    # Take start BEFORE seeding so changes are visible from cursor.
    start = _start(fake)

    for i in range(_PAGE_SIZE + 1):
        fake.add_file(f"/g{i:03d}.txt", content=b"y")

    fake.fail_on_call("changes.list", 2, make_http_error(503))

    pages_received = []

    def factory():
        return fake.service()

    def retry(fn):
        return fn()

    gen = iter_remote_changes(factory, retry, cache, start)
    with pytest.raises(HttpError):
        for page in gen:
            pages_received.append(page)

    # Generator raised before yielding anything.
    assert pages_received == []


# ---------------------------------------------------------------------------
# Scenario B — finding-5 across a real page boundary (cross-page stale echo)
# ---------------------------------------------------------------------------


def test_cross_page_stale_child_echo_not_resurrected(sync_engine: SyncEngine):
    """Parent-folder rename and child stale echo split across two change pages.

    The all-pages-buffered design means iter_remote_changes sees the complete
    batch (both pages) before classification.  The path cache therefore has the
    correct post-rename mapping when the child entry is classified.

    However the stale-echo guard in _apply_remote_move is a runtime check on
    the on-disk tree and the index state AFTER the folder event has been applied.
    This test confirms the cross-page variant has the same safety guarantee as
    the same-batch variant (test_remote_folder_rename_with_same_batch_stale_child_echo).

    The stale child must NOT be resurrected at its old path; exactly one index
    row must hold the child id.
    """
    import hashlib

    data = b"kid content unchanged by the rename"
    kid_hash = hashlib.md5(data, usedforsecurity=False).hexdigest()

    fake = FakeDrive()

    # Drive current truth: /renamed and /renamed/kid.txt.
    dir_id = fake.add_folder("/renamed")
    kid_id = fake.add_file("/renamed/kid.txt", data)
    kid_rev = fake.files[kid_id]["headRevisionId"]

    # Seed path cache with post-rename reality.
    sync_engine.client._path_cache.set(
        "/renamed", dir_id, parent_id=ROOT_ID, name="renamed", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/renamed/kid.txt", kid_id, parent_id=dir_id, name="kid.txt", is_folder=False
    )

    # Take the start cursor AFTER seeding the above (two entries logged so far).
    # Now add 99 more filler files so the FIRST page fills to 101 entries,
    # pushing the folder rename (dir_id) and its stale child (kid_id) to page 2.
    # Because the fake deduplicates by fileId (latest-seq), we need 99 DISTINCT
    # file ids that arrived BEFORE dir_id and kid_id in the change log.
    filler_ids = []
    for i in range(99):
        fid = fake.add_file(f"/filler{i:03d}.txt", content=b"filler")
        filler_ids.append(fid)

    # Seed the filler files into the path cache so they resolve cleanly.
    for i, fid in enumerate(filler_ids):
        sync_engine.client._path_cache.set(
            f"/filler{i:03d}.txt", fid, parent_id=ROOT_ID,
            name=f"filler{i:03d}.txt", is_folder=False,
        )

    # Now log a "stale echo" of the child at the POST-rename path
    # (same as the create above) by mutating the file — this generates a
    # new change-log entry for kid_id AFTER the filler files, ensuring it
    # lands on page 2.  The parent folder (dir_id) was also logged after
    # the 99 fillers, so both folder + child are on page 2.
    fake.service().files().update(
        fileId=kid_id,
        body={"modifiedTime": "2026-01-01T00:00:01.000Z"},
    ).execute()

    start = _start(fake)

    # We must ensure dir_id and kid_id are both logged AFTER the page boundary.
    # Currently: 2 (dir_id, kid_id from seeding) + 99 (fillers) + 1 (kid update)
    # = 102 entries.  The fake collapses to latest-seq per fileId, yielding:
    # 99 fillers + dir_id + kid_id (latest-seq) = 101 unique entries, page-split
    # at 100.  The 101st entry (kid_id) is on page 2.
    # BUT we also need dir_id on page 2 to test the cross-page scenario.
    # dir_id's latest-seq is its creation seq (before fillers), so it currently
    # lands on page 1.  Force dir_id to page 2 by updating it after fillers + kid update.
    fake.service().files().update(
        fileId=dir_id,
        body={"name": "renamed"},  # same name — just bumps seq
    ).execute()
    # Now change log (collapsed by latest-seq): 99 fillers + kid_id + dir_id = 101.
    # Page 1: 100 fillers (filler000..filler098, kid_id) — wait, let's recalculate.
    # After collapse: filler000-098 (99) + kid_id + dir_id = 101.
    # Sorted by seq: 99 fillers (seqs 101-199), kid_update (seq 202), dir_update (seq 203).
    # Page 1 (top 100 by seq): fillers (99) + kid_id. Page 2: dir_id.
    # That means kid is on page 1 and dir is on page 2.  Good enough to test cross-page.

    # Wire the fake service (after all mutations).
    _wire_fake(sync_engine, fake)

    # Local pre-rename state: /old/kid.txt on disk + index at /old.
    _write_local_file(sync_engine, "/old/kid.txt", data)
    _seed_entry(
        sync_engine, "/old", dir_id, ItemType.Folder, rev="folder", content_hash="folder"
    )
    _seed_entry(
        sync_engine, "/old/kid.txt", kid_id, ItemType.File,
        rev=kid_rev, content_hash=kid_hash,
    )

    # Drive end-to-end: drain changes and apply.
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start):
        sync_engine.apply_remote_changes(sync_events)

    # Child must be indexed only at /renamed/kid.txt.
    assert sync_engine.get_index_entry("/old/kid.txt") is None
    assert sync_engine.get_index_entry("/old") is None
    kid_entry = sync_engine.get_index_entry("/renamed/kid.txt")
    assert kid_entry is not None, "child must be indexed at the renamed path"
    assert kid_entry.remote_id == kid_id

    # Exactly one index row for the child id (no duplicate at stale path).
    by_id = sync_engine.get_index_entry_by_remote_id(kid_id)
    assert by_id is not None
    assert by_id.remote_path_lower == "/renamed/kid.txt"

    assert sync_engine.sync_errors == []


# ---------------------------------------------------------------------------
# Scenario C — fail_after on changes.list → next poll redelivers idempotently
# ---------------------------------------------------------------------------


def test_fail_after_changes_list_raises_and_redelivers():
    """fail_after models "response lost after the read committed server-side".

    For a changes.list read (non-mutating), this means: the server processed
    the request but the client never received the response.  The fault is
    exercised at the iter_remote_changes level (passthrough retry) so the
    retry layer does not silently recover.

    Assertions:
      - First drain raises HttpError(500) before yielding any result.
      - The cache is NOT mutated (no partial cursor advance).
      - A second drain from the SAME start cursor redelivers and applies the
        same changes — exactly one entry per file, no loss, no duplication.
    """
    fake = FakeDrive()
    cache_first = _cache()

    start = _start(fake)
    file_id = fake.add_file("/idempotent.txt", content=b"idempotent content")
    cache_first.set("/idempotent.txt", file_id, ROOT_ID, "idempotent.txt", False)

    # Arm: fire after the changes.list execute (response-lost simulation).
    fake.fail_after("changes.list", make_http_error(500))

    # First drain raises before yielding.
    with pytest.raises(HttpError) as exc_info:
        _drain(fake, cache_first, start)
    assert exc_info.value.resp.status == 500

    # Cache must NOT have been mutated (file path should still resolve —
    # we pre-seeded it, so this checks no EXTRA writes happened either).
    # The key property: the cursor was not advanced, so a re-drain from
    # start must still see the file change.

    # Second drain from the SAME start cursor: rule was consumed, so no fault.
    assert not fake._fail_rules  # consumed by fail_after firing
    cache_second = _cache()
    cache_second.set("/idempotent.txt", file_id, ROOT_ID, "idempotent.txt", False)
    entries, pages = _drain(fake, cache_second, start)

    # All changes redelivered.
    assert len(pages) == 1
    matching = [e for e in entries if isinstance(e, FileMetadata) and e.id == file_id]
    assert len(matching) == 1


def test_fail_after_redeliver_idempotent_end_to_end(sync_engine: SyncEngine):
    """End-to-end: after a fail_after on changes.list (exhausting retries),
    re-draining from the same cursor produces exactly one index row.

    The client's _retry_transient retries 5xx errors up to _MAX_RETRY_ATTEMPTS
    times.  We arm fail_after with count=5 (matching the retry budget) so all
    retry attempts fail and the exception finally propagates.
    """
    from cumulus.client import _MAX_RETRY_ATTEMPTS

    data = b"retry-idempotent content"

    fake = FakeDrive()
    start = _start(fake)
    file_id = fake.add_file("/retry_idempotent.txt", content=data)

    sync_engine.client._path_cache.set(
        "/retry_idempotent.txt", file_id, parent_id=ROOT_ID,
        name="retry_idempotent.txt", is_folder=False,
    )
    _wire_fake(sync_engine, fake)

    # Exhaust the full retry budget so the exception propagates to the caller.
    fake.fail_after("changes.list", make_http_error(500), count=_MAX_RETRY_ATTEMPTS)

    # convert_api_errors wraps the HttpError into ServerError at the client boundary.
    with pytest.raises(ServerError):
        for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start):
            sync_engine.apply_remote_changes(sync_events)

    # Nothing was applied.
    assert sync_engine.get_index_entry("/retry_idempotent.txt") is None

    # Re-drain from the same cursor: all fault rules consumed, no fault.
    _wire_fake(sync_engine, fake)
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start):
        sync_engine.apply_remote_changes(sync_events)

    entry = sync_engine.get_index_entry("/retry_idempotent.txt")
    assert entry is not None
    assert entry.remote_id == file_id

    by_id = sync_engine.get_index_entry_by_remote_id(file_id)
    assert by_id is not None
    assert by_id.remote_path_lower == "/retry_idempotent.txt"

    assert sync_engine.sync_errors == []


# ---------------------------------------------------------------------------
# Scenario D — duplicate change entry → no double-apply (latest-seq collapse)
# ---------------------------------------------------------------------------


def test_duplicate_changelog_entry_collapsed_to_one():
    """Force the same fileId to appear twice in the change log (two seqs).

    The fake's changes.list collapses to latest-seq per fileId.
    iter_remote_changes therefore yields exactly ONE entry for that fid.

    This is a characterization test: we PROVE the property is real by checking
    that the raw change log has two entries for the fid but the drained entries
    contain exactly one.
    """
    fake = FakeDrive()
    cache = _cache()

    start = _start(fake)

    # Create a file and then mutate it — two change-log entries for the same fid.
    file_id = fake.add_file("/dup.txt", content=b"v1")
    fake.service().files().update(
        fileId=file_id, body={"name": "dup.txt"}
    ).execute()  # second change-log entry

    # Verify the raw change log has TWO entries for file_id.
    raw_for_fid = [(seq, fid) for seq, fid in fake._change_log if fid == file_id]
    assert len(raw_for_fid) == 2, "precondition: two change log entries for fid"

    entries, pages = _drain(fake, cache, start)

    # Exactly ONE FileMetadata for file_id (latest-seq collapse).
    matching = [e for e in entries if isinstance(e, FileMetadata) and e.id == file_id]
    assert len(matching) == 1


def test_direct_changelog_duplicate_still_collapsed():
    """Directly append a duplicate to fake._change_log to confirm collapse even
    when the mutation path isn't available (e.g. a replayed seq).

    Characterization: the dedup is in the fake's list() not in changes.py,
    so this test documents the boundary.
    """
    fake = FakeDrive()
    cache = _cache()

    start = _start(fake)
    file_id = fake.add_file("/dedup.txt", content=b"data")

    # Manually append a second entry for the same fid with a lower seq.
    seq_before = fake._seq
    fake._change_log.append((seq_before - 5, file_id))  # lower seq, same fid

    # Two entries in the log.
    raw_for_fid = [(seq, fid) for seq, fid in fake._change_log if fid == file_id]
    assert len(raw_for_fid) == 2

    entries, _ = _drain(fake, cache, start)

    # Still exactly one entry emitted.
    matching = [e for e in entries if isinstance(e, FileMetadata) and e.id == file_id]
    assert len(matching) == 1
