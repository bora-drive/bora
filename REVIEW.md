# PLAN.md & TODO.md Review — Issues & Gaps

This report identifies problems, risks, and unaddressed issues in the migration plan.

---

## 1. Incomplete Phase 1 Renaming (Residual Dropbox References)

**Status:** Phase 1 is marked complete in TODO.md, but it isn't.

The codebase still has **~200 residual Dropbox SDK references** that haven't been touched:

| File | Problem |
|---|---|
| `client.py` | Still `import dropbox`, class is still `DropboxClient`, references `_DropboxSDK`, `DropboxOAuth2FlowNoRedirect`, `API_HOST` from Dropbox SDK |
| `errorhandling.py` | Still `from dropbox import async_, auth, common, exceptions, files, sharing, users` |
| `utils/hashing.py` | Still `DropboxContentHasher` class name, Dropbox block-hash algorithm |
| `sync.py` | Still `from .client import DropboxClient` (line 64), uses `DropboxClient` type |
| `manager.py` | Still references `API_HOST`, `DROPBOX_API_HOSTNAME`, checks connectivity against `api.dropboxapi.com` |
| `exceptions.py` | Backward-compat aliases (`DropboxServerError`, `DropboxConnectionError`, etc.) and `dbx_path`/`dbx_path_from` properties on `CumulusApiError` |

The TODO says Phase 1 is done, but `client.py` and `errorhandling.py` were explicitly marked for **complete rewrite** in later phases. The problem is that Phase 1 claims field renames are done while these core files remain entirely Dropbox-coupled. This creates a false sense of progress — you can't even import the package without the `dropbox` SDK installed.

**Recommendation:** Either un-check Phase 1 items that depend on Phases 5-6, or add an explicit note that Phase 1 is "complete except for files scheduled for full rewrite."

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

## 3. Symlink Support Is Silently Lost

Dropbox stores `symlink_target` in file metadata and Maestral recreates symlinks locally. Google Drive has **no symlink concept**. The sync engine (`sync.py`) still references `symlink_target` in ~10 places for conflict detection and file creation.

Neither PLAN.md nor TODO.md mentions what happens to symlinks:
- Should they be uploaded as regular files (follow the symlink)?
- Should they be skipped with a warning?
- Should the `symlink_target` field be removed from the data model?

**Recommendation:** Add a decision item to TODO.md: "Decide symlink handling policy (upload as regular file / skip / error)."

---

## 4. `rev:`-Prefixed Downloads Are Dropbox-Specific

The sync engine downloads files via `client.download(f"rev:{event.rev}", tmp_fname)` (sync.py line ~3542). This is a Dropbox-specific URI scheme for downloading a specific revision.

Google Drive downloads by file ID, not by revision. The TODO mentions implementing `download()` but doesn't call out this specific call-site pattern that needs changing in `sync.py`. Since sync.py is listed under "Significant Modifications" but the `rev:` download pattern isn't mentioned, it could be missed.

**Recommendation:** Add to Phase 8 (Sync Engine Adaptations): "Replace `rev:{event.rev}` download pattern with file-ID-based download."

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

## 9. No Migration Path for Existing Users

The TODO mentions database migration in Phase 11 but frames it as "Write migration from old schema (if supporting upgrades from Dropbox version) or fresh start." There's no decision recorded.

If there are any users of the original Maestral Dropbox client who might switch to this fork, their existing database has:
- `dbx_path_lower` as primary keys (now `remote_path_lower`)
- Dropbox content hashes (not MD5)
- Dropbox revision strings (not Google Drive versions)
- Dropbox file IDs (not Google Drive IDs)

None of this data is transferable. A fresh start is the only viable option.

**Recommendation:** Make the decision explicit: "Fresh database on first run. No migration from Dropbox schema." Remove the ambiguity.

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

## 12. Selective Sync with ID-Based Paths

