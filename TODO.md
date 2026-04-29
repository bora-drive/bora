# TODO: Maestral → Google Drive Fork

All known tasks, roughly in dependency order. Check off as completed.

---

## Phase 1: Project Setup & Renaming

- [x] Fork the repo and create a new branch
- [x] Choose a project name: keeping "maestral" with Google Drive backend
- [x] Update `pyproject.toml`:
  - [x] Change project name and description
  - [x] Remove `dropbox` dependency
  - [x] Add `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2`
  - [x] Update classifiers and URLs
- [x] Update `README.md` with new project description
- [x] Update `constants.py` — branding strings
- [x] Update `config/main.py` — default config values and section names
- [x] Rename `DropboxAuthError` → `AuthError` in `exceptions.py`
- [x] Rename `DropboxConnectionError` → `ConnectionError` in `exceptions.py`
- [x] Rename `DropboxServerError` → `ServerError` in `exceptions.py`
- [x] Rename `NoDropboxDirError` → `NoSyncDirError` in `exceptions.py`
- [x] Update all imports of renamed exceptions across the codebase
- [x] Rename `dropbox_path` → `sync_path` in `sync.py`, `main.py`, `manager.py`
- [x] Rename `dbx_path` → `remote_path` in `models.py` (fields: `dbx_path`, `dbx_path_lower`, `dbx_path_from`, `dbx_path_from_lower`)
- [x] Rename `dbx_id` → `remote_id` in `models.py`
- [x] Propagate field renames through `sync.py` (~365 references)
- [x] Propagate field renames through `main.py` (~213 references)
- [x] Propagate field renames through `manager.py` (~46 references)
- [x] Propagate field renames through `cli/*.py`
- [ ] Update `autostart.py` — service/application names (will be done as part of full rename)
- [ ] Update `keyring.py` — keyring service name (will be done as part of full rename)
- [ ] Update `notify.py` — notification text (will be done as part of full rename)
- [ ] Rename project from "maestral" to "cumulus" throughout (package name, imports, CLI entry point, config names, constants, docs, tests)

## Phase 2: Google Cloud Project

- [ ] Create a Google Cloud project
- [ ] Enable the Google Drive API
- [ ] Configure the OAuth consent screen
  - [ ] Add `https://www.googleapis.com/auth/drive` scope
  - [ ] Set application name and branding
- [ ] Create OAuth 2.0 Client ID (Desktop application type)
- [ ] Download `client_secret.json`
- [ ] Add `client_secret.json` to `src/maestral/resources/` (or a config mechanism)
- [ ] Add `.gitignore` entry for credentials if needed
- [ ] Document the Google Cloud setup for contributors

## Phase 3: Authentication

- [ ] Implement OAuth2 flow in new `client.py` using `google-auth-oauthlib`
  - [ ] `get_auth_url()` — generate authorization URL
  - [ ] `link(code)` — exchange auth code for tokens
  - [ ] `unlink()` — revoke token and clear credentials
- [ ] Store refresh token in keyring (adapt existing `keyring.py` storage)
- [ ] Implement token refresh logic (auto-refresh on 401)
- [ ] Implement `linked` property (check for valid stored credentials)
- [ ] Test auth flow end-to-end

## Phase 4: Path↔ID Cache (NEW MODULE)

- [ ] Create `path_cache.py` module
- [ ] Design SQLite schema for path↔ID mapping:
  - [ ] `file_id` (primary key)
  - [ ] `remote_path` (indexed)
  - [ ] `remote_path_lower` (indexed)
  - [ ] `parent_id`
  - [ ] `name`
  - [ ] `is_folder`
- [ ] Implement `path_to_id(remote_path) -> str | None`
- [ ] Implement `id_to_path(file_id) -> str | None`
- [ ] Implement `resolve_path(file_id) -> str` (walk parent chain)
- [ ] Implement `set(remote_path, file_id, parent_id, name, is_folder)`
- [ ] Implement `remove(remote_path)` with cascade for children
- [ ] Implement `move(old_path, new_path)` with child path updates
- [ ] Implement `populate_from_listing(items)` for initial full sync
- [ ] Handle duplicate name detection and resolution
- [ ] Handle files with multiple parents (pick canonical)
- [ ] Write unit tests for path cache

