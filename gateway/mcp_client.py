"""
Lightweight MCP (Model Context Protocol) client for the gateway.

Manages subprocess-based MCP servers (stdio transport) and provides
a unified interface to call tools across all configured servers.

This uses the JSON-RPC protocol over stdin/stdout as defined by MCP spec.
"""

import json
import logging
import shutil
import subprocess
import sys
import threading
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from gateway.config import McpServerConfig

logger = logging.getLogger(__name__)


@dataclass
class McpTool:
    """A tool exposed by an MCP server."""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    server_name: str = ""


@dataclass
class McpToolResult:
    """Result from calling an MCP tool."""

    success: bool
    content: str = ""
    error: Optional[str] = None


class McpServerProcess:
    """Manages a single MCP server subprocess (stdio transport)."""

    def __init__(self, config: McpServerConfig):
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._request_id = 0
        self._lock = threading.Lock()
        self._tools: List[McpTool] = []

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> bool:
        """Start the MCP server subprocess."""
        if self.is_alive:
            return True

        # Pre-check: verify command exists on PATH before attempting Popen
        # Special case: 'python' always resolves to sys.executable
        if self.config.command == 'python':
            pass  # sys.executable is always valid
        elif not shutil.which(self.config.command):
            logger.error("MCP server command not found on PATH: %s", self.config.command)
            return False

        cmd = [self.config.command] + self.config.args
        # When the command is 'python', use sys.executable so the subprocess
        # runs in the same Python environment as claw (same venv/install).
        if cmd[0] == 'python':
            cmd[0] = sys.executable
        env = dict(os.environ)
        # Merge config env vars, but skip empty values so that env vars
        # already set in os.environ (from ~/.claw/.env loaded at startup)
        # take precedence over empty placeholders in config.
        for k, v in self.config.env.items():
            if v:  # only override when config has a non-empty value
                env[k] = v
            elif k not in env:
                env[k] = v  # set empty placeholder only if not already set

        try:
            # On Windows, use CREATE_NO_WINDOW to avoid console popups
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW

            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=creationflags,
            )
            logger.info("Started MCP server '%s': %s", self.name, " ".join(cmd))

            # Initialize the connection
            if not self._initialize():
                # Read stderr for diagnostics before stopping (non-blocking)
                try:
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        f = pool.submit(self._process.stderr.read, 2000)
                        try:
                            stderr_out = f.result(timeout=1.0)
                            if stderr_out:
                                logger.error(
                                    "MCP server '%s' stderr: %s",
                                    self.name,
                                    stderr_out.decode("utf-8", errors="replace").strip(),
                                )
                        except Exception:
                            pass
                except Exception:
                    pass
                self.stop()
                return False

            return True
        except FileNotFoundError:
            logger.error("MCP server command not found: %s", self.config.command)
            return False
        except Exception as e:
            logger.error("Failed to start MCP server '%s': %s", self.name, e)
            return False

    def stop(self):
        """Terminate the MCP server subprocess."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            except Exception:
                pass
            self._process = None
        self._tools = []
        logger.info("Stopped MCP server '%s'", self.name)

    def _send_request(self, method: str, params: Optional[dict] = None, timeout: float = 60) -> Optional[dict]:
        """Send a JSON-RPC request and read the response.

        Returns None if the process has exited or stdout is closed.
        Uses a configurable timeout (default 60s) to prevent blocking indefinitely.
        Pass a larger timeout for slow operations like initialize on cold uvx/npx starts.
        """
        if not self.is_alive:
            logger.warning("MCP server '%s' is not running, cannot send request", self.name)
            return None

        with self._lock:
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
            }
            if params:
                request["params"] = params

            try:
                line = json.dumps(request) + "\n"
                self._process.stdin.write(line.encode("utf-8"))
                self._process.stdin.flush()

                # Read response with timeout to prevent blocking forever
                response_line = self._read_line_with_timeout(timeout=timeout)
                if not response_line:
                    logger.warning(
                        "MCP server '%s' returned empty response or timed out",
                        self.name,
                    )
                    return None

                return json.loads(response_line.decode("utf-8"))
            except Exception as e:
                logger.error("MCP request failed for '%s': %s", self.name, e)
                return None

    def _read_line_with_timeout(self, timeout: float = 60) -> Optional[bytes]:
        """Read a line from stdout with a timeout. Returns None on timeout or EOF.

        Default is 60s to accommodate uvx/npx cold starts which need to
        download packages on first run. Subsequent calls are much faster.
        """
        import concurrent.futures

        def _readline():
            return self._process.stdout.readline()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_readline)
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                logger.warning("MCP server '%s' read timed out after %ds", self.name, timeout)
                return None

    def _send_notification(self, method: str, params: Optional[dict] = None) -> None:
        """Send a fire-and-forget JSON-RPC notification (no response expected).

        Notifications differ from requests: they have no 'id' field and the
        server MUST NOT send a response. Using _send_request for notifications
        causes a timeout because we'd wait for a reply that never comes.
        """
        if not self.is_alive:
            return
        with self._lock:
            notification = {"jsonrpc": "2.0", "method": method}
            if params:
                notification["params"] = params
            try:
                line = json.dumps(notification) + "\n"
                self._process.stdin.write(line.encode("utf-8"))
                self._process.stdin.flush()
            except Exception as e:
                logger.debug("MCP notification failed for '%s': %s", self.name, e)

    def _initialize(self) -> bool:
        """Send initialize request to the MCP server."""
        # Use a longer timeout for initialize — uvx/npx may need to download
        # packages on first run, which can take 30-60s.
        response = self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "claw-gateway",
                "version": "0.13.0",
            },
        }, timeout=90)

        if not response or "error" in response:
            logger.error("MCP initialize failed for '%s': %s", self.name, response)
            return False

        # Send initialized notification — fire-and-forget, no response expected.
        self._send_notification("notifications/initialized")
        return True

    def list_tools(self) -> List[McpTool]:
        """Fetch available tools from the MCP server."""
        if self._tools:
            return self._tools

        response = self._send_request("tools/list")
        if not response or "result" not in response:
            return []

        tools_data = response["result"].get("tools", [])
        self._tools = [
            McpTool(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=self.name,
            )
            for t in tools_data
        ]
        return self._tools

    def call_tool(self, tool_name: str, arguments: dict) -> McpToolResult:
        """Call a tool on this MCP server.

        Handles two failure scenarios:
        - Subprocess exited or stdout closed: returns descriptive unavailability error
        - isError flag in response: returns failure with error content from response
        """
        # Check if process is alive before attempting the call
        if not self.is_alive:
            return McpToolResult(
                success=False,
                error=f"MCP server '{self.name}' process has exited and is unavailable",
            )

        response = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

        if not response:
            # _send_request returned None — stdout closed or process exited mid-request
            return McpToolResult(
                success=False,
                error=f"MCP server '{self.name}' is unavailable (stdout closed or process exited)",
            )

        if "error" in response:
            err = response["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return McpToolResult(success=False, error=msg)

        result = response.get("result", {})
        content_parts = result.get("content", [])
        text_parts = []
        for part in content_parts:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))

        content_text = "\n".join(text_parts)
        is_error = result.get("isError", False)

        return McpToolResult(
            success=not is_error,
            content=content_text,
            error=content_text if is_error else None,
        )


class McpManager:
    """Manages multiple MCP server processes and provides unified tool access."""

    def __init__(self):
        self._servers: Dict[str, McpServerProcess] = {}

    def add_server(self, config: McpServerConfig) -> None:
        """Register an MCP server configuration."""
        self._servers[config.name] = McpServerProcess(config)

    def start_all(self) -> Dict[str, bool]:
        """Start all registered MCP servers. Returns {name: success}."""
        results = {}
        for name, server in self._servers.items():
            results[name] = server.start()
        return results

    def stop_all(self) -> None:
        """Stop all running MCP servers."""
        for server in self._servers.values():
            server.stop()

    def get_all_tools(self) -> List[McpTool]:
        """Get tools from all running MCP servers."""
        tools = []
        for server in self._servers.values():
            if server.is_alive:
                tools.extend(server.list_tools())
        return tools

    def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> McpToolResult:
        """Call a tool on a specific MCP server."""
        server = self._servers.get(server_name)
        if not server:
            return McpToolResult(success=False, error=f"Unknown server: {server_name}")
        if not server.is_alive:
            return McpToolResult(success=False, error=f"Server '{server_name}' is not running")
        return server.call_tool(tool_name, arguments)

    def find_tool(self, tool_name: str) -> Optional[McpTool]:
        """Find a tool by name across all servers."""
        for server in self._servers.values():
            if server.is_alive:
                for tool in server.list_tools():
                    if tool.name == tool_name:
                        return tool
        return None

    def call_tool_by_name(self, tool_name: str, arguments: dict) -> McpToolResult:
        """Call a tool by name without specifying a server.

        Searches all running servers for a matching tool name and invokes
        on the first server that exposes it.
        """
        tool = self.find_tool(tool_name)
        if not tool:
            return McpToolResult(success=False, error=f"Tool not found: {tool_name}")
        return self.call_tool(tool.server_name, tool_name, arguments)

    @property
    def server_status(self) -> Dict[str, bool]:
        """Return {server_name: is_alive} for all registered servers."""
        return {name: server.is_alive for name, server in self._servers.items()}
