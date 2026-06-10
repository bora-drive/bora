# Cluster E — Port-Review Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all 14 verified findings from the 2026-06-10 port review: four data-loss-class bugs (full-Drive reindex cascade, duplicate-name cross-write, remote moves leaving stale local copies, rev-check TOCTOU), four robustness bugs (retry-duplicate creates, stale change-token dead-end, cross-page change drops, dead exception clauses), and six smaller holes (mignore/excluded encoding, make_dir conflict, path-cache thread safety, longpoll gate, auth edges, quota constants).

**Architecture:** The findings share one root: the changes/path_cache layer emulates Dropbox semantics for the path-keyed sync engine, and the emulation leaks. The fixes stay within the existing path-keyed architecture but make the emulation honest: the change feed stops emitting entries the cache rejected, remote moves are applied locally by `remote_id` (generalizing the existing `_apply_case_change` machinery), creates become idempotent via pre-generated Drive file IDs, and recursive listings are scoped to the requested subtree instead of rebuilding the world. Error handling gains the missing `CursorResetError` path and retires dead Dropbox-era except clauses.

**Tech Stack:** Python, pytest, offline `FakeDrive` (tests/fake_drive.py — already supports `generateIds`, `revisions`, `fail_next` fault injection), linked real-account suite for final confirmation only.

**Evidence:** Each task header cites the review finding and its file:line evidence. Verified 2026-06-10 against branch `google-drive-port` (HEAD 551d180).

> ⚠️ **Test-run safety (from project memory):** do NOT run `pytest tests/offline` while a real smoke-test account is linked — the keyring test collision wipes the linked account. Unlink or use a scratch config first.

---

## File structure

- **Modify** `src/cumulus/client.py` — scoped recursive listing, longpoll gate, cursor-reset mapping, ID pool, auth guards, quota constants.
- **Modify** `src/cumulus/changes.py` — conflict-rejection skip, whole-batch fixpoint, cursor-reset mapping.
- **Modify** `src/cumulus/writes.py` — idempotent creates, make_dir conflict probe, TOCTOU narrowing + post-upload revision audit.
- **Modify** `src/cumulus/sync.py` — `_apply_remote_move`, dead except clauses, mignore/excluded decoding.
- **Modify** `src/cumulus/path_cache.py` — internal lock.
- **Modify** `src/cumulus/path_codec.py` — `fold_human`.
- **Modify** `src/cumulus/manager.py` — `CursorResetError` recovery branch.
- **Modify** `src/cumulus/oauth.py` / `src/cumulus/exceptions.py` — only if compile errors surface; no planned changes.
- **Extend** `tests/fake_drive.py` — honor `body["id"]` on create (409 on duplicate), revision bookkeeping on update.
- **Extend** `tests/offline/test_changes.py`, `tests/offline/test_writes.py`, `tests/offline/test_sync.py`, `tests/offline/test_path_cache.py`, `tests/offline/test_client_auth.py`.

Task order is severity-first; Tasks 1–4 are the data-loss fixes. Tasks are independent unless noted (Task 6 must precede Task 7; Task 2 before Task 4).

---

### Task 1: Scoped recursive listing (review F1 — full-Drive reindex + restart delete cascade)

`client.list_folder_iterator(path, recursive=True)` currently ignores `path` and runs `_bootstrap_recursive` (client.py:932-934): it wipes/rebuilds the entire path cache and returns every item in the Drive. `sync._update_index_recursive` (sync.py:2459-2465, called after every local folder move) and `_get_remote_folder` then index never-downloaded files, which `_get_local_changes_while_inactive` (sync.py:1806) deletes from Drive on the next restart.

**Files:**
- Modify: `src/cumulus/client.py` (list_folder_iterator ~904-980, new `_scoped_recursive`)
- Test: `tests/offline/test_sync.py` (or `tests/offline/test_writes.py` if the client fixture lives there — match the existing FakeDrive wiring, e.g. `client._get_service = Mock(return_value=fake.service())`)

- [ ] **Step 1: Write the failing tests**

```python
def test_recursive_list_of_subfolder_is_scoped(linked_client, fake_drive):
    """list_folder(path, recursive=True) for a non-root path must return only
    that subtree and must NOT rebuild the path cache (review F1)."""
    sub_id = fake_drive.add_folder("/sub")
    inner_id = fake_drive.add_file("/sub/inner.txt", content=b"x")
    out_id = fake_drive.add_file("/outside.txt", content=b"y")
    # Bootstrap once so the cache knows everything.
    linked_client.list_folder("/", recursive=True)

    result = linked_client.list_folder("/sub", recursive=True)
    paths = {md.path_display for md in result.entries}
    assert paths == {"/sub/inner.txt"}, paths

    # Cache must still know the out-of-subtree file (no clear+rebuild).
    assert linked_client._path_cache.path_to_id("/outside.txt") == out_id
    assert linked_client._path_cache.path_to_id("/sub") == sub_id
    assert linked_client._path_cache.path_to_id("/sub/inner.txt") == inner_id


def test_recursive_list_of_root_still_bootstraps(linked_client, fake_drive):
    """recursive=True on '/' keeps the bootstrap semantics (initial sync)."""
    fake_drive.add_file("/a.txt", content=b"a")
    result = linked_client.list_folder("/", recursive=True)
    assert {md.path_display for md in result.entries} == {"/a.txt"}
```

> NOTE for the implementer: build `linked_client`/`fake_drive` fixtures from the existing pattern in `tests/offline/test_writes.py` (FakeDrive + `_get_service` mock + a `PathIDCache` over an in-memory Database, `root_id = fake ROOT_ID`).

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_sync.py::test_recursive_list_of_subfolder_is_scoped -v`
Expected: FAIL — `paths` contains `/outside.txt` (whole Drive returned).

- [ ] **Step 3: Implement `_scoped_recursive`**

In `client.py`, replace the `if recursive:` block of `list_folder_iterator` (lines 932-934):

```python
        if recursive:
            if remote_path in ("", "/"):
                # Initial-sync bootstrap: enumerate the whole Drive and
                # atomically (re)build the path cache.
                yield from self._bootstrap_recursive(include_non_downloadable_files)
            else:
                yield from self._scoped_recursive(
                    remote_path, include_non_downloadable_files
                )
            return
```

Add below `_bootstrap_recursive`:

```python
    def _scoped_recursive(
        self, remote_path: str, include_non_downloadable_files: bool = False
    ) -> Iterator[ListFolderResult]:
        """BFS-list one subtree via per-folder ``files.list`` calls.

        Unlike :meth:`_bootstrap_recursive` this never clears the cache:
        discovered items are written through with ``path_cache.set`` so
        later operations can resolve them. Used by the sync engine to
        refresh an index subtree after a folder move.
        """
        root_folder_id = self._path_cache.path_to_id(remote_path)
        if root_folder_id is None:
            raise NotFoundError(
                "Not found",
                f"No folder at '{remote_path}' is known to Cumulus.",
                remote_path=remote_path,
            )
        queue: list[tuple[str, str]] = [(root_folder_id, remote_path)]
        while queue:
            folder_id, folder_path = queue.pop(0)
            page_token: str | None = None
            while True:
                with convert_api_errors(remote_path=folder_path):
                    resp = self._retry_transient(
                        lambda pt=page_token, fid=folder_id: self._get_service()
                        .files()
                        .list(
                            q=f"'{fid}' in parents and trashed=false",
                            fields=f"nextPageToken, files({FILE_FIELDS})",
                            pageSize=_LIST_PAGE_SIZE,
                            pageToken=pt,
                        )
                        .execute()
                    )
                self._count_request()
                entries: list[Metadata] = []
                for file in resp.get("files", []):
                    mime = file.get("mimeType")
                    if is_google_native(mime) and not include_non_downloadable_files:
                        continue
                    child_path = compose_path(folder_path, file["name"])
                    is_folder = is_google_folder(mime)
                    self._path_cache.set(
                        child_path, file["id"], parent_id=folder_id,
                        name=file["name"], is_folder=is_folder,
                    )
                    if is_folder:
                        entries.append(to_folder_metadata(file, child_path))
                        queue.append((file["id"], child_path))
                    else:
                        entries.append(to_file_metadata(file, child_path))
                page_token = resp.get("nextPageToken")
                yield ListFolderResult(
                    entries=entries,
                    has_more=bool(page_token) or bool(queue),
                    cursor="",
                )
                if not page_token:
                    break
