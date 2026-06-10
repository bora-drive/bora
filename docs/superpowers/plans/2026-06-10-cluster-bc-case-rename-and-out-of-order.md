# Cluster B+C — Case-Only Renames & Out-of-Order Change Application — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. REQUIRED for each fix: superpowers:systematic-debugging — these mechanisms are NOT yet pinned to a confirmed root cause; do Phase 1 (reproduce + pin) before writing any fix.

**Goal:** Make case-only renames (local and remote) and out-of-order remote change application converge correctly, removing three Stage-5 sync-engine xfails.

**Architecture:** Three related sync-engine ordering/identity bugs, none involving data loss:
- **B1 — local case-only rename** (`test_case_change_local`, xfail at test_sync.py:304): a live `/folder` → `/FOLDER` rename arrives from watchdog as a Dir/FileMovedEvent and is missed by the move handling, so `get_metadata("/folder")` afterward raises `NotFoundError`. Note the **while-stopped** variant (`test_case_change_local_while_stopped`, :340) passes — it goes through `_get_local_changes_while_inactive()` which decomposes the rename into create+delete — so the gap is specifically `_on_local_moved` (sync.py:2374) handling a case-only directory rename.
- **B2 — remote case-only rename** (`test_case_change_remote`, xfail at test_sync.py:376): a remote `/folder` → `/FOLDER` rename is not reflected locally; `get_metadata("/folder")` raises `NotFoundError`. This is the change-feed/download apply path (`_on_remote_folder`, sync.py:3668, and `correct_case`, sync.py:1287) not handling a name that differs only by case under the case-fold key.
- **C — out-of-order application** (`test_out_of_order_indexing`, xfail at test_sync.py:1434): applying remote changes children-before-parents via `_create_local_entry`/`_ensure_parent` leaves a child (`/parent/child_2`) uncreated. Related to the Drive change-feed's lack of parent-before-child ordering (see [dropbox-vs-drive-differences](../references/dropbox-vs-drive-differences.md) §14).

**Tech Stack:** Python, pytest, offline `SyncEngine` + `FakeDrive` (tests/offline), linked real-account suite (tests/linked/integration). Prefer deterministic offline reproductions; use the linked test only for final confirmation.

---

## File structure

- **Modify** `src/cumulus/sync.py` — `_on_local_moved` (B1, ~2374), `_on_remote_folder` / `correct_case` / `_correct_case_helper` (B2, ~1287/3668), `_create_local_entry` / `_ensure_parent` (C, ~3474/3518).
- **Create/extend** `tests/offline/test_sync.py` — deterministic regression tests for each.
- **Modify** `tests/linked/integration/test_sync.py` — remove each xfail once green.

---

### Task 1 (B1): Pin the local case-only-rename mechanism

**Files:** investigation only — `src/cumulus/sync.py:2374` (`_on_local_moved`), `tests/linked/integration/test_sync.py:305`.

- [ ] **Step 1: Reproduce and capture the decision path**

Run `test_case_change_local` with `--runxfail` against the real account in tmux, with the debug log isolated:
`: > /home/exedev/.cache/cumulus/cumulus-test-config-integration.log; python -m pytest "tests/linked/integration/test_sync.py::test_case_change_local" --runxfail -q`
Then grep the log for `/folder`, `/FOLDER`, `moved`, `correct_case`, `NotFoundError`.
Expected output: confirm whether `_on_local_moved` receives the Dir-moved event and what it does with a source/dest that are case-fold-equal (same folded key).

- [ ] **Step 2: Read `_on_local_moved` and the move-to-remote path**

Identify why a case-only move is not propagated: likely the folded source and destination keys are equal, so the move is treated as a no-op (the path cache keys by folded path) and the remote `name` is never updated via `files.update(name=...)`. Write the pinned mechanism into this plan as a one-paragraph note before proceeding.

- [ ] **Step 3: Decide the fix shape**

