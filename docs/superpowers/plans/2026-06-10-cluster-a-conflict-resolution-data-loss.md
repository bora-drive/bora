# Cluster A — Conflict-Resolution Data-Loss Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the sync engine from silently overwriting a locally-modified file (losing the user's edit) when the same file changed on the server, by routing the rev-mismatch upload failure into the existing conflicted-copy machinery.

**Architecture:** Conflict handling is split across the upload and download paths. On a rev mismatch, `writes.upload` raises `DataChangedError` (writes.py:204-216) — the definitive "remote moved under us" conflict signal. Today `_on_local_file_modified` catches it, mislabels it "modified during upload," and returns `SyncStatus.Skipped` *without recording a sync error* (sync.py:2537-2542). That drops the conflict signal: the download side's "unresolved upload error → Conflict" branch (`_check_download_conflict`, sync.py:3302-3315) never fires, so detection falls to the timing-sensitive `_ctime_newer_than_last_sync` heuristic, which in the upload-first interleaving returns "no unsynced changes" → `RemoteNewer` → remote overwrites local, **no conflicted copy, edit lost**. The fix lets the `DataChangedError` (a `SyncError` subclass) propagate so `_create_remote_entry`'s `except SyncError` handler records an Up error for the path; the download then sees the error and creates the conflicted copy. This restores the atomic-conflict semantics Dropbox got from the server (see [dropbox-vs-drive-differences](../references/dropbox-vs-drive-differences.md) §18).

**Tech Stack:** Python, pytest, the offline `SyncEngine` fixture + `FakeDrive` (tests/offline), and the linked real-account integration suite (tests/linked/integration).

**Evidence:** Reproduced live 2026-06-10 — `test_file_conflict_modified` failed 2/5 runs; failing runs log `Could not upload "/file.txt": the file was modified during upload` → `Local item "/file.txt" has no unsynced changes: remote item is newer` → `Created local file "/file.txt"` (remote content written over the local edit, no conflicted copy). Passing runs log `has unsynced local changes: conflict` → `Download conflict: renamed ... conflicted copy`. The determinant is whether the upload or download worker reaches the file first.

---

## File structure

- **Modify** `src/cumulus/sync.py` — `_on_local_file_modified` (~2537-2542): stop swallowing the rev-mismatch `DataChangedError`; let it surface as an Up sync-error.
- **Create/extend** `tests/offline/test_sync.py` — a deterministic regression test that wires `FakeDrive` into the offline `sync_engine`, forces a rev mismatch, and asserts an Up conflict error is recorded (not a silent skip).
- **Modify** `tests/linked/integration/test_sync.py` — remove the cluster-A xfail markers as each test goes green; verify the replace-with-unsynced-changes siblings.

---

### Task 1: Deterministic offline regression test (red)

**Files:**
- Test: `tests/offline/test_sync.py`

The existing `sync_engine` fixture (tests/offline/test_sync.py:185-204) gives a real on-disk sync folder + in-memory index but does not wire a fake server, so an actual upload can't run. Add a fake-server-backed variant and a test that drives `_on_local_file_modified` through a rev mismatch. Reuse the FakeDrive wiring pattern from `tests/offline/test_writes.py` (`client._get_service = Mock(return_value=fake_service())`) and the seeding helpers already in this file (`_write_local_file`, `_seed_index_entry`).

- [ ] **Step 1: Write the failing test**

