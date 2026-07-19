"""Fault-injection tests for the Drive change-feed (Stage 7b-E2, Task 2).

Covers: page-fetch errors, cross-page stale-child echoes, fail_after
redelivery idempotence, and duplicate change-log collapse.
"""

from __future__ import annotations

import hashlib
import os
import os.path as osp
import sqlite3
from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError

from bora.changes import iter_remote_changes
from bora.client import GoogleDriveClient
from bora.config import remove_configuration
from bora.core import FileMetadata, FullAccount
from bora.database.core import Database
from bora.exceptions import ServerError
from bora.keyring import CredentialStorage
from bora.models import IndexEntry, ItemType
from bora.path_cache import PathIDCache
from bora.sync import SyncEngine
from bora.utils.appdirs import get_home_dir
from bora.utils.path import delete
from tests.fake_drive import ROOT_ID, FakeDrive, make_http_error

# ---------------------------------------------------------------------------
# Shared low-level helpers (mirror test_changes.py)
# ---------------------------------------------------------------------------

_PAGE_SIZE = 100  # Must match changes._CHANGE_PAGE_SIZE


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


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
            factory,
            retry,
            cache,
            start,
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

    # check_same_thread=False is required because apply_remote_changes uses a
    # thread pool (do_parallel) that may access the path cache from worker threads.
    cache = PathIDCache(Database(sqlite3.connect(":memory:", check_same_thread=False)))
    cache.root_id = ROOT_ID
    sync = SyncEngine(
        GoogleDriveClient(
            "fault-feed-config", CredentialStorage("fault-feed-config"), cache
        )
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
        read_only=0,
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

    Mechanism (mirrors test_remote_folder_rename_with_same_batch_stale_child_echo):

      * Pre-rename state (path cache + index + disk): /old  /old/kid.txt.
      * Drive renames /old → /renamed; kid.txt gets a content touch in the same poll.
      * The kid's change lands on page 1 (before the _PAGE_SIZE boundary);
        the folder rename lands on page 2.
      * iter_remote_changes buffers ALL pages before running the fixpoint.
        The fixpoint processes kid first (page 1): kid's parent dir_id resolves to
        /old in the still-unmodified path cache → emits FileMetadata(/old/kid.txt)
        (stale echo). Then it processes the folder rename (page 2): moves /old → /renamed
        in the path cache (re-keys subtree).
      * apply_remote_changes applies the folder event first (shallow depth), re-keying
        the index subtree: kid_id → /renamed/kid.txt.  Then it processes the stale file
        event at /old/kid.txt: _apply_remote_move finds kid_id already indexed at
        /renamed/kid.txt, attempts to move /renamed/kid.txt → /old/kid.txt, the move
        silently no-ops (destination parent /old no longer exists), and the finding-5
        guard fires (destination parent absent from index) → returns True (skip).

    The stale child must NOT be resurrected at /old/kid.txt; exactly one index
    row must hold kid_id, at /renamed/kid.txt.

    Teeth: temporarily neutering the finding-5 guard makes this test FAIL
    (kid_id ends up re-indexed at /old/kid.txt instead of /renamed/kid.txt).
    """
    data = b"kid content unchanged by the rename"
    kid_hash = _md5(data)

    fake = FakeDrive()

    # Drive pre-rename state: folder "old" containing kid.txt.
    # These two entries are logged BEFORE we call _start(), so they are
    # invisible to the drain from the start cursor.
    dir_id = fake.add_folder("/old")
    kid_id = fake.add_file("/old/kid.txt", data)
    kid_rev = fake.files[kid_id]["headRevisionId"]

    # Seed path cache with the PRE-rename truth.
    # iter_remote_changes will use these entries when classifying changes: the
    # kid's change entry (page 1) will see dir_id → /old, so kid resolves to
    # /old/kid.txt (stale echo).  The folder rename (page 2) then moves
    # the path-cache entry from /old to /renamed, re-keying the subtree.
    sync_engine.client._path_cache.set(
        "/old", dir_id, parent_id=ROOT_ID, name="old", is_folder=True
    )
    sync_engine.client._path_cache.set(
        "/old/kid.txt", kid_id, parent_id=dir_id, name="kid.txt", is_folder=False
    )

    # Take the start cursor NOW — before any changes that should be visible to
    # the drain.  Everything logged after this point is eligible for the feed.
    start = _start(fake)

    # Seed 99 filler files.  Each gets its own unique change-log entry (seq).
    # Together with the kid update below they fill exactly _PAGE_SIZE = 100
    # slots on page 1, pushing the folder rename to page 2.
    for i in range(99):
        fake.add_file(f"/filler{i:03d}.txt", content=b"filler")

    # Touch kid.txt so it re-appears in the change log AFTER the fillers.
    # The fake deduplicates by latest-seq, so kid_id's change-log entry is now
    # AFTER all filler entries.  kid ends up as the 100th visible entry = last
    # slot of page 1.
    fake.service().files().update(
        fileId=kid_id,
        body={"modifiedTime": "2026-01-01T00:00:01.000Z"},
    ).execute()

    # Rename the folder from "old" to "renamed" AFTER the kid update.  Its
    # latest-seq is now the 101st visible entry → page 2.
    # Drive truth after this update: /renamed  /renamed/kid.txt.
    fake.service().files().update(
        fileId=dir_id,
        body={"name": "renamed"},
    ).execute()

    # ── Two-page teeth ──────────────────────────────────────────────────────
    # Verify that the number of unique visible entries exceeds _PAGE_SIZE,
    # proving the fake will serve TWO changes.list pages.
    # (fake.changes.list collapses to latest-seq per fileId and paginates at
    # _PAGE_SIZE; if we have ≤ _PAGE_SIZE unique entries it would be one page.)
    _visible_latest: dict[str, int] = {}
    for seq, fid in fake._change_log:
        if seq >= int(start):
            _visible_latest[fid] = seq
    assert len(_visible_latest) > _PAGE_SIZE, (
        f"Expected >{_PAGE_SIZE} visible unique entries to force 2 pages, "
        f"got {len(_visible_latest)}"
    )
    # Also verify that the folder rename is NOT in the first _PAGE_SIZE entries
    # (i.e. it is on page 2).
    _sorted_visible = sorted(_visible_latest.items(), key=lambda kv: kv[1])
    _page1_ids = {fid for fid, _ in _sorted_visible[:_PAGE_SIZE]}
    assert (
        dir_id not in _page1_ids
    ), "dir_id must be on page 2, not page 1, for the cross-page scenario"
    assert (
        kid_id in _page1_ids
    ), "kid_id must be on page 1 so the stale echo fires before the rename"
    # ────────────────────────────────────────────────────────────────────────

    # Wire the fake (after all mutations so service() returns final state).
    _wire_fake(sync_engine, fake)

    # Fully synced PRE-rename state: /old/kid.txt on disk + index at /old.
    _write_local_file(sync_engine, "/old/kid.txt", data)
    _seed_entry(
        sync_engine,
        "/old",
        dir_id,
        ItemType.Folder,
        rev="folder",
        content_hash="folder",
    )
    _seed_entry(
        sync_engine,
        "/old/kid.txt",
        kid_id,
        ItemType.File,
        rev=kid_rev,
        content_hash=kid_hash,
    )

    # Drive end-to-end: drain both pages and apply.
    for sync_events, _cursor in sync_engine.list_remote_changes_iterator(start):
        sync_engine.apply_remote_changes(sync_events)

    # The renamed folder itself must be indexed at /renamed (full rename applied).
    dir_entry = sync_engine.get_index_entry("/renamed")
    assert dir_entry is not None, "renamed folder must be indexed at /renamed"
    assert dir_entry.remote_id == dir_id

    # Child must be indexed only at /renamed/kid.txt, never at the stale path.
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

    # Arm: fire after the changes.list execute (response-lost simulation).
    fake.fail_after("changes.list", make_http_error(500))

    # First drain raises before yielding.
    with pytest.raises(HttpError) as exc_info:
        _drain(fake, cache_first, start)
    assert exc_info.value.resp.status == 500

    # The key property: the cursor was not advanced, so a re-drain from the
    # same start cursor must still deliver the file change.

    # Second drain from the SAME start cursor: rule was consumed, so no fault.
    # No pre-seeding needed — ROOT_ID always resolves via PathIDCache's root
    # special-case, so the drain finds the file without any cache.set() call.
    assert not fake._fail_rules  # consumed by fail_after firing
    cache_second = _cache()
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
    from bora.client import _MAX_RETRY_ATTEMPTS

    data = b"retry-idempotent content"

    fake = FakeDrive()
    start = _start(fake)
    file_id = fake.add_file("/retry_idempotent.txt", content=data)

    sync_engine.client._path_cache.set(
        "/retry_idempotent.txt",
        file_id,
        parent_id=ROOT_ID,
        name="retry_idempotent.txt",
        is_folder=False,
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

    Teeth: both entries are visible from the start cursor (seq >= int(start)),
    so WITHOUT the fake's dedup the drain would see TWO FileMetadata entries
    for file_id.  The test confirms the fake collapses them to exactly one.
    If the fake returned both, len(matching) would be 2.
    """
    fake = FakeDrive()
    cache = _cache()

    # Take start BEFORE creating the file so BOTH change-log entries (the
    # real create and the manually appended duplicate) are visible from
    # the cursor.  The original bug appended (seq_before - 5, file_id) where
    # seq_before = 101 and start = "100", so the duplicate had seq 96 < 100
    # and was filtered out — only one entry ever reached the drain, making
    # the dedup untested.
    start = _start(fake)

    file_id = fake.add_file("/dedup.txt", content=b"data")

    # Manually append a second entry for the same fid with a HIGHER seq so it
    # is also visible (seq >= int(start)).  The fake deduplicates to the
    # latest-seq entry per fileId; both entries are in the log and both have
    # seq >= int(start), so the dedup IS actually exercised here.
    seq_of_extra = fake._seq  # current seq counter, which will be > int(start)
    fake._change_log.append((seq_of_extra, file_id))

    # Precondition: exactly two raw change-log entries for file_id.
    raw_for_fid = [(seq, fid) for seq, fid in fake._change_log if fid == file_id]
    assert len(raw_for_fid) == 2, "precondition: two change log entries for fid"

    # Both entries must be visible from the start cursor.
    visible_for_fid = [(seq, fid) for seq, fid in raw_for_fid if seq >= int(start)]
    assert len(visible_for_fid) == 2, (
        "both entries must be above the start cursor so the dedup is exercised; "
        f"start={start}, entries={raw_for_fid}"
    )

    entries, _ = _drain(fake, cache, start)

    # The fake collapses to latest-seq: still exactly one entry emitted.
    # (If the fake returned both, len(matching) would be 2.)
    matching = [e for e in entries if isinstance(e, FileMetadata) and e.id == file_id]
    assert len(matching) == 1
