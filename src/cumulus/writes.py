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
from .exceptions import (
    BadInputError,
    CumulusApiError,
    DataChangedError,
    NotFoundError,
)
from .path_codec import decode_name, encode_name, fold_path

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

    def _resolve_file_id(self, remote_path: str) -> str:
        """Look up the Drive file ID for ``remote_path`` in the cache.

        Raises ``NotFoundError`` when the path is not known. Used by
        every write op that targets an existing item (remove, move,
        upload-update, restore).
        """
        file_id = self._client._path_cache.path_to_id(remote_path)
        if file_id is None:
            raise NotFoundError(
                "Not found",
                f"No item at {remote_path!r} is known to Cumulus.",
                remote_path=remote_path,
            )
        return file_id

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

    def _autorename_unique(self, parent_path: str, encoded_name: str) -> str:
        """Return an encoded path segment not already in path_cache under
        parent_path.

        Tries ``encoded_name``, ``encoded(name (1))``, ``encoded(name (2))``,
        … up to N=999. Probes are done in encoded namespace so collisions
        for names containing slashes or other special chars are detected
        correctly. Used by upload, move, and make_dir when autorename=True.
        """
        cache = self._client._path_cache
        if cache.path_to_id(parent_path + "/" + encoded_name) is None:
            return encoded_name
        decoded = decode_name(encoded_name)
        stem, ext = os.path.splitext(decoded)
        for n in range(1, _AUTORENAME_LIMIT + 1):
            candidate_decoded = f"{stem} ({n}){ext}"
            candidate_encoded = encode_name(candidate_decoded)
            if cache.path_to_id(parent_path + "/" + candidate_encoded) is None:
                return candidate_encoded
        raise BadInputError(
            "Autorename exhausted",
            f"Could not find a non-colliding name for {encoded_name!r} under "
            f"{parent_path!r} within {_AUTORENAME_LIMIT} attempts.",
            remote_path=parent_path + "/" + encoded_name,
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
        client = self._client
        client._ensure_root_id()
        file_id = self._resolve_file_id(remote_path)

        if parent_rev is not None:
            with convert_api_errors(remote_path=remote_path):
                meta = client._retry_transient(
                    lambda: client._get_service()
                    .files()
                    .get(fileId=file_id,
                         fields="headRevisionId,mimeType")
                    .execute()
                )
            client._count_request()
            if meta.get("mimeType") == _FOLDER_MIME:
                raise BadInputError(
                    "Invalid operation",
                    "parent_rev cannot be specified for a folder.",
                    remote_path=remote_path,
                )
            if meta.get("headRevisionId") != parent_rev:
                raise DataChangedError(
                    "Item changed on Google Drive",
                    f"{remote_path!r} changed on Drive since Cumulus last "
                    "saw it.",
                    remote_path=remote_path,
                )

        with convert_api_errors(remote_path=remote_path):
            resp = client._retry_transient(
                lambda: client._get_service()
                .files()
                .update(
                    fileId=file_id,
                    body={"trashed": True},
                    fields="id,name,mimeType,trashed",
                )
                .execute()
            )
        client._count_request()
        client._path_cache.remove(remote_path)
        return self._wrap_metadata(resp, remote_path)

    def _hard_delete(self, remote_path: str) -> None:
        """Irreversibly delete via ``files.delete``. Internal primitive
        for a future Stage 6 CLI flag — not exposed on the public surface
        in Stage 4."""
        client = self._client
        client._ensure_root_id()
        file_id = self._resolve_file_id(remote_path)
        with convert_api_errors(remote_path=remote_path):
            client._retry_transient(
                lambda: client._get_service()
                .files()
                .delete(fileId=file_id)
                .execute()
            )
        client._count_request()
        client._path_cache.remove(remote_path)

    def _wrap_metadata(
        self, resp: dict[str, Any], remote_path: str
    ) -> "FileMetadata | FolderMetadata":
        """Wrap a Drive ``files.update`` / ``files.create`` response in
        FileMetadata or FolderMetadata. Minimal projection used by remove
        and move; upload uses its own richer projection.

        ``modifiedTime`` is read when present (e.g. move responses);
        when absent (e.g. trash responses omit it), an epoch sentinel
        is used.
        """
        if resp.get("mimeType") == _FOLDER_MIME:
            return FolderMetadata(
                name=resp.get("name", ""),
                path_lower=fold_path(remote_path),
                path_display=remote_path,
                id=resp["id"],
                shared=False,
            )
        from .changes import parse_drive_time
        modified = parse_drive_time(resp.get("modifiedTime"))
        return FileMetadata(
            name=resp.get("name", ""),
            path_lower=fold_path(remote_path),
            path_display=remote_path,
            id=resp["id"],
            client_modified=modified,
            server_modified=modified,
            rev=resp.get("headRevisionId", ""),
            size=int(resp.get("size", 0) or 0),
            symlink_target=None,
            shared=False,
            modified_by=None,
            is_downloadable=True,
            content_hash=resp.get("md5Checksum", ""),
        )

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
        if autorename:
            raw_name = self._autorename_unique(parent_path, raw_name)
            remote_path = parent_path + "/" + raw_name
        drive_name = self._decode_segment(raw_name)
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
