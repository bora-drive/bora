# Stage 3 — Read client (download + change feed) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the read half of the Google Drive client — metadata lookup, download, folder listing, and the change feed — so the daemon can populate the path cache from a fresh Drive, observe and classify every remote mutation, and download non-native files.

**Architecture:** A new module `src/cumulus/changes.py` holds the change-feed state machine (the 4-row disambiguation table, sync-root scoping, cache write-through) plus the shared "Drive dict → core dataclass" projection helpers. `src/cumulus/client.py` fills in its read-method stubs as thin wrappers over single Drive API calls, delegating the change feed to `changes.py`. The client owns path-cache write-through: change-feed processing mutates the cache via `set`/`move`/`remove`; the cache is the source of truth and a cache miss raises `NotFoundError`. `list_folder('/', recursive=True)` is the load-bearing bootstrap that populates the cache atomically. `errorhandling.py` gains an `HttpError` branch with a per-status/per-reason mapping table, and `client.py` gets a `_retry_transient` helper (exponential backoff) wrapping every `.execute()` call.

**Tech Stack:** `google-api-python-client>=2.100.0` (`googleapiclient.discovery`, `googleapiclient.errors.HttpError`, `googleapiclient.http.MediaIoBaseDownload`), `httplib2` (transitively, for test fixtures), `sqlite3` + the in-house ORM for the path cache; `pytest` + `unittest.mock` for tests (no new test deps).

**Reading order for the implementer:** the design spec is at `docs/superpowers/specs/2026-05-12-stage-3-read-client-design.md`. Read it before starting — every task below references a numbered section of that spec. Note one spec-vs-code wording gap: spec §3.3 says "`Manager.__init__` wires the two together"; in the current codebase the class that constructs the client is `Maestral` in `src/cumulus/main.py` (Task 4 touches that). Stage 5's sync-engine port may revisit this.

---

## Task 1: `native_docs.py` — wildcard predicate + `is_shortcut`

**Spec ref:** §8.1, §8.2.

**Files:**
- Modify: `src/cumulus/native_docs.py`
- Test: `tests/offline/test_native_docs.py` (create)

- [ ] **Step 1: Write the failing tests**

  Create `tests/offline/test_native_docs.py`:

  ```python
  """Unit tests for cumulus.native_docs."""

  from __future__ import annotations

  import pytest

  from cumulus.native_docs import (
      GOOGLE_FOLDER_MIME_TYPE,
      GOOGLE_SHORTCUT_MIME_TYPE,
      filter_native_docs,
      is_google_folder,
      is_google_native,
      is_shortcut,
  )

  KNOWN_NATIVE = [
      "application/vnd.google-apps.document",
      "application/vnd.google-apps.spreadsheet",
      "application/vnd.google-apps.presentation",
      "application/vnd.google-apps.drawing",
      "application/vnd.google-apps.form",
      "application/vnd.google-apps.site",
      "application/vnd.google-apps.jam",
      "application/vnd.google-apps.script",
      "application/vnd.google-apps.map",
      "application/vnd.google-apps.shortcut",
      "application/vnd.google-apps.drive-sdk",
  ]


  @pytest.mark.parametrize("mime", KNOWN_NATIVE)
  def test_known_native_types_match(mime):
      assert is_google_native(mime) is True


  def test_folder_is_not_native():
      assert is_google_native(GOOGLE_FOLDER_MIME_TYPE) is False


  def test_future_native_type_matches_via_wildcard():
      # A type Google has not shipped yet must still be treated as native.
      assert is_google_native("application/vnd.google-apps.notebook") is True


  def test_regular_file_is_not_native():
      assert is_google_native("image/png") is False
      assert is_google_native("application/pdf") is False


  def test_none_and_empty_do_not_crash():
      assert is_google_native(None) is False
      assert is_google_native("") is False


  def test_is_google_folder():
      assert is_google_folder(GOOGLE_FOLDER_MIME_TYPE) is True
      assert is_google_folder("image/png") is False
      assert is_google_folder(None) is False


  def test_is_shortcut():
      assert is_shortcut(GOOGLE_SHORTCUT_MIME_TYPE) is True
      assert is_shortcut("application/vnd.google-apps.document") is False
      assert is_shortcut(None) is False


  def test_filter_native_docs_keeps_folders_and_files():
      items = [
          {"id": "1", "mimeType": "image/png"},
          {"id": "2", "mimeType": GOOGLE_FOLDER_MIME_TYPE},
          {"id": "3", "mimeType": "application/vnd.google-apps.document"},
          {"id": "4", "mimeType": "application/vnd.google-apps.shortcut"},
      ]
      kept = filter_native_docs(items)
      assert [it["id"] for it in kept] == ["1", "2"]
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_native_docs.py -v`
  Expected: FAIL — `ImportError: cannot import name 'GOOGLE_SHORTCUT_MIME_TYPE'` / `is_shortcut`.

