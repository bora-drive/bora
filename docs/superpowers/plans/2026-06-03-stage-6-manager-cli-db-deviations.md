# Stage 6 — Plan deviations, D1/D4 record, and smoke-test record

**Plan:** docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db.md
**Spec:** docs/superpowers/specs/2026-06-03-stage-6-manager-cli-db-design.md

## Implementation summary

Stage 6 (Manager + CLI + DB schema) is complete and full-daemon smoke-tested.
Most of the nominal stage (CLI/manager/models/RPC port) was absorbed
incrementally during Stages 2–5; this stage closed the four genuinely-remaining
items via subagent-driven-development.

Commit map (on `google-drive-port`):
- `d75de2b` Task 1 — remove dead path-root (team-namespace) code from
  `manager.py` (3 methods + section header + 2 callsites + `except
  PathRootError` branch + import) and the two dead `path_root_*` config defaults
- `0689bdc` Task 1 follow-up — drop the residual team-namespace docstring phrase
  and the orphaned unused `PathRootError` import in `errorhandling.py`
- `f02f207` Task 2 — document the reset-on-version-bump DB policy in
  `main.py::_update_from_pre_v1_6_0` (D2: no migration framework; the index is a
  derived cache, re-derived from Drive)
- `7ef4570` Task 3 — this deviations doc with the D1 RPC audit table + D4 break

Offline suite: **418 passed** (unchanged from the Stage 5 baseline), same
pre-existing `test_event_handler.py` fixture errors; `test_manager.py` stays in
the `--ignore` set (its Dropbox-era `test_migrate_path_root_*` tests exercise
the deleted method — Stage 7 owns the rewrite). Run with
`pytest tests/offline --ignore=tests/offline/test_manager.py -q`.

> **Update (Stage 7a, 2026-06-04):** the `--ignore` is gone. Stage 7a deleted
> the dead `test_migrate_path_root_*` tests, added real Drive manager tests, and
> fixed the `test_event_handler.py` fixture errors. The offline suite now runs
> fully green with `pytest tests/offline -q` (no `--ignore`). See
> `docs/superpowers/plans/2026-06-04-stage-7a-test-infrastructure-deviations.md`.

## D1 — Pyro5 RPC field-rename audit

The daemon exposes `Maestral` via `expose(Maestral)` (`daemon.py:466`). Every
field crossing the Pyro5 boundary was checked against the renamed Drive shape:

| Check | Expected | Verdict |
|---|---|---|
| `dbx_path` parameters/returns on RPC methods | none (use `remote_path`) | PASS — no matches in main.py/daemon.py |
| `client_modified` / `server_modified` | none (single `modified`) | PASS — no matches |
| `rev` semantics | `headRevisionId` (set in Stage 5) | PASS |

Conclusion: the RPC surface is Drive-shaped; no shim or version-bump needed.
The one pre-existing back-compat alias (`exceptions.py:55-70`,
`CumulusApiError.dbx_path`/`dbx_path_from` -> `remote_path`/`remote_path_from`)
is left as-is — it already exists and costs nothing; Stage 6 does not extend
the pattern.

## D4 — GUI compatibility: documented break

The out-of-scope GUI packages (`maestral-qt`, `maestral-cocoa`) import
`SyncEvent` / `FileMetadata` expecting a `dbx_path` attribute that no longer
exists. **Decision: intentional break.** No model-level compat shims are added
(shims for packages we neither port nor test are dead weight). The GUI port is
deferred indefinitely (master design §3). To be noted in the CHANGELOG at
Stage 8.

## Smoke-test record

**PASS — 4/4, 2026-06-03, account `cumulus@arko.net`, config `smoke-test`.**

This was the **first full-daemon end-to-end run** — Stages 0–5 only ever
hand-drove `SyncEngine` cycles in-process. The test launched the real daemon
(`cumulus start -c smoke-test -f -v`, Pyro5 server), exercised the CLI→daemon
RPC path, verified autonomous sync in both directions driven by the manager's
own worker threads (no hand-driven cycles), and confirmed clean shutdown.
A fresh empty local sync dir was used; initial sync mirrored the account's
existing ~8 items (correctly skipping orphaned/shared-with-me items via the
sync-root filter). The two files created during the run were trashed on Drive
in cleanup and the local dir removed.

| # | Check (the new thread/RPC/lifecycle layer) | Result |
|---|---|---|
| 1 | daemon + Pyro5 server come up against the linked account; initial sync completes ("Up to date") | PASS |
| 2 | CLI→daemon RPC roundtrip — `cumulus status` connects over Pyro5 and returns sane sync state ("Up to date", 0 errors) | PASS (see finding F1) |
| 3a | autonomous upload — local file picked up by the upload thread (log `Syncing ↑ 1/1`), confirmed on Drive with matching md5 (~40s incl. local-change poll) | PASS |
| 3b | autonomous download — file created out-of-band on Drive pulled locally by the download poll loop (~28s, incl. change-feed propagation latency) | PASS |
| 4 | clean shutdown (`cumulus stop`) — threads join ("Shutting down threads → Sync aborted → Paused → Shutting down"), daemon process exits 0, `status` reports "not running", no orphaned process, no leaked-session warnings | PASS |

### Smoke-test findings

**F1 (minor, pre-existing, deferred) — `cumulus status` shows `Account --` /
`Usage --`.** The status command reads `account.email` / `account.type` /
`account.usage` from the state file (`cli_info.py:31-33`), but
`Maestral.get_account_info()` / `get_space_usage()` never persist those fields
to state (`grep set_state` for account/email/usage returns nothing — the client
only caches `_cached_space_usage` in memory). So the status display is blank.

This is **not a Stage 6 regression** (it predates the path-root removal and is
unrelated to it) and **not a daemon/thread/RPC defect** — the Pyro5 roundtrip
itself works and returns correct sync state. It is a display-only
account-info-persistence gap whose fix touches Drive account-shape/display
semantics (e.g. what a Drive "account type" string should be), which is
account-ops / UX polish rather than the thread/lifecycle layer this gate
targets. **Deferred** to a later account-ops/UX pass (Stage 8 docs+release, or a
Stage 3 account-ops follow-up). No sync correctness, lifecycle, or RPC impact.

No daemon/thread/lifecycle bugs surfaced — contrary to the Stage 5 carry-over
prediction that startup-sequencing or shutdown bugs would first appear here, the
manager thread orchestration, startup sequencing, RPC marshalling, and clean
shutdown all worked on the first full-daemon run.
