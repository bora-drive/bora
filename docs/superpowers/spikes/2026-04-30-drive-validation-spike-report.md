# Stage 0 Drive Validation Spike — Report

**Date:** 2026-04-30
**Branch:** `spike/drive-validation` (deleted after this report is preserved on `google-drive-port`)
**Inputs answered:** `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` §4.1, questions 1–6
**Environment:** exe.dev Linux VM, headless (no local browser); single Google account
**Scripts:** `spike/q1_oauth_environments.py`, `spike/q2_md5_checksum.py`, `spike/q3_q6_change_feed.py`, `spike/q4_slash_in_name.py`, `spike/q5_combined_move_rename.py` (committed on the spike branch; deleted with the branch)
**Captured payloads:** `spike/outputs/` (gitignored on the spike branch; not preserved)

## Summary table

| # | Question | REVIEW2 ref | Result | Effect on design |
|---|---|---|---|---|
| 1 | `run_local_server` in dev environments | B1 | Mode A SUCCESS in headless VM with SSH-port-forward; Mode B (paste-redirect) FAILS with `InsecureTransportError` because `redirect_uri=http://localhost:8080` is rejected by oauthlib | **Refines.** Stage 2 default is `run_local_server` (works headless via tunnel). Mode B fallback requires either `OAUTHLIB_INSECURE_TRANSPORT=1` env var or a different flow shape (e.g. embedded loopback server). |
| 2 | `md5Checksum` vs `hashlib.md5` | (Stage 5 hashing) | All 5 payloads match exactly, including the 6 MB resumable-upload case | **Confirms.** `ContentHasher` (MD5) maps directly to Drive's `md5Checksum`. No special-casing needed. |
| 3 | `changes.list` for trash / hard-delete / move-out | A3, A4 | Trash: `removed=false`, `file.trashed=true`, full file body. Hard-delete: `removed=true`, **no `file` body**, only `fileId`. Move-out: `removed=false`, full file body with **updated** `parents`. | **Refines.** `_clean_remote_changes` cannot rely on `removed` alone — see Q6. |
| 4 | `/` in file name | C1 | **ACCEPTED literally.** `files.create(name='2024/05 report.pdf')` returned 200; `files.list` and `files.get` both return `name='2024/05 report.pdf'` unchanged. | **Decides.** Stage 1 must **escape** `/` in path serialization (not reject). Stage 4 upload must mirror-escape. |
| 5 | combined move+rename change entry shape | A5 | Exactly **one** change entry covering both the new name and new parents in a single `files.update` call | **Confirms.** Stage 4 `path_cache.move()` reads name+parents atomically from one entry. No split-entry handling required. |
| 6 | `removed` reasons | A4 | `removed=true` only for hard-delete (`files.delete`). Trash and move appear as `removed=false` regular changes; semantics distinguished by `file.trashed` and `file.parents` content. | **Refines.** Disambiguation rule: `removed=true` → propagate as deletion. `removed=false` + `file.trashed=true` → propagate as deletion (sync-driven trash semantics — Stage 4 C4). `removed=false` + `file.trashed=false` + parents outside sync scope → treat as move-out (delete from local mirror). Otherwise: regular update. |

---

## Q1 (B1): OAuth `run_local_server` in dev environments

**Procedure run:** `spike/q1_oauth_environments.py`. Mode A (`InstalledAppFlow.run_local_server(port=0)`) and Mode B (`run_flow_console` paste-redirect) attempted back-to-back.

**Observed:**

```
A: run_local_server: SUCCESS
  token expiry: 2026-04-30 05:29:48
  scopes: ['https://www.googleapis.com/auth/drive']

B: console paste redirect: FAILED
  exception: InsecureTransportError: (insecure_transport) OAuth 2 MUST utilize https.
```

**Mode A note:** the spike was run in a headless exe.dev VM. Mode A succeeded because the user reached the printed URL in an external browser and the OAuth callback path (loopback to a port the script bound) was reachable — likely via SSH port forwarding. Mode A is therefore viable for headless users **with SSH port-forward in place**, not viable for arbitrary headless setups without forwarding.

**Mode B failure root cause:** `spike/auth.py:run_flow_console` sets `redirect_uri="http://localhost:8080"`. `oauthlib.oauth2.rfc6749.parameters.parse_authorization_code_response` rejects HTTP redirect URIs by default (`InsecureTransportError`). Workarounds:

1. `OAUTHLIB_INSECURE_TRANSPORT=1` environment variable (whitelists HTTP for localhost). Standard for desktop OAuth clients.
2. Use the same loopback-server pattern as `run_local_server` but expose the URL+port to the user so they can complete the redirect from a different machine.

