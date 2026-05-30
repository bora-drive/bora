# Stage 4 — Write client (upload / move / delete / mkdir / restore)

**Status:** Design (awaiting review)
**Date:** 2026-05-30
**Stage of:** `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` §5.4
**Inputs:** the staged-port plan, `docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md` Q4/Q5/Q6 and §C3/§C5, the post-Stage-3 state of `src/cumulus/client.py` / `changes.py` / `path_cache.py` / `path_codec.py` / `errorhandling.py`, `docs/superpowers/plans/2026-05-12-stage-3-read-client-deviations.md` §"Notes for Stage 4 / Stage 5", and the Stage 3 design for the patterns this stage extends.

---

## 1. Purpose

Stand up the write half of the Google Drive client: upload (simple +
resumable), file/folder mutations (`move`, `remove`, `make_dir`), the two
batch operations, and `restore`. After this stage, the daemon can push
local changes to Drive. The stage also lands the write-side error-mapping
rows (remainder of Phase 6) and resolves the `restore()` `NotImplementedError`
stub deferred from Stage 3.

Stage 4 does not adapt `sync.py` to drive these methods; that is Stage 5.
Stage 4 does not implement echo suppression (so the change feed
re-observes the writes the client just made — that's a Stage 5 concern,
and `path_cache`'s mutations are idempotent so the re-observation is
harmless at the cache layer). Stage 4 does not implement sharing
(`share_dir` etc.) — those stay stubbed per the master plan.

## 2. Scope

### 2.1 In scope

