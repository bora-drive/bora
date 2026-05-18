"""Google Drive change-feed processing and Drive-dict projection.

Two responsibilities:

* **Projection** — pure helpers that turn a Drive ``files`` resource
  dict into a :mod:`cumulus.core` dataclass. Shared by ``client.py``'s
  listing/metadata methods and by the change-feed code below.
* **Change feed** — :func:`iter_remote_changes` pages Drive's
  ``changes.list``, classifies each entry against the 4-row
  disambiguation table (:func:`classify`), resolves file IDs to paths
  via the path cache, mutates the cache, and yields
  :class:`~cumulus.core.ListFolderResult` pages.

The module never imports ``client.py``: it receives the service factory
and the retry helper as parameters, keeping the dependency one-way
(``client.py`` → ``changes.py``).

See ``docs/superpowers/specs/2026-05-12-stage-3-read-client-design.md``
§6 for the design.
"""

from __future__ import annotations

import enum
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterator

from .core import DeletedMetadata, FileMetadata, FolderMetadata, ListFolderResult
from .native_docs import GOOGLE_FOLDER_MIME_TYPE, is_google_native
from .path_codec import compose_path, fold_path

if TYPE_CHECKING:
    from .path_cache import PathIDCache

logger = logging.getLogger(__name__)

__all__ = [
    "FILE_FIELDS",
    "ChangeDecision",
    "classify",
    "iter_remote_changes",
    "parse_drive_time",
    "to_file_metadata",
    "to_folder_metadata",
]

# The Drive ``files`` resource fields Cumulus needs for every read. Used
# verbatim by client.py's files.get / files.list calls and embedded into
# the change-feed field selector below.
FILE_FIELDS = (
    "id, name, parents, mimeType, modifiedTime, size, md5Checksum, "
    "shared, version, trashed, lastModifyingUser(permissionId)"
)

# Page size for changes.list. 100 keeps each cache-write batch modest.
_CHANGE_PAGE_SIZE = 100

# Field selector for changes.list — the file sub-resource mirrors
# FILE_FIELDS so projected metadata is identical to the listing path.
_CHANGE_FIELDS = (
    f"nextPageToken, newStartPageToken, "
    f"changes(removed, fileId, file({FILE_FIELDS}))"
)


def parse_drive_time(value: str | None) -> datetime:
    """Parse Drive's RFC-3339 timestamp into a UTC :class:`datetime`.

    Returns the Unix epoch (UTC) when *value* is missing — the sync
    engine treats a zero timestamp as "unknown"."""
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_file_metadata(file: dict[str, Any], path_display: str) -> FileMetadata:
    """Project a Drive ``files`` resource into a :class:`FileMetadata`.

    *path_display* is the encoded virtual path the caller has already
    resolved for this file."""
    modified = parse_drive_time(file.get("modifiedTime"))
    last_user = file.get("lastModifyingUser") or {}
    return FileMetadata(
        name=file["name"],
        path_lower=fold_path(path_display),
        path_display=path_display,
        id=file["id"],
        client_modified=modified,
        server_modified=modified,
        rev=str(file.get("version") or ""),
        size=int(file.get("size", 0) or 0),
        symlink_target=None,
        shared=bool(file.get("shared", False)),
        modified_by=last_user.get("permissionId"),
        is_downloadable=True,
        content_hash=file.get("md5Checksum", ""),
    )


def to_folder_metadata(
    file: dict[str, Any], path_display: str
) -> FolderMetadata:
    """Project a Drive folder resource into a :class:`FolderMetadata`."""
    return FolderMetadata(
        name=file["name"],
        path_lower=fold_path(path_display),
        path_display=path_display,
        id=file["id"],
        shared=bool(file.get("shared", False)),
    )


class ChangeDecision(enum.Enum):
    """How :func:`iter_remote_changes` should treat a change entry."""

    APPLY = "apply"
    """Create / update / in-scope move — apply via the path cache."""
    DELETE = "delete"
    """Hard-delete, trash, or move-out — propagate a local deletion."""
    OUT_OF_SCOPE = "out_of_scope"
    """A file that was never inside the sync root — drop silently."""
    UNKNOWN_FILE = "unknown_file"
    """A malformed change entry with no file body — drop with a log."""


