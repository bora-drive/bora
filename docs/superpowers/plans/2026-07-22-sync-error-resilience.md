# Sync Error Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A socket timeout no longer pauses sync permanently; files Drive refuses to export stop burning API calls every cycle; too-large exports actually sync via the docs.google.com endpoint; download-disabled files get a `.url` placeholder locally.

**Architecture:** Four independent fixes layered onto the existing error pipeline: (1) widen `CONNECTION_ERRORS` so httplib2-level socket errors classify as retryable/disconnect instead of fatal; (2) record the remote rev on `RestrictedContentError` sync errors and skip re-downloads while the rev is unchanged; (3) catch `exportSizeLimitExceeded` inside `_download_export` and stream from `https://docs.google.com/<kind>/d/<id>/export` instead; (4) a new `DownloadDisabledError` subclass triggers writing an `[InternetShortcut]` `.url` placeholder, guarded from upload by a check next to the existing `download_only` mirror guard.

**Tech Stack:** Python 3.10+, googleapiclient/httplib2, requests, sqlite (bora's own ORM), pytest with the `FakeDrive` offline harness.

**Spec:** `docs/superpowers/specs/2026-07-22-sync-error-resilience-design.md` (fallback endpoint live-verified 2026-07-22: 403-too-large via API, HTTP 200 with full 23.6 MB via `docs.google.com/presentation/d/<id>/export?format=pptx`; `format=md` works and is byte-identical to the API's markdown export).

## Global Constraints

- Commit as borabot with NO Co-Authored-By trailer (repo-local git config already set).
- `bin/test` and `bin/lint` must pass at the end of every task. No PRs — leave commits on `main`.
- DB schema policy: tables are derived caches. `Manager._guard_schema` auto-drops any table whose on-disk columns differ from the model (`src/bora/database/orm.py:253`) — adding a column to a model requires NO migration code.
- Flaky-test policy: rerun `tests/offline` failures in isolation before blaming a change; remove leftover `~/…_dummy_dir` dirs if offline cascades appear.
- The docs.google.com endpoint is unofficial: any failure there must degrade to the existing `RestrictedContentError` path, never crash.

---

### Task 1: Classify socket-level errors as connection errors

The crash from the field log: `TimeoutError` (SSL read timeout under httplib2) escapes `_retry_transient` (`src/bora/client.py:450`), is mapped by `convert_api_errors`'s generic `except OSError` branch to `BoraApiError`, and hits the manager's catch-all (`src/bora/manager.py:611`) which stops the daemon WITHOUT re-arming autostart. Widening `CONNECTION_ERRORS` fixes retry, mapping, and manager behavior in one move — `manager.py` already handles bora's `ConnectionError` correctly at `manager.py:605` (DISCONNECTED + autostart).

**Files:**
- Modify: `src/bora/errorhandling.py:52` (CONNECTION_ERRORS tuple + imports)
- Modify: `tests/fake_drive.py:52` (stale docstring on `make_timeout`)
- Test: `tests/offline/test_errorhandling.py`

**Interfaces:**
- Produces: `CONNECTION_ERRORS` additionally contains `builtins.TimeoutError`, `http.client.HTTPException`, `socket.gaierror`, `httplib2.HttpLib2Error`. Tasks 2–4 rely on transport errors NOT surfacing as `BoraApiError`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/offline/test_errorhandling.py` (match its existing imports/style; it already imports `convert_api_errors` and bora's `ConnectionError` — extend imports as needed):

```python
import http.client
import socket

import httplib2
import pytest

from bora.errorhandling import CONNECTION_ERRORS, convert_api_errors
from bora.exceptions import ConnectionError


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("The read operation timed out"),
        socket.gaierror(8, "nodename nor servname provided"),
        http.client.IncompleteRead(b"partial"),
        httplib2.ServerNotFoundError("Unable to find the server"),
    ],
)
def test_socket_level_errors_are_connection_errors(exc):
    # Classification: the retry loop keys off this tuple.
    assert isinstance(exc, CONNECTION_ERRORS)
    # Conversion: convert_api_errors maps them to bora's ConnectionError,
    # which the manager handles with DISCONNECTED + autostart (not a fatal
    # BoraApiError pause).
    with pytest.raises(ConnectionError):
        with convert_api_errors():
            raise exc


