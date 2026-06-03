# Stage 5 — Plan deviations and smoke-test record

**Plan:** `docs/superpowers/plans/2026-06-03-stage-5-sync-engine.md`
**Spec:** `docs/superpowers/specs/2026-06-03-stage-5-sync-engine-design.md`
**Finding:** `docs/superpowers/plans/2026-06-03-stage-5-move-update-rev-finding.md`

## Implementation summary

Stage 5 (sync-engine integration) is complete and smoke-tested. The work
landed in 8 code commits on `google-drive-port` (`1c07f16..2ed6e33`),
on top of the 3 design/plan commits (`9ba38f1`, `bab6720`, `4b7da10`),
executed via subagent-driven-development (fresh implementer + spec/quality
review per task).

The offline suite stands at **418 passed** (Stage 4 baseline was 399; the
+19 are the Stage 5 hashing, conflict-resolution, echo-suppression,
download-seam, modifiedTime, close()-wiring, and NotFoundError-regression
tests), with the same pre-existing failures carried from earlier stages
(`test_manager.py` collection error; `test_event_handler.py` 5 fixture
errors — `FileExistsError` on `/home/exedev/dummy_dir`). Run offline tests
with `pytest tests/offline --ignore=tests/offline/test_manager.py -q`.

Commit map:
- `1c07f16` Task 1 — MD5 `ContentHasher` replacing `DropboxContentHasher`
- `eb96147` Task 2 — collapse `client_modified`/`server_modified` → `modified`
- `e22b865` Task 3 — `rev = headRevisionId` on the read side (echo-critical)
- `db709ed` Task 4 — download by fileId via `event.remote_id`
- `82a86cc` Task 5 — conflict resolution verified; `move(update_rev)` finding
- `b3d8522` Task 6 — set `modifiedTime` on upload; index records headRevisionId
- `d344a6f` Task 7 — use `GoogleDriveClient` directly; wire `client.close()`
- `2ed6e33` Task 8 fix — NotFoundError-as-upload-required (smoke-test finding)

## Deviations from the spec (corrections surfaced during planning)

Two spec assumptions were imprecise and were corrected before code (folded
into the spec, see its §3 "Correction" block):

1. **Download-by-id uses `event.remote_id`, not `event.change_remote_id`.**
   The master plan said `change_remote_id`, but that field holds the
   *account* that made the change (`modified_by`); the Drive fileId lives in
   `remote_id` (`models.py:492`, populated from `md.id`).

2. **`rev` must be `headRevisionId` on both read and write.** The write side
   (`writes.py`) already recorded `rev = headRevisionId`, but the read side
   (`changes.py::to_file_metadata`) recorded `rev = str(version)` (Drive's
   monotonic counter). They never matched, so `event.rev == local_rev` was
   always False on download — echo suppression silently degraded to the MD5
   fallback only. Fixed by adding `headRevisionId` to the shared `FILE_FIELDS`
   selector and switching the projection (Task 3). This is a latent-bug fix
   the planning pass caught before it could ship.

## Deviations from the plan (during implementation)

- **Task 1** — an extra in-scope reference (`tests/linked/unit/test_client.py`)
  still imports `DropboxContentHasher`. Left untouched: it is an auth-gated
  Dropbox-era test already broken on other names (`DropboxClient`). Stage 7
  (test-infra rewrite) owns it.
- **Task 2** — the plan listed the producers but missed one: `client.py:635`
  (`list_revisions` builds a `FileMetadata`). The implementer caught it via
  the grep gate; without it the tree would not import.
- **Task 5** — no conflict-resolution logic change was needed. All four
  `Conflict` outcomes plus the `_check_requires_upload` skip passed on the
  existing logic once `rev`/`content_hash` carried Drive values, confirming
  the heuristic was already correct under the new semantics. `move(update_rev)`
  resolved as **not load-bearing** (the rev guard lives on a separate
  `remove(parent_rev=...)`, not on `move`) — deferred to Stage 6.
- **Task 6** — `_on_local_file_modified` already recorded the upload response
  into the index (`sync.py:2520 update_index_from_remote_metadata(md_new)`),
  so the echo-suppression index path needed no change; only `modifiedTime` had
  to be added to the four upload-body sites (simple/resumable × create/update).

## Smoke-test findings (real bugs)

The end-to-end smoke test (`/tmp/stage5_smoke.py`, not committed) found **one
real engine bug**, fixed inline:

