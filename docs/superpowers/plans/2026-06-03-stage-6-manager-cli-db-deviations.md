# Stage 6 — Plan deviations, D1/D4 record, and smoke-test record

**Plan:** docs/superpowers/plans/2026-06-03-stage-6-manager-cli-db.md
**Spec:** docs/superpowers/specs/2026-06-03-stage-6-manager-cli-db-design.md

## Implementation summary

(Filled in as tasks land — commit hashes, offline-suite count.)

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

(Filled in by the full-daemon smoke-test task.)
