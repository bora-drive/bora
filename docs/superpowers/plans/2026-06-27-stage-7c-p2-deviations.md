# Stage 7c — P2 Accepted-Residuals Pass: Deviations / Evidence

**Date:** 2026-06-27
**Branch:** `google-drive-port`
**Spec:** `docs/superpowers/specs/2026-06-15-stage-7c-deferred-backlog.md` §3 (P2)
**Workflow:** controller-driven TDD (RED→GREEN→verify), per the established
cluster-E / 7b / 7c-P0 cadence. Fixes were small and self-contained, so they
were implemented directly rather than via subagents; the controller ran the
slow offline gate itself (per the standing "controller runs slow tests" note).

## Scope

The P2 block enumerated 7c-7 … 7c-13. This pass closed the three *actionable*
opportunistic items (7c-7, 7c-8, 7c-9); the rest were already deferred or
intentionally-not-done and required no work:

| Item | Disposition |
| --- | --- |
| **7c-7** | **FIXED** — `get_metadata` now emits the cached cased path. |
| **7c-8** | **FIXED** — `_download_revision_to_tempfile` cleans up on failure. |
| **7c-9** | **TRIAGED → won't-do (accepted)** — no schema bump in flight. |
| 7c-10 | Subsumed by 7c-1/7c-2 (closed in P0). No work. |
| 7c-11 | Deferred by design (needs its own RED-before-GREEN TDD cycle). |
| 7c-12 | Benign reset-race; fix only on a `_handle_sync_thread_errors` refactor. |
| 7c-13 | Investigated in P0 → intentionally NOT added (YAGNI). |

---

## 7c-7 — `get_metadata` fabricated a casefolded `path_display`

**Defect:** `get_metadata(remote_path)` echoed its *input* path into
`path_display` via `to_folder_metadata(file, remote_path)` /
`to_file_metadata(file, remote_path)`. Because `remote_path` is only a
case-*insensitive* lookup key (`path_to_id` folds it), a newly-included single
file / empty folder would materialize locally under the casefolded name
(`strasse`, not `Straße`) — while `name` came from the correctly-cased Drive
`name` field, producing an internally inconsistent `Metadata`.

**Fix (`src/cumulus/client.py`, `get_metadata`):** after resolving `file_id`,
recover the true cased path from the cache —
`cased_path = self._path_cache.id_to_path(file_id) or remote_path` — and pass
`cased_path` to both metadata projectors. `id_to_path` is guaranteed populated
here (the preceding `path_to_id` already matched a row holding this `file_id`);
the `or remote_path` fallback is a belt-and-braces guard that also keeps mypy
happy (`id_to_path` returns `str | None`), so the existing baseline `str | None`
mypy finding at the *other* (`list_folder`) callsite is unchanged.

**Tests (RED→GREEN, `tests/offline/test_client.py`):**
- `test_get_metadata_file_uses_cached_cased_path_not_fold` — seed `/Straße.txt`,
  look up via the casefolded `/strasse.txt`, assert `path_display == "/Straße.txt"`
  and `path_lower == fold_path("/Straße.txt")`.
- `test_get_metadata_folder_uses_cached_cased_path_not_fold` — folder branch.
- Both verified RED (returned `/strasse…`) before the fix, GREEN after.

---

## 7c-8 — `_download_revision_to_tempfile` leaked its temp file on failure

**Defect:** the helper `mkstemp`s a `cumulus-restore-*` temp file, then downloads
into it. Callers (`_rescue_revision`, `restore`) wrap only the *returned* path in
`try/finally`. If the download raised **before returning** (API error,
retry-exhaustion, `CancelledError` on shutdown), the caller never received the
path and the file was orphaned in `$TMPDIR`.

**Fix (`src/cumulus/writes.py`, `_download_revision_to_tempfile`):** wrap the
download body in `try / except BaseException: <unlink>; raise`. `BaseException`
(not `Exception`) so a `KeyboardInterrupt`/`SystemExit` mid-download also cleans
up before propagating; the bare `raise` preserves the original exception and
traceback. The `os.unlink` is itself guarded with `except OSError: pass`.

**Test (RED→GREEN, `tests/offline/test_writes.py`):**
- `test_download_revision_to_tempfile_cleans_up_on_download_failure` — monkeypatch
  `client._retry_transient` to raise mid-chunk, glob `$TMPDIR/cumulus-restore-*`
  before/after, assert the set is unchanged. Verified RED (one leaked file)
  before the fix, GREEN after. (`convert_api_errors` passes a plain
  `RuntimeError` through unchanged, so the test asserts on `RuntimeError`.)

---

## 7c-9 — `remote_path_lower` column name holds a *fold* value

**Triage decision: accept as-is (won't-rename).** The P2 pass touched only
`client.py` and `writes.py`; `models.py` was **not** modified, so the spec's
"rename only if a model-schema bump is being made for another reason"
precondition is not met. A standalone rename would touch every read/write site
of `IndexEntry.remote_path_lower` / `SyncEvent.remote_path_lower` for pure
cosmetics with zero behavioral benefit (the column already holds the correct
fold value, blessed by Stage 6 §1). Folded into Stage 8's naming sweep *iff* a
schema change lands there for another reason. Recorded in the spec §3 entry.

---

## Gate

- **Offline suite (`-m "not load"`, `--benchmark-disable`): PASS** —
  `648 passed, 2 deselected in 314.12s` (controller-run). The +3 over the
  prior non-load count are exactly the three new 7c-7/7c-8 tests; no regressions.
- **mypy** (`src/cumulus/client.py src/cumulus/writes.py`): **baseline parity** —
  14 errors with and without the change (all pre-existing: lambda-inference,
  the `list_folder` `str | None → str` projector arg, the `SharedLinkMetadata`
  type-ignore). No new errors introduced.
- **flake8**: **baseline parity** — src violation count unchanged (65); the two
  new test files have no new violations on the added lines.

## Files changed

| File | Change |
| --- | --- |
| `src/cumulus/client.py` | 7c-7: cased `path_display` in `get_metadata`. |
| `src/cumulus/writes.py` | 7c-8: temp-file cleanup-on-failure. |
| `tests/offline/test_client.py` | 2 new 7c-7 tests. |
| `tests/offline/test_writes.py` | 1 new 7c-8 test. |
| `docs/superpowers/specs/2026-06-15-stage-7c-deferred-backlog.md` | 7c-9 triage resolution. |

## Remaining 7c backlog after this pass

P2 deferred-by-design: **7c-11** (occupied-target *file* move on-disk orphan
re-upload — own TDD cycle), **7c-12** (breaker reset-race — benign),
**7c-13** (upload-path eviction — YAGNI). Next stage: **Stage 8** (docs +
release, naming sweep, `DropboxClient` alias removal).
