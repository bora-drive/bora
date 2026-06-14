# Stage 7b-E2 — Fault-Injection Test Suite (design)

**Date:** 2026-06-14
**Branch:** `google-drive-port`
**Predecessor:** Cluster-E remediation complete (`d1990ad`); offline suite GREEN
(555 passed / 0 failed, `--benchmark-disable`), mypy at baseline parity.
**Master-plan reference:** `2026-04-30-staged-port-plan-design.md` §5.7, item **E2**.

## 1. Context & decomposition

Stage 7b in the master plan bundles three deliverables: **E2** (sync-engine
fault-injection), **E3** (10k-file load test), and a set of residual items that
cluster-E explicitly deferred "to Stage 7b fault injection". Per the
2026-06-14 brainstorming session these are split into **three sequential
sub-stages, each with its own spec → plan → execute cycle**:

1. **7b-E2** — fault-injection tests *(this spec)*
2. **7b-E3** — 10k-file load test *(future spec)*
3. **7b-deferred** — the cluster-E items punted to 7b *(future spec)*

This document covers **7b-E2 only**.

## 2. Goal

A fault-injection test suite that drives the cumulus sync engine and write
client through Drive-side faults — failures, "committed but response lost"
windows, partial pages, and corrupt successes — to **expose and fix real
defects**. Each fault scenario is a **TDD red→green cycle**: write the failing
fault test first; if it reveals a genuine defect in the sync engine / write
client / change feed, fix it in the same sub-stage; then keep the test as a
regression guard. Where a category overlaps a cluster-E hardening (F2–F14,
findings 2 & 5), its tests also serve as a "try hard to break it" regression
suite for that fix.

**Non-goals:** load/scale testing (E3), the deferred-bucket items
(7b-deferred), and any UI/CLI surface. No new product behavior is designed
here — only tests and the defect fixes they force.

## 3. Approach

**Category-organized, one plan task per fault category** (brainstorm approach
A). Each task: extend fake-Drive hooks if needed → write the red fault tests →
fix any exposed defect in-task → green + re-stress the related cluster-E
hardening. A shared-infrastructure task (Task 1) lands first because the
category tasks depend on its hooks; the category tasks (2–9) are then largely
independent and parallelizable under subagent-driven execution; Task 10 is the
final gate.

Rejected alternatives: a parametrized scenario-matrix harness (broad coverage
but parametrized failures resist clean red→green isolation) and a
hardening-regression-only organization (protects recent fixes but is
unsystematic about *new* fault surface). Approach A takes the regression
strength of the latter by mapping each category to its related hardening,
without giving up systematic new-ground coverage.

## 4. Real code surfaces under test

- **`src/cumulus/client.py`** — `get_metadata`, `download` (resumable
  get_media), `upload` (resumable), `remove` / `remove_batch`, `move`,
  `make_dir`, `create`, `list_remote_changes` / `list_remote_changes_iterator`.
- **`src/cumulus/sync.py`** — `download_sync_cycle`, `apply_remote_changes`,
  `_apply_remote_move`, `_apply_case_change`.
- **`src/cumulus/changes.py`** — `iter_remote_changes`.
- **`tests/fake_drive.py`** — in-memory fake-Drive (E1), the fault-injection
  vehicle; **`tests/offline/test_fake_drive.py`** — the fake's own tests.

## 5. Task map

