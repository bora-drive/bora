"""Fault-injection tests for rate-limit and quota paths (Stage 7b-E2, Task 7).

Covers:
  A — 429 storm on feed and on writes:
      backoff converges, no deadlock, no dropped work;
      userRateLimitExceeded (403 retryable reason) converges;
      bounded-retry proof (teeth).
  B — dailyLimitExceeded semantics + storageQuotaExceeded + session counter:
      dailyLimitExceeded is NOT in RETRYABLE_403_REASONS → surfaces
      immediately as ConnectionError (not retried);
      storageQuotaExceeded → InsufficientSpaceError (fatal, not retried);
      _request_count is a pure diagnostic counter (no client-side quota budget).

Sleep neutralisation
--------------------
``_retry_transient`` backs off by calling ``self._poll_event.wait(sleep_for)``.
Returning False from wait means "not cancelled, proceed".  We monkeypatch
``client._poll_event.wait`` to return False so retries are instant.
``_poll_event.is_set`` → False so the upload loop does not abort immediately.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import Mock

import pytest

from cumulus.client import GoogleDriveClient, _MAX_RETRY_ATTEMPTS
from cumulus.config import remove_configuration
from cumulus.errorhandling import RETRYABLE_403_REASONS
from cumulus.database.core import Database
from cumulus.exceptions import ConnectionError as CumulusConnectionError
from cumulus.exceptions import InsufficientSpaceError
from cumulus.keyring import CredentialStorage
from cumulus.path_cache import PathIDCache
from tests.fake_drive import (
    ROOT_ID,
    FakeDrive,
    make_http_error,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_CONFIG = "test-config-fault-ratelimit"


def _make_cache() -> PathIDCache:
    c = PathIDCache(Database(sqlite3.connect(":memory:")))
    c.root_id = ROOT_ID
    return c


@pytest.fixture
def cof():
    """Client-on-fake with neutralised retry sleep and ensure_root_id.

    Sleep neutralisation: _poll_event.wait returns False (not cancelled),
    _poll_event.is_set returns False (not shut down) — retries are instant.
    """
    fake = FakeDrive()
    cache = _make_cache()
    client = GoogleDriveClient(_CONFIG, CredentialStorage(_CONFIG), cache)
    client._get_service = lambda: fake.service()
    # Neutralise ensure-root-id network call; cache already seeded.
    client._ensure_root_id = Mock()
    # Neutralise retry sleep: wait() returning False means "not cancelled".
    client._poll_event = Mock()
    client._poll_event.is_set = Mock(return_value=False)
    client._poll_event.wait = Mock(return_value=False)
    yield client, fake
    remove_configuration(_CONFIG)


# ===========================================================================
# Scenario A — 429 storm → backoff converges, no deadlock, no dropped work
# ===========================================================================


def test_429_storm_on_feed_converges(cof):
    """Arm 4 consecutive 429s on changes.list; drain converges and delivers all
    seeded changes within _MAX_RETRY_ATTEMPTS.

    This exercises the retryable path in _retry_transient (429 is in
    RETRYABLE_HTTP_STATUS).  The sleep is neutralised so this is instant.
    """
    client, fake = cof

    # Take the start cursor before seeding files.
    start = fake.service().changes().getStartPageToken().execute()["startPageToken"]

    # Seed two files so there are changes to deliver.
    fid_a = fake.add_file("/alpha.txt", b"content-a")
    fid_b = fake.add_file("/beta.txt", b"content-b")

    # Arm 4 consecutive 429s (count < _MAX_RETRY_ATTEMPTS=5 so it converges).
    fake.fail_next("changes.list", make_http_error(429), count=4)

    result = client.list_remote_changes(start)

    file_ids = {e.id for e in result.entries}
    assert fid_a in file_ids, "alpha.txt must be delivered after 429 storm"
    assert fid_b in file_ids, "beta.txt must be delivered after 429 storm"

    # Sleep was called (the neutralised wait) for each retry, confirming
    # backoff was applied rather than a tight spin.
    assert client._poll_event.wait.call_count >= 4, (
        "wait() must be called once per backoff sleep (429 storm had 4 faults)"
    )


def test_429_storm_bounded_raises_teeth(cof):
    """Teeth: arm count=_MAX_RETRY_ATTEMPTS 429s on changes.list → retries are
    exhausted and CumulusConnectionError is raised.

    This proves the above convergence test worked via actual retries (not
    because the fault silently never fired).  It also proves the retry loop
    is bounded and cannot deadlock.
    """
    client, fake = cof

    start = fake.service().changes().getStartPageToken().execute()["startPageToken"]
    fake.add_file("/gamma.txt", b"content-c")

    # Exhaust all retry slots: _MAX_RETRY_ATTEMPTS faults → no successful call.
    fake.fail_next("changes.list", make_http_error(429), count=_MAX_RETRY_ATTEMPTS)

    with pytest.raises(CumulusConnectionError):
        client.list_remote_changes(start)


def test_429_storm_on_move_converges(cof):
    """Arm 4 consecutive 429s on files.update → move converges after retries.

    _retry_transient retries 429 from RETRYABLE_HTTP_STATUS.
    """
    client, fake = cof

    fid = fake.add_file("/src.txt", b"data")
    client._path_cache.set(
        "/src.txt", fid, parent_id=ROOT_ID, name="src.txt", is_folder=False
    )

    # 4 transient 429s before the successful call.
    fake.fail_next("files.update", make_http_error(429), count=4)

    result = client._writes.move("/src.txt", "/dst.txt")

    assert fake.files[fid]["name"] == "dst.txt", "file must be renamed after 429 storm"
    assert client._path_cache.path_to_id("/dst.txt") == fid
    assert result.id == fid


def test_429_storm_on_move_bounded_raises_teeth(cof):
    """Teeth: arm _MAX_RETRY_ATTEMPTS 429s on files.update → raises, file unchanged."""
    client, fake = cof

    fid = fake.add_file("/orig.txt", b"data")
    client._path_cache.set(
        "/orig.txt", fid, parent_id=ROOT_ID, name="orig.txt", is_folder=False
    )

    fake.fail_next("files.update", make_http_error(429), count=_MAX_RETRY_ATTEMPTS)

    with pytest.raises(CumulusConnectionError):
        client._writes.move("/orig.txt", "/moved.txt")

    # Rename must not have been applied.
    assert fake.files[fid]["name"] == "orig.txt", "file name must be unchanged after exhausted retries"


def test_403_user_rate_limit_exceeded_retries_and_converges(cof):
    """userRateLimitExceeded (403) is in RETRYABLE_403_REASONS → retried.

    Arm 3 × 403/userRateLimitExceeded on files.update; the move converges.
    """
    assert "userRateLimitExceeded" in RETRYABLE_403_REASONS
    client, fake = cof

    fid = fake.add_file("/rate-limited.txt", b"data")
    client._path_cache.set(
        "/rate-limited.txt", fid, parent_id=ROOT_ID, name="rate-limited.txt",
        is_folder=False,
    )

    # 3 retryable 403s — well within the budget.
    fake.fail_next(
        "files.update",
        make_http_error(403, "userRateLimitExceeded"),
        count=3,
    )

    result = client._writes.move("/rate-limited.txt", "/rate-limited-dst.txt")

    assert fake.files[fid]["name"] == "rate-limited-dst.txt"
    assert result.id == fid


def test_403_user_rate_limit_exceeded_bounded_raises_teeth(cof):
    """Teeth: _MAX_RETRY_ATTEMPTS × 403/userRateLimitExceeded → CumulusConnectionError raised."""
    client, fake = cof

    fid = fake.add_file("/rl-teeth.txt", b"data")
    client._path_cache.set(
        "/rl-teeth.txt", fid, parent_id=ROOT_ID, name="rl-teeth.txt", is_folder=False
    )

    fake.fail_next(
        "files.update",
        make_http_error(403, "userRateLimitExceeded"),
        count=_MAX_RETRY_ATTEMPTS,
    )

    with pytest.raises(CumulusConnectionError):
        client._writes.move("/rl-teeth.txt", "/rl-teeth-dst.txt")

    assert fake.files[fid]["name"] == "rl-teeth.txt", (
        "file name must be unchanged after exhausted userRateLimitExceeded retries"
    )


# ===========================================================================
# Scenario B — dailyLimitExceeded / storageQuotaExceeded / session counter
# ===========================================================================


def test_daily_limit_exceeded_surfaces_immediately_not_retried(cof):
    """dailyLimitExceeded (403) is NOT in RETRYABLE_403_REASONS.

    _is_retryable_http_error returns False for this reason.
    _retry_transient therefore does NOT retry it; the very first failure
    propagates immediately as CumulusConnectionError (mapped from 403
    dailyLimitExceeded → ConnectionError in http_error_to_cumulus).

    Assert:
    - CumulusConnectionError is raised.
    - _poll_event.wait is NOT called (no retry backoff sleep), confirming
      there was exactly zero retry attempts.
    """
    assert "dailyLimitExceeded" not in RETRYABLE_403_REASONS
    client, fake = cof

    fid = fake.add_file("/daily.txt", b"data")
    client._path_cache.set(
        "/daily.txt", fid, parent_id=ROOT_ID, name="daily.txt", is_folder=False
    )

    # Arm ONE dailyLimitExceeded fault.
    fake.fail_next("files.update", make_http_error(403, "dailyLimitExceeded"), count=1)

    # Record how many times wait was called before the call.
    wait_before = client._poll_event.wait.call_count

    with pytest.raises(CumulusConnectionError):
        client._writes.move("/daily.txt", "/daily-dst.txt")

    wait_after = client._poll_event.wait.call_count

    assert wait_after == wait_before, (
        "dailyLimitExceeded must NOT trigger any retry backoff sleep; "
        f"wait() called {wait_after - wait_before} time(s) after the fault"
    )


def test_daily_limit_exceeded_teeth(cof):
    """Teeth: if the dailyLimitExceeded path WERE retried, arming count=1
    would let one retry succeed.  We prove no retry happened by verifying
    the file name is unchanged and the rule was consumed (confirming the
    fault fired exactly once and was not bypassed).
    """
    assert "dailyLimitExceeded" not in RETRYABLE_403_REASONS
    client, fake = cof

    fid = fake.add_file("/daily-teeth.txt", b"data")
    client._path_cache.set(
        "/daily-teeth.txt", fid, parent_id=ROOT_ID, name="daily-teeth.txt",
        is_folder=False,
    )

    # Arm count=1: if the code retried, the second attempt would succeed.
    fake.fail_next("files.update", make_http_error(403, "dailyLimitExceeded"), count=1)

    with pytest.raises(CumulusConnectionError):
        client._writes.move("/daily-teeth.txt", "/daily-teeth-dst.txt")

    # Fault rule was consumed (fired exactly once → removed from _fail_rules).
    assert not fake._fail_rules, (
        "dailyLimitExceeded fault rule must have been consumed (fired once, not retried past)"
    )
    # File unchanged — the move was never committed.
    assert fake.files[fid]["name"] == "daily-teeth.txt", (
        "file name must be unchanged: dailyLimitExceeded must not have been retried through"
    )


def test_storage_quota_exceeded_raises_insufficient_space(cof):
    """storageQuotaExceeded (403) maps to InsufficientSpaceError (fatal, not retried).

    InsufficientSpaceError is NOT a subclass of ConnectionError, so
    _retry_transient cannot retry it (only retries HttpError with
    _is_retryable_http_error returning True, or CONNECTION_ERRORS).
    Assert: InsufficientSpaceError is raised immediately, no retry sleeps.
    """
    assert "storageQuotaExceeded" not in RETRYABLE_403_REASONS
    client, fake = cof

    fid = fake.add_file("/quota.txt", b"data")
    client._path_cache.set(
        "/quota.txt", fid, parent_id=ROOT_ID, name="quota.txt", is_folder=False
    )

    fake.fail_next("files.update", make_http_error(403, "storageQuotaExceeded"), count=1)

    wait_before = client._poll_event.wait.call_count

    with pytest.raises(InsufficientSpaceError):
        client._writes.move("/quota.txt", "/quota-dst.txt")

    wait_after = client._poll_event.wait.call_count
    assert wait_after == wait_before, (
        "storageQuotaExceeded must NOT trigger any retry backoff sleep"
    )


def test_storage_quota_exceeded_teeth(cof):
    """Teeth: invert — arm a 403 rateLimitExceeded (retried) to confirm
    storageQuotaExceeded is specifically fatal and not merely a chance effect.

    With rateLimitExceeded, count=1 → retry succeeds → no exception.
    With storageQuotaExceeded, count=1 → exception on first failure.
    This contrast proves the storageQuotaExceeded mapping is load-bearing.
    """
    assert "rateLimitExceeded" in RETRYABLE_403_REASONS
    assert "storageQuotaExceeded" not in RETRYABLE_403_REASONS
    client, fake = cof

    fid_a = fake.add_file("/contrast-a.txt", b"data")
    client._path_cache.set(
        "/contrast-a.txt", fid_a, parent_id=ROOT_ID, name="contrast-a.txt",
        is_folder=False,
    )
    fid_b = fake.add_file("/contrast-b.txt", b"data")
    client._path_cache.set(
        "/contrast-b.txt", fid_b, parent_id=ROOT_ID, name="contrast-b.txt",
        is_folder=False,
    )

    # rateLimitExceeded: count=1 → one retry → succeeds.
    fake.fail_next("files.update", make_http_error(403, "rateLimitExceeded"), count=1)
    result = client._writes.move("/contrast-a.txt", "/contrast-a-dst.txt")
    assert result.id == fid_a  # converged: not fatal

    # storageQuotaExceeded: count=1 → raises immediately.
    fake.fail_next("files.update", make_http_error(403, "storageQuotaExceeded"), count=1)
    with pytest.raises(InsufficientSpaceError):
        client._writes.move("/contrast-b.txt", "/contrast-b-dst.txt")


def test_request_count_increments_per_request(cof):
    """_request_count increments with each Drive API request.

    The session counter is a pure diagnostic — there is no client-side per-day
    quota budget that pre-emptively blocks requests (the F14 fix removed the
    bogus per-day budget).  We confirm:
    - The counter starts at zero.
    - After N operations the counter is > 0.
    - There is no AttributeError for a 'budget' or 'quota_budget' attribute
      (i.e. no per-day budget attribute exists on the client).
    """
    client, fake = cof

    assert client._request_count == 0, "counter must start at zero"
    # These are the specific client-side budget attrs F14 removed; not an exhaustive guard.
    assert not hasattr(client, "quota_budget"), (
        "no client-side quota_budget attribute should exist (F14 fix)"
    )
    assert not hasattr(client, "_daily_budget"), (
        "no client-side _daily_budget attribute should exist (F14 fix)"
    )

    # Issue several operations.
    for i in range(5):
        fid = fake.add_file(f"/counter-{i}.txt", b"data")
        client._path_cache.set(
            f"/counter-{i}.txt", fid, parent_id=ROOT_ID,
            name=f"counter-{i}.txt", is_folder=False,
        )
        client._writes.move(f"/counter-{i}.txt", f"/counter-{i}-done.txt")

    after = client.request_count
    assert after > 0, f"counter must be > 0 after multiple ops; got {after}"


def test_no_preemptive_client_side_quota_rejection(cof):
    """No client-side per-day quota budget pre-emptively blocks requests.

    The F14 fix removed the bogus per-day budget (the 12k/60s limit is
    per-60-seconds, not per-day).  Issue many operations and assert none
    are pre-emptively rejected by a client-side counter — only real API
    errors (injected via fake.fail_next) should cause failures.

    This is a characterization test: we prove correctness by absence of
    a pre-emptive error when no fault is armed.
    """
    client, fake = cof

    # Issue 20 operations — well above any plausible per-batch budget guard.
    for i in range(20):
        fid = fake.add_file(f"/nobulk-{i}.txt", b"data")
        client._path_cache.set(
            f"/nobulk-{i}.txt", fid, parent_id=ROOT_ID,
            name=f"nobulk-{i}.txt", is_folder=False,
        )
        # No fault armed — must succeed every time.
        client._writes.move(f"/nobulk-{i}.txt", f"/nobulk-{i}-done.txt")

    # All 20 moves completed without pre-emptive rejection.
    for i in range(20):
        assert client._path_cache.path_to_id(f"/nobulk-{i}-done.txt") is not None, (
            f"op {i} must succeed with no client-side quota rejection"
        )
