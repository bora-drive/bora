# Staged Port Plan: Maestral → Google Drive (cumulus)

**Status:** Design (awaiting review)
**Date:** 2026-04-30
**Supersedes:** monolithic `PLAN.md` / `TODO.md` execution model
**Inputs:** `PLAN.md`, `TODO.md`, `REVIEW.md` (15 issues, all closed), `REVIEW2.md` (~25 additional gaps), `DROPBOX_API_ANALYSIS.md`, `SYNC_ENGINE_ANALYSIS.md`

---

## 1. Purpose

Replace the monolithic execution model in `PLAN.md` / `TODO.md` with a sequenced
set of **independently spec-able stages**, each scoped to a discrete spec → plan
→ implementation cycle. The trigger for this rewrite is that two passes of
review (`REVIEW.md`, `REVIEW2.md`) keep surfacing additional gaps when the plan
is read as a whole; smaller stages with clearer boundaries reduce that risk.

The first stage is a **risk-first validation spike** that confirms the
project's load-bearing API assumptions against a real Google Drive before any
detailed stage spec is written.

## 2. Why a spike-first sequence

`REVIEW2.md` lists ~25 unaddressed gaps after `REVIEW.md`'s 15 were closed.
Several of the High-severity items (B1 headless OAuth, A3 change-feed scoping,
A4 `removed` reasons, C1 `/`-in-name behavior, A5 combined move+rename) are
load-bearing for later stages but cannot be fully resolved by reading docs —
they need real API responses. Specifying Stages 1+ before validating them
risks specs that contradict reality.

The spike absorbs this risk: ~2–3 days of throwaway code answers each
high-severity ambiguity with a concrete payload, and the Stage 1+ specs are
then written against ground truth.

## 3. Scope and explicit non-goals

**In scope (this design):** the sequence of stages, what each covers, ordering
rationale, dependencies, deferrals.

**Not in scope (this design):** the per-stage spec content. Each stage gets
its own design document during its own brainstorming cycle.

**Explicit deferrals (out of scope for the v1 port):**

- Shared Drives (team spaces) — `is_team_space` returns False; only "My Drive"
- GUI packages (`maestral-qt`, `maestral-cocoa`) — out of scope; Stage 5
  decides between a compatibility shim or a documented break
- Webhook-based change notifications — polling only
- Batch API calls beyond `make_dir_batch` / `remove_batch`
- **Phase 5h sharing** (`share_dir`, `create_shared_link`, etc.) — stub for MVP
- **REVIEW2 A6 `appProperties`** — defer unless Stage 3/4 surface a need
  (PLAN.md skips symlinks, so symlink-target persistence is not required)

## 4. Stage 0 — Architecture validation spike

**Goal:** confirm the load-bearing API assumptions in PLAN.md and REVIEW2.md
against a real Drive before Stage 1's spec is written.

**Branch:** `spike/drive-validation` — deleted after the spike report is
committed. No production code from the spike is merged.

**Form factor:** a single Python script using `googleapiclient` directly. No
path cache, no sync engine, no error-handling hierarchy, no tests.

### 4.1 Questions to answer

