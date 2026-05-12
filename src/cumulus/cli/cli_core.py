from __future__ import annotations

import sys
import threading
from datetime import datetime
from os import path as osp
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import click
from rich.console import Console, ConsoleRenderable

from .. import oauth
from ..core import FolderMetadata, SharedLinkMetadata
from ..utils.path import delete
from .common import (
    check_for_fatal_errors,
    config_option,
    convert_api_errors,
    existing_config_option,
    inject_proxy,
)
from .core import CliException, RemotePath
from .dialogs import confirm, prompt, select, select_multiple, select_path
from .output import RichDateField, echo, info, ok, rich_table, warn

if TYPE_CHECKING:
    from ..daemon import MaestralProxy
    from ..main import Maestral


OK = click.style("[OK]", fg="green")
FAILED = click.style("[FAILED]", fg="red")
KILLED = click.style("[KILLED]", fg="red")


def stop_daemon_with_cli_feedback(config_name: str) -> None:
    """Wrapper around :meth:`daemon.stop_cumulus_daemon_process`
    with command line feedback."""

    from ..daemon import Stop, stop_cumulus_daemon_process

    echo("Stopping Maestral...", nl=False)
    res = stop_cumulus_daemon_process(config_name)
    if res == Stop.Ok:
        echo("\rStopping Maestral...        " + OK)
    elif res == Stop.NotRunning:
        echo("\rMaestral daemon is not running.")
    elif res == Stop.Killed:
        echo("\rStopping Maestral...        " + KILLED)
    elif res == Stop.Failed:
        echo("\rStopping Maestral...        " + FAILED)


def select_remote_path_dialog(
    config_name: str, default_dir_name: str | None = None, allow_merge: bool = False
) -> str:
    """
    A CLI dialog to ask for a local sync folder location.

    :param config_name: The configuration to use for the default folder name.
    :param default_dir_name: The default directory name. Defaults to
        "Sync ({config_name})" if not given.
    :param allow_merge: If ``True``, allows the selection of an existing folder without
        deleting it. Defaults to ``False``.
    :returns: Path given by user.
    """
    default_dir_name = default_dir_name or f"Sync ({config_name.capitalize()})"

    while True:
        res = select_path(
            "Please choose a local sync folder:",
            default=f"~/{default_dir_name}",
            files_allowed=False,
        )
        res = res.rstrip(osp.sep)

        sync_path = osp.expanduser(res)

        if osp.exists(sync_path):
            if allow_merge:
                text = (
                    "Directory already exists. Do you want to replace it "
                    "or merge its content with your sync folder?"
                )
                choice = select(text, options=["replace", "merge", "cancel"])
            else:
                text = (
                    "Directory already exists. Do you want to replace it? "
                    "Its content will be lost!"
                )
                replace = confirm(text)
                choice = 0 if replace else 2

            if choice == 0:
                err = delete(sync_path)
                if err:
                    warn(
                        "Could not write to selected location. "
                        "Please make sure that you have sufficient permissions."
                    )
                else:
                    ok("Replaced existing folder")
                    return sync_path
            elif choice == 1:
                ok("Merging with existing folder")
                return sync_path

        else:
            return sync_path


def _local_server_dialog(m: "MaestralProxy | Maestral") -> int:
    """Shape A: run the loopback server in the CLI process, then hand the
    resulting refresh_token to the daemon's link(refresh_token=...).

    Returns the same 0/1/2 contract as Maestral.link.
    """
    info("A browser will open to authorize Cumulus to access your Google Drive.")
    info("If you're on a headless server, run on your laptop first:")
    info("    ssh -L 8080:localhost:8080 <your-host>")
    info("Then re-run 'cumulus auth link' here.")

    try:
        creds = oauth.run_local_flow(port=8080)
    except Exception as exc:  # AuthError already; surface message and bail.
        warn(str(exc))
        return 1

    if not getattr(creds, "refresh_token", None):
        warn(
            "Authorization completed but no refresh token was returned. "
            "Re-run with -r and approve the consent screen, or use "
            "--no-local-server."
        )
        return 1

    return m.link(refresh_token=creds.refresh_token)


def _parse_redirect_url_or_code(value: str) -> str:
    """Accept either the full redirect URL pasted from the browser address
    bar (e.g. 'http://localhost:8080/?code=4/abc&state=x') or a bare code
    ('4/abc'). Returns the bare code, or '' on failure."""
    value = value.strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        codes = parse_qs(parsed.query).get("code", [])
        return codes[0] if codes else ""
    return value


def _paste_redirect_dialog(m: "MaestralProxy | Maestral") -> int:
    """Shape C: ask the daemon for an auth URL, prompt the user to open it,
    then paste back the redirected URL (or just the code). The daemon
    consumes the code via link(code=...).
    """
    url = m.get_auth_url()
    info("Open this URL in any browser to authorize Cumulus:")
    info(f"    {url}")
    info(
        "After approving, your browser will fail to connect to a "
        "localhost URL — that's expected. Copy the URL from your "
        "browser's address bar and paste it below."
    )

    raw = prompt("Paste the redirected URL (or just the code)")
    code = _parse_redirect_url_or_code(raw)
    if not code:
        warn("Could not find an authorization code in that input.")
        return 1
    return m.link(code=code)


