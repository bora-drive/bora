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
from typing import TYPE_CHECKING, Any, Sequence

from .core import FileMetadata, FolderMetadata, WriteMode
from .exceptions import CumulusApiError

if TYPE_CHECKING:  # avoid runtime cycle
    from .client import GoogleDriveClient
    from .models import SyncEvent

logger = logging.getLogger(__name__)


class _WriteHelper:
    """Owner of all mutating Drive API calls.

    Constructed once by ``GoogleDriveClient.__init__`` with a back-reference
    to the client; reads the client's ``_get_service``, ``_retry_transient``,
    ``_count_request``, ``_ensure_root_id``, ``_path_cache``, and
    ``_poll_event`` through that reference.
    """

    def __init__(self, client: "GoogleDriveClient") -> None:
        self._client = client

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
        raise NotImplementedError  # task 3

    def make_dir_batch(
        self, remote_paths: list[str]
    ) -> "list[FolderMetadata | CumulusApiError]":
        raise NotImplementedError  # task 8

    def restore(self, remote_path: str, rev: str) -> FileMetadata:
        raise NotImplementedError  # task 10
