# Cluster D — Drive Behavioral Acceptance (test + docs) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the two Drive-behavioral xfails by accepting Google Drive's native behavior — rewrite the tests to assert the *correct Drive outcome* (not the Dropbox outcome) and document the difference — rather than forcing Dropbox semantics onto the engine.

**Architecture:** No production code changes. These two tests fail because they encode Dropbox-specific behavior that Drive does not have:
- `test_unicode_decomposed` (test_sync.py:946) assumes the server normalizes decomposed (NFD) unicode names to composed (NFC) and mirrors the rename locally. Drive does **not** normalize unicode names — an NFD name persists as NFD.
- `test_long_path_error` (test_sync.py:1235) builds a too-long path as a single flat slash-containing key and expects a remote path-length error. Drive imposes **no** remote path-length limit and requires each parent folder to exist (no auto-parent-creation); the only real limit is the local filesystem.

The decision (user-approved, option b): assert Drive's actual behavior, drop the "bug" framing, and record both in [dropbox-vs-drive-differences.md](../references/dropbox-vs-drive-differences.md) (§5 unicode normalization, §6 remote path length — already added).

**Tech Stack:** Python, pytest, linked real-account suite (tests/linked/integration).

---

## File structure

- **Modify** `tests/linked/integration/test_sync.py` — rewrite `test_unicode_decomposed` and `test_long_path_error`; remove both xfail markers.
- **Reference** `docs/superpowers/references/dropbox-vs-drive-differences.md` — already documents §5 and §6; add a back-reference comment from each test.

---

### Task 1: Reframe `test_unicode_decomposed` to assert NFD persistence

**Files:**
- Modify: `tests/linked/integration/test_sync.py:945-988`

Drive preserves the decomposed (NFD) name. On a non-normalizing filesystem (Linux), the NFD file therefore stays at its NFD path and the NFC path never appears; the file round-trips consistently with no sync error. macOS normalizes at the filesystem layer, so the existing Darwin branch (file stays, `samefile` holds) is already correct and stays.

- [ ] **Step 1: Remove the xfail marker** at line 946.

- [ ] **Step 2: Rewrite the non-Darwin assertions to expect NFD persistence**

```python
@requires_account
def test_unicode_decomposed(m: Maestral) -> None:
    """A file with decomposed (NFD) unicode characters round-trips as NFD.

    Google Drive does not normalize unicode file names (unlike Dropbox, which
    re-composed NFD -> NFC). The decomposed name therefore persists end-to-end.
    See docs/superpowers/references/dropbox-vs-drive-differences.md §5.
    """
    file_name = b"fo\xcc\x81lder".decode()  # decomposed ó (NFD)
    local_path = f"{m.sync_path}/{file_name}"
    local_path_normalized = normalize_unicode(local_path)  # NFC form

    with open(local_path, "a") as f:
        f.write("content")

    wait_for_idle(m)

    if platform.system() == "Darwin":
        # macOS normalizes unicode at the filesystem layer; either spelling
        # resolves to the same file.
        assert osp.exists(local_path)
        assert osp.samefile(local_path, local_path_normalized)
    else:
        # Drive preserves NFD: the decomposed name stays, the NFC name is absent.
        assert osp.exists(local_path)
        assert not osp.exists(local_path_normalized)

    assert_no_errors(m)
    assert_synced(m)

    # Rename: the decomposed name still persists as decomposed after a rename.
    target_path = local_path + "_target"
    target_path_normalized = normalize_unicode(target_path)
    os.rename(local_path, target_path)
    wait_for_idle(m)

    if platform.system() == "Darwin":
        assert osp.exists(target_path)
        assert osp.samefile(target_path, target_path_normalized)
    else:
        assert osp.exists(target_path)
        assert not osp.exists(target_path_normalized)

    assert_no_errors(m)
    assert_synced(m)
```

> Note the rename now moves `local_path` (the NFD file that actually exists), not `local_path_normalized` (which the old test assumed had been created by an NFC re-mirror).

- [ ] **Step 3: Run against the real account**

