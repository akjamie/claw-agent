"""Unit tests for McpServerProcess with mock subprocess.

Tests the MCP client's JSON-RPC communication by mocking subprocess.Popen
stdin/stdout to simulate server responses.

Validates Requirements:
- 9.5: Unit tests for McpManager with mock subprocess
- 3.1: MCP server launched as subprocess with stdin/stdout pipes
- 3.6: tools/list returns available tools with name, description, input_schema
- 3.7: tools/call sends JSON-RPC request and returns result content
- 3.12: isError flag returns McpToolResult(success=False, error=content)
"""

import io
import json
from unittest.mock import patch, MagicMock

import pytest

from gateway.config import McpServerConfig
from gateway.mcp_client import McpServerProcess, McpTool, McpToolResult


def _make_config(name: str = "test-server", command: str = "echo") -> McpServerConfig:
    """Create a test McpServerConfig."""
    return McpServerConfig(
        name=name,
        command=command,
        args=["--some-arg"],
        env={"TEST_VAR": "1"},
        enabled=True,
    )


def _make_server(name: str = "test-server", command: str = "echo") -> McpServerProcess:
    """Create an McpServerProcess with a test config."""
    return McpServerProcess(_make_config(name, command))


def _json_line(obj: dict) -> bytes:
    """Encode a dict as a newline-delimited JSON bytes line."""
    return json.dumps(obj).encode("utf-8") + b"\n"


def _mock_stdout_lines(*responses: dict) -> io.BytesIO:
    """Create a BytesIO with multiple JSON-RPC response lines concatenated."""
    data = b"".join(_json_line(r) for r in responses)
    return io.BytesIO(data)


def _attach_mock_process(server: McpServerProcess, *responses: dict) -> MagicMock:
    """Attach a mock process with pre-loaded stdout responses to a server.

    Sets the process as alive (poll returns None) and provides a writable stdin.
    """
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # process is alive
    mock_proc.stdin = io.BytesIO()
    mock_proc.stdout = _mock_stdout_lines(*responses)
    server._process = mock_proc
    return mock_proc


class TestListTools:
    """Test list_tools returns McpTool objects parsed from mock JSON-RPC response."""

    def test_list_tools_returns_mcp_tool_objects(self):
        """list_tools should parse tools/list response into McpTool dataclass instances."""
        server = _make_server()

        tools_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "read_query",
                        "description": "Execute a SELECT query",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                    {
                        "name": "write_query",
                        "description": "Execute an INSERT/UPDATE/DELETE query",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                ]
            },
        }

        _attach_mock_process(server, tools_response)

        tools = server.list_tools()

        assert len(tools) == 2
        assert all(isinstance(t, McpTool) for t in tools)

        assert tools[0].name == "read_query"
        assert tools[0].description == "Execute a SELECT query"
        assert tools[0].input_schema == {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        }
        assert tools[0].server_name == "test-server"

        assert tools[1].name == "write_query"
        assert tools[1].description == "Execute an INSERT/UPDATE/DELETE query"
        assert tools[1].server_name == "test-server"

    def test_list_tools_empty_tools_array(self):
        """list_tools returns empty list when server has no tools."""
        server = _make_server()

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": []},
        }

        _attach_mock_process(server, response)

        tools = server.list_tools()
        assert tools == []

    def test_list_tools_caches_result(self):
        """list_tools caches tools after first call (does not re-request)."""
        server = _make_server()

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {"name": "cached_tool", "description": "A tool", "inputSchema": {}},
                ]
            },
        }

        _attach_mock_process(server, response)

        # First call fetches from server
        tools1 = server.list_tools()
        assert len(tools1) == 1

        # Second call returns cached (stdout is exhausted, would fail if re-read)
        tools2 = server.list_tools()
        assert tools2 == tools1

    def test_list_tools_no_response_returns_empty(self):
        """list_tools returns empty list when server returns no response (stdout closed)."""
        server = _make_server()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = io.BytesIO()
        mock_proc.stdout = io.BytesIO(b"")  # empty stdout
        server._process = mock_proc

        tools = server.list_tools()
        assert tools == []


