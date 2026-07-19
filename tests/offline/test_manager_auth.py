"""B3 verification: a TokenRevokedError raised inside a background sync
thread must stop the manager and surface a user-visible message.
"""

from __future__ import annotations

from threading import Event
from unittest import mock

from bora.exceptions import TokenRevokedError


def test_handle_sync_thread_errors_stops_on_token_revoked():
    """Pins the existing manager behavior: TokenRevokedError stops the
    manager and triggers the notifier. If this test starts failing because
    of a manager refactor, add an explicit branch — don't loosen the test.
    """
    from bora.manager import SyncManager

    # Minimal SyncManager stub: real construction requires SyncEngine.
    mgr = SyncManager.__new__(SyncManager)
    mgr._logger = mock.Mock()
    mgr.desktop_notifier = mock.Mock()
    mgr.stop = mock.Mock()
    mgr.autostart = Event()

    with mgr._handle_sync_thread_errors(running=Event(), autostart=Event()):
        raise TokenRevokedError(
            "Authentication revoked", "Run 'bora auth link -r' to re-link."
        )

    mgr.stop.assert_called_once()
    mgr.desktop_notifier.notify.assert_called_once()
    # The user-facing title must mention auth.
    call_args = mgr.desktop_notifier.notify.call_args
    title = call_args.args[0] if call_args.args else call_args.kwargs.get("title", "")
    assert "auth" in title.lower() or "revok" in title.lower()
