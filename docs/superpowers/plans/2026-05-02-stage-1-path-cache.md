# Stage 1: Path Cache Final Form Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the path-cache hardening described in `docs/superpowers/specs/2026-05-02-stage-1-path-cache-design.md`: extract a pure-function codec module (`path_codec.py`), update `path_cache.py` to use it (encoded paths, fold keys, schema column rename), and make `populate_from_listing` atomic against crashes. All work covered by offline unit tests.

**Architecture:** Two-module split. `src/cumulus/path_codec.py` is new — pure functions (`encode_name`, `decode_name`, `fold_key`, `compose_path`, `fold_path`), no DB or I/O. `src/cumulus/path_cache.py` is modified — it imports from `path_codec`, the schema column renames `remote_path_lower` → `remote_path_fold`, all lookup keys are derived via `fold_path()` rather than `.lower()`, and `populate_from_listing` is wrapped in a single SQLite transaction via a new `Database.transaction()` context manager.

**Tech Stack:** Python 3.10+, stdlib only (`urllib.parse`, `unicodedata`, `sqlite3`), pytest. No new dependencies.

---

## Spec Decisions Locked In

These are inherited from the design spec. Do not relitigate during execution:

- **Encoding policy:** URL-encoding via `urllib.parse.quote(name, safe='')`, uppercase hex, plus the trailing space/period rule (replace trailing `' '` or `'.'` with `%20` / `%2E`). Encode set: `/`, `%`, `*?"<>|`, NUL, control chars `\x01`–`\x1F`. Reserved Windows basenames (CON/PRN/AUX/NUL/COM*/LPT*) are explicitly **out of scope** — cache accepts them as-is. (spec §3.1)
- **Fold key:** `quote(NFC(casefold(NFC(name))), safe='')` per name component. The double NFC is intentional. (spec §3.2, §4.1)
- **Schema:** zero structural change beyond the rename `remote_path_lower` → `remote_path_fold`. Same column types and indices. The `name` column continues to hold the **raw** Drive name; `remote_path` now holds the **encoded** form. (spec §5.1)
- **API shape:** keep paths-as-keys public API (`set`, `move`, `remove`, `path_to_id`, etc.). Callers (Stage 3+) compose encoded paths via `path_codec.compose_path` before calling cache mutations. (spec §5.3)
- **Atomic populate:** plan-then-commit, three phases (index → BFS plan → atomic swap). Transaction wraps `clear()` + bulk insert. (spec §6.2)
- **No `listed_at` column.** Pagination checkpoint is Stage 3's job. (spec §6.4)
- **No schema-version check.** No `path_cache` database has been created in production. (spec §5.1)
- **Bulk insert helper:** *not* added in Stage 1 unless the existing per-row insert is measurably too slow on a 10k-file test (it isn't, this stage). Stage 7's E3 test surfaces the perf need if any. (spec §6.5)
- **Out-of-scope tests:** concurrent mutation under real `sync_lock`, cache-vs-Drive divergence, 10k-file load test. (spec §7.3)

## Deviations from this plan, caught during TDD execution

The plan was executed end-to-end on 2026-05-11. The following points
diverged from the literal plan text and are recorded here so future
readers don't misread the design intent.

1. **`encode_name` uses `safe=" "`, not `safe=""`** (`src/cumulus/path_codec.py`).
   The plan and spec said `safe=""`, but its accompanying tests assume
   internal spaces pass through unchanged (the trailing-space rule
   replaces a literal trailing `' '` with `%20`, which only makes sense
   if quote() didn't already encode it). `safe=" "` is the form that
   makes the trailing-char rule and the round-trip tests consistent.
   `fold_key` was given the same `safe=" "` for the same reason — so
   `fold_path` round-trips paths with internal spaces correctly.

2. **`fold_key` Turkish-i test asserts the actual fold output**, not
   idempotency on already-folded form. The plan's test was
   `fold_key(folded) == folded`, which fails because applying the codec
   twice double-encodes `%`. The replaced assertion checks that
   `fold_key("İstanbul") == "i%CC%87stanbul"` and that the equivalent
   decomposed input maps to the same key — which is what the spec §3.2
   "double NFC handles cases where casefold introduces decomposition"
   actually means.

3. **`TestEncodeIdempotency.test_already_encoded_double_encodes` second
   assertion fixed**. The plan had `decode(decode("%252F")) == "%2F"`,
   which evaluates to `"/"` (decode goes too far). The correct
   assertion is the single-decode form: `decode("%252F") == "%2F"`.

4. **`Manager.clear()` switched from `DROP TABLE`+recreate to
   `DELETE FROM`** (`src/cumulus/database/orm.py`). The plan called for
   wrapping `populate_from_listing`'s clear+insert in a single
   `Database.transaction()`. With the old `clear()`, the recreate path
   called `Cursor.executescript()`, which — per Python's `sqlite3`
   module — force-commits any pending transaction before running. That
   silently broke atomicity (a later rollback wouldn't restore the
   pre-`clear()` rows). `DELETE FROM` preserves the schema and
   participates correctly in the surrounding transaction. A regression
   test (`test_clear_inside_transaction_rolls_back` in
   `tests/offline/test_database.py`) pins this behavior.

These four fixes are all in service of preserving the spec's
load-bearing properties (codec round-trip, fold-equivalence, atomic
populate). None alter what spec §2 says is in or out of scope.

## File Structure

| Path | Action | Purpose |
|---|---|---|
| `src/cumulus/path_codec.py` | **Create** | Pure-function codec module: `encode_name`, `decode_name`, `fold_key`, `compose_path`, `fold_path` |
| `src/cumulus/database/core.py` | **Modify** | Add `Database.transaction()` context manager; gate `Database.execute()` auto-commit so it skips the implicit `with self.connection:` while a transaction is open |
| `src/cumulus/database/orm.py` | **Modify** | `Manager.clear()` → `DELETE FROM` (was `DROP TABLE`+recreate) so it is transaction-safe |
| `src/cumulus/path_cache.py` | **Modify** | Schema column rename, swap `.lower()` for `fold_path()`, fix `move()` name-derivation bug, atomic `populate_from_listing` |
| `tests/offline/test_path_codec.py` | **Create** | Unit tests for the codec (round-trips, fold-key edge cases, compose/fold paths) |
| `tests/offline/test_database.py` | **Create** | Unit tests for the new `Database.transaction()` semantics and `Manager.clear()` transactional behavior |
| `tests/offline/test_path_cache.py` | **Create** | Unit tests for the cache (encoding round-trip end-to-end, case conflicts, move cascade, subtree range, atomicity, invariants) |

## Test Strategy

- All tests are pytest, in `tests/offline/`, no network, no real Google account.
- Cache tests use an in-memory SQLite DB (`sqlite3.connect(":memory:")`). A small fixture (`make_cache()` helper inside `test_path_cache.py`) constructs a fresh `Database` + `PathIDCache` per test. We do **not** add a project-level `conftest.py` fixture for this — it'd be unused outside this one file.
- Cache tests follow this shape: set up state → mutate → assert → run `_assert_invariants(cache)` as a closing sanity check (defined in the test module itself, see Task 12).
- The atomicity test uses `unittest.mock.patch.object` to make `Manager.update` raise on the Nth call.

---

## Task 1: `path_codec.encode_name` / `decode_name`

**Files:**
- Create: `src/cumulus/path_codec.py`
- Create: `tests/offline/test_path_codec.py`

- [ ] **Step 1: Write `tests/offline/test_path_codec.py` with the encode/decode tests**

```python
"""Unit tests for cumulus.path_codec."""

from __future__ import annotations

import pytest

from cumulus.path_codec import decode_name, encode_name


class TestEncodeName:
    """Per-character encoding behavior."""

    def test_unreserved_chars_pass_through(self) -> None:
        # RFC 3986 unreserved set: alphanumerics + _.-~
        assert encode_name("simple_name-1.txt~") == "simple_name-1.txt~"

    @pytest.mark.parametrize(
        "raw,encoded",
        [
            ("a/b", "a%2Fb"),
            ("100%", "100%25"),
            ("foo*", "foo%2A"),
            ("a?b", "a%3Fb"),
            ('a"b', "a%22b"),
            ("a<b", "a%3Cb"),
            ("a>b", "a%3Eb"),
            ("a|b", "a%7Cb"),
            ("a\x00b", "a%00b"),
            ("a\x01b", "a%01b"),
            ("a\x1fb", "a%1Fb"),
        ],
    )
    def test_encode_set_chars(self, raw: str, encoded: str) -> None:
        assert encode_name(raw) == encoded

    def test_trailing_space_encoded(self) -> None:
        assert encode_name("foo ") == "foo%20"

    def test_trailing_period_encoded(self) -> None:
        assert encode_name("foo.") == "foo%2E"

    def test_internal_space_passes_through(self) -> None:
        # Only trailing space matters; internal spaces are FS-safe.
        assert encode_name("foo bar") == "foo bar"

    def test_internal_period_passes_through(self) -> None:
        assert encode_name("foo.bar.txt") == "foo.bar.txt"

    def test_uppercase_hex(self) -> None:
        # RFC 3986 canonical form is uppercase hex.
        assert encode_name("/") == "%2F"
        assert encode_name("/").lower() != encode_name("/")  # not "%2f"

    def test_non_ascii_unicode(self) -> None:
        # UTF-8 of "é" is C3 A9; uppercase hex.
        assert encode_name("café") == "caf%C3%A9"

    def test_empty_string(self) -> None:
        assert encode_name("") == ""


class TestDecodeName:
    def test_inverse_of_encode_for_random_inputs(self) -> None:
        cases = [
            "simple",
            "a/b",
            "100%",
            "foo ",
            "foo.",
            "café",
            "weird*?\"<>|",
            "with\x00nul",
            "",
            "已编码",
        ]
        for raw in cases:
            assert decode_name(encode_name(raw)) == raw, raw


class TestEncodeIdempotency:
    """encode_name is NOT idempotent on already-encoded forms.

    The spec contract: a Drive name literally `'%2F'` must round-trip back
    to `'%2F'`, so it must encode to `'%252F'`. encode(encode(x)) is
    therefore double-encoded — that's correct, not a bug.
    """

    def test_already_encoded_double_encodes(self) -> None:
        assert encode_name("%2F") == "%252F"
        assert decode_name(decode_name("%252F")) == "%2F"
```

- [ ] **Step 2: Run tests to verify they fail with import error**

Run: `pytest tests/offline/test_path_codec.py -v`
Expected: collection failure or `ModuleNotFoundError: No module named 'cumulus.path_codec'`.

- [ ] **Step 3: Create `src/cumulus/path_codec.py` with `encode_name` and `decode_name`**

```python
"""Pure-function codec for Drive name → filesystem-safe path segment.

Owns the policy that bridges Google Drive's permissive `name` field
(accepts `/`, control chars, Windows-reserved punctuation, etc.) and
the local filesystem (which does not). All transformations are pure
str → str — no I/O, no DB, no logging.

See `docs/superpowers/specs/2026-05-02-stage-1-path-cache-design.md` §3.1
for the full encoding policy.
"""

from __future__ import annotations

import unicodedata
import urllib.parse

__all__ = [
    "encode_name",
    "decode_name",
    "fold_key",
    "compose_path",
    "fold_path",
]


def encode_name(name: str) -> str:
    """Encode a single Drive name component for use as a path segment.

    Applies URL-encoding (uppercase hex) over the RFC 3986 unreserved set
    plus a trailing space/period rule for Windows compatibility.

    Idempotent only for already-canonical inputs; otherwise re-encoding
    double-encodes. This is correct: a Drive name literally ``'%2F'`` must
    round-trip back to ``'%2F'``, so it must encode to ``'%252F'``.
    """
    encoded = urllib.parse.quote(name, safe="")
    if encoded.endswith(" "):
        encoded = encoded[:-1] + "%20"
    elif encoded.endswith("."):
        encoded = encoded[:-1] + "%2E"
    return encoded


def decode_name(encoded: str) -> str:
    """Inverse of :func:`encode_name`."""
    return urllib.parse.unquote(encoded)
```

- [ ] **Step 4: Run tests to verify the encode/decode tests pass**

Run: `pytest tests/offline/test_path_codec.py::TestEncodeName tests/offline/test_path_codec.py::TestDecodeName tests/offline/test_path_codec.py::TestEncodeIdempotency -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/path_codec.py tests/offline/test_path_codec.py
git commit -m "feat(path_codec): add encode_name / decode_name with trailing-char rule"
```

---

## Task 2: `path_codec.fold_key`

**Files:**
- Modify: `src/cumulus/path_codec.py`
- Modify: `tests/offline/test_path_codec.py`

- [ ] **Step 1: Append fold-key tests to `tests/offline/test_path_codec.py`**

Add this class to the existing test file:

```python
from cumulus.path_codec import fold_key


class TestFoldKey:
    """Unicode case-fold + NFC normalization."""

    def test_ascii_case_folds(self) -> None:
        assert fold_key("Documents") == fold_key("documents") == "documents"

    def test_german_sharp_s_folds_to_ss(self) -> None:
        # casefold('ß') == 'ss'; both inputs collapse.
        assert fold_key("Straße") == fold_key("STRASSE") == "strasse"

    def test_greek_final_sigma_collapses(self) -> None:
        # 'ς' (final sigma) and 'σ' (medial sigma) both casefold to 'σ'.
        assert fold_key("Σοφος") == fold_key("σοφος") == fold_key("σοφος")

    def test_turkish_dotted_i_folds_consistently(self) -> None:
        # 'İ' (U+0130) casefolds to 'i' + combining dot above (U+0307).
        # The NFC-after-casefold step keeps the result stable.
        folded = fold_key("İstanbul")
        # Re-folding is idempotent.
        assert fold_key(folded) == folded

    def test_double_nfc_observable(self) -> None:
        # Decomposed input: 'é' as 'e' + combining acute (U+0301).
        decomposed = "café"
        composed = "café"
        # casefold(x) without leading NFC differs by composition order;
        # fold_key normalizes both to the same canonical form.
        assert fold_key(decomposed) == fold_key(composed)

    def test_url_encoded_output(self) -> None:
        # Output is URL-encoded so it's a stable byte string for indexing.
        assert fold_key("Café") == "caf%C3%A9"

    def test_empty_string(self) -> None:
        assert fold_key("") == ""

    def test_encode_set_chars_in_input(self) -> None:
        # fold_key encodes the encode-set the same way encode_name does.
        # ASCII letters and the encoded form are case-stable.
        assert fold_key("a/b") == "a%2Fb"
        assert fold_key("A/B") == "a%2Fb"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/offline/test_path_codec.py::TestFoldKey -v`
Expected: `ImportError: cannot import name 'fold_key'`.

- [ ] **Step 3: Add `fold_key` to `src/cumulus/path_codec.py`**

Append to the module:

```python
def fold_key(name: str) -> str:
    """Case-fold key for one name component.

    Computes ``quote(NFC(casefold(NFC(name))), safe='')``. The double NFC
    handles cases where casefold introduces decomposition (e.g. Turkish
    'İ' casefolds to 'i' + combining dot above, which the trailing NFC
    keeps in canonical form).
    """
    normalized = unicodedata.normalize("NFC", name)
    folded = normalized.casefold()
    folded = unicodedata.normalize("NFC", folded)
    return urllib.parse.quote(folded, safe="")
```

- [ ] **Step 4: Run all codec tests to verify the new tests pass**

Run: `pytest tests/offline/test_path_codec.py -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/path_codec.py tests/offline/test_path_codec.py
git commit -m "feat(path_codec): add Unicode-correct fold_key (NFC + casefold)"
```

---

## Task 3: `path_codec.compose_path` and `fold_path`

**Files:**
- Modify: `src/cumulus/path_codec.py`
- Modify: `tests/offline/test_path_codec.py`

- [ ] **Step 1: Append `compose_path` and `fold_path` tests**

Add to `tests/offline/test_path_codec.py`:

```python
from cumulus.path_codec import compose_path, fold_path


class TestComposePath:
    def test_root_parent(self) -> None:
        assert compose_path("/", "report.pdf") == "/report.pdf"

    def test_nested_parent(self) -> None:
        assert (
            compose_path("/Documents", "report.pdf") == "/Documents/report.pdf"
        )

    def test_parent_ending_in_slash_normalized(self) -> None:
        # Caller may pass "/" or "/Documents"; both yield correct output.
        assert (
            compose_path("/Documents/", "report.pdf") == "/Documents/report.pdf"
        )

    def test_raw_name_with_slash_encoded(self) -> None:
        # The Q4 spike case.
        assert (
            compose_path("/Documents", "2024/05 report.pdf")
            == "/Documents/2024%2F05 report.pdf"
        )

    def test_raw_name_with_non_ascii(self) -> None:
        assert compose_path("/", "café.txt") == "/caf%C3%A9.txt"

    def test_raw_name_empty_string(self) -> None:
        # Defensive: empty leaf returns parent + "/" + "".
        assert compose_path("/Documents", "") == "/Documents/"

    def test_parent_already_encoded_passes_through(self) -> None:
        # Parent path is already encoded; compose_path doesn't re-encode it.
        assert (
            compose_path("/2024%2F05 reports", "Q1.pdf")
            == "/2024%2F05 reports/Q1.pdf"
        )


class TestFoldPath:
    def test_root_only(self) -> None:
        assert fold_path("/") == "/"

    def test_simple_path(self) -> None:
        assert fold_path("/Documents/Report.pdf") == "/documents/report.pdf"

    def test_path_with_encoded_segment(self) -> None:
        # The encoded segment %2F is decoded, folded, re-encoded.
        # "2024%2F05" → raw "2024/05" → fold → "2024%2F05" (digits unchanged).
        assert (
            fold_path("/Documents/2024%2F05 report.pdf")
            == "/documents/2024%2F05 report.pdf"
        )

    def test_path_with_non_ascii(self) -> None:
        assert fold_path("/Caf%C3%A9/index.txt") == "/caf%C3%A9/index.txt"

    def test_mixed_case_segments(self) -> None:
        assert fold_path("/FOO/Bar/BAZ") == "/foo/bar/baz"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/offline/test_path_codec.py::TestComposePath tests/offline/test_path_codec.py::TestFoldPath -v`
Expected: `ImportError: cannot import name 'compose_path'`.

- [ ] **Step 3: Add `compose_path` and `fold_path` to `src/cumulus/path_codec.py`**

Append to the module:

```python
def compose_path(parent_path: str, raw_name: str) -> str:
    """Build an encoded child path from an encoded parent path and a raw
    Drive name.

    Example:
        compose_path('/Documents', '2024/05 report.pdf')
        -> '/Documents/2024%2F05 report.pdf'

    ``parent_path == '/'`` is the root; result is ``'/' + encode_name(raw_name)``.
    A trailing slash on ``parent_path`` is ignored.
    """
    if parent_path.endswith("/") and parent_path != "/":
        parent_path = parent_path[:-1]
    encoded_leaf = encode_name(raw_name)
    if parent_path == "/":
        return "/" + encoded_leaf
    return parent_path + "/" + encoded_leaf


def fold_path(encoded_path: str) -> str:
    """Fold key for a full encoded path.

    Splits on ``/``, decodes each segment, applies :func:`fold_key`,
    rejoins with ``/``. Used by the cache to derive ``remote_path_fold``
    from ``remote_path``.
    """
    if encoded_path == "/":
        return "/"
    # Leading "/" produces an empty first segment; preserve it.
    parts = encoded_path.split("/")
    folded_parts = [fold_key(decode_name(p)) if p else p for p in parts]
    return "/".join(folded_parts)
```

- [ ] **Step 4: Run all codec tests to verify they pass**

Run: `pytest tests/offline/test_path_codec.py -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/path_codec.py tests/offline/test_path_codec.py
git commit -m "feat(path_codec): add compose_path and fold_path"
```

---

## Task 4: `Database.transaction()` context manager

**Files:**
- Modify: `src/cumulus/database/core.py`
- Create: `tests/offline/test_database.py`

The current `Database.execute()` wraps each call in `with self.connection:`, which auto-commits per-call. Atomic populate needs multi-statement transactions, so we add an explicit `transaction()` context manager and gate the auto-commit on a flag so per-statement `db.execute()` calls inside a transaction don't break.

- [ ] **Step 1: Write `tests/offline/test_database.py`**

```python
"""Unit tests for cumulus.database.core.Database.transaction."""

from __future__ import annotations

import sqlite3

import pytest

from cumulus.database.core import Database


@pytest.fixture
def db() -> Database:
    conn = sqlite3.connect(":memory:")
    db = Database(conn)
    db.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT);")
    return db


class TestTransaction:
    def test_commit_on_success(self, db: Database) -> None:
        with db.transaction():
            db.execute("INSERT INTO t (val) VALUES (?)", "a")
            db.execute("INSERT INTO t (val) VALUES (?)", "b")
        rows = db.execute("SELECT val FROM t").fetchall()
        assert {r["val"] for r in rows} == {"a", "b"}

    def test_rollback_on_exception(self, db: Database) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with db.transaction():
                db.execute("INSERT INTO t (val) VALUES (?)", "a")
                raise RuntimeError("boom")
        # Insert was rolled back.
        rows = db.execute("SELECT val FROM t").fetchall()
        assert rows == []

    def test_execute_outside_transaction_still_auto_commits(
        self, db: Database
    ) -> None:
        # Existing behavior preserved.
        db.execute("INSERT INTO t (val) VALUES (?)", "a")
        rows = db.execute("SELECT val FROM t").fetchall()
        assert [r["val"] for r in rows] == ["a"]

    def test_nested_transaction_does_not_double_commit(
        self, db: Database
    ) -> None:
        # Re-entrant: inner is a no-op flag-wise; outer commits/rolls back.
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.execute("INSERT INTO t (val) VALUES (?)", "a")
                with db.transaction():
                    db.execute("INSERT INTO t (val) VALUES (?)", "b")
                raise RuntimeError("outer fails")
        rows = db.execute("SELECT val FROM t").fetchall()
        assert rows == []  # both rolled back
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/offline/test_database.py -v`
Expected: `AttributeError: 'Database' object has no attribute 'transaction'`.

- [ ] **Step 3: Modify `src/cumulus/database/core.py`**

Replace the entire file contents:

```python
"""
This model defines our core SQLite database interface.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator


class Database:
    """Wrapper around sqlite3.Connection with atomic transactions."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        self.connection = connection
        self._in_transaction = False

    def close(self) -> None:
        """Closes the SQL connection."""
        self.connection.close()

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        """
        Creates a cursor and executes the given SQL statement.

        Outside of a :meth:`transaction` block, each call auto-commits
        (wrapped in ``with self.connection:``). Inside a transaction,
        calls participate in the surrounding transaction and don't commit
        on their own.

        :param sql: SQL statement to execute.
        :param args: Parameters to substitute for placeholders in SQL statement.
        :returns: The created cursor.
        """
        if self._in_transaction:
            return self.connection.execute(sql, args)
        with self.connection:
            return self.connection.execute(sql, args)

    def executescript(self, script: str) -> None:
        """
        Creates a cursor and executes the given SQL script.

        :param script: SQL script to execute.
        """
        if self._in_transaction:
            self.connection.cursor().executescript(script)
            return
        with self.connection:
            self.connection.cursor().executescript(script)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Begin a transaction. Commits on success, rolls back on exception.

        Re-entrant calls are no-ops; only the outermost ``transaction()``
        commits or rolls back.
        """
        if self._in_transaction:
            yield
            return
        self._in_transaction = True
        try:
            with self.connection:
                yield
        finally:
            self._in_transaction = False
```

- [ ] **Step 4: Run all tests to verify the new tests pass and existing tests still pass**

Run: `pytest tests/offline/test_database.py -v`
Expected: all green.

Run: `pytest tests/offline/ -v`
Expected: no regressions in any other tests; `test_database.py` passes.

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/database/core.py tests/offline/test_database.py
git commit -m "feat(database): add Database.transaction() context manager"
```

---

## Task 5: Cache schema rename + lookup methods (set / has_case_conflict / path_to_id / id_to_path / get_entry / _select_children / remove)

**Files:**
- Modify: `src/cumulus/path_cache.py`
- Create: `tests/offline/test_path_cache.py`

This task is the largest because the schema column rename (`remote_path_lower` → `remote_path_fold`) and the swap from `.lower()` to `fold_path()` must happen together — they all read or write the same column. The methods updated here are the "mechanical codec swap" set; `move`, `resolve_path`, and `populate_from_listing` get their own tasks.

- [ ] **Step 1: Create `tests/offline/test_path_cache.py` with the lookup-method tests**

```python
"""Unit tests for cumulus.path_cache.PathIDCache."""

from __future__ import annotations

import sqlite3

import pytest

from cumulus.database.core import Database
from cumulus.path_cache import PathIDCache


def make_cache(root_id: str = "ROOT") -> PathIDCache:
    """Construct a fresh PathIDCache backed by an in-memory SQLite DB."""
    conn = sqlite3.connect(":memory:")
    db = Database(conn)
    cache = PathIDCache(db)
    cache.root_id = root_id
    return cache


class TestSetAndLookups:
    def test_set_then_path_to_id(self) -> None:
        cache = make_cache()
        ok = cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        assert ok is True
        assert cache.path_to_id("/report.pdf") == "F1"

    def test_set_then_id_to_path(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        assert cache.id_to_path("F1") == "/report.pdf"

    def test_id_to_path_for_root(self) -> None:
        cache = make_cache()
        assert cache.id_to_path("ROOT") == "/"

    def test_path_to_id_for_root(self) -> None:
        cache = make_cache()
        assert cache.path_to_id("/") == "ROOT"

    def test_path_to_id_case_insensitive(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/Documents",
            file_id="F2",
            parent_id="ROOT",
            name="Documents",
            is_folder=True,
        )
        assert cache.path_to_id("/documents") == "F2"
        assert cache.path_to_id("/DOCUMENTS") == "F2"

    def test_path_to_id_unicode_fold(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/Caf%C3%A9",
            file_id="F3",
            parent_id="ROOT",
            name="Café",
            is_folder=True,
        )
        # Different case input still resolves.
        assert cache.path_to_id("/caf%C3%A9") == "F3"

    def test_path_to_id_missing_returns_none(self) -> None:
        cache = make_cache()
        assert cache.path_to_id("/no-such-path") is None

    def test_get_entry_returns_full_row(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        entry = cache.get_entry("F1")
        assert entry is not None
        assert entry.file_id == "F1"
        assert entry.name == "report.pdf"
        assert entry.parent_id == "ROOT"
        assert entry.is_folder == 0


class TestEncodingRoundTrip:
    @pytest.mark.parametrize(
        "raw_name,encoded_path",
        [
            ("2024/05 report.pdf", "/2024%2F05 report.pdf"),
            ("100%", "/100%25"),
            ("foo?", "/foo%3F"),
            ("foo ", "/foo%20"),
            ("foo.", "/foo%2E"),
            ("café", "/caf%C3%A9"),
        ],
    )
    def test_set_stores_raw_name_and_encoded_path(
        self, raw_name: str, encoded_path: str
    ) -> None:
        cache = make_cache()
        cache.set(
            remote_path=encoded_path,
            file_id="X",
            parent_id="ROOT",
            name=raw_name,
            is_folder=False,
        )
        # name column holds raw, remote_path column holds encoded.
        entry = cache.get_entry("X")
        assert entry is not None
        assert entry.name == raw_name
        assert entry.remote_path == encoded_path
        # Round-trip: encoded path resolves back to id, id back to path.
        assert cache.path_to_id(encoded_path) == "X"
        assert cache.id_to_path("X") == encoded_path


class TestCaseConflict:
    def test_fold_collision_within_same_parent_skipped(self) -> None:
        cache = make_cache()
        ok1 = cache.set(
            remote_path="/Documents",
            file_id="F1",
            parent_id="ROOT",
            name="Documents",
            is_folder=True,
        )
        ok2 = cache.set(
            remote_path="/documents",
            file_id="F2",
            parent_id="ROOT",
            name="documents",
            is_folder=True,
        )
        assert ok1 is True
        assert ok2 is False  # second one rejected
        # First one still resolves.
        assert cache.path_to_id("/Documents") == "F1"
        assert cache.path_to_id("/documents") == "F1"  # fold-equal

    def test_update_of_same_file_not_a_conflict(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        # Re-set with the same file_id and same path: succeeds.
        ok = cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        assert ok is True

    def test_turkish_dotted_i_collision_detected(self) -> None:
        cache = make_cache()
        ok1 = cache.set(
            remote_path="/%C4%B0stanbul",  # 'İ' encoded
            file_id="F1",
            parent_id="ROOT",
            name="İstanbul",
            is_folder=True,
        )
        # 'i' + combining-dot — fold-equal to 'İ'.
        ok2 = cache.set(
            remote_path="/i%CC%87stanbul",
            file_id="F2",
            parent_id="ROOT",
            name="i̇stanbul",
            is_folder=True,
        )
        assert ok1 is True
        assert ok2 is False


class TestRemove:
    def test_remove_deletes_entry(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/report.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="report.pdf",
            is_folder=False,
        )
        cache.remove("/report.pdf")
        assert cache.path_to_id("/report.pdf") is None

    def test_remove_cascades_to_children(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/Documents",
            file_id="DIR",
            parent_id="ROOT",
            name="Documents",
            is_folder=True,
        )
        cache.set(
            remote_path="/Documents/a.pdf",
            file_id="A",
            parent_id="DIR",
            name="a.pdf",
            is_folder=False,
        )
        cache.set(
            remote_path="/Documents/sub/b.pdf",
            file_id="B",
            parent_id="SUB",  # parent_id can be inconsistent for this test
            name="b.pdf",
            is_folder=False,
        )
        cache.remove("/Documents")
        assert cache.path_to_id("/Documents") is None
        assert cache.path_to_id("/Documents/a.pdf") is None
        assert cache.path_to_id("/Documents/sub/b.pdf") is None

    def test_remove_does_not_match_sibling_with_shared_prefix(self) -> None:
        # /foo and /foobar — removing /foo must not remove /foobar.
        cache = make_cache()
        cache.set(
            remote_path="/foo",
            file_id="A",
            parent_id="ROOT",
            name="foo",
            is_folder=True,
        )
        cache.set(
            remote_path="/foobar",
            file_id="B",
            parent_id="ROOT",
            name="foobar",
            is_folder=True,
        )
        cache.remove("/foo")
        assert cache.path_to_id("/foo") is None
        assert cache.path_to_id("/foobar") == "B"
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `pytest tests/offline/test_path_cache.py -v`
Expected: most tests fail because the cache currently uses `.lower()` keys, mismatched against the new fold-based path inputs in the tests.

- [ ] **Step 3: Modify `src/cumulus/path_cache.py` — schema + lookup-method updates**

Open the file and apply these edits in order:

**(a) Update the module docstring (top of file):**

```python
"""Persistent mapping between remote paths and Google Drive file IDs.

Google Drive's API is ID-based: every file/folder has an opaque ``file_id``.
The sync engine (and the user) think in paths. This module bridges the gap.

Key responsibilities:

* **Path encoding** — Drive accepts characters in ``name`` (slash, control
  chars, Windows-reserved punctuation) that don't survive a filesystem
  path round-trip. The cache stores ``remote_path`` in URL-encoded form
  (via ``cumulus.path_codec``); the ``name`` column stores the raw Drive
  name as-is for upload purposes.
* **Case-fold lookup key** — ``remote_path_fold`` holds
  ``fold_path(remote_path)`` (NFC + casefold per name component). This
  replaces the simple ``.lower()`` key used in earlier prototypes; Drive
  doesn't return a server-computed fold form (Dropbox did), so we
  compute it client-side.
* **Concurrency** — the path cache shares ``SyncEngine.sync_lock``. It is
  only accessed during sync operations that already hold the lock, so no
  additional lock is needed.
* **Atomic populate** — ``populate_from_listing`` runs inside a single
  ``Database.transaction()``; on failure the cache is restored to its
  prior state.

See ``docs/superpowers/specs/2026-05-02-stage-1-path-cache-design.md`` for
the full design.
"""
```

**(b) Add the import for `path_codec` (with the existing imports):**

```python
from .path_codec import compose_path, encode_name, fold_path
```

**(c) Rename the `PathCacheEntry.remote_path_lower` column to `remote_path_fold` and update its docstring:**

```python
class PathCacheEntry(Model):
    """One row per known file or folder on Google Drive."""

    __tablename__ = "path_cache"

    file_id = NonNullColumn(SqlString(), primary_key=True)
    """Google Drive file ID (unique, opaque string)."""

    remote_path = NonNullColumn(SqlPath(), index=True)
    """URL-encoded virtual path, e.g. ``/Documents/2024%2F05 report.pdf``.

    Encoded via :func:`cumulus.path_codec.encode_name` per name component.
    The encoding makes the path safe to use as a filesystem path segment
    even when Drive ``name`` fields contain ``/``, control chars, or
    Windows-reserved punctuation."""

    remote_path_fold = NonNullColumn(SqlPath(), index=True)
    """Case-fold lookup key, computed as
    :func:`cumulus.path_codec.fold_path` (NFC + casefold per component).

    This is *not* a simple lowercase form — Drive's case-insensitive
    semantics use Unicode collation (closer to Python's ``str.casefold``
    plus NFC) than ASCII lowercase."""

    parent_id = Column(SqlString())
    """File ID of the parent folder.  ``None`` for the root."""

    name = NonNullColumn(SqlString())
    """Raw Drive name (e.g. ``2024/05 report.pdf`` literally, with the
    slash). Used by Stage 4 upload when re-naming on Drive."""

    is_folder = NonNullColumn(SqlInt())
    """1 for folders, 0 for files."""
```

**(d) Replace `path_to_id`:**

```python
    def path_to_id(self, remote_path: str) -> str | None:
        """Return the file ID for *remote_path*, or ``None`` if not cached.

        ``remote_path`` is the encoded path. The lookup is
        case-insensitive (uses ``remote_path_fold``).
        """
        if remote_path == self.ROOT_PATH:
            return self._root_id

        fold = fold_path(remote_path)
        rows = self._table.select_sql(
            "WHERE remote_path_fold = ?", os.fsencode(fold)
        )
        if rows:
            return rows[0].file_id
        return None
```

**(e) Replace `has_case_conflict`:**

```python
    def has_case_conflict(
        self, remote_path: str, file_id: str | None = None
    ) -> str | None:
        """Check whether *remote_path* case-conflicts with an existing entry.

        Returns the ``file_id`` of the conflicting entry, or ``None`` if
        there is no conflict. An entry with the same *file_id* is **not**
        considered a conflict (that's just an update of the same file).
        """
        fold = fold_path(remote_path)
        rows = self._table.select_sql(
            "WHERE remote_path_fold = ?", os.fsencode(fold)
        )
        for row in rows:
            if row.file_id != file_id:
                return row.file_id
        return None
```

**(f) Replace `set`:**

```python
    def set(
        self,
        remote_path: str,
        file_id: str,
        parent_id: str | None,
        name: str,
        is_folder: bool,
    ) -> bool:
        """Insert or update a cache entry.

        :param remote_path: encoded path (use
            :func:`cumulus.path_codec.compose_path` to derive from a Drive
            change event's parent + name).
        :param name: raw Drive ``name`` (un-encoded).

        Returns ``True`` if the entry was stored, ``False`` if it was
        rejected due to a case-insensitive name conflict with a different
        file. Updating an existing entry (same *file_id*) always succeeds.
        """
        conflict_id = self.has_case_conflict(remote_path, file_id)
        if conflict_id is not None:
            logger.warning(
                "Case conflict: '%s' (id=%s) conflicts with existing "
                "entry (id=%s). Skipping — rename one of the files in "
                "the Google Drive web UI to fix this.",
                remote_path,
                file_id,
                conflict_id,
            )
            return False

        entry = PathCacheEntry(
            file_id=file_id,
            remote_path=remote_path,
            remote_path_fold=fold_path(remote_path),
            parent_id=parent_id,
            name=name,
            is_folder=int(is_folder),
        )
        self._table.update(entry)  # upsert
        return True
```

**(g) Replace `remove`:**

```python
    def remove(self, remote_path: str) -> None:
        """Remove *remote_path* and all its children from the cache.

        ``remote_path`` is the encoded path.
        """
        fold = fold_path(remote_path)

        # Delete exact match.
        rows = self._table.select_sql(
            "WHERE remote_path_fold = ?", os.fsencode(fold)
        )
        for row in rows:
            self._table.delete_primary_key(row.file_id)

        # Delete children using a range query on the BLOB column.
        children = self._select_children(fold)
        for child in children:
            self._table.delete_primary_key(child.file_id)
```

**(h) Replace `_select_children`:**

```python
    def _select_children(self, parent_fold: str) -> list[PathCacheEntry]:
        """Select all entries whose ``remote_path_fold`` starts with
        ``parent_fold + "/"``.

        Uses a range query (``>=`` / ``<``) which works correctly on BLOB
        columns, unlike ``LIKE``.
        """
        prefix = parent_fold + "/"
        # Upper bound: increment the last character of the parent path.
        # '0' == chr(ord('/') + 1).
        upper = parent_fold + "0"
        return self._table.select_sql(
            "WHERE remote_path_fold >= ? AND remote_path_fold < ?",
            os.fsencode(prefix),
            os.fsencode(upper),
        )
```

**(i) Leave `move`, `resolve_path`, `populate_from_listing`, `clear`, `count`, `id_to_path`, `get_entry`, root-management properties untouched in this task** — they're addressed in Tasks 6, 7, 8.

- [ ] **Step 4: Run new path-cache tests to verify they pass**

Run: `pytest tests/offline/test_path_cache.py -v`
Expected: tests in `TestSetAndLookups`, `TestEncodingRoundTrip`, `TestCaseConflict`, `TestRemove` are green. (Tests for `move`, `resolve_path`, `populate_from_listing` don't exist yet.)

- [ ] **Step 5: Run the full offline suite to check for regressions**

Run: `pytest tests/offline/ -v`
Expected: no regressions. The cache module file imports cleanly; old `.lower()` callers don't exist outside this module.

- [ ] **Step 6: Commit**

```bash
git add src/cumulus/path_cache.py tests/offline/test_path_cache.py
git commit -m "feat(path_cache): rename column to remote_path_fold, swap lookups to codec"
```

---

## Task 6: `move()` rewrite (codec + name-derivation bug fix)

**Files:**
- Modify: `src/cumulus/path_cache.py`
- Modify: `tests/offline/test_path_cache.py`

The current `move()` derives the new `name` via `new_path.rsplit("/", 1)[-1]`, which after encoding pulls the *encoded* leaf — but the `name` column stores the raw Drive form. Fix: `name = decode_name(...)`. Also: switch lookups and prefix substitution from `.lower()` to `fold_path()`.

- [ ] **Step 1: Append move tests to `tests/offline/test_path_cache.py`**

```python
class TestMove:
    def test_simple_rename(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/old.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="old.pdf",
            is_folder=False,
        )
        ok = cache.move("/old.pdf", "/new.pdf")
        assert ok is True
        assert cache.path_to_id("/old.pdf") is None
        assert cache.path_to_id("/new.pdf") == "F1"
        # name column updated to the raw new name.
        entry = cache.get_entry("F1")
        assert entry is not None
        assert entry.name == "new.pdf"

    def test_rename_with_slash_in_new_name(self) -> None:
        # Combined move/rename where new raw name contains '/'.
        cache = make_cache()
        cache.set(
            remote_path="/old.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="old.pdf",
            is_folder=False,
        )
        # New encoded path for raw name "2024/05.pdf"
        ok = cache.move("/old.pdf", "/2024%2F05.pdf")
        assert ok is True
        entry = cache.get_entry("F1")
        assert entry is not None
        # name column is the raw form, not the encoded form.
        assert entry.name == "2024/05.pdf"
        assert entry.remote_path == "/2024%2F05.pdf"

    def test_folder_rename_cascades_to_descendants(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/old",
            file_id="DIR",
            parent_id="ROOT",
            name="old",
            is_folder=True,
        )
        cache.set(
            remote_path="/old/a.pdf",
            file_id="A",
            parent_id="DIR",
            name="a.pdf",
            is_folder=False,
        )
        cache.set(
            remote_path="/old/sub/b.pdf",
            file_id="B",
            parent_id="SUB",
            name="b.pdf",
            is_folder=False,
        )
        ok = cache.move("/old", "/new")
        assert ok is True
        assert cache.path_to_id("/new") == "DIR"
        assert cache.path_to_id("/new/a.pdf") == "A"
        assert cache.path_to_id("/new/sub/b.pdf") == "B"
        # Descendant `name` columns must NOT change — only their paths.
        entry_a = cache.get_entry("A")
        assert entry_a is not None
        assert entry_a.name == "a.pdf"
        entry_b = cache.get_entry("B")
        assert entry_b is not None
        assert entry_b.name == "b.pdf"

    def test_move_does_not_touch_sibling_with_shared_prefix(self) -> None:
        # /foo → /bar must not affect /foobar.
        cache = make_cache()
        cache.set(
            remote_path="/foo",
            file_id="A",
            parent_id="ROOT",
            name="foo",
            is_folder=True,
        )
        cache.set(
            remote_path="/foobar",
            file_id="B",
            parent_id="ROOT",
            name="foobar",
            is_folder=True,
        )
        cache.move("/foo", "/bar")
        assert cache.path_to_id("/bar") == "A"
        assert cache.path_to_id("/foobar") == "B"

    def test_move_to_case_conflicting_target_skipped(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/A.pdf",
            file_id="F1",
            parent_id="ROOT",
            name="A.pdf",
            is_folder=False,
        )
        cache.set(
            remote_path="/B.pdf",
            file_id="F2",
            parent_id="ROOT",
            name="B.pdf",
            is_folder=False,
        )
        # Try to rename F2 → "a.pdf" (fold-collides with F1).
        ok = cache.move("/B.pdf", "/a.pdf")
        assert ok is False
        # Both rows untouched.
        assert cache.path_to_id("/A.pdf") == "F1"
        assert cache.path_to_id("/B.pdf") == "F2"
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `pytest tests/offline/test_path_cache.py::TestMove -v`
Expected: failures because `move` still uses `.lower()` and the buggy raw-name derivation.

- [ ] **Step 3: Replace `move()` in `src/cumulus/path_cache.py`**

First add the import for `decode_name` to the existing import line:

```python
from .path_codec import compose_path, decode_name, encode_name, fold_path
```

Then replace `move`:

```python
    def move(self, old_path: str, new_path: str) -> bool:
        """Rename *old_path* → *new_path*, updating all children atomically.

        Both paths are encoded. Returns ``False`` (and does nothing) if
        *new_path* case-conflicts with an existing entry that isn't the
        item being moved.
        """
        old_fold = fold_path(old_path)
        rows = self._table.select_sql(
            "WHERE remote_path_fold = ?", os.fsencode(old_fold)
        )
        moving_id = rows[0].file_id if rows else None

        # Check for a case conflict at the destination.
        conflict_id = self.has_case_conflict(new_path, moving_id)
        if conflict_id is not None:
            logger.warning(
                "Case conflict on move: '%s' → '%s' conflicts with "
                "existing entry (id=%s). Skipping move.",
                old_path,
                new_path,
                conflict_id,
            )
            return False

        new_fold = fold_path(new_path)

        # Update the entry itself: new path, new fold, new raw name
        # (the leaf of new_path is encoded; decode it to get the raw
        # Drive name back out).
        new_leaf_encoded = new_path.rsplit("/", 1)[-1]
        new_leaf_raw = decode_name(new_leaf_encoded)

        for row in rows:
            row.remote_path = new_path
            row.remote_path_fold = new_fold
            row.name = new_leaf_raw
            self._table.update(row)

        # Update children's paths via prefix substitution. Children's
        # `name` columns don't change — only their stored paths.
        old_prefix = old_fold + "/"
        children = self._select_children(old_fold)
        for child in children:
            fold_suffix = child.remote_path_fold[len(old_prefix):]
            child.remote_path_fold = new_fold + "/" + fold_suffix
            cased_suffix = child.remote_path[len(old_path) + 1:]
            child.remote_path = new_path + "/" + cased_suffix
            self._table.update(child)

        return True
```

- [ ] **Step 4: Run move tests to verify they pass**

Run: `pytest tests/offline/test_path_cache.py::TestMove -v`
Expected: all green.

- [ ] **Step 5: Run the full path_cache test file**

Run: `pytest tests/offline/test_path_cache.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/cumulus/path_cache.py tests/offline/test_path_cache.py
git commit -m "fix(path_cache): codec-aware move() with raw-name derivation fix"
```

---

## Task 7: `resolve_path()` rewrite

**Files:**
- Modify: `src/cumulus/path_cache.py`
- Modify: `tests/offline/test_path_cache.py`

`resolve_path` walks the parent chain to reconstruct an entry's path. Now that `name` holds the raw Drive form, the walk must `encode_name` each segment when assembling the encoded path.

- [ ] **Step 1: Append resolve_path tests**

```python
class TestResolvePath:
    def test_resolve_simple_path(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/Documents",
            file_id="DIR",
            parent_id="ROOT",
            name="Documents",
            is_folder=True,
        )
        cache.set(
            remote_path="/Documents/report.pdf",
            file_id="F1",
            parent_id="DIR",
            name="report.pdf",
            is_folder=False,
        )
        assert cache.resolve_path("F1") == "/Documents/report.pdf"

    def test_resolve_path_with_slash_in_raw_name(self) -> None:
        # Walk reconstructs the encoded path even when raw names contain '/'.
        cache = make_cache()
        cache.set(
            remote_path="/2024%2F05",
            file_id="DIR",
            parent_id="ROOT",
            name="2024/05",
            is_folder=True,
        )
        cache.set(
            remote_path="/2024%2F05/report.pdf",
            file_id="F1",
            parent_id="DIR",
            name="report.pdf",
            is_folder=False,
        )
        assert cache.resolve_path("F1") == "/2024%2F05/report.pdf"

    def test_resolve_orphan_returns_none(self) -> None:
        # Entry whose parent isn't in the cache.
        cache = make_cache()
        cache.set(
            remote_path="/orphan.pdf",
            file_id="F1",
            parent_id="MISSING_PARENT",
            name="orphan.pdf",
            is_folder=False,
        )
        # MISSING_PARENT isn't in the table and isn't root_id.
        assert cache.resolve_path("F1") is None
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `pytest tests/offline/test_path_cache.py::TestResolvePath -v`
Expected: `test_resolve_path_with_slash_in_raw_name` fails (current `resolve_path` joins raw names with `/` and doesn't encode).

- [ ] **Step 3: Replace `resolve_path()` in `src/cumulus/path_cache.py`**

```python
    def resolve_path(self, file_id: str) -> str | None:
        """Walk the parent chain to reconstruct the full encoded path
        for *file_id*.

        Returns ``None`` if any ancestor is missing from the cache
        (orphan).
        """
        parts: list[str] = []
        current_id: str | None = file_id

        while current_id and current_id != self._root_id:
            entry = self._table.get(current_id)
            if entry is None:
                logger.warning(
                    "Orphaned file: cannot resolve parent chain for %s",
                    file_id,
                )
                return None
            # Encode each segment as we walk so the result is the encoded
            # form even when raw `name` contains an unsafe char.
            parts.append(encode_name(entry.name))
            current_id = entry.parent_id

        if current_id is None:
            # Walked off the tree without reaching root — orphan.
            logger.warning(
                "Orphaned file: parent chain for %s does not reach root",
                file_id,
            )
            return None

        parts.reverse()
        return "/" + "/".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/offline/test_path_cache.py::TestResolvePath -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/path_cache.py tests/offline/test_path_cache.py
git commit -m "fix(path_cache): codec-aware resolve_path encodes per segment"
```

---

## Task 8: Atomic `populate_from_listing`

**Files:**
- Modify: `src/cumulus/path_cache.py`
- Modify: `tests/offline/test_path_cache.py`

Restructure into three phases (index, plan, atomic swap). Wrap the swap in `Database.transaction()`. Switch the in-memory dedup pass from `name.lower()` to `fold_key(name)`.

- [ ] **Step 1: Append populate tests**

```python
from unittest.mock import patch

from cumulus.database.orm import Manager


def make_drive_item(
    file_id: str,
    name: str,
    parents: list[str] | None,
    is_folder: bool = False,
) -> dict:
    """Helper to build a fake `files.list` item."""
    item = {
        "id": file_id,
        "name": name,
        "mimeType": (
            "application/vnd.google-apps.folder" if is_folder else "text/plain"
        ),
    }
    if parents is not None:
        item["parents"] = parents
    return item


class TestPopulateFromListing:
    def test_simple_tree(self) -> None:
        cache = make_cache()
        items = [
            make_drive_item("DIR", "Documents", parents=["ROOT"], is_folder=True),
            make_drive_item("F1", "report.pdf", parents=["DIR"]),
            make_drive_item("F2", "notes.txt", parents=["ROOT"]),
        ]
        cache.populate_from_listing(items)
        assert cache.path_to_id("/Documents") == "DIR"
        assert cache.path_to_id("/Documents/report.pdf") == "F1"
        assert cache.path_to_id("/notes.txt") == "F2"

    def test_encodes_unsafe_names(self) -> None:
        cache = make_cache()
        items = [
            make_drive_item("F1", "2024/05 report.pdf", parents=["ROOT"]),
            make_drive_item("F2", "weird?.txt", parents=["ROOT"]),
        ]
        cache.populate_from_listing(items)
        # Lookup uses encoded path.
        assert cache.path_to_id("/2024%2F05 report.pdf") == "F1"
        assert cache.path_to_id("/weird%3F.txt") == "F2"
        # Raw names preserved in `name` column.
        assert cache.get_entry("F1").name == "2024/05 report.pdf"
        assert cache.get_entry("F2").name == "weird?.txt"

    def test_dedup_by_fold_key_within_parent(self) -> None:
        cache = make_cache()
        items = [
            make_drive_item("F1", "Documents", parents=["ROOT"], is_folder=True),
            make_drive_item("F2", "documents", parents=["ROOT"], is_folder=True),
        ]
        cache.populate_from_listing(items)
        # Lexicographic-first id wins (F1 < F2).
        assert cache.path_to_id("/Documents") == "F1"
        # The loser is not in the cache at all.
        assert cache.get_entry("F2") is None

    def test_orphan_skipped(self) -> None:
        cache = make_cache()
        items = [
            make_drive_item("F1", "ok.txt", parents=["ROOT"]),
            make_drive_item("F2", "orphan.txt", parents=["NOT_IN_TREE"]),
        ]
        cache.populate_from_listing(items)
        assert cache.path_to_id("/ok.txt") == "F1"
        assert cache.get_entry("F2") is None

    def test_clear_then_populate(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/old.pdf",
            file_id="STALE",
            parent_id="ROOT",
            name="old.pdf",
            is_folder=False,
        )
        items = [make_drive_item("F1", "new.pdf", parents=["ROOT"])]
        cache.populate_from_listing(items)
        # Stale row gone, new row present.
        assert cache.get_entry("STALE") is None
        assert cache.path_to_id("/new.pdf") == "F1"


class TestPopulateAtomicity:
    def test_rollback_on_mid_commit_failure(self) -> None:
        cache = make_cache()
        # Pre-populate with one row.
        cache.set(
            remote_path="/before.pdf",
            file_id="BEFORE",
            parent_id="ROOT",
            name="before.pdf",
            is_folder=False,
        )

        items = [
            make_drive_item("DIR", "Documents", parents=["ROOT"], is_folder=True),
            make_drive_item("F1", "report.pdf", parents=["DIR"]),
            make_drive_item("F2", "notes.txt", parents=["ROOT"]),
        ]

        # Patch Manager.update to raise on the 2nd call.
        original_update = Manager.update
        call_count = {"n": 0}

        def flaky_update(self, obj):  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated mid-commit failure")
            return original_update(self, obj)

        with patch.object(Manager, "update", flaky_update):
            with pytest.raises(RuntimeError, match="simulated"):
                cache.populate_from_listing(items)

        # The pre-existing row must still be queryable: rollback worked.
        assert cache.get_entry("BEFORE") is not None
        assert cache.path_to_id("/before.pdf") == "BEFORE"
        # None of the new items took effect.
        assert cache.get_entry("DIR") is None
        assert cache.get_entry("F1") is None
        assert cache.get_entry("F2") is None

    def test_planning_exception_does_not_touch_db(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/before.pdf",
            file_id="BEFORE",
            parent_id="ROOT",
            name="before.pdf",
            is_folder=False,
        )
        # An item with no `id` triggers a KeyError during planning.
        items = [{"name": "broken", "parents": ["ROOT"], "mimeType": "text/plain"}]
        with pytest.raises(KeyError):
            cache.populate_from_listing(items)
        # Pre-existing row still there.
        assert cache.get_entry("BEFORE") is not None

    def test_populate_requires_root_id(self) -> None:
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        cache = PathIDCache(db)  # root_id NOT set
        with pytest.raises(RuntimeError, match="root_id must be set"):
            cache.populate_from_listing([])
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `pytest tests/offline/test_path_cache.py::TestPopulateFromListing tests/offline/test_path_cache.py::TestPopulateAtomicity -v`
Expected: at least the dedup/atomicity tests fail (current implementation uses `name.lower()` and isn't atomic).

- [ ] **Step 3: Add the `fold_key` import to `src/cumulus/path_cache.py`**

Update the import line:

```python
from .path_codec import compose_path, decode_name, encode_name, fold_key, fold_path
```

- [ ] **Step 4: Replace `populate_from_listing()` in `src/cumulus/path_cache.py`**

```python
    def populate_from_listing(self, items: list[dict[str, Any]]) -> None:
        """Build the entire cache from a flat list of Google Drive file resources.

        Each dict should contain at least::

            {"id": str, "name": str, "parents": [str, ...], "mimeType": str}

        Duplicate names within the same parent (by fold key) are detected:
        the first item by file-ID lexicographic order wins; later
        duplicates are skipped with a warning.

        Atomic: either all non-skipped items are present after this
        returns, or the cache is restored to its prior state.
        """
        if self._root_id is None:
            raise RuntimeError(
                "root_id must be set before calling populate_from_listing"
            )

        # Phase 1: index by id and by parent.
        by_id: dict[str, dict[str, Any]] = {}
        children_of: dict[str, list[dict[str, Any]]] = {}

        for item in items:
            fid = item["id"]
            by_id[fid] = item

            parents = item.get("parents", [])
            parent_id = parents[0] if parents else None

            if parent_id is not None:
                children_of.setdefault(parent_id, []).append(item)

        # Phase 2: plan. Detect duplicates by fold key (NFC + casefold);
        # then BFS from root building encoded paths.
        skip_ids: set[str] = set()
        seen: dict[tuple[str | None, str], str] = {}

        for parent_id, kids in children_of.items():
            kids.sort(key=lambda x: x["id"])
            for kid in kids:
                key = (parent_id, fold_key(kid["name"]))
                if key in seen:
                    skip_ids.add(kid["id"])
                    logger.warning(
                        "Skipping duplicate name '%s' in folder %s "
                        "(keeping %s, skipping %s)",
                        kid["name"],
                        parent_id,
                        seen[key],
                        kid["id"],
                    )
                else:
                    seen[key] = kid["id"]

        # BFS plan; build PathCacheEntry instances without writing yet.
        plan: list[PathCacheEntry] = []
        queue: list[tuple[str, str]] = [(self._root_id, "/")]
        visited: set[str] = {self._root_id}

        while queue:
            folder_id, parent_path = queue.pop(0)
            for child in children_of.get(folder_id, []):
                cid = child["id"]
                if cid in skip_ids or cid in visited:
                    continue
                visited.add(cid)

                raw_name = child["name"]
                encoded_path = compose_path(parent_path, raw_name)
                is_folder = (
                    child.get("mimeType") == GOOGLE_FOLDER_MIME_TYPE
                )

                plan.append(
                    PathCacheEntry(
                        file_id=cid,
                        remote_path=encoded_path,
                        remote_path_fold=fold_path(encoded_path),
                        parent_id=folder_id,
                        name=raw_name,
                        is_folder=int(is_folder),
                    )
                )

                if is_folder:
                    queue.append((cid, encoded_path))

        # Log orphans (items in the listing whose parent isn't reachable
        # from root). Behavior: silently dropped from the plan, observability
        # via warning log.
        for item_id, item in by_id.items():
            if (
                item_id in visited
                or item_id in skip_ids
                or item_id == self._root_id
            ):
                continue
            parents = item.get("parents", [])
            parent_repr = parents[0] if parents else None
            logger.warning(
                "Orphaned file: %s (id=%s) has parent %s which is not "
                "reachable from sync root; skipping",
                item.get("name"),
                item_id,
                parent_repr,
            )

        # Phase 3: atomic swap. clear() + bulk insert under one transaction.
        with self._db.transaction():
            self._table.clear()
            for entry in plan:
                self._table.update(entry)
```

- [ ] **Step 5: Run populate tests to verify they pass**

Run: `pytest tests/offline/test_path_cache.py::TestPopulateFromListing tests/offline/test_path_cache.py::TestPopulateAtomicity -v`
Expected: all green.

- [ ] **Step 6: Run full path_cache test file**

Run: `pytest tests/offline/test_path_cache.py -v`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/cumulus/path_cache.py tests/offline/test_path_cache.py
git commit -m "feat(path_cache): atomic populate_from_listing with codec-aware planning"
```

---

## Task 9: Invariant helper + final regression sweep

**Files:**
- Modify: `tests/offline/test_path_cache.py`

Wraps up Stage 1 with a per-row invariant helper that the test suite exercises after various mutations, plus a full-suite green run.

- [ ] **Step 1: Add `_assert_invariants` helper and an integration test that uses it**

Append to `tests/offline/test_path_cache.py`:

```python
from cumulus.path_codec import compose_path as _compose_path  # noqa: E402
from cumulus.path_codec import fold_path as _fold_path  # noqa: E402


def _assert_invariants(cache: PathIDCache) -> None:
    """Walk every row and assert per-row invariants from spec §5.2.

    1. remote_path == compose_path(parent.remote_path, self.name)
       (with parent's remote_path == "/" when parent_id == root_id)
    2. remote_path_fold == fold_path(remote_path)
    """
    rows = cache._table.select_sql("")  # all rows
    by_id = {r.file_id: r for r in rows}
    for row in rows:
        # Invariant 2.
        assert row.remote_path_fold == _fold_path(row.remote_path), (
            f"row {row.file_id!r}: remote_path_fold {row.remote_path_fold!r} "
            f"!= fold_path({row.remote_path!r})"
        )
        # Invariant 1.
        if row.parent_id == cache.root_id:
            parent_path = "/"
        elif row.parent_id in by_id:
            parent_path = by_id[row.parent_id].remote_path
        else:
            # Orphan — invariant 1 doesn't apply (parent missing).
            continue
        expected = _compose_path(parent_path, row.name)
        assert row.remote_path == expected, (
            f"row {row.file_id!r}: remote_path {row.remote_path!r} != "
            f"compose_path({parent_path!r}, {row.name!r}) == {expected!r}"
        )


class TestInvariantsAfterMutations:
    def test_invariants_after_set_move_remove_populate(self) -> None:
        cache = make_cache()
        # Populate.
        items = [
            make_drive_item("DIR", "Documents", parents=["ROOT"], is_folder=True),
            make_drive_item("F1", "report.pdf", parents=["DIR"]),
            make_drive_item("F2", "weird/name.txt", parents=["ROOT"]),
        ]
        cache.populate_from_listing(items)
        _assert_invariants(cache)

        # Set a new row.
        cache.set(
            remote_path="/Documents/added.txt",
            file_id="F3",
            parent_id="DIR",
            name="added.txt",
            is_folder=False,
        )
        _assert_invariants(cache)

        # Move the folder.
        cache.move("/Documents", "/Archive")
        _assert_invariants(cache)

        # Remove a leaf.
        cache.remove("/Archive/added.txt")
        _assert_invariants(cache)

        # Re-populate (clears prior state).
        items2 = [
            make_drive_item("X1", "fresh.pdf", parents=["ROOT"]),
        ]
        cache.populate_from_listing(items2)
        _assert_invariants(cache)
```

- [ ] **Step 2: Run the new test**

Run: `pytest tests/offline/test_path_cache.py::TestInvariantsAfterMutations -v`
Expected: green.

- [ ] **Step 3: Run the full offline suite**

Run: `pytest tests/offline/ -v`
Expected: all green. No regressions in any file outside this stage's scope.

- [ ] **Step 4: Final commit**

```bash
git add tests/offline/test_path_cache.py
git commit -m "test(path_cache): per-row invariant helper exercised across mutations"
```

---

## Done criteria (from spec §9)

After Task 9 the following should all be true:

1. `src/cumulus/path_codec.py` exists with the §4.1 public surface.
2. `src/cumulus/path_cache.py` updated per spec §5 — schema rename, encoding throughout, atomic populate, `move()` `name`-derivation bug fixed.
3. `tests/offline/test_path_codec.py`, `tests/offline/test_database.py`, and `tests/offline/test_path_cache.py` exist and pass; cover spec §7.1 and §7.2.
4. Existing tests on `google-drive-port` continue to pass (no regressions).
5. `pytest tests/offline/` is fully green.
6. No new Drive-client dependencies introduced — codec is stdlib-only; cache only depends on existing ORM and `path_codec`.

---

## Quick reference: what each task hands off

| Task | Public surface added/changed |
|---|---|
| 1 | `path_codec.encode_name`, `path_codec.decode_name` |
| 2 | `path_codec.fold_key` |
| 3 | `path_codec.compose_path`, `path_codec.fold_path` |
| 4 | `Database.transaction()` |
| 5 | `PathCacheEntry.remote_path_fold` (column rename); `set`, `has_case_conflict`, `path_to_id`, `_select_children`, `remove`, `get_entry` (codec-aware) |
| 6 | `PathIDCache.move` (codec-aware + name-derivation bug fix) |
| 7 | `PathIDCache.resolve_path` (codec-aware) |
| 8 | `PathIDCache.populate_from_listing` (atomic, codec-aware planning) |
| 9 | Test invariant helper exercised across mutations; final regression sweep |
