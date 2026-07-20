"""Google Drive change-feed processing and Drive-dict projection.

Two responsibilities:

* **Projection** — pure helpers that turn a Drive ``files`` resource
  dict into a :mod:`bora.core` dataclass. Shared by ``client.py``'s
  listing/metadata methods and by the change-feed code below.
* **Change feed** — :func:`iter_remote_changes` drains Drive's
  ``changes.list`` pages, classifies each entry against the 4-row
  disambiguation table (:func:`classify`), resolves file IDs to paths
  via the path cache, mutates the cache, and yields a single
  :class:`~bora.core.ListFolderResult` per poll cycle.

The module never imports ``client.py``: it receives the service factory
and the retry helper as parameters, keeping the dependency one-way
(``client.py`` → ``changes.py``).

See ``docs/superpowers/specs/2026-05-12-stage-3-read-client-design.md``
§6 for the design.
"""

from __future__ import annotations

import enum
import logging
import posixpath
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterator, cast

from googleapiclient.errors import HttpError

from .core import DeletedMetadata, FileMetadata, FolderMetadata, ListFolderResult
from .exceptions import CursorResetError
from .export import ExportOptions, project_leaf_name
from .native_docs import (
    GOOGLE_FOLDER_MIME_TYPE,
    GOOGLE_SHORTCUT_MIME_TYPE,
    export_target,
    is_exportable,
    is_google_native,
    is_shortcut,
    is_unsupported_native,
)
from .path_cache import SHARED_WITH_ME_ID
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

