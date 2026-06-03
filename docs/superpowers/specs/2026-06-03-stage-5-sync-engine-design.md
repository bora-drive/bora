# Stage 5 — Sync Engine Integration (design)

**Status:** Design (awaiting review)
**Date:** 2026-06-03
**Branch:** `google-drive-port`
**Master plan:** `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` §5.5
**Depends on:** Stage 3 (read client), Stage 4 (write client) — both complete + smoke-tested
**Inputs:** `SYNC_ENGINE_ANALYSIS.md`, Stage 3/4 deviations files, current `sync.py`/`core.py`/`utils/hashing.py`

---

## 1. Purpose & target end-state

Stage 5 is the **integration crucible**: it makes `SyncEngine` (`src/cumulus/sync.py`)
operate against `GoogleDriveClient` and Drive semantics instead of the Dropbox
SDK semantics it was forked with.

**Definition of done (this stage):** a Stage 4-style harness constructs a
`GoogleDriveClient` + `SyncEngine` against a real local folder and the real
`cumulus@arko.net` Drive, drives `upload_sync_cycle()` and
`download_sync_cycle()` by hand, and proves bidirectional sync works
end-to-end — a local file lands on Drive, a Drive change lands locally, and the
engine does **not** re-process its own writes. This matches the Stage 2-4
"smoke test PASS" bar. The full daemon/manager run is **not** required here; it
is Stage 6's smoke test.

This is an **adaptation** stage, not a new-subsystem stage. There are no new
top-level components. Work happens inside existing files, plus one in-place
replacement (`ContentHasher` for `DropboxContentHasher`).

## 2. Scope

### 2.1 In scope (load-bearing for a clean bidirectional smoke test)

1. **Phase 7 — MD5 content hashing.** Replace `DropboxContentHasher` with an
   MD5 `ContentHasher`; retarget `StreamHasher`; update `get_local_hash()`,
   `utils/path.py::content_hash()`, and every comparison site
   (`_check_requires_upload`, `_check_download_conflict`,
   `SyncEvent.from_file_system_event`, `SyncEvent.from_metadata`).
2. **A1 — `modifiedTime` semantics.** Collapse `FileMetadata.client_modified` +
   `server_modified` into a single `modified` field (Drive `modifiedTime`);
   repoint all readers; **set** `modifiedTime` on upload to stop round-trip
   clobbering / feedback.
3. **`rev` → version-id / download-by-fileId.** `rev` carries `headRevisionId`
   for files (`"folder"` sentinel unchanged for folders). Replace the
   `client.download(f"rev:{event.rev}", …)` callsite with a download by
   **fileId** (`event.change_remote_id`), current head, with the existing
   post-download conflict re-check absorbing the TOCTOU race.
4. **Conflict resolution.** `_check_download_conflict` /
   `_check_requires_upload` and the folder-vs-file check (sync.py ~2651)
   adapted to MD5 + single `modified` (no `client_modified`/`server_modified`
   split).
5. **Echo suppression (emergent).** Ensure the upload path records Drive's
   returned `headRevisionId` + `md5Checksum` into the index and sets
   `modifiedTime`, so a re-observed change matches the index and is skipped by
   the existing conflict logic. No separate pending-write mechanism is built.
6. **Wiring & lifecycle.** `SyncEngine` uses `GoogleDriveClient` + `path_cache`
   (drop the `import ... as DropboxClient` alias); `client.close()` reachable
   from shutdown.

### 2.2 Deferred (explicit — one-line note in the spec, tracked for later stage)

- **D1** daemon RPC field-rename audit, **D4** GUI compat decision → **Stage 6**
  (manager / CLI / external interface).
- `_throttle_up` / bandwidth policy, `batch_size` tuning, `writes.py`
  consolidation, `_wrap_metadata`/`_project_file_metadata` merge,
  `_count_request` threading into `changes.py` → cleanup, **Stage 6**.
