"""
This module provides interactive commandline dialogs which are based on the
:mod:`survey` Python library.
"""

from __future__ import annotations

import functools
import os
from typing import Any, Callable, Sequence, TypeVar

from typing_extensions import ParamSpec

P = ParamSpec("P")
T = TypeVar("T")

_survey_paste_fix_done = False


def _import_survey():  # type: ignore[no-untyped-def]
    """Import survey and, once per process, swap its POSIX terminal-mode
    switching from tcsetattr(TCSAFLUSH) to TCSADRAIN.

    TCSAFLUSH discards unread input, and survey re-enters raw mode many
    times per keystroke. A paste that reaches the kernel in more than one
    chunk (macOS ttys hand over 64 bytes per read) therefore loses
    everything after the first chunk mid-prompt. TCSADRAIN flushes pending
    output but keeps pending input.
    """
    global _survey_paste_fix_done
    import survey

    if not _survey_paste_fix_done:
        _survey_paste_fix_done = True
        try:
            import termios

            from survey._core import _io_os_posix

            def _set(self, mode):  # type: ignore[no-untyped-def]
                termios.tcsetattr(self._i_fd, termios.TCSADRAIN, mode)

            _io_os_posix.IO._set = _set
        except ImportError:
            # Windows, or a survey release with reshaped internals — leave
            # survey untouched rather than break every prompt.
            pass
    return survey


def _style_message(message: str) -> str:
    return f"{message} "


def _style_hint(hint: str) -> str:
    return f"{hint} " if hint else ""


def _style_error(message: str) -> str:
    survey = _import_survey()

    return survey.utils.paint(survey.colors.basic("red"), message)


def exit_on_keyboard_interrupt(func: Callable[P, T]) -> Callable[P, T]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        survey = _import_survey()

        try:
            return func(*args, **kwargs)
        except (KeyboardInterrupt, survey.widgets.Escape):
            raise SystemExit("Aborted")

    return wrapper


@exit_on_keyboard_interrupt
def prompt(
    message: str,
    validate: Callable[[str], bool] | None = None,
) -> str:
    survey = _import_survey()

    def check(value: str) -> None:
        if validate is not None and not validate(value):
            raise survey.widgets.Abort(_style_error(f"'{value}' is not allowed"))

    return survey.routines.input(
        _style_message(message),
        validate=check,
        escapable=True,
        mark_color=survey.colors.basic("cyan"),
    )


@exit_on_keyboard_interrupt
def confirm(message: str, default: bool | None = True) -> bool:
    survey = _import_survey()

    default_to_str = {True: "y", False: "n", None: None}

    return survey.routines.inquire(
        _style_message(message),
        default=default_to_str[default],
        escapable=True,
        mark_color=survey.colors.basic("cyan"),
    )


@exit_on_keyboard_interrupt
def select(message: str, options: Sequence[str], hint: str | None = "") -> int:
    survey = _import_survey()

    if hint is None:
        kwargs = {}
    else:
        kwargs = {"hint": _style_hint(hint)}

    return survey.routines.select(
        _style_message(message),
        options=options,
        escapable=True,
        mark_color=survey.colors.basic("cyan"),
        **kwargs,
    )


@exit_on_keyboard_interrupt
def select_multiple(
    message: str, options: Sequence[str], hint: str | None = None
) -> list[int]:
    survey = _import_survey()

    if hint is None:
        kwargs = {}
    else:
        kwargs = {"hint": _style_hint(hint)}

    def reply(widget: Any, indices: set[int]) -> str:
        chosen = [options[index] for index in indices]
        response = ", ".join(chosen)

        if len(indices) == 0 or len(response) > 10:
            response = f"[{len(indices)} chosen]"

        return survey.utils.paint(survey.colors.basic("cyan"), response)

    return survey.routines.basket(
        _style_message(message),
        options=options,
        positive_mark="[✓] ",
        negative_mark="[ ] ",
        reply=reply,
        escapable=True,
        mark_color=survey.colors.basic("cyan"),
        **kwargs,
    )


@exit_on_keyboard_interrupt
def select_path(
    message: str,
    default: str | None = None,
    validate: Callable[[str], bool] = lambda x: True,
    exists: bool = False,
    files_allowed: bool = True,
    dirs_allowed: bool = True,
) -> str:
    survey = _import_survey()

    styled_message = _style_message(message)

    def check(value: str) -> None:
        value = value.strip()

        if value == "" and default:
            return

        full_path = os.path.expanduser(value)
        forbidden_dir = os.path.isdir(full_path) and not dirs_allowed
        forbidden_file = os.path.isfile(full_path) and not files_allowed
        exist_condition = os.path.exists(full_path) or not exists

        if not exist_condition:
            raise survey.widgets.Abort(_style_error(f"'{value}' does not exist"))
        elif forbidden_dir:
            raise survey.widgets.Abort(_style_error(f"'{value}' is not a file"))
        elif forbidden_file:
            raise survey.widgets.Abort(_style_error(f"'{value}' is not a folder"))
        elif not validate(value):
            raise survey.widgets.Abort(_style_error(f"'{value}' is not allowed"))

    def reply(widget: Any, value: str) -> str:
        return survey.utils.paint(survey.colors.basic("cyan"), value or default)

    kwargs = {"hint": f"[{default}] "} if default else {}

    result = survey.routines.input(
        styled_message,
        reply=reply,
        validate=check,
        escapable=True,
        mark_color=survey.colors.basic("cyan"),
        **kwargs,
    )

    result = result.strip()

    if result == "" and default:
        return default
    elif result == "":
        raise RuntimeError("No result and no default")

    return result
