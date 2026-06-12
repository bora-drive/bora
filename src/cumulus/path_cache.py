"""Persistent mapping between remote paths and Google Drive file IDs.

Google Drive's API is ID-based: every file/folder has an opaque ``file_id``.
The sync engine (and the user) think in paths. This module bridges the gap.

Key responsibilities:

* **Path encoding** — Drive accepts characters in ``name`` (slash, control
  chars, Windows-reserved punctuation) that don't survive a filesystem
  path round-trip. The cache stores ``remote_path`` in URL-encoded form
  (via ``cumulus.path_codec``); the ``name`` column stores the raw Drive
  name as-is for upload purposes.
* **Case-fold lookup key** — ``remote_path_fold`` holds
  ``fold_path(remote_path)`` (NFC + casefold per name component). This
  replaces the simple ``.lower()`` key used in earlier prototypes; Drive
  doesn't return a server-computed fold form (Dropbox did), so we
  compute it client-side.
* **Concurrency** — every public method acquires ``self._lock`` (an
  ``RLock``) before touching the database or shared state (exception:
  the ``root_id`` property, which is a GIL-atomic attribute read set
  once at link time and needs no lock).
  ``populate_from_listing`` holds the lock across the entire
  clear + insert transaction, so concurrent reads always see either
  the old or the new cache — never a half-built one.  RLock is used
  (rather than Lock) because some public methods call other public
  methods (e.g. ``set`` calls ``has_case_conflict``).
  ``SyncEngine.sync_lock`` is no longer the sole guard for cache access.
* **Atomic populate** — ``populate_from_listing`` runs inside a single
  ``Database.transaction()``; on failure the cache is restored to its
  prior state.

See ``docs/superpowers/specs/2026-05-02-stage-1-path-cache-design.md`` for
the full design.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from .database.orm import Column, Manager, Model, NonNullColumn
from .database.types import SqlInt, SqlPath, SqlString
from .native_docs import GOOGLE_FOLDER_MIME_TYPE
from .path_codec import compose_path, decode_name, encode_name, fold_key, fold_path

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
    """URL-encoded virtual path, e.g. ``/Documents/2024%2F05 report.pdf``.

    Encoded via :func:`cumulus.path_codec.encode_name` per name component.
    The encoding makes the path safe to use as a filesystem path segment
    even when Drive ``name`` fields contain ``/``, control chars, or
    Windows-reserved punctuation."""

    remote_path_fold = NonNullColumn(SqlPath(), index=True)
    """Case-fold lookup key, computed as
    :func:`cumulus.path_codec.fold_path` (NFC + casefold per component).

    This is *not* a simple lowercase form — Drive's case-insensitive
    semantics use Unicode collation (closer to Python's ``str.casefold``
    plus NFC) than ASCII lowercase."""

    parent_id = Column(SqlString())
    """File ID of the parent folder.  ``None`` for the root."""

    name = NonNullColumn(SqlString())
    """Raw Drive name (e.g. ``2024/05 report.pdf`` literally, with the
    slash). Used by Stage 4 upload when re-naming on Drive."""

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
        self._lock = threading.RLock()

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

        ``remote_path`` is the encoded path. The lookup is
        case-insensitive (uses ``remote_path_fold``).
        """
        with self._lock:
            if remote_path == self.ROOT_PATH:
                return self._root_id

            fold = fold_path(remote_path)
            rows = self._table.select_sql(
                "WHERE remote_path_fold = ?", os.fsencode(fold)
            )
            if rows:
                return rows[0].file_id
            return None

    def id_to_path(self, file_id: str) -> str | None:
        """Return the cached remote path for *file_id*, or ``None``."""
        with self._lock:
            entry = self._table.get(file_id)
            if entry is not None:
                return entry.remote_path
            if file_id == self._root_id:
                return self.ROOT_PATH
            return None

    def get_entry(self, file_id: str) -> PathCacheEntry | None:
        """Return the full cache entry for *file_id*."""
        with self._lock:
            return self._table.get(file_id)

    # -- Mutations ---------------------------------------------------------

    def has_case_conflict(
        self, remote_path: str, file_id: str | None = None
    ) -> str | None:
        """Check whether *remote_path* case-conflicts with an existing entry.

        Returns the ``file_id`` of the conflicting entry, or ``None`` if
        there is no conflict. An entry with the same *file_id* is **not**
        considered a conflict (that's just an update of the same file).
        """
        with self._lock:
            fold = fold_path(remote_path)
            rows = self._table.select_sql(
                "WHERE remote_path_fold = ?", os.fsencode(fold)
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

        :param remote_path: encoded path (use
            :func:`cumulus.path_codec.compose_path` to derive from a Drive
            change event's parent + name).
        :param name: raw Drive ``name`` (un-encoded).

        Returns ``True`` if the entry was stored, ``False`` if it was
        rejected due to a case-insensitive name conflict with a different
        file. Updating an existing entry (same *file_id*) always succeeds.
        """
        with self._lock:
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
                remote_path_fold=fold_path(remote_path),
                parent_id=parent_id,
                name=name,
                is_folder=int(is_folder),
            )
            self._table.update(entry)  # upsert
            return True

    def remove(self, remote_path: str) -> None:
        """Remove *remote_path* and all its children from the cache.

        ``remote_path`` is the encoded path.
        """
        with self._lock:
            fold = fold_path(remote_path)

            # Delete exact match.
            rows = self._table.select_sql(
                "WHERE remote_path_fold = ?", os.fsencode(fold)
            )
            for row in rows:
                self._table.delete_primary_key(row.file_id)

            # Delete children using a range query on the BLOB column.
            children = self._select_children(fold)
            for child in children:
                self._table.delete_primary_key(child.file_id)

    def move(self, old_path: str, new_path: str) -> bool:
        """Rename *old_path* → *new_path*, updating all children atomically.

        Both paths are encoded. Returns ``False`` (and does nothing) if
        *new_path* case-conflicts with an existing entry that isn't the
        item being moved.
        """
        with self._lock:
            old_fold = fold_path(old_path)
            rows = self._table.select_sql(
                "WHERE remote_path_fold = ?", os.fsencode(old_fold)
            )
            moving_id = rows[0].file_id if rows else None

            # Check for a case conflict at the destination.
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

            new_fold = fold_path(new_path)

            # Update the entry itself: new path, new fold, new raw name. The
            # leaf of new_path is encoded; decode it to recover the raw Drive
            # name (the column stores raw form for upload).
            new_leaf_encoded = new_path.rsplit("/", 1)[-1]
            new_leaf_raw = decode_name(new_leaf_encoded)

            for row in rows:
                row.remote_path = new_path
                row.remote_path_fold = new_fold
                row.name = new_leaf_raw
                self._table.update(row)

            # Update children's paths via prefix substitution. Children's
            # `name` columns don't change — only their stored paths.
            old_prefix = old_fold + "/"
            children = self._select_children(old_fold)
            for child in children:
                fold_suffix = child.remote_path_fold[len(old_prefix):]
                child.remote_path_fold = new_fold + "/" + fold_suffix
                cased_suffix = child.remote_path[len(old_path) + 1:]
                child.remote_path = new_path + "/" + cased_suffix
                self._table.update(child)

            return True

    def resolve_path(self, file_id: str) -> str | None:
        """Walk the parent chain to reconstruct the full encoded path
        for *file_id*.

        Returns ``None`` if any ancestor is missing from the cache
        (orphan).
        """
        with self._lock:
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
                # Encode each segment as we walk so the result is the encoded
                # form even when raw `name` contains an unsafe char.
                parts.append(encode_name(entry.name))
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

        Duplicate names within the same parent (by fold key) are detected:
        the first item by file-ID lexicographic order wins; later
        duplicates are skipped with a warning.

        Atomic: either all non-skipped items are present after this
        returns, or the cache is restored to its prior state.
        """
        if self._root_id is None:
            raise RuntimeError(
                "root_id must be set before calling populate_from_listing"
            )
        root_id = self._root_id  # snapshot once; unlocked phases 1-2 are self-consistent

        # Phase 1: index by id and by parent.
        by_id: dict[str, dict[str, Any]] = {}
        children_of: dict[str, list[dict[str, Any]]] = {}

        for item in items:
            fid = item["id"]
            by_id[fid] = item

            parents = item.get("parents", [])
            parent_id = parents[0] if parents else None

            if parent_id is not None:
                children_of.setdefault(parent_id, []).append(item)

        # Phase 2: plan. Detect duplicates by fold key (NFC + casefold);
        # then BFS from root building encoded paths.
        skip_ids: set[str] = set()
        seen: dict[tuple[str | None, str], str] = {}

        for parent_id, kids in children_of.items():
            kids.sort(key=lambda x: x["id"])
            for kid in kids:
                key = (parent_id, fold_key(kid["name"]))
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

        # BFS plan; build PathCacheEntry instances without writing yet.
        plan: list[PathCacheEntry] = []
        queue: list[tuple[str, str]] = [(root_id, "/")]
        visited: set[str] = {root_id}

        while queue:
            folder_id, parent_path = queue.pop(0)
            for child in children_of.get(folder_id, []):
                cid = child["id"]
                if cid in skip_ids or cid in visited:
                    continue
                visited.add(cid)

                raw_name = child["name"]
                encoded_path = compose_path(parent_path, raw_name)
                is_folder = (
                    child.get("mimeType") == GOOGLE_FOLDER_MIME_TYPE
                )

                plan.append(
                    PathCacheEntry(
                        file_id=cid,
                        remote_path=encoded_path,
                        remote_path_fold=fold_path(encoded_path),
                        parent_id=folder_id,
                        name=raw_name,
                        is_folder=int(is_folder),
                    )
                )

                if is_folder:
                    queue.append((cid, encoded_path))

        # Log orphans (items in the listing whose parent isn't reachable
        # from root). Behavior: silently dropped from the plan,
        # observability via warning log.
        for item_id, item in by_id.items():
            if (
                item_id in visited
                or item_id in skip_ids
                or item_id == root_id
            ):
                continue
            parents = item.get("parents", [])
            parent_repr = parents[0] if parents else None
            logger.warning(
                "Orphaned file: %s (id=%s) has parent %s which is not "
                "reachable from sync root; skipping",
                item.get("name"),
                item_id,
                parent_repr,
            )

        # Phase 3: atomic swap. clear() + bulk insert under one transaction,
        # held under self._lock so concurrent reads see old or new — never
        # a half-built state.
        with self._lock, self._db.transaction():
            self._table.clear()
            for entry in plan:
                self._table.update(entry)

    def clear(self) -> None:
        """Remove all entries from the cache."""
        with self._lock:
            self._table.clear()

    def count(self) -> int:
        """Return the number of cached entries."""
        with self._lock:
            return self._table.count()

    # -- Internal helpers --------------------------------------------------

    def _select_children(self, parent_fold: str) -> list[PathCacheEntry]:
        """Select all entries whose ``remote_path_fold`` starts with
        ``parent_fold + "/"``.

        Uses a range query (``>=`` / ``<``) which works correctly on BLOB
        columns, unlike ``LIKE``.
        """
        prefix = parent_fold + "/"
        # Upper bound: increment the last character of the parent path.
        upper = parent_fold + "0"  # '0' == chr(ord('/') + 1)
        return self._table.select_sql(
            "WHERE remote_path_fold >= ? AND remote_path_fold < ?",
            os.fsencode(prefix),
            os.fsencode(upper),
        )