- Full daemon/manager end-to-end run → **Stage 6** smoke test.
- **Cross-page parent/child change-feed ordering** durability (Stage 3
  carryover) → documented known limitation; durable fix in **Stage 6** (cursor
  durability). Unlikely to fire in the Stage 5 smoke test (small, single-page
  change sets).

### 2.3 Flagged-but-conditional

- **`move(update_rev=)`** — only becomes load-bearing if the conflict path needs
  a rev-checked move precondition. Resolved during the conflict-resolution work
  (item 4); if not needed, it defers to Stage 6.

## 3. The core semantic mapping

This table is the spine of the stage.

| Concept | Dropbox (today) | Google Drive (Stage 5) | Primary bite points |
|---|---|---|---|
| Version id (`rev`) | opaque per-version string | `headRevisionId`; `"folder"` sentinel for folders | `IndexEntry.rev`, conflict check (~3229), index updates |
| Content integrity | proprietary block hash | MD5 (`md5Checksum`), matches `hashlib.md5` (spike Q2) | `ContentHasher`, all `content_hash ==` sites |
| Modified time | `client_modified` + `server_modified` | single `modified` (`modifiedTime`) | `core.FileMetadata`, folder/file check (~2651), feedback prevention |
| Download a version | `download(f"rev:{rev}", …)` | `download` by **fileId** (current head) via `event.change_remote_id`; post-download conflict re-check | callsite (~3542) + `download` signature |
| Echo suppression | emergent: index rev+hash match → conflict-skip | same mechanism; requires upload to record `headRevisionId` + md5 + set `modifiedTime` | upload handlers, `update_index_from_*` |
| Folder rev | `"folder"` | `"folder"` (unchanged) | — |

## 4. Component-by-component design (bottom-up layering)

### 4.1 `core.py` — metadata model (foundation)
- Collapse `FileMetadata.client_modified` + `server_modified` → single
  `modified: datetime` (UTC, from Drive `modifiedTime`). Remove both old fields.
- `rev: str` now carries `headRevisionId` (files) / `"folder"` (folders).
- `content_hash: str` now MD5 hex.
- Audit and repoint every reader of `.client_modified` / `.server_modified`
  (incl. sync.py ~2651 `md.server_modified.timestamp()` → `md.modified.timestamp()`).
- `changes.py::to_file_metadata` projects the single `modified`.

### 4.2 `utils/hashing.py` — `ContentHasher` (MD5)
- New `ContentHasher` wrapping `hashlib.md5(usedforsecurity=False)` exposing the
  same `.update()/.hexdigest()/.copy()` surface as `DropboxContentHasher`, so
  `StreamHasher` and call sites change minimally.
- `StreamHasher` retargeted to wrap `ContentHasher`.
- `utils/path.py::content_hash()` returns `(md5_hex, mtime)` — same shape.
- Delete `DropboxContentHasher` (no back-compat alias; nothing ships yet).

### 4.3 Download seam — `client.download` + callsite
- Add optional `file_id: str | None = None` to `client.download`. When given,
  skip the `path_to_id` lookup and download that id's head; when absent, behave
  as today (resolve path). Stage 3/4 callers unaffected.
- sync.py ~3542 →
  `self.client.download(event.remote_path, tmp_fname, file_id=event.change_remote_id, sync_event=event)`
  (real path passed for logging/errors; fileId drives the fetch).
- Confirm `SyncEvent.change_remote_id` is populated from the Drive fileId in
  `from_metadata`.
- The vestigial `except SyncError: err.remote_path = event.remote_path` rewrite
  (was needed because the first arg was a rev-string) is cleaned up — the error
  already carries the right path.

### 4.4 Conflict resolution — `_check_download_conflict` / `_check_requires_upload`
- `_check_requires_upload`: `event.content_hash == md_old.content_hash` works
  once both are MD5. Symlink-target comparison stays (Drive has no symlinks;
  both sides `None`, clause is a no-op).