Likely fix: when a move's source and destination differ only by case (folded keys equal but cased names differ), still issue the remote rename (`client.move` / `files.update(name=...)`) and update the cache's cased name, rather than short-circuiting as a no-op. Record the exact approach here.

---

### Task 2 (B1): Deterministic test + fix

**Files:** `tests/offline/test_sync.py`, `src/cumulus/sync.py` (`_on_local_moved`).

- [ ] **Step 1: Write the failing test**

Wire `FakeDrive` into the offline `sync_engine` (pattern from tests/offline/test_writes.py). Seed `/folder` (folder) in the fake + index + on disk, then drive `_on_local_moved` with a Dir-moved event from `/folder` to `/FOLDER` and assert the fake's stored `name` becomes `FOLDER` and `get_metadata` resolves the item under the new casing.

```python
def test_local_case_only_rename_propagates_to_remote(sync_engine):
    """A /folder -> /FOLDER local rename must update the remote name, not no-op."""
    # ... seed FakeDrive folder + index + on-disk dir as /folder (see test_writes.py
    #     for the FakeDrive wiring and tests/offline/test_sync.py helpers) ...
    event = _dir_moved_event(sync_engine, "/folder", "/FOLDER")  # build per existing patterns
    sync_engine._on_local_moved(event)
    md = sync_engine.client.get_metadata("/FOLDER")
    assert md is not None and md.name == "FOLDER"
```

> The implementer fills the seeding to match FakeDrive's API; the assertion (remote name becomes `FOLDER`, resolvable under the new casing) is the contract.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/offline/test_sync.py::test_local_case_only_rename_propagates_to_remote -v`
Expected: FAIL (rename is short-circuited; remote `name` stays `folder`).

- [ ] **Step 3: Implement the minimal fix in `_on_local_moved`**

Per the Task 1 decision: detect case-only moves (folded keys equal, cased basenames differ) and still issue the remote rename + cache cased-name update.

- [ ] **Step 4: Run to verify it passes; run the offline sync suite**

Run: `python -m pytest tests/offline/test_sync.py -v`
Expected: new test PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add tests/offline/test_sync.py src/cumulus/sync.py
git commit -m "fix(cluster-b): propagate local case-only renames to the remote name"
```

---

### Task 3 (B2): Pin + fix the remote case-only-rename apply

**Files:** `src/cumulus/sync.py` (`_on_remote_folder` ~3668, `correct_case`/`_correct_case_helper` ~1287/1330), `tests/offline/test_sync.py`, `tests/linked/integration/test_sync.py:377`.

- [ ] **Step 1: Reproduce and pin**

Run `test_case_change_remote` with `--runxfail`; capture the log. Determine why a remote `/folder` → `/FOLDER` change-feed entry does not re-case the local folder (candidate: the apply path keys by folded path, finds the existing entry equal, and skips the local rename; or `correct_case` returns the stale casing). Write the pinned mechanism here.

- [ ] **Step 2: Write the failing offline test**

Seed `/folder` synced (fake + index + disk). Build a download SyncEvent for the same item with `remote_path="/FOLDER"` (same folded key, new casing) and apply it via `_create_local_entry`. Assert the on-disk directory is renamed to `FOLDER` and the index cased path is updated.

```python
def test_remote_case_only_rename_recases_local(sync_engine):
    # ... seed /folder synced ...
    event = _download_event(sync_engine, "/FOLDER", rev="folder", content_hash="folder",
                            change_type=ChangeType.Moved)  # or the type the feed delivers
    sync_engine._create_local_entry(event)
    assert os.path.isdir(sync_engine.to_local_path_from_cased("/FOLDER"))
    assert sync_engine.get_index_entry("/folder").remote_path_cased == "/FOLDER"