def _report_link_result(m: "MaestralProxy | Maestral", res: int) -> None:
    if res == 0:
        try:
            email = m.get_account_info().email or "your account"
        except Exception:
            # In edge cases (proxy disconnect mid-link, malformed response)
            # we still want to surface success rather than crash.
            email = "your account"
        ok(f"Linked to {email}")
    elif res == 1:
        warn("Invalid authorization, please try again.")
    elif res == 2:
        warn("Could not connect to Google, please try again.")


@click.command(help="Start the sync daemon.")
@click.option(
    "--foreground",
    "-f",
    is_flag=True,
    default=False,
    help="Start Maestral in the foreground.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Print log messages to stderr.",
)
@config_option
@convert_api_errors
def start(foreground: bool, verbose: bool, config_name: str) -> None:
    from ..daemon import (
        CommunicationError,
        MaestralProxy,
        Start,
        is_running,
        start_cumulus_daemon,
        start_cumulus_daemon_process,
        wait_for_startup,
    )

    if is_running(config_name):
        echo("Daemon is already running.")
        return

    @convert_api_errors
    def startup_dialog() -> None:
        try:
            wait_for_startup(config_name)
        except CommunicationError:
            return

        m = MaestralProxy(config_name)

        if m.pending_link:
            res = _local_server_dialog(m)
            _report_link_result(m, res)
            if res != 0:
                return

        if m.pending_sync_folder:
            path = select_remote_path_dialog(config_name, allow_merge=True)

            while True:
                try:
                    m.create_sync_directory(path)
                    break
                except OSError:
                    warn(
                        "Could not create folder. Please make sure that you have "
                        "permissions to write to the selected location or choose a "
                        "different location."
                    )

            include_all = confirm("Would you like sync all folders?")

            if not include_all:
                # get all top-level sync folders
                info("Loading...")
                entries = m.list_folder("/", recursive=False)

                names = [e.name for e in entries if isinstance(e, FolderMetadata)]

                choices = select_multiple(
                    "Choose which folders to include", options=names
                )

                excluded_paths = [
                    f"/{name}"
                    for index, name in enumerate(names)
                    if index not in choices
                ]

                m.excluded_items = excluded_paths

            ok("Setup completed. Starting sync.")

        m.start_sync()

    if foreground:
        setup_thread = threading.Thread(target=startup_dialog, daemon=True)
        setup_thread.start()

        start_cumulus_daemon(config_name, log_to_stderr=verbose)

    else:
        echo("Starting Maestral...", nl=False)

        res = start_cumulus_daemon_process(config_name)

        if res == Start.Ok:
            echo("\rStarting Maestral...        " + OK)
        elif res == Start.AlreadyRunning:
            echo("\rStarting Maestral...        " + "Already running.")
        else:
            echo("\rStarting Maestral...        " + FAILED)
            echo("Please check logs for more information.")

        startup_dialog()


@click.command(help="Stop the sync daemon.")
@existing_config_option
def stop(config_name: str) -> None:
    stop_daemon_with_cli_feedback(config_name)


@click.command(help="Run the GUI if installed.")
@config_option
def gui(config_name: str) -> None:
    import termios

    from importlib.metadata import entry_points, requires, version
    from packaging.requirements import Requirement
    from packaging.version import Version

    from ..daemon import start_cumulus_daemon_process

    # Find all entry points for "cumulus_gui" registered by other packages.
    gui_entry_points = entry_points(group="cumulus_gui")

    if len(gui_entry_points) == 0:
        raise CliException(
            "No cumulus GUI installed. Please run 'pip3 install cumulus[gui]'."
        )

    entry_point_names = [e.name for e in gui_entry_points]

    if len(entry_point_names) > 1 and sys.stdout.isatty():
        try:
            index = select("Multiple GUIs found, please choose:", entry_point_names)
        except termios.error:
            # Error can occur when not connected to a terminal. Fall back to the first
            # detected GUI instead of failing with an error.
            index = 0
    else:
        index = 0

    entry_point = gui_entry_points[entry_point_names[index]]

    if entry_point.name in {"cumulus_cocoa", "cumulus_qt"}:
        # For 1st party GUIs "cumulus_cocoa" or "cumulus_qt", check if the installed
        # version fulfills requirements in cumulus's gui extra.
        requirement_names = requires("cumulus")
        if requirement_names is not None:
            for name in requirement_names:
                r = Requirement(name)
                if r.marker and r.marker.evaluate({"extra": "gui"}):
                    version_str = version(r.name)
                    if not r.specifier.contains(Version(version_str), prereleases=True):
                        raise CliException(
                            f"{r.name}{r.specifier} required but you have {version_str}"
                        )
    # Start the daemon
    res = start_cumulus_daemon_process(config_name)

    # Run the GUI.
    run = entry_point.load()
    run(config_name, res)


@click.command(help="Pause syncing.")
@inject_proxy(fallback=False, existing_config=True)
def pause(m: Maestral) -> None:
    m.stop_sync()
    ok("Syncing paused.")


