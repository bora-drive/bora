# TODO: Bora (Maestral → Google Drive Fork)

Status as of 2026-07-19, verified against the codebase. The original phase plan
(Maestral → Google Drive port) is complete except for the items under
**Remaining work** below. The project name ended up as **bora** (not "cumulus";
since 2026-07-20 the bundled OAuth client in `client_secret.json` also lives in
a Google Cloud project named `bora-500821`, replacing the original
`cumulus-496101`).

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

Everything automatable in this section was completed in the 2026-07-20 sweep,
and the live-login checklist was run the same day against
`bora-test@arko.net` (details below). What's left: Google OAuth app
verification (needs the Cloud Console), the sharing 403 sub-check (needs a
second account), another OS, or a release-time decision — see the
consolidated lists at the end of this section.

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

1. [x] **Run the linked suites** — DONE 2026-07-20 against
   `bora-test@arko.net` (new `bora-500821` OAuth client): all 90 linked tests
   pass, each verified in an isolated single-test pytest run (`-m slow` burst
   included). Live testing surfaced and fixed three real bugs: `sharedWithMe`
   is not a selectable Drive v3 field (projections now use
   `sharedWithMeTime`), the local `/Shared with me` mount dir was uploaded as
   a real folder by local indexing, and the while-inactive scan synthesized a
   spurious create event for the mount on every restart.
2. [x] **Google Docs export live spot-check** — DONE 2026-07-20 (see the
   "Google Docs Export" section below for details): mirrors open, edits
   re-export within a poll interval with no conflicted copy, nothing is ever
   re-uploaded, Drive `version` verified present and monotonic. One gap
   found, recorded below: the `bora export markdown off` flip only takes
   effect after a daemon restart, and an existing unchanged mirror is not
   re-exported until the Doc next changes.
3. [x] **Sharing live verification** — DONE 2026-07-20 (probe script:
   `$CLAUDE_JOB_DIR/tmp/sharing_probe.py` pattern): `webViewLink` shapes for
   regular file, folder, Doc, Sheet and Slides all parse back to the correct
   id via `_URL_ID_PATTERNS`; the account-wide `anyoneWithLink` listing
   returned every shared item; `create_shared_link` is idempotent and
   `revoke_shared_link` works by URL for both binary files and native docs.
   STILL OPEN: the `permissions.create` 403 →
   `InsufficientPermissionsError` mapping needs an item the test account
   cannot share — requires a second account to share something read-only
   into `bora-test@arko.net` first (the account currently has no
   shared-with-me items).
4. [x] **Selective-sync rename live check** — DONE 2026-07-20: a real
   `changes.list` rename arrives exactly as modeled (single entry carrying
   the new `name` + `parents`, `removed=False`, `file.trashed=False`), and
   an end-to-end rename of an excluded folder (`bora excluded add` →
   remote rename → poll) rewrote `excluded_items` to the new path with no
   local re-download.
5. [x] **Real-account end-to-end smoke test** — DONE 2026-07-20 (config
   `smoke-test`, sync dir `~/BoraSmoke`): link via refresh token, local tree
   uploaded (inotify path), remote-born file downloaded within a poll cycle,
   `bora status` / `bora auth status` show email/type/usage, 0 sync errors.
   Found and fixed a real CLI bug: piped (non-tty) `bora ls` printed nothing
   because it re-iterated an exhausted `list_folder_iterator` (regression
   test added in `tests/offline/test_cli.py`).
6. **Google OAuth app verification** — consent screen is still in Testing
   mode, so refresh tokens expire after 7 days.

Minor observation from the live runs (display-level, not a sync defect):
`bora ls` showed native docs by their raw Drive `name` (`live-doc`) while the
canonical `path_display` and the on-disk mirror carry the projected leaf
(`live-doc.md`) — `to_file_metadata` intentionally leaves `name` unprojected.
Decision taken: `ls` should display the projected leaf. Implemented via
`_display_leaf()` in `src/bora/cli/cli_info.py`, covering the long-format
table, piped output, and the tty grid.

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
- [x] Live spot-check — DONE 2026-07-20 against `bora-test@arko.net`:
  mirrors open correctly (`.md` byte-exact, `.xlsx`/`.pptx` valid Office
  zips, all read-only on disk); an API-level Doc edit (media import — the
  Drive-API equivalent of a web-UI edit) re-exported the mirror within one
  poll interval with NO conflicted copy (the f15d19c untouched-mirror fix
  holds live); nothing was ever re-uploaded (the Doc's `modifiedTime` stayed
  at the edit's own timestamp and no mirror-named binaries appeared in
  Drive).
