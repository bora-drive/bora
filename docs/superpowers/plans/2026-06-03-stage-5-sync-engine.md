# Stage 5 — Sync Engine Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `SyncEngine` operate against Google Drive semantics (MD5 hashing, single `modifiedTime`, `headRevisionId` revs, download-by-fileId, emergent echo suppression) so a harness can drive bidirectional sync end-to-end against the real `cumulus@arko.net` Drive.

**Architecture:** Adaptation stage — no new top-level components. Bottom-up layering: (1) MD5 content hashing → (2) collapse the two modified-time fields → (3) align `rev` on `headRevisionId` across read+write → (4) download-by-fileId seam → (5) conflict resolution under the new semantics → (6) upload feedback + echo suppression → (7) wiring/lifecycle → (8) smoke test → (9) deviations doc. Each task is independently testable.

**Tech Stack:** Python 3.12, `googleapiclient`, SQLAlchemy-style ORM (`models.py`), pytest. Tests live in `tests/offline/`. Spec: `docs/superpowers/specs/2026-06-03-stage-5-sync-engine-design.md`.

**Conventions for every task:**
- Run the full offline suite with `pytest tests/offline --ignore=tests/offline/test_manager.py -q` (the `test_manager.py` collection error and `test_event_handler.py` fixture errors are tracked Stage 6/7 failures — do **not** try to fix them here).
- **Never** run `pytest tests/offline` while a smoke-test account is linked (keyring fixture wipes credentials — see memory `feedback_keyring_test_collision.md`). The smoke test (Task 8) is the only task that links an account, and it runs no pytest.
- Commit after each task with the message shown.

---

### Task 1: MD5 `ContentHasher` replacing `DropboxContentHasher`

**Files:**
- Modify: `src/cumulus/utils/hashing.py`
- Modify: `src/cumulus/utils/path.py:26,471`
- Create: `tests/offline/test_hashing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/offline/test_hashing.py`:

```python
"""Tests for the MD5 ContentHasher and StreamHasher."""

from __future__ import annotations

import hashlib
import io

from cumulus.utils.hashing import ContentHasher, StreamHasher


def test_content_hasher_matches_hashlib_md5():
    data = b"the quick brown fox" * 1000
    h = ContentHasher()
    h.update(data)
    assert h.hexdigest() == hashlib.md5(data).hexdigest()


def test_content_hasher_empty():
    h = ContentHasher()
    assert h.hexdigest() == hashlib.md5(b"").hexdigest()


def test_content_hasher_chunked_equals_oneshot():
    data = bytes(range(256)) * 5000
    h = ContentHasher()
    for i in range(0, len(data), 4096):
        h.update(data[i : i + 4096])
    assert h.hexdigest() == hashlib.md5(data).hexdigest()


def test_content_hasher_copy_is_independent():
    h = ContentHasher()
    h.update(b"abc")
    c = h.copy()
    c.update(b"def")
    assert h.hexdigest() == hashlib.md5(b"abc").hexdigest()
    assert c.hexdigest() == hashlib.md5(b"abcdef").hexdigest()


def test_stream_hasher_read_hashes_passthrough():
    data = b"streamed content \xff\x00\xfe" * 500
    src = io.BytesIO(data)
    h = ContentHasher()
    wrapped = StreamHasher(src, h)
    out = b""
    while True:
        chunk = wrapped.read(128)
        if not chunk:
            break
        out += chunk
    assert out == data
    assert h.hexdigest() == hashlib.md5(data).hexdigest()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/offline/test_hashing.py -q`
Expected: FAIL with `ImportError: cannot import name 'ContentHasher'`.

- [ ] **Step 3: Replace `DropboxContentHasher` with `ContentHasher`**

In `src/cumulus/utils/hashing.py`, replace the entire `DropboxContentHasher` class (lines 12–92) with:

```python
class ContentHasher:
    """
    Computes the MD5 hash Google Drive exposes as the ``md5Checksum`` field.

    Exposes the same interface as the hashers in the standard ``hashlib``
    package (``update`` / ``digest`` / ``hexdigest`` / ``copy``), so it is a
    drop-in for any code that previously used a streaming hasher.

    :Example:

        >>> hasher = ContentHasher()
        >>> with open('some-file', 'rb') as f:
        ...     while True:
        ...         chunk = f.read(1024 * 1024)
        ...         if not chunk:
        ...             break
        ...         hasher.update(chunk)
        ... print(hasher.hexdigest())
    """

    def __init__(self) -> None:
        self._hasher = hashlib.md5(usedforsecurity=False)
        self.digest_size = self._hasher.digest_size

    def update(self, new_data: bytes | bytearray) -> None:
        self._hasher.update(new_data)

    def digest(self) -> bytes:
        return self._hasher.digest()

    def hexdigest(self) -> str:
        return self._hasher.hexdigest()

    def copy(self) -> "ContentHasher":
        c = ContentHasher.__new__(ContentHasher)
        c._hasher = self._hasher.copy()
        c.digest_size = self.digest_size
        return c
```

Then update `StreamHasher`'s docstring and type hint to reference `ContentHasher` (lines ~99, ~103, ~114): change `DropboxContentHasher or any 'hashlib' hasher` → `ContentHasher or any 'hashlib' hasher`, the doctest `>>> hasher = DropboxContentHasher()` → `>>> hasher = ContentHasher()`, and the `__init__` annotation `hasher: DropboxContentHasher` → `hasher: ContentHasher`.

- [ ] **Step 4: Point `utils/path.py::content_hash` at the MD5 hasher**

In `src/cumulus/utils/path.py`:
- Line 26: `from .hashing import DropboxContentHasher` → `from .hashing import ContentHasher`
- Line 471: `hasher = DropboxContentHasher()` → `hasher = ContentHasher()`

The rest of `content_hash` (the `"folder"` return, the symlink `hasher.update(b"")` sentinel, the `hexdigest()` return) is unchanged — it now yields MD5.

- [ ] **Step 5: Run tests**

Run: `pytest tests/offline/test_hashing.py -q`
Expected: PASS (5 tests).

Run: `pytest tests/offline --ignore=tests/offline/test_manager.py -q`
Expected: no *new* failures vs the Stage 4 baseline. (Any test asserting an old Dropbox block-hash literal will fail — if so, update that literal to the MD5 value in this task.)

- [ ] **Step 6: Commit**

```bash
git add src/cumulus/utils/hashing.py src/cumulus/utils/path.py tests/offline/test_hashing.py
git commit -m "feat(stage-5): MD5 ContentHasher replacing DropboxContentHasher"
```

---

### Task 2: Collapse `client_modified`/`server_modified` into a single `modified`

This is an atomic refactor — the field rename and every producer/reader must change in one commit or the tree will not import.

**Files:**
- Modify: `src/cumulus/core.py:142-145`
- Modify: `src/cumulus/changes.py:82-90`
- Modify: `src/cumulus/writes.py:307-308,438-445`
- Modify: `src/cumulus/models.py:333`
- Modify: `src/cumulus/sync.py:2651`
- Modify: `src/cumulus/main.py`, `src/cumulus/cli/cli_info.py`, `src/cumulus/cli/cli_maintenance.py` (readers)
- Test: `tests/offline/test_core_models.py`, `tests/offline/test_changes.py`, `tests/offline/test_writes.py` (update constructors)

- [ ] **Step 1: Find every reference**

Run: `grep -rn "client_modified\|server_modified" src/ tests/`
Expected: occurrences in the files listed above. Treat this list as the work-list for this task.

- [ ] **Step 2: Write/adjust the failing test**

In `tests/offline/test_core_models.py`, add (or adapt an existing `FileMetadata` construction test):

```python
def test_file_metadata_has_single_modified_field():
    from datetime import datetime, timezone
    from cumulus.core import FileMetadata

    md = FileMetadata(
        name="f.txt",
        path_lower="/f.txt",
        path_display="/f.txt",
        id="ID1",
        modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        rev="REV1",
        size=3,
        symlink_target=None,
        shared=False,
        modified_by=None,
        is_downloadable=True,
        content_hash="abc",
    )
    assert md.modified.year == 2026
    assert not hasattr(md, "client_modified")
    assert not hasattr(md, "server_modified")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/offline/test_core_models.py::test_file_metadata_has_single_modified_field -q`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'modified'`.

- [ ] **Step 4: Change the model**

In `src/cumulus/core.py`, replace lines 142–145:

```python
    client_modified: datetime
    """Modified time in UTC as provided by clients"""
    server_modified: datetime
    """Server-side modified time in UTC"""
