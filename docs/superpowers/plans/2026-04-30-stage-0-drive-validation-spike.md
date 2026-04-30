# Stage 0: Drive Validation Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer six load-bearing Google Drive API questions from `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` §4.1 against a real Drive, capturing payloads and producing `SPIKE_REPORT.md` so Stage 1+ specs can be written against ground truth instead of doc-derived assumptions.

**Architecture:** Throwaway Python scripts on a `spike/drive-validation` branch, one script per question (with two questions sharing infrastructure). Scripts use `googleapiclient` directly with no path cache, no sync engine, no error-handling hierarchy, no tests — per spec §4.1. Captured payloads land in `spike/outputs/`. Findings synthesized in `SPIKE_REPORT.md`, which is the only artifact preserved on `google-drive-port` after the spike branch is deleted.

**Tech Stack:** Python 3.10+, `google-api-python-client`, `google-auth-oauthlib`, `googleapiclient.http.MediaInMemoryUpload`, `hashlib`. No new dependencies — all three Google libs are already in `pyproject.toml`.

---

## Spec Decisions Locked In

These are inherited from the design spec — do not relitigate during execution:

- **Form factor:** simple scripts under `spike/`, no abstraction shared with `src/cumulus/`. (spec §4 form factor)
- **Branch lifecycle:** work happens on `spike/drive-validation`, branched from `google-drive-port`. The branch is deleted after `SPIKE_REPORT.md` is preserved on `google-drive-port`. No production code merges from the spike. (spec §4 branch)
- **Scope:** answer the six questions in spec §4.1. Do not add a seventh.
- **Out of scope:** path cache, sync engine, error-handling hierarchy, CLI, tests. (spec §4.3)
- **Unshare scenario in Q3:** mentioned in spec §4.1 but skip — requires multi-account setup and is not load-bearing for Stage 1. Note the skip in `SPIKE_REPORT.md` as a known gap; do not block the spike on it.
- **Credentials:** `spike/client_secret.json` is gitignored. Stage 2 commits a production `client_secret.json` to `src/cumulus/resources/`; the spike does not.

## File Structure

The spike adds one directory under repo root and one new file in `docs/`. Nothing under `src/cumulus/` is touched.

| Path | Created in task | Purpose |
|---|---|---|
| `spike/.gitignore` | Task 1 | Excludes `client_secret.json`, `token.json`, `outputs/` |
| `spike/requirements.txt` | Task 1 | Pin spike deps (matches pyproject) |
| `spike/README.md` | Task 1 / 2 | Setup steps and how to run each script |
| `spike/__init__.py` | Task 3 | Makes `spike` a package so `from spike.auth import …` works |
| `spike/auth.py` | Task 3 | Shared OAuth helper used by Q2–Q5 |
| `spike/q1_oauth_environments.py` | Task 4 | Q1: run_local_server vs paste-redirect flow |
| `spike/q2_md5_checksum.py` | Task 5 | Q2: md5Checksum vs hashlib.md5 |
| `spike/q3_q6_change_feed.py` | Task 6 | Q3 + Q6: trash / hard-delete / move-out + `removed` reasons |
| `spike/q4_slash_in_name.py` | Task 7 | Q4: `/`-in-name behavior |
| `spike/q5_combined_move_rename.py` | Task 8 | Q5: combined move+rename change entry shape |
| `spike/outputs/*` | Tasks 4–8 | Captured payloads (gitignored) |
| `SPIKE_REPORT.md` (repo root, spike branch only) | Task 9 | Synthesized findings — temporary location |
| `docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md` | Task 10 | Permanent location on `google-drive-port` |

Q3 and Q6 share a script because they query the same change-feed payloads (the spec lists them as separate questions but the procedures are identical: capture `changes.list` after a mutation). Splitting them into two scripts would duplicate the setup. The report still answers them as separate questions.

---

## Task 1: Branch + scaffolding + dependencies

**Files:**
- Create: `spike/.gitignore`
- Create: `spike/requirements.txt`
- Create: `spike/README.md`
- Create: `spike/outputs/.gitkeep`

- [ ] **Step 1: Create the spike branch off `google-drive-port`**

```bash
git checkout google-drive-port
git pull --ff-only origin google-drive-port 2>/dev/null || true
git checkout -b spike/drive-validation
```

Expected: `Switched to a new branch 'spike/drive-validation'`. If the new-branch creation fails because the branch already exists, do not delete it without checking with the user — it may contain prior spike work.

- [ ] **Step 2: Create the `spike/` directory and outputs subdirectory**

```bash
mkdir -p spike/outputs
touch spike/outputs/.gitkeep
```

- [ ] **Step 3: Write `spike/.gitignore`**

```
client_secret.json
token.json
outputs/
__pycache__/
*.pyc
```

The `outputs/` ignore covers captured payloads (which may contain emails / file IDs). The `.gitkeep` is fine to commit because git ignores listed files but not the explicit `.gitkeep`.

- [ ] **Step 4: Write `spike/requirements.txt`**

These are pinned to the same lower bounds as `pyproject.toml`. Reusing the project's `pip install -e .` would also work, but the spike isolates itself from the larger cumulus install.

```
google-api-python-client>=2.100.0
google-auth>=2.23.0
google-auth-oauthlib>=1.1.0
google-auth-httplib2>=0.1.1
```

- [ ] **Step 5: Write `spike/README.md` (initial — Task 2 expands it)**

```markdown
# Drive Validation Spike

Throwaway scripts that answer six load-bearing Google Drive API questions
defined in `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` §4.1.

This branch (`spike/drive-validation`) will be **deleted** once the report
is preserved on `google-drive-port`. The only artifact retained is
`SPIKE_REPORT.md`. Do not merge anything else from this branch.

## Layout

- `auth.py` — shared OAuth helper
- `q1_oauth_environments.py` — Q1 (B1): run_local_server vs paste-redirect
- `q2_md5_checksum.py` — Q2: md5Checksum vs local hashlib.md5
- `q3_q6_change_feed.py` — Q3 + Q6 (A3, A4): change-feed payloads for trash, hard-delete, move-out
- `q4_slash_in_name.py` — Q4 (C1): `/`-in-name behavior
- `q5_combined_move_rename.py` — Q5 (A5): combined move+rename in one update
- `outputs/` — captured payloads (gitignored)

See "Setup" below for Google Cloud project + OAuth client steps.
```

