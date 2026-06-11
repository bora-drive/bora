"""Unit tests for cumulus.changes."""

from __future__ import annotations

from datetime import datetime, timezone

from cumulus.changes import (
    parse_drive_time,
    to_file_metadata,
    to_folder_metadata,
)
from cumulus.core import FileMetadata, FolderMetadata

SAMPLE_FILE = {
    "id": "F1",
    "name": "report.pdf",
    "parents": ["ROOT"],
    "mimeType": "application/pdf",
    "modifiedTime": "2026-04-30T04:32:17.043Z",
    "size": "2048",
    "md5Checksum": "abc123",
    "shared": False,
    "headRevisionId": "rev-abc",
    "trashed": False,
    "lastModifyingUser": {"permissionId": "perm-9"},
}

SAMPLE_FOLDER = {
    "id": "D1",
    "name": "Documents",
    "parents": ["ROOT"],
    "mimeType": "application/vnd.google-apps.folder",
    "shared": True,
}


def test_parse_drive_time_round_trips():
    dt = parse_drive_time("2026-04-30T04:32:17.043Z")
    assert dt == datetime(2026, 4, 30, 4, 32, 17, 43000, tzinfo=timezone.utc)


def test_parse_drive_time_handles_none():
    dt = parse_drive_time(None)
    assert dt.tzinfo is timezone.utc


def test_to_file_metadata_maps_all_fields():
    meta = to_file_metadata(SAMPLE_FILE, "/report.pdf")
    assert isinstance(meta, FileMetadata)
    assert meta.name == "report.pdf"
    assert meta.path_display == "/report.pdf"
    assert meta.id == "F1"
    assert meta.rev == "rev-abc"
    assert meta.size == 2048
    assert meta.content_hash == "abc123"
    assert meta.symlink_target is None
    assert meta.is_downloadable is True
    assert meta.modified_by == "perm-9"
    assert meta.modified == datetime(2026, 4, 30, 4, 32, 17, 43000, tzinfo=timezone.utc)


def test_to_file_metadata_tolerates_missing_optional_fields():
    bare = {"id": "F2", "name": "x.bin", "mimeType": "application/octet-stream"}
    meta = to_file_metadata(bare, "/x.bin")
    assert meta.size == 0
    assert meta.content_hash == ""
    assert meta.modified_by is None


def test_to_file_metadata_handles_null_version():
    meta = to_file_metadata({"id": "F3", "name": "f.bin", "version": None}, "/f.bin")
    assert meta.rev == ""


def test_to_file_metadata_rev_is_head_revision_id():
    from cumulus.changes import to_file_metadata

    drive_file = {
        "id": "FID",
        "name": "f.txt",
        "mimeType": "text/plain",
        "modifiedTime": "2026-01-01T00:00:00.000Z",
        "size": "3",
        "md5Checksum": "abc123",
        "headRevisionId": "HEADREV9",
        "version": "42",
        "shared": False,
    }
    md = to_file_metadata(drive_file, "/f.txt")
    assert md.rev == "HEADREV9"


def test_to_folder_metadata_maps_fields():
    meta = to_folder_metadata(SAMPLE_FOLDER, "/Documents")
    assert isinstance(meta, FolderMetadata)
    assert meta.name == "Documents"
    assert meta.path_display == "/Documents"
    assert meta.id == "D1"
    assert meta.shared is True


# ==== classify ============================================================

import sqlite3

from cumulus.changes import ChangeDecision, _emit_apply, classify
from cumulus.database.core import Database
from cumulus.path_cache import PathIDCache


def _cache() -> PathIDCache:
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = "ROOT"
    return c


def test_classify_removed_true_is_delete():
    change = {"removed": True, "fileId": "F1"}
    assert classify(change, _cache()) is ChangeDecision.DELETE


def test_classify_trashed_is_delete():
    cache = _cache()
    change = {"removed": False, "fileId": "F1",
              "file": {"id": "F1", "name": "a", "parents": ["ROOT"],
                       "trashed": True}}
    assert classify(change, cache) is ChangeDecision.DELETE


def test_classify_move_out_is_delete():
    cache = _cache()
    cache.set("/a.pdf", "F1", "ROOT", "a.pdf", False)
    change = {"removed": False, "fileId": "F1",
              "file": {"id": "F1", "name": "a.pdf",
                       "parents": ["OUTSIDE"], "trashed": False}}
    assert classify(change, cache) is ChangeDecision.DELETE


