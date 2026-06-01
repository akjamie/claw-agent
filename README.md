# Claw Agent

A self-improving AI agent with a messaging gateway — connects to WeChat (and more platforms coming) and routes messages through MCP tool servers.

## Quick Start

```sh
# Install
pip install -e ".[dev]"

# Connect WeChat (scan QR code)
claw gateway add weixin

# Start the gateway
claw gateway start
```

## Commands

```
claw version                    Show version
claw models                     Configure AI model/provider

claw gateway status             Show gateway configuration
claw gateway add [platform]     Add a messaging platform
claw gateway remove <platform>  Remove a platform
claw gateway mcp                Configure MCP tool servers
claw gateway start [--verbose]  Start the gateway
```

## Gateway

The gateway receives messages from messaging platforms and routes them through MCP (Model Context Protocol) servers for tool access.

### Supported Platforms

| Platform | Auth Method | Status |
|----------|-------------|--------|
| WeChat / Weixin | QR code scan (iLink Bot) | ✓ |
| Telegram | Bot token | Planned |
| Feishu / Lark | App credentials | Planned |

### MCP Servers

The gateway can manage MCP tool servers as subprocesses:

- **SQLite** — `uvx mcp-server-sqlite` (database queries)
- **GitHub** — `npx @modelcontextprotocol/server-github` (repo operations)

Configure with `claw gateway mcp`.

## Project Structure

```
cli/                    CLI package (entry point: claw)
├── main.py             argparse dispatcher
├── config.py           ~/.claw/config.json management
├── models_cmd.py       claw models command
├── gateway_cmd.py      claw gateway command
└── interactive_ui.py   Terminal menus and prompts

gateway/                Gateway package
├── config.py           Gateway configuration
├── platform_base.py    Platform adapter ABC
├── weixin.py           WeChat iLink adapter
├── mcp_client.py       MCP server process manager
└── server.py           Gateway orchestrator

tests/                  pytest test suite
```

## Development

```sh
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check cli/ gateway/
```

## Configuration

Non-sensitive settings live in `~/.claw/config.json`:

```json
{
  "gateway": {
    "host": "0.0.0.0",
    "port": 18080,
    "platforms": {
      "weixin": { "enabled": true }
    },
    "mcp_servers": {
      "sqlite": {
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "./claw_data.db"],
        "enabled": true
      }
    }
  }
}
```

Sensitive credentials are stored in `.env` (either `{cwd}/.env` or `~/.claw/.env`):

```env
WEIXIN_TOKEN=your_bot_token_here
WEIXIN_ACCOUNT_ID=your_account_id
WEIXIN_BASE_URL=https://ilinkai.weixin.qq.com
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxx
```

The `.env` file is auto-created by `claw gateway add` and never committed to git.

## License

MIT