- [ ] **Step 6: Install dependencies and verify imports**

Run from repo root:

```bash
python3 -m venv .venv-spike
source .venv-spike/bin/activate
pip install -r spike/requirements.txt
python -c "from googleapiclient.discovery import build; from google_auth_oauthlib.flow import InstalledAppFlow; print('imports OK')"
```

Expected: `imports OK`. If the venv is unwanted (e.g. you already use a project venv with the deps), skip the venv creation; the imports check is the load-bearing assertion.

- [ ] **Step 7: Commit**

```bash
git add spike/.gitignore spike/requirements.txt spike/README.md spike/outputs/.gitkeep
git commit -m "chore(spike): scaffold drive validation spike layout"
```

---

## Task 2: Document Google Cloud setup steps

The engineer running the spike needs a real Google account and a GCP project with Drive API enabled and an OAuth client. We document the steps in `spike/README.md` so the engineer (or you, in a future session) can complete them.

**Files:**
- Modify: `spike/README.md`

- [ ] **Step 1: Append "Setup" section to `spike/README.md`**

```markdown

## Setup (do this once before running any script)

### 1. Google Cloud project + Drive API

1. Go to https://console.cloud.google.com/projectcreate and create a project
   (any name; e.g. `cumulus-spike`).
2. With the new project selected, go to
   https://console.cloud.google.com/apis/library/drive.googleapis.com and
   click **Enable**.

### 2. OAuth consent screen

1. Go to https://console.cloud.google.com/apis/credentials/consent.
2. Choose **External** user type (you'll add yourself as a test user).
3. App name: `cumulus-spike`. Support email: your email. Developer contact:
   your email. Save and continue.
4. Scopes: skip (we'll declare scopes at runtime). Save and continue.
5. Test users: add the Google account you'll use for the spike. Save.

### 3. OAuth client

1. Go to https://console.cloud.google.com/apis/credentials.
2. **Create credentials** → **OAuth client ID** → application type
   **Desktop app**. Name: `cumulus-spike-desktop`.
3. Click **Download JSON** and save it as `spike/client_secret.json`
   (gitignored).

### 4. Python deps

```bash
python3 -m venv .venv-spike
source .venv-spike/bin/activate
pip install -r spike/requirements.txt
```

### 5. First-time auth

```bash
python -m spike.auth
```

This runs the OAuth flow once and writes `spike/token.json` (gitignored).
Subsequent scripts (q2–q5) reuse this token.

## Running the questions

Each script writes to `spike/outputs/`. After all five run successfully,
synthesize findings in `SPIKE_REPORT.md` (see Task 9 of the plan).

```bash
python -m spike.q1_oauth_environments
python -m spike.q2_md5_checksum
python -m spike.q3_q6_change_feed
python -m spike.q4_slash_in_name
python -m spike.q5_combined_move_rename
```

Order matters only for q1 (it produces the cached token). q2–q5 are
independent and can be run in any order.
```

- [ ] **Step 2: Commit**

```bash
git add spike/README.md
git commit -m "docs(spike): add GCP project / OAuth client setup steps"
```

---

## Task 3: Shared OAuth helper

**Files:**
- Create: `spike/__init__.py`
- Create: `spike/auth.py`

- [ ] **Step 1: Create `spike/__init__.py`**

Empty file — makes `spike` a package so `python -m spike.q1_oauth_environments` works.

```python
```

(File contents: empty.)

- [ ] **Step 2: Write `spike/auth.py`**

```python
"""OAuth helper for the drive validation spike.

Exposes `build_drive_service()` for q2–q5 and the two flow primitives
(`run_flow_local_server`, `run_flow_console`) for q1, which probes both.

Token cached at `spike/token.json` (gitignored).
"""
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive"]
CLIENT_SECRET_PATH = "spike/client_secret.json"
TOKEN_PATH = "spike/token.json"


def load_credentials():
    """Return cached creds, refreshing if possible. Returns None if no token."""
    if not os.path.exists(TOKEN_PATH):
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
        return creds
    return None


def save_credentials(creds):
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())


def run_flow_local_server():
    """Tries InstalledAppFlow.run_local_server. Requires a reachable browser."""
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
    return flow.run_local_server(port=0)


def run_flow_console():
    """Manual paste-the-redirect-URL flow.

    Replaces the deprecated 'oob' flow. The user opens the auth URL in any
    browser, approves, then pastes the resulting localhost redirect URL.
    The browser will say 'site cannot be reached' — that's fine; we only
    need the URL with the ?code= parameter.
    """
    flow = InstalledAppFlow.from_client_secrets_file(
        CLIENT_SECRET_PATH,
        SCOPES,
        redirect_uri="http://localhost:8080",
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    print(f"\nVisit this URL in any browser:\n  {auth_url}\n")
    print("After approving, you will be redirected to a URL like:")
    print("  http://localhost:8080/?state=...&code=...&scope=...")
    print("(The browser may say 'site cannot be reached' — that is expected.)")
    redirect_response = input("\nPaste the full redirect URL: ").strip()
    flow.fetch_token(authorization_response=redirect_response)
    return flow.credentials


def get_credentials(mode="auto"):
    """Get credentials, prompting for OAuth if needed.

    mode:
        'auto'         — use cached token if present, else run_local_server
        'local-server' — force run_local_server (discards cache)
        'console'      — force paste-redirect flow (discards cache)
    """
    if mode == "auto":
        creds = load_credentials()
        if creds:
            return creds
        creds = run_flow_local_server()
    elif mode == "local-server":
        creds = run_flow_local_server()
    elif mode == "console":
        creds = run_flow_console()
    else:
        raise ValueError(f"Unknown mode: {mode}")
    save_credentials(creds)
    return creds


def build_drive_service(mode="auto"):
    creds = get_credentials(mode=mode)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


if __name__ == "__main__":
    import sys

    arg_mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    service = build_drive_service(mode=arg_mode)
    about = service.about().get(fields="user(emailAddress,displayName)").execute()
    print(f'Authenticated as: {about["user"]["emailAddress"]}')
```

