# Maestral Dropbox API Interface Analysis

> ⚠️ **PRE-PORT BASELINE — describes the *original* Dropbox SDK integration, not
> the shipped Google Drive (`cumulus`) code.** This document was written at the
> start of the port to map the existing Dropbox surface; it has not been (and is
> not meant to be) updated to the current implementation. The Dropbox SDK is gone
> — `client.py` now wraps `googleapiclient`, the exception hierarchy was renamed
> (`MaestralApiError`→`CumulusApiError`, etc.), and the method/class names below no
> longer exist except for a backward-compat `DropboxClient = GoogleDriveClient`
> alias slated for removal in Stage 8. For *current* Drive behavior see
> `dropbox-vs-drive-differences.md` and the per-stage specs. Kept for historical
> context only.

## Overview

Maestral has a **clean abstraction layer** between its sync logic and the Dropbox API. The architecture follows this pattern:

```
Sync Engine (sync.py)
    ↓ uses
DropboxClient (client.py)         ← Maestral's abstraction layer
    ↓ wraps
_DropboxSDK (client.py)           ← Thin subclass of Dropbox SDK
    ↓ extends
dropbox.Dropbox (SDK)             ← Official Dropbox Python SDK
```

All Dropbox SDK types are converted to Maestral's own dataclasses at the client boundary. The sync engine never sees raw SDK objects.

---

## 1. Dropbox SDK Types and Methods Used

### SDK Imports (client.py)

```python
from dropbox import Dropbox, common, create_session, exceptions, files, sharing, users
from dropbox.dropbox_client import USER_AUTH, BadInputException, RouteErrorResult, RouteResult
from dropbox.oauth import DropboxOAuth2FlowNoRedirect
from dropbox.session import API_HOST
```

### SDK Imports (errorhandling.py)

```python
from dropbox import async_, auth, common, exceptions, files, sharing, users
from dropbox.stone_validators import ValidationError
```

### SDK API Methods Called (via `self.dbx.*` or `self.dbx_base.*`)

| Category | SDK Method | Maestral Wrapper | Returns (Maestral type) |
|----------|-----------|-----------------|------------------------|
| **Auth** | `auth_token_revoke()` | `unlink()` | None |
| **Users** | `users_get_current_account()` | `get_account_info()` | `FullAccount` |
| **Users** | `users_get_account(dbid)` | `get_account_info(dbid)` | `Account` |
| **Users** | `users_get_space_usage()` | `get_space_usage()` | `PersonalSpaceUsage` |
| **Files** | `files_get_metadata()` | `get_metadata()` | `Metadata \| None` |
| **Files** | `files_list_revisions()` | `list_revisions()` | `list[FileMetadata]` |
| **Files** | `files_restore()` | `restore()` | `FileMetadata` |
| **Files** | `files_download()` | `download()` | `FileMetadata` |
| **Files** | `files_upload()` | `upload()` (small files) | `FileMetadata` |
| **Files** | `files_upload_session_start()` | `upload()` (large files) | `FileMetadata` |
| **Files** | `files_upload_session_append_v2()` | `upload()` (large files) | (internal) |
| **Files** | `files_upload_session_finish()` | `upload()` (large files) | `FileMetadata` |
| **Files** | `files_delete_v2()` | `remove()` | `FileMetadata \| FolderMetadata` |
| **Files** | `files_delete_batch()` | `remove_batch()` | `list[Metadata \| MaestralApiError]` |
| **Files** | `files_delete_batch_check()` | `remove_batch()` | (internal) |
| **Files** | `files_move_v2()` | `move()` | `FileMetadata \| FolderMetadata` |
| **Files** | `files_create_folder_v2()` | `make_dir()` | `FolderMetadata` |
| **Files** | `files_create_folder_batch()` | `make_dir_batch()` | `list[FolderMetadata \| MaestralApiError]` |
| **Files** | `files_create_folder_batch_check()` | `make_dir_batch()` | (internal) |
| **Files** | `files_list_folder()` | `list_folder()` / `list_folder_iterator()` | `ListFolderResult` |
| **Files** | `files_list_folder_continue()` | `list_folder_iterator()` / `list_remote_changes_iterator()` | `ListFolderResult` |
| **Files** | `files_list_folder_get_latest_cursor()` | `get_latest_cursor()` | `str` (cursor) |
| **Files** | `files_list_folder_longpoll()` | `wait_for_remote_changes()` | `bool` |
| **Sharing** | `sharing_share_folder()` | `share_dir()` | `FolderMetadata \| None` |
| **Sharing** | `sharing_check_share_job_status()` | `share_dir()` | (internal) |
| **Sharing** | `sharing_create_shared_link_with_settings()` | `create_shared_link()` | `SharedLinkMetadata` |
| **Sharing** | `sharing_revoke_shared_link()` | `revoke_shared_link()` | None |
| **Sharing** | `sharing_list_shared_links()` | `list_shared_links()` | `list[SharedLinkMetadata]` |

