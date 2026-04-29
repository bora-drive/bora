# PLAN.md & TODO.md Review — Issues & Gaps

This report identifies problems, risks, and unaddressed issues in the migration plan.

---

## 1. ~~Incomplete Phase 1 Renaming (Residual Dropbox References)~~ (RESOLVED)

`client.py` and `errorhandling.py` were stubbed in commit 74c8aea (removing all `import dropbox` statements). Remaining Dropbox *naming* (~230 refs in sync.py, `DropboxContentHasher` in hashing.py, backward-compat aliases in exceptions.py) will be cleaned up in later phases.

A note has been added to TODO.md Phase 1 acknowledging this explicitly.

---

## 2. No `path_cache.py` Module Exists Yet — But Everything Depends On It

The PLAN correctly identifies the path↔ID cache as the **hardest change** (Section 3.1), but neither document addresses several critical design questions:

### 2a. Cache Consistency Under Concurrent Access
The sync engine runs upload and download in separate threads (serialized by `sync_lock`). The path cache will be mutated by both the change-feed processor (downloads) and the upload path (which needs to resolve local paths to IDs). The plan doesn't discuss:
- Whether the cache needs its own lock separate from `sync_lock`
- What happens if a file is moved remotely between path resolution and the actual API call
- How to handle the cache becoming stale mid-sync-cycle

### 2b. Initial Population Is Expensive
Google Drive's `files.list` with `q='<parentId> in parents'` is folder-by-folder. A recursive listing of a large Drive requires either:
- Many sequential API calls (slow, rate-limit prone)
- A single `files.list` with no parent filter + post-processing (returns everything)

The plan mentions `populate_from_listing(items)` but doesn't discuss the strategy for initial population or its cost against the 12k queries/day rate limit.

### 2c. Orphaned Files / Broken Parent Chains
Google Drive can have files whose parent folders have been trashed or where the user doesn't have access to the parent. `resolve_path(file_id)` walking the parent chain will fail. Neither document addresses this.

**Recommendation:** Add a design section for the path cache covering: concurrency model, initial population strategy, orphan handling, and cache invalidation/rebuild triggers.

---

## 3. ~~Symlink Support Is Silently Lost~~ (RESOLVED)

**Decision:** Skip symlinks during upload with a logged warning. Google Drive has no symlink concept, so symlink metadata will never arrive from the remote side.

- PLAN.md risk table updated with symlink row
- TODO.md Phase 8 updated with three specific tasks (skip on upload, remove recreation code, keep schema field)
- TODO.md Known Limitations updated

---

## 4. ~~`rev:`-Prefixed Downloads Are Dropbox-Specific~~ (RESOLVED)

**Decision:** Replace `f"rev:{event.rev}"` with `event.change_remote_id` (the Google Drive file ID) at sync.py ~line 3542. The client's `download()` will take a file ID and call `files.get?alt=media`.

- TODO.md Phase 8: added explicit task for this callsite

---

## 5. Rate Limits Are Severely Underestimated

The PLAN says Google Drive has "12k queries/day default per-user" and rates this as **Medium** risk. This is actually a **High** risk:

- 12,000 queries/day = ~8.3 queries/minute = 1 query every ~7 seconds
- A single sync cycle listing a folder with 1000 files takes ~10 paginated requests
- Each file upload requires: resolve parent ID (1 query) + upload (1 query) + update cache (possible additional query) = 2-3 queries per file
- Initial sync of a Drive with 10,000 files could consume the entire daily quota
- The 30-second polling interval for change detection = ~2,880 queries/day just for polling

The PLAN mentions batch API calls as "out of scope for MVP" but doesn't quantify the impact. Without batching, even moderate usage will hit rate limits.

**Recommendation:** 
- Elevate rate limits to **High** severity
- Add a TODO item: "Implement request budgeting/counting to warn users before hitting limits"
- Consider making the polling interval configurable (e.g., 60s or 120s default)
- Prioritize batch API support earlier than "later optimization"

---

## 6. Connection Monitoring Still Points at Dropbox

`manager.py` checks connectivity by pinging `https://api.dropboxapi.com` (via `DROPBOX_API_HOSTNAME`). This is used for:
- Auto-reconnection after network outages
- Connection status reporting to the UI

Neither TODO.md Phase 10 item "Update `_connection_helper` to check Google Drive connectivity" is specific about what URL to check. Google's Drive API endpoint is `https://www.googleapis.com/drive/v3/`, but a simple connectivity check might be better aimed at `https://www.googleapis.com` or `https://accounts.google.com`.

**Recommendation:** Add specificity to the TODO: decide the health-check endpoint and whether it should be authenticated or unauthenticated.

---

## 7. ~~Case Sensitivity Mismatch Not Fully Addressed~~ (RESOLVED)

**Decision:** Enforce Dropbox-like case-insensitivity. If two files differ only by case in the same folder, the first one wins and the second is skipped with a warning.

**Implementation (path_cache.py):**
- `has_case_conflict(remote_path, file_id)` — checks for an existing entry at the same `remote_path_lower` with a different `file_id`.
- `set()` returns `False` and logs a warning if a case conflict is detected.
- `move()` returns `False` and logs a warning if the destination case-conflicts.
- `populate_from_listing()` already detected duplicates via `(parent_id, name.lower())`; this is consistent with the new policy.
- `IndexEntry` keeps `remote_path_lower` as PK — case-conflicting files are simply never admitted to the cache or index.

---

## 8. ~~Google Docs/Sheets/Slides Handling Is Deferred But Pervasive~~ (RESOLVED)

**Decision:** Skip all Google-native documents (Docs, Sheets, Slides, etc.) at the API client layer so the sync engine never sees them.

