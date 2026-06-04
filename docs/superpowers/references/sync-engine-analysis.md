# Maestral Sync Engine — Detailed Analysis

## 1. How the Sync Loop Works (Upload/Download Cycle)

### Architecture Overview

Three layers orchestrate syncing:

- **`Maestral`** (main.py) — Public API facade. Creates `DropboxClient`, `SyncEngine`, and `SyncManager`. Exposes user-facing methods like `start_sync()`, `stop_sync()`, `exclude_items()`, `include_items()`.
- **`SyncManager`** (manager.py) — Thread coordinator. Spawns and manages the actual worker threads, monitors connectivity, handles startup sequencing.
- **`SyncEngine`** (sync.py) — Core sync logic. Contains all conflict resolution, file hashing, index management, event processing, and the actual upload/download implementations.

### Thread Model (SyncManager)

`SyncManager.start()` spawns up to 5 threads:

| Thread | Name | Method | Purpose |
|--------|------|--------|---------|
| Startup | `maestral-sync-startup` | `startup_worker()` | One-shot: retries failed downloads, resumes interrupted downloads, runs initial `download_sync_cycle()`, then runs `upload_local_changes_while_inactive()` to catch up on changes that happened while stopped. Sets `startup_completed` event when done. |
| Download | `maestral-download` | `download_worker()` | Long-running loop: calls `wait_for_remote_changes()` (long-poll), then `download_sync_cycle()` on changes. Checks for path root updates (team namespace changes) before applying changes. |
| Download (added items) | `maestral-folder-download` | `download_worker_added_item()` | Drains the `download_queue` (PersistentQueue) of paths newly included via selective sync. Calls `get_remote_item()` for each. |
| Upload | `maestral-upload` | `upload_worker()` | Long-running loop: calls `wait_for_local_changes()` (blocks on watchdog queue), then `upload_sync_cycle()`. |
| Connection monitor | `maestral-connection-helper` | `connection_monitor()` | Perpetual: checks connectivity to `api.dropboxapi.com` every 10s. Auto-starts sync on reconnection if `autostart` is set. |
| FS Observer | `maestral-fsobserver` / `maestral-fsemitter` | (watchdog) | Watches local Dropbox folder recursively for file system events. |

Upload and download run in **separate threads** but are serialized by `sync_lock` (an `RLock`) — they alternate, they don't run concurrently. Within each cycle, individual file operations are parallelized via `ThreadPoolExecutor` with `NUM_THREADS = min(24, CPU_CORE_COUNT)` workers.

### Upload Cycle (Local → Remote)

**Entry point:** `SyncEngine.upload_sync_cycle()`

1. **`list_local_changes(delay=1)`** — Drains `FSEventHandler.local_file_event_queue` until idle for 1 second. Returns raw watchdog events.
2. **`_clean_local_events(events)`** — Deduplicates events per path (keeps one representative event). Splits move events into delete+create, recombines if both sides survive deduplication. Collapses children of moved/deleted directories into the parent event. Handles type changes (file→folder, folder→file) as delete+create pairs.
3. **`_sync_events_from_fs_events(events)`** — Converts watchdog `FileSystemEvent` objects to `SyncEvent` objects in a thread pool (parallelizes content hashing via `SyncEvent.from_file_system_event()`).
4. **`apply_local_changes(sync_events)`** — Categorizes events into `deleted`, `dir_moved`, and `other` (by hierarchy level). Filters out excluded/mignored items. Applies in order:
   - Deletions first (parallel via `_create_remote_entry`)
   - Directory moves second (sequential — must be top-down)
   - Other events by hierarchy level (parallel within each level)
5. **`_create_remote_entry(event)`** — Dispatches to one of:
   - **`_on_local_file_modified(event)`** — For file adds/modifications. Checks `_check_requires_upload()` (compares content hash with remote). Uses `WriteMode.Add` for new files, `WriteMode.Update` for modifications (with rev matching). Calls `client.upload()`. Handles `_handle_upload_conflict()` if Dropbox auto-renamed.
   - **`_on_local_folder_created(event)`** — Calls `client.make_dir()` or `client.share_dir()` for top-level team folders.
   - **`_on_local_moved(event)`** — Calls `client.move()`. Falls back to rescan if source doesn't exist on Dropbox. Removes destination file first if it would collide (with rev check).
   - **`_on_local_deleted(event)`** — Checks remote metadata for conflicts (e.g., remote was modified since last sync). Calls `client.remove()` with `parent_rev` for files (optimistic concurrency).
