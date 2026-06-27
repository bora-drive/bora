# Stage 7c — Deferred Backlog (consolidated)

**Date opened:** 2026-06-15
**Branch:** `google-drive-port`
**Status:** OPEN — this is the single tracking home for work punted out of
Stages 7a/7b. It replaces the previously-unenumerated "7b-deferred" reference in
the 7b-E2/E3 design specs.

## Why this doc exists

The 7b-E2 and 7b-E3 design specs name a third sub-stage — *"7b-deferred — the
cluster-E items punted to 7b (future spec)"* — but no document ever enumerated
what it contains. The concrete items were scattered across the E2 deviations
file (three `strict=True` xfails), the cluster-E deviations/execution-state docs
(accepted residuals), and the Stage 0 spike report (unrun scenarios). This spec
consolidates them so nothing is lost when the work is picked up.

Renamed **7b-deferred → Stage 7c** for a clean spec→plan→execute cycle, parallel
to 7a/7b. Stage 8 (docs + release, naming sweep, `DropboxClient` alias removal)
remains separate and is explicitly **out of scope** here (see §5).

## Priority key

- **P0** — confirmed production defects with live `strict=True` xfail guards.
  These are the reason 7c exists; the user deferred them (2026-06-14) to a
  dedicated post-gate TDD+review fix cycle.
- **P1** — unvalidated assumptions / loose ends that currently have no home.
- **P2** — accepted residuals worth triaging (fix vs. document-forever).

---

## 1. P0 — Confirmed production defects (the core of 7c)

All three were independently adversarially verified by code-trace (not test
artifacts) during 7b-E2 and captured as `@pytest.mark.xfail(strict=True)`
regression guards. Each guard fails CI the moment the defect is fixed, forcing
the mark's removal. Full detail in
`docs/superpowers/plans/2026-06-14-stage-7b-e2-fault-injection-deviations.md`
(Findings B, C, persistent-400).

### 7c-1 — Finding B: occupied-target folder move creates a duplicate `remote_id` row
- **Guard:** `tests/offline/test_fault_divergence.py::test_occupied_target_folder_move_creates_duplicate_id_row` (xfail strict).
- **Mechanism:** when a remote folder is moved onto a path occupied by an
  *untracked* local folder, `_apply_remote_move` correctly bails, but
  `_check_download_conflict` then compares `content_hash "folder" == "folder"`
  and returns `Conflict.Identical` without checking whether an index row exists
  at the event path. `update_index_from_sync_event` then inserts a **second**
  index row with the same `remote_id` (stale orphan at the old path + new row at
  the new path); the occupant is silently adopted instead of conflict-copied,
  contradicting `_apply_remote_move`'s own docstring. This is a **normal sync
  operation** — no fault injection needed.
- **Proposed fix:** in `_check_download_conflict`, an existing local folder with
  **no** index row at the event path must return `Conflict.Conflict`
  (conflict-copy), not `Identical` — consistent with how a file occupant is
  handled.
- **Severity:** data-integrity (silent adoption + index divergence).

### 7c-2 — Finding C: `get_index_entry_by_remote_id` has no uniqueness guard
- **Guard:** `tests/offline/test_fault_divergence.py::test_duplicate_remote_id_rows_no_dangling_stale_row` (xfail strict). (`..._no_crash` already passes.)
- **Mechanism:** no `UNIQUE` constraint on `IndexEntry.remote_id`
  (`models.py:491`, `index=True` only). `get_index_entry_by_remote_id`
  (`sync.py:920`) does `rows[0]` (first-wins, no `ORDER BY`). After a move,
  `_apply_remote_move` re-keys only the selected row, leaving the duplicate as a
  **stale orphan** that a later lookup can return — breaking subsequent moves and
  causing a spurious local delete on the next scan.
- **B feeds C:** Finding B is the operational path that *creates* the duplicate
  `remote_id` rows in normal operation, making C reachable. Fix B and C together
  for full closure.
- **Proposed fix:** add a schema `UNIQUE` constraint on `remote_id`. Low-cost —
  the index is a derived cache rebuilt on version bump, so there is no migration
  framework to satisfy; it converts silent corruption into a detectable error.

