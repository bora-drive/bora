"""Unit tests for bora.path_cache.PathIDCache."""

from __future__ import annotations

import sqlite3

import pytest

from bora.database.core import Database
from bora.export import ExportOptions
from bora.path_cache import SHARED_WITH_ME_ID, SHARED_WITH_ME_PATH, PathIDCache

DOC_MIME = "application/vnd.google-apps.document"


def make_cache(root_id: str = "ROOT") -> PathIDCache:
    """Construct a fresh PathIDCache backed by an in-memory SQLite DB."""
    conn = sqlite3.connect(":memory:")
    db = Database(conn)
    cache = PathIDCache(db)
    cache.root_id = root_id
    return cache


class TestSetAndLookups:
    def test_set_then_path_to_id(self) -> None:
        cache = make_cache()
        ok = cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        assert ok is True
        assert cache.path_to_id("/report.pdf") == "F1"

    def test_set_then_id_to_path(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        assert cache.id_to_path("F1") == "/report.pdf"

    def test_id_to_path_for_root(self) -> None:
        cache = make_cache()
        assert cache.id_to_path("ROOT") == "/"

    def test_path_to_id_for_root(self) -> None:
        cache = make_cache()
        assert cache.path_to_id("/") == "ROOT"

    def test_path_to_id_case_insensitive(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/Documents",
            file_id="F2",
            parent_id="ROOT",
            name="Documents",
            is_folder=True,
        )
        assert cache.path_to_id("/documents") == "F2"
        assert cache.path_to_id("/DOCUMENTS") == "F2"

    def test_path_to_id_unicode_fold(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/Caf%C3%A9",
            file_id="F3",
            parent_id="ROOT",
            name="Café",
            is_folder=True,
        )
        # Different case input still resolves.
        assert cache.path_to_id("/caf%C3%A9") == "F3"

    def test_path_to_id_missing_returns_none(self) -> None:
        cache = make_cache()
        assert cache.path_to_id("/no-such-path") is None

    def test_get_entry_returns_full_row(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        entry = cache.get_entry("F1")
        assert entry is not None
        assert entry.file_id == "F1"
        assert entry.name == "report.pdf"
        assert entry.parent_id == "ROOT"
        assert entry.is_folder == 0


class TestEncodingRoundTrip:
    @pytest.mark.parametrize(
        "raw_name,encoded_path",
        [
            ("2024/05 report.pdf", "/2024%2F05 report.pdf"),
            ("100%", "/100%25"),
            ("foo?", "/foo%3F"),
            ("foo ", "/foo%20"),
            ("foo.", "/foo%2E"),
            ("café", "/caf%C3%A9"),
        ],
    )
    def test_set_stores_raw_name_and_encoded_path(
        self, raw_name: str, encoded_path: str
    ) -> None:
        cache = make_cache()
        cache.set(
            remote_path=encoded_path,
            file_id="X",
            parent_id="ROOT",
            name=raw_name,
            is_folder=False,
        )
        # name column holds raw, remote_path column holds encoded.
        entry = cache.get_entry("X")
        assert entry is not None
        assert entry.name == raw_name
        assert entry.remote_path == encoded_path
        # Round-trip: encoded path resolves back to id, id back to path.
        assert cache.path_to_id(encoded_path) == "X"
        assert cache.id_to_path("X") == encoded_path


class TestCaseConflict:
    def test_fold_collision_within_same_parent_skipped(self) -> None:
        cache = make_cache()
        ok1 = cache.set(
            remote_path="/Documents",
            file_id="F1",
            parent_id="ROOT",
            name="Documents",
            is_folder=True,
        )
        ok2 = cache.set(
            remote_path="/documents",
            file_id="F2",
            parent_id="ROOT",
            name="documents",
            is_folder=True,
        )
        assert ok1 is True
        assert ok2 is False  # second one rejected
        # First one still resolves.
        assert cache.path_to_id("/Documents") == "F1"
        assert cache.path_to_id("/documents") == "F1"  # fold-equal

    def test_update_of_same_file_not_a_conflict(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        # Re-set with the same file_id and same path: succeeds.
        ok = cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        assert ok is True

    def test_turkish_dotted_i_collision_detected(self) -> None:
        cache = make_cache()
        ok1 = cache.set(
            remote_path="/%C4%B0stanbul",  # 'İ' encoded
            file_id="F1",
            parent_id="ROOT",
            name="İstanbul",
            is_folder=True,
        )
        # 'i' + combining-dot — fold-equal to 'İ'.
        ok2 = cache.set(
            remote_path="/i%CC%87stanbul",
            file_id="F2",
            parent_id="ROOT",
            name="i̇stanbul",
            is_folder=True,
        )
        assert ok1 is True
        assert ok2 is False


class TestRemove:
    def test_remove_deletes_entry(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        cache.remove("/report.pdf")
        assert cache.path_to_id("/report.pdf") is None

    def test_remove_cascades_to_children(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/Documents",
            file_id="DIR",
            parent_id="ROOT",
            name="Documents",
            is_folder=True,
        )
        cache.set(
            remote_path="/Documents/a.pdf",
            file_id="A",
            parent_id="DIR",
            name="a.pdf",
            is_folder=False,
        )
        cache.set(
            remote_path="/Documents/sub/b.pdf",
            file_id="B",
            parent_id="SUB",  # parent_id can be inconsistent for this test
            name="b.pdf",
            is_folder=False,
        )
        cache.remove("/Documents")
        assert cache.path_to_id("/Documents") is None
        assert cache.path_to_id("/Documents/a.pdf") is None
        assert cache.path_to_id("/Documents/sub/b.pdf") is None

    def test_remove_does_not_match_sibling_with_shared_prefix(self) -> None:
        # /foo and /foobar — removing /foo must not remove /foobar.
        cache = make_cache()
        cache.set(
            remote_path="/foo",
            file_id="A",
            parent_id="ROOT",
            name="foo",
            is_folder=True,
        )
        cache.set(
            remote_path="/foobar",
            file_id="B",
            parent_id="ROOT",
            name="foobar",
            is_folder=True,
        )
        cache.remove("/foo")
        assert cache.path_to_id("/foo") is None
        assert cache.path_to_id("/foobar") == "B"


class TestMove:
    def test_simple_rename(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/old.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="old.pdf",
            is_folder=False,
        )
        ok = cache.move("/old.pdf", "/new.pdf")
        assert ok is True
        assert cache.path_to_id("/old.pdf") is None
        assert cache.path_to_id("/new.pdf") == "F1"
        # name column updated to the raw new name.
        entry = cache.get_entry("F1")
        assert entry is not None
        assert entry.name == "new.pdf"

    def test_rename_with_slash_in_new_name(self) -> None:
        # Combined move/rename where new raw name contains '/'.
        cache = make_cache()
        cache.set(
            remote_path="/old.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="old.pdf",
            is_folder=False,
        )
        # New encoded path for raw name "2024/05.pdf"
        ok = cache.move("/old.pdf", "/2024%2F05.pdf")
        assert ok is True
        entry = cache.get_entry("F1")
        assert entry is not None
        # name column is the raw form, not the encoded form.
        assert entry.name == "2024/05.pdf"
        assert entry.remote_path == "/2024%2F05.pdf"

    def test_folder_rename_cascades_to_descendants(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/old",
            file_id="DIR",
            parent_id="ROOT",
            name="old",
            is_folder=True,
        )
        cache.set(
            remote_path="/old/a.pdf",
            file_id="A",
            parent_id="DIR",
            name="a.pdf",
            is_folder=False,
        )
        cache.set(
            remote_path="/old/sub/b.pdf",
            file_id="B",
            parent_id="SUB",
            name="b.pdf",
            is_folder=False,
        )
        ok = cache.move("/old", "/new")
        assert ok is True
        assert cache.path_to_id("/new") == "DIR"
        assert cache.path_to_id("/new/a.pdf") == "A"
        assert cache.path_to_id("/new/sub/b.pdf") == "B"
        # Descendant `name` columns must NOT change — only their paths.
        entry_a = cache.get_entry("A")
        assert entry_a is not None
        assert entry_a.name == "a.pdf"
        entry_b = cache.get_entry("B")
        assert entry_b is not None
        assert entry_b.name == "b.pdf"

    def test_move_does_not_touch_sibling_with_shared_prefix(self) -> None:
        # /foo → /bar must not affect /foobar.
        cache = make_cache()
        cache.set(
            remote_path="/foo",
            file_id="A",
            parent_id="ROOT",
            name="foo",
            is_folder=True,
        )
        cache.set(
            remote_path="/foobar",
            file_id="B",
            parent_id="ROOT",
            name="foobar",
            is_folder=True,
        )
        cache.move("/foo", "/bar")
        assert cache.path_to_id("/bar") == "A"
        assert cache.path_to_id("/foobar") == "B"

    def test_move_to_case_conflicting_target_skipped(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/A.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="A.pdf",
            is_folder=False,
        )
        cache.set(
            remote_path="/B.pdf",
            file_id="F2",
            parent_id="ROOT",
            name="B.pdf",
            is_folder=False,
        )
        # Try to rename F2 → "a.pdf" (fold-collides with F1).
        ok = cache.move("/B.pdf", "/a.pdf")
        assert ok is False
        # Both rows untouched.
        assert cache.path_to_id("/A.pdf") == "F1"
        assert cache.path_to_id("/B.pdf") == "F2"


class TestResolvePath:
    def test_resolve_simple_path(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/Documents",
            file_id="DIR",
            parent_id="ROOT",
            name="Documents",
            is_folder=True,
        )
        cache.set(
            remote_path="/Documents/report.pdf",
            file_id="F1",
            parent_id="DIR",
            name="report.pdf",
            is_folder=False,
        )
        assert cache.resolve_path("F1") == "/Documents/report.pdf"

    def test_resolve_path_with_slash_in_raw_name(self) -> None:
        # Walk reconstructs the encoded path even when raw names contain '/'.
        cache = make_cache()
        cache.set(
            remote_path="/2024%2F05",
            file_id="DIR",
            parent_id="ROOT",
            name="2024/05",
            is_folder=True,
        )
        cache.set(
            remote_path="/2024%2F05/report.pdf",
            file_id="F1",
            parent_id="DIR",
            name="report.pdf",
            is_folder=False,
        )
        assert cache.resolve_path("F1") == "/2024%2F05/report.pdf"

    def test_resolve_orphan_returns_none(self) -> None:
        # Entry whose parent isn't in the cache.
        cache = make_cache()
        cache.set(
            remote_path="/orphan.pdf",
            file_id="F1",
            parent_id="MISSING_PARENT",
            name="orphan.pdf",
            is_folder=False,
        )
        # MISSING_PARENT isn't in the table and isn't root_id.
        assert cache.resolve_path("F1") is None


from unittest.mock import patch  # noqa: E402

from bora.database.orm import Manager  # noqa: E402


def make_drive_item(
    file_id: str,
    name: str,
    parents: list[str] | None,
    is_folder: bool = False,
) -> dict:
    """Helper to build a fake `files.list` item."""
    item: dict = {
        "id": file_id,
        "name": name,
        "mimeType": (
            "application/vnd.google-apps.folder" if is_folder else "text/plain"
        ),
    }
    if parents is not None:
        item["parents"] = parents
    return item


class TestPopulateFromListing:
    def test_simple_tree(self) -> None:
        cache = make_cache()
        items = [
            make_drive_item("DIR", "Documents", parents=["ROOT"], is_folder=True),
            make_drive_item("F1", "report.pdf", parents=["DIR"]),
            make_drive_item("F2", "notes.txt", parents=["ROOT"]),
        ]
        cache.populate_from_listing(items)
        assert cache.path_to_id("/Documents") == "DIR"
        assert cache.path_to_id("/Documents/report.pdf") == "F1"
        assert cache.path_to_id("/notes.txt") == "F2"

    def test_encodes_unsafe_names(self) -> None:
        cache = make_cache()
        items = [
            make_drive_item("F1", "2024/05 report.pdf", parents=["ROOT"]),
            make_drive_item("F2", "weird?.txt", parents=["ROOT"]),
        ]
        cache.populate_from_listing(items)
        # Lookup uses encoded path.
        assert cache.path_to_id("/2024%2F05 report.pdf") == "F1"
        assert cache.path_to_id("/weird%3F.txt") == "F2"
        # Raw names preserved in `name` column.
        entry_f1 = cache.get_entry("F1")
        entry_f2 = cache.get_entry("F2")
        assert entry_f1 is not None and entry_f1.name == "2024/05 report.pdf"
        assert entry_f2 is not None and entry_f2.name == "weird?.txt"

    def test_dedup_by_fold_key_within_parent(self) -> None:
        cache = make_cache()
        items = [
            make_drive_item("F1", "Documents", parents=["ROOT"], is_folder=True),
            make_drive_item("F2", "documents", parents=["ROOT"], is_folder=True),
        ]
        cache.populate_from_listing(items)
        # Lexicographic-first id wins (F1 < F2).
        assert cache.path_to_id("/Documents") == "F1"
        # The loser is not in the cache at all.
        assert cache.get_entry("F2") is None

    def test_orphan_skipped(self) -> None:
        cache = make_cache()
        items = [
            make_drive_item("F1", "ok.txt", parents=["ROOT"]),
            make_drive_item("F2", "orphan.txt", parents=["NOT_IN_TREE"]),
        ]
        cache.populate_from_listing(items)
        assert cache.path_to_id("/ok.txt") == "F1"
        assert cache.get_entry("F2") is None

    def test_clear_then_populate(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/old.pdf",
            file_id="STALE",
            parent_id="ROOT",
            name="old.pdf",
            is_folder=False,
        )
        items = [make_drive_item("F1", "new.pdf", parents=["ROOT"])]
        cache.populate_from_listing(items)
        # Stale row gone, new row present.
        assert cache.get_entry("STALE") is None
        assert cache.path_to_id("/new.pdf") == "F1"


class TestPopulateSharedRoots:
    def test_shared_root_attaches_under_sentinel(self) -> None:
        cache = make_cache()  # root_id="ROOT"
        shared_root = {
            "id": "SR",
            "name": "TeamFolder",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["SOMEONE_ELSES_ID"],
            "sharedWithMe": True,
        }
        child = {
            "id": "SC",
            "name": "notes.txt",
            "mimeType": "text/plain",
            "parents": ["SR"],
        }
        cache.populate_from_listing([shared_root, child])
        assert cache.id_to_path("SR") == "/Shared with me/TeamFolder"
        assert cache.id_to_path("SC") == "/Shared with me/TeamFolder/notes.txt"

    def test_non_shared_orphan_still_dropped(self) -> None:
        cache = make_cache()
        orphan = {
            "id": "O",
            "name": "x.txt",
            "mimeType": "text/plain",
            "parents": ["UNREACHABLE"],  # no sharedWithMe flag
        }
        cache.populate_from_listing([orphan])
        assert cache.id_to_path("O") is None


class TestReservedNameCollision:
    def test_real_folder_named_shared_with_me_is_renamed(self) -> None:
        cache = make_cache()  # root_id="ROOT"
        real = {
            "id": "RF",
            "name": "Shared with me",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["ROOT"],
        }
        cache.populate_from_listing([real])
        path = cache.id_to_path("RF")
        assert path is not None
        assert path != "/Shared with me"
        assert path.startswith("/Shared with me")  # suffixed variant

    def test_shared_root_named_shared_with_me_not_renamed(self) -> None:
        cache = make_cache()
        # A shared root literally named "Shared with me" is a degenerate
        # but legal case: it nests one level under the sentinel and must
        # not be mistaken for the reserved-name collision (it is listed in
        # shared_root_ids, so the guard must not touch it).
        shared_root = {
            "id": "SR",
            "name": "Shared with me",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["SOMEONE_ELSES_ID"],
            "sharedWithMe": True,
        }
        cache.populate_from_listing([shared_root])
        assert cache.id_to_path("SR") == "/Shared with me/Shared with me"


class TestPopulateAtomicity:
    def test_rollback_on_mid_commit_failure(self) -> None:
        cache = make_cache()
        # Pre-populate with one row.
        cache.set(
            remote_path="/before.pdf",
            file_id="BEFORE",
            parent_id="ROOT",
            name="before.pdf",
            is_folder=False,
        )

        items = [
            make_drive_item("DIR", "Documents", parents=["ROOT"], is_folder=True),
            make_drive_item("F1", "report.pdf", parents=["DIR"]),
            make_drive_item("F2", "notes.txt", parents=["ROOT"]),
        ]

        # Patch Manager.update to raise on the 2nd call.
        original_update = Manager.update
        call_count = {"n": 0}

        def flaky_update(self, obj):  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated mid-commit failure")
            return original_update(self, obj)

        with patch.object(Manager, "update", flaky_update):
            with pytest.raises(RuntimeError, match="simulated"):
                cache.populate_from_listing(items)

        # The pre-existing row must still be queryable: rollback worked.
        assert cache.get_entry("BEFORE") is not None
        assert cache.path_to_id("/before.pdf") == "BEFORE"
        # None of the new items took effect.
        assert cache.get_entry("DIR") is None
        assert cache.get_entry("F1") is None
        assert cache.get_entry("F2") is None

    def test_planning_exception_does_not_touch_db(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/before.pdf",
            file_id="BEFORE",
            parent_id="ROOT",
            name="before.pdf",
            is_folder=False,
        )
        # An item with no `id` triggers a KeyError during planning.
        items = [{"name": "broken", "parents": ["ROOT"], "mimeType": "text/plain"}]
        with pytest.raises(KeyError):
            cache.populate_from_listing(items)
        # Pre-existing row still there.
        assert cache.get_entry("BEFORE") is not None

    def test_populate_requires_root_id(self) -> None:
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        cache = PathIDCache(db)  # root_id NOT set
        with pytest.raises(RuntimeError, match="root_id must be set"):
            cache.populate_from_listing([])


import threading  # noqa: E402

from bora.path_codec import compose_path as _compose_path  # noqa: E402
from bora.path_codec import fold_path as _fold_path  # noqa: E402


def _assert_invariants(cache: PathIDCache) -> None:
    """Walk every row and assert per-row invariants from spec §5.2.

    1. remote_path == compose_path(parent.remote_path, self.name)
       (with parent's remote_path == "/" when parent_id == root_id)
    2. remote_path_fold == fold_path(remote_path)
    """
    rows = cache._table.select_sql("")  # all rows
    by_id = {r.file_id: r for r in rows}
    for row in rows:
        # Invariant 2.
        assert row.remote_path_fold == _fold_path(row.remote_path), (
            f"row {row.file_id!r}: remote_path_fold {row.remote_path_fold!r} "
            f"!= fold_path({row.remote_path!r})"
        )
        # Invariant 1.
        if row.parent_id == cache.root_id:
            parent_path = "/"
        elif row.parent_id in by_id:
            parent_path = by_id[row.parent_id].remote_path
        else:
            # Orphan — invariant 1 doesn't apply (parent missing).
            continue
        expected = _compose_path(parent_path, row.name)
        assert row.remote_path == expected, (
            f"row {row.file_id!r}: remote_path {row.remote_path!r} != "
            f"compose_path({parent_path!r}, {row.name!r}) == {expected!r}"
        )


class TestConcurrentAccess:
    def test_concurrent_reads_during_populate_are_safe(self, tmp_path) -> None:
        """path_to_id from another thread during populate_from_listing must
        see either the old or the new cache — never a half-built one, and
        never raise (review F10)."""
        conn = sqlite3.connect(str(tmp_path / "c.db"), check_same_thread=False)
        db = Database(conn)
        cache = PathIDCache(db)
        cache.root_id = "ROOT"
        cache.set("/seed.txt", "id-seed", "ROOT", "seed.txt", False)

        listing = [
            {
                "id": f"id-{i}",
                "name": f"f{i}.txt",
                "parents": ["ROOT"],
                "mimeType": "text/plain",
            }
            for i in range(500)
        ]
        errors: list[Exception] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                try:
                    got = cache.path_to_id("/f0.txt")
                    assert got in (None, "id-0"), f"unexpected id: {got!r}"
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    return

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for _ in range(20):
            cache.populate_from_listing(listing)
        stop.set()
        for t in threads:
            t.join()
        assert errors == [], errors


class TestInvariantsAfterMutations:
    def test_invariants_after_set_move_remove_populate(self) -> None:
        cache = make_cache()
        # Populate.
        items = [
            make_drive_item("DIR", "Documents", parents=["ROOT"], is_folder=True),
            make_drive_item("F1", "report.pdf", parents=["DIR"]),
            make_drive_item("F2", "weird/name.txt", parents=["ROOT"]),
        ]
        cache.populate_from_listing(items)
        _assert_invariants(cache)

        # Set a new row.
        cache.set(
            remote_path="/Documents/added.txt",
            file_id="F3",
            parent_id="DIR",
            name="added.txt",
            is_folder=False,
        )
        _assert_invariants(cache)

        # Move the folder.
        cache.move("/Documents", "/Archive")
        _assert_invariants(cache)

        # Remove a leaf.
        cache.remove("/Archive/added.txt")
        _assert_invariants(cache)

        # Re-populate (clears prior state).
        items2 = [
            make_drive_item("X1", "fresh.pdf", parents=["ROOT"]),
        ]
        cache.populate_from_listing(items2)
        _assert_invariants(cache)


class TestSharedWithMeVirtualRoot:
    def test_id_to_path_maps_sentinel(self) -> None:
        cache = make_cache()
        assert cache.id_to_path(SHARED_WITH_ME_ID) == SHARED_WITH_ME_PATH

    def test_path_to_id_maps_reserved_path(self) -> None:
        cache = make_cache()
        assert cache.path_to_id(SHARED_WITH_ME_PATH) == SHARED_WITH_ME_ID

    def test_child_of_sentinel_resolves(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/Shared with me/Report.pdf",
            file_id="S1",
            parent_id=SHARED_WITH_ME_ID,
            name="Report.pdf",
            is_folder=False,
        )
        assert cache.resolve_path("S1") == "/Shared with me/Report.pdf"
        assert cache.id_to_path("S1") == "/Shared with me/Report.pdf"


class TestExportLeafProjection:
    """Task 4a: populate_from_listing/move/resolve_path project the export
    leaf name onto the canonical remote_path while keeping the raw Drive
    name in the ``name`` column."""

    def test_populate_projects_export_leaf(self) -> None:
        cache = make_cache()
        items = [
            {"id": "d1", "name": "Report", "mimeType": DOC_MIME,
             "parents": ["ROOT"]},
        ]
        cache.populate_from_listing(items, export_opts=ExportOptions(True, False))
        assert cache.path_to_id("/Report.docx") == "d1"
        # Raw Drive name survives in the name column (id-keyed API calls).
        entry = cache.get_entry("d1")
        assert entry is not None and entry.name == "Report"

    def test_populate_disabled_keeps_raw_leaf(self) -> None:
        cache = make_cache()
        items = [
            {"id": "d1", "name": "Report", "mimeType": DOC_MIME,
             "parents": ["ROOT"]},
        ]
        # Export disabled (default) -> byte-identical to today: no extension.
        cache.populate_from_listing(items)
        assert cache.path_to_id("/Report") == "d1"
        assert cache.path_to_id("/Report.docx") is None

    def test_populate_markdown_projects_md_extension(self) -> None:
        cache = make_cache()
        items = [
            {"id": "d1", "name": "Report", "mimeType": DOC_MIME,
             "parents": ["ROOT"]},
        ]
        cache.populate_from_listing(items, export_opts=ExportOptions(True, True))
        assert cache.path_to_id("/Report.md") == "d1"

    def test_move_preserves_raw_name(self) -> None:
        cache = make_cache()
        cache.populate_from_listing(
            [{"id": "d1", "name": "Report", "mimeType": DOC_MIME,
              "parents": ["ROOT"]}],
            export_opts=ExportOptions(True, False),
        )
        # A pure relocation: the projected leaf ('Report.docx') must NOT leak
        # into the name column — the raw Drive name stays "Report".
        cache.move("/Report.docx", "/Sub/Report.docx", name="Report")
        assert cache.path_to_id("/Sub/Report.docx") == "d1"
        entry = cache.get_entry("d1")
        assert entry is not None and entry.name == "Report"

    def test_move_without_name_still_decodes_leaf(self) -> None:
        # Regression: ordinary callers (no name arg) keep decoding the raw
        # name from the new path leaf.
        cache = make_cache()
        cache.set("/old.pdf", "F1", "ROOT", "old.pdf", False)
        cache.move("/old.pdf", "/new.pdf")
        entry = cache.get_entry("F1")
        assert entry is not None and entry.name == "new.pdf"

    def test_resolve_path_returns_projected_leaf(self) -> None:
        cache = make_cache()
        cache.populate_from_listing(
            [{"id": "d1", "name": "Report", "mimeType": DOC_MIME,
              "parents": ["ROOT"]}],
            export_opts=ExportOptions(True, False),
        )
        # resolve_path must return the stored projected path, not a path
        # rebuilt from the raw name column ("/Report").
        assert cache.resolve_path("d1") == "/Report.docx"
