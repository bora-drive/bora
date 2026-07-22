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
import requests
from googleapiclient.errors import HttpError

ROOT_ID = "ROOT"
FOLDER_MIME = "application/vnd.google-apps.folder"

# application/vnd.google-apps.* prefix shared by every native Workspace
# type (Docs, Sheets, Slides, Forms, ...) and the folder type. Kept as a
# plain string check here (rather than importing bora.native_docs) so the
# fake stays a pure API-shape double with no dependency on app code.
_NATIVE_PREFIX = "application/vnd.google-apps."

# add_native's kind -> Drive mimeType map (Task 8).
_NATIVE_KIND_MIME = {
    "document": "application/vnd.google-apps.document",
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "presentation": "application/vnd.google-apps.presentation",
    "form": "application/vnd.google-apps.form",
}


def _is_native_doc_mime(mime: str | None) -> bool:
    """True for a non-folder application/vnd.google-apps.* mimeType."""
    return bool(mime) and mime.startswith(_NATIVE_PREFIX) and mime != FOLDER_MIME


def make_http_error(status: int, reason: str = "", message: str = "error") -> HttpError:
    """Build a googleapiclient HttpError the same way the real API surfaces one."""
    resp = httplib2.Response({"status": status})
    resp.reason = message
    body: dict = {"error": {"code": status, "message": message}}
    if reason:
        body["error"]["errors"] = [{"reason": reason, "message": message}]
    return HttpError(resp, json.dumps(body).encode("utf-8"))


def make_timeout(message: str = "fake timeout") -> Exception:
    """A transport-level timeout, shaped like the production retry path catches.

    Returns ``requests.exceptions.Timeout`` because ``CONNECTION_ERRORS`` in
    ``src/bora/errorhandling.py`` includes that type and raw ``socket.timeout``
    (via ``builtins.TimeoutError``), so downstream retry tests will exercise
    the real retry path.
    """
    return requests.exceptions.Timeout(message)


def make_conn_reset(message: str = "connection reset by peer") -> Exception:
    """A connection-reset, shaped like requests surfaces one."""
    return requests.exceptions.ConnectionError(message)


def _now_rfc3339() -> str:
    return datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _web_view_link(fid: str, mime: str) -> str:
    """A realistic webViewLink for a record (folders use /drive/folders/)."""
    if mime == FOLDER_MIME:
        return f"https://drive.google.com/drive/folders/{fid}"
    return f"https://drive.google.com/file/d/{fid}/view"


def _owner_permission() -> dict[str, Any]:
    """The implicit owner permission every Drive item starts with."""
    return {
        "id": "perm-1",
        "type": "user",
        "role": "owner",
        "emailAddress": "bora-test@example.com",
    }


