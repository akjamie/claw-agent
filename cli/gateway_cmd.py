"""
CLI command: claw gateway

Subcommands:
  claw gateway start           — Start the gateway server
  claw gateway status          — Show gateway status / config summary
  claw gateway add [platform]  — Add and authenticate a platform
  claw gateway remove <platform> — Remove a platform
  claw gateway mcp             — Configure MCP servers
"""

import asyncio
import argparse
import logging
import sys
1
from cli.interactive_ui import (
    print_header,
    print_success,
    print_error,
    print_info,
    print_warning,
    prompt_text,
    prompt_yes_no,
    prompt_choice,
)
from gateway.config import (
    GatewayConfig,
    PlatformConfig,
    load_gateway_config,
    save_gateway_config,
    get_default_gateway_config,
)


# ─── Platform registry ────────────────────────────────────────────────────────
# Each platform defines: display name, setup function, description.
# To add a new platform, just add an entry here and implement _add_<name>().

PLATFORMS = {
    "weixin": {
        "display": "WeChat / Weixin",
        "description": "Personal WeChat via iLink Bot (QR scan login)",
    },
    # Future platforms:
    # "telegram": {
    #     "display": "Telegram",
    #     "description": "Telegram Bot API (requires bot token from @BotFather)",
    # },
    # "feishu": {
    #     "display": "Feishu / Lark",
    #     "description": "Feishu Bot (requires app_id and app_secret)",
    # },
}


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


# ─── claw gateway start ──────────────────────────────────────────────────────

def _cmd_start(args: argparse.Namespace) -> bool:
    """Start the gateway server."""
    _setup_logging(verbose=args.verbose)

    config = load_gateway_config()

    enabled_platforms = [
        name for name, p in config.platforms.items() if p.enabled
    ]
    enabled_mcp = [
        name for name, s in config.mcp_servers.items() if s.enabled
    ]

    if not enabled_platforms:
        print_warning("No platforms configured.")
        print_info("Run 'claw gateway add' to set up a messaging platform.")
        print()

    print_header("Starting Claw Gateway")
    if enabled_platforms:
        print_info(f"Platforms: {', '.join(enabled_platforms)}")
    if enabled_mcp:
        print_info(f"MCP servers: {', '.join(enabled_mcp)}")
    print_info(f"Port: {config.port}")
    print()

    from gateway.server import GatewayServer

    server = GatewayServer(config)

    import signal

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _main():
            await server.start()
            stop_event = asyncio.Event()

            # On Windows, add signal handler via the loop
            if sys.platform == "win32":
                # Windows: use signal.signal since loop.add_signal_handler isn't supported
                def _sigint_handler(sig, frame):
                    stop_event.set()
                signal.signal(signal.SIGINT, _sigint_handler)
            else:
                loop.add_signal_handler(signal.SIGINT, stop_event.set)

            print_info("Gateway running. Press Ctrl+C to stop.")
            await stop_event.wait()
            print()
            print_info("Shutting down...")
            await server.stop()

        try:
            loop.run_until_complete(_main())
        except KeyboardInterrupt:
            # Fallback if signal handler didn't catch it
            loop.run_until_complete(server.stop())
        finally:
            loop.close()

    _run()
    print_info("Gateway stopped.")
    return True


# ─── claw gateway status ─────────────────────────────────────────────────────