- **Phase 5e — upload.** `upload(local_path, remote_path, write_mode,
  update_rev, autorename, sync_event)`. Simple upload ≤5 MB
  (`MediaFileUpload(resumable=False)`); resumable >5 MB with 8 MB
  chunks, mirroring Stage 3's `download` chunking. `WriteMode.{Add,
  Update, Overwrite}` semantics per the §5.1 table. `md5Checksum`
  verification after upload. Bandwidth-throttle and shutdown-interrupt
  respect between chunks, reusing the `_poll_event` machinery from
  Stage 3.
- **Phase 5f — file/folder ops.** `remove(remote_path, parent_rev)`,
  `move(remote_path, new_path, autorename)`, `make_dir(remote_path,
  autorename)`, `make_dir_batch(remote_paths)`,
  `remove_batch(entries)`. Combined move+rename in one `files.update`
  (A5).
- **`restore(remote_path, rev)`.** Revision-download-to-tempfile via
  `revisions().get(fileId, revisionId=rev, alt=media)`, then re-upload
  through the upload path as `WriteMode.Update` (new current revision).
  Native Google docs raise `UnsupportedFileError`.
- **C3 — upload name handling.** Decode the local name segment via
  `path_codec.decode_name` before sending to Drive (the reverse of
  what listing does on the read side). A local file `a%2Fb.txt` is
  uploaded as Drive `a/b.txt`; a local `a%2525b.txt` becomes Drive
  `a%25b.txt`.
- **C4 — trash vs delete default.** Sync-driven deletes default to
  `files.update(fileId, body={trashed: True})` (recoverable for 30
  days). The client also supports hard `files.delete` as a primitive;
  exposing it to users via a CLI flag is a Stage 6 decision.
- **C5 — `storageQuotaExceeded` mid-upload.** Abort the resumable
  session, translate to `InsufficientSpaceError`, let the sync engine
  decide retry/pause policy. The client carries no cross-call upload
  state.
- **D7 — `autorename`.** When the target name collides with an
  existing sibling, append ` (N)` (N = 1, 2, …) and retry until a name
  free of `path_cache` collisions is found. Uses `path_cache` only —
  no extra Drive API call.
- **Write-side error mapping.** `errorhandling.py`
  `convert_api_errors` gains the per-status/per-reason rows in §6.1
  (storage quota, write permissions, update-target 404,
  precondition-fail, invalid-name). The Stage 3 `HttpError` branch
  already exists; Stage 4 appends rows.
- **Path-cache write-through.** Every successful write op calls the
  corresponding `path_cache.set` / `move` / `remove` synchronously,
  consistent with how `changes.py` mutates the cache during change-feed
  iteration. Make-then-upload chains (`make_dir("/foo")` then
  `upload("/foo/bar.txt")`) work without waiting for the change feed.
- **Quota counter on the write side.** `writes.py` is threaded
  `_count_request`. Batch ops increment by the number of sub-requests
  (a 100-delete batch consumes 100 units in one HTTP round-trip). This
  closes part of the Stage 3 quota-accounting gap on the write side.
  The read-side gap in `changes.py` is unchanged this stage.
- **Test rebuilds and extensions.** New `tests/offline/test_writes.py`
  for the mutating-op surface. `tests/offline/test_client.py` extended
  for the write delegators. `tests/offline/test_errorhandling.py`
  extended for the new write-side `HttpError` rows.

### 2.2 Out of scope

- **`sync.py` adaptation** to call the new write methods. Stage 5.
- **Echo suppression** — preventing the sync engine from treating the
  client's own write as a remote-originating change. Stage 5 owns this
  via the cursor / pending-write set.
- **Sharing methods** (`share_dir`, `create_shared_link`,
  `revoke_shared_link`, `list_shared_links`). Stay stubbed; revisit
  post-MVP per master plan §3.
- **CLI flag for irreversible `files.delete`.** Master plan §5.4
  defers to Stage 6. The Stage 4 client supports the primitive;
  Stage 6 decides exposure.
- **In-memory fake-Drive (E1).** Stage 7. Stage 4 mocks at the
  `_get_service()` boundary, per Stage 3's convention.
- **Retrofitting `_count_request` into `changes.py`.** Stage 3
  deviation noted this as "Stage 4/5 may thread the counter through";
  Stage 4 declines to expand scope into the read side. Stage 5 may
  pick it up.
- **`acknowledgeAbuse=true` on download — not a write concern.**
- **Persisting `root_id` across daemon restarts.** Stage 6 (DB
  schema).
- **Ordering ancestors in `make_dir_batch`.** The sync engine creates
  folders top-down; the batch helper does not itself reorder or insert
  missing parents. See §4.5 assumption.

## 3. Architecture

### 3.1 Module layout

```
src/cumulus/
  client.py           MODIFIED — upload/remove/move/make_dir/
                                 make_dir_batch/remove_batch/restore
                                 become thin delegators to writes.py.
                                 share_dir stays stubbed. Stays
                                 ~1200 lines.
  writes.py           NEW — mutating-op orchestration: resumable-upload
                            state machine, WriteMode resolution,
                            autorename loop, batch sub-request
                            grouping, restore (revision fetch +
                            re-upload), cache write-through. ~400-550
                            LOC.
  errorhandling.py    MODIFIED — convert_api_errors HttpError branch
                                 gains the write-side rows in §6.1.
  path_cache.py       UNCHANGED — set/move/remove suffice.
  path_codec.py       UNCHANGED — decode_name reused on the upload
                                  path.
  core.py             UNCHANGED — WriteMode, FileMetadata /
                                  FolderMetadata fit Drive writes
                                  unchanged.

tests/offline/
  test_writes.py           NEW — see §7.
  test_client.py           EXTENDED — write delegators wire-through.
  test_errorhandling.py    EXTENDED — write-side HttpError rows.
