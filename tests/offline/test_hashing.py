"""Tests for the MD5 ContentHasher and StreamHasher."""

from __future__ import annotations

import hashlib
import io

from bora.utils.hashing import ContentHasher, StreamHasher


def test_content_hasher_matches_hashlib_md5():
    data = b"the quick brown fox" * 1000
    h = ContentHasher()
    h.update(data)
    assert h.hexdigest() == hashlib.md5(data).hexdigest()


def test_content_hasher_empty():
    h = ContentHasher()
    assert h.hexdigest() == hashlib.md5(b"").hexdigest()
    assert ContentHasher().digest() == hashlib.md5(b"").digest()


def test_content_hasher_chunked_equals_oneshot():
    data = bytes(range(256)) * 5000
    h = ContentHasher()
    for i in range(0, len(data), 4096):
        h.update(data[i : i + 4096])
    assert h.hexdigest() == hashlib.md5(data).hexdigest()


def test_content_hasher_copy_is_independent():
    h = ContentHasher()
    h.update(b"abc")
    c = h.copy()
    c.update(b"def")
    assert h.hexdigest() == hashlib.md5(b"abc").hexdigest()
    assert c.hexdigest() == hashlib.md5(b"abcdef").hexdigest()


def test_stream_hasher_read_hashes_passthrough():
    data = b"streamed content \xff\x00\xfe" * 500
    src = io.BytesIO(data)
    h = ContentHasher()
    wrapped = StreamHasher(src, h)
    out = b""
    while True:
        chunk = wrapped.read(128)
        if not chunk:
            break
        out += chunk
    assert out == data
    assert h.hexdigest() == hashlib.md5(data).hexdigest()
