"""Unit tests for bora.changes."""

from __future__ import annotations

from datetime import datetime, timezone

from bora.changes import (
    parse_drive_time,
    to_file_metadata,
    to_folder_metadata,
)
from bora.core import FileMetadata, FolderMetadata
from bora.exceptions import CursorResetError
from bora.export import ExportOptions

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

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
    from bora.changes import to_file_metadata

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


def test_export_rev_falls_back_to_modified_time_when_version_absent():
    """Finding 4: a native doc under export whose Drive resource omits
    ``version`` must not raise KeyError (which would wedge every poll cycle).
    The rev falls back to modifiedTime, which is still change-sensitive."""
    doc = {
        "id": "DOCID",
        "name": "Report",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-01-02T03:04:05.678Z",
        # No "version" key at all.
    }
    md = to_file_metadata(doc, "/Report.docx", export_opts=ExportOptions(True, False))
    assert md.rev == "2026-01-02T03:04:05.678Z"
    assert md.export_mime == DOCX_MIME
    assert md.content_hash is None


def test_to_folder_metadata_maps_fields():
    meta = to_folder_metadata(SAMPLE_FOLDER, "/Documents")
    assert isinstance(meta, FolderMetadata)
    assert meta.name == "Documents"
    assert meta.path_display == "/Documents"
    assert meta.id == "D1"
    assert meta.shared is True


def test_to_file_metadata_reads_can_edit_false():
    file = {**SAMPLE_FILE, "capabilities": {"canEdit": False}}
    meta = to_file_metadata(file, "/report.pdf")
    assert meta.can_edit is False


def test_to_file_metadata_can_edit_defaults_true_when_absent():
    meta = to_file_metadata(SAMPLE_FILE, "/report.pdf")
    assert meta.can_edit is True


def test_to_folder_metadata_reads_can_edit():
    file = {**SAMPLE_FOLDER, "capabilities": {"canEdit": False}}
    meta = to_folder_metadata(file, "/Docs")
    assert meta.can_edit is False


# ==== classify ============================================================

import sqlite3

from bora.changes import ChangeDecision, _emit_apply, _emit_shortcut, classify
from bora.database.core import Database
from bora.path_cache import SHARED_WITH_ME_ID, PathIDCache


def _cache() -> PathIDCache:
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = "ROOT"
    return c


def test_classify_removed_true_is_delete():
    change = {"removed": True, "fileId": "F1"}
    assert classify(change, _cache()) is ChangeDecision.DELETE


def test_classify_trashed_is_delete():
    cache = _cache()
    change = {
        "removed": False,
        "fileId": "F1",
        "file": {"id": "F1", "name": "a", "parents": ["ROOT"], "trashed": True},
    }
    assert classify(change, cache) is ChangeDecision.DELETE


def test_classify_move_out_is_delete():
    cache = _cache()
    cache.set("/a.pdf", "F1", "ROOT", "a.pdf", False)
    change = {
        "removed": False,
        "fileId": "F1",
        "file": {"id": "F1", "name": "a.pdf", "parents": ["OUTSIDE"], "trashed": False},
    }
    assert classify(change, cache) is ChangeDecision.DELETE


def test_classify_never_in_scope_is_out_of_scope():
    cache = _cache()
    change = {
        "removed": False,
        "fileId": "F9",
        "file": {"id": "F9", "name": "x", "parents": ["OUTSIDE"], "trashed": False},
    }
    assert classify(change, cache) is ChangeDecision.OUT_OF_SCOPE


def test_classify_in_scope_is_apply():
    cache = _cache()
    change = {
        "removed": False,
        "fileId": "F1",
        "file": {"id": "F1", "name": "a.pdf", "parents": ["ROOT"], "trashed": False},
    }
    assert classify(change, cache) is ChangeDecision.APPLY


def test_classify_missing_file_body_is_unknown():
    cache = _cache()
    change = {"removed": False, "fileId": "F1", "file": None}
    assert classify(change, cache) is ChangeDecision.UNKNOWN_FILE


def test_classify_shared_with_me_is_apply_without_parent():
    cache = _cache()  # root_id="ROOT"
    change = {
        "removed": False,
        "fileId": "SR",
        "file": {
            "id": "SR",
            "name": "Team",
            "parents": ["OTHER"],
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "sharedWithMe": True,
        },
    }
    assert classify(change, cache) is ChangeDecision.APPLY