```

### 3.2 Dependency direction

`writes.py` depends on `path_cache.py`, `core.py`, `errorhandling.py`,
`path_codec.py`, `native_docs.py` (for the native-doc check in
`restore`), and a narrow interface from `client.py`: `_get_service()`,
`_retry_transient`, `_count_request`, `_ensure_root_id`, and the
`_poll_event` for throttle/shutdown. `client.py` depends on
`writes.py`. No cycles — same shape as the Stage 3 `client.py` ↔
`changes.py` split.

### 3.3 How `writes.py` is wired

`GoogleDriveClient.__init__` constructs a `_WriteHelper` instance and
holds it on `self._writes`. The helper takes a reference to the client
(typed as a `Protocol` covering the five client-internals listed
above). Each public write method on `GoogleDriveClient` is a one-line
delegator:

```python
def upload(self, local_path, remote_path, *, write_mode=WriteMode.Add,
           update_rev=None, autorename=False, sync_event=None):
    return self._writes.upload(local_path, remote_path,
                               write_mode=write_mode,
                               update_rev=update_rev,
                               autorename=autorename,
                               sync_event=sync_event)
```

The helper owns the resumable-upload loop, the autorename search, the
batch chunking, and the cache write-through. The client surface stays
the public API for the sync engine.

### 3.4 Why extract `writes.py` (and only `writes.py`)

1. The write side has the same property that justified the Stage 3
   `changes.py` extraction: real multi-call orchestration (resumable
   chunking, batch sub-request grouping, autorename retry loops,
   download-revision + re-upload) that's worth isolating as a testable
   unit.
2. Adding ~400-550 LOC of write code into `client.py` would push it to
   ~1600+ lines, well past the Stage 3 spec's "leave headroom" target.
   Keeping `client.py` ~1200 lines preserves it as a thin façade.
3. `move`, `remove`, `make_dir` individually are single-call wrappers,
   but each still needs path resolution + cache write-through + error
   mapping + (for `move`/`make_dir`) the autorename loop, so collocating
   them with `upload` and the batch ops keeps the cache write-through
   policy in one file.

### 3.5 File growth target

Post-Stage-4 line counts (target):
- `client.py` ≈ 1200 (delegators replace the `_not_implemented` stubs;
  net change small).
- `writes.py` 400–550 (new).
- `errorhandling.py` +30–50 (new HttpError rows).
- `tests/offline/test_writes.py` 600–900 (new).

## 4. Write operations

### 4.1 `upload`

```python
def upload(local_path, remote_path, *,
           write_mode=WriteMode.Add,
           update_rev=None,
           autorename=False,
           sync_event=None) -> FileMetadata
```

**Steps.**

1. Split `remote_path` into parent + name. Decode the local-name
   segment via `path_codec.decode_name` to recover the true Drive name
   (C3). Resolve the parent path → `parentId` via `path_cache`; missing
   parent → `NotFoundError`.
2. Compute the local MD5 while reading (streaming hash; reused for
   verify step 7).
3. Resolve `write_mode` against `path_cache` per the §4.1.1 table.
4. If `autorename=True` and the chosen name collides with an existing
   sibling in `path_cache`, run the §4.1.2 autorename loop to pick a
   unique ` (N)` name.
5. Choose simple vs resumable: `os.path.getsize(local_path) <=
   5 * 1024 * 1024` → `MediaFileUpload(resumable=False)`; else
   `MediaFileUpload(resumable=True, chunksize=8*1024*1024)`.
6. Drive the request:
   - **Create branch** (`files.create`): build body
     `{name, parents:[parentId]}`, `fields="id,name,md5Checksum,
     modifiedTime,size,headRevisionId"`.
   - **Update branch** (`files.update`): if `update_rev` was passed,
     first `files.get(fileId, fields='headRevisionId')` and compare
     to `update_rev`; mismatch → `DataChangedError` *before* the
     upload starts.
7. For the resumable path, loop `request.next_chunk()` until done.
   Between chunks: report progress to `sync_event` (bytes uploaded),
   honor the bandwidth throttle, honor `_poll_event` (shutdown
   interrupt — same machinery as Stage 3 `download`).
8. MD5 verify: compare the response `md5Checksum` against the local
   MD5 computed in step 2. Mismatch → `DataCorruptionError`. If the
   response omits `md5Checksum`, log a warning and skip (some unusual
   uploads — e.g. shortcuts created via this surface — may lack it;
   ordinary binary uploads always have it).
