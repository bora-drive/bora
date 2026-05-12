# Stage 3 — Read client (download + change feed)

**Status:** Design (awaiting review)
**Date:** 2026-05-12
**Stage of:** `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` §5.3
**Inputs:** the staged-port plan, `docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md` Q3/Q4/Q5/Q6, the post-Stage-2 state of `src/cumulus/client.py` / `path_cache.py` / `native_docs.py` / `errorhandling.py`, REVIEW2 entries A2/A3/A4 and C6, and the Stage 2 spec/plan for the API surface they leave open.

---

## 1. Purpose

Stand up the read half of the Google Drive client: metadata lookup, download,
folder listing, and the change feed. After this stage, the daemon can
populate the path cache from a fresh Drive, observe and classify every
mutation that happens on Drive, and download non-native files. The stage
also lands the read-side error-mapping table (`HttpError` → Cumulus
exceptions) and the transient-error retry helper that every later stage
will reuse.

Stage 3 does not implement any mutating Drive API call (upload, move,
delete, mkdir, batch). Those are Stage 4. Stage 3 also does not adapt
the existing `sync.py` to consume the new shapes; that is Stage 5.

## 2. Scope

### 2.1 In scope

- **Phase 5c — metadata ops.** `get_metadata(remote_path)`,
  `list_revisions(remote_path, limit)`. `restore(remote_path, rev)` stays
  a stub raising `NotImplementedError` until Stage 4 lands `upload`.
- **Phase 5d — download.** `download(remote_path, local_path,
  sync_event=None)` via `MediaIoBaseDownload` chunked (8 MB chunks),
  progress hook into `sync_event`, bandwidth-throttle respect,
  `md5Checksum` verification on completion.
- **Phase 5g — change detection.** `get_latest_cursor`, `list_folder`,
  `list_folder_iterator`, `wait_for_remote_changes` (interruptible
  poll), `list_remote_changes_iterator`.
- **Path-cache write-through in the change feed.** The change-feed
  iterator mutates `path_cache` via `set` / `move` / `remove` as it
  processes each entry. The sync engine receives `FileMetadata` /
  `FolderMetadata` / `DeletedMetadata` with paths already resolved.
- **A2 — native-docs filter extension.** `native_docs.is_google_native`
  switches from an enumerated frozenset to a wildcard predicate
  (`mime_type.startswith("application/vnd.google-apps.")` excluding the
  folder MIME). Shortcuts are filtered out at the same boundary;
  `shortcutDetails.targetId` is not followed.
- **A3 — change-feed sync-root scoping.** Each change is in-scope iff
  at least one of `file.parents` resolves to a non-`None` cache entry.
  Out-of-scope changes are dropped without cache mutation.
- **A4 — `removed` disambiguation.** The 4-row table from
  the spike report (§Q6) implemented in `changes.classify()`, plus an
  explicit split of the "no parent in cache" row into "move-out"
  (fileId in cache) vs. "never in scope" (fileId not in cache).
- **C6 — Computers / Backup folders.** Items unreachable from
  `root_id` (Computers, Backup, shared-with-me) are dropped by
  `populate_from_listing`'s existing BFS. A one-shot INFO log after
  bootstrap reports the skipped-item count; per-item detail at DEBUG.
- **Read-side error mapping.** `convert_api_errors` gains an
  `HttpError` branch with the per-status/per-reason table in §6.1.
- **Retry helper.** `_retry_transient(call)` in `client.py` wraps each
  `.execute()` site: exponential backoff (1s→32s, max 5 attempts,
  jitter, honors `Retry-After`). Lives on the client so its
  `time.sleep()` can use the same `_poll_event` as
  `wait_for_remote_changes`.
- **`root_id` resolution.** `_ensure_root_id` does
  `files.get(fileId='root', fields='id')` once per daemon lifetime,
  caches on `PathIDCache.root_id`. No config/DB persistence yet.
- **Test rebuilds.** `tests/offline/test_client.py` and
  `tests/offline/test_errorhandling.py` are rebuilt from scratch (both
  are pre-existing collection errors from leftover Dropbox imports).
  New: `tests/offline/test_changes.py`, `tests/offline/test_native_docs.py`.

### 2.2 Out of scope

- **Any mutating Drive API call.** `upload`, `make_dir`,
  `make_dir_batch`, `remove`, `remove_batch`, `move`, `autorename`,
  sharing. Stage 4.
- **`restore()`.** Stub only; cross-stage dependency on Stage 4
  `upload`. Stage 6 CLI either hides the command or returns
  "not yet supported".
