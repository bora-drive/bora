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

from datetime import datetime, timezone
from typing import Any

from .core import FileMetadata, FolderMetadata
from .path_codec import fold_path

__all__ = [
    "FILE_FIELDS",
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
