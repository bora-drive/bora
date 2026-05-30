# Stage 4 — Write client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the write half of the Cumulus Google Drive client (upload, move, remove, mkdir, batches, restore) so the daemon can push local changes to Drive.

**Architecture:** A new `src/cumulus/writes.py` module owns mutating-op orchestration (resumable upload state machine, autorename loop, batch chunking, restore, path-cache write-through) — mirroring the Stage 3 `changes.py` extraction. `client.py`'s seven `_not_implemented` write stubs and the `restore` `NotImplementedError` stub become one-line delegators to a `_WriteHelper` instance held on the client. `errorhandling.py` gains one new HttpError row (412 `conditionNotMet`); the other write-side rows from spec §6.1 are already handled by Stage 3's fall-throughs.

**Tech Stack:** Python 3.10+, `googleapiclient` (Drive v3), `googleapiclient.http.MediaFileUpload` / `MediaIoBaseDownload` / `BatchHttpRequest`, `pytest` + `unittest.mock`, existing project infrastructure (`PathIDCache`, `path_codec`, `convert_api_errors`, `_retry_transient`).

**Reading order for the implementer:** the design spec is at `docs/superpowers/specs/2026-05-30-stage-4-write-client-design.md`. Read it before starting — every task below references a numbered section of that spec. Read `src/cumulus/changes.py` and `tests/offline/test_changes.py` for the established extraction-helper pattern Stage 3 set up. The mock fixtures in `tests/offline/conftest.py` (`make_http_error`) and the `client` / `cache` / `fake_service()` helpers in `tests/offline/test_client.py` are reused by every task here.

---

## File Plan

**New files:**
- `src/cumulus/writes.py` — the `_WriteHelper` class with all mutating-op logic (~400-550 LOC).
- `tests/offline/test_writes.py` — coverage per spec §7.

**Modified files:**
- `src/cumulus/client.py` — `__init__` instantiates `self._writes`; the 7 `_not_implemented` write stubs + the `restore` `NotImplementedError` stub become one-line delegators.
- `src/cumulus/errorhandling.py` — `http_error_to_cumulus` adds the 412 `conditionNotMet` branch.
- `tests/offline/test_client.py` — extend with delegator-wiring tests for each new public write method.
- `tests/offline/test_errorhandling.py` — extend with parameterized cases for the new error row.

**Deviations file** (created at task 11):
- `docs/superpowers/plans/2026-05-30-stage-4-write-client-deviations.md` — smoke-test record and any in-flight deviations.

---

## Task 1: Add 412 conditionNotMet → DataChangedError to errorhandling

The 412 precondition-failed branch is the one genuinely new mapping row. The other rows in spec §6.1 are already covered by Stage 3's `http_error_to_cumulus`: `403 storageQuotaExceeded` → `InsufficientSpaceError` (line 145-151), generic 403 fall-through → `InsufficientPermissionsError` (line 166-171), `404` → `NotFoundError` (line 172-178), `400` → `BadInputError` (line 126-132).

**Files:**
- Modify: `src/cumulus/errorhandling.py:172-178` (insert 412 branch after the 404 branch)
- Test: `tests/offline/test_errorhandling.py` (append)

- [ ] **Step 1.1: Write the failing test**

Append to `tests/offline/test_errorhandling.py`:

```python
def test_http_error_412_condition_not_met_maps_to_data_changed(make_http_error):
    from cumulus.errorhandling import http_error_to_cumulus
    from cumulus.exceptions import DataChangedError

    exc = make_http_error(412, reason="conditionNotMet")
    out = http_error_to_cumulus(412, "conditionNotMet", exc, remote_path="/foo.txt")
    assert isinstance(out, DataChangedError)
    assert "/foo.txt" in str(out) or out.remote_path == "/foo.txt"


def test_http_error_412_other_reason_falls_through(make_http_error):
    from cumulus.errorhandling import http_error_to_cumulus
    from cumulus.exceptions import DataChangedError

    # Any 412 should map to DataChangedError regardless of sub-reason.
    exc = make_http_error(412, reason="")
    out = http_error_to_cumulus(412, "", exc)
    assert isinstance(out, DataChangedError)
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
pytest tests/offline/test_errorhandling.py -k "412" -v
```

Expected: FAIL — current behavior returns the generic `CumulusApiError` for 412 (the last `return` in `http_error_to_cumulus`), so the `isinstance(out, DataChangedError)` assertion fails.

- [ ] **Step 1.3: Implement the 412 branch**

In `src/cumulus/errorhandling.py`, locate the 404 block (currently around line 172-178) and insert a 412 block immediately after it:

```python
    if status == 412:
        return DataChangedError(
            "Item changed on Google Drive",
            "The item changed on Drive since Cumulus last checked. "
            "Cumulus will reconcile on the next sync pass.",
            remote_path=remote_path,
            local_path=local_path,
        )
```

Ensure `DataChangedError` is in the `from .exceptions import (...)` block at the top of the file. If not, add it.

- [ ] **Step 1.4: Run test to verify it passes**

```bash
pytest tests/offline/test_errorhandling.py -k "412" -v
```

Expected: PASS (both tests).

- [ ] **Step 1.5: Run the full errorhandling test file**

```bash
pytest tests/offline/test_errorhandling.py -v
```

Expected: all existing tests still PASS.

- [ ] **Step 1.6: Commit**

```bash
git add src/cumulus/errorhandling.py tests/offline/test_errorhandling.py
git commit -m "feat(stage-4/errorhandling): map 412 conditionNotMet to DataChangedError"
```

---

## Task 2: writes.py scaffold + client.py delegators

Create the `writes.py` module with `_WriteHelper` skeleton; replace all 8 stub methods on `GoogleDriveClient` (`upload`, `remove`, `remove_batch`, `move`, `make_dir`, `make_dir_batch`, `restore`) with one-line delegators. `share_dir` stays stubbed. After this task, calling any write method raises `NotImplementedError` from the helper — but the wiring is in place and existing tests still pass.

**Files:**
- Create: `src/cumulus/writes.py`
- Modify: `src/cumulus/client.py` — `__init__` adds `self._writes`; replace stubs at lines 643-833 (the eight write methods listed above).
- Test: `tests/offline/test_client.py` (append delegator tests)

- [ ] **Step 2.1: Write failing tests for the scaffold + delegators**

Append to `tests/offline/test_client.py`:

```python
# ==== writes.py wiring + delegators =======================================

def test_client_constructs_write_helper(client):
    from cumulus.writes import _WriteHelper

    assert isinstance(client._writes, _WriteHelper)
    assert client._writes._client is client


def test_upload_delegates_to_helper(client, monkeypatch):
    captured = {}

    def fake_upload(local_path, remote_path, *, write_mode, update_rev,
                    autorename, sync_event):
        captured.update(
            local_path=local_path, remote_path=remote_path,
            write_mode=write_mode, update_rev=update_rev,
            autorename=autorename, sync_event=sync_event,
        )
        return "sentinel-result"

    monkeypatch.setattr(client._writes, "upload", fake_upload)
    from cumulus.core import WriteMode

    out = client.upload(
        "/local/a.txt", "/a.txt",
        write_mode=WriteMode.Overwrite, update_rev="rev1",
        autorename=True, sync_event=None,
    )
    assert out == "sentinel-result"
    assert captured == {
        "local_path": "/local/a.txt", "remote_path": "/a.txt",
        "write_mode": WriteMode.Overwrite, "update_rev": "rev1",
        "autorename": True, "sync_event": None,
    }


def test_remove_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "remove",
        lambda remote_path, parent_rev=None: captured.update(
            remote_path=remote_path, parent_rev=parent_rev,
        ) or "sentinel",
    )
    assert client.remove("/a.txt", parent_rev="r1") == "sentinel"
    assert captured == {"remote_path": "/a.txt", "parent_rev": "r1"}


def test_move_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "move",
        lambda remote_path, new_path, *, autorename=False: captured.update(
            remote_path=remote_path, new_path=new_path, autorename=autorename,
        ) or "sentinel",
    )
    assert client.move("/a.txt", "/b/a.txt", autorename=True) == "sentinel"
    assert captured == {"remote_path": "/a.txt", "new_path": "/b/a.txt", "autorename": True}


def test_make_dir_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "make_dir",
        lambda remote_path, *, autorename=False: captured.update(
            remote_path=remote_path, autorename=autorename,
        ) or "sentinel",
    )
    assert client.make_dir("/foo", autorename=True) == "sentinel"
    assert captured == {"remote_path": "/foo", "autorename": True}


def test_make_dir_batch_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "make_dir_batch",
        lambda remote_paths: captured.update(paths=list(remote_paths)) or ["s"],
    )
    assert client.make_dir_batch(["/a", "/b"]) == ["s"]
    assert captured == {"paths": ["/a", "/b"]}


def test_remove_batch_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "remove_batch",
        lambda entries, batch_size=100: captured.update(
            entries=list(entries), batch_size=batch_size,
        ) or ["s"],
    )
    assert client.remove_batch([("/a", None), ("/b", "r")]) == ["s"]
    assert captured == {"entries": [("/a", None), ("/b", "r")], "batch_size": 100}


def test_restore_delegates_to_helper(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client._writes, "restore",
        lambda remote_path, rev: captured.update(
            remote_path=remote_path, rev=rev,
        ) or "sentinel",
    )
    assert client.restore("/a.txt", "rev5") == "sentinel"
    assert captured == {"remote_path": "/a.txt", "rev": "rev5"}
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
pytest tests/offline/test_client.py -k "delegate or write_helper" -v
```