```python
# Add near the other conflict tests in tests/offline/test_sync.py.
# Imports assumed present in this module: Mock, SyncDirection, ChangeType,
# SyncEvent, SyncStatus, ItemType. Add a FakeDrive import mirroring test_writes.py.
from unittest.mock import Mock
from tests.offline.utils.fake_drive import FakeDrive  # match the import path used in test_writes.py


def _local_modified_event(sync, remote_path, content_hash):
    """A local file-modified upload SyncEvent."""
    return SyncEvent(
        direction=SyncDirection.Up,
        item_type=ItemType.File,
        sync_time=datetime.today().timestamp(),
        remote_path=remote_path,
        remote_path_lower=remote_path.lower(),
        remote_id="id:remote",
        local_path=sync.to_local_path_from_cased(remote_path),
        rev=None,
        content_hash=content_hash,
        symlink_target=None,
        change_type=ChangeType.Modified,
        status=SyncStatus.Queued,
        size=0,
        completed=0,
    )


def test_upload_rev_mismatch_records_conflict_not_silent_skip(sync_engine):
    """A modify-upload whose remote rev moved must surface a conflict signal.

    Regression for the cluster-A data-loss bug: the rev-mismatch DataChangedError
    was swallowed as SyncStatus.Skipped with no sync error, so the download side
    never created a conflicted copy and the local edit was overwritten.
    """
    remote_path = "/conflict_up.txt"

    # FakeDrive holds the file at a NEWER head revision than our index.
    fake = FakeDrive()
    file_id = fake.add_file(remote_path, content=b"remote content 2",
                            head_revision_id="REV-REMOTE-NEW")
    sync_engine.client._get_service = Mock(return_value=fake.service())
    # Point the path cache at the fake file so the upload resolves it.
    sync_engine.client.path_cache.add(remote_path.lower(), file_id, parent_id="ROOT")

    # Index + local disk reflect an OLDER rev with locally-modified content.
    local_data = b"local edit the user wants to keep"
    _write_local_file(sync_engine, remote_path, local_data)
    _seed_index_entry(sync_engine, remote_path, rev="REV-OLD",
                      content_hash=_md5(b"original content"), last_sync=0.0)

    event = _local_modified_event(sync_engine, remote_path, content_hash=_md5(local_data))

    # Drive the full upload wrapper (it catches SyncError and records it).
    sync_engine._create_remote_entry(event)

    # The conflict signal MUST be recorded for the path on the Up direction, so the
    # download side will create the conflicted copy instead of overwriting.
    up_errors = sync_engine.sync_errors_for_path(
        remote_path.lower(), direction=SyncDirection.Up
    )
    assert len(up_errors) > 0, "rev-mismatch upload silently skipped — local edit would be lost"
```

> NOTE for the implementer: confirm the exact `FakeDrive` constructor / `add_file` / `service()` / head-revision API against `tests/offline/test_writes.py` and `tests/offline/test_fake_drive.py`, and the `path_cache.add(...)` signature against `src/cumulus/path_cache.py`. Adjust the seeding calls to match; the *assertion* is the contract that must not change.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_sync.py::test_upload_rev_mismatch_records_conflict_not_silent_skip -v`
Expected: FAIL — `up_errors` is empty (the current code returns `SyncStatus.Skipped` and `_create_remote_entry` then clears errors).

- [ ] **Step 3: Commit the red test**

```bash
git add tests/offline/test_sync.py
git commit -m "test(cluster-a): failing regression for swallowed rev-mismatch conflict"
```

---

### Task 2: Surface the rev-mismatch as a conflict (green)

**Files:**
- Modify: `src/cumulus/sync.py:2537-2542` (the `except DataChangedError` block in `_on_local_file_modified`)

`writes.upload` raises `DataChangedError` **only** for the rev-precondition mismatch (writes.py:204-216) — never for a benign mid-upload local change — so it is always a real conflict and must not be skipped. `DataChangedError` is a `SyncError` subclass (exceptions.py:87), so letting it propagate routes it to `_create_remote_entry`'s `except SyncError → _handle_sync_error(direction=Up)` handler (sync.py:2343-2345), which records the Up error the download side needs.

- [ ] **Step 1: Remove the swallow**

Delete the `except DataChangedError` branch so the error propagates. The benign `except (NotFoundError, NotAFolderError, IsAFolderError)` branch immediately above it stays unchanged.

```python
        try:
            with self._parallel_up_semaphore:
                md_new = self.client.upload(
                    event.local_path,
                    event.remote_path,
                    autorename=True,
                    write_mode=mode,
                    update_rev=local_rev,
                    sync_event=event,
                )
        except (NotFoundError, NotAFolderError, IsAFolderError):
            # Note: NotAFolderError can be raised when a parent in the local path
            # refers to a file instead of a folder.
            self._logger.debug(
                'Could not upload "%s": the file does not exist', event.local_path
            )
            return SyncStatus.Skipped
        # DataChangedError (rev precondition mismatch) is intentionally NOT caught
        # here: it means the remote moved under us — a genuine conflict. Letting it
        # propagate as a SyncError records an Up error for the path, so
        # _check_download_conflict's "unresolved upload error -> Conflict" branch
        # fires and the download path creates the conflicted copy. Swallowing it as
        # Skipped drops the conflict signal and the local edit is overwritten
        # (cluster-A data-loss bug). See dropbox-vs-drive-differences.md §18.
