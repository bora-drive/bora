"""Linked resilience tests: rate limiting, token refresh, disconnect/reconnect.

These behaviours were previously covered only by offline fault injection
(tests/offline/test_fault_ratelimit.py and friends, which run against
FakeDrive). The tests here are the real-account ground-truth counterparts:
every recovery path ends in a REAL request against the live Drive API, so a
pass proves the client actually converges against Google, not just against
our model of Google.

Design notes
------------
* Rate limiting. Deliberately provoking Drive's server-side limiter needs an
  abusive request volume (the per-user quota is thousands of requests per
  60 s), so the deterministic tests instead inject a single real-shaped
  429 / 403(userRateLimitExceeded) HttpError into the first attempt of an
  operation and let the client's real ``_retry_transient`` loop re-issue the
  real request against the live API. A separate write-burst test (marked
  ``slow``, deselected by default per pyproject addopts) issues rapid
  sequential writes — the closest safe real-world equivalent to a genuine
  burst — and asserts convergence whether or not Drive throttles it.

* Token refresh. The access token is force-expired in place
  (``credentials.expiry`` set into the past); google_auth_httplib2 then
  refreshes through ``PersistingCredentials`` before the next request, which
  must succeed and persist the refreshed token to storage.

* Disconnect/reconnect. True network manipulation is not portable, so the
  transport fault is injected at the client's ``_retry_transient`` seam: the
  wrapped callable raises ``requests.exceptions.ConnectionError`` (a member
  of ``CONNECTION_ERRORS``, exactly what a mid-operation link drop surfaces
  from the HTTP layer) for the first N attempts, then delegates to the real
  network call. Recovery is therefore proven by a real round-trip.

Sleep neutralisation (same trick as tests/offline/test_fault_ratelimit.py):
``_retry_transient`` backs off via ``self._poll_event.wait(sleep_for)``;
returning False means "not cancelled, proceed", so replacing ``_poll_event``
with a Mock makes retries instant while still counting backoff sleeps.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
import requests

from bora.client import _MAX_RETRY_ATTEMPTS, GoogleDriveClient
from bora.core import FileMetadata, FolderMetadata
from bora.exceptions import ConnectionError as BoraConnectionError
from bora.oauth import PersistingCredentials
from tests.fake_drive import make_http_error
from tests.linked.conftest import requires_account

resources = os.path.dirname(os.path.dirname(__file__)) + "/resources"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _neutralise_retry_sleep(monkeypatch, client: GoogleDriveClient) -> Mock:
    """Make _retry_transient backoff sleeps instant (offline-suite trick).

    wait() returning False means "not cancelled"; is_set() returning False
    means "not shut down". The Mock also records how many backoff sleeps
    were requested, so tests can assert backoff was applied.
    """
    poll_event = Mock()
    poll_event.is_set = Mock(return_value=False)
    poll_event.wait = Mock(return_value=False)
    monkeypatch.setattr(client, "_poll_event", poll_event)
    return poll_event


def _inject_faults(monkeypatch, client: GoogleDriveClient, make_exc, count: int):
    """Raise *make_exc()* on the first *count* request attempts, then pass
    through to the real network call.

    Wraps the client's ``_retry_transient`` so the injected exception is
    raised INSIDE the real retry loop — the loop's own backoff/retry logic
    then re-issues the REAL request against the live API. Returns a state
    dict: ``remaining`` (faults left to fire; set to 0 to "reconnect") and
    ``fired`` (faults raised so far — teeth for the injection itself).
    """
    state = {"remaining": count, "fired": 0}
    real_retry = GoogleDriveClient._retry_transient

    def injecting_retry(call):
        def flaky_call():
            if state["remaining"] > 0:
                state["remaining"] -= 1
                state["fired"] += 1
                raise make_exc()
            return call()

        return real_retry(client, flaky_call)

    monkeypatch.setattr(client, "_retry_transient", injecting_retry)
    return state


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@requires_account
def test_429_retry_converges_against_real_api(
    client: GoogleDriveClient, monkeypatch
) -> None:
    """A real-shaped 429 on the first attempts is retried; the retry issues a
    real request and the read converges with correct data."""
    file = resources + "/file.txt"
    uploaded = client.upload(file, "/rl-read.txt")

    poll_event = _neutralise_retry_sleep(monkeypatch, client)
    state = _inject_faults(monkeypatch, client, lambda: make_http_error(429), count=2)

    result = client.list_folder("/")

    assert state["fired"] == 2, "both injected 429s must have fired"
    assert (
        poll_event.wait.call_count >= 2
    ), "each 429 must be absorbed by a backoff sleep, not a tight spin"
    ids = {getattr(e, "id", None) for e in result.entries}
    assert uploaded.id in ids, "listing must deliver real data after the 429s"


@requires_account
def test_403_user_rate_limit_retry_converges_against_real_api(
    client: GoogleDriveClient, monkeypatch
) -> None:
    """403/userRateLimitExceeded (Drive's per-user throttle response) on a
    write is retried; the retried real request commits the write."""
    poll_event = _neutralise_retry_sleep(monkeypatch, client)
    state = _inject_faults(
        monkeypatch,
        client,
        lambda: make_http_error(403, "userRateLimitExceeded"),
        count=2,
    )

    md = client.make_dir("/rl-write-dir")

    assert state["fired"] == 2, "both injected 403s must have fired"
    assert poll_event.wait.call_count >= 2
    assert isinstance(md, FolderMetadata)
    fetched = client.get_metadata("/rl-write-dir")
    assert isinstance(fetched, FolderMetadata)
    assert fetched.id == md.id


@requires_account
def test_rate_limit_retries_exhausted_then_recovers(
    client: GoogleDriveClient, monkeypatch
) -> None:
    """Teeth: a 429 storm longer than the retry budget surfaces as
    ConnectionError; once the storm ends the same client recovers."""
    _neutralise_retry_sleep(monkeypatch, client)
    state = _inject_faults(
        monkeypatch,
        client,
        lambda: make_http_error(429),
        count=_MAX_RETRY_ATTEMPTS,
    )

    with pytest.raises(BoraConnectionError):
        client.list_folder("/")
    assert state["fired"] == _MAX_RETRY_ATTEMPTS

    # Storm over (faults consumed) — the very next call must succeed.
    result = client.list_folder("/")
    assert result is not None


@requires_account
@pytest.mark.slow
def test_write_burst_absorbs_real_rate_limiting(client: GoogleDriveClient) -> None:
    """Issue a rapid sequential write burst and assert full convergence.

    Drive throttles per-user write rates well below the read quota, so a
    tight burst of creates is the closest safe real-world equivalent of a
    rate-limit storm: if Google returns real 403/429 throttle responses
    mid-burst, _retry_transient's backoff must absorb them (real sleeps —
    deliberately NOT neutralised here) and every write must still commit.
    A genuinely quota-abusive burst (thousands of requests) is avoided on
    purpose. Marked slow: deselected by default, run with ``-m slow``.
    """
    n = 30
    folders = [f"/burst-{i}" for i in range(n)]
    requests_before = client.request_count

    for path in folders:
        md = client.make_dir(path)
        assert isinstance(md, FolderMetadata)

    result = client.list_folder("/")
    paths = {e.path_display for e in result.entries}
    for path in folders:
        assert path in paths, f"{path} must exist after the burst"
    assert client.request_count - requests_before >= n


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


@requires_account
def test_token_refresh_after_forced_expiry(
    client: GoogleDriveClient, monkeypatch
) -> None:
    """Force-expire the access token; the next API call must auto-refresh
    (through PersistingCredentials) and succeed.

    google.auth compares ``expiry`` (naive UTC) against utcnow, and
    google_auth_httplib2 refreshes invalid credentials before each request,
    so setting expiry into the past makes the next real call refresh first.
    PersistingCredentials.refresh must also persist the refreshed token back
    to credential storage.
    """
    creds = client._credentials
    assert isinstance(creds, PersistingCredentials)

    refresh_calls: list[bool] = []
    real_refresh = PersistingCredentials.refresh

    def spying_refresh(self, request):
        refresh_calls.append(True)
        return real_refresh(self, request)

    monkeypatch.setattr(PersistingCredentials, "refresh", spying_refresh)

    # Force-expire in place (direct assignment: refresh overwrites it).
    creds.expiry = datetime.utcnow() - timedelta(hours=1)
    assert creds.expired, "credentials must report expired before the call"

    result = client.list_folder("/")  # triggers the auto-refresh

    assert result is not None
    assert refresh_calls, "the expired token must have been refreshed"
    assert not creds.expired, "credentials must be live again after refresh"
    assert creds.expiry is not None
    assert creds.expiry > datetime.utcnow()
    assert creds.token

    # PersistingCredentials.refresh persisted the refreshed token.
    blob = client._cred_storage.token
    assert blob, "refreshed credentials must be persisted to storage"
    assert json.loads(blob)["token"] == creds.token


# ---------------------------------------------------------------------------
# Network disconnect / reconnect
# ---------------------------------------------------------------------------


@requires_account
def test_network_drop_mid_upload_recovers(
    client: GoogleDriveClient, monkeypatch, tmp_path
) -> None:
    """A one-shot connection drop during an upload is retried transparently;
    the retried real request commits the file with the right content.

    requests.exceptions.ConnectionError is in CONNECTION_ERRORS — the exact
    shape the HTTP layer surfaces when the link goes down mid-request.
    """
    poll_event = _neutralise_retry_sleep(monkeypatch, client)
    state = _inject_faults(
        monkeypatch,
        client,
        lambda: requests.exceptions.ConnectionError("simulated network drop"),
        count=1,
    )

    file = resources + "/file.txt"
    md = client.upload(file, "/net-drop.txt")

    assert state["fired"] == 1, "the injected connection drop must have fired"
    assert poll_event.wait.call_count >= 1, "the drop must trigger a backoff sleep"
    assert isinstance(md, FileMetadata)
    assert md.size == os.path.getsize(file)


@requires_account
def test_network_outage_surfaces_then_reconnect_succeeds(
    client: GoogleDriveClient, monkeypatch
) -> None:
    """A full outage (every attempt fails) surfaces as ConnectionError after
    the retry budget; on "reconnect" the very next call succeeds.

    The same client object is reused across the outage — no relink, no new
    service — mirroring a laptop losing and regaining connectivity.
    """
    file = resources + "/file.txt"
    uploaded = client.upload(file, "/net-outage.txt")

    _neutralise_retry_sleep(monkeypatch, client)
    state = _inject_faults(
        monkeypatch,
        client,
        lambda: requests.exceptions.ConnectionError("simulated network outage"),
        count=_MAX_RETRY_ATTEMPTS,
    )

    with pytest.raises(BoraConnectionError):
        client.list_folder("/")
    assert (
        state["fired"] == _MAX_RETRY_ATTEMPTS
    ), "the outage must have exhausted the full retry budget"

    # Network restored: injection exhausted, calls pass through for real.
    result = client.list_folder("/")
    ids = {getattr(e, "id", None) for e in result.entries}
    assert uploaded.id in ids, "the client must recover on the next call"