- [x] Drive `version` field — VERIFIED live 2026-07-20: present on every
  native doc from creation and strictly monotonic across edits (observed
  4→6→7→9→11 on one Doc). Note: Drive also bumps `version` server-side
  without user-visible edits (e.g. right after creation and after import
  processing), so the engine may occasionally see a phantom "change" and
  re-export an unchanged mirror — harmless (no upload, idempotent
  re-export). The `modifiedTime` fallback remains untested live because
  `version` is in practice always present.
- [x] RESOLVED (c036c17): the `export_google_docs`/`export_google_docs_markdown`
  setters in `main.py` now call `sync.reload_cached_config()`, so a toggle
  flip reaches the running engine without a daemon restart. Forcing
  re-export of an already-unchanged mirror is still not automatic (an
  unchanged Doc produces no change entry), so the mirror converts on the
  Doc's next real edit, or immediately via `bora rebuild-index` — the CLI
  now prints a hint to that effect. Live spot-check (plan Task 13) is still
  pending before release.
- [x] FIXED (f15d19c, with tests): a server-side re-export of an exported
  mirror used to spawn a spurious writable conflicted copy of the prior mirror
  (exports have no server hash, so `_check_download_conflict` fell through to
  the ctime heuristic, and the read-only chmod bumps ctime after `last_sync` is
  recorded). `_check_download_conflict` now decides an *untouched* mirror the
  robust way — byte-comparing the local file against the surviving
  `download_only` index row's `content_hash` — so a re-export of an unedited
  mirror is `RemoteNewer` (no conflict copy); only a genuinely edited mirror is
  preserved.
- [x] RESOLVED (255bf37): added `_adopt_untouched_mirror` as a fallback in
  `_on_remote_file`'s post-download re-check — it byte-compares the freshly
  downloaded export bytes directly against the local mirror, with no
  `download_only` index row required, and adopts silently on a match (no
  conflict copy, no re-upload). Nuance for the record: offline analysis
  during implementation found the original "rebuild-with-mirrors" limitation
  above was overstated for byte-stable exports — the post-download conflict
  re-check already sees a locally computed export md5, so untouched `.md`
  mirrors were already resolved as `Identical` even before this fix. This
  change makes the recovery explicit and independent of hash-cache/index
  incidentals, rather than "fixing" a break that fully existed. The residual,
  still-unverified risk is byte-UNSTABLE re-exports (`.xlsx`/`.pptx` are zip
  containers that may embed timestamps) — that is exactly what the live
  spot-check (plan Task 13) is meant to measure, and it has not run yet.
- [x] RESOLVED: `Manager.did_drop_table` for the path cache now has a reader.
  Verified the path cache has no lazy repopulation on a lookup miss (Drive's
  API is ID-based — no path-lookup endpoint to fall back to — so a miss
  raises `NotFoundError`, and change-feed write-through only repopulates an
  entry once its ancestor chain is already cached), so a bare log line would
  have been wrong: `PathIDCache.did_drop_table` is exposed as a public
  property and folded into `SyncEngine._create_or_load_db`'s
  `dropped_on_drift` check, pairing a path-cache-only schema drift with the
  same sync-state reset (and resulting bootstrap full listing) as the
  engine's own derived-cache tables.

---

## Known limitations / deferred

- **Shared Drives (Team Drives)** — My Drive only; `is_team_space` hardcoded
  `False`; no `driveId`/`supportsAllDrives` usage.
- **Google Docs/Sheets/Slides export** — LANDED and live-verified (see
  "Google Docs Export" above); Forms/Sites/Drawings remain skipped. The
  export-toggle-flip gap and the rebuild-with-mirrors limitation are both
  resolved in code (c036c17, 255bf37); a live spot-check of the fixes,
  including byte-unstable `.xlsx`/`.pptx` re-exports, is still pending
  (plan Task 13).
- **Webhook change notifications** — polling only (~180 s), by design.
- **GUI packages** (`maestral-qt`/`maestral-cocoa` equivalents) — not ported;
  desktop entry references `bora gui` but there is no GUI.
