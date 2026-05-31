"""Stage 4 — write-side helper.

``_WriteHelper`` owns the mutating-op orchestration on top of the Drive
v3 service that ``GoogleDriveClient`` exposes. It mirrors the Stage 3
``changes.py`` extraction: ``client.py`` keeps a thin public façade and
delegates every mutating method here. Cache write-through is owned by
this module — every successful op mutates the client's ``PathIDCache``
synchronously before returning.

See ``docs/superpowers/specs/2026-05-30-stage-4-write-client-design.md``
for the design and behaviour table.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Sequence

from .core import FileMetadata, FolderMetadata, WriteMode
from .errorhandling import convert_api_errors
from .exceptions import BadInputError, CumulusApiError, NotFoundError
from .path_codec import decode_name, fold_path

if TYPE_CHECKING:  # avoid runtime cycle
    from .client import GoogleDriveClient
    from .models import SyncEvent

logger = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_AUTORENAME_LIMIT = 999


class _WriteHelper:
    """Owner of all mutating Drive API calls.

    Constructed once by ``GoogleDriveClient.__init__`` with a back-reference
    to the client; reads the client's ``_get_service``, ``_retry_transient``,
    ``_count_request``, ``_ensure_root_id``, ``_path_cache``, and
    ``_poll_event`` through that reference.
    """

    def __init__(self, client: "GoogleDriveClient") -> None:
        self._client = client

    # ---- shared helpers --------------------------------------------------

    @staticmethod
    def _split_parent_name(remote_path: str) -> tuple[str, str]:
        """Split ``remote_path`` into (parent_path, name). Root parent is "".

        ``/foo/bar`` -> ("/foo", "bar"). ``/foo`` -> ("", "foo").
        Raises BadInputError for non-path-shaped inputs.
        """
        if "/" not in remote_path or remote_path == "/":
            raise BadInputError(
                "Invalid path",
                f"Cannot derive a parent + name from {remote_path!r}.",
                remote_path=remote_path,
            )
        parent, name = remote_path.rsplit("/", 1)
        return parent, name

    @staticmethod
    def _decode_segment(name: str) -> str:
        """Decode a path segment via path_codec for the Drive ``name``."""
        return decode_name(name)

    def _resolve_parent_id(self, parent_path: str, *, for_path: str) -> str:
        cache = self._client._path_cache
        if parent_path == "":
            assert cache.root_id is not None
            return cache.root_id
        parent_id = cache.path_to_id(parent_path)
        if parent_id is None:
            raise NotFoundError(
                "Parent folder not found",
                f"The parent of {for_path!r} is not known to Cumulus.",
                remote_path=for_path,
            )
        return parent_id

    def _autorename_unique(self, parent_path: str, name: str) -> str:
        """Return a name not already in path_cache under parent_path.

        Tries ``name``, ``name (1)``, ``name (2)``, … up to N=999. Used by
        upload, move, and make_dir when ``autorename=True``.
        """
        cache = self._client._path_cache
        joiner = "" if parent_path == "" else parent_path
        if cache.path_to_id(joiner + "/" + name) is None:
            return name
        stem, ext = os.path.splitext(name)
        for n in range(1, _AUTORENAME_LIMIT + 1):
            candidate = f"{stem} ({n}){ext}"
            if cache.path_to_id(joiner + "/" + candidate) is None:
                return candidate
        raise BadInputError(
            "Autorename exhausted",
            f"Could not find a non-colliding name for {name!r} under "
            f"{parent_path!r} within {_AUTORENAME_LIMIT} attempts.",
            remote_path=joiner + "/" + name,
        )

    # ---- public API (filled in by later tasks) --------------------------

    def upload(
        self,
        local_path: str,
        remote_path: str,
        *,
        write_mode: WriteMode = WriteMode.Add,
        update_rev: str | None = None,
        autorename: bool = False,
        sync_event: "SyncEvent | None" = None,
    ) -> FileMetadata:
        raise NotImplementedError  # task 6 / task 7

    def remove(
        self, remote_path: str, parent_rev: str | None = None
    ) -> "FileMetadata | FolderMetadata":
        raise NotImplementedError  # task 4

    def remove_batch(
        self,
        entries: Sequence[tuple[str, str | None]],
        batch_size: int = 100,
    ) -> "list[FileMetadata | FolderMetadata | CumulusApiError]":
        raise NotImplementedError  # task 9

    def move(
        self, remote_path: str, new_path: str, *, autorename: bool = False
    ) -> "FileMetadata | FolderMetadata":
        raise NotImplementedError  # task 5

    def make_dir(
        self, remote_path: str, *, autorename: bool = False
    ) -> FolderMetadata:
        client = self._client
        client._ensure_root_id()
        parent_path, raw_name = self._split_parent_name(remote_path)
        drive_name = self._decode_segment(raw_name)
        if autorename:
            drive_name = self._autorename_unique(parent_path, drive_name)
            # If autorename picked a suffixed name, the effective
            # remote_path also shifts so the cache write-through stores
            # under the actual stored path.
            remote_path = parent_path + "/" + drive_name
        parent_id = self._resolve_parent_id(parent_path, for_path=remote_path)

        body = {
            "name": drive_name,
            "mimeType": _FOLDER_MIME,
            "parents": [parent_id],
        }
        with convert_api_errors(remote_path=remote_path):
            resp = client._retry_transient(
                lambda: client._get_service()
                .files()
                .create(body=body, fields="id,name,mimeType")
                .execute()
            )
        client._count_request()
        client._path_cache.set(
            remote_path,
            resp["id"],
            parent_id=parent_id,
            name=drive_name,
            is_folder=True,
        )
        return FolderMetadata(
            name=resp["name"],
            path_lower=fold_path(remote_path),
            path_display=remote_path,
            id=resp["id"],
            shared=False,
        )

    def make_dir_batch(
        self, remote_paths: list[str]
    ) -> "list[FolderMetadata | CumulusApiError]":
        raise NotImplementedError  # task 8

    def restore(self, remote_path: str, rev: str) -> FileMetadata:
        raise NotImplementedError  # task 10
