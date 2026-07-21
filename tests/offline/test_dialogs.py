"""Tests for the survey terminal-mode patch in bora.cli.dialogs.

survey switches the tty in and out of raw mode with tcsetattr(TCSAFLUSH)
many times per keystroke, and TCSAFLUSH discards unread input. When a
terminal delivers a paste to the kernel in more than one chunk (macOS ttys
hand over 64 bytes at a time), everything after the first chunk is thrown
away mid-prompt. dialogs patches survey to use TCSADRAIN, which flushes
pending output but keeps pending input.
"""

from __future__ import annotations

import os
import sys

import pytest

from bora.cli import dialogs

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="termios patch is POSIX-only"
)

# survey instantiates a module-level IO from sys.stdin/sys.stdout at import
# time, which requires real file descriptors — pytest's captured stdin has
# no fileno(). Keep devnull streams open for the life of the test module.
_stdin = open(os.devnull)
_stdout = open(os.devnull, "w")


def _import_patched_survey(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _stdin)
    monkeypatch.setattr(sys, "stdout", _stdout)
    return dialogs._import_survey()


def test_import_survey_patches_posix_set_to_tcsadrain(monkeypatch):
    import termios

    _import_patched_survey(monkeypatch)
    from survey._core import _io_os_posix

    calls = []
    monkeypatch.setattr(
        termios, "tcsetattr", lambda fd, when, mode: calls.append((fd, when))
    )

    fake_io = type("FakeIO", (), {"_i_fd": 42})()
    _io_os_posix.IO._set(fake_io, [0, 0, 0, 0, 0, 0, [0] * 32])

    assert calls == [(42, termios.TCSADRAIN)]


def test_import_survey_returns_survey_module(monkeypatch):
    survey = _import_patched_survey(monkeypatch)
    assert survey.__name__ == "survey"
