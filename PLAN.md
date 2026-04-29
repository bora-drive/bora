# Plan: Maestral Fork for Google Drive

This document describes how to adapt Maestral — an open-source Dropbox sync client — to
work with Google Drive instead of Dropbox.

## 1. Architecture Overview

Maestral has a clean layered architecture (~10k lines of core Python):

| Layer | Files | Lines | Dropbox Coupling |
|---|---|---|---|
| API Client | `client.py`, `errorhandling.py` | 2593 | **Total** — wraps `dropbox` SDK, converts all types |
| Data Models | `core.py`, `models.py` | 848 | **None** (SDK-free dataclasses), but field names reflect Dropbox (`dbx_path`, `rev`, `content_hash`) |
| Sync Engine | `sync.py` | 3961 | Calls `self.client.*` through a clean 15-method interface; Dropbox naming throughout |
| Orchestrator | `main.py`, `manager.py` | 2367 | Branding/naming only (`dropbox_path`, `DropboxClient`) |
| CLI | `cli/*.py` | ~1200 | Branding/naming only |
| Hashing | `utils/hashing.py` | 148 | Dropbox-specific 4MB-block SHA-256 algorithm |

The critical insight is that the Dropbox SDK is **completely contained** within `client.py`
and `errorhandling.py`. The sync engine never touches raw SDK objects — it only uses
Maestral's own dataclasses from `core.py`. This makes the backend swap architecturally
feasible.

## 2. Client Interface

The sync engine (`sync.py`) calls exactly these methods on `self.client`:

```
client.get_metadata(path, ...)        — 6 calls
client.remove(path, ...)              — 2 calls
client.make_dir(path, ...)            — 2 calls
client.list_folder_iterator(path, ...)— 2 calls
client.account_info                   — 3 calls (property)
client.wait_for_remote_changes(...)   — 1 call
client.upload(...)                    — 1 call
client.share_dir(path)                — 1 call
client.move(from, to, ...)            — 1 call
client.list_remote_changes_iterator(.)— 1 call
client.list_folder(path, ...)         — 1 call
client.is_team_space                  — 1 call (property)
client.get_account_info(dbid)         — 1 call
client.download(path, dst, ...)       — 1 call
client.config_name                    — 1 call (property)
```

This is the interface that the new Google Drive client must implement.

## 3. Concept Mapping: Dropbox → Google Drive

### 3.1 Path-Based vs ID-Based API (HARDEST CHANGE)

Dropbox's API is **path-based**: you pass `/path/to/file.txt` and get back metadata.
Google Drive's API is **ID-based**: files are identified by opaque IDs, not paths.

Additional complications:
- Google Drive allows **multiple files with the same name** in the same folder.
- A file can have **multiple parents** (appear in multiple folders).
- There is no native "get file by path" operation.

**Solution:** Build a persistent local **path↔ID mapping cache** that:
- Maps every known `remote_path` to a `file_id`.
- Is populated on first sync (full folder listing) and kept in sync via the change feed.
- Translates every path-based call from the sync engine into an ID-based Google API call.
- Translates every ID-based result from Google back into a path for the sync engine.
- Enforces unique names within a folder (reject or rename duplicates).

This cache should be stored in SQLite alongside the existing sync database.

### 3.2 Change Detection / Cursors

| Dropbox | Google Drive |
|---|---|
| `list_folder` returns a `cursor` string | `changes.list` returns a `startPageToken` / `newStartPageToken` |
| `list_folder/continue` with cursor to get incremental changes | `changes.list` with `pageToken` for incremental changes |
| `list_folder/longpoll` blocks until changes arrive (server-sent long-poll) | `changes.watch` sets up a webhook (push notification) |

The cursor-based change feed maps directly. The long-poll mechanism does not.

**Solution for `wait_for_remote_changes`:** Replace the long-poll with **periodic polling**
of `changes.list` (e.g., every 30s). This is simpler than setting up webhooks and is
sufficient for a desktop client. Webhooks can be added later as an optimization.

### 3.3 File Revisions

| Dropbox | Google Drive |
|---|---|
| `rev` — unique string per file version | `version` (integer) + `headRevisionId` |
| Used for conflict detection and `update` write mode | Used similarly; `fields` parameter must request it |

Direct mapping. Use `headRevisionId` as the `rev` field in `FileMetadata`.

### 3.4 Content Hashing

| Dropbox | Google Drive |
|---|---|
| Custom 4MB-block SHA-256 hash | `md5Checksum` provided in file metadata |
| Must be computed locally to verify downloads | Provided by the API; compute MD5 locally to compare |

**Solution:** Replace `DropboxContentHasher` with standard `hashlib.md5`. Update all
`content_hash` fields/comparisons. The `StreamHasher` wrapper class can stay — just swap
the underlying hasher.

