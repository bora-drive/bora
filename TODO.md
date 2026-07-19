# TODO: Bora (Maestral → Google Drive Fork)

Status as of 2026-07-19, verified against the codebase. The original phase plan
(Maestral → Google Drive port) is complete except for the items under
**Remaining work** below. The project name ended up as **bora** (not "cumulus";
that name survives only as the Google Cloud project id `cumulus-496101` in
`client_secret.json`).

---

## Completed

- [x] **Phase 1 — Setup & renaming.** Package renamed to `bora`, all
  `dbx_*`/Dropbox naming removed from `src/` (zero `dbx_` occurrences; the only
  remaining "dropbox"/"maestral" strings are explanatory comments and the
  `.dropbox` ignore-list entries in `constants.py`).
- [x] **Phase 2 — Google Cloud project.** Desktop-app OAuth client committed at
  `src/bora/resources/client_secret.json`; contributor setup documented in
  `docs/setup.md`. Consent screen is still in Testing mode (see release
  checklist).
- [x] **Phase 3 — Authentication.** OAuth flow (`get_auth_url`/`link`/`unlink`)
  via `google-auth-oauthlib`; refresh token in keyring; auto-refresh via
  `PersistingCredentials`; `linked` property. Covered by
  `tests/offline/test_oauth.py` and `test_client_auth.py`.
- [x] **Phase 4 — Path↔ID cache.** `path_cache.py` with SQLite-backed
  `PathCacheEntry`, path/id lookups, cascade remove, subtree move,
  `populate_from_listing`, duplicate-name resolution. ~40 unit tests.
- [x] **Phase 5 — Google Drive client** (except sharing and upload throttling,
  see below). `GoogleDriveClient` with retry/backoff, account ops, metadata,
  revisions/restore, MD5-verified download with throttling, simple + resumable
  upload with MD5 verify, file ops with autorename and batch variants, cursor
  and change-feed (polling), cache write-through.
- [x] **Phase 6 — Error handling** (except 409 mapping, see below). HttpError
  status/reason mapping, `RefreshError`→`AuthError`, network→`ConnectionError`,
  `convert_api_errors` kept, `os_to_bora_error`.
- [x] **Phase 7 — Content hashing.** `ContentHasher` (MD5) replaces
  `DropboxContentHasher`; post-download/upload verification against Drive's
  `md5Checksum`; hashless native-doc files handled.
- [x] **Phase 8 — Sync engine** (except selective-sync rename, see below).
  Polling change detection, id-keyed change feed, MD5 comparisons,
  download-by-file-id, removed-vs-trashed distinction, Drive cursor format,
  case-sensitivity handling.
- [x] **Phase 9 — CLI.** All `cli/` modules rebranded and ported; end-to-end
  command coverage in `tests/offline/test_cli.py`.
- [x] **Phase 10 — Manager.** New client type, polling download worker,
  Team-Space migration removed, `ensure_sync_folder_present`.
- [x] **Phase 11 — Database.** Path cache table; `remote_path`/`remote_id`
  fields throughout; fresh DB on first run (no migrations); MD5 in
  `HashCacheEntry`.
- [x] **Phase 12 — Testing.** All offline suites ported to the Drive backend
  with a stateful `FakeDrive`; linked unit + integration suites run against a
  real test account (`tests/README.md`); fault-injection suites for rate
  limits, auth, cursor, feed, downloads, writes.
- [x] **Phase 13 — Documentation** (with staleness fixes pending, see below).
  Sphinx config, `docs/setup.md`, `docs/differences.md`.
- [x] **Phase 14 — Packaging** (partial). `bora` entry point, desktop entry,
  icons, clean-venv `pip install .` verified.

### Beyond the original plan

- [x] **Shared with me** — virtual `/Shared with me` root, `read_only` index
  column, upload gating, view-only files read-only on disk.
- [x] **Symlinks ↔ Drive shortcuts** — bidirectional mapping (local symlinks
  create/retarget Drive shortcuts; remote shortcuts materialize as platform
  symlinks / `.lnk`). Supersedes the old "skip symlinks with warning" plan;
  those TODO items are obsolete and removed.