def test_classify_never_in_scope_is_out_of_scope():
    cache = _cache()
    change = {"removed": False, "fileId": "F9",
              "file": {"id": "F9", "name": "x", "parents": ["OUTSIDE"],
                       "trashed": False}}
    assert classify(change, cache) is ChangeDecision.OUT_OF_SCOPE


def test_classify_in_scope_is_apply():
    cache = _cache()
    change = {"removed": False, "fileId": "F1",
              "file": {"id": "F1", "name": "a.pdf", "parents": ["ROOT"],
                       "trashed": False}}
    assert classify(change, cache) is ChangeDecision.APPLY


def test_classify_missing_file_body_is_unknown():
    cache = _cache()
    change = {"removed": False, "fileId": "F1", "file": None}
    assert classify(change, cache) is ChangeDecision.UNKNOWN_FILE


# ==== iter_remote_changes — FakeDrive-driven tests ========================

import sqlite3
from unittest.mock import patch

from cumulus.changes import iter_remote_changes
from cumulus.core import DeletedMetadata, FileMetadata, FolderMetadata
from tests.fake_drive import ROOT_ID, FakeDrive


def _fake_cache() -> PathIDCache:
    """Path cache pre-seeded with ROOT_ID as the sync root."""
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = ROOT_ID
    return c


def _start(fake: FakeDrive) -> str:
    """Return the current change cursor (no changes yet recorded after this)."""
    return fake.service().changes().getStartPageToken().execute()["startPageToken"]


def _drain(fake: FakeDrive, cache: PathIDCache, start: str) -> list:
    """Run iter_remote_changes to exhaustion; return all applied entries."""
    def factory():
        return fake.service()

    def retry(fn):
        return fn()

    pages = list(iter_remote_changes(factory, retry, cache, start))
    return [e for page in pages for e in page.entries], pages


def test_iter_changes_create_writes_cache_and_yields():
    """A new file in the change feed is written to the cache and yielded."""
    fake = FakeDrive()
    cache = _fake_cache()
    start = _start(fake)
    fid = fake.add_file("/a.pdf", content=b"x")
    entries, pages = _drain(fake, cache, start)
    assert len(pages) == 1
    assert isinstance(entries[0], FileMetadata)
    assert cache.path_to_id("/a.pdf") == fid
    assert pages[0].has_more is False


def test_iter_changes_delete_removes_from_cache():
    """A hard-delete change propagates DeletedMetadata and removes from cache."""
    fake = FakeDrive()
    cache = _fake_cache()
    # Create the file so cache knows about it.
    fid = fake.add_file("/a.pdf", content=b"x")
    start = _start(fake)
    cache.set("/a.pdf", fid, ROOT_ID, "a.pdf", False)
    fake.service().files().delete(fileId=fid).execute()
    entries, pages = _drain(fake, cache, start)
    assert isinstance(entries[0], DeletedMetadata)
    assert cache.path_to_id("/a.pdf") is None


def test_iter_changes_rename_moves_cache_entry():
    """A rename change updates the cache: old path gone, new path present."""
    fake = FakeDrive()
    cache = _fake_cache()
    fid = fake.add_file("/old.pdf", content=b"x")
    cache.set("/old.pdf", fid, ROOT_ID, "old.pdf", False)
    start = _start(fake)
    fake.service().files().update(fileId=fid, body={"name": "new.pdf"}).execute()
    _drain(fake, cache, start)
    assert cache.path_to_id("/old.pdf") is None
    assert cache.path_to_id("/new.pdf") == fid


def test_iter_changes_intra_batch_dependency():
    """Folder created then file created inside it: both appear in results."""
    fake = FakeDrive()
    cache = _fake_cache()
    start = _start(fake)
    # Folder change recorded first, then file — parent-before-child.
    did = fake.add_folder("/Docs")
    fid = fake.add_file("/Docs/a.pdf", content=b"x")
    entries, pages = _drain(fake, cache, start)
    assert cache.path_to_id("/Docs/a.pdf") == fid
    entry_types = {type(e) for e in entries}
    assert FolderMetadata in entry_types
    assert FileMetadata in entry_types