### 7c-3 — persistent-400: unbounded reindex spin on a permanently-400 change feed
- **Guard:** `tests/offline/test_fault_cursor.py::test_scenario_b_persistent_400_is_bounded` (xfail strict).
- **Mechanism:** `CursorResetError` (from `changes.list` → 400) is caught only at
  `manager.py:561` (`_handle_sync_thread_errors`), which does `stop()` +
  `sync.reset_sync_state()` (cursor → `""`) + `autostart.set()`;
  `connection_monitor` then restarts. If the feed 400s for *every* token
  (including fresh ones from bootstrap — e.g. a malformed `fields` constant), the
  manager loops bootstrap → fresh cursor → 400 → reset → restart forever with
  **no circuit-breaker / max-attempts / backoff** in `manager.py`. Steady state
  is an infinite slow spin (~10s period), never terminating, with notification
  spam.
- **Proposed fix (small, ~10–15 lines):** a cross-restart counter on
  `SyncManager` (instance var), incremented in the `except CursorResetError`
  branch, that gives up after N (stop without re-autostart + surface a visible
  error) and resets on a healthy cycle. Not an architectural change.
- **Test note:** the guard *simulates* the manager restart cycle (bounded loop
  over `reset_sync_state` + `download_sync_cycle`) rather than running the real
  thread loop; the simulation was verified faithful to the real
  catch→reset→restart→400 flow. The fix should keep the test honest (ideally
  exercise the real `_handle_sync_thread_errors` counter).

> **Execution note:** removing each `xfail(strict=True)` mark as its defect is
> fixed is part of the definition-of-done — a strict xfail XPASSes (fails CI) the
> moment the bug is gone.

---

## 2. P1 — Unvalidated assumptions / loose ends with no home

### 7c-4 — `root_id` not persisted across daemon restarts — **RESOLVED 2026-06-27: accept the per-start resolve (do NOT persist); cross-stage promise CLOSED**
- Stage 3 §2.2 **and** Stage 4 §2.2 both deferred "persisting `root_id` across
  daemon restarts" to "Stage 6 (DB schema)." Stage 6 never picked it up, and no
  later stage did. Still in-memory only: `_ensure_root_id` resolves
  `files.get(fileId='root')` and caches on `PathIDCache.root_id`
  (`client.py:428`, `path_cache.py:114`), with no config/DB column.
- **Decision (P1, 2026-06-27): keep the per-start resolve; do not persist. No
  code change.** Rationale:
  - **Cost is negligible.** `_ensure_root_id` is idempotent — exactly one
    `files.get(fileId='root', fields='id')` per daemon lifetime, behind
    `_retry_transient`, then a GIL-atomic cached read for every subsequent call.
  - **Persisting introduces a real correctness hazard for ~zero benefit.** A
    persisted `root_id` (especially a config key, which survives the
    version-bump `PathIDCache` rebuild) would go stale if the user unlinks and
    relinks a *different* Google account — operations would target the old
    account's root. The in-memory resolve is immune: it is re-derived fresh from
    the current credentials on every start, so it always matches the linked
    account.
  - The `PathIDCache` is a derived cache rebuilt on version bump, so even a
    DB-column persistence would be discarded on rebuild (re-resolving anyway) —
    extra surface area with no durability guarantee.
  - Offline-start is a non-issue: the daemon can do nothing offline regardless,
    and `_ensure_root_id` resolves once connectivity returns (handled by
    `_retry_transient` + `connection_monitor`).
- **Action taken:** the dangling Stage 3/4 → Stage 6 "persist root_id" promise is
  formally closed as "won't do (accepted)." Item complete.

### 7c-5 — Spike Q3: unshare scenario — **VALIDATED LIVE 2026-06-27 ✅ (assumption confirmed)**
- **Live result (2026-06-27, cumulus-test@arko.net + a 2nd account):** shared a
  file into the test account (appeared in the change feed as
  `removed=False trashed=False shared=True` → cumulus UPSERT/apply, but dropped
  as out-of-scope shared-with-me since its parent isn't under root), then
  unshared it. The unshare produced **exactly** `removed=True, file=None` — the
  same shape as a hard-delete. cumulus's `classify` routes it through the
  delete path (`changes.py:353` → `_emit_delete`). **The spike's working
  assumption ("treat unshare like hard-delete, removed=true, no file body") is
  confirmed against real Drive.** Item complete. (Original notes retained below.)
- Stage 0 spike report (`spikes/2026-04-30-...-report.md`, Known gaps) left the
  unshare-of-a-shared-item scenario unrun (needs a second Google account). The
  working assumption is "treat unshare like hard-delete (`removed=true`, no file
  body)." Never re-validated in Stage 3 or later.
