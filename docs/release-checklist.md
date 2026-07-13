# Bora v0.1.0 Release Checklist

Manual / off-VM steps to take Bora from "code-complete on the branch" to a
published release. None of these are done automatically; check each off
deliberately.

## Pre-release — external identities still named for earlier codenames

These reference real external resources and require re-provisioning to change;
the bundled values still say "cumulus":

- [ ] **OAuth client / GCP project (`cumulus-496101`).** The shipped
      `src/bora/resources/client_secret.json` belongs to Google Cloud project
      `cumulus-496101`. Decide whether to keep it or re-create the OAuth client
      under a Bora-named project before public release, and (see rotation
      policy below) restrict it to the Drive scopes actually used.
- [ ] **Update-check / repo URLs.** `src/bora/constants.py` `GITHUB_RELEASES_API`
      points at `https://api.github.com/repos/samschott/bora/releases`, which
      does not exist — the auto-update check will 404. Set it to the real Bora
      repository, or disable the update check for v0.1.0. Also fix the
      `samschott/bora` issue URLs in `src/bora/sync.py` and `src/bora/models.py`,
      and replace the `https://bora.tools` homepage placeholder in
      `pyproject.toml` / docs if the real site/repo differs.
- [ ] **Test account email (`cumulus@arko.net`).** `tests/README.md` documents
      the dedicated test account by its real address; update if the account is
      re-provisioned under a Bora name.

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
- [ ] **Real-account end-to-end smoke.** Link a live Google account
      (`bora auth link`), sync a small tree, and confirm `bora status` /
      `bora auth status` now show the account email/type/usage (the Stage 8
      account-state fix). NOTE: the rebrand changed the on-disk config identity
      to `bora`, so any pre-rebrand `cumulus` link must be re-created.

## Cut the release

- [ ] Confirm the offline suite is green (`pytest tests/offline -m "not load"`)
      and a clean-venv `pip install .` works with `bora --version` → 0.1.0
      (verified during Stage 8 Task 10).
- [ ] Bump the version if needed (`bump2version`); currently `0.1.0`.
- [ ] Build artifacts: `python -m build` → sdist + wheel in `dist/`.
- [ ] (Optional) Upload to PyPI; create a GitHub release using the CHANGELOG
      v0.1.0 notes.