def test_iter_changes_intra_batch_dependency_reversed_order():
    """Child change delivered before parent: fixpoint resolves both correctly.

    Drive does not guarantee parent-before-child ordering in changes.list.
    We simulate child-first delivery by manipulating the change log so the
    file's seq is lower than the folder's seq (the fake returns the file
    change first). The fixpoint in iter_remote_changes must still resolve both.
    """
    fake = FakeDrive()
    cache = _fake_cache()
    start = _start(fake)
    # Insert folder first (lower seq), then file (higher seq).
    did = fake.add_folder("/Docs")
    fid = fake.add_file("/Docs/a.pdf", content=b"x")
    # Swap the change log entries so child (fid, lower seq) appears first.
    log = fake._change_log
    did_pos = next(i for i, (_, id_) in enumerate(log) if id_ == did)
    fid_pos = next(i for i, (_, id_) in enumerate(log) if id_ == fid)
    did_seq, _ = log[did_pos]
    fid_seq, _ = log[fid_pos]
    # Swap seq values: child gets the smaller seq so it sorts before parent.
    log[did_pos] = (fid_seq, did)
    log[fid_pos] = (did_seq, fid)
    entries, pages = _drain(fake, cache, start)
    assert cache.path_to_id("/Docs/a.pdf") == fid
    entry_types = {type(e) for e in entries}
    assert FolderMetadata in entry_types
    assert FileMetadata in entry_types


def test_iter_changes_move_into_new_folder_is_not_a_delete():
    """File moved into a brand-new folder: must not be misclassified as delete.

    F1 already lives at /a.pdf. In one batch it is moved into a brand-new
    folder, and the fake delivers F1's change before the folder's change.
    F1 must be re-homed under /Docs — not misread as a move-out and deleted,
    because its new parent is created later in the same batch.
    """
    fake = FakeDrive()
    cache = _fake_cache()
    fid = fake.add_file("/a.pdf", content=b"x")
    cache.set("/a.pdf", fid, ROOT_ID, "a.pdf", False)
    start = _start(fake)
    did = fake.add_folder("/Docs")
    # Move the file into /Docs.
    svc = fake.service()
    svc.files().update(
        fileId=fid,
        body={},
        addParents=did,
        removeParents=ROOT_ID,
    ).execute()
    # Swap seq so file's move change comes before folder's create change.
    # The file has two entries (create + move); we want the LAST one (move).
    log = fake._change_log
    did_pos = next(i for i, (_, id_) in enumerate(log) if id_ == did)
    # Find the last occurrence of fid (the move event, not the original create).
    fid_pos = len(log) - 1 - next(
        i for i, (_, id_) in enumerate(reversed(log)) if id_ == fid
    )
    did_seq, _ = log[did_pos]
    fid_seq, _ = log[fid_pos]
    log[did_pos] = (fid_seq, did)
    log[fid_pos] = (did_seq, fid)
    entries, pages = _drain(fake, cache, start)
    assert cache.path_to_id("/a.pdf") is None
    assert cache.path_to_id("/Docs/a.pdf") == fid
    assert DeletedMetadata not in {type(e) for e in entries}


def test_iter_changes_drops_native_doc():
    """A Google-native document change yields no entries and nothing cached."""
    fake = FakeDrive()
    cache = _fake_cache()
    start = _start(fake)
    # Insert a native doc directly via the private _insert helper.
    gid = fake._insert("Doc", ROOT_ID, "application/vnd.google-apps.document", b"")
    entries, pages = _drain(fake, cache, start)
    assert entries == []
    assert cache.get_entry(gid) is None


def test_iter_changes_drops_shortcut():
    """A Drive shortcut change yields no entries and nothing cached."""
    fake = FakeDrive()
    cache = _fake_cache()
    start = _start(fake)
    sid = fake._insert("Link", ROOT_ID, "application/vnd.google-apps.shortcut", b"")
    entries, pages = _drain(fake, cache, start)
    assert entries == []
    assert cache.get_entry(sid) is None


def test_iter_changes_drops_unknown_delete():
    """A delete for a file the cache never knew about is silently dropped."""
    fake = FakeDrive()
    cache = _fake_cache()
    # Create a file before the start token so its create isn't in the feed.
    fid = fake.add_file("/ghost.txt", content=b"x")
    start = _start(fake)
    # Delete it — the delete change IS in the feed, but cache doesn't know it.
    fake.service().files().delete(fileId=fid).execute()
    entries, pages = _drain(fake, cache, start)
    assert entries == []


def test_iter_changes_paginates():
    """Changes spanning two Drive pages are both included in a single result.

    We force two Drive pages by patching _CHANGE_PAGE_SIZE to 1.  Since the
    function now drains all pages before yielding (review F6), it returns one
    ListFolderResult whose entries cover both Drive pages.  has_more is always
    False; the cursor is the final newStartPageToken.
    """
    fake = FakeDrive()
    cache = _fake_cache()
    start = _start(fake)
    fid1 = fake.add_file("/a.pdf", content=b"x")
    fid2 = fake.add_file("/b.pdf", content=b"y")

    import cumulus.changes as _changes_mod

    with patch.object(_changes_mod, "_CHANGE_PAGE_SIZE", 1):
        pages = list(iter_remote_changes(
            lambda: fake.service(),
            lambda fn: fn(),
            cache,
            start,
        ))

    assert len(pages) == 1, "all Drive pages are drained into a single result"
    assert pages[0].has_more is False
    all_entries = [e for page in pages for e in page.entries]
    paths = {e.path_display for e in all_entries}
    assert "/a.pdf" in paths
    assert "/b.pdf" in paths
    assert cache.path_to_id("/a.pdf") == fid1
    assert cache.path_to_id("/b.pdf") == fid2


