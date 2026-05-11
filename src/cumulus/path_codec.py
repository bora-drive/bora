"""Pure-function codec for Drive name → filesystem-safe path segment.

Owns the policy that bridges Google Drive's permissive ``name`` field
(accepts ``/``, control chars, Windows-reserved punctuation, etc.) and
the local filesystem (which does not). All transformations are pure
str → str — no I/O, no DB, no logging.

See ``docs/superpowers/specs/2026-05-02-stage-1-path-cache-design.md`` §3.1
for the full encoding policy.
"""

from __future__ import annotations

import unicodedata
import urllib.parse

__all__ = [
    "encode_name",
    "decode_name",
    "fold_key",
    "compose_path",
    "fold_path",
]


def encode_name(name: str) -> str:
    """Encode a single Drive name component for use as a path segment.

    Applies URL-encoding (uppercase hex) over the RFC 3986 unreserved set
    plus a trailing space/period rule for Windows compatibility.

    Idempotent only for already-canonical inputs; otherwise re-encoding
    double-encodes. This is correct: a Drive name literally ``'%2F'`` must
    round-trip back to ``'%2F'``, so it must encode to ``'%252F'``.
    """
    # safe=" " lets internal spaces through. Trailing-space rule below
    # converts a literal trailing space to %20 (a Windows constraint).
    encoded = urllib.parse.quote(name, safe=" ")
    if encoded.endswith(" "):
        encoded = encoded[:-1] + "%20"
    elif encoded.endswith("."):
        encoded = encoded[:-1] + "%2E"
    return encoded


def decode_name(encoded: str) -> str:
    """Inverse of :func:`encode_name`."""
    return urllib.parse.unquote(encoded)
