from __future__ import annotations

from typing import TYPE_CHECKING

import click

from .common import convert_api_errors, existing_config_option, inject_proxy
from .core import CliException, RemotePath
from .output import echo, ok

if TYPE_CHECKING:
    from ..main import Bora


@click.command(
    help="""
Automatically start the sync daemon on login.

A systemd or launchd service will be created to start a sync daemon for the given
configuration on user login.
""",
)
@click.option("--yes", "-Y", is_flag=True, default=False)
@click.option("--no", "-N", is_flag=True, default=False)
@existing_config_option
def autostart(yes: bool, no: bool, config_name: str) -> None:
    from ..autostart import AutoStart

    auto_start = AutoStart(config_name)

    if not auto_start.implementation:
        echo(
            "Autostart is currently not supported for your platform.\n"
            "Autostart requires systemd on Linux or launchd on macOS."
        )
        return

    if yes or no:
        if yes:
            auto_start.enable()
            ok("Enabled start on login.")
        else:
            auto_start.disable()
            ok("Disabled start on login.")
    else:
        if auto_start.enabled:
            echo("Autostart is enabled. Use -N to disable.")
        else:
            echo("Autostart is disabled. Use -Y to enable.")


@click.group(help="View and manage excluded folders.")
def excluded() -> None:
    pass


@excluded.command(name="list", help="List all excluded files and folders.")
@inject_proxy(fallback=True, existing_config=True)
def excluded_list(m: Bora) -> None:
    excluded_items = m.excluded_items

    if len(excluded_items) == 0:
        echo("No excluded files or folders.")
    else:
        for item in sorted(excluded_items):
            echo(item)


@excluded.command(
    name="add",
    help="Add files or folders to the excluded list and re-sync.",
)
@click.argument("sync_paths", type=RemotePath(), nargs=-1)
@inject_proxy(fallback=True, existing_config=True)
@convert_api_errors
def excluded_add(m: Bora, sync_paths: list[str]) -> None:
    if any(p == "/" for p in sync_paths):
        raise CliException("Cannot exclude the root directory.")

    m.exclude_items(*sync_paths)
    for path in sync_paths:
        ok(f"Excluded '{path}'")


@excluded.command(
    name="remove",
    help="""
Remove files or folders from the excluded list and re-sync.

It is safe to call this method with items which have already been included, they will
not be downloaded again. If the given path lies inside an excluded folder, the parent
folder will be included as well (but no other items inside it).
""",
)
@click.argument("sync_paths", type=RemotePath(), nargs=-1)
@inject_proxy(fallback=False, existing_config=True)
@convert_api_errors
def excluded_remove(m: Bora, sync_paths: str) -> None:
    if any(p == "/" for p in sync_paths):
        return echo("The root directory is always included")

    m.include_items(*sync_paths)
    for path in sync_paths:
        ok(f"Included '{path}'")
    ok("Downloading...")


@click.group(help="Manage desktop notifications.")
def notify() -> None:
    pass


@notify.command(
    name="level",
    help="Get or set the level for desktop notifications.",
)
@click.argument(
    "level_name",
    required=False,
    type=click.Choice(["ERROR", "SYNCISSUE", "FILECHANGE"], case_sensitive=False),
)
@inject_proxy(fallback=True, existing_config=True)
def notify_level(m: Bora, level_name: str) -> None:
    from .. import notify as _notify

    if level_name:
        m.notification_level = _notify.level_name_to_number(level_name)
        ok(f"Notification level set to {level_name}.")
    else:
        level_name = _notify.level_number_to_name(m.notification_level)
        echo(f"Notification level: {level_name}.")


@notify.command(
    name="snooze",
    help="Snooze desktop notifications of file changes.",
)
@click.argument("minutes", type=click.IntRange(min=0))
@inject_proxy(fallback=True, existing_config=True)
def notify_snooze(m: Bora, minutes: int) -> None:
    m.notification_snooze = minutes

    if minutes > 0:
        ok(f"Notifications snoozed for {minutes} min. Set snooze to 0 to reset.")
    else:
        ok("Notifications enabled.")


@click.group(help="View and manage bandwidth limits. Changes take effect immediately.")
def bandwidth_limit() -> None:
    pass


@bandwidth_limit.command(
    name="up", help="Get / set bandwidth limit for uploads in MB/sec (0 = unlimited)."
)
@click.argument(
    "mb_per_second",
    required=False,
    type=click.FLOAT,
)
@inject_proxy(fallback=True, existing_config=True)
def bandwidth_limit_up(m: Bora, mb_per_second: float | None) -> None:
    if mb_per_second is not None:
        m.bandwidth_limit_up = mb_per_second * 10**6
        speed_str = f"{mb_per_second} MB/sec" if mb_per_second > 0 else "unlimited"
        ok(f"Upload bandwidth limit set to {speed_str}.")
    else:
        mb_per_second = m.bandwidth_limit_up / 10**6
        echo(f"{mb_per_second} MB/sec" if mb_per_second > 0 else "unlimited")


@bandwidth_limit.command(
    name="down",
    help="Get / set bandwidth limit for downloads in MB/sec (0 = unlimited).",
)
@click.argument(
    "mb_per_second",
    required=False,
    type=click.FLOAT,
)
@inject_proxy(fallback=True, existing_config=True)
def bandwidth_limit_down(m: Bora, mb_per_second: float | None) -> None:
    if mb_per_second is not None:
        m.bandwidth_limit_down = mb_per_second * 10**6
        speed_fmt = f"{mb_per_second} MB/sec" if mb_per_second > 0 else "unlimited"
        ok(f"Download bandwidth limit set to {speed_fmt}.")
    else:
        mb_per_second = m.bandwidth_limit_down / 10**6
        echo(f"{mb_per_second} MB/sec" if mb_per_second > 0 else "unlimited")


@click.group(help="View and manage exporting native Google Docs, Sheets and Slides.")
def export() -> None:
    pass


@export.command(
    name="docs",
    help="Get / set whether to export native Google Docs, Sheets and Slides.",
)
@click.argument(
    "state",
    required=False,
    type=click.Choice(["on", "off"], case_sensitive=False),
)
@inject_proxy(fallback=True, existing_config=True)
def export_docs(m: Bora, state: str | None) -> None:
    if state is not None:
        m.export_google_docs = state.lower() == "on"
        ok(f"Exporting native Google Docs: {state.lower()}.")
        echo(
            "Existing mirrors convert on each Doc's next edit; "
            "run 'bora rebuild-index' to convert them now."
        )
    else:
        echo("on" if m.export_google_docs else "off")


@export.command(
    name="markdown",
    help="Get / set whether to export Google Docs to Markdown (instead of .docx).",
)
@click.argument(
    "state",
    required=False,
    type=click.Choice(["on", "off"], case_sensitive=False),
)
@inject_proxy(fallback=True, existing_config=True)
def export_markdown(m: Bora, state: str | None) -> None:
    if state is not None:
        m.export_google_docs_markdown = state.lower() == "on"
        ok(f"Exporting Google Docs to Markdown: {state.lower()}.")
        echo(
            "Existing mirrors convert on each Doc's next edit; "
            "run 'bora rebuild-index' to convert them now."
        )
    else:
        echo("on" if m.export_google_docs_markdown else "off")
