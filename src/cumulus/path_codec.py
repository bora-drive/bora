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
    "encode_path",
    "decode_path",
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


def _encode_segment_safe(segment: str) -> str:
    """:func:`encode_name` a segment, but leave it untouched if it contains
    bytes that cannot be utf-8 encoded (surrogate escapes from a path with an
    unknown file-system encoding). Such paths are caught and rejected
    downstream by ``sync.check_encoding`` with a ``PathError``; encoding them
    here would instead raise an unhandled ``UnicodeEncodeError`` during
    ``SyncEvent`` construction."""
    if not segment:
        return segment
    try:
        return encode_name(segment)
    except UnicodeEncodeError:
        return segment


def encode_path(path: str) -> str:
    """Encode every name component of a virtual path.

    Maps a decoded (human-readable) path — as it appears on the local file
    system — to the encoded virtual path used by the index and path cache.
    Inverse of :func:`decode_path`. The leading empty segment from a leading
    ``/`` is preserved; ``"/"`` and ``""`` pass through unchanged.
    """
    if path in ("", "/"):
        return path
    return "/".join(_encode_segment_safe(p) for p in path.split("/"))


def decode_path(path: str) -> str:
    """Decode every name component of an encoded virtual path.

    Maps an encoded virtual path (index / path-cache / ``path_display`` form)
    back to the decoded, human-readable form used for the local file system.
    Inverse of :func:`encode_path`.
    """
    if path in ("", "/"):
        return path
    return "/".join(decode_name(p) if p else p for p in path.split("/"))


def fold_key(name: str) -> str:
    """Case-fold key for one name component.

    Computes ``quote(NFC(casefold(NFC(name))), safe='')``. The double NFC
    handles cases where casefold introduces decomposition (e.g. Turkish
    'İ' casefolds to 'i' + combining dot above, which the trailing NFC
    keeps in canonical form).
    """
    normalized = unicodedata.normalize("NFC", name)
    folded = normalized.casefold()
    folded = unicodedata.normalize("NFC", folded)
    # safe=" " mirrors encode_name; both paths and fold-keys preserve
    # internal spaces so that fold_path round-trips consistently.
    return urllib.parse.quote(folded, safe=" ")


def compose_path(parent_path: str, raw_name: str) -> str:
    """Build an encoded child path from an encoded parent path and a raw
    Drive name.

    Example::

        compose_path('/Documents', '2024/05 report.pdf')
        -> '/Documents/2024%2F05 report.pdf'

    ``parent_path == '/'`` is the root; result is ``'/' + encode_name(raw_name)``.
    A trailing slash on ``parent_path`` is ignored.
    """
    if parent_path.endswith("/") and parent_path != "/":
        parent_path = parent_path[:-1]
    encoded_leaf = encode_name(raw_name)
    if parent_path == "/":
        return "/" + encoded_leaf
    return parent_path + "/" + encoded_leaf


def fold_path(encoded_path: str) -> str:
    """Fold key for a full encoded path.

    Splits on ``/``, decodes each segment, applies :func:`fold_key`,
    rejoins with ``/``. Used by the cache to derive ``remote_path_fold``
    from ``remote_path``.
    """
    if encoded_path == "/":
        return "/"
    # Leading "/" produces an empty first segment; preserve it.
    parts = encoded_path.split("/")

    def _fold_segment(p: str) -> str:
        if not p:
            return p
        try:
            return fold_key(decode_name(p))
        except UnicodeEncodeError:
            # Non-utf8 / surrogate-escaped name — leave it untouched so the
            # caller (e.g. check_encoding) can reject it with a PathError
            # rather than raising an unhandled UnicodeEncodeError here.
            return p

    return "/".join(_fold_segment(p) for p in parts)