### SDK Data Types Referenced in Conversions / Error Handling

**files module:**
- `files.FileMetadata`, `files.FolderMetadata`, `files.DeletedMetadata` — metadata types
- `files.WriteMode` (.add, .overwrite, .update) — upload write modes
- `files.ListRevisionsMode` — revision listing mode
- `files.ListFolderResult` — folder listing result
- `files.UploadSessionCursor` — chunked upload cursor
- `files.CommitInfo` — chunked upload commit
- `files.DeleteArg` — batch delete argument
- Error types: `RelocationError`, `CreateFolderError`, `CreateFolderEntryError`, `DeleteError`, `UploadError`, `UploadSessionStartError`, `UploadSessionFinishError`, `UploadSessionLookupError`, `UploadSessionAppendError`, `DownloadError`, `ListFolderError`, `ListFolderContinueError`, `ListFolderLongpollError`, `ListRevisionsError`, `RestoreError`, `GetMetadataError`, `WriteError`, `LookupError`

**users module:**
- `users.FullAccount`, `users.Account`, `users.SpaceUsage`
- Error types: `GetAccountError`

**sharing module:**
- `sharing.SharedLinkSettings`, `sharing.SharedLinkMetadata`, `sharing.ListSharedLinksResult`
- `sharing.LinkAudience`, `sharing.RequestedLinkAccessLevel`
- Error types: `CreateSharedLinkWithSettingsError`, `RevokeSharedLinkError`, `ListSharedLinksError`, `ShareFolderErrorBase`, `ShareFolderError`, `SharePathError`

**common module:**
- `common.PathRoot` — for namespace/path root headers
- `common.TeamRootInfo` — for team root detection
- `common.PathRootError` — error type

**auth module:**
- `auth.AuthError` — authentication error variants

**async_ module:**
- `async_.PollError` — batch job polling error

**oauth module:**
- `DropboxOAuth2FlowNoRedirect` — OAuth2 PKCE flow

---

## 2. Abstraction Layers

### Layer 1: `_DropboxSDK` (extends `dropbox.Dropbox`)

A thin subclass of the official SDK's `Dropbox` class that overrides `request_json_string()` to support **streamed/chunked uploads** (the SDK normally requires `bytes` for request bodies, but Maestral passes iterators for bandwidth throttling).

### Layer 2: `DropboxClient` (main abstraction)

This is the **primary abstraction boundary**. Key responsibilities:

1. **Type conversion**: Every SDK return value is converted to a Maestral dataclass via `convert_*` functions before being returned. The sync engine never handles raw SDK types.

2. **Error translation**: All SDK exceptions are caught by the `convert_api_errors()` context manager and translated to `MaestralApiError` subclasses. This happens in `errorhandling.py`.

3. **Chunked upload management**: Files > 4MB are automatically uploaded in chunks via upload sessions. Includes retry logic on `DataCorruptionError` (up to 10 retries).

4. **Bandwidth throttling**: Upload and download iterators enforce configurable bandwidth limits (`bandwidth_limit_up`, `bandwidth_limit_down`) distributed across concurrent transfers.

5. **Content hash verification**: Downloads are verified against Dropbox content hashes. Uploads include content hashes for server-side verification.

6. **Pagination handling**: `list_folder_iterator()` and `list_remote_changes_iterator()` yield pages; `flatten_results()` collapses them into single results.

7. **Async batch job polling**: `remove_batch()`, `make_dir_batch()`, and `share_dir()` handle Dropbox's async job pattern (start → poll until complete).

8. **OAuth/linking**: Manages the OAuth2 PKCE flow, token storage via `CredentialStorage`, SDK initialization with refresh/access tokens.