Expected: FAIL with `ImportError: cannot import name '_WriteHelper'` (writes.py doesn't exist yet) and/or AttributeError on `client._writes`.

- [ ] **Step 2.3: Create `src/cumulus/writes.py`**

```python
"""Stage 4 — write-side helper.

``_WriteHelper`` owns the mutating-op orchestration on top of the Drive
v3 service that ``GoogleDriveClient`` exposes. It mirrors the Stage 3
``changes.py`` extraction: ``client.py`` keeps a thin public façade and
delegates every mutating method here. Cache write-through is owned by
this module — every successful op mutates the client's ``PathIDCache``
synchronously before returning.

See ``docs/superpowers/specs/2026-05-30-stage-4-write-client-design.md``
for the design and behaviour table.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Sequence

from .core import FileMetadata, FolderMetadata, WriteMode, SyncEvent
from .exceptions import CumulusApiError

if TYPE_CHECKING:  # avoid runtime cycle
    from .client import GoogleDriveClient

logger = logging.getLogger(__name__)


class _WriteHelper:
    """Owner of all mutating Drive API calls.

    Constructed once by ``GoogleDriveClient.__init__`` with a back-reference
    to the client; reads the client's ``_get_service``, ``_retry_transient``,
    ``_count_request``, ``_ensure_root_id``, ``_path_cache``, and
    ``_poll_event`` through that reference.
    """

    def __init__(self, client: "GoogleDriveClient") -> None:
        self._client = client

    # ---- public API (filled in by later tasks) --------------------------

    def upload(
        self,
        local_path: str,
        remote_path: str,
        *,
        write_mode: WriteMode = WriteMode.Add,
        update_rev: str | None = None,
        autorename: bool = False,
        sync_event: SyncEvent | None = None,
    ) -> FileMetadata:
        raise NotImplementedError  # task 6 / task 7

    def remove(
        self, remote_path: str, parent_rev: str | None = None
    ) -> FileMetadata | FolderMetadata:
        raise NotImplementedError  # task 4

    def remove_batch(
        self,
        entries: Sequence[tuple[str, str | None]],
        batch_size: int = 100,
    ) -> list[FileMetadata | FolderMetadata | CumulusApiError]:
        raise NotImplementedError  # task 9

    def move(
        self, remote_path: str, new_path: str, *, autorename: bool = False
    ) -> FileMetadata | FolderMetadata:
        raise NotImplementedError  # task 5

    def make_dir(
        self, remote_path: str, *, autorename: bool = False
    ) -> FolderMetadata:
        raise NotImplementedError  # task 3

    def make_dir_batch(
        self, remote_paths: list[str]
    ) -> list[FolderMetadata | CumulusApiError]:
        raise NotImplementedError  # task 8

    def restore(self, remote_path: str, rev: str) -> FileMetadata:
        raise NotImplementedError  # task 10
```

- [ ] **Step 2.4: Wire `_WriteHelper` into `GoogleDriveClient.__init__`**

In `src/cumulus/client.py`, after the other `__init__` attribute assignments (look for `self._path_cache = path_cache` or the last `self.<attr> =` line in `__init__`), add:

```python
        from .writes import _WriteHelper
        self._writes = _WriteHelper(self)
```

(Use the local import to avoid a top-of-file cycle between `client.py` and `writes.py`.)

- [ ] **Step 2.5: Replace the 8 stub methods in `client.py` with delegators**

Find the stub methods in `src/cumulus/client.py` (currently around lines 643-833). Replace each in turn with a delegator — keep docstrings, drop the `_not_implemented()` body. Example transformation for `make_dir`:

Before:
```python
    def make_dir(self, remote_path: str, autorename: bool = False) -> FolderMetadata:
        """Creates a folder.
        ...
        """
        _not_implemented()
        return FolderMetadata("", "", "", "", False)  # type: ignore[call-arg]  # unreachable
```

After:
```python
    def make_dir(self, remote_path: str, autorename: bool = False) -> FolderMetadata:
        """Creates a folder.
        ...
        """
        return self._writes.make_dir(remote_path, autorename=autorename)
```

Do the same for: `upload`, `remove`, `remove_batch`, `move`, `make_dir`, `make_dir_batch`, `restore`. Keep `share_dir` as the `_not_implemented` stub (it's out of scope per spec §2.2). Preserve each method's signature and docstring verbatim — only the body changes.

For `restore`, replace the `NotImplementedError(...)` body with `return self._writes.restore(remote_path, rev)`.

- [ ] **Step 2.6: Run delegator tests to verify they pass**

```bash
pytest tests/offline/test_client.py -k "delegate or write_helper" -v
```

Expected: all 8 PASS.

- [ ] **Step 2.7: Run the full offline test suite**

```bash
pytest tests/offline -v
```

Expected: no new failures vs the Stage 3 baseline (the pre-existing failures noted in `project_cumulus_port.md` are tracked work for later stages; do not "fix" them here). The `client.upload`/`remove`/etc. methods now route to `_WriteHelper` whose stubs raise `NotImplementedError` — that's the expected end state for this task.

- [ ] **Step 2.8: Commit**

```bash
git add src/cumulus/writes.py src/cumulus/client.py tests/offline/test_client.py
git commit -m "feat(stage-4/writes): scaffold _WriteHelper, wire client delegators"
```

---

## Task 3: make_dir

Implement `_WriteHelper.make_dir` plus the three shared helpers it introduces (`_decode_segment`, `_resolve_parent_id`, `_autorename_unique`). Subsequent tasks (move, upload) reuse these helpers — implement them carefully here.

**Files:**
- Modify: `src/cumulus/writes.py`
- Create: `tests/offline/test_writes.py`

- [ ] **Step 3.1: Write the failing tests**

Create `tests/offline/test_writes.py`:

```python
"""Offline unit tests for cumulus.writes._WriteHelper.

The Drive API is mocked at the client._get_service() boundary, same as
test_client.py. Each test injects a chained MagicMock service.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, Mock, patch

import pytest

from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.database.core import Database
from cumulus.exceptions import (
    BadInputError,
    DataChangedError,
    NotFoundError,
)
from cumulus.keyring import CredentialStorage
from cumulus.path_cache import PathIDCache

_CONFIG = "test-config-writes-offline"


@pytest.fixture
def cache() -> PathIDCache:
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = "ROOT"
    return c


@pytest.fixture
def client(cache: PathIDCache):
    cred = CredentialStorage(_CONFIG)
    cl = GoogleDriveClient(_CONFIG, cred, cache)
    # Disable the network-touching ensure-root-id step: tests pre-seed ROOT.
    cl._ensure_root_id = Mock()
    yield cl
    cred.delete_creds()
    remove_configuration(_CONFIG)


def fake_service() -> MagicMock:
    return MagicMock(name="drive-service")


# ==== make_dir ============================================================

def test_make_dir_creates_folder_and_updates_cache(client, cache):
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new",
        "name": "foo",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    result = client._writes.make_dir("/foo")

    # Drive call shape
    create = svc.files.return_value.create
    create.assert_called_once()
    body = create.call_args.kwargs["body"]
    assert body == {
        "name": "foo",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["ROOT"],
    }
    # Cache write-through
    assert cache.path_to_id("/foo") == "F-new"
    # Return value
    assert result.id == "F-new"
    assert result.name == "foo"


def test_make_dir_decodes_name_segment(client, cache):
    # path_codec encodes '/' in names as %2F; the upload/mkdir side
    # must decode before sending to Drive.
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-x", "name": "a/b",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.make_dir("/a%2Fb")

    body = svc.files.return_value.create.call_args.kwargs["body"]
    assert body["name"] == "a/b"


def test_make_dir_missing_parent_raises_not_found(client):
    client._get_service = Mock(return_value=fake_service())
    with pytest.raises(NotFoundError):
        client._writes.make_dir("/nope/foo")


def test_make_dir_autorename_single_collision(client, cache):
    cache.set("/foo", "F-existing")
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "foo (1)",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    result = client._writes.make_dir("/foo", autorename=True)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    assert body["name"] == "foo (1)"
    assert cache.path_to_id("/foo (1)") == "F-new"
    assert result.name == "foo (1)"


def test_make_dir_autorename_multi_collision(client, cache):
    cache.set("/foo", "F1")
    cache.set("/foo (1)", "F2")
    cache.set("/foo (2)", "F3")
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "foo (3)",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.make_dir("/foo", autorename=True)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    assert body["name"] == "foo (3)"


def test_make_dir_collision_without_autorename_creates_duplicate_name(client, cache):
    # When autorename=False, Drive allows duplicate names. The cache
    # has an existing entry but the call goes through unchanged.
    cache.set("/foo", "F1")
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "foo",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.make_dir("/foo", autorename=False)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    assert body["name"] == "foo"
    # Cache.set overwrites — the new ID wins at this path.
    assert cache.path_to_id("/foo") == "F-new"


def test_make_dir_autorename_bounded_at_999(client, cache):
    # Seed 1000 collisions to force overflow.
    cache.set("/foo", "F0")
    for n in range(1, 1000):
        cache.set(f"/foo ({n})", f"F{n}")
    client._get_service = Mock(return_value=fake_service())

    with pytest.raises(BadInputError):
        client._writes.make_dir("/foo", autorename=True)


def test_make_dir_counts_request(client, cache):
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "foo",
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)
    client._count_request = Mock()

    client._writes.make_dir("/foo")

    client._count_request.assert_called_once()
```

- [ ] **Step 3.2: Run tests to verify they fail**

```bash
pytest tests/offline/test_writes.py -v
```

Expected: FAIL with `NotImplementedError` from the helper stub.

- [ ] **Step 3.3: Implement `make_dir` and the shared helpers in `writes.py`**

Add to `src/cumulus/writes.py`:

```python
import os
from .errorhandling import convert_api_errors
from .exceptions import BadInputError, NotFoundError
from .path_codec import decode_name

_FOLDER_MIME = "application/vnd.google-apps.folder"
_AUTORENAME_LIMIT = 999
```

Then add the helper methods (inside `_WriteHelper`):

```python
    # ---- shared helpers --------------------------------------------------

    @staticmethod
    def _split_parent_name(remote_path: str) -> tuple[str, str]:
        """Split ``remote_path`` into (parent_path, name). Root is "".

        ``/foo/bar`` -> ("/foo", "bar"). ``/foo`` -> ("", "foo").
        """
        if "/" not in remote_path or remote_path == "/":
            raise BadInputError(
                "Invalid path",
                f"Cannot derive a parent + name from {remote_path!r}.",
                remote_path=remote_path,
            )
        parent, name = remote_path.rsplit("/", 1)
        return parent, name

    @staticmethod
    def _decode_segment(name: str) -> str:
        """Decode a path segment via path_codec for the Drive ``name``."""
        return decode_name(name)

    def _resolve_parent_id(self, parent_path: str, *, for_path: str) -> str:
        cache = self._client._path_cache
        if parent_path == "":
            assert cache.root_id is not None
            return cache.root_id
        parent_id = cache.path_to_id(parent_path)
        if parent_id is None:
            raise NotFoundError(
                "Parent folder not found",
                f"The parent of {for_path!r} is not known to Cumulus.",
                remote_path=for_path,
            )
        return parent_id

    def _autorename_unique(self, parent_path: str, name: str) -> str:
        """Return a name not already in path_cache under parent_path.

        Tries ``name``, ``name (1)``, ``name (2)``, … up to N=999. Used by
        upload, move, and make_dir when ``autorename=True``.
        """
        cache = self._client._path_cache
        joiner = "" if parent_path == "" else parent_path
        if cache.path_to_id(joiner + "/" + name) is None:
            return name
        stem, ext = os.path.splitext(name)
        for n in range(1, _AUTORENAME_LIMIT + 1):
            candidate = f"{stem} ({n}){ext}"
            if cache.path_to_id(joiner + "/" + candidate) is None:
                return candidate
        raise BadInputError(
            "Autorename exhausted",
            f"Could not find a non-colliding name for {name!r} under "
            f"{parent_path!r} within {_AUTORENAME_LIMIT} attempts.",
            remote_path=joiner + "/" + name,
        )
```

Then replace the `make_dir` stub with:

```python
    def make_dir(
        self, remote_path: str, *, autorename: bool = False
    ) -> FolderMetadata:
        client = self._client
        client._ensure_root_id()
        parent_path, raw_name = self._split_parent_name(remote_path)
        drive_name = self._decode_segment(raw_name)
        if autorename:
            drive_name = self._autorename_unique(parent_path, drive_name)
            # If autorename picked a suffixed name, the effective
            # remote_path also shifts so the cache write-through stores
            # under the actual stored path.
            remote_path = (parent_path + "/" + drive_name)
        parent_id = self._resolve_parent_id(parent_path, for_path=remote_path)

        body = {
            "name": drive_name,
            "mimeType": _FOLDER_MIME,
            "parents": [parent_id],
        }
        with convert_api_errors(remote_path=remote_path):
            resp = client._retry_transient(
                lambda: client._get_service()
                .files()
                .create(body=body, fields="id,name,mimeType")
                .execute()
            )
        client._count_request()
        client._path_cache.set(remote_path, resp["id"])
        from .core import FolderMetadata as _FM
        from .path_codec import fold_path
        return _FM(
            name=resp["name"],
            path_lower=fold_path(remote_path),
            path_display=remote_path,
            id=resp["id"],
            shared=False,
        )
```

(Top of file: replace the `if TYPE_CHECKING` block with appropriate imports — `FolderMetadata`, `FileMetadata` are already pulled. Keep `path_codec.fold_path` imported at the top once: `from .path_codec import decode_name, fold_path`.)

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
pytest tests/offline/test_writes.py -v
```

Expected: all 8 make_dir tests PASS.

- [ ] **Step 3.5: Commit**

```bash
git add src/cumulus/writes.py tests/offline/test_writes.py
git commit -m "feat(stage-4/writes): make_dir + shared helpers (autorename, decode, parent resolve)"
```

---

## Task 4: remove

Implement `_WriteHelper.remove` (default trash) and the internal `_hard_delete` primitive. `parent_rev` precondition is a client-side check via `files.get(headRevisionId)`; mismatch raises `DataChangedError`.

**Files:**
- Modify: `src/cumulus/writes.py`
- Modify: `tests/offline/test_writes.py`

- [ ] **Step 4.1: Write the failing tests**

Append to `tests/offline/test_writes.py`:

```python
# ==== remove ==============================================================

def test_remove_trashes_by_default(client, cache):
    cache.set("/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "foo.txt",
        "mimeType": "application/pdf", "trashed": True,
    }
    client._get_service = Mock(return_value=svc)

    result = client._writes.remove("/foo.txt")

    update = svc.files.return_value.update
    update.assert_called_once()
    assert update.call_args.kwargs["fileId"] == "F1"
    assert update.call_args.kwargs["body"] == {"trashed": True}
    # files.delete must NOT have been called.
    svc.files.return_value.delete.assert_not_called()
    # Cache write-through
    assert cache.path_to_id("/foo.txt") is None
    assert result.id == "F1"


def test_remove_missing_path_raises_not_found(client):
    client._get_service = Mock(return_value=fake_service())
    with pytest.raises(NotFoundError):
        client._writes.remove("/nope.txt")


def test_remove_parent_rev_match_succeeds(client, cache):
    cache.set("/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "headRevisionId": "REV-X", "mimeType": "application/pdf",
    }
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "foo.txt",
        "mimeType": "application/pdf", "trashed": True,
    }
    client._get_service = Mock(return_value=svc)

    client._writes.remove("/foo.txt", parent_rev="REV-X")

    svc.files.return_value.update.assert_called_once()


def test_remove_parent_rev_mismatch_raises_data_changed(client, cache):
    cache.set("/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "headRevisionId": "REV-NEW", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    with pytest.raises(DataChangedError):
        client._writes.remove("/foo.txt", parent_rev="REV-OLD")

    svc.files.return_value.update.assert_not_called()


def test_remove_parent_rev_on_folder_raises_bad_input(client, cache):
    cache.set("/foo", "D1")
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "mimeType": "application/vnd.google-apps.folder",
    }
    client._get_service = Mock(return_value=svc)

    with pytest.raises(BadInputError):
        client._writes.remove("/foo", parent_rev="anything")


def test_hard_delete_primitive(client, cache):
    cache.set("/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.delete.return_value.execute.return_value = None
    client._get_service = Mock(return_value=svc)

    client._writes._hard_delete("/foo.txt")

    svc.files.return_value.delete.assert_called_once_with(fileId="F1")
    assert cache.path_to_id("/foo.txt") is None
```

- [ ] **Step 4.2: Run tests to verify they fail**

```bash
pytest tests/offline/test_writes.py -k "remove or hard_delete" -v
```

Expected: FAIL with `NotImplementedError`.

- [ ] **Step 4.3: Implement `remove` and `_hard_delete` in `writes.py`**

Replace the `remove` stub and add `_hard_delete`:

```python
    def remove(
        self, remote_path: str, parent_rev: str | None = None
    ) -> FileMetadata | FolderMetadata:
        client = self._client
        client._ensure_root_id()
        file_id = client._path_cache.path_to_id(remote_path)
        if file_id is None:
            raise NotFoundError(
                "Not found",
                f"No item at {remote_path!r} is known to Cumulus.",
                remote_path=remote_path,
            )

        if parent_rev is not None:
            with convert_api_errors(remote_path=remote_path):
                meta = client._retry_transient(
                    lambda: client._get_service()
                    .files()
                    .get(fileId=file_id,
                         fields="headRevisionId,mimeType")
                    .execute()
                )
            client._count_request()
            if meta.get("mimeType") == _FOLDER_MIME:
                raise BadInputError(
                    "Invalid operation",
                    "parent_rev cannot be specified for a folder.",
                    remote_path=remote_path,
                )
            if meta.get("headRevisionId") != parent_rev:
                raise DataChangedError(
                    "Item changed on Google Drive",
                    f"{remote_path!r} changed on Drive since Cumulus last "
                    "saw it.",
                    remote_path=remote_path,
                )

        with convert_api_errors(remote_path=remote_path):
            resp = client._retry_transient(
                lambda: client._get_service()
                .files()
                .update(
                    fileId=file_id,
                    body={"trashed": True},
                    fields="id,name,mimeType,trashed",
                )
                .execute()
            )
        client._count_request()
        client._path_cache.remove(remote_path)
        return self._wrap_metadata(resp, remote_path)

    def _hard_delete(self, remote_path: str) -> None:
        """Irreversibly delete via ``files.delete``. Internal primitive
        for a future Stage 6 CLI flag — not exposed on the public surface
        in Stage 4."""
        client = self._client
        client._ensure_root_id()
        file_id = client._path_cache.path_to_id(remote_path)
        if file_id is None:
            raise NotFoundError(
                "Not found",
                f"No item at {remote_path!r} is known to Cumulus.",
                remote_path=remote_path,
            )
        with convert_api_errors(remote_path=remote_path):
            client._retry_transient(
                lambda: client._get_service()
                .files()
                .delete(fileId=file_id)
                .execute()
            )
        client._count_request()
        client._path_cache.remove(remote_path)

    def _wrap_metadata(
        self, resp: dict[str, Any], remote_path: str
    ) -> FileMetadata | FolderMetadata:
        """Wrap a Drive ``files.update`` / ``files.create`` response in
        FileMetadata or FolderMetadata. Minimal projection used by remove
        and (later) move; upload uses its own richer projection."""
        from .changes import to_file_metadata, to_folder_metadata
        if resp.get("mimeType") == _FOLDER_MIME:
            # to_folder_metadata expects 'parents' etc. — for trashed
            # responses we may not have them. Use a minimal hand-rolled
            # FolderMetadata.
            return FolderMetadata(
                name=resp.get("name", ""),
                path_lower=fold_path(remote_path),
                path_display=remote_path,
                id=resp["id"],
                shared=False,
            )
        return FileMetadata(
            name=resp.get("name", ""),
            path_lower=fold_path(remote_path),
            path_display=remote_path,
            id=resp["id"],
            client_modified=None,  # not needed for delete-result
            server_modified=None,
            rev="",
            size=0,
            symlink_target=None,
            shared=False,
            modified_by=None,
            is_downloadable=True,
            content_hash="",
        )
```

Add `from .exceptions import DataChangedError, NotFoundError` to the top if not already imported. Update the `_FOLDER_MIME` constant usage in the conditional.

(If `FileMetadata`'s dataclass requires non-None `client_modified` / `server_modified`, use `datetime.fromtimestamp(0, timezone.utc)` instead of `None` — adjust based on `core.py` field types.)

- [ ] **Step 4.4: Run tests to verify they pass**

```bash
pytest tests/offline/test_writes.py -k "remove or hard_delete" -v
```

Expected: all 6 PASS.

- [ ] **Step 4.5: Commit**

```bash
git add src/cumulus/writes.py tests/offline/test_writes.py
git commit -m "feat(stage-4/writes): remove (trash default) + parent_rev precondition + hard_delete primitive"
```

---

## Task 5: move

Implement `_WriteHelper.move`. A single `files.update(fileId, body={name}, addParents, removeParents)` covers move + rename atomically (A5 / spike Q5). Autorename at the destination when collisions exist.

**Files:**
- Modify: `src/cumulus/writes.py`
- Modify: `tests/offline/test_writes.py`

- [ ] **Step 5.1: Write the failing tests**

Append to `tests/offline/test_writes.py`:

```python
# ==== move ================================================================

def test_move_renames_in_place(client, cache):
    cache.set("/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "bar.txt", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.move("/foo.txt", "/bar.txt")

    update = svc.files.return_value.update
    body = update.call_args.kwargs
    assert body["fileId"] == "F1"
    assert body["body"] == {"name": "bar.txt"}
    # Same parent on both sides — no addParents / removeParents needed.
    assert body.get("addParents") in (None, "", "ROOT")
    assert body.get("removeParents") in (None, "", "ROOT")
    # Cache write-through
    assert cache.path_to_id("/foo.txt") is None
    assert cache.path_to_id("/bar.txt") == "F1"


def test_move_changes_parent(client, cache):
    cache.set("/src", "DSRC")
    cache.set("/dst", "DDST")
    cache.set("/src/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "foo.txt", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.move("/src/foo.txt", "/dst/foo.txt")

    body = svc.files.return_value.update.call_args.kwargs
    assert body["body"] == {"name": "foo.txt"}
    assert body["addParents"] == "DDST"
    assert body["removeParents"] == "DSRC"
    assert cache.path_to_id("/src/foo.txt") is None
    assert cache.path_to_id("/dst/foo.txt") == "F1"


def test_move_combined_rename_and_reparent(client, cache):
    cache.set("/src", "DSRC")
    cache.set("/dst", "DDST")
    cache.set("/src/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "bar.txt", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.move("/src/foo.txt", "/dst/bar.txt")

    body = svc.files.return_value.update.call_args.kwargs
    assert body["body"] == {"name": "bar.txt"}
    assert body["addParents"] == "DDST"
    assert body["removeParents"] == "DSRC"


def test_move_missing_source_raises_not_found(client):
    client._get_service = Mock(return_value=fake_service())
    with pytest.raises(NotFoundError):
        client._writes.move("/nope.txt", "/dst.txt")


def test_move_missing_dest_parent_raises_not_found(client, cache):
    cache.set("/foo.txt", "F1")
    client._get_service = Mock(return_value=fake_service())
    with pytest.raises(NotFoundError):
        client._writes.move("/foo.txt", "/no/such/dir/foo.txt")


def test_move_autorename_on_dest_collision(client, cache):
    cache.set("/src", "DSRC")
    cache.set("/dst", "DDST")
    cache.set("/src/foo.txt", "F1")
    cache.set("/dst/foo.txt", "Fexisting")
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "foo (1).txt", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.move("/src/foo.txt", "/dst/foo.txt", autorename=True)

    body = svc.files.return_value.update.call_args.kwargs
    assert body["body"] == {"name": "foo (1).txt"}
    assert cache.path_to_id("/dst/foo (1).txt") == "F1"


def test_move_decodes_new_name_segment(client, cache):
    cache.set("/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "a/b", "mimeType": "application/pdf",
    }
    client._get_service = Mock(return_value=svc)

    client._writes.move("/foo.txt", "/a%2Fb")

    body = svc.files.return_value.update.call_args.kwargs
    assert body["body"]["name"] == "a/b"
```

- [ ] **Step 5.2: Run tests to verify they fail**

```bash
pytest tests/offline/test_writes.py -k "move" -v
```

Expected: FAIL with `NotImplementedError`.

- [ ] **Step 5.3: Implement `move`**

Replace the `move` stub:

```python
    def move(
        self, remote_path: str, new_path: str, *, autorename: bool = False
    ) -> FileMetadata | FolderMetadata:
        client = self._client
        client._ensure_root_id()
        cache = client._path_cache

        file_id = cache.path_to_id(remote_path)
        if file_id is None:
            raise NotFoundError(
                "Not found",
                f"No item at {remote_path!r} is known to Cumulus.",
                remote_path=remote_path,
            )

        old_parent_path, _ = self._split_parent_name(remote_path)
        new_parent_path, raw_new_name = self._split_parent_name(new_path)
        new_name = self._decode_segment(raw_new_name)

        if autorename:
            new_name = self._autorename_unique(new_parent_path, new_name)
            new_path = new_parent_path + "/" + new_name

        old_parent_id = self._resolve_parent_id(
            old_parent_path, for_path=remote_path
        )
        new_parent_id = self._resolve_parent_id(
            new_parent_path, for_path=new_path
        )

        update_kwargs: dict[str, Any] = {
            "fileId": file_id,
            "body": {"name": new_name},
            "fields": "id,name,mimeType",
        }
        if old_parent_id != new_parent_id:
            update_kwargs["addParents"] = new_parent_id
            update_kwargs["removeParents"] = old_parent_id

        with convert_api_errors(remote_path=remote_path):
            resp = client._retry_transient(
                lambda: client._get_service()
                .files()
                .update(**update_kwargs)
                .execute()
            )
        client._count_request()
        cache.move(remote_path, new_path)
        return self._wrap_metadata(resp, new_path)
```

- [ ] **Step 5.4: Run tests to verify they pass**

```bash
pytest tests/offline/test_writes.py -k "move" -v
```

Expected: all 7 PASS. Note: the `test_move_renames_in_place` assertion permits `addParents`/`removeParents` to be absent (the same-parent optimization above omits them); update the test assertion if your implementation always passes them.

- [ ] **Step 5.5: Commit**

```bash
git add src/cumulus/writes.py tests/offline/test_writes.py
git commit -m "feat(stage-4/writes): move (combined move+rename, autorename, cache write-through)"
```

---

## Task 6: upload — simple path (≤5MB)

Implement `_WriteHelper.upload` for the ≤5MB path: WriteMode resolution per spec §4.1.1 table, decode_name on the segment, autorename, non-resumable `MediaFileUpload`, `update_rev` precondition, MD5 verify post-upload, cache write-through for Add/Overwrite-create. Defer the >5MB resumable loop to Task 7 (the simple path is a branch in `upload`; the resumable branch is the other).

**Files:**
- Modify: `src/cumulus/writes.py`
- Modify: `tests/offline/test_writes.py`

- [ ] **Step 6.1: Write the failing tests**

Append to `tests/offline/test_writes.py`:

```python
# ==== upload — simple (<=5MB) =============================================

import hashlib
import os as _os
import tempfile


def _write_tempfile(content: bytes) -> str:
    fd, path = tempfile.mkstemp()
    with _os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def test_upload_add_creates_new_file(client, cache):
    data = b"hello world"
    local = _write_tempfile(data)
    md5 = _md5_hex(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": md5,
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    result = client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Add)

    create = svc.files.return_value.create
    create.assert_called_once()
    body = create.call_args.kwargs["body"]
    assert body == {"name": "hello.txt", "parents": ["ROOT"]}
    assert cache.path_to_id("/hello.txt") == "F-new"
    assert result.id == "F-new"
    _os.unlink(local)


def test_upload_add_existing_without_autorename_raises_file_conflict(client, cache):
    cache.set("/hello.txt", "F-existing")
    local = _write_tempfile(b"x")
    client._get_service = Mock(return_value=fake_service())
    from cumulus.core import WriteMode
    from cumulus.exceptions import FileConflictError

    with pytest.raises(FileConflictError):
        client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Add)
    _os.unlink(local)


def test_upload_add_existing_with_autorename_picks_unique_name(client, cache):
    cache.set("/hello.txt", "F-existing")
    data = b"x"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello (1).txt",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Add, autorename=True)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    assert body["name"] == "hello (1).txt"
    assert cache.path_to_id("/hello (1).txt") == "F-new"
    _os.unlink(local)


def test_upload_update_existing(client, cache):
    cache.set("/hello.txt", "F1")
    data = b"new"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev2",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Update)

    svc.files.return_value.update.assert_called_once()
    assert svc.files.return_value.update.call_args.kwargs["fileId"] == "F1"
    # Path -> ID unchanged; cache should not be re-set (no-op).
    assert cache.path_to_id("/hello.txt") == "F1"
    _os.unlink(local)


def test_upload_update_missing_raises_not_found(client, cache):
    local = _write_tempfile(b"x")
    client._get_service = Mock(return_value=fake_service())
    from cumulus.core import WriteMode

    with pytest.raises(NotFoundError):
        client._writes.upload(local, "/nope.txt", write_mode=WriteMode.Update)
    _os.unlink(local)


def test_upload_overwrite_creates_when_missing(client, cache):
    data = b"x"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Overwrite)

    svc.files.return_value.create.assert_called_once()
    _os.unlink(local)


def test_upload_overwrite_updates_when_existing(client, cache):
    cache.set("/hello.txt", "F1")
    data = b"x"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "F1", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev2",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Overwrite)

    svc.files.return_value.update.assert_called_once()
    _os.unlink(local)


def test_upload_md5_mismatch_raises_data_corruption(client, cache):
    data = b"hello"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello.txt",
        "mimeType": "text/plain",
        "md5Checksum": "0" * 32,  # deliberate mismatch
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode
    from cumulus.exceptions import DataCorruptionError

    with pytest.raises(DataCorruptionError):
        client._writes.upload(local, "/hello.txt", write_mode=WriteMode.Add)
    _os.unlink(local)


def test_upload_missing_md5_warns_and_succeeds(client, cache, caplog):
    data = b"hello"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "hello.txt",
        "mimeType": "text/plain",
        # md5Checksum deliberately omitted
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="cumulus.writes"):
        result = client._writes.upload(
            local, "/hello.txt", write_mode=WriteMode.Add
        )
    assert result.id == "F-new"
    assert any("md5" in r.message.lower() for r in caplog.records)
    _os.unlink(local)


def test_upload_update_rev_mismatch_raises_data_changed(client, cache):
    cache.set("/hello.txt", "F1")
    local = _write_tempfile(b"x")
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "headRevisionId": "rev-new",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    with pytest.raises(DataChangedError):
        client._writes.upload(
            local, "/hello.txt",
            write_mode=WriteMode.Update, update_rev="rev-old",
        )
    # Upload was never attempted.
    svc.files.return_value.update.assert_not_called()
    _os.unlink(local)


def test_upload_decodes_name_segment(client, cache):
    data = b"x"
    local = _write_tempfile(data)
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "F-new", "name": "a/b",
        "mimeType": "text/plain",
        "md5Checksum": _md5_hex(data),
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(len(data)),
        "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    client._writes.upload(local, "/a%2Fb", write_mode=WriteMode.Add)

    body = svc.files.return_value.create.call_args.kwargs["body"]
    assert body["name"] == "a/b"
    _os.unlink(local)
```

- [ ] **Step 6.2: Run tests to verify they fail**

```bash
pytest tests/offline/test_writes.py -k "upload" -v
```

Expected: FAIL with `NotImplementedError`.

- [ ] **Step 6.3: Implement the simple `upload` path**

Add to the top of `writes.py`:

```python
from googleapiclient.http import MediaFileUpload
from .exceptions import DataCorruptionError, FileConflictError
```

Add constant:

```python
_SIMPLE_UPLOAD_THRESHOLD = 5 * 1024 * 1024  # 5 MB
_RESUMABLE_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
```

Replace `upload`:

```python
    def upload(
        self,
        local_path: str,
        remote_path: str,
        *,
        write_mode: WriteMode = WriteMode.Add,
        update_rev: str | None = None,
        autorename: bool = False,
        sync_event: SyncEvent | None = None,
    ) -> FileMetadata:
        client = self._client
        client._ensure_root_id()

        parent_path, raw_name = self._split_parent_name(remote_path)
        drive_name = self._decode_segment(raw_name)

        cache = client._path_cache
        existing_id = cache.path_to_id(remote_path)

        # WriteMode resolution per spec §4.1.1.
        if write_mode is WriteMode.Add:
            if existing_id is not None and not autorename:
                raise FileConflictError(
                    "File already exists",
                    f"An item already exists at {remote_path!r}.",
                    remote_path=remote_path,
                    local_path=local_path,
                )
            if existing_id is not None and autorename:
                drive_name = self._autorename_unique(parent_path, drive_name)
                remote_path = parent_path + "/" + drive_name
            mode = "create"
            target_id: str | None = None
        elif write_mode is WriteMode.Update:
            if existing_id is None:
                raise NotFoundError(
                    "Not found",
                    f"No item at {remote_path!r} to update.",
                    remote_path=remote_path,
                )
            mode = "update"
            target_id = existing_id
        elif write_mode is WriteMode.Overwrite:
            if existing_id is None:
                mode = "create"
                target_id = None
            else:
                mode = "update"
                target_id = existing_id
        else:
            raise BadInputError(
                "Invalid WriteMode",
                f"Unknown write_mode {write_mode!r}.",
                remote_path=remote_path,
                local_path=local_path,
            )

        # update_rev precondition (Update mode only).
        if mode == "update" and update_rev is not None:
            with convert_api_errors(remote_path=remote_path, local_path=local_path):
                meta = client._retry_transient(
                    lambda: client._get_service()
                    .files()
                    .get(fileId=target_id, fields="headRevisionId")
                    .execute()
                )
            client._count_request()
            if meta.get("headRevisionId") != update_rev:
                raise DataChangedError(
                    "Item changed on Google Drive",
                    f"{remote_path!r} changed on Drive since Cumulus last saw it.",
                    remote_path=remote_path,
                    local_path=local_path,
                )

        # Resolve parent for create.
        if mode == "create":
            parent_id = self._resolve_parent_id(
                parent_path, for_path=remote_path
            )
        else:
            parent_id = None  # update has no parent change in upload

        # Compute local MD5 while reading.
        local_md5 = self._md5_file(local_path)

        size = _os.path.getsize(local_path)
        media = MediaFileUpload(
            local_path,
            resumable=(size > _SIMPLE_UPLOAD_THRESHOLD),
            chunksize=_RESUMABLE_CHUNK_SIZE,
        )

        fields = ("id,name,mimeType,md5Checksum,modifiedTime,size,"
                  "headRevisionId")

        if size > _SIMPLE_UPLOAD_THRESHOLD:
            # Resumable path — Task 7.
            resp = self._upload_resumable(
                mode=mode, target_id=target_id, parent_id=parent_id,
                drive_name=drive_name, media=media, fields=fields,
                remote_path=remote_path, local_path=local_path,
                sync_event=sync_event,
            )
        else:
            # Simple path.
            with convert_api_errors(remote_path=remote_path, local_path=local_path):
                if mode == "create":
                    body = {"name": drive_name, "parents": [parent_id]}
                    request = client._get_service().files().create(
                        body=body, media_body=media, fields=fields,
                    )
                else:
                    request = client._get_service().files().update(
                        fileId=target_id, media_body=media, fields=fields,
                    )
                resp = client._retry_transient(request.execute)
            client._count_request()

        # MD5 verify.
        drive_md5 = resp.get("md5Checksum")
        if drive_md5 is None:
            logger.warning(
                "Drive omitted md5Checksum for %s; skipping verify.",
                remote_path,
            )
        elif drive_md5 != local_md5:
            raise DataCorruptionError(
                "Upload checksum mismatch",
                f"Drive returned md5={drive_md5!r} but local md5={local_md5!r} "
                f"for {remote_path!r}.",
                remote_path=remote_path,
                local_path=local_path,
            )

        if mode == "create":
            cache.set(remote_path, resp["id"])

        return self._project_file_metadata(resp, remote_path)

    @staticmethod
    def _md5_file(local_path: str) -> str:
        import hashlib as _h
        h = _h.md5()
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _project_file_metadata(
        self, resp: dict[str, Any], remote_path: str
    ) -> FileMetadata:
        from .changes import parse_drive_time
        return FileMetadata(
            name=resp.get("name", ""),
            path_lower=fold_path(remote_path),
            path_display=remote_path,
            id=resp["id"],
            client_modified=parse_drive_time(resp.get("modifiedTime")),
            server_modified=parse_drive_time(resp.get("modifiedTime")),
            rev=resp.get("headRevisionId", ""),
            size=int(resp.get("size", 0) or 0),
            symlink_target=None,
            shared=False,
            modified_by=None,
            is_downloadable=True,
            content_hash=resp.get("md5Checksum", ""),
        )

    def _upload_resumable(self, **kwargs):
        # Implemented in Task 7.
        raise NotImplementedError
```

(Verify `FileMetadata`'s constructor signature in `core.py` — adjust field names/order if needed.)

- [ ] **Step 6.4: Run tests to verify they pass**

```bash
pytest tests/offline/test_writes.py -k "upload" -v
```

Expected: all 11 simple-upload tests PASS.

- [ ] **Step 6.5: Commit**

```bash
git add src/cumulus/writes.py tests/offline/test_writes.py
git commit -m "feat(stage-4/writes): upload simple path (<=5MB) + WriteMode table + MD5 verify"
```

---

## Task 7: upload — resumable path (>5MB)

Implement `_upload_resumable` — the chunked upload loop with progress, throttle, `_poll_event` shutdown-interrupt, and `storageQuotaExceeded` mid-stream handling. The control flow mirrors Stage 3's `download` chunk loop in `client.py`.

**Files:**
- Modify: `src/cumulus/writes.py`
- Modify: `tests/offline/test_writes.py`

- [ ] **Step 7.1: Write the failing tests**

Append to `tests/offline/test_writes.py`:

```python
# ==== upload — resumable (>5MB) ===========================================

def _big_tempfile(size_bytes: int) -> str:
    """Sparse-ish big file for size-threshold tests. Content is the byte 'a'."""
    fd, path = tempfile.mkstemp()
    with _os.fdopen(fd, "wb") as f:
        f.write(b"a" * size_bytes)
    return path


def test_upload_simple_at_threshold(client, cache):
    # 5 MB exactly -> simple path (threshold is >, not >=).
    size = 5 * 1024 * 1024
    local = _big_tempfile(size)
    md5 = hashlib.md5(b"a" * size).hexdigest()
    svc = fake_service()
    svc.files.return_value.create.return_value.execute.return_value = {
        "id": "Fa", "name": "big.bin",
        "mimeType": "application/octet-stream",
        "md5Checksum": md5,
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(size), "headRevisionId": "rev1",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.core import WriteMode

    # Patch MediaFileUpload to capture the resumable flag.
    with patch("cumulus.writes.MediaFileUpload") as mfu:
        mfu.return_value = Mock()
        client._writes.upload(local, "/big.bin", write_mode=WriteMode.Add)
    assert mfu.call_args.kwargs.get("resumable") is False
    _os.unlink(local)


def test_upload_resumable_above_threshold(client, cache):
    # 5 MB + 1 byte -> resumable path.
    size = 5 * 1024 * 1024 + 1
    local = _big_tempfile(size)
    md5 = hashlib.md5(b"a" * size).hexdigest()

    # Fake out the resumable next_chunk loop:
    # next_chunk returns (status, None) twice then (status, response).
    progress_status = Mock()
    progress_status.resumable_progress = 0
    progress_status.total_size = size

    final_resp = {
        "id": "Fa", "name": "big.bin",
        "mimeType": "application/octet-stream",
        "md5Checksum": md5,
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(size), "headRevisionId": "rev1",
    }

    request = Mock()
    request.next_chunk = Mock(side_effect=[
        (progress_status, None),
        (progress_status, None),
        (progress_status, final_resp),
    ])

    svc = fake_service()
    svc.files.return_value.create.return_value = request
    client._get_service = Mock(return_value=svc)

    from cumulus.core import WriteMode
    with patch("cumulus.writes.MediaFileUpload") as mfu:
        mfu.return_value = Mock()
        client._writes.upload(local, "/big.bin", write_mode=WriteMode.Add)

    assert mfu.call_args.kwargs["resumable"] is True
    assert mfu.call_args.kwargs["chunksize"] == 8 * 1024 * 1024
    assert request.next_chunk.call_count == 3
    assert cache.path_to_id("/big.bin") == "Fa"
    _os.unlink(local)


def test_upload_resumable_storage_quota_exceeded(client, cache, make_http_error):
    size = 5 * 1024 * 1024 + 1
    local = _big_tempfile(size)

    request = Mock()
    request.next_chunk = Mock(side_effect=make_http_error(
        403, reason="storageQuotaExceeded",
    ))
    svc = fake_service()
    svc.files.return_value.create.return_value = request
    client._get_service = Mock(return_value=svc)
    # Disable retry sleep.
    client._retry_transient = lambda fn: fn()

    from cumulus.core import WriteMode
    from cumulus.exceptions import InsufficientSpaceError

    with patch("cumulus.writes.MediaFileUpload") as mfu:
        mfu.return_value = Mock()
        with pytest.raises(InsufficientSpaceError):
            client._writes.upload(local, "/big.bin", write_mode=WriteMode.Add)

    # Cache must NOT have been mutated.
    assert cache.path_to_id("/big.bin") is None
    _os.unlink(local)


def test_upload_resumable_progress_reported(client, cache):
    size = 5 * 1024 * 1024 + 1
    local = _big_tempfile(size)
    md5 = hashlib.md5(b"a" * size).hexdigest()
    final_resp = {
        "id": "Fa", "name": "big.bin",
        "mimeType": "application/octet-stream",
        "md5Checksum": md5,
        "modifiedTime": "2026-05-30T12:00:00.000Z",
        "size": str(size), "headRevisionId": "rev1",
    }
    s1 = Mock(); s1.resumable_progress = 4 * 1024 * 1024
    s2 = Mock(); s2.resumable_progress = size
    request = Mock()
    request.next_chunk = Mock(side_effect=[
        (s1, None), (s2, final_resp),
    ])
    svc = fake_service()
    svc.files.return_value.create.return_value = request
    client._get_service = Mock(return_value=svc)

    sync_event = Mock()
    from cumulus.core import WriteMode
    with patch("cumulus.writes.MediaFileUpload") as mfu:
        mfu.return_value = Mock()
        client._writes.upload(
            local, "/big.bin",
            write_mode=WriteMode.Add, sync_event=sync_event,
        )

    # Progress reported per chunk that produced a status.
    assert sync_event.update_progress.call_count >= 2
    _os.unlink(local)


def test_upload_resumable_shutdown_aborts(client, cache):
    size = 5 * 1024 * 1024 + 1
    local = _big_tempfile(size)
    s = Mock(); s.resumable_progress = 1 * 1024 * 1024
    request = Mock()
    # After first chunk, shutdown event fires; loop should bail.
    request.next_chunk = Mock(return_value=(s, None))
    svc = fake_service()
    svc.files.return_value.create.return_value = request
    client._get_service = Mock(return_value=svc)
    client._poll_event = Mock()
    # First .is_set() check: False (proceed). Second: True (abort).
    client._poll_event.is_set = Mock(side_effect=[False, True])

    from cumulus.core import WriteMode
    from cumulus.exceptions import CancelledError

    with patch("cumulus.writes.MediaFileUpload") as mfu:
        mfu.return_value = Mock()
        with pytest.raises(CancelledError):
            client._writes.upload(local, "/big.bin", write_mode=WriteMode.Add)
    _os.unlink(local)
```

(Note: `SyncEvent.update_progress` — verify the actual method name in `core.py`; adjust assertion. If the project uses a different progress API, update the test and implementation in lockstep.)

- [ ] **Step 7.2: Run tests to verify they fail**

```bash
pytest tests/offline/test_writes.py -k "resumable or simple_at_threshold" -v
```

Expected: FAIL (`_upload_resumable` is a `NotImplementedError` stub from Task 6, or threshold check is off).

- [ ] **Step 7.3: Implement `_upload_resumable`**

Replace the `_upload_resumable` stub:

```python
    def _upload_resumable(
        self, *, mode: str, target_id: str | None, parent_id: str | None,
        drive_name: str, media: Any, fields: str,
        remote_path: str, local_path: str,
        sync_event: SyncEvent | None,
    ) -> dict[str, Any]:
        from .exceptions import CancelledError
        client = self._client

        with convert_api_errors(remote_path=remote_path, local_path=local_path):
            if mode == "create":
                body = {"name": drive_name, "parents": [parent_id]}
                request = (
                    client._get_service().files().create(
                        body=body, media_body=media, fields=fields,
                    )
                )
            else:
                request = (
                    client._get_service().files().update(
                        fileId=target_id, media_body=media, fields=fields,
                    )
                )

            response: dict[str, Any] | None = None
            while response is None:
                if client._poll_event.is_set():
                    raise CancelledError(
                        "Upload cancelled",
                        f"Upload of {local_path!r} was interrupted by shutdown.",
                    )
                status, response = client._retry_transient(request.next_chunk)
                if sync_event is not None and status is not None:
                    sync_event.update_progress(
                        status.resumable_progress
                    )
        client._count_request()
        return response
```

(If `SyncEvent` exposes a different progress API — e.g. assigning `sync_event.completed = status.resumable_progress` — adjust both the test and this call site to the real API. Read `src/cumulus/core.py` for the `SyncEvent` shape; Stage 3's `download` is the reference.)

- [ ] **Step 7.4: Run tests to verify they pass**

```bash
pytest tests/offline/test_writes.py -k "resumable or simple_at_threshold" -v
```

Expected: all 5 PASS.

- [ ] **Step 7.5: Commit**

```bash
git add src/cumulus/writes.py tests/offline/test_writes.py
git commit -m "feat(stage-4/writes): resumable upload (>5MB) with progress, throttle, shutdown, quota mapping"
```

---

## Task 8: make_dir_batch

Drive's batch HTTP endpoint groups up to 100 sub-requests per round-trip. Implement the chunking + per-sub-request callback pattern; reuse it for `remove_batch` in Task 9.

**Files:**
- Modify: `src/cumulus/writes.py`
- Modify: `tests/offline/test_writes.py`

- [ ] **Step 8.1: Write the failing tests**

Append to `tests/offline/test_writes.py`:

```python
# ==== make_dir_batch ======================================================

def _batch_request_double(responses_per_id: dict[str, Any]):
    """Build a fake BatchHttpRequest that, on execute(), invokes each
    registered callback with (request_id, response, exception)."""
    class _Batch:
        def __init__(self):
            self._calls = []

        def add(self, request, callback, request_id=None):
            self._calls.append((request_id, callback))

        def execute(self):
            for rid, cb in self._calls:
                resp_or_exc = responses_per_id.get(rid)
                if isinstance(resp_or_exc, Exception):
                    cb(rid, None, resp_or_exc)
                else:
                    cb(rid, resp_or_exc, None)

    return _Batch()


def test_make_dir_batch_creates_each_folder(client, cache):
    svc = fake_service()
    responses = {
        "0": {"id": "DA", "name": "a", "mimeType": _FOLDER_MIME},
        "1": {"id": "DB", "name": "b", "mimeType": _FOLDER_MIME},
        "2": {"id": "DC", "name": "c", "mimeType": _FOLDER_MIME},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)

    result = client._writes.make_dir_batch(["/a", "/b", "/c"])

    assert [r.id for r in result] == ["DA", "DB", "DC"]
    assert cache.path_to_id("/a") == "DA"
    assert cache.path_to_id("/b") == "DB"
    assert cache.path_to_id("/c") == "DC"


def test_make_dir_batch_chunks_at_100(client, cache):
    svc = fake_service()
    batches = []

    def new_batch(callback=None):
        responses = {str(i): {"id": f"D{i}", "name": str(i),
                              "mimeType": _FOLDER_MIME}
                     for i in range(101)}
        b = _batch_request_double(responses)
        batches.append(b)
        return b

    svc.new_batch_http_request = Mock(side_effect=new_batch)
    client._get_service = Mock(return_value=svc)

    paths = [f"/d{i}" for i in range(101)]
    result = client._writes.make_dir_batch(paths)

    # Two batch round-trips: sizes 100 + 1.
    assert len(batches) == 2
    assert len(batches[0]._calls) == 100
    assert len(batches[1]._calls) == 1
    assert len(result) == 101


def test_make_dir_batch_handles_mixed_success_and_error(
    client, cache, make_http_error,
):
    svc = fake_service()
    err = make_http_error(404, reason="notFound")
    responses = {
        "0": {"id": "DA", "name": "a", "mimeType": _FOLDER_MIME},
        "1": err,
        "2": {"id": "DC", "name": "c", "mimeType": _FOLDER_MIME},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)

    result = client._writes.make_dir_batch(["/a", "/b", "/c"])

    assert result[0].id == "DA"
    assert isinstance(result[1], CumulusApiError)
    assert result[2].id == "DC"
    # Only successful entries write through to the cache.
    assert cache.path_to_id("/a") == "DA"
    assert cache.path_to_id("/b") is None
    assert cache.path_to_id("/c") == "DC"


def test_make_dir_batch_counts_subrequests(client, cache):
    svc = fake_service()
    responses = {
        "0": {"id": "DA", "name": "a", "mimeType": _FOLDER_MIME},
        "1": {"id": "DB", "name": "b", "mimeType": _FOLDER_MIME},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)
    client._count_request = Mock()

    client._writes.make_dir_batch(["/a", "/b"])

    # 2 sub-requests in 1 batch -> _count_request(2).
    client._count_request.assert_called_once_with(2)
```

(Add `from cumulus.writes import _FOLDER_MIME` to the test file imports, or hard-code the string.)

- [ ] **Step 8.2: Run tests to verify they fail**

```bash
pytest tests/offline/test_writes.py -k "make_dir_batch" -v
```

Expected: FAIL with `NotImplementedError`.

- [ ] **Step 8.3: Implement `make_dir_batch`**

Add at the top of `writes.py`:

```python
from .errorhandling import http_error_to_cumulus, extract_http_reason
from googleapiclient.errors import HttpError
```

Replace `make_dir_batch`:

```python
    _BATCH_LIMIT = 100

    def make_dir_batch(
        self, remote_paths: list[str]
    ) -> list[FolderMetadata | CumulusApiError]:
        client = self._client
        client._ensure_root_id()
        results: list[FolderMetadata | CumulusApiError | None] = [None] * len(
            remote_paths
        )

        for chunk_start in range(0, len(remote_paths), self._BATCH_LIMIT):
            chunk = list(enumerate(
                remote_paths[chunk_start:chunk_start + self._BATCH_LIMIT],
                start=chunk_start,
            ))
            batch = client._get_service().new_batch_http_request()
            for idx, remote_path in chunk:
                try:
                    parent_path, raw_name = self._split_parent_name(
                        remote_path
                    )
                    drive_name = self._decode_segment(raw_name)
                    parent_id = self._resolve_parent_id(
                        parent_path, for_path=remote_path
                    )
                except CumulusApiError as e:
                    results[idx] = e
                    continue
                body = {
                    "name": drive_name,
                    "mimeType": _FOLDER_MIME,
                    "parents": [parent_id],
                }
                request = (
                    client._get_service().files().create(
                        body=body, fields="id,name,mimeType",
                    )
                )
                batch.add(
                    request,
                    callback=self._make_batch_callback(
                        results, idx, remote_path,
                        on_success=lambda resp, p=remote_path: (
                            client._path_cache.set(p, resp["id"]),
                            FolderMetadata(
                                name=resp.get("name", ""),
                                path_lower=fold_path(p),
                                path_display=p,
                                id=resp["id"],
                                shared=False,
                            ),
                        )[1],
                    ),
                    request_id=str(idx),
                )
            with convert_api_errors():
                client._retry_transient(batch.execute)
            client._count_request(len(chunk))
        # Backfill any None slots (shouldn't happen, but defensive)
        return [r if r is not None else CumulusApiError("batch slot empty", "")
                for r in results]

    @staticmethod
    def _make_batch_callback(results, idx, remote_path, *, on_success):
        def cb(request_id, response, exception):
            if exception is not None:
                if isinstance(exception, HttpError):
                    results[idx] = http_error_to_cumulus(
                        exception.resp.status,
                        extract_http_reason(exception),
                        exception,
                        remote_path=remote_path,
                    )
                else:
                    results[idx] = CumulusApiError(
                        "Batch sub-request failed",
                        str(exception),
                        remote_path=remote_path,
                    )
                return
            results[idx] = on_success(response)
        return cb
```

(Implementation note: the `on_success` lambda above uses tuple-trickery to do both a cache write-through *and* return the metadata. If you find that awkward, replace with a small named local function:

```python
def _on_success(resp):
    client._path_cache.set(remote_path, resp["id"])
    return FolderMetadata(name=resp.get("name", ""),
                          path_lower=fold_path(remote_path),
                          path_display=remote_path,
                          id=resp["id"], shared=False)
```

The tests above assert the resulting behavior, not the lambda shape.)

- [ ] **Step 8.4: Run tests to verify they pass**

```bash
pytest tests/offline/test_writes.py -k "make_dir_batch" -v
```

Expected: all 4 PASS.

- [ ] **Step 8.5: Commit**

```bash
git add src/cumulus/writes.py tests/offline/test_writes.py
git commit -m "feat(stage-4/writes): make_dir_batch (chunking, callbacks, mixed results, quota)"
```

---

## Task 9: remove_batch

Same batch pattern as Task 8, but each sub-request is `files.update(fileId, body={trashed:True})`. Optional per-entry `parent_rev` precondition costs an extra `files.get` per checked entry.

**Files:**
- Modify: `src/cumulus/writes.py`
- Modify: `tests/offline/test_writes.py`

- [ ] **Step 9.1: Write the failing tests**

Append to `tests/offline/test_writes.py`:

```python
# ==== remove_batch ========================================================

def test_remove_batch_trashes_each_entry(client, cache):
    cache.set("/a.txt", "FA")
    cache.set("/b.txt", "FB")
    svc = fake_service()
    responses = {
        "0": {"id": "FA", "name": "a.txt", "mimeType": "text/plain",
              "trashed": True},
        "1": {"id": "FB", "name": "b.txt", "mimeType": "text/plain",
              "trashed": True},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)

    # Capture the update bodies registered with the batch.
    update_kwargs_seen = []
    original_update = svc.files.return_value.update
    def capture(**kw):
        update_kwargs_seen.append(kw)
        return Mock()
    original_update.side_effect = capture

    result = client._writes.remove_batch([("/a.txt", None), ("/b.txt", None)])

    assert all(kw["body"] == {"trashed": True} for kw in update_kwargs_seen)
    assert {kw["fileId"] for kw in update_kwargs_seen} == {"FA", "FB"}
    assert cache.path_to_id("/a.txt") is None
    assert cache.path_to_id("/b.txt") is None
    assert len(result) == 2


def test_remove_batch_missing_entry_returns_error(client, cache):
    cache.set("/a.txt", "FA")
    svc = fake_service()
    responses = {
        "0": {"id": "FA", "name": "a.txt", "mimeType": "text/plain",
              "trashed": True},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)

    result = client._writes.remove_batch([("/a.txt", None), ("/nope.txt", None)])

    assert result[0].id == "FA"
    assert isinstance(result[1], NotFoundError)


def test_remove_batch_with_rev_runs_precondition(client, cache):
    cache.set("/a.txt", "FA")
    svc = fake_service()
    # Pre-check returns matching rev.
    svc.files.return_value.get.return_value.execute.return_value = {
        "headRevisionId": "rev-good",
        "mimeType": "text/plain",
    }
    responses = {
        "0": {"id": "FA", "name": "a.txt", "mimeType": "text/plain",
              "trashed": True},
    }
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double(responses)
    )
    client._get_service = Mock(return_value=svc)

    client._writes.remove_batch([("/a.txt", "rev-good")])

    svc.files.return_value.get.assert_called_once()


def test_remove_batch_rev_mismatch_returns_data_changed(client, cache):
    cache.set("/a.txt", "FA")
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "headRevisionId": "rev-new", "mimeType": "text/plain",
    }
    # No batch response needed — precheck fails before batch executes for
    # that index.
    svc.new_batch_http_request = Mock(
        return_value=_batch_request_double({})
    )
    client._get_service = Mock(return_value=svc)

    result = client._writes.remove_batch([("/a.txt", "rev-old")])

    assert isinstance(result[0], DataChangedError)
    # Cache untouched.
    assert cache.path_to_id("/a.txt") == "FA"
```

- [ ] **Step 9.2: Run tests to verify they fail**

```bash
pytest tests/offline/test_writes.py -k "remove_batch" -v
```

Expected: FAIL with `NotImplementedError`.

- [ ] **Step 9.3: Implement `remove_batch`**

Replace the stub:

```python
    def remove_batch(
        self,
        entries: Sequence[tuple[str, str | None]],
        batch_size: int = 100,
    ) -> list[FileMetadata | FolderMetadata | CumulusApiError]:
        client = self._client
        client._ensure_root_id()
        cache = client._path_cache
        results: list[FileMetadata | FolderMetadata | CumulusApiError | None] = [
            None
        ] * len(entries)

        # Resolve file IDs + precondition checks before chunking the batch.
        # If a precheck fails (NotFound or DataChanged), that slot gets the
        # error and is excluded from the batch.
        resolved: list[tuple[int, str, str] | None] = [None] * len(entries)
        for idx, (remote_path, parent_rev) in enumerate(entries):
            file_id = cache.path_to_id(remote_path)
            if file_id is None:
                results[idx] = NotFoundError(
                    "Not found",
                    f"No item at {remote_path!r} is known to Cumulus.",
                    remote_path=remote_path,
                )
                continue
            if parent_rev is not None:
                with convert_api_errors(remote_path=remote_path):
                    meta = client._retry_transient(
                        lambda fid=file_id: client._get_service()
                        .files()
                        .get(fileId=fid, fields="headRevisionId,mimeType")
                        .execute()
                    )
                client._count_request()
                if meta.get("mimeType") == _FOLDER_MIME:
                    results[idx] = BadInputError(
                        "Invalid operation",
                        "parent_rev cannot be specified for a folder.",
                        remote_path=remote_path,
                    )
                    continue
                if meta.get("headRevisionId") != parent_rev:
                    results[idx] = DataChangedError(
                        "Item changed on Google Drive",
                        f"{remote_path!r} changed on Drive since Cumulus last saw it.",
                        remote_path=remote_path,
                    )
                    continue
            resolved[idx] = (idx, remote_path, file_id)

        live = [r for r in resolved if r is not None]
        for chunk_start in range(0, len(live), batch_size):
            chunk = live[chunk_start:chunk_start + batch_size]
            batch = client._get_service().new_batch_http_request()
            for idx, remote_path, file_id in chunk:
                request = (
                    client._get_service().files().update(
                        fileId=file_id,
                        body={"trashed": True},
                        fields="id,name,mimeType,trashed",
                    )
                )
                batch.add(
                    request,
                    callback=self._make_batch_callback(
                        results, idx, remote_path,
                        on_success=lambda resp, p=remote_path: (
                            cache.remove(p),
                            self._wrap_metadata(resp, p),
                        )[1],
                    ),
                    request_id=str(idx),
                )
            with convert_api_errors():
                client._retry_transient(batch.execute)
            client._count_request(len(chunk))

        return [r if r is not None else CumulusApiError("batch slot empty", "")
                for r in results]
```

- [ ] **Step 9.4: Run tests to verify they pass**

```bash
pytest tests/offline/test_writes.py -k "remove_batch" -v
```

Expected: all 4 PASS.

- [ ] **Step 9.5: Commit**

```bash
git add src/cumulus/writes.py tests/offline/test_writes.py
git commit -m "feat(stage-4/writes): remove_batch (trash default, per-entry rev precondition, chunking)"
```

---

## Task 10: restore

Native-doc check first → download the named revision via `revisions().get(alt=media)` to a temp file → re-upload through `self.upload(WriteMode.Update)` → unlink temp file in `finally`.

**Files:**
- Modify: `src/cumulus/writes.py`
- Modify: `tests/offline/test_writes.py`

- [ ] **Step 10.1: Write the failing tests**

Append to `tests/offline/test_writes.py`:

```python
# ==== restore =============================================================

def test_restore_missing_path_raises_not_found(client):
    client._get_service = Mock(return_value=fake_service())
    with pytest.raises(NotFoundError):
        client._writes.restore("/nope.txt", "rev1")


def test_restore_native_doc_raises_unsupported(client, cache):
    cache.set("/notes.gdoc", "FN")
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "mimeType": "application/vnd.google-apps.document",
    }
    client._get_service = Mock(return_value=svc)
    from cumulus.exceptions import UnsupportedFileError

    with pytest.raises(UnsupportedFileError):
        client._writes.restore("/notes.gdoc", "rev1")
    # No download attempted.
    svc.revisions.return_value.get.assert_not_called()


