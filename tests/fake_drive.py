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