```

with:

```python
    modified: datetime
    """Last-modified time in UTC (Google Drive ``modifiedTime``)"""
```

- [ ] **Step 5: Update producers**

`src/cumulus/changes.py:89-90` — replace:

```python
        client_modified=modified,
        server_modified=modified,
```

with:

```python
        modified=modified,
```

`src/cumulus/writes.py:307-308` and `:444-445` — in both `_project_file_metadata` and `_wrap_metadata`, replace the two lines:

```python
            client_modified=parse_drive_time(resp.get("modifiedTime")),
            server_modified=parse_drive_time(resp.get("modifiedTime")),
```

(in `_wrap_metadata` the variable form `client_modified=modified, server_modified=modified,`) with a single `modified=...` line using the same right-hand side:

```python
            modified=parse_drive_time(resp.get("modifiedTime")),
```

and in `_wrap_metadata`:

```python
            modified=modified,
```

- [ ] **Step 6: Update readers**

`src/cumulus/models.py:333` — replace `change_time = md.client_modified.timestamp()` with `change_time = md.modified.timestamp()`.

`src/cumulus/sync.py:2651` — replace `if md.server_modified.timestamp() >= self.get_last_sync(` with `if md.modified.timestamp() >= self.get_last_sync(`.

For each remaining hit from Step 1 in `main.py`, `cli/cli_info.py`, `cli/cli_maintenance.py`: these read `.client_modified` / `.server_modified` for display. Replace each with `.modified`. (Run the Step-1 grep again after editing; it must return zero hits in `src/`.)

- [ ] **Step 7: Update test constructors**

Re-run `grep -rn "client_modified\|server_modified" tests/`. For every `FileMetadata(...)` construction in tests, replace the two `client_modified=` / `server_modified=` kwargs with a single `modified=` kwarg (use the `server_modified` value if they differ). Grep must return zero hits in `tests/` afterward.

- [ ] **Step 8: Run tests**

Run: `pytest tests/offline --ignore=tests/offline/test_manager.py -q`
Expected: PASS (no new failures vs baseline; the new `test_file_metadata_has_single_modified_field` passes).

- [ ] **Step 9: Commit**

```bash
git add src/cumulus tests/offline
git commit -m "refactor(stage-5): collapse client/server_modified into single modified field"
```

---

### Task 3: Align `rev` on `headRevisionId` across the read side (echo-critical)

Today `changes.py::to_file_metadata` sets `rev = str(version)` while `writes.py` records `rev = headRevisionId`. They never match, so `event.rev == local_rev` is always False on download. This task makes the read side use `headRevisionId`.

**Files:**
- Modify: `src/cumulus/changes.py:51-54,91`
- Test: `tests/offline/test_changes.py`

- [ ] **Step 1: Write the failing test**

In `tests/offline/test_changes.py`, add:

```python
def test_to_file_metadata_rev_is_head_revision_id():
    from cumulus.changes import to_file_metadata

    drive_file = {
        "id": "FID",
        "name": "f.txt",
        "mimeType": "text/plain",
        "modifiedTime": "2026-01-01T00:00:00.000Z",
        "size": "3",
        "md5Checksum": "abc123",
        "headRevisionId": "HEADREV9",
        "version": "42",
        "shared": False,
    }
    md = to_file_metadata(drive_file, "/f.txt")
    assert md.rev == "HEADREV9"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/offline/test_changes.py::test_to_file_metadata_rev_is_head_revision_id -q`
Expected: FAIL — `md.rev` equals `"42"` (the version), not `"HEADREV9"`.

- [ ] **Step 3: Add `headRevisionId` to `FILE_FIELDS` and switch the projection**

In `src/cumulus/changes.py`, replace `FILE_FIELDS` (lines 51–54):

```python
FILE_FIELDS = (
    "id, name, parents, mimeType, modifiedTime, size, md5Checksum, "
    "shared, version, trashed, lastModifyingUser(permissionId)"
)
```

with (drop the now-unused `version`, add `headRevisionId`):

```python
FILE_FIELDS = (
    "id, name, parents, mimeType, modifiedTime, size, md5Checksum, "
    "shared, headRevisionId, trashed, lastModifyingUser(permissionId)"
)
```

Then change line 91:

```python
        rev=str(file.get("version") or ""),
```

to:

```python
        rev=file.get("headRevisionId", ""),
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/offline/test_changes.py -q`
Expected: PASS. (If any existing test asserted `rev == version`, update it to the headRevisionId expectation — that assertion encoded the bug.)

Run: `pytest tests/offline --ignore=tests/offline/test_manager.py -q`
Expected: no new failures.

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/changes.py tests/offline/test_changes.py
git commit -m "fix(stage-5): rev=headRevisionId on read side to match write side (echo suppression)"
```

---

### Task 4: Download-by-fileId seam

`client.download` currently resolves a path via the cache. The sync engine must download a specific file by its Drive id. Add an optional `file_id` param and switch the callsite off the Dropbox `f"rev:{...}"` string.

**Files:**
- Modify: `src/cumulus/client.py:660-684`
- Modify: `src/cumulus/sync.py:3540-3549`
- Test: `tests/offline/test_client.py`

- [ ] **Step 1: Write the failing test**

In `tests/offline/test_client.py`, add (follow the existing `download` test's mocking style for `_get_service`/`files().get`/`get_media`; adapt names to the file's fixtures):

```python
def test_download_by_file_id_skips_path_lookup(monkeypatch, drive_client):
    # When file_id is given, download must NOT call path_to_id.
    called = {"path_to_id": False}
    monkeypatch.setattr(
        drive_client._path_cache,
        "path_to_id",
        lambda p: (called.__setitem__("path_to_id", True) or "WRONG"),
    )
    # ... arrange the same service mock the existing download test uses,
    # asserting files().get(fileId="GIVEN_ID") / get_media(fileId="GIVEN_ID")
    drive_client.download("/whatever.txt", str(tmp_target), file_id="GIVEN_ID")
    assert called["path_to_id"] is False
```

(If the existing `download` test already builds a reusable `drive_client` fixture + service mock, reuse it; otherwise mirror that test's setup. The behavioural assertion is: `file_id` given ⇒ `path_to_id` is never called and the Drive `fileId` used is `"GIVEN_ID"`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/offline/test_client.py::test_download_by_file_id_skips_path_lookup -q`
Expected: FAIL — `download()` has no `file_id` parameter (`TypeError`).

- [ ] **Step 3: Add the `file_id` parameter**

In `src/cumulus/client.py`, change the `download` signature (line 660) to:

```python
    def download(
        self,
        remote_path: str,
        local_path: str,
        sync_event: SyncEvent | None = None,
        *,
        file_id: str | None = None,
    ) -> FileMetadata:
```

Update the docstring to note: "If *file_id* is given, the file is fetched by that Drive id (current head) and *remote_path* is used only for logging and error messages; otherwise *remote_path* is resolved through the path cache."

Then replace the path-resolution block (lines 677–684):

```python
        self._ensure_root_id()
        file_id = self._path_cache.path_to_id(remote_path)
        if file_id is None:
            raise NotFoundError(
                "Not found",
                f"No item at '{remote_path}' is known to Cumulus.",
                remote_path=remote_path,
            )
```

with:

```python
        self._ensure_root_id()
        if file_id is None:
            file_id = self._path_cache.path_to_id(remote_path)
        if file_id is None:
            raise NotFoundError(
                "Not found",
                f"No item at '{remote_path}' is known to Cumulus.",
                remote_path=remote_path,
            )
```

The rest of `download` (the `files().get(fileId=file_id, …)` head fetch, native-doc guard, `get_media`, MD5 verify) is unchanged and now works for an explicit id.

- [ ] **Step 4: Switch the sync-engine callsite**

In `src/cumulus/sync.py` (~3540), replace:

```python
                    md = self.client.download(
                        f"rev:{event.rev}", tmp_fname, sync_event=event
                    )
                event = SyncEvent.from_metadata(md, self)
            except SyncError as err:
                # Replace rev number with path.
                err.remote_path = event.remote_path
                raise err
```

with:

```python
                    md = self.client.download(
                        event.remote_path,
                        tmp_fname,
                        sync_event=event,
                        file_id=event.remote_id,
                    )
                event = SyncEvent.from_metadata(md, self)
            except SyncError as err:
                raise err
```

The `err.remote_path = event.remote_path` rewrite was only needed because the first arg used to be a `rev:` string; the error already carries the real path now. (If the linter flags `except ... : raise err` as redundant, drop the try/except entirely so the `SyncError` propagates unchanged — verify no other cleanup lived in that block first.)

- [ ] **Step 5: Run tests**

Run: `pytest tests/offline/test_client.py tests/offline/test_sync.py -q`
Expected: PASS.

Run: `pytest tests/offline --ignore=tests/offline/test_manager.py -q`
Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add src/cumulus/client.py src/cumulus/sync.py tests/offline/test_client.py
git commit -m "feat(stage-5): download by fileId via event.remote_id; drop rev-string callsite"
```

---

### Task 5: Conflict resolution under MD5 + single `modified`

`_check_download_conflict` and `_check_requires_upload` compare `rev` and `content_hash`, which now carry Drive values — verify they behave correctly and add Drive-shaped tests. Resolve the `move(update_rev=)` question.

**Files:**
- Modify (if needed): `src/cumulus/sync.py:3207-3290,2748-2780`
- Test: `tests/offline/test_sync.py`

- [ ] **Step 1: Write the table-driven failing test**

In `tests/offline/test_sync.py`, add a test exercising each `Conflict` outcome with Drive-shaped values (reuse the file's existing `sync_engine` fixture and index-seeding helpers; adapt names):

```python
import pytest
from cumulus.sync import Conflict


@pytest.mark.parametrize(
    "remote_rev, remote_hash, seed_rev, seed_hash, local_bytes, expect",
    [
        ("HEADREV1", "md5A", "HEADREV1", "md5A", b"A", Conflict.LocalNewerOrIdentical),
        ("HEADREV2", "md5A", "HEADREV1", "md5A", b"A", Conflict.Identical),
    ],
)
def test_download_conflict_drive_values(
    sync_engine, make_download_event, remote_rev, remote_hash,
    seed_rev, seed_hash, local_bytes, expect,
):
    event = make_download_event(
        rev=remote_rev, content_hash=remote_hash, local_bytes=local_bytes
    )
    sync_engine._seed_index(event.remote_path_lower, rev=seed_rev, content_hash=seed_hash)
    assert sync_engine._check_download_conflict(event) == expect
```

If `make_download_event` / `_seed_index` helpers don't exist in `test_sync.py`, build minimal local helpers in the test module: write `local_bytes` to `event.local_path`, construct a download `SyncEvent`, and insert an `IndexEntry` with the seed rev/hash via the engine's index API.

- [ ] **Step 2: Run test to verify it fails (or passes — confirm semantics)**

Run: `pytest tests/offline/test_sync.py -k download_conflict_drive_values -q`
Expected: the two cases pass on the existing logic (equal rev → `LocalNewerOrIdentical`; equal hash → `Identical`). If either FAILS, that is a real semantic gap — fix `_check_download_conflict` so the documented behaviour holds, then re-run. This step's value is *proving* the logic is correct under Drive values, not assuming it.

- [ ] **Step 3: Add the `_check_requires_upload` skip test**

```python
def test_requires_upload_false_when_md5_matches(sync_engine, make_upload_event, monkeypatch):
    event = make_upload_event(content_hash="md5SAME")
    fake_remote = _file_md(content_hash="md5SAME", symlink_target=None)  # helper
    monkeypatch.setattr(sync_engine.client, "get_metadata", lambda p: fake_remote)
    assert sync_engine._check_requires_upload(event) is False
```

Run: `pytest tests/offline/test_sync.py -k requires_upload_false_when_md5 -q`
Expected: PASS (the `event.content_hash == md_old.content_hash` branch already does this once both are MD5).

- [ ] **Step 4: Resolve the `move(update_rev=)` question**

Inspect `src/cumulus/sync.py::_on_local_moved` (search `def _on_local_moved`). Determine whether it preconditions the move on a rev (Dropbox did a rev-checked remove-of-destination). If it calls `client.move(...)` with no rev guard and the existing logic is sufficient for the move smoke item, **leave `move` as-is** and add a one-line note in the deviations doc (Task 9) that `move(update_rev=)` was confirmed not load-bearing for Stage 5. Do **not** add the parameter speculatively. If a rev guard is genuinely required (the method tries to pass `parent_rev`/`update_rev` to `move`), record that finding and stop to discuss before expanding scope.

- [ ] **Step 5: Run tests**

Run: `pytest tests/offline/test_sync.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/cumulus/sync.py tests/offline/test_sync.py
git commit -m "test(stage-5): conflict resolution verified under MD5 + headRevisionId"
```

---

### Task 6: Upload feedback — set `modifiedTime`, record index values, prove echo suppression

The upload must set `modifiedTime` (so a round-trip doesn't report a surprising server time) and the upload handler must record Drive's returned `headRevisionId` + md5 in the index, so a re-observed change self-skips.

**Files:**
- Modify (if needed): `src/cumulus/writes.py` (upload body), `src/cumulus/sync.py::_on_local_file_modified`
- Test: `tests/offline/test_sync.py`, `tests/offline/test_writes.py`

- [ ] **Step 1: Write the echo-suppression unit test**

In `tests/offline/test_sync.py`, add the make-or-break test:

```python
def test_echo_of_own_upload_is_skipped(sync_engine, make_upload_result_event):
    # Simulate: we uploaded /foo.txt; index now holds the returned rev+md5.
    md = make_upload_result_event(path="/foo.txt", rev="HEADREVX", content_hash="md5X")
    sync_engine.update_index_from_remote_metadata(md)
    # The change feed re-delivers the same file as a download event.
    from cumulus.models import SyncEvent
    echo = SyncEvent.from_metadata(md, sync_engine)
    from cumulus.sync import Conflict
    verdict = sync_engine._check_download_conflict(echo)
    assert verdict in (Conflict.LocalNewerOrIdentical, Conflict.Identical)
```

Run: `pytest tests/offline/test_sync.py -k echo_of_own_upload -q`
Expected: PASS — `echo.rev (HEADREVX) == local_rev (HEADREVX)` ⇒ `LocalNewerOrIdentical`. If it FAILS, the index is not recording `headRevisionId`; fix the upload→index path (Step 3) until it passes. This test is the data-level proof behind the spec's §5.3 claim.

- [ ] **Step 2: Verify `modifiedTime` is set on upload**

Search `src/cumulus/writes.py` for the `files.create` / `files.update` media-upload body (the upload method). Check whether the request body sets `modifiedTime`. Write a `test_writes.py` test asserting the upload body includes `modifiedTime` set from the local file mtime:

```python
def test_upload_sets_modified_time(write_helper, tmp_path, captured_body):
    # arrange a local file with a known mtime; run upload; assert the
    # files.create/update body carried "modifiedTime".
    ...
    assert "modifiedTime" in captured_body
```

Run it. Expected: FAIL if `modifiedTime` is not currently set.

- [ ] **Step 3: Set `modifiedTime` on upload + ensure index records returned metadata**

In the `writes.py` upload method, add `modifiedTime` to the create/update request body, formatted as RFC-3339 UTC from the local file's mtime, e.g.:

```python
from datetime import datetime, timezone
mtime = os.path.getmtime(local_path)
body["modifiedTime"] = (
    datetime.fromtimestamp(mtime, tz=timezone.utc)
    .isoformat()
    .replace("+00:00", "Z")
)
```

(Place this where `body` is assembled for both the simple and resumable paths; the resumable metadata body is the `body` dict passed to the resumable request — set it there too. Request `headRevisionId,modifiedTime,md5Checksum` in the response `fields` — line ~575 already lists these.)

In `src/cumulus/sync.py::_on_local_file_modified`, confirm the post-upload path calls `update_index_from_remote_metadata(md_new)` (or `update_index_from_sync_event`) with the FileMetadata returned by `client.upload` — so the index holds the new `headRevisionId` + md5. If it already does (the Stage 3/4 code does), no change beyond Step 2's body edit.

- [ ] **Step 4: Run tests**

Run: `pytest tests/offline/test_sync.py tests/offline/test_writes.py -q`
Expected: PASS, including `test_echo_of_own_upload_is_skipped` and `test_upload_sets_modified_time`.

Run: `pytest tests/offline --ignore=tests/offline/test_manager.py -q`
Expected: no new failures.

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/writes.py src/cumulus/sync.py tests/offline
git commit -m "feat(stage-5): set modifiedTime on upload; index records headRevisionId for echo suppression"
```

---

### Task 7: Wiring & lifecycle

Drop the `import ... as DropboxClient` alias, ensure `SyncEngine` is constructed with the `GoogleDriveClient` + `path_cache`, and make `client.close()` reachable from shutdown.

**Files:**
- Modify: `src/cumulus/sync.py:64` and usages
- Modify: `src/cumulus/main.py` (shutdown path)
- Test: `tests/offline/test_sync.py`

- [ ] **Step 1: Replace the alias import**

In `src/cumulus/sync.py` line 64, replace:

```python
from .client import GoogleDriveClient as DropboxClient  # TODO: rename all usages
```

with:

```python
from .client import GoogleDriveClient
```

Then update usages: `grep -n "DropboxClient" src/cumulus/sync.py` and replace each `DropboxClient` token with `GoogleDriveClient` (the type annotation at line ~542 `client: DropboxClient` becomes `client: GoogleDriveClient`).

- [ ] **Step 2: Run the suite to catch breakage**

Run: `pytest tests/offline --ignore=tests/offline/test_manager.py -q`
Expected: no new failures (this is a rename; the symbol resolves to the same class).

- [ ] **Step 3: Wire `client.close()` into shutdown**

In `src/cumulus/main.py`, find `shutdown_daemon` (and/or the `Maestral` teardown). Confirm `self.client.close()` is called during shutdown; if not, add it (guard with `hasattr`/`try` if the client may be unlinked). Add a test in `tests/offline/test_main.py` *only if* a clean fixture exists; otherwise verify by inspection and note it as smoke-covered.

- [ ] **Step 4: Commit**

```bash
git add src/cumulus/sync.py src/cumulus/main.py tests/offline
git commit -m "refactor(stage-5): use GoogleDriveClient directly; wire client.close() into shutdown"
```

---

### Task 8: End-to-end smoke test against the real Drive

Prove bidirectional sync works. This is the §7 "stage complete" bar.

**Files:**
- Create: `/tmp/stage5_smoke.py` (NOT committed)

- [ ] **Step 1: Pre-flight**

Confirm no pytest run is in progress and that linking the smoke account will not collide with tests (you will run **no** pytest in this task). Use the linked `smoke-test` config (mirror the Stage 4 harness at `/tmp/stage4_smoke.py` if it still exists, or reconstruct from the Stage 4 deviations file's description).

- [ ] **Step 2: Write the harness**

`/tmp/stage5_smoke.py` constructs a `GoogleDriveClient` + a `SyncEngine` (DB-backed `PathIDCache`, real local sync-root dir) pointed at an isolated `/stage5-smoke-<ts>` Drive subfolder. Bootstrap the cache via `list_folder('/stage5-smoke-<ts>', recursive=True)`. Then drive the cycles by hand. Implement all 8 checks (see Step 3). Trash the smoke folder via `client.remove("/stage5-smoke-<ts>")` in a `finally`.

- [ ] **Step 3: Run the 8 checks and record PASS/FAIL per item with a timestamp**

1. **Local→remote create** — write local file → `upload_sync_cycle()` → assert it exists on Drive, md5 matches, index holds `headRevisionId` + md5.
2. **Remote→local create** — `client.upload` a new file directly → `download_sync_cycle()` → assert it lands locally with correct bytes.
3. **Echo suppression (make-or-break)** — immediately after check 1, run `download_sync_cycle()` → assert the just-uploaded file is **skipped** (no re-download, no `(conflicting copy)` sibling created locally, status Skipped/Identical).
4. **Local modify → upload** — change the local file → upload → assert a new revision (index rev changed) and md5 updated.
5. **Remote modify → download** — `client.upload(WriteMode.Update)` on Drive → `download_sync_cycle()` → assert local bytes update (RemoteNewer path).
6. **Delete propagation** — delete locally → upload → assert Drive `trashed`; delete on Drive → download → assert local file removed.
7. **Conflict** — modify both sides without syncing between → `download_sync_cycle()` → assert a conflict-copy is created locally and both versions survive.
8. **`modifiedTime` feedback** — after an upload, run a second `download_sync_cycle()` → assert the file is not seen as changed (no feedback loop; ties back to check 3).

- [ ] **Step 4: Capture results**

Record the run output (PASS/FAIL per item, timestamp, account, any observations or surprises) — this feeds Task 9. If any check fails, debug per `superpowers:systematic-debugging`, fix inline, re-run. Check 3 failing means echo suppression is broken — revisit Task 3/Task 6 before declaring the stage done.

- [ ] **Step 5: Unlink / restore test-safe state**

After the run, ensure the smoke account state is cleaned up so a later `pytest tests/offline` won't be affected (per the keyring memory).

(No commit — the harness is not committed.)

---

### Task 9: Deviations + smoke-test record, memory update

**Files:**
- Create: `docs/superpowers/plans/2026-06-03-stage-5-sync-engine-deviations.md`
- Modify: memory `project_cumulus_port.md` + `MEMORY.md`

- [ ] **Step 1: Write the deviations doc**

Mirror the Stage 3/4 deviations structure: implementation summary (commit range, offline-test count delta), deviations from plan, deviations from spec, risks/edge cases noted, the `move(update_rev=)` resolution from Task 5 Step 4, the full smoke-test checklist with PASS/FAIL + timestamp + observations from Task 8, and a "Stage 6 carry-over notes" section (D1 RPC audit, D4 GUI compat, `_throttle_up`, batch tuning, `writes.py` consolidation, `_count_request` threading, cross-page change-feed ordering durability, any echo-storm findings).

- [ ] **Step 2: Commit the doc**

```bash
git add docs/superpowers/plans/2026-06-03-stage-5-sync-engine-deviations.md
git commit -m "docs(stage-5): deviations + smoke-test record"
```

- [ ] **Step 3: Update memory**

Update `project_cumulus_port.md` to mark Stage 5 complete (commit range, smoke-test PASS date, key findings: rev=headRevisionId fix, download-by-remote_id, emergent echo suppression confirmed) and set "Current stage: Stage 6 (Manager + CLI + DB schema)". Update the `MEMORY.md` one-line pointer.

---

## Self-Review

**Spec coverage:**
- §2.1.1 MD5 hashing → Task 1 ✓
- §2.1.2 single `modified` + set on upload → Task 2 (field) + Task 6 (set on upload) ✓
- §2.1.3 rev→headRevisionId + download-by-fileId → Task 3 (rev) + Task 4 (download) ✓
- §2.1.4 conflict resolution → Task 5 ✓
- §2.1.5 echo suppression → Task 3 + Task 6 (proven by `test_echo_of_own_upload_is_skipped` + smoke check 3) ✓
- §2.1.6 wiring + `client.close()` → Task 7 ✓
- §7.1 offline tests → each task's tests ✓; §7.2 smoke → Task 8 ✓; §7.3 deliverables → Task 9 ✓
- §9 `move(update_rev=)` open question → Task 5 Step 4 ✓
- Deferrals (D1, D4, throttle, batch tuning, consolidation, cross-page ordering) → recorded as Stage 6 carry-overs in Task 9 ✓

**Placeholder scan:** No TBD/TODO left in steps. The one residual judgement call (`move(update_rev=)`) is an explicit inspect-and-decide step with a defined default (leave as-is) and a stop-and-discuss escape hatch, not a placeholder.

**Type consistency:** `ContentHasher` (Task 1) used consistently; `modified` field name consistent across Tasks 2/6; `rev=headRevisionId` consistent Tasks 3/5/6; `event.remote_id` for download consistent Tasks 4/spec; `file_id` param name consistent Task 4.