| Task | Category | New/changed test module | Re-stresses |
|---|---|---|---|
| 1 | Fake-Drive hook generalization (shared infra) | `tests/fake_drive.py` + `test_fake_drive.py` | — |
| 2 | Partial change-feed pages | `test_fault_feed.py` | F6 cross-page fixpoint, finding 5 |
| 3 | Mid-cycle / crash-window faults | `test_fault_crash.py` | F2 / finding 2 delete-loss, F7 idempotent creates |
| 4 | Drive-vs-cache divergence | `test_fault_divergence.py` | F3 remote-moves-by-id, Task-3 occupied-target |
| 5 | Write-client mid-flight faults | `test_fault_writes.py` | F7, resumable-upload path |
| 6 | Download integrity faults | `test_fault_download.py` (+ corruption hook) | (new ground) |
| 7 | Rate-limit / 429 backoff | `test_fault_ratelimit.py` | F14 quota model |
| 8 | Cursor-invalidation faults | `test_fault_cursor.py` | F5 / Task-8 auto-reindex |
| 9 | Auth/token faults mid-cycle | `test_fault_auth.py` | F13 auth edges |
| 10 | Final gate | full offline suite + lint/type | — |

## 6. Task 1 — fake-Drive fault surface (shared infrastructure)

Generalize the fault API to the **union** of what tasks 2–9 need — no more.
The Task-1 plan spec enumerates which hooks each later task consumes so the
fake is not over-built. Every hook gets coverage in `test_fake_drive.py` (the
fake is itself tested).

1. **Uniform keying across every resource `.execute()`.** Audit and wire
   `_maybe_fail` / `_maybe_fail_after` into all ops the later tasks touch:
   `files.list/get/create/update/delete`, `changes.list`, `revisions.list`,
   `get_media`/download, and batch. Today the `fail_after_check` plumbing only
   reaches some request types (`fail_after` is effectively exercised only on
   `files.create`).