class FakeDrive:
    """In-memory Drive backing store + Resource-tree factory."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self._id_counter = 0
        self._rev_counter = 0
        self._perm_counter = 1  # "perm-1" is the implicit owner permission
        self._seq = 100  # opaque change cursor; ints rendered as str
        self._change_log: list[tuple[int, str]] = []  # (seq, fileId)
        self._fail_rules: list[dict] = []
        # Per-file-id download corruption directives (Task 6).
        # Maps file_id -> mode, where mode in {"truncate", "flip", "wrong_md5"}.
        # Consumed by get_media when building the _MediaRequest.
        self._corruption: dict[str, str] = {}
        # Bytes served by files().export_media, keyed by (file_id, export_mime).
        # Populated via set_export_bytes (Task 8).
        self._export_bytes: dict[tuple[str, str], bytes] = {}
        # One-shot hook fired at the start of the next files.update finalize,
        # before the update is applied. Used to deterministically interleave a
        # "collaborator" write inside the check-then-write race window
        # (cluster-E Task 5 / review F4).
        self.on_next_update: Callable[[], None] | None = None
        self.user = {
            "permissionId": "perm-1",
            "displayName": "Test User",
            "emailAddress": "bora-test@example.com",
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
            "webViewLink": _web_view_link(ROOT_ID, FOLDER_MIME),
            "permissions": [_owner_permission()],
        }

    # ---- id / rev / change bookkeeping ----
    def _new_id(self) -> str:
        self._id_counter += 1
        return f"file-{self._id_counter}"

    def _new_rev(self) -> str:
        self._rev_counter += 1
        return f"rev-{self._rev_counter}"

    def _new_perm_id(self) -> str:
        self._perm_counter += 1
        return f"perm-{self._perm_counter}"

    def _record_change(self, file_id: str, *, is_edit: bool = False) -> None:
        """Append a change-log entry for *file_id*.

        ``is_edit=True`` marks a server-side edit (files.update) rather than
        a creation/deletion; for a native Google-Workspace doc this bumps the
        Drive ``version`` counter (string int increment), letting change-feed
        tests simulate a collaborator editing a Doc/Sheet/Slide out-of-band
        (Task 8). Creation/deletion never bump: ``add_native`` seeds the
        starting ``version`` itself."""
        self._change_log.append((self._seq, file_id))
        self._seq += 1
        if is_edit:
            rec = self.files.get(file_id)
            # "version" in rec scopes the bump to records that opted in via
            # add_native — shortcuts and other google-apps.* mimeTypes match
            # _is_native_doc_mime but are created via _insert/create, which
            # never set "version", so they must not be touched here.
            if (
                rec is not None
                and "version" in rec
                and _is_native_doc_mime(rec.get("mimeType"))
            ):
                rec["version"] = str(int(rec["version"]) + 1)

    def fail_next(
        self,
        key: str,
        exc: Exception,
        *,
        count: int = 1,
        when: Callable[..., bool] | None = None,
    ) -> None:
        """Raise *exc* BEFORE each of the next *count* ``.execute()`` calls of
        *key* (optionally only when ``when(**call_kwargs)`` is true)."""
        self._fail_rules.append(
            {
                "key": key,
                "exc": exc,
                "skip": 0,
                "remaining": count,
                "when": when,
                "after": False,
            }
        )

    def fail_after(
        self,
        key: str,
        exc: Exception,
        *,
        count: int = 1,
        when: Callable[..., bool] | None = None,
    ) -> None:
        """Raise *exc* AFTER each of the next *count* ``.execute()`` calls of
        *key* commit (finalize runs first). Models "committed but response lost"."""
        self._fail_rules.append(
            {
                "key": key,
                "exc": exc,
                "skip": 0,
                "remaining": count,
                "when": when,
                "after": True,
            }
        )

    def fail_on_call(
        self,
        key: str,
        n: int,
        exc: Exception,
        *,
        when: Callable[..., bool] | None = None,
    ) -> None:
        """Raise *exc* on the *n*-th (1-based) matching ``.execute()`` of *key*,
        letting the first n-1 pass."""
        self._fail_rules.append(
            {
                "key": key,
                "exc": exc,
                "skip": n - 1,
                "remaining": 1,
                "when": when,
                "after": False,
            }
        )

    def fail_when(
        self, key: str, predicate: Callable[..., bool], exc: Exception
    ) -> None:
        """Raise *exc* on the next ``.execute()`` of *key* whose call kwargs
        satisfy ``predicate(**call_kwargs)``."""
        self._fail_rules.append(
            {
                "key": key,
                "exc": exc,
                "skip": 0,
                "remaining": 1,
                "when": predicate,
                "after": False,
            }
        )

    def fail_every(self, key: str, n: int, exc: Exception) -> dict:
        """Raise *exc* on every *n*-th matching ``.execute()`` of *key*,
        indefinitely. The first n-1 calls pass, the n-th fails, and so on.

        Returns the rule dict; read ``rule["fires"]`` for the live count of
        times it has fired (used to prove a storm actually hit the backoff
        path rather than silently never firing)."""
        if n < 1:
            raise ValueError("fail_every requires n >= 1")
        rule = {
            "key": key,
            "exc": exc,
            "every": n,
            "seen": 0,
            "fires": 0,
            "when": None,
            "after": False,
        }
        self._fail_rules.append(rule)
        return rule

    def _consume_fail(self, key: str, kwargs: dict, *, after: bool) -> None:
        """Fire the first matching, ready rule for (key, after-phase). At most
        one rule fires per call. A rule with a remaining ``skip`` count consumes
        the call toward its skip without firing. A recurring ``every`` rule fires
        on every n-th matching call and never auto-removes.
        Only the first matching ``every`` rule per (key, after-phase) is ever
        consulted."""
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

    def _maybe_fail(self, key: str, kwargs: dict | None = None) -> None:
        self._consume_fail(key, kwargs or {}, after=False)

    def _maybe_fail_after(self, key: str, kwargs: dict | None = None) -> None:
        self._consume_fail(key, kwargs or {}, after=True)

    def corrupt_download(self, file_id: str, *, mode: str) -> None:
        """Arm a single-use download corruption for *file_id*.

        ``mode`` must be one of:

        ``"truncate"``
            ``get_media`` serves one fewer byte than the original content.
            The file record's ``md5Checksum`` remains the original (correct)
            checksum, so the integrity check detects the mismatch.

        ``"flip"``
            ``get_media`` serves the original length but with the first byte
            flipped (XOR 0xFF).  The advertised ``md5Checksum`` is unchanged,
            so the integrity check detects the mismatch.

        ``"wrong_md5"``
            The bytes served are correct, but the file record's
            ``md5Checksum`` is replaced with a known-wrong value.  Models a
            server returning an inconsistent checksum while the bytes are fine.

        Note: ``truncate`` and ``flip`` are single-use (the corruption is
        consumed at the next ``get_media`` call).  ``wrong_md5`` mutates the
        record's ``md5Checksum`` permanently — a caller needing a clean
        subsequent download must restore
        ``fake.files[file_id]['md5Checksum']`` itself.
        """
        if mode not in ("truncate", "flip", "wrong_md5"):
            raise ValueError(f"Unknown corruption mode: {mode!r}")
        self._corruption[file_id] = mode
        if mode == "wrong_md5":
            # Corrupt the advertised checksum; leave served bytes intact.
            self.files[file_id]["md5Checksum"] = "deadbeef" * 4

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
            if (
                parent_id in rec["parents"]
                and rec["name"] == name
                and not rec["trashed"]
            ):
                return fid
        raise KeyError(f"no child {name!r} under {parent_id}")

    def id_of(self, path: str) -> str:
        """Return the file id at *path* (a plain, decoded path like
        ``/Docs/report.pdf``)."""
        parent_id, name = self._resolve_path_to_parent_and_name(path)
        return self._child_by_name(parent_id, name)

    def get_by_path(self, path: str) -> dict[str, Any]:
        """Return the raw backing-store record at *path*."""
        return self.files[self.id_of(path)]

    def add_folder(self, path: str) -> str:
        parent_id, name = self._resolve_path_to_parent_and_name(path)
        return self._insert(name, parent_id, FOLDER_MIME, b"")

    def add_shared_root(self, name: str, *, shared_with_me: bool = True) -> str:
        """Insert a folder owned by another user, parented outside the sync
        root, and shared with this account (a "Shared with me" attach
        point). Not reachable via ``_resolve_path_to_parent_and_name`` /
        the sync root's parent chain — only via ``sharedWithMe=true``
        queries, mirroring the real Drive semantics."""
        fid = self._insert(name, "OTHER_ROOT", FOLDER_MIME, b"")
        # Real Drive v3 has no "sharedWithMe" File field (only the query
        # term); direct shares carry sharedWithMeTime instead.
        if shared_with_me:
            self.files[fid]["sharedWithMeTime"] = "2026-01-01T00:00:00.000Z"
        self.files[fid]["ownedByMe"] = False
        self.files[fid]["capabilities"] = {"canEdit": False}
        return fid

    def add_file(
        self, path: str, content: bytes = b"", literal_name: str | None = None
    ) -> str:
        parent_id, name = self._resolve_path_to_parent_and_name(path)
        return self._insert(literal_name or name, parent_id, "text/plain", content)

    def _resolve_folder(self, path: str) -> str:
        """Return the file id of the folder at *path* (``"/"`` is the root)."""
        parts = [p for p in path.strip("/").split("/") if p]
        parent_id = ROOT_ID
        for seg in parts:
            parent_id = self._child_by_name(parent_id, seg)
        return parent_id

    def add_native(
        self, name: str, kind: str, parent: str = "/", version: str = "1"
    ) -> str:
        """Insert a native Google Workspace file (Doc/Sheet/Slides/Form).

        ``kind`` is one of ``"document"``, ``"spreadsheet"``,
        ``"presentation"``, ``"form"`` and is mapped to the matching
        ``application/vnd.google-apps.*`` mimeType. Native docs have no
        downloadable bytes in real Drive: ``md5Checksum`` and
        ``headRevisionId`` are ``None``, and ``content`` is empty — export
        bytes are registered separately via :meth:`set_export_bytes` and
        served through ``files().export_media``. ``version`` seeds the
        Drive version counter used as the export identity's ``rev`` (see
        ``changes.to_file_metadata``); :meth:`_record_change` bumps it on
        a subsequent ``files.update`` (Task 8)."""
        mime = _NATIVE_KIND_MIME.get(kind)
        if mime is None:
            raise ValueError(f"Unknown native kind: {kind!r}")
        parent_id = self._resolve_folder(parent)
        fid = self._new_id()
        rec = {
            "id": fid,
            "name": name,
            "parents": [parent_id],
            "mimeType": mime,
            "modifiedTime": _now_rfc3339(),
            "createdTime": _now_rfc3339(),
            "trashed": False,
            "shared": False,
            "md5Checksum": None,
            "size": None,
            "headRevisionId": None,
            "version": version,
            "content": b"",
            "revisions": [],
            "lastModifyingUser": {"permissionId": "perm-1"},
            "webViewLink": _web_view_link(fid, mime),
            "permissions": [_owner_permission()],
        }
        self.files[fid] = rec
        self._record_change(fid)
        return fid

    def set_export_bytes(self, file_id: str, export_mime: str, content: bytes) -> None:
        """Register the bytes *content* served by
        ``files().export_media(fileId=file_id, mimeType=export_mime)``."""
        self._export_bytes[(file_id, export_mime)] = content

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
            "webViewLink": _web_view_link(fid, mime),
            "permissions": [_owner_permission()],
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
            # Snapshot of the content at this revision; consumed by
            # revisions.get_media and stripped from revisions.list responses.
            "content": rec["content"],
        }


# ---------------------------------------------------------------------------
# Media-download helpers (Task A8)
# ---------------------------------------------------------------------------


class _FakeHttp:
    """Minimal httplib2-shaped transport for MediaIoBaseDownload.

    MediaIoBaseDownload calls ``http.request(uri, method, headers=...)``
    (via ``_retry_request``) and reads ``resp.status`` plus
    ``content-range`` / ``content-length`` off the response.
    """

    def __init__(
        self,
        content: bytes,
        fail_check: Callable[[str, dict], None] | None = None,
        fail_key: str = "files.get_media",
        fail_kwargs: dict | None = None,
    ):
        self._content = content
        self._fail_check = fail_check
        self._fail_key = fail_key
        self._fail_kwargs = fail_kwargs or {}

    def request(self, uri, method="GET", body=None, headers=None):
        if self._fail_check is not None:
            self._fail_check(self._fail_key, self._fail_kwargs)
        total = len(self._content)
        rng = (headers or {}).get("range") or (headers or {}).get("Range")
        if rng:
            spec = rng.split("=", 1)[1]
            begin_s, end_s = spec.split("-")
            begin, end = int(begin_s), int(end_s)
            chunk = self._content[begin : end + 1]
            resp = httplib2.Response(
                {
                    "status": 206,
                    "content-range": f"bytes {begin}-{begin + len(chunk) - 1}/{total}",
                    "content-length": str(len(chunk)),
                }
            )
        else:
            chunk = self._content
            resp = httplib2.Response({"status": 200, "content-length": str(total)})
        return resp, chunk


class _MediaRequest:
    """Mimics the HttpRequest that MediaIoBaseDownload wraps.

    ``MediaIoBaseDownload.__init__`` reads ``.uri``, ``.http``, and
    ``.headers``; ``next_chunk`` passes the range header to ``http.request``.
    """

    def __init__(
        self,
        content: bytes,
        fail_check: Callable[[str, dict], None] | None = None,
        fail_key: str = "files.get_media",
        fail_kwargs: dict | None = None,
    ):
        self.uri = "https://fake.drive/get_media"
        self.http = _FakeHttp(
            content,
            fail_check=fail_check,
            fail_key=fail_key,
            fail_kwargs=fail_kwargs,
        )
        self.headers: dict = {}
        self.postproc = lambda resp, content: content


# ---------------------------------------------------------------------------
# Resource tree
# ---------------------------------------------------------------------------


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

    def __init__(
        self,
        fail_key,
        fail_check,
        content: bytes,
        finalize,
        fail_after_check=None,
        fail_kwargs=None,
    ):
        self._fail_key = fail_key
        self._fail_check = fail_check
        self._content = content
        self._finalize = finalize
        self._fail_after_check = fail_after_check
        self._fail_kwargs = fail_kwargs or {}
        self._step = 0

    def next_chunk(self, num_retries: int = 0):
        self._fail_check(self._fail_key, self._fail_kwargs)
        total = len(self._content)
        if self._step == 0 and total > 0:
            self._step = 1
            half = max(1, total // 2)
            return _MediaUploadProgress(half, total), None
        response = self._finalize()
        if self._fail_after_check is not None:
            self._fail_after_check(self._fail_key, self._fail_kwargs)
        return _MediaUploadProgress(total, total), response


class _Request:
    """Mimics a googleapiclient HttpRequest: only ``.execute()`` is used."""

    def __init__(
        self,
        fail_key: str,
        fail_check: Callable[[str, dict], None],
        fn: Callable[[], Any],
        fail_after_check: Callable[[str, dict], None] | None = None,
        fail_kwargs: dict | None = None,
    ):
        self._fail_key = fail_key
        self._fail_check = fail_check
        self._fn = fn
        self._fail_after_check = fail_after_check
        self._fail_kwargs = fail_kwargs or {}

    def execute(self, num_retries: int = 0, http: Any = None) -> Any:
        self._fail_check(self._fail_key, self._fail_kwargs)
        result = self._fn()
        if self._fail_after_check is not None:
            self._fail_after_check(self._fail_key, self._fail_kwargs)
        return result


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
      "name = '<value>'"           -> name equality (Drive-escaped)
      "mimeType = '<value>'"       -> mimeType equality
      "sharedWithMe=true"          -> rec["sharedWithMe"] truthy
      "visibility='anyoneWithLink'" -> rec has an 'anyone' permission
    """
    if not query:
        return True
    for frag in query.split(" and "):
        frag = frag.strip()
        if frag in ("trashed=false", "trashed = false"):
            if rec["trashed"]:
                return False
        elif frag in (
            "visibility='anyoneWithLink'",
            "visibility = 'anyoneWithLink'",
        ):
            if not any(p.get("type") == "anyone" for p in rec.get("permissions", [])):
                return False
        elif frag in ("sharedWithMe=true", "sharedWithMe = true"):
            if not rec.get("sharedWithMeTime"):
                return False
        elif " in parents" in frag:
            pid = frag.split(" in parents")[0].strip().strip("'")
            if pid not in rec["parents"]:
                return False
        elif frag.startswith("name = '") and frag.endswith("'"):
            # Unescape Drive-style escapes: \' -> '  \\ -> \
            raw = frag[len("name = '") : -1]
            unescaped = raw.replace("\\'", "'").replace("\\\\", "\\")
            if rec["name"] != unescaped:
                return False
        elif frag.startswith("mimeType = '") and frag.endswith("'"):
            value = frag[len("mimeType = '") : -1]
            if rec["mimeType"] != value:
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

        return _Request(
            "files.get",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={"fileId": fileId, "fields": fields},
        )

    def list(self, q="", fields="", pageSize=100, pageToken=None, **_):
        def run():
            matched = [
                r
                for r in self._d.files.values()
                if r["id"] != ROOT_ID and _match_query(r, q)
            ]
            matched.sort(key=lambda r: r["id"])
            start = int(pageToken) if pageToken else 0
            page = matched[start : start + pageSize]
            inner = (
                fields[fields.index("files(") + len("files(") : fields.rindex(")")]
                if "files(" in fields
                else "*"
            )
            resp = {"files": [_project(r, inner) for r in page]}
            if start + pageSize < len(matched):
                resp["nextPageToken"] = str(start + pageSize)
            return resp

        return _Request(
            "files.list",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={
                "q": q,
                "pageToken": pageToken,
                "pageSize": pageSize,
                "fields": fields,
            },
        )

    def create(self, body=None, media_body=None, fields="", **_):
        def finalize():
            body_ = body or {}
            content = _read_media(media_body) if media_body is not None else b""
            mime = body_.get(
                "mimeType", "text/plain" if media_body is not None else FOLDER_MIME
            )
            # Honor a pre-generated id supplied in the body (Task 6 idempotency).
            # If the id already exists, raise 409 — a previous attempt committed.
            if body_.get("id"):
                fid = body_["id"]
                if fid in self._d.files:
                    raise make_http_error(
                        409, "conflict", "A file already exists with the provided ID."
                    )
            else:
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
                "webViewLink": _web_view_link(fid, mime),
                "permissions": [_owner_permission()],
            }
            if "shortcutDetails" in body_:
                rec["shortcutDetails"] = dict(body_["shortcutDetails"])
            if not is_folder:
                rec["revisions"].append(self._d._revision_dict(rec))
            self._d.files[fid] = rec
            self._d._record_change(fid)
            return _project(rec, fields)

        _resumable = (
            callable(getattr(media_body, "resumable", None)) and media_body.resumable()
        )
        _create_kwargs = {"body": body, "media_body": media_body}
        if media_body is not None and _resumable:
            return _ResumableRequest(
                "files.create",
                self._d._maybe_fail,
                _read_media(media_body),
                finalize,
                fail_after_check=self._d._maybe_fail_after,
                fail_kwargs=_create_kwargs,
            )
        return _Request(
            "files.create",
            self._d._maybe_fail,
            finalize,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs=_create_kwargs,
        )

    def delete(self, fileId, **_):
        def run():
            self._d.files.pop(fileId, None)
            self._d._record_change(fileId)
            return None

        return _Request(
            "files.delete",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={"fileId": fileId},
        )

    def generateIds(self, count=1, **_):
        def run():
            return {"ids": [self._d._new_id() for _ in range(count)]}

        return _Request(
            "files.generateIds",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={"count": count},
        )

    def get_media(self, fileId, **_):
        rec = self._d.files[fileId]
        content = rec["content"]
        mode = self._d._corruption.pop(fileId, None)
        if mode == "truncate":
            # Serve one fewer byte; the record's md5Checksum is unchanged (correct
            # for the original bytes), so the integrity check detects the mismatch.
            content = content[:-1] if content else content
        elif mode == "flip":
            # Flip the first byte; same length, different bytes.
            if content:
                content = bytes([content[0] ^ 0xFF]) + content[1:]
        # "wrong_md5" leaves bytes intact; corruption is in the record md5Checksum
        # (already applied by corrupt_download).
        return _MediaRequest(
            content,
            fail_check=self._d._maybe_fail,
            fail_key="files.get_media",
            fail_kwargs={"fileId": fileId},
        )

    def export_media(self, fileId, mimeType, **_):
        """Mirror get_media, but serves bytes registered via
        set_export_bytes for a native doc's (fileId, mimeType) pair."""
        content = self._d._export_bytes.get((fileId, mimeType))
        if content is None:
            raise make_http_error(
                404,
                "notFound",
                f"No export bytes registered for {fileId!r} at {mimeType!r}",
            )
        return _MediaRequest(
            content,
            fail_check=self._d._maybe_fail,
            fail_key="files.export_media",
            fail_kwargs={"fileId": fileId, "mimeType": mimeType},
        )

    def update(
        self,
        fileId,
        body=None,
        media_body=None,
        addParents=None,
        removeParents=None,
        fields="",
        **_,
    ):
        def finalize():
            hook = self._d.on_next_update
            if hook is not None:
                self._d.on_next_update = None
                hook()
            rec = self._d.files[fileId]
            body_ = body or {}
            if "name" in body_:
                rec["name"] = body_["name"]
            if "trashed" in body_:
                rec["trashed"] = body_["trashed"]
            if "modifiedTime" in body_:
                rec["modifiedTime"] = body_["modifiedTime"]
            if "shortcutDetails" in body_:
                rec.setdefault("shortcutDetails", {}).update(body_["shortcutDetails"])
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
            self._d._record_change(fileId, is_edit=True)
            return _project(rec, fields)

        _resumable = (
            callable(getattr(media_body, "resumable", None)) and media_body.resumable()
        )
        _update_kwargs = {
            "fileId": fileId,
            "body": body,
            "addParents": addParents,
            "removeParents": removeParents,
        }
        if media_body is not None and _resumable:
            return _ResumableRequest(
                "files.update",
                self._d._maybe_fail,
                _read_media(media_body),
                finalize,
                fail_after_check=self._d._maybe_fail_after,
                fail_kwargs=_update_kwargs,
            )
        return _Request(
            "files.update",
            self._d._maybe_fail,
            finalize,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs=_update_kwargs,
        )


