# Shared with me (M1/M2/M3) — Live Smoke Test Plan

**Purpose:** M1/M2/M3 (the "Shared with me" feature) are covered by the offline suite only. This plan validates them end-to-end against real Google Drive, because on this project live smoke tests have repeatedly caught bugs unit tests miss (OAuth/network/IPC/permission behavior). Run this in a **fresh session** once a test Google account is available.

**Branch under test:** `shared-with-me` (HEAD at time of writing: `37c4cc7`; 33+ commits above `main`).

**Feature recap (what to observe):**
- **M1** — Drive "Shared with me" content appears locally under a top-level `/Shared with me` directory; view-only items are read-only on disk and never uploaded.
- **M2** — My-Drive shortcuts become local symlinks pointing at the target's local path (download-only).
- **M3** — local symlink create/retarget/delete/move propagates back to Drive shortcuts.

---

## ⚠️ Safety preconditions (READ FIRST)

1. **Do NOT run `pytest tests/offline` while an account is linked.** A keyring test-collision can wipe the linked account's credentials (known hazard on this project). Finish all offline testing *before* linking; if you need offline tests again, unlink first.
2. **Two Google accounts are required.** Account **A** = the account Bora syncs (link this one). Account **B** = a second account that *shares* content with A. You can use one browser profile per account.
3. This is a headless VM — use the **paste-the-redirect-URL** link flow (`bora auth link --paste`), not the loopback flow. (Validated previously: the paste-redirect round-trip succeeds with `OAUTHLIB_INSECURE_TRANSPORT` unset — do not set it.)
4. Use a **throwaway/test** account for A. The sync folder will materialize real shared content.

---

## Phase 0 — Environment setup

Run these as the user (interactive login needs a real terminal — prefix with `!` in the session to run inline, e.g. `! bora auth link --paste`).

```bash
# 1. Confirm the branch and a clean build.
cd /home/exedev/maestral
git rev-parse --abbrev-ref HEAD          # expect: shared-with-me
pip install -e .                          # ensure the editable install matches this branch

# 2. Link account A (paste-redirect flow). Follow the printed URL in a browser
#    logged in as A, approve, then paste the final redirect URL back.
bora auth link --paste

# 3. Point Bora at a fresh local sync folder and start the daemon.
#    (If `bora start` runs an interactive setup dialog, accept the default sync
#    path or set one, e.g. ~/BoraDrive.)
bora start
bora status                               # expect: linked account A, syncing

# 4. Note the sync folder path for later checks:
SYNC=~/BoraDrive                          # <-- set to the actual chosen path
```

Baseline check: `ls "$SYNC"` shows account A's My Drive content. **`/Shared with me` may not exist yet** — it is created lazily when the first shared item arrives (Phase 1.1).

---

## Phase 1 — M1: shared content materialization