- **Sharing methods** (`share_dir`, `create_shared_link`,
  `revoke_shared_link`, `list_shared_links`). Stubbed; revisit
  post-MVP per master plan §3.
- **`sync.py` adaptations** to consume the new API. Stage 5.
  `sync.py` stays in its Dropbox-flavored state for now.
- **Persisting `root_id` across daemon restarts.** Stage 6 (DB schema).
  Re-resolved on each startup for now.
- **In-memory fake-Drive (E1).** Stage 7. Stage 3 mocks at the
  `_get_service()` boundary on a per-test basis.
- **Webhook / push-based change notification.** Polling only per
  master plan §3.
- **`appProperties` schema.** A6 deferred unless Stage 3
  surfaces a concrete need (master plan §3); no such need has
  surfaced.
- **`acknowledgeAbuse=true` override for `cannotDownloadAbusiveFile`.**
  Future stage.

## 3. Architecture

### 3.1 Module layout

```
src/cumulus/
  client.py           MODIFIED — fills in get_metadata, list_revisions,
                                 download, list_folder, list_folder_iterator,
                                 get_latest_cursor, wait_for_remote_changes.
                                 list_remote_changes_iterator delegates to
                                 changes.py. Adds _ensure_root_id and
                                 _retry_transient.
  changes.py          NEW — iter_remote_changes + classify + helpers
                            (the 4-row A4 table, A3 scope check, cache
                            write-through, FileMetadata projection).
  native_docs.py      MODIFIED — wildcard predicate; is_shortcut helper.
  errorhandling.py    MODIFIED — convert_api_errors gains HttpError
                                 branch with per-status/per-reason mapping.
  path_cache.py       UNCHANGED — Stage 1 API is sufficient.
  core.py             UNCHANGED — existing FileMetadata / FolderMetadata
                                  / DeletedMetadata shapes fit Drive.

tests/offline/
  test_client.py          REBUILD — Drive-focused (was Dropbox-imports
                                    collection error).
  test_errorhandling.py   REBUILD — HttpError status mapping table.
  test_changes.py         NEW — classify table, scope check, cache
                                write-through, multi-page iteration.
  test_native_docs.py     NEW — wildcard predicate + shortcut handling.
```

### 3.2 Dependency direction

`changes.py` depends on `path_cache.py`, `native_docs.py`, `core.py`,
`errorhandling.py`, `path_codec.py`, and a small interface from
`client.py` (just `_get_service()` + `_retry_transient`). `client.py`
depends on `changes.py`. No cycles.

### 3.3 How the client gets its `PathIDCache`

`GoogleDriveClient.__init__` gains a required `path_cache: PathIDCache`
parameter. The cache is owned by `SyncEngine` (it lives on the same
`Database` instance the engine uses). `Manager.__init__` wires the two
together: it constructs the `SyncEngine` first, then passes
`sync_engine.path_cache` into `GoogleDriveClient.__init__`. Test code
that constructs the client directly passes a fake/in-memory cache.

The dependency direction stays acyclic: `client.py` imports
`PathIDCache` from `path_cache.py`; `SyncEngine` continues to construct
the cache; `Manager` glues. No change to `path_cache.py` itself.

### 3.4 Why extract `changes.py` (and only `changes.py`)

1. The change-feed has self-contained state-machine logic — the 4-row
   disambiguation table, parent-membership scoping, cache
   write-through, the move-out vs. never-in-scope split — that
   benefits from being a testable unit.
2. It is the largest single Stage-3 addition (~250–350 LOC); keeping
   `client.py` under ~1100 lines post-Stage-3 leaves headroom for
   Stage 4's mutating methods.
3. It is the only Stage-3 piece that does its own multi-call
   orchestration (paginate → classify → resolve → filter → mutate
   cache). Every other Stage 3 method is a thin wrapper over a single
   Drive call.

Splitting `metadata.py` and `download.py` out separately is deferred —
each is a small, mostly-stateless method whose body reads cleanly as a
method on the client. Re-evaluate after Stage 4 if the combined client
exceeds ~1500 lines.

## 4. Public API surface on `GoogleDriveClient`

Signatures and one-line semantics. Bodies land in the plan.

