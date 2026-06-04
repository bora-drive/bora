# Stage 7a — Test infrastructure: fake-Drive + suite rewrite (design)

**Status:** Design (awaiting review)
**Date:** 2026-06-04
**Branch:** `google-drive-port`
**Master design:** `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` §5.7 (Stage 7)
**Prior stage:** Stage 6 (Manager + CLI + DB) — complete, full-daemon smoke 4/4
(`docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db-deviations.md`)

---

## 1. Purpose & decomposition

Master-design Stage 7 ("Test infrastructure & coverage") bundles four pieces:
Phase 12 (port/rewrite all tests), **E1** (in-memory fake-Drive), **E2**
(sync-engine fault-injection), **E3** (10k-file load test). These have a clear
dependency — E1 is the foundation the rewritten tests, E2, and E3 all build on —
and the bundle is large. Following the same staging discipline that drove the
whole port, Stage 7 is split:

- **Stage 7a (this spec): "make the suite green and trustworthy."** E1 fake-Drive,
  rewrite the offline suite onto it, rebuild the real-account linked suite,
  un-ignore/rewrite `test_manager.py`, fix `test_event_handler.py`, write
  `tests/README.md`.
- **Stage 7b (later, own spec): "stress & resilience."** E2 sync-engine
  fault-injection and E3 10k-file load test. 7a builds the *seam* for E2 (a
  per-method error hook on the fake) but writes no fault tests.

## 2. Why both a fake-Drive **and** a real-account suite

The fake-Drive (offline) gives fast, deterministic coverage of the
translation layer — the code that turns Drive REST responses into
`FileMetadata`, classifies change-feed entries, escapes `/`-in-name, handles
combined move+rename. That is exactly where the Stage 0 spike and later smoke
tests found real bugs, and where regressions will hide.

But a fake can lie: it only proves the code is internally consistent with our
*model* of Drive. Real-account tests are the ground truth that the model is
correct. Both layers are kept. The fake is the routine CI workhorse; the
real-account suite is the periodic truth check (and the stage's completion
gate). This decision is deliberate: "if we aren't verifying against a real
account we have no idea if any of this works."

## 3. Current state (2026-06-04)

- **Offline suite: 418 passing** with two loose ends:
  `test_manager.py` is fully `--ignore`d (Dropbox-era `TeamRootInfo`/
  `UserRootInfo` + `test_migrate_path_root_*` against methods Stage 6 deleted in
  `d75de2b`); `test_event_handler.py` has 5 `FileExists` fixture errors.
- **`tests/linked/` is entirely Dropbox-shaped:** `conftest.py` links via
  `DROPBOX_ACCESS_TOKEN`/`DROPBOX_REFRESH_TOKEN`; `lock.py`/`test_main.py`/
  `test_sync.py` do `from dropbox import files`; `test_client.py` tests
  `upload_session`/`share_dir`/`sharedlink` (Dropbox concepts; sharing is an MVP
  stub on our side).
- The `DropboxClient = GoogleDriveClient` alias (`client.py:1210`) is still used
  by `tests/offline/conftest.py` and `test_clean_local_events.py`.
- No `tests/README.md`; no in-memory fake-Drive.
- The service-build choke point is `client._get_service()` (`client.py:373`);
  nearly every read path funnels through it, and `writes.py` calls
  `client._get_service()` for batches (`writes.py:529`, `:674`).

## 4. Scope

**In scope (7a):**

1. **E1 — in-memory fake-Drive** (`tests/fake_drive.py`): stateful
   service-boundary fake injected at `client._get_service()`.
2. **Offline suite rewired onto the fake** where sequenced state matters;
   `DropboxClient` retired from `tests/`; `test_manager.py` un-ignored &
   rewritten; `test_event_handler.py` fixture errors fixed.
3. **Real-account linked suite rebuilt** against a dedicated Google test
   account: Drive-native session lock, `CUMULUS_TEST_REFRESH_TOKEN` env
   convention, whole suite `skipif`-gated when creds absent; Dropbox-only tests
   dropped.
4. **`tests/README.md`** documenting offline vs. linked runs, env convention,
   test-account/lock model, fake-Drive fidelity contract.

**Out of scope (deferred):** E2 fault-injection tests, E3 load test (→ 7b);
removal of the production `DropboxClient` alias (→ Stage 8 naming sweep); OAuth
verification submission (→ Stage 8). 7a does **not** chase a coverage-percentage
target — it rewires existing tests onto the fake and closes the named gaps.

## 5. E1 — the in-memory fake-Drive (`tests/fake_drive.py`)

### 5.1 State model