- [x] **Batch API calls** — `remove_batch`/`make_dir_batch` use real batch HTTP
  requests (originally deferred as out of scope).

---

## Remaining work

### Code

- [ ] **Enforce upload bandwidth throttling.** `bandwidth_limit_up` is stored
  and config-plumbed (`client.py`, `main.py`) but never applied — no throttle
  call in any `writes.py` upload path. Mirror `_throttle_down`
  (`client.py:795`) between resumable-upload chunks.
- [ ] **Sharing (Phase 5h).** `share_dir` is a `NotImplementedError` stub
  (`client.py:909`); `create_shared_link`, `revoke_shared_link`, and
  `list_shared_links` do not exist; nothing produces `SharedLinkMetadata`
  (`core.py:227`). Implement via Drive permissions + `webViewLink`, or
  explicitly descope and remove the stub.
- [ ] **Map HTTP 409 → `ConflictError`.** `http_error_to_bora` has no 409
  branch; conflicts fall through to generic `BoraApiError`
  (`errorhandling.py:197`).
- [ ] **Remove or implement `google_api_to_bora_error`.** Residual stub raising
  `NotImplementedError` (`errorhandling.py:333`); the real path goes through
  `http_error_to_bora`/`convert_api_errors`.
- [ ] **Selective sync: update exclusion list on remote folder rename.** A
  rename arriving via the change feed does not rewrite the old path in
  `excluded_items`, so a renamed excluded folder silently starts syncing.
  Only deletion-pruning exists (`sync.py:3393`); excluded folders are not
  indexed, so `_apply_remote_move` cannot catch them.
- [ ] **Test selective-sync exclusion across renames** (online and offline
  scenarios) once the above is implemented.
- [ ] **Log a warning when a file has multiple parents.** The cache
  canonicalizes to `parents[0]` (`path_cache.py:393`) but the planned warning
  was never added.

### Tests

- [ ] **Real resumable-upload linked test.** The fixture (`large-file.pdf`,
  ~1.8 MB) is below the 5 MB resumable threshold, so the resumable branch is
  never exercised against real Drive. Add a >5 MB fixture (or generate one in
  the test).
- [ ] **Linked tests for rate limiting, token refresh, and network
  disconnect/reconnect.** Currently covered only by offline fault injection.

### Docs

- [ ] **Fix stale symlink section in `docs/differences.md`** (~line 99): it
  says symlinks are not synced, but they now map to Drive shortcuts
  bidirectionally.
- [ ] **User guide.** `docs/setup.md` covers linking; `docs/index.rst` defers
  the user manual to a placeholder external site. Write a real guide (choosing
  the sync folder, selective sync, CLI tour).
- [ ] **Update `docs/conf.py` leftovers** — author still "Sam Schott",
  version 1.9.6.

### Packaging & release (tracked in `docs/release-checklist.md`)

- [ ] systemd service template (none exists; desktop entry and icons are done)
- [ ] Windows smoke test
- [ ] Real-account end-to-end smoke test
- [ ] Google OAuth app verification (consent screen still in Testing mode —
  refresh tokens expire after 7 days)
- [ ] Publish to PyPI (optional)
- [ ] Create a Homebrew formula, modeled on the formula for `maestral`

---

## Known limitations / deferred

- **Shared Drives (Team Drives)** — My Drive only; `is_team_space` hardcoded
  `False`; no `driveId`/`supportsAllDrives` usage.
- **Google Docs/Sheets/Slides export** — native docs are detected and skipped
  (`native_docs.py`); export as one-way read-only mirrors is spec'd
  (`docs/` design docs, commits `dc7588a`, `54292db`) but not implemented.
- **Webhook change notifications** — polling only (~180 s), by design.
- **GUI packages** (`maestral-qt`/`maestral-cocoa` equivalents) — not ported;
  desktop entry references `bora gui` but there is no GUI.
