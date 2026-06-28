# Stage 8 — Docs + Release (rebrand to **Bora**)

**Date:** 2026-06-28
**Branch:** `google-drive-port`
**Status:** design / spec
**Prereq:** Stage 7 complete (7a–7c, all P0/P1/P2 closed; offline suite green, mypy/flake8 parity)

---

## 1. Goal

Take the Google-Drive port from "feature-complete on a working branch" to
"releasable as an independently-named project." Stage 8 is the final stage of
the staged port. It produces:

1. A **single consistent product name** — the project ships as **Bora**
   (CLI command `bora`, domain `bora.tools`, bundle id `tools.bora.app`).
2. **Clean public surface** — no leftover Dropbox/Maestral/cumulus identifiers
   or backward-compat aliases in shipped code.
3. **Buildable, accurate docs** — Sphinx builds; README/setup/user/differences
   docs describe Bora-on-Google-Drive, not Maestral-on-Dropbox.
4. A **release checklist** capturing the manual steps that cannot be done
   autonomously from this VM (OAuth-app verification submission, Windows smoke
   test, tag/build/publish, real-account end-to-end smoke).

No new sync behavior is introduced. This is naming, docs, packaging, alias
removal, and a release-readiness audit.

## 2. Naming decisions (authoritative)

These are fixed for this stage:

| Thing | Value |
|---|---|
| Product / brand name | **Bora** |
| CLI command / console-script | `bora` |
| Python top-level package | `bora` (rename `src/cumulus/` → `src/bora/`) |
| Distribution name (`pyproject` `name`) | `bora` |
| Main class | `Bora` (was `Maestral`) |
| Daemon proxy class | `BoraProxy` (was `MaestralProxy`) |
| `APP_NAME` | `"Bora"` (was `"Cumulus"`) |
| `BUNDLE_ID` | `"tools.bora.app"` (was `"com.cumulus.app"`) |
| On-disk config dir (`CONFIG_DIR_NAME`) | `"bora"` (was `"cumulus"`) |
| Test env vars | `BORA_TEST_REFRESH_TOKEN`, `BORA_TEST_ACCESS_TOKEN`, `BORA_AUTH_PORT`, `BORA_CLIENT_SECRET_PATH` (was `CUMULUS_*`) |
| GUI extra packages | `bora-qt`, `bora-cocoa` (was `cumulus-qt`/`cumulus-cocoa`) |
| Icon resource | `bora.png` (was `maestral.png`) |
| Homepage URL | `https://bora.tools` |
| Version | `0.1.0` (unchanged) |

This folds **two** rename axes into one sweep: residual upstream `Maestral`
identifiers *and* the port's interim `cumulus` codename both become `bora`.

### 2.1 What is preserved (legitimate attribution — do NOT scrub)

- `LICENSE.txt` — MIT © 2018 Sam Schott (the upstream author). Keep; a fork
  may add its own copyright line but must retain the original.
- README "fork of Maestral" credit + link to
  `https://github.com/samschott/maestral`.