6. Updates `local_cursor` to current timestamp.

**Startup catch-up:** `upload_local_changes_while_inactive()` does a full directory scan via `_get_local_changes_while_inactive()`, comparing the file system against the index to detect adds (in snapshot but not index), deletes (in index but not snapshot), and modifications (`mtime > last_sync`).

### Download Cycle (Remote → Local)

**Entry point:** `SyncEngine.download_sync_cycle()`

1. If `remote_cursor == ""` (first sync), begins full indexing from `/`. Otherwise calls `list_remote_changes_iterator(last_cursor)` for incremental changes.
2. **`list_remote_changes_iterator(last_cursor)`** — Calls `client.list_folder_iterator("/", recursive=True)` for initial sync, or `client.list_remote_changes_iterator(last_cursor)` for incremental. For each page:
   - `_clean_remote_changes()` — Deduplicates to one event per path. Handles type changes (e.g., folder→file) by emitting a synthetic `DeletedMetadata` before the new metadata.
   - Converts `Metadata` objects to `SyncEvent` via `SyncEvent.from_metadata()`.
   - Yields `(sync_events, cursor)` tuples.
3. **`apply_remote_changes(sync_events)`** — Categorizes into `deleted`, `folders` (by level), and `files`. Filters excluded items. Removes deleted items from exclusion list. Applies in order:
   - Deletions (parallel)
   - Folders by hierarchy level (top-down, parallel within level)
   - Files (parallel)
4. **`_create_local_entry(event)`** — Dispatches to one of:
   - **`_on_remote_file(event)`** — Calls `_check_download_conflict()`. Downloads to temp file via `client.download(f"rev:{event.rev}", tmp_fname)`. Re-checks conflict after download. Moves temp file to final location. Preserves permissions if the Dropbox ID matches (same file, content update).
   - **`_on_remote_folder(event)`** — Conflict check, then `os.mkdir()`. Ensures parents exist first via `_ensure_parent()`.
   - **`_on_remote_deleted(event)`** — Conflict check, then `delete()` locally.
   - All three call `_apply_case_change()` first to handle casing renames.
5. Saves `remote_cursor` after each page of changes (incremental save for crash recovery).
6. Tracks indexing progress via `sync.indexing_counter` state for resume support.

### Conflict Resolution

**Download conflicts** (`_check_download_conflict`) use a multi-step algorithm returning one of `{Identical, LocalNewerOrIdentical, RemoteNewer, Conflict}`:
1. If `remote.rev == local_rev` in index → local is same or newer, skip download.
2. If `content_hash` matches AND `symlink_target` matches → Identical, update index only.
3. If there are unresolved upload errors for this path → Conflict.
4. If local ctime ≤ last_sync time (recursively for folders) → RemoteNewer, safe to overwrite.
5. If remote is a deletion but local has changes → LocalNewerOrIdentical.
6. Otherwise → Conflict.

On Conflict: the local file is renamed to `"<name> (<user>'s conflicted copy <date>).<ext>"` and the remote version is downloaded. The renamed file is rescanned for upload.

**Upload conflicts** are handled server-side by Dropbox's `autorename=True`. If Dropbox renames the uploaded file, `_handle_upload_conflict()` mirrors the rename locally.

### Event Flow Summary

```
Local FS changes → watchdog Observer → FSEventHandler.on_any_event()
  → filters ignored events (self-caused) → local_file_event_queue
  → upload_worker drains queue → upload_sync_cycle()
    → list_local_changes() → _clean_local_events() → _sync_events_from_fs_events()
    → apply_local_changes() → _create_remote_entry() → client.upload/move/remove/make_dir
    → update_index_from_dbx_metadata()

Remote changes → download_worker → wait_for_remote_changes() (long-poll)
  → download_sync_cycle()
    → list_remote_changes_iterator() → _clean_remote_changes()
    → apply_remote_changes() → _create_local_entry() → client.download / os.mkdir / delete
    → update_index_from_sync_event()
```

---

## 2. Dropbox-Specific Concepts Embedded in the Sync Logic

### 2.1 Cursors (Opaque Pagination/Change Tokens)

