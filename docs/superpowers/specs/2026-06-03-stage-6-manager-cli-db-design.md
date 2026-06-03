# Stage 6 — Manager + CLI + DB schema

**Status:** Design (awaiting review)
**Date:** 2026-06-03
**Master design:** `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` §5.6
**Predecessor:** Stage 5 (sync engine integration) — complete + smoke-tested
**Covers:** Phase 9 (CLI), Phase 10 (manager), Phase 11 (DB schema), REVIEW2 D1/D2/D4

---

## 1. Framing

The master design scoped Stage 6 as a large stage (CLI port + manager port +
DB schema + migration framework). A pre-spec audit of the current tree found
that **most of that work was already absorbed incrementally during Stages
2–5**:

- **CLI** (`src/cumulus/cli/`) — every command group is Drive-shaped.
  `cli_core.py`, `cli_info.py`, `cli_maintenance.py` use `FileMetadata.modified`,
  `remote_path`, `rev`, `remote_id`. The remaining files (`cli_main`,
  `cli_settings`, `dialogs`, `common`, `core`, `output`, `utils`) are
  Drive-agnostic. No `dropbox`/`dbx` strings remain in the CLI.
- **Manager** (`manager.py`) — threads, worker loops, and the connection
  monitor already drive the Drive client (`GOOGLE_API_HOSTNAME`,
  `download_sync_cycle`, `upload_sync_cycle`, `wait_for_remote_changes`).
- **DB models** (`models.py`) — `SyncEvent`, `IndexEntry`, `HashCacheEntry`,
  `SyncErrorEntry` all use `remote_path` / `remote_path_lower` / `remote_id` /
  `rev` (Drive shape).
- **RPC surface** (`main.py` / `daemon.py`) — `expose(Maestral)` with a
  `remote_path`-based method set; no `dbx_path` parameters.

What remains are four discrete items. Stage 6 closes them and runs the **first
full-daemon end-to-end validation** (Stages 0–5 never started the actual
daemon process — Stage 5 hand-drove `SyncEngine` cycles in-process).

This spec is deliberately tight. The Stage 5 carry-over polish items
(`writes.py` consolidation, `_count_request` threading, `batch_size` /
`_throttle_up` bandwidth tuning, cross-page change-feed cursor durability), the
GUI port, and the wholesale rewrite of the broken Dropbox-era test files are
all **explicitly out of scope** (see §7).

## 2. Scope — four work items

### W1. Remove path-root vestiges (`manager.py`)

Dropbox's team-namespace / path-root migration logic is dead under Drive (no
team namespaces). Three methods and their callsites are currently inert stubs:

| Symbol | Current state | `manager.py` |
|---|---|---|
| `check_and_update_path_root()` | returns `False` | ~:372 |
| `_needs_path_root_update()` | returns `False` | ~:394 |
| `_update_path_root()` | raises `NotImplementedError` | ~:408 |

Plus the no-op callsites in the worker / startup loops (~:465, ~:570, ~:629).

**Action:** delete all three methods and remove or inline-simplify the three
callsites so the worker loops no longer reference path-root logic.

**Gate (must run before deleting):** grep the in-scope tree for
`check_and_update_path_root` / `_needs_path_root_update` / `_update_path_root`.
Confirm no `main.py` RPC method or other in-scope caller depends on the names.
The out-of-scope GUI packages are **not** a constraint (see W3/D4). If a live
in-scope caller is found, simplify it to drop the path-root branch rather than
preserve a dead call.

**Done when:** the three methods are gone, the callsites no longer mention
path-root, the tree imports, and the offline suite still collects/passes with
the existing `--ignore=tests/offline/test_manager.py` set unchanged.

### W2. D2 — document the reset-on-version-bump policy (no framework)

**Decision:** the index / history / hash_cache tables are a **derived cache of
Drive state**, not a system of record. On a schema-version change we drop and
re-derive them from Drive rather than migrate in place. There will be no
`schema_version` stamp and no migration registry — re-indexing is cheap and
always available, and this is a fresh fork with no shipped schema to migrate
from.

