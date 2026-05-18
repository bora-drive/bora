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

## Post-implementation fix — change-feed intra-page ordering

The smoke test (item 5) falsified a spec assumption. Spec §6.2's
per-page algorithm processed `changes.list` entries in Drive's delivery
order, and the risk register (design §10) bet that "Drive's
`changes.list` documents in-order delivery" so a folder-create always
precedes a file-move-into.

**It does not.** Against the real account, Drive delivered a file
created inside a brand-new folder (`smoke-D.txt`, change index 1)
*before* that folder's own change (`smoke-new`, index 6) in the same
page. The single-pass resolver classified the file as `OUT_OF_SCOPE`
(its parent was not yet in the path cache) and silently dropped it —
the file vanished from both the yielded entries and the path cache,
unrecoverable until a full re-bootstrap.

**Fix** (`changes.py`, `iter_remote_changes`): each page is now resolved
with a fixpoint instead of a single in-order pass. Terminal changes
(hard-delete, trash, malformed) are handled up front; the remaining
parent-dependent changes are replayed until a pass resolves nothing
new. A folder applied in one pass unblocks its children in the next.
The `_emit_delete` / `_emit_apply` helpers were extracted so both the
fixpoint and the terminal/leftover handling share one code path. The
fix also corrects a latent mis-delete: a file moved into a same-page
new folder is no longer mistaken for a move-out. `classify()` is
unchanged. Two regression tests added to `test_changes.py`
(`..._reversed_order`, `..._move_into_new_folder_is_not_a_delete`).

**Residual limitation:** a parent/child split *across* page boundaries
(>100 changes in one poll, parent on a later page) is still
delivery-order dependent. Fixing it needs cross-page buffering, which
interacts with the per-page cursor-durability contract — deferred to
Stage 5, where cursor persistence semantics live. Noted for that stage.

## Manual smoke test

Run once against the real Google smoke-test account when implementation
is complete. Construct the client directly (the daemon/Maestral path is
not required) — e.g. a `python -c` script or a scratch script that
builds a `GoogleDriveClient` with a fresh `PathIDCache`.

- [x] **1. Link.** `cumulus auth link` against the smoke-test account
  succeeds (Stage 2 path).
- [x] **2. Bootstrap.** From a fresh DB, call
  `list_folder('/', recursive=True)`. The path cache populates with a
  count matching the account; native docs / Computers / shared-with-me
  are dropped; the INFO "Indexed N / skipped M" log appears.
- [x] **3. Cursor.** `get_latest_cursor('/')` returns a token; record it.
- [x] **4. Mutate on drive.google.com.** Create a folder, upload a file
  into it, rename a file, trash a file, move a file out of My Drive.
  Wait ~30 s.
- [x] **5. Change feed.** `list_remote_changes_iterator(<token>)`:
  confirm the create / rename / trash / move-out each produce the
  expected `FileMetadata` / `DeletedMetadata`, and the path cache
  reflects the new state.
- [x] **6. Download.** `download()` one changed file; confirm byte
  equality against a parallel web-UI download and that MD5 verification
  logs success.

**Result:** PASS — 2026-05-18, account `cumulus@arko.net`.

1. **Link** — PASS. Used the `--no-local-server` paste flow
   (`get_auth_url` → `link(code=...)`).
2. **Bootstrap** — PASS. 6 in-scope items indexed; the Google Doc
   `smoke-doc` (`application/vnd.google-apps.document`) was dropped by
   `is_google_native` before the cache populate; a later run with an
   orphan present produced the `Indexed 6 … skipped 2` log. Note: the
   log's `skipped M` counts only `populate_from_listing` exclusions
   (Computers/Backup, shared-with-me, orphans, dup-names) — native docs
   are filtered earlier and are *not* in that count, so an account with
   no out-of-scope items logs the short `Indexed N items` form. The
   checklist's "Indexed N / skipped M" wording was imprecise; behaviour
   is correct.
3. **Cursor** — PASS. `get_latest_cursor('/')` returned a Drive
   `startPageToken`.
4. **Mutate** — done via drive.google.com (create folder + file-in-it,
   rename, trash, in-Drive reparent, and a move to a Shared Drive for
   the "move out of My Drive" case).
5. **Change feed** — PASS, **after a bug fix**. The first run dropped
   a file created inside a new folder; root-caused to the change-feed
   intra-page ordering assumption (see the post-implementation fix
   section above). After the fix, replaying the original problematic
   page (real Drive data, child delivered before parent) resolves the
   file correctly; rename → updated `FileMetadata`, trash and
   move-to-Shared-Drive → `DeletedMetadata`, in-Drive reparent →
   `FileMetadata` with the new path; the path cache tracks each.
6. **Download** — PASS. Downloaded a 17-byte file; the local MD5
   matched Drive's `content_hash` and `download()`'s internal MD5
   verification did not raise.

**Operational note (test hygiene, not a Stage 3 defect):** the offline
test suite shares the global `keyrings.alt.file.PlaintextKeyring` store
(`~/.local/share/python_keyring/keyring_pass.cfg`). Running
`pytest tests/offline` while a real account is linked under config name
`test-config` clobbered the live `account_id` keyring pointer (the
keyring/oauth tests reuse that config name). Recovery was possible —
the token blob survived and was re-linked via `link(refresh_token=…)` —
but the collision should be fixed: the offline tests need an isolated
config name or an isolated keyring backend. Worth a follow-up ticket.

## Notes for Stage 4 / Stage 5

- Stage 4 (write client) must implement `restore()` (currently a `NotImplementedError` stub) once `upload` exists.
- `Maestral.shutdown_daemon()` does not yet call `self.client.close()`. Stage 3 built the `_poll_event` shutdown-interrupt mechanism (`close()`/`unlink()` set it; `wait_for_remote_changes` and `_retry_transient` honor it), but nothing calls `client.close()` on daemon shutdown yet. Stage 5's manager/poll-loop wiring must add `self.client.close()` to `shutdown_daemon()` so a sync thread blocked in the poll sleep stops promptly.
- `list_folder_iterator(recursive=False)` yields `cursor=""` on its final page (it is not a change cursor). Only the `recursive=True` bootstrap yields a real change cursor (from `getStartPageToken`). Stage 5's sync engine must take the change-feed start cursor from the bootstrap, not from a non-recursive listing.
- `Maestral` opens a separate `Database`/`PathIDCache` for the client; `SyncEngine` has its own DB connection. Stage 5 consolidates these onto one shared `PathIDCache`.
- `_count_request` quota tracking does not cover `changes.list` calls made inside `changes.iter_remote_changes` (that module receives only `service_factory` + `retry`, not the counter). Stage 4/5 may thread the counter through if precise quota accounting is needed.