`FakeDrive` holds a dict of file records keyed by `fileId`. Each record carries
the fields our code reads: `id`, `name`, `parents` (list), `mimeType`,
`md5Checksum`, `headRevisionId`, `size`, `trashed`, `modifiedTime`,
`createdTime`, a per-file `revisions` list, and a content blob. A monotonic
`change_log` and a `start_page_token` counter back the change feed. Declarative
seed helpers let tests build trees: `fake.add_folder("/a/b")`,
`fake.add_file("/a/b/x.txt", content=b"…")`, and a seeded root folder.

### 5.2 Surface (only what our code calls)

A fake `Resource` tree returning request objects with `.execute()`:

- `files().list / get / create / update / delete / get_media / generateIds`
- `changes().list / getStartPageToken`
- `about().get`
- `new_batch_http_request()` → batch object supporting
  `.add(request, request_id, callback)` and `.execute()` that invokes each
  callback with `(request_id, response, exception)`.

### 5.3 Injection seam

Tests set `client._service = fake` (or monkeypatch `_get_service` to return it).
No production-code change required — `_get_service` is the single choke point.

### 5.4 Fidelity contract (acceptance criteria, from the Stage 0 spike)

1. `md5Checksum` = real `hashlib.md5` of stored content; `headRevisionId` bumps
   on every content update and is the canonical `rev`.
2. `changes().list` removed-reason semantics modeled exactly: trash →
   `removed=false` + `file.trashed=true` + full body; hard-delete → `removed=true`
   + `fileId` only; move-out → `removed=false` + full body with updated `parents`.
3. `/` accepted **literally** in `name`; `list`/`get` round-trip it unchanged.
4. Combined move+rename = **one** `files.update` producing **one** change entry
   with both new name and new parents.
5. `files.list(q=…)` supports the query fragments our code emits
   (`'<parent>' in parents`, `name = '…'`, `trashed = false`, `mimeType …`) and
   `pageToken` pagination with a test-settable page size.
6. `files.update` honors a rev/precondition check so the 412 `conditionNotMet`
   path is reachable.

### 5.5 The two fidelity-sensitive spots (hardest parts of the fake)

- **Resumable upload:** `create/update(media_body=resumable)` returns a request
  whose `.next_chunk()` drives a fake resumable session to completion, reporting
  `MediaUploadProgress` so `writes.py`'s progress/shutdown loop
  (`writes.py:_upload_resumable`, `:371`) runs for real.
- **Download:** `client.py` wraps the fake's `get_media()` request in the *real*
  `MediaIoBaseDownload` (`client.py:712`). The fake's get-media request therefore
  exposes the minimal `httplib2`-shaped `.http.request(uri, headers)` that honors
  a `Range` header and returns `(resp, chunk)` — so the real downloader's chunk
  loop, md5 check, and sidecar logic exercise unchanged.

### 5.6 Fault-injection seam (built in 7a, used in 7b)

An optional per-method hook table — `fake.fail_next("files.list", HttpError(...))`
— so 7b can inject 429/412/500/partial-page without touching the fake's core.

## 6. Rewiring the offline suite onto the fake

**`conftest.py`:** add a `fake_drive` fixture (fresh `FakeDrive`) and a `client`
variant wired to it (`client._service = fake_drive`, root id seeded). Replace
`from cumulus.client import DropboxClient` with `GoogleDriveClient` throughout
`tests/` (the production alias stays — its removal is Stage 8).

**Tests that move onto the fake (sequenced state is the win):**

- `test_changes.py` — change-feed classification against real `changes().list`
  payloads from the fake (removed-reasons, move+rename, pagination).
- `test_client.py` / `test_writes.py` — round-trip state: upload→`get_metadata`
  shows the file with correct md5/rev; move→list reflects new path; trash→change
  feed reports it; resumable upload drives `next_chunk`; download via real
  `MediaIoBaseDownload`.
- `test_clean_local_events.py` — drop the `DropboxClient` import; otherwise
  unchanged.

**Tests that stay as pure unit tests (no fake):** `test_path_codec.py`,
`test_hashing.py`, `test_path_cache.py`, `test_oauth.py`,
`test_errorhandling*.py`, `test_native_docs.py`, `test_core_models.py`,
config/utils tests. Rewriting them would be churn for no coverage gain.

**Regression coverage (must be explicit offline tests):** the two spike/smoke
bugs — parent-before-child change ordering (Stage 3, `05d58ed`) and
new-file-upload `NotFoundError`-from-`get_metadata` (Stage 5, `2ed6e33`).

## 7. The real-account linked suite

### 7.1 Account & creds