### 3.5 Upload Mechanism

| Dropbox | Google Drive |
|---|---|
| Simple upload for files ≤ 150MB | Simple upload for files ≤ 5MB |
| Upload sessions (chunked) for larger files | Resumable uploads for larger files |
| `upload_session_start` / `append` / `finish` | Single resumable upload URI, send chunks via PUT |

Different mechanism, same concept. The `google-api-python-client` handles resumable
uploads via `MediaFileUpload` with `resumable=True`.

**Bandwidth throttling:** Both `MediaFileUpload` and `MediaIoBaseDownload` expose a
`next_chunk()` loop. Throttle by sleeping between chunks: after each chunk, compute how
long the transfer *should* have taken at the configured bytes/sec rate and sleep the
difference. Granularity is one chunk (typically 256 KB–1 MB).

### 3.6 Authentication

| Dropbox | Google Drive |
|---|---|
| OAuth2 PKCE via `DropboxOAuth2FlowNoRedirect` | OAuth2 via `google-auth-oauthlib` with `InstalledAppFlow` |
| App key embedded in client | Client ID + secret from Google Cloud Console |
| Refresh token stored in keyring | Refresh token stored in keyring (same) |

Standard OAuth2 on both sides. Scopes needed:
- `https://www.googleapis.com/auth/drive` (full access) or
- `https://www.googleapis.com/auth/drive.file` (app-created files only — too restrictive)

### 3.7 Shared Folders / Team Spaces

| Dropbox | Google Drive |
|---|---|
| Namespaces with `path_root` header | Shared Drives (separate `driveId`) |
| `is_team_space` flag | `supportsAllDrives=True` parameter |

Conceptually similar. For MVP, support only "My Drive" (personal). Add Shared Drive
support later.

### 3.8 Shared Links

| Dropbox | Google Drive |
|---|---|
| `sharing.create_shared_link_with_settings` | `permissions.create` (role=reader, type=anyone) + `webViewLink` |
| Returns URL directly | Set permission, then read `webViewLink` from file metadata |

Different API pattern but same end result.

### 3.9 Error Handling

| Dropbox SDK | Google Drive API |
|---|---|
| `dropbox.exceptions.ApiError` with typed error unions | `googleapiclient.errors.HttpError` with HTTP status codes |
| ~40+ specific error variants | Standard HTTP codes (401, 403, 404, 409, 429, 500, 503) |

Map HTTP status codes to Maestral's exception hierarchy:
- 401 → `AuthError` (token expired/revoked)
- 403 → `InsufficientPermissionsError` or `AuthError`
- 404 → `NotFoundError`
- 409 → `ConflictError`
- 429 → Rate limit (retry with backoff)
- 500/503 → `ServerError`
- Network errors → `ConnectionError`

## 4. Files to Change

### 4.1 Replace Entirely

| File | Why |
|---|---|
| `client.py` (1799 LOC) | Entire file is Dropbox SDK wrapper — rewrite as `GoogleDriveClient` |
| `errorhandling.py` (794 LOC) | Entire file maps Dropbox SDK exceptions — rewrite for Google API errors |
| `utils/hashing.py` (148 LOC) | Dropbox-specific hash algorithm — replace with MD5 |

### 4.2 Significant Modifications

| File | What Changes |
|---|---|
| `sync.py` (3961 LOC) | Rename `dbx_path` → `remote_path` throughout; adapt change feed handling; update hash comparisons; replace `rev:{event.rev}` download pattern with file-ID-based download |
| `models.py` (574 LOC) | Rename `dbx_path`/`dbx_id` fields; update `SyncEvent.from_metadata` and `from_fs_events` |
| `main.py` (1581 LOC) | Rename `dropbox_path` → `drive_path`; swap `DropboxClient` → `GoogleDriveClient`; update OAuth flow |
| `manager.py` (786 LOC) | Rename references; adapt `wait_for_remote_changes` polling; update connection monitoring |
| `core.py` (274 LOC) | Rename fields if desired; no structural changes needed |

### 4.3 Light Modifications (Branding/Naming)

| File | What Changes |
|---|---|
| `exceptions.py` | Rename `DropboxAuthError` → `AuthError`, `DropboxConnectionError` → `ConnectionError`, etc. |
| `constants.py` | Update branding constants |
| `config/main.py` | Update default config values |
| `cli/*.py` | Update help text, branding, command descriptions |
| `keyring.py` | Update keyring service names |
| `notify.py` | Update notification text |
| `autostart.py` | Update service names |

### 4.4 No Changes Needed