Set `CUMULUS_TEST_REFRESH_TOKEN` (re-mint if expired). Not concurrent with `tests/offline` (keyring collision).
Run: `python -m pytest "tests/linked/integration/test_sync.py::test_unicode_decomposed" -q`
Expected: PASS — NFD name persists, `assert_synced` holds, no errors.

- [ ] **Step 4: Commit**

```bash
git add tests/linked/integration/test_sync.py
git commit -m "test(cluster-d): assert Drive preserves NFD unicode names (no NFC normalization)"
```

---

### Task 2: Reframe `test_long_path_error` for Drive's nested-parent model

**Files:**
- Modify: `tests/linked/integration/test_sync.py:1234-1256`

The test's intent — a remote path too long for the *local* filesystem surfaces a `PathError` on download — is still valid on Drive (Drive has no remote limit, but the local FS does). The fix is to build the deep path as genuinely nested folders (each parent created) instead of one flat slash-containing name, so it exercises the local-path-too-long download path the way Drive actually delivers it.

- [ ] **Step 1: Remove the xfail marker** at line 1235.

- [ ] **Step 2: Rewrite to create nested parents**

```python
@requires_account
def test_long_path_error(m: Maestral) -> None:
    """A remote item whose path exceeds the LOCAL filesystem limit surfaces a
    PathError on download.

    Google Drive imposes no remote path-length limit and does not auto-create
    intermediate parents, so the deep path must be built as nested folders (one
    parent at a time). See docs/superpowers/references/dropbox-vs-drive-differences.md §6.
    """
    max_path_length, _ = fs_max_lengths_for_path()

    # Build a nested folder chain whose cumulative LOCAL path exceeds the limit.
    # Each segment is a real Drive folder; the deepest holds the file.
    segment = "nested"
    depth = (max_path_length // (len(segment) + 1)) + 1
    nested_tree: DirTreeType = {"file.txt": "content"}
    for _ in range(depth):
        nested_tree = {segment: nested_tree}

    create_remote_tree(m, nested_tree)  # creates each parent folder, then the file

    wait_for_idle(m)

    sync_errors = m.sync_errors

    assert len(sync_errors) > 0
    assert sync_errors[-1].type == "PathError"
    assert sync_errors[-1].direction == SyncDirection.Down
```

> The implementer must confirm `create_remote_tree` (test_sync.py:1660) recurses into nested dicts creating each parent folder. If it does not, build the chain with explicit `m.test_remote.make_dir(...)` calls per level before creating the file. The contract: a locally-too-long path produces a Down `PathError`, and Drive accepts the long remote path without a remote-length error.

- [ ] **Step 3: Run against the real account**

Run: `python -m pytest "tests/linked/integration/test_sync.py::test_long_path_error" -q`
Expected: PASS — Drive accepts the deep nested path; the download surfaces a `PathError` because the local path is too long.

- [ ] **Step 4: Commit**

```bash
git add tests/linked/integration/test_sync.py
git commit -m "test(cluster-d): build long path as nested Drive folders; assert local PathError"
```

---

### Task 3: Verify the differences doc and gate

- [ ] **Step 1: Confirm the differences doc covers both cases**

Open `docs/superpowers/references/dropbox-vs-drive-differences.md`; verify §5 (unicode normalization) and §6 (remote path length) describe the accepted Drive behavior and the user-visible consequence. Add detail if either is thin.

- [ ] **Step 2: Full linked integration run (tmux, real account)**

Run: `python -m pytest tests/linked/integration -q -ra`
Expected: `test_unicode_decomposed` and `test_long_path_error` are no longer xfail; zero unexpected failures.

- [ ] **Step 3: Commit any doc touch-ups**

```bash
git add docs/superpowers/references/dropbox-vs-drive-differences.md
git commit -m "docs(cluster-d): finalize Drive-vs-Dropbox unicode + path-length notes"
```

---

## Self-review notes
- **Scope:** no production code changes — these are accepted platform differences, per the user's option (b).
- **Coverage:** both xfails are removed; both tests now assert Drive's real behavior; both are cross-referenced to the differences doc.
- **Placeholder check:** the only deferred detail is whether `create_remote_tree` recurses (Task 2 Step 2 note) — the assertion contract is fully specified either way.
