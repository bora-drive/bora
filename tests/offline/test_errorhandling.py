"""Unit tests for bora.errorhandling — HTTP and OSError mapping.

The auth-library exception mapping (RefreshError / GoogleAuthError) is
covered separately in test_errorhandling_auth.py.
"""

from __future__ import annotations

import errno

import pytest

from bora.errorhandling import (
    convert_api_errors,
    extract_http_reason,
    http_error_to_bora,
    os_to_bora_error,
)
from bora.exceptions import (
    BadInputError,
    BoraApiError,
    ConnectionError,
    InsufficientPermissionsError,
    InsufficientSpaceError,
    NotFoundError,
    RestrictedContentError,
    ServerError,
    TokenRevokedError,
)

# ---- extract_http_reason --------------------------------------------------


def test_extract_reason_reads_drive_sub_reason(make_http_error):
    exc = make_http_error(403, reason="rateLimitExceeded")
    assert extract_http_reason(exc) == "rateLimitExceeded"


def test_extract_reason_empty_when_absent(make_http_error):
    exc = make_http_error(404)
    assert extract_http_reason(exc) == ""


# ---- http_error_to_bora mapping table ----------------------------------


@pytest.mark.parametrize(
    "status, reason, expected_cls",
    [
        (400, "", BadInputError),
        (401, "", TokenRevokedError),
        (403, "userRateLimitExceeded", ConnectionError),
        (403, "rateLimitExceeded", ConnectionError),
        (403, "sharingRateLimitExceeded", ConnectionError),
        (403, "storageQuotaExceeded", InsufficientSpaceError),
        (403, "dailyLimitExceeded", ConnectionError),
        (403, "cannotDownloadAbusiveFile", RestrictedContentError),
        (403, "insufficientFilePermissions", InsufficientPermissionsError),
        (403, "someUnknownReason", InsufficientPermissionsError),
        (404, "", NotFoundError),
        (429, "", ConnectionError),
        (500, "", ServerError),
        (502, "", ServerError),
        (503, "", ServerError),
        (504, "", ServerError),
        (418, "", BoraApiError),
    ],
)
def test_http_error_mapping(make_http_error, status, reason, expected_cls):
    exc = make_http_error(status, reason=reason)
    result = http_error_to_bora(status, reason, exc)
    assert type(result) is expected_cls


def test_convert_api_errors_wraps_http_error(make_http_error):
    exc = make_http_error(404)
    with pytest.raises(NotFoundError):
        with convert_api_errors(remote_path="/x"):
            raise exc


def test_convert_api_errors_preserves_cause(make_http_error):
    exc = make_http_error(404)
    try:
        with convert_api_errors():
            raise exc
    except NotFoundError as converted:
        assert converted.__cause__ is exc
    else:
        pytest.fail("expected NotFoundError")


# ---- os_to_bora_error (unchanged behavior, pinned here) ----------------


def test_os_error_permission_maps_to_insufficient_permissions():
    exc = PermissionError("denied")
    result = os_to_bora_error(exc)
    assert isinstance(result, InsufficientPermissionsError)


def test_os_error_not_found_maps_to_not_found():
    exc = FileNotFoundError("missing")
    result = os_to_bora_error(exc)
    assert isinstance(result, NotFoundError)


def test_os_error_enospc_maps_to_insufficient_space():
    exc = OSError(errno.ENOSPC, "no space")
    result = os_to_bora_error(exc)
    assert isinstance(result, InsufficientSpaceError)


def test_os_error_enametoolong_reports_limits_in_correct_order():
    from bora.exceptions import PathError
    from bora.utils.path import fs_max_lengths_for_path

    # fs_max_lengths_for_path returns (max_path, max_name) in that order.
    max_path, max_name = fs_max_lengths_for_path("/")
    assert max_path != max_name, "limits must differ for this test to be meaningful"

    exc = OSError(errno.ENAMETOOLONG, "name too long")
    result = os_to_bora_error(exc, local_path="/")
    assert isinstance(result, PathError)

    # The name limit must appear in the file-name clause and the path limit in
    # the path clause, i.e. "shorter than {max_name} and {max_path} characters".
    assert (
        f"shorter than {max_name} and {max_path} characters" in result.message
    ), result.message


def test_http_error_412_condition_not_met_maps_to_data_changed(make_http_error):
    from bora.errorhandling import http_error_to_bora
    from bora.exceptions import DataChangedError

    exc = make_http_error(412, reason="conditionNotMet")
    out = http_error_to_bora(412, "conditionNotMet", exc, remote_path="/foo.txt")
    assert isinstance(out, DataChangedError)
    assert "/foo.txt" in str(out) or out.remote_path == "/foo.txt"


def test_http_error_412_other_reason_falls_through(make_http_error):
    from bora.errorhandling import http_error_to_bora
    from bora.exceptions import DataChangedError

    # Any 412 should map to DataChangedError regardless of sub-reason.
    exc = make_http_error(412, reason="")
    out = http_error_to_bora(412, "", exc)
    assert isinstance(out, DataChangedError)