- Accurate technical-comparison comments in source (e.g. "Dropbox used a
  long-poll; Google Drive has no equivalent, so we poll"). These document a
  real design decision and stay.
- `tests/README.md` references to the upstream test methodology where they
  describe behavior, not branding.

### 2.2 What is explicitly NOT renamed

- **Historical planning records** under `docs/superpowers/specs/` and
  `docs/superpowers/plans/` (Stages 0–7). These are a dated record of the port
  under its then-codename "cumulus" and the pre-rename "Maestral" source. We do
  not rewrite history. New Stage 8 docs are authored under `bora`. A single
  note is added to `MEMORY.md` / project docs recording the cumulus→bora
  rename and its date so the historical name is traceable.
- `docs/superpowers/references/dropbox-vs-drive-differences.md` keeps its
  technical content; a **user-facing** differences doc is authored separately
  (W3) that may reference or summarize it.

### 2.3 Breaking change acknowledged

Renaming `CONFIG_DIR_NAME` (`cumulus`→`bora`), `BUNDLE_ID`, the keyring service
identity, and the `CUMULUS_*` env vars **orphans any existing dev/test install**
— stored OAuth tokens and config under the old name will not be found, so the
test account must be re-linked after the rename. This is acceptable because the
project is pre-release (`0.1.0`, never published) with no external users. The
release checklist (W6) records that the first published build establishes the
`bora` on-disk identity; there is no migration shim.

## 3. Workstreams

Stage 8 is decomposed into seven workstreams. **W0 runs first** (per the
release-readiness-review requirement) and its findings may add tasks to or
re-order W1–W6. W1–W2 are code; W3–W4 are docs; W5–W6 are packaging/release.

### W0 — Release-readiness review (FIRST)

A full-project audit, **before** the rename churn, to find anything that would
block a release or leave the CLI incomplete for Google Drive. This is a
read-only investigation that produces a findings list; fixes are folded into
the appropriate workstream (or a new one if material). Audit dimensions:

1. **CLI completeness** — enumerate every `bora`/`cumulus` CLI command and
   subcommand; for each, confirm it is wired to a working Google-Drive code
   path (not a Dropbox-only stub, not raising `NotImplementedError`, not
   referencing removed Dropbox SDK calls). Flag any command that is dead,
   Dropbox-specific, or silently broken.
2. **Import / dead-path integrity** — no module imports a removed Dropbox SDK;
   no unreachable Dropbox-era branches that would crash if hit; `python -c
   "import bora; import bora.cli"` style smoke after rename.
3. **Dependency hygiene** — every imported third-party package is declared in
   `pyproject.toml`; no leftover `dropbox`/Dropbox-SDK dependency; optional
   extras resolve.
4. **Entry point / packaging** — `bora = "bora.cli:main"` resolves; `pip
   install .` in a clean venv succeeds; `bora --help` runs.
5. **Resource / data files** — `client_secret.json`, icon, `py.typed`,
   `package-data` globs all present and shipped.
6. **Residual-name scan** — grep shipped surface for `maestral`, `cumulus`,
   `dropbox`, `dbx`, `Maestral`, `Cumulus`, `Dropbox` and classify each
   remaining hit as (a) rename target, (b) legitimate attribution, or (c)
   alias to remove.
7. **Test/CI signal** — offline suite green at HEAD before starting; note any
   xfail/skip that hides a release blocker.

**Output:** a written findings list (committed, e.g. as a section in the
deviations doc or an inline checklist) that the rest of the stage executes
against. The review may legitimately conclude "no new blockers" for some
dimensions — that is a valid, recorded result.

**W0 ran 2026-06-28; outcome recorded in §7.** It found two confirmed release
blockers, both addressed by the new workstream **W0.5** below; everything else
was clean or folded into W1/W5/W6 as minor items.

### W0.5 — Release blockers from the W0 review

1. **Cut the `sharelink` command group** (Blocker 1). `sharelink
   create|revoke|list` call `_not_implemented()` (`client.py`), which raises
   `NotImplementedError` — uncaught by `convert_api_errors` (`cli/common.py`),
   so they crash with a raw traceback. The CLI options (`--password`,
   `--expiry`, `LinkAudience`/`LinkAccessLevel`) are Dropbox shared-link
   semantics that do not map to Drive. **Decision: remove the `sharelink`
   group from the CLI for v0.1.0** and remove its now-unused client stub
   methods (`create_shared_link`, `revoke_shared_link`, `list_shared_links`).
   Audit `share_dir` (`client.py:885`, also a stub) for reachability from the
   sync path; if unreachable, remove it too, otherwise leave it but ensure it
   cannot crash a sync. Real Drive sharing is **deferred** (see §4.1). The
   user-facing differences doc (W3) notes "shared links not yet supported."

2. **Persist account info to state** (Blocker 2). `status`, `auth status`, and
   `config-files` read `email`/`type`/`usage` from the `account` state section,
   but the Drive port fetches account info via `about.get` into an in-memory
   cache only and never writes those keys (upstream wrote them in
   `get_account_info`/`get_space_usage`). Fix: when account info / space usage
   is fetched, persist `email`, `type` (e.g. "personal"), and a formatted
   `usage` string to the `account` state section so the CLI displays them.
   Add/extend a test asserting the state keys are written after a fetch.

### W1 — Full rename to Bora

- `git mv src/cumulus src/bora`; update every `from cumulus`/`import cumulus`
  to `bora` across `src/`, `tests/`, and shipped docs.
- `pyproject.toml`: `name = "bora"`; `[project.scripts] bora = "bora.cli:main"`;
  optional extras `bora-qt`/`bora-cocoa`; `tool.setuptools.package-data` key
  `bora`; data-files icon path; `urls.Homepage = "https://bora.tools"`.
- Rename classes `Maestral`→`Bora`, `MaestralProxy`→`BoraProxy` and all
  references (`__all__`, type hints, docstring `>>>` examples, daemon RPC
  registration, CLI factory `m = Bora(config_name)`).
- Constants: `APP_NAME="Bora"`, `BUNDLE_ID="tools.bora.app"`,
  `CONFIG_DIR_NAME="bora"`.
- Env vars: `CUMULUS_*` → `BORA_*` in source and tests; update
  `tests/README.md`.
- Rename icon resource `maestral.png`→`bora.png`; update `pyproject.toml` and
  `docs/conf.py` references.
- User-facing strings: "Maestral"/"Cumulus" → "Bora" (e.g. "Please relink
  Bora to get a new access token").
- Delete stale build artifacts `src/maestral.egg-info/` and
  `src/cumulus.egg-info/`.
- W0 minor items: rename the leftover `dbid` var/label in `auth status`
  (`cli/cli_core.py:461`) to a Drive-appropriate "Remote ID"; clean the
  `models.py:110` docstring reference to the removed `dropbox.files.Metadata`.
- Preserve everything in §2.1; do not touch §2.2.

### W2 — Alias removal

Remove the backward-compat aliases that earlier stages left with
"remove in Stage 8" / "Backward compat" markers, migrating call-sites:

- `DropboxClient = GoogleDriveClient` (`client.py`) → use `GoogleDriveClient`.
- `DropboxServerError`, `DropboxConnectionError`, `NoDropboxDirError`,
  `DropboxAuthError` (`exceptions.py`) → use the canonical names.
- `dbx_path` / `dbx_path_from` compat params/properties on the API-error class
  → use the canonical `*_path` names.

Verify no shipped code or test depends on the removed names; offline suite
green after migration.

### W3 — Docs (Sphinx + user-facing)

- `docs/conf.py`: `autoapi_dirs = ["../src/bora"]`; `project`/`title` → Bora;
  `html_logo = "bora.png"`; remove the dead `dropbox` intersphinx mapping.
- `docs/index.rst`: `autoapi/cumulus|maestral/*` paths → `autoapi/bora/*`;
  import examples → `from bora.main import Bora`; Dropbox → Google Drive in
  prose.
- **Setup guide**: Google Cloud project + OAuth client setup, including the
  "add yourself as a test user" workaround for unverified apps (ties to B2).
- **README rewrite**: Bora branding, `bora` CLI commands, install
  (`pip install bora` / `bora[gui]`), Google Drive folder semantics, remove
  dead Dropbox docker image and screenshots, fix links. Keep the fork-of-
  Maestral attribution (§2.1).
- **User-facing differences doc**: "Differences from Dropbox / Maestral
  upstream," summarizing the behavioral deltas (no long-poll → 180 s polling
  latency [D6], sharing model, path semantics, etc.), referencing the
  dev-facing reference doc.
- Build the Sphinx docs (`sphinx-build` via the `docs` extra) to verify they
  compile with no broken autoapi/toctree references.

### W4 — Housekeeping

- `PLAN.md` §3.2: 30 s → 180 s polling claim.
- `PLAN.md` / `TODO.md`: residual `maestral`/`cumulus`/`Dropbox` filename and
  naming sweep (shipped/forward-looking references only; not the historical
  superpowers records per §2.2).
- **CHANGELOG**: delete the upstream Maestral history; create a fresh
  `CHANGELOG.md` starting at `v0.1.0` summarizing the fork (Google Drive
  engine, rename to Bora, 180 s polling, known limitations).

### W5 — Packaging polish

- Add `oauthlib` to `dependencies` (imported directly at `client.py:37` but
  currently only resolved transitively via google-auth-oauthlib — W0 finding).
- `pip install .` in a clean venv; `bora --help` and a no-network CLI
  walkthrough (`bora --version`, `bora config-files`, etc. — read-only
  commands) succeed.
- Add a Linux `.desktop` entry for GUI launch; document that systemd user
  units are generated dynamically by `autostart.py` (no static template
  needed).
- Confirm `urls.Homepage` and any in-code/docs URLs point at `bora.tools` or
  the real repo; flag remaining placeholders.

### W6 — Release checklist doc (manual steps)

A committed checklist (e.g. `docs/release-checklist.md`) capturing the steps
that must be run by a human / off-VM:

- **B2** — submit the Google OAuth app for verification; until it clears,
  document the add-as-test-user workaround for contributors/early users.
- **D5** — credential-rotation policy for `client_secret.json` (release-note +
  deprecation timeline so an old shipped credential can be retired cleanly).
- **Windows** smoke test (out of reach from this Linux VM).
- **Tag / build / publish** — version bump procedure (`bump2version`), build
  sdist+wheel, optional PyPI upload, GitHub release.
- **Real-account end-to-end smoke** — the "stage complete" manual confirmation
  against a live Google Drive account (re-link required after the rename per
  §2.3).

## 4. Out of scope (deferred, recorded in W6)

- Actually submitting the OAuth verification, running the Windows test, and
  cutting/publishing the release — these are human/off-VM actions.
- GUI sub-packages (`bora-qt`, `bora-cocoa`) — separate distributions; Stage 8
  only updates their names in the extras list.
- Any new sync-engine behavior or bug fixes beyond what W0 surfaces as a
  genuine release blocker.

### 4.1 Deferred to a later version (post-v0.1.0 backlog)

- **Drive sharing API** — re-introduce `sharelink` (create/revoke/list) built
  on Drive's permissions API (`permissions().create` → `webViewLink`, revoke,
  list), with CLI options modeled on Drive's sharing model rather than
  Dropbox's link semantics, and live validation. Cut from v0.1.0 per the W0
  decision (§7); the differences doc notes it is unsupported for now.