```python
# ---- Root / bootstrap -----------------------------------------------------

def _ensure_root_id(self) -> str:
    """Resolve and cache the Drive 'root' alias → fileId on path_cache.
    Idempotent; one round-trip per daemon lifetime."""

# ---- Metadata -------------------------------------------------------------

def get_metadata(self, remote_path: str,
                 include_deleted: bool = False) -> Metadata | None:
    """Cache-resolves remote_path → fileId; raises NotFoundError on miss.
    Returns FolderMetadata for folder MIME, FileMetadata for downloadable
    types, None for native-doc MIME (per A2)."""

def list_revisions(self, remote_path: str,
                   limit: int = 10) -> list[FileMetadata]:
    """Drive: revisions.list(fileId). 'mode' parameter dropped (Drive
    only accepts fileId). limit applied client-side after pagination."""

def restore(self, remote_path: str, rev: str) -> FileMetadata:
    """Stub — raises NotImplementedError('restore requires Stage 4 upload')."""

# ---- Download -------------------------------------------------------------

def download(self, remote_path: str, local_path: str,
             sync_event: SyncEvent | None = None) -> FileMetadata:
    """Drive: files.get_media(fileId) via MediaIoBaseDownload chunked.
    Chunk size 8 MB. After each chunk: update sync_event.completed if
    provided; honor bandwidth_limit_down. On completion: md5 verify
    against file.md5Checksum (DataCorruptionError on mismatch). Raises
    UnsupportedFileError on native-doc MIME (defensive)."""

# ---- Listing --------------------------------------------------------------

def list_folder(self, remote_path: str,
                recursive: bool = False,
                include_deleted: bool = False,
                include_mounted_folders: bool = True,
                include_non_downloadable_files: bool = False
                ) -> ListFolderResult:
    """Thin wrapper that exhausts list_folder_iterator pages."""

def list_folder_iterator(self, remote_path: str, recursive: bool = False,
                         **kwargs) -> Iterator[ListFolderResult]:
    """recursive=True is the bootstrap path (§5). recursive=False:
    cache-resolves remote_path to parentId; paginates
    files.list(q="'<parentId>' in parents and trashed=false"). Native
    docs filtered out unless include_non_downloadable_files."""

# ---- Change feed ----------------------------------------------------------

def get_latest_cursor(self, remote_path: str,
                      include_non_downloadable_files: bool = False) -> str:
    """Drive: changes.getStartPageToken(). remote_path silently ignored
    (Drive's startPageToken is account-global, not folder-scoped). The
    parameter is preserved for sync-engine compatibility; Stage 5 may
    drop it from the signature when adapting sync.py."""

def wait_for_remote_changes(self, last_cursor: str,
                            timeout: int = DEFAULT_POLL_INTERVAL) -> bool:
    """Interruptible sleep via self._poll_event.wait(timeout), then
    changes.list(pageToken=last_cursor, pageSize=1,
    fields='changes(fileId)'). Returns True iff response has any
    changes."""

def list_remote_changes_iterator(self, last_cursor: str
                                 ) -> Iterator[ListFolderResult]:
    """Delegates to changes.iter_remote_changes (§6)."""
```

### 4.1 Sleep interruptibility

`GoogleDriveClient.__init__` adds two new attributes:

- `self._path_cache: PathIDCache` — from the constructor parameter (§3.3).
- `self._poll_event: threading.Event` — fresh per client instance.

`close()` and `unlink()` are extended to call `self._poll_event.set()`
(currently `close()` is a `pass` and `unlink()` only clears credential
state). Both `_retry_transient`'s backoff sleeps and
`wait_for_remote_changes`'s poll sleep use
`self._poll_event.wait(seconds)` so a stop signal interrupts both
within ~1 ms instead of waiting out the full interval.

### 4.2 Native-doc filtering inventory

| Site | Behavior on native/shortcut MIME |
|---|---|
| `client.get_metadata` | returns `None` |
| `client.list_folder_iterator` | dropped from yielded pages before `populate_from_listing` (recursive) or before yielding (non-recursive) |
| `changes.iter_remote_changes` | dropped, no cache write |
| `client.download` | raises `UnsupportedFileError` (defensive) |
| `path_cache.populate_from_listing` | accepts whatever it's given and BFS-filters by reachability; it does not itself filter by MIME, so the caller must pre-filter natives |

## 5. Initial recursive listing (bootstrap)

`list_folder('/', recursive=True)` is the one method that populates
the cache from cold. Every other method assumes a warm cache.

### 5.1 Algorithm