```

- [ ] **Step 2: Run the new test to verify it passes**

Run: `python -m pytest tests/offline/test_sync.py::test_upload_rev_mismatch_records_conflict_not_silent_skip -v`
Expected: PASS — an Up error is now recorded.

- [ ] **Step 3: Run the full offline sync suite (no regressions)**

Run: `python -m pytest tests/offline/test_sync.py -v`
Expected: all PASS, including the existing `test_conflict_*` and echo-suppression tests.

- [ ] **Step 4: Commit**

```bash
git add src/cumulus/sync.py
git commit -m "fix(cluster-a): treat rev-mismatch upload as conflict, not silent skip"
```

---

### Task 3: Confirm the download side converges to a conflicted copy (offline)

**Files:**
- Test: `tests/offline/test_sync.py`

Prove the recorded Up error flips the download decision to `Conflict` (the branch that creates the conflicted copy), so the chain end-to-end preserves the local edit.

- [ ] **Step 1: Write the test**

```python
def test_download_conflict_when_upload_error_present(sync_engine):
    """With an unresolved Up error for the path, the remote change is a Conflict
    (which triggers conflicted-copy creation) — not a RemoteNewer overwrite."""
    remote_path = "/conflict_down.txt"
    local_data = b"local edit"
    _write_local_file(sync_engine, remote_path, local_data)
    _seed_index_entry(sync_engine, remote_path, rev="REV-OLD",
                      content_hash=_md5(b"original"), last_sync=0.0)

    # Simulate the upload having recorded an Up conflict error for this path.
    err = DataChangedError("Item changed on Google Drive", "rev mismatch",
                           remote_path=remote_path,
                           local_path=sync_engine.to_local_path_from_cased(remote_path))
    sync_engine._handle_sync_error(err, direction=SyncDirection.Up)

    event = _download_event(sync_engine, remote_path, rev="REV-REMOTE-NEW",
                            content_hash=_md5(b"remote content 2"))

    assert sync_engine._check_download_conflict(event) is Conflict.Conflict