## 5. Definition of done

1. Spec + plan committed under `docs/superpowers/`.
2. W0 findings recorded; any blocker either fixed or explicitly deferred with
   rationale.
3. Rename complete: a grep of the **shipped** surface (`src/`, `tests/`,
   `pyproject.toml`, user-facing `docs/`) for `maestral`/`cumulus`/`Maestral`/
   `Cumulus` returns only intentional attribution (§2.1) or historical records
   (§2.2); no `Dropbox*` aliases remain.
4. Offline test suite green (mypy + flake8 parity with the pre-stage baseline);
   load tests unaffected.
5. Sphinx docs build with no broken references.
6. `pip install .` in a clean venv succeeds and `bora --help` runs.
7. Release checklist committed.
8. (Manual, tracked in checklist, not gating this stage's code-complete:
   real-account smoke, OAuth verification, Windows test, publish.)

## 6. Risks

| Risk | Mitigation |
|---|---|
| Rename churn breaks an import or RPC name silently | W0 import-integrity audit + full offline suite + `bora --help` smoke after W1 |
| Config-dir/keyring rename orphans the live test link mid-validation | Acknowledged (§2.3); re-link as an explicit step in W6 real-account smoke |
| Sweeping `cumulus` in historical superpowers docs rewrites the record | Scoped out (§2.2); only shipped/forward-looking surfaces are renamed |
| Sphinx autoapi silently emits empty docs if `autoapi_dirs` wrong | W3 builds docs and checks for broken references as an acceptance gate |
| Alias removal misses an internal call-site | W2 migrates call-sites then runs the full suite; mypy strict catches unresolved names |

