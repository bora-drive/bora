"""
This module provides constants used throughout bora, the GUI and CLI. It should
be kept free of memory heavy imports.
"""

import pathlib
import platform

# system imports
import sys
from enum import Enum
from importlib.metadata import PackageNotFoundError, metadata
from importlib.resources import as_file, files
from typing import ContextManager


def resource_path(package: str, resource: str) -> ContextManager[pathlib.Path]:
    return as_file(files(package) / resource)


FROZEN = getattr(sys, "frozen", False)

for package in (
    __package__,
    "bora",
    "bora-cocoa",
    "bora-qt",
    "bora-gui",
):
    try:
        FROZEN = "Briefcase-Version" in metadata(package) or FROZEN
    except PackageNotFoundError:
        pass

# app
APP_NAME = "Bora"
BUNDLE_ID = "tools.bora.app"
APP_ICON_PATH = resource_path("bora.resources", "bora.png").__enter__()
ENV = {"PYTHONOPTIMIZE": "2", "LC_CTYPE": "UTF-8"}
DEFAULT_CONFIG_NAME = "bora"

# sync
OLD_REV_FILE = ".bora"
MIGNORE_FILE = ".mignore"
FILE_CACHE = ".bora.cache"

EXCLUDED_FILE_NAMES = frozenset(
    [
        "desktop.ini",
        "Thumbs.db",
        "thumbs.db",
        ".DS_Store",
        ".ds_tore",
        ".Spotlight-V100",
        ".Trashes",
        ".fseventd",
        ".localized",
        ".TemporaryItems",
        "Icon\r",
        "icon\r",
        ".com.apple.timemachine.supported",
        ".dropbox",
        ".dropbox.attr",
        ".dropbox.cache",
        FILE_CACHE,
        OLD_REV_FILE,
    ]
)

EXCLUDED_DIR_NAMES = frozenset([".dropbox.cache", FILE_CACHE])

# state messages
IDLE = "Up to date"
SYNCING = "Syncing..."
PAUSED = "Paused"
CONNECTED = "Connected"
DISCONNECTED = "Connection lost"
CONNECTING = "Connecting..."
SYNC_ERROR = "Sync error"
ERROR = "Fatal error"


# file status enum
class FileStatus(Enum):
    """Enumeration of sync status"""

    Unwatched = "unwatched"
    Uploading = "uploading"
    Downloading = "downloading"
    Error = "error"
    Synced = "up to date"


# platform detection
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# keys
# Google OAuth client ID / secret will be loaded from config or resources.

# urls
# TODO: Update to point to the fork's releases URL.
GITHUB_RELEASES_API = "https://api.github.com/repos/samschott/bora/releases"