- [ ] **Step 3: Verify auth helper runs end-to-end**

Prerequisite: `spike/client_secret.json` exists (Task 2 §3 in the README).

```bash
python -m spike.auth
```

Expected: a browser opens (or prints a URL), you approve, and the script prints `Authenticated as: <your-email>`. A `spike/token.json` is written.

If `run_local_server` fails (common in headless environments), Q1's script will explore the alternative; for now you can manually run `python -m spike.auth console` and paste the redirect URL.

- [ ] **Step 4: Commit**

```bash
git add spike/__init__.py spike/auth.py
git commit -m "feat(spike): shared OAuth helper with cached token"
```

---

## Task 4: Q1 — OAuth environment compatibility

Spec §4.1 row 1, REVIEW2 ref **B1**.

**Files:**
- Create: `spike/q1_oauth_environments.py`
- Output: `spike/outputs/q1_findings.txt` (gitignored)

- [ ] **Step 1: Write `spike/q1_oauth_environments.py`**

```python
"""Q1 (B1): Does OAuth `run_local_server` work in our dev environments?

Procedure:
  Mode A — InstalledAppFlow.run_local_server(port=0). Requires a reachable
           browser and a successful loopback redirect.
  Mode B — Manual paste-redirect flow. Should work in any environment where
           the user can visit a URL in *some* browser (even on a different
           machine).

What the spike informs (Stage 2):
  - If Mode A works only locally, the onboarding UX is "type `cumulus auth
    link` and a browser pops up". Headless users get Mode B.
  - If Mode A also works over SSH-with-port-forward, that path is documented
    for power users.
  - If Mode A reliably fails in the engineer's daily environment, Stage 2's
    default is the paste-redirect flow.

This script does not draw conclusions — it captures observations. The
SPIKE_REPORT.md synthesizes them.

Output: spike/outputs/q1_findings.txt
"""
import traceback
from datetime import datetime, timezone

from spike import auth

OUTPUT = "spike/outputs/q1_findings.txt"


def try_mode(label, fn):
    print(f"\n=== Trying {label} ===")
    try:
        creds = fn()
        auth.save_credentials(creds)
        return (
            f"{label}: SUCCESS\n"
            f"  token expiry: {creds.expiry}\n"
            f"  scopes: {creds.scopes}\n"
        )
    except KeyboardInterrupt:
        return f"{label}: ABORTED by user\n"
    except Exception as e:  # noqa: BLE001 — capture any failure for the report
        tb = traceback.format_exc()
        return (
            f"{label}: FAILED\n"
            f"  exception: {type(e).__name__}: {e}\n"
            f"  traceback:\n{tb}\n"
        )


def main():
    findings = [
        f"Q1 OAuth environments — captured {datetime.now(timezone.utc).isoformat()}\n",
        f"Host environment notes (fill in manually if relevant): "
        f"OS, container, SSH/local, browser availability\n\n",
    ]

    print(
        "\nThis script will attempt two OAuth flows.\n"
        "Press Ctrl+C if a flow hangs and we'll record the abort."
    )

    findings.append(try_mode("A: run_local_server", auth.run_flow_local_server))
    findings.append(try_mode("B: console paste redirect", auth.run_flow_console))

    cached = auth.load_credentials()
    findings.append(
        f"\nFinal cached token state: {'present and valid' if cached else 'absent'}\n"
    )

    with open(OUTPUT, "w") as f:
        f.writelines(findings)
    print(f"\nFindings written to {OUTPUT}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run Mode A first, observe behavior**

```bash
python -m spike.q1_oauth_environments
```

Expected: the script prints `=== Trying A: run_local_server ===` and either opens a browser (success path) or fails with a traceback the script captures. After Mode A finishes (success or failure), it prompts for Mode B's paste-redirect URL.

If you're in a headless VM with no browser, Mode A will likely either hang trying to open a browser or print a URL it cannot redirect to. Press Ctrl+C if it hangs more than ~30 seconds; the script logs the abort.

- [ ] **Step 3: Complete Mode B**

When prompted, copy the printed URL into a browser on any reachable machine, approve consent, copy the resulting `http://localhost:8080/?...code=...` URL, and paste it back into the terminal.

Expected: `B: SUCCESS` line in `spike/outputs/q1_findings.txt`.

- [ ] **Step 4: Inspect findings**

```bash
cat spike/outputs/q1_findings.txt
```

Expected: one block per mode (A, B), each with either `SUCCESS` (and token expiry / scopes) or `FAILED` with traceback. No partial files.

- [ ] **Step 5: (Optional) Repeat on a second environment**

If you have access to a different environment (e.g. a local desktop with a browser, *and* this spike was first run in a VM, or vice versa), run the script again and append the second environment's `q1_findings.txt` content as a second labeled section. Note the environment in `findings[1]`.

This step is optional — the spec only requires answering whether Mode A works in the engineer's primary environment. If Mode A succeeded the first time, this step is unnecessary.

- [ ] **Step 6: Commit**

```bash
git add spike/q1_oauth_environments.py
git commit -m "feat(spike): q1 OAuth environment compatibility probe"
```

The `outputs/` directory is gitignored, so `q1_findings.txt` is not committed. That is intentional — the synthesized version goes into `SPIKE_REPORT.md` (Task 9).

---

## Task 5: Q2 — md5Checksum vs hashlib.md5

Spec §4.1 row 2, informs Stage 5 hashing.

**Files:**
- Create: `spike/q2_md5_checksum.py`
- Output: `spike/outputs/q2_md5_results.json` (gitignored)

- [ ] **Step 1: Write `spike/q2_md5_checksum.py`**