# ==== parent-before-child regression =====================================


def test_iter_remote_changes_new_file_in_new_folder():
    """Regression: parent-before-child ordering (Stage 3, 05d58ed).

    When a folder and a file inside it are both created, and the fake delivers
    them in change-log order (folder first in this case), both must appear in
    the results after iter_remote_changes processes them.
    """
    fake = FakeDrive()
    cache = _fake_cache()
    start = _start(fake)
    fake.add_folder("/new")
    fake.add_file("/new/c.txt", content=b"x")
    entries, pages = _drain(fake, cache, start)
    applied = {e.path_display for e in entries}
    assert "/new" in applied
    assert "/new/c.txt" in applied


# ==== _emit_apply cache-rejection guard (review F2) =======================


def test_fold_conflicting_change_is_not_emitted():
    """A change whose path the cache rejects (duplicate / case-fold-equal
    sibling) must NOT be emitted — emitting it points the engine's
    download at a path mapped to a DIFFERENT file's id (review F2)."""
    cache = _cache()
    cache.set("/Report.pdf", "id-A", "ROOT", "Report.pdf", False)

    out: list = []
    change = {
        "fileId": "id-B",
        "file": {
            "id": "id-B", "name": "report.pdf", "parents": ["ROOT"],
            "mimeType": "text/plain", "trashed": False,
        },
    }
    _emit_apply(change, cache, out)

    assert out == [], "rejected change leaked into the feed output"
    assert cache.path_to_id("/report.pdf") == "id-A"  # cache untouched


def test_move_to_conflicting_path_is_not_emitted():
    """File id-B is already cached at /other.txt; a change moves it to the
    case-fold-equal /report.pdf (which id-A already owns).  cache.move
    returns False → nothing emitted, cache still maps /other.txt to id-B
    (review F2 — move variant)."""
    cache = _cache()
    cache.set("/Report.pdf", "id-A", "ROOT", "Report.pdf", False)
    cache.set("/other.txt", "id-B", "ROOT", "other.txt", False)

    out: list = []
    change = {
        "fileId": "id-B",
        "file": {
            "id": "id-B", "name": "report.pdf", "parents": ["ROOT"],
            "mimeType": "text/plain", "trashed": False,
        },
    }
    _emit_apply(change, cache, out)

    assert out == [], "rejected move leaked into the feed output"
    assert cache.path_to_id("/other.txt") == "id-B"   # original path kept
    assert cache.path_to_id("/report.pdf") == "id-A"  # original owner untouched


# ==== cross-page parent/child resolution (review F6) =====================


def test_child_before_parent_across_pages_is_not_dropped():
    """A child delivered on page N whose brand-new parent folder arrives on
    page N+1 must still be emitted (review F6).

    The per-page fixpoint cannot resolve the child on page 1 because the
    parent hasn't arrived yet — it appears on page 2.  After the fix the
    whole batch is drained before classification, so the parent is already
    in the cache when the fixpoint runs.
    """
    import sqlite3
    from cumulus.database.core import Database
    from cumulus.path_cache import PathIDCache

    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = "root"

    child = {
        "fileId": "id-c",
        "file": {
            "id": "id-c", "name": "kid.txt", "parents": ["id-p"],
            "mimeType": "text/plain", "trashed": False,
        },
    }
    parent = {
        "fileId": "id-p",
        "file": {
            "id": "id-p", "name": "newdir", "parents": ["root"],
            "mimeType": "application/vnd.google-apps.folder", "trashed": False,
        },
    }
    pages = [
        {"changes": [child], "nextPageToken": "t2"},
        {"changes": [parent], "newStartPageToken": "t3"},
    ]

    class _Svc:
        def changes(self):
            return self

        def list(self, **kw):
            return self

        def execute(self):
            return pages.pop(0)

    results = list(iter_remote_changes(lambda: _Svc(), lambda fn: fn(), cache, "t1"))
    entries = [e for r in results for e in r.entries]
    paths = {e.path_display for e in entries}
    assert "/newdir/kid.txt" in paths, paths
    assert results[-1].cursor == "t3"