def test_restore_downloads_revision_and_reuploads(client, cache, monkeypatch):
    cache.set("/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "mimeType": "text/plain",
    }
    # The revisions().get(alt=media) call is downloaded via MediaIoBaseDownload.
    # Patch it to write fake bytes to the temp file.
    fake_get_media = Mock()
    svc.revisions.return_value.get_media = Mock(return_value=fake_get_media)
    client._get_service = Mock(return_value=svc)

    captured_uploads = []

    def fake_upload(local_path, remote_path, *, write_mode, **kwargs):
        with open(local_path, "rb") as f:
            captured_uploads.append((remote_path, write_mode, f.read()))
        return FileMetadata(
            name="foo.txt",
            path_lower=remote_path.lower(),
            path_display=remote_path,
            id="F1",
            client_modified=None, server_modified=None, rev="rev-new",
            size=3, symlink_target=None, shared=False,
            modified_by=None, is_downloadable=True,
            content_hash="abc",
        )

    monkeypatch.setattr(client._writes, "upload", fake_upload)

    # Patch the helper's revision-to-tempfile downloader to write a known payload.
    monkeypatch.setattr(
        client._writes, "_download_revision_to_tempfile",
        lambda file_id, rev: (lambda p: (open(p, "wb").write(b"OLD"), p)[1])(
            tempfile.mkstemp()[1]
        ),
    )

    result = client._writes.restore("/foo.txt", "rev-old")

    assert len(captured_uploads) == 1
    assert captured_uploads[0][0] == "/foo.txt"
    from cumulus.core import WriteMode
    assert captured_uploads[0][1] is WriteMode.Update
    assert captured_uploads[0][2] == b"OLD"
    assert result.id == "F1"


