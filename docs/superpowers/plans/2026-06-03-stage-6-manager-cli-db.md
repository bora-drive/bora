# Stage 6 — Manager + CLI + DB schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four genuinely-remaining Stage 6 items — remove dead path-root code, document the reset-on-version-bump DB policy, record the D1 RPC audit + D4 GUI break, and run the first full-daemon end-to-end smoke test.

**Architecture:** Most of nominal Stage 6 (CLI/manager/models/RPC port) was absorbed in Stages 2–5. This stage is deletion + documentation + audit + verification. There is little new code: Tasks 1–2 are surgical edits gated by grep + import + offline-suite checks; Task 3 is a recorded audit; Task 4 is the make-or-break full-daemon smoke against the real `cumulus@arko.net` account, with any thread/lifecycle bugs fixed inline.

**Tech Stack:** Python 3, `pytest` (offline suite), the `cumulus` CLI + Pyro5 daemon, `googleapiclient` (smoke harness only), git.

**Spec:** `docs/superpowers/specs/2026-06-03-stage-6-manager-cli-db-design.md`

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `src/cumulus/manager.py` | delete 3 path-root methods + section header + 3 callsites + `except PathRootError` branch + now-unused import | 1 |
| `src/cumulus/config/main.py` | remove the two dead `path_root_*` config defaults (vestige, grep-gated) | 1 |
| `src/cumulus/main.py` | comment stating the reset-on-version-bump / derived-cache DB policy | 2 |
| `docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db-deviations.md` | D1 audit table, D4 documented break, smoke-test record | 3, 4 |
| smoke harness (uncommitted, e.g. `/tmp/stage6_smoke.py` + CLI commands) | drive the full daemon end-to-end | 4 |

---

## Task 1: Remove path-root vestiges from `manager.py`

Drive has no team-namespace / path-root concept. Three methods are inert stubs
(`check_and_update_path_root` returns `False`, `_needs_path_root_update`
returns `False`, `_update_path_root` raises `NotImplementedError`), called from
three no-op sites. Remove all of it. This is deletion-only — there is no new
behavior to test, so the verification is: grep-gate, tree imports, offline
suite stays green.

**Files:**
- Modify: `src/cumulus/manager.py` (lines ~370–419 methods; ~463–467, ~568–571, ~627–629 callsites; `PathRootError` import)
- Modify: `src/cumulus/config/main.py:56-57` (dead config defaults)

- [ ] **Step 1: Grep-gate — confirm no live in-scope caller depends on the names**

Run:
```bash
cd /home/exedev/maestral
grep -rn "check_and_update_path_root\|_needs_path_root_update\|_update_path_root" src/cumulus/
grep -rn "PathRootError" src/cumulus/
grep -rn "path_root_type\|path_root_nsid" src/cumulus/
```
Expected: the three method names appear ONLY in `src/cumulus/manager.py` (definitions + the three callsites). `PathRootError` appears only in `src/cumulus/exceptions.py` (the class definition) and `src/cumulus/manager.py:629` (the handler) — i.e. it is never raised in `src/cumulus/`. `path_root_type`/`path_root_nsid` appear only in `src/cumulus/config/main.py` (defaults). No `main.py` RPC method references any of them.

If any live in-scope caller outside `manager.py` is found, STOP and reconcile with the spec before deleting (the out-of-scope GUI is not a constraint; an in-scope caller is). If `PathRootError` is found *raised* anywhere in `src/cumulus/`, keep its import and leave a bare handler — but per the spike this should not happen.

- [ ] **Step 2: Delete the three path-root methods and their section header**

In `src/cumulus/manager.py`, delete the entire block from the section-header comment through the end of `_update_path_root` — currently lines ~370–419:

```python
    # ---- path root management --------------------------------------------------------

    def check_and_update_path_root(self) -> bool:
        ...
        raise NotImplementedError(
            "Drive has no team-namespace concept; "
            "_update_path_root should not be called."
        )
```

The line immediately after (`# ---- thread methods ----`) becomes the next code.

- [ ] **Step 3: Remove the `download_worker` callsite**

In `download_worker` (currently ~463–467), delete the path-root comment, the `if`, and its `return`:

```python
                # Check for root namespace updates. Don't apply any remote
                # changes in case of a changed root path.
                if self.check_and_update_path_root():
                    return

```
After deletion, `has_changes = self.sync.wait_for_remote_changes(...)` is followed directly by `if not running.is_set():`.

- [ ] **Step 4: Remove the `startup_worker` callsite**

In `startup_worker` (currently ~568–571), delete:

```python
            # Update path root and migrate local folders. This is required when a user
            # joins or leaves a team and their root namespace changes.
            self.check_and_update_path_root()

```
After deletion, `self.sync.client.get_space_usage()` is followed directly by `if not running.is_set():`.

- [ ] **Step 5: Remove the `except PathRootError` handler and its import**

In `_handle_sync_thread_errors` (currently ~627–629), delete the whole branch:

```python
        except PathRootError:
            self._logger.debug("API call failed due to path root error", exc_info=True)
            self.check_and_update_path_root()
```
Then remove `PathRootError` from the `from .exceptions import (...)` line at the top of `manager.py` (leave the other names in that import intact). Do NOT delete the `PathRootError` class from `exceptions.py` — it is harmless and out of scope.

- [ ] **Step 6: Remove the two dead path-root config defaults**

In `src/cumulus/config/main.py:56-57`, delete the two vestige lines (Step 1 confirmed nothing in `src/cumulus/` reads them):

```python
        "path_root_type": "user",  # the root folder type: team or user
        "path_root_nsid": "",  # the namespace id of the root path
```

- [ ] **Step 7: Verify the tree imports**

Run:
```bash
cd /home/exedev/maestral && python -c "import cumulus.manager, cumulus.main, cumulus.config.main; print('import OK')"
```
Expected: `import OK` with no `NameError`/`ImportError` (catches a missed `PathRootError` reference or a dangling callsite).

- [ ] **Step 8: Verify the offline suite stays green**

Run:
```bash
cd /home/exedev/maestral && pytest tests/offline --ignore=tests/offline/test_manager.py -q
```
Expected: same result as the Stage 5 baseline — **418 passed**, with the pre-existing `test_event_handler.py` fixture errors unchanged. `test_manager.py` stays ignored (it contains Dropbox-era `test_migrate_path_root_*` tests that call the now-deleted method; rewriting it is Stage 7, not this task). The pass count must NOT drop.

- [ ] **Step 9: Commit**

```bash
cd /home/exedev/maestral
git add src/cumulus/manager.py src/cumulus/config/main.py
git commit -m "$(printf 'refactor(stage-6): remove dead path-root (team-namespace) code\n\nDrive has no team-namespace concept. Delete check_and_update_path_root,\n_needs_path_root_update, _update_path_root, their three no-op callsites,\nthe except PathRootError branch + unused import, and the two dead\npath_root_* config defaults. Offline suite unchanged (418 passed).\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Task 2: Document the reset-on-version-bump DB policy

**Decision (spec W2):** the index/history/hash_cache tables are a derived cache
of Drive state. Schema changes are handled by dropping and re-deriving from
Drive — no migration framework, no `schema_version` stamp. This already happens
in `_update_from_pre_v1_6_0()`; make the policy explicit so a future reader
doesn't mistake the table-drop for a bug or reach for a migration framework.

**Files:**
- Modify: `src/cumulus/main.py` (the `_update_from_pre_v1_6_0` body, ~1565)

- [ ] **Step 1: Add the policy comment at the schema-reset site**

In `src/cumulus/main.py`, at the top of `_update_from_pre_v1_6_0`'s body (just under the `def`/docstring, before `db_path = ...`), add:

```python
        # DB-schema policy (Stage 6, D2): the index / history / hash_cache
        # tables are a DERIVED CACHE of Drive state, not a system of record.
        # There is deliberately no schema-version stamp and no migration
        # framework — on any schema change we DROP these tables and re-derive
        # them from Drive by reindexing. Re-indexing is cheap and always
        # available, so blind table-drop here is intentional, not data loss.
```

- [ ] **Step 2: Verify the tree imports**

Run:
```bash
cd /home/exedev/maestral && python -c "import cumulus.main; print('import OK')"
```
Expected: `import OK`.

- [ ] **Step 3: Commit**

```bash
cd /home/exedev/maestral
git add src/cumulus/main.py
git commit -m "$(printf 'docs(stage-6): make reset-on-version-bump DB policy explicit (D2)\n\nThe index DB is a derived cache of Drive state; schema changes are\nhandled by dropping and re-deriving via reindex, by design. No\nmigration framework, no schema-version stamp.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Task 3: Record the D1 RPC audit + D4 GUI-break decision

This task produces the stage's deviations doc with two documentation
deliverables: the D1 audit table (Pyro5 RPC surface confirmed Drive-shaped) and
the D4 decision (out-of-scope GUI `dbx_path` is a documented break, no model
shims). The smoke-test record (Task 4) is appended to the same file.