```
_ensure_root_id()

# Step 1 — page through everything visible to the user.
raw_items = []
page_token = None
while True:
    resp = retry(lambda: service.files().list(
        q="trashed=false",
        fields=("nextPageToken, files(id, name, parents, mimeType, "
                "modifiedTime, size, md5Checksum, shared, version)"),
        pageSize=1000,
        pageToken=page_token,
    ).execute())
    raw_items.extend(resp.get("files", []))
    page_token = resp.get("nextPageToken")
    if not page_token:
        break

# Step 2 — drop non-folder native MIME types; keep folders + downloadable.
# is_google_native already excludes the folder MIME (§8.1), so this single
# predicate keeps folders and regular files and drops Docs/Sheets/Slides/
# Forms/shortcuts/etc.
filtered = [it for it in raw_items if not is_google_native(it.get("mimeType"))]

# Step 3 — atomic populate. path_cache BFS-filters to items reachable
# from root_id; everything else (Computers, Backup, shared-with-me,
# orphans) is dropped with a debug log per item.
self._path_cache.populate_from_listing(filtered)

# Step 4 — yield pages of the resulting cache state as ListFolderResult.
yield from _yield_pages_from_cache(self._path_cache, page_size=1000)
```

### 5.2 Why one giant `files.list` instead of BFS

Drive's `files.list` does *not* have a "descendants of X" filter. The
alternatives are:

1. **One big list, then BFS-filter in `populate_from_listing`** (chosen).
   Reuses Stage 1 code. Orphans / shared-with-me / Computers drop out
   naturally. API cost: ~N/1000 requests; for 50k files = 50 requests
   ≈ 0.4 % of daily quota. Memory peak: ~25 MB at 50k items × ~500
   bytes per dict.
2. **BFS through `'<parent>' in parents` queries.** One `files.list`
   per folder. For 5k folders = 5k requests ≈ 40 % of daily quota.
   Memory-cheap, quota-expensive.

Approach 1 wins on quota at all realistic scales. The memory ceiling
becomes a concern at >500k files — out of scope for v1.

### 5.3 Computers / Backup / shared-with-me handling (C6)

These appear in `files.list` because they are visible to the user, but
their `parents` chain does not reach `root_id`. `populate_from_listing`
already BFS-filters by reachability from `root_id` and drops anything
else.

C6's "skip with log warning" requirement: one INFO log after the
populate of the form `"Skipped N items outside My Drive root (e.g.
Computers/Backup, shared-with-me): see debug log for details."`. Per-item
detail at DEBUG only — avoids log spam for accounts with many
shared-with-me items.

### 5.4 `_ensure_root_id`

```python
def _ensure_root_id(self) -> str:
    if self._path_cache.root_id is not None:
        return self._path_cache.root_id
    resp = self._retry_transient(lambda: self._get_service().files().get(
        fileId="root", fields="id"
    ).execute())
    self._path_cache.root_id = resp["id"]
    return resp["id"]
```

Called by every method that hits the API. One round-trip per daemon
lifetime (in memory only; Stage 6 persists).

### 5.5 Bootstrap contract

Spec contract: `list_folder('/', recursive=True)` is only valid on
initial sync. Calling it mid-sync wipes the cache and discards any
deltas the change feed has accumulated. Documented at the method's
docstring; Stage 5 sync-engine port enforces this at the caller.

## 6. The change-feed module — `src/cumulus/changes.py`

### 6.1 Public surface

```python
def iter_remote_changes(
    service_factory: Callable[[], "Resource"],
    retry: Callable[[Callable[[], Any]], Any],
    path_cache: PathIDCache,
    last_cursor: str,
) -> Iterator[ListFolderResult]:
    """Paginate Drive's changes.list and yield one ListFolderResult per
    page. Each yielded entry has paths resolved via path_cache; the
    cache is mutated as a side effect (set / move / remove). The cursor
    on each yielded page is the *next* pageToken, or the
    newStartPageToken on the final page."""
```

Pure-ish: state lives in `path_cache` only. Tests pass a fake
`service_factory` plus an in-memory cache.

### 6.2 Per-page algorithm