For each step: perform the Drive-side action as account **B** (or in A's Drive web UI), wait for the next sync cycle (watch `bora status` / the sync folder), then verify the local result.

### 1.1 Share a single file → lands under `/Shared with me/`
- **B:** share a file (e.g. `hello.txt`) with A (as **Editor**).
- **Verify:** `"$SYNC/Shared with me/hello.txt"` appears locally with the shared content.
- **Verify:** it is writable (Editor access) — `test -w "$SYNC/Shared with me/hello.txt"`.

### 1.2 Nested shared folder → deep descendants resolve  *(validates spec §2 open item)*
- **B:** create `TeamFolder/Sub/deep.txt`, share **TeamFolder** with A.
- **Verify:** `"$SYNC/Shared with me/TeamFolder/Sub/deep.txt"` exists locally with content.
- **If the deepest file is MISSING** but `TeamFolder` itself synced: the ambient `trashed=false` listing did not cover shared descendants → the per-root child-enumeration fallback (M1 plan Task 4, Step 6) is needed. **Record this** — it is the one unresolved design question.

### 1.3 View-only file → read-only on disk, never uploaded  *(M1 §5)*
- **B:** share a file (e.g. `readonly.txt`) with A as **Viewer** (no edit).
- **Verify:** local file has **no write bit** — `test ! -w "$SYNC/Shared with me/readonly.txt"` succeeds; `stat -c '%A' ...` shows `r--` for the user.
- **Attempt a local edit** (force it: `chmod +w` then append, or edit via an app): confirm the change is **not** uploaded and there is **no repeated 403 error storm** in `bora status` / logs. The upload is silently skipped.

### 1.4 Unshare → local removal  *(M1 §3)*
- **B:** revoke A's access to `hello.txt` (from 1.1).
- **Verify:** `"$SYNC/Shared with me/hello.txt"` is removed locally on the next cycle.

### 1.5 Selective-sync off-switch  *(M1 §7)*
- **Exclude a shared subfolder:** `bora excluded add "/Shared with me/TeamFolder/Sub"` → verify `"$SYNC/Shared with me/TeamFolder/Sub"` is deleted locally; `bora excluded list` shows it.
- **Re-include:** `bora excluded remove "/Shared with me/TeamFolder/Sub"` → verify it re-materializes.
- **Whole-feature off:** `bora excluded add "/Shared with me"` → verify the entire `/Shared with me` tree is removed locally. Then `bora excluded remove "/Shared with me"` to restore.

### 1.6 Reserved-name collision (optional / rare)  *(M1 §8)*
- **A:** in My Drive, create a real folder literally named `Shared with me`.
- **Verify:** the virtual root wins `/Shared with me`; the real folder is skipped with a warning in the log (not silently shadowing shared content). Cosmetic/rare — record behavior.

---

## Phase 2 — M2: shortcut → symlink (download-only)

### 2.1 Shortcut to an owned file → local symlink
- **A (Drive web UI):** create a shortcut in My Drive to an existing owned file (right-click → Organize → Add shortcut, or "Make shortcut").
- **Verify:** a **symlink** appears locally at the shortcut's path — `test -L "$SYNC/<shortcut path>"`; `readlink` shows a **relative** path to the target; opening it resolves to the target's content.

### 2.2 Shortcut across namespaces → shared target
- **A:** create a shortcut pointing at a file under `/Shared with me/…`.
- **Verify:** the local symlink resolves into `"$SYNC/Shared with me/…"` (uniform resolution across My-Drive and shared namespaces).

### 2.3 Shortcut to an excluded/unavailable target → error, then auto-materialize
- **A:** create a shortcut to a file, then exclude that target via `bora excluded add "<target path>"` (or point at a not-yet-synced item).
- **Verify:** a **sync error** is recorded (`bora status` shows it) and **no dangling symlink** is created.
- **Re-include** the target (`bora excluded remove …`): verify the symlink **materializes automatically** on a later cycle.

### 2.4 Shortcut chain → one-hop guard
- **A:** create a shortcut whose target is *itself another shortcut*.
- **Verify:** a **sync error** is recorded (one-hop rule) — **not** a symlink loop or infinite retry.

### 2.5 Download-only: local symlink edit not propagated (M2 only)
- Locally modify a materialized shortcut symlink's target (`ln -sf <other> "$SYNC/<shortcut>"`) **before doing Phase 3**.
- **Verify (M2 semantics):** the change is **silently skipped** (no error, no Drive change). *(Phase 3 changes this behavior — do 2.5 only if validating M2 in isolation; otherwise skip straight to Phase 3.)*

---

## Phase 3 — M3: reverse propagation (local symlink → Drive shortcut)

> M3 supersedes M2's download-only skip: local symlink changes now DO propagate.

### 3.1 Create local symlink to a synced target → Drive shortcut created
- **Local:** `ln -s <relative target> "$SYNC/mylink"` where the target is a synced Drive file.
- **Verify:** a Drive **shortcut** named `mylink` appears in A's Drive web UI, targeting the file. The local index row is a symlink.

### 3.2 Retarget the local symlink → shortcut updated
- **Local:** `ln -sf <different synced target> "$SYNC/mylink"`.
- **Verify:** the Drive shortcut now points at the new target (old shortcut removed, new created — retarget = delete+create). Confirm there is exactly **one** shortcut named `mylink` (no autorenamed duplicate).

### 3.3 Delete the local symlink → Drive shortcut removed
- **Local:** `rm "$SYNC/mylink"`.
- **Verify:** the Drive shortcut is removed (trashed) — and does **not** reappear locally on the next cycle (no resurrection).

### 3.4 Move/rename the local symlink → Drive shortcut moved
- **Local:** `mv "$SYNC/mylink" "$SYNC/renamedlink"` (or into a subfolder).
- **Verify:** the Drive shortcut is renamed/moved to match; the local item is still a symlink after the move.

### 3.5 Symlink to an unknown target → left local + error
- **Local:** `ln -s /etc/hostname "$SYNC/badlink"` (target outside the sync folder / not a Drive item).
- **Verify:** **no** Drive shortcut is created; a **sync error** is recorded; the local symlink is left in place.

### 3.6 Write-access gate → no shortcut in a view-only folder
- **Local:** create a symlink inside a **view-only** shared folder (from 1.3's Viewer share, if it's a folder).
- **Verify:** nothing is created on Drive (gated on the containing folder's write access); a notice/skip is recorded.

---

## Phase 4 — Teardown

```bash
bora stop
bora auth unlink        # IMPORTANT before running offline tests again
bora status             # expect: not linked
```

Only after `unlink` is it safe to run `pytest tests/offline` again.

---

## Results template (fill in during the run)

| # | Scenario | Expected | Result | Notes |
|---|----------|----------|--------|-------|
| 1.1 | share file | under /Shared with me/, writable | | |
| 1.2 | nested shared folder | deep descendant resolves | | **§2 coverage verdict** |
| 1.3 | view-only file | read-only, no upload/403 storm | | |
| 1.4 | unshare | local removal | | |
| 1.5 | selective-sync exclude/include + off-switch | prune + restore | | |
| 1.6 | reserved-name collision | virtual root wins | | rare |
| 2.1 | shortcut→owned | relative symlink resolves | | |
| 2.2 | shortcut→shared | resolves cross-namespace | | |
| 2.3 | shortcut→excluded target | error, then auto-materialize | | |
| 2.4 | shortcut chain | error, no loop | | |
| 3.1 | create symlink | Drive shortcut created | | |
| 3.2 | retarget | shortcut updated, no dup | | |
| 3.3 | delete symlink | shortcut removed, no resurrect | | |
| 3.4 | move symlink | shortcut moved, still symlink | | |
| 3.5 | unknown target | left local + error | | |
| 3.6 | view-only folder gate | nothing created | | |

**If any scenario fails:** capture `bora status`, the relevant log lines, and the exact local/Drive state, then open a `superpowers:systematic-debugging` session against the `shared-with-me` branch. The most likely genuinely-open item is **1.2** (ambient-listing coverage of shared descendants); everything else has offline coverage and is expected to pass.
