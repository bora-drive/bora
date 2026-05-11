"""Unit tests for cumulus.database.core.Database.transaction."""

from __future__ import annotations

import sqlite3

import pytest

from cumulus.database.core import Database


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
