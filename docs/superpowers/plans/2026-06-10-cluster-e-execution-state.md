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
| 3. Remote moves by remote_id | F3 | 🟡 IMPLEMENTED, **REVIEWS PENDING** | `a4edfb0` | Implementer reported DONE; spec review NOT run; quality review NOT run |
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

1. `git branch --show-current` must be `google-drive-port`; `git status` clean; HEAD should be `a4edfb0` (or later if resumed mid-task).
2. **Next action: run the two pending reviews for Task 3** (commit `a4edfb0`, base `25e8c43`):
   - Spec review per plan Task 3 — verify `_apply_remote_move`, `get_index_entry_by_remote_id`, wiring in `_on_remote_file`/`_on_remote_folder` (not `_on_remote_deleted`), 3 new tests in tests/offline/test_sync.py, cluster-B case-rename tests still green. Targeted test cmd: `python -m pytest tests/offline/test_sync.py tests/offline/test_changes.py -q` (47 passed at implementation time).
   - Quality review (template: superpowers requesting-code-review/code-reviewer.md). **Must adjudicate the items below.**
3. Then continue Tasks 4→15 in order, one implementer at a time, two-stage review each.
4. Test discipline: targeted modules per task (full offline suite = ~18 min, run only at Task 15). NEVER run tests/linked (needs real account). ⚠️ Never run tests/offline while a real smoke-test account is linked (keyring collision).

## Task 3 — open items for its pending reviews

Implementer deviations/hardenings beyond the plan sketch (deliberate, must be reviewed):
1. Calls `_ensure_parent(event)` before the local move (reparent may arrive before its new parent folder exists locally; `utils/path.move()` silently no-ops on missing destination parent).
2. Aborts the index re-key if the disk move failed or silently no-oped (`exc is not None or not osp.exists(event.local_path)`) — index stays consistent with disk; falls back to pre-fix behavior.
3. **Flagged edge (reviewer must adjudicate):** if something else already occupies the new local path, the disk move is skipped but the index IS still re-keyed → old local file becomes untracked, local scan would re-upload it at the old path. Possible follow-up: conflicted-copy the occupant first, or also abort the re-key in that branch.
4. Implementer's conflict-interaction analysis (move + local edit → edit travels with rename; move + remote content change → conflicted copy at NEW path) — spot-check the reasoning.

## Carry-over minor notes (fold into Task 15 deviations file)

- Task 2 review: duplicate warnings on fold-conflict (changes.py warning + path_cache.set/move's own warning) — consider demoting the cache-level one to debug. Warning text "rename one of the duplicates… to sync both" is optimistic: renaming the WINNER won't resurrect the loser until the loser itself changes (cursor already consumed).
- Task 1 review: non-recursive list_folder branch still yields fold-conflicted entries (pre-existing, doesn't claim write-through; left as-is deliberately).

## Context for a fresh session

- This plan remediates 14 verified findings from the 2026-06-10 port review (7 finder angles + per-finding adversarial verification). Memory file `project_cumulus_port.md` has the full finding list (F1-F14).
- Offline suite baseline before this cluster: 479 passed; after Task 1: 481; counts grow with each task's tests.
- Subagent prompts used: superpowers/skills/subagent-driven-development/{implementer,spec-reviewer,code-quality-reviewer}-prompt.md + requesting-code-review/code-reviewer.md.
- Implementer model policy: sonnet for mechanical tasks (1-2 files, full spec in plan), default/most-capable for judgment tasks (3, 5) and all reviews.