```

- [ ] **Step 4: Run both tests + the offline client/sync suites**

Run: `python -m pytest tests/offline/test_sync.py tests/offline/test_writes.py -q`
Expected: new tests PASS, no regressions. (`_update_index_recursive` passes `md.path_lower` — the folded path; `path_to_id` folds its input, and `fold_path` is idempotent on folded paths, so resolution works unchanged.)

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/client.py tests/offline/test_sync.py
git commit -m "fix(cluster-e): scope recursive list_folder to the requested subtree

A recursive listing of a non-root path no longer rebuilds the whole
path cache and returns the full Drive — which let a folder move during
incomplete initial sync index never-downloaded files that the startup
reconciler then trashed on Drive (review F1)."
```

---

### Task 2: Change feed must not emit entries the cache rejected (review F2 — duplicate-name cross-write)

`_emit_apply` (changes.py:208-218) ignores the `False` return of `path_cache.set()`/`move()` on a fold-key conflict but still appends metadata at the colliding path, so the engine downloads file B over file A's local copy and later path-keyed writes hit A's Drive file.

**Files:**
- Modify: `src/cumulus/changes.py:208-218`
- Test: `tests/offline/test_changes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_fold_conflicting_change_is_not_emitted(path_cache):
    """A change whose path the cache rejects (duplicate / case-fold-equal
    sibling) must NOT be emitted — emitting it points the engine's
    download at a path mapped to a DIFFERENT file's id (review F2)."""
    path_cache.root_id = "root"
    path_cache.set("/Report.pdf", "id-A", "root", "Report.pdf", False)

    out: list = []
    change = {
        "fileId": "id-B",
        "file": {
            "id": "id-B", "name": "report.pdf", "parents": ["root"],
            "mimeType": "text/plain", "trashed": False,
        },
    }
    _emit_apply(change, path_cache, out)

    assert out == [], "rejected change leaked into the feed output"
    assert path_cache.path_to_id("/report.pdf") == "id-A"  # cache untouched
```

> Match the existing `path_cache` fixture / imports in `tests/offline/test_changes.py` (it already imports `_emit_apply` or tests via `iter_remote_changes`; if only the latter, drive the same scenario through `iter_remote_changes` with a fake service returning this one change).

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_changes.py::test_fold_conflicting_change_is_not_emitted -v`
Expected: FAIL — `out` contains a FileMetadata for `/report.pdf`.

- [ ] **Step 3: Implement the skip**

Replace changes.py lines 208-218:

```python
    old_path = path_cache.id_to_path(file["id"])
    is_folder = mime == GOOGLE_FOLDER_MIME_TYPE
    if old_path is None:
        stored = path_cache.set(
            new_path, file["id"], parent_id, file["name"], is_folder
        )
    elif old_path != new_path:
        stored = path_cache.move(old_path, new_path)
    else:
        stored = True  # pure content update; cache path already correct.
    if not stored:
        # The cache rejected the path (duplicate-name / case-fold conflict
        # with a different file id). Emitting metadata at this path would
        # cross-wire two files' content. Drop the entry; the path keeps
        # mapping to the first file (first-id-wins, matching
        # populate_from_listing).
        logger.warning(
            "Dropping change for %r (id=%s): path conflicts with an "
            "existing item. Rename one of the duplicates in the Google "
            "Drive web UI to sync both.",
            new_path, file["id"],
        )
        return
    if is_folder:
        out_entries.append(to_folder_metadata(file, new_path))
    else:
        out_entries.append(to_file_metadata(file, new_path))
```

- [ ] **Step 4: Run the changes suite**

Run: `python -m pytest tests/offline/test_changes.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/changes.py tests/offline/test_changes.py
git commit -m "fix(cluster-e): drop change-feed entries the path cache rejected

A fold-key conflict (duplicate or case-differing sibling, legal on
Drive) previously still emitted metadata at the colliding path, so the
engine downloaded one file's content over another's local copy and
later path-keyed writes targeted the wrong Drive file (review F2)."
```

---

### Task 3: Apply remote moves locally by remote_id (review F3 — stale local copies after remote rename)

The feed emits a single APPLY at the new path for a remote move (changes.py:212-213); sync.py only deletes local items on `DeletedMetadata` and never matches by `remote_id`, so the old local file/tree and index entry persist forever. Fix at the engine: before the case-change/conflict logic, look the event's `remote_id` up in the index; if it lives at a different folded path, move the local item and re-key the index subtree — the general-rename sibling of `_apply_case_change` (sync.py:3774).

**Files:**
- Modify: `src/cumulus/sync.py` — new `_apply_remote_move` + calls in `_on_remote_file` and `_on_remote_folder` (immediately before each `self._apply_case_change(event)`); new index lookup helper
- Test: `tests/offline/test_sync.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_remote_file_rename_moves_local_file(sync_engine_with_fake):
    """A remote rename (same id, new path) must move the local file and
    re-key the index — not leave the old copy on disk forever (review F3)."""
    sync = sync_engine_with_fake
    _write_local_file(sync, "/a.txt", b"content")
    _seed_index_entry(sync, "/a.txt", remote_id="id-1", rev="rev-1")

    md = FileMetadata(
        name="b.txt", path_lower="/b.txt", path_display="/b.txt",
        id="id-1", modified=datetime.now(timezone.utc), rev="rev-1",
        size=7, symlink_target=None, shared=False, modified_by=None,
        is_downloadable=True, content_hash=_md5(b"content"),
    )
    event = SyncEvent.from_metadata(md, sync)
    sync._create_local_entry(event)

    assert not osp.exists(sync.to_local_path_from_cased("/a.txt"))
    assert osp.exists(sync.to_local_path_from_cased("/b.txt"))
    assert sync.get_index_entry("/a.txt") is None
    entry = sync.get_index_entry("/b.txt")
    assert entry is not None and entry.remote_id == "id-1"


def test_remote_folder_rename_moves_local_tree(sync_engine_with_fake):
    """A remote folder rename must take the whole local subtree and its
    index rows with it (Drive emits no per-child change entries)."""
    sync = sync_engine_with_fake
    _write_local_file(sync, "/old/kid.txt", b"k")
    _seed_index_entry(sync, "/old", remote_id="id-dir", rev="folder",
                      item_type=ItemType.Folder)
    _seed_index_entry(sync, "/old/kid.txt", remote_id="id-kid", rev="rev-k")

    md = FolderMetadata(name="new", path_lower="/new", path_display="/new",
                        id="id-dir", shared=False)
    event = SyncEvent.from_metadata(md, sync)
    sync._create_local_entry(event)

    assert osp.isdir(sync.to_local_path_from_cased("/new"))
    assert osp.exists(sync.to_local_path_from_cased("/new/kid.txt"))
    assert not osp.exists(sync.to_local_path_from_cased("/old"))
    assert sync.get_index_entry("/old/kid.txt") is None
    kid = sync.get_index_entry("/new/kid.txt")
    assert kid is not None and kid.remote_id == "id-kid"
```

> Reuse the seeding helpers already in tests/offline/test_sync.py (`_write_local_file`, `_seed_index_entry`); extend `_seed_index_entry` with `remote_id=`/`item_type=` kwargs if missing.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_sync.py::test_remote_file_rename_moves_local_file -v`
Expected: FAIL — `/a.txt` still exists locally and in the index.

- [ ] **Step 3: Add the index-by-remote-id lookup**

Next to `get_index_entry` in sync.py:

```python
    def get_index_entry_by_remote_id(self, remote_id: str) -> IndexEntry | None:
        """Return the index entry currently holding *remote_id*, if any."""
        with self._database_access():
            rows = self._index_table.select_sql(
                "WHERE remote_id = ?", remote_id
            )
        return rows[0] if rows else None
```

> NOTE for the implementer: match the `select_sql` parameter encoding used by the other `_index_table.select_sql` call sites (some columns need `os.fsencode`); `remote_id` is a plain string column.

- [ ] **Step 4: Implement `_apply_remote_move`**

Add above `_apply_case_change` (sync.py:~3774):

