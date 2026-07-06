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

import hashlib
import logging
import os
import posixpath
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from .core import FileMetadata, FolderMetadata, WriteMode
from .errorhandling import convert_api_errors, extract_http_reason, http_error_to_bora
from .exceptions import (
    BadInputError,
    CancelledError,
    BoraApiError,
    DataChangedError,
    DataCorruptionError,
    FileConflictError,
    FolderConflictError,
    NotFoundError,
    UnsupportedFileError,
)
from .native_docs import GOOGLE_SHORTCUT_MIME_TYPE, is_google_native
from .path_codec import compose_path, decode_name, encode_name, fold_path

if TYPE_CHECKING:  # avoid runtime cycle
    from .client import GoogleDriveClient
    from .models import SyncEvent

logger = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_AUTORENAME_LIMIT = 999
_BATCH_LIMIT = 100
_SIMPLE_UPLOAD_THRESHOLD = 5 * 1024 * 1024  # 5 MB
_RESUMABLE_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB


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
                f"No item at {remote_path!r} is known to Bora.",
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
                f"The parent of {for_path!r} is not known to Bora.",
                remote_path=for_path,
            )
        return parent_id

    def _autorename_unique(
        self,
        parent_path: str,
        encoded_name: str,
        exclude_id: str | None = None,
    ) -> str:
        """Return an encoded path segment not already in path_cache under
        parent_path.

        Tries ``encoded_name``, ``encoded(name (1))``, ``encoded(name (2))``,
        … up to N=999. Probes are done in encoded namespace so collisions
        for names containing slashes or other special chars are detected
        correctly. Used by upload, move, and make_dir when autorename=True.

        :param exclude_id: File ID that does not count as a collision. Pass
            the ID of the item being moved so a case-only rename (the folded
            destination equals the folded source) is not falsely detected as
            a collision with the item itself.
        """
        cache = self._client._path_cache
        if cache.has_case_conflict(
            parent_path + "/" + encoded_name, exclude_id
        ) is None:
            return encoded_name
        decoded = decode_name(encoded_name)
        stem, ext = os.path.splitext(decoded)
        for n in range(1, _AUTORENAME_LIMIT + 1):
            candidate_decoded = f"{stem} ({n}){ext}"
            candidate_encoded = encode_name(candidate_decoded)
            if cache.has_case_conflict(
                parent_path + "/" + candidate_encoded, exclude_id
            ) is None:
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
        client = self._client
        client._ensure_root_id()

        parent_path, raw_name = self._split_parent_name(remote_path)

        cache = client._path_cache
        existing_id = cache.path_to_id(remote_path)

        # WriteMode resolution per spec §4.1.1.
        if write_mode is WriteMode.Add:
            if existing_id is not None and not autorename:
                raise FileConflictError(
                    "File already exists",
                    f"An item already exists at {remote_path!r}.",
                    remote_path=remote_path,
                    local_path=local_path,
                )
            if existing_id is not None and autorename:
                raw_name = self._autorename_unique(parent_path, raw_name)
                remote_path = parent_path + "/" + raw_name
            mode = "create"
            target_id: str | None = None
        elif write_mode is WriteMode.Update:
            if existing_id is None:
                raise NotFoundError(
                    "Not found",
                    f"No item at {remote_path!r} to update.",
                    remote_path=remote_path,
                )
            mode = "update"
            target_id = existing_id
        elif write_mode is WriteMode.Overwrite:
            if existing_id is None:
                mode = "create"
                target_id = None
            else:
                mode = "update"
                target_id = existing_id
        else:
            raise BadInputError(
                "Invalid WriteMode",
                f"Unknown write_mode {write_mode!r}.",
                remote_path=remote_path,
                local_path=local_path,
            )

        drive_name = self._decode_segment(raw_name)

        # update_rev precondition (Update mode and Overwrite-when-existing).
        if mode == "update" and update_rev is not None:
            with convert_api_errors(remote_path=remote_path, local_path=local_path):
                meta = client._retry_transient(
                    lambda: client._get_service()
                    .files()
                    .get(fileId=target_id, fields="headRevisionId")
                    .execute()
                )
            client._count_request()
            if meta.get("headRevisionId") != update_rev:
                raise DataChangedError(
                    "Item changed on Google Drive",
                    f"{remote_path!r} changed on Drive since Bora last saw it.",
                    remote_path=remote_path,
                    local_path=local_path,
                )

        # Resolve parent for create.
        if mode == "create":
            parent_id = self._resolve_parent_id(
                parent_path, for_path=remote_path
            )
        else:
            parent_id = None  # update has no parent change in upload

        # These steps read the local file (MD5, mtime, size). Wrap them in
        # convert_api_errors so a local OS error — e.g. a permission-denied
        # file — surfaces as a Bora SyncError (InsufficientPermissionsError)
        # that the per-item handler records, rather than escaping as a raw
        # OSError to the sync-thread error handler.
        with convert_api_errors(remote_path=remote_path, local_path=local_path):
            # Compute local MD5 while reading.
            local_md5 = self._md5_file(local_path)

            # Drive wants modifiedTime as RFC-3339 UTC; canonicalise to the
            # "...Z" form so a round-trip of our own write reports the same
            # server time and does not spawn a change-feed echo with a
            # surprising mtime.
            modified_rfc3339 = (
                datetime.fromtimestamp(os.path.getmtime(local_path), tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

            size = os.path.getsize(local_path)
            media = MediaFileUpload(
                local_path,
                resumable=(size > _SIMPLE_UPLOAD_THRESHOLD),
                chunksize=_RESUMABLE_CHUNK_SIZE,
            )

        fields = ("id,name,mimeType,md5Checksum,modifiedTime,size,"
                  "headRevisionId")

        if size > _SIMPLE_UPLOAD_THRESHOLD:
            # Resumable path (large files).
            resp = self._upload_resumable(
                mode=mode, target_id=target_id, parent_id=parent_id,
                drive_name=drive_name, media=media, fields=fields,
                remote_path=remote_path, local_path=local_path,
                modified_rfc3339=modified_rfc3339,
                sync_event=sync_event,
            )
        else:
            # Simple path.
            if mode == "create":
                # Reserve a pre-generated ID so a retried create (after a
                # "committed but response lost" connection error) fails with
                # 409 instead of inserting a same-name duplicate (review F7).
                new_file_id = client.reserve_file_id()
                body = {
                    "id": new_file_id,
                    "name": drive_name,
                    "parents": [parent_id],
                    "modifiedTime": modified_rfc3339,
                }
                request = client._get_service().files().create(
                    body=body, media_body=media, fields=fields,
                )
                try:
                    with convert_api_errors(
                        remote_path=remote_path, local_path=local_path
                    ):
                        resp = client._retry_transient(request.execute)
                except BoraApiError as exc:
                    # convert_api_errors has already converted the HttpError (409
                    # has no dedicated bora class), so inspect __cause__ rather
                    # than catching HttpError directly.
                    if (
                        isinstance(exc.__cause__, HttpError)
                        and exc.__cause__.resp.status == 409
                    ):
                        # A previous create attempt committed but its response
                        # was lost. Fetch the record by the known id.
                        with convert_api_errors(
                            remote_path=remote_path, local_path=local_path
                        ):
                            resp = client._retry_transient(
                                lambda: client._get_service()
                                .files()
                                .get(fileId=new_file_id, fields=fields)
                                .execute()
                            )
                    else:
                        raise
            else:
                with convert_api_errors(
                    remote_path=remote_path, local_path=local_path
                ):
                    request = client._get_service().files().update(
                        fileId=target_id,
                        body={"modifiedTime": modified_rfc3339},
                        media_body=media, fields=fields,
                    )
                    resp = client._retry_transient(request.execute)
            client._count_request()

        # MD5 verify.
        drive_md5 = resp.get("md5Checksum")
        if drive_md5 is None:
            logger.warning(
                "Drive omitted md5Checksum for %s; skipping verify.",
                remote_path,
            )
        elif drive_md5 != local_md5:
            raise DataCorruptionError(
                "Upload checksum mismatch",
                f"Drive returned md5={drive_md5!r} but local md5={local_md5!r} "
                f"for {remote_path!r}.",
                remote_path=remote_path,
                local_path=local_path,
            )

        # Post-upload TOCTOU audit (review F4). Drive v3 has no atomic write
        # precondition, so the headRevisionId check above is check-then-write
        # with a race window spanning the whole upload. Detect an interleaved
        # remote revision from revision history and rescue it. Best-effort:
        # our own write already committed, so a failing audit/rescue must not
        # fail the upload — the buried revision stays recoverable from the
        # file's revision history on Drive.
        if mode == "update" and update_rev is not None:
            try:
                self._audit_update_rev(
                    resp["id"], update_rev, resp.get("headRevisionId"),
                    remote_path=remote_path, local_path=local_path,
                )
            except (BoraApiError, OSError) as exc:
                logger.warning(
                    "Post-upload revision audit for %r failed (%s); a remote "
                    "revision committed during our upload may not have been "
                    "rescued as a conflicted copy. It remains recoverable "
                    "from the file's revision history on Drive.",
                    remote_path, exc,
                )

        if mode == "create":
            cache.set(
                remote_path, resp["id"],
                parent_id=parent_id, name=drive_name,
                is_folder=False,
            )

        return self._project_file_metadata(resp, remote_path)

    @staticmethod
    def _md5_file(local_path: str) -> str:
        h = hashlib.md5(usedforsecurity=False)
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _project_file_metadata(
        self, resp: dict[str, Any], remote_path: str
    ) -> FileMetadata:
        from .changes import parse_drive_time
        return FileMetadata(
            name=resp.get("name", ""),
            path_lower=fold_path(remote_path),
            path_display=remote_path,
            id=resp["id"],
            modified=parse_drive_time(resp.get("modifiedTime")),
            rev=resp.get("headRevisionId", ""),
            size=int(resp.get("size", 0) or 0),
            symlink_target=None,
            shared=False,
            modified_by=None,
            is_downloadable=True,
            content_hash=resp.get("md5Checksum", ""),
        )

    def _upload_resumable(
        self, *, mode: str, target_id: str | None, parent_id: str | None,
        drive_name: str, media: Any, fields: str,
        remote_path: str, local_path: str,
        modified_rfc3339: str,
        sync_event: "SyncEvent | None",
    ) -> dict[str, Any]:
        client = self._client

        if mode == "create":
            # Reserve a pre-generated ID for idempotency (review F7): if the
            # session commits but the response is lost, a retry's 409 resolves
            # to the already-committed record instead of duplicating.
            new_file_id = client.reserve_file_id()
            body = {
                "id": new_file_id,
                "name": drive_name,
                "parents": [parent_id],
                "modifiedTime": modified_rfc3339,
            }
            request = client._get_service().files().create(
                body=body, media_body=media, fields=fields,
            )
        else:
            new_file_id = None
            request = client._get_service().files().update(
                fileId=target_id,
                body={"modifiedTime": modified_rfc3339},
                media_body=media, fields=fields,
            )

        response: dict[str, Any] | None = None
        try:
            with convert_api_errors(remote_path=remote_path, local_path=local_path):
                while response is None:
                    if client._poll_event.is_set():
                        raise CancelledError(
                            "Upload cancelled",
                            f"Upload of {local_path!r} was interrupted by shutdown.",
                        )
                    status, response = client._retry_transient(request.next_chunk)
                    if sync_event is not None and status is not None:
                        sync_event.completed = status.resumable_progress
        except BoraApiError as exc:
            # convert_api_errors has already converted the HttpError (409
            # has no dedicated bora class), so inspect __cause__ rather
            # than catching HttpError directly.
            if (
                new_file_id is not None
                and isinstance(exc.__cause__, HttpError)
                and exc.__cause__.resp.status == 409
            ):
                # Previous resumable session committed but response was lost.
                with convert_api_errors(remote_path=remote_path, local_path=local_path):
                    response = client._retry_transient(
                        lambda: client._get_service()
                        .files()
                        .get(fileId=new_file_id, fields=fields)
                        .execute()
                    )
            else:
                raise

        client._count_request()
        return response

    def _audit_update_rev(
        self,
        file_id: str,
        expected_prev: str,
        new_head: str | None,
        *,
        remote_path: str,
        local_path: str,
    ) -> None:
        """Post-upload TOCTOU audit (review F4).

        Drive v3 offers no atomic write precondition (no If-Match), so a
        remote revision can land between our ``headRevisionId`` check and
        the ``files.update`` — the window spans the whole resumable upload.
        Detect that from revision history: if the revision immediately
        preceding our new head is not the rev we checked against, a foreign
        revision was committed during our upload and buried. Rescue it as a
        remote conflicted copy (Drive retains non-head revisions of binary
        files, so its content is still downloadable).

        Costs exactly one ``revisions.list`` per rev-checked update in the
        no-race case (more only for >1000 revisions). If several foreign
        revisions interleaved, only the newest (the one directly under our
        head) is rescued; older ones remain in revision history.

        Native Google docs cannot be downloaded via ``revisions.get_media``,
        but they never enter the path cache / sync index (the change feed
        drops them), so the rev-checked update path cannot target them; if
        one is ever hit regardless, the rescue fails and is swallowed by the
        caller's best-effort guard.
        """
        client = self._client

        # Fix 3: hoist the None check above the pagination loop to avoid
        # wasted RPCs when the upload response omitted headRevisionId.
        # Fix 1: if the head id is unchanged, Drive coalesced our burst update
        # into the same revision — no foreign interleave is possible, so
        # return immediately. A foreign commit always produces a new revision
        # id, so this guard is unconditionally sound.
        if new_head is None or new_head == expected_prev:
            return

        ids: list[str] = []
        page_token: str | None = None
        while True:

            def _fetch_page(token: str | None = page_token) -> dict[str, Any]:
                return (
                    client._get_service()
                    .revisions()
                    .list(
                        fileId=file_id,
                        fields="nextPageToken,revisions(id)",
                        pageSize=1000,
                        pageToken=token,
                    )
                    .execute()
                )

            with convert_api_errors(
                remote_path=remote_path, local_path=local_path
            ):
                resp = client._retry_transient(_fetch_page)
            client._count_request()
            ids.extend(r["id"] for r in resp.get("revisions", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        if new_head not in ids:
            return  # history truncated/purged; nothing to audit against
        head_idx = ids.index(new_head)  # revisions are oldest-first
        if head_idx == 0:
            return
        prev = ids[head_idx - 1]
        if prev == expected_prev:
            return
        logger.warning(
            "Remote revision %s of %r was committed during our upload and "
            "overwritten; rescuing it as a conflicted copy.",
            prev, remote_path,
        )
        self._rescue_revision(file_id, prev, remote_path)

    def _rescue_revision(
        self, file_id: str, revision_id: str, remote_path: str
    ) -> None:
        """Download *revision_id* and re-upload it as a same-folder
        ``name (conflicted copy <date>).ext`` sibling. The change feed then
        delivers the sibling to every device, converging both edits.

        Cannot recurse: the rescue upload uses ``WriteMode.Add`` without
        ``update_rev``, so it never re-enters the audit.
        """
        tmp_path = self._download_revision_to_tempfile(file_id, revision_id)
        try:
            # Build the cc_path canonically: decode the encoded leaf, split
            # stem/ext on the human-readable name, re-encode via compose_path
            # (mirrors _autorename_unique's decode → suffix → re-encode idiom).
            parent_path, raw_leaf = self._split_parent_name(remote_path)
            stem, ext = os.path.splitext(decode_name(raw_leaf))
            date = datetime.now().strftime("%Y-%m-%d")
            cc_path = compose_path(parent_path or "/", f"{stem} (conflicted copy {date}){ext}")
            self.upload(
                tmp_path, cc_path,
                write_mode=WriteMode.Add, autorename=True,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

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
                    f"{remote_path!r} changed on Drive since Bora last "
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
            modified=modified,
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
    ) -> list["FileMetadata | FolderMetadata | BoraApiError"]:
        client = self._client
        client._ensure_root_id()
        cache = client._path_cache
        results: list[FileMetadata | FolderMetadata | BoraApiError | None] = [
            None
        ] * len(entries)

        # Resolve file IDs + precondition checks before chunking the batch.
        # Failed prechecks land in the result slot and skip the batch.
        resolved: list[tuple[int, str, str] | None] = [None] * len(entries)
        for idx, (remote_path, parent_rev) in enumerate(entries):
            file_id = cache.path_to_id(remote_path)
            if file_id is None:
                results[idx] = NotFoundError(
                    "Not found",
                    f"No item at {remote_path!r} is known to Bora.",
                    remote_path=remote_path,
                )
                continue
            if parent_rev is not None:
                with convert_api_errors(remote_path=remote_path):
                    meta = client._retry_transient(
                        lambda fid=file_id: client._get_service()
                        .files()
                        .get(fileId=fid, fields="headRevisionId,mimeType")
                        .execute()
                    )
                client._count_request()
                if meta.get("mimeType") == _FOLDER_MIME:
                    results[idx] = BadInputError(
                        "Invalid operation",
                        "parent_rev cannot be specified for a folder.",
                        remote_path=remote_path,
                    )
                    continue
                if meta.get("headRevisionId") != parent_rev:
                    results[idx] = DataChangedError(
                        "Item changed on Google Drive",
                        f"{remote_path!r} changed on Drive since Bora last saw it.",
                        remote_path=remote_path,
                    )
                    continue
            resolved[idx] = (idx, remote_path, file_id)

        live = [r for r in resolved if r is not None]
        for chunk_start in range(0, len(live), batch_size):
            chunk = live[chunk_start:chunk_start + batch_size]
            batch = client._get_service().new_batch_http_request()
            for idx, remote_path, file_id in chunk:
                request = (
                    client._get_service().files().update(
                        fileId=file_id,
                        body={"trashed": True},
                        fields="id,name,mimeType,trashed",
                    )
                )
                batch.add(
                    request,
                    callback=self._make_remove_batch_callback(
                        results, idx, remote_path,
                    ),
                    request_id=str(idx),
                )
            with convert_api_errors():
                client._retry_transient(batch.execute)
            client._count_request(len(chunk))

        return [r if r is not None else BoraApiError(
            "batch slot empty", "")
                for r in results]

    def _make_remove_batch_callback(
        self, results: list, idx: int, remote_path: str,
    ):
        """Build a per-sub-request callback for remove_batch."""
        client = self._client

        def cb(request_id, response, exception):
            if exception is not None:
                results[idx] = self._batch_exception_to_bora(
                    exception, remote_path
                )
                return
            client._path_cache.remove(remote_path)
            results[idx] = self._wrap_metadata(response, remote_path)
        return cb

    def move(
        self, remote_path: str, new_path: str, *, autorename: bool = False
    ) -> "FileMetadata | FolderMetadata":
        client = self._client
        client._ensure_root_id()
        cache = client._path_cache

        file_id = self._resolve_file_id(remote_path)

        old_parent_path, _ = self._split_parent_name(remote_path)
        new_parent_path, raw_new_name = self._split_parent_name(new_path)

        if autorename:
            # Exclude the item being moved itself from the collision probe:
            # for a case-only rename the folded destination equals the folded
            # source, which is not a collision (cluster-B bug B1).
            raw_new_name = self._autorename_unique(
                new_parent_path, raw_new_name, exclude_id=file_id
            )
            new_path = new_parent_path + "/" + raw_new_name

        new_name = self._decode_segment(raw_new_name)

        old_parent_id = self._resolve_parent_id(
            old_parent_path, for_path=remote_path
        )
        new_parent_id = self._resolve_parent_id(
            new_parent_path, for_path=new_path
        )

        update_kwargs: dict[str, Any] = {
            "fileId": file_id,
            "body": {"name": new_name},
            "fields": "id,name,mimeType,modifiedTime,size,headRevisionId,md5Checksum",
        }
        if old_parent_id != new_parent_id:
            update_kwargs["addParents"] = new_parent_id
            update_kwargs["removeParents"] = old_parent_id

        with convert_api_errors(remote_path=remote_path):
            resp = client._retry_transient(
                lambda: client._get_service()
                .files()
                .update(**update_kwargs)
                .execute()
            )
        client._count_request()
        if not cache.move(remote_path, new_path):
            logger.warning(
                "path_cache.move(%r -> %r) rejected by case-conflict; "
                "cache and Drive may be out of sync until the change feed "
                "corrects this.",
                remote_path, new_path,
            )
        return self._wrap_metadata(resp, new_path)

    @staticmethod
    def _escape_drive_name(name: str) -> str:
        """Escape a Drive name for use in a ``files.list`` query string.

        Drive query strings use single-quoted values; backslash and single-quote
        must be escaped as ``\\`` and ``\\'`` respectively.
        """
        return name.replace("\\", "\\\\").replace("'", "\\'")

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

        # Conflict probe (review F11): before creating, check whether a
        # same-name folder already exists under the same parent on Drive.
        # This can happen when another device created the folder and the local
        # change feed hasn't delivered the event yet (stale cache).
        # Skipped when autorename=True because that path WANTS a unique name.
        if not autorename:
            escaped = self._escape_drive_name(drive_name)
            with convert_api_errors(remote_path=remote_path):
                probe = client._retry_transient(
                    lambda: client._get_service()
                    .files()
                    .list(
                        q=(
                            f"name = '{escaped}' and '{parent_id}' in parents "
                            f"and mimeType = '{_FOLDER_MIME}' and trashed = false"
                        ),
                        fields="files(id, name)",
                        pageSize=1,
                    )
                    .execute()
                )
            client._count_request()
            hits = probe.get("files", [])
            if hits:
                # Adopt the existing folder into the cache so follow-up
                # operations (get_metadata in the engine's merge branch)
                # resolve it.
                adopted = client._path_cache.set(
                    remote_path,
                    hits[0]["id"],
                    parent_id=parent_id,
                    name=hits[0]["name"],
                    is_folder=True,
                )
                if not adopted:
                    # A stale entry (a different id) occupies the folded path —
                    # e.g. another device deleted and recreated the folder while
                    # our change feed lagged.  The probe result is fresher;
                    # evict the stale entry and re-adopt.
                    logger.debug(
                        "Evicting stale cache entry for %r; probe found "
                        "fresher id %r.",
                        remote_path,
                        hits[0]["id"],
                    )
                    client._path_cache.remove(remote_path)
                    client._path_cache.set(
                        remote_path,
                        hits[0]["id"],
                        parent_id=parent_id,
                        name=hits[0]["name"],
                        is_folder=True,
                    )
                raise FolderConflictError(
                    "Folder already exists",
                    f"A folder with this name already exists at {remote_path!r} "
                    "on Google Drive; the existing folder can be merged instead "
                    "of creating a duplicate.",
                    remote_path=remote_path,
                )

        new_folder_id = client.reserve_file_id()
        body = {
            "id": new_folder_id,
            "name": drive_name,
            "mimeType": _FOLDER_MIME,
            "parents": [parent_id],
        }
        try:
            with convert_api_errors(remote_path=remote_path):
                resp = client._retry_transient(
                    lambda: client._get_service()
                    .files()
                    .create(body=body, fields="id,name,mimeType")
                    .execute()
                )
        except BoraApiError as exc:
            # convert_api_errors has already converted the HttpError (409
            # has no dedicated bora class), so inspect __cause__ rather
            # than catching HttpError directly.
            if (
                isinstance(exc.__cause__, HttpError)
                and exc.__cause__.resp.status == 409
            ):
                # Previous create committed; fetch the existing folder.
                with convert_api_errors(remote_path=remote_path):
                    resp = client._retry_transient(
                        lambda: client._get_service()
                        .files()
                        .get(fileId=new_folder_id, fields="id,name,mimeType")
                        .execute()
                    )
            else:
                raise
        client._count_request()
        if not client._path_cache.set(
            remote_path,
            resp["id"],
            parent_id=parent_id,
            name=drive_name,
            is_folder=True,
        ):
            logger.warning(
                "path_cache.set(%r, %r) rejected by case-conflict; "
                "cache and Drive may be out of sync until the change feed "
                "corrects this.",
                remote_path, resp["id"],
            )
        return FolderMetadata(
            name=resp["name"],
            path_lower=fold_path(remote_path),
            path_display=remote_path,
            id=resp["id"],
            shared=False,
        )

    def create_shortcut(
        self,
        remote_path: str,
        target_remote_path: str,
        *,
        autorename: bool = False,
    ) -> FileMetadata:
        """Create a Drive shortcut at *remote_path* pointing at the item
        currently synced at *target_remote_path*."""
        client = self._client
        client._ensure_root_id()
        parent_path, raw_name = self._split_parent_name(remote_path)
        if autorename:
            raw_name = self._autorename_unique(parent_path, raw_name)
            remote_path = parent_path + "/" + raw_name
        drive_name = self._decode_segment(raw_name)
        parent_id = self._resolve_parent_id(parent_path, for_path=remote_path)
        target_id = self._resolve_file_id(target_remote_path)  # NotFoundError if absent

        new_id = client.reserve_file_id()
        body = {
            "id": new_id,
            "name": drive_name,
            "mimeType": GOOGLE_SHORTCUT_MIME_TYPE,
            "parents": [parent_id],
            "shortcutDetails": {"targetId": target_id},
        }
        with convert_api_errors(remote_path=remote_path):
            resp = client._retry_transient(
                lambda: client._get_service()
                .files()
                .create(body=body, fields="id,name,mimeType,shortcutDetails")
                .execute()
            )
        client._count_request()
        client._path_cache.set(
            remote_path, resp["id"], parent_id=parent_id,
            name=drive_name, is_folder=False,
        )
        # Relative-to-own-dir target, matching the download-side convention
        # (see changes.py:_emit_shortcut).
        rel_target = posixpath.relpath(
            target_remote_path, posixpath.dirname(remote_path)
        )
        return FileMetadata(
            name=resp["name"], path_lower=fold_path(remote_path),
            path_display=remote_path, id=resp["id"],
            modified=datetime.now(timezone.utc), rev="", size=0,
            symlink_target=rel_target, shared=False, modified_by=None,
            is_downloadable=False, content_hash="", can_edit=True,
        )

    def retarget_shortcut(
        self, remote_path: str, target_remote_path: str
    ) -> FileMetadata:
        """Point an existing Drive shortcut at a new target."""
        client = self._client
        client._ensure_root_id()
        file_id = self._resolve_file_id(remote_path)
        target_id = self._resolve_file_id(target_remote_path)
        with convert_api_errors(remote_path=remote_path):
            resp = client._retry_transient(
                lambda: client._get_service()
                .files()
                .update(
                    fileId=file_id,
                    body={"shortcutDetails": {"targetId": target_id}},
                    fields="id,name,mimeType,shortcutDetails",
                )
                .execute()
            )
        client._count_request()
        rel_target = posixpath.relpath(
            target_remote_path, posixpath.dirname(remote_path)
        )
        return FileMetadata(
            name=resp["name"], path_lower=fold_path(remote_path),
            path_display=remote_path, id=resp["id"],
            modified=datetime.now(timezone.utc), rev="", size=0,
            symlink_target=rel_target, shared=False, modified_by=None,
            is_downloadable=False, content_hash="", can_edit=True,
        )

    def make_dir_batch(
        self, remote_paths: list[str]
    ) -> "list[FolderMetadata | BoraApiError]":
        client = self._client
        client._ensure_root_id()
        results: list[FolderMetadata | BoraApiError | None] = [None] * len(
            remote_paths
        )

        for chunk_start in range(0, len(remote_paths), _BATCH_LIMIT):
            chunk = list(enumerate(
                remote_paths[chunk_start:chunk_start + _BATCH_LIMIT],
                start=chunk_start,
            ))
            batch = client._get_service().new_batch_http_request()
            scheduled_count = 0
            for idx, remote_path in chunk:
                try:
                    parent_path, raw_name = self._split_parent_name(
                        remote_path
                    )
                    drive_name = self._decode_segment(raw_name)
                    parent_id = self._resolve_parent_id(
                        parent_path, for_path=remote_path
                    )
                except BoraApiError as e:
                    results[idx] = e
                    continue
                new_folder_id = client.reserve_file_id()
                body = {
                    "id": new_folder_id,
                    "name": drive_name,
                    "mimeType": _FOLDER_MIME,
                    "parents": [parent_id],
                }
                request = (
                    client._get_service().files().create(
                        body=body, fields="id,name,mimeType",
                    )
                )
                batch.add(
                    request,
                    callback=self._make_dir_batch_callback(
                        results, idx, remote_path, drive_name, parent_id,
                        new_folder_id,
                    ),
                    request_id=str(idx),
                )
                scheduled_count += 1

            if scheduled_count > 0:
                with convert_api_errors():
                    client._retry_transient(batch.execute)
                client._count_request(scheduled_count)

        # Backfill any None slots (shouldn't happen, but defensive)
        return [r if r is not None else BoraApiError(
            "batch slot empty", "")
                for r in results]

    def _make_dir_batch_callback(
        self, results: list, idx: int, remote_path: str,
        drive_name: str, parent_id: str, new_folder_id: str,
    ):
        """Build a per-sub-request callback for make_dir_batch."""
        client = self._client

        def cb(request_id, response, exception):
            if exception is not None:
                if (
                    isinstance(exception, HttpError)
                    and exception.resp.status == 409
                ):
                    # Previous create committed; fetch the existing folder by
                    # the pre-generated id.
                    try:
                        with convert_api_errors(remote_path=remote_path):
                            response = client._retry_transient(
                                lambda: client._get_service()
                                .files()
                                .get(fileId=new_folder_id, fields="id,name,mimeType")
                                .execute()
                            )
                    except BoraApiError as fetch_exc:
                        results[idx] = fetch_exc
                        return
                else:
                    results[idx] = self._batch_exception_to_bora(
                        exception, remote_path
                    )
                    return
            client._path_cache.set(
                remote_path, response["id"],
                parent_id=parent_id,
                name=drive_name,
                is_folder=True,
            )
            results[idx] = FolderMetadata(
                name=response.get("name", ""),
                path_lower=fold_path(remote_path),
                path_display=remote_path,
                id=response["id"],
                shared=False,
            )
        return cb

    @staticmethod
    def _batch_exception_to_bora(
        exception: Exception, remote_path: str
    ) -> BoraApiError:
        if isinstance(exception, HttpError):
            return http_error_to_bora(
                exception.resp.status,
                extract_http_reason(exception),
                exception,
                remote_path=remote_path,
            )
        return BoraApiError(
            "Batch sub-request failed",
            str(exception),
            remote_path=remote_path,
        )

    def restore(self, remote_path: str, rev: str) -> FileMetadata:
        client = self._client
        client._ensure_root_id()
        file_id = self._resolve_file_id(remote_path)

        with convert_api_errors(remote_path=remote_path):
            meta = client._retry_transient(
                lambda: client._get_service()
                .files()
                .get(fileId=file_id, fields="mimeType")
                .execute()
            )
        client._count_request()
        if is_google_native(meta.get("mimeType", "")):
            raise UnsupportedFileError(
                "Cannot restore native Google Doc",
                "restore() is not supported for Google Docs / Sheets / "
                "Slides — their revisions cannot be round-tripped as bytes.",
                remote_path=remote_path,
            )

        tmp_path = self._download_revision_to_tempfile(file_id, rev)
        try:
            return self.upload(
                tmp_path, remote_path, write_mode=WriteMode.Update,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _download_revision_to_tempfile(self, file_id: str, rev: str) -> str:
        import tempfile as _t
        client = self._client
        fd, path = _t.mkstemp(prefix="bora-restore-")
        os.close(fd)
        # Callers only clean up the path we *return*; if the download raises
        # before we return, the mkstemp'd file would be orphaned (7c-8), so
        # clean it up here on any failure (including cancellation).
        try:
            with convert_api_errors():
                request = client._get_service().revisions().get_media(
                    fileId=file_id, revisionId=rev,
                )
                with open(path, "wb") as fh:
                    downloader = MediaIoBaseDownload(
                        fh, request, chunksize=_RESUMABLE_CHUNK_SIZE,
                    )
                    done = False
                    while not done:
                        if client._poll_event.is_set():
                            raise CancelledError(
                                "Restore cancelled",
                                f"Restore of {file_id!r}@{rev!r} interrupted "
                                "by shutdown.",
                            )
                        _status, done = client._retry_transient(
                            downloader.next_chunk
                        )
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        client._count_request()
        return path
