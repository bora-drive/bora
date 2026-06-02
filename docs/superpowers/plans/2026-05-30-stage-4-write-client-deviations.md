# Stage 4 — Plan deviations and smoke-test record

**Plan:** `docs/superpowers/plans/2026-05-30-stage-4-write-client.md`
**Spec:** `docs/superpowers/specs/2026-05-30-stage-4-write-client-design.md`

## Implementation summary

Stage 4 is complete: the write client landed across 11 plan tasks plus
six follow-up fix/refactor commits, totalling 17 commits on
`google-drive-port`. The full offline suite stands at 399
passing (62 new Stage 4 tests above the Stage 3 baseline of 337),
with the same pre-existing failures in `test_manager.py` (collection
error: `FullAccount.__init__() got an unexpected keyword argument
'email_verified'`) and `test_event_handler.py` (5 fixture errors:
`FileExistsError` on `/home/exedev/dummy_dir`) carried over from
earlier stages.

`src/cumulus/writes.py` is 791 LOC (the spec §3.5 target was
400–550 LOC — the actual is over the upper bound; see "Deviations from
the spec" below). `src/cumulus/client.py` is 1203 LOC, within
the spec target.

Stage 4 test distribution:
- `tests/offline/test_writes.py`: 52 tests, all pass
- `tests/offline/test_client.py`: 56 tests, all pass (8 new delegator
  tests added in Task 2, net +7 after removal of
  `test_restore_raises_not_implemented` in Task 10)
- `tests/offline/test_errorhandling.py`: 26 tests, all pass (2 new
  412 tests added in Task 1)

## Deviations from the plan

### `PathIDCache.set` signature

The plan's test templates seeded the cache with `cache.set("/foo", "F1")` —
a 2-arg form. The actual `PathIDCache.set` takes 5 args:
`(remote_path, file_id, parent_id, name, is_folder)`. Tests were updated
throughout to use the full signature. Implementation call sites
(`make_dir`, `upload`-create, `make_dir_batch` callbacks) also use the
5-arg form.

### `PathIDCache.set` conflict semantics

The plan assumed `cache.set` would overwrite on duplicate. In reality,
`cache.set` rejects writes where a different `file_id` already occupies
the same case-folded path (returns False, logs a warning). The
`make_dir` and `upload` flows handle this transparently — the Drive
call still goes through, but the cache keeps the old ID. The test
`test_make_dir_collision_without_autorename_creates_duplicate_name`
asserts the conflict-reject behavior.

### Autorename encoding

The plan's autorename implementation worked in the decoded namespace.
A code-quality review of Task 3 identified that `path_cache` stores
paths in *encoded* form (per `path_codec.encode_name`). The autorename
helper was refactored to probe and return encoded segments (commit
`2c6041a`), and the autorename tests were updated to assert encoded
forms (`/foo %281%29` rather than `/foo (1)`).

### `client.py` keyword-only enforcement

The Stage-1/2 stub signatures on `GoogleDriveClient.upload`, `move`,
and `make_dir` predated spec §3.3 and did not have the `*` separator
for keyword-only args. Task 2's code review caught this; commit
`524f50d` added the `*` separators to match the spec.

### `_resolve_file_id` and `_wrap_metadata` shared helpers

The plan introduced these helpers inline within `remove` (Task 4) and
`move` (Task 5) respectively. During Task 4's review, both were
extracted as shared `_WriteHelper` methods (commit `ac48066`) so
that `move`, `upload`-update, `restore`, and `remove_batch` could
reuse them.

### `_md5_file` placement and `usedforsecurity=False`

Code-quality review of Task 6 flagged that `_md5_file(local_path)` is
called before the simple-vs-resumable branch, but for the resumable
path the MD5 verify needs the value too — so the placement is correct
for both paths. Task 7 added `usedforsecurity=False` to match
`client.download`'s pattern.

## Deviations from the spec

### `writes.py` over the LOC target

Spec §3.5 targeted `writes.py` at 400–550 LOC; actual is 791 LOC.
The over-run accumulated from each per-op task adding a callback or
projection helper. The methods are individually focused and well-tested;
no single op is too large. A consolidation pass could collapse the two
batch-callback factories (`_make_dir_batch_callback`,
`_make_remove_batch_callback`) and reduce `_wrap_metadata` /
`_project_file_metadata` overlap. Deferred to a Stage-5 cleanup if
the file becomes hard to navigate.

### Spec §6.1 — most write-side error rows already covered by Stage 3

Only the 412 `conditionNotMet` row was genuinely new (commit `2ea68c7`).
The other four rows the spec called out (`403 storageQuotaExceeded`,
`403 insufficientFilePermissions`, `404 on update target`, `400 badRequest`)
all fall through to existing Stage 3 mappings that produce the right
exception types. The spec was conservative; the actual additions
needed were smaller.

### `batch_size` parameter inconsistency

`remove_batch(entries, batch_size=100)` accepts a caller-overridable
chunk size; `make_dir_batch` uses a fixed `_BATCH_LIMIT`. Both behaviors
match the plan's signatures, but the surface inconsistency was flagged
in Task 9's review. Not fixed in Stage 4 — defer to Stage 5/6 if a
caller actually needs to tune the batch size.

## Risks and edge cases noted during implementation

- **Tempfile leak window in `_download_revision_to_tempfile`** — if the
  download itself raises mid-stream (rare; transient errors are retried),
  the tempfile created at the top of the helper is orphaned. The
  `restore` outer `try/finally` only runs after the helper returns.
  Low-probability leak; could be tightened with a nested `try/except`
  in the helper.

- **No bandwidth throttle in restore's revision download** — Stage 3
  `download` calls `_throttle_down` between chunks; `_download_revision_to_tempfile`
  does not. Restore is rare and user-initiated; not throttling on it
  is acceptable for now.

- **`make_dir_batch` parent-ordering assumption** — per spec §4.5,
  `make_dir_batch` does not order or backfill parent folders. The sync
  engine creates folders top-down. If a child precedes its parent in a
  batch input, the child's sub-request gets a `NotFoundError` in its
  result slot. Documented.

- **No empty-chunk guard in `remove_batch`** — `make_dir_batch` skips
  `batch.execute()` if `scheduled_count == 0`. `remove_batch` filters
  failures into a `live` list before chunking, so no chunk can be
  empty. Behaviorally equivalent; surface inconsistency only.

- **No `folder + parent_rev` batch test** — single-entry `remove` has
  `test_remove_parent_rev_on_folder_raises_bad_input` (Task 4) for this
  path. The batch implementation (`remove_batch` lines 486–492) handles
  the case but the batch-path test is missing. Low-risk; the logic is a
  near-clone of the single-entry path.

- **No bandwidth-throttle in resumable upload** — Spec §4.1 step 7 says
  "report progress, honor the bandwidth throttle, honor `_poll_event`
  between chunks." `_upload_resumable` honors `_poll_event` and
  `sync_event` but never calls a `_throttle_up` (which would mirror
  `client.download`'s `_throttle_down`). No `_throttle_up` exists on
  the client today. Acceptable for Stage 4 (no upload throttle has
  been specced project-wide); Stage 5 should add one when the sync
  engine surfaces real bandwidth policy.

- **`writes.move` does not check `path_cache.move()`'s return value** —
  `PathIDCache.move(old, new)` returns False on a case-folded path
  conflict at the destination (a different `file_id` already occupies
  the destination's case-folded key). In that case, the Drive call has
  already mutated the remote, but the local cache still maps the old
  path → file_id. The sync engine would see a phantom "remote item
  exists at old path" until the change feed corrects it. The Task 10
  polish commit (`3206598`) adds a WARNING log when this
  happens so the desync mode is observable.

- **`move` lacks an `update_rev` parameter** — `upload(WriteMode.Update,
  update_rev=...)` and `remove(parent_rev=...)` both let the caller
  precondition the mutation on the current `headRevisionId`. `move`
  does not. Stage 5's sync engine may want a "rename only if the file
  is still revision X" guard; without it, the engine has to do a
  separate `files.get` + check before calling `move`, which races.
  Adding `update_rev` to `move` is a localized change but the API
  surface is frozen for Stage 4. Defer to Stage 5 or 6.

- **"Shouldn't happen" backfill in batch returns** — Both
  `make_dir_batch` and `remove_batch` end with
  `CumulusApiError("batch slot empty", "")` for any slot that somehow
  wasn't filled. The error carries no `remote_path` and no useful
  message — if it ever fires (e.g. `googleapiclient` drops a callback),
  the caller sees a near-useless error. Low-probability defensive
  code; harden in Stage 5 if it ever fires.

## Smoke test checklist

Run against the real `cumulus@arko.net` Drive account, ideally on a
freshly-bootstrapped sync root (or a dedicated subfolder). Record
PASS/FAIL per item with a timestamp and any observations.

> **Important:** do not run `pytest tests/offline` while a smoke-test
> account is linked — the keyring test fixture collides and wipes the
> linked credentials. See memory `feedback_keyring_test_collision.md`.

- [x] **Upload — small (<5 MB).** Create a local 1 KB text file in the
      sync root; verify it appears in Drive with matching content and
      `md5Checksum`. Confirm `path_cache.path_to_id` resolves it.
- [x] **Upload — large (>5 MB).** Create a local 10 MB random binary;
      verify the resumable code path runs (logs show chunked progress),
      the upload completes, and the MD5 matches.
- [x] **Move.** Move a file across folders and rename in the same op
      (`/folder-a/foo.txt → /folder-b/bar.txt`). Verify the Drive item's
      `parents` updated and the local cache reflects the new path.
- [x] **Trash (default `remove`).** Delete a file via the client;
      verify Drive shows `trashed: true` (still present in Trash) and
      `path_cache` no longer resolves the path.
- [x] **`make_dir`.** Create a folder; verify it appears on Drive and
      in the cache.
- [x] **`make_dir_batch` (≤3 paths).** Create three folders in one
      batch; verify all three appear; verify `_count_request` recorded
      3 sub-requests for 1 HTTP round-trip (instrument if needed).
- [x] **Restore.** Make two revisions of a small text file via two
      `upload(WriteMode.Update)` calls; then `restore(remote_path, rev)`
      where `rev` is the first revision's ID; verify the file's current
      content reverts to the first-revision bytes (and a *new* revision
      is created — restore does not roll back history).

**Result:** PASS — 2026-06-02, account `cumulus@arko.net`, config
`smoke-test`. All 7 items passed on the first run. No bugs surfaced.
The smoke-test script lived at `/tmp/stage4_smoke.py`; it constructed
a `GoogleDriveClient` directly with a fresh in-memory `PathIDCache`,
bootstrapped via `list_folder('/', recursive=True)` (8 in-scope items;
2 orphaned items skipped: a shared-test folder and its .DS_Store
child), then ran each item inside an isolated `/stage4-smoke-<ts>`
folder which was trashed via `client.remove()` on completion.

Observations from the run:

1. **Upload — small.** 864-byte text file. MD5 verify passed (`content_hash` field
   on returned `FileMetadata` matched local MD5).
2. **Upload — large.** 6,291,473-byte payload (~6 MB + 17 B) hit the resumable
   path. Single chunk completed in 2.0s; MD5 verify passed; `size` field on
   the response matched local size exactly.
3. **Move.** Combined cross-folder + rename (`/stage4-smoke-…/movable.txt`
   → `/stage4-smoke-…/subdir/moved-and-renamed.txt`). Drive's `files.update`
   handled both atomically; `fileId` unchanged; cache moved cleanly (old
   path → None, new path → fileId).
4. **Trash.** Default `remove()` issued `files.update(trashed: True)`. Cache
   cleared. (Did not re-fetch via Drive to confirm `trashed: true` flag,
   but the return-value `id` matched the original.)
5. **`make_dir`.** Single folder created; cache write-through verified.
6. **`make_dir_batch`.** 3 folders in 1 batch HTTP round-trip; `_request_count`
   increased by exactly 3 (sub-request-accurate quota accounting).
7. **Restore.** Two `upload(WriteMode.Update)` calls produced 2 revisions;
   `restore()` to the oldest revision succeeded; the file's current content
   reverted to the original bytes; `list_revisions()` count went from 2 to
   3 (restore added a NEW revision rather than rolling back history, per
   spec §4.6).

Cleanup `client.remove("/stage4-smoke-…")` succeeded; the smoke folder
is in Drive Trash, recoverable for 30 days.

## Stage 5 carry-over notes

(To be filled in once smoke tests run and reveal any genuine surprises.
Mirror the Stage 3 deviations file's "Notes for Stage 4 / Stage 5"
section structure.)

- **`writes.py` LOC consolidation** — if the file grows further in
  Stage 5 or the two batch-callback factories diverge more, consider
  extracting the batch helpers to a `_batch.py` module.
- **`_wrap_metadata` vs `_project_file_metadata`** — Stage 5 may
  consolidate these into one projection helper if usage patterns align.
- **Sync-engine echo suppression** — Stage 4 deliberately did not
  prevent the change feed from re-observing the client's own writes
  (cache mutations are idempotent so it's harmless at the cache layer,
  but the engine still gets duplicate change events). Stage 5 must add
  echo suppression via the cursor / pending-write set.
- **`changes.py` quota counter** — Stage 3 deviation noted
  `_count_request` is not threaded into `changes.py`. Stage 4 declined
  to expand scope into the read side; Stage 5 may pick it up.
