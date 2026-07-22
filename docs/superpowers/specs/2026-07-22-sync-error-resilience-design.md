# Sync error resilience: timeouts, unexportable files, large exports

**Date:** 2026-07-22
**Status:** Approved (design); fallback endpoint live-verified

## Problem

A single startup sync pass (observed 2026-07-21 on `b6ec0bb`) surfaced
three distinct failure classes:

1. **Fatal:** a socket read timeout during an export download escaped
   retry handling, was converted to a generic `BoraApiError`, and hit the
   manager's catch-all handler — which stops the daemon *without*
   re-arming autostart. One dropped read pauses sync permanently.
2. **Persistent, retried forever:** files whose owner disabled
   download/copy (`cannotExportFile`) fail on every sync cycle. They can
   never succeed, but `startup_worker` re-attempts each recorded
   download error every cycle (~1–9 s of wasted export attempts per file
   per cycle).
3. **Persistent, but fixable:** files over the Drive API's ~10 MB export
   limit (`exportSizeLimitExceeded`) fail the same way — yet the content
   is actually retrievable through the browser-facing export endpoint.

## Fixes

### 1. Classify socket-level errors as connection errors

`CONNECTION_ERRORS` (`errorhandling.py`) lists only `requests`
exceptions and `builtins.ConnectionError`, but googleapiclient performs
I/O through httplib2 — raw socket exceptions leak through untyped.

Add to `CONNECTION_ERRORS`:

- `builtins.TimeoutError` (also covers `socket.timeout` on Python ≥3.10)
- `http.client.HTTPException` (covers `IncompleteRead`, `BadStatusLine`;
  `RemoteDisconnected` is already a `ConnectionResetError`)
- `socket.gaierror` (DNS failure while offline)
- `httplib2.HttpLib2Error` (covers `ServerNotFoundError`)

No manager changes needed. `_retry_transient` (`client.py`) then retries
these with its existing backoff; when retries are exhausted,
`convert_api_errors` maps them to bora's `ConnectionError`, which
`manager.py` already handles correctly: log DISCONNECTED, stop, re-arm
autostart, resume on reconnect.

### 2. Skip persistently-unexportable files until the remote changes

Decision: **retry only on remote change** (not retry-every-cycle, not a
manual exclusion list).

- Add a nullable `remote_rev` column to `SyncErrorEntry`
  (`models.py`), populated with the remote file's `version` (fall back
  to `modifiedTime`) when a `RestrictedContentError` is recorded.
  Existing DBs migrate via `ALTER TABLE ... ADD COLUMN` (nullable, so
  old rows simply retry once and re-record).
- The startup retry loop (`manager.py` `startup_worker`) and download
  cycle skip a path whose recorded error is a `RestrictedContentError`
  with an unchanged remote rev. The comparison uses metadata the change
  feed / listing already provides — no export attempt, no extra API
  call.
- A differing rev clears the skip and retries for real; success removes
  the error entry (existing behavior).
- Logging: one INFO summary per cycle ("skipping N files Google Drive
  refuses to export (unchanged since last attempt)"); per-file detail at
  DEBUG. Entries remain visible in `bora status` / errors listing.

### 3. Too-large exports: docs.google.com fallback

**Live-verified 2026-07-22** against the linked `drive-inspect` test
account with a 22 MiB uploaded deck/doc:

- API `files.export_media`: HTTP 403 "This file is too large to be
  exported."
- `GET https://docs.google.com/presentation/d/<id>/export?format=pptx`
  with the same OAuth bearer token: HTTP 200, full 23.6 MB, correct
  MIME type, file opens as valid pptx with all slides.
- `format=md` on `/document/d/<id>/export` works and is byte-identical
  to the API's `text/markdown` export.

Implementation, in `_download_export` (`client.py`):

- Catch the `exportSizeLimitExceeded` HttpError and retry once via
  `https://docs.google.com/{kind}/d/{file_id}/export?format={ext}`,
  where `kind` maps from the native MIME type
  (document / spreadsheets / presentation) and `ext` is the same export
  extension bora already selects (`export_target`).
- Authenticate with the client's existing credentials (bearer token,
  refreshed if expired). Stream to the `.bora-part` tmp file with the
  same chunk size, throttle, hash, and atomic-rename structure as the
  primary path.
- Any failure of the fallback (non-200, connection error) raises the
  existing `RestrictedContentError` so fix 2's skip-until-change takes
  over. The endpoint is unofficial: degrade, never crash.

### 4. Download-disabled files: `.url` placeholder

Decision: write a link placeholder rather than skipping silently.

- When a `cannotExportFile` refusal is recorded, write an
  `[InternetShortcut]`-format `.url` file (openable on macOS and Linux)
  pointing at the doc's `webViewLink` (add to `FILE_FIELDS` if absent).
  Name: the projected export leaf name with `.url` appended in place of
  the export extension — `Gambit Guide.md` → `Gambit Guide.url` — so the
  placeholder never collides with a future real export of the same file.
- Index the placeholder as the synced local representation of the
  remote file: it must not upload back to Drive, must not register as a
  local change, and is deleted when the remote file is deleted or when
  it becomes exportable (rev change → fix 2 retries → the real export is
  written and the placeholder removed).
- Local edits/deletes of a placeholder are ignored (never pushed);
  a deleted placeholder is rewritten on the next cycle where the error
  entry still stands.

## Testing

- Unit: `TimeoutError` (and `IncompleteRead`, `ServerNotFoundError`)
  from a wrapped call → retried by `_retry_transient` → converted to
  `ConnectionError`, and manager transitions to DISCONNECTED + autostart
  instead of pausing.
- Unit: recorded `RestrictedContentError` with unchanged rev → no
  download attempt; changed rev → retry; success clears entry.
- Unit: fallback URL construction per native MIME type; fallback
  failure → `RestrictedContentError` (no crash); tmp-file cleanup on
  abort.
- Unit: placeholder write, no-upload, ignore-local-edit, cleanup on
  remote delete, replacement on successful export.
- Live: verified manually 2026-07-22 (22 MiB deck/doc uploaded to the
  linked test account; API export 403s, fallback returns full valid
  content). No automated linked test — generating a >10 MB native file
  needs image libraries that are not test dependencies, and a 22 MB
  upload per suite run is not worth it; the offline tests cover the
  fallback mechanism.

Ordering: fix 1 → 2 → 3 → 4, one commit each, `bin/test` + `bin/lint`
green at each step.

## Out of scope

- Retry-policy refactor of the wider error-handling machinery.
- Placeholders for other unsyncable classes (abusive-file flag,
  unsupported native types) — can adopt the same mechanism later.
