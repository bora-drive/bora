"""
This model defines our core SQLite database interface.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator


class Database:
    """Wrapper around sqlite3.Connection with atomic transactions."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        self.connection = connection
        self._in_transaction = False

    def close(self) -> None:
        """Closes the SQL connection."""
        self.connection.close()

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        """
        Creates a cursor and executes the given SQL statement.

        Outside of a :meth:`transaction` block, each call auto-commits
        (wrapped in ``with self.connection:``). Inside a transaction,
        calls participate in the surrounding transaction and don't commit
        on their own.

        :param sql: SQL statement to execute.
        :param args: Parameters to substitute for placeholders in SQL statement.
        :returns: The created cursor.
        """
        if self._in_transaction:
            return self.connection.execute(sql, args)
        with self.connection:
            return self.connection.execute(sql, args)

    def executescript(self, script: str) -> None:
        """
        Creates a cursor and executes the given SQL script.

        :param script: SQL script to execute.
        """
        if self._in_transaction:
            self.connection.cursor().executescript(script)
            return
        with self.connection:
            self.connection.cursor().executescript(script)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Begin a transaction. Commits on success, rolls back on exception.

        Re-entrant calls are no-ops; only the outermost ``transaction()``
        commits or rolls back.
        """
        if self._in_transaction:
            yield
            return
        self._in_transaction = True
        try:
            with self.connection:
                yield
        finally:
            self._in_transaction = False
