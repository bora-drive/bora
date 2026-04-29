# PLAN.md & TODO.md Review — Issues & Gaps

This report identifies problems, risks, and unaddressed issues in the migration plan.

---

## 1. ~~Incomplete Phase 1 Renaming (Residual Dropbox References)~~ (RESOLVED)

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

## 2. ~~No `path_cache.py` Module Exists Yet — But Everything Depends On It~~ (RESOLVED)

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

## 3. ~~Symlink Support Is Silently Lost~~ (RESOLVED — decision recorded)

**Decision:** Follow symlinks on upload (upload the target as a regular file). Remote metadata will never contain `symlink_target` since Google Drive has no symlink concept. The `symlink_target` field remains in `IndexEntry` but will always be `None` for Google Drive entries.

**Phase 8 actions needed:**
- `sync.py` lines ~3526–3532: remove the symlink recreation code path (Google Drive will never provide `symlink_target` metadata)
- Upload path: `os.path.realpath()` is already used for local paths, which follows symlinks — no change needed
- `IndexEntry.symlink_target` column: keep in schema (harmless), always store `None`
- `models.py` `FileMetadata.symlink_target`: always `None` in Google Drive metadata mapping

---

## 4. ~~`rev:`-Prefixed Downloads Are Dropbox-Specific~~ (RESOLVED — documented)

**Decision:** Replace `rev:{event.rev}` with file-ID-based download in Phase 8.
TODO comment added at `sync.py` line ~3542 marking the exact callsite.
`event.change_remote_id` (the Google Drive file ID) should be used instead.

---

## 5. ~~Rate Limits Are Severely Underestimated~~ (RESOLVED)

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

## 6. ~~Connection Monitoring Still Points at Dropbox~~ (RESOLVED)

**Already fixed in commit 07da2e2** (Issue 3). `manager.py` now uses `GOOGLE_API_HOSTNAME` = `API_HEALTH_URL` = `"https://www.googleapis.com"` for connectivity checks. Unauthenticated HEAD request — no credentials needed.
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

## 9. ~~No Migration Path for Existing Users~~ (RESOLVED — decision recorded)

**Decision:** Fresh database on first run. No migration from Dropbox schema.

Existing Maestral/Dropbox databases contain Dropbox-specific content hashes, revision strings, file IDs, and path formats. None of this data is transferable to Google Drive. On first launch, Cumulus creates a new database and performs a full sync.
---

## 10. ~~Test Suite Still References Dropbox SDK~~ (RESOLVED — decision recorded)

**Decision:** Tests will be rewritten in Phase 12, not ported.

- Mocking: use `unittest.mock` to mock `googleapiclient` calls
- Path cache tests: add as a Phase 4 deliverable (unit tests for `PathIDCache` and `native_docs`)
- Integration tests: require a test Google account; setup instructions will be added to `tests/README.md`
- The 12 files that `import dropbox` are dead code until Phase 12
---

## 11. ~~`dropbox` SDK Is Still a Runtime Dependency~~ (RESOLVED)

**Already fixed in commit 74c8aea** (Issue 1). `client.py` and `errorhandling.py` were stubbed to remove all `dropbox` SDK imports. The package can now be imported without the `dropbox` SDK installed.
---

## 12. ~~Selective Sync with ID-Based Paths~~ (RESOLVED — decision recorded)

**Decision:** Keep path-based exclusions for MVP. When a rename comes through the change feed and the old path matches an exclusion, update the exclusion list entry to the new path. This keeps the config layer unchanged and handles the most common case (user renames an excluded folder in the web UI).

A future version may switch to ID-based exclusions for full robustness, but that requires config format migration and UI changes.
---

## 13. ~~Bandwidth Throttling Implementation Couples to Dropbox SDK~~ (RESOLVED — decision recorded)

**Decision:** Throttle between chunks, not within them.

