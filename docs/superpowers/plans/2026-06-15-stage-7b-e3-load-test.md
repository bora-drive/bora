# Stage 7b-E3 — 10k-File Initial-Sync Load Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hermetic, in-memory load test that drives the cumulus sync engine through a full initial index of a ~10,000-entry Drive tree against the fake-Drive — under a rate-limit storm and across a crash/resume — and proves the end state is complete and uncorrupted at scale.

**Architecture:** One new test module `tests/offline/test_load_initial_sync.py` following the E2 fault-module conventions (the `sync_engine` fixture + `_wire_fake` + `_poll_event` Mock sleep-neutralization). A `build_tree(...)` helper bulk-seeds the fake-Drive O(N). Two scenario *helpers* (`_run_storm`, `_run_crash_resume`) carry the logic + correctness assertions; they are called twice — once by tiny **non-slow** S0 guards (N≈150) that run in the normal green loop, and once by **`@pytest.mark.load`** S1/S2 tests (N=10k) that are deselected from the normal run and invoked explicitly at the gate. A minimal recurring fault hook `fail_every(key, n, exc)` is added to `tests/fake_drive.py` to arm the storm.

**Tech Stack:** Python 3.10+, pytest, the existing `tests/fake_drive.py` in-memory Drive, `cumulus.sync.SyncEngine.download_sync_cycle`, `cumulus.client.GoogleDriveClient._bootstrap_recursive` / `_retry_transient`, `cumulus.path_cache.PathIDCache`. Stdlib `tracemalloc` + `time.monotonic` for soft hang/leak guards.

---

## Background the implementer needs (read before starting)

These facts are load-bearing for the assertions. They are the *real* behavior of the
initial-index path and are asserted as-is (not as an idealized model):

1. **Cursor stays `""` until the whole index completes.** `_bootstrap_recursive`
   (`src/cumulus/client.py:1116-1132`) yields `cursor=""` for every page **except the
   last**, which yields the real `startPageToken`. So `download_sync_cycle`
   (`src/cumulus/sync.py:3030-3089`) keeps `remote_cursor == ""` until the entire index
   completes. A mid-index crash therefore **restarts the full listing** on resume — there
   is no resume-from-offset. S2 validates *idempotent re-indexing*, not resume-from-offset.

2. **The full flat listing is buffered in memory by design** (`client.py:1049-1052`).
   Memory is ~O(N) at the listing layer **on purpose**. The memory soft-guard is a
   *generous absolute ceiling* that catches a runaway/leak (O(N) duplication beyond the one
   expected buffer), **not** a sublinearity claim. The chunked apply + `gc.collect()` in
   `download_sync_cycle` keeps the apply layer flat on top of that one buffer.