**Implementation:**
- New module `src/cumulus/native_docs.py` defines:
  - `GOOGLE_NATIVE_MIME_TYPES` — frozenset of all non-downloadable MIME types
  - `is_google_native(mime_type)` — returns True for Docs/Sheets/Slides/etc.
  - `filter_native_docs(items)` — strips native docs from a list of Drive API dicts
- Six filter points documented in the module docstring:
  1. `list_folder_iterator` / `list_folder` — strip from results
  2. `list_remote_changes_iterator` — strip from change entries
  3. `get_metadata` — return None for native docs
  4. `download` — raise error if somehow reached
  5. `populate_from_listing` — excluded from cache
  6. `_apply_remote_changes` — secondary guard
- Client stub docstrings annotated with filter-point notes
- `path_cache.py` updated to use `GOOGLE_FOLDER_MIME_TYPE` constant instead of hardcoded string

---

## 9. ~~No Migration Path for Existing Users~~ (RESOLVED)

**Decision:** Fresh database on first run. No migration from Dropbox schema. Dropbox content hashes, revision strings, and file IDs are not transferable.

- TODO.md Phase 11: replaced ambiguous migration checkbox with explicit fresh-start decision
- PLAN.md §8: added risk row confirming no migration needed

---

## 10. Test Suite Still References Dropbox SDK

The test suite has **12 files** that import from the `dropbox` SDK and **31 test files** total. Phase 12 of TODO.md lists porting tests, but:
- The tests can't even be *imported* until `client.py` and `errorhandling.py` are rewritten
- No test strategy is defined for the path cache (the most complex new component)
- Integration tests require a test Google account — no setup instructions exist
- The TODO doesn't mention mocking strategy for Google API calls

**Recommendation:** Add to Phase 12: "Define mocking approach for `googleapiclient` (use `unittest.mock` or `google-api-python-client`'s built-in `HttpMock`)." Add path cache testing as a Phase 4 deliverable, not Phase 12.

---

## 11. `dropbox` SDK Is Still a Runtime Dependency

The `pyproject.toml` has been updated to remove the `dropbox` dependency and add Google libraries. But `client.py` and `errorhandling.py` still `import dropbox`. The package **cannot be installed or run** without the `dropbox` SDK, despite it being removed from dependencies.

This means the project is currently in a **broken intermediate state** where:
- `pip install` won't pull in `dropbox`
- But importing `cumulus.client` will fail with `ModuleNotFoundError`

**Recommendation:** Either add `dropbox` back as a temporary dependency until Phase 5, or stub out `client.py` and `errorhandling.py` with placeholder implementations that don't import from `dropbox`.

---

## 12. ~~Selective Sync with ID-Based Paths~~ (RESOLVED)

**Decision:** Keep path-based exclusions. When a folder rename arrives via the change feed and the old path matches an exclusion entry, update the exclusion to the new path. Known limitation: if the client is offline during a rename, the exclusion may go stale (the change feed shows only the new path). Users can fix this by re-excluding the folder under its new name.

- TODO.md Phase 8: replaced vague "test selective sync" checkbox with specific tasks
- PLAN.md §8: added risk row for stale exclusions

---

## 13. ~~Bandwidth Throttling Implementation Couples to Dropbox SDK~~ (RESOLVED)

**Decision:** Sleep between chunks. Both `MediaFileUpload` and `MediaIoBaseDownload` use a `next_chunk()` loop. After each chunk, compute how long the transfer *should* have taken at the configured bytes/sec rate and sleep the difference. Coarse-grained (chunk-size granularity) but simple and sufficient.

- TODO.md Phases 5d/5e: updated throttling tasks with the chunk-sleep approach
- PLAN.md §3.5: added throttling design note

---

## 14. Missing: OAuth Client Secret Distribution Strategy

Phase 2 says "Download `client_secret.json`" and "Add to `src/maestral/resources/`". The PLAN's risk assessment mentions OAuth verification but not the fundamental distribution problem:

- Google requires that `client_secret.json` not be publicly exposed for web apps, but for "Desktop" app type, the secret is considered public
- However, if the secret is committed to a public repo, anyone can impersonate the app
- The app will be subject to Google's per-project rate limits shared across ALL users
- Google may revoke the project if usage patterns look abusive

**Recommendation:** Add a decision item: "Determine OAuth credential distribution strategy" — options include: embedded in package (simple but shared quota), user-provided (each user creates their own Google Cloud project), or server-side token exchange (complex but most robust).

---

## 15. ~~Effort Estimate Is Likely 2-3x Too Optimistic~~ (RESOLVED)

**Revised estimate:** 4–6 weeks MVP, 8–12 weeks production.

PLAN.md §9 updated with revised per-phase estimates. Key changes: path cache bumped to 1–2 weeks, sync engine to 1–2 weeks, added 1–2 week integration debugging phase.

---

## Summary: Top 5 Critical Issues

| # | Issue | Severity | Action Needed |
|---|---|---|---|
| 1 | ~~Package is broken — can't import without `dropbox` SDK~~ | **Resolved** | Stubbed `client.py` and `errorhandling.py`; removed all `dropbox` imports |
| 2 | ~~Path↔ID cache design is under-specified~~ | **Resolved** | Implemented `path_cache.py` with design decisions documented |
| 3 | ~~Rate limits will block normal usage without batching~~ | **Resolved** | 180s polling, quota counter with warnings, batch endpoint notes |
| 4 | ~~Case sensitivity differences can corrupt the index~~ | **Resolved** | Enforce case-insensitive uniqueness in path cache (`set`, `move`, `populate_from_listing`) |
| 5 | ~~Google Docs filtering needed in multiple places, not just one~~ | **Resolved** | `native_docs.py` module with MIME type set + filter function; 6 filter points documented |
