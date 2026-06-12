# Cluster E — Review Remediation — Execution Notes & Deviations

Plan: `2026-06-10-cluster-e-review-remediation.md` (15 tasks, findings F1–F14).
Executed 2026-06-10 → 2026-06-12 via subagent-driven development (fresh
implementer per task, spec-compliance review, code-quality review, fix →
re-review loops). Per-task review state is tabulated in
`2026-06-10-cluster-e-execution-state.md`; this file records every deviation
from the plan text, the judgment calls reviewers ratified, and the accepted
residuals.

## Plan flaws found and fixed during execution

- **Task 4 (F6) design gap:** the plan's batch-then-classify sketch emitted
  move-out DELETEs only after the apply fixpoint stalled, so a file created at
  a just-vacated path was consumed-with-warning and never synced. Fixed beyond
  the plan in `4b71319`: rejected applies defer (not consume), an outer loop
  alternates fixpoint and move-out passes until no progress, and the conflict
  warning fires exactly once at final drop.
- **Task 6 (F7) broken sketch:** the plan's `except HttpError` around the
  create sites can never fire — `convert_api_errors` converts every HttpError
  first. Implemented instead by inspecting `exc.__cause__` on the converted
  `CumulusApiError` (works because `http_error_to_cumulus` chains with
  `raise … from`), with a why-comment at each of the three sites; the batch
  callback sees raw HttpError and checks it directly.
- **Task 8 (F5) ordering flaw:** the plan's sketch ran `reset_sync_state()`
  before `stop()`; `stop()` clears `autostart`, so the sketch's resume signal
  would be lost (and reset could race a worker holding `sync_lock`).
  Implemented stop → reset → `autostart.set()`, with a try/finally
  guaranteeing autostart even if reset raises.
- **Task 8 scope narrowing:** plan said map HTTP "400, 404" to
  CursorResetError; implemented 400-only — `convert_api_errors` maps 404 to
  `NotFoundError` and a 404 from `changes.list` is not a token-expiry signal.
- **Task 3 (F3) flagged edge adjudicated FIX:** when the move target was
  already occupied, the plan sketch's re-key silently adopted the occupant
  into the moved file's sync identity (equal revs → no conflicted copy → next
  scan overwrites remote + resurrects the old path; realistic via
  offline-reconnect). `c4de428` makes occupied-target an early return — the
  rule is now: re-key iff the old path never had the item OR the disk move
  succeeded.
- **Task 14 (F14):** kept `errorhandling.py`'s "daily quota reached" mapping —
  `dailyLimitExceeded` is a genuine server-side per-project cap, distinct from
  the fictitious client-side per-day budget that was removed.
- **Final gate caught a real bug:** Task 8's rebuild notification used
  `notify.INFO`, which the notify module never defined — AttributeError inside
  the error handler whenever a desktop notifier is attached. `a5e2037` adds a
  proper `INFO = 20` level (upstream has no INFO level and no cursor-reset
  notification; this is a port invention) and strengthens the manager test
  with a mock notifier that would have caught it.

## Implementer deviations from plan sketches (reviewed and ratified)

- **Task 3:** `_ensure_parent` before the disk move (reparent can arrive
  before its new parent exists; `utils/path.move` no-ops on a missing
  destination parent); re-key aborted when an attempted move failed;
  `index=True` added to `models.remote_id` (per-item lookups were full-table
  scans; `CREATE INDEX IF NOT EXISTS` auto-migrates existing DBs on open);
  `startswith` guard in the child re-key loop (parity with
  `_apply_case_change`).
- **Task 5 (F4):** `_audit_update_rev` paginates `revisions.list` instead of
  the plan's single `pageSize=1000` call (the new head is the LAST revision;
  ignoring `nextPageToken` would silently disable the audit on >1000-revision
  files); `_rescue_revision` reuses `_download_revision_to_tempfile` (shared
  with `restore()`); conflicted-copy naming matches sync.py's ownerless
  `"{stem} (conflicted copy {date}){ext}"`, built decode → suffix →
  `compose_path` (the plan's construction produced non-canonical
  percent-encoded names); audit/rescue failures are contained
  (`except (CumulusApiError, OSError)` → warning) so a successful upload is
  never marked failed; **coalescing guard** `new_head == expected_prev`
  prevents spurious conflicted copies when Drive folds rapid same-user
  updates into the same head revision.
- **Task 6:** id reserved BEFORE the request object is built so a pure
  transport retry reuses the same id; pool cleared on `unlink()` (ids from
  account A must not leak into a relinked account B); fake gained
  `fail_after` (finalize-then-raise) modelling committed-but-response-lost.