**REVIEW2 B1 effect — refines.** Stage 2's onboarding spec should:
- Default to `run_local_server` (works for users with a local browser AND for headless users with SSH port-forward).
- Document the SSH port-forward pattern explicitly: e.g. `ssh -L 8080:localhost:8080 …` then run the auth flow remotely.
- For the paste-redirect fallback, set `OAUTHLIB_INSECURE_TRANSPORT=1` in the environment before starting the flow (or implement a different fallback). The current `run_flow_console` in `spike/auth.py` is a reference for what NOT to ship without that env var.

---

## Q2: md5Checksum vs hashlib.md5

**Procedure run:** `spike/q2_md5_checksum.py`. Five files uploaded.

**Observed:**

| File | Size | Local md5 | Drive md5Checksum | Match |
|---|---|---|---|---|
| small_text.txt | 31 | `6253216f2a2eafeba94e6d2bcbbc52da` | `6253216f2a2eafeba94e6d2bcbbc52da` | ✓ |
| utf8_text.txt | 33 | `ce6782c62e5ac78acadb4e741ad2cdb2` | `ce6782c62e5ac78acadb4e741ad2cdb2` | ✓ |
| non_ascii_bytes.bin | 1024 | `b2ea9f7fcea831a4a63b213f41a8855b` | `b2ea9f7fcea831a4a63b213f41a8855b` | ✓ |
| small_binary.bin | 1024 | `1be8e80907ad260362100d406021e724` | `1be8e80907ad260362100d406021e724` | ✓ |
| large_binary.bin | 6,291,456 | `721264f06a74ccfd4230fdf65b297dcb` | `721264f06a74ccfd4230fdf65b297dcb` | ✓ |

`all_match: true`. The 6 MB file exercises the resumable-upload path (>5 MB threshold) and matches identically.

**Stage 5 hashing effect — confirms.** Drive's `md5Checksum` is byte-for-byte equivalent to `hashlib.md5(content).hexdigest()` for user-uploaded binaries across the file-size and byte-distribution range we tested. The Stage 5 plan's `ContentHasher` (MD5) maps directly to `md5Checksum` with no transformation. Note: this finding does not extend to Google-native types (Docs/Sheets/Slides) — those don't carry `md5Checksum` at all and are excluded from sync per `native_docs.py`.

---

## Q3 (A3): Change-feed payloads for trash, hard-delete, move-out

**Procedure run:** `spike/q3_q6_change_feed.py`. Three mutations on three files in a watched folder. Unshare not run (single-account; see "Known gaps").

**Note on payload noise:** the script's pre-token sleep (`CHANGE_FEED_LAG_SECONDS=3`) was not always sufficient to drain prior file-creation events from the change feed. The trash capture, for example, contains 3 entries: 2 are belated create-noise, 1 is the actual trash mutation. The relevant entry per capture is identified below by its semantic match.

### Trash (`files.update(trashed=True)`)

Relevant change entry (full file body present, abbreviated to load-bearing fields):

```json
{
  "kind": "drive#change",
  "type": "file",
  "removed": false,
  "fileId": "1WcVVnnVKfuHOo9idJPzA-khZKvSiAiUk",
  "file": {
    "id": "1WcVVnnVKfuHOo9idJPzA-khZKvSiAiUk",
    "name": "spike_trash.txt",
    "parents": ["1iWKLkUG1Rd0Fn9J0GDbqumVsPpHAxs-x"],
    "trashed": true,
    "explicitlyTrashed": true,
    ...
  }
}
```

Salient fields: `removed=false`, `file.trashed=true`, `file.explicitlyTrashed=true`, parents unchanged. Full metadata available.

### Hard delete (`files.delete`)

```json
{
  "kind": "drive#change",
  "type": "file",
  "changeType": "file",
  "time": "2026-04-30T04:31:13.153Z",
  "removed": true,
  "fileId": "14KRVMcP_jUgnOc58Gw9lhZb-oQgdT5QQ"
}
```

Salient fields: `removed=true`, **no `file` body**. Only `fileId` is reported. The sync engine cannot recover any metadata — must rely on its own cached state to know what was at this fileId.

### Move out of watched folder (`removeParents=watched, addParents=other`)

Relevant change entry (full file body):

```json
{
  "kind": "drive#change",
  "type": "file",
  "removed": false,
  "fileId": "1dKA9L8-hN1k8WPC6KGic__C_2FfMTvla",
  "file": {
    "id": "1dKA9L8-hN1k8WPC6KGic__C_2FfMTvla",
    "name": "spike_move_out.txt",
    "parents": ["19hyhik02-CFEKsiMpdZQVyjdYluyL4KN"],
    "trashed": false,
    ...
  }
}
```

