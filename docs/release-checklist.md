# Bora v0.1.0 Release Checklist

Manual / off-VM steps to take Bora from "code-complete on the branch" to a
published release. None of these are done automatically; check each off
deliberately.

## Pre-release — external identities still named for earlier codenames

These referenced real external resources named for earlier codenames; all
resolved as of 2026-07-20:

- [x] **OAuth client / GCP project.** Done 2026-07-20: the shipped
      `src/bora/resources/client_secret.json` now belongs to the Bora-named
      Google Cloud project `bora-500821` (OAuth client "bora"), replacing the
      original `cumulus-496101`. Still confirm before publishing (see rotation
      policy below) that the client is restricted to the Drive scopes actually
      used.
- [x] **Update-check / repo URLs.** Done 2026-07-20: `GITHUB_RELEASES_API`
      points at `indirect/bora` (the repo is private until public release, so
      `bora check-for-updates`-style calls fail cleanly with an update-check
      error until then); the stray `samschott/bora` issue URLs in
      `src/bora/sync.py` / `src/bora/models.py` were restored to the upstream
      `samschott/maestral` issues they cite; the `https://bora.tools`
      placeholder was replaced with the GitHub repo URL in `pyproject.toml`,
      `README.md`, the Homebrew formula and the systemd unit.
- [x] **Test account email.** Done 2026-07-20: the linked-test account is
      `bora-test@arko.net` (documented in `tests/README.md`), provisioned under
      the Bora name; the old `cumulus@arko.net` smoke account is no longer
      referenced.

## Pre-release — verification & accounts

- [x] **OAuth app verification (B2).** Done 2026-07-21: the consent screen for
      `bora-500821` was published to "In production" (unverified). Confirmed
      in the console that it requests only the single
      `https://www.googleapis.com/auth/drive` scope, and that the project has
      only the one "bora" Desktop OAuth client — no extra clients or scopes.
      Linking now shows Google's "unverified app" warning (Advanced →
      continue) and the app is capped at 100 total users until full
      verification. Full verification (public homepage + privacy policy,
      in-app scope justification, demo video, annual CASA assessment) is
      deferred to the public release — see `docs/setup.md`.
- [x] **Credential rotation policy (D5).** Done 2026-07-21: rotation policy
      documented in `docs/credential-rotation.md` (when to rotate, the
      new-client → patch-release → deprecation-window → old-client-deletion
      mechanics, and the pre-publish confirmation checklist). The console
      confirmation itself — client belongs to `bora-500821`, only the `drive`
      scope, no extra clients/scopes — was run as part of the B2 publish
      above and matches the checklist in `docs/credential-rotation.md`.
- [x] **Windows is not currently supported.** Verified 2026-07-20: Bora
      cannot install-and-run on Windows today — `fcntl`/`xattr` are
      imported unconditionally in `src/bora/utils/path.py` and
      `src/bora/daemon.py` (both POSIX-only), `xattr` is an unconditional
      dependency with no Windows wheel, and
      `src/bora/utils/appdirs.py` raises `RuntimeError("Platform not
      supported")` for every path lookup on anything but Darwin/Linux.
      This matches Bora's own declared platform support (pyproject
      classifiers, CLI `--help`). See `docs/windows-smoke-test.md` for the
      full blocker list, what a port would need, and the smoke-test
      checklist to run once one exists. **Decision (Andre, 2026-07-21):
      DEFERRED past v0.1.0** — v0.1.0 ships macOS/Linux only, consistent
      with `docs/windows-smoke-test.md`; a Windows port is out of scope for
      this release.
- [x] **Real-account end-to-end smoke.** Done 2026-07-20 against
      `bora-test@arko.net` (config `smoke-test`): linked via `bora auth link
      --refresh-token`, synced a small tree both directions (local upload via
      inotify, remote download within one poll cycle), `bora status` /
      `bora auth status` show the account email/type/usage, 0 sync errors.
      Surfaced and fixed a piped-`bora ls` empty-output bug along the way.

## Packaging

- [x] **systemd user service template.** Done: `packaging/bora-daemon@.service`,
      a template user unit (instance name = config name, matching the
      `bora-daemon@<config>.service` name that `bora autostart` generates in
      `src/bora/autostart.py`). Uses `Type=notify` + `WatchdogSec=30s` — the
      daemon sends READY/WATCHDOG/STOPPING via sd_notify (`src/bora/daemon.py`).
      Install/usage notes are in the unit's comment header. Not wired into
      `pyproject.toml` data-files (neither is `packaging/bora.desktop`); distro
      packages should install it to `/usr/lib/systemd/user/`.
- [ ] **Homebrew formula.** A template exists at `packaging/homebrew/bora.rb`
      (homebrew-core Python-virtualenv pattern; upstream maestral now ships
      only a cask, so the formula follows the standard pattern instead). At
      release time it still needs: the real sdist/tarball `url` + `sha256`
      (currently a git-URL placeholder, since Bora is not on PyPI), and the
      resource blocks regenerated to the full transitive dependency set with
      `brew update-python-resources packaging/homebrew/bora.rb` — the
      checked-in blocks cover direct pyproject.toml deps with PLACEHOLDER
      checksums only. Also re-check the `python@3.x` dependency then.

## Cut the release

- [ ] Confirm the offline suite is green (`pytest tests/offline -m "not load"`)
      and a clean-venv `pip install .` works with `bora --version` → 0.1.0
      (verified during Stage 8 Task 10).
- [ ] Bump the version if needed (`bump2version`); currently `0.1.0`.
- [ ] Build artifacts: `python -m build` → sdist + wheel in `dist/`.
- [ ] (Optional) Upload to PyPI; create a GitHub release using the CHANGELOG
      v0.1.0 notes.
