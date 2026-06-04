# Stage 7a — Test Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an in-memory fake Google Drive service, rewire the offline test suite onto it, rebuild the real-account linked suite against a dedicated test account, and close the remaining test loose ends so `pytest tests/offline` is fully green with no `--ignore` and zero `dropbox` imports.

**Architecture:** A hand-rolled stateful fake (`tests/fake_drive.py`) mimics the `googleapiclient` "Resource" tree (`files()/changes()/about()/revisions()/new_batch_http_request()`), injected at the single choke point `client._get_service()`. The real `client.py`/`writes.py`/`changes.py`/`path_cache` run unchanged on top of it. Offline tests that exercise sequenced state move onto the fake; pure-unit tests stay as-is. The linked suite is gated behind `CUMULUS_TEST_REFRESH_TOKEN` and a Drive-native session lock.

**Tech Stack:** Python, pytest, `googleapiclient` (`MediaFileUpload`, `MediaIoBaseDownload`, `HttpError`), `httplib2`, `hashlib`.

**Spec:** `docs/superpowers/specs/2026-06-04-stage-7a-test-infrastructure-design.md`

---

## File Structure

**Created:**
- `tests/fake_drive.py` — the in-memory fake Drive service (the deliverable).
- `tests/offline/test_fake_drive.py` — contract tests proving the fake honours the §5.4 fidelity contract.
- `tests/README.md` — how to run offline vs. linked, env convention, lock model, fidelity contract.

**Modified (offline):**
- `tests/offline/conftest.py` — add `fake_drive` + fake-wired `client`/`sync` fixtures; `DropboxClient` → `GoogleDriveClient`.
- `tests/offline/test_changes.py` — feed `changes().list` payloads from the fake.
- `tests/offline/test_client.py` — round-trip state via the fake; new-file `NotFoundError` regression.
- `tests/offline/test_writes.py` — upload/move/remove/mkdir/batch/restore against the fake.
- `tests/offline/test_clean_local_events.py` — drop `DropboxClient` import.
- `tests/offline/test_manager.py` — un-ignore; delete path-root tests; fix valid ones.
- `tests/offline/test_event_handler.py` — fix 5 `FileExists` fixture errors.

**Modified/rewritten (linked):**
- `tests/linked/conftest.py`, `tests/linked/unit/conftest.py`, `tests/linked/integration/conftest.py` — `CUMULUS_TEST_REFRESH_TOKEN`, `skipif` gating, `GoogleDriveClient`.
- `tests/linked/lock.py` — Drive-native session lock (replaces `DropboxTestLock`).
- `tests/linked/unit/test_client.py`, `tests/linked/integration/test_main.py`, `tests/linked/integration/test_sync.py` — port to Drive shapes; drop sharing tests.

**Reference (read, do not modify):** `src/cumulus/client.py`, `src/cumulus/writes.py`, `src/cumulus/changes.py` (`FILE_FIELDS`), `src/cumulus/core.py`.

---

## Drive API surface the fake must model (reference)

Built from an exhaustive sweep of `_get_service()` call sites. The fake must support exactly these.

| Resource.method | Request kwargs | Response fields the code reads |
|---|---|---|
| `files().get` | `fileId`, `fields` (one of `"id"`, `FILE_FIELDS`, `"headRevisionId"`, `"headRevisionId,mimeType"`, `"mimeType"`) | per `fields` |
| `files().list` | `q` (`"'<id>' in parents"` ± `" and trashed=false"`, or `"trashed=false"`), `fields=f"nextPageToken, files({FILE_FIELDS})"`, `pageSize`, `pageToken` | `files` (array), `nextPageToken` |
| `files().create` | `body={name,parents,modifiedTime}`, `media_body`, `fields` | `id,name,mimeType,md5Checksum,modifiedTime,size,headRevisionId` |
| `files().update` | `fileId`, `body` (`{modifiedTime}` \| `{trashed}` \| `{name,addParents,removeParents}`), `media_body`, `fields` | as create / `trashed` / move fields |
| `files().delete` | `fileId` | (none) |
| `files().get_media` | `fileId` | streamed via `MediaIoBaseDownload` |
| `changes().getStartPageToken` | — | `startPageToken` |
| `changes().list` | `pageToken`, `pageSize`, `fields`, `includeRemoved` | `changes(removed,fileId,file(FILE_FIELDS))`, `nextPageToken`, `newStartPageToken` |
| `about().get` | `fields` | `user(permissionId,displayName,emailAddress,photoLink)`, `storageQuota(usage,limit)` |
| `revisions().list` | `fileId`, `fields`, `pageSize`, `pageToken` | `revisions(id,modifiedTime,size,md5Checksum,lastModifyingUser(permissionId))`, `nextPageToken` |
| `revisions().get_media` | `fileId`, `revisionId` | streamed via `MediaIoBaseDownload` |
| `new_batch_http_request()` | `.add(request, request_id, callback)` | callback `(request_id, response|None, exception|None)` |

`FILE_FIELDS` (from `changes.py:51`): `"id, name, parents, mimeType, modifiedTime, size, md5Checksum, shared, headRevisionId, trashed, lastModifyingUser(permissionId)"`.

---

## Phase A — the fake Drive (`tests/fake_drive.py`)

> TDD note: the fake is the production artefact of this phase. Each task writes a contract test in `tests/offline/test_fake_drive.py` (the "failing test"), runs it red, then adds the implementing section to `tests/fake_drive.py`, then runs it green. Run the whole contract file at each task's verify step so earlier tasks stay green.

### Task A1: State model, projection, seeding, error helper

