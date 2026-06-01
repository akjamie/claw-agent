"""
Claw Gateway — lightweight messaging gateway.

Receives messages from platform adapters (e.g. WeChat via iLink),
routes them through configured MCP servers, and returns responses.
"""

__all__ = ["GatewayServer", "GatewayConfig", "WeixinAdapter", "qr_login"]

from gateway.server import GatewayServer
from gateway.config import GatewayConfig
from gateway.weixin import WeixinAdapter, qr_login
