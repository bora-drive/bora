# "Shared with me" + Shortcut Symlinks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sync Google Drive "Shared with me" content into a top-level `/Shared with me` directory in the local sync folder, and turn My Drive *shortcuts* into local symlinks pointing at their target's local path.

**Architecture:** A virtual root node inside the existing `PathIDCache` (a reserved sentinel id `SHARED_WITH_ME_ID` mapped to the display path `/Shared with me`, mirroring how `root_id` maps to `/`). Shared-with-me attach points (Drive items with `sharedWithMe == true` and no resolvable My-Drive parent) are re-parented onto that sentinel; descendants resolve up their real parent chain into it for free. Per-item `capabilities.canEdit` gates write-back: view-only items are made read-only on disk and never uploaded. My Drive shortcuts ride the pre-existing (but currently unreachable) `symlink_target` download branch.

**Tech Stack:** Python 3.10+, `google-api-python-client` (Drive v3), a custom ORM over SQLite (`src/bora/database/`), `watchdog` for local FS events, `pytest` (offline suite under `tests/offline/`, `FakeDrive` stub under `tests/fake_drive.py`).

## Global Constraints

- **Package is `bora`** (a rename of Maestral). All source under `src/bora/`, tests under `tests/`. The `--cov=maestral` in CI is legacy naming; ignore it.
- **Python floor 3.10** — use `X | None` unions, `list[...]`, `from __future__ import annotations` is already imported in the files you touch.
- **No DB schema-version framework exists.** Policy (documented `src/bora/main.py:1538-1544`): the index / history / hash_cache tables are a *derived cache*, not a system of record. On any schema change the tables are **dropped and re-derived** from Drive by reindexing — there is deliberately no version stamp and no `ALTER TABLE` migration. **This overrides spec §6:** to add a column you add the `Column(...)` to the model; the drop-and-reindex on next update (or a fresh link) rebuilds the table. The product is *unreleased*, so no migration step is required at all.
- **Every local filesystem mutation the engine performs must be wrapped** in `self.fs_events.ignore(<Event>(path), recursive=...)` so watchdog does not echo it back as a local change. Pattern examples: `sync.py:3726` (symlink create), `sync.py:3866` (mkdir).
- **Three path forms** and their bridge (footgun — get this right):
  - `fold_human` (NFC + casefold, decoded) — the stored form of `excluded_items`.
  - `fold_path` (NFC + casefold, encoded) — the key for the index and sync-error tables.
  - encoded display path (`compose_path` output) — the `PathIDCache` `remote_path`.
  - Bridge: `fold_path(encode_path(fold_human_value))`. See `main.py:1173`.
- **Offline tests** run via `python -m pytest tests/offline` (`--benchmark-disable` is applied automatically by `pyproject.toml:119`). Deselect slow load tests with `-m 'not load'`. Keep **mypy and flake8 at parity** (no new errors) — run `python -m mypy src/bora` and `python -m flake8 src/bora tests` after each task.
- **Dataclass default ordering:** `FileMetadata`/`FolderMetadata` (`core.py`) currently have *no* defaulted fields. New fields you add with a default (`= True`) MUST be appended after all existing fields.
- **Milestones are sequential.** M1 delivers `/Shared with me/` on its own. M2 (shortcuts) depends on M1's resolved paths. M3 (reverse propagation) depends on M2. Do not start a milestone before its predecessor is green.

---

# Milestone M1 — Shared-content materialization

Delivers `/Shared with me/` as real, read-only-aware local content. Spec §1–3, §5–8.

---

### Task 1: Request and model the new Drive fields (`sharedWithMe`, `capabilities.canEdit`, `shortcutDetails`)

**Files:**
- Modify: `src/bora/changes.py:54-57` (`FILE_FIELDS`), `changes.py:80-113` (`to_file_metadata`, `to_folder_metadata`)
- Modify: `src/bora/core.py:136-167` (`FileMetadata`, `FolderMetadata`)
- Test: `tests/offline/test_changes.py`, `tests/offline/test_core_models.py`

**Interfaces:**
- Produces: `FileMetadata.can_edit: bool` and `FolderMetadata.can_edit: bool` (default `True`). `FILE_FIELDS` now additionally carries `sharedWithMe, ownedByMe, capabilities(canEdit), shortcutDetails(targetId, targetMimeType)`. `to_file_metadata` / `to_folder_metadata` read `capabilities.canEdit` (default `True` when absent — items you own report no `canEdit=false`).
- Consumed by: Task 6 (classify reads raw `sharedWithMe`), Task 8 (`can_edit` → index), Task 12 (raw `shortcutDetails`).

- [ ] **Step 1: Write the failing test**

Add to `tests/offline/test_changes.py` (near the existing `test_to_file_metadata_*` tests):

```python
def test_to_file_metadata_reads_can_edit_false():
    file = {**SAMPLE_FILE, "capabilities": {"canEdit": False}}
    meta = to_file_metadata(file, "/report.pdf")
    assert meta.can_edit is False


def test_to_file_metadata_can_edit_defaults_true_when_absent():
    meta = to_file_metadata(SAMPLE_FILE, "/report.pdf")
    assert meta.can_edit is True


def test_to_folder_metadata_reads_can_edit():
    file = {**SAMPLE_FOLDER, "capabilities": {"canEdit": False}}
    meta = to_folder_metadata(file, "/Docs")
    assert meta.can_edit is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_changes.py::test_to_file_metadata_reads_can_edit_false -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'can_edit'` is not raised yet; instead `AttributeError: 'FileMetadata' object has no attribute 'can_edit'`.

- [ ] **Step 3: Add the dataclass fields**

In `src/bora/core.py`, append to `FileMetadata` (after `content_hash`, line 157):

```python
    content_hash: str
    """A content hash of the file"""
    can_edit: bool = True
    """Whether the current user may edit this item (Drive capabilities.canEdit).
    Defaults True for owned My-Drive items; False for view-only shared items."""
```

And append to `FolderMetadata` (after `shared`, line 167):

```python
    shared: bool
    """Whether the folder is shared"""
    can_edit: bool = True
    """Whether the current user may edit this folder (Drive capabilities.canEdit)."""
```

- [ ] **Step 4: Extend `FILE_FIELDS` and the projection functions**

In `src/bora/changes.py`, replace `FILE_FIELDS` (lines 54-57):

```python
FILE_FIELDS = (
    "id, name, parents, mimeType, modifiedTime, size, md5Checksum, "
    "shared, headRevisionId, trashed, lastModifyingUser(permissionId), "
    "sharedWithMe, ownedByMe, capabilities(canEdit), "
    "shortcutDetails(targetId, targetMimeType)"
)
```

In `to_file_metadata` (append the `can_edit` kwarg before the closing paren, after `content_hash=...`):

```python
        is_downloadable=True,
        content_hash=file.get("md5Checksum", ""),
        can_edit=bool(file.get("capabilities", {}).get("canEdit", True)),
    )
```

In `to_folder_metadata` (after `shared=...`):

```python
        id=file["id"],
        shared=bool(file.get("shared", False)),
        can_edit=bool(file.get("capabilities", {}).get("canEdit", True)),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_changes.py -k "can_edit or to_file_metadata or to_folder_metadata" -v`
Expected: PASS (all).

- [ ] **Step 6: Run the full changes + models suites for regressions**

Run: `python -m pytest tests/offline/test_changes.py tests/offline/test_core_models.py -q`
Expected: PASS. (Existing `FileMetadata(...)` constructions still work — `can_edit` is defaulted.)

- [ ] **Step 7: Typecheck & lint**

Run: `python -m mypy src/bora && python -m flake8 src/bora tests`
Expected: no new errors.

- [ ] **Step 8: Commit**

```bash
git add src/bora/changes.py src/bora/core.py tests/offline/test_changes.py
git commit -m "feat(shared): request sharedWithMe/canEdit/shortcutDetails; model can_edit"
```

---

### Task 2: Add the virtual-root sentinel to `PathIDCache`

**Files:**
- Modify: `src/bora/path_cache.py` (module constants near line 51; `id_to_path` line 148-156; `path_to_id` line 130-146; `resolve_path` line 300-332)
- Test: `tests/offline/test_path_cache.py`

**Interfaces:**
- Produces: module constants `SHARED_WITH_ME_ID: str = "__bora_shared_with_me__"` and `SHARED_WITH_ME_PATH: str = "/Shared with me"`. `PathIDCache.id_to_path(SHARED_WITH_ME_ID) == "/Shared with me"`; `path_to_id("/Shared with me") == SHARED_WITH_ME_ID`; `resolve_path` treats `SHARED_WITH_ME_ID` as a stop node (its children resolve to `/Shared with me/...`).
- Consumed by: Task 3 (populate seeding), Task 6 (classify/apply), Task 5 (local dir).

Note: the sentinel id is a synthetic string that can never collide with a real Drive file id (Drive ids are URL-safe base64, never containing `_` doubled like this literal). It is deliberately not a table row — like `root_id`, the mapping is code-only.

- [ ] **Step 1: Write the failing test**

Add a new class to `tests/offline/test_path_cache.py`:

```python
from bora.path_cache import SHARED_WITH_ME_ID, SHARED_WITH_ME_PATH


class TestSharedWithMeVirtualRoot:
    def test_id_to_path_maps_sentinel(self) -> None:
        cache = make_cache()
        assert cache.id_to_path(SHARED_WITH_ME_ID) == SHARED_WITH_ME_PATH

    def test_path_to_id_maps_reserved_path(self) -> None:
        cache = make_cache()
        assert cache.path_to_id(SHARED_WITH_ME_PATH) == SHARED_WITH_ME_ID

    def test_child_of_sentinel_resolves(self) -> None:
        cache = make_cache()
        cache.set(
            remote_path="/Shared with me/Report.pdf",
            file_id="S1",
            parent_id=SHARED_WITH_ME_ID,
            name="Report.pdf",
            is_folder=False,
        )
        assert cache.resolve_path("S1") == "/Shared with me/Report.pdf"
        assert cache.id_to_path("S1") == "/Shared with me/Report.pdf"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_path_cache.py::TestSharedWithMeVirtualRoot -v`
Expected: FAIL — `ImportError: cannot import name 'SHARED_WITH_ME_ID'`.