- `_check_download_conflict`: `event.rev == local_rev` and `content_hash`
  branches work as-is once rev=`headRevisionId` and hash=MD5. The
  `_ctime_newer_than_last_sync` branch is local-only, unaffected. No
  timestamp-based branch in this method needs rewriting; the only
  `server_modified` use is the folder/file check at ~2651 (handled in 4.1).
- Resolve the `move(update_rev=)` question here: add it only if a rev-checked
  move precondition is actually needed; otherwise defer.

### 4.5 Upload feedback — set `modifiedTime`, record post-upload index values
- On upload, **set** `modifiedTime` to the local file's mtime so the round-trip
  doesn't report a surprising server time. Verify whether Stage 4's `upload`
  already sets it; add if not.
- Upload handlers (`_on_local_file_modified`, etc.) write the **returned**
  `FileMetadata`'s `rev` (`headRevisionId`) + `content_hash` (md5) into the
  index via `update_index_from_remote_metadata`, so the re-observed change
  matches → emergent echo suppression.

### 4.6 Wiring + lifecycle
- Replace `from .client import GoogleDriveClient as DropboxClient  # TODO`
  (sync.py ~64) with a real `GoogleDriveClient` import; rename usages.
- `SyncEngine.__init__` takes/constructs the client with the `path_cache`.
- Wire `client.close()` into the shutdown path (Stage 3 carryover).
- Harness constructs `SyncEngine` directly (no manager).

## 5. Data flow

### 5.1 Upload cycle (local → Drive)
```
local file written
  → watchdog → list_local_changes → _clean_local_events
  → SyncEvent.from_file_system_event              [MD5 via ContentHasher]
  → apply_local_changes → _create_remote_entry → _on_local_file_modified
      → _check_requires_upload: client.get_metadata(path)
            MD5 == remote md5 ? → skip + update index   (no-op guard)
            else → client.upload(..., modifiedTime = local mtime)
      → returned FileMetadata{rev=headRevisionId, content_hash=md5, modified}
      → update_index_from_remote_metadata          [index == Drive exactly]
  → local_cursor = now
```

### 5.2 Download cycle (Drive → local)
```
download_sync_cycle
  → list_remote_changes_iterator(cursor)          [changes.py fixpoint resolve,
                                                    path-cache write-through]
  → SyncEvent.from_metadata    [change_remote_id=fileId, rev=headRevisionId, MD5]
  → apply_remote_changes → _create_local_entry → _on_remote_file
      → _check_download_conflict (vs index):
            event.rev == local_rev → LocalNewerOrIdentical → skip   ◄ own-upload echo
            MD5 == local hash      → Identical → skip
            ctime ≤ last_sync      → RemoteNewer → download
            else                   → Conflict → conflict-copy + download
      → client.download(path, tmp, file_id=change_remote_id)  [head by fileId]
      → re-check conflict post-download (TOCTOU) → move into place
      → update_index_from_sync_event
  → save remote_cursor per page                    [crash-recovery checkpoint]
```

### 5.3 Echo, traced
Harness uploads `/foo.txt` → index gets `rev=R1, md5=H1`. Next
`download_sync_cycle` pulls the change feed, which includes `/foo.txt` (Drive
reports our own write) as `SyncEvent{rev=R1}`. `_check_download_conflict` sees
`event.rev (R1) == local_rev (R1)` → `LocalNewerOrIdentical` → **skipped**. No
special-casing. This is the central correctness claim the smoke test confirms.

## 6. Error handling

Stage 5 adds essentially **no new error mapping** — Stages 3/4 built the
`HttpError → Cumulus` hierarchy via `convert_api_errors` / `errorhandling.py`.

- **Download-by-fileId 404** (file hard-deleted between change-feed read and
  download) → `NotFoundError`; the cycle records a `SyncErrorEntry` and retries.
  The rev-string `err.remote_path` rewrite becomes vestigial and is removed.
- **MD5 mismatch on download** → existing `DataCorruptionError`; retried. No change.
- **Upload conflict** → Stage 4 `upload(autorename=…)` + engine
  `_handle_upload_conflict` mirrors a server rename locally. Confirm it compiles
  against the Drive client; correctness exercised by smoke item 7. No deepening.