class TestCallTool:
    """Test call_tool sends correct JSON-RPC request and returns McpToolResult."""

    def test_call_tool_sends_correct_json_rpc_request(self):
        """call_tool should write a valid tools/call JSON-RPC request to stdin."""
        server = _make_server()

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "query result: 42"}],
            },
        }

        mock_proc = _attach_mock_process(server, response)

        result = server.call_tool("read_query", {"query": "SELECT 1"})

        # Verify the request written to stdin
        stdin_data = mock_proc.stdin.getvalue().decode("utf-8")
        request = json.loads(stdin_data.strip())

        assert request["jsonrpc"] == "2.0"
        assert request["method"] == "tools/call"
        assert request["params"]["name"] == "read_query"
        assert request["params"]["arguments"] == {"query": "SELECT 1"}
        assert "id" in request

    def test_call_tool_returns_mcp_tool_result_with_content(self):
        """call_tool should return McpToolResult with success=True and extracted text content."""
        server = _make_server()

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "Hello from tool"}],
            },
        }

        _attach_mock_process(server, response)

        result = server.call_tool("greet", {"name": "world"})

        assert isinstance(result, McpToolResult)
        assert result.success is True
        assert result.content == "Hello from tool"
        assert result.error is None

    def test_call_tool_concatenates_multiple_text_parts(self):
        """call_tool joins multiple text content parts with newlines."""
        server = _make_server()

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "Line 1"},
                    {"type": "text", "text": "Line 2"},
                    {"type": "text", "text": "Line 3"},
                ],
            },
        }

        _attach_mock_process(server, response)

        result = server.call_tool("multi_output", {})

        assert result.success is True
        assert result.content == "Line 1\nLine 2\nLine 3"


class TestCallToolIsError:
    """Test call_tool with isError: true returns failure result (Requirement 3.12)."""

    def test_is_error_true_returns_failure(self):
        """When isError is true, call_tool returns McpToolResult with success=False."""
        server = _make_server()

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "Permission denied: cannot write"}],
                "isError": True,
            },
        }

        _attach_mock_process(server, response)

        result = server.call_tool("write_query", {"query": "DROP TABLE users"})

        assert isinstance(result, McpToolResult)
        assert result.success is False
        assert result.error == "Permission denied: cannot write"
        assert result.content == "Permission denied: cannot write"

    def test_is_error_true_with_multiple_error_parts(self):
        """isError with multiple text parts joins them in the error field."""
        server = _make_server()

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "Error: table not found"},
                    {"type": "text", "text": "Hint: check table name spelling"},
                ],
                "isError": True,
            },
        }

        _attach_mock_process(server, response)

        result = server.call_tool("read_query", {"query": "SELECT * FROM typo"})

        assert result.success is False
        assert "Error: table not found" in result.error
        assert "Hint: check table name spelling" in result.error


class TestStartCommandNotFound:
    """Test start() with command not found returns False (Requirement 3.4)."""

    @patch("shutil.which", return_value=None)
    def test_start_command_not_found_returns_false(self, mock_which):
        """start() returns False when shutil.which cannot find the command."""
        server = _make_server(command="nonexistent_binary")

        result = server.start()

        assert result is False
        assert server._process is None
        mock_which.assert_called_once_with("nonexistent_binary")

    @patch("shutil.which", return_value=None)
    def test_start_command_not_found_does_not_launch_subprocess(self, mock_which):
        """start() should not attempt Popen when command is not on PATH."""
        server = _make_server(command="missing_cmd")

        with patch("subprocess.Popen") as mock_popen:
            result = server.start()

            assert result is False
            mock_popen.assert_not_called()

    @patch("shutil.which", return_value="/usr/bin/uvx")
    @patch("subprocess.Popen")
    def test_start_success_with_initialize_handshake(self, mock_popen, mock_which):
        """start() returns True when command exists and initialize handshake succeeds."""
        # Set up mock process that responds to initialize
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        init_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "test-mcp", "version": "1.0"},
            },
        }
        # Second response for notifications/initialized (which also calls _send_request)
        notif_response = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": None,
        }

        mock_proc.stdin = io.BytesIO()
        mock_proc.stdout = _mock_stdout_lines(init_response, notif_response)
        mock_proc.stderr = io.BytesIO(b"")
        mock_popen.return_value = mock_proc

        server = _make_server(command="uvx")

        result = server.start()

        assert result is True
        assert server.is_alive is True
        mock_popen.assert_called_once()
