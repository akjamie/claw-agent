"""Unit tests for McpManager.find_tool and call_tool_by_name."""

from unittest.mock import patch, MagicMock
from gateway.mcp_client import McpManager, McpServerProcess, McpTool, McpToolResult
from gateway.config import McpServerConfig


def _make_config(name: str) -> McpServerConfig:
    return McpServerConfig(name=name, command="echo", args=[], env={}, enabled=True)


def _make_manager_with_tools():
    """Create an McpManager with two mock servers, each exposing different tools."""
    manager = McpManager()
    config_a = _make_config("server_a")
    config_b = _make_config("server_b")
    manager.add_server(config_a)
    manager.add_server(config_b)

    # Patch is_alive and list_tools on the server processes
    server_a = manager._servers["server_a"]
    server_b = manager._servers["server_b"]

    server_a._process = MagicMock()
    server_a._process.poll.return_value = None  # is_alive = True
    server_a._tools = [
        McpTool(name="tool_x", description="Tool X", input_schema={}, server_name="server_a"),
        McpTool(name="tool_y", description="Tool Y", input_schema={}, server_name="server_a"),
    ]

    server_b._process = MagicMock()
    server_b._process.poll.return_value = None  # is_alive = True
    server_b._tools = [
        McpTool(name="tool_z", description="Tool Z", input_schema={}, server_name="server_b"),
    ]

    return manager


class TestFindTool:
    def test_find_tool_returns_matching_tool(self):
        manager = _make_manager_with_tools()
        tool = manager.find_tool("tool_x")
        assert tool is not None
        assert tool.name == "tool_x"
        assert tool.server_name == "server_a"

    def test_find_tool_returns_tool_from_second_server(self):
        manager = _make_manager_with_tools()
        tool = manager.find_tool("tool_z")
        assert tool is not None
        assert tool.name == "tool_z"
        assert tool.server_name == "server_b"

    def test_find_tool_returns_none_when_not_found(self):
        manager = _make_manager_with_tools()
        tool = manager.find_tool("nonexistent_tool")
        assert tool is None

    def test_find_tool_skips_dead_servers(self):
        manager = _make_manager_with_tools()
        # Kill server_a
        manager._servers["server_a"]._process.poll.return_value = 1  # is_alive = False
        tool = manager.find_tool("tool_x")
        assert tool is None  # tool_x is on server_a which is dead


class TestCallToolByName:
    def test_call_tool_by_name_invokes_on_correct_server(self):
        manager = _make_manager_with_tools()

        # Mock the call_tool on the underlying server process
        expected_result = McpToolResult(success=True, content="result data")
        manager._servers["server_b"]._send_request = MagicMock(return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "result data"}]
            }
        })

        result = manager.call_tool_by_name("tool_z", {"arg1": "value1"})
        assert result.success is True
        assert result.content == "result data"

    def test_call_tool_by_name_returns_error_when_tool_not_found(self):
        manager = _make_manager_with_tools()
        result = manager.call_tool_by_name("nonexistent_tool", {})
        assert result.success is False
        assert result.error == "Tool not found: nonexistent_tool"

    def test_call_tool_by_name_returns_error_when_no_servers(self):
        manager = McpManager()
        result = manager.call_tool_by_name("any_tool", {})
        assert result.success is False
        assert result.error == "Tool not found: any_tool"
