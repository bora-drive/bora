"""Module for content hashing file contents."""

from __future__ import annotations

# system imports
import hashlib
from typing import BinaryIO, Protocol, Union

_WritableBuffer = Union[bytes, bytearray]


class _Hasher(Protocol):
    """Structural type for any hasher with an ``update`` method."""

    def update(self, data: bytes | bytearray) -> None: ...


class ContentHasher:
    """
    Computes the MD5 hash Google Drive exposes as the ``md5Checksum`` field.

    Exposes the same interface as the hashers in the standard ``hashlib``
    package (``update`` / ``digest`` / ``hexdigest`` / ``copy``), so it is a
    drop-in for any code that previously used a streaming hasher.

    :Example:

        >>> hasher = ContentHasher()
        >>> with open('some-file', 'rb') as f:
        ...     while True:
        ...         chunk = f.read(1024 * 1024)
        ...         if not chunk:
        ...             break
        ...         hasher.update(chunk)
        ... print(hasher.hexdigest())
    """

    def __init__(self) -> None:
        self._hasher = hashlib.md5(usedforsecurity=False)
        self.digest_size = self._hasher.digest_size

    def update(self, new_data: bytes | bytearray) -> None:
        self._hasher.update(new_data)

    def digest(self) -> bytes:
        return self._hasher.digest()

    def hexdigest(self) -> str:
        return self._hasher.hexdigest()

    def copy(self) -> "ContentHasher":
        c = ContentHasher.__new__(ContentHasher)
        c._hasher = self._hasher.copy()
        c.digest_size = self.digest_size
        return c


class StreamHasher:
    """
    A wrapper around a file-like object (either for reading or writing)
    that hashes everything that passes through it.  Can be used with
    ContentHasher or any 'hashlib' hasher.

    :Example:

        >>> hasher = ContentHasher()
        >>> with open('some-file', 'rb') as f:
        ...     wrapped_f = StreamHasher(f, hasher)
        ...     response = some_api_client.upload(wrapped_f)
        >>> locally_computed = hasher.hexdigest()
        >>> assert response.content_hash == locally_computed

    :param f: File-like object.
    :param hasher: Hasher to use. Must implement an ``update`` method.
    """

    def __init__(self, f: BinaryIO, hasher: _Hasher) -> None:
        self._f = f
        self._hasher = hasher

    def close(self) -> None:
        return self._f.close()

    def flush(self) -> None:
        return self._f.flush()

    def fileno(self) -> int:
        return self._f.fileno()

    def tell(self) -> int:
        return self._f.tell()

    def read(self, size: int = -1) -> bytes:
        b = self._f.read(size)
        self._hasher.update(b)
        return b

    def write(self, b: _WritableBuffer) -> int:
        self._hasher.update(b)
        return self._f.write(b)

    def readline(self, size: int = -1) -> bytes:
        b = self._f.readline(size)
        self._hasher.update(b)
        return b

    def readlines(self, hint: int = -1) -> list[bytes]:
        bs = self._f.readlines(hint)
        for b in bs:
            self._hasher.update(b)
        return bs