| File | Why |
|---|---|
| `database/` | Generic ORM, no Dropbox references |
| `fsevents/` | Local filesystem watching, backend-agnostic |
| `utils/path.py` | Path manipulation, mostly generic (one `dbx` reference in a function name) |
| `utils/caches.py` | Generic caching utilities |
| `utils/appdirs.py` | Platform directory detection |
| `utils/integration.py` | Desktop integration utilities |
| `daemon.py` | Pyro5 daemon, backend-agnostic |
| `logging.py` | Logging setup |

## 5. New Components to Build

### 5.1 Path↔ID Cache (`path_cache.py`)

New module to maintain the mapping between remote paths and Google Drive file IDs:

```python
class PathIDCache:
    def path_to_id(self, remote_path: str) -> str | None
    def id_to_path(self, file_id: str) -> str | None
    def set(self, remote_path: str, file_id: str, parent_id: str) -> None
    def remove(self, remote_path: str) -> None
    def move(self, old_path: str, new_path: str) -> None
    def populate_from_listing(self, items: list) -> None
    def resolve_path(self, file_id: str) -> str  # walk parents to build full path
```

Backed by SQLite. Must handle:
- Atomic moves (rename parent path → all children update).
- Deletion cascades (delete folder → remove all children from cache).
- Conflict resolution when Google Drive has duplicate names.

### 5.2 Google Drive Client (`client.py`)

New implementation of the client interface. Internally uses:
- `googleapiclient.discovery.build('drive', 'v3', ...)` for API calls.
- `google.oauth2.credentials.Credentials` for auth.
- `PathIDCache` for path↔ID translation.
- `MediaFileUpload` / `MediaIoBaseDownload` for file transfers.

### 5.3 MD5 Content Hasher (`utils/hashing.py`)

Simplified replacement:

```python
class ContentHasher:
    def __init__(self):
        self._hasher = hashlib.md5()
    def update(self, data: bytes) -> None:
        self._hasher.update(data)
    def hexdigest(self) -> str:
        return self._hasher.hexdigest()
```

`StreamHasher` stays largely unchanged — it just wraps a different hasher.

## 6. Dependencies

### Remove
- `dropbox>=11.28.0,<13.0`

### Add
- `google-api-python-client>=2.100.0` — Google API client
- `google-auth>=2.23.0` — Authentication
- `google-auth-oauthlib>=1.1.0` — OAuth2 flow for installed apps
- `google-auth-httplib2>=0.1.1` — HTTP transport

## 7. Google Cloud Setup

Before any code runs, a Google Cloud project is required:

1. Create a project in Google Cloud Console.
2. Enable the Google Drive API.
3. Configure the OAuth consent screen (External, with drive scope).
4. Create OAuth 2.0 credentials (Desktop application type).
5. Download `client_secret.json` and embed/distribute with the app.
6. If distributing publicly, submit for Google's OAuth verification review.

## 8. Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Path↔ID translation bugs (stale cache, race conditions) | **High** | Persist cache in SQLite; validate on every change; full rescan fallback |
| Google Drive rate limits (more aggressive than Dropbox — 12k queries/day default per-user) | **Medium** | Batch API calls; exponential backoff; request quota increase |
| Duplicate filenames in same folder | **Medium** | Detect and refuse to sync, or auto-rename with suffix |
| Files with multiple parents | **Low** | Pick first parent as canonical; log warning |
| No long-poll equivalent | **Medium** | Polling every 30s is fine for desktop; webhooks later |
| OAuth verification for public distribution | **Medium** | Apply early; use restricted scopes where possible |
| Google Docs/Sheets/Slides (not real files) | **Medium** | Skip or export as PDF/OOXML; they have no content hash or downloadable bytes |
| Symlinks (no Google Drive equivalent) | **Low** | Skip symlinks during upload; log warning. `symlink_target` field kept in schema but always `None`. |
| Selective sync exclusions go stale on folder rename while offline | **Low** | Update exclusion paths when rename arrives via change feed; user can re-exclude if missed |

## 9. Estimated Effort

| Phase | Effort | Description |
|---|---|---|
| Phase 1: Fork, rename, dependencies | 1 day | Mechanical renaming and project setup |
| Phase 2: Google Drive client + path cache | 3–5 days | Core new code (~2000 LOC) |
| Phase 3: Error handling | 1 day | Map HTTP errors to exception hierarchy |
| Phase 4: Content hashing | 0.5 day | Replace Dropbox hash with MD5 |
| Phase 5: Adapt sync engine | 2–3 days | Path renaming, change feed adaptation, polling |
| Phase 6: CLI and branding | 1 day | Update all user-facing text |
| Phase 7: Testing | 2–3 days | Port test suite, integration testing |
| **Total** | **~2 weeks MVP, ~4–6 weeks production** | |