```
for each page of changes.list(pageToken=cursor, fields="..."):
    out_entries = []
    for change in page["changes"]:
        decision = classify(change, path_cache)
        if decision in (OUT_OF_SCOPE, UNKNOWN_FILE):
            continue
        if decision is DELETE:
            path = path_cache.id_to_path(change["fileId"])
            if path is None:
                continue            # never knew about this file
            path_cache.remove(path)
            out_entries.append(DeletedMetadata(...))
        elif decision is APPLY:
            file = change["file"]
            if is_google_native(file["mimeType"]) or is_shortcut(file["mimeType"]):
                continue            # A2: drop natives + shortcuts
            new_parent_id = file["parents"][0]
            new_path = compose_path(
                path_cache.id_to_path(new_parent_id),
                file["name"],
            )
            old_path = path_cache.id_to_path(file["id"])
            is_folder = (file["mimeType"] == GOOGLE_FOLDER_MIME_TYPE)
            if old_path is None:
                path_cache.set(new_path, file["id"], new_parent_id,
                               file["name"], is_folder=is_folder)
            elif old_path != new_path:
                path_cache.move(old_path, new_path)  # Q5: one atomic entry
            # else: pure content update; cache already correct
            out_entries.append(_to_file_or_folder_metadata(file, new_path))

    yield ListFolderResult(
        entries=out_entries,
        has_more=("nextPageToken" in page),
        cursor=page.get("nextPageToken") or page["newStartPageToken"],
    )
```

### 6.3 `classify()` — the A4 disambiguation table

```python
def classify(change, path_cache):
    if change.get("removed"):
        return DELETE              # row 1: hard-delete / unshare / lost visibility
    file = change.get("file")
    if file is None:
        return UNKNOWN_FILE        # malformed; drop with debug log
    if file.get("trashed"):
        return DELETE              # row 2: user trashed
    parents = file.get("parents") or []
    in_scope = any(path_cache.id_to_path(p) is not None for p in parents)
    if not in_scope:
        # row 3 split: move-out vs. never-in-scope.
        if path_cache.id_to_path(file["id"]) is not None:
            return DELETE          # move-out → propagate local delete
        return OUT_OF_SCOPE        # never in scope; drop silently
    return APPLY                   # row 4
```

The row-3 split is not literally in the master design's table but is a
direct consequence of it: move-out emits the same `removed=false`
shape as a never-in-scope create, and cache membership of the fileId
is the only signal that distinguishes them.

### 6.4 No `files.get` fallback in Stage 3

Master plan §5.3 A3 says "misses trigger a `files.get` to learn parents."
Re-reading the disambiguation table, no row constructed against the
spike's actual payload requires this. For `removed=true` the file is
gone on Drive; `files.get` would 404. For `removed=false` the change
entry already carries `file.parents`. The cache-membership check
covers both move-out and never-in-scope.

Spec decision: no `files.get` fallback in Stage 3. Unknown fileIds are
dropped silently with a DEBUG log. If Stage 3 implementation surfaces
a concrete trigger, fold it in as a deviation. The mention in the
master plan stays as an option for a later stage.

### 6.5 Field projection — `_to_file_or_folder_metadata`

| `core.FileMetadata` field | Drive source |
|---|---|
| `name` | `file.name` (raw; may contain `/`) |
| `path_display` | encoded `new_path` |
| `path_lower` | `fold_path(new_path)` |
| `id` | `file.id` |
| `client_modified` | `file.modifiedTime` (Drive collapses to one timestamp; Stage 5 A1 may rewire) |
| `server_modified` | same as `client_modified` |
| `rev` | `str(file.version)` (Drive's analogue of Dropbox's rev) |
| `size` | `int(file.size)` |
| `symlink_target` | always `None` (master plan: symlinks unsupported) |
| `shared` | `file.shared` if present, else `False` |
| `modified_by` | `file.lastModifyingUser.permissionId` if present, else `None` |
| `is_downloadable` | `True` (after A2 filter) |
| `content_hash` | `file.md5Checksum` |

`FolderMetadata`: subset — `id`, `name`, `path_lower`, `path_display`,
`shared`.

## 7. Error mapping and retry

### 7.1 The HTTP mapping table (`errorhandling.py`)

`convert_api_errors` gains an `HttpError` branch inserted before the
existing `CONNECTION_ERRORS` catch:

```python
except googleapiclient.errors.HttpError as exc:
    status = exc.resp.status
    reason = _extract_reason(exc)  # parses error_details / content JSON
    raise _http_error_to_cumulus(
        status, reason, exc, remote_path, local_path
    ) from exc
```

`_http_error_to_cumulus` dispatch:

| Status | Sub-reason | Cumulus exception |
|---|---|---|
| 400 | (any) | `BadInputError` |
| 401 | (any) | `TokenRevokedError` (refresh already failed if we are here) |
| 403 | `userRateLimitExceeded` / `rateLimitExceeded` / `sharingRateLimitExceeded` | `ConnectionError` (post-retry only) |
| 403 | `storageQuotaExceeded` | `InsufficientSpaceError` |
| 403 | `dailyLimitExceeded` | `ConnectionError` + prominent WARNING log |
| 403 | `cannotDownloadAbusiveFile` | `RestrictedContentError` |
| 403 | (other) | `InsufficientPermissionsError` |
| 404 | (any) | `NotFoundError` |
| 429 | (any) | `ConnectionError` (post-retry only) |
| 5xx (500, 502, 503, 504) | (any) | `ServerError` (post-retry only) |
| (other) | (any) | `CumulusApiError` (generic) |

