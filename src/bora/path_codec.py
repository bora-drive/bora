"""Pure-function codec for Drive name → filesystem-safe path segment.

Owns the policy that bridges Google Drive's permissive ``name`` field
(accepts ``/``, control chars, Windows-reserved punctuation, etc.) and
the local filesystem (which does not). All transformations are pure
str → str — no I/O, no DB, no logging.

See ``docs/superpowers/specs/2026-05-02-stage-1-path-cache-design.md`` §3.1
for the full encoding policy.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Callable

__all__ = [
    "encode_name",
    "decode_name",
    "encode_path",
    "decode_path",
    "encode_path_local",
    "decode_path_local",
    "fold_key",
    "compose_path",
    "fold_path",
    "fold_human",
]

# Escapes of the characters that can never appear in a local file name on any
# platform: the path separator and NUL. ``decode_path_local`` keeps these
# escaped so a Drive name like ``11/26/25`` materializes as the single local
# segment ``11%2F26%2F25`` instead of spilling extra directory levels into the
# local path; ``encode_path_local`` collapses the resulting double-encoding on
# the way back up. Case-insensitive: canonical escapes are uppercase, but a
# folded path may carry ``%2f``.
_FS_UNSAFE_ESCAPE_RE = re.compile(r"(%2F|%00)", re.IGNORECASE)
_FS_UNSAFE_DOUBLE_RE = re.compile(r"%25(2F|00)", re.IGNORECASE)


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


def _decode_name_local(encoded: str) -> str:
    """:func:`decode_name`, except escapes of filesystem-unsafe characters
    (``/`` and NUL) are kept verbatim so the result is always a valid single
    path segment. Safe because in a canonically encoded segment every ``%``
    starts an escape triple, so ``%2F``/``%00`` can only match real escapes
    (a Drive name literally containing ``%2F`` is encoded as ``%252F``,
    which contains no ``%2F`` substring)."""
    parts = _FS_UNSAFE_ESCAPE_RE.split(encoded)
    # re.split with a capture group interleaves the kept escapes at odd
    # indices; decode only the chunks between them.
    return "".join(part if i % 2 else decode_name(part) for i, part in enumerate(parts))


def _encode_name_local(name: str) -> str:
    """Inverse of :func:`_decode_name_local`: :func:`encode_name`, then
    collapse the double-encoding of kept filesystem-unsafe escapes
    (``%252F`` -> ``%2F``, ``%2500`` -> ``%00``) so a locally materialized
    segment maps back to the exact virtual path it came from.

    Consequence: a local file literally named ``a%2Fb`` aliases to the same
    virtual path as the mirror of Drive name ``a/b`` — unavoidable, since
    POSIX cannot represent ``/`` in a name and both must share the local
    form ``a%2Fb``."""
    return _FS_UNSAFE_DOUBLE_RE.sub(lambda m: "%" + m.group(1), encode_name(name))


def _encode_segment_safe(
    segment: str, encoder: Callable[[str], str] | None = None
) -> str:
    """:func:`encode_name` a segment, but leave it untouched if it contains
    bytes that cannot be utf-8 encoded (surrogate escapes from a path with an
    unknown file-system encoding). Such paths are caught and rejected
    downstream by ``sync.check_encoding`` with a ``PathError``; encoding them
    here would instead raise an unhandled ``UnicodeEncodeError`` during
    ``SyncEvent`` construction."""
    if not segment:
        return segment
    try:
        return (encoder or encode_name)(segment)
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


def decode_path_local(path: str) -> str:
    """:func:`decode_path` for the virtual -> local-filesystem boundary.

    Escapes of characters that cannot appear in a local file name (``/`` and
    NUL) are kept verbatim, so every decoded segment is a valid local name
    and the path has exactly as many components as the virtual one.
    Inverse of :func:`encode_path_local`.
    """
    if path in ("", "/"):
        return path
    return "/".join(_decode_name_local(p) if p else p for p in path.split("/"))


def encode_path_local(path: str) -> str:
    """:func:`encode_path` for the local-filesystem -> virtual boundary.

    Collapses the double-encoding of segments materialized by
    :func:`decode_path_local` (``%252F`` -> ``%2F``, ``%2500`` -> ``%00``)
    so a local mirror path maps back to the exact virtual path it came
    from. Inverse of :func:`decode_path_local`.
    """
    if path in ("", "/"):
        return path
    return "/".join(
        _encode_segment_safe(p, _encode_name_local) for p in path.split("/")
    )


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


def fold_human(path: str) -> str:
    """Quoting-free canonical fold of a DECODED (human-readable) path.

    Per segment: ``NFC(casefold(NFC(segment)))``.

    **Identity:** ``decode_path(fold_path(encode_path(p))) == fold_human(p)``,
    so this is the canonical form to compare user-supplied paths (mignore
    patterns, selective-sync exclusions) against engine fold-form paths after
    :func:`decode_path`. Idempotent.

    ``"/"`` and ``""`` pass through unchanged.
    """
    if path in ("", "/"):
        return path

    def _fold(seg: str) -> str:
        if not seg:
            return seg
        n = unicodedata.normalize("NFC", seg)
        return unicodedata.normalize("NFC", n.casefold())

    return "/".join(_fold(p) for p in path.split("/"))