def _cmd_status(args: argparse.Namespace) -> bool:
    """Show gateway configuration and status."""
    import os

    config = load_gateway_config()

    print_header("Gateway Status")
    print_info("Running: ✗ not running")
    print()

    # Check if anything is configured at all
    has_platforms = bool(config.platforms)
    has_mcp = bool(config.mcp_servers)

    if not has_platforms and not has_mcp:
        print_info("Not configured. Run 'claw gateway add' to get started.")
        return True

    print_info(f"Port: {config.port}")
    print()

    # Platforms
    if has_platforms:
        print_header("Platforms")
        for name, p in config.platforms.items():
            display = PLATFORMS.get(name, {}).get("display", name)
            if name == "weixin":
                token = os.environ.get("WEIXIN_TOKEN", "")
                account_id = os.environ.get("WEIXIN_ACCOUNT_ID", "")
                if p.enabled and token:
                    print_info(f"  {display}: ✓ connected")
                    if account_id:
                        print_info(f"    account: {account_id[:12]}...")
                elif p.enabled and not token:
                    print_warning(f"  {display}: ⚠ enabled but credentials missing")
                    print_info("    Run 'claw gateway add weixin' to authenticate")
                else:
                    print_info(f"  {display}: ○ disabled")
            else:
                status = "✓ connected" if p.enabled else "○ disabled"
                print_info(f"  {display}: {status}")
        print()

    # MCP Servers
    if has_mcp:
        print_header("MCP Servers")
        for name, s in config.mcp_servers.items():
            status = "✓ enabled" if s.enabled else "○ disabled"
            cmd = f"{s.command} {' '.join(s.args)}"
            print_info(f"  {name}: {status}")
            print_info(f"    {cmd}")
        print()

    # Usage hint
    enabled_platforms = [n for n, p in config.platforms.items() if p.enabled]
    if enabled_platforms:
        print_info("Run 'claw gateway start' to launch.")
    else:
        print_warning("No platforms enabled. Run 'claw gateway add' to connect one.")

    return True


# ─── claw gateway add [platform] ─────────────────────────────────────────────

def _cmd_add(args: argparse.Namespace) -> bool:
    """Add and authenticate a messaging platform."""
    platform_name = getattr(args, "platform", None)

    # If no platform specified, show selection menu
    if not platform_name:
        platform_keys = list(PLATFORMS.keys())
        choices = [
            f"{info['display']} — {info['description']}"
            for info in PLATFORMS.values()
        ]
        idx = prompt_choice("Select a platform to add:", choices)
        platform_name = platform_keys[idx]

    # Validate platform name
    if platform_name not in PLATFORMS:
        print_error(f"Unknown platform: '{platform_name}'")
        print_info(f"Available: {', '.join(PLATFORMS.keys())}")
        return False

    # Dispatch to platform-specific setup
    setup_fn = _PLATFORM_SETUP.get(platform_name)
    if not setup_fn:
        print_error(f"Platform '{platform_name}' setup not yet implemented.")
        return False

    return setup_fn()


def _add_weixin() -> bool:
    """WeChat iLink QR login flow."""
    from gateway.weixin import qr_login
    from cli.config import save_env_value, get_env_path

    print_header("Add WeChat / Weixin")
    print_info("This will display a QR code. Scan it with your WeChat app.")
    print()

    result = asyncio.run(qr_login())

    if not result:
        print_error("Login failed or timed out.")
        return False

    # Ensure ~/.claw/.env exists so credentials go there (not CWD)
    env_path = get_env_path()
    if not env_path.exists():
        env_path.touch()

    # Save sensitive credentials to ~/.claw/.env
    save_env_value("WEIXIN_TOKEN", result["token"])
    save_env_value("WEIXIN_ACCOUNT_ID", result["account_id"])
    save_env_value("WEIXIN_BASE_URL", result["base_url"])
    save_env_value("WEIXIN_USER_ID", result.get("user_id", ""))

    # Save only the enabled flag to config.json (no secrets)
    config = load_gateway_config()
    weixin_cfg = config.platforms.get("weixin")
    if not weixin_cfg:
        weixin_cfg = PlatformConfig(name="weixin", enabled=False, settings={})
        config.platforms["weixin"] = weixin_cfg

    weixin_cfg.enabled = True
    save_gateway_config(config)

    print()
    print_success("WeChat connected!")
    print_info(f"Account: {result['account_id'][:12]}...")
    print_info(f"Credentials saved to {env_path}")
    print_info("Run 'claw gateway start' to begin receiving messages.")
    return True


# Platform setup dispatch table — add new platforms here
_PLATFORM_SETUP = {
    "weixin": _add_weixin,
    # "telegram": _add_telegram,
    # "feishu": _add_feishu,
}