- **`modifiedTime` set failure** → part of the normal `files.update`/`create`
  body; fails through the existing path. No separate handling.
- **No new exception types** speculatively. If the smoke test surfaces an
  unmapped Drive code, add the single row then (Stage 3/4 pattern).

## 7. Testing

### 7.1 Offline unit tests (bulk of verification — `pytest tests/offline`)
- **`ContentHasher`** — MD5 of known vectors matches `hashlib.md5`; `StreamHasher`
  streaming == one-shot; empty, multi-chunk, non-ASCII files.
- **`core.FileMetadata`** — single `modified` field; construct from `changes.py`
  projection; no residual `client_modified`/`server_modified` references.
- **Conflict resolution** — table-driven over `_check_download_conflict`: equal
  rev → `LocalNewerOrIdentical`; equal MD5 → `Identical`; ctime ≤ last_sync →
  `RemoteNewer`; both changed → `Conflict`. Plus `_check_requires_upload`
  skip-on-equal-MD5.
- **Download seam** — `download(file_id=…)` skips `path_to_id`; path-only call
  still resolves via cache (regression).
- **Echo suppression** — simulate upload→index-update, feed the same metadata as
  a download `SyncEvent`, assert `_check_download_conflict` returns a skip verdict.
- Pre-existing failures (`test_manager.py`, `test_event_handler.py`) stay out of
  scope (Stage 6/7 per project notes).

### 7.2 Smoke test (end-to-end proof; the §7 "stage complete" bar)
Stage 4-style harness (`/tmp/stage5_smoke.py`, not committed): `GoogleDriveClient`
+ `SyncEngine` against a real local folder + real `cumulus@arko.net` Drive,
inside an isolated `/stage5-smoke-<ts>` subfolder, trashed on completion. Uses
the linked `smoke-test` config. **Do not run `pytest tests/offline` while the
smoke account is linked** (keyring-collision; see memory).

1. **Local→remote create** — write local file, `upload_sync_cycle()`, verify on
   Drive with matching md5 and index holds `headRevisionId` + md5.
2. **Remote→local create** — create on Drive, `download_sync_cycle()`, verify
   local content.
3. **Echo suppression** — immediately after #1, `download_sync_cycle()`; assert
   the uploaded file is **skipped** (no re-download, no conflict copy). *Make-or-break.*
4. **Local modify → upload** — change local file, re-upload, verify new revision
   + updated index rev.
5. **Remote modify → download** — change on Drive, download, verify local update
   (RemoteNewer path).
6. **Delete propagation** — local delete → Drive trash; Drive delete → local
   removal.
7. **Conflict** — modify both sides without syncing between, download, verify a
   conflict-copy is created and both versions survive.
8. **`modifiedTime` feedback** — after an upload, confirm a subsequent download
   cycle does not see the file as changed (no time-based feedback loop).

### 7.3 Deliverables on completion
- Spec + implementation plan committed under `docs/superpowers/`.
- Deviations / smoke-test record file (mirroring Stages 3/4).
- Memory update (`project_cumulus_port.md`).

## 8. Definition of "stage complete"
Per master plan §7: spec committed; plan committed; code merged to
`google-drive-port`; new offline tests pass and existing tests pass-or-migrated
(excluding the tracked Stage 6/7 failures); manual smoke test PASS against
`cumulus@arko.net`.

## 9. Open questions / to revisit
- **`move(update_rev=)`** — resolve during §4.4; load-bearing only if a
  rev-checked move precondition is needed, else Stage 6.
- **Echo storms despite emergent suppression** — if the smoke test shows the
  engine re-processing its own writes, add an explicit pending-write set as a
  Stage 6 follow-up (not built speculatively).
- **Cross-page change-feed ordering** — documented limitation; durable fix in
  Stage 6.

## 10. Next step
After review, invoke `superpowers:writing-plans` to draft the Stage 5
implementation plan (bottom-up task order per §4), then execute via
`subagent-driven-development`.