## 7. W0 review outcome (recorded 2026-06-28)

Ran against branch HEAD (`fb5834a`) via three audits (CLI completeness,
import/dependency integrity) plus a direct CLI smoke. Offline baseline
known-green at `afde90c` (648 passed). No account currently linked.

**Verdict: 2 confirmed release blockers (both handled in W0.5); no leftover
Dropbox SDK; all 25 modules import; all resources present; 22/24 CLI commands
correctly wired to working Drive/local code paths.**

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | 🔴 blocker | `sharelink create/revoke/list` are stubs that raise uncaught `NotImplementedError`; Dropbox-only link semantics | **Cut** for v0.1.0 (W0.5-1); sharing deferred (§4.1) |
| 2 | 🔴 blocker | account `email`/`type`/`usage` never persisted to state → `status`/`auth status`/`config-files` show `--` when linked | **Fix** — persist on fetch (W0.5-2) |
| 3 | 🟡 minor | `oauthlib` imported directly but only transitively declared | Add to `dependencies` (W5) |
| 4 | 🟡 minor | stale `src/maestral.egg-info/` lists a `dropbox` requirement | Delete (W1) |
| 5 | 🟡 minor | `models.py:110` docstring references removed `dropbox.files.Metadata` | Clean up (W1) |
| 6 | 🟡 minor | `auth status` `dbid` var/"Remote ID" label leftover | Rename in sweep (W1) |
| 7 | 🟡 note | live `client_secret.json` committed (expected for installed-app flow) | Flag rotation in checklist (W6/D5) |

Confirmed-OK (no action): revisions/`diff`/`restore` (real Drive revision
download+reupload), `auth link`/`unlink`/`status` OAuth flow, `ls` with Drive
`shared` field, selective-sync, `activity`/`history`/`filestatus`, daemon
lifecycle, `config`/`log`/`notify`/`bandwidth-limit`/`autostart`/`move-dir`/
`rebuild-index`/`completion`. No leftover Dropbox SDK import or call anywhere.