def test_restore_cleans_up_tempfile_on_success(client, cache, monkeypatch):
    cache.set("/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "mimeType": "text/plain",
    }
    client._get_service = Mock(return_value=svc)

    tmp_paths: list[str] = []

    def fake_download(file_id, rev):
        fd, p = tempfile.mkstemp()
        _os.close(fd)
        tmp_paths.append(p)
        return p

    monkeypatch.setattr(
        client._writes, "_download_revision_to_tempfile", fake_download
    )
    monkeypatch.setattr(
        client._writes, "upload",
        lambda *a, **kw: FileMetadata(
            name="foo.txt", path_lower="/foo.txt", path_display="/foo.txt",
            id="F1", client_modified=None, server_modified=None, rev="x",
            size=0, symlink_target=None, shared=False, modified_by=None,
            is_downloadable=True, content_hash="",
        ),
    )

    client._writes.restore("/foo.txt", "rev-old")

    assert tmp_paths
    for p in tmp_paths:
        assert not _os.path.exists(p), f"tempfile {p} was not cleaned up"


def test_restore_cleans_up_tempfile_on_upload_exception(client, cache, monkeypatch):
    cache.set("/foo.txt", "F1")
    svc = fake_service()
    svc.files.return_value.get.return_value.execute.return_value = {
        "mimeType": "text/plain",
    }
    client._get_service = Mock(return_value=svc)

    tmp_paths: list[str] = []

    def fake_download(file_id, rev):
        fd, p = tempfile.mkstemp()
        _os.close(fd)
        tmp_paths.append(p)
        return p

    def boom(*a, **kw):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(
        client._writes, "_download_revision_to_tempfile", fake_download
    )
    monkeypatch.setattr(client._writes, "upload", boom)

    with pytest.raises(RuntimeError):
        client._writes.restore("/foo.txt", "rev-old")

    assert tmp_paths
    for p in tmp_paths:
        assert not _os.path.exists(p)