```

- [ ] **Step 2: Run to verify it passes**

Run: `python -m pytest tests/offline/test_sync.py::test_download_conflict_when_upload_error_present -v`
Expected: PASS (this exercises the already-correct branch at sync.py:3302-3315; it characterizes the contract Task 2 depends on).

- [ ] **Step 3: Commit**

```bash
git add tests/offline/test_sync.py
git commit -m "test(cluster-a): download conflict-check honors recorded upload error"
```

---

### Task 4: Live confirmation — pure conflict tests

**Files:**
- Modify: `tests/linked/integration/test_sync.py` — `test_file_conflict_modified` (xfail at :567), `test_file_conflict_created` (xfail at :601)

- [ ] **Step 1: Remove the two xfail markers**

Delete the `@pytest.mark.xfail(...)` decorators above `test_file_conflict_modified` and `test_file_conflict_created`.

- [ ] **Step 2: Run each repeatedly against the real account**

Set `CUMULUS_TEST_REFRESH_TOKEN` (re-mint if expired — Testing-mode tokens last ~7 days). Do **not** run `tests/offline` concurrently (keyring collision). In tmux:

Run: loop each test ~8× —
`for i in $(seq 8); do python -m pytest "tests/linked/integration/test_sync.py::test_file_conflict_modified" -q || echo "FAIL $i"; done`
then the same for `test_file_conflict_created`.
Expected: all green, no `FAIL` lines. The data-loss interleaving (upload-first) must now also produce a conflicted copy.

- [ ] **Step 3: Commit**

```bash
git add tests/linked/integration/test_sync.py
git commit -m "test(cluster-a): un-xfail conflict tests — data-loss fix verified live"
```

---

### Task 5: Verify and clear the replace-with-unsynced-changes siblings

**Files:**
- Modify: `tests/linked/integration/test_sync.py` — xfails at :647, :714, :790, :856 (the four `*_replaced_*_and_unsynced_*_changes` tests)

These exercise file↔folder replacement while the other side has unsynced changes. They are the same "conflict signal lost between upload-attempt and download-heuristic" family but go through different handlers (`_on_local_deleted`, `_on_remote_folder`/`_on_remote_file`, folder-vs-file type mismatch). Verify whether Task 2 fixes each; any that remain need their own targeted handling.

- [ ] **Step 1: Run each sibling ~6× with the debug log captured**

For each test, loop it and, on any failure, inspect `/home/exedev/.cache/cumulus/cumulus-test-config-integration.log` for the `/file.txt` (or `/folder`) conflict decision — confirm whether it now reaches `has unsynced local changes: conflict` / `Download conflict: renamed`.
Expected: identify which pass outright after Task 2 and which still drop the local change.

- [ ] **Step 2: For each test that is now green, remove its xfail; commit**

```bash
git add tests/linked/integration/test_sync.py
git commit -m "test(cluster-a): un-xfail replace+unsynced siblings fixed by conflict-signal fix"
```

- [ ] **Step 3: For any sibling still failing, open a focused follow-up**

If a sibling still loses the local change, do NOT force its xfail off. Capture the failing log, identify the handler that drops the signal (likely a type-mismatch path where the local unsynced change is deleted rather than moved to a conflicted copy), and add a sub-task here with the pinned mechanism before fixing. Keep its xfail with an updated, accurate reason until fixed.

---

### Task 6: Full regression gate

- [ ] **Step 1: Offline suite green (no `--ignore`)**

Run: `python -m pytest tests/offline -q`
Expected: all PASS (470+ tests). (Account must not be linked-and-running concurrently.)

- [ ] **Step 2: Full linked integration run (tmux, real account)**

Run: `python -m pytest tests/linked/integration -q -ra`
Expected: previously-12 xfails reduced by the cluster-A tests now passing; zero unexpected failures.

- [ ] **Step 3: Commit any final marker/doc updates**

```bash
git add -A
git commit -m "test(cluster-a): conflict-resolution data-loss fixed; gate green"
```

---

## Self-review notes
- **Spec coverage:** the data-loss mechanism (upload swallows rev-mismatch → download heuristic overwrites) is addressed by Task 2; Tasks 1/3 lock the contract offline and deterministically; Tasks 4/5 confirm live and sweep the sibling family; Task 6 is the gate.
- **No silent caps:** Task 5 explicitly forbids re-hiding an unfixed sibling behind xfail without a pinned mechanism.
- **Type consistency:** `sync_errors_for_path(path_lower, direction=SyncDirection.Up)`, `_handle_sync_error(err, direction=SyncDirection.Up)`, and `_check_download_conflict(event) -> Conflict` match the signatures in sync.py (837, 1559, 3260) and the `Conflict` enum (sync.py:162).