Salient fields: `removed=false`, full file body, `parents` reflects the **new** parent (the "other" folder; the `watched` folder ID no longer appears). The change entry is **indistinguishable** from a normal in-scope move/rename without inspecting whether any of the listed parents are still inside the sync root.

**REVIEW2 A3 effect — refines.** `_clean_remote_changes` rules for Stage 3:

1. **`removed=true`** → propagate as deletion. fileId only; sync engine resolves cached state to know which local path to delete.
2. **`removed=false` and `file.trashed=true`** → propagate as deletion (sync-driven trash treats trashed as gone, matching Drive UX where trashed items disappear from the user's view).
3. **`removed=false`, `file.trashed=false`, and `intersection(file.parents, sync_root_subtree) == ∅`** → file moved out of sync scope; propagate as deletion of the local mirror.
4. **`removed=false`, `file.trashed=false`, and at least one parent in sync scope** → propagate as create/update/move within the mirror. `path_cache` updates name + parents from the single entry.

Rule 3 is the key A3 finding: change entries don't include sync-root context, so the engine must compute `file.parents ⊆ sync_root` to filter. The current cache-side path resolution covers this, provided the cache is populated.

---

## Q4 (C1): `/` in file name

**Procedure run:** `spike/q4_slash_in_name.py`. Attempted `files.create(name='2024/05 report.pdf', parents=[<test_parent>])`.

**Observed:** `create_succeeded=true`. Drive accepted the name literally and returned it unchanged across the create, list, and get responses:

```json
{
  "create_response": { "id": "...", "name": "2024/05 report.pdf", "parents": [...] },
  "list_response":   { "files": [ { "name": "2024/05 report.pdf", ... } ] },
  "get_response":    { "id": "...", "name": "2024/05 report.pdf", "parents": [...] }
}
```

The slash was preserved end-to-end with no escaping, no transformation, no warning.

**REVIEW2 C1 effect — decides.** Drive **accepts** `/` in names as a literal character. Therefore:

- **Stage 1 path cache (C1 policy):** must **escape** `/` when serializing a Drive name into a virtual filesystem path, since the local path syntax interprets `/` as a separator. The corresponding inverse-escape on hydration is required.
- **Stage 4 upload sanitization (C3):** must mirror the cache's escape policy when uploading local names to Drive. If a local file is named `a%2Fb.txt` (escaped form of `a/b.txt`), the upload must un-escape before sending to Drive. If a local file legitimately contains `%2F`, the escape policy must double-escape.

The "reject" alternative is no longer an option — Drive does not enforce a `/` ban, so users may already have such names in their existing Drive content. Stage 1's spec must commit to a specific escape codec (URL-encoding of `/` and the escape character itself is the simplest sound choice).

---

## Q5 (A5): Combined move + rename

**Procedure run:** `spike/q5_combined_move_rename.py`. Single `files.update(fileId, body={"name": "renamed.txt"}, addParents=B, removeParents=A)`.

**Observed:** `relevant_change_count_for_file=1`. The single entry covers both the rename and the move:

```json
{
  "kind": "drive#change",
  "type": "file",
  "removed": false,
  "fileId": "1gd_x4g5zB4frhxXvqpkJMAKbqndK4G0w",
  "file": {
    "id": "1gd_x4g5zB4frhxXvqpkJMAKbqndK4G0w",
    "name": "renamed.txt",
    "parents": ["1b3arJzLzNaTzERs829hlKOkFyyWJctKW"],
    "originalFilename": "original.txt",
    "trashed": false,
    "modifiedTime": "2026-04-30T04:32:17.043Z",
    ...
  }
}
```

Both the new name (`renamed.txt`) and the new parent (folder B's ID) are present in one entry. `originalFilename` retains the pre-rename name `original.txt` — incidentally useful for reconciling against the cache when the cache only knows the prior name. `version` incremented to 4 (was 3 after create).

**REVIEW2 A5 effect — confirms.** Stage 4 `path_cache.move()` reads the new name and new parents from a single change entry atomically. No split-entry coordination is required. The implementation can apply name+parent in a single transaction.

---

## Q6 (A4): `removed` reasons in change entries

**Source:** same captures as Q3.

| Mutation | `removed` | `file` populated? | `file.trashed` | `file.parents` |
|---|---|---|---|---|
| trash | `false` | yes | `true` | unchanged (still in original parent) |
| hard_delete | `true` | **no** | n/a | n/a |
| move_out | `false` | yes | `false` | **updated** to new parent (out of sync scope) |

**REVIEW2 A4 effect — refines.** `removed=true` alone is **NOT sufficient** to identify all "file is gone for sync purposes" cases. The full disambiguation:

| Observation | Interpretation | Action |
|---|---|---|
| `removed=true` (no `file`) | Hard-deleted (or unshared / lost visibility) | Delete from local mirror; cache resolves fileId → cached path |
| `removed=false`, `file.trashed=true` | User trashed the file | Delete from local mirror |
| `removed=false`, `file.trashed=false`, no parent in sync scope | Moved out of sync scope | Delete from local mirror |
| `removed=false`, `file.trashed=false`, parent in sync scope | Normal create/update/move within mirror | Apply via path_cache |

This is the rule set Stage 3's read client must implement. The single-account scenario covers everything except "unshare from another owner" — see Known gaps.

---

## Known gaps

- **Q3 unshare scenario not run.** Requires a second Google account to share to and revoke from. Empirical observation deferred. Working assumption (treat as `removed=true` with no file body, same as hard-delete) is consistent with how Drive surfaces lost-visibility events; if Stage 3 finds a concrete case where this is wrong, run the missing scenario then.
- **Change-feed lag and noise.** The spike's pre-token sleep of 3 seconds was not always sufficient to drain prior file-creation events from the feed. For Stage 3, polling-loop captures should expect occasional belated entries (e.g. a create event arriving in a poll window after the `getStartPageToken` for that window). The change feed is eventually consistent but not strictly serialized to user actions; consumers must be idempotent against re-observed changes (already a property of `path_cache` since it keys by fileId).
- **Q1 environment coverage limited to one VM.** `run_local_server` worked here via SSH port-forward inferred from the success result; behavior on a fully-isolated headless box (no port forwarding available) is not directly observed. The Mode B paste-redirect path is broken-as-shipped due to `InsecureTransportError`; Stage 2 must address this before claiming headless support.

## Recommended updates to staged-port-plan-design.md

1. **§4.1 row 1 (Q1) "What it informs":** Refine to: "Stage 2 default = `run_local_server` (works locally and over SSH port-forward). Documented fallback for fully-isolated headless: paste-redirect with `OAUTHLIB_INSECURE_TRANSPORT=1` env var enabled, or an embedded-loopback variant. Plain paste-redirect with HTTP redirect_uri is broken as written."
2. **§4.1 row 4 (Q4) "What it informs":** Replace "escape policy vs. reject policy" with the decided answer: "**escape** (Drive accepts `/` literally; the cache and uploader must escape on serialization)."
3. **§4.1 row 5 (Q5) "What it informs":** Replace the conditional "whether path cache `move()` must be atomic" with the decided answer: "**path_cache `move()` reads name+parents atomically from a single change entry.** No split-entry coordination required."
4. **§4.1 row 6 (Q6) "What it informs":** Replace the conditional with the decided rule set (the four-row table from this report's Q6 section).
5. **§5.1 Stage 1 in-scope C1 bullet:** Update from "decide and implement `/`-in-name policy" to "**escape** `/` and the escape character itself when serializing Drive `name` to virtual path; symmetric un-escape on hydration."
6. **§5.2 Stage 2 in-scope B1 bullet:** Update from "headless-friendly OAuth UX. Specific approach informed by spike Q1." to: "Default flow: `run_local_server`. Document SSH port-forward for headless users. Paste-redirect fallback requires `OAUTHLIB_INSECURE_TRANSPORT=1` (or an embedded-loopback rewrite); `spike/auth.py:run_flow_console` is a reference for what fails without it."
7. **§5.3 Stage 3 in-scope A4 bullet:** Update from "`removed` disambiguation rules (informed by spike Q3, Q6)" to embed the rule table from Q6 of this report.
8. **§5.4 Stage 4 in-scope A5 bullet:** Update from "path cache `move()` must update name and parent atomically" to: "path_cache `move()` reads name+parents atomically from one change entry — confirmed by Q5."
9. **§9 Open questions:** Mark resolved: C1 (escape), A5 (one entry, atomic). C4 (trash vs delete default) and D4 (GUI compat) remain open per their original deferral.

## Stage 1 readiness

With Q1, Q2, Q3, Q4, Q5, Q6 answered, the following Stage 1+ specs are unblocked:

- **Stage 1 path cache** unblocked on C1 (escape) — Q4.
- **Stage 1 cache-side `removed` interpretation** unblocked — Q3, Q6.
- **Stage 2 auth** unblocked on B1 with the caveat that paste-redirect needs the `OAUTHLIB_INSECURE_TRANSPORT` workaround documented — Q1.
- **Stage 4 path_cache atomicity** unblocked — Q5.
- **Stage 5 hashing plan** unblocked — Q2.

No Stage 1+ spec is blocked on a question the spike did not answer. The staged port plan design can proceed with the §10 next step ("Brainstorm the Stage 1 spec").