| # | Question (REVIEW2 ref) | Procedure | What it informs |
|---|---|---|---|
| 1 | Does OAuth `run_local_server` work in your dev environments? (B1) | Run locally and via SSH; if either fails, prototype out-of-band PKCE flow | **Spike answer:** Mode A succeeds in headless VM via SSH port-forward; paste-redirect fails with `InsecureTransportError` (HTTP redirect_uri rejected by oauthlib). Stage 2 default = `run_local_server` + documented SSH port-forward; paste-redirect fallback requires `OAUTHLIB_INSECURE_TRANSPORT=1` or an embedded-loopback rewrite. |
| 2 | Does `md5Checksum` match locally-computed `hashlib.md5`? | Upload 5 files (small, large, binary, text, non-ASCII bytes); compare | **Spike answer:** all 5 match exactly, including 6 MB resumable case. Stage 5 hashing plan stands as-is — `ContentHasher` (MD5) maps directly to Drive's `md5Checksum`. |
| 3 | What does `changes.list` return for trash / hard-delete / unshare / move-out-of-scope? (A3, A4) | Set up a folder, get `startPageToken`, perform each mutation, capture payloads | **Spike answer (unshare deferred — single account):** trash → `removed=false` + `file.trashed=true` + full file body; hard-delete → `removed=true` + no file body, only `fileId`; move-out → `removed=false` + full file body with updated `parents`. `_clean_remote_changes` rules embedded in §5.3. |
| 4 | Can you create a Drive file whose `name` contains `/`? (C1) | `files.create(name='2024/05 report.pdf')`; if it succeeds, see how `files.list` returns it | **Spike answer:** Drive accepts `/` literally — `name`, `list`, `get` all roundtrip the slash unchanged. Stage 1 must **escape** (not reject); Stage 4 mirror-escapes on upload. |
| 5 | What does a combined move + rename look like in one change entry? (A5) | Single `files.update` with new name and `addParents`/`removeParents`; observe change | **Spike answer:** exactly one change entry covers both — `path_cache.move()` reads name+parents atomically from the single entry. No split-entry coordination required. |
| 6 | What `removed` reasons can a change entry actually carry? (A4) | Trigger trash, hard-delete (`files.delete`), permission revoke, move-out-of-scope; record each payload | **Spike answer:** `removed=true` alone is **not** sufficient — only set for hard-delete. Trash → `removed=false` + `file.trashed=true`. Move-out → `removed=false` + parents updated. Disambiguation table embedded in §5.3. |

### 4.2 Deliverable

`SPIKE_REPORT.md` (committed at end of spike, branch then deleted) listing each
question, the actual response captured, and which REVIEW2 item the result
**confirms**, **contradicts**, or **refines**. Contradictions or refinements
feed back into this design before Stage 1's spec is written.

### 4.3 Non-deliverables

- No production code merged
- No test infrastructure
- No path cache integration
- No error-handling hierarchy
- No CLI

## 5. Post-spike stages — overview

Eight stages, each its own spec → plan → implementation cycle. Each stage is
sized to ~3–10 days of focused work.

| # | Stage | Phase coverage | REVIEW2 coverage |
|---|---|---|---|
| 1 | Path cache final form | Phase 4 hardening (already partly built) | C1, C2, C3 (cache side), C7, A6 (only if needed), E2 (path-cache fault-injection) |
| 2 | Auth & onboarding | Phase 2, 3 | B1, B3 |
| 3 | Read client (download + change feed) | Phase 5a–5d, 5g, read-side of Phase 6 | A2, A3, A4, C6 (skip-with-log) |
| 4 | Write client (upload / move / delete / mkdir) | Phase 5e, 5f, write-side of Phase 6 | A5, C3 (upload sanitization), C4, C5, D7 |
| 5 | Sync engine integration | Phase 7, Phase 8 | A1, D1, D4, line-3542 callsite |
| 6 | Manager + CLI + DB schema | Phase 9, 10, 11 | D2 |
| 7 | Test infrastructure & coverage | Phase 12 | E1, E2 (sync-engine side), E3 |
| 8 | Docs + release | Phase 13, 14 | B2, D3, D5, D6, F items |

### 5.1 Stage 1 — Path cache final form

**Why first:** every later stage translates paths through the path cache.
`path_cache.py` already exists (53a97e7) and has case-insensitive uniqueness
(76577f4). This stage hardens it for production.

**In scope:**

- C1 — `/`-in-name policy: **escape** `/` (and the escape character itself)
  when serializing a Drive `name` into the virtual path; symmetric un-escape
  on hydration. Decided by spike Q4 (Drive accepts `/` literally as a name
  character).
- C2 — replace `name.lower()` with `casefold() + NFC` for case-conflict keys.
- C3 (cache side) — reject names containing filesystem-reserved characters
  (`*?"<>|` plus embedded NUL) before admission to the cache.
- C7 — initial-sync atomicity: add `listed_at` per folder so an interrupted
  initial recursive listing can resume without partial population corrupting
  the cache.
- A6 (conditional) — `appProperties` schema decisions only if Stage 3 / 4
  surface a concrete need.
- E2 — fault-injection unit tests: cache-vs-Drive divergence, partial
  population recovery, concurrent mutation under `sync_lock`.

**Out of scope:** anything that requires the Google Drive client (Stage 3+).

### 5.2 Stage 2 — Auth & onboarding

**Why second:** unblocks all integration testing in later stages. Independent
of Stage 1.

**In scope:**