**Files:**
- Create: `tests/fake_drive.py`
- Test: `tests/offline/test_fake_drive.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/offline/test_fake_drive.py
import hashlib

from tests.fake_drive import ROOT_ID, FakeDrive


def test_seed_root_and_file():
    fake = FakeDrive()
    assert fake.files[ROOT_ID]["mimeType"] == "application/vnd.google-apps.folder"

    folder_id = fake.add_folder("/docs")
    file_id = fake.add_file("/docs/a.txt", content=b"hello")

    folder = fake.files[folder_id]
    rec = fake.files[file_id]
    assert folder["parents"] == [ROOT_ID]
    assert folder["mimeType"] == "application/vnd.google-apps.folder"
    assert rec["parents"] == [folder_id]
    assert rec["name"] == "a.txt"
    assert rec["md5Checksum"] == hashlib.md5(b"hello").hexdigest()
    assert rec["size"] == 5
    assert rec["trashed"] is False
    assert rec["headRevisionId"]  # non-empty
    assert rec["content"] == b"hello"


def test_slash_in_name_is_literal():
    fake = FakeDrive()
    fid = fake.add_file("2024-05 report.pdf", content=b"x", literal_name="a/b")
    assert fake.files[fid]["name"] == "a/b"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.fake_drive'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/fake_drive.py
"""In-memory fake of the googleapiclient Google Drive service.

Stateful service-boundary fake injected at ``client._get_service()``.
The real client.py / writes.py / changes.py run unchanged on top of it.
See docs/superpowers/specs/2026-06-04-stage-7a-test-infrastructure-design.md.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

import httplib2
from googleapiclient.errors import HttpError

ROOT_ID = "ROOT"
FOLDER_MIME = "application/vnd.google-apps.folder"


def make_http_error(status: int, reason: str = "", message: str = "error") -> HttpError:
    """Build a googleapiclient HttpError the same way the real API surfaces one."""
    resp = httplib2.Response({"status": status})
    resp.reason = message
    body: dict = {"error": {"code": status, "message": message}}
    if reason:
        body["error"]["errors"] = [{"reason": reason, "message": message}]
    return HttpError(resp, json.dumps(body).encode("utf-8"))


def _now_rfc3339() -> str:
    return datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


class FakeDrive:
    """In-memory Drive backing store + Resource-tree factory."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self._id_counter = 0
        self._rev_counter = 0
        self._seq = 100  # opaque change cursor; ints rendered as str
        self._change_log: list[tuple[int, str]] = []  # (seq, fileId)
        self._fail: dict[str, list[Exception]] = {}
        self.user = {
            "permissionId": "perm-1",
            "displayName": "Test User",
            "emailAddress": "cumulus-test@example.com",
            "photoLink": "https://example.com/p.jpg",
        }
        self.storage_quota = {"usage": "1024", "limit": "16106127360"}
        # Seed the sync root.
        self.files[ROOT_ID] = {
            "id": ROOT_ID,
            "name": "My Drive",
            "parents": [],
            "mimeType": FOLDER_MIME,
            "modifiedTime": _now_rfc3339(),
            "createdTime": _now_rfc3339(),
            "trashed": False,
            "shared": False,
            "md5Checksum": None,
            "size": None,
            "headRevisionId": None,
            "content": b"",
            "revisions": [],
            "lastModifyingUser": {"permissionId": "perm-1"},
        }

    # ---- id / rev / change bookkeeping ----
    def _new_id(self) -> str:
        self._id_counter += 1
        return f"file-{self._id_counter}"

    def _new_rev(self) -> str:
        self._rev_counter += 1
        return f"rev-{self._rev_counter}"

    def _record_change(self, file_id: str) -> None:
        self._change_log.append((self._seq, file_id))
        self._seq += 1

    def fail_next(self, key: str, exc: Exception) -> None:
        """Queue *exc* to be raised on the next ``.execute()`` of *key*
        (e.g. ``"files.list"``). Used by Stage 7b fault-injection."""
        self._fail.setdefault(key, []).append(exc)

    def _maybe_fail(self, key: str) -> None:
        queue = self._fail.get(key)
        if queue:
            raise queue.pop(0)

    # ---- seeding helpers ----
    def _resolve_path_to_parent_and_name(self, path: str) -> tuple[str, str]:
        parts = [p for p in path.strip("/").split("/") if p]
        name = parts[-1]
        parent_id = ROOT_ID
        for seg in parts[:-1]:
            parent_id = self._child_by_name(parent_id, seg)
        return parent_id, name

    def _child_by_name(self, parent_id: str, name: str) -> str:
        for fid, rec in self.files.items():
            if parent_id in rec["parents"] and rec["name"] == name and not rec["trashed"]:
                return fid
        raise KeyError(f"no child {name!r} under {parent_id}")

    def add_folder(self, path: str) -> str:
        parent_id, name = self._resolve_path_to_parent_and_name(path)
        return self._insert(name, parent_id, FOLDER_MIME, b"")

    def add_file(
        self, path: str, content: bytes = b"", literal_name: str | None = None
    ) -> str:
        parent_id, name = self._resolve_path_to_parent_and_name(path)
        return self._insert(literal_name or name, parent_id, "text/plain", content)

    def _insert(self, name: str, parent_id: str, mime: str, content: bytes) -> str:
        fid = self._new_id()
        is_folder = mime == FOLDER_MIME
        rec = {
            "id": fid,
            "name": name,
            "parents": [parent_id],
            "mimeType": mime,
            "modifiedTime": _now_rfc3339(),
            "createdTime": _now_rfc3339(),
            "trashed": False,
            "shared": False,
            "md5Checksum": None if is_folder else hashlib.md5(content).hexdigest(),
            "size": None if is_folder else len(content),
            "headRevisionId": None if is_folder else self._new_rev(),
            "content": content,
            "revisions": [],
            "lastModifyingUser": {"permissionId": "perm-1"},
        }
        if not is_folder:
            rec["revisions"].append(self._revision_dict(rec))
        self.files[fid] = rec
        self._record_change(fid)
        return fid

    def _revision_dict(self, rec: dict) -> dict:
        return {
            "id": rec["headRevisionId"],
            "modifiedTime": rec["modifiedTime"],
            "size": rec["size"],
            "md5Checksum": rec["md5Checksum"],
            "lastModifyingUser": {"permissionId": "perm-1"},
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): FakeDrive state model + seeding"
```

---

### Task A2: Projection + the `files().get` / `files().list` request tree

**Files:**
- Modify: `tests/fake_drive.py`
- Test: `tests/offline/test_fake_drive.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/offline/test_fake_drive.py
from cumulus.client import GoogleDriveClient


def test_get_returns_file_fields():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"hello")
    svc = fake.service()
    resp = svc.files().get(
        fileId=fid,
        fields="id, name, parents, mimeType, modifiedTime, size, md5Checksum, "
        "shared, headRevisionId, trashed, lastModifyingUser(permissionId)",
    ).execute()
    assert resp["id"] == fid
    assert resp["name"] == "a.txt"
    assert resp["md5Checksum"] == __import__("hashlib").md5(b"hello").hexdigest()
    assert resp["headRevisionId"]
    assert resp["parents"] == [ROOT_ID]


def test_list_children_filters_trashed_and_paginates():
    fake = FakeDrive()
    folder = fake.add_folder("/d")
    fake.add_file("/d/a.txt", content=b"a")
    fake.add_file("/d/b.txt", content=b"b")
    svc = fake.service()
    resp = svc.files().list(
        q=f"'{folder}' in parents and trashed=false",
        fields="nextPageToken, files(id, name)",
        pageSize=1,
        pageToken=None,
    ).execute()
    assert len(resp["files"]) == 1
    token = resp["nextPageToken"]
    resp2 = svc.files().list(
        q=f"'{folder}' in parents and trashed=false",
        fields="nextPageToken, files(id, name)",
        pageSize=1,
        pageToken=token,
    ).execute()
    assert len(resp2["files"]) == 1
    assert "nextPageToken" not in resp2
    names = {resp["files"][0]["name"], resp2["files"][0]["name"]}
    assert names == {"a.txt", "b.txt"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: FAIL — `AttributeError: 'FakeDrive' object has no attribute 'service'`

- [ ] **Step 3: Write minimal implementation**

Add a `_FILE_FIELD_KEYS` projector, the request wrapper, the resource tree, and the `q` parser. Append to `tests/fake_drive.py`:

```python
class _Request:
    """Mimics a googleapiclient HttpRequest: only ``.execute()`` is used."""

    def __init__(self, fail_key: str, fail_check: Callable[[str], None], fn: Callable[[], Any]):
        self._fail_key = fail_key
        self._fail_check = fail_check
        self._fn = fn

    def execute(self, num_retries: int = 0, http: Any = None) -> Any:
        self._fail_check(self._fail_key)
        return self._fn()


def _project(rec: dict, fields: str) -> dict:
    """Return only the requested top-level field keys from a record.

    Field strings are Drive selectors like ``"id, name, parents"`` or
    ``"lastModifyingUser(permissionId)"``; nested parens are returned whole.
    The fake stores every readable field, so projection is a key filter.
    """
    keys = _parse_field_keys(fields)
    out: dict = {}
    for key in keys:
        if key == "*" or key in rec:
            if key == "*":
                continue
            value = rec[key]
            if value is not None:
                out[key] = value
    return out


def _parse_field_keys(fields: str) -> list[str]:
    keys = []
    depth = 0
    token = ""
    for ch in fields:
        if ch == "(":
            depth += 1
            # keep only the name before "("
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            keys.append(token.strip())
            token = ""
            continue
        if depth == 0 and ch not in "(),":
            token += ch
    if token.strip():
        keys.append(token.strip())
    # strip any leading "files"/"changes"/"revisions" wrappers handled by caller
    return [k for k in keys if k]


def _match_query(rec: dict, query: str) -> bool:
    """Evaluate the tiny query grammar our code emits.

    Supported fragments joined by ' and ':
      "'<parent_id>' in parents"   -> parent membership
      "trashed=false"              -> not trashed
    """
    if not query:
        return True
    for frag in query.split(" and "):
        frag = frag.strip()
        if frag in ("trashed=false", "trashed = false"):
            if rec["trashed"]:
                return False
        elif " in parents" in frag:
            pid = frag.split(" in parents")[0].strip().strip("'")
            if pid not in rec["parents"]:
                return False
        else:
            raise AssertionError(f"FakeDrive: unsupported query fragment {frag!r}")
    return True
```

Now add the resource-tree classes and `FakeDrive.service()`:

```python
class _FilesResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive

    def get(self, fileId, fields="", **_):
        def run():
            rec = self._d.files.get(fileId)
            if rec is None or rec["trashed"] and fileId not in self._d.files:
                raise make_http_error(404, "notFound", "File not found")
            if rec is None:
                raise make_http_error(404, "notFound", "File not found")
            return _project(rec, fields)
        return _Request("files.get", self._d._maybe_fail, run)

    def list(self, q="", fields="", pageSize=100, pageToken=None, **_):
        def run():
            matched = [
                r for r in self._d.files.values()
                if r["id"] != ROOT_ID and _match_query(r, q)
            ]
            matched.sort(key=lambda r: r["id"])
            start = int(pageToken) if pageToken else 0
            page = matched[start:start + pageSize]
            inner = fields.split("files(", 1)[1].rstrip(")") if "files(" in fields else "*"
            resp = {"files": [_project(r, inner) for r in page]}
            if start + pageSize < len(matched):
                resp["nextPageToken"] = str(start + pageSize)
            return resp
        return _Request("files.list", self._d._maybe_fail, run)


