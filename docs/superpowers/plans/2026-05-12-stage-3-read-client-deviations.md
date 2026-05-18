# Stage 3 — Read client — Plan deviations & smoke-test record

This file records (a) any deviations from the spec/plan made during
implementation, and (b) the result of the manual smoke test required by
master design §7 / Stage 3 spec §9.8.

## Plan deviations

All 17 code tasks were implemented as written in the plan. No structural
deviations occurred. Code-review fix-ups during execution were limited to
small polish (extra test assertions, doc comments, a sidecar-cleanup
hardening in `download`, an accurate bootstrap skip-count log message) and
did not change any design decision.

One code-review point was deliberately **not** actioned: the Task 5 review
flagged that `DataCorruptionError` / `NotFoundError` / `UnsupportedFileError`
are imported into `client.py` before the tasks that use them. This batching
is what the plan prescribes (the imports go live in Tasks 8 and 12 of the
same stage); the plan's decision was upheld to avoid threading
import-additions fragilely through later task dispatches.

Known intentional deviations baked into the plan (spec vs. plan):

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

## Notes for Stage 4 / Stage 5

- Stage 4 (write client) must implement `restore()` (currently a `NotImplementedError` stub) once `upload` exists.
- `Maestral.shutdown_daemon()` does not yet call `self.client.close()`. Stage 3 built the `_poll_event` shutdown-interrupt mechanism (`close()`/`unlink()` set it; `wait_for_remote_changes` and `_retry_transient` honor it), but nothing calls `client.close()` on daemon shutdown yet. Stage 5's manager/poll-loop wiring must add `self.client.close()` to `shutdown_daemon()` so a sync thread blocked in the poll sleep stops promptly.
- `list_folder_iterator(recursive=False)` yields `cursor=""` on its final page (it is not a change cursor). Only the `recursive=True` bootstrap yields a real change cursor (from `getStartPageToken`). Stage 5's sync engine must take the change-feed start cursor from the bootstrap, not from a non-recursive listing.
- `Maestral` opens a separate `Database`/`PathIDCache` for the client; `SyncEngine` has its own DB connection. Stage 5 consolidates these onto one shared `PathIDCache`.
- `_count_request` quota tracking does not cover `changes.list` calls made inside `changes.iter_remote_changes` (that module receives only `service_factory` + `retry`, not the counter). Stage 4/5 may thread the counter through if precise quota accounting is needed.