9. **Path root management**: Handles Dropbox Team Spaces via namespace headers (`common.PathRoot.root()`).

10. **Longpoll**: `wait_for_remote_changes()` wraps the longpoll endpoint with backoff handling.

### Layer 3: Error Handling (`errorhandling.py`)

A comprehensive error translation layer with:
- `convert_api_errors()` — context manager that catches `DropboxException`, `ValidationError`, `OSError`, and connection errors
- `dropbox_to_maestral_error()` — maps ~40+ specific SDK error variants to Maestral exceptions
- `os_to_maestral_error()` — maps OS errno codes to Maestral exceptions
- Helper functions: `get_write_error_msg()`, `get_lookup_error_msg()`, `get_session_lookup_error_msg()`, `get_bad_path_error_msg()`

### Conversion Functions (bottom of client.py)

| Function | SDK Type → Maestral Type |
|----------|-------------------------|
| `convert_account()` | `users.Account` → `core.Account` |
| `convert_full_account()` | `users.FullAccount` → `core.FullAccount` |
| `convert_space_usage()` | `users.SpaceUsage` → `core.PersonalSpaceUsage` |
| `convert_metadata()` | `files.{File,Folder,Deleted}Metadata` → `core.{File,Folder,Deleted}Metadata` |
| `convert_list_folder_result()` | `files.ListFolderResult` → `core.ListFolderResult` |
| `convert_shared_link_metadata()` | `sharing.SharedLinkMetadata` → `core.SharedLinkMetadata` |
| `convert_list_shared_link_result()` | `sharing.ListSharedLinksResult` → `core.ListSharedLinkResult` |

---

## 3. Key Data Models and Dropbox Mapping

### core.py — Pure Dataclasses (Dropbox-agnostic)

These are **plain Python dataclasses with no SDK dependencies**. They mirror Dropbox concepts but are SDK-independent.

| Maestral Type | Dropbox SDK Equivalent | Notes |
|---------------|----------------------|-------|
| `Metadata` | `files.Metadata` | Base: `name`, `path_lower`, `path_display` |
| `FileMetadata` | `files.FileMetadata` | Adds: `id`, `client_modified`, `server_modified`, `rev`, `size`, `symlink_target`, `shared`, `modified_by`, `is_downloadable`, `content_hash` |
| `FolderMetadata` | `files.FolderMetadata` | Adds: `id`, `shared` |
| `DeletedMetadata` | `files.DeletedMetadata` | No extra fields |
| `ListFolderResult` | `files.ListFolderResult` | `entries`, `has_more`, `cursor` |
| `Account` | `users.Account` | `account_id`, `display_name`, `email`, etc. |
| `FullAccount` | `users.FullAccount` | Extends Account with `country`, `locale`, `team`, `account_type`, `root_info` |
| `SpaceUsage` | (derived from `users.SpaceUsage`) | `used`, `allocated` |
| `PersonalSpaceUsage` | `users.SpaceUsage` | Adds `team_usage` |
| `Team` | `users.Team` (via FullAccount) | `id`, `name` |
| `RootInfo` | `common.RootInfo` | `root_namespace_id`, `home_namespace_id` |
| `UserRootInfo` | `common.UserRootInfo` | Same fields as RootInfo |
| `TeamRootInfo` | `common.TeamRootInfo` | Adds `home_path` |
| `AccountType` | `users.AccountType` | Enum: Basic, Pro, Business, Other |
| `WriteMode` | `files.WriteMode` | Enum: Add, Update, Overwrite |
| `SharedLinkMetadata` | `sharing.SharedLinkMetadata` | `url`, `name`, `path_lower`, `expires`, `link_permissions` |
| `LinkPermissions` | `sharing.LinkPermissions` | `can_revoke`, `allow_download`, `effective_audience`, `link_access_level`, `require_password` |
| `LinkAudience` | `sharing.LinkAudience` | Enum: Public, Team, NoOne, Other |
| `LinkAccessLevel` | `sharing.RequestedLinkAccessLevel` | Enum: Viewer, Editor, Other |
| `ListSharedLinkResult` | `sharing.ListSharedLinksResult` | `entries`, `has_more`, `cursor` |

### models.py — ORM Models (sync state tracking)

These are **database-backed models** using Maestral's custom ORM. They track sync state and are **not** direct mirrors of SDK types, but they reference Dropbox concepts.