class _ChangesResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive

    def getStartPageToken(self, **_):
        def run():
            return {"startPageToken": str(self._d._seq)}

        return _Request(
            "changes.getStartPageToken",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={},
        )

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
                if "changes(" in fields
                else "*"
            )
            file_inner = (
                inner.split("file(", 1)[1].rsplit(")", 1)[0]
                if "file(" in inner
                else "*"
            )
            changes = []
            for fid, _seq in page:
                rec = self._d.files.get(fid)
                if rec is None:
                    changes.append({"removed": True, "fileId": fid})
                else:
                    changes.append(
                        {
                            "removed": False,
                            "fileId": fid,
                            "file": _project(rec, file_inner),
                        }
                    )
            resp: dict = {"changes": changes}
            if rest:
                resp["nextPageToken"] = str(rest[0][1])
            else:
                resp["newStartPageToken"] = str(self._d._seq)
            return resp

        return _Request(
            "changes.list",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={
                "pageToken": pageToken,
                "pageSize": pageSize,
                "includeRemoved": includeRemoved,
            },
        )


class _AboutResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive

    def get(self, fields="", **_):
        def run():
            return {
                "user": dict(self._d.user),
                "storageQuota": dict(self._d.storage_quota),
            }

        return _Request(
            "about.get",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={},
        )