9. `path_cache.set(remote_path, response['id'])`.
10. Wrap response in `FileMetadata` and return.

`storageQuotaExceeded` from any chunk → abort, raise
`InsufficientSpaceError` (caught by the `_retry_transient` /
`convert_api_errors` wrapper around `next_chunk`).

#### 4.1.1 `WriteMode` resolution

| mode | file exists at path | action |
|---|---|---|
| `Add` | no | `files.create` |
| `Add` | yes, `autorename=False` | `FileConflictError` |
| `Add` | yes, `autorename=True` | autorename → `files.create` |
| `Update` | yes | `files.update` (media) on that ID |
| `Update` | no | `NotFoundError` |
| `Overwrite` | yes | `files.update` on that ID |
| `Overwrite` | no | `files.create` |

"Exists at path" = `path_cache.get(remote_path)` returns a non-`None`
ID.

#### 4.1.2 `autorename` loop

For a target path `/foo/bar.txt`:

```
candidate = "bar.txt"; n = 0
while path_cache.get(parent + "/" + candidate) is not None:
    n += 1
    stem, ext = os.path.splitext("bar.txt")
    candidate = f"{stem} ({n}){ext}"
```

Bounded at N=999 (defensive; never expected to hit). The same loop is
reused for `move` and `make_dir`. Quota cost: zero — `path_cache` is
in-process. Staleness risk acknowledged: if the local cache lacks a
Drive-side sibling created by a third party between bootstrap and this
op, the upload would silently produce a duplicate name on Drive. The
sync engine's `(conflicting copy …)` naming reduces collision rates;
true uniqueness is a Stage-5 sync-policy concern.

### 4.2 `move`

```python
def move(remote_path, new_path, *, autorename=False) -> FileMetadata | FolderMetadata
```

1. Resolve `remote_path` → `fileId` via `path_cache`; missing →
   `NotFoundError`.
2. Split `new_path` into new parent + new name. Decode new name via
   `path_codec.decode_name`. Resolve new parent → `newParentId`.
3. If `autorename=True` and the destination path is in `path_cache`,
   run the §4.1.2 loop to pick a non-colliding name.
