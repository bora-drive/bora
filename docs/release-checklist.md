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

- [ ] **OAuth app verification (B2).** Submit the Google OAuth app for
      verification. Until it clears, the consent screen stays in "Testing" mode
      and refresh tokens expire after 7 days — document the
      add-yourself-as-a-test-user workaround (already in `docs/setup.md`) for
      contributors and early users.
- [ ] **Credential rotation policy (D5).** Document how the shipped
      `client_secret.json` will be rotated if needed (release note +
      deprecation timeline) so an old shipped credential can be retired without
      poisoning the project's abuse quota. Confirm the client is restricted to
      the intended Drive scopes and project before publishing.
- [ ] **Windows smoke test.** Install and run a basic link + sync on Windows
      (out of reach from the Linux dev VM).
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
