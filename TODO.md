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

Everything automatable in this section was completed in the 2026-07-20 sweep
(details below). What's left needs a live Google login, another OS, or a
release-time decision — see the consolidated lists at the end of this section.

### Completed 2026-07-20

- [x] **Upload bandwidth throttling.** `_throttle_up` mirrors `_throttle_down`
  between resumable-upload chunks (invoked from `writes.py`); simple ≤5 MB
  uploads are single-request and stay unthrottled by design, matching the
  download side. Offline tests for both paths.
- [x] **Sharing (Phase 5h).** Implemented via Drive permissions +
  `webViewLink`: `create_shared_link` (idempotent `anyone`-reader permission),
  `revoke_shared_link` (accepts the link URL like upstream, or a remote path),
  `list_shared_links` (`visibility='anyoneWithLink'` query), and a real
  `share_dir` (ensure folder + optional per-email role grant). Drive has no
  expiry/password on basic links — those fields are `None`/`False` (documented
  in docstrings). FakeDrive grew a permissions resource; ~20 offline tests.
- [x] **Map HTTP 409 → `ConflictError`** in `http_error_to_bora`, matching the
  404/412 branch style.
- [x] **`google_api_to_bora_error` removed** (was an unused stub).
- [x] **Selective sync: exclusion list follows remote renames/moves.** The
  engine snapshots excluded-item ids from the path cache before each change
  batch and prefix-rewrites `excluded_items` (item + children) at
  classification time; renamed/moved excluded folders stay excluded. 4 offline
  end-to-end tests (rename, reparent, non-excluded rename no-op, synced parent
  holding excluded children). Known gap: a rename processed right after a
  cursor reset or in a crash-replay window can't recover the old path (ids
  aren't persisted in config) and would start syncing — confirm acceptable in
  the live check.
- [x] **Multi-parent warning** — logged wherever multiple parents collapse to
  `parents[0]`: `path_cache.populate_from_listing` and
  `changes._emit_apply`/`_emit_shortcut`.
- [x] **Linked tests WRITTEN** (cannot run here — no account; see live list):
  >5 MB resumable upload generated at test time (proves the resumable branch),
  and `tests/linked/unit/test_resilience.py` — rate-limit retry convergence
  against the real API (+ a `slow`-marked 30-write burst), forced token expiry
  → auto-refresh → keyring persistence, network disconnect/reconnect.
- [x] **Docs.** `differences.md` symlink section rewritten from the shortcut
  code (and the adjacent native-docs section un-contradicted); real user guide
  at `docs/userguide.md` wired into `index.rst` (all CLI commands verified);
  `conf.py` author/version now read from package metadata; `bora config
  --help` key list updated (bandwidth, parallelism, export toggles).
