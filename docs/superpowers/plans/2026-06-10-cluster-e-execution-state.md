# Cluster E — Execution State (pause/resume checkpoint)

**Plan:** `docs/superpowers/plans/2026-06-10-cluster-e-review-remediation.md` (15 tasks)
**Workflow:** superpowers:subagent-driven-development — fresh implementer subagent per task, then spec-compliance review, then code-quality review; fix → re-review loops until approved.
**Branch:** `google-drive-port`. All work committed; tree clean at every checkpoint.
**Checkpoint written:** 2026-06-10, after Task 3 implementation.

## Task status

| Task | Finding | Status | Commits | Review state |
|---|---|---|---|---|
| 1. Scoped recursive listing | F1 | ✅ COMPLETE | `a601b73`, `5c21354` | Spec ✅; quality found 1 Important (fold-conflict pass-through) → fixed in `5c21354`; re-review ✅ |
| 2. Feed skips cache-rejected entries | F2 | ✅ COMPLETE | `25e8c43` | Combined spec+quality ✅ "Ready: Yes". 2 Minor polish notes (see carry-overs) |
| 3. Remote moves by remote_id | F3 | ✅ COMPLETE | `a4edfb0`, `c4de428` | Spec ✅; quality found 1 Critical (occupied-target re-key → silent overwrite + resurrection) + 1 Important (remote_id unindexed) + 2 small minors → fixed in `c4de428`; re-review ✅ "Ready: Yes" |
| 4. Cross-page fixpoint | F6 | ⬜ pending | — | depends on Task 2 (done) |
| 5. TOCTOU revision audit | F4 | ⬜ pending | — | |
| 6. Idempotent creates (generateIds) | F7 | ⬜ pending | — | must precede Task 7 |
| 7. make_dir conflict probe | F11 | ⬜ pending | — | |
| 8. CursorResetError + auto-reindex | F5 | ⬜ pending | — | |
| 9. Dead except clauses + re-download | F9 | ⬜ pending | — | |
| 10. mignore/excluded decoded matching | F8 | ⬜ pending | — | |
| 11. PathIDCache internal lock | F10 | ⬜ pending | — | |
| 12. Longpoll gate nextPageToken | F12 | ⬜ pending | — | |
| 13. Auth edges | F13 | ⬜ pending | — | |
| 14. Quota model correction | F14 | ⬜ pending | — | |
| 15. Final gate | — | ⬜ pending | — | full offline suite + lint + deviations file + whole-branch review |

Session task list (TaskList tool) mirrors this table: #1-#2 completed, #3 in_progress, #4-#15 pending.

## Resume procedure

1. `git branch --show-current` must be `google-drive-port`; `git status` clean; HEAD should be `c4de428` (or later if resumed mid-task).
2. **Next action: Task 4** (cross-page fixpoint, plan line 437), then continue Tasks 5→15 in order, one implementer at a time, two-stage review each.
3. Test discipline: targeted modules per task (full offline suite = ~18 min, run only at Task 15). NEVER run tests/linked (needs real account). ⚠️ Never run tests/offline while a real smoke-test account is linked (keyring collision). Targeted suite count after Task 3: 48 passed (test_sync.py + test_changes.py).

## Task 3 — review resolution (2026-06-11)

Hardenings 1-2 (ensure_parent before move; abort re-key on failed move) confirmed correct. Flagged edge 3 adjudicated FIX NOW: the occupied-target branch re-keyed the index, silently adopting the occupant into the moved file's identity (equal revs → no conflicted copy → next scan overwrites remote + resurrects old path; realistic via offline-reconnect). Fixed in `c4de428`: occupied target now early-returns with disk+index untouched, deferring to downstream conflict logic (empirically verified: conflicted copy of occupant + download at new path). Rule is now consistent: re-key iff old path never had the item OR disk move succeeded. Also in `c4de428`: `index=True` on models.remote_id (auto-migrates via CREATE INDEX IF NOT EXISTS on startup), startswith guard in child re-key loop (parity with `_apply_case_change`), fallback logs raised to info. Conflict-interaction analysis (edge 4) spot-checked by reviewer: rev unchanged on Drive rename → equal revs → Skipped, zero re-download on pure moves.

## Carry-over minor notes (fold into Task 15 deviations file)

- Task 2 review: duplicate warnings on fold-conflict (changes.py warning + path_cache.set/move's own warning) — consider demoting the cache-level one to debug. Warning text "rename one of the duplicates… to sync both" is optimistic: renaming the WINNER won't resurrect the loser until the loser itself changes (cursor already consumed).
- Task 1 review: non-recursive list_folder branch still yields fold-conflicted entries (pre-existing, doesn't claim write-through; left as-is deliberately).
- Task 3 review (deferred minors): `_seed_entry` near-duplicates `_seed_index_entry` in tests/offline/test_sync.py — consolidate when next touching. `_apply_remote_move` uses `osp.exists` not `lexists` (broken-symlink edge, speculative). `get_index_entry_by_remote_id` returns `rows[0]` nondeterministically if duplicate remote_id rows exist (possible after occupied-target/failed-move fallbacks) — docstring note or ORDER BY. Pre-existing ORM row-cache staleness: cache mutated before transaction commit → phantom cached row on rollback (same pattern in `_apply_case_change`).
- Task 3 occupied-target residuals (accepted): stale old-path copy + its index row persist until next local scan; duplicate remote_id rows possible transiently. Optional follow-up: conflicted-copy the occupant inside `_apply_remote_move`.

## Context for a fresh session

- This plan remediates 14 verified findings from the 2026-06-10 port review (7 finder angles + per-finding adversarial verification). Memory file `project_cumulus_port.md` has the full finding list (F1-F14).
- Offline suite baseline before this cluster: 479 passed; after Task 1: 481; counts grow with each task's tests.
- Subagent prompts used: superpowers/skills/subagent-driven-development/{implementer,spec-reviewer,code-quality-reviewer}-prompt.md + requesting-code-review/code-reviewer.md.
- Implementer model policy: sonnet for mechanical tasks (1-2 files, full spec in plan), default/most-capable for judgment tasks (3, 5) and all reviews.