@click.command(help="Resume syncing.")
@inject_proxy(fallback=False, existing_config=True)
def resume(m: Maestral) -> None:
    if not check_for_fatal_errors(m):
        m.start_sync()
        ok("Syncing resumed.")


@click.group(help="Link, unlink and view the remote account.")
def auth() -> None:
    pass


@auth.command(name="link", help="Link a new remote account.")
@click.option(
    "--relink", "-r", is_flag=True, default=False,
    help="Relink to the existing account. Keeps the sync state.",
)
@click.option(
    "--no-local-server", is_flag=True, default=False,
    help="Use the paste-the-redirect-URL flow instead of the loopback "
         "server. Useful for headless setups without SSH port-forwarding.",
)
@click.option(
    "--refresh-token", "refresh_token", default=None, hidden=True,
)
@click.option(
    "--access-token", "access_token", default=None, hidden=True,
)
@inject_proxy(fallback=True, existing_config=False)
@convert_api_errors
def auth_link(
    m: "Maestral",
    relink: bool,
    no_local_server: bool,
    refresh_token: str | None,
    access_token: str | None,
) -> None:
    if not m.pending_link and not relink:
        echo(
            "Cumulus is already linked. Use '-r' to relink to the same "
            "account or specify a new config name with '-c'."
        )
        return

    if refresh_token or access_token:
        res = m.link(refresh_token=refresh_token, access_token=access_token)
    elif no_local_server:
        res = _paste_redirect_dialog(m)
    else:
        res = _local_server_dialog(m)

    _report_link_result(m, res)


@auth.command(
    name="unlink",
    help="""
Unlink your remote account.

If Maestral is running, it will be stopped before unlinking.
""",
)
@click.option(
    "--yes", "-Y", is_flag=True, default=False, help="Skip confirmation prompt."
)
@existing_config_option
@convert_api_errors
def auth_unlink(yes: bool, config_name: str) -> None:
    if not yes:
        yes = confirm("Are you sure you want unlink your account?", default=False)

    if yes:
        from ..main import Maestral

        stop_daemon_with_cli_feedback(config_name)
        m = Maestral(config_name)
        m.unlink()

        ok("Unlinked Maestral.")


@auth.command(name="status", help="View authentication status.")
@existing_config_option
def auth_status(config_name: str) -> None:
    from ..config import CumulusConfig, CumulusState

    conf = CumulusConfig(config_name)
    state = CumulusState(config_name)

    dbid = conf.get("auth", "account_id")
    email = state.get("account", "email")
    account_type = state.get("account", "type").capitalize()

    echo("")
    echo(f"Email:         {email}")
    echo(f"Account type:  {account_type}")
    echo(f"Remote ID:    {dbid}")
    echo("")


@click.group(help="Create and manage shared links.")
def sharelink() -> None:
    pass


@sharelink.command(
    name="create", help="Create a shared link for a file or folder. Return the URL."
)
@click.argument("sync_path", type=RemotePath())
@click.option(
    "-p",
    "--password",
    help="Optional password for the link.",
)
@click.option(
    "-e",
    "--expiry",
    metavar="DATE",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]),
    help="Expiry time for the link (e.g. '2025-07-24 20:50').",
)
@inject_proxy(fallback=True, existing_config=True)
@convert_api_errors
def sharelink_create(
    m: Maestral,
    sync_path: str,
    password: str,
    expiry: datetime | None,
) -> None:
    link_info = m.create_shared_link(sync_path, password=password, expires=expiry)
    echo(link_info.url)


@sharelink.command(name="revoke", help="Revoke a shared link.")
@click.argument("url", nargs=-1, required=True)
@inject_proxy(fallback=True, existing_config=True)
@convert_api_errors
def sharelink_revoke(m: Maestral, url: list[str]) -> None:
    for u in url:
        m.revoke_shared_link(u)
        ok(f"Revoked shared link {u}")


@sharelink.command(
    name="list", help="List shared links for given paths or all shared links."
)
@click.argument("sync_path", nargs=-1, type=RemotePath())
@click.option(
    "-l",
    "--long",
    is_flag=True,
    default=False,
    help="Show output in long format with metadata.",
)
@inject_proxy(fallback=True, existing_config=True)
@convert_api_errors
def sharelink_list(m: Maestral, sync_path: list[str], long: bool) -> None:
    links: list[SharedLinkMetadata]

    if len(sync_path) > 0:
        links = []
        for remote_path in sync_path:
            links.extend(m.list_shared_links(remote_path))
    else:
        links = m.list_shared_links()

    if long:
        link_table = rich_table("URL", "Item", "Access", "Expires")

        for link in links:
            dt_field: ConsoleRenderable | str

            if link.expires:
                dt_field = RichDateField(link.expires)
            else:
                dt_field = "-"

            if link.link_permissions.require_password:
                access = "password"
            else:
                access = link.link_permissions.effective_audience.value

            link_table.add_row(link.url, link.name, access, dt_field)

        console = Console()
        console.print(link_table)

    else:
        echo("\n".join(link.url for link in links))