- [x] **systemd user unit** `packaging/bora-daemon@.service` — Type=notify +
  watchdog, verified against the daemon's sd_notify support and
  `autostart.py`'s template naming; parses clean under `systemd-analyze
  verify`.
- [x] **Homebrew formula template** `packaging/homebrew/bora.rb`. Note: the
  upstream `maestral` formula no longer exists in homebrew-core (maestral is
  now cask-only), so it's modeled on the current
  `virtualenv_install_with_resources` pattern. url/sha256/resource hashes are
  clearly-marked placeholders to fill at release
  (`brew update-python-resources`); run `ruby -c` on a machine with Ruby.

### Needs a live Google login (consolidated — do together)

1. **Run the linked suites** (`tests/README.md`: link the dedicated test
   account, set `BORA_TEST_REFRESH_TOKEN`, then `pytest tests/linked` plus
   `pytest tests/linked -m slow` for the write burst). Covers the new
   resumable-upload, rate-limit, token-refresh, and disconnect tests, and the
   selective-sync integration tests.
2. **Google Docs export live spot-check** (pre-release gate): mirrors open
   correctly; a web-UI Doc edit re-exports within a poll interval; nothing is
   ever re-uploaded; `bora export markdown off` re-exports as `.docx`; verify
   the Drive `version` field is present and monotonically increments on
   native-doc edits (`modifiedTime` fallback exists but is unverified live).
3. **Sharing live verification:** real `webViewLink` URL shapes for all item
   types (esp. native docs on docs.google.com) against `_URL_ID_PATTERNS`;
   whether the `anyoneWithLink` listing returns the `permissions` field for
   all items; `permissions.create` on an unshareable item maps 403 →
   `InsufficientPermissionsError`.
4. **Selective-sync rename live check:** confirm a real `changes.list` rename
   entry arrives as modeled (single entry, new `parents`+`name`, not
   removed/trashed), and exercise a rename of an excluded folder end to end.
5. **Real-account end-to-end smoke test** (release checklist).
6. **Google OAuth app verification** — consent screen is still in Testing
   mode, so refresh tokens expire after 7 days.

### Needs another environment or a release decision

- [ ] Windows smoke test (needs a Windows machine).
- [ ] Publish to PyPI (optional).
- [ ] Finalize the Homebrew formula at release time (real url/sha256,
  regenerate resources, `ruby -c`).

---

## Google Docs Export (landed 2026-07-19)

One-way export of native Google Workspace files to Office/Markdown on download.
Config toggles `sync.export_google_docs` / `sync.export_google_docs_markdown`
(both default ON), CLI `bora export docs on|off` / `bora export markdown on|off`.

- Doc → `.md` (markdown default on) or `.docx`, Sheet → `.xlsx`, Slides → `.pptx`
  via `files().export_media`; md5 computed locally (Drive has no server hash for
  exports). Forms/Sites/Drawings/shortcuts stay skipped entirely.
- Mirrors land read-only and `download_only` in the index; local edits are never
  uploaded (a genuine local divergence is preserved as a conflicted copy).
- Server-side edits re-export via the Drive `version` change signal; the export
  leaf name is the canonical `remote_path` (projected across listing, change feed
  and path cache), so a Doc keeps one identity keyed by `remote_id`.
- Covered end-to-end by `tests/offline/test_sync.py::test_export_full_cycle_integration`.

**Remaining manual work / known gaps** (see `.superpowers/sdd/task-12-report.md`):
- [ ] Live spot-check against a real linked Google account (SKIPPED — no account
  in this environment): verify the mirrors open correctly, a web-UI Doc edit
  auto-updates the mirror within a poll interval, nothing is ever re-uploaded,
  and `bora export markdown off` re-exports as `.docx`.
- [ ] Verify empirically that the live Drive `version` field is present and
  monotonically increments on native-doc edits (design assumption replacing
  `headRevisionId`; UNVERIFIED against live Drive). The `modifiedTime` fallback
  is now implemented — `to_file_metadata` reads `version` via `.get` and
  derives the rev from `modifiedTime` when it is absent, so a missing field
  degrades gracefully instead of wedging the poll cycle. Part of the skipped
  live check.
- [x] FIXED (f15d19c, with tests): a server-side re-export of an exported
  mirror used to spawn a spurious writable conflicted copy of the prior mirror
  (exports have no server hash, so `_check_download_conflict` fell through to
  the ctime heuristic, and the read-only chmod bumps ctime after `last_sync` is
  recorded). `_check_download_conflict` now decides an *untouched* mirror the
  robust way — byte-comparing the local file against the surviving
  `download_only` index row's `content_hash` — so a re-export of an unedited
  mirror is `RemoteNewer` (no conflict copy); only a genuinely edited mirror is
  preserved.
- [ ] KNOWN LIMITATION (rebuild-with-mirrors): the fix above depends on the
  `download_only` index row (holding the export hash) surviving. After a
  `rebuild-index` / `reset_sync_state` / re-link while exported mirrors are
  already on disk, that row is gone, so an untouched mirror no longer matches
  and hits the ctime heuristic instead. Result: with export ON the mirror is
  conflict-copied and re-uploaded to Drive as a new binary (the copy keeps the
  mirror's read-only perms; upload happens regardless); with export OFF the
  mirror uploads as a new binary. First upgrade is safe — no mirrors exist yet
  when the schema-drift reset runs. Suggested future fix: byte-compare freshly
  exported bytes against the local file via the `.bora-part` sidecar and adopt
  the mirror silently on a match (no conflict copy, no upload), re-seeding the
  index row.
- [ ] FOLLOW-UP: `Manager.did_drop_table` is read only by the sync-engine DB
  owner; the path-cache (client DB) `Manager` sets it with no reader. Inert
  today (`PathCacheEntry` schema unchanged), but a future column change there
  would hit the same drop-without-reset class of problem.

---

## Known limitations / deferred

- **Shared Drives (Team Drives)** — My Drive only; `is_team_space` hardcoded
  `False`; no `driveId`/`supportsAllDrives` usage.
- **Google Docs/Sheets/Slides export** — LANDED (see "Google Docs Export"
  above); Forms/Sites/Drawings remain skipped, and the live spot-check plus
  the rebuild-with-mirrors limitation are still open.
- **Webhook change notifications** — polling only (~180 s), by design.
- **GUI packages** (`maestral-qt`/`maestral-cocoa` equivalents) — not ported;
  desktop entry references `bora gui` but there is no GUI.
