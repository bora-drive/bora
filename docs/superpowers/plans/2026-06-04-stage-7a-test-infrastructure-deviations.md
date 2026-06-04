# Stage 7a — Plan deviations, verification, and real-account gate

**Plan:** `docs/superpowers/plans/2026-06-04-stage-7a-test-infrastructure.md`
**Spec:** `docs/superpowers/specs/2026-06-04-stage-7a-test-infrastructure-design.md`
**Executed:** 2026-06-04 via subagent-driven-development (fresh subagent per task, review between).

## Implementation summary

Stage 7a ("make the suite green and trustworthy") is **code-complete**. The
offline suite runs fully green with no `--ignore` and zero `dropbox` references;
the real-account linked suite collects and skips cleanly when credentials are
absent. The **one outstanding item is the manual real-account gate** (see
"Real-account gate" below) — it requires `CUMULUS_TEST_REFRESH_TOKEN`, which was
not available in the execution environment.

Final counts:
- `pytest tests/offline` → **470 passed, 0 failed, 0 errors** (no `--ignore`).
- `pytest tests/linked` (no creds) → **82 skipped, 0 errors** (clean skip).
- `grep -rn "import dropbox|from dropbox|DropboxClient|DropboxContentHasher|DropboxTestLock" tests/` → **none** (source).

## Commit map (on `google-drive-port`, `dedcb25..HEAD`)

Phase A — fake-Drive (`tests/fake_drive.py`) + contract suite
(`tests/offline/test_fake_drive.py`):
- `ffe2790` A1 state model + seeding
- `4667de0` A2 files.get/list + projection + query parser; `5f9d0a8` robustness fix
- `3058981` A3 real client.get_metadata round-trips on the fake
- `84e75d3` A4 files.create/update (+ move+rename single entry)
- `3946e15` A5 files.delete (hard-delete) + generateIds
- `b2ae831` A6 changes.list (removed-reasons, pagination, move+rename)
- `634698b` A7 about.get + revisions.list
- `73481a9` A8 get_media download via real `MediaIoBaseDownload`
- `85e8225` A9 resumable upload `next_chunk` loop
- `b058227` A10 batch callbacks + fidelity-contract guard

Phase B — rewire offline suite:
- `4a96ff4` B1 conftest `fake_drive`/`client_on_fake` fixtures; `GoogleDriveClient`
- `b69bc12` B2 change-feed tests on the fake + parent-before-child regression
- `89fc97e` B3 client round-trip on the fake + new-file `NotFoundError` regression
- `cc7ade8` B4 writes on the fake (quota/412 paths) + drop `DropboxClient` alias use

Phase C — loose ends:
- `ac898fe` C1 un-ignore `test_manager`; drop path-root tests; Drive `FullAccount`
- `13d5b6d` C1 follow-up: add 4 real Drive manager tests
- `120b563` C2 fix `test_event_handler` `FileExists` fixture errors
- `c16c0ad` C2 follow-up: wire fake into event-handler tests (drop real-credentials path)
- `f908b56` C3 offline green with no `--ignore`; annotate Stage 6 note

Phase D — real-account linked suite:
- `955bd05` D1/D2/D3 Drive-native harness (lock, skipif gate, `wait_until`)
- `f0ecf08` D4 port linked client tests; drop sharing
- `a7ecf62` D5/D6 port linked sync/main integration tests

Phase E — docs:
- `7315f73` E1 `tests/README.md`
- (this file) E2 deviations + verification record

## Plan deviations (discovered during execution)

1. **Injection seam (plan §5.3 / B1).** The plan drafted `client._service =
   fake.service()`. In reality `GoogleDriveClient._get_service()` does **not**
   cache via a `self._service` attribute and is guarded by a `linked`/OAuth
   check; there is no `client._root_id` (the root id lives on the path cache as
   `cache.root_id`). Correct seam (found in A3, applied in B1): monkeypatch
   `client._get_service = lambda: fake.service()` and pre-seed `cache.root_id =
   ROOT_ID`.

2. **`media_body.resumable` is a method, not an attribute (A9).** The plan's
   `getattr(media_body, "resumable", False)` is always truthy (it returns the
   bound method), which would have routed *every* upload through the resumable
   path and broken the simple-upload test. Fixed to `callable(getattr(media_body,
   "resumable", None)) and media_body.resumable()`. Caught by the regression
   suite staying green.

3. **`GoogleDriveClient.__init__` requires `path_cache` (D1).** The plan's linked
   conftest drafted `GoogleDriveClient(TEST_CONFIG, cred_storage)` (2 args); the
   real signature needs a third `path_cache`. The harness now builds an in-memory
   `PathIDCache`.