The behavior already exists: `Maestral._update_from_pre_v1_6_0()`
(`main.py:1565`) drops `hash_cache` / `'index'` / `'history'` and resets sync
state; `_check_and_run_post_update_scripts()` (`main.py:1519`) already notes
"Legacy Dropbox-era migrations have been removed. This is a fresh fork."

**Action:** make the policy explicit, not implicit. Add a short comment at the
schema-reset path (`main.py` near `_update_from_pre_v1_6_0` / the
post-update-scripts seam) stating: *the index DB is a derived cache; schema
changes are handled by dropping and re-deriving from Drive, by design — there
is no migration framework.* Record the same statement in this spec (here) and
the stage deviations doc. No code framework is written.

**Done when:** the policy is stated in code and in the spec; no new migration
machinery is added.

### W3. D1 RPC audit + D4 GUI-compat decision

**D1 (audit, deliverable = a table in this spec / deviations doc).** One pass
over the `expose(Maestral)` surface (`daemon.py:466`; methods/properties in
`main.py`) confirming every field that crosses the Pyro5 boundary uses the
renamed Drive shape (`remote_path` not `dbx_path`; single `modified`; `rev` =
`headRevisionId`). The audit found it already clean; Stage 6 formalizes that
as a recorded table and flags any straggler found during execution.

**D4 (decision = documented break).** The out-of-scope GUI packages
(`maestral-qt`, `maestral-cocoa`) import `SyncEvent` / `FileMetadata` expecting
a `dbx_path` attribute that no longer exists. We accept this as an
**intentional break**: no model-level compat shims are added (adding shims for
packages we neither port nor test is dead weight). The pre-existing
`exceptions.py:55-70` `dbx_path` / `dbx_path_from` aliases on `CumulusApiError`
stay only because they already exist and cost nothing — Stage 6 does not extend
that pattern to the models.

**Done when:** the D1 table is recorded; the D4 break is recorded in the stage
deviations doc and noted for the CHANGELOG (Stage 8 owns the actual CHANGELOG
entry).

### W4. First full-daemon end-to-end smoke test (verification gate)

This is what is genuinely new in Stage 6: the daemon process, the Pyro5
server, the manager threads (startup / download / upload / connection-monitor),
startup sequencing, and the CLI→daemon RPC roundtrip have never run together.
Stage 5 validated engine *logic* by hand-driving cycles; Stage 6 validates the
*thread / RPC / lifecycle layer*.

**Scope — focused on the new layer** (engine behaviors — conflict, move,
trash, restore — are already proven 7/7 in Stage 5 and are not re-run here):

1. `cumulus start` via the real daemon path → daemon + Pyro5 server come up
   against the linked `cumulus@arko.net` account.
2. CLI→daemon RPC roundtrip — `cumulus status` and account info return sane
   Drive data over Pyro5.
3. **Autonomous** sync, each direction, *without* hand-driving cycles:
   - drop a local file → the upload thread pushes it to Drive;
   - create a file on Drive → the download poll loop pulls it locally.
4. `cumulus stop` → clean shutdown: threads join, `client.close()` fires, no
   orphaned daemon / lock.

**Bugs surfaced here are fixed inline** (the Stage 5 carry-over note explicitly
predicted thread / startup / shutdown bugs would first appear at this gate).
Operational note carried from Stage 5: Drive's `changes.list` lags a few
seconds; the daemon's poll loop absorbs this, but the smoke harness must allow
bounded-retry polling rather than asserting immediately after a mutation.

**Done when:** all four checks PASS against `cumulus@arko.net`, recorded in the
stage deviations / smoke-test doc with the same rigor as prior stages. As with
earlier stages the smoke script need not be committed; created Drive items are
cleaned up in a `finally` block.

## 3. Components touched

| File | Change | Item |
|---|---|---|
| `src/cumulus/manager.py` | delete 3 path-root methods + 3 callsites | W1 |
| `src/cumulus/main.py` | comment documenting reset-on-bump policy | W2 |
| `docs/.../stage-6-...-deviations.md` | D1 table, D4 break, smoke record | W2/W3/W4 |
| (smoke harness, uncommitted) | full-daemon end-to-end script | W4 |