- [ ] **Step 3: Rewrite `native_docs.py`**

  Replace the body of `src/cumulus/native_docs.py` (keep the module docstring's intent; update it) with:

  ```python
  """Google-native (Workspace) document detection and filtering.

  Google Docs, Sheets, Slides, etc. have MIME types under
  ``application/vnd.google-apps.*``. They have no downloadable bytes, no
  ``md5Checksum``, and cannot be synced as regular files. Drive shortcuts
  (``application/vnd.google-apps.shortcut``) are also non-syncable.

  Cumulus's MVP strategy: **skip** these items entirely. They are filtered
  out of change-feed results and listing results before the sync engine
  sees them.

  The predicate is a wildcard over the ``application/vnd.google-apps.``
  prefix (excluding the folder type) rather than an enumerated set, so a
  native MIME subtype Google ships in the future is handled without a
  code change.

  Filter points (see REVIEW.md Issue #5 and the Stage 3 spec §4.2):
    1. ``client.list_folder_iterator`` / ``list_folder`` — strip from results
    2. ``changes.iter_remote_changes`` — strip from change entries
    3. ``client.get_metadata`` — return ``None`` for native docs
    4. ``client.download`` — raise an error if somehow reached
    5. ``client.list_folder_iterator(recursive=True)`` — pre-filter before
       ``path_cache.populate_from_listing``
  """

  from __future__ import annotations

  from typing import Any

  __all__ = [
      "GOOGLE_NATIVE_PREFIX",
      "GOOGLE_FOLDER_MIME_TYPE",
      "GOOGLE_SHORTCUT_MIME_TYPE",
      "is_google_native",
      "is_google_folder",
      "is_shortcut",
      "filter_native_docs",
  ]

  # https://developers.google.com/drive/api/guides/mime-types
  GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
  GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
  GOOGLE_SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"


  def is_google_native(mime_type: str | None) -> bool:
      """Return ``True`` if *mime_type* is a non-downloadable Google-native
      type.

      Any ``application/vnd.google-apps.*`` type qualifies **except** the
      folder type. Drive shortcuts qualify (they match the prefix and are
      not folders). Folders return ``False`` — they are handled normally
      by the sync engine.
      """
      if mime_type is None:
          return False
      return (
          mime_type.startswith(GOOGLE_NATIVE_PREFIX)
          and mime_type != GOOGLE_FOLDER_MIME_TYPE
      )


  def is_google_folder(mime_type: str | None) -> bool:
      """Return ``True`` if *mime_type* is the Google Drive folder type."""
      return mime_type == GOOGLE_FOLDER_MIME_TYPE


  def is_shortcut(mime_type: str | None) -> bool:
      """Return ``True`` if *mime_type* is the Drive shortcut type.

      Shortcuts are already covered by :func:`is_google_native`; this
      helper exists for call sites that want to log or branch on shortcuts
      specifically.
      """
      return mime_type == GOOGLE_SHORTCUT_MIME_TYPE


  def filter_native_docs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
      """Return a copy of *items* with Google-native documents removed.

      Folders and regular (binary) files are kept. This is the primary
      filter applied to ``files.list`` and ``changes.list`` results before
      they reach the sync engine.
      """
      return [
          item
          for item in items
          if not is_google_native(item.get("mimeType"))
      ]
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_native_docs.py -v`
  Expected: PASS (all 18 parametrized + plain tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/native_docs.py tests/offline/test_native_docs.py
  git commit -m "feat(stage-3/native-docs): wildcard native-MIME predicate + is_shortcut"
  ```

---

## Task 2: `errorhandling.py` — `HttpError` mapping table

**Spec ref:** §6.1, §7.1.

**Files:**
- Modify: `src/cumulus/errorhandling.py`
- Modify: `tests/offline/conftest.py` (add the `make_http_error` fixture)
- Test: `tests/offline/test_errorhandling.py` (rebuild — currently a Dropbox-imports collection error)

- [ ] **Step 1: Add the `make_http_error` fixture to `conftest.py`**

  Append to `tests/offline/conftest.py`:

  ```python
  import json as _json

  import httplib2
  from googleapiclient.errors import HttpError


  @pytest.fixture
  def make_http_error():
      """Factory fixture: build a googleapiclient HttpError with a given
      status code and optional Drive sub-reason string."""

      def _make(status: int, reason: str = "", retry_after: str | None = None):
          headers = {"status": status}
          if retry_after is not None:
              headers["retry-after"] = retry_after
          resp = httplib2.Response(headers)
          body: dict = {"error": {"code": status, "message": "test error"}}
          if reason:
              body["error"]["errors"] = [{"reason": reason, "message": "test error"}]
          return HttpError(resp, _json.dumps(body).encode("utf-8"))

      return _make
  ```

- [ ] **Step 2: Write the failing tests**

  Replace the entire contents of `tests/offline/test_errorhandling.py` with:

  ```python
  """Unit tests for cumulus.errorhandling — HTTP and OSError mapping.

  The auth-library exception mapping (RefreshError / GoogleAuthError) is
  covered separately in test_errorhandling_auth.py.
  """

  from __future__ import annotations

  import errno

  import pytest

  from cumulus.errorhandling import (
      convert_api_errors,
      extract_http_reason,
      http_error_to_cumulus,
      os_to_cumulus_error,
  )
  from cumulus.exceptions import (
      BadInputError,
      ConnectionError,
      CumulusApiError,
      InsufficientPermissionsError,
      InsufficientSpaceError,
      NotFoundError,
      RestrictedContentError,
      ServerError,
      TokenRevokedError,
  )


  # ---- extract_http_reason --------------------------------------------------

  def test_extract_reason_reads_drive_sub_reason(make_http_error):
      exc = make_http_error(403, reason="rateLimitExceeded")
      assert extract_http_reason(exc) == "rateLimitExceeded"


  def test_extract_reason_empty_when_absent(make_http_error):
      exc = make_http_error(404)
      assert extract_http_reason(exc) == ""


  # ---- http_error_to_cumulus mapping table ----------------------------------

  @pytest.mark.parametrize(
      "status, reason, expected_cls",
      [
          (400, "", BadInputError),
          (401, "", TokenRevokedError),
          (403, "userRateLimitExceeded", ConnectionError),
          (403, "rateLimitExceeded", ConnectionError),
          (403, "sharingRateLimitExceeded", ConnectionError),
          (403, "storageQuotaExceeded", InsufficientSpaceError),
          (403, "dailyLimitExceeded", ConnectionError),
          (403, "cannotDownloadAbusiveFile", RestrictedContentError),
          (403, "insufficientFilePermissions", InsufficientPermissionsError),
          (404, "", NotFoundError),
          (429, "", ConnectionError),
          (500, "", ServerError),
          (502, "", ServerError),
          (503, "", ServerError),
          (504, "", ServerError),
          (418, "", CumulusApiError),
      ],
  )
  def test_http_error_mapping(make_http_error, status, reason, expected_cls):
      exc = make_http_error(status, reason=reason)
      result = http_error_to_cumulus(status, reason, exc)
      assert type(result) is expected_cls


  def test_convert_api_errors_wraps_http_error(make_http_error):
      exc = make_http_error(404)
      with pytest.raises(NotFoundError):
          with convert_api_errors(remote_path="/x"):
              raise exc


  def test_convert_api_errors_preserves_cause(make_http_error):
      exc = make_http_error(404)
      try:
          with convert_api_errors():
              raise exc
      except NotFoundError as converted:
          assert converted.__cause__ is exc
      else:
          pytest.fail("expected NotFoundError")


  # ---- os_to_cumulus_error (unchanged behavior, pinned here) ----------------

  def test_os_error_permission_maps_to_insufficient_permissions():
      exc = PermissionError("denied")
      result = os_to_cumulus_error(exc)
      assert isinstance(result, InsufficientPermissionsError)


  def test_os_error_not_found_maps_to_not_found():
      exc = FileNotFoundError("missing")
      result = os_to_cumulus_error(exc)
      assert isinstance(result, NotFoundError)


  def test_os_error_enospc_maps_to_insufficient_space():
      exc = OSError(errno.ENOSPC, "no space")
      result = os_to_cumulus_error(exc)
      assert isinstance(result, InsufficientSpaceError)
  ```

- [ ] **Step 3: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_errorhandling.py -v`
  Expected: FAIL — `ImportError: cannot import name 'extract_http_reason'` / `http_error_to_cumulus`.

- [ ] **Step 4: Implement the mapping in `errorhandling.py`**

  In `src/cumulus/errorhandling.py`:

  a. Add to the imports block (after `import requests`):

  ```python
  import json

  from googleapiclient.errors import HttpError
  ```

  b. Extend `__all__`:

  ```python
  __all__ = [
      "CONNECTION_ERRORS",
      "RETRYABLE_403_REASONS",
      "RETRYABLE_HTTP_STATUS",
      "extract_http_reason",
      "http_error_to_cumulus",
      "google_api_to_cumulus_error",
      "os_to_cumulus_error",
      "convert_api_errors",
  ]
  ```

  c. After the `CONNECTION_ERRORS` tuple definition, add:

  ```python
  # 403 sub-reasons that mean "rate-limited, retry" rather than a hard
  # permission failure. See the Stage 3 spec §6.1.
  RETRYABLE_403_REASONS = frozenset(
      {
          "userRateLimitExceeded",
          "rateLimitExceeded",
          "sharingRateLimitExceeded",
      }
  )

  # HTTP status codes that are always transient and worth retrying.
  RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
  ```

  d. Add these two functions just above `convert_api_errors`:

  ```python
  def extract_http_reason(exc: HttpError) -> str:
      """Pull the Drive sub-reason string (e.g. ``rateLimitExceeded``) from
      an :exc:`~googleapiclient.errors.HttpError`. Returns ``""`` when the
      error body has no machine-readable reason.
      """
      try:
          content = exc.content
          if isinstance(content, bytes):
              content = content.decode("utf-8", errors="replace")
          data = json.loads(content)
      except (ValueError, AttributeError, TypeError):
          return ""
      error = data.get("error", {})
      errors = error.get("errors") or []
      if errors and isinstance(errors[0], dict):
          return errors[0].get("reason", "")
      # Some Drive responses carry a top-level status string instead.
      status = error.get("status", "")
      return status if isinstance(status, str) else ""


  def http_error_to_cumulus(
      status: int,
      reason: str,
      exc: HttpError,
      remote_path: str | None = None,
      local_path: str | None = None,
  ) -> CumulusApiError:
      """Map an HTTP status + Drive sub-reason to a Cumulus exception.

      See the Stage 3 spec §6.1 for the full table. Retryable statuses
      (429, 5xx, the rate-limit 403 reasons) only reach this function
      after ``client._retry_transient`` has exhausted its budget, so they
      map straight to a user-facing ``ConnectionError`` / ``ServerError``.
      """
      if status == 400:
          return BadInputError(
              "Bad request",
              "Cumulus made an invalid request to Google Drive.",
              remote_path=remote_path,
              local_path=local_path,
          )
      if status == 401:
          return TokenRevokedError(
              "Authentication revoked",
              "Your Google account has been disconnected. "
              "Run 'cumulus auth link -r' to re-link.",
          )
      if status == 403:
          if reason in RETRYABLE_403_REASONS:
              return ConnectionError(
                  "Google Drive rate limit reached",
                  "Too many requests to Google Drive. Cumulus will retry.",
              )
          if reason == "storageQuotaExceeded":
              return InsufficientSpaceError(
                  "Cannot sync item",
                  "Your Google Drive account is out of storage space.",
                  remote_path=remote_path,
                  local_path=local_path,
              )
          if reason == "dailyLimitExceeded":
              return ConnectionError(
                  "Google Drive daily quota reached",
                  "The daily Google Drive API quota for this app has been "
                  "exhausted. Sync will resume once the quota resets.",
              )
          if reason == "cannotDownloadAbusiveFile":
              return RestrictedContentError(
                  "Cannot download file",
                  "Google flagged this file as malware or otherwise abusive "
                  "and will not allow it to be downloaded.",
                  remote_path=remote_path,
                  local_path=local_path,
              )
          return InsufficientPermissionsError(
              "Insufficient permissions",
              "You do not have permission to access this item on Google Drive.",
              remote_path=remote_path,
              local_path=local_path,
          )
      if status == 404:
          return NotFoundError(
              "Not found",
              "The requested item does not exist on Google Drive.",
              remote_path=remote_path,
              local_path=local_path,
          )
      if status == 429:
          return ConnectionError(
              "Google Drive rate limit reached",
              "Too many requests to Google Drive. Cumulus will retry.",
          )
      if status in (500, 502, 503, 504):
          return ServerError(
              "Google Drive server error",
              "Google Drive reported an internal error. Cumulus will retry.",
          )
      return CumulusApiError(
          f"Google Drive API error ({status})",
          str(exc),
          remote_path=remote_path,
          local_path=local_path,
      )
  ```

  e. In `convert_api_errors`, replace the placeholder comment
     `# TODO: Stage 3 — catch googleapiclient.errors.HttpError ...` with a
     real branch. The `except` block sits **before** `except CONNECTION_ERRORS`:

  ```python
      except HttpError as exc:
          raise http_error_to_cumulus(
              exc.resp.status,
              extract_http_reason(exc),
              exc,
              remote_path,
              local_path,
          ) from exc
  ```

- [ ] **Step 5: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_errorhandling.py tests/offline/test_errorhandling_auth.py -v`
  Expected: PASS — the rebuilt file plus the untouched auth file both green.

- [ ] **Step 6: Commit**

  ```bash
  git add src/cumulus/errorhandling.py tests/offline/test_errorhandling.py tests/offline/conftest.py
  git commit -m "feat(stage-3/errors): map HttpError to Cumulus exceptions"
  ```

---

## Task 3: `client.py` — constructor gains `path_cache` + `_poll_event`

**Spec ref:** §3.3, §4.1.

**Files:**
- Modify: `src/cumulus/client.py` (`__init__`, `link`, `unlink`, `close`)
- Modify: `tests/offline/conftest.py` (`sync` + `client` fixtures)
- Modify: `tests/offline/test_client_auth.py` (`client` fixture)
- Modify: `tests/offline/test_clean_local_events.py` (`sync` fixture)
- Test: `tests/offline/test_client.py` (rebuild — currently a Dropbox-imports collection error)

> **Note:** the linked-test fixtures under `tests/linked/` also construct
> the client and will need the new argument, but linked tests are out of
> Stage 3 scope (spec §2.2) and are not part of the offline test run.
> Leave them for the stage that revives `tests/linked/`.

- [ ] **Step 1: Write the failing test (and rebuild `test_client.py`)**

  Replace the entire contents of `tests/offline/test_client.py` with this
  fresh Drive-focused scaffold (later tasks append to it):

  ```python
  """Offline unit tests for cumulus.client.GoogleDriveClient.

  Drive API calls are mocked at the `_get_service()` boundary: each test
  builds a MagicMock service and assigns `client._get_service` to a Mock
  returning it.
  """

  from __future__ import annotations

  import sqlite3
  from unittest.mock import MagicMock, Mock

  import pytest

  from cumulus.client import GoogleDriveClient
  from cumulus.config import remove_configuration
  from cumulus.database.core import Database
  from cumulus.keyring import CredentialStorage
  from cumulus.path_cache import PathIDCache

  _CONFIG = "test-config-s3-client"


  @pytest.fixture
  def cache() -> PathIDCache:
      """Fresh in-memory path cache with the sync root pre-seeded."""
      c = PathIDCache(Database(sqlite3.connect(":memory:")))
      c.root_id = "ROOT"
      return c


  @pytest.fixture
  def client(cache: PathIDCache):
      cred = CredentialStorage(_CONFIG)
      cl = GoogleDriveClient(_CONFIG, cred, cache)
      yield cl
      cred.delete_creds()
      remove_configuration(_CONFIG)


  def fake_service() -> MagicMock:
      """A MagicMock standing in for a built Drive v3 service."""
      return MagicMock(name="drive-service")


  # ==== constructor =========================================================

  def test_constructor_stores_path_cache(client, cache):
      assert client._path_cache is cache


  def test_close_sets_poll_event(client):
      assert client._poll_event.is_set() is False
      client.close()
      assert client._poll_event.is_set() is True
  ```

- [ ] **Step 2: Run the test to verify it fails**

  Run: `python -m pytest tests/offline/test_client.py -v`
  Expected: FAIL — `TypeError: __init__() missing 1 required positional argument: 'path_cache'`.

- [ ] **Step 3: Modify `GoogleDriveClient.__init__` and lifecycle methods**

  In `src/cumulus/client.py`:

  a. Add to the top-level imports:

  ```python
  import threading
  ```

  and, in the local-imports block:

  ```python
  from .path_cache import PathIDCache
  ```

  b. Change the `__init__` signature and add the two new attributes. The
     new signature inserts `path_cache` as the third positional parameter:

  ```python
      def __init__(
          self,
          config_name: str,
          cred_storage: Any,
          path_cache: PathIDCache,
          timeout: float = 100,
          session: Any | None = None,
          bandwidth_limit_up: float = 0,
          bandwidth_limit_down: float = 0,
      ) -> None:
          self.config_name = config_name
          self._cred_storage = cred_storage
          self._path_cache = path_cache
          self._state = CumulusState(config_name)
          self._timeout = timeout
          self._cached_account_info: FullAccount | None = None
          self._cached_space_usage: PersonalSpaceUsage | None = None

          # Throttling config.
          self.bandwidth_limit_up = bandwidth_limit_up
          self.bandwidth_limit_down = bandwidth_limit_down

          # Request quota tracking.
          self._request_count = 0
          self._request_day: int = 0  # day-of-year when counter was last reset
          self._warned_at_threshold = False
          self._warned_at_critical = False

          # Auth state.
          self._credentials: oauth.PersistingCredentials | None = None
          self._pending_paste_flow: _PasteFlowState | None = None

          # Shutdown signal: set on close()/unlink(), cleared on link().
          # Interrupts the change-feed poll sleep and retry backoff sleeps
          # so the daemon stops within ~1 ms instead of waiting them out.
          self._poll_event = threading.Event()
  ```

  c. In `link`, clear the poll event at the very start of the method body
     (a re-link starts a fresh session):

  ```python
      def link(
          self,
          code: str | None = None,
          refresh_token: str | None = None,
          access_token: str | None = None,
      ) -> int:
          """Link with a Google account. Returns 0 / 1 / 2 (success / invalid /
          connection failure).
          """
          self._poll_event.clear()
          try:
  ```

  (only the `self._poll_event.clear()` line is new; the rest of `link` is
  unchanged.)

  d. In `unlink`, add the poll-event set at the end of the method:

  ```python
          self._credentials = None
          self._pending_paste_flow = None
          self._cached_account_info = None
          self._cached_space_usage = None
          self._poll_event.set()
  ```

  e. Replace the `close` method body:

  ```python
      def close(self) -> None:
          """Cleans up resources and signals any in-flight poll/retry sleep
          to abort (daemon shutdown)."""
          self._poll_event.set()
  ```

- [ ] **Step 4: Update the other offline fixtures**

  In `tests/offline/conftest.py`, add near the top imports:

  ```python
  import sqlite3

  from cumulus.database.core import Database
  from cumulus.path_cache import PathIDCache
  ```

  and a helper plus the two updated fixtures:

  ```python
  def _make_path_cache() -> PathIDCache:
      cache = PathIDCache(Database(sqlite3.connect(":memory:")))
      cache.root_id = "ROOT"
      return cache
  ```

  Update the `sync` fixture's client construction line:

  ```python
      sync = SyncEngine(
          DropboxClient("test-config", CredentialStorage("test-config"), _make_path_cache())
      )
  ```

  Update the `client` fixture:

  ```python
  @pytest.fixture
  def client():
      yield DropboxClient(
          "test-config", CredentialStorage("test-config"), _make_path_cache()
      )
      remove_configuration("test-config")
  ```

  In `tests/offline/test_client_auth.py`, add to the imports:

  ```python
  import sqlite3

  from cumulus.database.core import Database
  from cumulus.path_cache import PathIDCache
  ```

  and update the `client` fixture:

  ```python
  @pytest.fixture
  def client(cred_storage):
      cache = PathIDCache(Database(sqlite3.connect(":memory:")))
      cache.root_id = "ROOT"
      yield GoogleDriveClient("test-config-client", cred_storage, cache)
  ```

  In `tests/offline/test_clean_local_events.py`, add the same `sqlite3` /
  `Database` / `PathIDCache` imports and update its `sync` fixture's client
  construction the same way as `conftest.py`'s `sync` fixture:

  ```python
      sync = SyncEngine(
          DropboxClient(
              "test-config",
              CredentialStorage("test-config"),
              PathIDCache(Database(sqlite3.connect(":memory:"))),
          )
      )
  ```

- [ ] **Step 5: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py tests/offline/test_client_auth.py -v`
  Expected: PASS — the two constructor tests plus all Stage 2 auth tests.

- [ ] **Step 6: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py tests/offline/test_client_auth.py tests/offline/conftest.py tests/offline/test_clean_local_events.py
  git commit -m "feat(stage-3/client): constructor takes path_cache + poll-event lifecycle"
  ```

---

## Task 4: `main.py` — wire a `PathIDCache` into the client

**Spec ref:** §3.3.

**Files:**
- Modify: `src/cumulus/main.py` (`Maestral.__init__`)
- Test: `tests/offline/test_main.py` (append one test)

- [ ] **Step 1: Write the failing test**

  Append to `tests/offline/test_main.py`:

  ```python
  def test_maestral_wires_path_cache_into_client(m):
      """Stage 3: the client must receive a PathIDCache from Maestral."""
      from cumulus.path_cache import PathIDCache

      assert isinstance(m.client._path_cache, PathIDCache)
  ```

  (The `m` fixture comes from `tests/offline/conftest.py`.)

- [ ] **Step 2: Run the test to verify it fails**

  Run: `python -m pytest tests/offline/test_main.py::test_maestral_wires_path_cache_into_client -v`
  Expected: FAIL — `TypeError: __init__() missing 1 required positional argument: 'path_cache'` raised while constructing `Maestral`.

- [ ] **Step 3: Construct the cache in `Maestral.__init__`**

  In `src/cumulus/main.py`, add to the local-imports block:

  ```python
  from .path_cache import PathIDCache
  ```

  Then replace the "Set up sync infrastructure" block (currently lines
  ~195–203, the `self.client = DropboxClient(...)` / `self.sync = ...` /
  `self.manager = ...` group) with:

  ```python
          # Set up sync infrastructure.
          #
          # Stage 3 gives the read client its own DB-backed path cache.
          # The SyncEngine still opens its own connection to the same DB
          # file; Stage 5's sync-engine port consolidates the two onto a
          # single shared Database/PathIDCache.
          self._client_db_path = get_data_path(
              "cumulus", f"{self.config_name}.db"
          )
          self._client_db = Database(
              sqlite3.connect(self._client_db_path, check_same_thread=False)
          )
          self._path_cache = PathIDCache(self._client_db)
          self.client = DropboxClient(
              self.config_name,
              self.cred_storage,
              self._path_cache,
              bandwidth_limit_up=self.bandwidth_limit_up,
              bandwidth_limit_down=self.bandwidth_limit_down,
          )
          self.sync = SyncEngine(self.client, self._dn)
          self.manager = SyncManager(self.sync, self._dn)
  ```

  (`sqlite3`, `Database`, and `get_data_path` are already imported in
  `main.py` — verified at lines 16, 57, 82.)

- [ ] **Step 4: Run the test to verify it passes**

  Run: `python -m pytest tests/offline/test_main.py::test_maestral_wires_path_cache_into_client -v`
  Expected: PASS.

- [ ] **Step 5: Confirm no regression beyond the known baseline**

  Run: `python -m pytest tests/offline/ -q`
  Expected: the pre-existing failures in `test_manager.py`, `test_main.py`,
  `test_event_handler.py`, `test_daemon.py` may remain (Stage 5/6 work),
  but there must be **no new collection errors** and no new failures
  attributable to the constructor change. Record the failure count.

- [ ] **Step 6: Commit**

  ```bash
  git add src/cumulus/main.py tests/offline/test_main.py
  git commit -m "feat(stage-3/main): construct PathIDCache and pass it to the client"
  ```

---

## Task 5: `client.py` — `_retry_transient` helper

**Spec ref:** §7.2, §7.3.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_client.py`:

  ```python
  # ==== _retry_transient ====================================================

  def test_retry_returns_on_first_success(client):
      call = Mock(return_value="ok")
      assert client._retry_transient(call) == "ok"
      assert call.call_count == 1


  def test_retry_retries_then_succeeds(client, make_http_error):
      client._poll_event = Mock()
      client._poll_event.wait.return_value = False  # no shutdown
      call = Mock(side_effect=[make_http_error(503), make_http_error(503), "ok"])
      assert client._retry_transient(call) == "ok"
      assert call.call_count == 3


  def test_retry_does_not_retry_non_retryable(client, make_http_error):
      # A 404 is not transient — it must propagate unchanged (the caller's
      # convert_api_errors maps it later).
      from googleapiclient.errors import HttpError

      call = Mock(side_effect=make_http_error(404))
      with pytest.raises(HttpError):
          client._retry_transient(call)
      assert call.call_count == 1


  def test_retry_exhausts_and_raises_last(client, make_http_error):
      from googleapiclient.errors import HttpError

      client._poll_event = Mock()
      client._poll_event.wait.return_value = False
      call = Mock(side_effect=[make_http_error(500)] * 10)
      with pytest.raises(HttpError):
          client._retry_transient(call)
      assert call.call_count == 5  # _MAX_RETRY_ATTEMPTS


  def test_retry_retries_rate_limited_403(client, make_http_error):
      client._poll_event = Mock()
      client._poll_event.wait.return_value = False
      call = Mock(side_effect=[make_http_error(403, "rateLimitExceeded"), "ok"])
      assert client._retry_transient(call) == "ok"
      assert call.call_count == 2


  def test_retry_does_not_retry_permission_403(client, make_http_error):
      from googleapiclient.errors import HttpError

      call = Mock(side_effect=make_http_error(403, "insufficientFilePermissions"))
      with pytest.raises(HttpError):
          client._retry_transient(call)
      assert call.call_count == 1


  def test_retry_aborts_on_shutdown(client, make_http_error):
      from cumulus.exceptions import CancelledError

      client._poll_event = Mock()
      client._poll_event.wait.return_value = True  # shutdown during backoff
      call = Mock(side_effect=make_http_error(503))
      with pytest.raises(CancelledError):
          client._retry_transient(call)
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_client.py -k retry -v`
  Expected: FAIL — `AttributeError: 'GoogleDriveClient' object has no attribute '_retry_transient'`.

- [ ] **Step 3: Implement the helper**

  In `src/cumulus/client.py`:

  a. Add to the top-level imports:

  ```python
  import random
  ```

  b. Extend the `typing` import to include `Callable` and `TypeVar`:

  ```python
  from typing import (
      TYPE_CHECKING,
      Any,
      Callable,
      Iterator,
      Sequence,
      TypeVar,
      overload,
  )
  ```

  c. Add to the imports from `googleapiclient`:

  ```python
  from googleapiclient.errors import HttpError
  ```

  d. Extend the `errorhandling` import:

  ```python
  from .errorhandling import (
      CONNECTION_ERRORS,
      RETRYABLE_403_REASONS,
      RETRYABLE_HTTP_STATUS,
      convert_api_errors,
      extract_http_reason,
  )
  ```

  e. Extend the `exceptions` import:

  ```python
  from .exceptions import (
      CancelledError,
      CumulusApiError,
      DataCorruptionError,
      NotFoundError,
      NotLinkedError,
      UnsupportedFileError,
  )
  ```

  f. Below the existing module constants (after `DEFAULT_POLL_INTERVAL`),
     add:

  ```python
  # Transient-error retry policy (Stage 3 spec §7.2).
  _MAX_RETRY_ATTEMPTS = 5
  _INITIAL_BACKOFF = 1.0   # seconds
  _MAX_BACKOFF = 32.0      # seconds

  T = TypeVar("T")


  def _is_retryable_http_error(exc: HttpError) -> bool:
      """Whether an HttpError is a transient failure worth retrying."""
      status = exc.resp.status
      if status in RETRYABLE_HTTP_STATUS:
          return True
      if status == 403:
          return extract_http_reason(exc) in RETRYABLE_403_REASONS
      return False


  def _retry_after_seconds(exc: HttpError) -> float | None:
      """Parse a numeric Retry-After header from an HttpError, if present.

      Returns ``None`` when the header is absent or is an HTTP-date form
      (we fall back to exponential backoff in that case)."""
      retry_after = exc.resp.get("retry-after")
      if not retry_after:
          return None
      try:
          return float(retry_after)
      except (TypeError, ValueError):
          return None
  ```

  g. Add the `_retry_transient` method to `GoogleDriveClient`, right after
     `_get_service` (in the "Service builder" section):

  ```python
      def _retry_transient(self, call: Callable[[], T]) -> T:
          """Run *call* and retry it on transient Google Drive errors.

          Retries 429 / 5xx / rate-limit-403 HttpErrors and connection
          errors with exponential backoff (1s → 32s) plus jitter, honoring
          a numeric ``Retry-After`` header when present. After
          ``_MAX_RETRY_ATTEMPTS`` it re-raises the last exception unchanged
          so the caller's ``convert_api_errors`` can map it.

          Raises :exc:`~cumulus.exceptions.CancelledError` if the client's
          ``_poll_event`` fires during a backoff sleep (daemon shutdown).
          """
          attempt = 0
          backoff = _INITIAL_BACKOFF
          while True:
              try:
                  return call()
              except HttpError as exc:
                  if (
                      not _is_retryable_http_error(exc)
                      or attempt >= _MAX_RETRY_ATTEMPTS - 1
                  ):
                      raise
                  sleep_for = _retry_after_seconds(exc)
                  if sleep_for is None:
                      sleep_for = backoff + random.uniform(0, 0.5)
              except CONNECTION_ERRORS:
                  if attempt >= _MAX_RETRY_ATTEMPTS - 1:
                      raise
                  sleep_for = backoff + random.uniform(0, 0.5)
              if self._poll_event.wait(sleep_for):
                  raise CancelledError(
                      "Sync cancelled",
                      "Google Drive request interrupted by daemon shutdown.",
                  )
              backoff = min(backoff * 2, _MAX_BACKOFF)
              attempt += 1
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py -k retry -v`
  Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py
  git commit -m "feat(stage-3/client): _retry_transient with exponential backoff"
  ```

---

## Task 6: `client.py` — `_ensure_root_id`

**Spec ref:** §5.4.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_client.py`:

  ```python
  # ==== _ensure_root_id =====================================================

  def test_ensure_root_id_resolves_and_caches(cache):
      cred = CredentialStorage(_CONFIG + "-root")
      cl = GoogleDriveClient(_CONFIG + "-root", cred, cache)
      cache.root_id = None  # force resolution
      service = fake_service()
      service.files.return_value.get.return_value.execute.return_value = {
          "id": "ROOT_XYZ"
      }
      cl._get_service = Mock(return_value=service)

      assert cl._ensure_root_id() == "ROOT_XYZ"
      assert cache.root_id == "ROOT_XYZ"
      # Second call must not hit the service again.
      assert cl._ensure_root_id() == "ROOT_XYZ"
      assert service.files.return_value.get.call_count == 1

      cred.delete_creds()
      remove_configuration(_CONFIG + "-root")


  def test_ensure_root_id_short_circuits_when_set(client):
      # cache.root_id was pre-seeded to "ROOT" by the fixture.
      client._get_service = Mock(side_effect=AssertionError("should not be called"))
      assert client._ensure_root_id() == "ROOT"
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_client.py -k ensure_root_id -v`
  Expected: FAIL — `AttributeError: ... has no attribute '_ensure_root_id'`.

- [ ] **Step 3: Implement `_ensure_root_id`**

  In `src/cumulus/client.py`, add this method to `GoogleDriveClient` in the
  "Service builder" section, just after `_retry_transient`:

  ```python
      def _ensure_root_id(self) -> str:
          """Resolve the Drive 'root' alias to a concrete file ID and cache
          it on the path cache. Idempotent — one round-trip per daemon
          lifetime. Every method that hits the Drive API calls this first.
          """
          if self._path_cache.root_id is not None:
              return self._path_cache.root_id
          with convert_api_errors():
              resp = self._retry_transient(
                  lambda: self._get_service()
                  .files()
                  .get(fileId="root", fields="id")
                  .execute()
              )
          self._path_cache.root_id = resp["id"]
          return resp["id"]
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py -k ensure_root_id -v`
  Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py
  git commit -m "feat(stage-3/client): _ensure_root_id resolves the sync root"
  ```

---

## Task 7: `changes.py` — projection helpers + field constants

**Spec ref:** §3.1, §6.5.

**Files:**
- Create: `src/cumulus/changes.py`
- Test: `tests/offline/test_changes.py` (create)

> `changes.py` is created here with only the pure projection helpers and
> field constants. Tasks 15 and 16 add `classify()` and
> `iter_remote_changes()`. Putting the projection helpers here (rather than
> in `client.py`) keeps the dependency one-directional: `client.py` imports
> from `changes.py`, never the reverse.

- [ ] **Step 1: Write the failing tests**

  Create `tests/offline/test_changes.py`:

  ```python
  """Unit tests for cumulus.changes."""

  from __future__ import annotations

  from datetime import datetime, timezone

  from cumulus.changes import (
      parse_drive_time,
      to_file_metadata,
      to_folder_metadata,
  )
  from cumulus.core import FileMetadata, FolderMetadata

  SAMPLE_FILE = {
      "id": "F1",
      "name": "report.pdf",
      "parents": ["ROOT"],
      "mimeType": "application/pdf",
      "modifiedTime": "2026-04-30T04:32:17.043Z",
      "size": "2048",
      "md5Checksum": "abc123",
      "shared": False,
      "version": 7,
      "trashed": False,
      "lastModifyingUser": {"permissionId": "perm-9"},
  }

  SAMPLE_FOLDER = {
      "id": "D1",
      "name": "Documents",
      "parents": ["ROOT"],
      "mimeType": "application/vnd.google-apps.folder",
      "shared": True,
  }


  def test_parse_drive_time_round_trips():
      dt = parse_drive_time("2026-04-30T04:32:17.043Z")
      assert dt == datetime(2026, 4, 30, 4, 32, 17, 43000, tzinfo=timezone.utc)


  def test_parse_drive_time_handles_none():
      dt = parse_drive_time(None)
      assert dt.tzinfo is timezone.utc


  def test_to_file_metadata_maps_all_fields():
      meta = to_file_metadata(SAMPLE_FILE, "/report.pdf")
      assert isinstance(meta, FileMetadata)
      assert meta.name == "report.pdf"
      assert meta.path_display == "/report.pdf"
      assert meta.id == "F1"
      assert meta.rev == "7"
      assert meta.size == 2048
      assert meta.content_hash == "abc123"
      assert meta.symlink_target is None
      assert meta.is_downloadable is True
      assert meta.modified_by == "perm-9"
      assert meta.client_modified == meta.server_modified


  def test_to_file_metadata_tolerates_missing_optional_fields():
      bare = {"id": "F2", "name": "x.bin", "mimeType": "application/octet-stream"}
      meta = to_file_metadata(bare, "/x.bin")
      assert meta.size == 0
      assert meta.content_hash == ""
      assert meta.modified_by is None


  def test_to_folder_metadata_maps_fields():
      meta = to_folder_metadata(SAMPLE_FOLDER, "/Documents")
      assert isinstance(meta, FolderMetadata)
      assert meta.name == "Documents"
      assert meta.path_display == "/Documents"
      assert meta.id == "D1"
      assert meta.shared is True
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_changes.py -v`
  Expected: FAIL — `ModuleNotFoundError: No module named 'cumulus.changes'`.

- [ ] **Step 3: Create `src/cumulus/changes.py`**

  ```python
  """Google Drive change-feed processing and Drive-dict projection.

  Two responsibilities:

  * **Projection** — pure helpers that turn a Drive ``files`` resource
    dict into a :mod:`cumulus.core` dataclass. Shared by ``client.py``'s
    listing/metadata methods and by the change-feed code below.
  * **Change feed** — :func:`iter_remote_changes` pages Drive's
    ``changes.list``, classifies each entry against the 4-row
    disambiguation table (:func:`classify`), resolves file IDs to paths
    via the path cache, mutates the cache, and yields
    :class:`~cumulus.core.ListFolderResult` pages.

  The module never imports ``client.py``: it receives the service factory
  and the retry helper as parameters, keeping the dependency one-way
  (``client.py`` → ``changes.py``).

  See ``docs/superpowers/specs/2026-05-12-stage-3-read-client-design.md``
  §6 for the design.
  """

  from __future__ import annotations

  from datetime import datetime, timezone
  from typing import Any

  from .core import FileMetadata, FolderMetadata
  from .path_codec import fold_path

  __all__ = [
      "FILE_FIELDS",
      "parse_drive_time",
      "to_file_metadata",
      "to_folder_metadata",
  ]

  # The Drive ``files`` resource fields Cumulus needs for every read. Used
  # verbatim by client.py's files.get / files.list calls and embedded into
  # the change-feed field selector below.
  FILE_FIELDS = (
      "id, name, parents, mimeType, modifiedTime, size, md5Checksum, "
      "shared, version, trashed, lastModifyingUser(permissionId)"
  )


  def parse_drive_time(value: str | None) -> datetime:
      """Parse Drive's RFC-3339 timestamp into a UTC :class:`datetime`.

      Returns the Unix epoch (UTC) when *value* is missing — the sync
      engine treats a zero timestamp as "unknown"."""
      if not value:
          return datetime.fromtimestamp(0, tz=timezone.utc)
      return datetime.fromisoformat(value.replace("Z", "+00:00"))


  def to_file_metadata(file: dict[str, Any], path_display: str) -> FileMetadata:
      """Project a Drive ``files`` resource into a :class:`FileMetadata`.

      *path_display* is the encoded virtual path the caller has already
      resolved for this file."""
      modified = parse_drive_time(file.get("modifiedTime"))
      last_user = file.get("lastModifyingUser") or {}
      return FileMetadata(
          name=file["name"],
          path_lower=fold_path(path_display),
          path_display=path_display,
          id=file["id"],
          client_modified=modified,
          server_modified=modified,
          rev=str(file.get("version", "")),
          size=int(file.get("size", 0) or 0),
          symlink_target=None,
          shared=bool(file.get("shared", False)),
          modified_by=last_user.get("permissionId"),
          is_downloadable=True,
          content_hash=file.get("md5Checksum", ""),
      )


  def to_folder_metadata(
      file: dict[str, Any], path_display: str
  ) -> FolderMetadata:
      """Project a Drive folder resource into a :class:`FolderMetadata`."""
      return FolderMetadata(
          name=file["name"],
          path_lower=fold_path(path_display),
          path_display=path_display,
          id=file["id"],
          shared=bool(file.get("shared", False)),
      )
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_changes.py -v`
  Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/changes.py tests/offline/test_changes.py
  git commit -m "feat(stage-3/changes): Drive-dict projection helpers"
  ```

---

## Task 8: `client.py` — `get_metadata`

**Spec ref:** §4 (`get_metadata`), §4.2.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_client.py`:

  ```python
  # ==== get_metadata ========================================================

  def _seed(cache, path, file_id, name, parent="ROOT", is_folder=False):
      cache.set(path, file_id, parent, name, is_folder)


  def test_get_metadata_returns_file_metadata(client, cache):
      from cumulus.core import FileMetadata

      _seed(cache, "/report.pdf", "F1", "report.pdf")
      service = fake_service()
      service.files.return_value.get.return_value.execute.return_value = {
          "id": "F1", "name": "report.pdf", "mimeType": "application/pdf",
          "parents": ["ROOT"], "modifiedTime": "2026-04-30T00:00:00Z",
          "size": "10", "md5Checksum": "h", "version": 2, "trashed": False,
      }
      client._get_service = Mock(return_value=service)

      meta = client.get_metadata("/report.pdf")
      assert isinstance(meta, FileMetadata)
      assert meta.id == "F1"


  def test_get_metadata_returns_folder_metadata(client, cache):
      from cumulus.core import FolderMetadata

      _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
      service = fake_service()
      service.files.return_value.get.return_value.execute.return_value = {
          "id": "D1", "name": "Documents",
          "mimeType": "application/vnd.google-apps.folder",
          "parents": ["ROOT"], "trashed": False,
      }
      client._get_service = Mock(return_value=service)

      meta = client.get_metadata("/Documents")
      assert isinstance(meta, FolderMetadata)


  def test_get_metadata_returns_none_for_native_doc(client, cache):
      _seed(cache, "/Notes", "G1", "Notes")
      service = fake_service()
      service.files.return_value.get.return_value.execute.return_value = {
          "id": "G1", "name": "Notes",
          "mimeType": "application/vnd.google-apps.document",
          "parents": ["ROOT"], "trashed": False,
      }
      client._get_service = Mock(return_value=service)

      assert client.get_metadata("/Notes") is None


  def test_get_metadata_raises_not_found_on_cache_miss(client):
      from cumulus.exceptions import NotFoundError

      client._get_service = Mock(side_effect=AssertionError("should not call API"))
      with pytest.raises(NotFoundError):
          client.get_metadata("/no-such-file")


  def test_get_metadata_trashed_returns_none(client, cache):
      _seed(cache, "/old.pdf", "F9", "old.pdf")
      service = fake_service()
      service.files.return_value.get.return_value.execute.return_value = {
          "id": "F9", "name": "old.pdf", "mimeType": "application/pdf",
          "parents": ["ROOT"], "trashed": True,
      }
      client._get_service = Mock(return_value=service)

      assert client.get_metadata("/old.pdf", include_deleted=False) is None
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_client.py -k get_metadata -v`
  Expected: FAIL — `NotImplementedError` from the existing stub.

- [ ] **Step 3: Implement `get_metadata`**

  In `src/cumulus/client.py`:

  a. Add to the local-imports block:

  ```python
  from . import changes
  from .changes import FILE_FIELDS, to_file_metadata, to_folder_metadata
  from .native_docs import is_google_folder, is_google_native
  from .path_codec import compose_path
  ```

  b. Replace the `get_metadata` method body (currently `_not_implemented()`)
     with:

  ```python
      def get_metadata(
          self, remote_path: str, include_deleted: bool = False
      ) -> Metadata | None:
          """Gets metadata for an item on Google Drive.

          :param remote_path: Encoded virtual path of the item.
          :param include_deleted: Whether to return data for trashed items.
          :returns: Metadata of the item, or ``None`` for Google-native
              documents (which cannot be synced) and for trashed items
              when *include_deleted* is ``False``.
          :raises NotFoundError: if *remote_path* is not known to the
              path cache.
          """
          self._ensure_root_id()
          file_id = self._path_cache.path_to_id(remote_path)
          if file_id is None:
              raise NotFoundError(
                  "Not found",
                  f"No item at '{remote_path}' is known to Cumulus.",
                  remote_path=remote_path,
              )
          with convert_api_errors(remote_path=remote_path):
              file = self._retry_transient(
                  lambda: self._get_service()
                  .files()
                  .get(fileId=file_id, fields=FILE_FIELDS)
                  .execute()
              )
          self._count_request()
          if file.get("trashed") and not include_deleted:
              return None
          mime = file.get("mimeType")
          if is_google_native(mime):
              return None
          if is_google_folder(mime):
              return to_folder_metadata(file, remote_path)
          return to_file_metadata(file, remote_path)
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py -k get_metadata -v`
  Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py
  git commit -m "feat(stage-3/client): get_metadata via files.get"
  ```

---

## Task 9: `client.py` — `list_revisions` + `restore` stub message

**Spec ref:** §4 (`list_revisions`, `restore`).

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_client.py`:

  ```python
  # ==== list_revisions / restore ============================================

  def test_list_revisions_returns_newest_first(client, cache):
      _seed(cache, "/report.pdf", "F1", "report.pdf")
      service = fake_service()
      service.revisions.return_value.list.return_value.execute.return_value = {
          "revisions": [
              {"id": "r1", "modifiedTime": "2026-01-01T00:00:00Z", "size": "10"},
              {"id": "r2", "modifiedTime": "2026-02-01T00:00:00Z", "size": "20"},
          ]
      }
      client._get_service = Mock(return_value=service)

      revs = client.list_revisions("/report.pdf")
      assert [r.rev for r in revs] == ["r2", "r1"]


  def test_list_revisions_respects_limit(client, cache):
      _seed(cache, "/report.pdf", "F1", "report.pdf")
      service = fake_service()
      service.revisions.return_value.list.return_value.execute.return_value = {
          "revisions": [
              {"id": f"r{i}", "modifiedTime": "2026-01-01T00:00:00Z", "size": "1"}
              for i in range(20)
          ]
      }
      client._get_service = Mock(return_value=service)

      revs = client.list_revisions("/report.pdf", limit=5)
      assert len(revs) == 5


  def test_list_revisions_paginates(client, cache):
      _seed(cache, "/report.pdf", "F1", "report.pdf")
      service = fake_service()
      service.revisions.return_value.list.return_value.execute.side_effect = [
          {"revisions": [{"id": "r1", "modifiedTime": "2026-01-01T00:00:00Z",
                          "size": "1"}], "nextPageToken": "p2"},
          {"revisions": [{"id": "r2", "modifiedTime": "2026-02-01T00:00:00Z",
                          "size": "1"}]},
      ]
      client._get_service = Mock(return_value=service)

      revs = client.list_revisions("/report.pdf")
      assert {r.rev for r in revs} == {"r1", "r2"}


  def test_list_revisions_cache_miss_raises(client):
      from cumulus.exceptions import NotFoundError

      client._get_service = Mock(side_effect=AssertionError("no API call"))
      with pytest.raises(NotFoundError):
          client.list_revisions("/missing")


  def test_restore_raises_not_implemented(client):
      with pytest.raises(NotImplementedError):
          client.restore("/report.pdf", "r1")
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_client.py -k "revisions or restore" -v`
  Expected: FAIL — `NotImplementedError` from the stub for `list_revisions`
  (and `restore` raises a `NotImplementedError` with the old message).

- [ ] **Step 3: Implement `list_revisions` and update `restore`**

  In `src/cumulus/client.py`:

  a. Add to the `changes` import line the `parse_drive_time` helper:

  ```python
  from .changes import FILE_FIELDS, parse_drive_time, to_file_metadata, to_folder_metadata
  ```

  b. Add `fold_path` to the `path_codec` import:

  ```python
  from .path_codec import compose_path, fold_path
  ```

  c. Replace the `list_revisions` method body with:

  ```python
      def list_revisions(
          self, remote_path: str, limit: int = 10
      ) -> list[FileMetadata]:
          """Lists revisions for the file at *remote_path*, most recent
          first, capped at *limit*.

          The Dropbox-era ``mode`` parameter is dropped — Drive's
          ``revisions.list`` only accepts a file ID.

          :raises NotFoundError: if *remote_path* is not in the path cache.
          """
          self._ensure_root_id()
          file_id = self._path_cache.path_to_id(remote_path)
          if file_id is None:
              raise NotFoundError(
                  "Not found",
                  f"No item at '{remote_path}' is known to Cumulus.",
                  remote_path=remote_path,
              )
          raw: list[dict[str, Any]] = []
          page_token: str | None = None
          with convert_api_errors(remote_path=remote_path):
              while True:
                  resp = self._retry_transient(
                      lambda pt=page_token: self._get_service()
                      .revisions()
                      .list(
                          fileId=file_id,
                          fields=(
                              "nextPageToken, revisions(id, modifiedTime, "
                              "size, md5Checksum, "
                              "lastModifyingUser(permissionId))"
                          ),
                          pageSize=100,
                          pageToken=pt,
                      )
                      .execute()
                  )
                  self._count_request()
                  raw.extend(resp.get("revisions", []))
                  page_token = resp.get("nextPageToken")
                  if not page_token:
                      break
          # Drive returns revisions oldest-first; we want newest-first.
          raw.reverse()
          name = remote_path.rsplit("/", 1)[-1]
          path_lower = fold_path(remote_path)
          result: list[FileMetadata] = []
          for rev in raw[:limit]:
              modified = parse_drive_time(rev.get("modifiedTime"))
              last_user = rev.get("lastModifyingUser") or {}
              result.append(
                  FileMetadata(
                      name=name,
                      path_lower=path_lower,
                      path_display=remote_path,
                      id=file_id,
                      client_modified=modified,
                      server_modified=modified,
                      rev=rev["id"],
                      size=int(rev.get("size", 0) or 0),
                      symlink_target=None,
                      shared=False,
                      modified_by=last_user.get("permissionId"),
                      is_downloadable=True,
                      content_hash=rev.get("md5Checksum", ""),
                  )
              )
          return result
  ```

  d. Replace the `restore` method body with an explicit deferred stub:

  ```python
      def restore(self, remote_path: str, rev: str) -> FileMetadata:
          """Restore an old revision of a file.

          Not available until Stage 4 (write client) lands ``upload``:
          Google Drive has no single-call revision-restore, so restore is
          implemented as download-revision + re-upload, which needs the
          write client.
          """
          raise NotImplementedError(
              "restore() is not available until Stage 4 (write client) "
              "lands upload support."
          )
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py -k "revisions or restore" -v`
  Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py
  git commit -m "feat(stage-3/client): list_revisions; restore stub deferred to Stage 4"
  ```

---

## Task 10: `client.py` — `list_folder_iterator` (non-recursive) + `list_folder`

**Spec ref:** §4 (`list_folder`, `list_folder_iterator`).

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_client.py`:

  ```python
  # ==== list_folder_iterator (non-recursive) ================================

  def test_list_folder_non_recursive_yields_entries(client, cache):
      from cumulus.core import FileMetadata, FolderMetadata

      _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
      service = fake_service()
      service.files.return_value.list.return_value.execute.return_value = {
          "files": [
              {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
               "parents": ["D1"], "trashed": False, "size": "1"},
              {"id": "D2", "name": "Sub",
               "mimeType": "application/vnd.google-apps.folder",
               "parents": ["D1"], "trashed": False},
          ]
      }
      client._get_service = Mock(return_value=service)

      pages = list(client.list_folder_iterator("/Documents"))
      entries = pages[0].entries
      assert len(entries) == 2
      assert any(isinstance(e, FileMetadata) for e in entries)
      assert any(isinstance(e, FolderMetadata) for e in entries)


  def test_list_folder_non_recursive_filters_native_docs(client, cache):
      _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
      service = fake_service()
      service.files.return_value.list.return_value.execute.return_value = {
          "files": [
              {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
               "parents": ["D1"], "trashed": False, "size": "1"},
              {"id": "G1", "name": "Doc",
               "mimeType": "application/vnd.google-apps.document",
               "parents": ["D1"], "trashed": False},
          ]
      }
      client._get_service = Mock(return_value=service)

      entries = list(client.list_folder_iterator("/Documents"))[0].entries
      assert [e.id for e in entries] == ["F1"]


  def test_list_folder_non_recursive_paginates(client, cache):
      _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
      service = fake_service()
      service.files.return_value.list.return_value.execute.side_effect = [
          {"files": [{"id": "F1", "name": "a", "mimeType": "application/pdf",
                      "parents": ["D1"], "trashed": False, "size": "1"}],
           "nextPageToken": "p2"},
          {"files": [{"id": "F2", "name": "b", "mimeType": "application/pdf",
                      "parents": ["D1"], "trashed": False, "size": "1"}]},
      ]
      client._get_service = Mock(return_value=service)

      pages = list(client.list_folder_iterator("/Documents"))
      assert len(pages) == 2
      assert pages[0].has_more is True
      assert pages[1].has_more is False


  def test_list_folder_non_recursive_cache_miss_raises(client):
      from cumulus.exceptions import NotFoundError

      client._get_service = Mock(side_effect=AssertionError("no API call"))
      with pytest.raises(NotFoundError):
          list(client.list_folder_iterator("/missing-folder"))


  def test_list_folder_flattens(client, cache):
      _seed(cache, "/Documents", "D1", "Documents", is_folder=True)
      service = fake_service()
      service.files.return_value.list.return_value.execute.side_effect = [
          {"files": [{"id": "F1", "name": "a", "mimeType": "application/pdf",
                      "parents": ["D1"], "trashed": False, "size": "1"}],
           "nextPageToken": "p2"},
          {"files": [{"id": "F2", "name": "b", "mimeType": "application/pdf",
                      "parents": ["D1"], "trashed": False, "size": "1"}]},
      ]
      client._get_service = Mock(return_value=service)

      result = client.list_folder("/Documents")
      assert {e.id for e in result.entries} == {"F1", "F2"}
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_client.py -k list_folder -v`
  Expected: FAIL — `NotImplementedError` from the stubs.

- [ ] **Step 3: Implement the non-recursive listing**

  In `src/cumulus/client.py`:

  a. Below the retry-policy constants added in Task 5, add:

  ```python
  # Page size for files.list calls.
  _LIST_PAGE_SIZE = 1000
  ```

  b. Replace the `list_folder` method body with:

  ```python
      def list_folder(
          self,
          remote_path: str,
          recursive: bool = False,
          include_deleted: bool = False,
          include_mounted_folders: bool = True,
          include_non_downloadable_files: bool = False,
      ) -> ListFolderResult:
          """Lists the contents of a folder, exhausting all pages.

          See :meth:`list_folder_iterator` for parameter meaning.
          """
          pages = list(
              self.list_folder_iterator(
                  remote_path,
                  recursive=recursive,
                  include_deleted=include_deleted,
                  include_mounted_folders=include_mounted_folders,
                  include_non_downloadable_files=include_non_downloadable_files,
              )
          )
          return self.flatten_results(pages)
  ```

  c. Replace the `list_folder_iterator` method body with the version below.
     The `recursive=True` branch delegates to `_bootstrap_recursive`, which
     is implemented in Task 11 — add a forward-referenced call now; Task 11
     fills in the method.

  ```python
      def list_folder_iterator(
          self,
          remote_path: str,
          recursive: bool = False,
          include_deleted: bool = False,
          include_mounted_folders: bool = True,
          limit: int | None = None,
          include_non_downloadable_files: bool = False,
      ) -> Iterator[ListFolderResult]:
          """Lists folder contents as a paginated iterator.

          ``recursive=True`` (only meaningful for the sync root ``"/"``) is
          the initial-sync bootstrap: it enumerates the whole reachable
          subtree and atomically populates the path cache. See
          :meth:`_bootstrap_recursive`.

          ``recursive=False`` lists one folder's direct children;
          *remote_path* must already be known to the path cache.

          Google-native documents are filtered out unless
          *include_non_downloadable_files* is ``True``.
          """
          self._ensure_root_id()
          if recursive:
              yield from self._bootstrap_recursive(include_non_downloadable_files)
              return

          parent_id = self._path_cache.path_to_id(remote_path)
          if parent_id is None:
              raise NotFoundError(
                  "Not found",
                  f"No folder at '{remote_path}' is known to Cumulus.",
                  remote_path=remote_path,
              )
          query = f"'{parent_id}' in parents"
          if not include_deleted:
              query += " and trashed=false"
          page_size = limit or _LIST_PAGE_SIZE
          page_token: str | None = None
          while True:
              with convert_api_errors(remote_path=remote_path):
                  resp = self._retry_transient(
                      lambda pt=page_token: self._get_service()
                      .files()
                      .list(
                          q=query,
                          fields=f"nextPageToken, files({FILE_FIELDS})",
                          pageSize=page_size,
                          pageToken=pt,
                      )
                      .execute()
                  )
              self._count_request()
              entries: list[Metadata] = []
              for file in resp.get("files", []):
                  mime = file.get("mimeType")
                  if is_google_native(mime) and not include_non_downloadable_files:
                      continue
                  child_path = compose_path(remote_path, file["name"])
                  if is_google_folder(mime):
                      entries.append(to_folder_metadata(file, child_path))
                  else:
                      entries.append(to_file_metadata(file, child_path))
              page_token = resp.get("nextPageToken")
              yield ListFolderResult(
                  entries=entries,
                  has_more=bool(page_token),
                  cursor=page_token or "",
              )
              if not page_token:
                  break
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py -k list_folder -v`
  Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py
  git commit -m "feat(stage-3/client): non-recursive list_folder via files.list"
  ```

---

## Task 11: `client.py` — `list_folder_iterator` recursive bootstrap

**Spec ref:** §5.1, §5.2, §5.3, §5.5.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_client.py`:

  ```python
  # ==== list_folder_iterator (recursive bootstrap) ==========================

  def _bootstrap_service(files_pages, start_token="TOK1"):
      """Build a fake service for the recursive bootstrap: a
      changes.getStartPageToken call plus paginated files.list calls."""
      service = fake_service()
      service.changes.return_value.getStartPageToken.return_value.execute.return_value = {
          "startPageToken": start_token
      }
      service.files.return_value.list.return_value.execute.side_effect = files_pages
      return service


  def test_bootstrap_populates_cache_and_yields(client, cache):
      cache.root_id = "ROOT"
      pages = [{
          "files": [
              {"id": "D1", "name": "Docs",
               "mimeType": "application/vnd.google-apps.folder",
               "parents": ["ROOT"], "trashed": False},
              {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
               "parents": ["D1"], "trashed": False, "size": "1"},
          ]
      }]
      client._get_service = Mock(return_value=_bootstrap_service(pages))

      result_pages = list(client.list_folder_iterator("/", recursive=True))
      ids = {e.id for page in result_pages for e in page.entries}
      assert ids == {"D1", "F1"}
      # Cache populated for path resolution.
      assert cache.path_to_id("/Docs") == "D1"
      assert cache.path_to_id("/Docs/a.pdf") == "F1"
      # Last page carries the change cursor captured before listing.
      assert result_pages[-1].cursor == "TOK1"
      assert result_pages[-1].has_more is False


  def test_bootstrap_excludes_native_docs(client, cache):
      cache.root_id = "ROOT"
      pages = [{
          "files": [
              {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
               "parents": ["ROOT"], "trashed": False, "size": "1"},
              {"id": "G1", "name": "Doc",
               "mimeType": "application/vnd.google-apps.document",
               "parents": ["ROOT"], "trashed": False},
          ]
      }]
      client._get_service = Mock(return_value=_bootstrap_service(pages))

      result_pages = list(client.list_folder_iterator("/", recursive=True))
      ids = {e.id for page in result_pages for e in page.entries}
      assert ids == {"F1"}
      assert cache.get_entry("G1") is None


  def test_bootstrap_drops_items_outside_root(client, cache, caplog):
      import logging

      cache.root_id = "ROOT"
      pages = [{
          "files": [
              {"id": "F1", "name": "a.pdf", "mimeType": "application/pdf",
               "parents": ["ROOT"], "trashed": False, "size": "1"},
              # Parent OUTSIDE_ROOT is not reachable from ROOT.
              {"id": "F2", "name": "shared.pdf", "mimeType": "application/pdf",
               "parents": ["OUTSIDE_ROOT"], "trashed": False, "size": "1"},
          ]
      }]
      client._get_service = Mock(return_value=_bootstrap_service(pages))

      with caplog.at_level(logging.INFO, logger="cumulus.client"):
          result_pages = list(client.list_folder_iterator("/", recursive=True))
      ids = {e.id for page in result_pages for e in page.entries}
      assert ids == {"F1"}
      assert cache.get_entry("F2") is None
      assert any("skipped" in rec.message for rec in caplog.records)


  def test_bootstrap_fetches_all_pages(client, cache):
      cache.root_id = "ROOT"
      pages = [
          {"files": [{"id": "F1", "name": "a", "mimeType": "application/pdf",
                      "parents": ["ROOT"], "trashed": False, "size": "1"}],
           "nextPageToken": "p2"},
          {"files": [{"id": "F2", "name": "b", "mimeType": "application/pdf",
                      "parents": ["ROOT"], "trashed": False, "size": "1"}]},
      ]
      client._get_service = Mock(return_value=_bootstrap_service(pages))

      result_pages = list(client.list_folder_iterator("/", recursive=True))
      ids = {e.id for page in result_pages for e in page.entries}
      assert ids == {"F1", "F2"}
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_client.py -k bootstrap -v`
  Expected: FAIL — `AttributeError: ... has no attribute '_bootstrap_recursive'`.

- [ ] **Step 3: Implement `_bootstrap_recursive`**

  In `src/cumulus/client.py`, add this method to `GoogleDriveClient`,
  immediately after `list_folder_iterator`:

  ```python
      def _bootstrap_recursive(
          self, include_non_downloadable_files: bool = False
      ) -> Iterator[ListFolderResult]:
          """Initial-sync bootstrap: enumerate the whole Drive, populate
          the path cache atomically, and yield the result as pages.

          This is the *only* method that builds the path cache from
          scratch. It must be called only on initial sync — it clears and
          rebuilds the cache, discarding any change-feed deltas. See spec
          §5.5.
          """
          # Capture the change cursor BEFORE listing so a change that
          # lands mid-listing is not missed by the first poll cycle.
          with convert_api_errors():
              token_resp = self._retry_transient(
                  lambda: self._get_service()
                  .changes()
                  .getStartPageToken()
                  .execute()
              )
          start_token = token_resp["startPageToken"]

          # Page through every non-trashed item visible to the user.
          raw_items: list[dict[str, Any]] = []
          page_token: str | None = None
          while True:
              with convert_api_errors():
                  resp = self._retry_transient(
                      lambda pt=page_token: self._get_service()
                      .files()
                      .list(
                          q="trashed=false",
                          fields=f"nextPageToken, files({FILE_FIELDS})",
                          pageSize=_LIST_PAGE_SIZE,
                          pageToken=pt,
                      )
                      .execute()
                  )
              self._count_request()
              raw_items.extend(resp.get("files", []))
              page_token = resp.get("nextPageToken")
              if not page_token:
                  break

          root_id = self._path_cache.root_id
          # Drop the sync root itself (it is implicit) and, unless asked
          # otherwise, Google-native docs. populate_from_listing keeps
          # folders regardless; is_google_native already excludes folders.
          filtered = [
              it
              for it in raw_items
              if it.get("id") != root_id
              and (
                  include_non_downloadable_files
                  or not is_google_native(it.get("mimeType"))
              )
          ]

          # Atomic populate. The cache BFS-filters to items reachable from
          # root_id; Computers/Backup, shared-with-me and orphans drop out.
          self._path_cache.populate_from_listing(filtered)
          indexed = self._path_cache.count()
          skipped = len(filtered) - indexed
          if skipped > 0:
              logger.info(
                  "Indexed %d items from Google Drive; skipped %d items "
                  "outside My Drive root (Computers/Backup, shared-with-me, "
                  "orphans) — see debug log for detail.",
                  indexed,
                  skipped,
              )
          else:
              logger.info("Indexed %d items from Google Drive.", indexed)

          # Yield the in-scope items as pages. An item is in scope iff the
          # cache resolved a path for it. The final page carries the
          # change cursor captured above.
          in_scope = [
              it
              for it in filtered
              if self._path_cache.id_to_path(it["id"]) is not None
          ]
          if not in_scope:
              yield ListFolderResult(
                  entries=[], has_more=False, cursor=start_token
              )
              return
          for start in range(0, len(in_scope), _LIST_PAGE_SIZE):
              chunk = in_scope[start : start + _LIST_PAGE_SIZE]
              is_last = start + _LIST_PAGE_SIZE >= len(in_scope)
              entries: list[Metadata] = []
              for file in chunk:
                  path = self._path_cache.id_to_path(file["id"])
                  if is_google_folder(file.get("mimeType")):
                      entries.append(to_folder_metadata(file, path))
                  else:
                      entries.append(to_file_metadata(file, path))
              yield ListFolderResult(
                  entries=entries,
                  has_more=not is_last,
                  cursor=start_token if is_last else "",
              )
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py -k bootstrap -v`
  Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py
  git commit -m "feat(stage-3/client): recursive bootstrap populates the path cache"
  ```

---

## Task 12: `client.py` — `download` (chunked + md5 verify)

**Spec ref:** §4 (`download`).

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_client.py`:

  ```python
  # ==== download ============================================================

  import hashlib as _hashlib


  class FakeDownloader:
      """Stand-in for googleapiclient.http.MediaIoBaseDownload. Writes the
      bytes stashed on the request mock to the file handle in one chunk."""

      def __init__(self, fh, request, chunksize=None):
          self._fh = fh
          self._data = request._fake_data
          self._done = False

      def next_chunk(self):
          self._fh.write(self._data)
          self._done = True
          status = Mock(resumable_progress=len(self._data))
          return status, True


  def _download_service(file_meta, file_bytes):
      service = fake_service()
      service.files.return_value.get.return_value.execute.return_value = file_meta
      media_request = Mock()
      media_request._fake_data = file_bytes
      service.files.return_value.get_media.return_value = media_request
      return service


  def test_download_writes_file_and_verifies_md5(client, cache, tmp_path, monkeypatch):
      monkeypatch.setattr("cumulus.client.MediaIoBaseDownload", FakeDownloader)
      _seed(cache, "/a.bin", "F1", "a.bin")
      data = b"hello world"
      meta = {
          "id": "F1", "name": "a.bin", "mimeType": "application/octet-stream",
          "parents": ["ROOT"], "trashed": False, "size": str(len(data)),
          "md5Checksum": _hashlib.md5(data).hexdigest(), "version": 1,
      }
      client._get_service = Mock(return_value=_download_service(meta, data))

      dest = tmp_path / "a.bin"
      result = client.download("/a.bin", str(dest))
      assert dest.read_bytes() == data
      assert result.id == "F1"


  def test_download_native_doc_raises(client, cache, tmp_path):
      from cumulus.exceptions import UnsupportedFileError

      _seed(cache, "/Doc", "G1", "Doc")
      meta = {
          "id": "G1", "name": "Doc",
          "mimeType": "application/vnd.google-apps.document",
          "parents": ["ROOT"], "trashed": False,
      }
      service = fake_service()
      service.files.return_value.get.return_value.execute.return_value = meta
      client._get_service = Mock(return_value=service)

      with pytest.raises(UnsupportedFileError):
          client.download("/Doc", str(tmp_path / "Doc"))


  def test_download_md5_mismatch_raises_and_cleans_up(client, cache, tmp_path, monkeypatch):
      from cumulus.exceptions import DataCorruptionError

      monkeypatch.setattr("cumulus.client.MediaIoBaseDownload", FakeDownloader)
      _seed(cache, "/a.bin", "F1", "a.bin")
      data = b"hello world"
      meta = {
          "id": "F1", "name": "a.bin", "mimeType": "application/octet-stream",
          "parents": ["ROOT"], "trashed": False, "size": str(len(data)),
          "md5Checksum": "deadbeef" * 4, "version": 1,  # wrong hash
      }
      client._get_service = Mock(return_value=_download_service(meta, data))

      dest = tmp_path / "a.bin"
      with pytest.raises(DataCorruptionError):
          client.download("/a.bin", str(dest))
      assert not dest.exists()
      assert not (tmp_path / "a.bin.cumulus-part").exists()


  def test_download_cache_miss_raises(client, tmp_path):
      from cumulus.exceptions import NotFoundError

      client._get_service = Mock(side_effect=AssertionError("no API call"))
      with pytest.raises(NotFoundError):
          client.download("/missing", str(tmp_path / "x"))


  def test_download_updates_sync_event(client, cache, tmp_path, monkeypatch):
      monkeypatch.setattr("cumulus.client.MediaIoBaseDownload", FakeDownloader)
      _seed(cache, "/a.bin", "F1", "a.bin")
      data = b"abcdef"
      meta = {
          "id": "F1", "name": "a.bin", "mimeType": "application/octet-stream",
          "parents": ["ROOT"], "trashed": False, "size": str(len(data)),
          "md5Checksum": _hashlib.md5(data).hexdigest(), "version": 1,
      }
      client._get_service = Mock(return_value=_download_service(meta, data))

      sync_event = Mock()
      client.download("/a.bin", str(tmp_path / "a.bin"), sync_event=sync_event)
      assert sync_event.completed == len(data)
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_client.py -k download -v`
  Expected: FAIL — `NotImplementedError` from the stub.

- [ ] **Step 3: Implement `download`**

  In `src/cumulus/client.py`:

  a. Add to the top-level imports:

  ```python
  import hashlib
  ```

  b. Add to the `googleapiclient` imports:

  ```python
  from googleapiclient.http import MediaIoBaseDownload
  ```

  c. Below `_LIST_PAGE_SIZE`, add:

  ```python
  # Download chunk size — Google's recommended granularity for
  # MediaIoBaseDownload.
  _DOWNLOAD_CHUNK_SIZE = 8 * 1024 * 1024
  ```

  d. Replace the `download` method body with:

  ```python
      def download(
          self,
          remote_path: str,
          local_path: str,
          sync_event: SyncEvent | None = None,
      ) -> FileMetadata:
          """Downloads a file from Google Drive to *local_path*.

          Downloads to a ``.cumulus-part`` sidecar, verifies the MD5
          against Drive's ``md5Checksum``, then atomically renames into
          place. *sync_event*, if given, has ``.completed`` updated after
          each chunk.

          :raises NotFoundError: if *remote_path* is unknown to the cache.
          :raises UnsupportedFileError: if it resolves to a native doc.
          :raises DataCorruptionError: on an MD5 mismatch.
          """
          self._ensure_root_id()
          file_id = self._path_cache.path_to_id(remote_path)
          if file_id is None:
              raise NotFoundError(
                  "Not found",
                  f"No item at '{remote_path}' is known to Cumulus.",
                  remote_path=remote_path,
              )
          tmp_path = local_path + ".cumulus-part"
          with convert_api_errors(remote_path=remote_path, local_path=local_path):
              meta_file = self._retry_transient(
                  lambda: self._get_service()
                  .files()
                  .get(fileId=file_id, fields=FILE_FIELDS)
                  .execute()
              )
              self._count_request()
              if is_google_native(meta_file.get("mimeType")):
                  raise UnsupportedFileError(
                      "Cannot download file",
                      "Google-native documents (Docs, Sheets, Slides, "
                      "etc.) cannot be synced as files.",
                      remote_path=remote_path,
                      local_path=local_path,
                  )
              request = self._get_service().files().get_media(fileId=file_id)
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
              self._count_request()

              expected = meta_file.get("md5Checksum")
              if expected:
                  hasher = hashlib.md5()
                  with open(tmp_path, "rb") as fh:
                      for block in iter(lambda: fh.read(1 << 20), b""):
                          hasher.update(block)
                  if hasher.hexdigest() != expected:
                      os.remove(tmp_path)
                      raise DataCorruptionError(
                          "Download failed",
                          "The downloaded file is corrupt (MD5 mismatch). "
                          "Cumulus will retry.",
                          remote_path=remote_path,
                          local_path=local_path,
                      )
              os.replace(tmp_path, local_path)
          return to_file_metadata(meta_file, remote_path)

      def _throttle_down(self, nbytes: int) -> None:
          """Crude download bandwidth throttle: sleep proportionally to the
          configured limit. A no-op when no limit is set."""
          if self.bandwidth_limit_down > 0:
              time.sleep(nbytes / self.bandwidth_limit_down)
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py -k download -v`
  Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py
  git commit -m "feat(stage-3/client): chunked download with MD5 verification"
  ```

---

## Task 13: `client.py` — `get_latest_cursor`

**Spec ref:** §4 (`get_latest_cursor`).

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_client.py`:

  ```python
  # ==== get_latest_cursor ===================================================

  def test_get_latest_cursor_returns_start_page_token(client):
      service = fake_service()
      service.changes.return_value.getStartPageToken.return_value.execute.return_value = {
          "startPageToken": "TOK99"
      }
      client._get_service = Mock(return_value=service)

      assert client.get_latest_cursor("/") == "TOK99"


  def test_get_latest_cursor_ignores_remote_path(client):
      service = fake_service()
      service.changes.return_value.getStartPageToken.return_value.execute.return_value = {
          "startPageToken": "TOK99"
      }
      client._get_service = Mock(return_value=service)

      # Any remote_path must produce the same account-global token.
      assert client.get_latest_cursor("/Documents/Sub") == "TOK99"
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_client.py -k get_latest_cursor -v`
  Expected: FAIL — `NotImplementedError` from the stub.

- [ ] **Step 3: Implement `get_latest_cursor`**

  In `src/cumulus/client.py`, replace the `get_latest_cursor` method body:

  ```python
      def get_latest_cursor(
          self, remote_path: str, include_non_downloadable_files: bool = False
      ) -> str:
          """Gets the latest change token for the account.

          :param remote_path: Ignored — Drive's ``startPageToken`` is
              account-global, not folder-scoped. The parameter is kept for
              compatibility with the sync engine's call sites.
          :returns: Drive's ``startPageToken``.
          """
          with convert_api_errors():
              resp = self._retry_transient(
                  lambda: self._get_service()
                  .changes()
                  .getStartPageToken()
                  .execute()
              )
          self._count_request()
          return resp["startPageToken"]
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py -k get_latest_cursor -v`
  Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py
  git commit -m "feat(stage-3/client): get_latest_cursor via changes.getStartPageToken"
  ```

---

## Task 14: `client.py` — `wait_for_remote_changes`

**Spec ref:** §4 (`wait_for_remote_changes`), §4.1.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_client.py`:

  ```python
  # ==== wait_for_remote_changes =============================================

  def test_wait_returns_true_when_changes_present(client):
      client._poll_event = Mock()
      client._poll_event.wait.return_value = False  # timed out normally
      service = fake_service()
      service.changes.return_value.list.return_value.execute.return_value = {
          "changes": [{"fileId": "F1"}]
      }
      client._get_service = Mock(return_value=service)

      assert client.wait_for_remote_changes("CURSOR", timeout=1) is True


  def test_wait_returns_false_when_no_changes(client):
      client._poll_event = Mock()
      client._poll_event.wait.return_value = False
      service = fake_service()
      service.changes.return_value.list.return_value.execute.return_value = {
          "changes": []
      }
      client._get_service = Mock(return_value=service)

      assert client.wait_for_remote_changes("CURSOR", timeout=1) is False


  def test_wait_returns_false_on_shutdown_without_polling(client):
      client._poll_event = Mock()
      client._poll_event.wait.return_value = True  # shutdown during sleep
      client._get_service = Mock(side_effect=AssertionError("must not poll"))

      assert client.wait_for_remote_changes("CURSOR", timeout=1) is False
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_client.py -k "wait_for" -v`
  Expected: FAIL — `NotImplementedError` from the stub.

- [ ] **Step 3: Implement `wait_for_remote_changes`**

  In `src/cumulus/client.py`, replace the `wait_for_remote_changes` method
  body:

  ```python
      def wait_for_remote_changes(
          self, last_cursor: str, timeout: int = DEFAULT_POLL_INTERVAL
      ) -> bool:
          """Waits for remote changes since *last_cursor*.

          Google Drive has no long-poll endpoint, so this sleeps for
          *timeout* seconds (interruptibly, via ``_poll_event``) and then
          checks ``changes.list`` for at least one change.

          :returns: ``True`` if changes are available; ``False`` on a
              clean timeout with no changes, or if the sleep was
              interrupted by daemon shutdown.
          """
          interrupted = self._poll_event.wait(timeout)
          if interrupted:
              return False
          with convert_api_errors():
              resp = self._retry_transient(
                  lambda: self._get_service()
                  .changes()
                  .list(
                      pageToken=last_cursor,
                      pageSize=1,
                      fields="changes(fileId)",
                  )
                  .execute()
              )
          self._count_request()
          return len(resp.get("changes", [])) > 0
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py -k "wait_for" -v`
  Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py
  git commit -m "feat(stage-3/client): interruptible polling wait_for_remote_changes"
  ```

---

## Task 15: `changes.py` — `ChangeDecision` + `classify`

**Spec ref:** §6.3, §9.5.

**Files:**
- Modify: `src/cumulus/changes.py`
- Test: `tests/offline/test_changes.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_changes.py`:

  ```python
  # ==== classify ============================================================

  import sqlite3

  from cumulus.changes import ChangeDecision, classify
  from cumulus.database.core import Database
  from cumulus.path_cache import PathIDCache


  def _cache() -> PathIDCache:
      c = PathIDCache(Database(sqlite3.connect(":memory:")))
      c.root_id = "ROOT"
      return c


  def test_classify_removed_true_is_delete():
      change = {"removed": True, "fileId": "F1"}
      assert classify(change, _cache()) is ChangeDecision.DELETE


  def test_classify_trashed_is_delete():
      cache = _cache()
      change = {"removed": False, "fileId": "F1",
                "file": {"id": "F1", "name": "a", "parents": ["ROOT"],
                         "trashed": True}}
      assert classify(change, cache) is ChangeDecision.DELETE


  def test_classify_move_out_is_delete():
      cache = _cache()
      cache.set("/a.pdf", "F1", "ROOT", "a.pdf", False)
      change = {"removed": False, "fileId": "F1",
                "file": {"id": "F1", "name": "a.pdf",
                         "parents": ["OUTSIDE"], "trashed": False}}
      assert classify(change, cache) is ChangeDecision.DELETE


  def test_classify_never_in_scope_is_out_of_scope():
      cache = _cache()
      change = {"removed": False, "fileId": "F9",
                "file": {"id": "F9", "name": "x", "parents": ["OUTSIDE"],
                         "trashed": False}}
      assert classify(change, cache) is ChangeDecision.OUT_OF_SCOPE


  def test_classify_in_scope_is_apply():
      cache = _cache()
      change = {"removed": False, "fileId": "F1",
                "file": {"id": "F1", "name": "a.pdf", "parents": ["ROOT"],
                         "trashed": False}}
      assert classify(change, cache) is ChangeDecision.APPLY


  def test_classify_missing_file_body_is_unknown():
      cache = _cache()
      change = {"removed": False, "fileId": "F1", "file": None}
      assert classify(change, cache) is ChangeDecision.UNKNOWN_FILE
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_changes.py -k classify -v`
  Expected: FAIL — `ImportError: cannot import name 'ChangeDecision'`.

- [ ] **Step 3: Implement `ChangeDecision` and `classify`**

  In `src/cumulus/changes.py`:

  a. Add `import enum` and `import logging` to the top-of-file imports.
     Replace the existing `from typing import Any` line with:

  ```python
  from typing import TYPE_CHECKING, Any
  ```

  Then add, below the imports (before `__all__`):

  ```python
  if TYPE_CHECKING:
      from .path_cache import PathIDCache

  logger = logging.getLogger(__name__)
  ```

  b. Extend `__all__`:

  ```python
  __all__ = [
      "FILE_FIELDS",
      "ChangeDecision",
      "classify",
      "parse_drive_time",
      "to_file_metadata",
      "to_folder_metadata",
  ]
  ```

  c. Add the enum and function (after the projection helpers):

  ```python
  class ChangeDecision(enum.Enum):
      """How :func:`iter_remote_changes` should treat a change entry."""

      APPLY = "apply"
      """Create / update / in-scope move — apply via the path cache."""
      DELETE = "delete"
      """Hard-delete, trash, or move-out — propagate a local deletion."""
      OUT_OF_SCOPE = "out_of_scope"
      """A file that was never inside the sync root — drop silently."""
      UNKNOWN_FILE = "unknown_file"
      """A malformed change entry with no file body — drop with a log."""


  def classify(
      change: dict[str, Any], path_cache: "PathIDCache"
  ) -> ChangeDecision:
      """Classify a Drive ``changes.list`` entry per the A4 disambiguation
      table (spec §6.3).

      Row 3 of the spec table — "no parent in scope" — is split here into
      *move-out* (the file ID is still in the cache, so it left the sync
      root and the local mirror must delete it) and *never-in-scope* (the
      file ID is not in the cache, so it was created outside the sync root
      and is simply ignored).
      """
      if change.get("removed"):
          return ChangeDecision.DELETE
      file = change.get("file")
      if file is None:
          return ChangeDecision.UNKNOWN_FILE
      if file.get("trashed"):
          return ChangeDecision.DELETE
      parents = file.get("parents") or []
      in_scope = any(path_cache.id_to_path(p) is not None for p in parents)
      if not in_scope:
          if path_cache.id_to_path(file["id"]) is not None:
              return ChangeDecision.DELETE
          return ChangeDecision.OUT_OF_SCOPE
      return ChangeDecision.APPLY
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_changes.py -k classify -v`
  Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/changes.py tests/offline/test_changes.py
  git commit -m "feat(stage-3/changes): classify change entries (A4 disambiguation)"
  ```

---

## Task 16: `changes.py` — `iter_remote_changes`

**Spec ref:** §6.1, §6.2, §6.4, §9.5.

**Files:**
- Modify: `src/cumulus/changes.py`
- Test: `tests/offline/test_changes.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_changes.py`:

  ```python
  # ==== iter_remote_changes =================================================

  from unittest.mock import MagicMock

  from cumulus.changes import iter_remote_changes
  from cumulus.core import DeletedMetadata, FileMetadata, FolderMetadata


  def _passthrough_retry(call):
      """A retry helper that just calls through (no real retrying)."""
      return call()


  def _changes_service(*pages):
      """Build a fake service whose changes.list returns the given pages."""
      service = MagicMock(name="drive-service")
      service.changes.return_value.list.return_value.execute.side_effect = pages
      return lambda: service


  def test_iter_changes_create_writes_cache_and_yields():
      cache = _cache()
      page = {
          "newStartPageToken": "NEXT",
          "changes": [
              {"removed": False, "fileId": "F1",
               "file": {"id": "F1", "name": "a.pdf", "parents": ["ROOT"],
                        "mimeType": "application/pdf", "trashed": False,
                        "size": "1", "version": 1}},
          ],
      }
      results = list(iter_remote_changes(
          _changes_service(page), _passthrough_retry, cache, "CURSOR"
      ))
      assert len(results) == 1
      assert isinstance(results[0].entries[0], FileMetadata)
      assert cache.path_to_id("/a.pdf") == "F1"
      assert results[0].cursor == "NEXT"
      assert results[0].has_more is False


  def test_iter_changes_delete_removes_from_cache():
      cache = _cache()
      cache.set("/a.pdf", "F1", "ROOT", "a.pdf", False)
      page = {
          "newStartPageToken": "NEXT",
          "changes": [{"removed": True, "fileId": "F1"}],
      }
      results = list(iter_remote_changes(
          _changes_service(page), _passthrough_retry, cache, "CURSOR"
      ))
      assert isinstance(results[0].entries[0], DeletedMetadata)
      assert cache.path_to_id("/a.pdf") is None


  def test_iter_changes_rename_moves_cache_entry():
      cache = _cache()
      cache.set("/old.pdf", "F1", "ROOT", "old.pdf", False)
      page = {
          "newStartPageToken": "NEXT",
          "changes": [
              {"removed": False, "fileId": "F1",
               "file": {"id": "F1", "name": "new.pdf", "parents": ["ROOT"],
                        "mimeType": "application/pdf", "trashed": False,
                        "size": "1", "version": 2}},
          ],
      }
      list(iter_remote_changes(
          _changes_service(page), _passthrough_retry, cache, "CURSOR"
      ))
      assert cache.path_to_id("/old.pdf") is None
      assert cache.path_to_id("/new.pdf") == "F1"


  def test_iter_changes_intra_batch_dependency():
      # Folder D1 created in entry 1; file F1 moved into D1 in entry 2.
      cache = _cache()
      page = {
          "newStartPageToken": "NEXT",
          "changes": [
              {"removed": False, "fileId": "D1",
               "file": {"id": "D1", "name": "Docs", "parents": ["ROOT"],
                        "mimeType": "application/vnd.google-apps.folder",
                        "trashed": False}},
              {"removed": False, "fileId": "F1",
               "file": {"id": "F1", "name": "a.pdf", "parents": ["D1"],
                        "mimeType": "application/pdf", "trashed": False,
                        "size": "1", "version": 1}},
          ],
      }
      results = list(iter_remote_changes(
          _changes_service(page), _passthrough_retry, cache, "CURSOR"
      ))
      assert cache.path_to_id("/Docs/a.pdf") == "F1"
      assert {type(e) for e in results[0].entries} == {FolderMetadata, FileMetadata}


  def test_iter_changes_drops_native_doc():
      cache = _cache()
      page = {
          "newStartPageToken": "NEXT",
          "changes": [
              {"removed": False, "fileId": "G1",
               "file": {"id": "G1", "name": "Doc", "parents": ["ROOT"],
                        "mimeType": "application/vnd.google-apps.document",
                        "trashed": False}},
          ],
      }
      results = list(iter_remote_changes(
          _changes_service(page), _passthrough_retry, cache, "CURSOR"
      ))
      assert results[0].entries == []
      assert cache.get_entry("G1") is None


  def test_iter_changes_drops_unknown_delete():
      cache = _cache()
      page = {
          "newStartPageToken": "NEXT",
          "changes": [{"removed": True, "fileId": "NEVER_SEEN"}],
      }
      results = list(iter_remote_changes(
          _changes_service(page), _passthrough_retry, cache, "CURSOR"
      ))
      assert results[0].entries == []


  def test_iter_changes_paginates():
      cache = _cache()
      page1 = {
          "nextPageToken": "p2",
          "changes": [
              {"removed": False, "fileId": "F1",
               "file": {"id": "F1", "name": "a.pdf", "parents": ["ROOT"],
                        "mimeType": "application/pdf", "trashed": False,
                        "size": "1", "version": 1}},
          ],
      }
      page2 = {
          "newStartPageToken": "NEXT",
          "changes": [
              {"removed": False, "fileId": "F2",
               "file": {"id": "F2", "name": "b.pdf", "parents": ["ROOT"],
                        "mimeType": "application/pdf", "trashed": False,
                        "size": "1", "version": 1}},
          ],
      }
      results = list(iter_remote_changes(
          _changes_service(page1, page2), _passthrough_retry, cache, "CURSOR"
      ))
      assert len(results) == 2
      assert results[0].has_more is True
      assert results[0].cursor == "p2"
      assert results[1].has_more is False
      assert results[1].cursor == "NEXT"
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_changes.py -k iter_changes -v`
  Expected: FAIL — `ImportError: cannot import name 'iter_remote_changes'`.

- [ ] **Step 3: Implement `iter_remote_changes`**

  In `src/cumulus/changes.py`:

  a. Extend the imports — add to the existing `from .core import ...` line
     and the `native_docs` import:

  ```python
  from .core import DeletedMetadata, FileMetadata, FolderMetadata, ListFolderResult
  from .native_docs import GOOGLE_FOLDER_MIME_TYPE, is_google_native
  from .path_codec import compose_path, fold_path
  ```

  b. Extend `__all__` to add `"iter_remote_changes"`.

  c. Below `FILE_FIELDS`, add the change-feed field selector and page size:

  ```python
  # Page size for changes.list. 100 keeps each cache-write batch modest.
  _CHANGE_PAGE_SIZE = 100

  # Field selector for changes.list — the file sub-resource mirrors
  # FILE_FIELDS so projected metadata is identical to the listing path.
  _CHANGE_FIELDS = (
      f"nextPageToken, newStartPageToken, "
      f"changes(removed, fileId, file({FILE_FIELDS}))"
  )
  ```

  d. Add the function at the end of the module:

  ```python
  def iter_remote_changes(
      service_factory: "Callable[[], Any]",
      retry: "Callable[[Callable[[], Any]], Any]",
      path_cache: "PathIDCache",
      last_cursor: str,
  ) -> "Iterator[ListFolderResult]":
      """Page Drive's ``changes.list`` and yield one
      :class:`~cumulus.core.ListFolderResult` per page.

      Each yielded entry has its path resolved via *path_cache*, and the
      cache is mutated as a side effect (``set`` / ``move`` / ``remove``).
      Native docs and shortcuts are dropped. The cursor on each page is
      the next ``pageToken``; on the final page it is the
      ``newStartPageToken`` to resume polling from.

      :param service_factory: zero-arg callable returning a Drive service.
      :param retry: the client's ``_retry_transient`` helper.
      :param path_cache: the path cache to resolve and mutate.
      :param last_cursor: the page token to start from.
      """
      cursor = last_cursor
      while True:
          resp = retry(
              lambda c=cursor: service_factory()
              .changes()
              .list(
                  pageToken=c,
                  fields=_CHANGE_FIELDS,
                  pageSize=_CHANGE_PAGE_SIZE,
                  includeRemoved=True,
              )
              .execute()
          )
          out_entries: list[Any] = []
          for change in resp.get("changes", []):
              decision = classify(change, path_cache)
              if decision is ChangeDecision.UNKNOWN_FILE:
                  logger.debug("Skipping malformed change entry: %r", change)
                  continue
              if decision is ChangeDecision.OUT_OF_SCOPE:
                  continue
              if decision is ChangeDecision.DELETE:
                  path = path_cache.id_to_path(change["fileId"])
                  if path is None:
                      logger.debug(
                          "Delete for unknown fileId %s; nothing to remove",
                          change.get("fileId"),
                      )
                      continue
                  path_cache.remove(path)
                  out_entries.append(
                      DeletedMetadata(
                          name=path.rsplit("/", 1)[-1],
                          path_lower=fold_path(path),
                          path_display=path,
                      )
                  )
                  continue
              # decision is APPLY.
              file = change["file"]
              mime = file.get("mimeType")
              # is_google_native already covers shortcuts via the wildcard.
              if is_google_native(mime):
                  continue
              parents = file.get("parents") or []
              parent_id = next(
                  (p for p in parents if path_cache.id_to_path(p) is not None),
                  None,
              )
              # classify() guaranteed at least one in-scope parent.
              parent_path = path_cache.id_to_path(parent_id)
              new_path = compose_path(parent_path, file["name"])
              old_path = path_cache.id_to_path(file["id"])
              is_folder = mime == GOOGLE_FOLDER_MIME_TYPE
              if old_path is None:
                  path_cache.set(
                      new_path, file["id"], parent_id, file["name"], is_folder
                  )
              elif old_path != new_path:
                  path_cache.move(old_path, new_path)
              # else: pure content update; cache path already correct.
              if is_folder:
                  out_entries.append(to_folder_metadata(file, new_path))
              else:
                  out_entries.append(to_file_metadata(file, new_path))

          next_token = resp.get("nextPageToken")
          new_start = resp.get("newStartPageToken")
          cursor = next_token or new_start or ""
          yield ListFolderResult(
              entries=out_entries,
              has_more=bool(next_token),
              cursor=cursor,
          )
          if not next_token:
              break
  ```

  e. Replace the top-of-file typing import line (`from typing import
     TYPE_CHECKING, Any`) with:

  ```python
  from typing import TYPE_CHECKING, Any, Callable, Iterator
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_changes.py -v`
  Expected: PASS (all tests — projection, classify, and iter_remote_changes).

- [ ] **Step 5: Commit**

  ```bash
  git add src/cumulus/changes.py tests/offline/test_changes.py
  git commit -m "feat(stage-3/changes): iter_remote_changes with cache write-through"
  ```

---

## Task 17: `client.py` — `list_remote_changes_iterator` + `list_remote_changes`

**Spec ref:** §4 (`list_remote_changes_iterator`), §6.

**Files:**
- Modify: `src/cumulus/client.py`
- Test: `tests/offline/test_client.py` (append)

- [ ] **Step 1: Write the failing tests**

  Append to `tests/offline/test_client.py`:

  ```python
  # ==== list_remote_changes(_iterator) ======================================

  def test_list_remote_changes_iterator_delegates(client, cache):
      from cumulus.core import FileMetadata

      service = fake_service()
      service.changes.return_value.list.return_value.execute.return_value = {
          "newStartPageToken": "NEXT",
          "changes": [
              {"removed": False, "fileId": "F1",
               "file": {"id": "F1", "name": "a.pdf", "parents": ["ROOT"],
                        "mimeType": "application/pdf", "trashed": False,
                        "size": "1", "version": 1}},
          ],
      }
      client._get_service = Mock(return_value=service)

      pages = list(client.list_remote_changes_iterator("CURSOR"))
      assert isinstance(pages[0].entries[0], FileMetadata)
      assert cache.path_to_id("/a.pdf") == "F1"


  def test_list_remote_changes_iterator_converts_http_error(client, make_http_error):
      service = fake_service()
      service.changes.return_value.list.return_value.execute.side_effect = (
          make_http_error(404)
      )
      client._get_service = Mock(return_value=service)

      from cumulus.exceptions import NotFoundError

      with pytest.raises(NotFoundError):
          list(client.list_remote_changes_iterator("CURSOR"))


  def test_list_remote_changes_flattens(client, cache):
      service = fake_service()
      service.changes.return_value.list.return_value.execute.return_value = {
          "newStartPageToken": "NEXT", "changes": [],
      }
      client._get_service = Mock(return_value=service)

      result = client.list_remote_changes("CURSOR")
      assert result.entries == []
      assert result.cursor == "NEXT"
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `python -m pytest tests/offline/test_client.py -k remote_changes -v`
  Expected: FAIL — `NotImplementedError` from the stubs.

- [ ] **Step 3: Implement the delegation methods**

  In `src/cumulus/client.py`, replace the `list_remote_changes` and
  `list_remote_changes_iterator` method bodies:

  ```python
      def list_remote_changes(self, last_cursor: str) -> ListFolderResult:
          """Lists changes since *last_cursor*, exhausting all pages."""
          pages = list(self.list_remote_changes_iterator(last_cursor))
          return self.flatten_results(pages)

      def list_remote_changes_iterator(
          self, last_cursor: str
      ) -> Iterator[ListFolderResult]:
          """Lists remote changes as a paginated iterator.

          Delegates to :func:`cumulus.changes.iter_remote_changes`, which
          classifies each change, resolves paths, mutates the path cache,
          and strips Google-native documents. HTTP errors raised mid-stream
          are converted to Cumulus exceptions.
          """
          self._ensure_root_id()
          with convert_api_errors():
              yield from changes.iter_remote_changes(
                  self._get_service,
                  self._retry_transient,
                  self._path_cache,
                  last_cursor,
              )
  ```

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `python -m pytest tests/offline/test_client.py -k remote_changes -v`
  Expected: PASS (3 tests).

- [ ] **Step 5: Run the full Stage 3 test surface**

  Run:
  ```bash
  python -m pytest tests/offline/test_client.py tests/offline/test_changes.py \
    tests/offline/test_native_docs.py tests/offline/test_errorhandling.py \
    tests/offline/test_errorhandling_auth.py tests/offline/test_client_auth.py -v
  ```
  Expected: all PASS.

- [ ] **Step 6: Commit**

  ```bash
  git add src/cumulus/client.py tests/offline/test_client.py
  git commit -m "feat(stage-3/client): list_remote_changes(_iterator) delegate to changes.py"
  ```

---

## Task 18: Manual smoke-test checklist + plan-deviations file

**Spec ref:** §9.7, §9.8.

**Files:**
- Create: `docs/superpowers/plans/2026-05-12-stage-3-read-client-deviations.md`

- [ ] **Step 1: Create the deviations / smoke-test file**

  Create `docs/superpowers/plans/2026-05-12-stage-3-read-client-deviations.md`:

  ```markdown
  # Stage 3 — Read client — Plan deviations & smoke-test record

  This file records (a) any deviations from the spec/plan made during
  implementation, and (b) the result of the manual smoke test required by
  master design §7 / Stage 3 spec §9.8.

  ## Plan deviations

  | # | Deviation | Reason |
  |---|---|---|
  | _(record any here as implementation proceeds)_ | | |

  Known intentional deviations baked into the plan:

  - **`RETRYABLE_403_REASONS` / `RETRYABLE_HTTP_STATUS` live in
    `errorhandling.py`, not `client.py`.** Spec §7.2 showed them in
    `client.py`; they were moved to `errorhandling.py` so both the retry
    helper (client) and the HTTP mapping table (errorhandling) share one
    definition without a circular import.
  - **Projection helpers live in `changes.py`.** Spec §3.1 already places
    "FileMetadata projection" in `changes.py`; this keeps the
    `client.py → changes.py` dependency one-directional.
  - **No `files.get` fallback in the change feed.** Per spec §6.4 — no
    concrete trigger for it exists; unknown file IDs are dropped with a
    debug log.
  - **Client wiring is in `Maestral.__init__` (`main.py`), not
    `Manager.__init__`.** Spec §3.3 said "Manager"; `Manager` does not
    construct the client in the current codebase.

  ## Manual smoke test

  Run once against the real Google smoke-test account when implementation
  is complete. Construct the client directly (the daemon/Maestral path is
  not required) — e.g. a `python -c` script or a scratch script that
  builds a `GoogleDriveClient` with a fresh `PathIDCache`.

  - [ ] **1. Link.** `cumulus auth link` against the smoke-test account
    succeeds (Stage 2 path).
  - [ ] **2. Bootstrap.** From a fresh DB, call
    `list_folder('/', recursive=True)`. The path cache populates with a
    count matching the account; native docs / Computers / shared-with-me
    are dropped; the INFO "Indexed N / skipped M" log appears.
  - [ ] **3. Cursor.** `get_latest_cursor('/')` returns a token; record it.
  - [ ] **4. Mutate on drive.google.com.** Create a folder, upload a file
    into it, rename a file, trash a file, move a file out of My Drive.
    Wait ~30 s.
  - [ ] **5. Change feed.** `list_remote_changes_iterator(<token>)`:
    confirm the create / rename / trash / move-out each produce the
    expected `FileMetadata` / `DeletedMetadata`, and the path cache
    reflects the new state.
  - [ ] **6. Download.** `download()` one changed file; confirm byte
    equality against a parallel web-UI download and that MD5 verification
    logs success.

  **Result:** _(record PASS/FAIL per item, date, and account here)_
  ```

- [ ] **Step 2: Verify the offline suite has no new collection errors**

  Run: `python -m pytest tests/offline/ -q`
  Expected: `test_client.py` and `test_errorhandling.py` now collect and
  pass. Pre-existing failures in `test_manager.py`, `test_main.py`,
  `test_event_handler.py`, `test_daemon.py` may remain (Stage 5/6 work) —
  confirm the count has not grown relative to the Task 4 Step 5 baseline.

- [ ] **Step 3: Commit**

  ```bash
  git add docs/superpowers/plans/2026-05-12-stage-3-read-client-deviations.md
  git commit -m "docs(stage-3): smoke-test checklist and deviations file"
  ```

- [ ] **Step 4: Run the manual smoke test**

  Perform the six smoke-test items above against the real account, record
  results in the deviations file, and commit:

  ```bash
  git add docs/superpowers/plans/2026-05-12-stage-3-read-client-deviations.md
  git commit -m "docs(stage-3): record manual smoke-test results"
  ```

---

## Done criteria

Stage 3 is complete when:

1. This plan's tasks are all committed to `google-drive-port`.
2. `python -m pytest tests/offline/test_client.py tests/offline/test_changes.py tests/offline/test_native_docs.py tests/offline/test_errorhandling.py` is all-green.
3. The two pre-existing collection errors (`test_client.py`, `test_errorhandling.py`) are cleared, and the offline suite has no new failures beyond the Stage 5/6 baseline.
4. The manual smoke test (§9.7 of the spec) is recorded in the deviations file.