- **Offline code-trace (2026-06-27):** the engine side of the assumption is
  sound. `changes.py` `classify` emits a delete whenever a change carries
  `removed=true` **or** the file is `trashed` (`changes.py:141` and the
  `iter_remote_changes` path at `changes.py:353`); `_emit_delete` removes the
  indexed item by path/remote_id. So **IF** Drive signals an unshare as
  `removed=true` in the change feed, the engine already does the right thing —
  this is the same well-tested removal path used by hard-delete/trash (and the
  cluster-E finding-2 crash-replay work). No engine change needed for the
  assumed event shape.
- **Genuinely live-only residual:** whether Drive actually emits `removed=true`
  on an unshare of a shared-with-you item (vs. leaving a stale, now-inaccessible
  entry, or a permission-only change). That is unanswerable offline and needs a
  2-account session.
- **Action (live):** with a 2-account setup, share an item to the test account,
  let it sync, then unshare and confirm the change feed delivers `removed=true`
  (engine then deletes locally — already covered). If Drive instead leaves a
  stale entry, open a follow-up to detect lost-access. Batch with 7c-6 in the
  next linked session.

### 7c-6 — Spike Q1: headless paste-redirect OAuth — **VALIDATED LIVE 2026-06-27 ✅**
- **Live result (2026-06-27):** ran the real paste-redirect round-trip against
  cumulus-test@arko.net — generated the auth URL via `make_paste_flow`, approved
  in a browser, pasted the failed `http://localhost:8080/?code=...` redirect URL
  back, and exchanged the code via `fetch_token(code=...)`. **It succeeded
  end-to-end** (real refresh token obtained, account confirmed as the dedicated
  test account). Critically, the exchange ran **with `OAUTHLIB_INSECURE_TRANSPORT`
  explicitly UNSET** and did **not** raise `InsecureTransportError` — confirming
  the offline finding live: the in-process paste flow is not blocked. The
  spike's "broken-as-shipped" concern is conclusively retired. The robustness
  note below (env var only in `daemon.py`) stands as a latent future-dep caveat,
  not a current bug. Item complete. (Original notes retained below.)
- The spike flagged the Mode-B paste-redirect path as "broken-as-shipped due to
  `InsecureTransportError`; Stage 2 must address this before claiming headless
  support." Stage 2 set `OAUTHLIB_INSECURE_TRANSPORT=1` (`daemon.py:32`), but no
  doc confirmed the headless paste-redirect flow working end-to-end.
- **Offline investigation (2026-06-27):**
  - The flow is fully wired: `get_auth_url` + `_PasteFlowState` (`client.py:282`),
    `make_paste_flow` (`oauth.py:186`, `redirect_uri=http://localhost:8080`),
    `_parse_redirect_url_or_code` (CLI extracts the bare code), and the
    `OAuth2Error` catch on the exchange (`client.py:338`, cluster-E F13).
  - **The spike's blocker is not reproducible in the current code + deps.** With
    `OAUTHLIB_INSECURE_TRANSPORT` explicitly **unset**, both
    `flow.fetch_token(code=...)` (the path the paste flow uses — it extracts the
    code, not the http URL) **and** `fetch_token(authorization_response="http://localhost...")`
    reach Google's HTTPS token endpoint and fail only on the fake code
    (`InvalidGrantError`) — no `InsecureTransportError`. The code-extraction
    design sidesteps the http-response validation, and the installed oauthlib
    does not reject the http loopback redirect. So the "broken-as-shipped" risk
    is effectively gone.
  - **Robustness note (latent, not currently biting):** `OAUTHLIB_INSECURE_TRANSPORT`
    is set only in `daemon.py`; the in-process CLI link path (`auth_link` uses
    `@inject_proxy(fallback=True)`, so first-link runs in-process with no daemon)
    does **not** import `daemon.py` and does **not** set the env var. It works
    today only because the current oauthlib doesn't enforce the http-loopback
    check. If a future oauthlib version reintroduces strict checking, set
    `OAUTHLIB_INSECURE_TRANSPORT` in `oauth.py` (imported by both daemon and CLI
    link paths) rather than only `daemon.py`. Cheap one-line defensive fix;
    deferred (no failing test today — adding it now would be speculative).