```python
"""Q2: Does Drive's `md5Checksum` match locally-computed `hashlib.md5`?

Procedure:
  Upload five files spanning common edge cases (small text, UTF-8 text,
  full-byte-range binary, random small binary, random large binary that
  crosses the 5MB simple-vs-resumable threshold). Compute `hashlib.md5` on
  the bytes we sent. Fetch the file metadata `md5Checksum` field. Compare.

What the spike informs (Stage 5 hashing plan):
  If all five match: the planned `ContentHasher` (MD5) maps cleanly to
  Drive's `md5Checksum`. If any mismatch: special-casing is needed
  (most likely on Google-native types, but those don't carry md5Checksum
  at all, so we don't include them here).

Output: spike/outputs/q2_md5_results.json
"""
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone

from googleapiclient.http import MediaInMemoryUpload

from spike.auth import build_drive_service

OUTPUT = "spike/outputs/q2_md5_results.json"


def make_test_payloads():
    return {
        "small_text.txt": b"Hello, Drive validation spike!\n",
        "utf8_text.txt": "café résumé naïve 漢字 ✓\n".encode("utf-8"),
        "non_ascii_bytes.bin": bytes(range(256)) * 4,
        "small_binary.bin": secrets.token_bytes(1024),
        # 6 MB > the 5 MB simple-upload threshold — exercises resumable path.
        "large_binary.bin": secrets.token_bytes(6 * 1024 * 1024),
    }


def upload_and_compare(service, name, content):
    local_md5 = hashlib.md5(content).hexdigest()
    media = MediaInMemoryUpload(content, mimetype="application/octet-stream")
    metadata = {"name": f"spike_q2_{name}"}
    file = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,size,md5Checksum",
        )
        .execute()
    )
    return {
        "name": name,
        "size_bytes": len(content),
        "local_md5": local_md5,
        "drive_md5Checksum": file.get("md5Checksum"),
        "match": local_md5 == file.get("md5Checksum"),
        "drive_size": file.get("size"),
        "file_id": file["id"],
    }


def cleanup(service, file_ids):
    for fid in file_ids:
        try:
            service.files().delete(fileId=fid).execute()
        except Exception as e:  # noqa: BLE001
            print(f"  cleanup failed for {fid}: {e}")


def main():
    service = build_drive_service()
    payloads = make_test_payloads()
    results = []
    for name, content in payloads.items():
        print(f"Uploading {name} ({len(content)} bytes)...")
        results.append(upload_and_compare(service, name, content))

    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "all_match": all(r["match"] for r in results),
        "mismatches": [r["name"] for r in results if not r["match"]],
        "results": results,
    }

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nResults written to {OUTPUT}")
    print(f"All match: {summary['all_match']}")

    print("\nCleaning up uploaded files...")
    cleanup(service, [r["file_id"] for r in results])


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script**

```bash
python -m spike.q2_md5_checksum
```

Expected: five `Uploading <name> (<size> bytes)...` lines, then `All match: True` (presumed; the spike confirms or denies), then cleanup. Total runtime ~10–30 seconds depending on the 6MB upload speed.

- [ ] **Step 3: Inspect results**

```bash
cat spike/outputs/q2_md5_results.json
```

Expected: a JSON object with `all_match`, an array of `results` each containing `local_md5`, `drive_md5Checksum`, `match`. If any `match: false`, dig in: print the file's full metadata via `service.files().get(fileId=..., fields='*')` and look for hints (revision, indexable text override, etc.).

- [ ] **Step 4: Commit**

```bash
git add spike/q2_md5_checksum.py
git commit -m "feat(spike): q2 md5Checksum vs hashlib.md5 comparison"
```

---

## Task 6: Q3 + Q6 — Change feed payloads (trash, hard-delete, move-out, `removed` reasons)

Spec §4.1 rows 3 and 6. REVIEW2 refs **A3** and **A4**.

These two questions ask for the same data (change-feed payloads after each kind of mutation), so one script captures all of them. The synthesis in `SPIKE_REPORT.md` answers Q3 and Q6 separately. Skip the "unshare" sub-case — it requires a second account.

**Files:**
- Create: `spike/q3_q6_change_feed.py`
- Outputs: `spike/outputs/q3_q6_payloads/{trash,hard_delete,move_out}.json` (gitignored)

- [ ] **Step 1: Write `spike/q3_q6_change_feed.py`**

```python
"""Q3 + Q6 (A3, A4): What does `changes.list` return for trash, hard-delete,
and move-out-of-scope mutations? What `removed` reasons appear?

Procedure:
  Set up a watched folder and a side folder (the "out of scope" destination).
  Create three files inside the watched folder. For each of the three
  mutations:
    1. Get a fresh `startPageToken`.
    2. Perform the mutation.
    3. Wait briefly (change feed has small lag).
    4. Call `changes.list` until exhausted, collecting every page.
    5. Save the full response.

  Skipped: "unshare" — requires a second Google account to share to and
  revoke from. Mark as a gap in SPIKE_REPORT.md if Stage 3 surfaces a
  concrete need.

What the spike informs (Stage 3):
  - Whether `removed=True` alone is sufficient to propagate a delete, or
    whether `change.file.trashed` / parent visibility must also be checked.
  - How a "move out of sync root" looks in the change feed (separate
    `removed`-style change vs. a normal change with new parents).
  - What concrete fields `_clean_remote_changes` should branch on.

Output: spike/outputs/q3_q6_payloads/{trash,hard_delete,move_out}.json
"""
import json
import os
import time
from datetime import datetime, timezone

from googleapiclient.http import MediaInMemoryUpload

from spike.auth import build_drive_service

OUTPUT_DIR = "spike/outputs/q3_q6_payloads"
CHANGE_FEED_LAG_SECONDS = 3


def get_token(service):
    return service.changes().getStartPageToken().execute()["startPageToken"]


def list_all_changes(service, page_token):
    pages = []
    while True:
        resp = (
            service.changes()
            .list(
                pageToken=page_token,
                fields="*",
                includeRemoved=True,
                spaces="drive",
            )
            .execute()
        )
        pages.append(resp)
        if "nextPageToken" in resp:
            page_token = resp["nextPageToken"]
        else:
            return pages


def create_folder(service, name, parent_id=None):
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]
    return service.files().create(body=body, fields="id,name").execute()


