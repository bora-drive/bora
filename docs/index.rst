
Bora Developer Documentation
============================

This documentation provides an API reference for the bora daemon.
It is built from the current dev branch and is intended for developers.
For a user manual and an overview of Bora's functionality, please
refer to `bora.tools <https://bora.tools>`_.

.. toctree::
   :hidden:
   :caption: Background
   :maxdepth: 2

   setup
   differences
   background/sync_logic
   background/logging
   background/config_files
   background/state_files
   background/contributing

.. toctree::
   :hidden:
   :caption: Reference
   :maxdepth: 2

   autoapi/bora/autostart/index
   autoapi/bora/changes/index
   autoapi/bora/cli/index
   autoapi/bora/client/index
   autoapi/bora/config/index
   autoapi/bora/constants/index
   autoapi/bora/core/index
   autoapi/bora/daemon/index
   autoapi/bora/database/index
   autoapi/bora/errorhandling/index
   autoapi/bora/exceptions/index
   autoapi/bora/fsevents/index
   autoapi/bora/keyring/index
   autoapi/bora/logging/index
   autoapi/bora/main/index
   autoapi/bora/manager/index
   autoapi/bora/models/index
   autoapi/bora/native_docs/index
   autoapi/bora/notify/index
   autoapi/bora/oauth/index
   autoapi/bora/path_cache/index
   autoapi/bora/path_codec/index
   autoapi/bora/sync/index
   autoapi/bora/utils/index
   autoapi/bora/writes/index

Getting started
***************

To use the Bora API in a Python interpreter, import the main module first and
initialize a Bora instance with a configuration name. For this example, we use a new
configuration "private" which is not yet linked to a Google Drive account:

    >>> from bora.main import Bora
    >>> m = Bora(config_name="private")

Config files will be created on-demand for the new configuration, as described in
:doc:`background/config_files` and :doc:`background/state_files`.

We now link the instance to an existing Google Drive account. This is done by generating a
Google Drive URL for the user to visit and authorize Bora. Using the :meth:`link` method,
the resulting auth code is exchanged for an access token to make Google Drive API calls. See
Google's `oauth-guide <https://developers.google.com/identity/protocols/oauth2>`_
for details on the OAuth2 PKCE flow which we use.
When the auth flow is successfully completed, the credentials will be saved in the
system keyring (e.g., macOS Keychain or Gnome Keyring).

    >>> url = m.get_auth_url()  # get token from Google Drive website
    >>> print(f"Please go to {url} to retrieve a Google Drive authorization token.")
    >>> token = input("Enter auth token: ")
    >>> res = m.link(token)

The call to :meth:`link` will return 0 on success, 1 for an invalid code and 2 for
connection errors. We verify that linking succeeded and proceed to create a local
Google Drive folder and start syncing:

    >>> if res == 0:
    ...     m.create_dropbox_directory("~/Google Drive (Private)")
    ...     m.start_sync()