def test_emit_apply_attaches_shared_root_to_sentinel():
    cache = _cache()
    out: list = []
    change = {
        "file": {
            "id": "SR",
            "name": "Team",
            "parents": ["OTHER"],
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "sharedWithMe": True,
        }
    }
    assert _emit_apply(change, cache, out) is True
    assert cache.id_to_path("SR") == "/Shared with me/Team"


def test_shortcut_emits_relative_symlink_metadata():
    cache = _cache()  # root_id="ROOT"
    cache.set("/Docs", "D", "ROOT", "Docs", True)
    cache.set("/Docs/report.pdf", "T", "D", "report.pdf", False)
    out: list = []
    change = {
        "file": {
            "id": "SL",
            "name": "link",
            "parents": ["ROOT"],
            "mimeType": "application/vnd.google-apps.shortcut",
            "trashed": False,
            "shortcutDetails": {"targetId": "T", "targetMimeType": "application/pdf"},
        }
    }
    assert _emit_apply(change, cache, out) is True
    md = out[-1]
    assert md.symlink_target == "Docs/report.pdf"  # relative to "/" dir


def test_shortcut_with_unresolved_target_is_not_emitted():
    cache = _cache()
    out: list = []
    change = {
        "file": {
            "id": "SL",
            "name": "link",
            "parents": ["ROOT"],
            "mimeType": "application/vnd.google-apps.shortcut",
            "trashed": False,
            "shortcutDetails": {"targetId": "MISSING"},
        }
    }
    # Returns False -> deferred by the fixpoint; nothing emitted this pass.
    assert _emit_apply(change, cache, out) is False
    assert out == []


def test_shortcut_to_shortcut_chain_is_refused():
    cache = _cache()
    cache.set("/other-link", "OTHER", "ROOT", "other-link", False)
    out: list = []
    change = {
        "file": {
            "id": "SL",
            "name": "link",
            "parents": ["ROOT"],
            "mimeType": "application/vnd.google-apps.shortcut",
            "trashed": False,
            "shortcutDetails": {
                "targetId": "OTHER",
                "targetMimeType": "application/vnd.google-apps.shortcut",
            },
        }
    }
    # Consumed (not retried) but not emitted, and not cached as a symlink.
    assert _emit_apply(change, cache, out) is True
    assert out == []


def test_shortcut_cross_subtree_relative_target():
    """Task-12 brief example: a shortcut under the Shared-with-me virtual
    root whose target resolves under the real sync root needs a '../' hop
    to reach it (the two live in different subtrees)."""
    cache = _cache()  # root_id="ROOT"; SHARED_WITH_ME_ID resolves via the
    # sentinel without seeding (see PathIDCache.id_to_path).
    cache.set("/Docs", "D", "ROOT", "Docs", True)
    cache.set("/Docs/report.pdf", "T", "D", "report.pdf", False)
    out: list = []
    change = {
        "file": {
            "id": "SL",
            "name": "link",
            "parents": [SHARED_WITH_ME_ID],
            "mimeType": "application/vnd.google-apps.shortcut",
            "trashed": False,
            "shortcutDetails": {"targetId": "T", "targetMimeType": "application/pdf"},
        }
    }
    assert _emit_apply(change, cache, out) is True
    md = out[-1]
    assert md.path_display == "/Shared with me/link"
    assert md.symlink_target == "../Docs/report.pdf"


def test_unshare_removed_true_deletes_from_cache():
    cache = _cache()
    cache.set("/Shared with me/Team", "SR", SHARED_WITH_ME_ID, "Team", True)
    out: list = []
    from bora.changes import _emit_delete

    _emit_delete({"removed": True, "fileId": "SR"}, cache, out, None)
    assert cache.id_to_path("SR") is None
    assert any(getattr(e, "path_display", None) == "/Shared with me/Team" for e in out)


# ==== iter_remote_changes — FakeDrive-driven tests ========================

from unittest.mock import patch

from bora.changes import iter_remote_changes
from bora.core import DeletedMetadata
from tests.fake_drive import ROOT_ID, FakeDrive


def _fake_cache() -> PathIDCache:
    """Path cache pre-seeded with ROOT_ID as the sync root."""
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = ROOT_ID
    return c


