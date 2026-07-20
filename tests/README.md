# Test suite

The test suite has two layers: a fast, offline, fake-Drive-backed suite under
`tests/offline/`, and a real-account integration suite under `tests/linked/`.
Both layers exist for different reasons and are not substitutes for each other.

The **offline suite** gives fast, deterministic coverage of the translation
layer — the code that turns Drive REST responses into `FileMetadata`, classifies
change-feed entries, handles `/` in file names, and handles combined
move+rename. This is exactly where regressions hide and where most bugs have
historically been found. No credentials, no network, no secrets required.

The **linked suite** is the ground-truth check. A fake can only prove the code
is internally consistent with our *model* of Drive. Real-account tests prove the
model itself is correct. Both layers are kept deliberately: "if we aren't
verifying against a real account we have no idea if any of this works." The
offline suite is the routine CI workhorse; the linked suite is the periodic
truth check that keeps the fake honest.

---

## Running the offline suite

No account, no secrets, no network needed:

```bash
python -m pytest tests/offline
```

The suite currently collects 470 tests and is fully green.

### Writing new offline tests

Two fixtures are available from `tests/offline/conftest.py`:

- **`fake_drive`** — a fresh `FakeDrive` instance for each test.
- **`client_on_fake`** — a `ClientOnFake(client, fake)` dataclass: a real
  `GoogleDriveClient` wired to a `FakeDrive` via
  `client._get_service = lambda: fake.service()`. The client is fully
  functional; no Google API calls are made.

Use `fake.add_folder("/a/b")` and `fake.add_file("/a/b/x.txt", content=b"…")`
to build Drive trees declaratively before exercising client/sync logic.

---

## Running the linked suite

The entire `tests/linked/` tree is skipped cleanly when credentials are absent,
so contributor and credential-less local runs stay green-by-skip. To run against
a real account, set:

```bash
export BORA_TEST_REFRESH_TOKEN=<refresh-token>
export BORA_TEST_ACCESS_TOKEN=<access-token>   # optional; speeds up first call
python -m pytest tests/linked
```

`BORA_TEST_ACCESS_TOKEN` is optional. `BORA_TEST_REFRESH_TOKEN` is
required; its absence causes the entire `tests/linked/` tree to skip.

Tests marked `slow` (currently the rate-limit write-burst test in
`tests/linked/unit/test_resilience.py`) are deselected by default via the
project-wide `-m "not slow"` addopts; include them with
`python -m pytest tests/linked -m slow`.

### Resilience tests (`tests/linked/unit/test_resilience.py`)

Real-account counterparts to the offline fault-injection suites: rate-limit
retry/backoff (real-shaped 429/403 injected at the `_retry_transient` seam,
with the retried request hitting the live API, plus a `slow`-marked real
write burst), token refresh (access token force-expired, next call must
auto-refresh via `PersistingCredentials` and persist), and network
disconnect/reconnect (one-shot and full-outage `ConnectionError` injection
with real recovery). No test deliberately exhausts real Drive quota.

### Dedicated test account

The linked suite must use a **dedicated Google test account**
(`bora-test@arko.net`), linked under the config name **`bora-test-config`**.
This is distinct from the `smoke-test` config used for manual end-to-end
smoke testing.

**Do not run `pytest tests/linked` while a smoke-test account is linked.** The
keyring stores credentials by config name; using the same config for both a
linked test run and the smoke account will overwrite the smoke account's
credentials (the keyring-collision footgun).

### Drive-native session lock

The linked suite serializes concurrent test sessions using `DriveTestLock`
(`tests/linked/lock.py`). Drive has no atomic create-exclusive primitive, so
the lock protocol is:

1. List for a sentinel file named `.bora-test.lock` in the test account's
   Drive root.
2. If a non-stale lock exists (age < 10 minutes), wait and retry with backoff
   for up to 5 minutes.
3. Otherwise, create the sentinel file, re-list, and confirm ownership by
   checking that the created file has the earliest `createdTime` among
   contenders.
