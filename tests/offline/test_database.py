"""Unit tests for cumulus.database.core.Database.transaction."""

from __future__ import annotations

import sqlite3

import pytest

from cumulus.database.core import Database
from cumulus.database.orm import Manager, Model, NonNullColumn
from cumulus.database.types import SqlInt, SqlString


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