def classify(
    change: dict[str, Any], path_cache: "PathIDCache"
) -> ChangeDecision:
    """Classify a Drive ``changes.list`` entry per the A4 disambiguation
    table (spec §6.3).

    Row 3 of the spec table — "no parent in scope" — is split here into
    *move-out* (the file ID is still in the cache, so it left the sync
    root and the local mirror must delete it) and *never-in-scope* (the
    file ID is not in the cache, so it was created outside the sync root
    and is simply ignored).
    """
    if change.get("removed"):
        return ChangeDecision.DELETE
    file = change.get("file")
    if file is None:
        return ChangeDecision.UNKNOWN_FILE
    if file.get("trashed"):
        return ChangeDecision.DELETE
    parents = file.get("parents") or []
    in_scope = any(path_cache.id_to_path(p) is not None for p in parents)
    if not in_scope:
        if path_cache.id_to_path(file["id"]) is not None:
            return ChangeDecision.DELETE
        return ChangeDecision.OUT_OF_SCOPE
    return ChangeDecision.APPLY


def iter_remote_changes(
    service_factory: "Callable[[], Any]",
    retry: "Callable[[Callable[[], Any]], Any]",
    path_cache: "PathIDCache",
    last_cursor: str,
) -> "Iterator[ListFolderResult]":
    """Page Drive's ``changes.list`` and yield one
    :class:`~cumulus.core.ListFolderResult` per page.

    Each yielded entry has its path resolved via *path_cache*, and the
    cache is mutated as a side effect (``set`` / ``move`` / ``remove``).
    Native docs and shortcuts are dropped. The cursor on each page is
    the next ``pageToken``; on the final page it is the
    ``newStartPageToken`` to resume polling from.

    :param service_factory: zero-arg callable returning a Drive service.
    :param retry: the client's ``_retry_transient`` helper.
    :param path_cache: the path cache to resolve and mutate.
    :param last_cursor: the page token to start from.
    """
    cursor = last_cursor
    while True:
        resp = retry(
            lambda c=cursor: service_factory()
            .changes()
            .list(
                pageToken=c,
                fields=_CHANGE_FIELDS,
                pageSize=_CHANGE_PAGE_SIZE,
                includeRemoved=True,
            )
            .execute()
        )
        out_entries: list[Any] = []
        for change in resp.get("changes", []):
            decision = classify(change, path_cache)
            if decision is ChangeDecision.UNKNOWN_FILE:
                logger.debug("Skipping malformed change entry: %r", change)
                continue
            if decision is ChangeDecision.OUT_OF_SCOPE:
                continue
            if decision is ChangeDecision.DELETE:
                path = path_cache.id_to_path(change["fileId"])
                if path is None:
                    logger.debug(
                        "Delete for unknown fileId %s; nothing to remove",
                        change.get("fileId"),
                    )
                    continue
                path_cache.remove(path)
                out_entries.append(
                    DeletedMetadata(
                        name=path.rsplit("/", 1)[-1],
                        path_lower=fold_path(path),
                        path_display=path,
                    )
                )
                continue
            # decision is APPLY.
            file = change["file"]
            mime = file.get("mimeType")
            # is_google_native already covers shortcuts via the wildcard.
            if is_google_native(mime):
                continue
            parents = file.get("parents") or []
            parent_id = next(
                (p for p in parents if path_cache.id_to_path(p) is not None),
                None,
            )
            # classify() guaranteed at least one in-scope parent.
            parent_path = path_cache.id_to_path(parent_id)
            new_path = compose_path(parent_path, file["name"])
            old_path = path_cache.id_to_path(file["id"])
            is_folder = mime == GOOGLE_FOLDER_MIME_TYPE
            if old_path is None:
                path_cache.set(
                    new_path, file["id"], parent_id, file["name"], is_folder
                )
            elif old_path != new_path:
                path_cache.move(old_path, new_path)
            # else: pure content update; cache path already correct.
            if is_folder:
                out_entries.append(to_folder_metadata(file, new_path))
            else:
                out_entries.append(to_file_metadata(file, new_path))

        next_token = resp.get("nextPageToken")
        new_start = resp.get("newStartPageToken")
        cursor = next_token or new_start or ""
        yield ListFolderResult(
            entries=out_entries,
            has_more=bool(next_token),
            cursor=cursor,
        )
        if not next_token:
            break