- [ ] **Step 3: Add the constants**

In `src/bora/path_cache.py`, after the logger line (line 53), add:

```python
# Virtual root for "Shared with me" content. Like ``root_id``/"/", this
# id↔path mapping is code-only (no PathCacheEntry row). The id is a
# synthetic sentinel that cannot collide with a real Drive file id.
SHARED_WITH_ME_ID = "__bora_shared_with_me__"
SHARED_WITH_ME_PATH = "/Shared with me"
```

And extend `__all__` (line 51):

```python
__all__ = [
    "PathCacheEntry",
    "PathIDCache",
    "SHARED_WITH_ME_ID",
    "SHARED_WITH_ME_PATH",
]
```

- [ ] **Step 4: Wire the three resolution methods**

In `id_to_path` (line 148-156), add the sentinel branch alongside the root branch:

```python
    def id_to_path(self, file_id: str) -> str | None:
        """Return the cached remote path for *file_id*, or ``None``."""
        with self._lock:
            entry = self._table.get(file_id)
            if entry is not None:
                return entry.remote_path
            if file_id == self._root_id:
                return self.ROOT_PATH
            if file_id == SHARED_WITH_ME_ID:
                return SHARED_WITH_ME_PATH
            return None
```

In `path_to_id` (line 130-146), add the reserved-path branch next to the `ROOT_PATH` branch (after line 137-138):

```python
        with self._lock:
            if remote_path == self.ROOT_PATH:
                return self._root_id
            if remote_path == SHARED_WITH_ME_PATH:
                return SHARED_WITH_ME_ID
```

In `resolve_path` (line 300-332), make the sentinel a loop stop condition. Change the `while` guard (line 311) and the reached-root check (line 324):

```python
            while current_id and current_id not in (
                self._root_id,
                SHARED_WITH_ME_ID,
            ):
                entry = self._table.get(current_id)
                ...
            if current_id is None:
                # Walked off the tree without reaching a known root — orphan.
                logger.warning(
                    "Orphaned file: parent chain for %s does not reach root",
                    file_id,
                )
                return None

            if current_id == SHARED_WITH_ME_ID:
                parts.append(SHARED_WITH_ME_PATH.lstrip("/"))
            parts.reverse()
            return "/" + "/".join(parts)
```

Note: for the root case (`current_id == self._root_id`) the existing `"/" + "/".join(parts)` already yields the correct `/a/b`. For the sentinel case we prepend the reserved segment `Shared with me` so the path becomes `/Shared with me/a/b`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_path_cache.py::TestSharedWithMeVirtualRoot -v`
Expected: PASS.

- [ ] **Step 6: Full path-cache suite + typecheck/lint**

Run: `python -m pytest tests/offline/test_path_cache.py -q && python -m mypy src/bora && python -m flake8 src/bora tests`
Expected: PASS, no new type/lint errors.

- [ ] **Step 7: Commit**

```bash
git add src/bora/path_cache.py tests/offline/test_path_cache.py
git commit -m "feat(shared): add Shared-with-me virtual root sentinel to PathIDCache"
```

---

### Task 3: Seed `populate_from_listing` from shared roots

**Files:**
- Modify: `src/bora/path_cache.py:336-451` (`populate_from_listing`)
- Test: `tests/offline/test_path_cache.py`

**Interfaces:**
- Consumes: `SHARED_WITH_ME_ID` (Task 2); raw listing dicts now carry `sharedWithMe` (Task 1).
- Produces: after `populate_from_listing`, any item with `sharedWithMe == true` is attached under `SHARED_WITH_ME_ID` (its `parent_id` in the cache rewritten to the sentinel), and its descendants resolve into `/Shared with me/...`.

**Attach rule (spec §"virtual root"):** an item attaches under the sentinel iff it has `sharedWithMe == true`. Drive sets `sharedWithMe` only on the *top* of each shared subtree, so this flag is exactly the set of attach points. On insert, such an item's effective parent is `SHARED_WITH_ME_ID` regardless of its real (unreachable) `parents[0]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/offline/test_path_cache.py` (the `make_drive_item` helper at line 434 builds items; extend inline with `sharedWithMe`):

```python
class TestPopulateSharedRoots:
    def test_shared_root_attaches_under_sentinel(self) -> None:
        cache = make_cache()  # root_id="ROOT"
        shared_root = {
            "id": "SR", "name": "TeamFolder",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["SOMEONE_ELSES_ID"], "sharedWithMe": True,
        }
        child = {
            "id": "SC", "name": "notes.txt", "mimeType": "text/plain",
            "parents": ["SR"],
        }
        cache.populate_from_listing([shared_root, child])
        assert cache.id_to_path("SR") == "/Shared with me/TeamFolder"
        assert cache.id_to_path("SC") == "/Shared with me/TeamFolder/notes.txt"

    def test_non_shared_orphan_still_dropped(self) -> None:
        cache = make_cache()
        orphan = {
            "id": "O", "name": "x.txt", "mimeType": "text/plain",
            "parents": ["UNREACHABLE"],  # no sharedWithMe flag
        }
        cache.populate_from_listing([orphan])
        assert cache.id_to_path("O") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_path_cache.py::TestPopulateSharedRoots -v`
Expected: FAIL — `test_shared_root_attaches_under_sentinel` asserts a path but `id_to_path("SR")` is `None` (currently BFS-dropped as an orphan).

- [ ] **Step 3: Rewrite shared-root parents and seed the BFS**

In `populate_from_listing`, in Phase 1 (lines 356-368), detect shared roots and rewrite their parent to the sentinel *before* building `children_of`. Replace the Phase-1 loop:

```python
        # Phase 1: index by id and by parent. Shared-with-me attach points
        # (sharedWithMe == true) are re-parented onto the virtual root; Drive
        # sets that flag only on the top of each shared subtree, so descendants
        # keep their real parent and resolve into /Shared with me for free.
        by_id: dict[str, dict[str, Any]] = {}
        children_of: dict[str, list[dict[str, Any]]] = {}
        shared_root_ids: set[str] = set()

        for item in items:
            fid = item["id"]
            by_id[fid] = item

            if item.get("sharedWithMe"):
                shared_root_ids.add(fid)
                parent_id: str | None = SHARED_WITH_ME_ID
            else:
                parents = item.get("parents", [])
                parent_id = parents[0] if parents else None

            if parent_id is not None:
                children_of.setdefault(parent_id, []).append(item)
```

Seed the BFS from both roots. Replace lines 392-395:

```python
        # BFS plan; build PathCacheEntry instances without writing yet.
        # Seed from the real root AND the virtual shared-with-me root.
        plan: list[PathCacheEntry] = []
        queue: list[tuple[str, str]] = [
            (root_id, "/"),
            (SHARED_WITH_ME_ID, SHARED_WITH_ME_PATH),
        ]
        visited: set[str] = {root_id, SHARED_WITH_ME_ID}
```

The orphan-filter loop (lines 428-434) already skips anything in `visited`; shared descendants are now reachable from the sentinel seed so they are no longer logged/dropped. No change needed there.

Note: the per-child `PathCacheEntry(parent_id=folder_id, ...)` at line 416 stores `folder_id` which, for a shared root's BFS expansion, is `SHARED_WITH_ME_ID` — exactly the parent we want persisted.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_path_cache.py::TestPopulateSharedRoots -v`
Expected: PASS (both).

- [ ] **Step 5: Full path-cache suite (regression on ordinary populate)**

Run: `python -m pytest tests/offline/test_path_cache.py -q`
Expected: PASS. The `compose_path(parent_path, ...)` at line 406 handles the `/Shared with me` parent path normally (no `/`-special-casing needed since it is not `"/"`).

- [ ] **Step 6: Typecheck & lint, then commit**

Run: `python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/path_cache.py tests/offline/test_path_cache.py
git commit -m "feat(shared): seed populate_from_listing from shared-with-me roots"
```

---

### Task 4: Verify ambient-listing coverage; add `list_shared_roots()` fallback

**Files:**
- Modify: `src/bora/client.py` (new method near `list_folder_iterator`, line 952)
- Test: `tests/offline/test_client.py`

**Interfaces:**
- Produces: `GoogleDriveClient.list_shared_roots() -> list[dict[str, Any]]` — returns raw Drive `files` dicts for `q="sharedWithMe=true and trashed=false"`, projected with `FILE_FIELDS`. Mirrors the `_bootstrap_recursive` paging loop.
- Consumed by: the bootstrap verification step below, and available to the change feed for re-seeding.

**Why:** spec §2 requires confirming the bare `q="trashed=false"` listing already returns shared descendants (it does, empirically — Drive includes shared items in an unscoped `trashed=false` query). This task adds an explicit shared-roots query as a belt-and-suspenders seed source and a verification hook. `_bootstrap_recursive` already passes shared items to `populate_from_listing` (its `filtered` comprehension at `client.py:1091-1099` only strips native docs and the root itself, not shared items), so Task 3 alone makes them materialize. `list_shared_roots()` exists so the bootstrap can log/assert the shared-root count and fall back to per-root enumeration if a future Drive change stops returning shared descendants in the ambient listing.

- [ ] **Step 1: Write the failing test**

Add to `tests/offline/test_client.py` (uses the `client_on_fake` fixture from conftest):

```python
def test_list_shared_roots_returns_shared_items(client_on_fake) -> None:
    client, fake = client_on_fake.client, client_on_fake.fake
    fake.add_shared_root("Team Docs", shared_with_me=True)  # helper added below
    roots = client.list_shared_roots()
    assert any(r["name"] == "Team Docs" and r.get("sharedWithMe") for r in roots)
```

If `FakeDrive` lacks `add_shared_root`, add it in `tests/fake_drive.py` near `add_folder` (line 240):

```python
    def add_shared_root(self, name: str, *, shared_with_me: bool = True) -> str:
        """Insert a folder owned by another user, shared with this account."""
        fid = self._insert(name, parent_id="OTHER_ROOT", mime=FOLDER_MIME, content=b"")
        self._store[fid]["sharedWithMe"] = shared_with_me
        self._store[fid]["ownedByMe"] = False
        self._store[fid]["capabilities"] = {"canEdit": False}
        return fid
```

