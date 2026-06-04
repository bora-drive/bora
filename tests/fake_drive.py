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

    def service(self) -> "_Service":
        return _Service(self)

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


# ---------------------------------------------------------------------------
# Resource tree
# ---------------------------------------------------------------------------


class _Request:
    """Mimics a googleapiclient HttpRequest: only ``.execute()`` is used."""

    def __init__(self, fail_key: str, fail_check: Callable[[str], None], fn: Callable[[], Any]):
        self._fail_key = fail_key
        self._fail_check = fail_check
        self._fn = fn

    def execute(self, num_retries: int = 0, http: Any = None) -> Any:
        self._fail_check(self._fail_key)
        return self._fn()


def _parse_field_keys(fields: str) -> list[str]:
    keys = []
    depth = 0
    token = ""
    for ch in fields:
        if ch == "(":
            depth += 1
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
    return [k for k in keys if k]


def _project(rec: dict, fields: str) -> dict:
    """Return only the requested top-level field keys from a record.

    Field strings are Drive selectors like ``"id, name, parents"`` or
    ``"lastModifyingUser(permissionId)"``; nested parens are returned whole.
    The fake stores every readable field, so projection is a key filter.
    """
    keys = _parse_field_keys(fields)
    out: dict = {}
    for key in keys:
        if key in rec:
            value = rec[key]
            if value is not None:
                out[key] = value
    return out


def _read_media(media) -> bytes:
    """Read all bytes from a googleapiclient MediaUpload (in-memory or file)."""
    return media.getbytes(0, media.size())


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


class _FilesResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive

    def get(self, fileId, fields="", **_):
        def run():
            rec = self._d.files.get(fileId)
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
            inner = (
                fields[fields.index("files(") + len("files(") : fields.rindex(")")]
                if "files(" in fields
                else "*"
            )
            resp = {"files": [_project(r, inner) for r in page]}
            if start + pageSize < len(matched):
                resp["nextPageToken"] = str(start + pageSize)
            return resp
        return _Request("files.list", self._d._maybe_fail, run)

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
                "md5Checksum": None if is_folder else hashlib.md5(content).hexdigest(),
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
                rec["md5Checksum"] = hashlib.md5(content).hexdigest()
                rec["size"] = len(content)
                rec["headRevisionId"] = self._d._new_rev()
                rec["revisions"].append(self._d._revision_dict(rec))
            self._d._record_change(fileId)
            return _project(rec, fields)
        return _Request("files.update", self._d._maybe_fail, run)


class _ChangesResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive

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


class _AboutResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive

    def get(self, fields="", **_):
        def run():
            return {"user": dict(self._d.user), "storageQuota": dict(self._d.storage_quota)}
        return _Request("about.get", self._d._maybe_fail, run)


class _RevisionsResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive

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
