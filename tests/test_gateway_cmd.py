"""Unit tests for claw gateway CLI commands."""

from unittest.mock import patch, MagicMock
import argparse

import pytest


@pytest.fixture
def mock_config():
    """Create a mock GatewayConfig with default port 18080."""
    from gateway.config import GatewayConfig, PlatformConfig, McpServerConfig

    return GatewayConfig(
        host="0.0.0.0",
        port=18080,
        platforms={
            "weixin": PlatformConfig(name="weixin", enabled=False, settings={})
        },
        mcp_servers={
            "sqlite": McpServerConfig(
                name="sqlite",
                command="uvx",
                args=["mcp-server-sqlite", "--db-path", "./claw_data.db"],
                env={},
                enabled=True,
            )
        },
    )


class TestMcpCommand:
    """Tests for `claw gateway mcp` port-like validation in MCP config."""

    @patch("cli.gateway_cmd.save_gateway_config", return_value=True)
    @patch("cli.gateway_cmd.load_gateway_config")
    def test_mcp_enable_sqlite(self, mock_load, mock_save, mock_config):
        """Enable sqlite MCP server via the mcp command."""
        mock_load.return_value = mock_config

        # enable sqlite=y, db_path=default, enable github=n, enable tavily=n, enable weather=y
        inputs = ["y", "./claw_data.db", "n", "n", "y"]
        with patch("cli.interactive_ui.input", side_effect=inputs):
            from cli.gateway_cmd import _cmd_mcp

            result = _cmd_mcp(argparse.Namespace())

        assert result is True
        saved_config = mock_save.call_args[0][0]
        assert saved_config.mcp_servers["sqlite"].enabled is True


class TestAddCommand:
    """Tests for `claw gateway add` platform dispatch."""

    def test_add_unknown_platform_returns_false(self):
        """Adding an unknown platform shows error."""
        from cli.gateway_cmd import _cmd_add

        args = argparse.Namespace(platform="nonexistent")
        result = _cmd_add(args)
        assert result is False

    @patch("cli.gateway_cmd.prompt_choice", return_value=0)
    def test_add_no_platform_shows_menu(self, mock_choice):
        """Running add without platform name shows selection menu."""
        from cli.gateway_cmd import _cmd_add

        # Mock the weixin setup to avoid actual QR login
        with patch("cli.gateway_cmd._PLATFORM_SETUP", {"weixin": lambda: True}):
            args = argparse.Namespace(platform=None)
            result = _cmd_add(args)

        assert result is True
        mock_choice.assert_called_once()


class TestRemoveCommand:
    """Tests for `claw gateway remove`."""

    @patch("cli.gateway_cmd.save_gateway_config", return_value=True)
    @patch("cli.gateway_cmd.load_gateway_config")
    def test_remove_existing_platform(self, mock_load, mock_save, mock_config):
        """Removing an existing platform deletes it from config."""
        mock_load.return_value = mock_config

        with patch("cli.interactive_ui.input", return_value="y"):
            from cli.gateway_cmd import _cmd_remove

            args = argparse.Namespace(platform="weixin")
            result = _cmd_remove(args)

        assert result is True
        saved_config = mock_save.call_args[0][0]
        assert "weixin" not in saved_config.platforms

    def test_remove_nonexistent_platform(self, mock_config):
        """Removing a platform that doesn't exist shows error."""
        with patch("cli.gateway_cmd.load_gateway_config", return_value=mock_config):
            from cli.gateway_cmd import _cmd_remove

            args = argparse.Namespace(platform="telegram")
            result = _cmd_remove(args)

        assert result is False


class TestStatusCommand:
    """Tests for `claw gateway status`."""

    @patch("cli.gateway_cmd.load_gateway_config")
    def test_status_shows_port(self, mock_load, mock_config, capsys):
        """Status displays the configured port."""
        mock_load.return_value = mock_config

        from cli.gateway_cmd import _cmd_status

        result = _cmd_status(argparse.Namespace())

        assert result is True
        captured = capsys.readouterr()
        assert "18080" in captured.out