```

- [ ] **Step 10.2: Run tests to verify they fail**

```bash
pytest tests/offline/test_writes.py -k "restore" -v
```

Expected: FAIL with `NotImplementedError`.

- [ ] **Step 10.3: Implement `restore`**

Add to the top of `writes.py`:

```python
from googleapiclient.http import MediaIoBaseDownload
from .exceptions import UnsupportedFileError
from .native_docs import is_google_native
```

Replace the `restore` stub:

```python
    def restore(self, remote_path: str, rev: str) -> FileMetadata:
        client = self._client
        client._ensure_root_id()
        file_id = client._path_cache.path_to_id(remote_path)
        if file_id is None:
            raise NotFoundError(
                "Not found",
                f"No item at {remote_path!r} is known to Cumulus.",
                remote_path=remote_path,
            )

        with convert_api_errors(remote_path=remote_path):
            meta = client._retry_transient(
                lambda: client._get_service()
                .files()
                .get(fileId=file_id, fields="mimeType")
                .execute()
            )
        client._count_request()
        if is_google_native(meta.get("mimeType", "")):
            raise UnsupportedFileError(
                "Cannot restore native Google Doc",
                "restore() is not supported for Google Docs / Sheets / "
                "Slides — their revisions cannot be round-tripped as bytes.",
                remote_path=remote_path,
            )

        tmp_path = self._download_revision_to_tempfile(file_id, rev)
        try:
            return self.upload(
                tmp_path, remote_path, write_mode=WriteMode.Update,
            )
        finally:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass

    def _download_revision_to_tempfile(self, file_id: str, rev: str) -> str:
        import tempfile as _t
        client = self._client
        fd, path = _t.mkstemp(prefix="cumulus-restore-")
        _os.close(fd)
        with convert_api_errors():
            request = client._get_service().revisions().get_media(
                fileId=file_id, revisionId=rev,
            )
            with open(path, "wb") as fh:
                downloader = MediaIoBaseDownload(
                    fh, request, chunksize=_RESUMABLE_CHUNK_SIZE,
                )
                done = False
                while not done:
                    if client._poll_event.is_set():
                        raise CancelledError(
                            "Restore cancelled",
                            f"Restore of {file_id!r}@{rev!r} interrupted "
                            "by shutdown.",
                        )
                    _status, done = client._retry_transient(
                        downloader.next_chunk
                    )
        client._count_request()
        return path
