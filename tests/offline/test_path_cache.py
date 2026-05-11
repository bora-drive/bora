"""Unit tests for cumulus.path_cache.PathIDCache."""

from __future__ import annotations

import sqlite3

import pytest

from cumulus.database.core import Database
from cumulus.path_cache import PathIDCache


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