# ─── claw gateway remove <platform> ──────────────────────────────────────────

def _cmd_remove(args: argparse.Namespace) -> bool:
    """Remove a platform from the gateway."""
    platform_name = args.platform

    config = load_gateway_config()
    if platform_name not in config.platforms:
        print_error(f"Platform '{platform_name}' is not configured.")
        return False

    display = PLATFORMS.get(platform_name, {}).get("display", platform_name)
    if not prompt_yes_no(f"Remove {display}? This will delete its credentials.", default=False):
        print_info("Cancelled.")
        return False

    del config.platforms[platform_name]
    if save_gateway_config(config):
        print_success(f"Removed {display}.")
        return True
    else:
        print_error("Failed to save config.")
        return False


# ─── claw gateway mcp ────────────────────────────────────────────────────────

def _cmd_mcp(args: argparse.Namespace) -> bool:
    """Configure MCP servers."""
    config = load_gateway_config()

    # Always merge in any new default servers that aren't in the config yet.
    # This ensures newly added defaults (e.g. Tavily) appear even when the
    # user already has an existing config with other servers.
    default = get_default_gateway_config()
    for name, srv in default.mcp_servers.items():
        if name not in config.mcp_servers:
            config.mcp_servers[name] = srv
            print_info(f"New MCP server available: '{name}' (added to config)")

    print_header("MCP Server Configuration")

    for name, server_cfg in config.mcp_servers.items():
        enable = prompt_yes_no(
            f"Enable '{name}' ({server_cfg.command})?",
            default=server_cfg.enabled,
        )
        server_cfg.enabled = enable

        if enable and name == "sqlite":
            db_path = prompt_text("SQLite DB path", default="./claw_data.db")
            server_cfg.args = ["mcp-server-sqlite", "--db-path", db_path]
        elif enable and name == "github":
            token = prompt_text(
                "GitHub Personal Access Token",
                default=server_cfg.env.get("GITHUB_PERSONAL_ACCESS_TOKEN", ""),
            )
            server_cfg.env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
        elif enable and name == "tavily":
            api_key = prompt_text(
                "Tavily API Key",
                default=server_cfg.env.get("TAVILY_API_KEY", ""),
            )
            server_cfg.env["TAVILY_API_KEY"] = api_key
        # weather (openmeteo) needs no configuration — no API key required

    print()

    if save_gateway_config(config):
        print_success("MCP configuration saved!")
        return True
    else:
        print_error("Failed to save config.")
        return False


# ─── Parser registration ─────────────────────────────────────────────────────

def register_gateway_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'gateway' command and its subcommands with argparse."""
    gw_parser = subparsers.add_parser("gateway", help="Manage the messaging gateway")
    gw_sub = gw_parser.add_subparsers(dest="gateway_action")

    # start
    start_parser = gw_sub.add_parser("start", help="Start the gateway server")
    start_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )

    # status
    gw_sub.add_parser("status", help="Show gateway status")

    # add
    add_parser = gw_sub.add_parser("add", help="Add a messaging platform")
    add_parser.add_argument(
        "platform", nargs="?", default=None,
        help=f"Platform to add ({', '.join(PLATFORMS.keys())})",
    )

    # remove
    remove_parser = gw_sub.add_parser("remove", help="Remove a messaging platform")
    remove_parser.add_argument("platform", help="Platform to remove")

    # mcp
    gw_sub.add_parser("mcp", help="Configure MCP servers")


def run_gateway_command(args: argparse.Namespace) -> bool:
    """Dispatch gateway subcommands."""
    action = getattr(args, "gateway_action", None)

    if action == "start":
        return _cmd_start(args)
    elif action == "status":
        return _cmd_status(args)
    elif action == "add":
        return _cmd_add(args)
    elif action == "remove":
        return _cmd_remove(args)
    elif action == "mcp":
        return _cmd_mcp(args)
    else:
        # No subcommand — show status by default
        return _cmd_status(args)