- **Action (live, downgraded to nice-to-have):** confirm a real paste-redirect
  link round-trip on a port-forward-less host once a live session is set up. The
  specific failure mode that made it "broken" is no longer present.

---

## 3. P2 — Accepted residuals worth triaging

These are deliberately-accepted residuals from cluster-E / 7b. Listed here so a
single place distinguishes "accepted forever" from "fold into a fix." None are
blocking.

### 7c-7 — `get_metadata` fabricates `path_display` from the casefolded input
- `get_metadata` echoes its input path into `path_display` (`client.py:668`), so
  a newly-included single file / empty folder materializes locally under the
  **casefolded** name (`strasse`, not `Straße`). Pre-existing defect class.
- One-line fix sketch: recover the cased path via `path_cache.id_to_path(file_id)`.

### 7c-8 — `_download_revision_to_tempfile` leaks its temp file on mid-download failure
- On a mid-download failure the temp file is not cleaned up (pre-existing; shared
  with `restore()`). Candidate for a `try/finally` or context-managed temp path.

### 7c-9 — `remote_path_lower` column name holds a *fold* value — **TRIAGED 2026-06-27: accept the `_lower` name (won't-rename); no schema bump in flight**
- `IndexEntry.remote_path_lower` (`models.py`) and `SyncEvent.remote_path_lower`
  are populated with a casefold key via `fold_path()`, but Stage 1 deliberately
  renamed the **cache** column `remote_path_lower → remote_path_fold` to avoid
  exactly this confusion. The index-side models kept the old `_lower` name.
  Stage 6 §1 blessed `_lower` for the models, so the two specs are internally
  consistent — but the rename rationale was not applied uniformly. Pure
  naming/readability; rename only if a model-schema bump is being made for
  another reason (the index is a derived cache, so no data migration is needed).
- **Decision (P2, 2026-06-27): accept `_lower` as-is. No code change.** The 7c-P2
  pass (7c-7 `client.py`, 7c-8 `writes.py`) made **no** change to `models.py`, so
  the "rename only alongside an existing schema bump" precondition is not met. A
  standalone rename would touch every `IndexEntry.remote_path_lower` /
  `SyncEvent.remote_path_lower` read+write site purely for cosmetics, with no
  behavioral benefit (the column already holds the correct fold value and is
  blessed by Stage 6 §1). Folded into Stage 8's naming sweep *iff* a model-schema
  change lands there for another reason; otherwise it stays. Item closed as
  "won't do (accepted), revisit-on-bump."

### 7c-10 — Transient duplicate `remote_id` rows (subsumed by 7c-1 / 7c-2)
- cluster-E and 7b docs note duplicate `remote_id` rows can exist transiently
  after occupied-target / failed-move fallbacks. The 7c-1 + 7c-2 fixes (semantic
  conflict + `UNIQUE` constraint) close this class; tracked here only to note the
  overlap so it isn't treated as separate work.

### 7c-11 — Occupied-target **file** move leaves an on-disk orphan that re-uploads
- **Opened by the 7c P0 work (2026-06-27).** After 7c-2, an occupied-target file
  move evicts the stale old-path index row (correct — no duplicate `remote_id`),
  but the on-disk copy at the old path is left with no index row, so the next
  local scan treats it as a new local file and **re-uploads it as a duplicate**
  in Drive. Not data loss (the moved file is correct at the new path, the
  occupant is conflict-copied), and strictly better than the pre-7c duplicate-
  `remote_id` defect (finding C) it replaced — but more user-visible than the
  folder case (a spurious duplicate file appears).
- **Proposed fix (Option C):** have the occupied-target download fallback delete
  the source file on disk at the old path once the move completes (the file
  moved away from it). **Deferred deliberately:** deleting local files in the
  fallback path is higher-risk and warrants its own RED-before-GREEN TDD cycle
  with adversarial review, not a quick patch. Severity P2. Guard test reference:
  `test_remote_move_with_occupied_target_evicts_stale_index_and_conflicts`
  (test_sync.py) currently asserts the orphan-on-disk residual.