def _start(fake: FakeDrive) -> str:
    """Return the current change cursor (no changes yet recorded after this)."""
    return fake.service().changes().getStartPageToken().execute()["startPageToken"]


def _drain(
    fake: FakeDrive,
    cache: PathIDCache,
    start: str,
    resolve_deleted_path=None,
) -> list:
    """Run iter_remote_changes to exhaustion; return all applied entries."""

    def factory():
        return fake.service()

    def retry(fn):
        return fn()

    pages = list(
        iter_remote_changes(
            factory, retry, cache, start, resolve_deleted_path=resolve_deleted_path
        )
    )
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
    fake.add_folder("/Docs")
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
    fid_pos = (
        len(log) - 1 - next(i for i, (_, id_) in enumerate(reversed(log)) if id_ == fid)
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


def test_iter_changes_replayed_delete_resolves_via_index_when_cache_evicted():
    """Crash-window replay (cluster-E finding 2): a delete whose fileId was
    already evicted from the path cache by the crashed run must still re-emit
    DeletedMetadata, resolved against the surviving index by remote_id, so the
    engine can remove the stranded local file + index row. Without the index
    fallback the replayed delete is silently lost and the cursor advances past
    it (the file strands until a manual rebuild)."""
    fake = FakeDrive()
    cache = _fake_cache()
    fid = fake.add_file("/gone.txt", content=b"x")
    start = _start(fake)
    # The crashed run already removed /gone.txt from the path cache (durable),
    # so the cache does NOT know it now. The engine's index row, however,
    # survives because the apply never completed and the cursor never advanced.
    fake.service().files().delete(fileId=fid).execute()

    # Stand-in for SyncEngine.get_index_entry_by_remote_id -> cased path.
    surviving_index = {fid: "/gone.txt"}

    def resolve_deleted_path(file_id):
        return surviving_index.get(file_id)

    entries, pages = _drain(
        fake, cache, start, resolve_deleted_path=resolve_deleted_path
    )

    deletes = [e for e in entries if isinstance(e, DeletedMetadata)]
    assert len(deletes) == 1, "replayed delete must be re-emitted from the index"
    assert deletes[0].path_display == "/gone.txt"


def test_iter_changes_replayed_delete_unknown_to_index_is_still_dropped():
    """The index fallback must not invent deletes: if neither the cache nor the
    index knows the fileId, the delete stays a silent no-op (nothing to
    remove)."""
    fake = FakeDrive()
    cache = _fake_cache()
    fid = fake.add_file("/ghost.txt", content=b"x")
    start = _start(fake)
    fake.service().files().delete(fileId=fid).execute()

    entries, pages = _drain(
        fake, cache, start, resolve_deleted_path=lambda file_id: None
    )
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

    import bora.changes as _changes_mod

    with patch.object(_changes_mod, "_CHANGE_PAGE_SIZE", 1):
        pages = list(
            iter_remote_changes(
                lambda: fake.service(),
                lambda fn: fn(),
                cache,
                start,
            )
        )

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
            "id": "id-B",
            "name": "report.pdf",
            "parents": ["ROOT"],
            "mimeType": "text/plain",
            "trashed": False,
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
            "id": "id-B",
            "name": "report.pdf",
            "parents": ["ROOT"],
            "mimeType": "text/plain",
            "trashed": False,
        },
    }
    _emit_apply(change, cache, out)

    assert out == [], "rejected move leaked into the feed output"
    assert cache.path_to_id("/other.txt") == "id-B"  # original path kept
    assert cache.path_to_id("/report.pdf") == "id-A"  # original owner untouched


# ==== move-out + same-path reuse (Task 4 review fix) ======================

import logging

import pytest


