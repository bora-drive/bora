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

### 7c-4 — `root_id` not persisted across daemon restarts
- Stage 3 §2.2 **and** Stage 4 §2.2 both deferred "persisting `root_id` across
  daemon restarts" to "Stage 6 (DB schema)." Stage 6 never picked it up, and no
  later stage did. Still in-memory only: `_ensure_root_id` resolves
  `files.get(fileId='root')` and caches on `PathIDCache.root_id`
  (`client.py:428`, `path_cache.py:114`), with no config/DB column.
- **Impact:** tiny (one extra API call per daemon start). Decide: persist it
  (config key or DB column) or formally accept the per-start resolve and close
  the dangling cross-stage promise.

### 7c-5 — Spike Q3: unshare scenario never validated
- Stage 0 spike report (`spikes/2026-04-30-...-report.md`, Known gaps) left the
  unshare-of-a-shared-item scenario unrun (needs a second Google account). The
  working assumption is "treat unshare like hard-delete (`removed=true`, no file
  body)." Never re-validated in Stage 3 or later.
- **Action:** run the scenario against a 2-account setup, or formally ratify the
  assumption with a documented rationale.

### 7c-6 — Spike Q1: headless paste-redirect OAuth never re-validated end-to-end
- The spike flagged the Mode-B paste-redirect path as "broken-as-shipped due to
  `InsecureTransportError`; Stage 2 must address this before claiming headless
  support." Stage 2 set `OAUTHLIB_INSECURE_TRANSPORT=1` (`daemon.py:32`), but no
  doc confirms the headless paste-redirect flow was directly observed working
  end-to-end.
- **Action:** verify the headless paste-redirect link flow on a port-forward-less
  host, or downgrade the "headless support" claim accordingly.

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

### 7c-9 — `remote_path_lower` column name holds a *fold* value
- `IndexEntry.remote_path_lower` (`models.py`) and `SyncEvent.remote_path_lower`
  are populated with a casefold key via `fold_path()`, but Stage 1 deliberately
  renamed the **cache** column `remote_path_lower → remote_path_fold` to avoid
  exactly this confusion. The index-side models kept the old `_lower` name.
  Stage 6 §1 blessed `_lower` for the models, so the two specs are internally
  consistent — but the rename rationale was not applied uniformly. Pure
  naming/readability; rename only if a model-schema bump is being made for
  another reason (the index is a derived cache, so no data migration is needed).

### 7c-10 — Transient duplicate `remote_id` rows (subsumed by 7c-1 / 7c-2)
- cluster-E and 7b docs note duplicate `remote_id` rows can exist transiently
  after occupied-target / failed-move fallbacks. The 7c-1 + 7c-2 fixes (semantic
  conflict + `UNIQUE` constraint) close this class; tracked here only to note the
  overlap so it isn't treated as separate work.

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