No DB model changes, no CLI code changes, no RPC method changes are expected;
the audit (W3) may surface a straggler, handled inline if so.

## 4. Data flow / behavior changes

None to the sync data flow. W1 removes dead branches; W2 is documentation; W3
is audit + a recorded decision; W4 is verification only. The single behavioral
surface Stage 6 exercises for the first time is the autonomous daemon thread
orchestration (W4), which is validated rather than changed — unless the smoke
surfaces a bug, in which case the fix is scoped to the thread/lifecycle layer
and recorded as a deviation.

## 5. Error handling

W4 is the place errors will surface. Expected failure classes and handling:

- **Startup sequencing** — daemon fails to come up, or a worker thread dies on
  start: fix the sequencing inline; record as a smoke finding.
- **RPC marshalling** — a Pyro5-exposed return type doesn't serialize: fix the
  type / add a serializer; record.
- **Shutdown** — threads don't join or `client.close()` isn't reached: fix the
  shutdown path; record.
- **Change-feed latency** — not a bug; the harness polls with bounded retries.

## 6. Testing

- **Offline suite:** must continue to collect and pass with the existing
  `--ignore=tests/offline/test_manager.py` set
  (`pytest tests/offline --ignore=tests/offline/test_manager.py -q`,
  baseline 418 passing). W1 is deletion-only, so no new unit surface is
  expected; if removing a callsite changes an observable manager behavior, add
  a targeted offline test for it.
- **Full-daemon smoke (W4):** the verification gate, per §2 W4. This is the
  stage's "manual smoke test against a real Google account" required by the
  master design's definition of stage-complete.
- **Out of scope:** rewriting the broken Dropbox-era test files
  (`test_manager.py`, `test_main.py`, `test_daemon.py`) — that is Stage 7
  (test-infrastructure rewrite). Stage 6 does not "fix" them ad hoc.

## 7. Explicit non-goals

- Stage 5 carry-over polish: `writes.py` consolidation,
  `_wrap_metadata`/`_project_file_metadata` merge, `_count_request` threading
  into `changes.py`, `batch_size` tuning, `_throttle_up` / bandwidth policy.
- Cross-page change-feed cursor-durability (its own design problem; did not
  surface in the small single-page Stage 5 smoke sets).
- GUI packages (`maestral-qt`, `maestral-cocoa`) — deferred indefinitely; D4 is
  a documented break, not a port.
- Test-infrastructure rewrite (Stage 7) — broken Dropbox-era test files,
  in-memory fake-Drive, fault-injection, load test.
- `move(update_rev=)` — confirmed not load-bearing in Stage 5; add only if a
  rev-checked move precondition is actually needed (it is not, for this stage).
- A schema migration framework — explicitly rejected (W2): the index is a
  derived cache; re-derive from Drive.

## 8. Definition of done

1. Path-root methods + callsites removed; tree imports; offline suite green
   with the unchanged `--ignore` set.
2. Reset-on-version-bump policy documented in code (`main.py`) and in this spec.
3. D1 audit table recorded; D4 break recorded for the CHANGELOG.
4. Full-daemon smoke PASS (4/4 checks) against `cumulus@arko.net`, recorded in
   a stage deviations / smoke-test doc.
5. All changes committed to `google-drive-port`.

(Tagged release happens once at end of Stage 8, not per stage.)

## 9. Decisions locked during brainstorming

- **Scope:** tight "make the daemon real" stage (option A) — carry-over polish
  punted.
- **D2:** keep reset-on-version-bump; document it; no framework, no version
  stamp (the index is a derived cache, re-indexable anytime).
- **Path-root stubs:** remove entirely (grep-gated).
- **D1/D4:** D1 = recorded audit table; D4 = documented break, no model shims,
  GUI deferred.
- **Full-daemon smoke:** focused on the new thread / RPC / lifecycle layer; do
  not re-run the Stage 5 engine-behavior matrix.