class _ChangesResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive


class _AboutResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive


class _RevisionsResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive


class _Service:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive

    def files(self):
        return _FilesResource(self._d)

    def changes(self):
        return _ChangesResource(self._d)

    def about(self):
        return _AboutResource(self._d)

    def revisions(self):
        return _RevisionsResource(self._d)

    def new_batch_http_request(self):
        return _Batch()


# add to FakeDrive:
#     def service(self) -> "_Service":
#         return _Service(self)
```

Add the `service()` method onto `FakeDrive` (place it after `_maybe_fail`):

```python
    def service(self) -> "_Service":
        return _Service(self)
```

And a minimal `_Batch` placeholder (fleshed out in Task A8b/A11):

```python
class _Batch:
    def __init__(self):
        self._reqs: list = []

    def add(self, request, request_id=None, callback=None):
        self._reqs.append((request_id, request, callback))

    def execute(self, http=None):
        for request_id, request, callback in self._reqs:
            try:
                resp, exc = request.execute(), None
            except Exception as e:  # noqa: BLE001 - faithfully mirror batch
                resp, exc = None, e
            if callback:
                callback(request_id, resp, exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): FakeDrive files.get/list + projection + query parser"
```

---

### Task A3: `client`-on-fake round-trip — `get_metadata` + `list_folder`

This task proves the *real client* runs on the fake (not just the fake in isolation).

**Files:**
- Modify: `tests/offline/test_fake_drive.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/offline/test_fake_drive.py
import sqlite3

from cumulus.database.core import Database
from cumulus.keyring import CredentialStorage
from cumulus.path_cache import PathIDCache


def _client_on(fake: FakeDrive) -> GoogleDriveClient:
    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = ROOT_ID
    client = GoogleDriveClient(
        "test-config", CredentialStorage("test-config"), cache
    )
    client._service = fake.service()
    client._root_id = ROOT_ID
    return client


def test_client_get_metadata_round_trip():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"hello")
    fake._client_cache_seed = None
    client = _client_on(fake)
    # seed the cache so path resolution works
    client._path_cache.set("/a.txt", fid, ROOT_ID, "a.txt", False)
    md = client.get_metadata("/a.txt")
    assert md.rev == fake.files[fid]["headRevisionId"]
    assert md.content_hash == fake.files[fid]["md5Checksum"]
    assert md.size == 5
```

> Note: confirm `get_metadata`'s signature and how it resolves a path → id before finalizing this test. If `get_metadata` takes a fileId rather than a path, adjust the call accordingly (read `client.py:get_metadata`). The assertion targets (`rev`, `content_hash`, `size`) are stable regardless.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py::test_client_get_metadata_round_trip -q`
Expected: FAIL — most likely an `HttpError`/attribute mismatch revealing a projection gap.

- [ ] **Step 3: Fix the fake until the real client is satisfied**

No new file; iterate on `_FilesResource.get`/`_project` so the projected dict matches what `to_file_metadata` reads (`changes.py:77`). The `get` 404 branch above is deliberately redundant — simplify it to:

```python
    def get(self, fileId, fields="", **_):
        def run():
            rec = self._d.files.get(fileId)
            if rec is None:
                raise make_http_error(404, "notFound", "File not found")
            return _project(rec, fields)
        return _Request("files.get", self._d._maybe_fail, run)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): real client.get_metadata round-trips on FakeDrive"
```

---

### Task A4: `files().create` + `files().update` (simple upload, move+rename, trash) + rev precondition

**Files:**
- Modify: `tests/fake_drive.py`, `tests/offline/test_fake_drive.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/offline/test_fake_drive.py
from googleapiclient.http import MediaInMemoryUpload


def test_create_simple_upload():
    fake = FakeDrive()
    svc = fake.service()
    media = MediaInMemoryUpload(b"data", mimetype="text/plain", resumable=False)
    resp = svc.files().create(
        body={"name": "n.txt", "parents": [ROOT_ID], "modifiedTime": "2026-01-02T00:00:00Z"},
        media_body=media,
        fields="id,name,md5Checksum,size,headRevisionId",
    ).execute()
    assert resp["name"] == "n.txt"
    assert resp["md5Checksum"] == __import__("hashlib").md5(b"data").hexdigest()
    assert resp["size"] == 4
    assert resp["headRevisionId"]
    assert fake.files[resp["id"]]["parents"] == [ROOT_ID]


def test_update_move_and_rename_single_entry():
    fake = FakeDrive()
    src = fake.add_folder("/src")
    dst = fake.add_folder("/dst")
    fid = fake.add_file("/src/a.txt", content=b"x")
    base = fake._seq  # cursor before the move
    svc = fake.service()
    svc.files().update(
        fileId=fid,
        body={"name": "b.txt"},
        addParents=dst,
        removeParents=src,
        fields="id,name,headRevisionId",
    ).execute()
    rec = fake.files[fid]
    assert rec["name"] == "b.txt"
    assert rec["parents"] == [dst]
    # exactly one change entry covers both rename and reparent
    changes = svc.changes().list(
        pageToken=str(base), pageSize=100, fields="changes(removed,fileId,file(id,name))",
        includeRemoved=True,
    ).execute()["changes"]
    assert [c["fileId"] for c in changes] == [fid]


def test_update_trash_sets_flag():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    svc = fake.service()
    resp = svc.files().update(
        fileId=fid, body={"trashed": True}, fields="id,trashed"
    ).execute()
    assert resp["trashed"] is True
    assert fake.files[fid]["trashed"] is True


def test_update_precondition_412():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    svc = fake.service()
    # simulate a stale-rev precondition by failing the next update
    fake.fail_next("files.update", make_http_error(412, "conditionNotMet"))
    try:
        svc.files().update(fileId=fid, body={"name": "z"}, fields="id").execute()
        assert False, "expected 412"
    except HttpError as e:
        assert e.resp.status == 412
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: FAIL — `_FilesResource` has no `create`/`update`.

- [ ] **Step 3: Write minimal implementation**

Add a media-reading helper and the two methods to `_FilesResource`:

```python
def _read_media(media) -> bytes:
    """Read all bytes from a googleapiclient MediaUpload (in-memory or file)."""
    return media.getbytes(0, media.size())
```

```python
    # inside _FilesResource
    def create(self, body=None, media_body=None, fields="", **_):
        def run():
            body_ = body or {}
            mime = body_.get("mimeType", FOLDER_MIME if media_body is None else "text/plain")
            content = _read_media(media_body) if media_body is not None else b""
            fid = self._d._new_id()
            is_folder = mime == FOLDER_MIME
            rec = {
                "id": fid,
                "name": body_["name"],
                "parents": list(body_.get("parents", [ROOT_ID])),
                "mimeType": mime,
                "modifiedTime": body_.get("modifiedTime", _now_rfc3339()),
                "createdTime": _now_rfc3339(),
                "trashed": False,
                "shared": False,
                "md5Checksum": None if is_folder else __import__("hashlib").md5(content).hexdigest(),
                "size": None if is_folder else len(content),
                "headRevisionId": None if is_folder else self._d._new_rev(),
                "content": content,
                "revisions": [],
                "lastModifyingUser": {"permissionId": "perm-1"},
            }
            if not is_folder:
                rec["revisions"].append(self._d._revision_dict(rec))
            self._d.files[fid] = rec
            self._d._record_change(fid)
            return _project(rec, fields)
        return _Request("files.create", self._d._maybe_fail, run)

    def update(self, fileId, body=None, media_body=None, addParents=None,
               removeParents=None, fields="", **_):
        def run():
            rec = self._d.files[fileId]
            body_ = body or {}
            if "name" in body_:
                rec["name"] = body_["name"]
            if "trashed" in body_:
                rec["trashed"] = body_["trashed"]
            if "modifiedTime" in body_:
                rec["modifiedTime"] = body_["modifiedTime"]
            if removeParents:
                rec["parents"] = [p for p in rec["parents"] if p != removeParents]
            if addParents:
                rec["parents"] = rec["parents"] + [addParents]
            if media_body is not None:
                content = _read_media(media_body)
                rec["content"] = content
                rec["md5Checksum"] = __import__("hashlib").md5(content).hexdigest()
                rec["size"] = len(content)
                rec["headRevisionId"] = self._d._new_rev()
                rec["revisions"].append(self._d._revision_dict(rec))
            self._d._record_change(fileId)
            return _project(rec, fields)
        return _Request("files.update", self._d._maybe_fail, run)
