"""Unit tests for bora.database.core.Database.transaction."""

from __future__ import annotations

import sqlite3

import pytest

from bora.database.core import Database
from bora.database.orm import Manager, Model, NonNullColumn
from bora.database.types import SqlInt, SqlString
from bora.models import IndexEntry, ItemType


@pytest.fixture
def db() -> Database:
    conn = sqlite3.connect(":memory:")
    db = Database(conn)
    db.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT);")
    return db


class TestTransaction:
    def test_commit_on_success(self, db: Database) -> None:
        with db.transaction():
            db.execute("INSERT INTO t (val) VALUES (?)", "a")
            db.execute("INSERT INTO t (val) VALUES (?)", "b")
        rows = db.execute("SELECT val FROM t").fetchall()
        assert {r["val"] for r in rows} == {"a", "b"}

    def test_rollback_on_exception(self, db: Database) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with db.transaction():
                db.execute("INSERT INTO t (val) VALUES (?)", "a")
                raise RuntimeError("boom")
        # Insert was rolled back.
        rows = db.execute("SELECT val FROM t").fetchall()
        assert rows == []

    def test_execute_outside_transaction_still_auto_commits(
        self, db: Database
    ) -> None:
        # Existing behavior preserved.
        db.execute("INSERT INTO t (val) VALUES (?)", "a")
        rows = db.execute("SELECT val FROM t").fetchall()
        assert [r["val"] for r in rows] == ["a"]

    def test_nested_transaction_does_not_double_commit(
        self, db: Database
    ) -> None:
        # Re-entrant: inner is a no-op flag-wise; outer commits/rolls back.
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.execute("INSERT INTO t (val) VALUES (?)", "a")
                with db.transaction():
                    db.execute("INSERT INTO t (val) VALUES (?)", "b")
                raise RuntimeError("outer fails")
        rows = db.execute("SELECT val FROM t").fetchall()
        assert rows == []  # both rolled back


class _Widget(Model):
    __tablename__ = "widget"
    id = NonNullColumn(SqlInt(), primary_key=True)
    label = NonNullColumn(SqlString())


class TestManagerClear:
    """Manager.clear() must DELETE FROM rows (table survives).

    The previous implementation did DROP TABLE, which left subsequent
    Manager operations (update/save/has) querying a non-existent table.
    Atomic populate (Task 8) depends on clear-then-insert under a single
    transaction.
    """

    def test_clear_leaves_table_intact(self) -> None:
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        mgr: Manager[_Widget] = Manager(db, _Widget)
        mgr.save(_Widget(id=1, label="a"))
        mgr.save(_Widget(id=2, label="b"))
        assert mgr.count() == 2
        mgr.clear()
        # Table is still queryable — rows are gone but schema persists.
        assert mgr.count() == 0
        # New inserts work without re-creating the table.
        mgr.save(_Widget(id=3, label="c"))
        assert mgr.count() == 1

    def test_clear_drops_weakref_cache(self) -> None:
        # The weakref cache should not return stale entries after clear.
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        mgr: Manager[_Widget] = Manager(db, _Widget)
        w = _Widget(id=1, label="a")
        mgr.save(w)
        # Hold a strong reference (the cache is weak) and verify lookup works.
        assert mgr.get(1) is w
        mgr.clear()
        # After clear the row is gone from the DB; lookup must reflect that.
        assert mgr.get(1) is None

    def test_clear_then_update_inside_transaction(self) -> None:
        # Happy path: clear + update in one transaction commits cleanly.
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        mgr: Manager[_Widget] = Manager(db, _Widget)
        mgr.save(_Widget(id=1, label="old"))
        with db.transaction():
            mgr.clear()
            mgr.update(_Widget(id=2, label="new"))
        assert mgr.count() == 1
        assert mgr.get(2) is not None
        assert mgr.get(1) is None

    def test_clear_inside_transaction_rolls_back(self) -> None:
        # Atomicity: if the transaction fails after a clear(), the prior
        # rows must still be present. This is the contract atomic
        # populate_from_listing relies on.
        #
        # The previous DROP+CREATE implementation broke this because
        # executescript() in Python's sqlite3 module force-commits any
        # pending transaction before executing the script — silently
        # committing the DROP and breaking atomicity.
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        mgr: Manager[_Widget] = Manager(db, _Widget)
        mgr.save(_Widget(id=1, label="old"))

        with pytest.raises(RuntimeError, match="boom"):
            with db.transaction():
                mgr.clear()
                mgr.update(_Widget(id=2, label="new"))
                raise RuntimeError("boom")

        # The pre-transaction row must still be there.
        assert mgr.count() == 1
        assert mgr.get(1) is not None
        assert mgr.get(2) is None