```

- [ ] **Step 3: Run (fail) → implement fix → run (pass)**

Fix likely in `_on_remote_folder` / case-correction: when the incoming cased name differs from the stored cased name under an equal folded key, perform a local rename and update the index cased path.
Run: `python -m pytest tests/offline/test_sync.py -v` → all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/offline/test_sync.py src/cumulus/sync.py
git commit -m "fix(cluster-b): apply remote case-only renames to the local folder + index"
```

---

### Task 4 (C): Pin + fix out-of-order application

**Files:** `src/cumulus/sync.py` (`_create_local_entry` ~3474, `_ensure_parent` ~3518), `tests/offline/test_sync.py`, `tests/linked/integration/test_sync.py:1434`.

- [ ] **Step 1: Pin the mechanism**

`_ensure_parent` (sync.py:3518-3546) already syncs a missing parent before a child by calling `get_metadata` + `_on_remote_folder`. Reproduce `test_out_of_order_indexing` with `--runxfail` and determine why `/parent/child_2` is left uncreated when changes apply children-first: candidates — (a) `_ensure_parent` resolves the parent but the second child's own apply is skipped because an earlier failed/short-circuited pass marked state inconsistently; (b) `get_metadata` for the not-yet-cached parent raises `NotFoundError` and the child is dropped rather than retried. Write the pinned mechanism here.

- [ ] **Step 2: Write the failing offline test**

Apply a batch of download events in reverse order (`/parent/child_2`, `/parent/child_1`, `/parent`) and assert all three exist locally and in the index afterward.

```python
def test_out_of_order_children_before_parent_all_created(sync_engine):
    # ... seed FakeDrive with /parent, /parent/child_1, /parent/child_2 ...
    for rp in ["/parent/child_2", "/parent/child_1", "/parent"]:
        sync_engine._create_local_entry(_download_event(sync_engine, rp, rev="...", content_hash="..."))
    for rp in ["/parent", "/parent/child_1", "/parent/child_2"]:
        assert os.path.exists(sync_engine.to_local_path_from_cased(rp))
        assert sync_engine.get_index_entry(rp.lower()) is not None
```

- [ ] **Step 3: Run (fail) → implement fix → run (pass)**

Fix likely in `_ensure_parent`/`_create_local_entry`: ensure a child whose parent is not yet present is reliably created after the parent is materialized (e.g., resolve the parent from the fake/cache rather than only `get_metadata`, or re-queue the child instead of dropping it on a transient parent miss). Keep the change minimal and within the existing `_tree_traversal` lock.
Run: `python -m pytest tests/offline/test_sync.py -v` → all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/offline/test_sync.py src/cumulus/sync.py
git commit -m "fix(cluster-c): create children applied before their parent"
```

---

### Task 5: Live confirmation + remove xfails

**Files:** `tests/linked/integration/test_sync.py` (xfails at :304, :376, :1434).

- [ ] **Step 1: Remove the three xfail markers.**

- [ ] **Step 2: Run each test ~6× against the real account (tmux; not concurrent with `tests/offline`).**

Run: loop `test_case_change_local`, `test_case_change_remote`, `test_out_of_order_indexing`.
Expected: all green, no failures.

- [ ] **Step 3: Full linked integration run.**

Run: `python -m pytest tests/linked/integration -q -ra`
Expected: the three xfails are gone; zero unexpected failures.

- [ ] **Step 4: Commit**

```bash
git add tests/linked/integration/test_sync.py
git commit -m "test(cluster-bc): un-xfail case-rename + out-of-order tests verified live"
```

---

## Self-review notes
- **Discipline:** each fix is gated on a pinned mechanism (Tasks 1/3/4 Step 1) — no fix before root cause, per systematic-debugging.
- **Placeholders:** the offline test bodies intentionally defer FakeDrive seeding detail to the implementer (the exact fake API is in tests/offline/test_writes.py / test_fake_drive.py); each test's *assertion* is fully specified and is the contract.
- **No data loss:** unlike cluster A, these are convergence/identity bugs; none silently discard user content.