- Phase 2 — Google Cloud project setup; commit `client_secret.json` to
  `src/cumulus/resources/`; document for contributors.
- Phase 3 — `get_auth_url`, `link(code)`, `unlink()`, refresh-on-401, `linked`
  property.
- B1 — headless-friendly OAuth UX. Default: `run_local_server` (works
  locally and over SSH port-forward — confirmed by spike Q1). Document the
  port-forward pattern for headless users. Paste-redirect fallback requires
  `OAUTHLIB_INSECURE_TRANSPORT=1` env var (or an embedded-loopback rewrite);
  the spike's `run_flow_console` is a reference for the failure mode without
  it (`InsecureTransportError`).
- B3 — token-revocation propagation: when a background-thread API call returns
  401 / `RefreshError`, surface as `AuthError` and re-link prompt; verify the
  existing Dropbox-flow re-link path still functions.

**Out of scope:** B2 OAuth verification submission (Stage 8 — release prep).

### 5.3 Stage 3 — Read client (download + change feed)

**Why before write:** read-only operations can be safely integration-tested
against an existing populated Drive without risk of corruption. Read primitives
also populate the path cache, which the write client then reads from.

**In scope:**

- Phase 5a (scaffolding), 5b (account ops), 5c (metadata ops), 5d (download).
- Phase 5g (change detection): `get_latest_cursor`, `list_folder`,
  `list_folder_iterator`, `wait_for_remote_changes` (polling),
  `list_remote_changes_iterator`.
- A2 — extend `native_docs.py` filter to all `application/vnd.google-apps.*`
  MIME types plus shortcut handling (`shortcutDetails.targetId`: skip).
- A3 — change-feed filtering by sync-root: change entries don't include paths,
  so resolving each `fileId` to a path goes through the cache; misses trigger
  a `files.get` to learn parents.
- A4 — `removed` disambiguation rules (decided by spike Q3, Q6):

  | Observation | Interpretation | Action |
  |---|---|---|
  | `removed=true` (no `file`) | hard-deleted, unshared, or lost visibility | propagate delete; cache resolves fileId → cached path |
  | `removed=false` + `file.trashed=true` | user trashed | propagate delete |
  | `removed=false` + `file.trashed=false` + no parent in sync scope | moved out of sync scope | propagate delete of local mirror |
  | `removed=false` + `file.trashed=false` + parent in sync scope | normal create / update / move within mirror | apply via path_cache |

  Rule 3 (move-out detection) requires computing `file.parents ⊆ sync_root`
  per change; the cache resolution covers this provided the cache is
  populated.
- C6 — Computers / Backup folders: skip with log warning (single bullet, not
  a section).
- Read-side error mapping (subset of Phase 6): 401, 403, 404, 429, 500/503 for
  read endpoints; `convert_api_errors` context manager.

**Out of scope:** any mutating API call (Stage 4).

### 5.4 Stage 4 — Write client (upload / move / delete / mkdir)

**Why after read:** write resolves parent IDs via path cache, which Stage 3
populates from initial listing.

**In scope:**

- Phase 5e (upload): simple ≤5MB, resumable >5MB; `WriteMode.{Add,Update,
  Overwrite}`; MD5 verify after upload; cache update on success.
- Phase 5f (file/folder ops): `remove`, `move`, `make_dir`, `make_dir_batch`,
  `remove_batch`, `autorename`.
- A5 — combined move+rename in a single `files.update`; spike Q5 confirmed
  the change feed emits **one** entry covering both new name and new parent,
  so `path_cache.move()` reads name+parents atomically from the single entry
  (no split-entry coordination needed).
- C3 (upload sanitization) — reject or normalize names containing
  filesystem-reserved characters before they reach Drive. Cross-check with
  Stage 1's cache-side rule.
- C4 — trash vs delete policy decision: default to `files.update(trashed=True)`
  for sync-driven deletes (recoverable for 30 days). Whether to also expose a
  CLI flag for irreversible `files.delete` is a Stage 6 decision; this stage
  only commits to the sync-driven default and ensures the client supports both
  primitives.
- C5 — `storageQuotaExceeded` returned mid-resumable-upload: catch per chunk,
  translate to `InsufficientSpaceError`, decide pause/notify/retry policy.