class TestManagerSchemaDriftGuard:
    """Manager.__init__ must drop+recreate a table whose on-disk columns
    don't match the model (schema drift, e.g. after a column was added in
    a newer release), but must never drop a table whose columns already
    match -- these tables are a derived cache, so DROP+recreate is the
    sanctioned migration policy, never ALTER TABLE.
    """

    def test_drops_and_recreates_table_with_old_column_set(self) -> None:
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        # Simulate a pre-feature DB: a "widget" table missing the "label"
        # column that _Widget now defines, and holding a stale row.
        db.executescript(
            "CREATE TABLE widget (id INTEGER PRIMARY KEY, old_col TEXT);"
            "INSERT INTO widget (id, old_col) VALUES (1, 'stale');"
        )

        mgr: Manager[_Widget] = Manager(db, _Widget)

        # The table was dropped and recreated with the model's schema.
        assert mgr.count() == 0
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(widget);").fetchall()
        }
        assert columns == {"id", "label"}

        # And it is fully usable afterwards.
        mgr.save(_Widget(id=1, label="fresh"))
        assert mgr.count() == 1

    def test_matching_schema_is_not_dropped(self) -> None:
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        mgr: Manager[_Widget] = Manager(db, _Widget)
        mgr.save(_Widget(id=1, label="a"))
        mgr.save(_Widget(id=2, label="b"))
        assert mgr.count() == 2

        # Re-opening a Manager against the same, already-matching table
        # must not touch existing rows.
        mgr2: Manager[_Widget] = Manager(db, _Widget)
        assert mgr2.count() == 2
        assert mgr2.get(1) is not None
        assert mgr2.get(1).label == "a"  # type: ignore[union-attr]

    def test_missing_table_is_a_no_op(self) -> None:
        # Fresh database: no table exists yet. The guard must not raise and
        # must leave table creation to create_table_if_not_exists.
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        mgr: Manager[_Widget] = Manager(db, _Widget)
        assert mgr.count() == 0


class TestIndexEntryDownloadOnly:
    """Round-trip persistence for IndexEntry.download_only (Task 6)."""

    def _mgr(self) -> Manager[IndexEntry]:
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        return Manager(db, IndexEntry)

    def test_download_only_round_trips_true(self) -> None:
        mgr = self._mgr()
        mgr.save(
            IndexEntry(
                remote_path_lower="/doc.gdoc",
                remote_path_cased="/doc.gdoc",
                remote_id="id1",
                item_type=ItemType.File,
                rev="r1",
                read_only=0,
                download_only=1,
            )
        )
        # Bypass the weakref object cache so we exercise the actual SQL
        # round trip, not just the in-memory object we just saved.
        mgr.clear_cache()

        entry = mgr.get("/doc.gdoc")
        assert entry is not None
        assert entry.download_only == 1
        assert entry.is_download_only is True

    def test_legacy_row_without_download_only_reads_back_falsy(self) -> None:
        # A row saved without ever specifying download_only (as any row
        # written before this column existed effectively would be, since
        # the schema-drift guard recreates the table empty and the column
        # is nullable) must read back falsy, not raise.
        mgr = self._mgr()
        mgr.save(
            IndexEntry(
                remote_path_lower="/legacy.txt",
                remote_path_cased="/legacy.txt",
                remote_id="id2",
                item_type=ItemType.File,
                rev="r1",
                read_only=0,
            )
        )
        mgr.clear_cache()

        entry = mgr.get("/legacy.txt")
        assert entry is not None
        assert entry.download_only is None
        assert entry.is_download_only is False
