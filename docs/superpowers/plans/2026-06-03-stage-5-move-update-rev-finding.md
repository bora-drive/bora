# Stage 5 finding: `move(update_rev=)` is not load-bearing

**Date:** 2026-06-03
**Context:** Task 5 of the Stage 5 sync-engine port deferred a question about whether
`GoogleDriveClient.move` needs a revision precondition (`update_rev=`).

## What `_on_local_moved` does today

`SyncEngine._on_local_moved` (`src/cumulus/sync.py`) handles a destination collision
*before* the move: it looks up the index entry for `event.remote_path_lower` and, if it
is a file, calls `self.client.remove(local_entry.remote_path_lower,
parent_rev=local_entry.rev)`. That removal is the rev-checked operation — it only
deletes the colliding destination if its rev still matches our index (this mirrors the
Dropbox original's rev-checked removal of a colliding destination). It then performs the
move with a plain `self.client.move(remote_path_from, event.remote_path,
autorename=True)` — no rev or precondition argument. Any residual conflict at the
destination is resolved afterwards by `_handle_upload_conflict`.

## Why a rev-checked move precondition is not needed for Stage 5

The revision guard already lives on the `remove` call (`parent_rev=`), which Stage 4's
client supports. `move` itself never needs a rev precondition for Stage 5's flow: the
destination is cleared (rev-checked) first, and `autorename=True` plus
`_handle_upload_conflict` cover the remaining collision cases. The logic is
self-consistent with Stage 4's `move(remote_path, new_path, *, autorename=False)`
signature, which has no rev parameter.

## Decision

Adding `move(update_rev=)` is **not** required for Stage 5 and is **deferred to Stage 6**.
No parameter was added and no call site passes a rev/precondition to `client.move`.
