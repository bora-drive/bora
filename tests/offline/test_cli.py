import logging
from unittest import mock

from click.testing import CliRunner

from bora.autostart import AutoStart
from bora.cli import main
from bora.daemon import BoraProxy, Start, start_bora_daemon_process
from bora.logging import scoped_logger
from bora.main import Bora
from bora.notify import level_name_to_number, level_number_to_name
from bora.utils.appdirs import get_log_path

TEST_TIMEOUT = 60


def test_help() -> None:
    """Test help output without args and with --help arg."""
    runner = CliRunner()

    result_no_arg = runner.invoke(main)
    result_help_arg = runner.invoke(main, ["--help"])

    assert result_no_arg.exit_code == 2, result_no_arg.output
    assert result_help_arg.exit_code == 0, result_no_arg.output
    assert result_no_arg.output.startswith("Usage: main [OPTIONS] COMMAND [ARGS]")

    assert result_no_arg.output == result_help_arg.output


def test_invalid_config() -> None:
    """Test failure of commands that require an existing config file"""

    for command in [
        ("stop",),
        ("pause",),
        ("resume",),
        ("auth", "status"),
        ("auth", "unlink"),
        ("status",),
        ("filestatus",),
        ("activity",),
        ("history",),
        ("ls",),
        ("autostart",),
        ("excluded", "add"),
        ("excluded", "list"),
        ("excluded", "remove"),
        ("notify", "level"),
        ("notify", "snooze"),
        ("move-dir",),
        ("rebuild-index",),
        ("revs",),
        ("diff",),
        ("restore",),
        ("log", "level"),
        ("log", "clear"),
        ("log", "show"),
        ("config", "get", "path"),
        ("config", "set", "path"),
        ("config", "show"),
    ]:
        runner = CliRunner()
        result = runner.invoke(main, [*command, "-c", "non-existent-config"])

        assert result.exit_code == 1, command
        assert (
            result.output == "! Configuration 'non-existent-config' does not exist. "
            "Use 'bora config-files' to list all configurations.\n"
        )


def test_start_already_running(config_name: str) -> None:
    res = start_bora_daemon_process(config_name, timeout=TEST_TIMEOUT)

    assert res is Start.Ok

    runner = CliRunner()
    result = runner.invoke(main, ["start", "-c", config_name])

    assert result.exit_code == 0, result.output
    assert "already running" in result.output


def test_stop(config_name: str) -> None:
    res = start_bora_daemon_process(config_name, timeout=TEST_TIMEOUT)
    assert res is Start.Ok

    runner = CliRunner()
    result = runner.invoke(main, ["stop", "-c", config_name])

    assert result.exit_code == 0, result.output