@pytest.mark.parametrize("create_first", [True, False])
def test_create_at_path_vacated_by_move_out_same_batch(caplog, create_first):
    """File A is moved out of scope and file B is created at A's old path
    in the same batch.  B's apply is rejected while A still occupies
    ``/report.txt`` in the cache; it must be DEFERRED (not consumed with
    a conflict warning) and applied after A's move-out delete vacates
    the path.  Both intra-batch delivery orders must work."""
    cache = _cache()
    cache.set("/report.txt", "id-a", "ROOT", "report.txt", False)

    create_b = {
        "fileId": "id-b",
        "file": {
            "id": "id-b",
            "name": "report.txt",
            "parents": ["ROOT"],
            "mimeType": "text/plain",
            "trashed": False,
        },
    }
    move_out_a = {
        "fileId": "id-a",
        "file": {
            "id": "id-a",
            "name": "report.txt",
            "parents": ["OUTSIDE"],
            "mimeType": "text/plain",
            "trashed": False,
        },
    }
    changes = [create_b, move_out_a] if create_first else [move_out_a, create_b]
    pages = [{"changes": changes, "newStartPageToken": "t2"}]

    class _Svc:
        def changes(self):
            return self

        def list(self, **kw):
            return self

        def execute(self):
            return pages.pop(0)

    with caplog.at_level(logging.WARNING, logger="bora.changes"):
        results = list(
            iter_remote_changes(lambda: _Svc(), lambda fn: fn(), cache, "t1")
        )

    entries = [e for r in results for e in r.entries]
    deletes = [e for e in entries if isinstance(e, DeletedMetadata)]
    files = [e for e in entries if isinstance(e, FileMetadata)]
    assert [d.path_display for d in deletes] == ["/report.txt"]
    assert [f.path_display for f in files] == ["/report.txt"]
    assert files[0].id == "id-b"
    assert cache.path_to_id("/report.txt") == "id-b"
    # The delete must precede the create so downstream applies in order.
    assert entries.index(deletes[0]) < entries.index(files[0])
    # B was deferred, not dropped — the change-feed conflict warning
    # must not fire (path_cache's own internal warning is out of scope).
    assert not [
        r
        for r in caplog.records
        if r.name == "bora.changes" and r.levelno >= logging.WARNING
    ]


# ==== shortcut permanent-unresolved-target warning (Task 12 review fix) ==


