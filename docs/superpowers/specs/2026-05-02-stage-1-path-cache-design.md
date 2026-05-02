# Stage 1 — Path cache final form

**Status:** Design (awaiting review)
**Date:** 2026-05-02
**Stage of:** `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` §5.1
**Inputs:** the staged-port plan, `docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md`, the existing `src/cumulus/path_cache.py`, REVIEW2 entries C1, C2, C3, C7, E2.

---

## 1. Purpose

Harden the path cache (`src/cumulus/path_cache.py`) so it can serve as the
sole path↔file-ID mapping for every later stage. Concretely: commit to a
specific encoding policy for Drive `name` values that aren't safe to use as
filesystem path segments, swap `name.lower()` for a Unicode-correct
case-fold key, and make `populate_from_listing` atomic against crashes.

Stage 1 does not touch the Drive client (Stage 3+), the sync engine
(Stage 5), or any UX surface. It produces a small new pure-function module,
modifies one existing module, and adds a test suite for both.

## 2. Scope

### 2.1 In scope

- **C1** — escape `/` (and the escape character itself) when serializing a
  Drive `name` into the virtual path. Decided by spike Q4: Drive accepts
  `/` literally, so escape-not-reject is mandatory.
- **C2** — replace `name.lower()` with Unicode `casefold()` + NFC
  normalization for case-conflict and lookup keys.
- **C3 (cache side)** — the original "reject names with reserved chars"
  bullet is replaced by **encode every character that isn't safe in a
  filesystem path segment using the same codec as C1**. With this change
  the cache has no reserved-char rejection path; reserved chars become
  encoded forms (e.g. `report?.pdf` → `report%3F.pdf`). See §3.1 for the
  full encode set.
- **C7** — initial-population atomicity. The bulk populate is wrapped in a
  single SQLite transaction; on failure the cache is restored to its prior
  state. The `listed_at`-per-folder approach proposed in REVIEW2 is *not*
  adopted; see §6 for rationale.
- **E2** — fault-injection unit tests for the cache module.

### 2.2 Out of scope

- **A6** (`appProperties` schema). Originally listed as conditional in §5.1;
  excluded from Stage 1 entirely. If Stage 3 / 4 surface a concrete need,
  it gets handled in that stage's spec.
