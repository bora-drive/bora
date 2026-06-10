# Dropbox vs. Google Drive — Behavioral Differences

Reference catalogue of every known way Google Drive (the `cumulus` backend)
behaves differently from Dropbox (the original Maestral backend), and how the
port adapts. Compiled from the staged-port specs, deviation records, the Stage 0
validation spike, and confirmed integration findings.

This document is the canonical home for "why does Drive do X when Dropbox did
Y" questions. When a new difference is discovered, add it here.

Legend: each entry gives **Dropbox** behavior, **Drive** behavior, the **port's
adaptation**, and any **user-visible consequence**.

---

## Versioning & metadata

### 1. Revision identifiers
- **Dropbox:** globally-unique `rev` string per file version; addressable on its
  own anywhere (`rev:{rev}`).
- **Drive:** a version is addressed by the *pair* `(fileId, revisionId)`.
  `revisionId` is per-file, not globally unique. The current version is
  `headRevisionId`.
- **Port:** `IndexEntry.rev` stores `headRevisionId`. Revision-specific reads use
  `(file_id, revisionId)` (`writes._download_revision_to_tempfile`,
  `revisions().get_media`). `main.get_file_diff` signature changed from
  `(old_rev, new_rev)` to `(remote_path, old_rev, new_rev)` so it can resolve the
  file_id first.

### 2. Content hashing
- **Dropbox:** proprietary block hash (`DropboxContentHasher`).
- **Drive:** standard MD5 (`md5Checksum` field).
- **Port:** `utils/hashing.ContentHasher` is MD5. Conflict detection, upload-skip,
  and download integrity all compare MD5.

### 3. `modifiedTime` on upload
- **Dropbox:** `client_modified` parameter on upload.
- **Drive:** no per-upload client-modified parameter; server sets `modifiedTime`,
  but it can be set explicitly in the `files.create`/`files.update` body.
- **Port:** the write client sets `modifiedTime` from the local mtime on every
  create/update so modification times survive a round-trip.

### 4. Echo suppression key
- **Both:** an upload re-appears in the change feed with the new rev.
- **Port:** suppression compares `event.rev == local_rev`, and both sides must use
  `headRevisionId` (Stage 5 corrected a read-side bug that recorded a version
  counter instead, which broke suppression and caused re-downloads).

---

## Path & name semantics

### 5. Unicode normalization (NFD → NFC)
- **Dropbox:** normalized decomposed (NFD) names to composed (NFC) and mirrored
  that locally.
- **Drive:** does **not** normalize; an NFD name persists as NFD.
- **Port:** case-folding applies NFC for *key* computation, but a decomposed
  remote name is preserved as-is on disk. **Consequence:** a file saved with NFD
  bytes stays NFD (it is not silently re-composed). Tracked by
  `test_unicode_decomposed` (accepted Drive behavior; see cluster-D plan).

### 6. Remote path length
- **Dropbox:** enforced a remote path-length limit and auto-created intermediate
  parents.
- **Drive:** **no** remote path-length limit; each parent folder must already
  exist (no auto-parent-creation). Only the local filesystem imposes a length
  limit.
- **Port:** uploads resolve/create parents explicitly. `test_long_path_error` is
  reframed to assert Drive's behavior (see cluster-D plan).

### 7. Slash and special characters in names
- **Dropbox:** rejected `/`, backslash, and control characters; enforced Windows
  reserved names.
- **Drive:** accepts `/`, backslash, and most control characters as literal name
  characters (only the null byte is truly forbidden).
- **Port:** `path_codec` percent-encodes unsafe characters (`/`, controls, `%`,
  trailing space/period) with uppercase-hex when serializing a Drive name into a
  virtual path, and decodes at the local-filesystem boundary so on-disk names are
  human-readable. **Consequence:** a backslash in a name is valid on Drive, so
  tests asserting Dropbox's rejection were adjusted (greening: `test_local_path_error`).

### 8. Case-fold key computation
- **Dropbox:** server returned a precomputed `path_lower`.
- **Drive:** no precomputed lowercase key.
- **Port:** `path_codec.fold_path`/`fold_key` compute the case-fold key locally
  (`casefold()` + NFC + percent-encode). A Stage-1 bug where two different
  fold implementations disagreed on hex casing (`%C3%B3` vs `%c3%b3`) broke index
  lookups for non-ASCII names and was fixed during Stage-7a greening.

---

## File identity & structure

### 9. ID-centric vs. path-centric API
- **Dropbox:** path-centric; most calls key off the path.
- **Drive:** ID-centric; every file has an immutable opaque `fileId`, and paths
  are transient.
- **Port:** a persistent `PathIDCache` (encoded-path ↔ fileId) is the source of
  truth; path operations resolve to an ID first.

### 10. File ID immutability
- **Drive:** `fileId` is stable across rename, move, trash, and untrash; only true
  deletion removes it.
- **Port:** the path cache relies on this to re-key a row on move/rename rather
  than treating it as delete+create.