def test_iter_changes_shortcut_unresolved_target_warns_once(caplog):
    """A shortcut whose target never resolves must be dropped with exactly
    one per-item WARNING naming the shortcut and its unresolved target,
    once the final leftover pass gives up on it (task brief: "dropped with
    a sync-error-worthy log"; spec §"record a sync error"). The fixpoint's
    own retries (warn_on_conflict=False) must not have already logged it."""
    cache = _cache()
    change = {
        "fileId": "SL",
        "file": {
            "id": "SL",
            "name": "link",
            "parents": ["ROOT"],
            "mimeType": "application/vnd.google-apps.shortcut",
            "trashed": False,
            "shortcutDetails": {"targetId": "MISSING"},
        },
    }
    pages = [{"changes": [change], "newStartPageToken": "t2"}]

    class _Svc:
        def changes(self):
            return self

        def list(self, **kw):
            return self

        def execute(self):
            return pages.pop(0)

    with caplog.at_level(logging.WARNING, logger="bora.changes"):
        results = list(
            iter_remote_changes(lambda: _Svc(), lambda fn: fn(), cache, "t1")
        )

    entries = [e for r in results for e in r.entries]
    assert entries == [], "unresolved-target shortcut must not be emitted"
    warnings = [
        r
        for r in caplog.records
        if r.name == "bora.changes" and r.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1, warnings
    message = warnings[0].getMessage()
    assert "link" in message
    assert "MISSING" in message


def test_emit_shortcut_unresolved_target_silent_while_deferring(caplog):
    """The fixpoint's retrying calls pass warn_on_conflict=False; a shortcut
    target that is merely not-yet-resolved must stay silent so later
    batches / passes can still resolve it without spamming a warning on
    every iteration."""
    cache = _cache()
    out: list = []
    change = {
        "file": {
            "id": "SL",
            "name": "link",
            "parents": ["ROOT"],
            "mimeType": "application/vnd.google-apps.shortcut",
            "trashed": False,
            "shortcutDetails": {"targetId": "MISSING"},
        }
    }

    with caplog.at_level(logging.WARNING, logger="bora.changes"):
        result = _emit_shortcut(change, cache, out, warn_on_conflict=False)

    assert result is False
    assert out == []
    assert not [
        r
        for r in caplog.records
        if r.name == "bora.changes" and r.levelno >= logging.WARNING
    ]


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

    from bora.database.core import Database
    from bora.path_cache import PathIDCache

    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = "root"

    child = {
        "fileId": "id-c",
        "file": {
            "id": "id-c",
            "name": "kid.txt",
            "parents": ["id-p"],
            "mimeType": "text/plain",
            "trashed": False,
        },
    }
    parent = {
        "fileId": "id-p",
        "file": {
            "id": "id-p",
            "name": "newdir",
            "parents": ["root"],
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
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


# ==== expired change token → CursorResetError (Task 8 / review F5) =======


def test_invalid_page_token_raises_cursor_reset(make_http_error):
    """A 400 from changes.list (stale/invalid pageToken) must raise
    CursorResetError, not propagate as BadInputError (review F5)."""

    exc = make_http_error(400, "badRequest")

    class _Svc:
        def changes(self):
            return self

        def list(self, **kw):
            return self

        def execute(self):
            raise exc

    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = "ROOT"

    with pytest.raises(CursorResetError):
        list(iter_remote_changes(lambda: _Svc(), lambda fn: fn(), cache, "stale"))


# ==== Task 4b: export projection through the change feed ==================


def _drain_export(fake, cache, start, opts, resolve_deleted_path=None):
    """Like _drain, but threads *opts* (ExportOptions) into iter_remote_changes."""
    def factory():
        return fake.service()

    def retry(fn):
        return fn()

    pages = list(
        iter_remote_changes(
            factory, retry, cache, start,
            resolve_deleted_path=resolve_deleted_path,
            export_opts=opts,
        )
    )
    return [e for page in pages for e in page.entries], pages


def test_exportable_doc_emitted_with_projection():
    """A Doc change under export produces a projected APPLY entry whose path
    carries the extension and whose export identity is set; the name column
    keeps the raw Drive name."""
    fake = FakeDrive()
    cache = _fake_cache()
    start = _start(fake)
    fid = fake.add_native("Report", "document", parent="/")
    entries, pages = _drain_export(fake, cache, start, ExportOptions(True, False))
    paths = [e.path_display for e in entries]
    assert "/Report.docx" in paths
    assert cache.path_to_id("/Report.docx") == fid
    md = next(e for e in entries if e.path_display == "/Report.docx")
    assert isinstance(md, FileMetadata)
    assert md.export_mime == DOCX_MIME
    assert md.name == "Report"  # raw Drive name preserved
    # Raw name survives in the cache for id-keyed API calls.
    entry = cache.get_entry(fid)
    assert entry is not None and entry.name == "Report"


def test_exportable_doc_dropped_when_export_disabled():
    """With export disabled the Doc is dropped entirely — behavior identical
    to today (no entry, nothing cached)."""
    fake = FakeDrive()
    cache = _fake_cache()
    start = _start(fake)
    fake.add_native("Report", "document", parent="/")
    entries, pages = _drain(fake, cache, start)
    assert entries == []
    assert cache.path_to_id("/Report.docx") is None
    assert cache.path_to_id("/Report") is None


def test_form_still_dropped_even_with_export_enabled():
    """A Google Form is unsupported-native: dropped whether export is on or
    off. It must never appear in the emitted entries."""
    fake = FakeDrive()
    cache = _fake_cache()
    start = _start(fake)
    fake.add_native("Survey", "form", parent="/")
    entries, pages = _drain_export(fake, cache, start, ExportOptions(True, False))
    assert entries == []
    assert cache.path_to_id("/Survey") is None


def test_exportable_doc_rename_reprojects_and_keeps_raw_name():
    """A Doc renamed on Drive re-projects the path and updates the raw name,
    but never lets the projected leaf leak into the name column."""
    fake = FakeDrive()
    cache = _fake_cache()
    fid = fake.add_native("Report", "document", parent="/")
    # Seed the cache as populate would (projected path, raw name).
    cache.set("/Report.docx", fid, ROOT_ID, "Report", False)
    start = _start(fake)
    fake.service().files().update(
        fileId=fid, body={"name": "Renamed"}
    ).execute()
    entries, pages = _drain_export(fake, cache, start, ExportOptions(True, False))
    assert cache.path_to_id("/Report.docx") is None
    assert cache.path_to_id("/Renamed.docx") == fid
    entry = cache.get_entry(fid)
    assert entry is not None and entry.name == "Renamed"