```

> `addParents`/`removeParents` are single ID strings (confirmed in writes.py:605 move), so they're filtered/appended as scalars, not lists.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): FakeDrive files.create/update + move+rename single entry"
```

---

### Task A5: `files().delete` (hard-delete) + `files().generateIds`

**Files:**
- Modify: `tests/fake_drive.py`, `tests/offline/test_fake_drive.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/offline/test_fake_drive.py
def test_delete_hard_removes_record():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    base = fake._seq
    svc = fake.service()
    svc.files().delete(fileId=fid).execute()
    assert fid not in fake.files
    changes = svc.changes().list(
        pageToken=str(base), pageSize=100, fields="changes(removed,fileId)",
        includeRemoved=True,
    ).execute()["changes"]
    assert changes == [{"removed": True, "fileId": fid}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py::test_delete_hard_removes_record -q`
Expected: FAIL — `_FilesResource` has no `delete`.

- [ ] **Step 3: Write minimal implementation**

```python
    # inside _FilesResource
    def delete(self, fileId, **_):
        def run():
            self._d.files.pop(fileId, None)
            self._d._record_change(fileId)
            return None
        return _Request("files.delete", self._d._maybe_fail, run)

    def generateIds(self, count=1, **_):
        def run():
            return {"ids": [self._d._new_id() for _ in range(count)]}
        return _Request("files.generateIds", self._d._maybe_fail, run)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): FakeDrive files.delete (hard-delete change entry)"
```

---

### Task A6: `changes().getStartPageToken` + `changes().list` (removed-reasons, pagination)

**Files:**
- Modify: `tests/fake_drive.py`, `tests/offline/test_fake_drive.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/offline/test_fake_drive.py
def test_changes_removed_reason_semantics():
    fake = FakeDrive()
    trashed = fake.add_file("/t.txt", content=b"a")
    hard = fake.add_file("/h.txt", content=b"b")
    base = fake._seq
    svc = fake.service()
    svc.files().update(fileId=trashed, body={"trashed": True}, fields="id").execute()
    svc.files().delete(fileId=hard).execute()
    fields = "nextPageToken, newStartPageToken, changes(removed, fileId, file(id, name, trashed))"
    resp = svc.changes().list(
        pageToken=str(base), pageSize=100, fields=fields, includeRemoved=True
    ).execute()
    by_id = {c["fileId"]: c for c in resp["changes"]}
    # trash: removed=false, file body present with trashed=true
    assert by_id[trashed]["removed"] is False
    assert by_id[trashed]["file"]["trashed"] is True
    # hard-delete: removed=true, no file body
    assert by_id[hard]["removed"] is True
    assert "file" not in by_id[hard]
    # exhausted page yields a fresh newStartPageToken, no nextPageToken
    assert "nextPageToken" not in resp
    assert resp["newStartPageToken"] == str(fake._seq)


def test_changes_pagination():
    fake = FakeDrive()
    fake.add_file("/a.txt", content=b"a")
    fake.add_file("/b.txt", content=b"b")
    svc = fake.service()
    resp = svc.changes().list(
        pageToken="100", pageSize=1, fields="nextPageToken, changes(fileId)",
        includeRemoved=True,
    ).execute()
    assert len(resp["changes"]) == 1
    assert "nextPageToken" in resp
    resp2 = svc.changes().list(
        pageToken=resp["nextPageToken"], pageSize=1,
        fields="nextPageToken, newStartPageToken, changes(fileId)", includeRemoved=True,
    ).execute()
    assert len(resp2["changes"]) == 1
    assert "newStartPageToken" in resp2


def test_get_start_page_token():
    fake = FakeDrive()
    svc = fake.service()
    resp = svc.changes().getStartPageToken().execute()
    assert resp["startPageToken"] == str(fake._seq)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: FAIL — `_ChangesResource` has no `list`/`getStartPageToken`.

- [ ] **Step 3: Write minimal implementation**

```python
    # inside _ChangesResource
    def getStartPageToken(self, **_):
        def run():
            return {"startPageToken": str(self._d._seq)}
        return _Request("changes.getStartPageToken", self._d._maybe_fail, run)

    def list(self, pageToken, pageSize=100, fields="", includeRemoved=True, **_):
        def run():
            start = int(pageToken)
            latest: dict[str, int] = {}
            for seq, fid in self._d._change_log:
                if seq >= start:
                    latest[fid] = seq
            ordered = sorted(latest.items(), key=lambda kv: kv[1])  # (fid, seq)
            page = ordered[:pageSize]
            rest = ordered[pageSize:]
            inner = (
                fields.split("changes(", 1)[1].rsplit(")", 1)[0]
                if "changes(" in fields else "*"
            )
            file_inner = (
                inner.split("file(", 1)[1].rsplit(")", 1)[0]
                if "file(" in inner else "*"
            )
            changes = []
            for fid, _seq in page:
                rec = self._d.files.get(fid)
                if rec is None:
                    changes.append({"removed": True, "fileId": fid})
                else:
                    changes.append({
                        "removed": False,
                        "fileId": fid,
                        "file": _project(rec, file_inner),
                    })
            resp: dict = {"changes": changes}
            if rest:
                resp["nextPageToken"] = str(rest[0][1])
            else:
                resp["newStartPageToken"] = str(self._d._seq)
            return resp
        return _Request("changes.list", self._d._maybe_fail, run)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): FakeDrive changes.list (removed-reasons, pagination, token)"
```

---

### Task A7: `about().get` + `revisions().list`

**Files:**
- Modify: `tests/fake_drive.py`, `tests/offline/test_fake_drive.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/offline/test_fake_drive.py
def test_about_get():
    fake = FakeDrive()
    svc = fake.service()
    resp = svc.about().get(
        fields="user(emailAddress,displayName,permissionId,photoLink),"
        "storageQuota(usage,limit)"
    ).execute()
    assert resp["user"]["emailAddress"] == "cumulus-test@example.com"
    assert resp["storageQuota"]["limit"] == "16106127360"


def test_revisions_list():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"v1")
    svc = fake.service()
    svc.files().update(
        fileId=fid,
        media_body=MediaInMemoryUpload(b"v2", mimetype="text/plain"),
        fields="id",
    ).execute()
    resp = svc.revisions().list(
        fileId=fid,
        fields="nextPageToken, revisions(id, modifiedTime, size, md5Checksum, "
        "lastModifyingUser(permissionId))",
        pageSize=100,
        pageToken=None,
    ).execute()
    assert len(resp["revisions"]) == 2
    assert resp["revisions"][-1]["md5Checksum"] == __import__("hashlib").md5(b"v2").hexdigest()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: FAIL — `_AboutResource`/`_RevisionsResource` have no methods.

- [ ] **Step 3: Write minimal implementation**

```python
    # inside _AboutResource
    def get(self, fields="", **_):
        def run():
            return {"user": dict(self._d.user), "storageQuota": dict(self._d.storage_quota)}
        return _Request("about.get", self._d._maybe_fail, run)
```

```python
    # inside _RevisionsResource
    def list(self, fileId, fields="", pageSize=100, pageToken=None, **_):
        def run():
            revs = self._d.files[fileId]["revisions"]
            start = int(pageToken) if pageToken else 0
            page = revs[start:start + pageSize]
            resp = {"revisions": list(page)}
            if start + pageSize < len(revs):
                resp["nextPageToken"] = str(start + pageSize)
            return resp
        return _Request("revisions.list", self._d._maybe_fail, run)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): FakeDrive about.get + revisions.list"
```

---

### Task A8: download seam — `files().get_media` + `revisions().get_media` via real `MediaIoBaseDownload`

