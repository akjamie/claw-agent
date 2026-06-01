"""Unit tests for McpServerProcess.call_tool — isError handling and subprocess-exit scenarios.

Validates Requirements 3.11 and 3.12:
- 3.11: Closed stdout / exited process returns failure result with descriptive error
- 3.12: isError flag in JSON-RPC result returns McpToolResult(success=False, error=content)
"""

import io
import json
from unittest.mock import MagicMock

from gateway.config import McpServerConfig
from gateway.mcp_client import McpServerProcess, McpToolResult


def _make_server_process(name: str = "test-server") -> McpServerProcess:
    """Create an McpServerProcess with a mock config."""
    config = McpServerConfig(
        name=name,
        command="echo",
        args=[],
        env={},
        enabled=True,
    )
    return McpServerProcess(config)


def _mock_stdout_with_response(response: dict) -> io.BytesIO:
    """Create a BytesIO that returns a JSON-RPC response line."""
    line = json.dumps(response).encode("utf-8") + b"\n"
    return io.BytesIO(line)


class TestCallToolIsError:
    """Tests for isError flag handling in call_tool (Requirement 3.12)."""

    def test_is_error_true_returns_failure_result(self):
        """When isError is true in the result, call_tool returns McpToolResult with success=False."""
        server = _make_server_process()

        # Simulate a running process with a response containing isError: true
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "Something went wrong"}],
                "isError": True,
            },
        }

        mock_process = MagicMock()
        mock_process.poll.return_value = None  # process is alive
        mock_process.stdin = io.BytesIO()
        mock_process.stdout = _mock_stdout_with_response(response)
        server._process = mock_process

        result = server.call_tool("some_tool", {"arg": "value"})

        assert isinstance(result, McpToolResult)
        assert result.success is False
        assert result.error == "Something went wrong"
        assert result.content == "Something went wrong"

    def test_is_error_false_returns_success_result(self):
        """When isError is false (or absent), call_tool returns McpToolResult with success=True."""
        server = _make_server_process()

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "All good"}],
                "isError": False,
            },
        }

        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.stdin = io.BytesIO()
        mock_process.stdout = _mock_stdout_with_response(response)
        server._process = mock_process

        result = server.call_tool("some_tool", {})

        assert result.success is True
        assert result.content == "All good"
        assert result.error is None

    def test_is_error_absent_defaults_to_success(self):
        """When isError key is absent from result, defaults to success=True."""
        server = _make_server_process()

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "Result data"}],
            },
        }

        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.stdin = io.BytesIO()
        mock_process.stdout = _mock_stdout_with_response(response)
        server._process = mock_process

        result = server.call_tool("some_tool", {})

        assert result.success is True
        assert result.content == "Result data"
        assert result.error is None

    def test_is_error_with_multiple_content_parts(self):
        """When isError is true with multiple text parts, error contains all parts joined."""
        server = _make_server_process()

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "Error line 1"},
                    {"type": "text", "text": "Error line 2"},
                ],
                "isError": True,
            },
        }

        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.stdin = io.BytesIO()
        mock_process.stdout = _mock_stdout_with_response(response)
        server._process = mock_process

        result = server.call_tool("some_tool", {})

        assert result.success is False
        assert result.error == "Error line 1\nError line 2"
        assert result.content == "Error line 1\nError line 2"


class TestCallToolSubprocessExit:
    """Tests for subprocess-exit / closed stdout scenarios (Requirement 3.11)."""

    def test_exited_process_returns_failure_with_descriptive_error(self):
        """When process has exited (poll returns non-None), call_tool returns failure."""
        server = _make_server_process()

        mock_process = MagicMock()
        mock_process.poll.return_value = 1  # process exited with code 1
        server._process = mock_process

        result = server.call_tool("some_tool", {"arg": "value"})

        assert isinstance(result, McpToolResult)
        assert result.success is False
        assert "unavailable" in result.error.lower() or "exited" in result.error.lower()

    def test_no_process_returns_failure(self):
        """When _process is None (never started), call_tool returns failure."""
        server = _make_server_process()
        # _process is None by default

        result = server.call_tool("some_tool", {})

        assert result.success is False
        assert result.error is not None
        assert len(result.error) > 0

    def test_closed_stdout_returns_failure_with_descriptive_error(self):
        """When stdout is closed (readline returns empty), call_tool returns failure."""
        server = _make_server_process()

        mock_process = MagicMock()
        mock_process.poll.return_value = None  # process appears alive
        mock_process.stdin = io.BytesIO()
        # stdout returns empty bytes (closed pipe)
        mock_process.stdout = io.BytesIO(b"")
        server._process = mock_process

        result = server.call_tool("some_tool", {})

        assert result.success is False
        assert result.error is not None
        assert "unavailable" in result.error.lower() or "closed" in result.error.lower() or "exited" in result.error.lower()

    def test_error_message_includes_server_name(self):
        """Failure error messages should include the server name for debugging."""
        server = _make_server_process(name="my-sqlite-server")

        mock_process = MagicMock()
        mock_process.poll.return_value = 0  # process exited
        server._process = mock_process

        result = server.call_tool("query", {})

        assert "my-sqlite-server" in result.error