"Post-retry only" rows mean the exception only reaches the mapping
after `_retry_transient` exhausted its budget. First occurrence is
re-tried internally and never surfaces.

### 7.2 The retry helper (`client.py`)

```python
_RETRYABLE_403_REASONS = frozenset({
    "userRateLimitExceeded",
    "rateLimitExceeded",
    "sharingRateLimitExceeded",
})
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 5
_INITIAL_BACKOFF = 1.0      # seconds
_MAX_BACKOFF = 32.0

def _retry_transient(self, call: Callable[[], T]) -> T:
    """Run `call()`. On transient Drive errors, retry with exponential
    backoff + jitter. Honor Retry-After when present. After exhaustion,
    raise the last exception unchanged so convert_api_errors can map it.
    Raises CancelledError if `_poll_event` fires during a backoff
    sleep (daemon shutdown)."""
    attempt = 0
    backoff = _INITIAL_BACKOFF
    while True:
        try:
            return call()
        except HttpError as exc:
            if not _is_retryable(exc) or attempt >= _MAX_ATTEMPTS - 1:
                raise
            sleep_for = _retry_after_seconds(exc) or backoff + random.uniform(0, 0.5)
        except CONNECTION_ERRORS:
            if attempt >= _MAX_ATTEMPTS - 1:
                raise
            sleep_for = backoff + random.uniform(0, 0.5)
        if self._poll_event.wait(sleep_for):
            raise CancelledError(
                "Sync cancelled", "Daemon shutdown during retry backoff."
            )
        backoff = min(backoff * 2, _MAX_BACKOFF)
        attempt += 1
```

`_is_retryable(exc)`: `exc.resp.status in _RETRYABLE_STATUS` OR
(`exc.resp.status == 403` AND parsed reason in `_RETRYABLE_403_REASONS`).

The helper takes a no-arg callable rather than being a method
decorator: the retry boundary wraps exactly the `.execute()` call, not
the surrounding cache-write / path-translation logic. Cache mutations
after a successful API call must not be retried on a subsequent
failure.

### 7.3 Call-site pattern

```python
with convert_api_errors(remote_path=remote_path):
    response = self._retry_transient(
        lambda: self._get_service().files().get(fileId=fid, fields=...).execute()
    )
```

`convert_api_errors` is the outer layer; it sees the final exception
after retries exhaust. `_retry_transient` is the inner layer; it sees
raw `HttpError` and decides whether to retry.

## 8. A2 — Native-docs filter + shortcuts

### 8.1 Wildcard predicate

`native_docs.py`:

```python
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"

def is_google_native(mime_type: str | None) -> bool:
    """True for any application/vnd.google-apps.* type except folder.
    Forward-compatible against new Google-native MIME subtypes."""
    if mime_type is None:
        return False
    return (
        mime_type.startswith(GOOGLE_NATIVE_PREFIX)
        and mime_type != GOOGLE_FOLDER_MIME_TYPE
    )

def is_google_folder(mime_type: str | None) -> bool:
    return mime_type == GOOGLE_FOLDER_MIME_TYPE

def is_shortcut(mime_type: str | None) -> bool:
    return mime_type == GOOGLE_SHORTCUT_MIME_TYPE
```

The old `GOOGLE_NATIVE_MIME_TYPES` frozenset becomes dead and is
deleted. `filter_native_docs` keeps working because it routes through
`is_google_native`.

### 8.2 Shortcut handling

Shortcuts match `is_google_native` under the wildcard, so they are
filtered at the same boundaries as Docs / Sheets / Slides without
extra logic. `shortcutDetails.targetId` is *not* followed: shortcuts
are a UX-layer feature; following them would double-count files
(target also appears under its real parent) and the target might be a
native doc anyway. Skipping is the only consistent rule.

## 9. Tests

### 9.1 Existing-test posture

Two pre-existing collection errors clear in this stage:

- `tests/offline/test_client.py` — rebuilt; Dropbox imports deleted.
- `tests/offline/test_errorhandling.py` — rebuilt for the HttpError
  mapping table.