4. **`test_manager.py` had only dead tests (C1).** The plan assumed surviving
   manager tests (`ensure_drive_folder_present`, sync-dir/`NoSyncDirError`) to
   "keep & fix". In fact the file contained **only** the 4 dead path-root tests
   plus helper functions — so un-ignoring it after deletion left it empty. A C1
   follow-up (`13d5b6d`) added 4 genuinely-useful Drive manager tests
   (pending-flags, `create_sync_directory` happy path, `ensure_sync_folder_present`
   raise/pass on `NoSyncDirError`) so the file earns its place. The follow-up also
   fixed a silently-broken `fake_linked` helper (it saved `"1234"` as creds, which
   broke `client.linked`).

5. **`test_event_handler` had latent failures masked by the fixture errors (C2).**
   Fixing the 5 `FileExistsError` setup errors (root cause: a hardcoded
   `~/dummy_dir` in the conftest `sync` fixture that leaked across crashed runs;
   replaced with `tmp_path_factory`) unmasked 3 `DefaultCredentialsError`
   failures: `SyncEvent.from_file_system_event` accesses `client.account_info`,
   which lazily authenticates via `_fetch_account_info_for_creds` → real
   `build()`. Fixed (`c16c0ad`) by wiring the fake service + a stub
   `_cached_account_info` into the conftest `sync` fixture. No `src/` changes.

6. **B4 kept the existing mock-based write tests and *added* fake round-trips**
   rather than converting in place. Net coverage increased (54→67 in
   `test_writes.py`); the pre-existing request-shape unit tests are harmless and
   the fake now covers the stateful round-trips + error paths. Accepted.

No production (`src/`) code was modified anywhere in Stage 7a — every issue was a
test-harness concern.

## Fidelity contract & regression coverage (spec §9.3)

- The six fidelity points (§5.4) are asserted by the `tests/offline/test_fake_drive.py`
  contract suite (22 tests), including the consolidated `test_fidelity_contract_summary`
  guard and `test_changes_move_rename_single_entry` (point 4).
- The two spike/smoke regressions are explicit offline tests:
  parent-before-child change ordering →
  `test_changes.py::test_iter_remote_changes_new_file_in_new_folder` (`b69bc12`);
  new-file-upload `NotFoundError` →
  `test_client.py::test_get_metadata_unknown_path_raises_notfound` +
  `test_check_requires_upload_for_new_file_returns_true` (`89fc97e`).

## Minor fake-fidelity notes (non-blocking, recorded for 7b)

- `FakeDrive` stores `size` as an `int`; the real API returns it as a string.
  Client code (`to_file_metadata`) does `int(size or 0)`, so this is invisible
  through the translation layer — but a future test comparing a raw record
  `size` to a string would diverge.
- `_project(rec, "*")` returns `{}` (treats `"*"` as a literal key), not the
  full record. No current caller passes a bare `file(...)`-less changes selector,
  so it's unreached.
- `changes.list` accepts but ignores `includeRemoved` (all changes are always
  returned). Production only ever passes `includeRemoved=True`.
- The fake keeps only the latest content blob, so `revisions().get_media` returns
  current content regardless of `revisionId`. Sufficient for the offline
  `restore` round-trip; true historical-revision content is validated by the
  linked suite.

## Real-account gate — OUTSTANDING (spec §9.5)

The stage's final done-criterion is one real linked-suite run against the
dedicated test account, proving the fake is not lying. **This has not been run**
— the execution environment had no `CUMULUS_TEST_REFRESH_TOKEN`. To close it:

1. Provision a dedicated Google test account (distinct from the `cumulus@arko.net`
   smoke account). Mint a refresh token (the OAuth client is in Testing mode →
   7-day expiry; re-mint weekly until Stage 8 verification).
2. `export CUMULUS_TEST_REFRESH_TOKEN=…` (and optionally `CUMULUS_TEST_ACCESS_TOKEN`).
3. `python -m pytest tests/linked -q` and record pass/fail counts here.

When run, scrutinize these tests flagged by the porting subagents as uncertain
against a real account (they collect/skip cleanly but were written without a live
account to validate):

- `unit/test_client.py`: `test_upload_resumable` (the fixture is ~1.8 MB, below
  the 5 MB resumable threshold — needs a ≥5 MB file to actually exercise the
  resumable branch); `test_upload_hash_mismatch` /
  `test_download_hash_mismatch` (monkeypatch seams on `_md5_file` /
  `hashlib.md5(usedforsecurity=False)`); `test_change_feed` (60 s `wait_until`
  budget vs. real change-feed latency); `test_batch_methods` (20-folder cleanup
  reliance on `clean_drive_dir`).
- `integration/test_sync.py` / `test_main.py`: `test_rapid_remote_changes`,
  the two replaced-by-folder conflict tests (engine-dependent cc/autorename
  names), `test_get_file_diff` variants (rev IDs via `list_revisions`),
  `test_sync_event_conversion_performance` (env-specific threshold),
  `test_out_of_order_indexing`.

Until the real-account run passes, Stage 7a is **code-complete but not
stage-complete** per the master design's §7 definition (item 5: smoke test for
user-visible behavior). Stage 7b (E2 fault-injection, E3 load test) builds on
the `fail_next` seam already present in `FakeDrive`.