def create_file(service, parent_id, name):
    media = MediaInMemoryUpload(b"spike content for " + name.encode())
    return (
        service.files()
        .create(
            body={"name": name, "parents": [parent_id]},
            media_body=media,
            fields="id,name,parents",
        )
        .execute()
    )


def capture(service, action_name, action_fn):
    print(f"\n--- Capturing change feed for: {action_name} ---")
    token = get_token(service)
    print(f"  startPageToken: {token}")
    action_fn()
    time.sleep(CHANGE_FEED_LAG_SECONDS)
    pages = list_all_changes(service, token)
    out_path = os.path.join(OUTPUT_DIR, f"{action_name}.json")
    with open(out_path, "w") as f:
        json.dump(
            {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "start_token": token,
                "pages": pages,
                "total_changes": sum(len(p.get("changes", [])) for p in pages),
            },
            f,
            indent=2,
        )
    print(f"  Captured {sum(len(p.get('changes', [])) for p in pages)} change(s) -> {out_path}")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    service = build_drive_service()

    print("Setting up folders and files...")
    watched = create_folder(service, "spike_q3_watched")
    other = create_folder(service, "spike_q3_other")
    print(f"  watched={watched['id']} other={other['id']}")

    f_trash = create_file(service, watched["id"], "spike_trash.txt")
    f_hard = create_file(service, watched["id"], "spike_hard_delete.txt")
    f_move = create_file(service, watched["id"], "spike_move_out.txt")
    print(f"  files: trash={f_trash['id']} hard={f_hard['id']} move={f_move['id']}")

    # Let creates settle out of the change feed before we start measuring.
    time.sleep(CHANGE_FEED_LAG_SECONDS)

    capture(
        service,
        "trash",
        lambda: service.files()
        .update(fileId=f_trash["id"], body={"trashed": True})
        .execute(),
    )

    capture(
        service,
        "hard_delete",
        lambda: service.files().delete(fileId=f_hard["id"]).execute(),
    )

    capture(
        service,
        "move_out",
        lambda: service.files()
        .update(
            fileId=f_move["id"],
            addParents=other["id"],
            removeParents=watched["id"],
        )
        .execute(),
    )

    # Cleanup. files.delete on a folder is recursive in Drive.
    print("\nCleaning up folders...")
    for fid in [watched["id"], other["id"]]:
        try:
            service.files().delete(fileId=fid).execute()
        except Exception as e:  # noqa: BLE001
            print(f"  cleanup failed for {fid}: {e}")

    print(f"\nAll captures saved under {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script**

```bash
python -m spike.q3_q6_change_feed
```

Expected: prints folder/file IDs, then three `--- Capturing change feed for: <action> ---` blocks, each ending with `Captured N change(s) -> spike/outputs/q3_q6_payloads/<action>.json`. Cleanup at the end.

If `Captured 0 change(s)` for any action, increase `CHANGE_FEED_LAG_SECONDS` to 10 and re-run — the change feed can lag under load. If still zero changes, that itself is a finding to document.

- [ ] **Step 3: Inspect each payload**

```bash
ls spike/outputs/q3_q6_payloads/
python -c "import json; d=json.load(open('spike/outputs/q3_q6_payloads/trash.json')); print(json.dumps(d['pages'][0]['changes'], indent=2))"
python -c "import json; d=json.load(open('spike/outputs/q3_q6_payloads/hard_delete.json')); print(json.dumps(d['pages'][0]['changes'], indent=2))"
python -c "import json; d=json.load(open('spike/outputs/q3_q6_payloads/move_out.json')); print(json.dumps(d['pages'][0]['changes'], indent=2))"
```

For each, note:
- Is `change.removed` true or false?
- Is `change.file` populated? If yes, what's `file.trashed`, `file.parents`?
- Is there exactly one change entry for our file, or extras?

These observations are the inputs to Q3/Q6 sections of `SPIKE_REPORT.md`.

- [ ] **Step 4: Commit**

```bash
git add spike/q3_q6_change_feed.py
git commit -m "feat(spike): q3+q6 change feed payloads for trash/delete/move-out"
```

---

## Task 7: Q4 — `/`-in-name behavior

Spec §4.1 row 4, REVIEW2 ref **C1**.

**Files:**
- Create: `spike/q4_slash_in_name.py`
- Output: `spike/outputs/q4_findings.json` (gitignored)

- [ ] **Step 1: Write `spike/q4_slash_in_name.py`**

```python
"""Q4 (C1): Can you create a Drive file whose `name` contains `/`?

Procedure:
  Try `files.create(name='2024/05 report.pdf', parents=[<test_parent>])`.
  Branch on the result:
    - If the API returns 4xx, capture status and body. Stage 1's path-cache
      C1 policy is "reject `/` on admission" because Drive itself rejects.
    - If the API returns 200, capture the response and follow up with
      `files.list` and `files.get` to see how the name comes back. Stage 1's
      C1 policy then must be "escape `/` in path serialization" because
      Drive accepts names that look like paths.

What the spike informs (Stage 1):
  - The escape-vs-reject decision in path_cache.py, and the corresponding
    sanitizer for upload (Stage 4 C3).

Output: spike/outputs/q4_findings.json
"""
import json
import os
from datetime import datetime, timezone

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from spike.auth import build_drive_service

OUTPUT = "spike/outputs/q4_findings.json"


def main():
    service = build_drive_service()
    name = "2024/05 report.pdf"
    findings = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "requested_name": name,
    }

    parent = (
        service.files()
        .create(
            body={
                "name": "spike_q4_parent",
                "mimeType": "application/vnd.google-apps.folder",
            },
            fields="id",
        )
        .execute()
    )
    findings["parent_id"] = parent["id"]
    print(f"Created parent folder {parent['id']}")

    created_id = None
    try:
        media = MediaInMemoryUpload(b"spike q4 contents")
        created = (
            service.files()
            .create(
                body={"name": name, "parents": [parent["id"]]},
                media_body=media,
                fields="id,name,parents",
            )
            .execute()
        )
        created_id = created["id"]
        findings["create_succeeded"] = True
        findings["create_response"] = created
        print(f"  CREATE succeeded with name={created['name']!r}")

        listed = (
            service.files()
            .list(
                q=f"'{parent['id']}' in parents and trashed=false",
                fields="files(id,name,parents)",
            )
            .execute()
        )
        findings["list_response"] = listed
        print(f"  LIST returned {len(listed.get('files', []))} files")

        got = (
            service.files()
            .get(fileId=created_id, fields="id,name,parents")
            .execute()
        )
        findings["get_response"] = got
        print(f"  GET returned name={got['name']!r}")
    except HttpError as e:
        findings["create_succeeded"] = False
        findings["error_status"] = e.resp.status
        findings["error_reason"] = e.resp.reason
        findings["error_content"] = e.content.decode("utf-8", errors="replace")
        print(f"  CREATE failed: HTTP {e.resp.status} {e.resp.reason}")

    # Cleanup
    if created_id:
        try:
            service.files().delete(fileId=created_id).execute()
        except Exception:  # noqa: BLE001
            pass
    try:
        service.files().delete(fileId=parent["id"]).execute()
    except Exception:  # noqa: BLE001
        pass

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(findings, f, indent=2)
    print(f"\nFindings written to {OUTPUT}")
    print(f"create_succeeded: {findings.get('create_succeeded')}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script**

```bash
python -m spike.q4_slash_in_name
```

Expected: either `CREATE succeeded with name='2024/05 report.pdf'` followed by LIST and GET responses, or `CREATE failed: HTTP <code> <reason>`. Either branch is informative — both populate `findings`.

- [ ] **Step 3: Inspect**

```bash
cat spike/outputs/q4_findings.json
```

If `create_succeeded: true`, note whether `name` field comes back as `'2024/05 report.pdf'` (Drive preserves slashes) vs. truncated/escaped/transformed. Stage 1's C1 policy hinges on this.

- [ ] **Step 4: Commit**

```bash
git add spike/q4_slash_in_name.py
git commit -m "feat(spike): q4 slash-in-name behavior probe"
```

---

## Task 8: Q5 — Combined move + rename

Spec §4.1 row 5, REVIEW2 ref **A5**.

**Files:**
- Create: `spike/q5_combined_move_rename.py`
- Output: `spike/outputs/q5_payload.json` (gitignored)

- [ ] **Step 1: Write `spike/q5_combined_move_rename.py`**

```python
"""Q5 (A5): What does a combined move + rename look like in the change feed?

Procedure:
  Create folder A, folder B, and a file inside A. Get a startPageToken.
  Issue a single `files.update` that sets a new `name` AND switches
  `addParents` to B / `removeParents` from A. Inspect `changes.list`:
  is there one change entry covering both, or two?

What the spike informs (Stage 4):
  - Whether `path_cache.move()` must be atomic over name+parent in one call,
    or whether the change feed always splits them and the cache can apply
    them in sequence.

Output: spike/outputs/q5_payload.json
"""
import json
import os
import time
from datetime import datetime, timezone

from googleapiclient.http import MediaInMemoryUpload

from spike.auth import build_drive_service

OUTPUT = "spike/outputs/q5_payload.json"
CHANGE_FEED_LAG_SECONDS = 3


def main():
    service = build_drive_service()

    folder_a = (
        service.files()
        .create(
            body={
                "name": "spike_q5_A",
                "mimeType": "application/vnd.google-apps.folder",
            },
            fields="id",
        )
        .execute()
    )
    folder_b = (
        service.files()
        .create(
            body={
                "name": "spike_q5_B",
                "mimeType": "application/vnd.google-apps.folder",
            },
            fields="id",
        )
        .execute()
    )

    media = MediaInMemoryUpload(b"spike q5 contents")
    file = (
        service.files()
        .create(
            body={"name": "original.txt", "parents": [folder_a["id"]]},
            media_body=media,
            fields="id,name,parents",
        )
        .execute()
    )
    print(f"Created file {file['id']} in folder A ({folder_a['id']})")

    # Let the create settle out of the change feed.
    time.sleep(CHANGE_FEED_LAG_SECONDS)

    token = service.changes().getStartPageToken().execute()["startPageToken"]
    print(f"startPageToken: {token}")

    updated = (
        service.files()
        .update(
            fileId=file["id"],
            body={"name": "renamed.txt"},
            addParents=folder_b["id"],
            removeParents=folder_a["id"],
            fields="id,name,parents",
        )
        .execute()
    )
    print(f"Update response: {updated}")

    time.sleep(CHANGE_FEED_LAG_SECONDS)

    pages = []
    page_token = token
    while True:
        resp = (
            service.changes()
            .list(
                pageToken=page_token,
                fields="*",
                includeRemoved=True,
                spaces="drive",
            )
            .execute()
        )
        pages.append(resp)
        if "nextPageToken" in resp:
            page_token = resp["nextPageToken"]
        else:
            break

    relevant = [
        c
        for page in pages
        for c in page.get("changes", [])
        if c.get("fileId") == file["id"]
    ]

    findings = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "start_token": token,
        "update_response": updated,
        "all_pages": pages,
        "relevant_change_count_for_file": len(relevant),
        "relevant_changes": relevant,
    }

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(findings, f, indent=2)

    print(f"\nFindings written to {OUTPUT}")
    print(f"Number of change entries for the moved+renamed file: {len(relevant)}")

    # Cleanup
    for fid in [folder_a["id"], folder_b["id"]]:
        try:
            service.files().delete(fileId=fid).execute()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script**