`test_client_auth.py`, `test_oauth.py`, `test_errorhandling_auth.py`,
`test_path_cache.py`, `test_path_codec.py` are untouched.

### 9.2 Mocking pattern

`client._get_service()` is replaced with a `MagicMock` whose chained
attribute access (`.files().get().execute()` etc.) returns canned dicts.
This continues the Stage 2 pattern for `_fetch_account_info_for_creds`.
Stage 7's in-memory fake-Drive (E1) supersedes this once it lands.

### 9.3 `test_client.py` coverage (rebuilt)

- `_ensure_root_id` — one round-trip, cached, idempotent.
- `get_metadata` — cache hit returns FileMetadata; cache miss raises
  `NotFoundError`; native-doc MIME returns `None`; folder MIME returns
  FolderMetadata.
- `list_revisions` — pagination exhausts; `limit=N` truncates
  client-side; cache miss raises `NotFoundError`.
- `download` — chunked download via `MediaIoBaseDownload`;
  `sync_event.completed` updated per chunk; `bandwidth_limit_down`
  respected; md5 verification on completion (DataCorruptionError on
  mismatch); raises `UnsupportedFileError` for native-doc MIME.
- `list_folder_iterator(recursive=False)` — cache-resolves parent,
  paginates `files.list`, filters natives, yields ListFolderResult
  pages.
- `list_folder_iterator(recursive=True)` — bootstrap path: dumps
  everything, calls `path_cache.populate_from_listing`, yields
  cache-derived pages. Skipped-items INFO log fires when fixture
  includes shared-with-me / Computers items.
- `get_latest_cursor` — returns Drive's `startPageToken`; `remote_path`
  argument silently ignored.
- `wait_for_remote_changes` — sleeps `timeout` seconds (mocked); True
  when changes-list has entries, False when empty; sleep interrupted
  by `_poll_event.set()` returning early.
- `_retry_transient` — retries `_RETRYABLE_STATUS` codes up to
  `_MAX_ATTEMPTS`; honors `Retry-After`; exp backoff with jitter;
  CONNECTION_ERRORS retry; non-retryable HttpError propagates
  unchanged; max-attempts exhaustion propagates last exception;
  shutdown via `_poll_event.set()` aborts the backoff sleep.

### 9.4 `test_errorhandling.py` coverage (rebuilt)

One parametric test for the full HTTP mapping table — 11 rows ×
representative reason strings, asserts each maps to the right
exception subclass with `__cause__` preserved. Existing OSError
mapping tests kept. Stage 2 RefreshError / GoogleAuthError mappings
remain in `test_errorhandling_auth.py`.

### 9.5 `test_changes.py` coverage (new)

Per-row tests for `classify()`:

1. `removed=true, no file` → `DELETE`.
2. `removed=false, file.trashed=true` → `DELETE`.
3. `removed=false, file.trashed=false, parents=[outside-cache], fileId in cache` → `DELETE` (move-out).
4. `removed=false, file.trashed=false, parents=[outside-cache], fileId NOT in cache` → `OUT_OF_SCOPE`.
5. `removed=false, file.trashed=false, parents=[inside-cache]` → `APPLY`.
6. `removed=false, file=None` → `UNKNOWN_FILE` (malformed).

`iter_remote_changes` integration tests:

- single-page change list → one ListFolderResult, cursor = `newStartPageToken`.
- multi-page → multiple ListFolderResult, intermediate cursors = `nextPageToken`.
- intra-batch dependency: create folder X (entry 1) then move file F
  into X (entry 2) — F resolves via cache because X was written first.
- rename: entry maps cache `old_path` → `new_path` via `path_cache.move`.
- native doc in change feed → dropped, no cache write.
- shortcut in change feed → dropped, no cache write.
- delete of unknown fileId → silently dropped (no error, no cache
  write).

### 9.6 `test_native_docs.py` coverage (new)

- Known native types still match (documents, spreadsheets,
  presentations, drawings, forms, sites, jam, script, map, shortcut,
  drive-sdk).
- Folder MIME does NOT match `is_google_native`.
- Hypothetical future type `application/vnd.google-apps.notebook`
  matches via wildcard (regression test for the enumeration → wildcard
  switch).
- `is_shortcut` only matches the shortcut MIME.
- `None` / empty string don't crash.

### 9.7 Manual smoke test (master design §7)

Recorded as a checklist file alongside the stage plan. Run once
against the real Google account when implementation is complete. Six
items:

1. `cumulus auth link` against the smoke-test account (Stage 2 path).
2. From a fresh DB: trigger `list_folder('/', recursive=True)`
   (synthesized via `python -c` or a CLI sub-command). Verify the path
   cache populates with ~N entries matching the account, with native
   docs / Computers / shared-with-me dropped per the INFO log.
3. `get_latest_cursor('/')` returns a token; record it.
4. Mutate the Drive (web UI): create a folder, upload a file into it,
   rename a file, trash a file, move a file out of My Drive into
   Trash. Wait 30 s.
5. `list_remote_changes_iterator(<token>)` once: verify the create /
   rename / trash / move-out each produce the expected `FileMetadata`
   / `DeletedMetadata`. Verify `path_cache` reflects the new state.
6. `download` one of the changed files; verify byte equality against a
   parallel web-UI download and confirm md5 verification logs success.

### 9.8 Definition of "Stage 3 done"

1. Spec committed under `docs/superpowers/specs/`.
2. Implementation plan committed under `docs/superpowers/plans/`.
3. All Stage 3 code merged to `google-drive-port`.
4. Two pre-existing test collection errors (`test_client.py`,
   `test_errorhandling.py`) cleared. New offline tests pass; existing
   offline tests (Stage 1 + Stage 2) still pass at the current
   baseline. The pre-existing failures in `test_manager.py`,
   `test_main.py`, `test_event_handler.py`, `test_daemon.py` remain
   Stage 5 / 6 work.
5. Manual smoke test §9.7 completed; result recorded in the plan's
   deviations file.

## 10. Risk register

| Risk | Mitigation |
|---|---|
| Single-page `files.list` bootstrap exceeds memory on very large drives (>500k files) | Out of scope for v1 (master plan §3). Memory peak ~25 MB at 50k files. Stage 7's load test (E3) is the natural place to re-evaluate. |
| `populate_from_listing` rebuilds the cache from scratch on every recursive bootstrap — wipes subsequent change-feed deltas if the engine ever calls bootstrap mid-sync | Spec contract: `list_folder('/', recursive=True)` is initial-sync only. Documented on the method's docstring; Stage 5 enforces at the caller. |
| Orphan log floods on accounts with many shared-with-me items | One INFO summary line after bootstrap; per-item only at DEBUG. If smoke test surfaces a real-world orphan rate that still spams, demote populate_from_listing's per-orphan log to DEBUG. |
| `cannotDownloadAbusiveFile` lands on a file the user actually needs | `RestrictedContentError` is terminal; CLI hint points to Drive's `?acknowledgeAbuse=true` override. Implementing the override is deferred. |
| Retry budget exhaustion under sustained rate-limit pressure surfaces as `ConnectionError` to the sync engine, which retries — possible infinite outer loop | Stage 5 sync engine owns its own retry budget. Stage 3 prints a prominent WARNING log on retry exhaustion so operators see it. |
| `_poll_event` shared between `_retry_transient` sleep and `wait_for_remote_changes` sleep — spurious early wakes if one wait-point trips the other | Single event per client instance is intentional. Both wait-points want to be interrupted by the same shutdown signal; sharing is the correct semantics, not a bug. |
| The change-feed's intra-batch dependency rule (folder-create before file-move-into) relies on Drive emitting changes in time order across pages | Drive's `changes.list` documents in-order delivery. Smoke test §9.7 steps 4–5 exercises this with a real fixture. |
| `str(file.version)` as `rev` may collide with Stage 5's expectations | Stage 5 (A1) is the explicit owner of the `modifiedTime` / `rev` / `client_modified` collapse. Stage 3 ships the most natural mapping; Stage 5 may rewire. |
| Tests mocking at `_get_service()` boundary diverge from real googleapiclient behavior | Stage 7's in-memory fake-Drive (E1) is the planned mitigation. Smoke test §9.7 is the Stage-3 backstop. |

## 11. Open questions

None blocking. Two items resolved during implementation:

- Exact log wording / level for the skipped-items summary
  (cosmetic; lands in plan).
- Whether `wait_for_remote_changes` should use `pageSize=1` for the
  existence-check call or page through to count — default to
  `pageSize=1` (cheapest); revise only if a smoke-test scenario
  surfaces a problem with the "true/false" return contract.

One item explicitly deferred:

- `restore()` stays a stub. Stage 4 picks it up as a small wrapper
  once `upload` exists.

## 12. Next step

After this design is approved, invoke `superpowers:writing-plans` to
draft the Stage 3 implementation plan at
`docs/superpowers/plans/2026-05-12-stage-3-read-client.md`.
