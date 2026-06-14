# Stage 7b-E2 — Fault-Injection Test Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive the cumulus sync engine, write client, and change feed through Drive-side faults (failures, committed-but-lost responses, partial pages, corrupt successes) to expose and fix real defects, leaving each fault scenario behind as a regression guard.

**Architecture:** Category-organized. Task 1 generalizes the fake-Drive fault surface (the injection vehicle); Tasks 2–9 are one fault category each, largely independent and parallelizable; Task 10 is the final gate. Every scenario is a **TDD red→green cycle**: write the failing fault test, verify the RED empirically, triage what it exposes (§"Bug-triage protocol"), fix in-task if scoped or `xfail`+escalate if large, then green and re-stress the related cluster-E hardening.

**Tech Stack:** Python 3, pytest, `tests/fake_drive.py` in-memory Drive fake, `googleapiclient` `HttpError`, `httplib2`. No real network/account.

---

## Bug-triage protocol (applies to every red test in Tasks 2–9)

This is a **discovery** plan: a red test's production fix cannot be fully pre-written because the defect is unknown until the test runs. Every scenario step therefore follows this exact protocol — treat it as the implicit body of each "triage & fix" step:

1. **Red first, always.** Write the test. Run it. Confirm it fails **for the intended reason** (read the traceback — a `KeyError`/`AttributeError` in test setup is NOT the intended red; fix the test until it fails on the assertion under examination).
2. **Decide what the red means:**
   - **Genuine, scoped defect** → fix it in this task following existing cumulus patterns, scoped to the exposed defect only. Then green.
   - **Current behavior is actually correct, test merely pins it** → this is acceptable *characterization*. Keep the test green-from-the-start only if you first proved the behavior is correct (e.g. by inverting the assertion and watching it fail). Never use characterization to paper over a bug.
   - **Genuine defect whose fix is its own design problem** (architectural / cross-cutting) → mark the test `@pytest.mark.xfail(reason="finding: <one line>", strict=True)`, write a finding note in the deviations file, and surface it to the user (fix-now vs defer is the user's call). Do **not** silently expand scope.
3. **Commit** the test (+ any in-task fix) before moving to the next scenario.

## Test-discipline guards (carried from cluster-E — do not violate)

- **Never** run `tests/linked` (needs a real Google account).
- **Never** run `tests/offline` while a real smoke-test account is linked (keyring collision wipes the linked account — see memory `feedback_keyring_test_collision`).
- During development run only the **specific new module** (e.g. `pytest tests/offline/test_fault_feed.py -v`). The **full** offline suite is run **only at the Task 10 gate**.
- All test commands below assume CWD `/home/exedev/maestral` and use `--benchmark-disable` only at the gate.

## Shared harness (already exists — reuse, do not reinvent)

- **`tests/fake_drive.py`** — `FakeDrive`, `ROOT_ID`, `make_http_error(status, reason="", message="error")`. Seeding: `fake.add_file(path, content=b"...")`, `fake.add_folder(path)`; both return the file id. `fake.service()` returns the Resource tree. `fake._change_log` is `list[(seq, fileId)]`. `fake._seq` is the current cursor int.
- **`tests/offline/conftest.py`** fixtures: `fake_drive` (a bare `FakeDrive`), `client_on_fake` (`.client` = real `GoogleDriveClient` wired to `.fake`), `sync` (full `SyncEngine` on a fake + real local dir + observer), `make_http_error` (factory fixture, supports `retry_after=`).
- **`tests/offline/test_sync.py`** helpers (import or mirror): `sync_engine` fixture (offline `SyncEngine`, in-memory DB, real local dir); `_md5(data)`; `_write_local_file(sync, remote_path, data)`; `_seed_entry(sync, remote_path, remote_id, item_type, rev, content_hash)`; `_download_event(...)`. `IndexEntry`, `ItemType`, `SyncEvent`, `SyncDirection`, `SyncStatus`, `ChangeType` from `cumulus.models`.
- **`tests/offline/test_changes.py`** helpers (mirror in new feed tests): `_fake_cache()`, `_start(fake)` (returns current cursor str), `_drain(fake, cache, start, resolve_deleted_path=None)` (runs `iter_remote_changes` to exhaustion, returns `(entries, pages)`).

### Driving an end-to-end download cycle on a fake (the canonical pattern)

```python
from unittest.mock import Mock
from cumulus.core import FullAccount

sync_engine.client._get_service = Mock(return_value=fake.service())
sync_engine.client._cached_account_info = FullAccount(
    account_id="account-id", display_name="Offline Tester",
    email="offline@example.com", profile_photo_url=None, disabled=False,
)
start_cursor = fake.service().changes().getStartPageToken().execute()["startPageToken"]
# ... mutate fake / arm faults ...
for sync_events, cursor in sync_engine.list_remote_changes_iterator(start_cursor):
    sync_engine.apply_remote_changes(sync_events)
```

(`list_remote_changes_iterator` → `cumulus.changes.iter_remote_changes`, resolver = `sync_engine._index_path_for_remote_id`. The cursor is committed by the caller AFTER `apply_remote_changes` returns — that gap is the crash window Task 3 exploits.)

---

## Task 1: Fake-Drive fault surface (shared infrastructure)

Generalize the fault API to the **union** of what Tasks 2–9 need — no more. Today `fail_next`/`fail_after` are simple per-key FIFO queues; `_maybe_fail_after` only reaches `files.create`; `get_media` bypasses fault hooks entirely. Tasks 2–9 need: (a) uniform keying on every `.execute()`, (b) Nth-call faults, (c) argument-predicate faults, (d) a consistent exception factory. The download-corruption hook is **NOT** here — it is single-use and lives in Task 6.

**Files:**
- Modify: `tests/fake_drive.py`
- Test: `tests/offline/test_fake_drive.py`

### Design of the new fault API (target signatures)

```python
# On FakeDrive:
def fail_next(self, key, exc, *, count=1, when=None): ...   # raise BEFORE next `count` executes
def fail_after(self, key, exc, *, count=1, when=None): ...  # raise AFTER next `count` executes commit
def fail_on_call(self, key, n, exc, *, when=None): ...      # raise on the N-th (1-based) execute
def fail_when(self, key, predicate, exc): ...               # raise on next execute whose kwargs match
```

`key` is `"<resource>.<method>"`, e.g. `"files.list"`, `"changes.list"`, `"files.create"`, `"files.update"`, `"files.delete"`, `"files.get"`, `"files.generateIds"`, `"revisions.list"`, `"files.get_media"`. `when(**kwargs)`/`predicate(**kwargs)` receive the call kwargs (`fileId`, `q`, `pageToken`, `body`, …). Exception factory: extend the existing module-level `make_http_error`; add `make_timeout()` and `make_conn_reset()`.

- [ ] **Step 1: Write failing tests for the new fault API**

Add to `tests/offline/test_fake_drive.py`:

```python
import socket
import requests
from tests.fake_drive import FakeDrive, ROOT_ID, make_http_error, make_timeout, make_conn_reset


def test_fail_next_count_fails_n_then_succeeds():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    fake.fail_next("files.get", make_http_error(500), count=2)
    svc = fake.service()
    for _ in range(2):
        try:
            svc.files().get(fileId=fid, fields="id").execute()
            assert False, "expected 500"
        except HttpError as e:
            assert e.resp.status == 500
    # third call succeeds
    assert svc.files().get(fileId=fid, fields="id").execute()["id"] == fid


def test_fail_on_call_targets_the_nth_changes_list():
    fake = FakeDrive()
    fake.add_file("/a.txt", content=b"a")
    fake.add_file("/b.txt", content=b"b")
    fake.fail_on_call("changes.list", 2, make_http_error(503))
    svc = fake.service()
    # page 1 succeeds, page 2 faults
    r1 = svc.changes().list(pageToken="100", pageSize=1,
                            fields="nextPageToken, changes(fileId)", includeRemoved=True).execute()
    assert "nextPageToken" in r1
    try:
        svc.changes().list(pageToken=r1["nextPageToken"], pageSize=1,
                           fields="nextPageToken, changes(fileId)", includeRemoved=True).execute()
        assert False, "expected 503 on 2nd changes.list"
    except HttpError as e:
        assert e.resp.status == 503


def test_fail_when_predicate_targets_one_fileid_in_batch():
    fake = FakeDrive()
    a = fake.add_file("/a.txt", content=b"a")
    b = fake.add_file("/b.txt", content=b"b")
    fake.fail_when("files.update", lambda **kw: kw.get("fileId") == b,
                   make_http_error(500))
    svc = fake.service()
    svc.files().update(fileId=a, body={"name": "a2.txt"}, fields="id").execute()  # ok
    try:
        svc.files().update(fileId=b, body={"name": "b2.txt"}, fields="id").execute()
        assert False, "expected 500 on b only"
    except HttpError as e:
        assert e.resp.status == 500


def test_fail_after_commits_then_raises_on_arbitrary_key():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    fake.fail_after("files.delete", make_http_error(500))
    svc = fake.service()
    try:
        svc.files().delete(fileId=fid).execute()
        assert False, "expected post-commit 500"
    except HttpError as e:
        assert e.resp.status == 500
    assert fid not in fake.files  # the delete DID commit before the raise


def test_get_media_can_fault():
    fake = FakeDrive()
    fid = fake.add_file("/big.bin", content=b"y" * 100)
    fake.fail_next("files.get_media", make_timeout())
    request = fake.service().files().get_media(fileId=fid)
    import io
    from googleapiclient.http import MediaIoBaseDownload
    downloader = MediaIoBaseDownload(io.BytesIO(), request, chunksize=64)
    try:
        downloader.next_chunk()
        assert False, "expected timeout"
    except (socket.timeout, requests.exceptions.Timeout, TimeoutError):
        pass


def test_exception_factory_shapes():
    assert make_http_error(429, "userRateLimitExceeded").resp.status == 429
    assert isinstance(make_timeout(), (TimeoutError, socket.timeout))
    assert isinstance(make_conn_reset(), (ConnectionError, requests.exceptions.ConnectionError))
```

- [ ] **Step 2: Run the new tests; verify they fail**

Run: `pytest tests/offline/test_fake_drive.py -k "fail_next_count or fail_on_call or fail_when or fail_after_commits or get_media_can_fault or exception_factory" -v`
Expected: FAIL/ERROR — `make_timeout`/`make_conn_reset` undefined, `fail_next` rejects `count=`, `fail_on_call`/`fail_when` undefined, `get_media` has no fault hook.

- [ ] **Step 3: Implement the unified rule engine in `FakeDrive`**

Replace the `_fail` dict + `fail_next`/`fail_after`/`_maybe_fail`/`_maybe_fail_after` block (`tests/fake_drive.py:45,89-113`) with a single rule list. In `__init__`, replace `self._fail: dict[str, list[Exception]] = {}` with:

```python
        self._fail_rules: list[dict] = []
```

Then the methods:

```python
    def fail_next(self, key: str, exc: Exception, *, count: int = 1,
                  when: "Callable[..., bool] | None" = None) -> None:
        """Raise *exc* BEFORE each of the next *count* ``.execute()`` calls of
        *key* (optionally only when ``when(**call_kwargs)`` is true)."""
        self._fail_rules.append(
            {"key": key, "exc": exc, "skip": 0, "remaining": count,
             "when": when, "after": False})

    def fail_after(self, key: str, exc: Exception, *, count: int = 1,
                   when: "Callable[..., bool] | None" = None) -> None:
        """Raise *exc* AFTER each of the next *count* ``.execute()`` calls of
        *key* commit (finalize runs first). Models "committed but response lost"."""
        self._fail_rules.append(
            {"key": key, "exc": exc, "skip": 0, "remaining": count,
             "when": when, "after": True})

    def fail_on_call(self, key: str, n: int, exc: Exception, *,
                     when: "Callable[..., bool] | None" = None) -> None:
        """Raise *exc* on the *n*-th (1-based) matching ``.execute()`` of *key*,
        letting the first n-1 pass."""
        self._fail_rules.append(
            {"key": key, "exc": exc, "skip": n - 1, "remaining": 1,
             "when": when, "after": False})

    def fail_when(self, key: str, predicate: "Callable[..., bool]",
                  exc: Exception) -> None:
        """Raise *exc* on the next ``.execute()`` of *key* whose call kwargs
        satisfy ``predicate(**call_kwargs)``."""
        self._fail_rules.append(
            {"key": key, "exc": exc, "skip": 0, "remaining": 1,
             "when": predicate, "after": False})

    def _consume_fail(self, key: str, kwargs: dict, *, after: bool) -> None:
        """Fire the first matching, ready rule for (key, after-phase). At most
        one rule fires per call. A rule with a remaining ``skip`` count consumes
        the call toward its skip without firing."""
        for rule in self._fail_rules:
            if rule["key"] != key or rule["after"] != after:
                continue
            if rule["when"] is not None and not rule["when"](**kwargs):
                continue
            if rule["skip"] > 0:
                rule["skip"] -= 1
                return
            rule["remaining"] -= 1
            if rule["remaining"] <= 0:
                self._fail_rules.remove(rule)
            raise rule["exc"]

    def _maybe_fail(self, key: str, kwargs: dict | None = None) -> None:
        self._consume_fail(key, kwargs or {}, after=False)

    def _maybe_fail_after(self, key: str, kwargs: dict | None = None) -> None:
        self._consume_fail(key, kwargs or {}, after=True)
```

- [ ] **Step 4: Thread call kwargs through `_Request` / `_ResumableRequest`**

`_Request` (`tests/fake_drive.py:281-301`) — add `fail_kwargs`:

```python
class _Request:
    def __init__(self, fail_key, fail_check, fn, fail_after_check=None, fail_kwargs=None):
        self._fail_key = fail_key
        self._fail_check = fail_check
        self._fn = fn
        self._fail_after_check = fail_after_check
        self._fail_kwargs = fail_kwargs or {}

    def execute(self, num_retries: int = 0, http=None):
        self._fail_check(self._fail_key, self._fail_kwargs)
        result = self._fn()
        if self._fail_after_check is not None:
            self._fail_after_check(self._fail_key, self._fail_kwargs)
        return result
```

`_ResumableRequest.next_chunk` (`:268-278`) — pass `self._fail_kwargs` (add the same attr in its `__init__`, default `{}`) to both `self._fail_check(...)` and `self._fail_after_check(...)`.

- [ ] **Step 5: Wire `fail_kwargs` at every resource `.execute()` site**

For each `_Request(...)` construction in the resource classes, pass a `fail_kwargs` dict of the identifying call kwargs so predicates can match. Minimum set:
- `_FilesResource.get` (`:391`): `fail_kwargs={"fileId": fileId, "fields": fields}`
- `_FilesResource.list` (`:411`): `fail_kwargs={"q": q, "pageToken": pageToken}`
- `_FilesResource.create` (`:448-455`, both `_ResumableRequest` and `_Request`): `fail_kwargs={"body": body}`
- `_FilesResource.delete` (`:462`): `fail_kwargs={"fileId": fileId}`
- `_FilesResource.generateIds` (`:467`): `fail_kwargs={}`
- `_FilesResource.update` (`:504-507`, both): `fail_kwargs={"fileId": fileId, "body": body, "addParents": addParents, "removeParents": removeParents}`
- `_ChangesResource.getStartPageToken` (`:517`): `fail_kwargs={}`
- `_ChangesResource.list` (`:554`): `fail_kwargs={"pageToken": pageToken}`
- `_AboutResource.get` (`:564`): `fail_kwargs={}`
- `_RevisionsResource.list` (`:585`): `fail_kwargs={"fileId": fileId}`

For `get_media`, make the chunk transport fault-aware. In `_FakeHttp.__init__` accept `(content, fail_check=None, fail_key="files.get_media", fail_kwargs=None)` and at the top of `_FakeHttp.request` call `if self._fail_check: self._fail_check(self._fail_key, self._fail_kwargs)`. Update `_MediaRequest.__init__` to accept and forward those, and have `_FilesResource.get_media` (`:469-471`) and `_RevisionsResource.get_media` (`:587-592`) build the `_MediaRequest` with `fail_check=self._d._maybe_fail, fail_kwargs={"fileId": fileId}`.

- [ ] **Step 6: Add the exception factory helpers**

After `make_http_error` (`tests/fake_drive.py:29`):

```python
import socket  # add near top imports
import requests  # add near top imports


def make_timeout(message: str = "fake timeout") -> Exception:
    """A socket-level read timeout, shaped like the transport raises one."""
    return socket.timeout(message)


def make_conn_reset(message: str = "connection reset by peer") -> Exception:
    """A connection-reset, shaped like requests surfaces one."""
    return requests.exceptions.ConnectionError(message)
```

(If `_retry_transient` only catches `requests.exceptions.Timeout` — confirm against `errorhandling.py:62-68` `CONNECTION_ERRORS` — make `make_timeout` return `requests.exceptions.Timeout(message)` instead so retry tests exercise the real retry path. Choose based on what the production retry classifier catches; both forms are valid faults.)

- [ ] **Step 7: Run the full fake-Drive test module; verify green + no regressions**

Run: `pytest tests/offline/test_fake_drive.py -v`
Expected: PASS (new tests + all pre-existing ones — `fail_next` backward-compat is preserved by `count=1` default).

- [ ] **Step 8: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(7b-e2): generalize fake-Drive fault surface (task 1 shared infra)"
```

---

## Task 2: Partial change-feed pages (`test_fault_feed.py`)

Re-stresses F6 cross-page fixpoint and finding 5. Uses the `iter_remote_changes` `_drain`-style harness.

**Files:**
- Create: `tests/offline/test_fault_feed.py`
- Possibly modify: `src/cumulus/changes.py` (only if a red exposes a defect)

- [ ] **Step 1: Scenario A — page-2 fault must not advance the cursor past unapplied changes**

```python
import sqlite3
import pytest
from googleapiclient.errors import HttpError
from cumulus.changes import iter_remote_changes
from cumulus.database.core import Database
from cumulus.path_cache import PathIDCache
from cumulus.exceptions import CursorResetError
from tests.fake_drive import ROOT_ID, FakeDrive, make_http_error


def _cache():
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = ROOT_ID
    return c


def _start(fake):
    return fake.service().changes().getStartPageToken().execute()["startPageToken"]


def _drain(fake, cache, start, resolve_deleted_path=None, page_size=None):
    def factory():
        return fake.service()
    def retry(fn):
        return fn()
    pages = list(iter_remote_changes(factory, retry, cache, start,
                                     resolve_deleted_path=resolve_deleted_path))
    return [e for p in pages for e in p.entries], pages


def test_page2_fault_does_not_advance_cursor_or_lose_changes():
    fake = FakeDrive()
    cache = _cache()
    start = _start(fake)
    fake.add_file("/a.pdf", content=b"a")
    fake.add_file("/b.pdf", content=b"b")
    # _CHANGE_PAGE_SIZE is 100; force a real page break by failing the 2nd
    # changes.list. (If the drainer fetches >1 page only when pageSize forces it,
    # seed >100 files OR assert the iterator surfaces the error without committing.)
    fake.fail_on_call("changes.list", 2, make_http_error(503))
    with pytest.raises(HttpError):
        _drain(fake, cache, start)
    # The cursor was never yielded (no page completed) -> a fresh drain after the
    # fault clears still delivers BOTH files (no silent loss).
    entries, _ = _drain(fake, cache, start)
    names = {e.path_display for e in entries}
    assert names == {"/a.pdf", "/b.pdf"}
```

> NOTE on forcing pages: `iter_remote_changes` buffers *all* pages before yielding (changes.py:317-345), and `_CHANGE_PAGE_SIZE=100`. To get two real `changes.list` calls, seed ≥101 changed files, OR drive through the fake at a smaller page size if the iterator honors one. Verify the actual paging behavior empirically in Step 2; adjust the seed count so the 2nd `changes.list` is genuinely reached.

- [ ] **Step 2: Run scenario A; verify RED (or characterize)**

Run: `pytest tests/offline/test_fault_feed.py::test_page2_fault_does_not_advance_cursor_or_lose_changes -v`
Expected: the `pytest.raises(HttpError)` passes; the no-loss assertion is the real probe. If it fails (a page-1-applied change leaked the cursor), that is the defect → triage per protocol. If it passes, you have proven the all-pages-buffered design is crash-safe here (valid characterization).

- [ ] **Step 3: Triage & fix** per the Bug-triage protocol. Commit.

- [ ] **Step 4: Scenario B — logical op split across a page boundary (finding-5 across a real page break)**

Parent rename on page 1, child echo on page 2. Drive it end-to-end with the `sync_engine` + `_drain`-into-`apply_remote_changes` pattern (see "Driving an end-to-end download cycle"), mirroring `test_remote_folder_rename_with_same_batch_stale_child_echo` (test_sync.py:1263) but with the two changes landing in **separate pages**. Assert: child indexed only at the renamed path, never resurrected at the stale path; exactly one index row holds the child id.

- [ ] **Step 5: Run scenario B; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 6: Scenario C — `fail_after` on `changes.list` (page committed, response lost) → next poll redelivers, idempotent re-apply**

Use `fake.fail_after("changes.list", make_http_error(500))`. First drain raises after the server-side read; the change log is unchanged (read is non-mutating), so a second drain from the same cursor redelivers and applies exactly once. Assert no double-apply (one index row, one local file).

- [ ] **Step 7: Run scenario C; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 8: Scenario D — duplicate change entry within/across pages → no double-apply**

Force the same fileId to appear twice (e.g. mutate the file twice so it logs two seqs within the window, or inject a duplicate via direct `fake._change_log.append`). `iter_remote_changes` collapses by latest-seq per fid (changes.py:523-526) — assert exactly one resulting entry per fid. This is likely characterization (dedup already exists); prove it by asserting `len(entries)==1`.

- [ ] **Step 9: Run scenario D; verify; commit.**

---

## Task 3: Mid-cycle / crash-window faults (`test_fault_crash.py`)

Re-stresses F2 / finding-2 delete-loss and F7 idempotent creates. Drive end-to-end on `sync_engine`.

**Files:**
- Create: `tests/offline/test_fault_crash.py`
- Possibly modify: `src/cumulus/sync.py`, `src/cumulus/changes.py`

- [ ] **Step 1: Scenario A — crash between classification and cursor commit replays cleanly**

This is the F2/finding-2 case. Mirror `test_crash_replayed_delete_with_evicted_cache_removes_stranded_item` (test_sync.py:1772) but generalize: also cover a **create** and a **rename** replayed after the cache mutated but the cursor didn't advance. Assert the replayed batch converges to the correct on-disk + index state with `sync_errors == []`.

- [ ] **Step 2: Run scenario A; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 3: Scenario B — crash mid-`apply_remote_changes` (fault after item N) → restart re-applies N+1.. without corrupting N**

`apply_remote_changes` applies items via a thread pool (sync.py:3189-3222) and commits each item's index row independently; the batch cursor commits only after the whole batch (download_sync_cycle:3068). Simulate the crash by applying a batch, then **re-running the same batch from the same start cursor** (no cursor advance) and asserting idempotency: items already applied are `Skipped`/no-op, the rest apply, final state correct. Use a multi-item batch (a folder + two files + one delete).

- [ ] **Step 4: Run scenario B; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 5: Scenario C — `fail_after` on `files.create` during create-then-crash → generateIds/409 idempotency prevents the duplicate (F7)**

Drive an **upload** through the write client on a fake with `fake.fail_after("files.create", make_http_error(500))`. The create commits server-side (reserved id in `fake.files`); the response is lost; retry re-issues with the same reserved id and hits the fake's 409 path (fake_drive.py:420-424), then fetches by id. Assert exactly one file exists with the reserved id, content correct, no orphan duplicate. (Cross-check writes.py:285-324 simple-upload 409 handling.)

- [ ] **Step 6: Run scenario C; verify RED/characterize; triage & fix; commit.**

---

## Task 4: Drive-vs-cache divergence (`test_fault_divergence.py`)

Re-stresses F3 remote-moves-by-id and the Task-3 occupied-target residual.

**Files:**
- Create: `tests/offline/test_fault_divergence.py`
- Possibly modify: `src/cumulus/sync.py`

- [ ] **Step 1: Scenario A — stale id→path in cache after out-of-band move → `_apply_remote_move` resolves by `remote_id` (F3)**

Seed a synced file + index row, then move it out-of-band on the fake (different parent/name) while the path cache still holds the old path. Deliver the move via the change feed and apply. Assert the local file and index row follow the **remote_id**, not the stale path (mirror `test_remote_file_rename_moves_local_file` test_sync.py:998 but with a deliberately stale cache entry).

- [ ] **Step 2: Run scenario A; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 3: Scenario B — occupied-target move → early-return, conflict, no silent overwrite/resurrection**

This is the Task-3 residual. Directly reuse the construction of `test_remote_move_with_occupied_target_keeps_index_and_conflicts` (test_sync.py:1183) as the regression baseline, then add an **adversarial variant**: the occupant is a *folder* (not a file), and the moved item is a *folder* — verify the engine still conflict-copies the occupant and never re-keys onto it. Assert old index row intact, exactly one conflicted copy, moved item present at new path, `event.status is SyncStatus.Conflict`.

- [ ] **Step 4: Run scenario B; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 5: Scenario C — duplicate `remote_id` index rows → deterministic resolution, no crash**

`get_index_entry_by_remote_id` returns the first matching row (sync.py:920) with no uniqueness guard. Seed two index rows with the same `remote_id` at different paths, deliver a move for that id, and assert `_apply_remote_move` resolves deterministically and does **not** raise. If duplicate rows are left dangling or the wrong row wins, that is the defect → triage (likely an `xfail` finding if the fix is a schema/index-repair design problem).

- [ ] **Step 6: Run scenario C; verify RED; triage (fix-in-task or xfail+escalate); commit.**

- [ ] **Step 7: Scenario D — missing index row for a live Drive file → reindex/repair, not silent divergence**

Deliver a child create whose parent is live on the fake but absent from the index. `_ensure_parent` (sync.py:3615) should fetch+create the parent on demand. Assert the parent is reindexed and the child lands correctly. Confirm `_ensure_parent` is actually reached on the download path; if a missing parent silently drops the child, that is the defect → triage.

- [ ] **Step 8: Run scenario D; verify RED/characterize; triage & fix; commit.**

---

## Task 5: Write-client mid-flight faults (`test_fault_writes.py`)

Re-stresses F7 and the resumable-upload path. Use `client_on_fake`.

**Files:**
- Create: `tests/offline/test_fault_writes.py`
- Possibly modify: `src/cumulus/writes.py`, `src/cumulus/client.py`

- [ ] **Step 1: Scenario A — resumable upload faults mid-chunk → retry yields one correct file**

Upload a >5MB file (forces resumable, writes.py:50 `_SIMPLE_UPLOAD_THRESHOLD`). Arm `fake.fail_next("files.create", make_timeout())` (or `make_conn_reset`) to fault the first `next_chunk`. `_retry_transient` (client.py:390, 5 attempts, exp backoff) retries. Assert exactly one file in `fake.files` with correct md5, no truncation, no duplicate. (Note: the fake's `_ResumableRequest` finalizes on the 2nd `next_chunk`; arm the fault so it lands before finalize.)

- [ ] **Step 2: Run scenario A; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 3: Scenario B — `remove_batch` faults on one fileId mid-batch → remaining items not lost**

Seed N files, call `client.remove_batch([...])` with `fake.fail_when("files.update", lambda **kw: kw.get("fileId")==target, make_http_error(500))` (remove = trash via update). Assert the result list has one `CumulusApiError` for `target` and successes for the rest; the rest are trashed in `fake.files`. (Cross-check writes.py:689-779 batch callback handling.)

- [ ] **Step 4: Run scenario B; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 5: Scenario C — injected 5xx on `move` / `make_dir` → retry/backoff converges**

`fake.fail_next("files.update", make_http_error(503), count=2)` then `client.move(...)`; assert it converges (≤5 attempts) to one correct move. Same for `make_dir` with `fail_next("files.create", make_http_error(503), count=2)`. Assert final state correct and no duplicate folder (make_dir conflict-probe + 409 idempotency, writes.py:858-951).

- [ ] **Step 6: Run scenario C; verify RED/characterize; triage & fix; commit.**

---

## Task 6: Download integrity faults (`test_fault_download.py` + corruption hook)

New ground. The corruption hook lives here (single-use infra next to its user).

**Files:**
- Create: `tests/offline/test_fault_download.py`
- Modify: `tests/fake_drive.py` (add the download-corruption hook)
- Possibly modify: `src/cumulus/client.py`

- [ ] **Step 1: Add the download-corruption hook to the fake**

On `FakeDrive`, add `corrupt_download(self, file_id, *, mode, ...)` where `mode ∈ {"truncate", "flip", "wrong_md5"}`. Implement by having `_FakeHttp` (or `get_media`) consult a per-fileId corruption directive: truncate returns fewer bytes than `content-length` claims; flip mutates a byte in the returned chunk; wrong_md5 leaves the metadata md5 inconsistent with delivered bytes. Add a focused test in `test_fake_drive.py` proving the hook delivers corrupt bytes while metadata still advertises the original md5.

- [ ] **Step 2: Run the hook test; verify RED then implement then green; commit the hook.**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(7b-e2): add download-corruption hook (task 6 infra)"
```

- [ ] **Step 3: Scenario A — truncated `get_media` → hash mismatch detected, file not committed**

`client.download` writes to a `.cumulus-part` sidecar then verifies md5 against metadata (client.py:760-773, raises `DataCorruptionError`). Arm a truncating corruption, call `download`, assert `DataCorruptionError` and that the **target path does not exist** (sidecar cleaned up, client.py:775-777), and no partial file committed.

```python
import pytest
from cumulus.exceptions import DataCorruptionError

def test_truncated_download_raises_and_leaves_no_file(client_on_fake, tmp_path):
    fake = client_on_fake.fake
    client = client_on_fake.client
    fid = fake.add_file("/big.bin", content=b"z" * 4096)
    client._path_cache.set("/big.bin", fid, ROOT_ID, "big.bin", False)
    fake.corrupt_download(fid, mode="truncate")
    local = str(tmp_path / "big.bin")
    with pytest.raises(DataCorruptionError):
        client.download("/big.bin", local)
    import os.path as osp
    assert not osp.exists(local)
    assert not osp.exists(local + ".cumulus-part")
```

- [ ] **Step 4: Run scenario A; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 5: Scenario B — wrong-md5 content → integrity check rejects**

Arm `mode="flip"` so bytes differ but length matches. Assert `DataCorruptionError`, no file committed. (Confirms the md5 verify is byte-content based, not length based.)

- [ ] **Step 6: Run scenario B; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 7: Scenario C — resumable download faults mid-stream → resume or clean re-download**

Arm `fake.fail_on_call("files.get_media", 2, make_timeout())` on a multi-chunk file (>8MB, `_DOWNLOAD_CHUNK_SIZE`). Assert the per-chunk `_retry_transient` (client.py:752) recovers and the final file matches, OR — if mid-stream resume isn't supported — a clean re-download yields the correct file with no partial left behind. Whichever the code does, assert no corruption; if it commits a partial, that's the defect → triage.

- [ ] **Step 8: Run scenario C; verify RED/characterize; triage & fix; commit.**

---

## Task 7: Rate-limit / 429 backoff (`test_fault_ratelimit.py`)

Re-stresses F14 quota model. Use `client_on_fake`.

**Files:**
- Create: `tests/offline/test_fault_ratelimit.py`
- Possibly modify: `src/cumulus/client.py`, `src/cumulus/errorhandling.py`

- [ ] **Step 1: Scenario A — 429 storm on feed and on writes → backoff converges, no deadlock, no dropped work**

`fake.fail_next("changes.list", make_http_error(429), count=4)` then drive a feed poll; assert it converges within `_MAX_RETRY_ATTEMPTS=5` (client.py:111) and delivers all changes. Repeat with `fail_next("files.update", make_http_error(429, "userRateLimitExceeded"), count=4)` on a `move`; assert success. To keep the test fast, patch the backoff sleep: `_retry_transient` waits via `self._poll_event.wait(sleep_for)` (client.py:420) — set the poll event or monkeypatch the wait so retries don't actually sleep.

- [ ] **Step 2: Run scenario A; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 3: Scenario B — `dailyLimitExceeded` semantics + session-cumulative counter**

`make_http_error(403, "dailyLimitExceeded")` maps to `ConnectionError` and is **not** retried (errorhandling.py:152-157); `storageQuotaExceeded` → `InsufficientSpaceError` (fatal). Assert each surfaces the corrected exception type and the keep-going-vs-stop semantics match. Verify the session request counter (`_request_count`, client.py:210) behaves (increments per request, diagnostic only). If `dailyLimitExceeded` is wrongly retried into an infinite loop, that's the defect → triage.

- [ ] **Step 4: Run scenario B; verify RED/characterize; triage & fix; commit.**

---

## Task 8: Cursor-invalidation faults (`test_fault_cursor.py`)

Re-stresses F5 / Task-8 auto-reindex.

**Files:**
- Create: `tests/offline/test_fault_cursor.py`
- Possibly modify: `src/cumulus/sync.py`, `src/cumulus/changes.py`

- [ ] **Step 1: Scenario A — 400 invalid pageToken mid-stream → `CursorResetError` → auto-reindex, stop-before-reset ordering preserved**

`fake.fail_next("changes.list", make_http_error(400))`; drain `iter_remote_changes` and assert `CursorResetError` is raised (changes.py:332-340), not a generic error. Then drive the engine-level path (`download_sync_cycle` / its caller) and assert it triggers a full reindex (cursor reset to `""`) and that the reindex stops the current stream before resetting (no half-applied + advanced-cursor state). Confirm where the `CursorResetError` is caught and reindex is triggered (manager/sync caller) and assert that observable outcome.

- [ ] **Step 2: Run scenario A; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 3: Scenario B — 400 on a *fresh* token → bounded behavior, not infinite reindex spam**

The persistent-400 bug class Task-8 flagged: if a brand-new `startPageToken` itself 400s, the reindex must not loop forever requesting a token that keeps 400ing. Arm `fake.fail_next("changes.list", make_http_error(400), count=10)` (always-400) and assert the reindex/poll path is **bounded** (raises/stops after a bounded number of attempts) rather than spinning. If it spins, that's the defect → triage (likely `xfail`+escalate if the fix is a backoff/circuit-breaker design).

- [ ] **Step 4: Run scenario B; verify RED; triage (fix-in-task or xfail+escalate); commit.**

---

## Task 9: Auth/token faults mid-cycle (`test_fault_auth.py`)

Re-stresses F13 auth edges.

**Files:**
- Create: `tests/offline/test_fault_auth.py`
- Possibly modify: `src/cumulus/client.py`, `src/cumulus/sync.py`

- [ ] **Step 1: Scenario A — 401 / `TokenRevokedError` mid-batch → clean stop, no partial corruption, resumable after relink**

`fake.fail_when("files.get", lambda **kw: kw.get("fileId")==target, make_http_error(401))` during a multi-item download batch (or 401 on `changes.list`). 401 → `TokenRevokedError` (errorhandling.py:133-138). Assert: the cycle stops cleanly, the cursor did **not** advance past unapplied work, already-applied items are intact (no corruption), and a re-run after clearing the fault (simulating relink) completes the batch.

- [ ] **Step 2: Run scenario A; verify RED/characterize; triage & fix; commit.**

- [ ] **Step 3: Scenario B — token-refresh failure mid-sync → correct error surfaced, cross-restart safety holds**

Simulate a `google.auth.exceptions.RefreshError` surfacing through `convert_api_errors` → `TokenRevokedError` (errorhandling.py:218-223). Inject it via a fault on the active call. Assert `TokenRevokedError` is raised (not a raw `RefreshError`), the engine leaves consistent state, and a restart from the persisted cursor resumes correctly. (If credential refresh happens outside the `_get_service`/`_retry_transient` path, inject at the closest faultable call and document the seam in the deviations file.)

- [ ] **Step 4: Run scenario B; verify RED/characterize; triage & fix; commit.**

---

## Task 10: Final gate

**Files:**
- Create: `docs/superpowers/plans/2026-06-14-stage-7b-e2-fault-injection-deviations.md`

- [ ] **Step 1: Run the full offline suite (gate-only)**

First confirm no real smoke-test account is linked (test-discipline guard). Then:

Run: `pytest tests/offline -p no:randomly --benchmark-disable -q`
Expected: **0 failed, 0 unexpected skips**. Any `xfail`s from triage show as `xfail` (expected), not `XPASS`. Record the new pass count and compare to the 555 baseline (the spec's predecessor count) — document the delta (new fault tests added).

- [ ] **Step 2: Lint + type parity**

Run: `flake8 src/cumulus tests && mypy src/cumulus`
Expected: no new violations vs `d1990ad`. (Compare against the cluster-E baseline; pre-existing baseline violations are acceptable, new ones are not.)

- [ ] **Step 3: Write the deviations file**

Create `docs/superpowers/plans/2026-06-14-stage-7b-e2-fault-injection-deviations.md` recording: plan-vs-implementation deltas; every defect found and whether it was fixed-in-task or `xfail`-deferred (with finding notes and the user's fix-now/defer decision); the final pass count vs baseline; any fault seams that couldn't be modelled in-memory (noted for E3 / 7b-deferred / real-account smoke).

- [ ] **Step 4: Commit the gate result**

```bash
git add docs/superpowers/plans/2026-06-14-stage-7b-e2-fault-injection-deviations.md
git commit -m "docs(7b-e2): record fault-injection gate result and findings"
```

---

## Self-review checklist (run before execution)

- **Spec coverage:** Tasks 1–10 map 1:1 to the spec §5 task map; every §7 scenario bullet appears as a numbered scenario step under its task.
- **No silent caps:** any defect deferred to `xfail` is logged in the deviations file with a finding note (§8 triage). Any fault that can't be modelled in-memory is noted (spec §10 / Task 10 Step 3).
- **Type/name consistency:** fault API (`fail_next`/`fail_after`/`fail_on_call`/`fail_when`, `make_timeout`/`make_conn_reset`) is defined once in Task 1 and used verbatim in Tasks 2–9; harness helpers (`_drain`, `_seed_entry`, `_write_local_file`, `_md5`, the end-to-end driver) reference the real existing fixtures/helpers.
- **TDD discipline:** every category step is red-first with an explicit "verify RED" run and a triage step; production fixes are deliberately not pre-written because the defects are unknown until red (this is the explicit nature of the spec).