# The Drive ``files`` resource fields Bora needs for every read. Used
# verbatim by client.py's files.get / files.list calls and embedded into
# the change-feed field selector below.
FILE_FIELDS = (
    "id, name, parents, mimeType, modifiedTime, size, md5Checksum, version, "
    "shared, headRevisionId, trashed, lastModifyingUser(permissionId), "
    "sharedWithMeTime, ownedByMe, capabilities(canEdit), "
    "shortcutDetails(targetId, targetMimeType)"
    # NB: "sharedWithMe" is only a search-query term in Drive v3, not a File
    # resource field — requesting it in a fields projection is a 400
    # invalidParameter (found live, 2026-07-20). The presence of
    # sharedWithMeTime is the field-level signal for a direct share.
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


def to_file_metadata(
    file: dict[str, Any],
    path_display: str,
    export_opts: ExportOptions | None = None,
) -> FileMetadata:
    """Project a Drive ``files`` resource into a :class:`FileMetadata`.

    *path_display* is the encoded virtual path the caller has already
    resolved for this file.

    *export_opts*, when given and enabled, switches exportable native docs
    (Google Docs/Sheets/Slides) onto the export identity: ``rev`` becomes the
    Drive ``version`` field (they have no ``headRevisionId``), ``content_hash``
    is ``None`` (no server hash for these), and ``export_mime`` is set to the
    target export mime type. Ordinary files, and callers that omit
    *export_opts*, keep today's behavior."""
    opts = export_opts or ExportOptions.disabled()
    mime = file.get("mimeType")
    modified = parse_drive_time(file.get("modifiedTime"))
    last_user = file.get("lastModifyingUser") or {}

    export_mime: str | None = None
    rev: str
    content_hash: str | None
    if opts.enabled and is_exportable(mime):
        # Native docs have no headRevisionId; Drive's monotonic `version`
        # counter is the export identity's rev. Its presence on the live API
        # is unverified, so fail soft: a missing `version` falls back to the
        # (also change-sensitive) modifiedTime rather than raising KeyError
        # out of iter_remote_changes, which would wedge every poll cycle
        # (plan-authorized fallback).
        version = file.get("version")
        rev = str(version) if version is not None else file.get("modifiedTime", "")
        content_hash = None
        export_mime = export_target(cast(str, mime), opts.markdown)[0]
    else:
        rev = file.get("headRevisionId", "")
        content_hash = file.get("md5Checksum", "")

    return FileMetadata(
        name=file["name"],
        path_lower=fold_path(path_display),
        path_display=path_display,
        id=file["id"],
        modified=modified,
        rev=rev,
        size=int(file.get("size", 0) or 0),
        symlink_target=None,
        shared=bool(file.get("shared", False)),
        modified_by=last_user.get("permissionId"),
        is_downloadable=True,
        content_hash=content_hash,
        can_edit=bool(file.get("capabilities", {}).get("canEdit", True)),
        export_mime=export_mime,
    )


def to_folder_metadata(file: dict[str, Any], path_display: str) -> FolderMetadata:
    """Project a Drive folder resource into a :class:`FolderMetadata`."""
    return FolderMetadata(
        name=file["name"],
        path_lower=fold_path(path_display),
        path_display=path_display,
        id=file["id"],
        shared=bool(file.get("shared", False)),
        can_edit=bool(file.get("capabilities", {}).get("canEdit", True)),
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


def classify(change: dict[str, Any], path_cache: "PathIDCache") -> ChangeDecision:
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
        if file.get("sharedWithMeTime"):
            # New shared-with-me attach point: in scope under the virtual root.
            return ChangeDecision.APPLY
        if path_cache.id_to_path(file["id"]) is not None:
            return ChangeDecision.DELETE
        return ChangeDecision.OUT_OF_SCOPE
    return ChangeDecision.APPLY


def _emit_delete(
    change: dict[str, Any],
    path_cache: "PathIDCache",
    out_entries: list[Any],
    resolve_deleted_path: "Callable[[str], str | None] | None" = None,
) -> None:
    """Apply a hard-delete / trash / move-out: drop the file from the
    cache and append a :class:`DeletedMetadata`.

    The path is resolved from the cache. If the cache does not know the
    fileId and *resolve_deleted_path* is given, it is consulted as a
    fallback — this resolves a replayed delete after a crash mid-batch:
    the cache is mutated durably while the cursor advances only once the
    whole batch is applied, so on replay the id is already evicted from
    the cache, yet the engine index (keyed by remote_id) still holds the
    row. The fallback re-emits the deletion so the stranded local file +
    index row are removed (cluster-E finding 2). A delete for a fileId
    that neither the cache nor the resolver knows is a silent no-op
    (debug log only) — there is nothing in the local mirror to remove.
    """
    file_id = change.get("fileId")
    if file_id is None:
        # Malformed change with no id; nothing to resolve or remove.
        # (id_to_path would reject None — guard once up front.)
        logger.debug("Delete change without a fileId; nothing to remove")
        return
    path = path_cache.id_to_path(file_id)
    if path is not None:
        path_cache.remove(path)
    elif resolve_deleted_path is not None:
        # Cache miss: fall back to the engine index by remote_id. The id is
        # not in the cache, so there is nothing to remove there.
        path = resolve_deleted_path(file_id)
    if path is None:
        logger.debug("Delete for unknown fileId %s; nothing to remove", file_id)
        return
    out_entries.append(
        DeletedMetadata(
            name=path.rsplit("/", 1)[-1],
            path_lower=fold_path(path),
            path_display=path,
        )
    )


def _emit_apply(
    change: dict[str, Any],
    path_cache: "PathIDCache",
    out_entries: list[Any],
    *,
    warn_on_conflict: bool = True,
    export_opts: ExportOptions | None = None,
) -> bool:
    """Apply an in-scope create / content-update / move: write the path
    cache and append the projected metadata.

    Shortcuts are projected into symlink metadata via
    :func:`_emit_shortcut`. Unsupported native docs (Forms/Sites/Drawings)
    are always dropped (no cache write, no entry). Exportable native docs
    (Docs/Sheets/Slides) pass through with their leaf name projected
    (``Report`` -> ``Report.docx``) only when *export_opts* is enabled;
    otherwise they are dropped too, so export-disabled behavior is
    byte-identical to today. The caller invokes this only when
    :func:`classify` returned ``APPLY``, which guarantees at least one
    in-scope parent.

    Returns ``True`` when the change was consumed (metadata emitted, or
    intentionally dropped as a non-shortcut native doc) and ``False``
    when the path cache rejected the write, or a shortcut's parent /
    target does not resolve yet — a move-out delete later in the same
    batch may vacate the path, or a later change in the batch may
    resolve the target, so the caller can retry. Retrying callers pass
    ``warn_on_conflict=False`` so the duplicate-name warning fires only
    when the entry is finally dropped for good.
    """
    opts = export_opts or ExportOptions.disabled()
    file = change["file"]
    mime = file.get("mimeType")
    if is_shortcut(mime):
        return _emit_shortcut(
            change, path_cache, out_entries, warn_on_conflict=warn_on_conflict
        )
    # Forms/Sites/Drawings are always dropped; exportable Docs/Sheets/Slides
    # are dropped too unless export is enabled.
    if is_unsupported_native(mime):
        return True
    if is_google_native(mime) and not (opts.enabled and is_exportable(mime)):
        return True
    parents = file.get("parents") or []
    if len(parents) > 1 and warn_on_conflict:
        logger.warning(
            "File '%s' (id=%s) has multiple parents %s; only the first "
            "in-scope parent is used for its local path",
            file.get("name"),
            file.get("id"),
            parents,
        )
    parent_id = next(
        (p for p in parents if path_cache.id_to_path(p) is not None),
        None,
    )
    if parent_id is None and file.get("sharedWithMeTime"):
        # New shared-with-me attach point — mount under the virtual root.
        parent_id = SHARED_WITH_ME_ID
    # classify() returns APPLY only with at least one in-scope parent (or a
    # shared-with-me attach point), so the parent always resolves here.
    assert parent_id is not None
    parent_path = path_cache.id_to_path(parent_id)
    assert parent_path is not None
    new_path = compose_path(parent_path, project_leaf_name(file["name"], mime, opts))
    old_path = path_cache.id_to_path(file["id"])
    is_folder = mime == GOOGLE_FOLDER_MIME_TYPE
    if old_path is None:
        stored = path_cache.set(
            new_path, file["id"], parent_id, file["name"], is_folder
        )
    elif old_path != new_path:
        stored = path_cache.move(old_path, new_path, name=file["name"])
    else:
        stored = True  # pure content update; cache path already correct.
    if not stored:
        # The cache rejected the path (duplicate-name / case-fold conflict
        # with a different file id). Emitting metadata at this path would
        # cross-wire two files' content. Drop the entry; the path keeps
        # mapping to the first file (first-id-wins, matching
        # populate_from_listing).
        if warn_on_conflict:
            logger.warning(
                "Dropping change for %r (id=%s): path conflicts with an "
                "existing item. Rename one of the duplicates in the Google "
                "Drive web UI to sync both.",
                new_path,
                file["id"],
            )
        return False
    if is_folder:
        out_entries.append(to_folder_metadata(file, new_path))
    else:
        out_entries.append(to_file_metadata(file, new_path, export_opts=opts))
    return True


def _emit_shortcut(
    change: dict[str, Any],
    path_cache: "PathIDCache",
    out_entries: list[Any],
    *,
    warn_on_conflict: bool = True,
) -> bool:
    """Project a Drive shortcut into symlink FileMetadata.

    Resolves ``shortcutDetails.targetId`` to the target's cache path and
    emits a FileMetadata whose ``symlink_target`` is that path *relative to
    the symlink's own directory*. Returns False (defer) when the shortcut's
    own parent or its target does not resolve yet — the fixpoint retries.
    Resolves ONE hop only: a shortcut-to-shortcut target is refused.

    ``warn_on_conflict`` mirrors :func:`_emit_apply`'s parameter of the same
    name: the fixpoint's retrying calls pass ``False`` so a target that is
    merely not-yet-resolved stays silent, and only the final leftover pass
    (default ``True``) logs a per-item warning when the target never
    resolves and the shortcut is dropped for good.
    """
    file = change["file"]
    parents = file.get("parents") or []
    if len(parents) > 1 and warn_on_conflict:
        logger.warning(
            "File '%s' (id=%s) has multiple parents %s; only the first "
            "in-scope parent is used for its local path",
            file.get("name"),
            file.get("id"),
            parents,
        )
    parent_id = None
    parent_path = None
    for p in parents:
        candidate = path_cache.id_to_path(p)
        if candidate is not None:
            parent_id, parent_path = p, candidate
            break
    if parent_id is None or parent_path is None:
        return False  # parent not resolved yet; defer
    new_path = compose_path(parent_path, file["name"])

    details = file.get("shortcutDetails") or {}
    if details.get("targetMimeType") == GOOGLE_SHORTCUT_MIME_TYPE:
        logger.warning(
            "Shortcut %r points at another shortcut; not following the chain",
            file.get("name"),
        )
        return True

    target_id = details.get("targetId")
    target_path = path_cache.id_to_path(target_id) if target_id else None
    if target_path is None:
        # Target excluded / not yet resolved. Defer; the fixpoint or a later
        # batch may materialize it. If the target never resolves, the
        # caller's final leftover pass retries with warn_on_conflict=True
        # (the default) and we log the drop here — the fixpoint's own
        # retries pass warn_on_conflict=False so this stays silent while
        # a later change in the batch might still resolve the target.
        if warn_on_conflict:
            logger.warning(
                "Shortcut %r targets %s which is not synced; not creating " "a symlink",
                file.get("name"),
                target_id,
            )
        return False

    # Store the shortcut node itself in the cache so its own children (none,
    # for a shortcut) and future change-detection resolve; then emit metadata.
    old_path = path_cache.id_to_path(file["id"])
    if old_path is None:
        stored = path_cache.set(
            new_path, file["id"], parent_id, file["name"], is_folder=False
        )
    elif old_path != new_path:
        stored = path_cache.move(old_path, new_path)
    else:
        stored = True
    if not stored:
        return False

    rel_target = posixpath.relpath(target_path, posixpath.dirname(new_path))
    md = to_file_metadata(file, new_path)
    md.symlink_target = rel_target
    md.is_downloadable = False
    out_entries.append(md)
    return True


def iter_remote_changes(
    service_factory: "Callable[[], Any]",
    retry: "Callable[[Callable[[], Any]], Any]",
    path_cache: "PathIDCache",
    last_cursor: str,
    resolve_deleted_path: "Callable[[str], str | None] | None" = None,
    export_opts: ExportOptions | None = None,
) -> "Iterator[ListFolderResult]":
    """Drain Drive's ``changes.list`` pages and yield one
    :class:`~bora.core.ListFolderResult` per poll cycle.

    Each yielded entry has its path resolved via *path_cache*, and the
    cache is mutated as a side effect (``set`` / ``move`` / ``remove``).
    Unsupported native docs (Forms/Sites/Drawings) are always dropped;
    exportable Docs/Sheets/Slides pass through with a projected leaf name
    only when *export_opts* is enabled (else dropped too); shortcuts are
    projected into symlink metadata (see :func:`_emit_shortcut`). The
    cursor on the single yielded result is the ``newStartPageToken`` to
    resume polling from.

    ``changes.list`` does not guarantee parent-before-child ordering —
    a file created inside a brand-new folder can arrive before the
    folder's own change, and this can happen *across* page boundaries
    (a child on page N, its parent on page N+1). To handle this
    correctly all pages are drained to the final ``newStartPageToken``
    before classification begins. The fixpoint then runs over the whole
    batch so a parent resolved on any page unblocks its children.

    The cursor is only persisted after the entire batch is classified
    and applied, so a crash mid-batch re-fetches the batch on next
    startup. Re-applied *creates* and *moves* are idempotent (a create
    re-resolves to the same path; a move is recovered by remote_id via
    :meth:`SyncEngine._apply_remote_move`). Re-applied *deletes* would
    otherwise be lost: this function mutates ``path_cache`` durably as
    it classifies (a separate database from the engine's cursor), so on
    replay :func:`_emit_delete` finds the fileId already gone from the
    cache. *resolve_deleted_path* closes that crash window — the engine
    passes its index-by-remote_id lookup, so a replayed delete whose id
    was already evicted from the cache is re-resolved against the
    surviving index row and the ``DeletedMetadata`` is still emitted,
    removing the stranded local file plus its index row (cluster-E
    final-review finding 2). When the resolver is absent or the id is
    unknown to it as well, the delete remains a silent no-op.

    :param service_factory: zero-arg callable returning a Drive service.
    :param retry: the client's ``_retry_transient`` helper.
    :param path_cache: the path cache to resolve and mutate.
    :param last_cursor: the page token to start from.
    :param resolve_deleted_path: optional fileId -> cased-path fallback
        for replayed deletes whose id the cache has already evicted; the
        engine passes :meth:`SyncEngine._index_path_for_remote_id`.
    :param export_opts: export configuration; when enabled, exportable
        native docs pass through with their projected leaf name. Defaults
        to disabled (drop all native docs, as before).
    """
    # Phase 1: drain every page to the final newStartPageToken, buffering
    # raw changes. Parent-before-child is not guaranteed even ACROSS
    # pages, so classification must see the whole batch (review F6). A
    # poll cycle's batch is bounded by the changes since the last poll.
    cursor = last_cursor
    raw: list[dict[str, Any]] = []
    while True:
        try:

            def _list_changes_page(c: str = cursor) -> Any:
                return (
                    service_factory()
                    .changes()
                    .list(
                        pageToken=c,
                        fields=_CHANGE_FIELDS,
                        pageSize=_CHANGE_PAGE_SIZE,
                        includeRemoved=True,
                    )
                    .execute()
                )

            resp = retry(_list_changes_page)
        except HttpError as exc:
            if exc.resp.status == 400:
                raise CursorResetError(
                    "Sync cursor reset",
                    "The saved Google Drive change token is no longer "
                    "valid (Bora was offline too long). The index "
                    "will be rebuilt.",
                ) from exc
            raise
        raw.extend(resp.get("changes", []))
        next_token = resp.get("nextPageToken")
        cursor = next_token or resp.get("newStartPageToken") or ""
        if not next_token:
            break

    # Phase 2: terminal changes first (deletes/trash need no parent),
    # then the parent-dependent fixpoint over the WHOLE batch.
    out_entries: list[Any] = []
    pending: list[dict[str, Any]] = []
    for change in raw:
        file = change.get("file")
        if change.get("removed") or (file is not None and file.get("trashed")):
            _emit_delete(change, path_cache, out_entries, resolve_deleted_path)
        elif file is None:
            logger.debug("Skipping malformed change entry: %r", change)
        else:
            pending.append(change)

    # Outer loop: alternate the apply fixpoint and the move-out delete
    # pass until neither makes progress. Emitting a move-out delete can
    # vacate a path that a deferred (cache-rejected) apply needs — e.g.
    # file A moved out of scope and file B created at A's old path in
    # the same batch. Terminates because every iteration with
    # ``progress`` consumes at least one pending entry.
    while pending:
        progress = False

        # Fixpoint: apply every change whose parent now resolves, then
        # repeat — a folder applied this pass unblocks its children.
        # A cache-rejected apply is DEFERRED (kept pending, no warning):
        # a move-out delete below may free its path.
        while pending:
            deferred: list[dict[str, Any]] = []
            for change in pending:
                if classify(change, path_cache) is ChangeDecision.APPLY:
                    if _emit_apply(
                        change,
                        path_cache,
                        out_entries,
                        warn_on_conflict=False,
                        export_opts=export_opts,
                    ):
                        progress = True
                    else:
                        deferred.append(change)
                else:
                    deferred.append(change)
            if len(deferred) == len(pending):
                break  # nothing resolved this pass
            pending = deferred

        # Move-out pass: changes now classified DELETE are files the
        # cache knew that left the sync root — propagate the deletion.
        remaining: list[dict[str, Any]] = []
        for change in pending:
            if classify(change, path_cache) is ChangeDecision.DELETE:
                _emit_delete(change, path_cache, out_entries, resolve_deleted_path)
                progress = True
            else:
                remaining.append(change)
        pending = remaining

        if not progress:
            break

    # Leftovers after the loop stalls: never-in-scope creates (drop
    # silently) and applies whose path the cache rejected on every
    # attempt — genuinely fold-conflicted; drop them now, warning once.
    if pending:
        for change in pending:
            decision = classify(change, path_cache)
            if decision is ChangeDecision.DELETE:
                _emit_delete(change, path_cache, out_entries, resolve_deleted_path)
            elif decision is ChangeDecision.APPLY:
                # Deterministically rejected again (no cache mutation
                # since the stall); emits the Task-2 conflict warning.
                _emit_apply(change, path_cache, out_entries, export_opts=export_opts)
        logger.debug(
            "%d changes unresolved or out of scope after fixpoint",
            len(pending),
        )

    yield ListFolderResult(entries=out_entries, has_more=False, cursor=cursor)