2. **Nth-call faults.** Fault the *K-th* call of a key, not just the next
   (e.g. fail on the 2nd `changes.list` to model "page 1 delivered, page-2
   request faults"). A small count parameter on `fail_next` / `fail_after`.
3. **Argument-predicate faults.** `fail_when(key, predicate, exc)` inspecting
   call kwargs (`fileId`, `q`, `pageToken`) to target one specific file
   mid-batch or one specific page boundary. Consumed by tasks 2, 4, 5.
4. **Exception factory.** A helper producing correctly-shaped `HttpError`
   (400/401/429/500), socket-timeout, and connection-reset exceptions so each
   category test reads cleanly and consistently.

The **download-corruption hook** (truncation / byte-flip / wrong-md5 injection
into `get_media`) is *not* in Task 1: its only consumer is Task 6, so it lives
there (single-use infra next to its user).

## 7. Per-category scenarios (the red targets)

Each bullet is a red test first; if it exposes a real defect, fix in-task.

**Task 2 — Partial change-feed pages** (`test_fault_feed.py`)
- Page-2 request faults after page-1 applied → cursor must **not** advance past
  unapplied changes (no silent change loss).
- Logical op split across a page boundary: parent rename on page 1, child echo
  on page 2 (re-stress finding-5 stale-echo skip across a real page break, not
  just same-batch).
- `fail_after` on `changes.list` (page committed server-side, response lost) →
  next poll redelivers; verify idempotent re-apply.
- Duplicate change entry within / across pages → no double-apply.

**Task 3 — Mid-cycle / crash-window** (`test_fault_crash.py`)
- Crash between classification and cursor commit → batch replays cleanly
  (re-stress F2 / finding-2 delete-loss via the `resolve_deleted_path`
  resolver path).
- Crash mid-`apply_remote_changes` batch (fault after item N applied) → restart
  re-applies N+1.. without corrupting the N already done.
- `fail_after` on `files.create` during create-then-crash → idempotent-create /
  generateIds (F7) prevents the duplicate.

**Task 4 — Drive-vs-cache divergence** (`test_fault_divergence.py`)
- Path cache holds stale id→path after an out-of-band Drive move →
  `_apply_remote_move` resolves by `remote_id` (F3), not stale path.
- Occupied-target move (Task-3 residual): target path occupied → early-return,
  downstream conflict logic, no silent overwrite / resurrection.
- Duplicate `remote_id` index rows (the transient state Task-3 flagged) →
  deterministic resolution, no crash.
- Missing index row for a live Drive file → reindex / repair rather than
  silent divergence.

**Task 5 — Write-client mid-flight** (`test_fault_writes.py`)
- Resumable `upload` faults mid-chunk → resume / retry yields one correct file,
  no truncated or duplicate.
- `remove_batch` / batch faults mid-batch (predicate fault on one fileId) →
  partial-batch handled, remaining items not lost.
- Injected 5xx on `move` / `make_dir` → retry / backoff converges.

**Task 6 — Download integrity** (`test_fault_download.py` + corruption hook)
- Truncated `get_media` → hash mismatch detected, file not committed as
  complete.
- Wrong-md5 content → integrity check rejects; retried or surfaced, not
  silently written.
- Resumable download faults mid-stream → resume or clean re-download.

**Task 7 — Rate-limit / 429 backoff** (`test_fault_ratelimit.py`)
- 429 `userRateLimitExceeded` storm on feed and on writes → backoff converges,
  no deadlock, no dropped work.
- `dailyLimitExceeded` (F14): keep-going vs stop semantics match the corrected
  quota model; session-cumulative counter behaves.

**Task 8 — Cursor-invalidation** (`test_fault_cursor.py`)
- 400 invalid `pageToken` mid-stream → `CursorResetError` → auto-reindex
  (F5 / Task-8), stop-before-reset ordering preserved.
- 400 on a *fresh* token (the persistent-400 bug class Task-8 flagged) →
  bounded behavior, not infinite reindex spam.

**Task 9 — Auth/token mid-cycle** (`test_fault_auth.py`)
- 401 / `TokenRevokedError` mid-batch → clean stop, no partial corruption,
  resumable after relink.
- Token-refresh failure mid-sync (F13) → correct error surfaced, cross-restart
  safety holds.

## 8. TDD discipline & bug-triage protocol

- **Red first, always.** Every scenario starts as a failing test. Verify the
  RED empirically (the test fails for the intended reason) before writing or
  changing any production code.
- **Fix in-task.** A genuine defect exposed by a red test is fixed in the same
  task, then the test goes green. Production fixes follow the existing cumulus
  patterns and stay scoped to the exposed defect.
- **Triage non-trivial finds.** If a red test exposes a defect whose fix is
  large enough to be its own design problem (architectural, cross-cutting),
  capture it as an `xfail` with a finding note and surface it to the user
  rather than silently expanding scope. The user decides fix-now vs defer.
- **Characterization is acceptable** only where current behavior is correct and
  the test merely pins it; never use characterization to paper over a real bug.

## 9. Acceptance / final gate (Task 10)

- Full offline suite GREEN: **0 failed, 0 unexpected skips**, run with
  `--benchmark-disable` (the lone benchmark `test_performance` assertion still
  runs). Document the new pass count vs the 555 baseline.
- `flake8` / `mypy` at baseline parity (no new violations vs `d1990ad`).
- Any `xfail`-captured deferrals listed in the deviations file with finding
  notes and a fix-now/defer decision recorded.
- **Test-discipline guards (carried from cluster-E):** never run `tests/linked`
  (needs a real account); never run `tests/offline` while a real smoke-test
  account is linked (keyring collision); the full offline suite is run only at
  the gate.
- A deviations file (`docs/superpowers/plans/2026-06-14-stage-7b-e2-*-deviations.md`)
  records plan-vs-implementation deltas and any deferred finds, mirroring the
  cluster-E convention.

## 10. Open risks

- **Fault realism.** The fake-Drive models Drive, not Drive itself; some faults
  (server-side revision merging, eventual-consistency races) are inherently
  unmodellable in-memory and are explicitly out of E2 scope (noted for E3 /
  7b-deferred and real-account smoke).
- **Scope growth from fixes.** TDD+fix can surface more defects than expected;
  the triage protocol (§8) bounds this by escalating large fixes to the user
  instead of absorbing them silently.
