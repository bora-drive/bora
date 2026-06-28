# Google Cloud & OAuth Setup Guide

This guide walks through setting up a Google Cloud project with OAuth credentials so
you can run Bora (either from source or as a contributor). If you are installing Bora
from PyPI for personal use, the bundled credentials in `src/bora/resources/client_secret.json`
are used automatically and you can skip directly to the **Linking your account** section below.

---

## 1. Create a Google Cloud project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Click **Select a project** (top-left drop-down) then **New Project**.
3. Give it a name (e.g., `bora-sync`) and click **Create**.
4. Make sure the new project is selected in the drop-down before proceeding.

---

## 2. Enable the Google Drive API

1. In the left sidebar, go to **APIs & Services > Library**.
2. Search for "Google Drive API" and click the result.
3. Click **Enable**.

---

## 3. Configure the OAuth consent screen

1. Go to **APIs & Services > OAuth consent screen**.
2. Choose **External** (unless you are in a Google Workspace org and want to restrict
   access to that org).
3. Fill in the required fields:
   - **App name**: anything recognisable, e.g., `Bora`.
   - **User support email** and **Developer contact**: your email.
4. Click **Save and Continue** through the Scopes and Test Users steps (you will add a
   test user in the next section).
5. Leave the app in **Testing** mode for now. Apps in Testing mode can only be
   authorized by accounts you explicitly add as test users; they do not go through
   Google's full verification review.

### Add yourself as a test user

While your app is unverified and in Testing mode, only accounts listed as test users
can complete the OAuth flow:

1. On the **OAuth consent screen** page, scroll to **Test users**.
2. Click **Add Users** and enter the Gmail/Google account you want to sync with Bora.
3. Click **Save**.

> **Note:** Production use (shipping to arbitrary end-users) requires completing
> Google's app-verification review. Until that is done, keep the app in Testing mode
> and maintain the test-user list. This is tracked in the release checklist as item B2.

---

## 4. Create an OAuth client (Desktop app)

1. Go to **APIs & Services > Credentials**.
2. Click **Create Credentials > OAuth client ID**.
3. Set **Application type** to **Desktop app**.
4. Give it a name (e.g., `Bora Desktop`) and click **Create**.
5. Download the JSON file by clicking the download icon next to the new client ID.

### Where to put `client_secret.json`

Bora looks for credentials in the following order:

1. The path set in the `BORA_CLIENT_SECRET_PATH` environment variable (if set).
2. `src/bora/resources/client_secret.json` (bundled default, used when running from source
   or when the package was installed from PyPI with the bundled file).

To use your own credentials:

```console
# Option A — environment variable (recommended for development)
export BORA_CLIENT_SECRET_PATH=/path/to/your/client_secret.json
bora auth link

# Option B — replace the bundled file (for forks / custom builds)
cp /path/to/your/client_secret.json src/bora/resources/client_secret.json
```

---

## Linking your account

Once credentials are in place, link Bora to your Google Drive account:

```console
$ bora auth link
```

Bora will print a URL. Open it in a browser, sign in with a test-user account, grant
the requested permissions, and paste the authorization code back into the terminal.

On success, Bora stores a refresh token in your system keyring (macOS Keychain, GNOME
Keyring, or equivalent) and is ready to sync.

### Headless / SSH environments

If you are running Bora on a remote server over SSH and cannot open a browser on the
server, use a local port-forward to complete the loopback OAuth redirect:

1. On your **local machine**, open an SSH tunnel that forwards a local port to the
   server:

   ```console
   ssh -L 8080:localhost:8080 user@remote-host
   ```

2. On the **server**, start the auth flow:

   ```console
   bora auth link
   ```

3. Open the printed URL in your **local browser**. After you approve access, Google
   redirects to `http://localhost:8080/...`. Because of the tunnel, this request arrives
   at the server's port 8080, completing the flow automatically.

4. If automatic redirect capture does not work, copy the `code=` parameter from the
   browser's address bar and paste it when Bora prompts for it.

---

## Unlinking and re-linking

```console
$ bora auth unlink   # revoke credentials and remove from keyring
$ bora auth link     # link a new or different account
```

Check the current linked account at any time:

```console
$ bora auth status
```