- **`_check_requires_upload` did not handle `NotFoundError` (`2ed6e33`).** The
  Stage 3 Drive `get_metadata` *raises* `NotFoundError` when a path is unknown
  to its cache (which is the case for any brand-new local file), whereas the
  engine expected a `None` return meaning "no remote item." Every new-file
  upload broke against a real Drive. The offline suite missed this entirely
  because its `_check_requires_upload` tests only ever monkeypatched
  `get_metadata` to *return* a value, never to *raise* — a textbook
  "smoke tests find bugs unit tests miss" case. Fix: catch `NotFoundError` →
  return True (remote absent → upload required), plus a regression test
  (`test_requires_upload_true_when_remote_not_found`) exercising the raising
  path.

The other initial smoke failures were **harness artifacts**, not engine bugs:

- **sqlite cross-thread error** — the harness gave the path-cache an in-memory
  sqlite without `check_same_thread=False`; the engine touches the cache from
  `ThreadPoolExecutor` workers. Production uses a file DB with that flag. Fixed
  in the harness.
- **change-feed propagation latency** — a single `download_sync_cycle()` fired
  immediately after a mutation races Drive's eventually-consistent change feed
  and sometimes misses the just-published change. The real daemon polls in a
  loop (`wait_for_remote_changes`), so it catches up on the next tick. The
  harness now polls with bounded retries (`download_until`). **Operational
  note for Stage 6/8:** changes made on Drive can take a few seconds to appear
  in `changes.list`; the polling-interval UX note (master plan D6) should
  mention propagation latency on top of the poll interval.
- **delete-simulation cache clobber** — the harness initially simulated an
  "external" remote delete with `client.remove()`, which clears our own path
  cache, so the change feed couldn't resolve the fileId→path and dropped the
  delete. A real external delete leaves our cache intact. Fixed by trashing via
  the raw Drive API in the test.

## Smoke-test result

**PASS — 7/7, 2026-06-03, account `cumulus@arko.net`, config `smoke-test`.**

The harness constructs a real `GoogleDriveClient` + `SyncEngine` against a
fresh temp local sync dir, primes `remote_cursor` to the current
`startPageToken` (so only changes made during the run are seen — the
pre-existing account is never pulled), then drives the cycles by hand
(`upload_local_changes_while_inactive` for upload, `download_sync_cycle` for
download). All Drive items created are trashed in a `finally` block.

| # | Check | Result |
|---|---|---|
| 1 | local→remote create (upload, md5 + index rev match) | PASS |
| 3/8 | echo suppression — echo change delivered by the feed yet skipped; no re-download, no conflict copy, local mtime unchanged | PASS |
| 2 | remote→local create (download) | PASS |
| 5 | remote modify → download (RemoteNewer) | PASS |
| 4 | local modify → upload (new revision) | PASS |
| 6 | delete propagation (local→Drive trash; external Drive delete→local remove) | PASS |
| 7 | conflict (both sides modified) — conflict copy `"…(Cumulus Tester's conflicted copy 2026-06-03).txt"` created, both versions preserved | PASS |

Echo suppression (the make-or-break) is confirmed both at the unit level
(`test_echo_suppression_records_headrevisionid_and_skips_download`) and
end-to-end (smoke check 3/8: the engine receives its own write back through
the change feed and skips it via the `event.rev == local_rev` short-circuit).

Keyring note: the offline-suite validation run was bracketed by a backup/
restore of `~/.local/share/python_keyring/keyring_pass.cfg` so the
`smoke-test` link survived (see memory `feedback_keyring_test_collision.md`).

## Stage 6 carry-over notes

- **D1** daemon RPC field-rename audit; **D4** GUI compat decision (shim vs
  documented break) — manager/CLI/interface work.
- `_throttle_up` / bandwidth policy; `batch_size` tuning; `writes.py`
  consolidation; `_wrap_metadata`/`_project_file_metadata` merge;
  `_count_request` threading into `changes.py`.
- **Full daemon/manager end-to-end run** — Stage 5 validated the `SyncEngine`
  cycles directly; the manager threads + connection monitor + startup
  sequencing are Stage 6, with the first full-daemon smoke test there.
- **Cross-page parent/child change-feed ordering** durability (Stage 3
  carryover) — did not surface in the Stage 5 smoke test (small, single-page
  change sets); durable fix in Stage 6 (cursor durability).
- **`move(update_rev=)`** — confirmed not load-bearing for Stage 5; add only
  if a rev-checked move precondition is actually needed in Stage 6.
- **Change-feed propagation latency** — document in the Stage 8 user-facing
  "differences from Maestral" note alongside the poll-interval latency.
- `tests/linked/` is a Dropbox-era holdover (imports `DropboxClient`,
  `DropboxContentHasher`, `client_modified`); rebuild in Stage 7 (test infra).
