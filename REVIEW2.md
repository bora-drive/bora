# PLAN.md & TODO.md — Second-Pass Review

The original `REVIEW.md` enumerated 15 issues (all marked resolved). This
report lists **additional** problems that the plan and TODO still do not
address, found by re-reading both documents end-to-end against the Google
Drive v3 API and the existing Maestral architecture.

---

## A. API-semantics gaps

### A1. Modification-time semantics aren't mapped
Maestral relies on Dropbox's `client_modified` and `server_modified` for
conflict detection and for setting local `mtime`. PLAN §3 maps `rev`,
`content_hash`, `cursors`, but never says how the equivalents
(`modifiedTime`, `viewedByMeTime`, `createdTime`) feed `FileMetadata`. In
particular:

- Google Drive lets clients **set** `modifiedTime` on upload/update; without
  doing so, every download→upload round-trip would clobber the timestamp
  and feed back as a remote change.
- `client_modified` vs `server_modified` distinction must be collapsed to a
  single `modifiedTime`, which has consequences for the conflict heuristic
  in `sync.py` (`_check_download_conflict` compares both).

Not mentioned anywhere in PLAN/TODO.

### A2. Folders have no `md5Checksum` and no `size` — but `FileMetadata` does
`core.FileMetadata` requires `content_hash`, `size`, and `rev`. For Drive
folders these fields don't exist. `FolderMetadata` already handles that,
but native-doc filtering (Issue #5 resolved) only removes Docs/Sheets/
Slides — there are other Drive item types with no checksum (shortcuts,
third-party shortcuts, `application/vnd.google-apps.form`, `.map`,
`.site`, `.fusiontable`, `.script`). The TODO for `native_docs.py` should
be explicitly extended to cover **all** `application/vnd.google-apps.*`
MIME types, not only the three commonly named ones, and `Shortcut`
resolution (`shortcutDetails.targetId`) needs a decision (follow vs skip).

### A3. `changes.list` returns a flat global feed, not folder-scoped
PLAN §3.2 maps `changes.list` ↔ Dropbox cursors as if equivalent. They
aren't:

- Dropbox cursors are bound to the listed path. Maestral can have a
  cursor for `/Apps/Foo` and only see changes inside it.
- Google's `changes.list` returns **every** change the user is allowed to
  see across all of Drive (and optionally Shared Drives). Filtering to
  "items under the chosen sync root" is the caller's responsibility, and
  is non-trivial because change entries don't include the path; you only
  get a `fileId` and a (possibly stale) parent list.

The TODO does have one bullet "Handle changes to files outside sync scope
(filter by root folder)" but doesn't acknowledge that this filter requires
an up-to-date path cache — so a change for a file whose ancestry the cache
doesn't yet know about must trigger a `files.get` to learn parents. That
adds API calls beyond the budget already discussed in resolved Issue #3.

### A4. `changes.list` `removed` semantics are ambiguous
A `change` entry with `removed=True` can mean: trashed, hard-deleted,
un-shared, moved out of a Shared Drive, or simply no-longer-visible. The
TODO collapses these to one bullet ("Handle `removed` flag"). The sync
engine needs to distinguish at least "file is gone, propagate delete"
from "we lost visibility, do nothing" — otherwise a permission change
will cascade into a local delete.

### A5. Move + rename in one operation
Google Drive expresses a move as `files.update` with `addParents` /
`removeParents`, and a rename as `name=`. Both can happen in a single
change entry. TODO 5f mentions `move(from_path, to_path)` but doesn't
cover:

- A single Drive operation that **both** moves and renames; the path
  cache needs to handle the combined case atomically.
- Cross-parent moves where the new parent isn't yet in the cache.
- Drive's now-deprecated multi-parent semantics: an existing file with
  two parents that gets a third added — the chosen "canonical" parent
  (PLAN §8) may itself be `removeParents`'d later.

### A6. No mention of `appProperties` / `properties` for round-trip metadata
If cumulus wants to mark uploaded files (e.g. to record an upload-source
hash, or to persist symlink-target text in a Drive-readable way), Drive's
`appProperties` is the standard place. Worth at least a paragraph in
§3 or §5 — currently silent.

---

## B. Auth & onboarding gaps

### B1. `InstalledAppFlow.run_local_server` won't work in headless contexts
PLAN §3.6 says "Standard OAuth2 on both sides." But `run_local_server`
spawns a browser and opens a localhost socket — it will fail on:

- Headless Linux (servers, SSH sessions, CI)
- Sandboxed environments where `webbrowser.open` is no-op
- The Maestral CLI dialog flow, which today copies a code from a browser

Dropbox's `DropboxOAuth2FlowNoRedirect` is paste-the-code, which works
everywhere. The Google equivalent is `run_console()` (deprecated by
Google in 2022) or implementing the OAuth-out-of-band flow manually with
a PKCE redirect URI. Neither is called out. TODO Phase 3 says
"Implement OAuth2 flow … using `google-auth-oauthlib`" without picking a
UX. **This is a real blocker for parity with Maestral's existing CLI
`maestral auth link` UX.**

### B2. OAuth verification gating
PLAN §8 lists OAuth verification as Medium risk. Not mentioned: until
the app passes verification, any user outside the project's test-user
list sees the "unverified app" screen and cannot grant the
`auth/drive` scope at all if Google has marked it sensitive (it has).
For a forked open-source project this means **every contributor and
early user must be added as a test user** until verification, or each
user must use their own OAuth client. This is closer to High severity
for an OSS distribution and warrants a pre-release task in Phase 14.

### B3. Token revocation handling
TODO Phase 3 has `unlink()` revoke and clear, but doesn't cover the
more-common case where the user revokes the token via Google's account
settings. The next API call returns 401 / RefreshError; the existing
Dropbox path triggers `AuthError` and a re-link prompt. The Google flow
has the same path but token refresh failures from the background thread
need explicit propagation — currently no item.

---

## C. Sync-engine subtleties not on the list

### C1. Drive's `name` is not a path component in the strict sense
Drive allows `/` in `name`. Maestral's sync engine uses `/` as the path
separator. A file named `2024/05 report.pdf` would round-trip into a
bogus subdirectory in the local FS or vice versa. The path cache must
either escape `/` in names or refuse to sync such files. Not in PLAN or
TODO.

### C2. Case-insensitive collation locale
Issue #4 (resolved) used `name.lower()` for the case-conflict key.
Python's `str.lower()` is locale-naive but does Unicode casefolding only
imperfectly (`'İ'.lower() == 'i̇'` two codepoints). Drive's
case-insensitive search uses Google's collation, which is closer to
`str.casefold()` plus NFC. The path cache should use `casefold()` and
NFC-normalize names before comparison; otherwise some names that Drive
treats as duplicates won't be detected, and vice versa.

### C3. Filesystem reserved characters
Dropbox normalizes (or refuses) `:*?"<>|` for cross-platform safety;
Maestral inherits that. Google Drive accepts essentially any Unicode in
names including those characters and embedded NULs. There is no
bullet covering local-name sanitization on download or remote-name
rejection on upload. This is a real footgun on Windows (in scope per
TODO Phase 14: "Test on … Linux, macOS" — Windows isn't even tested).

### C4. Trash-vs-delete policy
Drive deletions go to Trash by default and are recoverable for 30 days.
`files.delete` skips Trash and is irreversible. TODO 5f says "call
`files.delete` or `files.update(trashed=True)`" without picking. This
must be a settled policy before sync ships — the wrong default will
cause angry users either way ("my files are gone forever" vs "I deleted
10 GB and my quota didn't drop").

### C5. Quota / storage exhaustion mid-sync
`storageQuotaExceeded` is mapped to `InsufficientSpaceError` (TODO
Phase 6), but the resumable-upload protocol returns this mid-stream; the
streaming upload task needs to catch and translate per-chunk. Currently
the TODO treats this only as an error-mapping bullet, not a sync-engine
behavior bullet (pause? notify? retry on next cycle?).

### C6. "Computers" / Backup folders
If the linked account uses Google's Backup-and-Sync "Computers" feature,
the user sees folders like `My Laptop` at the top level of Drive that
are actually `parents:[someComputerRootId]`. Listing children of
"My Drive" won't include them. Decision needed: ignore, error, or
support. Not in PLAN.

### C7. Initial-sync atomicity
Resolved Issue #2 added a path cache, but neither doc says what happens
if the initial recursive listing is interrupted halfway. The cache will
be partially populated; resuming requires a way to know which subtrees
are complete. Either the cache schema needs a `listed_at` per folder,
or initial sync must be all-or-nothing with a staging table.

---

## D. Architectural & operational gaps

### D1. PLAN §4.4 says `daemon.py` needs no changes — likely wrong
`daemon.py` exposes Pyro5 RPC methods that the CLI calls. Many of those
method names and signatures are derived from sync state — e.g. `status`,
`get_account_info`, `get_state`, `link`, `get_auth_url`. If field names
change (e.g. `dbx_path` → `remote_path` in models that cross the RPC
boundary), every consumer (CLI, GUI) breaks. Phase 9 covers CLI, but
the RPC-layer compatibility is silent. There is no Phase entry for
"audit daemon-exposed surface area for renamed types/fields."

### D2. Database schema changes have no migration code path
Phase 11 says "Fresh database on first run." Fine for users without
state. But the existing repo *does* ship `database/` migrations. Adding
the path-cache table to an existing fresh-install schema requires either
a new migration (preferred — keeps the migration framework intact) or
reset-on-version-bump logic. The TODO bullet "Add path cache table to
SQLite schema" doesn't say which.

### D3. Branding rename is incomplete: `cumulus` vs `maestral`
TODO says the package was renamed to `cumulus`, but PLAN.md still says
"keeping ‘maestral’" in §1 and `# Plan: Maestral Fork`. PLAN/TODO are
inconsistent on the project name. README, CHANGELOG, Sphinx config
should all be checked. Phase 13 lists docs but not CHANGELOG.

### D4. GUI packages explicitly out of scope, but they import `core`
`maestral-qt` and `maestral-cocoa` import `core.FileMetadata` and
friends. Renaming `dbx_path` to `remote_path` in `models.py`/`core.py`
breaks them silently. "Out of scope for MVP" doesn't mean "won't
break" — without a compatibility shim, the GUI packages will fail at
import time. Worth at least a deprecation alias, or a note that GUIs
are dead until ported.

### D5. `client_secret.json` in resources — license / distribution risk
Google's TOS for Desktop OAuth credentials says they're public, but
committing them in a public Git repo also makes them visible in forks
and mirrors. If the project ever rotates the credential, every prior
release still ships the old one and contributes to abuse-quota issues
attributed to the project. Worth a brief note on rotation strategy and
whether to bake the credential into the wheel only (not the source
repo). Currently TODO Phase 2 just says "commit it."

### D6. Polling cost ≠ change-detection latency
PLAN §3.2 picked 30s polling; resolved Issue #3 raised it to 180s for
quota. Neither doc states the user-visible consequence: "changes made
on drive.google.com appear locally up to 3 minutes later." That's a
user-experience regression vs Dropbox (long-poll = a few seconds). It
should be in user-facing docs (Phase 13) and the README's "differences
from Maestral upstream."

### D7. Two-way conflict naming
Maestral creates `(conflicting copy …)` files. Drive's own conflict
policy is to allow duplicate names silently. The plan does not specify
how cumulus's conflict copies will be **named on Drive** — Drive will
not complain about same-name siblings, but the local FS will. The
upload code must therefore enforce uniqueness *before* upload (which
contradicts Drive's permissiveness) or accept that conflict copies
will appear identical on drive.google.com. Not addressed.

---

## E. Testing gaps beyond Issue #14

### E1. `HttpMock` is request-replay, not behavior-modeling
Resolved Issue #14 picked `googleapiclient.http.HttpMock` for unit
tests. That class plays back canned responses; it cannot model state
("after upload, list shows new file"). For path-cache tests this means
every test author hand-writes JSON. A small in-memory fake-Drive (a few
hundred lines) would pay for itself across dozens of tests. Not
suggested anywhere.

### E2. No fault-injection tests for path cache
The path cache is the riskiest new component (PLAN agrees). There are
no proposed tests for: cache vs Drive divergence, partial population
recovery, concurrent mutation. TODO Phase 4 has "unit tests for path
cache" as a single bullet.

### E3. No load / stress test
The rate-limit redesign (Issue #3) hasn't been validated. A
10k-file initial-sync test against a real test account (or fake-Drive)
should be a Phase 12 integration item.

---

## F. Minor / housekeeping

- `tests/README.md` is referenced in TODO Phase 12 but doesn't exist
  yet — needs to be created or the bullet reworded.
- `PLAN.md §4.4` lists `utils/path.py` as no-changes but flags one
  `dbx` reference. Either fix it or move it out of the "no changes"
  list.
- `PLAN.md §5.3` shows `ContentHasher` with no block size — fine for
  MD5, but the existing `StreamHasher` calls `update(chunk)` and
  expects a 4MB internal block. Confirm streaming path doesn't
  regress on large files.
- `TODO.md` lists `cli/cli_main.py`, `cli_core.py`, `cli_info.py`,
  `cli_maintenance.py`, `cli_settings.py`, `dialogs.py`, `common.py`,
  `core.py` — verify the actual filenames in `src/cumulus/cli/`
  match (the package may have been renamed without TODO updates).
- The 30-second polling cited in PLAN §3.2 contradicts the 180-second
  decision in resolved Issue #3. PLAN should be updated to match.
- `_check_and_update_path_root` is mentioned but the rename target
  function name (e.g. `_check_and_update_drive`) isn't decided.

---

## Priorities (suggested)

| # | Issue | Severity |
|---|---|---|
| B1 | Headless OAuth flow (no `run_local_server`) | **High** — blocks CLI parity |
| C4 | Trash vs delete policy | **High** — data-loss risk |
| A3/A4 | Global change feed filtering & `removed` semantics | **High** — correctness |
| D1 | Daemon RPC field-rename audit | **High** — silent breakage |
| C1 | `/` in Drive names | **High** — corruption risk |
| A1 | `modifiedTime` semantics | **Medium** |
| C2 | Casefold + NFC for case-insensitivity | **Medium** |
| C3 | Reserved-character sanitization (Windows) | **Medium** |
| C7 | Initial-sync atomicity / resume | **Medium** |
| A5 | Combined move+rename in one change | **Medium** |
| A6 | `appProperties` for cumulus-side metadata | **Low** |
| D2 | Schema migration vs fresh-install policy | **Medium** |
| D3 | maestral/cumulus naming inconsistency | **Low** |
| D4 | GUI breakage from field renames | **Low** (out of scope, but document) |
| D5 | Credential rotation strategy | **Low** |
| D6 | Document polling-latency UX impact | **Low** |
| D7 | Conflict-copy naming on Drive side | **Low** |
| C5 | Quota-exhaustion mid-resumable-upload | **Medium** |
| C6 | "Computers" backup-folder handling | **Low** |
| E1/E2/E3 | Test scope: fake-Drive, fault injection, load | **Medium** |
| Misc F | Housekeeping | **Low** |

None of these are blocked by, or contradict, the 15 resolutions in
`REVIEW.md`; they are additional surface that the plan does not yet
cover.