| Model | Purpose | Dropbox-Relevant Fields |
|-------|---------|------------------------|
| `SyncEvent` | Tracks individual file/folder sync operations | `dbx_id`, `dbx_path`, `dbx_path_lower`, `dbx_path_from`, `rev`, `content_hash`, `change_dbid`, `symlink_target` |
| `IndexEntry` | Local index of remote state (maps to Dropbox's file tree) | `dbx_path_lower` (PK), `dbx_path_cased`, `dbx_id`, `rev`, `content_hash`, `symlink_target` |
| `HashCacheEntry` | Caches content hashes for local files | `hash_str` (Dropbox content hash format) |
| `SyncErrorEntry` | Persists sync errors | `dbx_path`, `dbx_path_lower`, `dbx_path_from`, `dbx_path_from_lower` |

**Key enums in models.py:**
- `SyncDirection`: Up / Down
- `SyncStatus`: Queued / Syncing / Done / Failed / Skipped / Aborted / Conflict
- `ItemType`: File / Folder / Unknown
- `ChangeType`: Added / Removed / Moved / Modified

**Factory methods on `SyncEvent`:**
- `from_metadata(md, sync_engine)` — converts `core.Metadata` (already converted from SDK) to a SyncEvent
- `from_file_system_event(event, sync_engine)` — converts a watchdog `FileSystemEvent` to a SyncEvent

### exceptions.py — Error Hierarchy (Dropbox-agnostic definitions)

All exceptions are **pure Python** with no SDK dependencies. The SDK → Maestral mapping happens in `errorhandling.py`.

```
MaestralApiError (base)
├── SyncError (recoverable, file-specific)
│   ├── DataCorruptionError
│   ├── DataChangedError
│   ├── InsufficientPermissionsError
│   ├── InsufficientSpaceError
│   ├── PathError
│   ├── NotFoundError
│   ├── ConflictError → FileConflictError, FolderConflictError
│   ├── IsAFolderError / NotAFolderError
│   ├── DropboxServerError
│   ├── RestrictedContentError
│   ├── UnsupportedFileError
│   ├── FileSizeError
│   ├── FileReadError
│   └── SymlinkError
├── DropboxConnectionError
├── CancelledError
├── NotLinkedError
├── InvalidDbidError
├── KeyringAccessError
├── NoDropboxDirError / CacheDirError
├── InotifyError / OutOfMemoryError
├── DatabaseError
├── DropboxAuthError → TokenExpiredError, TokenRevokedError
├── CursorResetError
├── BadInputError
├── BusyError
├── UnsupportedFileTypeForDiff
├── SharedLinkError
├── PathRootError
└── UpdateCheckError
```

---

## 4. Summary: Generic vs Dropbox-Specific

| File | Dropbox-Specific | Generic |
|------|-----------------|--------|
| **client.py** | All SDK calls, `_DropboxSDK` subclass, `convert_*` functions, OAuth flow, `get_hash()` using `DropboxContentHasher` | Bandwidth throttling, retry decorator, chunked I/O pattern, `file_was_modified()` |
| **core.py** | Data model *shapes* mirror Dropbox concepts (path_lower/path_display, rev, content_hash, etc.) | Pure dataclasses, no SDK imports, no Dropbox dependency |
| **models.py** | `SyncEvent.from_metadata()` uses core.Metadata types; fields like `rev`, `content_hash`, `dbx_id` are Dropbox concepts | ORM infrastructure, `SyncEvent.from_file_system_event()`, all enums, `IndexEntry`, `HashCacheEntry` |
| **exceptions.py** | Exception names reference Dropbox (e.g., `DropboxServerError`, `DropboxAuthError`) | All exception definitions are pure Python, no SDK imports |
| **errorhandling.py** | **Entirely Dropbox-specific**: imports all SDK error types, exhaustive mapping of ~40+ error variants | The `os_to_maestral_error()` function for local OS errors |

### Key Design Insight

The Dropbox SDK is **completely contained** within `client.py` and `errorhandling.py`. Everything above (sync engine, GUI, CLI) works exclusively with Maestral's own types from `core.py`, `models.py`, and `exceptions.py`. This means replacing the Dropbox SDK would require changes to only these two files (plus their conversion functions), not the sync logic itself.