3. **Deterministic instant backoff.** `_retry_transient` backs off via
   `self._poll_event.wait(sleep_for)` (`client.py:420`). The fixture replaces
   `client._poll_event` with a `Mock` whose `wait`/`is_set` return `False` ("not cancelled,
   not shut down") so backoff is instant with no behavior change. This is the exact
   neutralization used by `tests/offline/test_fault_ratelimit.py`.

4. **Page size is `_LIST_PAGE_SIZE = 1000`** (`client.py:116`). At N=10k the bootstrap
   yields ~10 pages; at N≈150 it yields a single page. The crash/resume helper lowers this
   constant via `monkeypatch` so even the small S0 run produces a genuine **multi-page,
   non-final-page** interruption (where `cursor == ""`).

5. **Folders are indexed too.** `apply_remote_changes` writes an index row for each folder
   event as well as each file (see `test_fault_crash.py::test_multi_item_batch_reapply_idempotent`,
   which asserts `get_index_entry("/newdir")`). So the expected index row count is
   `n_folders + n_files`.

6. **No manual path-cache seeding.** Unlike the E2 crash tests (which drive
   `list_remote_changes_iterator` from a real cursor and seed the cache by hand),
   `download_sync_cycle` from an empty cursor runs `_bootstrap_recursive`, which populates
   the path cache itself. `_ensure_root_id` is a no-op because the fixture sets
   `cache.root_id = ROOT_ID` (`client.py:428-434`). `_throttle_down` is a no-op because
   `bandwidth_limit_down` defaults to 0 (`client.py:781-785`). So no real sleeps and no
   network calls occur.

**Test-discipline guards (carried from E2 / cluster-E):** never run `tests/linked` (needs
a real account); never run `tests/offline` while a real smoke account is linked (keyring
collision); the slow `load` run is part of the E3 gate, **not** the per-task green loop.

**Per-task green-loop command (note the `-m "not load"`):**

```bash
python -m pytest tests/offline -m "not load" --benchmark-disable -q
```

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `tests/fake_drive.py` | Add `fail_every(key, n, exc)` recurring fault hook; extend `_consume_fail` to handle it. | Modify |
| `tests/offline/test_fake_drive.py` | Focused test of `fail_every`. | Modify |
| `pyproject.toml` | Register the `load` pytest marker. | Modify |
| `tests/offline/test_load_initial_sync.py` | New module: `sync_engine` fixture, `build_tree`, `_assert_complete_index`, `_run_storm`, `_run_crash_resume`, S0 (non-slow) guards, S1/S2 (`load`) scenarios. | Create |
| `docs/superpowers/plans/2026-06-15-stage-7b-e3-load-test-deviations.md` | Plan-vs-impl deltas, defect log, gate results, slow-run invocation/result, documented Stage-8 real-account smoke procedure. | Create |

---

## Task 1: Recurring fault hook `fail_every` on the fake-Drive

**Files:**
- Modify: `tests/fake_drive.py` (add `fail_every` after `fail_when` at `:133-139`; extend `_consume_fail` at `:141-156`)
- Test: `tests/offline/test_fake_drive.py`

The storm needs "fail every Nth call" on `files.list` and `files.get_media`. The existing
hooks (`fail_next` / `fail_after` / `fail_on_call` / `fail_when`) are one-shot or
single-predicate. Add the smallest viable recurring hook. Because `_retry_transient`
retries the *same* `.execute()`, spacing failures every `n>=2` calls guarantees a single
failure is followed by a successful retry — so the storm converges within
`_MAX_RETRY_ATTEMPTS` while still exercising the backoff path hundreds of times.

- [ ] **Step 1: Write the failing test**

Add to `tests/offline/test_fake_drive.py` (imports `FakeDrive`, `make_http_error` already
present in that module — verify and add if missing):

```python
def test_fail_every_fires_on_every_nth_call():
    """fail_every(key, n, exc) raises on every n-th matching .execute(),
    indefinitely, and tracks the number of times it fired."""
    from googleapiclient.errors import HttpError

    fake = FakeDrive()
    fake.add_file("/a.txt", b"x")
    rule = fake.fail_every("files.list", 3, make_http_error(429))
    svc = fake.service()

    outcomes = []
    for _ in range(9):
        try:
            svc.files().list(
                q="trashed=false", fields="nextPageToken, files(id)"
            ).execute()
            outcomes.append("ok")
        except HttpError:
            outcomes.append("fail")

    # Calls 3, 6, 9 (1-based) fail; the rest pass.
    assert outcomes == ["ok", "ok", "fail", "ok", "ok", "fail", "ok", "ok", "fail"]
    # The returned rule reference exposes the live fire counter.
    assert rule["fires"] == 3


def test_fail_every_does_not_affect_other_keys():
    """A fail_every rule on one key never fires for a different key."""
    fake = FakeDrive()
    fid = fake.add_file("/b.txt", b"y")
    fake.fail_every("files.list", 2, make_http_error(429))
    svc = fake.service()
    # files.get is a different key — must never fail.
    for _ in range(5):
        svc.files().get(fileId=fid, fields="id").execute()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py::test_fail_every_fires_on_every_nth_call -q`
Expected: FAIL with `AttributeError: 'FakeDrive' object has no attribute 'fail_every'`.

- [ ] **Step 3: Add `fail_every` after `fail_when`**

In `tests/fake_drive.py`, insert immediately after the `fail_when` method (after line 139):

```python
    def fail_every(self, key: str, n: int, exc: Exception) -> dict:
        """Raise *exc* on every *n*-th matching ``.execute()`` of *key*,
        indefinitely. The first n-1 calls pass, the n-th fails, and so on.

        Returns the rule dict; read ``rule["fires"]`` for the live count of
        times it has fired (used to prove a storm actually hit the backoff
        path rather than silently never firing)."""
        rule = {
            "key": key, "exc": exc, "every": n, "seen": 0, "fires": 0,
            "when": None, "after": False,
        }
        self._fail_rules.append(rule)
        return rule
```

- [ ] **Step 4: Extend `_consume_fail` to handle the recurring rule**

In `tests/fake_drive.py`, replace the body of `_consume_fail` (`:141-156`) with:

```python
    def _consume_fail(self, key: str, kwargs: dict, *, after: bool) -> None:
        """Fire the first matching, ready rule for (key, after-phase). At most
        one rule fires per call. A rule with a remaining ``skip`` count consumes
        the call toward its skip without firing. A recurring ``every`` rule fires
        on every n-th matching call and never auto-removes."""
        for rule in self._fail_rules:
            if rule["key"] != key or rule["after"] != after:
                continue
            if rule["when"] is not None and not rule["when"](**kwargs):
                continue
            if rule.get("every"):
                rule["seen"] += 1
                if rule["seen"] % rule["every"] == 0:
                    rule["fires"] += 1
                    raise rule["exc"]
                return
            if rule["skip"] > 0:
                rule["skip"] -= 1
                return
            rule["remaining"] -= 1
            if rule["remaining"] <= 0:
                self._fail_rules.remove(rule)
            raise rule["exc"]
```

Note: one-shot rules created by `fail_next`/`fail_after`/`fail_on_call`/`fail_when` have no
`"every"` key, so `rule.get("every")` is `None` (falsy) and they take the existing
skip/remaining path unchanged.

- [ ] **Step 5: Run both new tests to verify they pass**

Run: `python -m pytest tests/offline/test_fake_drive.py -k fail_every -q`
Expected: 2 passed.

- [ ] **Step 6: Run the full fake-drive suite (no regressions to the rule engine)**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: all pass (existing count + 2).

- [ ] **Step 7: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(7b-e3): add fail_every recurring fault hook to fake-Drive

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Load module scaffold — fixture, `build_tree`, shared assertion, `load` marker

**Files:**
- Modify: `pyproject.toml` (add `[tool.pytest.ini_options]` with the `load` marker)
- Create: `tests/offline/test_load_initial_sync.py`
- Test: a tiny non-slow `test_build_tree_shape` proves the seeder + a single clean
  `download_sync_cycle` index correctly (no faults yet).

`build_tree` must be **O(N)**: it bypasses `add_file`/`add_folder` (whose
`_resolve_path_to_parent_and_name` is O(N) per call → O(N²) overall) and calls
`fake._insert(name, parent_id, mime, content)` directly with parent ids it tracks itself.

- [ ] **Step 1: Register the `load` marker in `pyproject.toml`**

Append a new section after the `[tool.isort]` block (`pyproject.toml:111-112`):

```toml
[tool.pytest.ini_options]
markers = [
    "load: slow, large-scale load/correctness tests (deselect with -m 'not load')",
]
```

- [ ] **Step 2: Verify the marker is registered and the normal selection deselects it**

Run: `python -m pytest tests/offline -m "not load" --co -q | tail -3`
Expected: collection succeeds with no `PytestUnknownMarkWarning` for `load`.

- [ ] **Step 3: Write the module scaffold with the failing shape test**

Create `tests/offline/test_load_initial_sync.py`:

```python
"""Load / correctness-at-scale test for the initial download sync (Stage 7b-E3).

Drives SyncEngine.download_sync_cycle through a full initial index of a
~10,000-entry fake-Drive tree and proves the end state is complete and
uncorrupted — under a rate-limit storm (S1) and across a crash/resume (S2).

Two slow scenarios are marked @pytest.mark.load and excluded from the normal
offline run (run them with `-m load`). Tiny non-slow S0 guards run the SAME
helpers at N~=150 so harness/assertion regressions surface in the green loop.

Sleep neutralisation: _retry_transient backs off via _poll_event.wait().
The fixture replaces client._poll_event with a Mock whose wait/is_set return
False ("not cancelled, not shut down") so backoff is instant and deterministic
(mirrors tests/offline/test_fault_ratelimit.py).
"""

from __future__ import annotations

import hashlib
import os
import os.path as osp
import sqlite3
import time
import tracemalloc
from unittest.mock import Mock

import pytest

import cumulus.client as client_mod
from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.core import FullAccount
from cumulus.database.core import Database
from cumulus.exceptions import CancelledError
from cumulus.keyring import CredentialStorage
from cumulus.path_cache import PathIDCache
from cumulus.sync import SyncEngine
from cumulus.utils.appdirs import get_home_dir
from cumulus.utils.path import delete
from tests.fake_drive import FOLDER_MIME, ROOT_ID, FakeDrive, make_http_error

_STUB_ACCOUNT = FullAccount(
    account_id="account-id",
    display_name="Load Tester",
    email="load@example.com",
    profile_photo_url=None,
    disabled=False,
)

# Soft guards (see plan Background #2). Generous absolute ceilings tuned to the
# dev/CI machine at plan time; documented as soft guards so a slow machine does
# not produce false failures. They catch hangs/leaks, not performance.
_HANG_GUARD_SECONDS = 300.0
_MEMORY_CEILING_BYTES = 512 * 1024 * 1024


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# Engine fixture (mirrors test_fault_crash.py + ratelimit sleep neutralisation)
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_engine():
    local_dir = osp.join(get_home_dir(), "load_initial_sync_dir")
    os.mkdir(local_dir)

    # check_same_thread=False: apply_remote_changes uses a thread pool
    # (do_parallel) that touches the path cache from worker threads.
    cache = PathIDCache(Database(sqlite3.connect(":memory:", check_same_thread=False)))
    cache.root_id = ROOT_ID
    sync = SyncEngine(
        GoogleDriveClient(
            "load-initial-sync-config",
            CredentialStorage("load-initial-sync-config"),
            cache,
        )
    )
    sync.sync_path = local_dir
    sync.local_cursor = 0.0

    yield sync

    remove_configuration("load-initial-sync-config")
    delete(local_dir)


def _wire_fake(sync: SyncEngine, fake: FakeDrive) -> None:
    """Wire the fake service + neutralise the retry-backoff sleep."""
    sync.client._get_service = Mock(return_value=fake.service())
    sync.client._cached_account_info = _STUB_ACCOUNT
    # Instant, deterministic backoff: wait()->False (not cancelled),
    # is_set()->False (not shut down).
    sync.client._poll_event = Mock()
    sync.client._poll_event.wait = Mock(return_value=False)
    sync.client._poll_event.is_set = Mock(return_value=False)


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------


def build_tree(
    fake: FakeDrive, n_folders: int, n_files: int, branching: int = 16
) -> tuple[set[str], dict[str, str]]:
    """Bulk-seed the fake-Drive into a nested tree, O(N).

    Folders form a balanced tree of fan-out *branching* (bounded depth). Files
    are distributed round-robin across every folder (and the root). Each file
    gets DISTINCT content so md5s differ and per-file integrity is checkable.

    Returns (expected_folder_paths, {file_path: md5}).
    """
    id_by_index = {0: ROOT_ID}
    path_by_index = {0: ""}  # root path "" -> children compose to "/name"
    expected_folders: set[str] = set()

    for f in range(1, n_folders + 1):
        parent_idx = (f - 1) // branching
        parent_id = id_by_index[parent_idx]
        parent_path = path_by_index[parent_idx]
        name = f"folder-{f}"
        path = f"{parent_path}/{name}"
        fid = fake._insert(name, parent_id, FOLDER_MIME, b"")
        id_by_index[f] = fid
        path_by_index[f] = path
        expected_folders.add(path)

    expected_files: dict[str, str] = {}
    folder_indices = list(id_by_index.keys())  # includes root index 0
    for k in range(n_files):
        fidx = folder_indices[k % len(folder_indices)]
        parent_id = id_by_index[fidx]
        parent_path = path_by_index[fidx]
        name = f"file-{k}.txt"
        path = f"{parent_path}/{name}"
        content = f"cumulus-load-test file {k} unique-payload".encode()
        fake._insert(name, parent_id, "text/plain", content)
        expected_files[path] = _md5(content)

    return expected_folders, expected_files


# ---------------------------------------------------------------------------
# Shared correctness assertion (the S1/S2 hard gate)
# ---------------------------------------------------------------------------


def _assert_complete_index(
    sync: SyncEngine,
    expected_folders: set[str],
    expected_files: dict[str, str],
) -> None:
    """Assert the index + on-disk state is complete and uncorrupted."""
    # 1) Exactly one index row per remote_id; total == folders + files.
    remote_ids: list[str] = []
    for entry in sync.iter_index():
        remote_ids.append(entry.remote_id)
    assert len(remote_ids) == len(set(remote_ids)), "duplicate remote_id index rows"
    assert sync.index_count() == len(expected_folders) + len(expected_files), (
        f"index_count {sync.index_count()} != "
        f"{len(expected_folders) + len(expected_files)} expected"
    )

    # 2) Every folder present in the index and on disk.
    for path in expected_folders:
        assert sync.get_index_entry(path.lower()) is not None, f"missing folder row {path}"
        assert osp.isdir(sync.to_local_path_from_cased(path)), f"missing folder dir {path}"

    # 3) Every file present in the index, on disk, with bytes matching its md5.
    for path, expected_md5 in expected_files.items():
        entry = sync.get_index_entry(path.lower())
        assert entry is not None, f"missing file row {path}"
        local = sync.to_local_path_from_cased(path)
        assert osp.isfile(local), f"missing file on disk {path}"
        with open(local, "rb") as fh:
            assert _md5(fh.read()) == expected_md5, f"corrupt content {path}"

    # 4) Indexing finished and the cursor advanced off the empty sentinel.
    assert sync._state.get("sync", "did_finish_indexing") is True
    assert sync._state.get("sync", "indexing_counter") == 0
    assert sync.remote_cursor != "", "cursor must advance to the real start-token"
    assert sync.sync_errors == []


# ---------------------------------------------------------------------------
# S0 — non-slow shape guard (clean run, no faults)
# ---------------------------------------------------------------------------


def test_build_tree_shape(sync_engine: SyncEngine):
    """A clean initial sync of a small tree indexes every entry correctly.

    Guards build_tree + _assert_complete_index in the normal green loop.
    """
    fake = FakeDrive()
    expected_folders, expected_files = build_tree(fake, n_folders=20, n_files=80)
    _wire_fake(sync_engine, fake)

    sync_engine.download_sync_cycle()

    _assert_complete_index(sync_engine, expected_folders, expected_files)
```

- [ ] **Step 4: Run the shape test to verify it passes**

Run: `python -m pytest tests/offline/test_load_initial_sync.py::test_build_tree_shape -q`
Expected: PASS.

If it fails on the `index_count` assertion, confirm Background #5 (folders are indexed) by
printing `sync_engine.index_count()` vs `len(expected_folders)+len(expected_files)` — they
must be equal. Do not "fix" by loosening the assertion; if they genuinely differ, that is a
finding → apply the §8 triage protocol (see Task 5).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/offline/test_load_initial_sync.py
git commit -m "test(7b-e3): load module scaffold — build_tree, assertions, load marker

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Scenario S1 — initial sync under a rate-limit storm

**Files:**
- Modify: `tests/offline/test_load_initial_sync.py` (add `_run_storm` helper, `test_s0_storm_small`, `test_s1_storm_load`)

`_run_storm` arms a recurring 429 on `files.list` and a recurring rate-limit-403 on
`files.get_media`, runs one `download_sync_cycle`, asserts the complete-index gate, and
asserts the storm actually fired (combined `fires` > 0 — proving the backoff path was
exercised, not silently skipped). It is called by a tiny non-slow S0 guard and by the
`load`-marked S1 at N=10k.

- [ ] **Step 1: Write the failing S0 storm test (calls the not-yet-written helper)**

Append to `tests/offline/test_load_initial_sync.py`:

```python
# ---------------------------------------------------------------------------
# Scenario S1 — initial sync under a rate-limit storm
# ---------------------------------------------------------------------------


def _run_storm(
    sync: SyncEngine,
    fake: FakeDrive,
    expected_folders: set[str],
    expected_files: dict[str, str],
    *,
    list_every: int = 5,
    media_every: int = 7,
) -> None:
    """Arm a recurring 429/403 storm on list + download, run the full initial
    sync, and assert the hard correctness gate plus that the storm fired."""
    list_rule = fake.fail_every("files.list", list_every, make_http_error(429))
    media_rule = fake.fail_every(
        "files.get_media", media_every, make_http_error(403, "userRateLimitExceeded")
    )

    _wire_fake(sync, fake)
    sync.download_sync_cycle()

    _assert_complete_index(sync, expected_folders, expected_files)

    # The storm must actually have hit the backoff path.
    total_fires = list_rule["fires"] + media_rule["fires"]
    assert total_fires > 0, "storm never fired — backoff path was not exercised"
    # And the neutralised backoff sleep was invoked at least once per fire.
    assert sync.client._poll_event.wait.call_count >= total_fires, (
        "every injected fault must trigger a backoff wait()"
    )


def test_s0_storm_small(sync_engine: SyncEngine):
    """Non-slow S0 guard for the storm scenario (N~=150)."""
    fake = FakeDrive()
    expected_folders, expected_files = build_tree(fake, n_folders=30, n_files=120)
    _run_storm(sync_engine, fake, expected_folders, expected_files)
```

- [ ] **Step 2: Run the S0 storm test to verify it fails, then passes once the helper exists**

The helper is defined in the same step above, so this is a single red→green: run

Run: `python -m pytest tests/offline/test_load_initial_sync.py::test_s0_storm_small -q`
Expected: PASS (the storm fires on `get_media` at minimum; the index completes).

If it does NOT pass because the sync raised instead of converging, check that
`list_every`/`media_every` are `>= 2` (Background: Task 1 — spacing guarantees a single
failure is followed by a successful retry). If it exposes a genuine engine/client defect,
apply the §8 triage protocol (Task 5).

- [ ] **Step 3: Add the slow S1 load test at N=10k**

Append:

```python
@pytest.mark.load
def test_s1_storm_load(sync_engine: SyncEngine):
    """S1 (slow, N=10k): full initial sync under a rate-limit storm completes
    correctly. Soft guards: hang-guard wall-clock + tracemalloc peak ceiling."""
    fake = FakeDrive()
    expected_folders, expected_files = build_tree(
        fake, n_folders=2000, n_files=8000
    )

    tracemalloc.start()
    start = time.monotonic()
    _run_storm(sync_engine, fake, expected_folders, expected_files)
    elapsed = time.monotonic() - start
    _peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    # Soft guards (see plan Background #2): catch hangs/leaks, not performance.
    assert elapsed < _HANG_GUARD_SECONDS, (
        f"initial sync took {elapsed:.1f}s (> {_HANG_GUARD_SECONDS}s hang guard)"
    )
    assert _peak < _MEMORY_CEILING_BYTES, (
        f"peak python allocation {_peak / 1e6:.0f}MB exceeded the "
        f"{_MEMORY_CEILING_BYTES / 1e6:.0f}MB ceiling — possible O(N) leak"
    )
```

- [ ] **Step 4: Run S1 explicitly (this is a gate run, NOT the green loop)**

Run: `python -m pytest tests/offline/test_load_initial_sync.py::test_s1_storm_load -m load --benchmark-disable -q -s`
Expected: 1 passed. Record wall-clock and the storm fire counts in the deviations file
(Task 5). If `_peak` or `elapsed` exceed the ceilings on the execution machine and the run
is otherwise correct, re-tune `_MEMORY_CEILING_BYTES` / `_HANG_GUARD_SECONDS` upward and
note the calibration in deviations (they are documented soft guards, §11).

- [ ] **Step 5: Run the normal green loop to confirm S1 is deselected and S0 still green**

Run: `python -m pytest tests/offline/test_load_initial_sync.py -m "not load" -q`
Expected: `test_build_tree_shape` + `test_s0_storm_small` pass; `test_s1_storm_load` is
deselected (shown as deselected, not run).

- [ ] **Step 6: Commit**

```bash
git add tests/offline/test_load_initial_sync.py
git commit -m "test(7b-e3): S1 rate-limit-storm initial-sync scenario (+ S0 guard)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Scenario S2 — crash / resume idempotent re-index

**Files:**
- Modify: `tests/offline/test_load_initial_sync.py` (add `_run_crash_resume` helper, `test_s0_crash_resume_small`, `test_s2_crash_resume_load`)

`_run_crash_resume` runs `download_sync_cycle`, interrupts it after K applied chunks via
`_cancel_requested` (set from a counting wrapper around `apply_remote_changes`), asserts the
**partial** state (cursor still `""`, `did_finish_indexing` False, no duplicate rows / no
corrupt files among those applied), then re-enters `download_sync_cycle` and asserts the
full complete-index gate plus that the re-run did not duplicate or corrupt anything.

The helper lowers `_LIST_PAGE_SIZE` via `monkeypatch` so the interruption lands on a
**non-final** page (`cursor == ""`) even at small N — see Background #4.

- [ ] **Step 1: Write the failing S0 crash/resume test (defines + calls the helper)**

Append to `tests/offline/test_load_initial_sync.py`:

```python
# ---------------------------------------------------------------------------
# Scenario S2 — crash / resume idempotent re-index
# ---------------------------------------------------------------------------


def _run_crash_resume(
    sync: SyncEngine,
    fake: FakeDrive,
    expected_folders: set[str],
    expected_files: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_size: int,
    crash_after_chunks: int,
) -> None:
    """Interrupt the initial index after K applied chunks (on a non-final page,
    so cursor is still ""), then resume and assert idempotent completion."""
    # Lower the bootstrap page size so the interruption lands mid-listing.
    monkeypatch.setattr(client_mod, "_LIST_PAGE_SIZE", page_size)

    _wire_fake(sync, fake)

    # Interrupt after `crash_after_chunks` applied chunks via _cancel_requested.
    real_apply = sync.apply_remote_changes
    state = {"chunks": 0}

    def counting_apply(events):
        result = real_apply(events)
        state["chunks"] += 1
        if state["chunks"] == crash_after_chunks:
            sync._cancel_requested.set()
        return result

    monkeypatch.setattr(sync, "apply_remote_changes", counting_apply)

    with pytest.raises(CancelledError):
        sync.download_sync_cycle()

    # --- Partial state (Background #1): cursor still "", indexing unfinished. ---
    assert sync.remote_cursor == "", "mid-index crash must NOT advance the cursor"
    assert sync._state.get("sync", "did_finish_indexing") is False
    partial_ids = [e.remote_id for e in sync.iter_index()]
    assert len(partial_ids) == len(set(partial_ids)), "duplicate rows after crash"
    assert sync.index_count() > 0, "some entries must have been applied before crash"
    assert sync.index_count() < len(expected_folders) + len(expected_files), (
        "crash must leave a PARTIAL index"
    )
    # Files applied so far must already be intact on disk.
    for entry in sync.iter_index():
        path = entry.remote_path_cased
        if path.lower() in expected_files:
            local = sync.to_local_path_from_cased(path)
            assert osp.isfile(local)
            with open(local, "rb") as fh:
                assert _md5(fh.read()) == expected_files[path.lower()]

    # --- Resume: restore the real apply, clear cancel, re-enter the cycle. ---
    monkeypatch.setattr(sync, "apply_remote_changes", real_apply)
    sync._cancel_requested.clear()
    _wire_fake(sync, fake)
    sync.download_sync_cycle()

    # --- Final state: complete, correct, no duplication, no corruption. ---
    _assert_complete_index(sync, expected_folders, expected_files)


def test_s0_crash_resume_small(sync_engine: SyncEngine, monkeypatch):
    """Non-slow S0 guard for crash/resume (N~=150, page_size 50 -> 3+ pages)."""
    fake = FakeDrive()
    expected_folders, expected_files = build_tree(fake, n_folders=30, n_files=120)
    _run_crash_resume(
        sync_engine, fake, expected_folders, expected_files, monkeypatch,
        page_size=50, crash_after_chunks=1,
    )
```

- [ ] **Step 2: Run the S0 crash/resume test (red→green in one step since helper is defined)**

Run: `python -m pytest tests/offline/test_load_initial_sync.py::test_s0_crash_resume_small -q`
Expected: PASS.

Common failure to watch for: if `index_count()` after the crash equals the full count, the
interruption landed after the *final* page — increase the entry count or decrease
`page_size` so there are ≥3 pages and `crash_after_chunks` is strictly less than the page
count. If the partial-cursor assertion fails (`remote_cursor != ""`), the crash landed on
the final page (which carries the real token) — same fix. If resume exposes a genuine
non-idempotency defect (duplicate rows / corrupt re-download), apply the §8 triage protocol
(Task 5) — do not loosen the assertion.

- [ ] **Step 3: Add the slow S2 load test at N=10k**

Append:

```python
@pytest.mark.load
def test_s2_crash_resume_load(sync_engine: SyncEngine, monkeypatch):
    """S2 (slow, N=10k): crash mid-index, then resume; the re-index is
    idempotent and the final state is complete and uncorrupted. With page_size
    1000 the 10k tree is ~10 pages; crash after 3 leaves a non-final partial."""
    fake = FakeDrive()
    expected_folders, expected_files = build_tree(
        fake, n_folders=2000, n_files=8000
    )

    tracemalloc.start()
    start = time.monotonic()
    _run_crash_resume(
        sync_engine, fake, expected_folders, expected_files, monkeypatch,
        page_size=1000, crash_after_chunks=3,
    )
    elapsed = time.monotonic() - start
    _peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert elapsed < _HANG_GUARD_SECONDS, (
        f"crash/resume sync took {elapsed:.1f}s (> {_HANG_GUARD_SECONDS}s hang guard)"
    )
    assert _peak < _MEMORY_CEILING_BYTES, (
        f"peak python allocation {_peak / 1e6:.0f}MB exceeded the "
        f"{_MEMORY_CEILING_BYTES / 1e6:.0f}MB ceiling — possible O(N) leak"
    )
```

- [ ] **Step 4: Run S2 explicitly (gate run, not the green loop)**

Run: `python -m pytest tests/offline/test_load_initial_sync.py::test_s2_crash_resume_load -m load --benchmark-disable -q -s`
Expected: 1 passed. Record wall-clock in the deviations file (Task 5).

- [ ] **Step 5: Run both slow scenarios together**

Run: `python -m pytest tests/offline/test_load_initial_sync.py -m load --benchmark-disable -q`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/offline/test_load_initial_sync.py
git commit -m "test(7b-e3): S2 crash/resume idempotent re-index scenario (+ S0 guard)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Gate — flake8/mypy/full-suite + deviations file

**Files:**
- Create: `docs/superpowers/plans/2026-06-15-stage-7b-e3-load-test-deviations.md`

This task records the gate evidence and the deferred Stage-8 real-account smoke procedure.
**Do not run `tests/linked`. Do not run `tests/offline` while a real smoke account is
linked** (keyring collision).

- [ ] **Step 1: flake8 CLEAN on new/changed files**

Run: `python -m flake8 tests/offline/test_load_initial_sync.py tests/fake_drive.py tests/offline/test_fake_drive.py`
Expected: no output (clean).

- [ ] **Step 2: mypy at baseline parity (zero production changes)**

Run: `git diff main -- src/ | head` (expect empty unless a §8 fix was made), then
Run: `python -m mypy src/cumulus` (or the repo's standard mypy invocation)
Expected: same result as the pre-E3 baseline (no new errors). Record the exact command and
result in the deviations file.

- [ ] **Step 3: Normal offline suite GREEN with S0 included, S1/S2 deselected**

Run: `python -m pytest tests/offline -m "not load" --benchmark-disable -q`
Expected: GREEN. New non-slow additions vs the 636-test E2 baseline:
`test_build_tree_shape`, `test_s0_storm_small`, `test_s0_crash_resume_small`, and the 2
`fail_every` hook tests = +5. Record the exact collected/passed/xfailed counts.

- [ ] **Step 4: Slow load run GREEN on explicit invocation**

Run: `python -m pytest tests/offline/test_load_initial_sync.py -m load --benchmark-disable -q -s`
Expected: 2 passed. Record the run command, pass count, wall-clock for each scenario, and
the storm fire counts (have S1 print or capture `list_rule["fires"]` / `media_rule["fires"]`
— or temporarily log them) in the deviations file.

- [ ] **Step 5: Write the deviations file**

Create `docs/superpowers/plans/2026-06-15-stage-7b-e3-load-test-deviations.md` with:

```markdown
# Stage 7b-E3 — 10k-File Initial-Sync Load Test: Deviations & Findings

**Date:** 2026-06-15
**Plan:** `docs/superpowers/plans/2026-06-15-stage-7b-e3-load-test.md`
**Spec:** `docs/superpowers/specs/2026-06-15-stage-7b-e3-load-test-design.md`
**Branch:** `google-drive-port`. Pre-stage HEAD: <fill in>. Execution: <subagent / inline>.

## Headline
- Production changes this stage: <none, or the §8 fix made — show `git diff main -- src/`>.
- Tests added: +2 fake-drive hook tests, +3 non-slow S0 guards, +2 slow `load` scenarios.
- Findings: <none, or list each defect with fixed-in-task vs xfail-deferred + user decision>.

## Gate evidence
- flake8: <command + result>
- mypy: <command + result; baseline-parity statement>
- Normal offline suite (`-m "not load"`): <collected / passed / xfailed counts>
- Slow load run (`-m load`): <command + 2 passed>
  - S1 storm: wall-clock <…s>, list fires <…>, get_media fires <…>, peak alloc <…MB>
  - S2 crash/resume: wall-clock <…s>, peak alloc <…MB>
- Soft-guard calibration: `_HANG_GUARD_SECONDS=300`, `_MEMORY_CEILING_BYTES=512MB`
  (retuned to <…> if needed, with reason).

## Plan-vs-impl deltas
- <any deviation from the plan: e.g. branching factor, page_size, crash_after_chunks tuning>.

## Real-account smoke (documented, deferred to Stage-8 release smoke)
Out of E3 scope to execute. Procedure:
1. Back up the smoke account's credentials + keyring file OUTSIDE the repo, mode 0600,
   BEFORE any offline-suite interaction (keyring-backup guard).
2. Provision ~N files in the smoke test account.
3. Link the account; run an initial sync.
4. Verify all entries indexed + downloaded with correct content and no duplication
   (one index row per remote_id; on-disk md5 matches; folder hierarchy matches).
5. Verify the account survives intact afterward (the backup is the recovery path if the
   keyring collision wipes the linked account).
Note: in-memory faults model Drive's RESPONSES (429 / rate-limit-403), not server-side
eventual consistency / revision merging / cross-request races — those are inherently
unmodellable in-memory and are routed to this real-account smoke.
```

Fill in every `<…>` with the actual recorded values.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/plans/2026-06-15-stage-7b-e3-load-test-deviations.md
git commit -m "docs(7b-e3): gate evidence + deviations + deferred Stage-8 smoke

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (performed against the spec)

**Spec coverage:**
- §3 "one module + shared tree-builder + two scenario tests + non-slow harness guard" →
  Tasks 2/3/4 (module, `build_tree`, S1, S2, S0 guards). ✓
- §6 "minimal recurring fault hook (`fail_every`)" → Task 1. ✓
- §7 S1 storm assertions (all rows, one row per remote_id, md5 per file, folder hierarchy,
  `did_finish_indexing`/`indexing_counter`/cursor advanced, storm fired) →
  `_assert_complete_index` + the `total_fires > 0` assertion. ✓
- §7 S2 partial-state + idempotent-resume assertions → `_run_crash_resume`. ✓
- §7 S0 non-slow guard of both shapes → `test_s0_storm_small`, `test_s0_crash_resume_small`. ✓
- §5.1 cursor stays `""` mid-index → asserted in the partial-state block. ✓
- §5.2 O(N) buffer is by-design; ceiling is a generous absolute guard → `_MEMORY_CEILING_BYTES`
  documented as a soft guard, not a sublinearity claim. ✓
- §5.3 deterministic instant backoff → `_wire_fake` Mock neutralization. ✓
- §8 triage protocol → referenced at every "if it exposes a defect" step + recorded in
  deviations. ✓
- §9 deliverables/gate (`load` marker, deselection, hook test, flake8/mypy/green/slow-run,
  deviations file) → Tasks 1/2/5. ✓
- §10 real-account smoke documented + deferred → deviations template §"Real-account smoke". ✓
- §11 soft-bound calibration documented → deviations §"Soft-guard calibration". ✓

**Placeholder scan:** the only `<…>` placeholders are inside the *deviations template*,
which is intentionally filled with measured values at execution time — every code/test step
contains complete, runnable content.

**Type/name consistency:** `build_tree` returns `(set[str], dict[str,str])` consumed
identically by `_assert_complete_index`, `_run_storm`, `_run_crash_resume`. `fail_every`
returns the rule dict whose `["fires"]` key is read in `_run_storm`. `_wire_fake`,
`sync_engine`, `_md5`, `_HANG_GUARD_SECONDS`, `_MEMORY_CEILING_BYTES` are defined once and
referenced consistently. Real API names verified against source: `download_sync_cycle`,
`iter_index`, `index_count`, `get_index_entry`, `to_local_path_from_cased`,
`remote_cursor`, `_state.get("sync", "did_finish_indexing"|"indexing_counter")`,
`_cancel_requested`, `CancelledError` (from `cumulus.exceptions`),
`client._poll_event`, `client_mod._LIST_PAGE_SIZE`, `FakeDrive._insert`, `FOLDER_MIME`,
`ROOT_ID`, `make_http_error`.
