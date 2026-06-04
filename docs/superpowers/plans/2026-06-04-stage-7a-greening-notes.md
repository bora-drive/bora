# Stage 7a greening — working notes (live session 2026-06-04)

Companion to `2026-06-04-stage-7a-linked-integration-greening.md`. Records the
real baseline residual + per-category design decisions as they're confirmed.

Token: minted fresh, links `cumulus-test@arko.net` (verified, not smoke).
Baseline run: `tests/linked/integration --tb=short --show-capture=no` → `/tmp/int_baseline.txt`.

## Offline design conclusions (pre-baseline, read-only)

### Task 4 — get_file_diff (category F): DECISION = FIX (not xfail)
- Bug: `main.py:954` + `:971` use Dropbox `f"rev:{rev}"` path syntax against
  Drive's path-cache `download`/`get_metadata` → NotFoundError.
- Used by real CLI command `cli/cli_maintenance.py:193` (`diff`), not just tests.
- Drive revs need **file_id + revisionId** together (Dropbox revs were globally
  unique; Drive's are not). Current signature `get_file_diff(old_rev, new_rev)`
  lacks the file_id → insufficient.
- Infra exists: `writes.py:794 _download_revision_to_tempfile(file_id, rev)` via
  `revisions().get_media`; `client.py:575 list_revisions(remote_path)` returns
  FileMetadata with `.id`=file_id and `.rev`=revisionId.
- Planned fix: change signature to `get_file_diff(remote_path, old_rev, new_rev=None)`.
  CLI already has `sync_path` (`cli_maintenance.py:142,149`); re-point the 6 tests
  (plan explicitly allows re-pointing). Resolve file_id from remote_path, download
  old/new revs via get_media, pull modifiedTime from list_revisions/revisions().get.
  Bogus rev + valid path → 404 → NotFoundError (satisfies test_get_file_diff_not_found).

### Task 2 — path-encoding (category A): NOT obviously a prod bug — verify on disk first
- Codec (`path_codec.py`): `encode_name` URL-encodes ALL non-unreserved incl. `ó`→`%C3%B3`.
  Cache stores ENCODED `remote_path`. By design the codec exists so unsafe Drive
  names (`/`, controls) are filesystem-safe.
- Both the download writer AND the test helper derive local path from
  `md.path_display` via the SAME `to_local_path*` transform (download apply
  `sync.py:1792`; helper `sync.py:2730`). So a pure encoded-vs-decoded mismatch
  *between them* is unlikely.
- A subagent claimed `changes.py:_emit_apply` storing encoded path_display is a
  prod bug (vs `core.py:126` docstring "for the local file system"). NOT taken at
  face value: the 470-test offline suite passes with current behavior.
- ACTION when baseline lands: sync a unicode file live, `ls` the sync dir to see
  the ACTUAL on-disk name (`fólder` vs `f%C3%B3lder`). That + how `assert_local_tree`
  enumerates the tree settles encoding-vs-timing. Plan Task 2 demands ruling out
  `wait_for_idle`/`wait_until` timing before concluding "encoding bug".

### Task 3 — conflict-copy/replace (categories B, E): mostly NOT string mismatches
- Generator: `utils/path.py:293 generate_cc_name` → `"{name} ({suffix}){ext}"`,
  then ` ({suffix} {i})` on collision. `sync.py:3498 _local_cc_filename` builds
  suffix `"{owner}'s conflicted copy {YYYY-MM-DD}"` (or no-owner variant).
- Most conflict tests ALREADY call `m.sync._local_cc_filename(...)` dynamically →
  expected name is correct by construction. So their failures are likely timing/
  state/behavioral, NOT the name string. Work from tracebacks.
- `test_selective_sync_conflict` still hardcodes `"selective sync conflict"` /
  `"... 1"` suffix — verify against the production selective-sync-conflict naming.
- `test_file_status`: plan's "'unwatched'=='error'" doesn't match code — `test_main.py:138`
  asserts Unwatched (missing path), `:166` asserts Error (backslash path). Check actual.
- `test_restore`: no naming/status assertion; restores old rev + checks content.
- E2-flagged in `test_sync.py` docstring: test_rapid_remote_changes,
  test_remote_file_replaced_by_folder_and_unsynced_local_changes,
  test_local_file_replaced_by_folder_and_unsynced_remote_changes,
  test_sync_event_conversion_performance, test_out_of_order_indexing.

## Real baseline residual (from /tmp/int_baseline.txt — 41 failed / 23 passed / 2 skipped)

Down from 47 (lock fix d0e03c6 cleared ~6). New non-bucketed failure surfaced:
`test_move_dropbox_folder` (FileExists, isolation). Re-bucketed by *traceback*:

### ROOT CAUSE #1 — remote poll interval (TEST HARNESS) — FIX FOUND
`client.wait_for_remote_changes` has no long-poll; it `_poll_event.wait(timeout)`
sleeps the FULL interval (sync.py hardcodes `timeout=180`; `DEFAULT_POLL_INTERVAL=180`)
before checking `changes.list`. `busy()` is only the sync-lock (False during that
sleep), so the fixture's `wait_for_idle` (6×1s) returns ~174s before the remote
worker even polls → download-direction assertions race the change feed. Maestral's
original used Dropbox **longpoll** (instant), so its `wait_for_idle` worked; the port
swapped in a fixed quota-driven sleep and never adapted the harness.
**Fix (test-only):** `m` fixture shrinks `m.sync.wait_for_remote_changes` timeout to
2s on the test instance (production keeps 180s). CONFIRMED: `test_rapid_remote_changes`
flips green; download "not found locally" assertions stop firing.
Likely-cleared by this: test_folder_tree_remote, test_rapid_remote_changes,
test_out_of_order_indexing, and the download halves of lifecycle/conflict tests.

### OPEN #2 — content-divergence / `_check_download_conflict` "Equal revs" skip
`test_file_lifecycle` phase 3 (remote overwrite) fails "different content": the change
feed reports the new rev but `_check_download_conflict` (sync.py:3235) sees
`event.rev == local_rev` → `LocalNewerOrIdentical` → SKIPS download, so local stays
stale. Logs show sustained `Syncing ↑`/`Syncing ↓` churn that doesn't settle within
the wait window. Download path sets `last_sync` correctly (update_index_from_sync_event,
1007), so the simple last_sync=None echo theory is ruled out. MECHANISM NOT YET PINNED
— candidates: (a) test's `create_remote_tree` mutates remote via the SAME `m.client`
the engine uses (shared path cache) + 2s poll → races the engine mid-cycle, not seen in
prod's 180s settle; (b) a real convergence/echo bug. NEEDS: decide whether it's harness
(→ condition-based `wait_until(synced)` instead of `wait_for_idle`) or engine.
Affects: test_file_lifecycle[both], test_remote_file_replaced_by_folder{,_*},
test_remote_folder_replaced_by_file, conflict/unicode/case tests,
test_local_and_remote_creation_with_different_content.

### #3 — get_metadata raises NotFound vs returns None (TEST/contract)
test_mignore (`assert not m.client.get_metadata("/foo")` → raises NotFoundError),
test_case_change_local/remote setup (`get_metadata("/folder")`). Drive's get_metadata
raises; tests assume Dropbox's None-return. Decide: catch in test, or make a
None-returning helper. (Also some are timing — verify post-pollfix.)

### #4 — error-injection sync_errors==0 (category D) — triage
test_local_path_error, test_local_permission_error, test_symlink_error,
test_symlink_indexing_error, test_local_indexing_error, test_long_path_error,
test_unix_permissions. Inject local-FS error conditions expecting 1 sync error; got 0
(or FileNotFound). Drive error-mapping gap vs VM-env-specific (permissions/path-len/
symlink) → fix or skipif-with-reason. test_long_path_error: 4000-char path fails at
parent-resolution (Drive has no path-length limit like local FS).

### #5 — test isolation / cleanup (FileExists)
test_move_dropbox_folder (`/home/exedev/New GoogleDrive` left from prior run),
test_local_folder_replaced_by_file (`folder` already in sync dir at os.mkdir). Teardown
of a failed test leaves local state; or `New GoogleDrive` target not cleaned.

### #6 — get_file_diff prod bug (Task 4) — FIX (see above). 6 tests.

### #7 — misc / per-test
test_restore (restored 'new' not 'old' — restore picks wrong rev or test rev order),
test_file_status (`'unwatched'=='error'` at test_main.py:166 — invalid-path → Error
not produced on Drive), test_excluded_folder_cleared_on_deletion (excluded_items not
pruned on deletion), test_selective_sync_conflict (extra local file.txt — hardcoded
"selective sync conflict" name + timing), test_invalid_pending_download (bogus path
stays in download_queue), test_parallel_deletion_when_paused (SyncError Not found Up),
test_remote_folder_replaced_by_file_and_unsynced_local_changes (real Drive
"Insufficient permissions /folder/subfolder" Up — investigate).

## ⚠️ KEYRING COLLISION (learned the hard way, again — see memory)
Running `pytest tests/offline` CONCURRENTLY with a linked-integration run corrupts
`~/.local/share/python_keyring/keyring_pass.cfg` (interleaved multi-line writes →
configparser ParsingError) → cascades dozens of bogus ERRORS in the integration run
AND makes 2 offline oauth tests fail. NEVER run offline + linked concurrently. Repaired
by truncating the .cfg to its last valid entry (backup at *.corrupt.bak). run3 was
invalidated by this (43 spurious errors); run4 is the clean re-measure.

## FINAL (run5, clean): 41 → 9 failed (54 pass / 3 skip). 9 failures all xfailed
with documented root causes (green-modulo-documented-xfail GATE REACHED):
- 4 conflict tests (replace-with-unsynced-changes): flaky/incomplete conflict
  resolution under Drive change-feed timing (local change lost / cc not created) → Stage-5.
- 2 case_change (dir case-only rename delivered as FileMovedEvent, missed by dir-move
  handling → unresolvable via get_metadata) → Stage-5.
- test_unicode_decomposed: Drive does NOT normalize NFD→NFC (Dropbox did) → Drive-behavior/Stage-1.
- test_long_path_error: flat slash-key deep path fails at parent resolution (Drive needs
  each parent; no remote path-length limit) → test-port/Drive-model.
- test_out_of_order_indexing: reversed change application leaves a child uncreated → Stage-5.
CONFIRMATION RUN (int_final) revealed FLAKINESS: 3 tests that passed in run5 failed
(test_file_conflict_modified, test_file_conflict_created — conflict resolution is
timing-flaky, not just the 4 already-xfailed; and test_file_lifecycle[fólder] — unicode
remote-deletion change-feed-lag flake). xfailed those too → 12 xfail total
(strict=False so flaky-pass = xpass, never an unexpected failure):
  - conflict cluster now 6: file_conflict_modified, file_conflict_created,
    remote_file_replaced_*, remote_folder_replaced_*, local_folder_replaced_*,
    local_file_replaced_* — Stage-5 conflict-resolution is unreliable under Drive's
    change-feed timing (local change lost / cc not created / not reconciled).
  - 2 case_change, out_of_order — Stage-5.
  - unicode_decomposed (Drive no NFD→NFC), long_path (Drive parent-model),
    test_file_lifecycle[fólder] param (unicode-deletion timing).
Final gate run /tmp/int_gate.txt pending. Offline 470 green throughout.
NET: ~32 tests fixed via real production+harness fixes; remainder xfailed-with-reason.
KEY HONEST CAVEAT: Drive conflict-resolution is genuinely flaky/incomplete — a real
Stage-5 follow-up (some scenarios lose the local change = data loss). Worth dedicated work.

## RUN4 RESULT: 41 → 13 failed (50 pass / 3 skip). Then fixed 7 of 13 via subsets:
- surrogate regression (encode_path/fold_path now pass through non-utf8 → check_encoding
  rejects): test_file_status, test_unknown_path_encoding PASS.
- writes.upload wraps local reads (MD5/mtime/size) in convert_api_errors → permission
  errors surface as InsufficientPermissionsError: test_local_permission_error PASS.
- symlink rejection already green in run4 (test_symlink_error/_indexing); test_local_indexing_error PASS.
- test_move_dropbox_folder PASS (leftover removed + teardown fix).
- teardown hardened: _force_delete_tree chmods 0o700 before delete (permission tests
  chmod 000 → delete failed → "test runner N" dir leak). Fixes recurring pollution.
RESIDUAL after those (run5 confirms): conflict cluster (4) + case_change (2) +
unicode_decomposed + long_path + out_of_order = the deep cluster.
  - Conflict cc-owner: engine uses event.change_remote_id (→ no-owner "conflicted copy"
    for mutator changes) but tests build expected via _local_cc_filename(account_id)
    (→ "Tester's conflicted copy"). test_remote_file_replaced: fixable by test using
    no-owner name. test_file_conflict_modified + test_remote_folder_replaced: local
    change LOST (no cc) — deep conflict-detection, Stage-5. test_local_folder_replaced:
    folder/file type mismatch (docstring: "remote folder not checked for unsynced").
  - case_change_local/remote: case-only rename on case-sensitive FS not resolvable via
    get_metadata after — Stage-5 move/case handling.
  - unicode_decomposed: expects Drive to normalize NFD→NFC and mirror locally; Drive
    may not normalize — Drive-behavior, Stage-1.
  - long_path: deep nested path created as one flat slash-key fails at parent resolution
    on Drive (Dropbox auto-created parents) — test-port/Drive-model.
  - out_of_order: reversed change application, /parent/child_2 not created — internals.

## MORE FIXES LANDED (post-mutator):
- symlink rejection (PROD): `_on_local_file_modified` raises SymlinkError when
  event.symlink_target is not None (Drive can't store symlinks; upload sets
  symlink_target=None). Fixes test_symlink_error/_indexing.
- idempotent remote-delete (PROD): `_on_local_deleted` wraps get_metadata in
  try/except NotFoundError→None. Fixes test_parallel_deletion_when_paused.
- invalid pending download (PROD): `get_remote_item` wraps get_metadata + uses
  fold_path. Fixes test_invalid_pending_download.
- `_ensure_parent` get_metadata wrap (PROD).
- test_restore → wait_until (timing). test_file_status + test_local_path_error →
  non-utf8 / skip (backslash valid on Drive). test_move_dropbox_folder → fixture
  teardown deletes current m.sync_path.
STILL TBD after run4: test_long_path_error (deep-path setup fails at parent resolution
on Drive — likely skip w/ reason), test_unix_permissions, test_local_permission_error,
test_case_change_local/remote, test_unicode_decomposed (NFD/NFC), test_out_of_order_indexing,
test_selective_sync_conflict, test_excluded_folder_cleared_on_deletion.

## FIXES LANDED (session 2026-06-04, going for full green)
Production (offline 470 still green after each):
1. path-normalization: `to_remote_path_lower`/`correct_case`/SyncEvent now fold via
   `fold_path` (was `normalize`=.lower(), lowercased hex) — sync.py + models.py.
2. encode/decode boundary: local FS = decoded/human-readable, virtual/index = encoded.
   `to_remote_path` → `encode_path`, `to_local_path_from_cased` → `decode_path`; new
   `path_codec.encode_path`/`decode_path`. (local files now `fólder` not `f%C3%B3lder`.)
3. unicode-conflict detection: sync.py:2238 compared `normalize_unicode` vs
   `normalize_case` (never matched NFC/NFD) → now `normalize_unicode` both sides.
4. `_correct_case_helper`: get_metadata raises NotFoundError for unknown paths; wrap
   try/except → None (it did `if md:` expecting None). Fixes test_local_indexing_error path.
5. get_file_diff: rewritten to take `remote_path` + address revisions by
   (file_id, revision_id) via `_resolve_file_id`+`_download_revision_to_tempfile`+
   `revisions().get`. CLI + 6 tests updated. ALL 6 get_file_diff tests PASS live.
Test-harness (conftest/test_sync):
6. poll interval 2s (was 180) on the test instance.
7. condition-based `assert_synced` (retry until synced, 90s) — kills change-feed-lag flakiness.
8. robust `clean_drive_dir` (delete by file_id) — isolation.
9. `RemoteMutator`: 2nd independent client (own cache) for ALL test-side remote
   mutations (create_remote_tree, remove_remote, replace tests) — models a real 2nd
   device; fixes deletion non-propagation (changes._emit_delete needs the engine's
   cache intact) and replace-semantics.
10. test_mignore: expect NotFoundError (Drive contract) not None.
11. assert_synced casing check: build local path via to_local_path_from_cased (decode).
CONFIRMED PASS live (subsets): test_setup, test_file_lifecycle[both], test_folder_tree_remote,
test_remote_file_replaced_by_folder, test_rapid_remote_changes, test_case_conflict,
test_unicode... (file_lifecycle[fólder]), all 6 get_file_diff.

## REMAINING (to fix after run3 residual — analysis done):
- test_parallel_deletion_when_paused: delete-already-deleted-remote → NotFound SyncError;
  needs idempotent remote-delete (treat NotFound on Up-delete as success). PROD.
- test_invalid_pending_download: bogus path in download_queue not cleared on NotFound. PROD.
- test_local_path_error / test_file_status(error part): backslash is VALID on Drive
  (check_encoding only rejects non-utf8). Adapt to non-utf8 input or skip w/ reason.
- test_restore: reads local content directly after wait_for_idle (timing) → wrap wait_until.
- test_move_dropbox_folder: fixture teardown deletes stale local_drive_dir not moved
  m.sync_path → `~/New GoogleDrive` leaks (FileExists next run). Fix teardown.
- test_symlink_error/_indexing, test_local_permission_error, test_long_path_error,
  test_unix_permissions, test_local_indexing_error: category-D, env/Drive-specific; run3
  shows which my fixes already cleared, rest → skipif/xfail w/ reason.
- test_case_change_local/remote, test_unicode_decomposed, test_out_of_order_indexing,
  test_selective_sync_conflict, test_excluded_folder_cleared_on_deletion: re-check in run3.

## Re-measure with poll fix → /tmp/int_pollfix.txt — 41 failed / 23 pass / 2 skip
Net count unchanged BUT the failure SET shifted (4 now pass: test_remote_file_replaced_by_folder,
test_unix_permissions, test_local_file_folder_deleted_event, test_local_folder_replaced_by_file;
4 new fail: test_rapid_remote_changes, test_unknown_path_encoding,
test_local_and_remote_creation_with_equal_content, test_local_deletion_during_upload).
`test_rapid_remote_changes` PASSED in isolation but FAILED in the suite → the shifting is
pollution + a deterministic encoding bug, not 41 independent test bugs.

## CONFIRMED PRODUCTION BUG — path-normalization inconsistency (Stage 1 / core)
Two disagreeing "lowercase" normalizations for percent-encoded (non-ASCII/special) names:
- `utils/path.py:normalize()` = `string.lower()` → lowercases hex: `%C3%B3` → `%c3%b3`.
  Used by `to_remote_path_lower` (sync.py:1382) and thus `get_index_entry_for_local_path`
  (sync.py:915-916) and local-origin index keys / SyncEvent.remote_path_lower.
- `path_codec.py:fold_path/fold_key` = `quote(casefold(...))` → UPPERCASE hex `%C3%B3`.
  Used by the path cache and `md.path_lower`; remote-origin index entries keyed by
  `md.path_lower` (sync.py:1046).
=> For any encoded-name file, a LOCAL change looks up the index via lowercase-hex key and
   misses the remote-origin entry (uppercase-hex) → treated as new → re-upload/conflict/churn.
   Breaks bidirectional sync for all non-ASCII/special filenames. The integration suite
   correctly caught it; the 470 offline tests missed it (fake-Drive produces path_lower
   self-consistently, hiding the real-Drive divergence — a fake-Drive fidelity gap).
   This is core Stage 1 work, NOT Stage 7a test infra. Recommend: file as Stage 1 follow-up,
   xfail the encoding/normalization-dependent tests here with a tracking ref.

## Remaining test-HARNESS issues (Stage 7a-ownable)
- Timing: poll-interval fix (kept) is necessary; insufficient alone.
- `wait_for_idle` + assert-once is flaky vs Drive lag → move assertions to condition-based
  `wait_until(synced)` (conftest already has wait_until for this reason).
- Cross-test isolation: leftover `f%C3%B3lder` bleeds into unrelated tests (teardown of the
  encoded-name tests doesn't fully purge local+remote, partly BECAUSE of the normalization
  bug above — clean/remove can't match the mis-normalized path).

## Buckets that ARE plain test/per-test fixes
get_file_diff (prod, Task 4 — fix), get_metadata raises-vs-None (test contract),
error-injection category D (triage fix/skipif), test_file_status / test_restore /
test_excluded_folder_cleared_on_deletion / test_invalid_pending_download (per-test).