```python
    def _apply_remote_move(self, event: SyncEvent) -> None:
        """Apply a remote rename / reparent locally before downloading.

        Drive's change feed emits a single entry at the NEW path for a
        moved item (no DeletedMetadata for the old path, no per-child
        entries for folders). Detect the move by remote_id, move the
        local item, and re-key the index subtree, so the engine does not
        leave a stale copy at the old path. Case-only renames (folded
        key unchanged) are handled by :meth:`_apply_case_change`.
        """
        if not event.remote_id:
            return
        entry = self.get_index_entry_by_remote_id(event.remote_id)
        if entry is None or entry.remote_path_lower == event.remote_path_lower:
            return
        local_path_old = self.to_local_path_from_cased(entry.remote_path_cased)
        old_lower = entry.remote_path_lower
        old_cased = entry.remote_path_cased

        if osp.exists(local_path_old) and not osp.exists(event.local_path):
            event_cls = DirMovedEvent if isdir(local_path_old) else FileMovedEvent
            with self.fs_events.ignore(event_cls(local_path_old, event.local_path)):
                move(local_path_old, event.local_path)
            self._logger.debug(
                'Moved local item "%s" -> "%s" (remote move, id=%s)',
                old_cased, event.remote_path, event.remote_id,
            )

        with self._database_access(), self._db.transaction():
            rows = [entry]
            if entry.item_type is ItemType.Folder:
                rows += [
                    child
                    for child in self._index_table.select(
                        PathTreeQuery(IndexEntry.remote_path_lower, old_lower)
                    )
                    if child.remote_path_lower != old_lower
                ]
            for row in rows:
                old_pk = row.remote_path_lower
                row.remote_path_lower = (
                    event.remote_path_lower + row.remote_path_lower[len(old_lower):]
                )
                row.remote_path_cased = (
                    event.remote_path + row.remote_path_cased[len(old_cased):]
                )
                self._index_table.delete_primary_key(old_pk)
                self._index_table.update(row)
            self._case_conversion_cache.clear()
```

> NOTE for the implementer: `remote_path_lower` is the table's primary key (models.py:478) — the delete+re-insert per row is required; verify `delete_primary_key`/`update` against `database/orm.py` (mirror what `remove_node_from_index` and `_apply_case_change` use). If `_case_conversion_cache` has no `clear()`, evict the moved keys individually with `put`.

- [ ] **Step 5: Wire the calls**

In `_on_remote_file` and `_on_remote_folder` (sync.py:3668-3676 and the file variant above it), insert directly before each `self._apply_case_change(event)`:

```python
        self._apply_remote_move(event)
```

(Not in `_on_remote_deleted` — deletion events carry `remote_id=None`, models.py:307.)

- [ ] **Step 6: Run tests + full offline sync suite**

Run: `python -m pytest tests/offline/test_sync.py -q`
Expected: both new tests PASS; existing case-rename tests (cluster-B) stay green — for a case-only rename the folded keys are equal, so `_apply_remote_move` is a no-op by construction.

- [ ] **Step 7: Commit**

```bash
git add src/cumulus/sync.py tests/offline/test_sync.py
git commit -m "fix(cluster-e): apply remote moves locally by remote_id

Drive emits a single change at the new path for a rename/reparent;
the engine only deleted local items on DeletedMetadata, so every
remote move left the old file or whole folder tree on disk and a
stale index entry behind (review F3). Generalizes the cluster-B
_apply_case_change mechanism to arbitrary moves."
```

---

### Task 4: Cross-page parent/child resolution in the change feed (review F6)