This is one of the two fidelity-sensitive spots. The fake's get-media request must satisfy the *real* `MediaIoBaseDownload` (`.uri`, `.http.request(...)` honouring a `Range` header, `.headers`).

**Files:**
- Modify: `tests/fake_drive.py`, `tests/offline/test_fake_drive.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/offline/test_fake_drive.py
import io

from googleapiclient.http import MediaIoBaseDownload


def test_get_media_downloads_via_real_downloader():
    fake = FakeDrive()
    content = b"x" * (5 * 1024 * 1024 + 17)  # multi-chunk
    fid = fake.add_file("/big.bin", content=content)
    svc = fake.service()
    request = svc.files().get_media(fileId=fid)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=1024 * 1024)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    assert buf.getvalue() == content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py::test_get_media_downloads_via_real_downloader -q`
Expected: FAIL — `_FilesResource` has no `get_media`.

- [ ] **Step 3: Write minimal implementation**

```python
class _FakeHttp:
    """Minimal httplib2-shaped transport for MediaIoBaseDownload.

    MediaIoBaseDownload calls ``http.request(uri, headers={'range': ...})``
    and reads ``resp.status`` plus ``content-range`` / ``content-length``.
    """

    def __init__(self, content: bytes):
        self._content = content

    def request(self, uri, method="GET", body=None, headers=None):
        total = len(self._content)
        rng = (headers or {}).get("range") or (headers or {}).get("Range")
        if rng:
            spec = rng.split("=", 1)[1]
            begin_s, end_s = spec.split("-")
            begin, end = int(begin_s), int(end_s)
            chunk = self._content[begin:end + 1]
            resp = httplib2.Response({
                "status": 206,
                "content-range": f"bytes {begin}-{begin + len(chunk) - 1}/{total}",
                "content-length": str(len(chunk)),
            })
        else:
            chunk = self._content
            resp = httplib2.Response({"status": 200, "content-length": str(total)})
        return resp, chunk


class _MediaRequest:
    """Mimics the HttpRequest that MediaIoBaseDownload wraps."""

    def __init__(self, content: bytes):
        self.uri = "https://fake.drive/get_media"
        self.http = _FakeHttp(content)
        self.headers: dict = {}
        self.postproc = lambda resp, content: content
```

```python
    # inside _FilesResource
    def get_media(self, fileId, **_):
        rec = self._d.files[fileId]
        return _MediaRequest(rec["content"])
```

```python
    # inside _RevisionsResource
    def get_media(self, fileId, revisionId, **_):
        rec = self._d.files[fileId]
        # the fake keeps only the latest content blob; that is what restore reads
        return _MediaRequest(rec["content"])
```

> The fake stores only the latest content blob. `restore()` downloads a revision then re-uploads; for offline tests the latest blob is sufficient to exercise the round-trip. The linked suite validates true historical-revision content.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): FakeDrive get_media via real MediaIoBaseDownload"
```

---

### Task A9: resumable upload seam — `next_chunk()` loop

The second fidelity-sensitive spot. `writes.py:_upload_resumable` (line ~371) loops `status, response = request.next_chunk()` and reads `status.resumable_progress`. The fake's create/update with a `resumable=True` media must return a request exposing `.next_chunk()`.

**Files:**
- Modify: `tests/fake_drive.py`, `tests/offline/test_fake_drive.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/offline/test_fake_drive.py
def test_resumable_create_drives_next_chunk():
    fake = FakeDrive()
    svc = fake.service()
    media = MediaInMemoryUpload(b"y" * 100, mimetype="text/plain", resumable=True)
    request = svc.files().create(
        body={"name": "r.txt", "parents": [ROOT_ID]},
        media_body=media,
        fields="id,name,md5Checksum,size,headRevisionId",
    )
    response = None
    progress_seen = []
    while response is None:
        status, response = request.next_chunk()
        if status is not None:
            progress_seen.append(status.resumable_progress)
    assert response["name"] == "r.txt"
    assert response["size"] == 100
    assert progress_seen  # at least one progress callback fired
    assert fake.files[response["id"]]["md5Checksum"] == __import__("hashlib").md5(b"y" * 100).hexdigest()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py::test_resumable_create_drives_next_chunk -q`
Expected: FAIL — the returned `_Request` has no `next_chunk`.

- [ ] **Step 3: Write minimal implementation**

Add a resumable request type and branch `create`/`update` on `media_body.resumable`:

```python
class _MediaUploadProgress:
    def __init__(self, resumable_progress: int, total_size: int):
        self.resumable_progress = resumable_progress
        self.total_size = total_size

    def progress(self) -> float:
        return self.resumable_progress / self.total_size if self.total_size else 1.0