- **`remote_cursor`** (string): Stored in `MaestralState` as `state.sync.cursor`. Represents the Dropbox server's state at a point in time. Used by:
  - `client.wait_for_remote_changes(last_cursor)` — Long-poll endpoint (`files/list_folder/longpoll`).
  - `client.list_remote_changes_iterator(last_cursor)` — `files/list_folder/continue`.
  - `client.list_folder_iterator()` — Returns cursors in `ListFolderResult.cursor` for pagination.
  - Empty string `""` means "never synced" — triggers full indexing.
- **`local_cursor`** (float timestamp): Stored as `state.sync.lastsync`. Tracks the last time local changes were uploaded. Used to detect changes while offline by comparing file mtimes.

**Dropbox-specificity:** The cursor is an opaque server-side token. Other providers (Google Drive, OneDrive) have their own change token systems with different semantics. The cursor encodes both pagination state AND change detection state — Maestral exploits this by saving cursors mid-pagination for crash recovery.

### 2.2 Content Hashing (Dropbox's Block-Hash Algorithm)

`DropboxContentHasher` (utils/hashing.py) implements Dropbox's proprietary content hash:
- File is split into 4 MB blocks.
- Each block is SHA-256 hashed.
- Block hashes are concatenated and SHA-256 hashed again.
- Result is a hex string matching `FileMetadata.content_hash`.

Used pervasively:
- `SyncEngine.get_local_hash()` — Computes and caches local file hashes.
- `_check_requires_upload()` — Compares local hash with `md_old.content_hash` to skip unnecessary uploads.
- `_check_download_conflict()` — Compares `event.content_hash` with local hash for identical-content detection.
- `IndexEntry.content_hash` — Stored in SQLite index.
- `HashCacheEntry` — Caches hash by inode+mtime to avoid recomputation.

The hash is computed during `SyncEvent.from_file_system_event()` in a thread pool.

### 2.3 Revisions (`rev`)

- Each `FileMetadata` has a unique `rev` string identifying a specific version.
- Stored in `IndexEntry.rev` (the index tracks last-synced revision).
- Used for **optimistic concurrency control**:
  - `client.upload(..., write_mode=WriteMode.Update, update_rev=local_rev)` — Upload only succeeds if the remote rev matches (server rejects if file changed since last sync).
  - `client.remove(path, parent_rev=local_rev)` — Delete only succeeds if rev matches.
- Folders use the sentinel value `"folder"` for rev.
- `client.download(f"rev:{event.rev}", ...)` — Downloads a specific revision.

### 2.4 Namespaces and Path Roots (Team Spaces)

- Dropbox Teams use "root namespaces" — the `path_root` header determines what `/` means in API calls.
- `DropboxClient.update_path_root(root_info)` — Sets the SDK's `PathRoot` to the team's root namespace.
- `UserRootInfo` vs `TeamRootInfo` — Team users have a `home_path` (e.g., `/username`) within the team root.
- `SyncManager.check_and_update_path_root()` / `_update_path_root()` — Detects when a user joins/leaves a team, migrates local folder structure (moves personal files into/out of a home subfolder), migrates excluded items list, triggers reindex.
- `client.is_team_space` — Used in `_on_local_folder_created()` to call `client.share_dir()` instead of `client.make_dir()` for top-level folders in a team space.

### 2.5 Dropbox Path Semantics

- **`path_lower`** — Dropbox's canonical normalized path (lowercase + unicode normalization). Used as the **primary key** in the index.
- **`path_display`** — Cased path for display (basename is guaranteed correctly cased by the API, but ancestors may not be).
- `SyncEngine.correct_case()` — Reconstructs fully-cased paths by walking ancestors (cache → index → server query). This is necessary because the Dropbox API only guarantees correct casing for the basename.
- `normalize()` (from utils/path) — Converts to Dropbox's lowercase normalization.
- Case-insensitivity handling: Dropbox paths are inherently case-insensitive. Maestral detects the local FS case sensitivity and handles case conflicts (files that differ only in case) by renaming with "case conflict" suffix.

### 2.6 `dbx_id` (Dropbox File/Folder ID)

A permanent unique identifier for a file or folder that survives renames/moves:
- Stored in `IndexEntry.dbx_id` and `SyncEvent.dbx_id`.
- Used in `_on_remote_file()` to determine if a downloaded file is an update to the same file (preserves local permissions and xattrs if `event.dbx_id == old_entry.dbx_id`).