## Phase 5: Google Drive Client (CORE REWRITE)

### 5a: Client scaffolding

- [ ] Create new `client.py` with `GoogleDriveClient` class
- [ ] Initialize `googleapiclient.discovery.build('drive', 'v3', ...)` service
- [ ] Wire up `PathIDCache` instance
- [ ] Implement retry/backoff decorator (adapt existing `_retry_on_error`)
- [ ] Implement bandwidth throttling (adapt existing upload/download throttle)
- [ ] Implement `close()` / context manager

### 5b: Account operations

- [ ] `get_account_info()` → call `about.get(fields='user,storageQuota')`
- [ ] Convert Google `User` response to `core.FullAccount`
- [ ] `get_space_usage()` → extract from `about.get` `storageQuota`
- [ ] Convert to `core.PersonalSpaceUsage`
- [ ] Implement `account_info` property (cached)

### 5c: Metadata operations

- [ ] `get_metadata(remote_path)` → resolve path to ID via cache, call `files.get`
- [ ] Convert Google `File` resource to `core.FileMetadata` or `core.FolderMetadata`
- [ ] Handle `include_deleted` parameter (check `trashed` field)
- [ ] Handle `DeletedMetadata` for trashed files
- [ ] `list_revisions(remote_path)` → `revisions.list`
- [ ] `restore(remote_path, rev)` → copy old revision as current (Drive doesn't have direct restore)

### 5d: Download

- [ ] `download(remote_path, dst_path)` → resolve ID, use `MediaIoBaseDownload`
- [ ] Stream to temp file, verify MD5 checksum, then move to destination
- [ ] Handle bandwidth throttling during download
- [ ] Handle Google Docs/Sheets/Slides (skip or export)
- [ ] Return `FileMetadata` with correct fields

### 5e: Upload

- [ ] `upload(remote_path, data/path, write_mode)` → resolve parent ID
- [ ] Simple upload for small files (≤5MB)
- [ ] Resumable upload for large files (>5MB) via `MediaFileUpload(resumable=True)`
- [ ] Handle `WriteMode.Add` → `files.create`
- [ ] Handle `WriteMode.Update` → `files.update` with `fileId`
- [ ] Handle `WriteMode.Overwrite` → `files.update` without revision check
- [ ] Verify content hash (MD5) after upload
- [ ] Update path cache after successful upload
- [ ] Handle bandwidth throttling during upload

### 5f: File/folder operations

- [ ] `remove(remote_path)` → resolve ID, call `files.delete` or `files.update(trashed=True)`
- [ ] `move(from_path, to_path)` → resolve IDs, call `files.update` with new `name` and/or `parents`
- [ ] `make_dir(remote_path)` → resolve parent ID, call `files.create` with `mimeType=folder`
- [ ] `make_dir_batch(paths)` → batch API call or sequential
- [ ] `remove_batch(paths)` → batch API call or sequential
- [ ] Handle `autorename` parameter (check for conflicts, append suffix)
- [ ] Update path cache after each mutation

### 5g: Change detection (sync feed)

- [ ] `get_latest_cursor(path)` → call `changes.getStartPageToken`
- [ ] `list_folder(path, recursive)` → call `files.list` with `q` parameter for folder contents
- [ ] `list_folder_iterator(path, recursive)` → paginated `files.list` yielding `ListFolderResult`
- [ ] Convert Google `File` resources to `core.Metadata` subclasses in results
- [ ] Populate path cache from listing results
- [ ] `wait_for_remote_changes(cursor, timeout)` → **poll** `changes.list` with `pageToken`, sleep loop
- [ ] `list_remote_changes(cursor)` → call `changes.list`, translate file IDs to paths via cache
- [ ] `list_remote_changes_iterator(cursor)` → paginated version, yield `ListFolderResult` pages
- [ ] Handle `removed` flag in change entries (file deleted/moved out of scope)
- [ ] Handle changes to files outside sync scope (filter by root folder)

### 5h: Sharing

- [ ] `share_dir(remote_path)` → create permission (role=writer, type=anyone) on folder
- [ ] `create_shared_link(remote_path, ...)` → create viewer permission + return `webViewLink`
- [ ] `revoke_shared_link(url)` → remove permission
- [ ] `list_shared_links(remote_path)` → list permissions, build `SharedLinkMetadata`
- [ ] Convert Google permission/link data to `core.SharedLinkMetadata`
- [ ] Implement `is_team_space` property (Shared Drives — False for MVP)

## Phase 6: Error Handling (REWRITE)

- [ ] Rewrite `errorhandling.py` for Google API errors
- [ ] Map `googleapiclient.errors.HttpError` status codes:
  - [ ] 400 → `BadInputError`
  - [ ] 401 → `AuthError` (token expired)
  - [ ] 403 (reason: `userRateLimitExceeded`) → rate limit retry
  - [ ] 403 (reason: `insufficientPermissions`) → `InsufficientPermissionsError`
  - [ ] 403 (reason: `storageQuotaExceeded`) → `InsufficientSpaceError`
  - [ ] 404 → `NotFoundError`
  - [ ] 409 → `ConflictError`
  - [ ] 429 → rate limit retry with backoff
  - [ ] 500, 502, 503 → `ServerError`
- [ ] Map `google.auth.exceptions.RefreshError` → `AuthError`
- [ ] Map network/connection errors → `ConnectionError`
- [ ] Keep `convert_api_errors` context manager pattern
- [ ] Keep `os_to_maestral_error` (OS errors are backend-agnostic)

## Phase 7: Content Hashing

- [ ] Replace `DropboxContentHasher` with `ContentHasher` using `hashlib.md5`
- [ ] Simplify implementation (no block-based hashing needed for MD5)
- [ ] Update `StreamHasher` to work with new hasher (likely no changes needed)
- [ ] Update `get_hash()` function in `client.py`
- [ ] Update all references from `DropboxContentHasher` to `ContentHasher`
- [ ] Update `content_hash` comparison logic in `sync.py` (compare MD5 strings)
- [ ] Verify Google Drive's `md5Checksum` field matches locally computed MD5
- [ ] Handle files without `md5Checksum` (Google Docs/Sheets/Slides have none)

## Phase 8: Sync Engine Adaptations

- [ ] Update `SyncEngine.__init__` to accept `GoogleDriveClient` instead of `DropboxClient`
- [ ] Update `wait_for_remote_changes` to work with polling (no long-poll)
- [ ] Update `download_sync_cycle` for Google Drive change feed format
- [ ] Update `list_remote_changes_iterator` call site for new response format
- [ ] Update `_clean_remote_changes` for Google Drive change semantics
- [ ] Update `_on_remote_file` — verify MD5 instead of Dropbox content hash
- [ ] Update `upload_local_changes_while_inactive` — hash comparison logic
- [ ] Handle Google Docs/Sheets/Slides in remote changes (skip or export)
- [ ] Handle Google Drive's "file removed from folder" vs "file trashed" distinction
- [ ] Update `reset_sync_state` for new cursor format
- [ ] Test selective sync (excluded folders) with ID-based paths
- [ ] Verify case-sensitivity handling (Google Drive is case-insensitive in search, case-preserving in names)

## Phase 9: CLI Updates

- [ ] Update `cli/cli_main.py` — help text and branding
- [ ] Update `cli/cli_core.py` — `ls`, `mkdir`, `mv`, etc. with new path terminology
- [ ] Update `cli/cli_info.py` — account info display
- [ ] Update `cli/cli_maintenance.py` — rebuild index, etc.
- [ ] Update `cli/cli_settings.py` — configuration options
- [ ] Update `cli/dialogs.py` — setup prompts and link flow
- [ ] Update `cli/common.py` — shared CLI utilities
- [ ] Update `cli/core.py` — CLI command definitions
- [ ] Test all CLI commands end-to-end

## Phase 10: Manager Updates

- [ ] Update `SyncManager` for new client type
- [ ] Update `_startup` to handle Google Drive-specific folder setup
- [ ] Update `_check_and_update_path_root` for Shared Drives (or remove for MVP)
- [ ] Update `_connection_helper` to check Google Drive connectivity
- [ ] Update `_download_worker` loop for polling-based change detection
- [ ] Remove Dropbox Team Space migration logic (or adapt for Shared Drives)
- [ ] Update `ensure_dropbox_folder_present` → `ensure_drive_folder_present`

## Phase 11: Database Migration

- [ ] Add path cache table to SQLite schema
- [ ] Update `IndexEntry` model for renamed fields
- [ ] Update `SyncEvent` model for renamed fields
- [ ] Update `SyncErrorEntry` model for renamed fields
- [ ] Write migration from old schema (if supporting upgrades from Dropbox version) or fresh start
- [ ] Update `HashCacheEntry` to store MD5 instead of Dropbox hash

## Phase 12: Testing

### Unit tests (offline)

- [ ] Port `tests/offline/test_client.py` for `GoogleDriveClient` (mock Google API)
- [ ] Port `tests/offline/test_sync.py` for renamed fields
- [ ] Port `tests/offline/test_event_handler.py`
- [ ] Port `tests/offline/test_clean_local_events.py`
- [ ] Port `tests/offline/test_errorhandling.py` for new error mappings
- [ ] Port `tests/offline/test_main.py`
- [ ] Port `tests/offline/test_manager.py`
- [ ] Port `tests/offline/test_keyring.py`
- [ ] Port `tests/offline/test_cli.py`
- [ ] Port `tests/offline/test_daemon.py`
- [ ] Port `tests/offline/config/` tests
- [ ] Port `tests/offline/utils/` tests
- [ ] Write new tests for `PathIDCache`
- [ ] Write new tests for Google Drive error mapping

### Integration tests (linked)

- [ ] Set up test Google account with Drive API access
- [ ] Port `tests/linked/unit/test_client.py`
- [ ] Port `tests/linked/integration/test_sync.py`
- [ ] Port `tests/linked/integration/test_main.py`
- [ ] Test initial full sync (empty remote → local)
- [ ] Test initial full sync (populated remote → local)
- [ ] Test upload of new file
- [ ] Test upload of modified file
- [ ] Test upload of deleted file
- [ ] Test download of new file
- [ ] Test download of modified file
- [ ] Test download of deleted file
- [ ] Test conflict resolution (local and remote change same file)
- [ ] Test move/rename operations
- [ ] Test large file upload (resumable)
- [ ] Test selective sync (exclude/include folders)
- [ ] Test special characters in filenames
- [ ] Test rate limit handling
- [ ] Test token refresh
- [ ] Test network disconnection and reconnection

## Phase 13: Documentation

- [ ] Update `docs/` Sphinx configuration for new project name
- [ ] Update all docstrings referencing Dropbox
- [ ] Write setup guide (Google Cloud project creation, OAuth setup)
- [ ] Write user guide (linking account, choosing sync folder)
- [ ] Document differences from Dropbox behavior
- [ ] Document known limitations (Google Docs, multiple parents, etc.)

## Phase 14: Packaging & Distribution

- [ ] Update `pyproject.toml` scripts entry point
- [ ] Update systemd service template (if applicable)
- [ ] Update desktop entry / icons
- [ ] Test `pip install` from source
- [ ] Test on macOS
- [ ] Test on Linux
- [ ] Publish to PyPI (if public)
- [ ] Submit Google OAuth app for verification (if public)

---

## Known Limitations / Out of Scope for MVP

- [ ] Shared Drives (team spaces) — support "My Drive" only initially
- [ ] Google Docs/Sheets/Slides export — skip native Google files initially
- [ ] Webhook-based change notifications — use polling initially
- [ ] Batch API calls — use sequential calls initially, optimize later
- [ ] Files with multiple parents — pick first parent, log warning
- [ ] GUI packages (`maestral-qt`, `maestral-cocoa`) — update separately