```bash
python -m spike.q5_combined_move_rename
```

Expected: prints created file ID, then update response, then `Number of change entries for the moved+renamed file: <N>`. The most likely values are 1 (single combined entry) or 2 (separate name + parent changes), but the script handles any N.

- [ ] **Step 3: Inspect**

```bash
cat spike/outputs/q5_payload.json | python -c "import json,sys; d=json.load(sys.stdin); print('changes for file:', d['relevant_change_count_for_file']); print(json.dumps(d['relevant_changes'], indent=2))"
```

For each relevant change, note `change.file.name` and `change.file.parents`. If N=1: both new name and new parents are visible in one entry. If N>1: stage 4's path-cache `move()` only needs to be atomic per-entry, not across entries.

- [ ] **Step 4: Commit**

```bash
git add spike/q5_combined_move_rename.py
git commit -m "feat(spike): q5 combined move+rename change-feed shape"
```

---

## Task 9: Synthesize SPIKE_REPORT.md

After all five scripts have run successfully and you've reviewed each output, write the report. This is the single artifact preserved on `google-drive-port`.

**Files:**
- Create: `SPIKE_REPORT.md` (repo root, on the `spike/drive-validation` branch)

- [ ] **Step 1: Write the report skeleton**

Create `SPIKE_REPORT.md` at repo root. Use this exact structure (fill in `<…>` placeholders from `spike/outputs/`):