- **Downloads:** `MediaIoBaseDownload` writes in configurable chunk sizes. Insert a `time.sleep()` delay between chunks to throttle. Wrap the destination file object with a rate-limited writer.
- **Uploads:** `MediaFileUpload(resumable=True)` sends in chunks. Insert a `time.sleep()` between `next_chunk()` calls. The existing `bandwidth_limit_up`/`bandwidth_limit_down` config values translate directly to bytes-per-second limits.
- The `StreamHasher` pattern from the Dropbox client can be adapted for downloads but is not needed for uploads (Google computes checksums server-side).
- Implementation deferred to Phase 5d/5e when the actual transfer methods are built.
---

## 14. ~~Missing: OAuth Client Secret Distribution Strategy~~ (RESOLVED — decision recorded)

**Decision:** Embed credentials for "Desktop" app type (OAuth client type). This is Google's recommended approach for installed/CLI applications — the client secret is considered public for this app type.

- `client_secret.json` committed to `src/cumulus/resources/` (Desktop app type)
- Each user authenticates via browser-based OAuth consent flow
- Per-project quota (12k queries/day) is shared across all users
- If quota becomes a problem, users can provide their own Google Cloud project credentials via a config option (future enhancement)
- Google API Console project should be configured with reasonable rate-limit alerts
---

## 15. ~~Effort Estimate Is Likely 2-3x Too Optimistic~~ (RESOLVED — estimate revised)

**Revised estimate:** 4–6 weeks MVP, 8–12 weeks production-ready.

Key adjustments:
- Phase 2 (path cache): 1–2 weeks, not "3–5 days" — this is the hardest new component
- Phase 5 (client implementation): 1–2 weeks for full API integration + testing
- Phase 8 (sync engine): 1–2 weeks — 230+ references to update with subtle concurrency concerns
- Integration debugging: 1–2 weeks — path cache ↔ sync engine interaction is where most bugs will surface
- Buffer: 1 week for unforeseen issues
---

## Summary: All 15 Issues

| # | Issue | Status | Resolution |
|---|---|---|---|
| 1 | ~~Package is broken — can't import without `dropbox` SDK~~ | **Resolved** | Stubbed `client.py` and `errorhandling.py`; commit 74c8aea |
| 2 | ~~Path↔ID cache design is under-specified~~ | **Resolved** | Implemented `path_cache.py`; commit 53a97e7 |
| 3 | ~~Rate limits will block normal usage without batching~~ | **Resolved** | 180s polling, quota counter; commit 07da2e2 |
| 4 | ~~Case sensitivity differences can corrupt the index~~ | **Resolved** | Case-insensitive uniqueness in path cache; commit 76577f4 |
| 5 | ~~Google Docs filtering needed in multiple places~~ | **Resolved** | `native_docs.py` module with filter function; commit 3484cc3 |
| 6 | ~~Connection monitoring still points at Dropbox~~ | **Resolved** | Fixed in commit 07da2e2 (`API_HEALTH_URL`) |
| 7 | ~~Case sensitivity mismatch~~ | **Resolved** | Same as Issue 4 |
| 8 | ~~Google Docs handling deferred but pervasive~~ | **Resolved** | Same as Issue 5 |
| 9 | ~~No migration path for existing users~~ | **Resolved** | Decision: fresh database, no migration |
| 10 | ~~Test suite references Dropbox SDK~~ | **Resolved** | Decision: rewrite in Phase 12, not port |
| 11 | ~~`dropbox` SDK still a runtime dependency~~ | **Resolved** | Fixed in commit 74c8aea |
| 12 | ~~Selective sync with ID-based paths~~ | **Resolved** | Decision: keep path-based for MVP, update on rename |
| 13 | ~~Bandwidth throttling couples to Dropbox SDK~~ | **Resolved** | Decision: throttle between chunks |
| 14 | ~~OAuth client secret distribution~~ | **Resolved** | Decision: embed Desktop app type credentials |
| 15 | ~~Effort estimate too optimistic~~ | **Resolved** | Revised to 4–6 weeks MVP, 8–12 weeks production |