The existing selective sync stores exclusions as a list of lowercased remote paths (e.g., `["/photos", "/work/old-projects"]`). With Google Drive's ID-based API:
- A folder rename on Google Drive changes the path but not the ID
- The exclusion list would become stale if a user renames an excluded folder via the Google Drive web UI
- The path cache must be consulted to resolve exclusions to IDs

The TODO has a single checkbox: "Test selective sync (excluded folders) with ID-based paths." This understates the work — it's not just testing, it's a design change.

**Recommendation:** Decide whether to store exclusions as paths (simpler, existing behavior, but fragile with renames) or as file IDs (robust, but requires migration of the exclusion format and changes to the config layer).

---

## 13. Bandwidth Throttling Implementation Couples to Dropbox SDK

The existing bandwidth throttling works by wrapping the Dropbox SDK's upload/download streams with throttled iterators. The `_DropboxSDK` subclass overrides `request_json_string()` to support streamed request bodies.

Google's `MediaFileUpload` and `MediaIoBaseDownload` have different streaming models. The TODO mentions bandwidth throttling in Phases 5d and 5e but doesn't address how to integrate it with Google's upload/download classes.

`MediaFileUpload` with `resumable=True` sends chunks in fixed sizes — throttling would need to be applied between chunks, not within them. `MediaIoBaseDownload` writes to a file-like object in chunks.

**Recommendation:** Add a design note for how throttling integrates with Google's transfer classes. The `StreamHasher` pattern may work for downloads but not for `MediaFileUpload`.

---

## 14. Missing: OAuth Client Secret Distribution Strategy

Phase 2 says "Download `client_secret.json`" and "Add to `src/maestral/resources/`". The PLAN's risk assessment mentions OAuth verification but not the fundamental distribution problem:

- Google requires that `client_secret.json` not be publicly exposed for web apps, but for "Desktop" app type, the secret is considered public
- However, if the secret is committed to a public repo, anyone can impersonate the app
- The app will be subject to Google's per-project rate limits shared across ALL users
- Google may revoke the project if usage patterns look abusive

**Recommendation:** Add a decision item: "Determine OAuth credential distribution strategy" — options include: embedded in package (simple but shared quota), user-provided (each user creates their own Google Cloud project), or server-side token exchange (complex but most robust).

---

## 15. Effort Estimate Is Likely 2-3x Too Optimistic

The PLAN estimates "~2 weeks MVP, ~4-6 weeks production." Based on the scope:

- Phase 1 alone ("1 day") has already been done and is still incomplete
- Phase 2 (Google Drive client + path cache, "3-5 days") is the hardest part and involves building a complex caching layer with no precedent in the existing codebase
- Phase 5 (adapt sync engine, "2-3 days") involves touching 230+ references in a 3900-line file with subtle concurrency and conflict-resolution logic
- No time is allocated for debugging the integration between the new path cache and the existing sync engine, which is where most bugs will live

The path cache alone — with its cache consistency, orphan handling, duplicate name resolution, atomic moves with child updates, and concurrent access patterns — is likely a 1-2 week effort to get right.

**Recommendation:** Revise the estimate. A more realistic timeline: 4-6 weeks MVP, 8-12 weeks production.

---

## Summary: Top 5 Critical Issues

| # | Issue | Severity | Action Needed |
|---|---|---|---|
| 1 | ~~Package is broken — can't import without `dropbox` SDK~~ | **Resolved** | Stubbed `client.py` and `errorhandling.py`; removed all `dropbox` imports |
| 2 | ~~Path↔ID cache design is under-specified~~ | **Resolved** | Implemented `path_cache.py` with design decisions documented |
| 3 | ~~Rate limits will block normal usage without batching~~ | **Resolved** | 180s polling, quota counter with warnings, batch endpoint notes |
| 4 | ~~Case sensitivity differences can corrupt the index~~ | **Resolved** | Enforce case-insensitive uniqueness in path cache (`set`, `move`, `populate_from_listing`) |
| 5 | ~~Google Docs filtering needed in multiple places, not just one~~ | **Resolved** | `native_docs.py` module with MIME type set + filter function; 6 filter points documented |