```markdown
# Stage 0 Drive Validation Spike — Report

**Date:** 2026-04-30
**Branch:** spike/drive-validation (deleted after this report is preserved)
**Inputs answered:** docs/superpowers/specs/2026-04-30-staged-port-plan-design.md §4.1
**Environment(s):** <e.g. exe.dev VM (Linux, headless), local macOS desktop>

## Summary table

| # | Question | REVIEW2 ref | Result | Effect on design |
|---|---|---|---|---|
| 1 | run_local_server in dev environments | B1 | <works/fails per env> | <confirms / refines / contradicts> |
| 2 | md5Checksum vs hashlib.md5 | (Stage 5 hashing) | <all match / mismatches in N> | <confirms / refines> |
| 3 | changes.list payload for trash/hard-delete/move-out | A3, A4 | <captured> | <confirms / refines> |
| 4 | `/` in file name | C1 | <accepted as literal / rejected with HTTP X> | <decides reject vs escape> |
| 5 | combined move+rename change entry shape | A5 | <N entries> | <atomicity requirement> |
| 6 | `removed` reasons | A4 | <removed=true alone sufficient? / must inspect file.trashed / parents> | <disambiguation rules> |

---

## Q1 (B1): OAuth `run_local_server` in dev environments

**Procedure run:** see `spike/q1_oauth_environments.py`. Modes A
(`run_local_server`) and B (paste-redirect) attempted.

**Observed (per environment):**

- Environment 1 — <name>: Mode A <SUCCESS/FAILED with cause>; Mode B <…>
- Environment 2 — <name> (if probed): Mode A <…>; Mode B <…>

**REVIEW2 B1 effect:** <confirms | refines | contradicts>. <One sentence on
how this should shape Stage 2's onboarding spec.>

---

## Q2: md5Checksum vs hashlib.md5

**Procedure run:** see `spike/q2_md5_checksum.py`. Five files uploaded
(small text, UTF-8 text, full-byte-range bytes, small random, 6MB random
crossing the simple/resumable boundary).

**Observed:**

| File | Size | Local md5 | Drive md5Checksum | Match |
|---|---|---|---|---|
| small_text.txt | 31 | <hex> | <hex> | <yes/no> |
| utf8_text.txt | <…> | <…> | <…> | <…> |
| non_ascii_bytes.bin | 1024 | <…> | <…> | <…> |
| small_binary.bin | 1024 | <…> | <…> | <…> |
| large_binary.bin | 6291456 | <…> | <…> | <…> |

**Stage 5 hashing effect:** <ContentHasher (MD5) maps directly to
md5Checksum / requires special-casing for X>.

---

## Q3 (A3): Change-feed payloads for trash, hard-delete, move-out

**Procedure run:** see `spike/q3_q6_change_feed.py`. Three mutations in
`spike_q3_watched/`. Unshare not run — see "Known gaps" below.

### Trash (`files.update(trashed=True)`)

```json
<paste minimal change entry from spike/outputs/q3_q6_payloads/trash.json>
```

Salient fields: `removed=<value>`, `file.trashed=<value>`, `file.parents=<value>`.

### Hard delete (`files.delete`)

```json
<paste minimal change entry from spike/outputs/q3_q6_payloads/hard_delete.json>
```

Salient fields: `removed=<value>`, `file=<populated/null>`.

### Move out of watched folder

```json
<paste minimal change entry from spike/outputs/q3_q6_payloads/move_out.json>
```

Salient fields: `removed=<value>`, `file.parents=<new value>`.

**REVIEW2 A3 effect:** <The rule for `_clean_remote_changes` is …>.

---

## Q4 (C1): `/` in file name

**Procedure run:** see `spike/q4_slash_in_name.py`. Attempted
`files.create(name='2024/05 report.pdf', parents=[...])`.

**Observed:** `create_succeeded=<true/false>`. <If true: name field
returned as `<exact string>`; LIST and GET responses are
consistent/inconsistent. If false: HTTP `<status>`, body `<message>`.>

**REVIEW2 C1 effect:** Stage 1 path_cache will <reject / escape> `/` in
names because Drive itself <rejects / accepts>. Stage 4 upload
sanitization will <reject / escape correspondingly>.

---

## Q5 (A5): Combined move + rename

**Procedure run:** see `spike/q5_combined_move_rename.py`. Single
`files.update(name='renamed.txt', addParents=B, removeParents=A)`.

**Observed:** `relevant_change_count_for_file=<N>`. <Concrete entries
shown below.>

```json
<paste from spike/outputs/q5_payload.json relevant_changes>
```

**REVIEW2 A5 effect:** Stage 4 path_cache `move()` <must / need not> be
atomic across name+parent because the change feed <emits one entry / emits
N entries>.

---

## Q6 (A4): `removed` reasons in change entries

**Source:** same captures as Q3 plus inspection of the `removed` field
across all three mutations.

| Mutation | removed | file populated? | file.trashed | Notes |
|---|---|---|---|---|
| trash | <T/F> | <yes/no> | <T/F> | |
| hard_delete | <T/F> | <yes/no> | n/a | |
| move_out | <T/F> | <yes/no> | <T/F> | |

**REVIEW2 A4 effect:** `removed=true` alone is <sufficient / not
sufficient> to propagate a delete. <Concrete disambiguation rule for
Stage 3.>

---

## Known gaps

- Q3 unshare scenario not run — requires a second Google account. Mark
  for follow-up if Stage 3 surfaces a concrete need; until then,
  treating the unshare case the same as move-out is the working
  assumption.
- <Anything else that surfaced during the spike but wasn't on the
  question list.>

## Recommended updates to staged-port-plan-design.md

- §4.1 row N: <if any spike answer contradicted or refined the
  procedure / informs column, list the change here.>
- §5.X (Stage X): <if any stage's "In scope" needs adjustment based on
  findings, list it.>

If no updates required, state "No updates required — all answers
confirmed REVIEW2 assumptions."

## Stage 1 readiness

With Q1, Q4, Q3, Q5, Q6 answered, Stage 1's path-cache spec can be
written without further blockers on:
- C1 escape-vs-reject (Q4)
- Concurrency/move atomicity (Q5)
- Cache-side `removed` interpretation (Q3, Q6)

Stage 2's auth spec is unblocked on B1 (Q1).
Stage 5's hashing plan is unblocked on Q2.
```