class _RevisionsResource:
    def __init__(self, drive: "FakeDrive"):
        self._d = drive

    def list(self, fileId, fields="", pageSize=100, pageToken=None, **_):
        def run():
            revs = self._d.files[fileId]["revisions"]
            start = int(pageToken) if pageToken else 0
            # Oldest-first like the real API; strip the fake-internal
            # content snapshot from the wire response.
            page = [
                {k: v for k, v in r.items() if k != "content"}
                for r in revs[start : start + pageSize]
            ]
            resp = {"revisions": page}
            if start + pageSize < len(revs):
                resp["nextPageToken"] = str(start + pageSize)
            return resp

        return _Request(
            "revisions.list",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={"fileId": fileId},
        )

    def get_media(self, fileId, revisionId, **_):
        rec = self._d.files[fileId]
        for rev in rec["revisions"]:
            if rev["id"] == revisionId:
                return _MediaRequest(
                    rev["content"],
                    fail_check=self._d._maybe_fail,
                    fail_key="revisions.get_media",
                    fail_kwargs={"fileId": fileId, "revisionId": revisionId},
                )
        raise make_http_error(404, "notFound", "Revision not found")


class _PermissionsResource:
    """Minimal permissions() resource: create / list / delete.

    Backing store: each file record carries a ``permissions`` list seeded
    with the implicit owner permission. Adding any non-owner permission sets
    the record's ``shared`` flag; removing the last one clears it — matching
    how Drive derives ``shared`` from the permission set.
    """

    def __init__(self, drive: "FakeDrive"):
        self._d = drive

    def _rec(self, fileId: str) -> dict:
        rec = self._d.files.get(fileId)
        if rec is None:
            raise make_http_error(404, "notFound", "File not found")
        return rec

    @staticmethod
    def _update_shared_flag(rec: dict) -> None:
        rec["shared"] = any(
            p.get("role") != "owner" for p in rec.get("permissions", [])
        )

    def create(self, fileId, body=None, fields="", **_):
        def run():
            rec = self._rec(fileId)
            body_ = body or {}
            perm = {
                "id": self._d._new_perm_id(),
                "type": body_.get("type"),
                "role": body_.get("role"),
            }
            if body_.get("emailAddress"):
                perm["emailAddress"] = body_["emailAddress"]
            rec.setdefault("permissions", [_owner_permission()]).append(perm)
            self._update_shared_flag(rec)
            self._d._record_change(fileId)
            return _project(perm, fields) if fields else dict(perm)

        return _Request(
            "permissions.create",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={"fileId": fileId, "body": body},
        )

    def list(self, fileId, fields="", pageSize=100, pageToken=None, **_):
        def run():
            rec = self._rec(fileId)
            perms = rec.setdefault("permissions", [_owner_permission()])
            start = int(pageToken) if pageToken else 0
            page = perms[start : start + pageSize]
            inner = (
                fields[
                    fields.index("permissions(")
                    + len("permissions(") : fields.rindex(")")
                ]
                if "permissions(" in fields
                else ""
            )
            resp = {
                "permissions": [_project(p, inner) if inner else dict(p) for p in page]
            }
            if start + pageSize < len(perms):
                resp["nextPageToken"] = str(start + pageSize)
            return resp

        return _Request(
            "permissions.list",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={"fileId": fileId, "pageToken": pageToken},
        )

    def delete(self, fileId, permissionId, **_):
        def run():
            rec = self._rec(fileId)
            perms = rec.get("permissions", [])
            for i, perm in enumerate(perms):
                if perm["id"] == permissionId:
                    perms.pop(i)
                    break
            else:
                raise make_http_error(404, "notFound", "Permission not found")
            self._update_shared_flag(rec)
            self._d._record_change(fileId)
            return None

        return _Request(
            "permissions.delete",
            self._d._maybe_fail,
            run,
            fail_after_check=self._d._maybe_fail_after,
            fail_kwargs={"fileId": fileId, "permissionId": permissionId},
        )


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

    def permissions(self):
        return _PermissionsResource(self._d)

    def new_batch_http_request(self):
        return _Batch()
