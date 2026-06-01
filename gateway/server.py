"""
Gateway server — orchestrates platform adapters and MCP servers.

The GatewayServer is the central coordinator:
1. Loads config
2. Starts MCP server processes
3. Starts platform adapters (WeChat)
4. Routes incoming messages → MCP tools → replies
"""

import asyncio
import logging
from typing import Dict, Optional

from gateway.config import GatewayConfig, load_gateway_config
from gateway.mcp_client import McpManager
from gateway.platform_base import (
    IncomingMessage,
    PlatformAdapter,
)
from gateway.weixin import WeixinAdapter

logger = logging.getLogger(__name__)


class GatewayServer:
    """Main gateway server that ties platforms and MCP together."""

    def __init__(self, config: Optional[GatewayConfig] = None):
        self._config = config or load_gateway_config()
        self._mcp = McpManager()
        self._adapters: Dict[str, PlatformAdapter] = {}
        self._running = False

    @property
    def config(self) -> GatewayConfig:
        return self._config

    @property
    def mcp(self) -> McpManager:
        return self._mcp

    @property
    def is_running(self) -> bool:
        return self._running

    def _create_adapters(self) -> None:
        """Instantiate platform adapters based on config."""
        for name, platform_cfg in self._config.platforms.items():
            if not platform_cfg.enabled:
                logger.info("Platform '%s' is disabled, skipping", name)
                continue

            if name == "weixin":
                adapter = WeixinAdapter(platform_cfg)
                self._adapters[name] = adapter
            else:
                logger.warning("Unknown platform '%s', skipping", name)

    def _setup_mcp(self) -> Dict[str, bool]:
        """Register and start MCP servers."""
        for name, server_cfg in self._config.mcp_servers.items():
            if not server_cfg.enabled:
                logger.info("MCP server '%s' is disabled, skipping", name)
                continue
            self._mcp.add_server(server_cfg)

        return self._mcp.start_all()

    async def _handle_message(self, message: IncomingMessage) -> Optional[str]:
        """Route an incoming message through MCP tools and return a reply.

        MVP behavior: echo the message content + list available MCP tools.
        The full agent loop will be built in a later phase.
        """
        logger.info(
            "[%s] Message from %s: %s",
            message.platform,
            message.user_id,
            message.content[:50],
        )

        # MVP: Show available tools and echo
        tools = self._mcp.get_all_tools()
        if tools:
            tool_names = ", ".join(t.name for t in tools[:5])
            return (
                f"[claw-gateway] Received: {message.content}\n"
                f"Available tools: {tool_names}"
            )
        return f"[claw-gateway] Received: {message.content}\n(No MCP tools available)"

    async def start(self) -> None:
        """Start the gateway: MCP servers + platform adapters."""
        if self._running:
            return

        logger.info("Starting Claw Gateway...")

        # Start MCP servers
        mcp_results = self._setup_mcp()
        for name, ok in mcp_results.items():
            if ok:
                logger.info("MCP server '%s' started successfully", name)
            else:
                logger.error("MCP server '%s' failed to start", name)

        # Create and start platform adapters
        self._create_adapters()
        for name, adapter in self._adapters.items():
            try:
                await adapter.start(self._handle_message)
                logger.info("Platform '%s' started", name)
            except Exception as e:
                logger.error("Failed to start platform '%s': %s", name, e)

        self._running = True
        logger.info("Claw Gateway is running")

    async def stop(self) -> None:
        """Stop all adapters and MCP servers."""
        for name, adapter in self._adapters.items():
            try:
                await adapter.stop()
            except Exception as e:
                logger.error("Error stopping platform '%s': %s", name, e)

        self._mcp.stop_all()
        self._adapters.clear()
        self._running = False
        logger.info("Claw Gateway stopped")

    async def run_forever(self) -> None:
        """Start the gateway and block until interrupted."""
        await self.start()
        try:
            while self._running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.stop()

    def status(self) -> dict:
        """Return current gateway status."""
        return {
            "running": self._running,
            "platforms": {
                name: adapter.is_running()
                for name, adapter in self._adapters.items()
            },
            "mcp_servers": self._mcp.server_status,
        }
