# Shared with me — design

**Status:** Approved (brainstorm complete, ready for planning)
**Date:** 2026-07-05
**Package:** `bora` (Google Drive port of Maestral)

## Goal

Sync Google Drive "Shared with me" content into the local sync folder under a
top-level `/Shared with me` directory, and turn My Drive *shortcuts* into local
symlinks that point at wherever their target lives locally.

Two user-facing behaviors:

1. A `/Shared with me` directory at the top level holds shared-with-me files and
   folders, materialized as real local content.
2. Drive shortcuts in My Drive (`application/vnd.google-apps.shortcut`) become
   local symlinks pointing at the local path of their target — whether that
   target is under `/Shared with me` or elsewhere in My Drive.

## Decisions (locked during brainstorm)

- **Scope.** Sync *all* shared-with-me content by default. Pruning uses the
  existing `excluded` / selective-sync machinery — no new config surface.
- **Write direction.** Respect per-item capabilities. Request
  `capabilities.canEdit`; editable shared items sync back through the normal
  upload path, view-only items are made read-only on disk and never uploaded
  (no perpetual 403 retry storm).
- **Shortcuts.** Every My Drive shortcut becomes a local symlink to its target's
  local path. If the target is not present locally (excluded / inaccessible /
  not yet resolved), skip the symlink and record a sync error (retried).
- **Windows.** Real symlinks on POSIX. When `os.symlink()` fails (Windows
  without Developer Mode/admin), fall back to a native `.lnk` shortcut file.
- **Architecture.** Virtual root node inside the existing `PathIDCache`
  (approach A) — maximum reuse of path resolution, index, selective-sync, and
  the change-feed fixpoint. A schema migration is acceptable (unreleased).

## Background: why shared items are excluded today

The index and path cache are rooted at `root_id` (My Drive's root, mapped to
`/`). Shared-with-me items have no parent under `root_id`, so they are filtered
out at two choke points:

1. **Listing** — `_bootstrap_recursive` lists `q="trashed=false"` (everything
   visible, including shared/orphans), then `PathIDCache.populate_from_listing`
   (`path_cache.py:336`) BFS-filters to items reachable from `root_id`.
   Shared items drop out.
2. **Change scope** — `classify` (`changes.py:150`) marks a change with no
   in-scope parent as `OUT_OF_SCOPE` and drops it.

Shortcuts are additionally recognized (`native_docs.py`) and dropped everywhere
(`_emit_apply` at `changes.py:226`, bootstrap at `client.py:1097`).

A dead download branch already exists at `sync.py:3724-3730`: if metadata
carries `symlink_target`, it calls `os.symlink()` instead of downloading. It is
unreachable today because Drive metadata never sets `symlink_target`. This is
the hook the shortcut feature reuses.

## Architecture: virtual root

- Introduce a sentinel folder id `SHARED_WITH_ME_ID` and reserved display path
  `/Shared with me` in `path_cache.py`. `PathIDCache` maps this id →
  `/Shared with me`, mirroring how `root_id` → `/`.
- **Attach rule.** An item attaches under the virtual root iff it has *no
  resolvable My-Drive parent* **and** `sharedWithMe == true`. On insert its
  `parent` is rewritten to `SHARED_WITH_ME_ID`. Drive sets `sharedWithMe` only
  on the *top* of each shared subtree (not descendants), so this flag is exactly
  the set of attach points. Descendants resolve normally up their real parent
  chain into the virtual root — no per-descendant flag.
- The `/Shared with me` directory is created locally at sync start (like the
  root dir). It has no remote_id and is never uploaded.

## Components

### 1. Fields (`changes.py:54`, `FILE_FIELDS`)

Add: `sharedWithMe`, `ownedByMe`, `capabilities(canEdit)`,
`shortcutDetails(targetId, targetMimeType)`. The change feed selector
`_CHANGE_FIELDS` embeds `FILE_FIELDS`, so the changes stream carries these too.

### 2. Listing / bootstrap (`client.py`, `path_cache.py`)

- New `list_shared_roots()` → `files().list(q="sharedWithMe=true and
  trashed=false", fields=…)` returns the attach points.
- `populate_from_listing` seeds its BFS from `{root_id} ∪ {shared root ids}`
  instead of `root_id` alone. The existing full `trashed=false` listing already
  returns shared descendants, so once the roots are seeded, descendants populate
  for free.
- **Verification during implementation:** confirm the ambient `trashed=false`
  listing actually includes all shared descendants. If any are missed, fall back
  to per-root child enumeration (`'<rootId>' in parents`, recurse), bounded to
  shared subtrees.

### 3. Change feed (`changes.py`)

- `classify`: in-scope if any parent resolves **or** `sharedWithMe == true`. A
  new `sharedWithMe` item with no resolvable parent becomes a new shared root
  attached to the sentinel.
- **Unshare** arrives as `removed=True, file=None` (validated live, 7c-5) and
  flows through the existing delete path, removing the local subtree. No new
  delete logic.
- **Stop dropping shortcuts.** Where `_emit_apply` (`changes.py:226`) and
  bootstrap currently filter shortcut mime types, emit them as symlink metadata
  (§4) instead.