4. Resolve the *old* parent (for `removeParents`): take the file's
   current parent from `path_cache` (parent of the old path → ID). For
   robustness against multi-parent files (Drive legacy — should not
   occur in our sync root after Stage 1's BFS), pass the current parent
   we know about rather than reading from Drive.
5. Single `files.update(fileId, body={name: <new_name>},
   addParents=newParentId, removeParents=oldParentId, fields=...)` —
   atomic move+rename per A5/spike Q5.
6. `path_cache.move(remote_path, new_path)`.
7. Return `FileMetadata` or `FolderMetadata` depending on the
   response `mimeType`.

### 4.3 `remove`

```python
def remove(remote_path, parent_rev=None) -> FileMetadata | FolderMetadata
```

1. Resolve `remote_path` → `fileId` via `path_cache`; missing →
   `NotFoundError`.
2. If `parent_rev` is given: `files.get(fileId,
   fields='headRevisionId,mimeType')`; mismatch →
   `DataChangedError`. (Folders have no revisions — a `parent_rev`
   on a folder path raises `BadInputError`.)
3. **Default (trash):** `files.update(fileId, body={trashed: True},
   fields='id,name,mimeType,trashed')`. This is the sync-driven
   default per C4 (recoverable for 30 days).
4. **Hard delete primitive:** an internal `_hard_delete(remote_path)`
   exists for Stage-6's eventual CLI flag and calls
   `files.delete(fileId)`. Not exposed on the public `remove`
   surface in Stage 4.
5. `path_cache.remove(remote_path)`.
6. Return metadata wrapped as `FileMetadata` / `FolderMetadata`
   reflecting the trashed state.

### 4.4 `make_dir`

```python
def make_dir(remote_path, *, autorename=False) -> FolderMetadata
```

1. Split into parent + name. Decode name via
   `path_codec.decode_name`. Resolve parent → `parentId`; missing →
   `NotFoundError`.
2. If `autorename=True` and path is in cache, run the §4.1.2 loop.
3. `files.create(body={name, mimeType:
   'application/vnd.google-apps.folder', parents:[parentId]},
   fields='id,name,mimeType')`.
4. `path_cache.set(remote_path, folderId)`.
5. Return `FolderMetadata`.

### 4.5 `make_dir_batch` and `remove_batch`

`remove_batch` follows the same trash-default as `remove` (C4): each
sub-request is `files.update(fileId, body={trashed: True})`. The
hard-`files.delete` path is not exposed via the public batch surface
in Stage 4 (Stage 6 decision, mirroring §4.3 step 4). If the input
tuple's `rev` is given, the same `headRevisionId` precondition check
applies *per sub-request*, costing one extra `files.get` per checked
entry (the sync engine rarely passes `rev` on bulk deletes, so the
extra cost is typically zero).

Drive's batch HTTP endpoint groups ≤100 sub-requests per round-trip
via `service.new_batch_http_request()`. The helper:

1. Chunk the input list into groups of 100.
2. For each chunk, build a `BatchHttpRequest`. For each path/entry,
   register a callback that captures the result-or-error keyed by the
   input index.
3. `batch.execute()` → wrapped in `_retry_transient` +
   `convert_api_errors`. Per-sub-request errors are translated by
   the callback into the right `CumulusApiError` subclass via
   `convert_api_errors`'s `HttpError` table.
4. `_count_request(n)` where `n` = sub-requests in this chunk.
5. On success per sub-request, update `path_cache` (set / remove).
6. After all chunks complete, return a list aligned with the input
   order: each entry is `FolderMetadata` (`make_dir_batch`) /
   `FileMetadata`-or-`FolderMetadata` (`remove_batch`) or a
   `CumulusApiError`.

**Assumption (out of scope to enforce):** `make_dir_batch` inputs'
parent folders already exist. The sync engine creates folders
top-down, so this holds in practice. If a parent is missing the
sub-request fails with `NotFoundError` and the batch result for that
index carries the error — no ordering or backfill is attempted.

### 4.6 `restore`

```python
def restore(remote_path, rev) -> FileMetadata
```

1. Resolve `remote_path` → `fileId` via `path_cache`; missing →
   `NotFoundError`.
2. `files.get(fileId, fields='mimeType')`; if
   `native_docs.is_google_native(mimeType)` → `UnsupportedFileError`
   ("restore not supported for native Google Docs files"). Native-doc
   revisions cannot be round-tripped as media bytes.
3. Open a `NamedTemporaryFile(delete=False)`. In a `try` /
   `finally` that unlinks the temp file:
   a. `revisions().get(fileId, revisionId=rev, alt=media)`,
      streamed via `MediaIoBaseDownload` with 8 MB chunks into the
      temp file (mirrors Stage 3 `download`). Honor `_poll_event`
      between chunks.
   b. Call the helper's own `upload` (§4.1) with
      `local_path=temp_file.name`, `remote_path=remote_path`,
      `write_mode=WriteMode.Update` — re-upload as a new current
      revision through the standard upload path (gets MD5 verify and
      throttle for free).
4. The newly-uploaded revision *is* the current content; Drive does
   not delete intervening revisions. `restore` is "make the old
   content current again," not "roll back history."
5. Return the `FileMetadata` from the `upload` call.

## 5. Path-cache write-through

Every successful write op mutates `path_cache` synchronously before
returning:

| op | cache call |
|---|---|
| `upload` (Add / Overwrite-create) | `set(remote_path, fileId)` |
| `upload` (Update / Overwrite-replace) | no-op (path↔ID unchanged) |
| `move` | `move(remote_path, new_path)` |
| `remove` (trash or hard) | `remove(remote_path)` |
| `make_dir` | `set(remote_path, folderId)` |
| `make_dir_batch` | `set` per successful sub-request |
| `remove_batch` | `remove` per successful sub-request |
| `restore` | no-op (path↔ID unchanged) |

Idempotence: `path_cache.set` overwrites, `remove` is a no-op for
absent keys, `move` is a single transaction. Re-application by the
change feed when it later observes the same mutation is harmless at
the cache layer. Echo suppression at the sync-engine layer is Stage 5.

## 6. Error handling

### 6.1 New `HttpError` rows in `convert_api_errors`

| HTTP status / reason | Cumulus exception |
|---|---|
| `403 storageQuotaExceeded` | `InsufficientSpaceError` |
| `403 insufficientFilePermissions` / write-denied reasons | `InsufficientPermissionsError` |
| `404` on update / move / delete target ID | `NotFoundError` |
| `412 conditionNotMet` (precondition / If-Match) | `DataChangedError` |
| `400 badRequest` (invalid name or parent) | `BadInputError` |

Read-side rows (Stage 3) stay; this stage appends. Reasons are read
from the `HttpError` body's `errors[0].reason`; statuses from
`resp.status`. Unknown reasons within a known status fall through to
the existing default mapping for that status.

### 6.2 Client-side raises (not via `HttpError`)

- `update_rev` mismatch (pre-upload `headRevisionId` check) →
  `DataChangedError`.
- MD5 verify mismatch (post-upload) → `DataCorruptionError`.
- `remove` with `parent_rev` on a folder → `BadInputError`.
- `restore` on a native Google doc → `UnsupportedFileError`.
- `autorename` loop exceeds N=999 → `BadInputError` (defensive;
  should be unreachable).

### 6.3 Wrapping

Every `.execute()` site in `writes.py` (including batch
`execute()` and resumable `next_chunk()`) is wrapped in
`_retry_transient(...)` and `convert_api_errors`, same as Stage 3.
The pre-upload `files.get` for `update_rev` is also wrapped.

## 7. Testing

`tests/offline/test_writes.py` (NEW) mocks at the
`_get_service()` boundary with an in-memory fake `PathIDCache`, per
the Stage 3 convention. The real fake-Drive is Stage 7. Coverage:

- **`WriteMode` resolution table.** All 7 rows from §4.1.1 — each row
  is a separate test.
- **Simple vs resumable.** A test at 5 MB - 1 byte uses
  `MediaFileUpload(resumable=False)`; a test at 5 MB + 1 byte uses
  resumable with the right chunksize.
- **Resumable iteration.** Multi-chunk fake of `next_chunk()`,
  asserting progress callbacks fire per chunk, throttle is honored,
  `_poll_event` aborts mid-stream.
- **MD5 verify.** Match → success; mismatch →
  `DataCorruptionError`; missing `md5Checksum` → warning + success.
- **`autorename`.** Single collision → `name (1)`; multi-collision →
  iterates to first free name; cap at N=999 → `BadInputError`.
- **`move`.** Combined move+rename in one `files.update` call (assert
  body shape: `name` + `addParents` + `removeParents`).
  `path_cache.move` called once with the right args.
- **`remove`.** Trash default (assert body
  `{trashed: True}`). `parent_rev` match → succeeds; mismatch →
  `DataChangedError`. `parent_rev` on folder → `BadInputError`.
  Hard-delete primitive (internal): calls `files.delete`.
- **`make_dir`.** Body shape includes the folder mimeType and parent.
- **Batches.** Input length 1, 99, 100, 101 — verify chunking (101 →
  2 batches, sizes 100+1). Mixed result list (some succeed, some
  fail) returns aligned errors. `_count_request` increments by total
  sub-request count, not batch count.
- **`restore`.** Happy path (download revision → re-upload → returns
  new `FileMetadata`). Native-doc mimeType → `UnsupportedFileError`
  before any download. Temp file is cleaned up on both success and
  exception (test by patching `unlink` or using a
  `tempfile.gettempdir()` snapshot).
- **`storageQuotaExceeded`** at any chunk → `InsufficientSpaceError`.
- **`path_cache` write-through.** For every successful op above,
  assert the cache was mutated exactly as §5 prescribes.

`tests/offline/test_client.py` gains short delegator tests for each
new public write method, asserting it calls the helper with the
expected arguments. (The bulk of coverage lives in `test_writes.py`.)

`tests/offline/test_errorhandling.py` gains the 5 new rows from §6.1
as parameterized cases.

## 8. Open questions and master-plan deferrals

- **CLI flag for irreversible delete (C4).** Deferred to Stage 6. The
  primitive lives in Stage 4; exposure is Stage 6.
- **Multi-parent files.** Drive legacy lets a file have multiple
  parents. Stage 1's BFS treats only the cache-known parent as in
  scope. `move` passes the cache-known parent in `removeParents` and
  does not attempt to detect or repair multi-parenting. If it
  surfaces in practice, revisit in Stage 5 or 6.
- **Retrofitting `_count_request` into `changes.py`.** Read-side
  scope; Stage 5 may pick it up.
- **Resumable session resumption on transient failure.** Stage 4
  catches transient errors per-chunk via `_retry_transient` (each
  `next_chunk` is a retryable unit). Cross-process session
  resumption (saving the resumable URL across daemon restarts) is
  not implemented — `_poll_event` shutdown mid-upload aborts the
  session; the sync engine reschedules the whole upload on next run.

## 9. Risks

- **Path-cache staleness in `autorename`** (§4.1.2). If a third party
  creates a sibling on Drive between bootstrap and a Cumulus upload,
  `autorename` could miss it and produce a same-name duplicate.
  Mitigation in this stage: rely on the sync engine's
  `(conflicting copy …)` naming for low collision rates. A future
  hardening pass could add a single `files.list` confirm before the
  create (one extra request per autorename); deferred.
- **Batch endpoint quota accounting** is sub-request-based but the
  HTTP round-trip is single. `_count_request(n)` accounts the units
  correctly; if Drive ever changes batch billing semantics, only
  `make_dir_batch` / `remove_batch` need adjustment.
- **`restore` of a very large file** stages the whole revision in a
  temp file. This is acceptable: Drive's file-size cap and the
  user's disk are the constraints. Streaming directly from the
  revision GET to the resumable upload PUT is a future optimization;
  the spec keeps the simple form.
- **`update_rev` race window.** Between the pre-upload
  `headRevisionId` check and the `files.update` call, the file
  could change again. The window is small (one round-trip), and the
  sync engine's later change-feed observation surfaces any
  subsequent conflict. Eliminating the window would require an HTTP
  If-Match on `files.update`; Drive's support for this is
  inconsistent across endpoints — deferred.

## 10. Acceptance criteria

Stage 4 is complete when:

1. `src/cumulus/writes.py` exists and implements the §4 ops,
   `client.py`'s `_not_implemented` stubs for `upload`, `remove`,
   `move`, `make_dir`, `make_dir_batch`, `remove_batch`, and the
   `NotImplementedError` stub for `restore`, are all replaced by
   one-line delegators.
2. `errorhandling.py` adds the §6.1 rows.
3. `path_cache` write-through happens per §5 on every successful
   write op.
4. `tests/offline/test_writes.py` covers §7's checklist and passes
   under `pytest tests/offline -k writes`.
5. `tests/offline/test_client.py` and
   `tests/offline/test_errorhandling.py` extensions pass.
6. The full offline test suite has no new failures beyond the
   pre-existing Stage 5/6 baseline.
7. A short smoke test against a real Drive account (committed as a
   record similar to `docs/superpowers/plans/2026-05-12-stage-3-read-client-deviations.md`)
   verifies: upload of a small file, upload of a >5 MB file, move,
   trash, make_dir, batch make_dir (≤3 paths), restore of a prior
   revision.