def test_filestatus(m: Bora) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["filestatus", "/usr", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert result.output == "unwatched\n"

    result = runner.invoke(main, ["filestatus", "/invalid-dir", "-c", m.config_name])

    # the exception will be already raised by click's argument check
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert "'/invalid-dir' does not exist" in result.output


def test_autostart(m: Bora) -> None:
    autostart = AutoStart(m.config_name)
    autostart.disable()

    runner = CliRunner()
    result = runner.invoke(main, ["autostart", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert "disabled" in result.output

    result = runner.invoke(main, ["autostart", "-Y", "-c", m.config_name])

    if autostart.implementation:
        if result.exit_code == 0:
            assert "Enabled" in result.output
            assert autostart.enabled
        else:
            # TODO: be more specific here
            assert result.exception is not None
    else:
        assert "not supported" in result.output
        assert not autostart.enabled

    result = runner.invoke(main, ["autostart", "-N", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert "Disabled" in result.output
    assert not autostart.enabled


def test_excluded_list(m: Bora) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["excluded", "list", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert result.output == "No excluded files or folders.\n"


def test_excluded_add_raises_not_linked_error(m: Bora) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["excluded", "add", "test", "-c", m.config_name])

    assert result.exit_code == 1
    assert "No remote account linked" in result.output


def test_excluded_remove_raises_not_running_error(m: Bora) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["excluded", "remove", "test", "-c", m.config_name])

    assert result.exit_code == 1
    assert "Bora daemon is not running" in result.output


def test_notify_level(config_name: str) -> None:
    start_bora_daemon_process(config_name, timeout=TEST_TIMEOUT)
    m = BoraProxy(config_name)

    runner = CliRunner()
    result = runner.invoke(main, ["notify", "level", "-c", m.config_name])

    level_name = level_number_to_name(m.notification_level)

    assert result.exit_code == 0, result.output
    assert level_name in result.output

    level_name = "SYNCISSUE"
    level_number = level_name_to_number(level_name)
    result = runner.invoke(main, ["notify", "level", level_name, "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert level_name in result.output
    assert m.notification_level == level_number

    result = runner.invoke(main, ["notify", "level", "INVALID", "-c", m.config_name])

    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)


def test_notify_snooze(config_name: str) -> None:
    start_bora_daemon_process(config_name, timeout=TEST_TIMEOUT)
    m = BoraProxy(config_name)

    runner = CliRunner()
    result = runner.invoke(main, ["notify", "snooze", "20", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert 0 < m.notification_snooze <= 20

    result = runner.invoke(main, ["notify", "snooze", "0", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert m.notification_snooze == 0


def test_export_docs(m: Bora) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["export", "docs", "off", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert m.export_google_docs is False

    result = runner.invoke(main, ["export", "docs", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert "off" in result.output.lower()

    result = runner.invoke(main, ["export", "docs", "on", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert m.export_google_docs is True


def test_export_markdown(m: Bora) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["export", "markdown", "off", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert m.export_google_docs_markdown is False

    result = runner.invoke(main, ["export", "markdown", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert "off" in result.output.lower()

    result = runner.invoke(main, ["export", "markdown", "on", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert m.export_google_docs_markdown is True


def test_log_level(m: Bora) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["log", "level", "-c", m.config_name])

    level_name = logging.getLevelName(m.log_level)

    assert result.exit_code == 0, result.output
    assert level_name in result.output

    result = runner.invoke(main, ["log", "level", "DEBUG", "-c", m.config_name])
    assert result.exit_code == 0, result.output
    assert "DEBUG" in result.output

    result = runner.invoke(main, ["notify", "level", "INVALID", "-c", m.config_name])
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)


def test_log_show(m: Bora) -> None:
    # log a message
    logger = scoped_logger("bora", m.config_name)
    logger.info("Hello from pytest!")
    runner = CliRunner()
    result = runner.invoke(main, ["log", "show", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert "Hello from pytest!" in result.output


def test_log_clear(m: Bora) -> None:
    # log a message
    logger = scoped_logger("bora", m.config_name)
    logger.info("Hello from pytest!")
    runner = CliRunner()
    result = runner.invoke(main, ["log", "show", "-c", m.config_name])

    assert result.exit_code == 0, result.output
    assert "Hello from pytest!" in result.output

    # Stop connection helper to prevent spurious log messages.
    m.manager._connection_helper_running = False
    m.manager.connection_helper.join()

    # clear the logs
    result = runner.invoke(main, ["log", "clear", "-c", m.config_name])
    assert result.exit_code == 0, result.output

    logfile = get_log_path("bora", f"{m.config_name}.log")
    with open(logfile) as f:
        log_content = f.read()

    assert log_content == ""


from bora.cli.cli_core import _local_server_dialog


def test_local_server_dialog_invokes_run_local_flow_and_passes_refresh_token():
    m = mock.Mock()
    m.config_name = "test-config"
    m.link = mock.Mock(return_value=0)

    fake_creds = mock.Mock()
    fake_creds.refresh_token = "rt-from-loopback"

    with mock.patch(
        "bora.cli.cli_core.oauth.run_local_flow",
        return_value=fake_creds,
    ):
        res = _local_server_dialog(m)

    m.link.assert_called_once_with(refresh_token="rt-from-loopback")
    assert res == 0


def test_local_server_dialog_returns_1_when_creds_lack_refresh_token():
    m = mock.Mock()
    m.config_name = "test-config"
    m.link = mock.Mock()

    fake_creds = mock.Mock()
    fake_creds.refresh_token = None  # consent skipped; refresh_token missing.

    with mock.patch(
        "bora.cli.cli_core.oauth.run_local_flow",
        return_value=fake_creds,
    ):
        res = _local_server_dialog(m)

    assert res == 1
    m.link.assert_not_called()


from bora.cli.cli_core import (
    _parse_redirect_url_or_code,
    _paste_redirect_dialog,
)


def test_parse_redirect_url_extracts_code_from_full_redirect():
    url = "http://localhost:8080/?code=4%2F0AY0e-g7XYZ&state=abc&scope=drive"
    assert _parse_redirect_url_or_code(url) == "4/0AY0e-g7XYZ"


def test_parse_redirect_url_extracts_code_from_bare_code():
    assert _parse_redirect_url_or_code("4/0AY0e-g7XYZ") == "4/0AY0e-g7XYZ"


def test_parse_redirect_url_strips_whitespace():
    assert _parse_redirect_url_or_code("  4/0AY0e-g7XYZ  ") == "4/0AY0e-g7XYZ"


def test_parse_redirect_url_returns_empty_on_no_code():
    assert _parse_redirect_url_or_code("http://localhost:8080/?error=denied") == ""
    assert _parse_redirect_url_or_code("") == ""


def test_paste_redirect_dialog_calls_link_with_parsed_code(monkeypatch):
    m = mock.Mock()
    m.get_auth_url = mock.Mock(return_value="https://accounts.google.com/auth")
    m.link = mock.Mock(return_value=0)

    # Simulate user pasting the full redirect URL.
    monkeypatch.setattr(
        "bora.cli.cli_core.prompt",
        lambda _: "http://localhost:8080/?code=4/abc&state=x",
    )

    res = _paste_redirect_dialog(m)
    m.link.assert_called_once_with(code="4/abc")
    assert res == 0


def test_paste_redirect_dialog_returns_1_on_empty_parse(monkeypatch):
    m = mock.Mock()
    m.get_auth_url = mock.Mock(return_value="https://accounts.google.com/auth")
    m.link = mock.Mock()

    monkeypatch.setattr("bora.cli.cli_core.prompt", lambda _: "")

    res = _paste_redirect_dialog(m)
    m.link.assert_not_called()
    assert res == 1


def _make_proxy_mock(*, pending_link: bool = True, config_name: str = "test-cli"):
    """Return a MagicMock that acts as a context manager returning itself."""
    m = mock.MagicMock()
    m.__enter__ = mock.Mock(return_value=m)
    m.__exit__ = mock.Mock(return_value=False)
    m.pending_link = pending_link
    m.config_name = config_name
    return m


def test_auth_link_uses_local_server_dialog_by_default(monkeypatch):
    m_proxy = _make_proxy_mock()

    called = []

    def _fake_local(m):
        called.append("local")
        return 0

    def _fake_paste(m):
        called.append("paste")
        return 0

    monkeypatch.setattr("bora.cli.cli_core._local_server_dialog", _fake_local)
    monkeypatch.setattr("bora.cli.cli_core._paste_redirect_dialog", _fake_paste)
    monkeypatch.setattr(
        "bora.cli.cli_core._report_link_result",
        lambda m, res: None,
    )
    monkeypatch.setattr(
        "bora.daemon.BoraProxy",
        lambda config_name, fallback: m_proxy,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["auth", "link", "-c", "test-cli"])

    assert result.exit_code == 0, result.output
    assert called == ["local"]


def test_auth_link_uses_paste_redirect_when_flag_set(monkeypatch):
    m_proxy = _make_proxy_mock()

    called = []
    monkeypatch.setattr(
        "bora.cli.cli_core._local_server_dialog",
        lambda m: called.append("local") or 0,
    )
    monkeypatch.setattr(
        "bora.cli.cli_core._paste_redirect_dialog",
        lambda m: called.append("paste") or 0,
    )
    monkeypatch.setattr(
        "bora.cli.cli_core._report_link_result",
        lambda m, res: None,
    )
    monkeypatch.setattr(
        "bora.daemon.BoraProxy",
        lambda config_name, fallback: m_proxy,
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["auth", "link", "--no-local-server", "-c", "test-cli"]
    )

    assert result.exit_code == 0, result.output
    assert called == ["paste"]


def test_sharelink_command_removed():
    """sharelink is cut for v0.1.0 (Drive sharing deferred); it must not be
    registered, so invoking it errors as an unknown command."""
    from click.testing import CliRunner

    from bora.cli.cli_main import main

    result = CliRunner().invoke(main, ["sharelink", "list"])
    assert result.exit_code != 0
    assert "No such command 'sharelink'" in result.output
