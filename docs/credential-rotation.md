# Credential Rotation Policy

This page describes how the bundled OAuth client — the `client_secret.json`
shipped at `src/bora/resources/client_secret.json`, belonging to the Google Cloud
project `bora-500821` (OAuth client "bora") — gets rotated if it ever needs to be,
and what happens to existing users when that occurs. See
[Setup guide](setup.md) for how the client is used day to day, and the
[release checklist](release-checklist.md) (item D5) for the pre-publish
confirmation this policy depends on.

---

## When rotation happens

Rotation is only needed if the shipped credential itself becomes a liability, not for
routine maintenance. The concrete triggers are:

- **Leak.** The `client_secret.json` value is exposed somewhere it shouldn't be (a
  public commit, an unintended package artifact, a compromised build pipeline, etc.).
  An OAuth Desktop-app client secret is not meant to be truly secret (it can't stop a
  determined actor from extracting it from a distributed binary), but a *known* public
  leak is still a reason to retire the client, since it invites deliberate abuse.
- **Abuse-quota poisoning.** Someone uses the shipped client's credentials outside of
  Bora to hammer the Drive API, burns down the project's shared request quota, or
  triggers Google's abuse-detection systems against `bora-500821`. Because quota and
  abuse signals are scoped to the OAuth client/project, this can degrade or block
  Bora's Drive API access for every legitimate user until the offending client is
  retired.
- **Planned retirement.** A deliberate decision to rotate on a schedule (e.g. as part
  of hardening ahead of full Google verification), independent of any incident.

## Mechanics

1. **Create a new OAuth client** in the `bora-500821` Google Cloud Console project
   (**APIs & Services > Credentials > Create Credentials > OAuth client ID**,
   **Desktop app** type), following the same steps as section 4, "Create an OAuth
   client (Desktop app)", in the [Setup guide](setup.md). Run the pre-publish
   confirmation checklist below against it before it ships.
2. **Ship the new `client_secret.json`** by replacing the bundled file at
   `src/bora/resources/client_secret.json` and releasing a patch version. This is the
   only way existing installs pick up the new client — Bora does not fetch credentials
   remotely.
3. **Announce the rotation in the release notes** for that patch, stating a
   deprecation window (a fixed number of days/weeks) during which the *old* client
   keeps working. Users who update during the window keep syncing uninterrupted with
   their existing refresh tokens (a refresh token is tied to the client that issued
   it, but Bora silently re-links only when the client changes underneath a token that
   no longer validates — practically, users should re-link once they update). Users
   who don't update before the window closes are the ones who feel the cutover.
4. **Delete the old client from the Cloud Console** once the deprecation window ends.
   Deleting an OAuth client immediately invalidates every refresh token it issued —
   this is what actually forces the cutover. Any user still on the old client (whether
   or not they've updated Bora) will see their next API call fail as an auth error and
   will need to run `bora auth unlink` / `bora auth link` again to obtain a token from
   the new client.

Deleting the old client is the deliberate, irreversible step: it's what makes rotation
effective against a leaked or abused credential (old tokens stop working
immediately), and it's also what makes rotation disruptive (every user still on the
old client is forced to relink), which is why it only happens after the announced
window, not at the moment the new client is created.

## Pre-publish confirmation checklist

Before shipping *any* `client_secret.json` update — a rotation or the original
publish — confirm in the Cloud Console:

- [ ] The OAuth client belongs to the `bora-500821` project (not a leftover from an
      earlier codename project, a personal sandbox, or a fork).
- [ ] The consent screen requests only the single scope
      `https://www.googleapis.com/auth/drive` — no broader or additional scopes have
      been added.
- [ ] `bora-500821` has no other OAuth clients or consent-screen configurations beyond
      the one client Bora actually ships (no forgotten test clients left enabled,
      no duplicate consent screens).

This checklist was first run against the initial publish on 2026-07-21 (consent
screen published to "In production", unverified — see the
[release checklist](release-checklist.md)) and should be re-run in full for every
subsequent rotation.