```

(Import `CancelledError` from `.exceptions` at top of file if not already.)

- [ ] **Step 10.4: Run tests to verify they pass**

```bash
pytest tests/offline/test_writes.py -k "restore" -v
```

Expected: all 5 PASS.

- [ ] **Step 10.5: Run the full writes test file**

```bash
pytest tests/offline/test_writes.py -v
```

Expected: every Task 3–10 test passes (no skips, no xfails).

- [ ] **Step 10.6: Commit**

```bash
git add src/cumulus/writes.py tests/offline/test_writes.py
git commit -m "feat(stage-4/writes): restore via revision-download-to-tempfile + re-upload"
```

---

## Task 11: Full suite + acceptance + smoke-test deviations file

Verify the full offline suite still passes vs the documented Stage 3 baseline, then write the smoke-test checklist for the user to run against a real Drive account (`cumulus@arko.net`, per the project memory). The deviations file follows the same shape as `docs/superpowers/plans/2026-05-12-stage-3-read-client-deviations.md`.

**Files:**
- Create: `docs/superpowers/plans/2026-05-30-stage-4-write-client-deviations.md`

- [ ] **Step 11.1: Run the full offline suite**

```bash
pytest tests/offline -v 2>&1 | tail -40
```

Expected: passes match the Stage 3 baseline (337 passing). Pre-existing failures from later-stage work (Stage 5/6 — `test_manager.py`, `test_main.py`, `test_event_handler.py`, `test_daemon.py`, and the `test_sync.py` items tied to the unported `sync.py`) remain; do not "fix" them. The new `test_writes.py` adds ~35-40 passing cases.

If new failures appear, debug them — they are real Stage 4 regressions, not baseline.

- [ ] **Step 11.2: Check `client.py` and `writes.py` line counts vs spec §3.5 targets**

```bash
wc -l src/cumulus/client.py src/cumulus/writes.py
```

Expected: `client.py` ≈ 1100–1250 (within 50 of the pre-task-2 1198, given delegators replaced multi-line stubs); `writes.py` 400–550. If `writes.py` is much larger, look for accidental duplication; if much smaller, double-check coverage didn't get lost.

- [ ] **Step 11.3: Create the deviations file**

Create `docs/superpowers/plans/2026-05-30-stage-4-write-client-deviations.md`:

```markdown
# Stage 4 — Plan deviations and smoke-test record