- D7 — conflict-copy naming: enforce uniqueness on Drive side before upload
  (sync engine already produces `(conflicting copy …)` names; verify they don't
  collide with siblings).
- Write-side error mapping (remainder of Phase 6).

**Out of scope:** Phase 5h (sharing) — stubbed; revisit post-MVP.

### 5.5 Stage 5 — Sync engine integration

**Why here:** integration crucible. Needs Stages 3 and 4 complete.

**In scope:**

- Phase 7 — replace `DropboxContentHasher` with `ContentHasher` (MD5);
  `StreamHasher` largely unchanged; `HashCacheEntry` now stores MD5.
- Phase 8 — sync-engine adaptations: `dbx_path` rename already done in Phase 1;
  this stage covers the semantic adaptations.
  - A1 — map `modifiedTime` to `FileMetadata`; collapse Dropbox's
    `client_modified` / `server_modified` distinction; **set** `modifiedTime`
    on upload to prevent round-trip clobbering and feedback.
  - Line-3542 callsite — replace `f"rev:{event.rev}"` with `event.change_remote_id`.
  - Conflict-resolution heuristic (`_check_download_conflict`) updated for
    single `modifiedTime`.
- D1 — daemon RPC field-rename audit: enumerate every Pyro5-exposed method in
  `daemon.py`, identify any that crosses the renamed-field boundary, decide
  shim vs. break vs. version-bump.
- D4 — GUI compat decision: shim (re-export `remote_path` as `dbx_path` alias
  in `models.py` so `maestral-qt` / `maestral-cocoa` imports keep working) or
  documented break with a deprecation note in CHANGELOG. Phase 1 already
  renamed `dbx_path` → `remote_path`, so this stage decides whether to add a
  back-compat alias or accept the silent breakage of the GUI packages.

### 5.6 Stage 6 — Manager + CLI + DB schema

**In scope:**

- Phase 9 — CLI updates across `cli/cli_main.py`, `cli_core.py`, `cli_info.py`,
  `cli_maintenance.py`, `cli_settings.py`, `dialogs.py`, `common.py`,
  `cli/core.py`. Verify actual filenames in `src/cumulus/cli/` match.
- Phase 10 — manager updates: startup sequencing for Drive, polling-loop
  download worker, `_check_and_update_path_root` rename / removal,
  `ensure_dropbox_folder_present` → `ensure_drive_folder_present`,
  remove-or-adapt Team Space migration.
- Phase 11 — DB schema:
  - D2 — proper migration framework entry for the path-cache table (preferred
    over reset-on-version-bump).
  - `IndexEntry`, `SyncEvent`, `SyncErrorEntry` model updates for renamed
    fields.
  - Fresh database on first run for users without state.

### 5.7 Stage 7 — Test infrastructure & coverage

**Why this late:** the in-memory fake-Drive (E1) is built against a stable
client API; doing it earlier means rebuilding when Stage 3/4 evolve.

**In scope:**

- Phase 12 — port all existing offline + linked tests; rewrite the 12 files
  that import `dropbox`.
- E1 — in-memory fake-Drive: a few-hundred-line module that models state
  transitions (upload then list shows new file) so tests don't hand-write JSON
  for every scenario. Replaces the per-test `HttpMock` pattern for any test
  that exercises sequenced state.
- E2 (sync-engine side) — fault-injection: Drive-vs-cache divergence, mid-cycle
  failures, partial change-feed pages.
- E3 — load test: 10k-file initial sync against fake-Drive (or a real test
  account if available), validates the rate-limit redesign from REVIEW issue 3.