### 11. Move + rename atomicity
- **Dropbox:** a combined move+rename could surface as multiple change entries.
- **Drive:** a single `files.update(name, addParents, removeParents)` produces one
  atomic change entry.
- **Port:** `move()` is one API call / one cache update / one change entry.

### 12. Shared Drives / team namespace
- **Dropbox:** team spaces via `PathRoot` headers and separate user/team root
  types.
- **Drive:** Shared Drives are separate root namespaces, not an overlay; the port
  syncs only "My Drive."
- **Port:** the Dropbox path-root / team-namespace migration code was **removed**
  (Stage 6). Shared-Drive support is a future enhancement.

---

## Change feed

### 13. Long-poll vs. polling
- **Dropbox:** `list_folder/longpoll` returns the instant a change arrives.
- **Drive:** no long-poll; `changes.list` is a plain REST call requiring interval
  polling.
- **Port:** `wait_for_remote_changes` sleeps a fixed interval (production default
  180s, quota-driven) then checks for changes. **Consequence:** a few-seconds-to-
  minutes latency between a remote mutation and local detection; the linked test
  harness shrinks the interval to 2s and asserts via condition-polling.

### 14. Parent-before-child ordering
- **Dropbox:** change entries arrive parent-before-child within a page.
- **Drive:** **no** ordering guarantee; a child can precede its parent in the same
  page.
- **Port:** `changes.iter_remote_changes` uses a fixpoint loop (replay until a pass
  resolves nothing new) instead of a single in-order pass. A cross-page parent/
  child split is still ordering-dependent (deferred cursor-durability work, and
  related to cluster C — out-of-order local application).

### 15. Move-out-of-root semantics
- **Dropbox:** a file moved out of the sync root appears as a deletion.
- **Drive:** it appears as an ordinary change with an updated `parents` list (no
  longer containing the root); the engine must check parent scope.
- **Port:** the change classifier treats "no in-scope parent but fileId is cached"
  as a local delete (move-out), and "never cached" as out-of-scope/ignore.

### 16. Trash vs. hard-delete
- **Dropbox:** `remove` soft-deletes to trash by default.
- **Drive:** `files.delete` is a hard-delete; trashing requires
  `files.update(trashed=True)`.
- **Port:** `remove` defaults to trash to preserve recoverable semantics. The
  change classifier distinguishes `removed=true` (hard delete) from
  `trashed=true` (soft delete) — both propagate as a local deletion.

---

## Operation contracts

### 17. `get_metadata`: raises vs. returns None
- **Dropbox:** returns `None` for an unknown path.
- **Drive:** raises `NotFoundError` for a path unknown to the cache.
- **Port:** call sites that need None-on-missing wrap in `try/except
  NotFoundError`. Tests assert `NotFoundError` rather than `None`.

### 18. Precondition failure (rev mismatch)
- **Dropbox:** a write-conflict error on `update_rev` mismatch, and the **server**
  atomically created the conflicted copy on conflict.
- **Drive:** an update with a stale rev precondition raises HTTP 412
  (`conditionNotMet`) → mapped to `DataChangedError`. **Drive has no server-side
  conflicted-copy-on-conflict.**
- **Port:** the engine must detect and create conflicted copies itself
  (`generate_cc_name` / `_local_cc_filename`). The non-atomic emulation of this is
  the source of the cluster-A data-loss bug (see cluster-A plan).

### 19. Symlinks
- **Dropbox:** stored a `symlink_target` and synced symlinks.
- **Drive:** no native symlink representation.
- **Port:** `_on_local_file_modified` raises `SymlinkError`; symlinks are not
  synced.

### 20. Native Google Workspace documents
- **Dropbox:** N/A (all files are byte content).
- **Drive:** Docs/Sheets/Slides (`application/vnd.google-apps.*`) have no
  downloadable bytes and no `md5Checksum`.
- **Port:** native-doc MIME types are filtered out across the read pipeline
  (`native_docs`), so they never enter the index. **Consequence:** an account with
  native docs reports them as skipped; they are not mirrored locally.

### 21. Conflicted-copy naming
- **Dropbox:** server-side autorename (`name (1).ext`).
- **Drive:** none.
- **Port:** engine-driven `"<name> (<owner>'s conflicted copy <YYYY-MM-DD>).<ext>"`
  via `_local_cc_filename`. Tests compute the expected name dynamically rather
  than hardcoding a Dropbox string.

---

## Quota / batching

### 22. API quota
- **Drive:** a tighter per-user daily query quota than Dropbox.
- **Port:** the 180s poll interval is chosen to keep daily polling cost low;
  requests are counted via `_count_request` and a warning logs near the cap.

### 23. Batch operations
- **Dropbox:** native async batch delete / folder-create with job polling.
- **Drive:** no native batch endpoint per operation; batching uses the HTTP batch
  endpoint to group sub-requests into one round-trip, with per-slot error
  reporting.
- **Port:** `remove_batch` / `make_dir_batch` use the batch endpoint; each
  sub-request counts against quota.

---

*Last updated 2026-06-10 (Stage 7a → 7b interlude). Add new differences as they
are discovered.*