**Plan:** `docs/superpowers/plans/2026-05-30-stage-4-write-client.md`
**Spec:** `docs/superpowers/specs/2026-05-30-stage-4-write-client-design.md`

## Implementation deviations

(Fill in any divergences from the plan as they happen — e.g. helper
renamed, test reshaped, intermediate refactor. Stage 3's deviations
file is the reference for tone and granularity.)

## Smoke test checklist

Run against the real `cumulus@arko.net` Drive account, ideally on a
freshly-bootstrapped sync root (or a dedicated subfolder). Record
PASS/FAIL per item with a timestamp and any observations.

> **Important:** do not run `pytest tests/offline` while a smoke-test
> account is linked — the keyring test fixture collides and wipes the
> linked credentials. See memory `feedback_keyring_test_collision.md`.

- [ ] **Upload — small (<5 MB).** Create a local 1 KB text file in the
      sync root; verify it appears in Drive with matching content and
      `md5Checksum`. Confirm `path_cache.path_to_id` resolves it.
- [ ] **Upload — large (>5 MB).** Create a local 10 MB random binary;
      verify the resumable code path runs (logs show chunked progress),
      the upload completes, and the MD5 matches.
- [ ] **Move.** Move a file across folders and rename in the same op
      (`/folder-a/foo.txt → /folder-b/bar.txt`). Verify the Drive item's
      `parents` updated and the local cache reflects the new path.
