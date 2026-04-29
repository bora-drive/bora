"""Persistent mapping between remote paths and Google Drive file IDs.

Google Drive's API is ID-based: every file/folder has an opaque ``file_id``.
The sync engine (and the user) think in paths. This module bridges the gap.

Design decisions (see REVIEW.md Issue #2):

* **Concurrency** — the path cache shares ``SyncEngine.sync_lock``. It is only
  accessed during sync operations that already hold the lock, so no additional
  lock is needed.
* **Initial population** — a single filtered ``files.list`` call (``trashed=false``)
  retrieves all items, then the tree is reconstructed in-memory from parent
  references before bulk-inserting into SQLite.
* **Case-insensitive uniqueness** — Cumulus enforces Dropbox-like
  case-insensitivity: two items whose names differ only by case are treated as
  duplicates.  During bulk population the first item (by file-ID lexicographic
  order) wins; during incremental updates (``set`` / ``move``) the existing
  entry wins.  Conflicts are logged as warnings; users should fix them in the
  Google Drive web UI.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from .database.orm import Column, Manager, Model, NonNullColumn
from .database.types import SqlInt, SqlPath, SqlString
from .native_docs import GOOGLE_FOLDER_MIME_TYPE

if TYPE_CHECKING:
    from .database.core import Database

__all__ = ["PathCacheEntry", "PathIDCache"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class PathCacheEntry(Model):
    """One row per known file or folder on Google Drive."""

    __tablename__ = "path_cache"

    file_id = NonNullColumn(SqlString(), primary_key=True)
    """Google Drive file ID (unique, opaque string)."""

    remote_path = NonNullColumn(SqlPath(), index=True)
    """Canonical remote path, e.g. ``/Documents/report.pdf``.
    Cased as it appears on Drive."""

    remote_path_lower = NonNullColumn(SqlPath(), index=True)
    """Lower-cased remote path, used for case-insensitive lookups."""

    parent_id = Column(SqlString())
    """File ID of the parent folder.  ``None`` for the root."""

    name = NonNullColumn(SqlString())
    """File/folder name as it appears on Drive."""

    is_folder = NonNullColumn(SqlInt())
    """1 for folders, 0 for files."""


# ---------------------------------------------------------------------------
# Cache implementation
# ---------------------------------------------------------------------------


class PathIDCache:
    """Bidirectional path ↔ file-ID mapping backed by SQLite.

    :param db: An open :class:`~cumulus.database.core.Database` instance
        (the same one ``SyncEngine`` uses).
    """

    # The root folder has a well-known alias.
    ROOT_PATH = "/"

    def __init__(self, db: Database) -> None:
        self._db = db
        self._table: Manager[PathCacheEntry] = Manager(db, PathCacheEntry)
        self._root_id: str | None = None

    # -- Root management ---------------------------------------------------

    @property
    def root_id(self) -> str | None:
        """The Google Drive ID of the sync root ("My Drive" root folder)."""
        return self._root_id

    @root_id.setter
    def root_id(self, value: str) -> None:
        self._root_id = value

    # -- Core lookups ------------------------------------------------------

    def path_to_id(self, remote_path: str) -> str | None:
        """Return the file ID for *remote_path*, or ``None`` if not cached.

        The lookup is **case-insensitive**.
        """
        if remote_path == self.ROOT_PATH:
            return self._root_id

        lower = remote_path.lower()
        rows = self._table.select_sql(
            "WHERE remote_path_lower = ?", os.fsencode(lower)
        )
        if rows:
            return rows[0].file_id
        return None

    def id_to_path(self, file_id: str) -> str | None:
        """Return the cached remote path for *file_id*, or ``None``."""
        entry = self._table.get(file_id)
        if entry is not None:
            return entry.remote_path
        if file_id == self._root_id:
            return self.ROOT_PATH
        return None

    def get_entry(self, file_id: str) -> PathCacheEntry | None:
        """Return the full cache entry for *file_id*."""
        return self._table.get(file_id)

    # -- Mutations ---------------------------------------------------------

    def has_case_conflict(
        self, remote_path: str, file_id: str | None = None
    ) -> str | None:
        """Check whether *remote_path* case-conflicts with an existing entry.

        Returns the ``file_id`` of the conflicting entry, or ``None`` if there
        is no conflict.  An entry with the same *file_id* is **not** considered
        a conflict (that's just an update of the same file).
        """
        lower = remote_path.lower()
        rows = self._table.select_sql(
            "WHERE remote_path_lower = ?", os.fsencode(lower)
        )
        for row in rows:
            if row.file_id != file_id:
                return row.file_id
        return None

    def set(
        self,
        remote_path: str,
        file_id: str,
        parent_id: str | None,
        name: str,
        is_folder: bool,
    ) -> bool:
        """Insert or update a cache entry.

        Returns ``True`` if the entry was stored, ``False`` if it was rejected
        due to a case-insensitive name conflict with a *different* file.
        Updating an existing entry (same *file_id*) always succeeds.
        """
        conflict_id = self.has_case_conflict(remote_path, file_id)
        if conflict_id is not None:
            logger.warning(
                "Case conflict: '%s' (id=%s) conflicts with existing "
                "entry (id=%s). Skipping — rename one of the files in "
                "the Google Drive web UI to fix this.",
                remote_path,
                file_id,
                conflict_id,
            )
            return False

        entry = PathCacheEntry(
            file_id=file_id,
            remote_path=remote_path,
            remote_path_lower=remote_path.lower(),
            parent_id=parent_id,
            name=name,
            is_folder=int(is_folder),
        )
        self._table.update(entry)  # upsert
        return True

    def remove(self, remote_path: str) -> None:
        """Remove *remote_path* and all its children from the cache."""
        lower = remote_path.lower()

        # Delete exact match.
        rows = self._table.select_sql(
            "WHERE remote_path_lower = ?", os.fsencode(lower)
        )
        for row in rows:
            self._table.delete_primary_key(row.file_id)

        # Delete children using a range query on the BLOB column.
        children = self._select_children(lower)
        for child in children:
            self._table.delete_primary_key(child.file_id)

    def move(self, old_path: str, new_path: str) -> bool:
        """Rename *old_path* → *new_path*, updating all children atomically.

        Returns ``False`` (and does nothing) if *new_path* case-conflicts with
        an existing entry that isn't the item being moved.
        """
        # Check for a case conflict at the destination.
        old_lower = old_path.lower()
        rows = self._table.select_sql(
            "WHERE remote_path_lower = ?", os.fsencode(old_lower)
        )
        moving_id = rows[0].file_id if rows else None
        conflict_id = self.has_case_conflict(new_path, moving_id)
        if conflict_id is not None:
            logger.warning(
                "Case conflict on move: '%s' → '%s' conflicts with "
                "existing entry (id=%s). Skipping move.",
                old_path,
                new_path,
                conflict_id,
            )
            return False

        new_lower = new_path.lower()

        # Update the entry itself.
        for row in rows:
            row.remote_path = new_path
            row.remote_path_lower = new_lower
            row.name = new_path.rsplit("/", 1)[-1]
            self._table.update(row)

        # Update children.
        old_prefix = old_lower + "/"
        children = self._select_children(old_lower)
        for child in children:
            # Replace only the leading prefix.
            suffix = child.remote_path_lower[len(old_prefix):]
            child.remote_path_lower = new_lower + "/" + suffix
            # Preserve casing of the suffix from the cased path.
            cased_suffix = child.remote_path[len(old_path) + 1:]
            child.remote_path = new_path + "/" + cased_suffix
            self._table.update(child)

        return True

    def resolve_path(self, file_id: str) -> str | None:
        """Walk the parent chain to reconstruct the full path for *file_id*.

        Returns ``None`` if any ancestor is missing from the cache (orphan).
        """
        parts: list[str] = []
        current_id: str | None = file_id

        while current_id and current_id != self._root_id:
            entry = self._table.get(current_id)
            if entry is None:
                logger.warning(
                    "Orphaned file: cannot resolve parent chain for %s",
                    file_id,
                )
                return None
            parts.append(entry.name)
            current_id = entry.parent_id

        if current_id is None:
            # Walked off the tree without reaching root — orphan.
            logger.warning(
                "Orphaned file: parent chain for %s does not reach root", file_id
            )
            return None

        parts.reverse()
        return "/" + "/".join(parts)

    # -- Bulk operations ---------------------------------------------------

    def populate_from_listing(self, items: list[dict[str, Any]]) -> None:
        """Build the entire cache from a flat list of Google Drive file resources.

        Each dict should contain at least::

            {"id": str, "name": str, "parents": [str, ...], "mimeType": str}

        Duplicate names within the same parent are detected: the first item
        (by file-ID lexicographic order) wins; later duplicates are skipped
        with a warning.
        """
        self.clear()

        if self._root_id is None:
            raise RuntimeError(
                "root_id must be set before calling populate_from_listing"
            )

        # Index items by ID for parent-chain resolution.
        by_id: dict[str, dict[str, Any]] = {}
        children_of: dict[str, list[dict[str, Any]]] = {}  # parent_id -> [items]

        for item in items:
            fid = item["id"]
            by_id[fid] = item

            parents = item.get("parents", [])
            # Pick the first parent as canonical (see PLAN.md §3.1).
            parent_id = parents[0] if parents else None
            item["_parent_id"] = parent_id

            if parent_id is not None:
                children_of.setdefault(parent_id, []).append(item)

        # Detect duplicates: (parent_id, lower_name) -> winning file_id.
        seen: dict[tuple[str | None, str], str] = {}
        skip_ids: set[str] = set()

        for parent_id, kids in children_of.items():
            # Sort by file_id for deterministic winner selection.
            kids.sort(key=lambda x: x["id"])
            for kid in kids:
                key = (parent_id, kid["name"].lower())
                if key in seen:
                    skip_ids.add(kid["id"])
                    logger.warning(
                        "Skipping duplicate name '%s' in folder %s "
                        "(keeping %s, skipping %s)",
                        kid["name"],
                        parent_id,
                        seen[key],
                        kid["id"],
                    )
                else:
                    seen[key] = kid["id"]

        # BFS from root to build paths.
        queue: list[tuple[str, str]] = [(self._root_id, "")]  # (folder_id, path_prefix)
        visited: set[str] = {self._root_id}

        while queue:
            folder_id, prefix = queue.pop(0)
            for child in children_of.get(folder_id, []):
                cid = child["id"]
                if cid in skip_ids or cid in visited:
                    continue
                visited.add(cid)

                name = child["name"]
                path = f"{prefix}/{name}"
                is_folder = (
                    child.get("mimeType") == GOOGLE_FOLDER_MIME_TYPE
                )

                self.set(
                    remote_path=path,
                    file_id=cid,
                    parent_id=folder_id,
                    name=name,
                    is_folder=is_folder,
                )

                if is_folder:
                    queue.append((cid, path))

    def clear(self) -> None:
        """Remove all entries from the cache."""
        self._table.clear()

    def count(self) -> int:
        """Return the number of cached entries."""
        return self._table.count()

    # -- Internal helpers --------------------------------------------------

    def _select_children(self, parent_lower: str) -> list[PathCacheEntry]:
        """Select all entries whose ``remote_path_lower`` starts with
        ``parent_lower + "/"``.

        Uses a range query (``>=`` / ``<``) which works correctly on BLOB
        columns, unlike ``LIKE``.
        """
        prefix = parent_lower + "/"
        # Upper bound: increment the last character of the parent path.
        upper = parent_lower + "0"  # '0' == chr(ord('/') + 1)
        return self._table.select_sql(
            "WHERE remote_path_lower >= ? AND remote_path_lower < ?",
            os.fsencode(prefix),
            os.fsencode(upper),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


