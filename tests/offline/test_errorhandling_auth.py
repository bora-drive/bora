"""Tests for Stage 2 auth-library exception mapping in convert_api_errors.

The pre-existing tests/offline/test_errorhandling.py covers the legacy
Dropbox surface and currently has a collection error (the dropbox package
isn't installed); those tests are Stage 3/5/6 work. This file isolates
the new auth-library mapping tests so they collect cleanly without
touching the legacy file.
"""

from __future__ import annotations

import pytest
from google.auth.exceptions import GoogleAuthError, RefreshError

from bora.errorhandling import convert_api_errors
from bora.exceptions import AuthError, TokenRevokedError


def test_convert_api_errors_maps_refresh_error_to_token_revoked():
    with pytest.raises(TokenRevokedError) as excinfo:
        with convert_api_errors():
            raise RefreshError("invalid_grant")
    assert "revoked" in excinfo.value.title.lower()
    assert isinstance(excinfo.value, AuthError)


def test_convert_api_errors_maps_other_googleautherror_to_authError():
    class _Other(GoogleAuthError):
        pass

    with pytest.raises(AuthError) as excinfo:
        with convert_api_errors():
            raise _Other("something else broke")

    # Should NOT be a TokenRevokedError specifically — only the catch-all.
    assert not isinstance(excinfo.value, TokenRevokedError)