- Set up dedicated test Google account, document in `tests/README.md` (the
  file referenced in TODO Phase 12 that doesn't yet exist).

### 5.8 Stage 8 — Docs + release

**In scope:**

- Phase 13 — Sphinx config, docstrings, setup guide (Google Cloud + OAuth),
  user guide, differences-from-Dropbox doc.
- Phase 14 — packaging, scripts entry point, systemd template, desktop entry,
  pip install verification, macOS / Linux / **Windows** smoke test.
- B2 — submit Google OAuth app for verification; document the
  add-as-test-user workaround for contributors / early users until
  verification clears.
- D3 — cumulus / maestral naming sweep: README, CHANGELOG, Sphinx config,
  PLAN.md (if kept), `_check_and_update_path_root` rename target decision.
- D5 — credential rotation strategy: if `client_secret.json` is ever rotated,
  document the policy (release note, deprecation timeline, attribution
  cleanup) so prior releases shipping the old credential don't poison
  abuse-quota for the project.
- D6 — polling-latency UX note in user docs and README "differences from
  Maestral upstream" section: "changes made on drive.google.com appear locally
  up to 3 minutes later" given the 180s polling interval.
- F (housekeeping):
  - `tests/README.md` exists (covered in Stage 7 too).
  - `utils/path.py` `dbx`-named function: rename or move out of "no changes"
    list.
  - `PLAN.md §3.2` 30s polling claim updated to 180s.
  - PLAN.md / TODO.md sweep for residual filename references.

## 6. Dependency graph

```
Stage 0 (spike)
   ↓
Stage 1 (path cache)
   ↓
Stage 2 (auth)            ← could run in parallel with Stage 1 if desired
   ↓
Stage 3 (read client)
   ↓
Stage 4 (write client)
   ↓
Stage 5 (sync engine integration)
   ↓
Stage 6 (manager + CLI + DB)
   ↓
Stage 7 (test infrastructure) ← could overlap with Stage 6
   ↓
Stage 8 (docs + release)
```

Stages 1 and 2 are independent and can be parallelized; in solo-dev mode
sequential is simpler. Stage 7 can begin during Stage 6 if test scaffolding
is needed for Stage 6's verification. Otherwise the chain is linear.

## 7. Definition of "stage complete"

For each stage:

1. Spec written and committed to `docs/superpowers/specs/`.
2. Implementation plan written (writing-plans skill output) and committed.
3. Code merged to `google-drive-port` branch.
4. All new tests pass; existing tests pass or have been migrated.
5. For stages that produce user-visible behavior (2, 3, 4, 5, 6): a manual
   smoke test against a real Google account confirming the new capability
   works end-to-end.

Tagged release happens once at the end of Stage 8, not per-stage.

## 8. Risk register update

The `PLAN.md §8` risk table is preserved; this design adds the following
stage-bound risks:

| Risk | Stage | Mitigation |
|---|---|---|
| Spike findings invalidate Stage 1+ specs | 0 | Spike report explicitly lists confirms / contradicts / refines per question; design is updated before any Stage 1 spec is written |
| Path-cache concurrency model bugs surface only under load | 1, 7 | Stage 1 includes fault-injection tests (E2); Stage 7 adds 10k-file load test (E3) |
| Daemon RPC silent breakage from Phase 1 field renames | 5 (D1) | Explicit RPC audit task in Stage 5; shim or version-bump decision documented |
| OAuth verification screening blocks distribution | 8 (B2) | Submit early; document add-as-test-user workaround for contributors |
| Test-account quota exhaustion during Stage 7 load test | 7 (E3) | Use fake-Drive for the bulk of E3; reserve real-account run for final smoke |

## 9. Open questions / things to revisit after the spike

- ~~C1 `/`-in-name: reject or escape?~~ **Resolved by spike Q4 → escape.**
  Embedded in §5.1.
- ~~A5 combined move+rename atomicity in change feed.~~ **Resolved by spike
  Q5 → single entry, atomic.** Embedded in §5.4.
- ~~A4 `removed` disambiguation rules.~~ **Resolved by spike Q3+Q6** via the
  rule table embedded in §5.3.
- B1 OAuth UX: spike Q1 confirmed `run_local_server` works headless via
  port-forward; paste-redirect needs `OAUTHLIB_INSECURE_TRANSPORT=1`.
  Embedded in §5.2.
- C4 trash vs delete default: `trashed=True` proposed, but final policy
  pending Stage 4 spec and possibly user input.
- A6 `appProperties`: defer unless Stage 3 / 4 surface a concrete need.
- D4 GUI compat: shim or break? Decision deferred to Stage 5 spec.
- Q3 unshare scenario: not run in spike (single-account); revisit only if
  Stage 3 surfaces a concrete failure mode that the working assumption
  (treat unshare like hard-delete) doesn't cover.

## 10. Next step after this design is approved

Invoke the `superpowers:writing-plans` skill to draft the **Stage 0 spike**
implementation plan. Stage 1+ specs are written one at a time, after the
spike report is reviewed and any contradicted assumptions are folded back
into this design.