### 2.7 `modified_by` / `change_dbid` (Dropbox Account IDs)

Used for:
- Desktop notifications identifying who changed a file.
- Conflict copy naming: `"<User>'s conflicted copy <date>"`.
- `_display_name_for_account(dbid)` — Resolves Dropbox account IDs to display names.

### 2.8 Dropbox API Patterns

- **Long-polling**: `files/list_folder/longpoll` blocks up to `timeout + 90s jitter`. The download worker uses this instead of polling.
- **Autorename**: `client.upload(..., autorename=True)` / `client.make_dir(..., autorename=True)` — Dropbox renames to avoid conflicts.
- **WriteMode**: `Add` / `Update` / `Overwrite` — Maps directly to Dropbox's `files.WriteMode`.
- **Upload sessions**: Large files use chunked upload sessions (`_upload_session_start_helper`, `_upload_session_append_helper`, `_upload_session_finish_helper`).
- **Symlink support**: `FileMetadata.symlink_target` — Dropbox stores symlink targets. Maestral recreates symlinks locally rather than downloading.
- **`include_deleted` / `include_mounted_folders` / `include_non_downloadable_files`**: Dropbox-specific listing options.
- **Backoff handling**: `wait_for_remote_changes()` respects Dropbox's `backoff` response field.

---

## 3. How State is Tracked

### 3.1 SQLite Database (`{config_name}.db`)

Path: `~/.local/share/maestral/{config_name}.db`

Four tables managed via ORM (`database.orm.Manager`):

| Table | Model | Primary Key | Purpose |
|-------|-------|-------------|----------|
| `'index'` | `IndexEntry` | `dbx_path_lower` | **Sync index**: tracks every synced file/folder with its cased path, dbx_id, item_type, last_sync time, rev, content_hash, symlink_target. The single source of truth for "what we think the synced state is." |
| `history` | `SyncEvent` | `id` (auto-int) | **Sync history**: records of completed sync events. Capped at 1000 entries, pruned by `keep_history` config (default 2 weeks). |
| `hash_cache` | `HashCacheEntry` | `inode` | **Content hash cache**: maps inode → (local_path, hash, mtime). Avoids recomputing hashes when mtime hasn't changed. |
| `sync_errors` | `SyncErrorEntry` | `dbx_path_lower` | **Persistent sync errors**: failed syncs stored for automatic retry on next startup. |

Database access is serialized by `_db_lock` (RLock). All access goes through `_database_access()` context manager which catches and converts SQLite exceptions.

### 3.2 Config File (`MaestralConfig`)

Persistent configuration (survives resets):
- `sync.path` — Local Dropbox folder path.
- `sync.excluded_items` — Selective sync exclusion list (list of lowercased Dropbox paths).
- `sync.max_cpu_percent` — CPU throttling.
- `sync.keep_history` — How long to keep sync history.
- `sync.upload` / `sync.download` — Enable/disable upload/download independently.
- `app.max_parallel_uploads` — Parallelism limits.
- `app.bandwidth_limit_up` / `bandwidth_limit_down` — Bandwidth throttling.

### 3.3 State File (`MaestralState`)