4. Release by deleting the sentinel file. A crashed run self-heals after the
   10-minute staleness timeout.

This is managed automatically by the `test_lock` session fixture in
`tests/linked/conftest.py`. Tests declare a dependency on it via the
`@requires_account` marker, which also gates on `BORA_TEST_REFRESH_TOKEN`
being set.

### Obtaining and refreshing the token

Provision a dedicated Google account, run through the OAuth flow once (e.g.
using `bora link bora-test-config`), then extract the stored refresh
token from the keyring. Record it as `BORA_TEST_REFRESH_TOKEN` in whatever
CI secret store is in use.

### 7-day token-expiry caveat

The OAuth client is currently in **Testing mode**. Google expires refresh tokens
for Testing-mode clients after 7 days. Until Stage 8 verification is complete and
the app is promoted to Production mode, `BORA_TEST_REFRESH_TOKEN` must be
re-minted weekly. Token expiry degrades gracefully: an expired token causes the
`test_lock` session fixture to raise `AuthError`, which skips the session rather
than producing spurious failures.

---

## The fake-Drive (`tests/fake_drive.py`)

`FakeDrive` is an in-memory stateful fake of the Google Drive API service. It is
a service-boundary fake: tests inject it at `client._get_service()`, so the real
`client.py`, `writes.py`, and `changes.py` run unchanged on top of it.

It replaces the per-test `HttpMock` pattern (which replays static JSON fixtures)
with a stateful backing store that responds correctly to sequences of mutations:
upload → get_metadata shows the correct md5/rev; move → list reflects the new
path; trash → changes feed reports it with the right removed-reason semantics.
This exercises the real translation code with realistic, stateful Drive responses.

The contract test suite is `tests/offline/test_fake_drive.py` (22 tests). If a
behaviour covered by that suite breaks in the fake, the offline tests will catch
it. The linked suite is the periodic ground-truth check that confirms the fake's
behaviour matches the real API.

### Fidelity contract

The fake honours six fidelity points so "green against the fake" is trustworthy:

1. **`md5Checksum`** is the real `hashlib.md5` digest of the stored content.
   `headRevisionId` is bumped on every content update and is the canonical rev.

2. **`changes().list` removed-reason semantics** are modelled exactly: trash →
   `removed=false` + `file.trashed=true` + full file body; hard-delete →
   `removed=true` + `fileId` only; move-out-of-root → `removed=false` + full
   body with updated `parents`.

3. **`/` is accepted literally in `name`**; `list`/`get` round-trip it
   unchanged.

4. **Combined move+rename = one change entry.** A single `files.update` that
   changes both `name` and `parents` produces one change entry carrying both the
   new name and the new parents.

5. **`files.list(q=…)` supports the query fragments our code emits** —
   `'<parent>' in parents`, `name = '…'`, `trashed = false`, `mimeType …` — and
   `pageToken` pagination with a test-settable page size.

6. **`files.update` honours a rev/precondition check** so the 412
   `conditionNotMet` path is reachable in tests.

### Fault-injection hook

`FakeDrive.fail_next(method, exception)` enqueues an exception to be raised on
the next call to the named method (e.g. `"files.list"`). This seam is built in
Stage 7a for use by Stage 7b fault-injection tests; Stage 7a itself does not
write fault tests.

---

## Offline-vs-linked split rationale

Write an **offline test** when:

- The behaviour depends on sequenced state (upload → metadata check, trash →
  change-feed classification, move+rename → single entry).
- You are testing the translation layer: how our code interprets Drive REST
  responses, classifies change types, escapes path components.
- You want the test to run in CI without credentials.

Write a **linked test** when:

- You need ground-truth confirmation that our model of Drive's behaviour is
  correct (API-fidelity checks).
- You are testing a true end-to-end flow: link → sync → remote mutation →
  daemon detects change → local file updated.
- You suspect the fake may not model a specific API behaviour accurately.

When in doubt, start with an offline test using `client_on_fake`, and add a
linked counterpart if the behaviour is subtle enough to warrant real-API
verification.