- **Task 7 (F11):** probe runs before `reserve_file_id` (a conflict burns no
  pooled id); on adoption rejection (stale different-id cache entry) the
  stale entry is evicted and re-set — the probe result is fresher than the
  cache; conflict message made caller-neutral.
- **Task 9 (F9):** eager re-download after a delete conflict (upstream
  resurrects lazily via the next cycle; eager is strictly more robust on
  Drive — ratified as intentional divergence); warning logged when the eager
  redownload fails (its recorded Down error is wiped by the directionless
  error-clear, leaving no other trace).
- **Task 10 (F8) audit outcomes** (`normalize()` → canonical forms):
  `_handle_sync_error` ×2 and `_should_split_excluded` ×2 → `fold_path`
  (operands are encoded virtual paths); main.py `excluded_status` →
  `fold_human` (user input); `include_items`/`exclude_items` input sets →
  `fold_human`; membership queries →
  `is_excluded_by_user(fold_path(encode_path(p)))`; `_remove_after_excluded`
  housekeeping keys → `fold_path(encode_path(entry))` (identity
  `fold_path(encode_path(fold_human(p))) == fold_path(encode_path(p))`
  verified, the outer fold_path load-bearing for trailing-dot names);
  metadata-derived insertions decoded; outbound `list_folder_iterator` /
  `download_queue.put` encoded; the `excluded_items` setter fixed the same
  way; `reload_cached_config` re-cleans stale normalize-form config entries
  at init (ß/ς/ligature casefold-divergence classes). Local-filesystem
  `normalize()` uses left alone. The first spec review caught a regression in
  the initial audit (include_items would have silently failed for
  casefold-divergent and literal-%XX names) — fixed in `b692861`.
- **Task 13 (F13):** Stage-2 spec §5.1 "access-token link is functionally not
  linked" superseded — recorded in
  `2026-05-12-stage-2-auth-onboarding-deviations.md`. The relaxation is
  in-process only: a persisted token-only blob fails
  `Credentials.from_authorized_user_info` and loads as None, so `linked` is
  False after restart (double-layered: the storage path also requires a
  refresh token).

## Accepted residuals (known, deliberate, not fixed in this cluster)

- **Task 3:** occupied-target fallback leaves the stale old-path copy and its
  index row until the next local scan; duplicate `remote_id` rows possible
  transiently. Optional follow-up: conflicted-copy the occupant inside
  `_apply_remote_move`. `get_index_entry_by_remote_id` returns `rows[0]`
  nondeterministically under duplicates. `osp.exists` (not `lexists`) misses
  broken symlinks. ORM row cache is mutated before transaction commit →
  phantom cached row on rollback (pre-existing pattern, shared with
  `_apply_case_change`).
- **Task 4:** the drained `raw` change buffer is unbounded — months-offline
  batches can reach hundreds of MB (cheap follow-up: cap + fall back to the
  full-reindex path). Crash mid-apply of a huge backlog redoes the whole
  batch (idempotent, slow) — inherent to cursor-after-classification.
  `PathIDCache.set/move` log their own "Case conflict" warning on every
  rejected attempt, so deferred conflicted applies produce O(fixpoint passes)
  duplicate cache-level warnings (the changes-level warning fires exactly
  once). Cursor `""` when both tokens are absent → full reindex
  (pre-existing, safe).
- **Task 5:** residual lossless false positive if `expected_prev` is purged
  between commit and audit (spurious conflicted copy). `revisions.list`
  oldest-first ordering is observed-not-documented API behavior. Only the
  newest pre-head foreign revision is rescued. Drive server-side merging of
  rapid same-user revisions is undetectable by any history audit (note for
  Stage 7b fault injection). `_download_revision_to_tempfile` leaks its temp
  file on mid-download failure (pre-existing, shared with `restore()`).
- **Task 6:** a cross-invocation engine retry can still duplicate (create
  committed but `upload()` failed permanently; the next engine retry reserves
  a fresh id — the cache-based existence check never learned of the committed
  file; proper fix = engine-level retry semantics). The 409-resolve
  `files().get` is not `_count_request`ed and doesn't request `trashed`.
  `fail_after` is wired into `files.create` only (generalize for Stage 7b).