- [ ] **Trash (default `remove`).** Delete a file via the client;
      verify Drive shows `trashed: true` (still present in Trash) and
      `path_cache` no longer resolves the path.
- [ ] **`make_dir`.** Create a folder; verify it appears on Drive and
      in the cache.
- [ ] **`make_dir_batch` (≤3 paths).** Create three folders in one
      batch; verify all three appear; verify `_count_request` recorded
      3 sub-requests for 1 HTTP round-trip (instrument if needed).
- [ ] **Restore.** Make two revisions of a small text file via two
      `upload(WriteMode.Update)` calls; then `restore(remote_path, rev)`
      where `rev` is the first revision's ID; verify the file's current
      content reverts to the first-revision bytes (and a *new* revision
      is created — restore does not roll back history).

## Stage 5 carry-over notes

(Add any items the smoke test surfaces that should be queued for
Stage 5, mirroring the "Notes for Stage 4 / Stage 5" section of the
Stage 3 deviations file.)
```

- [ ] **Step 11.4: Commit the deviations file**

```bash
git add docs/superpowers/plans/2026-05-30-stage-4-write-client-deviations.md
git commit -m "docs(stage-4): plan-deviations file with smoke-test checklist"
```

- [ ] **Step 11.5: Confirm acceptance criteria from spec §10**

Walk the spec §10 list and tick each:

1. `src/cumulus/writes.py` exists, helper implements §4 ops, `client.py` stubs replaced by delegators. ✅
2. `errorhandling.py` adds the §6.1 rows. ✅ (only 412 was genuinely new; others were already covered)
3. Path-cache write-through happens per §5 on every successful write. ✅
4. `tests/offline/test_writes.py` covers §7's checklist and passes under `pytest tests/offline -k writes`. ✅
5. `tests/offline/test_client.py` and `tests/offline/test_errorhandling.py` extensions pass. ✅
6. No new offline-suite failures vs the Stage 5/6 baseline. ✅
7. Smoke test against a real Drive account — *user-driven*; recorded in the deviations file when run.

Items 1–6 are this task's verification gate. Item 7 is the user's smoke-test step after the implementation is complete; record the result in the deviations file when done.

---

## Self-Review Notes (for the implementer / reviewer)

- **WriteMode resolution table coverage.** Spec §4.1.1 has 7 rows. Task 6 covers them across:
  - Row 1 (Add + no): `test_upload_add_creates_new_file`
  - Row 2 (Add + yes + autorename=False): `test_upload_add_existing_without_autorename_raises_file_conflict`
  - Row 3 (Add + yes + autorename=True): `test_upload_add_existing_with_autorename_picks_unique_name`
  - Row 4 (Update + yes): `test_upload_update_existing`
  - Row 5 (Update + no): `test_upload_update_missing_raises_not_found`
  - Row 6 (Overwrite + yes): `test_upload_overwrite_updates_when_existing`
  - Row 7 (Overwrite + no): `test_upload_overwrite_creates_when_missing`

- **Spec §5 cache write-through table.** Task 3 (make_dir) tests `cache.set`; Task 4 (remove) tests `cache.remove`; Task 5 (move) tests `cache.move`; Task 6 (upload Add) tests `cache.set`; Task 6 (upload Update) tests no cache mutation; Task 8/9 batches test `cache.set` / `cache.remove` per success.

- **Helper function naming consistency.** All shared helpers are introduced in Task 3 (`_split_parent_name`, `_decode_segment`, `_resolve_parent_id`, `_autorename_unique`, `_wrap_metadata`) and reused by name in later tasks — no renames or duplicate definitions.

- **`SyncEvent` progress API.** The plan uses `sync_event.update_progress(bytes)` as a placeholder. Verify the actual `SyncEvent` shape in `src/cumulus/core.py` before implementing Task 7 — adjust the test assertion and the call site together. Stage 3's `download` is the reference implementation for the progress-reporting pattern.

- **`FileMetadata` / `FolderMetadata` constructors.** Plan code uses the field set seen in `changes.py:to_file_metadata`. Cross-check against `core.py` when typing the dataclass init in `_project_file_metadata` and `_wrap_metadata` — if `client_modified` is non-Optional, swap `None` → `datetime.fromtimestamp(0, timezone.utc)`.

- **Pre-existing test baseline.** `tests/offline/test_main.py`, `test_manager.py`, `test_event_handler.py`, `test_daemon.py`, `test_sync.py` carry known failures from later-stage work. Task 11's "no new failures" check is against *this* known baseline, not zero failures.
