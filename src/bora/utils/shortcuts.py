"""Platform-agnostic symlink / Windows .lnk shortcut writer.

POSIX gets a real symlink. Windows without Developer Mode / admin cannot
create symlinks; there we fall back to a minimal MS-SHLLINK (.lnk) file
carrying a relative-path target. Pure-Python — no extra dependency."""
from __future__ import annotations

import os
import struct

__all__ = ["write_platform_symlink", "write_lnk"]

# MS-SHLLINK ShellLinkHeader constants.
_HEADER_SIZE = 0x0000004C
_LINK_CLSID = b"\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46"
# LinkFlags: HasLinkTargetIDList off; HasRelativePath on (0x00000008)
#            + IsUnicode (0x00000080).
_LINK_FLAGS = 0x00000088
_FILE_ATTR_NORMAL = 0x00000080


def write_platform_symlink(target: str, link_path: str) -> None:
    """Create a symlink at *link_path* pointing to *target*.

    Falls back to a ``<link_path>.lnk`` shortcut file when the OS refuses
    a real symlink (typically Windows without privilege)."""
    try:
        os.symlink(target, link_path)
    except OSError:
        write_lnk(target, link_path + ".lnk")


def _unicode_str(value: str) -> bytes:
    """MS-SHLLINK StringData: CountCharacters (UInt16) + UTF-16LE chars."""
    encoded = value.encode("utf-16-le")
    return struct.pack("<H", len(value)) + encoded


def write_lnk(target: str, lnk_path: str) -> None:
    """Write a minimal .lnk carrying *target* as a RelativePath string."""
    header = struct.pack("<I", _HEADER_SIZE)
    header += _LINK_CLSID
    header += struct.pack("<I", _LINK_FLAGS)
    header += struct.pack("<I", _FILE_ATTR_NORMAL)
    header += b"\x00" * 8  # CreationTime
    header += b"\x00" * 8  # AccessTime
    header += b"\x00" * 8  # WriteTime
    header += struct.pack("<I", 0)  # FileSize
    header += struct.pack("<i", 0)  # IconIndex
    header += struct.pack("<I", 1)  # ShowCommand = SW_SHOWNORMAL
    header += struct.pack("<H", 0)  # HotKey
    header += b"\x00" * 10  # Reserved1/2/3
    # No LinkTargetIDList (flag off) and no LinkInfo (flag off).
    body = _unicode_str(target)  # RelativePath StringData (HasRelativePath)
    with open(lnk_path, "wb") as f:
        f.write(header + body)