def test_retry_transient_retries_timeout(client):
    # A TimeoutError on the first attempt must be retried, not raised.
    # `client` is the offline conftest fixture; neutralise backoff sleep.
    from unittest.mock import Mock

    client._poll_event = Mock()
    client._poll_event.is_set = Mock(return_value=False)
    client._poll_event.wait = Mock(return_value=False)

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("The read operation timed out")
        return "ok"

    assert client._retry_transient(flaky) == "ok"
    assert calls["n"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/offline/test_errorhandling.py -k "socket_level or retries_timeout" -v`
Expected: FAIL — `isinstance` assertion fails for all four exceptions; the retry test raises `TimeoutError`.

- [ ] **Step 3: Widen CONNECTION_ERRORS**

In `src/bora/errorhandling.py`, add imports near the existing `import builtins` block:

```python
import http.client
import socket

import httplib2
```

Replace the tuple at line 52:

```python
CONNECTION_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.RetryError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    builtins.ConnectionError,
    # googleapiclient performs I/O through httplib2, not requests, so
    # transport failures also surface as raw socket / http.client /
    # httplib2 exceptions (found live 2026-07-21: an SSL read timeout
    # escaped retry, mapped to a fatal BoraApiError, and paused sync
    # without autostart).
    builtins.TimeoutError,  # also covers socket.timeout on Python >= 3.10
    http.client.HTTPException,  # IncompleteRead, BadStatusLine, ...
    socket.gaierror,  # DNS failure while offline
    httplib2.HttpLib2Error,  # ServerNotFoundError, ...
)
```

Ordering note: `convert_api_errors` catches `CONNECTION_ERRORS` BEFORE its `except OSError` branch (`errorhandling.py:248-261`), so `TimeoutError`/`gaierror` (both OSError subclasses) now map to `ConnectionError` instead of `os_to_bora_error`. Local filesystem operations wrapped by the same context manager never raise these types, so no behavior change for genuine local-FS errors.

Also fix the now-stale docstring of `make_timeout` in `tests/fake_drive.py:52` (it currently says CONNECTION_ERRORS does not include `socket.timeout`); keep it returning `requests.exceptions.Timeout`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/offline/test_errorhandling.py tests/offline/test_fault_download.py tests/offline/test_fault_ratelimit.py -v`
Expected: PASS (including pre-existing retry tests).

- [ ] **Step 5: Run the full offline suite and lint, then commit**

Run: `bin/test && bin/lint`
Expected: green (rerun any known-flaky failures in isolation per memory).

```bash
git add src/bora/errorhandling.py tests/fake_drive.py tests/offline/test_errorhandling.py
git commit -m "errorhandling: classify httplib2-level socket errors as connection errors"
```

---

### Task 2: Skip persistently-unexportable files until the remote changes

`startup_worker` (`src/bora/manager.py:526`) re-attempts every recorded download error on every cycle. For `RestrictedContentError` (export refused / too large) this can never succeed until the remote file changes. Record the remote rev with the error and skip download attempts while it is unchanged. For exportable native docs, `FileMetadata.rev` is already Drive's monotonic `version` counter (fallback `modifiedTime`, see `src/bora/changes.py:121-129`), so a rev comparison is exactly "the remote changed".

**Files:**
- Modify: `src/bora/models.py` (SyncErrorEntry, ~line 624)
- Modify: `src/bora/sync.py` (`_handle_sync_error` ~1898, `_create_local_entry` ~4244, module-level constant)
- Modify: `src/bora/manager.py` (startup_worker ~522)
- Test: `tests/offline/test_sync.py` (or a new `tests/offline/test_skip_restricted.py` if test_sync.py has no FakeDrive download-cycle tests to sit beside)

**Interfaces:**
- Consumes: Task 1 (transport errors no longer masquerade as sync errors).
- Produces: `SyncErrorEntry.remote_rev: str | None` column; `SyncEngine._handle_sync_error(err, direction, remote_rev=None)` keyword; module constant `SKIP_UNTIL_CHANGE_TYPES: frozenset[str]` in `src/bora/sync.py` (Task 4 extends its contents). Skip behavior: `_create_local_entry` returns the event with `SyncStatus.Skipped` without touching the network when a recorded restricted error matches the incoming rev.

- [ ] **Step 1: Write the failing test**