### 7c-12 — Circuit-breaker counter reset-race under concurrency
- **Opened by the 7c-3 final review (2026-06-27).** `_handle_sync_thread_errors`
  is shared by all worker threads, all touching `_cursor_reset_count`. Because
  `stop()` is not instantaneous, a sibling worker's healthy pass can reset the
  counter a cycle or two after the change-feed worker incremented it. **Benign:**
  the breaker still trips (a persistent-400 feed never produces a healthy
  change-feed pass), worst case it takes a few extra cycles. P2, and a fix would
  add concurrency complexity (locking around the counter) for no behavioral gain
  — fold in only if `_handle_sync_thread_errors` is refactored for another
  reason.

### 7c-13 — Upload-path eviction symmetry (analyzed → intentionally NOT added)
- The 7c-3 final review noted `update_index_from_remote_metadata` (the upload /
  metadata index-write path) has no same-`remote_id` eviction, unlike
  `update_index_from_sync_event` (download path). **Investigated 2026-06-27 and
  intentionally left as-is:** every callsite of `update_index_from_remote_metadata`
  indexes an id at its *own* current path (upsert at the same path); the only
  flows that index an id at a *new* path while the old row persists are remote
  moves, handled on the download side (`_apply_remote_move` →
  `update_index_from_sync_event`), which already evicts. On a fresh DB the
  `UNIQUE` backstop means the upload path therefore cannot create or hit a
  duplicate in practice. Adding eviction here would be speculative code for a
  can't-occur case (YAGNI). Recorded so the note is not re-raised. Legacy-DB
  caveat is moot pre-release (all DBs fresh).

---

## 4. Already resolved — do **not** re-open

Flagged in earlier review notes but since closed; recorded to prevent
re-investigation:

- **`fail_after` generalization** — cluster-E noted it was wired only into
  `files.create`. 7b-E2 Task 1 review extended it uniformly to all resource
  `.execute()` sites (`files.update`, `changes.list`, etc.). **Done.**
- **T3 / cluster-E finding 5** (same-batch parent-rename + stale-child-echo) —
  fixed `f83267c` + `f51168b`. **Done.**
- **Crash-window delete-loss / cluster-E finding 2** — fixed `9a96447` +
  `e8525e6`. **Done.**
- **Load-test fixture contamination-hardening** — the 7c P0 gate hit a stale
  `~/load_initial_sync_dir` + partial index DB left by a killed background run.
  The `test_load_initial_sync.py` `sync_engine` fixture now defensively purges
  the sync dir + all config/state/index DB files at **setup** (not just
  teardown), so a killed prior run cannot contaminate the next. Verified by
  planting a stale dir + corrupt index DB and confirming a clean pass.
  **Done (2026-06-27).**

---

## 5. Explicitly out of 7c scope (→ Stage 8 / future features)

- **`DropboxClient = GoogleDriveClient` alias removal** and the
  `cumulus`/`maestral` naming sweep (master plan D3). Stage 8.
- **Stale docstrings/messages tied to the rename** — `client.py` `remove_batch`/
  `make_dir_batch` docstrings ("should use the batch endpoint" — already
  implemented), the `_not_implemented()` "see PLAN.md Phases 3–5" message,
  `maestral.png` in `resources/`. Sweep with Stage 8.
- **Sharing API surface** — `share_dir`, `create_shared_link`,
  `revoke_shared_link`, `list_shared_links` are `NotImplementedError` stubs
  (`client.py:149`). A genuine functional gap vs. Dropbox; a post-MVP **feature**
  decision, not a port-correctness bug. See differences-doc entry #27.
- **Shared Drives** and **multiple-parent legacy files** — see differences-doc
  entries #12 and #24; post-MVP features.

---

## 6. Suggested order of execution

1. **7c-1 + 7c-2 together** (B feeds C; one TDD+review cycle, remove both xfails).
2. **7c-3** (small, self-contained circuit-breaker; remove the third xfail).
3. **7c-4** (decide persist-vs-accept; quick).
4. **7c-5 / 7c-6** (need a real / 2-account setup; batch with the next linked
   spot-check session).
5. **7c-7 … 7c-9** (opportunistic; fold into the next touch of the relevant file).

**Definition of done for the P0 block:** all three `strict=True` xfails removed
(because the underlying defects are fixed, verified RED-before-GREEN), full
offline suite green, mypy/flake8 at baseline parity, deviations file written.

---

*Next step: invoke `superpowers:writing-plans` to draft the 7c-1/7c-2/7c-3
implementation plan, then execute via subagent-driven-development (mirroring the
cluster-E / 7b-E2 workflow).*