A dedicated Google test account (provisioned out of band by the user), linked
under a fixed test config name **distinct from the `smoke-test` config** — so a
linked run can never clobber the `cumulus@arko.net` smoke link (the documented
keyring-collision footgun). Creds via env: `CUMULUS_TEST_REFRESH_TOKEN` (and
optional `CUMULUS_TEST_ACCESS_TOKEN`). The session fixture links from the
refresh token; if absent, the **entire `tests/linked/` tree is `skipif`-skipped**
(green-by-skip for contributors and credential-less local runs).

### 7.2 Drive-native session lock (replaces `DropboxTestLock`)

`tests/linked/lock.py` rewritten. Drive has no atomic create-exclusive, so:
acquire by listing for a sentinel lock file (`.cumulus-test.lock`) in the test
account's Drive root → if present and not stale, wait/retry with backoff → else
`files.create` it, re-list, and verify we own the earliest-created instance.
Release = `files.delete`. A staleness timeout lets a crashed run self-heal. This
serializes concurrent test sessions against the single shared account.

### 7.3 Which linked tests survive the Drive port

- **Keep & port to Drive shapes:** `test_sync.py` (end-to-end up/down sync,
  conflict resolution, move+rename), `test_main.py` (link/unlink, account info,
  sync-dir lifecycle), the Drive parts of `test_client.py` (upload simple +
  resumable, download + md5, get_metadata, list_folder, change feed, move,
  trash/restore, make_dir + batch).
- **Drop (Dropbox-only / MVP stubs):** `share_dir`, `create_shared_link`,
  `sharedlink` lifecycle/errors, Dropbox-specific `upload_session` assertions.
  A single test asserting the sharing stub raises is enough.
- Replace `from dropbox import files` with Drive metadata shapes.

### 7.4 Change-feed latency

Drive's `changes.list` lags a few seconds behind a mutation (observed in the
Stage 5/6 smokes). Linked tests use **bounded-retry polling** via a
`wait_until(predicate, timeout)` helper in the linked conftest, not single-shot
asserts.

### 7.5 Token caveat (documented)

The Testing-mode OAuth client expires refresh tokens after 7 days until Stage 8
verification. `tests/README.md` documents that `CUMULUS_TEST_REFRESH_TOKEN` must
be re-minted weekly until then.

## 8. Loose ends

**`test_manager.py` (un-ignore + rewrite):**
- **Delete** the path-root/team-namespace tests (`test_migrate_path_root_*`) —
  they test code Stage 6 removed.
- **Keep & fix** the valid manager tests: `ensure_drive_folder_present` (renamed
  from `ensure_dropbox_folder_present`), sync-dir creation / `NoSyncDirError`,
  folder-structure helpers, `fake_linked` account wiring; trim the `FullAccount`
  fixture to the Drive shape (drop team fields).
- Remove from the `--ignore` set in CI and in the Stage 6 deviations' command.

**`test_event_handler.py` (fix 5 `FileExists` errors):** fixtures call
`new_dir.mkdir()` on an already-existing path (stale/shared state). Fix to a
clean per-test `tmp_path` (or `mkdir(exist_ok=True)` + teardown). Test-hygiene
only; no production change.

**`tests/README.md` (new):** running offline (no account); running linked (env
vars, dedicated account, 7-day token caveat, lock model); fake-Drive purpose +
fidelity contract; offline-vs-linked split rationale.

## 9. Definition of done

1. `pytest tests/offline` — **fully green, no `--ignore`, zero `dropbox`
   imports** in `tests/`.
2. `tests/linked/` — green against the dedicated account when
   `CUMULUS_TEST_REFRESH_TOKEN` is set; cleanly **skipped** (not errored) when
   absent.
3. The fake-Drive honors all six §5.4 fidelity-contract points; the two
   §6 regression bugs are covered as explicit offline tests.
4. `tests/README.md` exists and is accurate.
5. **Manual gate:** one real linked-suite run against the provisioned test
   account passes end-to-end — the proof the fake isn't lying.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Fake drifts from real Drive behavior → "green-against-fake" is false confidence | §5.4 fidelity contract drawn from spike payloads; §9.5 real-account gate validates the fake each stage; fake's surface is restricted to methods our code actually calls |
| Resumable-upload / `MediaIoBaseDownload` integration with the fake is fiddly | §5.5 calls these out as the hardest parts; build them first and test against the real linked suite to confirm equivalence |
| Real-account suite flakiness from change-feed latency | §7.4 bounded-retry `wait_until`; lock serializes sessions |
| Test refresh token expires weekly (Testing-mode OAuth) | §7.5 documented; `skipif`-gating means expiry degrades to skip, not failure |
| Shared test account corrupted by concurrent runs | §7.2 Drive-native lock with staleness self-heal |

## 11. Next step

After this spec is reviewed and approved, invoke `superpowers:writing-plans` to
produce the Stage 7a implementation plan (`- [ ]` task checklist) under
`docs/superpowers/plans/`.
