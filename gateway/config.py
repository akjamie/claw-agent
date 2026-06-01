"""
Gateway configuration — stored in ~/.claw/config.json under the "gateway" key.

Example config.json:
{
  "model": { ... },
  "gateway": {
    "host": "0.0.0.0",
    "port": 18080,
    "platforms": {
      "weixin": {
        "enabled": true,
        "app_id": "wx...",
        "app_secret": "...",
        "token": "...",
        "encoding_aes_key": ""
      }
    },
    "mcp_servers": {
      "sqlite": {
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "./data.db"],
        "enabled": true
      },
      "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
        "enabled": false
      }
    }
  }
}
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cli.config import load_config, save_config


@dataclass
class McpServerConfig:
    """Configuration for a single MCP server."""

    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "McpServerConfig":
        return cls(
            name=name,
            command=data.get("command", ""),
            args=data.get("args", []),
            env=data.get("env", {}),
            enabled=data.get("enabled", True),
        )


@dataclass
class PlatformConfig:
    """Configuration for a single platform adapter."""

    name: str
    enabled: bool = False
    settings: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.settings)
        d["enabled"] = self.enabled
        return d

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "PlatformConfig":
        if not isinstance(data, dict):
            return cls(name=name, enabled=False, settings={})
        # Work on a copy to avoid mutating the caller's dict
        data_copy = dict(data)
        enabled = data_copy.pop("enabled", False)
        return cls(name=name, enabled=enabled, settings=data_copy)


@dataclass
class GatewayConfig:
    """Top-level gateway configuration."""

    host: str = "0.0.0.0"
    port: int = 18080
    platforms: Dict[str, PlatformConfig] = field(default_factory=dict)
    mcp_servers: Dict[str, McpServerConfig] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "platforms": {k: v.to_dict() for k, v in self.platforms.items()},
            "mcp_servers": {k: v.to_dict() for k, v in self.mcp_servers.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GatewayConfig":
        if not isinstance(data, dict):
            return cls()

        platforms = {}
        for name, pdata in data.get("platforms", {}).items():
            platforms[name] = PlatformConfig.from_dict(name, pdata)

        mcp_servers = {}
        for name, sdata in data.get("mcp_servers", {}).items():
            mcp_servers[name] = McpServerConfig.from_dict(name, sdata)

        # Port validation: clamp to default 18080 if out of valid range 1–65535
        port = data.get("port", 18080)
        if not isinstance(port, int) or port < 1 or port > 65535:
            port = 18080

        return cls(
            host=data.get("host", "0.0.0.0"),
            port=port,
            platforms=platforms,
            mcp_servers=mcp_servers,
        )


def load_gateway_config() -> GatewayConfig:
    """Load gateway config from ~/.claw/config.json."""
    try:
        cfg = load_config()
        gw_data = cfg.get("gateway", {})
        return GatewayConfig.from_dict(gw_data)
    except Exception:
        # Malformed data or unexpected structure — return default config
        return GatewayConfig()


def save_gateway_config(gw_config: GatewayConfig) -> bool:
    """Save gateway config back to ~/.claw/config.json."""
    cfg = load_config()
    cfg["gateway"] = gw_config.to_dict()
    return save_config(cfg)


def get_default_gateway_config() -> GatewayConfig:
    """Return a sensible default config with WeChat platform and common MCP servers."""
    return GatewayConfig(
        host="0.0.0.0",
        port=18080,
        platforms={
            "weixin": PlatformConfig(
                name="weixin",
                enabled=False,
                settings={
                    "app_id": "",
                    "app_secret": "",
                    "token": "",
                    "encoding_aes_key": "",
                },
            ),
        },
        mcp_servers={
            "sqlite": McpServerConfig(
                name="sqlite",
                command="uvx",
                args=["mcp-server-sqlite", "--db-path", "./claw_data.db"],
                enabled=True,
            ),
            "github": McpServerConfig(
                name="github",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-github"],
                env={"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
                enabled=False,
            ),
            "tavily": McpServerConfig(
                name="tavily",
                command="uvx",
                args=["mcp-tavily"],
                env={"TAVILY_API_KEY": ""},
                enabled=False,
            ),
            "weather": McpServerConfig(
                name="weather",
                command="uvx",
                args=["openmeteo-mcp-server"],
                env={},
                enabled=True,
            ),
        },
    )