- **Task 7:** probe→create race (no atomic name check-and-create on Drive).
  Probe-hit then remote-trash before the merge `get_metadata` → Skipped;
  local folder unsynced until feed/rescan (ms window, mirrors upstream
  structure). Multiple same-name siblings: `pageSize=1` merges with an
  arbitrary one. `path_cache.set` logs a misleading "Case conflict" warning
  before the eviction self-heals.
- **Task 8:** a persistent-400 bug class (e.g. malformed fields constant)
  now loops re-index → 400 → re-index with notification spam instead of a
  visible stop (a SECOND 400 after the bootstrap's fresh `getStartPageToken`
  requires a code bug, not staleness; offline tests exercise that class;
  timestamp circuit-breaker = optional follow-up). Full `reset_sync_state`
  during the dead-token gap resurrects remote deletions instead of
  propagating them — upstream's deliberate data-safe rebuild semantics.
  Mid-pagination 400 on a fresh `nextPageToken` is also labelled "offline too
  long" (bug-class-only; the recovery is still sane).
- **Task 9:** failed-redownload Down error is wiped by the directionless
  `clear_sync_errors_from_event` (upstream-inherited; the new warning is the
  only trace; self-heals next cycle). Folder-delete asymmetry:
  `parent_rev=None` → remotely-edited children silently trashed (upstream
  parity). Dead `except FileConflictError` in `_on_local_folder_created`
  (confirmed dead, left in place — outside Task 9's two named sites).
  Upstream's own `PathError` clause was likely dead too (provenance note).
- **Task 10:** **`get_metadata` fabricates `path_display` by echoing its
  input path** (client.py) — a newly included single file or empty folder
  materializes locally under the casefolded name (`strasse` not `Straße`);
  pre-existing defect class; one-line fix sketch: recover the cased path via
  `path_cache.id_to_path(file_id)`. Mignore matching stays CASE-SENSITIVE
  (upstream parity, deliberate, pinned by test). Stale encoded-form
  `excluded_items` entries written by the old include_items bug remain dead
  config debris (not migrated). `fold_human` duplicates `fold_key`'s fold
  body; `fold_path(encode_path(…))` appears 4× in main.py (extract/name the
  composition if touched again). Persisted download queue may hold old
  decoded-form entries across the upgrade; literal-% ones self-heal as
  fake-deleted.
- **Task 11 (F10):** RPC `list_folder("/", recursive=True)` would run
  `populate_from_listing` on a Pyro thread outside `sync_lock` — internally
  safe now, but it can clobber engine write-through with a listing snapshot
  (no shipped caller does this). Bootstrap clear+insert holds the cache lock
  for seconds at 100k items (RPC reads block during "Indexing"; temp-table
  swap = future optimization). SyncEngine opens a second sqlite connection to
  the same .db file as `_client_db` (pre-existing Stage-5 consolidation
  TODO).
- **Task 13:** relink-with-access-token overwrites a stored refresh-token
  blob in the keyring (pre-existing; the unparseable blob is re-read and
  discarded by every cold `linked` call — wasteful, correct). Token expiry of
  a short-lived session reports "revoked" rather than "expired" (all
  RefreshErrors map to TokenRevokedError; cosmetic).
- **Tasks 1–2 carry-overs:** duplicate warnings on fold-conflict (changes.py
  + path_cache level — consider demoting the cache-level one to debug); the
  "rename one of the duplicates… to sync both" warning text is optimistic
  (renaming the WINNER won't resurrect the loser until the loser itself
  changes — cursor already consumed). Non-recursive `list_folder` branch
  still yields fold-conflicted entries (pre-existing; doesn't claim
  write-through; deliberate).

## Lint/type gate

- flake8 (configured ignores): 34 findings at HEAD — byte-identical to the
  pre-cluster-E baseline (zero new).
- mypy --strict: 21 errors at HEAD vs 21 at baseline (all pre-existing; the
  3 cluster-E-introduced errors — `notify.INFO`, two uninferable lambdas —
  were fixed in `a5e2037`).
- black/isort: not gated (matching prior stages); one known >88-col line
  noted during Task 5 review.

## FakeDrive extensions added for this cluster

`on_next_update` one-shot hook (rev-race injection); per-revision content
snapshots + `revisions.get_media` returning the requested revision (404 on
unknown); `files.create` honoring `body["id"]` with 409 on duplicate;
`fail_after(key, exc)` finalize-then-raise hook (files.create only);
`_match_query` support for `name = '…'` / `mimeType = '…'` fragments
(escape-aware). Known fake limitation: the q parser splits on `" and "`, so
names containing that literal substring mis-parse in the fake only (loudly).