Add to `tests/offline/test_sync.py`, next to the export-mirror tests (~line 4609), using the established idioms there: the `mt_sync_engine` fixture, `FakeDrive()` + `_wire_export_cycle(engine, fake)`, `fake.add_native(name=..., kind="document")`, version bumps via `fake._record_change(doc, is_edit=True)`, and failure injection `fake.fail_next("files.export_media", exc, count=N)` (the fake decrements the rule's `remaining` on every attempted call, which doubles as an attempt counter).

```python
def test_restricted_error_skipped_until_rev_changes(
    mt_sync_engine: SyncEngine,
) -> None:
    """A cannotExportFile refusal is recorded with the remote rev and the
    file is not re-downloaded until the rev changes."""
    fake = FakeDrive()
    _wire_export_cycle(mt_sync_engine, fake)

    doc = fake.add_native(name="Report", kind="document")
    fake.set_export_bytes(doc, _MD_MIME, b"# Report v1\n")
    # Every export attempt is refused; `remaining` doubles as a counter.
    fake.fail_next(
        "files.export_media",
        make_http_error(403, "cannotExportFile"),
        count=999,
    )
    attempts = lambda: 999 - fake._fail_rules[0]["remaining"]  # noqa: E731

    mt_sync_engine.download_sync_cycle()
    errors = mt_sync_engine.download_errors
    assert len(errors) == 1
    assert errors[0].type in ("RestrictedContentError", "DownloadDisabledError")
    assert errors[0].remote_rev == "1"
    first = attempts()
    assert first >= 1

    # Same rev -> the retry pass must not attempt another export.
    mt_sync_engine.get_remote_item(errors[0].remote_path_lower)
    assert attempts() == first

    # Remote edit bumps `version` -> retry for real.
    fake._record_change(doc, is_edit=True)
    mt_sync_engine.get_remote_item(errors[0].remote_path_lower)
    assert attempts() > first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/offline -k restricted_error_skipped -v`
Expected: FAIL — `remote_rev` attribute does not exist / second `get_remote_item` re-attempts the export.

- [ ] **Step 3: Add the column and the skip logic**

`src/bora/models.py`, inside `SyncErrorEntry` after `type`:

```python
    remote_rev = Column(SqlString())
    """Remote rev (Drive ``version`` for native docs) at the time the error
    was recorded. Set only for restricted-content download errors; used to
    skip pointless re-downloads until the remote file changes. Nullable —
    schema drift on existing DBs is handled by Manager._guard_schema's
    drop-and-recreate (derived cache)."""
```

`src/bora/sync.py`, module level near the other constants:

```python
# Sync-error types that cannot succeed until the remote file itself
# changes (export refused, export too large). Download retries for these
# are skipped while the recorded remote_rev is unchanged.
SKIP_UNTIL_CHANGE_TYPES = frozenset({"RestrictedContentError"})
```

`_handle_sync_error` (sync.py:1898) — new keyword, stored on the entry:

```python
    def _handle_sync_error(
        self,
        err: SyncError,
        direction: SyncDirection,
        remote_rev: str | None = None,
    ) -> None:
```

and in the `SyncErrorEntry(...)` constructor call (~line 1959) add `remote_rev=remote_rev,`.

Call site `_create_local_entry` (~line 4275): pass the rev only for restricted errors —

```python
        except SyncError as e:
            rev_for_skip = (
                event.rev
                if type(e).__name__ in SKIP_UNTIL_CHANGE_TYPES
                else None
            )
            self._handle_sync_error(
                e, direction=SyncDirection.Down, remote_rev=rev_for_skip
            )
            event.status = SyncStatus.Failed
```

(Use `type(e).__name__` so Task 4's subclass participates automatically once added to the set. The other `_handle_sync_error` call sites — sync.py:2741 up-direction, sync.py:3493 folder-level — keep the default `None`.)

Skip check in `_create_local_entry`, immediately after `self._slow_down()` and before `event.status = SyncStatus.Syncing`:

```python
        if event.is_file and not event.is_deleted and event.rev:
            with self._database_access():
                err_entry = self._sync_errors_table.get(event.remote_path_lower)
            if (
                err_entry is not None
                and err_entry.direction == SyncDirection.Down
                and err_entry.type in SKIP_UNTIL_CHANGE_TYPES
                and err_entry.remote_rev is not None
                and err_entry.remote_rev == event.rev
            ):
                self._logger.debug(
                    'Skipping "%s": Google Drive still refuses rev %s',
                    event.remote_path,
                    event.rev,
                )
                event.status = SyncStatus.Skipped
                return event
```

The early return deliberately bypasses `clear_sync_errors_from_event` (which only runs on the success path) so the error entry — and the skip — survive until the rev changes.

`src/bora/manager.py` startup_worker (~line 522) — one INFO summary instead of per-file noise:

```python
            # Retry failed downloads.
            errors = list(self.sync.download_errors)
            restricted = [e for e in errors if e.type in SKIP_UNTIL_CHANGE_TYPES]
            if restricted:
                self._logger.info(
                    "%s file(s) cannot be exported from Google Drive; "
                    "retrying only when they change remotely.",
                    len(restricted),
                )
            if errors:
                self._logger.info("Retrying failed syncs...")

            for error in errors:
                self.sync.get_remote_item(error.remote_path_lower)
```

Import `SKIP_UNTIL_CHANGE_TYPES` from `.sync` in manager.py.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/offline -k restricted_error_skipped -v`
Expected: PASS.

- [ ] **Step 5: Full suite, lint, commit**

Run: `bin/test && bin/lint`

```bash
git add src/bora/models.py src/bora/sync.py src/bora/manager.py tests/offline/
git commit -m "sync: retry restricted-content downloads only when the remote file changes"
```

---

### Task 3: Too-large exports — docs.google.com fallback

Live-verified (2026-07-22): with the API bearer token, `GET https://docs.google.com/{document|spreadsheets|presentation}/d/<id>/export?format=<ext>` returns the full export with no ~10 MB cap (23.6 MB pptx OK, valid file; `format=md` byte-identical to API markdown export). Wire it in as the `exportSizeLimitExceeded` fallback.

**Files:**
- Modify: `src/bora/client.py` (`_download_export` ~882, new `_export_via_web` method, module constant, imports)
- Test: `tests/offline/test_fault_download.py` (or sibling new file `tests/offline/test_export_fallback.py`)

**Interfaces:**
- Consumes: `extract_http_reason` from `.errorhandling` (add to the existing `from .errorhandling import (...)` block at client.py:60); `RestrictedContentError` from `.exceptions` (add to the block at client.py:67 if absent); `export_target` already imported/used at client.py:899.
- Produces: `_download_export` transparently falls back; on fallback failure raises `RestrictedContentError` ("Cannot export file. This file is too large for Google Drive to export.") so Task 2's skip machinery takes over. No public API change.

- [ ] **Step 1: Write the failing tests**

Use the client-on-fake pattern from `tests/offline/test_fault_download.py` (its `cof` fixture: FakeDrive + neutralised `_poll_event`; enable exports with `client.export_opts = ExportOptions(enabled=True, markdown=True)` and stub credentials with `client._credentials = Mock(valid=True, token="fake-token")`). Failure injection key is `"files.export_media"` via `fake.fail_next(key, exc, count=999)`. Monkeypatch `bora.client.requests.get` for the web endpoint:

```python
import io

from bora.exceptions import RestrictedContentError
from tests.fake_drive import make_http_error


class _FakeResp:
    def __init__(self, status_code, content=b"", ctype="application/octet-stream"):
        self.status_code = status_code
        self._content = content
        self.headers = {"content-type": ctype}

    def iter_content(self, chunk_size):
        buf = io.BytesIO(self._content)
        while True:
            chunk = buf.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_too_large_export_falls_back_to_web(cof, monkeypatch, tmp_path):
    client, fake = cof
    payload = b"x" * (3 * 1024 * 1024)
    doc_id = fake.add_native(name="big", kind="presentation")
    # API export always refuses with the too-large reason.
    fake.fail_next(
        "files.export_media",
        make_http_error(403, "exportSizeLimitExceeded"),
        count=999,
    )
    seen = {}

    def fake_get(url, headers=None, stream=None, timeout=None, **kw):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization", "")
        return _FakeResp(200, payload)

    monkeypatch.setattr("bora.client.requests.get", fake_get)
    local = str(tmp_path / "big.pptx")

    md = client.download("/big.pptx", local, file_id=doc_id)

    assert open(local, "rb").read() == payload
    assert md.size == len(payload)
    assert seen["url"] == (
        f"https://docs.google.com/presentation/d/{doc_id}/export?format=pptx"
    )
    assert seen["auth"].startswith("Bearer ")


def test_web_fallback_non_200_raises_restricted(cof, monkeypatch, tmp_path):
    client, fake = cof
    doc_id = fake.add_native(name="big", kind="presentation")
    fake.fail_next(
        "files.export_media",
        make_http_error(403, "exportSizeLimitExceeded"),
        count=999,
    )
    monkeypatch.setattr(
        "bora.client.requests.get", lambda *a, **kw: _FakeResp(403)
    )
    local = str(tmp_path / "big.pptx")

    import pytest

    with pytest.raises(RestrictedContentError):
        client.download("/big.pptx", local, file_id=doc_id)
    # sidecar cleaned up
    import os.path as osp

    assert not osp.exists(local + ".bora-part")


def test_other_export_403_does_not_fall_back(cof, monkeypatch, tmp_path):
    client, fake = cof
    doc_id = fake.add_native(name="doc", kind="document")
    fake.fail_next(
        "files.export_media",
        make_http_error(403, "cannotExportFile"),
        count=999,
    )
    called = {"n": 0}

    def fake_get(*a, **kw):
        called["n"] += 1
        return _FakeResp(200, b"nope")

    monkeypatch.setattr("bora.client.requests.get", fake_get)

    import pytest

    with pytest.raises(RestrictedContentError):
        client.download("/doc.md", str(tmp_path / "doc.md"), file_id=doc_id)
    assert called["n"] == 0
```

(If `cof` in `test_fault_download.py` doesn't already enable exports/credentials, do it inside each test as noted above rather than changing the shared fixture.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/offline -k "falls_back_to_web or non_200_raises or does_not_fall_back" -v`
Expected: FAIL — `RestrictedContentError` raised in the first test (no fallback exists yet).

- [ ] **Step 3: Implement the fallback**

`src/bora/client.py`, module level near `_DOWNLOAD_CHUNK_SIZE`:

```python
# Browser-facing export endpoints. The Drive API's files.export caps
# exports at ~10 MB (403 exportSizeLimitExceeded); these endpoints — the
# route the Drive web UI itself uses — serve the same bytes with no such
# cap and accept the same OAuth bearer token (live-verified 2026-07-22,
# 23.6 MB pptx + markdown byte-identical to the API export). Unofficial:
# every failure here must degrade to RestrictedContentError, never crash.
_WEB_EXPORT_KIND = {
    "application/vnd.google-apps.document": "document",
    "application/vnd.google-apps.spreadsheet": "spreadsheets",
    "application/vnd.google-apps.presentation": "presentation",
}
```

Restructure `_download_export` (client.py:882) so the API chunk loop can be swapped for the web fallback while keeping the shared sidecar/hash/rename tail:

```python
    def _download_export(
        self,
        meta_file: dict[str, Any],
        mime: str,
        file_id: str,
        remote_path: str,
        local_path: str,
        tmp_path: str,
        sync_event: SyncEvent | None,
    ) -> FileMetadata:
        """Downloads an exportable native doc via ``files().export_media``.

        Mirrors the ``get_media`` path's chunked-download / sidecar /
        atomic-rename structure, but there is no server-side md5 for an
        export, so the content hash is computed locally from the
        downloaded bytes instead of being checked against Drive.

        Exports over the API's ~10 MB cap (403 exportSizeLimitExceeded)
        are retried once through the browser-facing docs.google.com
        export endpoint, which has no such cap (see _WEB_EXPORT_KIND).
        """
        export_mime, ext = export_target(mime, self.export_opts.markdown)
        request = (
            self._get_service()
            .files()
            .export_media(fileId=file_id, mimeType=export_mime)
        )
        try:
            try:
                with open(tmp_path, "wb") as fh:
                    downloader = MediaIoBaseDownload(
                        fh, request, chunksize=_DOWNLOAD_CHUNK_SIZE
                    )
                    done = False
                    while not done:
                        status, done = self._retry_transient(
                            lambda: downloader.next_chunk()
                        )
                        if sync_event is not None and status is not None:
                            sync_event.completed = status.resumable_progress
                        self._throttle_down(_DOWNLOAD_CHUNK_SIZE)
            except HttpError as exc:
                if extract_http_reason(exc) != "exportSizeLimitExceeded":
                    raise
                self._export_via_web(
                    file_id, mime, ext, remote_path, local_path,
                    tmp_path, sync_event,
                )
            self._count_request()

            hasher = hashlib.md5(usedforsecurity=False)
            with open(tmp_path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    hasher.update(block)
            os.replace(tmp_path, local_path)
        except BaseException:
            with suppress(OSError):
                os.remove(tmp_path)
            raise
        md = to_file_metadata(meta_file, remote_path, export_opts=self.export_opts)
        md.content_hash = hasher.hexdigest()
        md.size = os.path.getsize(local_path)
        return md
```

New method below it:

```python
    def _export_via_web(
        self,
        file_id: str,
        mime: str,
        ext: str,
        remote_path: str,
        local_path: str,
        tmp_path: str,
        sync_event: SyncEvent | None,
    ) -> None:
        """Stream an export through the docs.google.com endpoint into
        *tmp_path*. Fallback for exports over the API's size cap; raises
        RestrictedContentError on any non-200 so the restricted-content
        skip machinery takes over (the endpoint is unofficial)."""
        kind = _WEB_EXPORT_KIND[mime]
        creds = self._credentials
        if creds is None or not creds.valid:
            if creds is None or creds.refresh_token is None:
                raise RestrictedContentError(
                    "Cannot export file",
                    "This file is too large for Google Drive to export.",
                    remote_path=remote_path,
                    local_path=local_path,
                )
            creds.refresh(_AuthRequest())
        url = f"https://docs.google.com/{kind}/d/{file_id}/export?format={ext}"
        resp = self._retry_transient(
            lambda: requests.get(
                url,
                headers={"Authorization": f"Bearer {creds.token}"},
                stream=True,
                timeout=self._timeout,
            )
        )
        with resp:
            if resp.status_code != 200:
                raise RestrictedContentError(
                    "Cannot export file",
                    "This file is too large for Google Drive to export.",
                    remote_path=remote_path,
                    local_path=local_path,
                )
            with open(tmp_path, "wb") as fh:
                for chunk in resp.iter_content(_DOWNLOAD_CHUNK_SIZE):
                    fh.write(chunk)
                    if sync_event is not None:
                        sync_event.completed += len(chunk)
                    self._throttle_down(len(chunk))
```

Imports: add `extract_http_reason` to the `.errorhandling` import block (client.py:60) and `RestrictedContentError` to the `.exceptions` block (client.py:67) if not already present. `requests` and `_AuthRequest` are already imported (client.py:33-35). Check that `sync_event.completed` starts at 0 when the API loop wrote nothing before failing — the 403 arrives on the first `next_chunk`, so no double counting; if the fake shows otherwise, reset `sync_event.completed = 0` at the top of `_export_via_web`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/offline -k "falls_back_to_web or non_200_raises or does_not_fall_back" -v`
Expected: PASS.

- [ ] **Step 5: Full suite, lint, commit**

Run: `bin/test && bin/lint`

```bash
git add src/bora/client.py tests/offline/
git commit -m "client: fall back to docs.google.com export endpoint for too-large exports"
```

---

### Task 4: `.url` placeholders for download-disabled files

Owner-disabled downloads (`cannotExportFile`) can never sync content. Write an `[InternetShortcut]` `.url` file pointing at the doc so it is at least reachable from the local folder. Distinguish the case with a new `DownloadDisabledError(RestrictedContentError)` subclass; keep the placeholder OUT of the index and guard it from upload next to the existing `download_only`-mirror guard (`src/bora/sync.py:2315`).

**Files:**
- Modify: `src/bora/exceptions.py` (~line 112, after RestrictedContentError)
- Modify: `src/bora/errorhandling.py:156` (cannotExportFile branch)
- Modify: `src/bora/sync.py` (SKIP_UNTIL_CHANGE_TYPES, `_create_local_entry`, `_apply_local_changes` sort loop ~2299, `_on_remote_deleted`, new helpers)
- Test: `tests/offline/test_sync.py` sibling (same file as Task 2's test)

**Interfaces:**
- Consumes: Task 2's `SKIP_UNTIL_CHANGE_TYPES`, `remote_rev` skip path, and `_handle_sync_error(..., remote_rev=...)` (the `type(e).__name__` check there picks up the subclass automatically).
- Produces: `bora.exceptions.DownloadDisabledError` (subclass of `RestrictedContentError` — every existing `isinstance`/`except RestrictedContentError` site keeps working, and existing tests asserting `RestrictedContentError` for cannotExportFile still pass); placeholder helpers `SyncEngine._placeholder_path(local_path) -> str` and `SyncEngine._write_url_placeholder(event) -> None`.

- [ ] **Step 1: Write the failing tests**

Same harness as Task 2 (`mt_sync_engine` + `FakeDrive` + `_wire_export_cycle`, in `tests/offline/test_sync.py` next to the export-mirror tests). A small arrange helper keeps the four tests DRY:

```python
def _arrange_disabled_doc(engine, fake):
    """One refused native doc, synced once -> placeholder expected."""
    doc = fake.add_native(name="Guide", kind="document")
    fake.set_export_bytes(doc, _MD_MIME, b"# Guide v1\n")
    fake.fail_next(
        "files.export_media",
        make_http_error(403, "cannotExportFile"),
        count=999,
    )
    engine.download_sync_cycle()
    return doc


def test_download_disabled_writes_url_placeholder(
    mt_sync_engine: SyncEngine,
) -> None:
    L = mt_sync_engine.to_local_path_from_cased
    fake = FakeDrive()
    _wire_export_cycle(mt_sync_engine, fake)
    doc = _arrange_disabled_doc(mt_sync_engine, fake)

    placeholder = L("/Guide.url")
    assert osp.isfile(placeholder)
    content = open(placeholder).read()
    assert content.startswith("[InternetShortcut]")
    assert f"https://drive.google.com/open?id={doc}" in content
    # read-only, like the export mirrors
    assert not (os.stat(placeholder).st_mode & 0o222)
    # error recorded as the distinguishing subclass
    assert mt_sync_engine.download_errors[0].type == "DownloadDisabledError"


def test_placeholder_is_never_uploaded(mt_sync_engine: SyncEngine) -> None:
    fake = FakeDrive()
    _wire_export_cycle(mt_sync_engine, fake)
    _arrange_disabled_doc(mt_sync_engine, fake)

    remote_before = _remote_names(fake)
    mt_sync_engine.upload_local_changes_while_inactive()
    assert _remote_names(fake) == remote_before


def test_placeholder_removed_when_remote_deleted(
    mt_sync_engine: SyncEngine,
) -> None:
    L = mt_sync_engine.to_local_path_from_cased
    fake = FakeDrive()
    _wire_export_cycle(mt_sync_engine, fake)
    doc = _arrange_disabled_doc(mt_sync_engine, fake)
    assert osp.isfile(L("/Guide.url"))

    fake.delete(doc)  # adapt to the fake's remote-delete helper (see its
    # trash/delete usage in neighboring deletion tests)
    mt_sync_engine.download_sync_cycle()
    assert not osp.lexists(L("/Guide.url"))


def test_placeholder_replaced_when_export_succeeds(
    mt_sync_engine: SyncEngine,
) -> None:
    L = mt_sync_engine.to_local_path_from_cased
    fake = FakeDrive()
    _wire_export_cycle(mt_sync_engine, fake)
    doc = _arrange_disabled_doc(mt_sync_engine, fake)
    assert osp.isfile(L("/Guide.url"))

    fake._fail_rules.clear()  # owner re-enabled downloads
    fake._record_change(doc, is_edit=True)  # bumps version past remote_rev
    mt_sync_engine.download_sync_cycle()

    assert osp.isfile(L("/Guide.md"))
    assert open(L("/Guide.md"), "rb").read() == b"# Guide v1\n"
    assert not osp.lexists(L("/Guide.url"))
    assert mt_sync_engine.download_errors == []
```

(The single `# adapt` note — the fake's remote-delete helper — is a name lookup in `tests/fake_drive.py`/neighboring tests, not a design gap.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/offline -k placeholder -v`
Expected: FAIL — no `.url` file written; error type is `RestrictedContentError`.

- [ ] **Step 3: Implement**

`src/bora/exceptions.py`, directly after `RestrictedContentError` (~line 112):

```python
class DownloadDisabledError(RestrictedContentError):
    """The owner disabled downloading, printing, and copying for viewers
    and commenters (Drive 403 cannotExportFile). Bora represents such
    files locally with an [InternetShortcut] .url placeholder."""
```

`src/bora/errorhandling.py:156` — change the `cannotExportFile` branch to return `DownloadDisabledError` (same title/message/paths; add it to the import block and `__all__` alongside the others if listed).

`src/bora/sync.py`:

1. Extend the constant from Task 2:

```python
SKIP_UNTIL_CHANGE_TYPES = frozenset(
    {"RestrictedContentError", "DownloadDisabledError"}
)
```

2. New helpers (near `_download_only_entry_for`, ~line 4363):

```python
    @staticmethod
    def _placeholder_path(local_path: str) -> str:
        """Map an export's local path to its .url placeholder path:
        'Gambit Guide.md' -> 'Gambit Guide.url'. The names never collide
        with a real export of the same file."""
        return osp.splitext(local_path)[0] + ".url"

    def _write_url_placeholder(self, event: SyncEvent) -> None:
        """Write a read-only [InternetShortcut] .url placeholder for a
        download-disabled file so it is reachable from the local folder.
        Uses the id-based Drive URL so no extra metadata fetch is needed."""
        path = self._placeholder_path(event.local_path)
        content = (
            "[InternetShortcut]\r\n"
            f"URL=https://drive.google.com/open?id={event.remote_id}\r\n"
        )
        try:
            with self.fs_events.ignore(
                FileCreatedEvent(path), recursive=False
            ):
                with self._writable_dir(osp.dirname(path)):
                    if osp.lexists(path):
                        self._restore_writable(path)
                    with open(path, "w", newline="") as fh:
                        fh.write(content)
                    os.chmod(path, 0o444)
        except OSError:
            # The placeholder is best-effort decoration on an already-
            # failed sync; never let it raise a second error.
            self._logger.debug(
                'Could not write placeholder "%s"', path, exc_info=True
            )

    def _placeholder_error_for(self, event: SyncEvent) -> SyncErrorEntry | None:
        """Return the DownloadDisabledError entry whose placeholder a
        *local* change targets, else None. Mirrors
        _download_only_entry_for for un-indexed .url placeholders."""
        candidates = [event.local_path]
        if event.local_path_from:
            candidates.append(event.local_path_from)
        if not any(p.endswith(".url") for p in candidates):
            return None
        with self._database_access():
            query = MatchQuery(SyncErrorEntry.direction, SyncDirection.Down)
            entries = self._sync_errors_table.select(query)  # adapt to ORM query API used at sync.py:1015
        for entry in entries:
            if entry.type != "DownloadDisabledError":
                continue
            if self._placeholder_path(entry.local_path) in candidates:
                return entry
        return None

    def _remove_placeholder_if_any(self, event: SyncEvent) -> None:
        """Delete a stale .url placeholder for *event*'s file, verifying
        it is ours (signature + this file's id) before touching it."""
        path = self._placeholder_path(event.local_path)
        if not osp.isfile(path):
            return
        try:
            with open(path) as fh:
                content = fh.read(1024)
        except OSError:
            return
        if not (
            content.startswith("[InternetShortcut]")
            and event.remote_id
            and f"id={event.remote_id}" in content
        ):
            return
        with self.fs_events.ignore(FileDeletedEvent(path), recursive=False):
            self._restore_writable(path)
            with self._writable_dir(osp.dirname(path)):
                with convert_api_errors():
                    delete(path, raise_error=False)
```

(`MatchQuery`, `delete`, `FileCreatedEvent`, `FileDeletedEvent`, `_writable_dir`, `_restore_writable` are all already imported/defined in sync.py — reuse them; adapt the select call to the exact ORM idiom used by `download_errors` at sync.py:1015.)

3. Hook into `_create_local_entry`:

- In the `except SyncError` branch (as amended in Task 2), before `_handle_sync_error`:

```python
            if isinstance(e, DownloadDisabledError) and event.is_file:
                self._write_url_placeholder(event)
```

- In the `else:` success branch, after `self.clear_sync_errors_from_event(event)`:

```python
            if event.is_file and not event.is_deleted:
                self._remove_placeholder_if_any(event)
```

- In the Task 2 skip branch (rev unchanged), re-arm a manually deleted placeholder:

```python
                if err_entry.type == "DownloadDisabledError" and not osp.lexists(
                    self._placeholder_path(event.local_path)
                ):
                    self._write_url_placeholder(event)
```

4. Guard local changes in the `_apply_local_changes` sort loop, directly after the `download_only` guard (~sync.py:2322):

```python
            ph_entry = self._placeholder_error_for(event)
            if ph_entry is not None:
                # A .url placeholder for a download-disabled file was
                # changed locally. Placeholders are bora-owned decoration:
                # never upload, never propagate deletes; a deleted
                # placeholder is rewritten on the next download cycle
                # while the error entry stands.
                self._logger.debug(
                    "Ignoring local change of placeholder: %s",
                    event.local_path,
                )
                continue
```

5. `_on_remote_deleted`: after successful handling of a remote file deletion, also call `self._remove_placeholder_if_any(event)` so orphaned placeholders disappear with their remote file. (Find the success point of `_on_remote_deleted` and add the call; the content check inside `_remove_placeholder_if_any` makes it safe even when no placeholder exists.)

Import `DownloadDisabledError` in sync.py's exceptions import block.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/offline -k placeholder -v`
Expected: PASS (all four).

- [ ] **Step 5: Full suite, lint, commit**

Run: `bin/test && bin/lint`

```bash
git add src/bora/exceptions.py src/bora/errorhandling.py src/bora/sync.py tests/offline/
git commit -m "sync: represent download-disabled files with a .url placeholder"
```

---

### Task 5: End-to-end verification and docs

**Files:**
- Modify: `CHANGELOG.md`
- Test: full suites

- [ ] **Step 1: Run the complete offline suite twice** (memory: offline flakes cascade — rerun failures in isolation before investigating)

Run: `bin/test`
Expected: green.

- [ ] **Step 2: Run the linked suite** (uses `drive-inspect`/`smoke-test`, both linked to bora-test@arko.net on this VM)

Run: `uv run pytest tests/linked -x -q`
Expected: green (or account-gated skips per the suite's markers).

- [ ] **Step 3: Update CHANGELOG.md** under an Unreleased heading, four bullets — one per fix, phrased for users (e.g. "A dropped network connection no longer pauses syncing permanently"; "Files whose owner disabled downloading now appear as .url shortcuts"; "Docs/Sheets/Slides over the 10 MB export limit now sync"; "Unexportable files no longer retried on every cycle").

- [ ] **Step 4: Lint and commit**

Run: `bin/lint`

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for sync error resilience fixes"
```

---

## Future work (explicitly out of scope)

- Listing-time detection of download-disabled files via `capabilities(canDownload)` in `FILE_FIELDS` would avoid even the first failed export attempt and let the placeholder participate in normal move/rename semantics — needs live verification with a second account's download-disabled share (only bora-test@arko.net is linked on this VM).
- Placeholders for other unsyncable classes (abusive-file flag, unsupported native types).