The parent-before-child fixpoint runs per page (changes.py:265-302, page size 100); a child whose new parent arrives on a later page is dropped as OUT_OF_SCOPE while sync.py persists the cursor past it. Fix: accumulate all pages to the final `newStartPageToken` first, run one fixpoint over the whole batch, yield a single result. (Depends on Task 2's `_emit_apply` shape.)

**Files:**
- Modify: `src/cumulus/changes.py` (`iter_remote_changes`)
- Test: `tests/offline/test_changes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_child_before_parent_across_pages_is_not_dropped(path_cache):
    """A child delivered on page N whose brand-new parent folder arrives on
    page N+1 must still be emitted (review F6)."""
    path_cache.root_id = "root"
    child = {"fileId": "id-c", "file": {
        "id": "id-c", "name": "kid.txt", "parents": ["id-p"],
        "mimeType": "text/plain", "trashed": False}}
    parent = {"fileId": "id-p", "file": {
        "id": "id-p", "name": "newdir", "parents": ["root"],
        "mimeType": "application/vnd.google-apps.folder", "trashed": False}}
    pages = [
        {"changes": [child], "nextPageToken": "t2"},
        {"changes": [parent], "newStartPageToken": "t3"},
    ]

    class _Svc:
        def changes(self): return self
        def list(self, **kw): return self
        def execute(self): return pages.pop(0)

    results = list(iter_remote_changes(lambda: _Svc(), lambda c: c(), path_cache, "t1"))
    entries = [e for r in results for e in r.entries]
    paths = {e.path_display for e in entries}
    assert "/newdir/kid.txt" in paths, paths
    assert results[-1].cursor == "t3"
```

> Adjust the fake-service shim to whatever pattern test_changes.py already uses for `iter_remote_changes` (it passes `service_factory` and `retry` directly, so a minimal shim works).

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_changes.py::test_child_before_parent_across_pages_is_not_dropped -v`
Expected: FAIL — `/newdir/kid.txt` missing (dropped on page 1).

- [ ] **Step 3: Rework `iter_remote_changes` to batch-then-classify**

Replace the body of the `while True:` page loop (changes.py:252-313) with:

```python
    # Phase 1: drain every page to the final newStartPageToken, buffering
    # raw changes. Parent-before-child is not guaranteed even ACROSS
    # pages, so classification must see the whole batch (review F6). A
    # poll cycle's batch is bounded by the changes since the last poll.
    cursor = last_cursor
    raw: list[dict[str, Any]] = []
    while True:
        resp = retry(
            lambda c=cursor: service_factory()
            .changes()
            .list(
                pageToken=c,
                fields=_CHANGE_FIELDS,
                pageSize=_CHANGE_PAGE_SIZE,
                includeRemoved=True,
            )
            .execute()
        )
        raw.extend(resp.get("changes", []))
        next_token = resp.get("nextPageToken")
        cursor = next_token or resp.get("newStartPageToken") or ""
        if not next_token:
            break

    # Phase 2: terminal changes first (deletes/trash need no parent),
    # then the parent-dependent fixpoint over the WHOLE batch.
    out_entries: list[Any] = []
    pending: list[dict[str, Any]] = []
    for change in raw:
        file = change.get("file")
        if change.get("removed") or (file is not None and file.get("trashed")):
            _emit_delete(change, path_cache, out_entries)
        elif file is None:
            logger.debug("Skipping malformed change entry: %r", change)
        else:
            pending.append(change)

    while pending:
        deferred: list[dict[str, Any]] = []
        for change in pending:
            if classify(change, path_cache) is ChangeDecision.APPLY:
                _emit_apply(change, path_cache, out_entries)
            else:
                deferred.append(change)
        if len(deferred) == len(pending):
            break  # no parent resolved this pass; the rest never will
        pending = deferred

    for change in pending:
        if classify(change, path_cache) is ChangeDecision.DELETE:
            _emit_delete(change, path_cache, out_entries)

    yield ListFolderResult(entries=out_entries, has_more=False, cursor=cursor)
```

Update the docstring: remove the "split *across* page boundaries is still delivery-order dependent" caveat; note that the consumer now sees one result per poll cycle and the cursor only advances after the whole batch is classified (crash mid-batch re-fetches the batch; downloads are idempotent).

- [ ] **Step 4: Run the changes + sync suites**

Run: `python -m pytest tests/offline/test_changes.py tests/offline/test_sync.py -q`
Expected: PASS. `download_sync_cycle` (sync.py:2977) iterates the same interface and now receives one chunk; the per-page memory note in its body still holds for the bootstrap path, which is unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/changes.py tests/offline/test_changes.py
git commit -m "fix(cluster-e): resolve change-feed parent/child ordering across pages

The fixpoint ran per 100-entry page, silently dropping a child whose
new parent folder arrived on a later page while the cursor advanced
past it (review F6). Classification now runs once over the whole
drained batch and the cursor is only persisted afterwards."
```

---

### Task 5: Narrow the rev-check TOCTOU and audit overwrites via revision history (review F4)

Dropbox's atomic `WriteMode.update(rev)` became check-then-write with no precondition (Drive v3 has no If-Match): writes.py:219-234 checks `headRevisionId`, then update/trash executes unconditionally — the race window spans an entire resumable upload. Full atomicity is impossible on Drive v3; the fix is (a) post-upload detection via `revisions.list`, and (b) rescuing a clobbered revision as a remote conflicted copy. (`remove` keeps check-then-trash: trash is user-recoverable and the window is milliseconds.)

**Files:**
- Modify: `src/cumulus/writes.py` (upload epilogue; helpers `_audit_update_rev`, `_rescue_revision`)
- Modify: `tests/fake_drive.py` — make `files.update` with `media_body` append a revision dict (mirror what `_insert` does) if it doesn't already
- Test: `tests/offline/test_writes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_upload_detects_interleaved_remote_revision(linked_writes, fake_drive):
    """If a remote revision lands between the rev check and our update,
    the clobbered revision must be rescued as a remote conflicted copy
    (review F4: Drive v3 has no atomic precondition)."""
    fid = fake_drive.add_file("/doc.txt", content=b"v1")
    rev1 = fake_drive.files[fid]["headRevisionId"]
    _bootstrap_cache(linked_writes)  # see existing helpers in this module

    # Simulate the race: a collaborator commits v2 AFTER our client read
    # rev1 but BEFORE files.update executes. fail_next-style hook: patch
    # the fake to insert the revision when files.update is first called.
    def interleave():
        rec = fake_drive.files[fid]
        rec["content"] = b"v2-collaborator"
        rec["headRevisionId"] = fake_drive._new_rev()
        rec["md5Checksum"] = hashlib.md5(b"v2-collaborator").hexdigest()
        rec["revisions"].append(fake_drive._revision_dict(rec))
    fake_drive.on_next_update = interleave  # add this tiny hook to the fake

    local = _make_local_file(b"v3-ours")
    linked_writes.upload(local, "/doc.txt",
                         write_mode=WriteMode.Update, update_rev=rev1)

    # Our content won (head), but the collaborator's revision was rescued
    # as a sibling conflicted copy on Drive.
    names = {r["name"] for r in fake_drive.files.values() if not r["trashed"]}
    assert any("conflicted copy" in n for n in names), names
```

> NOTE for the implementer: add the one-shot `on_next_update` hook to `tests/fake_drive.py`'s `files.update` finalize (call + clear it before applying the update). The pre-update rev check in upload() reads `headRevisionId` BEFORE the hook fires, reproducing the race deterministically.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_writes.py::test_upload_detects_interleaved_remote_revision -v`
Expected: FAIL — no conflicted copy created; the collaborator revision is silently buried.

- [ ] **Step 3: Implement the audit + rescue**

In `upload()`, after the MD5-verify block (writes.py:303-317) and before the `if mode == "create":` cache write, add:

```python
        if mode == "update" and update_rev is not None:
            self._audit_update_rev(
                target_id, update_rev, resp.get("headRevisionId"),
                remote_path, local_path,
            )
```

Add the helpers:

```python
    def _audit_update_rev(
        self, file_id: str, expected_prev: str, new_head: str | None,
        remote_path: str, local_path: str,
    ) -> None:
        """Post-upload TOCTOU audit. Drive v3 offers no atomic write
        precondition, so a remote revision can land between our
        headRevisionId check and the update. Detect it from revision
        history and rescue the clobbered revision as a remote conflicted
        copy (Drive retains non-head revisions of binary files)."""
        client = self._client
        with convert_api_errors(remote_path=remote_path):
            revs = client._retry_transient(
                lambda: client._get_service()
                .revisions()
                .list(fileId=file_id, fields="revisions(id)", pageSize=1000)
                .execute()
            )
        client._count_request()
        ids = [r["id"] for r in revs.get("revisions", [])]
        if new_head not in ids:
            return  # history truncated/purged; nothing to audit against
        head_idx = ids.index(new_head)
        prev = ids[head_idx - 1] if head_idx > 0 else None
        if prev == expected_prev or prev is None:
            return
        logger.warning(
            "Remote revision %s of %r was committed during our upload and "
            "overwritten; rescuing it as a conflicted copy.", prev, remote_path,
        )
        self._rescue_revision(file_id, prev, remote_path)

    def _rescue_revision(
        self, file_id: str, revision_id: str, remote_path: str
    ) -> None:
        """Download *revision_id* and re-upload it as a same-folder
        ' (conflicted copy)' sibling. The change feed then delivers the
        sibling to every device, converging both edits."""
        client = self._client
        fd, tmp = tempfile.mkstemp(prefix="cumulus-rescue-")
        os.close(fd)
        try:
            with convert_api_errors(remote_path=remote_path):
                request = client._get_service().revisions().get_media(
                    fileId=file_id, revisionId=revision_id
                )
                with open(tmp, "wb") as f:
                    downloader = MediaIoBaseDownload(f, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
            client._count_request()
            stem, ext = os.path.splitext(remote_path)
            cc_path = f"{stem} (conflicted copy){ext}"
            self.upload(tmp, cc_path, write_mode=WriteMode.Add, autorename=True)
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)
```

Add the imports at the top of writes.py if missing: `import contextlib`, `import tempfile`, `from googleapiclient.http import MediaIoBaseDownload`.

> NOTE for the implementer: `tests/fake_drive.py`'s `_RevisionsResource` must support `get_media(fileId, revisionId)`; if it only supports `list`, add it returning a `_MediaRequest` over the stored revision content (store `content` in `_revision_dict`).

- [ ] **Step 4: Run the writes suite**

Run: `python -m pytest tests/offline/test_writes.py -q`
Expected: PASS, including the no-race path (audit returns early when `prev == expected_prev`, costing one `revisions.list` per rev-checked update).

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/writes.py tests/fake_drive.py tests/offline/test_writes.py
git commit -m "fix(cluster-e): audit rev-checked uploads against revision history

Drive v3 has no If-Match, so the Dropbox-era atomic rev precondition is
a check-then-write race spanning the whole upload (review F4). After an
update with update_rev, verify via revisions.list that no revision
landed in between; if one did, rescue it as a remote conflicted copy."
```

---

### Task 6: Idempotent creates via pre-generated file IDs (review F7 — retry duplicates)

`_retry_transient` re-executes `files().create` after a lost response (connection reset post-commit), creating same-name siblings (writes.py:291-300 simple upload, :663-669 make_dir, :735 whole-batch retry). Drive's idempotency mechanism is supplying a pre-generated `id` in the create body: a retried create then fails with 409, which means "already committed".

**Files:**
- Modify: `src/cumulus/client.py` — ID pool (`reserve_file_id`)
- Modify: `src/cumulus/writes.py` — use pooled IDs in all four create sites; treat 409 as success
- Modify: `tests/fake_drive.py` — `files.create` honors `body["id"]` and raises 409 on a duplicate
- Test: `tests/offline/test_writes.py`

- [ ] **Step 1: Extend FakeDrive**

In `tests/fake_drive.py` `_FilesResource.create`'s `finalize`, before inserting: if `body.get("id")` is set, use it as the new record's id instead of `_new_id()`, and if that id already exists in `self._d.files`, raise the same fake-`HttpError`-style 409 the suite's fault-injection tests construct (reuse the existing HttpError builder in test_writes.py / fake_drive.py; message `"A file already exists with the provided ID."`).

- [ ] **Step 2: Write the failing test**

```python
def test_create_retry_after_lost_response_does_not_duplicate(linked_writes, fake_drive):
    """A files.create whose response is lost after server-side commit must
    not produce a same-name sibling when retried (review F7)."""
    _bootstrap_cache(linked_writes)
    # First execute commits server-side, then the transport "dies":
    # FakeDrive applies the create, and fail_next raises afterwards.
    fake_drive.fail_after("files.create", ConnectionError("reset"))  # add hook: apply, then raise
    local = _make_local_file(b"hello")

    md = linked_writes.upload(local, "/new.txt", write_mode=WriteMode.Add)

    siblings = [r for r in fake_drive.files.values()
                if r["name"] == "new.txt" and not r["trashed"]]
    assert len(siblings) == 1, f"duplicate created on retry: {siblings}"
    assert md.id == siblings[0]["id"]
```

> NOTE for the implementer: add `fail_after(key, exc)` to FakeDrive: like `fail_next`, but the request's `finalize()` runs first, then the exception is raised — modelling "committed but response lost". A few lines in `_Request.execute`.

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/offline/test_writes.py::test_create_retry_after_lost_response_does_not_duplicate -v`
Expected: FAIL — two `new.txt` records.

- [ ] **Step 4: Implement the ID pool on the client**

In `GoogleDriveClient.__init__` add `self._id_pool: list[str] = []` and `self._id_pool_lock = threading.Lock()` (import threading if missing). Add:

```python
    def reserve_file_id(self) -> str:
        """Return a pre-generated Drive file ID for an idempotent create.

        Supplying the id in the create body makes a retried create fail
        with 409 ("already exists with the provided ID") instead of
        committing a same-name duplicate. IDs are fetched 100 at a time.
        """
        with self._id_pool_lock:
            if not self._id_pool:
                with convert_api_errors():
                    resp = self._retry_transient(
                        lambda: self._get_service()
                        .files()
                        .generateIds(count=100, space="drive")
                        .execute()
                    )
                self._count_request()
                self._id_pool = list(resp.get("ids", []))
            return self._id_pool.pop()
```

- [ ] **Step 5: Use pooled IDs at every create site in writes.py**

For the simple-upload create (writes.py:285-293), resumable create (:365-375), and `make_dir` (:658-669): add `"id": <reserved>` to the body and wrap execution:

```python
                    new_file_id = client.reserve_file_id()
                    body = {
                        "id": new_file_id,
                        "name": drive_name,
                        "parents": [parent_id],
                        "modifiedTime": modified_rfc3339,
                    }
                    request = client._get_service().files().create(
                        body=body, media_body=media, fields=fields,
                    )
            ...
                try:
                    resp = client._retry_transient(request.execute)
                except HttpError as exc:
                    if exc.resp.status == 409:
                        # A previous attempt committed; fetch its metadata.
                        resp = client._retry_transient(
                            lambda: client._get_service().files()
                            .get(fileId=new_file_id, fields=fields)
                            .execute()
                        )
                    else:
                        raise
```

(`from googleapiclient.errors import HttpError` at the top of writes.py if missing; same pattern for make_dir with its `fields="id,name,mimeType"`; for the resumable path apply the 409 fallback around the `next_chunk` loop's session initiation — a 409 there also means a previous attempt committed.)

For `make_dir_batch` (:714-731): reserve one id per sub-request body; in `_make_dir_batch_callback`, when `exception` is an HttpError with status 409, perform the same `files().get(fileId=...)` fetch and fill the slot as success instead of an error.

- [ ] **Step 6: Run the writes suite**

Run: `python -m pytest tests/offline/test_writes.py tests/offline/test_fake_drive.py -q`
Expected: PASS (FakeDrive already exposes `files.generateIds`).

- [ ] **Step 7: Commit**

```bash
git add src/cumulus/client.py src/cumulus/writes.py tests/fake_drive.py tests/offline/test_writes.py
git commit -m "fix(cluster-e): make Drive creates idempotent via pre-generated IDs

Retrying files.create after a lost response duplicated files/folders
(Drive allows same-name siblings; review F7). Creates now carry a
pooled generateIds id; a 409 on retry means the first attempt
committed and resolves to its metadata instead of a duplicate."
```

---

### Task 7: make_dir conflict probe — revive the folder-merge path (review F11)

`make_dir` never checks for an existing same-name sibling (writes.py:646-684), so `FolderConflictError` has zero raise sites and sync.py's merge branch (sync.py:2598-2609, "folder already exists → index it") is dead code; two devices creating the same folder produce server-side duplicates. (Run after Task 6 — touches the same function.)

**Files:**
- Modify: `src/cumulus/writes.py` (`make_dir`)
- Test: `tests/offline/test_writes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_make_dir_existing_remote_folder_raises_conflict(linked_writes, fake_drive):
    """make_dir for a folder that already exists on Drive (created by
    another device; cache may be stale) must raise FolderConflictError so
    the engine merges instead of duplicating (review F11)."""
    fake_drive.add_folder("/Docs")
    _bootstrap_cache(linked_writes)
    # Simulate a stale cache: forget the folder locally, like a second
    # device that hasn't polled yet.
    linked_writes._client._path_cache.remove("/Docs")

    with pytest.raises(FolderConflictError):
        linked_writes.make_dir("/Docs")

    folders = [r for r in fake_drive.files.values()
               if r["name"] == "Docs" and not r["trashed"]]
    assert len(folders) == 1, "duplicate folder created"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_writes.py::test_make_dir_existing_remote_folder_raises_conflict -v`
Expected: FAIL — second `Docs` folder created.

- [ ] **Step 3: Implement the live probe**

In `make_dir`, after `parent_id = self._resolve_parent_id(...)` (writes.py:656) and only when `autorename` is False, insert:

```python
        if not autorename:
            escaped = drive_name.replace("\\", "\\\\").replace("'", "\\'")
            with convert_api_errors(remote_path=remote_path):
                probe = client._retry_transient(
                    lambda: client._get_service()
                    .files()
                    .list(
                        q=(
                            f"name = '{escaped}' and '{parent_id}' in parents "
                            f"and mimeType = '{_FOLDER_MIME}' and trashed = false"
                        ),
                        fields="files(id, name)",
                        pageSize=1,
                    )
                    .execute()
                )
            client._count_request()
            hits = probe.get("files", [])
            if hits:
                # Adopt the existing folder into the cache so follow-up
                # operations (get_metadata in the engine's merge branch)
                # resolve it.
                client._path_cache.set(
                    remote_path, hits[0]["id"], parent_id=parent_id,
                    name=hits[0]["name"], is_folder=True,
                )
                raise FolderConflictError(
                    "Folder already exists",
                    f"A folder already exists at {remote_path!r} on Google "
                    "Drive; it will be merged with the local folder.",
                    remote_path=remote_path,
                )
```

Import `FolderConflictError` from `.exceptions` at the top of writes.py. Also change the `path_cache.set(...)` after the create (writes.py:671-677) to log a warning when it returns `False`, mirroring `move` (writes.py:637-643).

- [ ] **Step 4: Run the writes + sync suites**

Run: `python -m pytest tests/offline/test_writes.py tests/offline/test_sync.py -q`
Expected: PASS — sync.py:2598's `except FolderConflictError` branch is now reachable and indexes the adopted folder via `get_metadata`.

- [ ] **Step 5: Commit**

```bash
git add src/cumulus/writes.py tests/offline/test_writes.py
git commit -m "fix(cluster-e): probe Drive before make_dir and raise FolderConflictError

make_dir blindly created same-name sibling folders (legal on Drive),
leaving sync.py's already-exists merge branch dead and producing
server-side duplicates in two-device races (review F11)."
```

---

### Task 8: Map an expired change token to CursorResetError and auto-recover (review F5)

A stale Drive `pageToken` (long offline period) maps 400→`BadInputError`, which manager.py:566's generic handler turns into a permanent stop with "Cumulus made an invalid request". `CursorResetError` (exceptions.py:224) is imported but never raised.

**Files:**
- Modify: `src/cumulus/changes.py` (wrap the changes.list call), `src/cumulus/client.py` (`wait_for_remote_changes`), `src/cumulus/manager.py` (`_handle_sync_thread_errors`)
- Test: `tests/offline/test_changes.py`, `tests/offline/test_manager.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/offline/test_changes.py
def test_invalid_page_token_raises_cursor_reset(path_cache):
    class _Svc:
        def changes(self): return self
        def list(self, **kw): return self
        def execute(self):
            raise make_http_error(400, "badRequest")  # reuse the suite's HttpError builder
    with pytest.raises(CursorResetError):
        list(iter_remote_changes(lambda: _Svc(), lambda c: c(), path_cache, "stale"))
```

```python
# tests/offline/test_manager.py
def test_cursor_reset_triggers_reindex_and_autostart(manager):
    """CursorResetError from the download worker must reset sync state and
    schedule a restart, not stop permanently (review F5)."""
    with manager._handle_sync_thread_errors(running=Event(), autostart=manager.autostart):
        raise CursorResetError("Sync cursor reset", "token expired")
    assert manager.sync.remote_cursor == ""        # full re-index scheduled
    assert manager.autostart.is_set()              # sync will resume
```

> Match the existing manager fixture in tests/offline/test_manager.py; if `reset_sync_state` clears more than `remote_cursor`, assert on whatever it observably resets (see manager.rebuild_index, manager.py:350).

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_changes.py::test_invalid_page_token_raises_cursor_reset tests/offline/test_manager.py::test_cursor_reset_triggers_reindex_and_autostart -v`
Expected: both FAIL (BadInputError / permanent stop).

- [ ] **Step 3: Raise CursorResetError at the feed call sites**

In `changes.py`, add imports `from googleapiclient.errors import HttpError` and `from .exceptions import CursorResetError`, and wrap the `retry(...)` changes.list call (the Phase-1 loop from Task 4):

```python
        try:
            resp = retry(
                lambda c=cursor: service_factory()
                .changes()
                .list(
                    pageToken=c,
                    fields=_CHANGE_FIELDS,
                    pageSize=_CHANGE_PAGE_SIZE,
                    includeRemoved=True,
                )
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status in (400, 404):
                raise CursorResetError(
                    "Sync cursor reset",
                    "The saved Google Drive change token is no longer "
                    "valid (Cumulus was offline too long). The index "
                    "will be rebuilt.",
                ) from exc
            raise
```

Apply the identical except-clause to the `changes().list` call in `client.wait_for_remote_changes` (client.py:1105-1115) — note that call is inside `convert_api_errors()`, which passes non-HttpError exceptions through unchanged, so the `CursorResetError` must be raised from an inner try/except around `self._retry_transient(...)`.

- [ ] **Step 4: Add the manager recovery branch**

In `_handle_sync_thread_errors` (manager.py:551-572), before the generic `except Exception`:

```python
        except CursorResetError:
            self._logger.warning(
                "Google Drive change token expired; rebuilding the index."
            )
            if self.desktop_notifier:
                self.desktop_notifier.notify(
                    "Rebuilding index",
                    "Cumulus was offline too long and will re-index "
                    "Google Drive. Sync resumes automatically.",
                    level=notify.INFO,
                )
            self.sync.reset_sync_state()
            self.stop()
            self.autostart.set()
```

Import `CursorResetError` in manager.py. (`reset_sync_state` is the same call `rebuild_index` uses, manager.py:350; an empty `remote_cursor` makes the next `download_sync_cycle` run the full bootstrap, sync.py:2959.)

- [ ] **Step 5: Run the suites and commit**

Run: `python -m pytest tests/offline/test_changes.py tests/offline/test_manager.py -q`
Expected: PASS.

```bash
git add src/cumulus/changes.py src/cumulus/client.py src/cumulus/manager.py tests/offline/test_changes.py tests/offline/test_manager.py
git commit -m "fix(cluster-e): map expired Drive change tokens to CursorResetError

A stale pageToken surfaced as BadInputError and stopped sync
permanently with a misleading message; CursorResetError was imported
but never raised (review F5). The manager now resets sync state and
auto-resumes with a full re-index, restoring the old maestral cursor-
reset semantics with better UX."
```

---

### Task 9: Retire dead Dropbox-era except clauses; re-download on delete conflict (review F9)

`client.remove(parent_rev=...)` raises `DataChangedError` (writes.py:423), but `_on_local_deleted` catches `PathError` (sync.py:2731) — dead — so the conflict escapes as a recorded Up error, the index entry survives, and the failing delete replays on every restart. `_on_local_moved`'s `except (NotFoundError, FileConflictError)` (sync.py:2421) has a dead second member for the same reason.

**Files:**
- Modify: `src/cumulus/sync.py:2719-2741` (`_on_local_deleted`), `:2421` (`_on_local_moved`)
- Test: `tests/offline/test_sync.py`

- [ ] **Step 1: Write the failing test**

```python
def test_local_delete_of_remotely_changed_file_redownloads(sync_engine_with_fake, fake_drive):
    """Deleting locally while the remote changed must skip the remote
    delete, drop the index entry, and restore the newer remote file —
    not loop a failing delete on every restart (review F9)."""
    sync = sync_engine_with_fake
    fid = fake_drive.add_file("/f.txt", content=b"new remote content")
    new_rev = fake_drive.files[fid]["headRevisionId"]
    _bootstrap_cache_for(sync)
    _seed_index_entry(sync, "/f.txt", remote_id=fid, rev="rev-stale")

    event = _local_deleted_event(sync, "/f.txt")  # build per existing patterns
    sync._create_remote_entry(event)

    # Remote file NOT deleted, no lingering Up error, index reflects the
    # re-downloaded file, local copy restored.
    assert not fake_drive.files[fid]["trashed"]
    assert sync.sync_errors_for_path("/f.txt", direction=SyncDirection.Up) == []
    assert osp.exists(sync.to_local_path_from_cased("/f.txt"))
    entry = sync.get_index_entry("/f.txt")
    assert entry is not None and entry.rev == new_rev
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_sync.py::test_local_delete_of_remotely_changed_file_redownloads -v`
Expected: FAIL — DataChangedError recorded as an Up error.

- [ ] **Step 3: Fix `_on_local_deleted`**

Replace the try/except at sync.py:2719-2741 with:

```python
        redownload = False
        try:
            # Will only perform delete if remote rev matches `local_rev`.
            self.client.remove(
                event.remote_path, parent_rev=local_rev if event.is_file else None
            )
            status = SyncStatus.Done
        except NotFoundError:
            self._logger.debug(
                'Could not delete "%s": the item no longer exists on remote',
                event.remote_path,
            )
            status = SyncStatus.Skipped
        except DataChangedError:
            self._logger.debug(
                'Could not delete "%s": it changed on Drive since last '
                "sync; re-downloading instead",
                event.remote_path,
            )
            status = SyncStatus.Skipped
            redownload = True

        # Remove revision metadata.
        self.remove_node_from_index(event.remote_path_lower)

        if redownload:
            # Restore the newer remote item locally. With the index entry
            # gone, the metadata classifies as Added and downloads fresh —
            # this also stops the startup reconciler from replaying the
            # failed delete forever.
            try:
                md = self.client.get_metadata(event.remote_path)
            except NotFoundError:
                md = None
            if md is not None:
                self._create_local_entry(SyncEvent.from_metadata(md, self))

        return status
```

(`DataChangedError` is already imported in sync.py via the exceptions import block — verify, add if missing.)

- [ ] **Step 4: Fix `_on_local_moved`**

At sync.py:2421 replace `except (NotFoundError, FileConflictError):` with `except (NotFoundError, DataChangedError):` — the destination file changed remotely since last sync, so we must not trash it; the subsequent `move(..., autorename=True)` then conflict-renames around it, matching the Dropbox-era intent.

- [ ] **Step 5: Run the sync suite and commit**

Run: `python -m pytest tests/offline/test_sync.py -q`
Expected: PASS.

```bash
git add src/cumulus/sync.py tests/offline/test_sync.py
git commit -m "fix(cluster-e): catch DataChangedError where PathError/FileConflictError are dead

client.remove raises DataChangedError on a rev mismatch, but
_on_local_deleted caught the Dropbox-era PathError (unreachable) so the
conflict became a permanently replaying Up error; the newer remote file
is now re-downloaded instead (review F9). Same fix for the dead
FileConflictError clause in _on_local_moved."
```

---

### Task 10: Match .mignore and selective-sync exclusions against decoded paths (review F8)

`encode_name` percent-quotes everything outside `[A-Za-z0-9_.~- ]`+space (path_codec.py:40), but mignore patterns are matched against the ENCODED `event.remote_path` (sync.py:1504-1509) and `excluded_items` are stored as `normalize()` of the raw user path (sync.py:705) while compared against `fold_path` output (sync.py:1481-1488) — so any name with non-ASCII or `( ) & + , ' #` silently fails both mechanisms.

Key identity that makes this clean: `decode_path(fold_path(x))` is exactly the per-segment `NFC(casefold(NFC(name)))` of the decoded path — a quoting-free canonical "human fold" form.

**Files:**
- Modify: `src/cumulus/path_codec.py` — add `fold_human`
- Modify: `src/cumulus/sync.py` — `_is_mignore_path`, `clean_excluded_items_list`, `is_excluded_by_user`; audit remaining `normalize()`-on-remote-path sites (sync.py:1603, 2210-2211)
- Test: `tests/offline/test_path_codec.py`, `tests/offline/test_sync.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/offline/test_path_codec.py
def test_fold_human_matches_decoded_fold_path():
    for name in ["Ördner", "Report (1).txt", "ΣΣΣ", "İstanbul", "a%b", "ümlaut ß"]:
        encoded = encode_path("/" + name)
        assert decode_path(fold_path(encoded)) == fold_human("/" + name)

def test_fold_human_idempotent():
    assert fold_human(fold_human("/Ördner/Straße")) == fold_human("/Ördner/Straße")
```

```python
# tests/offline/test_sync.py
def test_mignore_matches_non_ascii_names(sync_engine):
    sync_engine._mignore_rules = sync_engine._load_mignore_rules_form_str("Ördner/\n*.tmp\n")
    event = _local_dir_event(sync_engine, "/Ördner")  # build per existing helpers
    assert sync_engine.is_mignore(event)

def test_excluded_items_match_non_ascii_names(sync_engine):
    sync_engine.excluded_items = ["/Ördner"]
    assert sync_engine.is_excluded_by_user(fold_path(encode_path("/Ördner")))
    assert sync_engine.is_excluded_by_user(fold_path(encode_path("/Ördner/inner.txt")))
    assert not sync_engine.is_excluded_by_user(fold_path(encode_path("/Other")))
```

> Match the actual mignore-rules loader name (grep `mignore_rules` in sync.py) and the dir-event helper to the module's existing patterns.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_path_codec.py tests/offline/test_sync.py -k "fold_human or mignore_matches_non_ascii or excluded_items_match" -v`
Expected: codec tests FAIL on missing `fold_human`; sync tests FAIL on non-matching encoded forms.

- [ ] **Step 3: Add `fold_human` to path_codec.py**

```python
def fold_human(path: str) -> str:
    """Quoting-free canonical fold of a DECODED path: per segment,
    ``NFC(casefold(NFC(segment)))``. Identity:
    ``decode_path(fold_path(encode_path(p))) == fold_human(p)``, so this
    is the form to compare user-supplied paths (mignore patterns,
    selective-sync exclusions) against engine fold-form paths after
    :func:`decode_path`. Idempotent."""
    if path in ("", "/"):
        return path

    def _fold(seg: str) -> str:
        if not seg:
            return seg
        n = unicodedata.normalize("NFC", seg)
        return unicodedata.normalize("NFC", n.casefold())

    return "/".join(_fold(p) for p in path.split("/"))
```

Add `"fold_human"` to `__all__`.

- [ ] **Step 4: Fix the three sync.py mechanisms**

`_is_mignore_path` (sync.py:1504-1509) — match the decoded path:

```python
    def _is_mignore_path(self, remote_path: str, is_dir: bool = False) -> bool:
        # remote_path is the ENCODED virtual path; users write .mignore
        # patterns against the human-readable (decoded) form.
        relative_path = decode_path(remote_path).lstrip("/")
        if is_dir:
            relative_path = f"{relative_path}/"
        return self.mignore_rules.match_file(relative_path)
```

`clean_excluded_items_list` (sync.py:695-711) — canonicalize to `fold_human`:

```python
        # Remove duplicate entries by creating set, strip trailing '/'.
        # fold_human is idempotent, so re-cleaning stored entries is safe
        # (existing configs hold normalize()-form entries; fold_human
        # subsumes that form).
        remote_paths_lower = {fold_human(f).rstrip("/") for f in remote_paths}
```

`is_excluded_by_user` (sync.py:1481-1488) — decode the engine form before comparing:

```python
    def is_excluded_by_user(self, remote_path_lower: str) -> bool:
        decoded = decode_path(remote_path_lower)
        return any(is_equal_or_child(decoded, p) for p in self.excluded_items)
```

Imports: `from .path_codec import decode_path, fold_human` (extend the existing path_codec import line in sync.py).

- [ ] **Step 5: Audit the remaining `normalize()`-on-remote-path sites**

Run: `grep -n "normalize(" src/cumulus/sync.py src/cumulus/main.py src/cumulus/cli/*.py`
Expected hits include sync.py:1603 and sync.py:2210-2211 (per the review). For each hit where the operand is a REMOTE path that will be compared against `remote_path_lower`/`excluded_items`: replace `normalize(p)` with `fold_human(p)` when `p` is decoded user input, or `fold_path(p)` when `p` is an encoded virtual path. Where the operand is a LOCAL filesystem path, leave `normalize()` alone. Record each decision in the deviations file.

- [ ] **Step 6: Run the full offline suite and commit**

Run: `python -m pytest tests/offline -q`
Expected: PASS.

```bash
git add src/cumulus/path_codec.py src/cumulus/sync.py tests/offline/test_path_codec.py tests/offline/test_sync.py
git commit -m "fix(cluster-e): match mignore/excluded items against decoded paths

Patterns and exclusions were compared against percent-encoded virtual
paths, so any name with non-ASCII or common punctuation silently
failed to be ignored/excluded (review F8). Adds path_codec.fold_human
(= decode_path . fold_path) as the canonical user-path fold form."
```

---

### Task 11: Internal lock for PathIDCache (review F10 — RPC threads bypass sync_lock)

RPC methods (main.py get_metadata:817, list_folder:850, list_revisions:929) reach `path_to_id` from Pyro worker threads with no lock, on one shared sqlite connection (`check_same_thread=False`, main.py:206-208) whose `Database._in_transaction` flag is unprotected — a read during `populate_from_listing`'s clear+insert can see a half-built cache or break the atomic swap.

**Files:**
- Modify: `src/cumulus/path_cache.py`
- Test: `tests/offline/test_path_cache.py`

- [ ] **Step 1: Write the failing (flaky-by-design, made deterministic) test**

```python
def test_concurrent_reads_during_populate_are_safe(tmp_path):
    """path_to_id from another thread during populate_from_listing must
    see either the old or the new cache — never a half-built one, and
    never raise (review F10)."""
    import sqlite3, threading
    from cumulus.database.core import Database
    db = Database(sqlite3.connect(tmp_path / "c.db", check_same_thread=False))
    cache = PathIDCache(db)
    cache.root_id = "root"
    cache.set("/seed.txt", "id-seed", "root", "seed.txt", False)

    listing = [
        {"id": f"id-{i}", "name": f"f{i}.txt", "parents": ["root"],
         "mimeType": "text/plain"}
        for i in range(500)
    ]
    errors: list[Exception] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                got = cache.path_to_id("/f0.txt")
                assert got in (None, "id-0")
            except Exception as e:  # noqa: BLE001 — collecting for assertion
                errors.append(e)
                return

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(20):
        cache.populate_from_listing(listing)
    stop.set()
    for t in threads:
        t.join()
    assert errors == [], errors
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_path_cache.py::test_concurrent_reads_during_populate_are_safe -v`
Expected: FAIL or error (sqlite transaction-state corruption / inconsistent reads). If it passes flakily, raise the populate iteration count.

- [ ] **Step 3: Add the lock**

In `PathIDCache.__init__`: `self._lock = threading.RLock()` (add `import threading`). Wrap the body of every public method — `path_to_id`, `id_to_path`, `get_entry`, `has_case_conflict`, `set`, `remove`, `move`, `resolve_path`, `populate_from_listing`, `clear`, `count` — in `with self._lock:`. `populate_from_listing` holds it across the whole clear+insert transaction, so concurrent readers block briefly during bootstrap instead of seeing the half-built cache. Update the module docstring's Concurrency bullet (path_cache.py:18-20): the cache is now internally locked; `sync_lock` is no longer the only guard.

- [ ] **Step 4: Run the cache suite and commit**

Run: `python -m pytest tests/offline/test_path_cache.py -q`
Expected: PASS.

```bash
git add src/cumulus/path_cache.py tests/offline/test_path_cache.py
git commit -m "fix(cluster-e): serialize PathIDCache access with an internal lock

Daemon RPC threads (get_metadata, list_folder) read the cache without
sync_lock on a shared check_same_thread=False sqlite connection,
racing populate_from_listing's atomic swap (review F10)."
```

---

### Task 12: Longpoll gate must respect nextPageToken (review F12)

`wait_for_remote_changes` (client.py:1105-1117) gates the whole download cycle on `len(changes) > 0` of one `pageSize=1` page whose fields mask strips `nextPageToken` — an empty-but-non-final page reads as "no changes" forever, since the cursor only advances inside `download_sync_cycle`.

**Files:**
- Modify: `src/cumulus/client.py:1105-1117`
- Test: `tests/offline/test_client.py` (or wherever `wait_for_remote_changes` is tested — grep first)

- [ ] **Step 1: Write the failing test**

```python
def test_wait_for_remote_changes_true_on_empty_page_with_next_token(linked_client):
    """Drive may return an empty changes page WITH a nextPageToken (elided
    entries); that still means 'changes pending' (review F12)."""
    svc = Mock()
    svc.changes.return_value.list.return_value.execute.return_value = {
        "changes": [], "nextPageToken": "t2",
    }
    linked_client._get_service = Mock(return_value=svc)
    assert linked_client.wait_for_remote_changes("t1", timeout=0) is True
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline -k wait_for_remote_changes_true_on_empty_page -v`
Expected: FAIL — returns False.

- [ ] **Step 3: Fix the fields mask and the return**

In `wait_for_remote_changes`, change the list call and return (client.py:1109-1117):

```python
                .list(
                    pageToken=last_cursor,
                    pageSize=1,
                    fields="nextPageToken, changes(fileId)",
                )
                .execute()
            )
        self._count_request()
        return bool(resp.get("changes")) or bool(resp.get("nextPageToken"))
```

- [ ] **Step 4: Run and commit**

Run: `python -m pytest tests/offline -k wait_for_remote_changes -q`
Expected: PASS.

```bash
git add src/cumulus/client.py tests/offline
git commit -m "fix(cluster-e): treat an empty changes page with nextPageToken as pending changes

The poll gate stripped nextPageToken from the fields mask and returned
False on an empty first page, which can mask pending changes
indefinitely since the cursor only advances when the download cycle
runs (review F12)."
```

---

### Task 13: Auth edge cases (review F13)

Three verified edges: (1) `link(access_token=...)` returns 0 but `linked` stays False (refresh_token required), leaving a "linked but unusable" client — upstream maestral's access-token link produced a usable short-lived session; (2) `link(code=...)` only catches `RefreshError`/connection errors, so a mistyped pasted code raises a raw `oauthlib` traceback in the `--no-local-server` flow; (3) unlinked `get_account_info` passes `credentials=None` into `build()`, triggering `google.auth.default()` instead of `NotLinkedError`.

**Files:**
- Modify: `src/cumulus/client.py` (`linked`, `link`, `get_account_info`)
- Test: `tests/offline/test_client_auth.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_access_token_link_yields_usable_client(client, monkeypatch):
    """link(access_token=...) returning 0 must leave a client whose
    `linked` is True for the session (short-lived, like upstream
    maestral), not a 'linked but unusable' state (review F13.1)."""
    monkeypatch.setattr(
        client, "_fetch_account_info_for_creds", Mock(return_value=FAKE_ACCOUNT)
    )
    assert client.link(access_token="ya29.test") == 0
    assert client.linked is True


def test_link_with_bad_code_returns_invalid_not_traceback(client):
    """An invalid/expired pasted auth code must return 1 (review F13.2)."""
    from oauthlib.oauth2.rfc6749.errors import InvalidGrantError
    flow = Mock()
    flow.fetch_token.side_effect = InvalidGrantError()
    client._pending_paste_flow = _PasteFlowState(
        flow=flow, created_at=datetime.utcnow()
    )
    assert client.link(code="bad") == 1


def test_get_account_info_unlinked_raises_not_linked(client):
    """No credentials must raise NotLinkedError, never fall through to
    google.auth.default() / ADC (review F13.3)."""
    client._credentials = None
    client._cached_account_info = None
    with pytest.raises(NotLinkedError):
        client.get_account_info()
```

> Reuse the unlinked-`client` fixture and `FAKE_ACCOUNT`-style constants already present in tests/offline/test_client_auth.py (the access-token semantics test at ~line 222 documents the old behavior — update it to the new contract in this task).

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/offline/test_client_auth.py -k "access_token_link_yields_usable or bad_code_returns_invalid or unlinked_raises_not_linked" -v`
Expected: all three FAIL.

- [ ] **Step 3: Implement**

(a) `linked` (client.py:275-288) — accept an in-process access-token-only credential; the from-storage path still requires a refresh token:

```python
        return self._credentials is not None and (
            self._credentials.refresh_token is not None
            or self._credentials.token is not None
        )
```

(b) `link` (client.py:344-347) — catch the oauthlib family:

```python
        except RefreshError:
            return 1
        except OAuth2Error:
            # Bad / expired / already-used authorization code from the
            # paste flow (InvalidGrantError etc.).
            return 1
        except CONNECTION_ERRORS:
            return 2
```

with `from oauthlib.oauth2.rfc6749.errors import OAuth2Error` at the top of client.py.

(c) `get_account_info` (client.py:482-490) — guard before building:

```python
        if self._cached_account_info is None:
            if self._credentials is None and not self.linked:
                raise NotLinkedError(
                    "Not linked",
                    "Run 'cumulus auth link' to authorize Cumulus.",
                )
            self._cached_account_info = self._fetch_account_info_for_creds(
                self._credentials
            )
        return self._cached_account_info
```

(The `not self.linked` second clause lets `linked`'s lazy credential-loading populate `self._credentials` from storage first.)

- [ ] **Step 4: Run the auth suites and commit**

Run: `python -m pytest tests/offline/test_client_auth.py tests/offline/test_oauth.py tests/offline/test_errorhandling_auth.py -q`
Expected: PASS (update the old access-token-semantics test to the new contract; note the change in the deviations file — the Stage-2 spec §5.1 "functionally not linked" decision is superseded here to restore the `link()==0 ⇒ usable` contract).

```bash
git add src/cumulus/client.py tests/offline/test_client_auth.py
git commit -m "fix(cluster-e): close three auth edges

- link(access_token=...) now yields a usable (short-lived) session
  instead of returning success while `linked` stays False
- a bad/expired pasted auth code returns 1 instead of an uncaught
  oauthlib traceback in the --no-local-server flow
- unlinked get_account_info raises NotLinkedError instead of falling
  through to google.auth.default() / ambient ADC credentials
(review F13)"
```

---

### Task 14: Correct the quota model (review F14)

`_DAILY_QUOTA = 12_000` (client.py:96-106) treats Drive's default per-user limit of 12,000 queries **per 60 seconds** as per-**day**; the 80%/95% warnings fire on a false premise and the `DEFAULT_POLL_INTERVAL` comment justifies itself with wrong math. Rate limiting is already handled correctly by `_retry_transient`'s 429/403 backoff.

**Files:**
- Modify: `src/cumulus/client.py:96-106` and the `_count_request` warning block (~250-266)
- Test: `tests/offline/test_client.py` (only if an existing test asserts the warning — grep `_QUOTA_WARN` first)

- [ ] **Step 1: Remove the false-premise warning machinery**

Delete `_DAILY_QUOTA`, `_QUOTA_WARN_THRESHOLD`, `_QUOTA_CRITICAL_THRESHOLD` and the threshold-warning block inside `_count_request` (keep the counter itself and the `request_count` property — they feed status output). Replace the comment block with:

```python
# Google Drive's default per-user rate limit is 12,000 queries per
# 60 seconds (not per day). Transient 429 / rate-limit-403 responses
# are handled by _retry_transient's backoff; no client-side budget is
# needed. The request counter below is kept for status/diagnostics.
```

Update the `DEFAULT_POLL_INTERVAL` comment:

```python
# Default polling interval for remote change detection (seconds).
# Dropbox used a long-poll; Google Drive has no equivalent, so we poll.
# 180 s is a latency/courtesy tradeoff — the per-user rate limit
# (12k queries / 60 s) is nowhere near a constraint at this rate.
DEFAULT_POLL_INTERVAL = 180
```

- [ ] **Step 2: Fix any references**

Run: `grep -rn "_DAILY_QUOTA\|_QUOTA_WARN\|_QUOTA_CRITICAL\|12,000\|12k/day\|per day" src/cumulus tests/offline`
Expected: only the sites just edited; fix any test that asserted the old warning.

- [ ] **Step 3: Run the client suite and commit**

Run: `python -m pytest tests/offline -q`
Expected: PASS.

```bash
git add src/cumulus/client.py
git commit -m "fix(cluster-e): drop the per-day quota warnings built on wrong math

Drive's 12,000-query default limit is per 60 seconds per user, not per
day; the 80%/95% 'daily quota' warnings fired on a false premise
(review F14). Rate limiting remains handled by the 429/403 backoff."
```

---

### Task 15: Final gate

- [ ] **Step 1: Full offline suite** (⚠️ unlink any real smoke-test account first — keyring collision)

Run: `python -m pytest tests/offline -q`
Expected: all PASS (was 470 passing pre-plan; expect ~490+ with the new tests).

- [ ] **Step 2: Lint/type gate**

Run the repo's configured checks (see pyproject.toml / pre-commit config): `pre-commit run --all-files` if configured, else `python -m mypy src/cumulus` and `python -m ruff check src/cumulus` as available.
Expected: clean, or no new violations vs. main.

- [ ] **Step 3: Linked-suite spot checks (real account, tmux, one at a time)**

Run, per the cluster-A/BC harness conventions (isolated debug log, `--runxfail` not needed — these are new behaviors):
- `tests/linked/integration/test_sync.py::test_move_folder_remote` (or the nearest remote-rename test) — exercises Task 3 live.
- `tests/linked/integration/test_sync.py::test_excluded_folder` family — exercises Task 10 live.
- A manual stale-cursor drill for Task 8: stop the daemon, set `remote_cursor` state to a garbage token, start, confirm the warning + automatic re-index instead of a permanent stop.

- [ ] **Step 4: Write the deviations file**

Create `docs/superpowers/plans/2026-06-10-cluster-e-review-remediation-deviations.md` recording every deviation from this plan (FakeDrive API mismatches, the Task 10 normalize() audit decisions, the Task 13 Stage-2-spec supersession), then commit:

```bash
git add docs/superpowers/plans/2026-06-10-cluster-e-review-remediation-deviations.md
git commit -m "docs(cluster-e): execution notes and deviations"
```