### 4. Shortcut → symlink (M2)

- Shortcut metadata carries `shortcutDetails.targetId`. At apply time resolve
  `targetId → id_to_path → local target path`, and produce a `FileMetadata`
  with `symlink_target` set to a path **relative to the symlink's own
  directory** (robust to sync-root moves).
- Download reuses the branch at `sync.py:3724-3730`: `os.symlink(target, path)`
  on POSIX; on `OSError` (or on Windows), write a `.lnk` via a small platform
  writer under `utils/`.
- **Ordering / absence.** The change fixpoint already orders parents before
  children; extend the same wait so a shortcut applies only once its target
  resolves. If the target is excluded / inaccessible / never resolves in the
  batch, skip the symlink and record a sync error (retried next cycle;
  materializes automatically if the target later appears).
- **Chains.** Resolve one hop only. If the hop lands on another shortcut, record
  a sync error rather than looping.

### 5. Read-only / capabilities (M1)

- Model `can_edit: bool` on `FileMetadata` / `FolderMetadata` from
  `capabilities.canEdit`.
- **Download.** If `not can_edit`, chmod off the write bits after writing the
  file, wrapped in `fs_events.ignore(...)` so the chmod does not self-trigger an
  upload. Before the engine overwrites/deletes a read-only file for a later
  remote change, it chmod +w first.
- **Upload gating.** The `read_only` index flag is consulted in
  `_create_remote_entry` and its handlers before any mutation; read-only items
  are skipped silently (no error, no retry). Editable shared items sync back
  through the normal path.

### 6. Schema migration (`models.py`)

- Add a `read_only` boolean column to `IndexEntry` (the write-gate source of
  truth, so no remote fetch is needed at local-change time).
- Bump the DB schema version + add a migration step, following the existing
  `symlink_target` column pattern. Additive, default `read_only=False`.

### 7. Selective sync & off-switch

- No new config. `/Shared with me/...` are ordinary paths, so
  `excluded add "/Shared with me/Some Folder"` prunes and deletes locally via
  the existing `_remove_after_excluded`.
- Excluding `/Shared with me` itself disables the whole feature (removes the
  tree). That is the intended "turn it off" mechanism.

### 8. Collisions & edge cases

- **Reserved-name clash.** A real My-Drive folder literally named "Shared with
  me" collides with the virtual root. The virtual root wins the name; the real
  My-Drive folder gets a conflict-suffix rename, consistent with existing
  selective-sync conflict handling. Rare; documented.
- **Shortcut target namespaces.** Shortcut → shared item resolves into
  `/Shared with me/...`; shortcut → My-Drive item resolves into the normal tree.
  One uniform rule.

## Milestones

The feature is one coherent spec, delivered in three sequential milestones. Each
is independently testable.

- **M1 — Shared content materialization.** §1–3, §5–8. Virtual root, listing,
  change-feed classify/apply, capabilities/read-only, schema migration,
  selective sync, collisions. Delivers `/Shared with me/` on its own.
- **M2 — Shortcut symlinks (download-only).** §4 plus the Windows `.lnk`
  fallback. Depends on M1's resolved paths. Symlinks are download-only
  artifacts: the existing upload guard (`sync.py:2560`) is changed to *silently*
  ignore symlinks matching a known shortcut instead of erroring; local edits and
  deletes are not propagated in M2.
- **M3 — Reverse-direction symlink propagation.** Propagate local symlink
  changes back to Drive shortcuts:
  - Deleting a local symlink → delete the corresponding Drive shortcut.
  - Creating/retargeting a local symlink whose target maps to a known remote
    item → create/update the Drive shortcut (`files.create` with
    `mimeType=application/vnd.google-apps.shortcut` and
    `shortcutDetails.targetId`).
  - Symlinks whose target does not map to a known remote item are left local
    (no shortcut created) and surfaced as a sync error/notice.
  - Gate all of this on write access to the containing folder.

## Testing

- **Offline unit tests** (extend the existing ~648-test offline suite):
  - Virtual-root path resolution (`PathIDCache` with sentinel).
  - `classify` with `sharedWithMe` in/out of scope.
  - Shortcut → symlink metadata + relative-target computation (M2).
  - Read-only chmod + upload-gating (M1).
  - `.lnk` writer with mocked platform (M2).
  - Reserved-name collision (M1).
  - Reverse shortcut create/delete (M3).
- **Fixtures** for a `sharedWithMe=true` listing and shortcut resolution.
- **Live smoke test** (smoke tests catch real bugs on this project): from a
  second account, share a file → confirm it lands under `/Shared with me/`; add a
  shortcut → confirm the local symlink; revoke sharing → confirm local removal.
  For M3: delete a local symlink → confirm the Drive shortcut is removed.

## Open items for the plan

- Confirm ambient-listing coverage of shared descendants vs. per-root
  enumeration (§2).
- Exact `.lnk` writer approach on Windows (§4) — pure-Python writer vs.
  dependency.
- Migration mechanics and version bump specifics (§6).