Volatile state that can be reset:
- `sync.cursor` — **Remote cursor** (Dropbox's opaque change token).
- `sync.lastsync` — **Local cursor** (timestamp of last upload sync).
- `sync.last_reindex` — Timestamp of last full reindex.
- `sync.did_finish_indexing` — Whether initial indexing completed (for resume).
- `sync.indexing_counter` — Number of items indexed so far (for resume).
- `sync.pending_downloads` — Persistent queue of paths to download (for selective sync includes).
- `account.path_root_type` — `"user"` or `"team"`.
- `account.path_root_nsid` — Root namespace ID.
- `account.home_path` — User's home path in team space.

### 3.4 In-Memory Caches

- **`_case_conversion_cache`** (LRUCache, capacity 5000): Maps `dbx_path_lower` → correctly-cased path. Cleared after each sync cycle.
- **`FSEventHandler._ignored_events`**: Set of events to suppress (self-caused events during downloads). TTL-based expiry.
- **`ActivityTree`**: Sparse tree tracking in-progress sync events. Used by the UI to show file status.

### 3.5 State Reset

`reset_sync_state()` clears all 4 DB tables, resets sync state to defaults (cursor="", lastsync=0), triggers full reindexing on next start.

---

## 4. What Would Need to Change for a Different Cloud Backend

### 4.1 The `DropboxClient` — Must Be Replaced Entirely

The `DropboxClient` class (client.py) is a thin wrapper around the Dropbox Python SDK. A new backend needs an equivalent class implementing these operations:

| Method | What it does | Abstraction needed |
|--------|-------------|--------------------|
| `upload(local_path, dbx_path, write_mode, update_rev, autorename)` | Upload with concurrency control | Conditional upload (if-match etag/rev) |
| `download(rev_or_path, local_path)` | Download specific version | Download by version ID |
| `move(from_path, to_path, autorename)` | Server-side move | Move/rename |
| `remove(path, parent_rev)` | Conditional delete | Conditional delete |
| `make_dir(path, autorename)` | Create folder | Create folder |
| `share_dir(path)` | Create shared folder (team-specific) | Provider-specific sharing |
| `get_metadata(path, include_deleted)` | Get file/folder metadata | Stat |
| `list_folder(path, recursive)` | List contents | List |
| `list_folder_iterator(path, recursive)` | Paginated list | Paginated list |
| `wait_for_remote_changes(cursor, timeout)` | Long-poll for changes | Change notification (webhook, polling, etc.) |
| `list_remote_changes_iterator(cursor)` | Get changes since cursor | Delta/changes API |
| `get_latest_cursor(path)` | Get current state token | Snapshot token |
| `list_revisions(path)` | File version history | Version history |
| `restore(path, rev)` | Restore old version | Version restore |

### 4.2 Content Hashing — Must Be Replaced

`DropboxContentHasher` implements Dropbox's proprietary block-hashing algorithm. Every backend has its own integrity verification:
- **Google Drive**: MD5 checksums.
- **OneDrive**: SHA-1 or QuickXorHash.
- **S3**: MD5 or SHA-256 ETags.

Changes needed:
- Replace `DropboxContentHasher` in `utils/hashing.py`.
- Replace `content_hash()` in `utils/path.py`.
- Update `get_local_hash()` in `SyncEngine`.
- Update all content hash comparisons in `_check_download_conflict()`, `_check_requires_upload()`, `SyncEvent.from_file_system_event()`, `SyncEvent.from_metadata()`.
- The `IndexEntry.content_hash` column stores the provider-specific hash.

### 4.3 Cursors / Change Tokens — Must Be Adapted

Dropbox's cursor system is unusually powerful (opaque token that works for both pagination and incremental change detection). Other providers differ:
- **Google Drive**: `startPageToken` + `changes.list()` — similar concept but different API.
- **OneDrive**: `deltaLink` / `@odata.nextLink` — similar delta pattern.
- **S3**: No built-in change feed — requires polling via `ListObjectsV2` or S3 Events.

Changes needed:
- The `remote_cursor` property and its state persistence work for any string token. But the **semantics** change: not all providers support resumable pagination of change sets.
- `wait_for_remote_changes()` uses Dropbox's long-poll. Other providers need webhooks or polling.
- `download_sync_cycle()`'s incremental-cursor-save-per-page pattern depends on Dropbox guaranteeing that a mid-pagination cursor is valid for future `list_folder/continue` calls.

### 4.4 Revisions — Must Be Adapted

- Dropbox `rev` is a unique version string per file version, used for optimistic concurrency (`update_rev` in uploads, `parent_rev` in deletes).
- Other providers use ETags (HTTP), version IDs, or generation numbers.
- The `IndexEntry.rev` field and all rev-comparison logic in conflict detection would need updating.
- The `client.download(f"rev:{event.rev}", ...)` pattern for downloading specific versions is Dropbox-specific.

### 4.5 Namespace/Team Space Logic — Provider-Specific, Can Be Removed or Replaced

The entire `SyncManager._update_path_root()` / `check_and_update_path_root()` system, `UserRootInfo`/`TeamRootInfo`, and `client.update_path_root()` are deeply Dropbox-specific. Other providers have their own shared folder models.

### 4.6 Path Semantics — Must Be Adapted

- Dropbox uses case-insensitive paths globally (`path_lower`). Other providers may be case-sensitive.
- The dual-path system (`dbx_path_cased` / `dbx_path_lower`) and `correct_case()` machinery is Dropbox-specific.
- Dropbox's unicode normalization (NFC) is baked into the path handling.

### 4.7 Core Data Models (`core.py`) — Must Be Extended

`FileMetadata`, `FolderMetadata`, `DeletedMetadata`, `ListFolderResult` are Dropbox-shaped but provider-agnostic enough to serve as an internal representation. Key Dropbox-specific fields:
- `FileMetadata.rev` — Provider-specific version ID.
- `FileMetadata.content_hash` — Provider-specific hash.
- `FileMetadata.modified_by` — Dropbox account ID.
- `FileMetadata.symlink_target` — Dropbox-specific symlink support.
- `ListFolderResult.cursor` — Provider-specific change token.

### 4.8 What Would NOT Need to Change

Many components are provider-agnostic:
- **`FSEventHandler`** — Pure local file system watching via watchdog.
- **`_clean_local_events()`** — Event deduplication/consolidation is purely local logic.
- **`SyncManager`** thread orchestration — Thread lifecycle, connection monitoring, startup sequencing.
- **SQLite database schema and ORM** — The index, history, hash cache, and error tables are generic.
- **`ActivityTree`** — UI status tracking.
- **Selective sync / mignore** — File exclusion patterns.
- **Desktop notifications** — Generic notification system.
- **Conflict copy naming** — `generate_cc_name()` is generic.
- **CPU throttling** — `_slow_down()` is generic.
- **Local path utilities** — Case sensitivity detection, path normalization, etc.

### 4.9 Recommended Abstraction Strategy

To support multiple backends, introduce a **`CloudClient` protocol/ABC** with:

```python
class CloudClient(Protocol):
    def upload(self, local_path, remote_path, mode, match_rev) -> RemoteMetadata: ...
    def download(self, remote_path_or_id, local_path) -> RemoteMetadata: ...
    def move(self, from_path, to_path) -> RemoteMetadata: ...
    def remove(self, path, match_rev=None) -> None: ...
    def make_dir(self, path) -> RemoteMetadata: ...
    def get_metadata(self, path) -> RemoteMetadata | None: ...
    def list_folder(self, path, recursive) -> ListResult: ...
    def wait_for_changes(self, token, timeout) -> bool: ...
    def list_changes(self, token) -> Iterator[ChangeSet]: ...
    def compute_content_hash(self, local_path) -> str: ...  # Provider-specific hash
    def get_change_token(self, path) -> str: ...
```

Then refactor `SyncEngine` to use this protocol instead of `DropboxClient` directly. The biggest challenge is that the conflict resolution logic in `SyncEngine` is intimately tied to Dropbox semantics (rev-based optimistic concurrency, content hash algorithm, case-insensitive path model).

---

## 5. Key Method Reference

### SyncEngine (sync.py) — Core Methods

| Method | Lines | Responsibility |
|--------|-------|----------------|
| `__init__()` | ~570-630 | Init DB, config, locks, caches |
| `reload_cached_config()` | ~637 | Reloads dropbox_path, excluded_items, mignore, CPU limits |
| `remote_cursor` (property) | ~747 | Get/set Dropbox server cursor (persisted to state file) |
| `local_cursor` (property) | ~762 | Get/set local sync timestamp (persisted to state file) |
| `reset_sync_state()` | ~808 | Clears all DB tables and resets state |
| `get_index_entry()` | ~864 | Lookup single index entry by normalized path |
| `update_index_from_sync_event()` | ~940 | Updates index after a sync completes |
| `update_index_from_dbx_metadata()` | ~975 | Updates index from Dropbox metadata directly |
| `remove_node_from_index()` | ~1015 | Removes path and all children from index |
| `get_local_hash()` | ~1025 | Computes Dropbox content hash with caching |
| `correct_case()` | ~1230 | Reconstructs fully-cased path from basename-only casing |
| `to_dbx_path()` / `to_dbx_path_lower()` | ~1320 | Local path → Dropbox path conversion |
| `to_local_path()` / `to_local_path_from_cased()` | ~1340 | Dropbox path → local path conversion |
| `is_excluded()` | ~1365 | Checks system exclusions (temp files, caches) |
| `is_excluded_by_user()` | ~1410 | Checks selective sync exclusions |
| `is_mignore()` | ~1415 | Checks .mignore patterns |
| `cancel_sync()` | ~1450 | Sets cancel event, waits for sync_lock |
| `upload_local_changes_while_inactive()` | ~1490 | Full scan + upload for offline changes |
| `_get_local_changes_while_inactive()` | ~1520 | Directory walk comparing FS to index |
| `wait_for_local_changes()` | ~1590 | Blocks on FSEventHandler queue |
| `upload_sync_cycle()` | ~1600 | Full upload cycle: list → clean → apply |
| `list_local_changes()` | ~1620 | Drain event queue, clean, convert to SyncEvents |
| `apply_local_changes()` | ~1650 | Categorize and apply local changes to remote |
| `_clean_local_events()` | ~1710 | Deduplicate/consolidate watchdog events |
| `_create_remote_entry()` | ~2000 | Dispatch single upload event to appropriate handler |
| `_on_local_file_modified()` | ~2085 | Upload file (add/modify) |
| `_on_local_folder_created()` | ~2140 | Create remote folder |
| `_on_local_moved()` | ~2030 | Move on remote |
| `_on_local_deleted()` | ~2195 | Delete on remote (with conflict checks) |
| `_check_requires_upload()` | ~2310 | Compare content hashes to skip no-op uploads |
| `get_remote_item()` | ~2340 | Download single item (used for selective sync includes) |
| `wait_for_remote_changes()` | ~2395 | Long-poll wrapper |
| `download_sync_cycle()` | ~2415 | Full download cycle: list → apply → save cursor |
| `list_remote_changes_iterator()` | ~2460 | Get remote changes as SyncEvent iterator |
| `apply_remote_changes()` | ~2490 | Categorize and apply remote changes locally |
| `_create_local_entry()` | ~2610 | Dispatch single download event to handler |
| `_check_download_conflict()` | ~2680 | Multi-step conflict detection algorithm |
| `_on_remote_file()` | ~2780 | Download file to local |
| `_on_remote_folder()` | ~2850 | Create local folder |
| `_on_remote_deleted()` | ~2900 | Delete locally |
| `_apply_case_change()` | ~2940 | Rename local item for casing changes |
| `_clean_remote_changes()` | ~2750 | Deduplicate remote changes per path |
| `rescan()` | ~2960 | Force re-examination of a local path |
| `notify_user()` | ~2560 | Desktop notifications for sync events |

### SyncManager (manager.py) — Thread Management

| Method | Responsibility |
|--------|----------------|
| `start()` | Creates and starts all sync threads + FS observer |
| `stop()` | Stops threads, disables event handler, cancels sync |
| `startup_worker()` | One-shot: retry errors, resume downloads, initial sync |
| `download_worker()` | Loop: long-poll → download_sync_cycle |
| `download_worker_added_item()` | Loop: drain download_queue → get_remote_item |
| `upload_worker()` | Loop: wait_for_local_changes → upload_sync_cycle |
| `connection_monitor()` | Loop: check connectivity, auto-restart on reconnect |
| `check_and_update_path_root()` | Detect and handle team namespace changes |
| `_update_path_root()` | Migrate local folder structure for team changes |
| `rebuild_index()` | Stop → reset → restart |
| `_handle_sync_thread_errors()` | Context manager catching CancelledError, connection errors, PathRootError |

### Maestral (main.py) — Public API

| Method | Responsibility |
|--------|----------------|
| `start_sync()` | Validates state, calls `manager.start()` |
| `stop_sync()` | Calls `manager.stop()` |
| `exclude_items()` | Add to exclusion list, delete locally |
| `include_items()` | Remove from exclusion list, queue for download |
| `excluded_items` (property) | Get/set selective sync list with side effects |
| `get_file_status()` | Returns sync status string for a local path |
| `get_activity()` | Returns current in-progress sync events |
| `get_history()` | Returns completed sync events from DB |
| `link()` / `unlink()` | Dropbox account linking |
| `move_dropbox_directory()` | Relocate local Dropbox folder |
| `create_dropbox_directory()` | Initial setup of local folder |
| `rebuild_index()` | Triggers full reindex |
| `restore()` | Restore old file revision |
| `get_file_diff()` | Diff two revisions of a text file |
| `list_folder()` / `list_revisions()` | Proxy to client API |
| `create_shared_link()` / `revoke_shared_link()` | Sharing management |
| `check_for_updates()` | GitHub release API check |
| `shutdown_daemon()` | Stop sync + signal event loop |