- **Reserved Windows basenames** (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`,
  `LPT1`–`LPT9`). These are whole-name reservations, not character-level,
  and don't fit a per-character codec. The cache silently accepts them; if
  a user has such a basename on Drive, the local-mirror write fails with a
  clear OS error in Stage 5. Acceptable degraded path; users fix by
  renaming on Drive.
- **`nextPageToken` checkpoint** for paginated `files.list` interruption.
  Stage 3 concern (the API call), not Stage 1 (the cache).
- **Concurrent-mutation tests** for an external `sync_lock`. Stage 7's
  integration territory; the cache itself assumes serialized access.
- **Cache-vs-Drive divergence tests.** Need the read client to manufacture
  divergent state. Stage 7.
- **10k-file load test.** Stage 7 (E3).
- **Schema migration framework.** Stage 6 (D2). The `path_cache` table has
  never been created in production; Stage 1 ships the new schema as the
  initial schema with no version check.

## 3. Architecture

Two modules, one new and one modified, with a clean dependency direction
(`path_cache` depends on `path_codec`; nothing depends on `path_cache`
that would require importing the codec separately, but Stage 3+ callers
will import the codec directly to compose paths from Drive change events).

```
src/cumulus/
  path_codec.py    NEW — pure functions, no DB, no I/O
  path_cache.py    MODIFIED — uses path_codec, schema column rename
tests/offline/
  test_path_codec.py   NEW
  test_path_cache.py   NEW
```

### 3.1 The codec policy

A single URL-encoding policy applies to every character in a Drive `name`
that isn't safe in a filesystem path segment. The encode set:

| Char(s) | Reason |
|---|---|
| `/` | Path separator collision (spike Q4) |
| `%` | Escape character itself; must always escape for reversibility |
| `*` `?` `"` `<` `>` `|` | Windows-reserved punctuation |
| `\x00` (NUL) | Embedded NUL — invalid in every filesystem |
| `\x01` – `\x1F` | ASCII control characters — invalid on Windows, problematic elsewhere |

Implemented via `urllib.parse.quote(name, safe='')`, which uses the
RFC 3986 unreserved set (alphanumerics + `_.-~`) as its passthrough set.
The encode set above is a strict subset of "everything not unreserved," so
`safe=''` is the right call — no custom char list. Hex output is canonical
uppercase (`%2F`, not `%2f`), which is the Python default.

**Trailing space/period rule.** After per-char encoding, if the encoded
name ends in a literal space (`' '`) or period (`'.'`), replace the
trailing char with its percent-encoded form (`%20` or `%2E`). This handles
Windows' "filenames can't end in space or period" rule using the same
codec.

### 3.2 Why we calculate the fold key client-side

Maestral's existing `path_lower` column held the value Dropbox's API
returned: Dropbox enforces case-insensitive uniqueness server-side and
populates `path_lower` and `path_display` on every metadata response.
Maestral never computed it.

Drive's API returns `name` (raw) and `id` (canonical identity), and that's
it. There's no server-computed fold form. Drive's case-insensitive
uniqueness is loose — direct API calls can create fold-colliding names
within the same parent, which Drive will accept.

Three consumers in the cache rely on an indexed fold key:

1. `path_to_id(p)` — sync engine queries by encoded local path. Local FS
   case (APFS / NTFS) may differ from Drive case. Fold-key index lets the
   query be O(1) instead of full scan.
2. Case-conflict detection on `set` and `move` — `(parent_id, fold)`
   uniqueness check on admission.
3. Subtree range query (`_select_children`) — uses a fold-key range
   bound; works only on a stably-cased column.

So we accept the client-side compute cost (one
`quote(NFC(casefold(NFC(s))))` per name during populate, one per
mutation) as the cumulus-vs-maestral cost-side of the ID-based-API trade.

## 4. The codec module — `src/cumulus/path_codec.py`

Pure functions. No DB, no I/O, no logging side effects.

### 4.1 Public surface

```python
def encode_name(name: str) -> str:
    """Encode a single Drive name component for use as a path segment.

    Applies URL-encoding (uppercase hex) plus the trailing space/period
    rule from §3.1. Idempotent only for already-canonical inputs;
    otherwise re-encoding double-encodes (this is correct: a Drive name
    literally `'%2F'` must round-trip back to `'%2F'`, so it must encode
    to `'%252F'`)."""

def decode_name(encoded: str) -> str:
    """Inverse of encode_name."""

def fold_key(name: str) -> str:
    """Case-fold key for one name component.

    Computes quote(NFC(casefold(NFC(name))), safe=''). The double NFC
    handles cases where casefold introduces decomposition (e.g. Turkish
    'İ' casefolds to 'i' + combining-dot)."""

def compose_path(parent_path: str, raw_name: str) -> str:
    """Build an encoded child path from an encoded parent path and a
    raw Drive name.

        compose_path('/Documents', '2024/05 report.pdf')
        -> '/Documents/2024%2F05 report.pdf'

    parent_path == '/' is the root; result is '/' + encode_name(raw_name)."""

def fold_path(encoded_path: str) -> str:
    """Fold key for a full encoded path.

    Splits on '/', decodes each segment, applies fold_key, rejoins. Used
    by the cache to derive remote_path_fold from remote_path."""
```

### 4.2 What's deliberately not exported

- No `decode_path` symmetric to `compose_path`. Callers walk the path one
  segment at a time; there's no use case for "decode the whole path back
  to raw form" since paths are always operated on in encoded form
  everywhere except the Drive-API boundary, where individual names are
  decoded with `decode_name` per segment.
- No char-set constants. The set lives only in docstrings (and tests).
  `safe=''` to `urllib.parse.quote` does the work; a separate constant
  would be redundant and a future maintenance trap if the two drift.

## 5. Cache module — `src/cumulus/path_cache.py`

### 5.1 Schema (zero structural change)

The existing `path_cache` table schema is preserved column-by-column, with
exactly one rename: `remote_path_lower` → `remote_path_fold`. Content
semantics change for two columns; column types and indices are unchanged.

| Column | Type | Index | Content (was) | Content (now) |
|---|---|---|---|---|
| `file_id` | str | PK | Drive ID | Drive ID — unchanged |
| `remote_path` | bytes (`SqlPath`) | indexed | raw path | URL-encoded path (e.g. `/Documents/2024%2F05 report.pdf`) |
| `remote_path_fold` *(renamed from `remote_path_lower`)* | bytes (`SqlPath`) | indexed | `path.lower()` | `fold_path(remote_path)` |
| `parent_id` | str (nullable) | none | Drive ID | unchanged |
| `name` | str | none | raw Drive name | unchanged — still raw Drive name |
| `is_folder` | int | none | 0/1 | unchanged |

Schema-version handling: none. The `path_cache` table has never been
created in production; the new schema is the initial schema. Any
in-development databases on dev machines are throwaway.

### 5.2 Per-row invariants

Enforced by code (not SQL constraints):

1. `remote_path == compose_path(<parent's remote_path>, self.name)` for
   non-root rows. Root has no row; `root_id` is held only on the
   `PathIDCache` instance.
2. `remote_path_fold == fold_path(remote_path)`.
3. `(parent_id, fold_key(name))` is unique across the table — the
   case-conflict guard, enforced via the existing `has_case_conflict`
   path. Same-`file_id` updates are not flagged as conflicts.

### 5.3 API behavior changes

Public API signatures stay as-is. Behavior changes:

- **`set(remote_path, file_id, parent_id, name, is_folder)`** — caller
  passes encoded `remote_path` and raw `name`. Internally derives
  `remote_path_fold = fold_path(remote_path)`. Case-conflict guard uses
  `remote_path_fold` for the lookup key. Skip-with-log on conflict
  (returns `False`); behavior unchanged.
- **`move(old_path, new_path)`** — both paths encoded. Updates the row's
  `remote_path`, `remote_path_fold`, and `name`. The `name` column is set
  via `decode_name(new_path.rsplit("/", 1)[-1])` so it stays raw.
  Cascades to descendants via prefix substitution on `remote_path` and
  `remote_path_fold`. Descendant `name` columns don't change.
- **`remove(remote_path)`** — path encoded. Behavior unchanged
  structurally; `_select_children` switches to range-querying
  `remote_path_fold`.
- **`path_to_id(remote_path)`** — input encoded. Computes
  `fold_path(remote_path)`, queries `WHERE remote_path_fold = ?`.
- **`id_to_path(file_id)`** — returns the stored `remote_path` (encoded).
- **`has_case_conflict(remote_path, file_id=None)`** — uses
  `fold_path(remote_path)` for the lookup.
- **`resolve_path(file_id)`** — walks parent chain assembling encoded
  `remote_path` from segments; each segment is `encode_name(parent_name)`.
  Used as a cache integrity check by tests (per-row invariant 1).

### 5.4 Bug fix carried in this stage

The current `move()` implementation derives the row's new `name` via
`new_path.rsplit("/", 1)[-1]`. After the encoding switch, that string is
the *encoded* leaf, but the `name` column stores raw Drive form. Stage 1
fixes this: `name = decode_name(new_path.rsplit("/", 1)[-1])`.

## 6. Atomic `populate_from_listing`

### 6.1 Behavior contract

After `populate_from_listing(items)` returns, exactly one of:

1. The cache contains rows for every non-skipped item in `items`, with all
   per-row invariants from §5.2 holding.
2. The cache contains exactly what it contained before the call.

No intermediate state is observable.

### 6.2 Implementation shape

Three phases. Only the third touches the DB.

```python
def populate_from_listing(self, items):
    if self._root_id is None:
        raise RuntimeError(
            "root_id must be set before calling populate_from_listing"
        )

    # Phase 1: index by id, group children by parent (no DB).
    by_id, children_of = self._index_items(items)

    # Phase 2: BFS plan. Builds PathCacheEntry instances with all six
    # columns populated. Per-segment encode_name produces remote_path;
    # fold_path produces remote_path_fold. Dedup-by-fold-key warns and
    # drops losers (lexicographically lowest file_id wins, same as today).
    plan = self._plan_populate(by_id, children_of)

    # Phase 3: atomic swap.
    with self._db.transaction():
        self._table.clear()
        for entry in plan:
            self._table.update(entry)
```

The plan-then-commit ordering means encoding errors, cycle detection
errors, or any other planning-time exception leaves the DB untouched.

### 6.3 Failure-mode coverage

- Process crash mid-populate → SQLite rolls back; cache holds prior state.
- Encoding / BFS exception during plan → no DB writes attempted; cache
  holds prior state.
- Out-of-disk during commit → SQLite raises; transaction rolls back.
- Re-entrant call to `populate_from_listing` from the same thread →
  documented as "must hold sync_lock when calling" (same contract as the
  rest of the cache).

### 6.4 Why no `listed_at` column

REVIEW2 C7 proposed a per-folder `listed_at` column for resumable
incremental listing. The current `populate_from_listing` doesn't list
folder-by-folder — it accepts a single flat item list (the caller's bulk
`files.list` results across the entire sync root) and BFS-rebuilds. There
is no point during this build at which "folder X is fully listed" is a
useful per-row observation distinct from "the whole call succeeded."

The two real interruption sources:

1. **API pagination interrupted mid-stream.** Stage 3 concern. Solved by
   the read client checkpointing `nextPageToken`; the cache only ever
   sees a finished list. Out of scope for Stage 1.
2. **DB insert interrupted mid-write.** Stage 1 concern. Solved by the
   single transaction in §6.2.

### 6.5 Bulk-insert helper (conditional)

The transaction loop calls `Manager.update()` per row. For the 10k-file
case (Stage 7's E3 load test), per-row inserts may be measurably slower
than a multi-row `INSERT` or `executemany()`. If the existing
`Manager` doesn't already expose a bulk path, Stage 1 *may* add a
`Manager.bulk_insert(list[Model])` helper; otherwise defer to whichever
later stage's tests surface a real performance need. Correctness in Stage
1 doesn't depend on bulk insert.

## 7. Test plan (E2)

Two test files matching the module split.

### 7.1 `tests/offline/test_path_codec.py`

Pure-function tests. No DB.

- **`encode_name` / `decode_name` round-trip**, parametrized over:
  - ASCII alphanumerics and unreserved chars (`_.-~`)
  - Each char in the encode set: `/`, `%`, `*`, `?`, `"`, `<`, `>`, `|`,
    `\x00`, `\x01`, `\x1F`
  - Multi-char names containing one or more encode-set chars
  - Trailing space, trailing period
  - Non-ASCII Unicode: CJK, combining accents, emoji, BMP-edge chars
  - Empty string
  - Names that *look* pre-encoded (`100%25`, `a%2Fb`) — `encode_name`
    re-encodes them; `decode(encode(x)) == x` is the invariant.
- **`fold_key` correctness:**
  - Turkish `İ` casefolds and stays consistent post-NFC
  - German `ß` → `ss`
  - Greek final sigma `ς` and `σ` collapse
  - ASCII case folds normally
  - The double-NFC guarantee is observable: at least one fixture where
    `casefold(x)` differs from `NFC(casefold(NFC(x)))`, so the test
    catches a regression that drops either NFC step
- **`compose_path` cases:** root parent (`/`), nested parent, parent
  ending in `/`, raw name with `/`, raw name empty, parent and name both
  with non-ASCII.
- **`fold_path` cases:** path with already-encoded segments, path with
  mixed-case segments, root-only path.

### 7.2 `tests/offline/test_path_cache.py`

In-memory SQLite per test (existing or minimal new fixture).

- **Atomicity (marquee fault-injection test).**
  - `populate_from_listing` interrupted mid-commit → cache returns to
    prior state. Implementation: monkey-patch the per-row insert to
    raise on the Nth call, verify pre-call state recovered.
  - Pre-populated cache + interrupted populate → old data still queryable
    after the interruption.
  - Encoding raises during the plan phase → DB untouched.
- **Encoding round-trip end-to-end.** For each char in the encode set,
  drive a `set` → `id_to_path` → `name` round-trip; verify `remote_path`
  is encoded and `name` is raw.
- **Case-conflict behavior.**
  - Two items with names that fold-collide within the same parent:
    second `set` returns `False`, first retained.
  - Drive-side fold collision (`İ` vs `i̇`) detected.
  - `move` to a destination that fold-collides: returns `False`, source
    unchanged.
  - Update-of-self (`set` with same `file_id`): always succeeds.
- **Move correctness.**
  - Folder rename with N children → all descendants' `remote_path` and
    `remote_path_fold` prefix-substituted; descendant `name` columns
    unchanged.
  - Combined move + rename — single call updates name and parent path
    components (the spike Q5 shape).
  - Move with paths containing `%2F` segments — handled correctly; the
    encoded `/` doesn't confuse the prefix match.
- **Subtree query (`_select_children` / `remove`).**
  - Folder remove cascades to all descendants.
  - Range-bound correctness: a sibling whose path starts with the same
    prefix-as-string but isn't a descendant is *not* included
    (e.g. `/foo` and `/foobar`).
- **Path invariants helper.** A single `_assert_invariants(cache)` helper
  walks the table and checks per-row invariants 1 and 2 from §5.2; other
  tests call it as a closing sanity check.
- **Orphan handling.**
  - `populate_from_listing` with an item whose parent is missing:
    skipped from plan, warning logged, cache invariants hold.
  - `resolve_path(file_id)` on an orphaned entry returns `None` and logs.

### 7.3 Test coverage explicitly out of scope

- Concurrent mutation under a real `sync_lock` — Stage 7.
- Cache-vs-Drive divergence — needs Stage 3's read client.
- 10k-file load test — Stage 7 E3.

## 8. Risks and tradeoffs

| Risk | Mitigation |
|---|---|
| Encoding policy turns out to be wrong for some Drive name we haven't seen | Decode/encode are pure inverses by construction; any failure is observable in test corpus. The codec lives in one file and is easy to revise. |
| Reserved Windows basenames (`CON`, etc.) silently accepted by cache, fail on local-mirror write | Explicit out-of-scope §2.2; Stage 5 surfaces OS-write errors with clear messaging. Acceptable degraded path. |
| Per-row insert cost during 10k-file initial sync | Stage 7's E3 will measure; bulk-insert helper added in Stage 1 only if existing infrastructure makes it cheap, otherwise deferred. |
| Misnamed column (`remote_path_fold`) confuses readers familiar with Maestral's `path_lower` | Rename from `_lower` to `_fold` removes the explicit confusion; the §3.2 docstring on the cache module documents the cumulus-specific rationale. |

## 9. Definition of done

1. `src/cumulus/path_codec.py` exists with the §4.1 public surface.
2. `src/cumulus/path_cache.py` updated per §5 — schema rename, encoding
   throughout, atomic populate, `move()` `name`-derivation bug fixed.
3. `tests/offline/test_path_codec.py` and `tests/offline/test_path_cache.py`
   exist and pass; cover §7.1 and §7.2.
4. Existing tests on `google-drive-port` continue to pass (no regressions
   in unrelated tests).
5. The full `pytest tests/offline/` run is green.
6. No new Drive-client dependencies introduced — the codec is stdlib-only;
   the cache only depends on the existing ORM and `path_codec`.

## 10. Next step after this spec is approved

Invoke the `superpowers:writing-plans` skill to draft the Stage 1
implementation plan. Stage 2+ specs are written one at a time, after each
prior stage's plan and code merge.
