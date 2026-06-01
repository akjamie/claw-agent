"""Unit tests for GatewayConfig serialization — round-trip, defaults, and validation."""

from unittest.mock import patch

from gateway.config import GatewayConfig, load_gateway_config


class TestConfigRoundTrip:
    """GatewayConfig.from_dict(data).to_dict() round-trip equality."""

    def test_full_config_round_trip(self, gateway_config_dict):
        """Round-trip with a full config dict preserves all values."""
        result = GatewayConfig.from_dict(gateway_config_dict).to_dict()
        assert result == gateway_config_dict

    def test_round_trip_multiple_servers(self, gateway_config_dict):
        """Round-trip preserves multiple MCP server entries."""
        gateway_config_dict["mcp_servers"]["github"] = {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxx"},
            "enabled": False,
        }
        result = GatewayConfig.from_dict(gateway_config_dict).to_dict()
        assert result == gateway_config_dict


class TestDefaultConfig:
    """Default config values when gateway key is missing."""

    def test_empty_dict_returns_defaults(self):
        """from_dict({}) produces default host, port, empty platforms/servers."""
        cfg = GatewayConfig.from_dict({})
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 18080
        assert cfg.platforms == {}
        assert cfg.mcp_servers == {}

    def test_missing_gateway_key_in_load(self):
        """load_gateway_config returns defaults when 'gateway' key is absent."""
        with patch("gateway.config.load_config", return_value={}):
            cfg = load_gateway_config()
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 18080
        assert cfg.platforms == {}
        assert cfg.mcp_servers == {}


class TestMalformedJsonHandling:
    """Malformed JSON handling returns default config without raising."""

    def test_load_gateway_config_on_json_error(self):
        """load_gateway_config returns default config when load_config raises."""
        with patch(
            "gateway.config.load_config", side_effect=Exception("bad JSON")
        ):
            cfg = load_gateway_config()
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 18080
        assert cfg.platforms == {}
        assert cfg.mcp_servers == {}

    def test_from_dict_with_non_dict_input(self):
        """from_dict with non-dict input returns default config."""
        cfg = GatewayConfig.from_dict(None)  # type: ignore
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 18080


class TestPortValidation:
    """Port validation clamps out-of-range values to default 18080."""

    def test_port_zero_clamps_to_default(self):
        cfg = GatewayConfig.from_dict({"port": 0})
        assert cfg.port == 18080

    def test_port_negative_clamps_to_default(self):
        cfg = GatewayConfig.from_dict({"port": -1})
        assert cfg.port == 18080

    def test_port_above_max_clamps_to_default(self):
        cfg = GatewayConfig.from_dict({"port": 65536})
        assert cfg.port == 18080

    def test_port_non_integer_clamps_to_default(self):
        cfg = GatewayConfig.from_dict({"port": "not_a_number"})
        assert cfg.port == 18080

    def test_port_at_lower_bound_accepted(self):
        cfg = GatewayConfig.from_dict({"port": 1})
        assert cfg.port == 1

    def test_port_at_upper_bound_accepted(self):
        cfg = GatewayConfig.from_dict({"port": 65535})
        assert cfg.port == 65535