(Confirm the actual `_insert` return/record-store attribute names against `tests/fake_drive.py:250-273` and `FOLDER_MIME`/`_store` naming; adjust to match.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_client.py::test_list_shared_roots_returns_shared_items -v`
Expected: FAIL — `AttributeError: 'GoogleDriveClient' object has no attribute 'list_shared_roots'`.

- [ ] **Step 3: Implement `list_shared_roots`**

In `src/bora/client.py`, add after `list_folder_iterator` (after line 1038):

```python
    def list_shared_roots(self) -> list[dict[str, Any]]:
        """Return raw Drive ``files`` dicts for every "Shared with me"
        attach point (``sharedWithMe=true``). These are the tops of shared
        subtrees; descendants are fetched by the ambient bootstrap listing.
        """
        self._ensure_root_id()
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            with convert_api_errors():
                resp = self._retry_transient(
                    lambda pt=page_token: self._get_service()
                    .files()
                    .list(
                        q="sharedWithMe=true and trashed=false",
                        fields=f"nextPageToken, files({FILE_FIELDS})",
                        pageSize=_LIST_PAGE_SIZE,
                        pageToken=pt,
                    )
                    .execute()
                )
            self._count_request()
            items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items
```

- [ ] **Step 4: Wire a verification log into `_bootstrap_recursive`**

In `_bootstrap_recursive`, right before `self._path_cache.populate_from_listing(filtered)` (line 1103), add a coverage check:

```python
        # Verify the ambient trashed=false listing already contains the shared
        # roots we expect (spec §2). If any shared root is missing from the
        # flat listing, log it — descendants would otherwise be stranded.
        shared_root_ids = {r["id"] for r in self.list_shared_roots()}
        listed_ids = {it["id"] for it in raw_items}
        missing = shared_root_ids - listed_ids
        if missing:
            logger.warning(
                "Shared roots missing from ambient listing (%d): %s; "
                "shared content under them may not materialize",
                len(missing), sorted(missing),
            )
        self._path_cache.populate_from_listing(filtered)
```

- [ ] **Step 5: Run tests + regression**

Run: `python -m pytest tests/offline/test_client.py -q && python -m pytest tests/offline/test_load_initial_sync.py -q`
Expected: PASS.

- [ ] **Step 6: Typecheck & lint, then commit**

Run: `python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/client.py tests/offline/test_client.py tests/fake_drive.py
git commit -m "feat(shared): add list_shared_roots() and bootstrap coverage check"
```

---

### Task 5: Materialize the local `/Shared with me` directory at sync start

**Files:**
- Modify: `src/bora/sync.py` (near `ensure_sync_folder_present`, line 1235, and its call site at the start of a sync cycle)
- Test: `tests/offline/test_sync.py`

**Interfaces:**
- Produces: `SyncEngine.ensure_shared_with_me_dir_present() -> None` — creates `<sync_path>/Shared with me` if missing, wrapped in `fs_events.ignore`. Called wherever `ensure_sync_folder_present()` is called at sync-cycle start.
- Consumed by: nothing else directly; it guarantees the mount point exists before shared items download into it.

**Note:** the `/Shared with me` directory has no `remote_id`, is never uploaded, and is never itself indexed. It is created like the root sync dir.

- [ ] **Step 1: Write the failing test**

Add to `tests/offline/test_sync.py` (the `sync_engine` fixture provides a real temp sync dir):

```python
def test_ensure_shared_with_me_dir_creates_it(sync_engine) -> None:
    shared = os.path.join(sync_engine.sync_path, "Shared with me")
    assert not os.path.exists(shared)
    sync_engine.ensure_shared_with_me_dir_present()
    assert os.path.isdir(shared)

def test_ensure_shared_with_me_dir_idempotent(sync_engine) -> None:
    sync_engine.ensure_shared_with_me_dir_present()
    sync_engine.ensure_shared_with_me_dir_present()  # no raise on second call
    assert os.path.isdir(os.path.join(sync_engine.sync_path, "Shared with me"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_sync.py::test_ensure_shared_with_me_dir_creates_it -v`
Expected: FAIL — `AttributeError: 'SyncEngine' object has no attribute 'ensure_shared_with_me_dir_present'`.

- [ ] **Step 3: Implement the method**

In `src/bora/sync.py`, after `ensure_sync_folder_present` (line 1235), add. `SHARED_WITH_ME_PATH` must be imported at the top of `sync.py` (`from .path_cache import ... SHARED_WITH_ME_PATH`); `DirCreatedEvent` is already imported from `watchdog.events`:

```python
    def ensure_shared_with_me_dir_present(self) -> None:
        """Create the local "Shared with me" mount point if missing.

        Analogous to the sync root: it has no remote_id, is never uploaded,
        and is created directly rather than via the download path.
        """
        # SHARED_WITH_ME_PATH is "/Shared with me"; strip the leading slash.
        local_path = osp.join(self.sync_path, SHARED_WITH_ME_PATH.lstrip("/"))
        if osp.isdir(local_path):
            return
        with self.fs_events.ignore(DirCreatedEvent(local_path), recursive=False):
            os.makedirs(local_path, exist_ok=True)
```

(Confirm `osp` is the module alias for `os.path` in sync.py — it is used at `_new_tmp_file`/`ensure_sync_folder_present`; if the file uses bare `os.path`, match that.)

- [ ] **Step 4: Call it at sync-cycle start**

Find where `ensure_sync_folder_present()` is invoked at the start of a download/sync cycle (grep `ensure_sync_folder_present(` in `sync.py`) and add a call to `self.ensure_shared_with_me_dir_present()` immediately after it, so the mount point exists before any `/Shared with me/...` item downloads.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_sync.py -k shared_with_me -v`
Expected: PASS.

- [ ] **Step 6: Typecheck & lint, then commit**

Run: `python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/sync.py tests/offline/test_sync.py
git commit -m "feat(shared): materialize local /Shared with me directory at sync start"
```

---

### Task 6: Classify shared-with-me changes in scope

**Files:**
- Modify: `src/bora/changes.py:129-154` (`classify`), `changes.py:202-263` (`_emit_apply`)
- Test: `tests/offline/test_changes.py`

**Interfaces:**
- Consumes: `SHARED_WITH_ME_ID` (Task 2), `path_cache.id_to_path` sentinel branch.
- Produces: `classify` returns `APPLY` for a change whose `file.sharedWithMe == true` even when no parent resolves; `_emit_apply` re-parents such a new shared root onto `SHARED_WITH_ME_ID`.

**Unshare** already flows correctly: it arrives as `removed=True, file=None` and is handled by `_emit_delete` via cache membership (`changes.py:353-354`). No new delete logic — but add a regression test.

- [ ] **Step 1: Write the failing tests**

Add to `tests/offline/test_changes.py` (near the classify tests, using the `_cache()` helper):

```python
def test_classify_shared_with_me_is_apply_without_parent():
    cache = _cache()  # root_id="ROOT"
    change = {"removed": False, "fileId": "SR", "file": {
        "id": "SR", "name": "Team", "parents": ["OTHER"],
        "mimeType": "application/vnd.google-apps.folder",
        "trashed": False, "sharedWithMe": True}}
    assert classify(change, cache) is ChangeDecision.APPLY


def test_emit_apply_attaches_shared_root_to_sentinel():
    cache = _cache()
    out: list = []
    change = {"file": {
        "id": "SR", "name": "Team", "parents": ["OTHER"],
        "mimeType": "application/vnd.google-apps.folder",
        "trashed": False, "sharedWithMe": True}}
    assert _emit_apply(change, cache, out) is True
    assert cache.id_to_path("SR") == "/Shared with me/Team"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/offline/test_changes.py -k "shared_with_me or attaches_shared_root" -v`
Expected: FAIL — `classify` returns `OUT_OF_SCOPE` (no parent resolves, id not cached).

- [ ] **Step 3: Extend `classify`**

In `src/bora/changes.py`, modify `classify` (lines 148-154):

```python
    parents = file.get("parents") or []
    in_scope = any(path_cache.id_to_path(p) is not None for p in parents)
    if not in_scope:
        if file.get("sharedWithMe"):
            # A new shared-with-me attach point: in scope under the virtual root.
            return ChangeDecision.APPLY
        if path_cache.id_to_path(file["id"]) is not None:
            return ChangeDecision.DELETE
        return ChangeDecision.OUT_OF_SCOPE
    return ChangeDecision.APPLY
```

- [ ] **Step 4: Re-parent shared roots in `_emit_apply`**

In `_emit_apply` (lines 228-234), when no parent resolves but the file is a shared root, use the sentinel as parent. Replace the parent-resolution block:

```python
    parents = file.get("parents") or []
    parent_id = next(
        (p for p in parents if path_cache.id_to_path(p) is not None),
        None,
    )
    if parent_id is None and file.get("sharedWithMe"):
        # New shared-with-me attach point — mount under the virtual root.
        parent_id = SHARED_WITH_ME_ID
    parent_path = path_cache.id_to_path(parent_id)
    new_path = compose_path(parent_path, file["name"])
```

Add the import at the top of `changes.py` (it imports from `.path_cache` only under `TYPE_CHECKING` today; add a runtime import): near the other runtime imports add `from .path_cache import SHARED_WITH_ME_ID`. Verify no circular import — `path_cache.py` imports from `native_docs` and `path_codec`, not from `changes`, so this is safe.

- [ ] **Step 5: Add the unshare regression test**

```python
def test_unshare_removed_true_deletes_from_cache():
    cache = _cache()
    cache.set("/Shared with me/Team", "SR", SHARED_WITH_ME_ID, "Team", True)
    out: list = []
    from bora.changes import _emit_delete
    _emit_delete({"removed": True, "fileId": "SR"}, cache, out, None)
    assert cache.id_to_path("SR") is None
    assert any(getattr(e, "path_display", None) == "/Shared with me/Team"
               for e in out)
```

(Import `SHARED_WITH_ME_ID` at the top of the test module.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_changes.py -k "shared or unshare or attaches" -v`
Expected: PASS.

- [ ] **Step 7: Full changes suite + typecheck/lint, then commit**

Run: `python -m pytest tests/offline/test_changes.py -q && python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/changes.py tests/offline/test_changes.py
git commit -m "feat(shared): classify sharedWithMe changes in-scope under virtual root"
```

---

### Task 7: Add the `read_only` index column and thread `can_edit` through

**Files:**
- Modify: `src/bora/models.py:473-535` (`IndexEntry`), `src/bora/sync.py:1023-1120` (both `update_index_from_*` paths)
- Test: `tests/offline/test_sync.py`, `tests/offline/test_core_models.py`

**Interfaces:**
- Produces: `IndexEntry.read_only: int` column (`NonNullColumn(SqlInt(), default=0)`, 0/1), with `IndexEntry.is_read_only -> bool` property. `update_index_from_remote_metadata` sets `read_only = int(not md.can_edit)`.
- Consumed by: Task 9 (upload gate reads `read_only` from the index).

**Recall the Global Constraint:** no migration step — adding the column plus the drop-and-reindex policy handles existing installs; unreleased anyway.

- [ ] **Step 1: Write the failing test**

Add to `tests/offline/test_sync.py`:

```python
def test_index_records_read_only_from_metadata(sync_engine) -> None:
    from bora.core import FileMetadata
    md = FileMetadata(
        name="v.pdf", path_lower="/shared with me/v.pdf",
        path_display="/Shared with me/v.pdf", id="V1",
        modified=_utc_now(), rev="r1", size=1, symlink_target=None,
        shared=True, modified_by=None, is_downloadable=True,
        content_hash="h", can_edit=False,
    )
    sync_engine.update_index_from_remote_metadata(md)
    entry = sync_engine.get_index_entry("/shared with me/v.pdf")  # or the real accessor
    assert entry.is_read_only is True
```

(Use the existing helper the suite uses to fetch an index entry — check `test_sync.py` for `_seed_index_entry`/`get_index_entry`/`_index_table.get`; use `_utc_now()` or the module's datetime helper.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_sync.py::test_index_records_read_only_from_metadata -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'read_only'` (once step 4 wires it) or `AttributeError: is_read_only`.

- [ ] **Step 3: Add the column and property**

In `src/bora/models.py`, in `IndexEntry` after `symlink_target` (line 511-514), add:

```python
    read_only = NonNullColumn(SqlInt(), default=0)
    """1 if the current user may not edit this item (Drive canEdit == false).
    Consulted as the upload gate so no remote fetch is needed at local-change
    time. Defaults 0 (editable)."""
```

And a property after `is_symlink` (line 526-529):

```python
    @property
    def is_read_only(self) -> bool:
        """Returns True for view-only shared items."""
        return bool(self.read_only)
```

- [ ] **Step 4: Thread `can_edit` through both index writers**

In `src/bora/sync.py`, `update_index_from_remote_metadata` (lines 1080-1120): in the per-type branch (lines 1090-1101) derive `read_only`, and pass it into the `IndexEntry(...)` at 1109-1120:

```python
            if isinstance(md, FileMetadata):
                ...
                symlink_target = md.symlink_target
                read_only = int(not md.can_edit)
            elif isinstance(md, FolderMetadata):
                ...
                symlink_target = None
                read_only = int(not md.can_edit)
            ...
            entry = IndexEntry(
                remote_path_cased=remote_path_cased,
                remote_path_lower=md.path_lower,
                remote_id=md_id,
                item_type=item_type,
                last_sync=None,
                rev=rev,
                content_hash=hash_str,
                symlink_target=symlink_target,
                read_only=read_only,
            )
```

For the SyncEvent-based writer (`update_index_from_sync_event`, lines 1023-1078): `SyncEvent` has no `can_edit` today. Preserve the existing index value on re-index by reading it back before the write. Immediately before constructing the `IndexEntry` at line 1067, add:

```python
            existing = self._index_table.get(remote_path_lower)
            read_only = existing.read_only if existing is not None else 0
```

and add `read_only=read_only,` to that `IndexEntry(...)`. (This keeps a read-only file read-only across event-driven re-index; the authoritative source is always the metadata path above.)

- [ ] **Step 5: Run tests + models regression**

Run: `python -m pytest tests/offline/test_sync.py -k read_only tests/offline/test_core_models.py -q`
Expected: PASS.

- [ ] **Step 6: Typecheck & lint, then commit**

Run: `python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/models.py src/bora/sync.py tests/offline/test_sync.py
git commit -m "feat(shared): add read_only index column; thread can_edit through index writers"
```

---

### Task 8: Make view-only downloads read-only on disk

**Files:**
- Modify: `src/bora/sync.py` (`_on_remote_file` after the move, ~line 3799; `_on_remote_folder` after `os.mkdir`, ~line 3869; and the overwrite/delete paths that must chmod +w first)
- Test: `tests/offline/test_sync.py` (or `tests/linked/integration/test_sync.py` for the real chmod round-trip)

**Interfaces:**
- Consumes: `md.can_edit` / the event's read-only knowledge.
- Produces: after downloading a `not can_edit` file, its write bits are cleared (`0o444` file / `0o555` dir) inside `fs_events.ignore`; before the engine overwrites or deletes a read-only local file for a later remote change, it restores write permission first.

**Design decision:** `SyncEvent` does not carry `can_edit`. Read the authoritative read-only flag from the index entry that was just written (Task 7) rather than plumbing a new SyncEvent field. Add a small helper `_is_read_only_event(event) -> bool` that consults `self._index_table.get(event.remote_path_lower)`.

- [ ] **Step 1: Write the failing test**

A precise chmod assertion is cleanest as a linked integration test, but add an offline-level test that the helper + chmod hook fire. Add to `tests/offline/test_sync.py`:

```python
def test_readonly_file_download_clears_write_bits(sync_engine, monkeypatch) -> None:
    # Seed a read-only index entry, then invoke the chmod hook directly.
    import stat as _stat
    local = os.path.join(sync_engine.sync_path, "Shared with me", "v.txt")
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "w") as f:
        f.write("x")
    sync_engine._apply_read_only(local, is_dir=False)  # helper added below
    mode = _stat.S_IMODE(os.lstat(local).st_mode)
    assert not (mode & 0o222)  # no write bits
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_sync.py::test_readonly_file_download_clears_write_bits -v`
Expected: FAIL — `AttributeError: '_apply_read_only'`.

- [ ] **Step 3: Add the chmod helpers**

In `src/bora/sync.py` add two helpers on `SyncEngine`:

```python
    def _apply_read_only(self, local_path: str, *, is_dir: bool) -> None:
        """Clear write bits on a view-only shared item, ignoring the
        resulting chmod so it is not echoed back as a local change."""
        mode = 0o555 if is_dir else 0o444
        with self.fs_events.ignore(
            FileModifiedEvent(local_path), recursive=False
        ):
            os.chmod(local_path, mode)

    def _restore_writable(self, local_path: str) -> None:
        """Restore write permission before overwriting/deleting a read-only
        local file for an incoming remote change."""
        try:
            cur = stat.S_IMODE(os.lstat(local_path).st_mode)
        except FileNotFoundError:
            return
        with self.fs_events.ignore(
            FileModifiedEvent(local_path), recursive=False
        ):
            os.chmod(local_path, cur | 0o200)
```

(Confirm `FileModifiedEvent` is imported from `watchdog.events` — add to the existing import at `sync.py:53` if absent — and `stat` is imported.)

Add the read-only lookup helper:

```python
    def _is_read_only_event(self, event: "SyncEvent") -> bool:
        entry = self._index_table.get(event.remote_path_lower)
        return entry is not None and entry.is_read_only
```

- [ ] **Step 4: Call `_apply_read_only` after download / mkdir**

In `_on_remote_file`, after the `move(...)` block (after line 3799) and after `update_index_from_sync_event` (line 3801, so the index row exists), add:

```python
        if self._is_read_only_event(event):
            self._apply_read_only(event.local_path, is_dir=False)
```

In `_on_remote_folder`, after `os.mkdir` (line 3869) / its index update, add the same with `is_dir=True`.

Before the engine overwrites or deletes a local file for a remote change — the `move(tmp_fname, event.local_path, ...)` (line 3793) and the remote-delete local removal path — call `self._restore_writable(event.local_path)` first (a read-only target would otherwise `EACCES`). Add it immediately before the `move` at line 3793 and before the local `delete(...)` in the remote-delete handler.

- [ ] **Step 5: Run the offline test; add a linked round-trip test**

Run: `python -m pytest tests/offline/test_sync.py -k read_only -v`
Expected: PASS.

Add (for the real download path) to `tests/linked/integration/test_sync.py` a test that shares a view-only file from a second account and asserts `not (os.stat(local).st_mode & 0o222)` — mark it with the suite's existing account-gated marker. (This runs only under `BORA_TEST_REFRESH_TOKEN`; document it, do not block M1 on it.)

- [ ] **Step 6: Typecheck & lint, then commit**

Run: `python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/sync.py tests/offline/test_sync.py tests/linked/integration/test_sync.py
git commit -m "feat(shared): make view-only shared downloads read-only on disk"
```

---

### Task 9: Gate uploads on the `read_only` flag

**Files:**
- Modify: `src/bora/sync.py:2386-2415` (`_create_remote_entry` dispatch)
- Test: `tests/offline/test_sync.py`

**Interfaces:**
- Consumes: `IndexEntry.read_only` (Task 7), `_is_read_only_event` (Task 8).
- Produces: `_create_remote_entry` returns the event with `SyncStatus.Skipped` (no error, no retry) when the item is read-only, before any create/update/delete handler runs. Editable shared items fall through the normal path unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/offline/test_sync.py`:

```python
def test_read_only_local_change_is_skipped_not_uploaded(sync_engine) -> None:
    from bora.models import SyncEvent, SyncStatus
    # Seed a read-only index entry for the path.
    _seed_index_entry(sync_engine, "/shared with me/v.txt", remote_id="V1",
                      read_only=1)
    event = _local_modified_event(sync_engine, "/Shared with me/v.txt")
    called = []
    sync_engine._on_local_file_modified = lambda e: called.append(e)  # spy
    result = sync_engine._create_remote_entry(event)
    assert result.status == SyncStatus.Skipped
    assert called == []  # handler never invoked
```

(Adapt `_seed_index_entry`/`_local_modified_event` to the helpers already in `test_sync.py`; ensure `_seed_index_entry` accepts/sets `read_only`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_sync.py::test_read_only_local_change_is_skipped_not_uploaded -v`
Expected: FAIL — status is not `Skipped`; the spy is called.

- [ ] **Step 3: Add the gate**

In `_create_remote_entry` (line 2386), immediately before the `try:` dispatch (line 2405), add:

```python
        if self._is_read_only_event(event):
            # View-only shared item: never propagate local mutations. Skip
            # silently — no error, no retry storm (spec §5).
            event.status = SyncStatus.Skipped
            return event
```

(Match how the method currently returns/sets status — if it returns a `SyncEvent` after setting `event.status`, mirror that; check the existing return at the end of the method.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_sync.py -k "read_only or skipped" -v`
Expected: PASS.

- [ ] **Step 5: Full sync suite regression + typecheck/lint, then commit**

Run: `python -m pytest tests/offline/test_sync.py -q && python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/sync.py tests/offline/test_sync.py
git commit -m "feat(shared): gate uploads on read_only index flag (silent skip)"
```

---

### Task 10: Handle the reserved-name collision

**Files:**
- Modify: `src/bora/path_cache.py:336-451` (`populate_from_listing` Phase 2, and the change-feed apply)
- Test: `tests/offline/test_path_cache.py`

**Interfaces:**
- Consumes: `generate_cc_name` (`utils/path.py:293`) is the existing conflict-rename helper, but it operates on *local* paths; for the cache we need a name-level conflict rename. The simplest correct behavior: the virtual root wins the reserved path `/Shared with me`, and a real My-Drive folder literally named "Shared with me" directly under root is given a conflict-suffixed cache path.
- Produces: when a real root-child folder folds to the reserved name, its cache entry is renamed (e.g. `Shared with me (name conflict)`), the sentinel keeps `/Shared with me`.

**Rarity:** documented edge case. Keep it minimal and well-logged.

- [ ] **Step 1: Write the failing test**

```python
class TestReservedNameCollision:
    def test_real_folder_named_shared_with_me_is_renamed(self) -> None:
        cache = make_cache()  # root_id="ROOT"
        real = {"id": "RF", "name": "Shared with me",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["ROOT"]}
        cache.populate_from_listing([real])
        path = cache.id_to_path("RF")
        assert path is not None
        assert path != "/Shared with me"
        assert path.startswith("/Shared with me")  # suffixed variant
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_path_cache.py::TestReservedNameCollision -v`
Expected: FAIL — the real folder is stored at exactly `/Shared with me`, colliding with the sentinel path.

- [ ] **Step 3: Rename real root-children that fold to the reserved name**

In `populate_from_listing`, in the BFS build (inside the `while queue` loop, after computing `encoded_path` at line 406), guard against the reserved path for root-children:

```python
                raw_name = child["name"]
                encoded_path = compose_path(parent_path, raw_name)
                # The virtual root owns "/Shared with me"; a real My-Drive
                # folder that folds to it gets a conflict suffix (spec §8).
                if (
                    fold_path(encoded_path) == fold_path(SHARED_WITH_ME_PATH)
                    and cid not in shared_root_ids
                ):
                    raw_name = f"{raw_name} (name conflict)"
                    encoded_path = compose_path(parent_path, raw_name)
                    logger.warning(
                        "Real folder %r collides with the reserved "
                        "'/Shared with me' path; renamed to %r locally",
                        child["name"], raw_name,
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_path_cache.py::TestReservedNameCollision tests/offline/test_path_cache.py::TestPopulateSharedRoots -v`
Expected: PASS.

- [ ] **Step 5: Full path-cache suite + typecheck/lint, then commit**

Run: `python -m pytest tests/offline/test_path_cache.py -q && python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/path_cache.py tests/offline/test_path_cache.py
git commit -m "feat(shared): rename real My-Drive folder colliding with reserved path"
```

---

### Task 11: Verify selective-sync prunes shared paths (integration test)

**Files:**
- Test: `tests/offline/test_main.py` (or wherever `exclude_items` is tested)
- Possibly modify: `src/bora/main.py` only if a bug surfaces (none expected — `/Shared with me/...` are ordinary paths).

**Interfaces:**
- Consumes: existing `Bora.exclude_items` / `_remove_after_excluded` (`main.py:1120-1190`), `is_excluded_by_user`.
- Produces: confirmation that `excluded add "/Shared with me/Team"` prunes the index + local tree, and that excluding `/Shared with me` itself disables the whole feature.

No new config surface (spec §7). This task is a guard test that the reuse actually works end-to-end.

- [ ] **Step 1: Write the test**

Add to the appropriate offline test module:

```python
def test_exclude_shared_subfolder_removes_index_and_local(sync_engine, tmp_path):
    # Seed a shared item in index + on disk, then exclude it.
    local = os.path.join(sync_engine.sync_path, "Shared with me", "Team")
    os.makedirs(local, exist_ok=True)
    _seed_index_entry(sync_engine, "/shared with me/team", remote_id="SR",
                      item_type="folder")
    # Drive the exclusion path (adapt to the real Bora facade in this fixture).
    sync_engine.excluded_items = ["/shared with me/team"]
    # After exclusion the local tree is gone and the index row cleared.
    assert not os.path.exists(local)
    assert sync_engine._index_table.get("/shared with me/team") is None
```

(If `exclude_items` lives on the `Bora` facade rather than `SyncEngine`, use the `m` fixture from `conftest.py` and its `exclude_items`; adapt the seeding to that fixture's DB.)

- [ ] **Step 2: Run test**

Run: `python -m pytest tests/offline -k exclude_shared -v`
Expected: PASS (no production change needed). If it FAILS, systematically debug (superpowers:systematic-debugging) — a failure means the reserved path needs the same `fold_path(encode_path(...))` bridge treatment as ordinary paths; fix in `main.py` and re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/offline/test_main.py
git commit -m "test(shared): selective-sync prunes shared-with-me paths via existing machinery"
```

---

### M1 gate

- [ ] Run the full offline suite excluding slow load tests:

Run: `python -m pytest tests/offline -m 'not load' -q`
Expected: all pass (≥ the pre-existing count, plus the new tests). Then run the load subset separately as the controller (they are multi-minute — do not run inside a subagent): `python -m pytest tests/offline -m load -q`.

- [ ] `python -m mypy src/bora && python -m flake8 src/bora tests` — parity with baseline.
- [ ] **M1 is independently shippable here** — `/Shared with me/` materializes read-only-aware. Consider a `superpowers:requesting-code-review` checkpoint before M2.

---

# Milestone M2 — Shortcut symlinks (download-only)

Turns My Drive shortcuts into local symlinks. Depends on M1's resolved paths. Spec §4.

---

### Task 12: Emit shortcut metadata instead of dropping shortcuts

**Files:**
- Modify: `src/bora/changes.py:202-263` (`_emit_apply`), and `src/bora/client.py:1091-1099` (bootstrap `filtered`)
- Test: `tests/offline/test_changes.py`

**Interfaces:**
- Consumes: `is_shortcut` (`native_docs.py:71`), raw `shortcutDetails.targetId` (Task 1), `path_cache.id_to_path` to resolve the target's cache path.
- Produces: a shortcut change yields a `FileMetadata` whose `symlink_target` is set to the target's local path **relative to the symlink's own directory** (robust to sync-root moves). If the target does not resolve in this batch, the shortcut is deferred (kept pending); if it never resolves, it is dropped with a sync-error-worthy log.

**Relative-target computation:** given the shortcut's cache path `new_path` and the target's cache path `target_path` (both encoded, rooted at `/`), the relative symlink text is `os.path.relpath(target_path, os.path.dirname(new_path))`. Both are POSIX-style `/`-joined virtual paths, so use `posixpath.relpath`. Example: shortcut `/Shared with me/link`, target `/Docs/report.pdf` → `../Docs/report.pdf`.

- [ ] **Step 1: Write the failing test**

```python
def test_shortcut_emits_relative_symlink_metadata():
    cache = _cache()  # root_id="ROOT"
    cache.set("/Docs", "D", "ROOT", "Docs", True)
    cache.set("/Docs/report.pdf", "T", "D", "report.pdf", False)
    out: list = []
    change = {"file": {
        "id": "SL", "name": "link", "parents": ["ROOT"],
        "mimeType": "application/vnd.google-apps.shortcut", "trashed": False,
        "shortcutDetails": {"targetId": "T",
                            "targetMimeType": "application/pdf"}}}
    assert _emit_apply(change, cache, out) is True
    md = out[-1]
    assert md.symlink_target == "Docs/report.pdf"  # relative to "/" dir


def test_shortcut_with_unresolved_target_is_not_emitted():
    cache = _cache()
    out: list = []
    change = {"file": {
        "id": "SL", "name": "link", "parents": ["ROOT"],
        "mimeType": "application/vnd.google-apps.shortcut", "trashed": False,
        "shortcutDetails": {"targetId": "MISSING"}}}
    # Returns False -> deferred by the fixpoint; nothing emitted this pass.
    assert _emit_apply(change, cache, out) is False
    assert out == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/offline/test_changes.py -k shortcut -v`
Expected: FAIL — `_emit_apply` currently returns `True` and emits nothing for any native/shortcut mime (the `is_google_native` early return at line 226).

- [ ] **Step 3: Replace the shortcut drop with symlink emission**

In `_emit_apply`, replace the early-return block (lines 223-227) so shortcuts are handled, other native docs still dropped:

```python
    file = change["file"]
    mime = file.get("mimeType")
    if is_shortcut(mime):
        return _emit_shortcut(change, path_cache, out_entries)
    # Other Google-native docs (Docs/Sheets/…) are still dropped.
    if is_google_native(mime):
        return True
```

Add `_emit_shortcut` next to `_emit_apply`:

```python
def _emit_shortcut(
    change: dict[str, Any],
    path_cache: "PathIDCache",
    out_entries: list[Any],
) -> bool:
    """Project a Drive shortcut into symlink FileMetadata.

    Resolves ``shortcutDetails.targetId`` to the target's cache path and
    emits a FileMetadata whose ``symlink_target`` is that path *relative to
    the symlink's own directory*. Returns False (defer) when the shortcut's
    own parent or its target does not resolve yet — the fixpoint retries.
    Resolves ONE hop only: a shortcut-to-shortcut target is refused.
    """
    file = change["file"]
    parents = file.get("parents") or []
    parent_id = next(
        (p for p in parents if path_cache.id_to_path(p) is not None), None
    )
    if parent_id is None:
        return False  # parent not resolved yet; defer
    new_path = compose_path(path_cache.id_to_path(parent_id), file["name"])

    details = file.get("shortcutDetails") or {}
    target_id = details.get("targetId")
    target_path = path_cache.id_to_path(target_id) if target_id else None
    if target_path is None:
        # Target excluded / not yet resolved. Defer; the fixpoint or a later
        # batch may materialize it. A permanently-missing target is logged by
        # the caller's stall handler.
        return False

    # Store the shortcut node itself in the cache so its own children (none,
    # for a shortcut) and future change-detection resolve; then emit metadata.
    old_path = path_cache.id_to_path(file["id"])
    if old_path is None:
        stored = path_cache.set(
            new_path, file["id"], parent_id, file["name"], is_folder=False
        )
    elif old_path != new_path:
        stored = path_cache.move(old_path, new_path)
    else:
        stored = True
    if not stored:
        return False

    rel_target = posixpath.relpath(target_path, posixpath.dirname(new_path))
    md = to_file_metadata(file, new_path)
    md.symlink_target = rel_target
    md.is_downloadable = False
    out_entries.append(md)
    return True
```

Add imports at the top of `changes.py`: `import posixpath` and extend the `native_docs` import to include `is_shortcut`. Note `to_file_metadata` on a shortcut file dict yields `size=0`, `content_hash=""` — acceptable for a symlink; `symlink_target` is overwritten immediately after.

**One-hop chain guard:** if `details.get("targetMimeType") == GOOGLE_SHORTCUT_MIME_TYPE`, refuse — return `True` with a warning (consume, do not loop):

```python
    if details.get("targetMimeType") == GOOGLE_SHORTCUT_MIME_TYPE:
        logger.warning(
            "Shortcut %r points at another shortcut; not following the chain",
            file.get("name"),
        )
        return True
```

Place this guard right after reading `details`, before resolving `target_path`. Import `GOOGLE_SHORTCUT_MIME_TYPE` from `native_docs`.

- [ ] **Step 4: Stop dropping shortcuts in bootstrap**

In `client.py` `_bootstrap_recursive`, the `filtered` comprehension (lines 1091-1099) drops all `is_google_native` items, which includes shortcuts. Keep shortcuts so `populate_from_listing`/downstream can materialize them. Change the predicate to exclude only *non-shortcut* native docs:

```python
        filtered = [
            it
            for it in raw_items
            if it.get("id") != root_id
            and (
                include_non_downloadable_files
                or not is_google_native(it.get("mimeType"))
                or is_shortcut(it.get("mimeType"))
            )
        ]
```

(Add `is_shortcut` to the `native_docs` import in `client.py`.) Note: `populate_from_listing` builds `PathCacheEntry` for shortcuts as non-folders (mime ≠ folder), which is correct. The symlink *materialization* from the bootstrap path is handled where bootstrap pages are turned into download events (the shortcut needs its `symlink_target` computed there too) — but since the change-feed `_emit_shortcut` is the primary path and bootstrap is followed by a normal reconcile, keep bootstrap producing plain cache entries and let the reconcile/download compute symlink targets via metadata. If a bootstrap-time symlink is required, mirror `_emit_shortcut`'s relative-target computation in the bootstrap page projection; add that only if the M2 smoke test shows shortcuts from initial sync not materializing.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_changes.py -k shortcut -v`
Expected: PASS.

- [ ] **Step 6: Full changes + client suites + typecheck/lint, then commit**

Run: `python -m pytest tests/offline/test_changes.py tests/offline/test_client.py -q && python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/changes.py src/bora/client.py tests/offline/test_changes.py
git commit -m "feat(shortcut): emit relative symlink metadata for Drive shortcuts"
```

---

### Task 13: Windows `.lnk` writer under `utils/`

**Files:**
- Create: `src/bora/utils/shortcuts.py`
- Test: `tests/offline/utils/test_shortcuts.py`

**Interfaces:**
- Produces: `write_platform_symlink(target: str, link_path: str) -> None` — attempts `os.symlink(target, link_path)`; on `OSError` (Windows without privilege, or unsupported FS) writes a minimal `.lnk` at `link_path + ".lnk"` via a pure-Python writer `write_lnk(target, lnk_path)`.
- Consumed by: Task 14 (download branch).

**Decision (spec open item):** use a **pure-Python `.lnk` writer** — no new dependency. The MS-SHLLINK format for a bare relative path is small and stable. Keep it minimal: a header + a `RelativePath` string. This avoids adding `pywin32`/`winshell` to the dependency floor.

- [ ] **Step 1: Write the failing test**

Create `tests/offline/utils/test_shortcuts.py`:

```python
import os
import pytest
from bora.utils.shortcuts import write_platform_symlink, write_lnk


def test_write_platform_symlink_posix(tmp_path):
    target = "../Docs/report.pdf"
    link = str(tmp_path / "link")
    write_platform_symlink(target, link)
    assert os.path.islink(link)
    assert os.readlink(link) == target


def test_write_lnk_produces_file_with_target(tmp_path):
    lnk = str(tmp_path / "link.lnk")
    write_lnk("../Docs/report.pdf", lnk)
    data = open(lnk, "rb").read()
    assert data[:4] == (76).to_bytes(4, "little")  # HeaderSize = 0x4C
    assert b"report.pdf" in data  # relative path embedded (UTF-16 or ASCII)


def test_write_platform_symlink_falls_back_to_lnk(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("no symlink privilege")
    monkeypatch.setattr(os, "symlink", boom)
    link = str(tmp_path / "link")
    write_platform_symlink("../Docs/report.pdf", link)
    assert os.path.exists(link + ".lnk")
    assert not os.path.exists(link)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/offline/utils/test_shortcuts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bora.utils.shortcuts'`.

- [ ] **Step 3: Implement the module**

Create `src/bora/utils/shortcuts.py`:

```python
"""Platform-agnostic symlink / Windows .lnk shortcut writer.

POSIX gets a real symlink. Windows without Developer Mode / admin cannot
create symlinks; there we fall back to a minimal MS-SHLLINK (.lnk) file
carrying a relative-path target. Pure-Python — no extra dependency."""
from __future__ import annotations

import os
import struct

__all__ = ["write_platform_symlink", "write_lnk"]

# MS-SHLLINK ShellLinkHeader constants.
_HEADER_SIZE = 0x0000004C
_LINK_CLSID = b"\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46"
# LinkFlags: HasLinkTargetIDList off; HasRelativePath on (0x00000008)
#            + IsUnicode (0x00000080).
_LINK_FLAGS = 0x00000088
_FILE_ATTR_NORMAL = 0x00000080


def write_platform_symlink(target: str, link_path: str) -> None:
    """Create a symlink at *link_path* pointing to *target*.

    Falls back to a ``<link_path>.lnk`` shortcut file when the OS refuses
    a real symlink (typically Windows without privilege)."""
    try:
        os.symlink(target, link_path)
    except OSError:
        write_lnk(target, link_path + ".lnk")


def _unicode_str(value: str) -> bytes:
    """MS-SHLLINK StringData: CountCharacters (UInt16) + UTF-16LE chars."""
    encoded = value.encode("utf-16-le")
    return struct.pack("<H", len(value)) + encoded


def write_lnk(target: str, lnk_path: str) -> None:
    """Write a minimal .lnk carrying *target* as a RelativePath string."""
    header = struct.pack("<I", _HEADER_SIZE)
    header += _LINK_CLSID
    header += struct.pack("<I", _LINK_FLAGS)
    header += struct.pack("<I", _FILE_ATTR_NORMAL)
    header += b"\x00" * 8  # CreationTime
    header += b"\x00" * 8  # AccessTime
    header += b"\x00" * 8  # WriteTime
    header += struct.pack("<I", 0)  # FileSize
    header += struct.pack("<i", 0)  # IconIndex
    header += struct.pack("<I", 1)  # ShowCommand = SW_SHOWNORMAL
    header += struct.pack("<H", 0)  # HotKey
    header += b"\x00" * 10          # Reserved1/2/3
    # No LinkTargetIDList (flag off) and no LinkInfo (flag off).
    body = _unicode_str(target)  # RelativePath StringData (HasRelativePath)
    with open(lnk_path, "wb") as f:
        f.write(header + body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/offline/utils/test_shortcuts.py -v`
Expected: PASS. (The header is exactly 0x4C bytes; `report.pdf` appears UTF-16LE-encoded so `b"report.pdf"` will not match verbatim — adjust the assertion to check for the UTF-16 form `"report.pdf".encode("utf-16-le") in data`.)

Fix the test assertion in `test_write_lnk_produces_file_with_target` accordingly:

```python
    assert "report.pdf".encode("utf-16-le") in data
```

- [ ] **Step 5: Typecheck & lint, then commit**

Run: `python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/utils/shortcuts.py tests/offline/utils/test_shortcuts.py
git commit -m "feat(shortcut): pure-Python platform symlink / .lnk writer"
```

---

### Task 14: Materialize symlinks in the download path

**Files:**
- Modify: `src/bora/sync.py:3724-3732` (`_on_remote_file` symlink branch)
- Test: `tests/offline/test_sync.py`

**Interfaces:**
- Consumes: `write_platform_symlink` (Task 13); `event.symlink_target` (populated by Task 12 metadata → SyncEvent).
- Produces: the download branch calls `write_platform_symlink(event.symlink_target, event.local_path)` instead of a bare `os.symlink`, so Windows falls back to `.lnk`.

- [ ] **Step 1: Write the failing test**

```python
def test_symlink_download_uses_platform_writer(sync_engine, monkeypatch):
    calls = []
    import bora.sync as sync_mod
    monkeypatch.setattr(sync_mod, "write_platform_symlink",
                        lambda t, p: calls.append((t, p)))
    event = _symlink_event(sync_engine, "/Shared with me/link",
                           target="../Docs/report.pdf")  # helper builds SyncEvent
    sync_engine._on_remote_file(event)
    assert calls == [("../Docs/report.pdf",
                      os.path.join(sync_engine.sync_path, "Shared with me", "link"))]
```

(Add `_symlink_event` to the test module: build a `SyncEvent` with `symlink_target` set and `is_file=True`; mirror the existing `_download_event` helper at `test_sync.py:253`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_sync.py::test_symlink_download_uses_platform_writer -v`
Expected: FAIL — `_on_remote_file` calls bare `os.symlink`, and `write_platform_symlink` is not imported in `sync.py`.

- [ ] **Step 3: Swap in the platform writer**

Add the import at the top of `sync.py`: `from .utils.shortcuts import write_platform_symlink`. Replace the symlink branch body (lines 3726-3731):

```python
        if event.symlink_target is not None:
            # Don't download but reproduce the shortcut locally (symlink on
            # POSIX, .lnk fallback on Windows).
            with self.fs_events.ignore(
                FileCreatedEvent(event.local_path), recursive=False
            ):
                with convert_api_errors(remote_path=event.remote_path):
                    write_platform_symlink(event.symlink_target, event.local_path)
            status = SyncStatus.Done
```

Note: the removed `os.lstat` line (3731) fed a `stat` used later — check the post-branch code (line 3792+ uses `stat` from the download branch). Since the symlink branch sets `status = SyncStatus.Done` and returns before the download `move`, confirm the symlink branch already skips the `stat`-consuming code (it did — the `else` download branch owns `stat`). If `stat` is referenced after the if/else unconditionally, retain an `os.lstat(event.local_path)` inside the ignore block. Verify by reading lines 3800-3810 during implementation.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_sync.py -k symlink -v`
Expected: PASS.

- [ ] **Step 5: Typecheck & lint, then commit**

Run: `python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/sync.py tests/offline/test_sync.py
git commit -m "feat(shortcut): materialize shortcuts via platform symlink writer"
```

---

### Task 15: Relax the upload guard for known shortcut symlinks

**Files:**
- Modify: `src/bora/sync.py:2560-2570` (`_on_local_file_modified` symlink guard)
- Test: `tests/offline/test_sync.py`

**Interfaces:**
- Consumes: the index — a symlink whose path has an `IndexEntry` with `is_symlink` (i.e. a known shortcut) is *silently ignored* on upload in M2; unknown local symlinks still raise `SymlinkError`.
- Produces: M2 makes shortcut symlinks download-only. Local edits/deletes are not propagated in M2 (that is M3).

- [ ] **Step 1: Write the failing test**

```python
def test_local_symlink_matching_known_shortcut_is_skipped(sync_engine):
    from bora.models import SyncStatus
    _seed_index_entry(sync_engine, "/shared with me/link", remote_id="SL",
                      symlink_target="../Docs/report.pdf")
    event = _local_modified_event(sync_engine, "/Shared with me/link")
    event.symlink_target = "../Docs/report.pdf"
    status = sync_engine._on_local_file_modified(event)
    assert status == SyncStatus.Skipped


def test_unknown_local_symlink_still_errors(sync_engine):
    from bora.exceptions import SymlinkError  # confirm import path
    event = _local_modified_event(sync_engine, "/nope/link")
    event.symlink_target = "to_nowhere"
    with pytest.raises(SymlinkError):
        sync_engine._on_local_file_modified(event)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/offline/test_sync.py -k symlink_matching -v`
Expected: FAIL — the current guard raises `SymlinkError` for *any* symlink.

- [ ] **Step 3: Relax the guard**

In `_on_local_file_modified`, replace the symlink guard (lines 2564-2570):

```python
        if event.symlink_target is not None:
            entry = self._index_table.get(event.remote_path_lower)
            if entry is not None and entry.is_symlink:
                # Known shortcut symlink: download-only in M2. Ignore local
                # mutations silently (M3 will propagate them).
                return SyncStatus.Skipped
            error = SymlinkError(
                "Could not upload item",
                "Syncing symlinks is not supported.",
            )
            error.local_path = event.local_path
            raise error
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_sync.py -k symlink -v`
Expected: PASS.

- [ ] **Step 5: Typecheck & lint, then commit**

Run: `python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/sync.py tests/offline/test_sync.py
git commit -m "feat(shortcut): silently ignore known shortcut symlinks on upload (M2)"
```

---

### M2 gate

- [ ] `python -m pytest tests/offline -m 'not load' -q` — all pass.
- [ ] `python -m mypy src/bora && python -m flake8 src/bora tests` — parity.
- [ ] **Live smoke test (catches real bugs on this project):** from a second Drive account, add a My-Drive shortcut to a synced file → confirm a local symlink appears pointing at the correct relative path; on a Windows VM (or with `os.symlink` monkeypatched) confirm the `.lnk` fallback. Budget for inline fixes.

---

# Milestone M3 — Reverse-direction symlink propagation

Propagate local symlink changes back to Drive shortcuts. Depends on M2. Spec §"M3".

---

### Task 16: Delete a local symlink → delete the Drive shortcut

**Files:**
- Modify: `src/bora/sync.py:2704` (`_on_local_deleted`) — only if shortcuts are being filtered out
- Test: `tests/offline/test_sync.py`

**Interfaces:**
- Consumes: the existing **path-based** delete `GoogleDriveClient.remove(remote_path, parent_rev=None)` (`client.py:829` → `WriteClient.remove`, `writes.py:590`), which resolves the id via `_resolve_file_id(remote_path)` internally.
- Produces: deleting a local shortcut symlink deletes the corresponding Drive shortcut. A shortcut has an ordinary index row + path-cache entry, so the generic `_on_local_deleted` path should already call `self.client.remove(event.remote_path)`. This task confirms that and only adds code if a symlink guard short-circuits it.

- [ ] **Step 1: Read `_on_local_deleted` and confirm the delete call**

Read `src/bora/sync.py:2704` and its helpers. Establish whether a local delete of a symlink already reaches `self.client.remove(event.remote_path)` (it deletes by path, resolving the id from the cache). Note the exact method call used.

- [ ] **Step 2: Write the test**

```python
def test_delete_local_symlink_deletes_remote_shortcut(sync_engine):
    calls = []
    # remove() is path-based: (remote_path, parent_rev=None).
    sync_engine.client.remove = lambda p, parent_rev=None: calls.append(p)
    _seed_index_entry(sync_engine, "/shared with me/link", remote_id="SL",
                      symlink_target="../Docs/report.pdf")
    # Seed the path cache so the delete can resolve, mirroring the suite's
    # existing deletion tests.
    sync_engine._path_cache.set("/Shared with me/link", "SL",
                                parent_id="SR", name="link", is_folder=False)
    event = _local_deleted_event(sync_engine, "/Shared with me/link")
    sync_engine._on_local_deleted(event)
    assert calls == ["/Shared with me/link"]
```

(Adapt `_local_deleted_event` to the suite's helper for building a deleted `SyncEvent`; match the real `remote_path` casing the engine produces.)

- [ ] **Step 3: Run test to verify current behavior**

Run: `python -m pytest tests/offline/test_sync.py::test_delete_local_symlink_deletes_remote_shortcut -v`
Expected: If it PASSES, deletion already propagates — keep the test as a regression guard and skip to Step 5. If it FAILS (a symlink/read-only guard skips the delete), proceed to Step 4.

- [ ] **Step 4: Wire the delete if it was being skipped**

Only if Step 3 failed: in `_on_local_deleted`, ensure a symlink whose index row carries a `remote_id` calls `self.client.remove(event.remote_path)`. Gate on write access to the containing folder (Task 18's `_restore_writable`/read-only-parent check applies equally — a read-only shared folder's shortcut cannot be deleted remotely; skip silently). Wrap no local FS mutation here (the local symlink is already gone).

- [ ] **Step 5: Typecheck & lint, then commit**

Run: `python -m pytest tests/offline/test_sync.py -k delete_local_symlink -q && python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/sync.py tests/offline/test_sync.py
git commit -m "feat(shortcut): propagate local symlink deletion to Drive shortcut (M3)"
```

---

### Task 17: Create/retarget a local symlink → create/update the Drive shortcut

**Files:**
- Modify: `src/bora/writes.py` (new `create_shortcut`; new `retarget_shortcut`), `src/bora/client.py` (thin facade methods next to `make_dir`, line 873), `src/bora/sync.py:2548` (`_on_local_file_modified` symlink branch from Task 15)
- Test: `tests/offline/test_client.py` / `test_writes.py`, `tests/offline/test_sync.py`

**Interfaces:**
- Produces:
  - `WriteClient.create_shortcut(remote_path: str, target_remote_path: str, *, autorename: bool = False) -> FileMetadata` — resolves `parent_id` via `_resolve_parent_id`, `target_id` via `_resolve_file_id(target_remote_path)`, then `files.create` with `mimeType=GOOGLE_SHORTCUT_MIME_TYPE` and `shortcutDetails.targetId`; writes the path cache like `make_dir`.
  - `WriteClient.retarget_shortcut(remote_path: str, target_remote_path: str) -> FileMetadata` — `files.update(fileId, body={"shortcutDetails": {"targetId": ...}})`.
  - `GoogleDriveClient.create_shortcut(...)` / `.retarget_shortcut(...)` — thin delegations, matching the `make_dir` facade (`client.py:873`).
- Consumes: everything is **path-based** (matches the rest of the write API). The sync layer must convert the local symlink's target text → a `target_remote_path` before calling; if that target maps to no known item, `_resolve_file_id` raises `NotFoundError` → surface a notice, leave local (Task 18).

- [ ] **Step 1: Write the failing test (write client)**

```python
def test_create_shortcut_sets_target(client_on_fake):
    client, fake = client_on_fake.client, client_on_fake.fake
    # Seed a target file and its cache entry.
    fake.add_file("/Docs/report.pdf", content=b"x")
    # Bootstrap the cache so path_to_id resolves /Docs and /Docs/report.pdf.
    list(client.list_folder_iterator("/", recursive=True))
    result = client.create_shortcut("/Docs/link", "/Docs/report.pdf")
    assert result.symlink_target is not None
    # The created Drive object is a shortcut pointing at the target's id.
    created = fake.get_by_path("/Docs/link")  # adapt to FakeDrive accessor
    assert created["mimeType"] == "application/vnd.google-apps.shortcut"
    assert created["shortcutDetails"]["targetId"] == fake.id_of("/Docs/report.pdf")
```

(Extend `tests/fake_drive.py` so `files.create` honors a `shortcutDetails` body and `mimeType=shortcut`, and add `get_by_path`/`id_of` accessors if absent. Keep `test_fake_drive.py` green.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_client.py -k create_shortcut -v`
Expected: FAIL — `AttributeError: create_shortcut`.

- [ ] **Step 3: Implement `create_shortcut` in `writes.py`**

Model on `make_dir` (`writes.py:846`). Add to `WriteClient`:

```python
    def create_shortcut(
        self,
        remote_path: str,
        target_remote_path: str,
        *,
        autorename: bool = False,
    ) -> FileMetadata:
        """Create a Drive shortcut at *remote_path* pointing at the item
        currently synced at *target_remote_path*."""
        client = self._client
        client._ensure_root_id()
        parent_path, raw_name = self._split_parent_name(remote_path)
        if autorename:
            raw_name = self._autorename_unique(parent_path, raw_name)
            remote_path = parent_path + "/" + raw_name
        drive_name = self._decode_segment(raw_name)
        parent_id = self._resolve_parent_id(parent_path, for_path=remote_path)
        target_id = self._resolve_file_id(target_remote_path)  # NotFoundError if absent

        new_id = client.reserve_file_id()
        body = {
            "id": new_id,
            "name": drive_name,
            "mimeType": GOOGLE_SHORTCUT_MIME_TYPE,
            "parents": [parent_id],
            "shortcutDetails": {"targetId": target_id},
        }
        with convert_api_errors(remote_path=remote_path):
            resp = client._retry_transient(
                lambda: client._get_service()
                .files()
                .create(body=body, fields="id,name,mimeType,shortcutDetails")
                .execute()
            )
        client._count_request()
        client._path_cache.set(
            remote_path, resp["id"], parent_id=parent_id,
            name=drive_name, is_folder=False,
        )
        # Relative-to-own-dir target, matching the download-side convention.
        rel_target = posixpath.relpath(
            target_remote_path, posixpath.dirname(remote_path)
        )
        return FileMetadata(
            name=resp["name"], path_lower=fold_path(remote_path),
            path_display=remote_path, id=resp["id"],
            modified=datetime.now(timezone.utc), rev="", size=0,
            symlink_target=rel_target, shared=False, modified_by=None,
            is_downloadable=False, content_hash="", can_edit=True,
        )

    def retarget_shortcut(
        self, remote_path: str, target_remote_path: str
    ) -> FileMetadata:
        """Point an existing Drive shortcut at a new target."""
        client = self._client
        client._ensure_root_id()
        file_id = self._resolve_file_id(remote_path)
        target_id = self._resolve_file_id(target_remote_path)
        with convert_api_errors(remote_path=remote_path):
            resp = client._retry_transient(
                lambda: client._get_service()
                .files()
                .update(
                    fileId=file_id,
                    body={"shortcutDetails": {"targetId": target_id}},
                    fields="id,name,mimeType,shortcutDetails",
                )
                .execute()
            )
        client._count_request()
        rel_target = posixpath.relpath(
            target_remote_path, posixpath.dirname(remote_path)
        )
        return FileMetadata(
            name=resp["name"], path_lower=fold_path(remote_path),
            path_display=remote_path, id=resp["id"],
            modified=datetime.now(timezone.utc), rev="", size=0,
            symlink_target=rel_target, shared=False, modified_by=None,
            is_downloadable=False, content_hash="", can_edit=True,
        )
```

Add imports to `writes.py`: `import posixpath`, `from datetime import datetime, timezone` (if not present), and `from .native_docs import GOOGLE_SHORTCUT_MIME_TYPE`. Confirm `reserve_file_id`, `fold_path`, `_decode_segment` are already available (used by `make_dir`/`upload`).

Add the facade methods to `client.py` after `make_dir` (line 880):

```python
    def create_shortcut(
        self, remote_path: str, target_remote_path: str, *, autorename: bool = False
    ) -> FileMetadata:
        """Creates a Drive shortcut at *remote_path* → *target_remote_path*."""
        return self._writes.create_shortcut(
            remote_path, target_remote_path, autorename=autorename
        )

    def retarget_shortcut(
        self, remote_path: str, target_remote_path: str
    ) -> FileMetadata:
        """Points an existing shortcut at a new target."""
        return self._writes.retarget_shortcut(remote_path, target_remote_path)
```

- [ ] **Step 4: Wire local-symlink create/retarget into the upload path**

In `_on_local_file_modified`, extend the symlink branch (from Task 15). Add a helper `_target_remote_path(event) -> str | None` that converts the symlink text to a remote path: resolve `os.path.normpath(os.path.join(os.path.dirname(event.local_path), event.symlink_target))` to an absolute local path, strip the `self.sync_path` prefix, and convert to the engine's remote-path form using the **same** local→remote conversion the engine already uses to build `event.remote_path` (grep for how `SyncEvent.remote_path` is derived from `local_path` — e.g. a `to_remote_path`/`sync_event` factory — and reuse it). Return `None` if the target is outside the sync folder.

```python
        if event.symlink_target is not None:
            entry = self._index_table.get(event.remote_path_lower)
            target_remote = self._target_remote_path(event)
            if target_remote is None or self._path_cache.path_to_id(target_remote) is None:
                self._add_sync_error(  # match the real error-surfacing API
                    event, "Symlink target is not a synced Drive item; left local."
                )
                return SyncStatus.Skipped
            if entry is not None and entry.is_symlink and entry.remote_id:
                self.client.retarget_shortcut(event.remote_path, target_remote)
            else:
                self.client.create_shortcut(event.remote_path, target_remote)
            self.update_index_from_sync_event(event)
            return SyncStatus.Done
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_client.py tests/offline/test_sync.py -k shortcut -v`
Expected: PASS.

- [ ] **Step 6: Typecheck & lint, then commit**

Run: `python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/writes.py src/bora/client.py src/bora/sync.py tests/offline/test_client.py tests/offline/test_sync.py tests/fake_drive.py
git commit -m "feat(shortcut): create/retarget Drive shortcuts from local symlinks (M3)"
```

---

### Task 18: Gate reverse propagation on write access

**Files:**
- Modify: `src/bora/sync.py` (the M3 create/retarget/delete branches from Tasks 16-17)
- Test: `tests/offline/test_sync.py`

**Interfaces:**
- Consumes: `IndexEntry.read_only` of the *containing folder*; the sync-error surface (`_add_sync_error` or the real equivalent — grep `add_sync_error`/`SyncError`).
- Produces: shortcut create/retarget/delete is skipped silently (no retry storm) when the containing folder is read-only. (Unmapped-target handling already lands in Task 17 Step 4; this task adds the read-only-folder gate and its test.)

- [ ] **Step 1: Write the failing test**

```python
def test_shortcut_create_skipped_in_readonly_folder(sync_engine):
    from bora.models import SyncStatus
    _seed_index_entry(sync_engine, "/shared with me/team", remote_id="SR",
                      item_type="folder", read_only=1)
    created = []
    sync_engine.client.create_shortcut = lambda *a, **k: created.append(a)
    event = _local_symlink_create_event(
        sync_engine, "/Shared with me/Team/link", target="../report.pdf")
    status = sync_engine._on_local_file_modified(event)
    assert status == SyncStatus.Skipped
    assert created == []  # no remote create attempted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/offline/test_sync.py -k readonly_folder -v`
Expected: FAIL — a create is attempted.

- [ ] **Step 3: Add the write-access gate**

In the symlink branch of `_on_local_file_modified` (before resolving the target / calling the client), add a read-only-parent check:

```python
            parent_lower = fold_path(posixpath.dirname(event.remote_path_lower))
            parent_entry = self._index_table.get(parent_lower)
            if parent_entry is not None and parent_entry.is_read_only:
                return SyncStatus.Skipped  # cannot write into a view-only folder
```

Apply the same guard in the Task 16 delete branch (a read-only shared folder's shortcut cannot be removed remotely).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/offline/test_sync.py -k "readonly_folder or unmapped_symlink or shortcut" -v`
Expected: PASS.

- [ ] **Step 5: Full offline suite, typecheck, lint, then commit**

Run: `python -m pytest tests/offline -m 'not load' -q && python -m mypy src/bora && python -m flake8 src/bora tests`

```bash
git add src/bora/sync.py tests/offline/test_sync.py
git commit -m "feat(shortcut): gate reverse propagation on write access to containing folder"
```

---

### M3 gate

- [ ] `python -m pytest tests/offline -m 'not load' -q` then `-m load -q` (controller-run) — all pass.
- [ ] `python -m mypy src/bora && python -m flake8 src/bora tests` — parity.
- [ ] **Live smoke test:** delete a local shortcut symlink → confirm the Drive shortcut is removed; create a local symlink to a synced file → confirm a Drive shortcut appears; point a symlink at a non-synced target → confirm it stays local with a notice.
- [ ] `superpowers:finishing-a-development-branch` to integrate.

---

## Notes for the executor

- **Line numbers drift** as you edit. Each task quotes the *current* surrounding code so you can re-anchor by content, not just line number. Re-grep before editing.
- **`FakeDrive` gaps.** The offline `FakeDrive` (`tests/fake_drive.py`) does not model `sharedWithMe`, `capabilities`, `shortcutDetails`, or shortcut `files.create` today. Several tasks add small helpers (`add_shared_root`, shortcut create). Extend `_insert`/`_project`/`_match_query` as needed and keep the FakeDrive contract suite (`test_fake_drive.py`) green.
- **Do not run `tests/offline` while a smoke-test account is linked** (keyring collision wipes the linked account — see the team memory). Run the linked suite separately.
- **Load/slow tests are controller-run**, never inside an implementer subagent (they stall on multi-minute waits).
- **Deviation from spec §6 is intentional and load-bearing:** there is no schema-version bump because this fork has no migration framework — it drops and reindexes. Do not invent an `ALTER TABLE` migration.