- [ ] **Step 2: Fill in the placeholders from `spike/outputs/`**

For each `<…>` and each section's JSON paste-target, copy from the matching output file. Keep JSON snippets to the minimal fields that prove the point — don't paste 200-line dumps. The full payload remains in `spike/outputs/` for anyone who needs to re-derive.

When pasting JSON snippets, include only the keys that are load-bearing (`removed`, `file.id`, `file.name`, `file.trashed`, `file.parents`, `file.mimeType`). Strip the `kind`, `time`, `teamDriveId` fields unless they turned out to matter.

- [ ] **Step 3: Re-read the spec §4.1 with the report open and verify each row is answered**

```bash
cat docs/superpowers/specs/2026-04-30-staged-port-plan-design.md | sed -n '/4.1 Questions to answer/,/4.2 Deliverable/p'
```

For each row 1–6 in the spec table: confirm the report has a section that addresses it, with concrete data (not "<…>" placeholders left over).

- [ ] **Step 4: Commit**

```bash
git add SPIKE_REPORT.md
git commit -m "docs(spike): SPIKE_REPORT.md with Q1-Q6 findings"
```

---

## Task 10: Preserve report on `google-drive-port` and delete spike branch

The spike branch and its scripts are intentionally throwaway. Only `SPIKE_REPORT.md` survives. We move it to a permanent location under `docs/superpowers/spikes/` and update the staged-port design with any required revisions.

**Files (on `google-drive-port`):**
- Create: `docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md`
- Modify (if findings dictate): `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md`

- [ ] **Step 1: Switch back to `google-drive-port`**

```bash
git checkout google-drive-port
```

- [ ] **Step 2: Pull `SPIKE_REPORT.md` from the spike branch into the docs path**

```bash
mkdir -p docs/superpowers/spikes
git show spike/drive-validation:SPIKE_REPORT.md > docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md
```

Verify:

```bash
head -10 docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md
```

Expected: the title `# Stage 0 Drive Validation Spike — Report` and the `**Date:** 2026-04-30` line.

- [ ] **Step 3: Apply any "Recommended updates to staged-port-plan-design.md" the report flagged**

If the report's "Recommended updates" section says **No updates required**, skip to Step 4.

Otherwise: open `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md` and apply the listed changes inline. Examples of what this might look like:
- §4.1 row update: refine the "What it informs" cell with the actual answer (e.g. "Q4: Drive accepts `/` literally — Stage 1 escapes; Stage 4 mirror-escapes").
- §5.X scope update: e.g. moving an item from "in scope" to "out of scope" if the spike showed it's unnecessary.
- §9 open question resolution: cross out items the spike answered.

Each change should be a small targeted edit, not a rewrite. Keep the surrounding structure.

- [ ] **Step 4: Commit on `google-drive-port`**

```bash
git add docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md
# `git add` on an unchanged path is a no-op, so this is safe whether or not
# Step 3 modified the spec file:
git add docs/superpowers/specs/2026-04-30-staged-port-plan-design.md
git status
git commit -m "docs: preserve Stage 0 spike report and fold findings into staged-port design"
```

`git status` before commit lets you confirm exactly which files are staged — useful for verifying that the spec file is included only if Step 3 actually modified it.

- [ ] **Step 5: Confirm with the user before deleting the spike branch**

Before the next step, **stop and confirm with the user**:

> "Stage 0 spike report is now preserved at `docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md` on `google-drive-port`. The `spike/drive-validation` branch (containing throwaway scripts in `spike/` and the original `SPIKE_REPORT.md` at repo root) is ready to be deleted. Confirm to delete?"

Do not skip this confirmation. Per the design spec the branch is throwaway, but verifying the report is correctly preserved before destroying the working branch is cheap insurance against losing data.

- [ ] **Step 6: Delete the spike branch (only after user confirmation in Step 5)**

```bash
git branch -D spike/drive-validation
```

If the branch was pushed to a remote (it should not have been, but verify):

```bash
git ls-remote --heads origin spike/drive-validation
```

If a remote ref exists, ask the user whether to delete it:

```bash
# Only if user confirms:
git push origin --delete spike/drive-validation
```

- [ ] **Step 7: Final state verification**

```bash
git branch | grep spike/drive-validation || echo "spike branch deleted"
ls docs/superpowers/spikes/
git log --oneline -5
```

Expected: `spike branch deleted`, the new spike report listed, and the most recent commit on `google-drive-port` is the one that preserved the report.

---

## Definition of Done

Per spec §7 ("Definition of stage complete") for Stage 0:

1. ✅ Spec written and committed: `docs/superpowers/specs/2026-04-30-staged-port-plan-design.md`
2. ✅ Implementation plan written and committed (this file)
3. Spike branch worked through tasks 1–9
4. SPIKE_REPORT.md preserved on `google-drive-port` at `docs/superpowers/spikes/2026-04-30-drive-validation-spike-report.md`
5. Any spec contradictions or refinements folded back into `2026-04-30-staged-port-plan-design.md`
6. Spike branch deleted (after user confirmation)

Stage 0 has no manual smoke test step (it has no user-visible behavior — the deliverable is a document).

After this is done, the next step is brainstorming Stage 1 (path cache final form), per spec §10.