class _ResumableRequest:
    """Drives a fake chunked upload in two next_chunk() calls.

    The content is fully available (the media wraps a local file/bytes),
    so chunking is simulated purely to exercise writes.py's progress loop.
    """

    def __init__(self, fail_key, fail_check, content: bytes, finalize: Callable[[], Any]):
        self._fail_key = fail_key
        self._fail_check = fail_check
        self._content = content
        self._finalize = finalize
        self._step = 0

    def next_chunk(self, num_retries: int = 0):
        self._fail_check(self._fail_key)
        total = len(self._content)
        if self._step == 0 and total > 0:
            self._step = 1
            half = max(1, total // 2)
            return _MediaUploadProgress(half, total), None
        response = self._finalize()
        return _MediaUploadProgress(total, total), response
```

In `_FilesResource.create` and `.update`, when `media_body is not None and getattr(media_body, "resumable", False)` is true, return a `_ResumableRequest` whose `finalize` runs the same record-mutation `run()` body. Refactor so the mutation logic is a closure reused by both the `_Request` and `_ResumableRequest` paths:

```python
    # revised _FilesResource.create
    def create(self, body=None, media_body=None, fields="", **_):
        def finalize():
            body_ = body or {}
            content = _read_media(media_body) if media_body is not None else b""
            mime = body_.get("mimeType", "text/plain" if media_body is not None else FOLDER_MIME)
            fid = self._d._new_id()
            is_folder = mime == FOLDER_MIME
            rec = {
                "id": fid, "name": body_["name"],
                "parents": list(body_.get("parents", [ROOT_ID])),
                "mimeType": mime,
                "modifiedTime": body_.get("modifiedTime", _now_rfc3339()),
                "createdTime": _now_rfc3339(), "trashed": False, "shared": False,
                "md5Checksum": None if is_folder else __import__("hashlib").md5(content).hexdigest(),
                "size": None if is_folder else len(content),
                "headRevisionId": None if is_folder else self._d._new_rev(),
                "content": content, "revisions": [],
                "lastModifyingUser": {"permissionId": "perm-1"},
            }
            if not is_folder:
                rec["revisions"].append(self._d._revision_dict(rec))
            self._d.files[fid] = rec
            self._d._record_change(fid)
            return _project(rec, fields)

        if media_body is not None and getattr(media_body, "resumable", False):
            return _ResumableRequest(
                "files.create", self._d._maybe_fail, _read_media(media_body), finalize
            )
        return _Request("files.create", self._d._maybe_fail, finalize)
```

Apply the same `finalize`/`_ResumableRequest` split to `update` (keep its existing mutation body inside `finalize`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): FakeDrive resumable upload next_chunk loop"
```

---

### Task A10: batch requests + fidelity-contract consolidation test

**Files:**
- Modify: `tests/fake_drive.py` (the `_Batch` from A2 already works — add a contract test), `tests/offline/test_fake_drive.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/offline/test_fake_drive.py
def test_batch_invokes_callbacks_with_signature():
    fake = FakeDrive()
    svc = fake.service()
    results = {}

    def cb(request_id, response, exception):
        results[request_id] = (response, exception)

    batch = svc.new_batch_http_request()
    batch.add(svc.files().create(body={"name": "x.txt", "parents": [ROOT_ID]},
              media_body=MediaInMemoryUpload(b"x", mimetype="text/plain"),
              fields="id,name"), request_id="0", callback=cb)
    fake.fail_next("files.create", make_http_error(403, "storageQuotaExceeded"))
    batch.add(svc.files().create(body={"name": "y.txt", "parents": [ROOT_ID]},
              media_body=MediaInMemoryUpload(b"y", mimetype="text/plain"),
              fields="id,name"), request_id="1", callback=cb)
    batch.execute()
    assert results["0"][0]["name"] == "x.txt" and results["0"][1] is None
    assert results["1"][0] is None
    assert isinstance(results["1"][1], HttpError)


def test_fidelity_contract_summary():
    """The six §5.4 fidelity points, asserted together as a guard."""
    fake = FakeDrive()
    svc = fake.service()
    # 1. md5 + headRevisionId canonical
    fid = fake.add_file("/a.txt", content=b"hello")
    assert fake.files[fid]["md5Checksum"] == __import__("hashlib").md5(b"hello").hexdigest()
    # 3. slash literal
    sid = fake.add_file("ignored", content=b"z", literal_name="2024/05")
    got = svc.files().get(fileId=sid, fields="name").execute()
    assert got["name"] == "2024/05"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: FAIL if the batch error path or `_Batch` wiring is incomplete; otherwise confirm both new tests are collected and run.

- [ ] **Step 3: Write minimal implementation**

The `_Batch` class from Task A2 already matches the required callback contract. If the batch error test fails, ensure `_Batch.execute` catches `HttpError` (it catches `Exception`, which covers it). No code change expected beyond confirming `new_batch_http_request()` returns `_Batch`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q`
Expected: PASS — the full fake contract suite is green.

- [ ] **Step 5: Commit**

```bash
git add tests/fake_drive.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): FakeDrive batch callbacks + fidelity-contract guard"
```

---

## Phase B — rewire the offline suite onto the fake

### Task B1: conftest fixtures + `DropboxClient` → `GoogleDriveClient`

**Files:**
- Modify: `tests/offline/conftest.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/offline/test_fake_drive.py
def test_client_fixture_is_wired_to_fake(client_on_fake):
    fid = client_on_fake.fake.add_file("/a.txt", content=b"hi")
    client_on_fake.client._path_cache.set("/a.txt", fid, ROOT_ID, "a.txt", False)
    md = client_on_fake.client.get_metadata("/a.txt")
    assert md.size == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_fake_drive.py::test_client_fixture_is_wired_to_fake -q`
Expected: FAIL — fixture `client_on_fake` does not exist.

- [ ] **Step 3: Write minimal implementation**

In `tests/offline/conftest.py`: change the import on line 11 to `from cumulus.client import GoogleDriveClient`, update the `sync`/`client` fixtures to use `GoogleDriveClient`, and add the fake fixtures:

```python
import dataclasses

from tests.fake_drive import ROOT_ID, FakeDrive


@pytest.fixture
def fake_drive():
    return FakeDrive()


@dataclasses.dataclass
class ClientOnFake:
    client: "GoogleDriveClient"
    fake: FakeDrive


@pytest.fixture
def client_on_fake(fake_drive):
    cache = _make_path_cache()
    cache.root_id = ROOT_ID
    client = GoogleDriveClient(
        "test-config", CredentialStorage("test-config"), cache
    )
    client._service = fake_drive.service()
    client._root_id = ROOT_ID
    yield ClientOnFake(client=client, fake=fake_drive)
    remove_configuration("test-config")
```

Replace the two remaining `DropboxClient(` references in the `sync` and `client` fixtures with `GoogleDriveClient(`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/offline/test_fake_drive.py -q && python -m pytest tests/offline -q --ignore=tests/offline/test_manager.py`
Expected: PASS; existing offline count unchanged or higher, no new failures.

- [ ] **Step 5: Commit**

```bash
git add tests/offline/conftest.py tests/offline/test_fake_drive.py
git commit -m "test(stage-7a): conftest fake_drive/client_on_fake fixtures; GoogleDriveClient"
```

---

### Task B2: rewrite `test_changes.py` onto the fake

**Files:**
- Modify: `tests/offline/test_changes.py`

- [ ] **Step 1: Read the current file and identify hand-built `changes()` dicts**

Run: `python -m pytest tests/offline/test_changes.py -q` to capture the current passing set, then read the file. The pure-classification unit tests (`classify` table) stay as-is. The tests that hand-build `changes.list` response dicts and feed `iter_remote_changes` move onto the fake.

- [ ] **Step 2: Write the failing test (fake-driven change feed)**

```python
# add to tests/offline/test_changes.py
import sqlite3

from cumulus.changes import iter_remote_changes
from cumulus.database.core import Database
from cumulus.path_cache import PathIDCache
from tests.fake_drive import ROOT_ID, FakeDrive


def _cache():
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = ROOT_ID
    return c


def test_iter_remote_changes_new_file_in_new_folder():
    """Regression: parent-before-child ordering (Stage 3, 05d58ed)."""
    fake = FakeDrive()
    cache = _cache()
    start = fake.service().changes().getStartPageToken().execute()["startPageToken"]
    folder = fake.add_folder("/new")
    child = fake.add_file("/new/c.txt", content=b"x")

    def factory():
        return fake.service()

    def retry(fn):
        return fn()

    pages = list(iter_remote_changes(factory, retry, cache, start))
    applied = {e.path_display for page in pages for e in page.entries}
    assert "/new" in applied
    assert "/new/c.txt" in applied
```

- [ ] **Step 3: Run to verify it fails, then make it pass**

Run: `python -m pytest tests/offline/test_changes.py::test_iter_remote_changes_new_file_in_new_folder -q`
Expected: PASS immediately (the fixpoint logic at changes.py:286 already handles this). If it FAILS, the fake's `changes.list` ordering is wrong — fix the fake, not `changes.py`.

> This test is the explicit regression guard for the parent-before-child bug. Keep it even though it should pass first try — it proves the fake can express the scenario the old `HttpMock` could not.

- [ ] **Step 4: Port the remaining hand-built change-dict tests**

For each existing test that constructs a `{"changes": [...]}` dict, replace the literal with fake mutations (`fake.add_file`, `svc.files().update(..., body={"trashed": True})`, `svc.files().delete(...)`) and assert on `iter_remote_changes` output. Keep `classify`-only unit tests untouched.

- [ ] **Step 5: Run the file + commit**

Run: `python -m pytest tests/offline/test_changes.py -q`
Expected: PASS

```bash
git add tests/offline/test_changes.py
git commit -m "test(stage-7a): change-feed tests on FakeDrive + parent-before-child regression"
```

---

### Task B3: rewrite `test_client.py` round-trip + new-file `NotFoundError` regression

**Files:**
- Modify: `tests/offline/test_client.py`

- [ ] **Step 1: Write the failing test (new-file upload regression)**

```python
# add to tests/offline/test_client.py
def test_check_requires_upload_handles_missing_path(client_on_fake):
    """Regression (Stage 5, 2ed6e33): get_metadata raising NotFoundError for
    an unknown path must mean 'upload required', not crash."""
    cof = client_on_fake
    # path the cache does not know -> get_metadata should raise NotFoundError,
    # and the upload-decision path must treat that as 'needs upload'.
    md = None
    from cumulus.exceptions import NotFoundError
    try:
        md = cof.client.get_metadata("/does-not-exist.txt")
    except NotFoundError:
        md = None
    assert md is None  # the decision code keys off this
```

> Confirm the exact method that consumes this (`_check_requires_upload` per the memory) and assert through it if it is reachable in an offline unit test; otherwise this asserts the `get_metadata` contract the decision relies on. Read `client.py`/`sync.py` to wire the strongest available assertion.

- [ ] **Step 2: Run to verify, then port round-trip tests**

Replace `HttpMock`-style canned tests with fake round-trips:
- upload (simple ≤5MB and resumable >5MB) → `get_metadata` shows md5/rev/size
- `list_folder` reflects the tree
- `move` → `get_metadata` at new path; old path gone
- trash → change feed reports delete
- download via the real `MediaIoBaseDownload` writes correct bytes + md5

- [ ] **Step 3: Run the file**

Run: `python -m pytest tests/offline/test_client.py -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/offline/test_client.py
git commit -m "test(stage-7a): client round-trip on FakeDrive + new-file NotFoundError regression"
```

---

### Task B4: rewrite `test_writes.py` onto the fake; B5: fix `test_clean_local_events.py` import

**Files:**
- Modify: `tests/offline/test_writes.py`, `tests/offline/test_clean_local_events.py`

- [ ] **Step 1: Port `test_writes.py`**

Convert upload/move/remove/make_dir/make_dir_batch/remove_batch/restore tests to drive a `client_on_fake`-backed `_WriteHelper` and assert state via the fake (`fake.files[...]`) and via `get_metadata`. Use `fake.fail_next("files.create", make_http_error(403, "storageQuotaExceeded"))` to exercise the quota → `InsufficientSpaceError` mapping, and `fail_next("files.update", make_http_error(412, "conditionNotMet"))` for the `DataChangedError` path.

- [ ] **Step 2: Fix `test_clean_local_events.py`**

Change line 14 `from cumulus.client import DropboxClient` → `from cumulus.client import GoogleDriveClient` and the constructor on line 27. No behavioural change.

- [ ] **Step 3: Run both files**

Run: `python -m pytest tests/offline/test_writes.py tests/offline/test_clean_local_events.py -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/offline/test_writes.py tests/offline/test_clean_local_events.py
git commit -m "test(stage-7a): writes on FakeDrive (quota/412 paths); drop DropboxClient alias use"
```

---

## Phase C — loose ends

### Task C1: un-ignore + rewrite `test_manager.py`

**Files:**
- Modify: `tests/offline/test_manager.py`

- [ ] **Step 1: Delete the path-root/team tests**

Remove every `test_migrate_path_root_*` test and the `TeamRootInfo`/`UserRootInfo` imports and any fixture fields referencing team/path-root. These exercise methods Stage 6 deleted (`d75de2b`).

- [ ] **Step 2: Fix the `FullAccount` fixture to the Drive shape**

Update the module-level `account_info = FullAccount(...)` to the trimmed Drive fields (drop `root_info`/team fields). Cross-check against `src/cumulus/core.py::FullAccount`.

- [ ] **Step 3: Fix renamed manager surface**

Replace `ensure_dropbox_folder_present` references with `ensure_drive_folder_present`; keep sync-dir creation / `NoSyncDirError` / folder-structure tests.

- [ ] **Step 4: Run the file (no longer ignored)**

Run: `python -m pytest tests/offline/test_manager.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/offline/test_manager.py
git commit -m "test(stage-7a): un-ignore test_manager; drop path-root tests; Drive FullAccount"
```

---

### Task C2: fix `test_event_handler.py` `FileExists` errors

**Files:**
- Modify: `tests/offline/test_event_handler.py`

- [ ] **Step 1: Diagnose**

Run: `python -m pytest tests/offline/test_event_handler.py -q`
Expected: 5 ERRORs — `FileExistsError` from `new_dir.mkdir()` (lines 23, 55, 68, 90) because the fixture reuses a path across tests.

- [ ] **Step 2: Fix the fixture**

Make the directory creation idempotent + isolated: build the test dir under the test's own `tmp_path` (preferred) or change each `new_dir.mkdir()` to `new_dir.mkdir(exist_ok=True)` and ensure teardown removes it. Confirm the fixture that supplies the base directory is function-scoped, not session-scoped.

- [ ] **Step 3: Run the file**

Run: `python -m pytest tests/offline/test_event_handler.py -q`
Expected: PASS (0 errors)

- [ ] **Step 4: Commit**

```bash
git add tests/offline/test_event_handler.py
git commit -m "test(stage-7a): fix test_event_handler FileExists fixture errors"
```

---

### Task C3: full offline suite green with no `--ignore`

**Files:** none (verification + any CI invocation doc)

- [ ] **Step 1: Run the whole offline suite with no ignore**

Run: `python -m pytest tests/offline -q`
Expected: PASS — no `--ignore`, no errors. Confirm zero `dropbox` imports remain:

Run: `rg -n "import dropbox|from dropbox|DropboxClient" tests/`
Expected: only `tests/linked/...` remaining (handled in Phase D); `tests/offline` must be clean.

- [ ] **Step 2: Update the Stage 6 deviations command note**

In `docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db-deviations.md`, the line documenting `pytest tests/offline --ignore=tests/offline/test_manager.py` is now stale. Add a one-line note that Stage 7a removed the ignore (do not rewrite history — just annotate).

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db-deviations.md
git commit -m "docs(stage-7a): offline suite green with no --ignore; annotate Stage 6 note"
```

---

## Phase D — real-account linked suite

> The whole `tests/linked/` tree must skip cleanly when `CUMULUS_TEST_REFRESH_TOKEN` is unset. Build the gating + lock first, then port the tests.

### Task D1: linked conftest — env convention + `skipif` gating

**Files:**
- Modify: `tests/linked/conftest.py`, `tests/linked/unit/conftest.py`, `tests/linked/integration/conftest.py`

- [ ] **Step 1: Add a credential-presence gate**

In `tests/linked/conftest.py`, replace the Dropbox env vars and `DropboxClient`:

```python
import os
import pytest

from cumulus.client import GoogleDriveClient
from cumulus.config import remove_configuration
from cumulus.exceptions import AuthError
from cumulus.keyring import CredentialStorage

from .lock import DriveTestLock

TEST_CONFIG = "cumulus-test-config"  # distinct from the 'smoke-test' config
LOCK_NAME = ".cumulus-test.lock"

_HAS_CREDS = bool(os.environ.get("CUMULUS_TEST_REFRESH_TOKEN"))
requires_account = pytest.mark.skipif(
    not _HAS_CREDS, reason="set CUMULUS_TEST_REFRESH_TOKEN to run linked tests"
)


@pytest.fixture(scope="session", autouse=True)
def test_lock():
    if not _HAS_CREDS:
        pytest.skip("no CUMULUS_TEST_REFRESH_TOKEN")
    cred_storage = CredentialStorage(TEST_CONFIG)
    c = GoogleDriveClient(TEST_CONFIG, cred_storage)
    refresh_token = os.environ["CUMULUS_TEST_REFRESH_TOKEN"]
    access_token = os.environ.get("CUMULUS_TEST_ACCESS_TOKEN")
    res = c.link(refresh_token=refresh_token, access_token=access_token)
    if res > 0:
        raise AuthError(f"linking test account failed (code {res})")
    lock = DriveTestLock(c, LOCK_NAME)
    if not lock.acquire():
        raise TimeoutError("Could not acquire Drive test lock")
    yield lock
    lock.release()
    remove_configuration(TEST_CONFIG)
    cred_storage.delete_creds()
```

Apply the parallel `GoogleDriveClient`/env changes to `unit/conftest.py` and `integration/conftest.py` (drop `clean_dropbox_dir`'s Dropbox specifics; rename to `clean_drive_dir`, using `client.list_folder` + trash to clear the account between tests).

- [ ] **Step 2: Verify clean skip**

Run (no creds): `python -m pytest tests/linked -q`
Expected: all skipped, **zero errors**.

- [ ] **Step 3: Commit**

```bash
git add tests/linked/conftest.py tests/linked/unit/conftest.py tests/linked/integration/conftest.py
git commit -m "test(stage-7a): linked conftest — CUMULUS_TEST_REFRESH_TOKEN gate + skip"
```

---

### Task D2: Drive-native session lock (`tests/linked/lock.py`)

**Files:**
- Rewrite: `tests/linked/lock.py`

- [ ] **Step 1: Replace `DropboxTestLock` with `DriveTestLock`**

```python
"""Drive-native cross-session test lock.

Drive has no atomic create-exclusive, so acquisition is: list for the
lock file; if a non-stale lock exists, back off and retry; else create
one, re-list, and confirm we own the earliest-created instance.
"""

import time

from cumulus.client import GoogleDriveClient

_STALE_SECONDS = 600
_RETRY_SECONDS = 5
_MAX_WAIT_SECONDS = 300


class DriveTestLock:
    def __init__(self, client: GoogleDriveClient, lock_name: str):
        self._client = client
        self._name = lock_name
        self._lock_id: str | None = None

    def acquire(self) -> bool:
        deadline = _MAX_WAIT_SECONDS
        while deadline > 0:
            existing = self._find_locks()
            fresh = [r for r in existing if not self._is_stale(r)]
            if not fresh:
                self._purge(existing)
                created_id = self._create_lock()
                # re-list; we win only if ours is the earliest non-stale lock
                contenders = sorted(
                    (r for r in self._find_locks() if not self._is_stale(r)),
                    key=lambda r: (r["createdTime"], r["id"]),
                )
                if contenders and contenders[0]["id"] == created_id:
                    self._lock_id = created_id
                    return True
                # lost the race: drop ours and retry
                self._client.remove(file_id=created_id)
            time.sleep(_RETRY_SECONDS)
            deadline -= _RETRY_SECONDS
        return False

    def release(self) -> None:
        if self._lock_id:
            self._client.remove(file_id=self._lock_id)
            self._lock_id = None

    # ---- helpers (implement against the real client surface) ----
    def _find_locks(self) -> list[dict]:
        """Return raw Drive records for lock files in the account root.
        Use the client's low-level service to list by name == self._name."""
        svc = self._client._get_service()
        resp = svc.files().list(
            q=f"name = '{self._name}' and trashed = false",
            fields="files(id, name, createdTime)",
            pageSize=100,
        ).execute()
        return resp.get("files", [])

    def _create_lock(self) -> str:
        svc = self._client._get_service()
        resp = svc.files().create(
            body={"name": self._name}, fields="id"
        ).execute()
        return resp["id"]

    def _is_stale(self, rec: dict) -> bool:
        # createdTime is RFC-3339; a lock older than _STALE_SECONDS is abandoned
        from cumulus.changes import parse_drive_time
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc) - parse_drive_time(rec["createdTime"])).total_seconds()
        return age > _STALE_SECONDS

    def _purge(self, recs: list[dict]) -> None:
        for r in recs:
            if self._is_stale(r):
                try:
                    self._client.remove(file_id=r["id"])
                except Exception:
                    pass
```

> Verify `client.remove`'s signature accepts `file_id=` (confirmed kwarg-only in Stage 4, `524f50d`). If `remove` requires a path, use the low-level `svc.files().delete(fileId=...).execute()` instead.

- [ ] **Step 2: Smoke the lock logic offline against the fake (optional sanity)**

Add a quick offline test that drives `DriveTestLock` against a `FakeDrive`-backed client to confirm acquire/release create+delete the lock record. Run it; PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/linked/lock.py
git commit -m "test(stage-7a): Drive-native DriveTestLock (create/list/stale-purge)"
```

---

### Task D3: `wait_until` helper for change-feed latency

**Files:**
- Modify: `tests/linked/conftest.py`

- [ ] **Step 1: Add a bounded-retry polling helper**

```python
# in tests/linked/conftest.py
import time


def wait_until(predicate, timeout=60, interval=2):
    """Poll *predicate* until truthy or *timeout* seconds elapse.

    Drive's changes.list lags a few seconds behind a mutation, so linked
    assertions must poll rather than assert once."""
    deadline = timeout
    while deadline > 0:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
        deadline -= interval
    raise AssertionError(f"condition not met within {timeout}s")
```

Expose it as a fixture or import it directly in the linked tests.

- [ ] **Step 2: Commit**

```bash
git add tests/linked/conftest.py
git commit -m "test(stage-7a): wait_until bounded-retry helper for change-feed latency"
```

---

### Task D4: port `tests/linked/unit/test_client.py` to Drive; drop sharing

**Files:**
- Modify: `tests/linked/unit/test_client.py`

- [ ] **Step 1: Drop Dropbox-only tests**

Delete `test_share_dir_new`, `test_share_dir_existing`, `test_sharedlink_lifecycle`, `test_sharedlink_errors`, and the Dropbox-specific `upload_session` assertions. Add one test asserting the sharing stub raises `NotImplementedError` (confirm the actual stub behaviour in `client.py`/`writes.py`).

- [ ] **Step 2: Port the surviving tests to Drive shapes + `GoogleDriveClient`**

Keep upload (simple + resumable), download + md5 verify, `get_metadata`, `list_folder`, change feed, `move`, trash/restore, `make_dir` + batch. Mark each with `@requires_account`. Use `wait_until` for any change-feed assertion. Replace hash-mismatch tests that monkeypatched the Dropbox hasher with the MD5 `ContentHasher` equivalent.

- [ ] **Step 3: Verify clean skip without creds**

Run: `python -m pytest tests/linked/unit/test_client.py -q`
Expected: all skipped, no errors.

- [ ] **Step 4: Commit**

```bash
git add tests/linked/unit/test_client.py
git commit -m "test(stage-7a): port linked client tests to Drive; drop sharing tests"
```

---

### Task D5/D6: port `integration/test_sync.py` and `integration/test_main.py`

**Files:**
- Modify: `tests/linked/integration/test_sync.py`, `tests/linked/integration/test_main.py`

- [ ] **Step 1: Replace `from dropbox import files`**

Remove the Dropbox import; assert against Drive metadata shapes / local filesystem state instead.

- [ ] **Step 2: Port sync + main tests**

`test_sync.py`: end-to-end up/down sync, conflict resolution, move+rename — wrap remote-state assertions in `wait_until`. `test_main.py`: link/unlink, account info, sync-dir lifecycle. Mark all `@requires_account`.

- [ ] **Step 3: Verify clean skip**

Run: `python -m pytest tests/linked/integration -q`
Expected: all skipped, no errors.

- [ ] **Step 4: Commit**

```bash
git add tests/linked/integration/test_sync.py tests/linked/integration/test_main.py
git commit -m "test(stage-7a): port linked sync/main integration tests to Drive"
```

---

## Phase E — docs + final gate

### Task E1: `tests/README.md`

**Files:**
- Create: `tests/README.md`

- [ ] **Step 1: Write the README**

Cover: (a) running offline (`python -m pytest tests/offline` — no account, no secrets); (b) running linked (`export CUMULUS_TEST_REFRESH_TOKEN=…`, dedicated test account distinct from the smoke account, the 7-day Testing-mode token-expiry caveat, the Drive lock model); (c) the fake-Drive — its purpose, the six-point fidelity contract, why "green against fake" is trustworthy, and that the linked suite is the periodic ground-truth check; (d) the offline-vs-linked split rationale.

- [ ] **Step 2: Commit**

```bash
git add tests/README.md
git commit -m "docs(stage-7a): tests/README — offline vs linked, fake-Drive fidelity contract"
```

---

### Task E2: final verification + deviations record

**Files:**
- Create: `docs/superpowers/plans/2026-06-04-stage-7a-test-infrastructure-deviations.md`

- [ ] **Step 1: Run the full offline gate**

Run: `python -m pytest tests/offline -q`
Expected: PASS — no `--ignore`, no errors.

Run: `rg -n "import dropbox|from dropbox" tests/`
Expected: no matches anywhere in `tests/`.

Run (no creds): `python -m pytest tests/linked -q`
Expected: all skipped, zero errors.

- [ ] **Step 2: Real-account gate (manual, the §9.5 done criterion)**

With the provisioned test account: `export CUMULUS_TEST_REFRESH_TOKEN=…` then `python -m pytest tests/linked -q`. Record PASS/fail counts. This is the proof the fake is not lying. (If creds are unavailable at execution time, mark this task blocked and flag it to the user — do not claim the stage complete without it.)

- [ ] **Step 3: Write the deviations + smoke record**

Document any plan deviations, the offline/linked counts, the real-account result, and any fake-fidelity gaps discovered. Follow the format of `docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db-deviations.md`.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-06-04-stage-7a-test-infrastructure-deviations.md
git commit -m "docs(stage-7a): deviations + offline/linked verification record"
```

---

## Self-review checklist (run before execution)

- **Spec coverage:** E1 fake-Drive → Phase A (all resources incl. revisions). Offline rewire → Phase B. `test_manager`/`test_event_handler` → C1/C2. Real-account suite (account, env, lock, skipif, latency) → D1–D6. README → E1. Done-criteria gate → E2. Fault-injection seam → A1 `fail_next` (+ used in A4/A10/B4). The two spike regressions → B2 (parent-before-child) and B3 (new-file `NotFoundError`).
- **Spec gap fixed in plan:** the `revisions()` resource, omitted from spec §5.2's surface list, is modelled in Tasks A7/A8 — noted for the deviations record.
- **No placeholders:** every code step shows real code; test-rewrite tasks (B2–B4, C1, D4–D6) point at concrete files with the exact transformation rule and representative full examples rather than re-transcribing every existing test (the engineer has the originals in-tree).
- **Type consistency:** `FakeDrive`, `ROOT_ID`, `make_http_error`, `service()`, `fail_next`, `_Request`/`_ResumableRequest`/`_MediaRequest`/`_Batch`, `DriveTestLock`, `requires_account`, `wait_until` are named identically across all tasks.