**Files:**
- Create: `docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db-deviations.md`

- [ ] **Step 1: Enumerate the Pyro5-exposed surface and scan for renamed-field crossings**

Run:
```bash
cd /home/exedev/maestral
grep -rn "dbx_path\|client_modified\|server_modified" src/cumulus/main.py src/cumulus/daemon.py
grep -n "def \|@property" src/cumulus/main.py | grep -iv "^\s*def _" | head -200
```
Expected: the first grep returns **no matches** in `main.py`/`daemon.py` (the RPC surface is `remote_path`-based, single `modified`, `rev`=`headRevisionId`). The `exceptions.py` `dbx_path` alias is acknowledged separately and is NOT part of the RPC method surface. If the first grep returns a match, record it as a straggler in the table below and fix it inline (rename to the Drive field) before proceeding.

- [ ] **Step 2: Write the deviations doc with the D1 table and D4 decision**

Create `docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db-deviations.md` with this content (fill the D1 verdict from Step 1's actual result):

```markdown
# Stage 6 — Plan deviations, D1/D4 record, and smoke-test record

**Plan:** docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db.md
**Spec:** docs/superpowers/specs/2026-06-03-stage-6-manager-cli-db-design.md

## Implementation summary

(Filled in as tasks land — commit hashes, offline-suite count.)

## D1 — Pyro5 RPC field-rename audit

The daemon exposes `Maestral` via `expose(Maestral)` (`daemon.py:466`). Every
field crossing the Pyro5 boundary was checked against the renamed Drive shape:

| Check | Expected | Verdict |
|---|---|---|
| `dbx_path` parameters/returns on RPC methods | none (use `remote_path`) | PASS — no matches in main.py/daemon.py |
| `client_modified` / `server_modified` | none (single `modified`) | PASS — no matches |
| `rev` semantics | `headRevisionId` (set in Stage 5) | PASS |

Conclusion: the RPC surface is Drive-shaped; no shim or version-bump needed.
The one pre-existing back-compat alias (`exceptions.py:55-70`,
`CumulusApiError.dbx_path`/`dbx_path_from` → `remote_path`/`remote_path_from`)
is left as-is — it already exists and costs nothing; Stage 6 does not extend
the pattern.

## D4 — GUI compatibility: documented break

The out-of-scope GUI packages (`maestral-qt`, `maestral-cocoa`) import
`SyncEvent` / `FileMetadata` expecting a `dbx_path` attribute that no longer
exists. **Decision: intentional break.** No model-level compat shims are added
(shims for packages we neither port nor test are dead weight). The GUI port is
deferred indefinitely (master design §3). To be noted in the CHANGELOG at
Stage 8.

## Smoke-test record

(Filled in by Task 4.)
```

- [ ] **Step 3: Commit**

```bash
cd /home/exedev/maestral
git add docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db-deviations.md
git commit -m "$(printf 'docs(stage-6): D1 RPC audit table + D4 documented GUI break\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Task 4: First full-daemon end-to-end smoke test (verification gate)

This is the make-or-break for Stage 6: the daemon process, the Pyro5 server,
the manager threads (startup/download/upload/connection-monitor), startup
sequencing, and the CLI→daemon RPC roundtrip have never run together (Stage 5
hand-drove `SyncEngine` cycles in-process). Validate the new thread/RPC/lifecycle
layer against the real account. Engine behaviors (conflict/move/trash/restore)
are already 7/7 in Stage 5 and are NOT re-run here.

**Account/config:** linked account `cumulus@arko.net`, config `smoke-test`
(established in Stages 4–5). **Keyring caveat:** do NOT run the offline suite
against a different config while the `smoke-test` link is active, and bracket
any keyring-touching test run with a backup/restore of
`~/.local/share/python_keyring/keyring_pass.cfg` (see memory
`feedback_keyring_test_collision.md`).

**Setup note:** the full daemon performs an initial sync (`startup_worker` →
`download_sync_cycle`), which downloads existing My-Drive content into the sync
folder. `cumulus@arko.net` is a dedicated test account, so this is acceptable;
point the sync folder at a fresh empty directory and let initial sync settle
before the autonomous-sync checks. Propagation latency: Drive's `changes.list`
lags a few seconds — poll with bounded retries, never assert immediately after
a mutation (carried from the Stage 5 smoke).

**Files:**
- Smoke harness (uncommitted) — a shell/Python sequence driving the CLI; Drive-side file creation uses raw `googleapiclient` with the stored creds.

- [ ] **Step 1: Pre-flight — confirm the link and a clean sync dir**

```bash
cd /home/exedev/maestral
export CUMULUS_SMOKE_DIR=$(mktemp -d)
# point config 'smoke-test' at the temp sync dir, then:
cumulus auth status -c smoke-test
```
Expected: reports linked as `cumulus@arko.net`. If not linked, re-link per the Stage 2/4 procedure before continuing.

- [ ] **Step 2: Start the daemon and confirm the Pyro5 server + RPC roundtrip (checks 1 & 2)**

```bash
cumulus start -c smoke-test
# wait for startup to settle, then:
cumulus status -c smoke-test
```
Expected — **check 1 (daemon + Pyro5 up):** `cumulus start` returns success and the daemon process is running. **check 2 (RPC roundtrip):** `cumulus status` connects over Pyro5 and returns sane Drive data (account email, connected/idle state, space usage) — not a connection error or stale/garbage fields. Record both outcomes.

- [ ] **Step 3: Autonomous upload (check 3a) — local→Drive without hand-driving a cycle**

```bash
echo "stage6 smoke $(date -u +%s)" > "$CUMULUS_SMOKE_DIR/stage6-up.txt"
# poll with bounded retries (up to ~60s), do NOT assert immediately:
cumulus ls / -c smoke-test     # or query Drive directly via API for stage6-up.txt
```
Expected — **check 3a:** the upload thread (not a hand-driven cycle) pushes `stage6-up.txt` to Drive; it appears in `cumulus ls` / a Drive `files.list` within the poll window. Record pass/fail and the observed latency.

- [ ] **Step 4: Autonomous download (check 3b) — Drive→local via the poll loop**

Create a file on Drive *out of band* (raw `googleapiclient` `files.create` using the stored `smoke-test` creds, parented in My-Drive root), e.g. `stage6-down.txt`. Then:
```bash
# poll the local sync dir with bounded retries (up to ~60s):
ls -la "$CUMULUS_SMOKE_DIR"
```
Expected — **check 3b:** the download poll loop (`wait_for_remote_changes` → `download_sync_cycle`) pulls `stage6-down.txt` into `$CUMULUS_SMOKE_DIR` within the poll window. Record pass/fail.

- [ ] **Step 5: Clean shutdown (check 4)**

```bash
cumulus stop -c smoke-test
```
Expected — **check 4:** the daemon stops cleanly — worker threads join, `client.close()` fires (no "session not closed"/leaked-socket warnings), no orphaned daemon process and no stale lock file left behind. Verify no lingering process:
```bash
cumulus status -c smoke-test   # expect: "not running" / cannot connect
```

- [ ] **Step 6: Cleanup — trash the Drive items created during the run**

Trash `stage6-up.txt` and `stage6-down.txt` on Drive (raw API `files.update(trashed=True)` or `cumulus`-side delete), and `rm -rf "$CUMULUS_SMOKE_DIR"`. Restore the `smoke-test` config's original sync-dir setting if it was changed.

- [ ] **Step 7: If any check failed — fix inline (systematic-debugging), then re-run**

The Stage 5 carry-over predicted thread/startup/shutdown bugs would first surface here. For each failure: form a hypothesis about the thread/RPC/lifecycle layer, fix it in the relevant source file (`manager.py`, `main.py`, `daemon.py`, or `sync.py`), re-run the affected check, and record the bug + fix in the deviations doc's "Smoke-test findings (real bugs)" section. Commit each fix separately with a `fix(stage-6): ...` message. Re-run Steps 2–5 until 4/4 pass.

- [ ] **Step 8: Record the result and finalize the deviations doc**

Fill in the "Smoke-test record" section of
`docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db-deviations.md` with a
4-row table (checks 1, 2, 3a/3b, 4), the date, account/config, any bugs found +
fixed, and the overall PASS/FAIL. Also fill the "Implementation summary"
(commit range, offline-suite count). Then commit:

```bash
cd /home/exedev/maestral
git add docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db-deviations.md
git commit -m "$(printf 'docs(stage-6): full-daemon smoke-test record + deviations\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Definition of done (from spec §8)

1. Path-root methods + callsites + config defaults removed; tree imports; offline suite green (418 passed, unchanged `--ignore`). — Task 1
2. Reset-on-version-bump policy documented in `main.py` and the spec. — Task 2
3. D1 audit table recorded; D4 break recorded for the CHANGELOG. — Task 3
4. Full-daemon smoke PASS (4/4 checks) against `cumulus@arko.net`, recorded in the deviations doc. — Task 4
5. All changes committed to `google-drive-port`.
```
