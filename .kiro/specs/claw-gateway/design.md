# Technical Design Document

## Overview

The Claw Gateway is a lightweight messaging gateway module that extends the `claw` CLI. It provides a platform adapter abstraction for receiving messages (starting with WeChat/Weixin), an MCP client for managing tool-server subprocesses, and a server orchestrator that ties them together. This is the MVP plumbing layer — agent logic will be added in a later phase.

The design prioritizes:
- **Windows-first**: All subprocess handling uses `CREATE_NO_WINDOW`, stdlib HTTP server (no Unix-only deps)
- **Minimal dependencies**: Uses only stdlib + existing project deps (no new packages required)
- **Extensibility**: Platform ABC allows adding new adapters without modifying core

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         claw CLI                                 │
│  main.py → gateway_cmd.py (argparse: start/status/init/config) │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    GatewayServer (server.py)                     │
│  - Orchestrates adapters + MCP                                  │
│  - Routes IncomingMessage → handler → reply                     │
│  - asyncio event loop                                           │
└──────────┬──────────────────────────────────┬───────────────────┘
           │                                  │
           ▼                                  ▼
┌─────────────────────────┐    ┌──────────────────────────────────┐
│  Platform Adapters       │    │  MCP Client (mcp_client.py)      │
│  ┌───────────────────┐  │    │  ┌────────────────────────────┐  │
│  │ PlatformAdapter   │  │    │  │ McpManager                 │  │
│  │ (ABC)             │  │    │  │  - add_server()            │  │
│  └───────┬───────────┘  │    │  │  - start_all()             │  │
│          │               │    │  │  - get_all_tools()         │  │
│          ▼               │    │  │  - call_tool()             │  │
│  ┌───────────────────┐  │    │  └────────────┬───────────────┘  │
│  │ WeixinAdapter     │  │    │               │                   │
│  │ (weixin.py)       │  │    │               ▼                   │
│  │  - HTTPServer     │  │    │  ┌────────────────────────────┐  │
│  │  - XML parse/build│  │    │  │ McpServerProcess           │  │
│  │  - Signature      │  │    │  │  - subprocess.Popen        │  │
│  │    verification   │  │    │  │  - JSON-RPC over stdio     │  │
│  └───────────────────┘  │    │  │  - initialize/list/call    │  │
│                          │    │  └────────────────────────────┘  │
└─────────────────────────┘    └──────────────────────────────────┘
           │                                  │
           ▼                                  ▼
    WeChat MP Server                  MCP Servers (subprocess)
    (HTTP webhook)                    - uvx mcp-server-sqlite
                                      - npx @modelcontextprotocol/server-github
```

## Components and Interfaces

### 1. Configuration Layer (`cli/gateway/config.py`)

**Responsibilities:** Load, save, and validate gateway configuration from `~/.claw/config.json`.

**Public Interface:**

```python
def load_gateway_config() -> GatewayConfig:
    """Load gateway config from ~/.claw/config.json 'gateway' key."""

def save_gateway_config(gw_config: GatewayConfig) -> bool:
    """Save gateway config, preserving non-gateway keys in config.json."""

def get_default_gateway_config() -> GatewayConfig:
    """Return default config: weixin disabled, sqlite enabled, github disabled."""
```

### 2. Platform Adapter ABC (`cli/gateway/platform_base.py`)

**Responsibilities:** Define the contract for all messaging platform integrations.

**Public Interface:**

```python
class PlatformAdapter(ABC):
    @property
    @abstractmethod
    def platform_name(self) -> str: ...

    @abstractmethod
    async def start(self, handler: MessageHandler) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_message(self, message: OutgoingMessage) -> bool: ...

    @abstractmethod
    def is_running(self) -> bool: ...
```

**MessageHandler type:** `Callable[[IncomingMessage], Coroutine[Any, Any, Optional[str]]]`

### 3. WeChat Adapter (`cli/gateway/weixin.py`)

**Responsibilities:** Handle WeChat Official Account webhook callbacks, verify signatures, parse/build XML messages.

**Public Interface (module-level functions, independently testable):**

```python
def verify_signature(token: str, signature: str, timestamp: str, nonce: str) -> bool:
    """SHA1(sort([token, timestamp, nonce])) == signature"""

def parse_xml_message(xml_body: bytes) -> dict:
    """XML bytes → {tag: text} dictionary. Returns {} on parse failure."""

def build_text_reply(from_user: str, to_user: str, content: str) -> str:
    """Construct WeChat XML text reply with CDATA sections."""
```

**Class Interface:**

```python
class WeixinAdapter(PlatformAdapter):
    def __init__(self, config: PlatformConfig): ...
    # Implements all PlatformAdapter abstract methods
    # Uses HTTPServer in daemon thread for webhook handling
```

**Threading Model:**

```
Main asyncio loop (GatewayServer)
    └── daemon Thread: HTTPServer.serve_forever()
            └── WeixinRequestHandler.do_POST()
                    └── asyncio.run_coroutine_threadsafe(handler, loop)
                            → future.result(timeout=4.5)
```

### 4. MCP Client (`cli/gateway/mcp_client.py`)

**Responsibilities:** Manage MCP server subprocesses, send JSON-RPC requests, provide unified tool access.

**Public Interface:**

```python
class McpServerProcess:
    def __init__(self, config: McpServerConfig): ...
    def start(self) -> bool: ...
    def stop(self) -> None: ...
    def list_tools(self) -> List[McpTool]: ...
    def call_tool(self, tool_name: str, arguments: dict) -> McpToolResult: ...
    @property
    def is_alive(self) -> bool: ...

class McpManager:
    def add_server(self, config: McpServerConfig) -> None: ...
    def start_all(self) -> Dict[str, bool]: ...
    def stop_all(self) -> None: ...
    def get_all_tools(self) -> List[McpTool]: ...
    def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> McpToolResult: ...
    def find_tool(self, tool_name: str) -> Optional[McpTool]: ...
    @property
    def server_status(self) -> Dict[str, bool]: ...
```

**JSON-RPC Protocol (MCP stdio transport):**

| Phase | Method | Direction |
|-------|--------|-----------|
| Init | `initialize` | client → server |
| Init | `notifications/initialized` | client → server |
| Discovery | `tools/list` | client → server |
| Execution | `tools/call` | client → server |

Wire format: Newline-delimited JSON (`\n` terminated lines on stdin/stdout).

### 5. Gateway Server (`cli/gateway/server.py`)

**Responsibilities:** Orchestrate platform adapters and MCP servers as a single unit.

**Public Interface:**

```python
class GatewayServer:
    def __init__(self, config: Optional[GatewayConfig] = None): ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def run_forever(self) -> None: ...
    def status(self) -> dict: ...
    @property
    def config(self) -> GatewayConfig: ...
    @property
    def mcp(self) -> McpManager: ...
    @property
    def is_running(self) -> bool: ...
```

**Lifecycle:**

```
GatewayServer.__init__(config)
    → start()
        ├── _setup_mcp() → start enabled MCP servers
        ├── _create_adapters() → instantiate enabled platform adapters
        └── adapter.start(self._handle_message)
    → run_forever()
        └── asyncio.sleep(1) loop until KeyboardInterrupt
    → stop()
        ├── adapter.stop() for each adapter
        └── mcp.stop_all()
```

### 6. CLI Integration (`cli/gateway_cmd.py`)

**Responsibilities:** Provide `claw gateway` command with subcommands for managing the gateway.

**Public Interface:**

```python
def register_gateway_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register 'gateway' command and its subcommands with argparse."""

def run_gateway_command(args: argparse.Namespace) -> bool:
    """Dispatch gateway subcommands. Returns True on success."""
```

**Command Tree:**

```
claw gateway
├── start [--verbose/-v]     → Start gateway, block until Ctrl+C
├── status                   → Show config summary (default action)
├── init [--force/-f]        → Write default config
└── config                   → Interactive configuration wizard
```

## Data Models

### IncomingMessage

```python
@dataclass
class IncomingMessage:
    platform: str           # e.g. "weixin"
    user_id: str            # Platform user identifier (max 128 chars)
    username: str           # Display name (max 64 chars)
    content: str            # Text content of the message
    message_id: str         # Platform message ID (max 128 chars)
    timestamp: float        # Unix epoch seconds (default: time.time())
    raw: dict               # Original platform-specific payload
```

### OutgoingMessage

```python
@dataclass
class OutgoingMessage:
    platform: str           # Target platform
    user_id: str            # Recipient user ID (max 128 chars)
    content: str            # Text content (max 4096 chars)
    reply_to: Optional[str] # message_id being replied to
    extra: dict             # Platform-specific extras (media, buttons)
```

### McpTool

```python
@dataclass
class McpTool:
    name: str               # Tool name from MCP server
    description: str        # Human-readable description
    input_schema: dict      # JSON Schema for tool arguments
    server_name: str        # Which MCP server exposes this tool
```

### McpToolResult

```python
@dataclass
class McpToolResult:
    success: bool           # True if tool executed without error
    content: str            # Concatenated text content from response
    error: Optional[str]    # Error message if success=False
```

### GatewayConfig

```python
@dataclass
class GatewayConfig:
    host: str = "0.0.0.0"
    port: int = 8080        # Range: 1–65535
    platforms: Dict[str, PlatformConfig]
    mcp_servers: Dict[str, McpServerConfig]
```

### PlatformConfig

```python
@dataclass
class PlatformConfig:
    name: str
    enabled: bool = False
    settings: Dict[str, Any]  # Platform-specific (app_id, token, etc.)
```

### McpServerConfig

```python
@dataclass
class McpServerConfig:
    name: str
    command: str            # Executable (e.g. "uvx", "npx")
    args: List[str]         # Command arguments
    env: Dict[str, str]     # Extra environment variables
    enabled: bool = True
```

### Configuration JSON Schema

```json
{
  "gateway": {
    "host": "0.0.0.0",
    "port": 8080,
    "platforms": {
      "weixin": {
        "enabled": true,
        "app_id": "wx1234567890",
        "app_secret": "secret_here",
        "token": "my_verify_token",
        "encoding_aes_key": ""
      }
    },
    "mcp_servers": {
      "sqlite": {
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "./claw_data.db"],
        "env": {},
        "enabled": true
      },
      "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxx"},
        "enabled": false
      }
    }
  }
}
```

## Error Handling

### WeChat Adapter Errors

| Scenario | Behavior |
|----------|----------|
| Signature verification fails (GET/POST) | HTTP 403 "Forbidden" |
| XML parse failure | HTTP 200 "success" (no dispatch) |
| Non-text message type | HTTP 200 "success" (no dispatch) |
| Handler timeout (>4.5s) | Log error, HTTP 200 "success" |
| Handler raises exception | Log error, HTTP 200 "success" |
| Empty POST body | HTTP 200 "success" |

Rationale: WeChat requires HTTP 200 for all acknowledged messages. Returning non-200 causes WeChat to retry, which is undesirable for parse failures.

### MCP Client Errors

| Scenario | Behavior |
|----------|----------|
| Command not found (`FileNotFoundError`) | Log error, `start()` returns False |
| Initialize handshake fails | Terminate subprocess, `start()` returns False |
| Subprocess exits during request | Return `McpToolResult(success=False, error="...")` |
| `isError` flag in response | Return `McpToolResult(success=False, error=content)` |
| Stdout closed / no response | Return `McpToolResult(success=False, error="No response")` |
| JSON decode error on response | Log error, return None from `_send_request` |

### Gateway Server Errors

| Scenario | Behavior |
|----------|----------|
| Platform adapter fails to start | Log error, continue with remaining adapters |
| MCP server fails to start | Log error, continue with remaining servers |
| `start()` called while running | Return immediately (idempotent) |
| `KeyboardInterrupt` during `run_forever` | Call `stop()`, exit cleanly |

### CLI Errors

| Scenario | Behavior |
|----------|----------|
| Invalid port input in config wizard | Warning message, keep previous value |
| Config save failure | Print error, return False |
| No platforms enabled on start | Warning message, start anyway |

## Correctness Properties

### Property 1: Signature Verification Correctness
For any token T, timestamp TS, and nonce N, `verify_signature(T, SHA1(sort([T, TS, N])), TS, N)` SHALL return True.

**Validates: Requirements 8.1, 8.2**

### Property 2: XML Round-Trip
For any `from_user`, `to_user`, `content` strings, `parse_xml_message(build_text_reply(from_user, to_user, content).encode())` SHALL return a non-empty dict containing those values.

**Validates: Requirements 7.3, 7.4**

### Property 3: Config Round-Trip
For any valid config dict D, `GatewayConfig.from_dict(D).to_dict()` SHALL produce a dict semantically equal to D.

**Validates: Requirements 5.8**

### Property 4: Idempotent Start
Calling `GatewayServer.start()` multiple times SHALL NOT create duplicate adapters or MCP server processes.

**Validates: Requirements 4.9**

### Property 5: Graceful Shutdown
After `GatewayServer.stop()` completes, all adapter `is_running()` SHALL return False and all MCP subprocess `is_alive` SHALL return False.

**Validates: Requirements 4.4**

### Property 6: Thread Safety
Concurrent calls to `McpServerProcess._send_request()` from different threads SHALL NOT interleave JSON-RPC messages on stdin.

**Validates: Requirements 3.10**

### Property 7: Timeout Guarantee
The WeChat adapter SHALL respond to every POST request within 5 seconds (4.5s handler timeout + overhead).

**Validates: Requirements 2.8**

### Property 8: No Crash on Bad Input
Malformed XML, empty bodies, and unexpected message types SHALL NOT raise unhandled exceptions in the HTTP handler.

**Validates: Requirements 2.9, 2.10**

## File Structure

```
cli/
├── gateway/
│   ├── __init__.py          # Package exports: GatewayServer, GatewayConfig
│   ├── config.py            # GatewayConfig, PlatformConfig, McpServerConfig
│   ├── platform_base.py     # PlatformAdapter ABC, IncomingMessage, OutgoingMessage
│   ├── weixin.py            # WeixinAdapter, verify_signature, parse_xml, build_reply
│   ├── mcp_client.py        # McpServerProcess, McpManager, McpTool, McpToolResult
│   └── server.py            # GatewayServer orchestrator
├── gateway_cmd.py           # CLI: claw gateway {start,status,init,config}
└── main.py                  # Updated: registers gateway subcommand

tests/
├── __init__.py
├── conftest.py              # Shared fixtures
├── test_weixin_signature.py # Unit: verify_signature
├── test_weixin_xml.py       # Unit: parse_xml_message, build_text_reply
├── test_gateway_config.py   # Unit: GatewayConfig round-trip serialization
├── test_mcp_client.py       # Unit: McpServerProcess with mock subprocess
└── test_weixin_adapter.py   # Integration: WeixinAdapter HTTP handling
```

## Dependencies

No new dependencies required. The gateway uses only stdlib modules:
- `http.server` — WeChat webhook HTTP server
- `xml.etree.ElementTree` — XML parsing
- `hashlib` — SHA1 signature verification
- `subprocess` — MCP server process management
- `json` — JSON-RPC protocol
- `asyncio` — async orchestration
- `threading` — HTTP server background thread
- `logging` — structured logging
- `dataclasses` — config/message data models

Existing project deps used:
- `cli.config` — `load_config()`, `save_config()`
- `cli.interactive_ui` — CLI prompts and output formatting

## Windows Considerations

1. **Subprocess creation**: `CREATE_NO_WINDOW` flag prevents console popups for MCP servers
2. **HTTP server**: `http.server.HTTPServer` works identically on Windows
3. **Threading**: Daemon threads for HTTP server (auto-cleanup on process exit)
4. **Path handling**: MCP server commands (`uvx`, `npx`) resolve via Windows PATH
5. **UTF-8**: WeChat XML is UTF-8; Python's `xml.etree` handles this natively
6. **Signal handling**: `KeyboardInterrupt` (Ctrl+C) works in Windows console for graceful shutdown

## Testing Strategy

### Unit Tests

| Test File | Covers | Approach |
|-----------|--------|----------|
| `test_weixin_signature.py` | `verify_signature()` | Direct function calls with known SHA1 values |
| `test_weixin_xml.py` | `parse_xml_message()`, `build_text_reply()` | Known XML inputs/outputs, malformed input |
| `test_gateway_config.py` | `GatewayConfig.from_dict()/.to_dict()` | Round-trip equality, defaults, edge cases |
| `test_mcp_client.py` | `McpServerProcess`, `McpManager` | Mock `subprocess.Popen` stdin/stdout |

### Integration Tests

| Test File | Covers | Approach |
|-----------|--------|----------|
| `test_weixin_adapter.py` | Full HTTP request/response cycle | Start real `HTTPServer` on localhost, send requests via `urllib` |

### Test Fixtures (`conftest.py`)

- `weixin_token`: Fixed token string for signature tests
- `sample_text_xml`: Valid WeChat text message XML bytes
- `sample_event_xml`: Valid WeChat event XML bytes
- `gateway_config_dict`: Sample config dict for serialization tests

### Execution

```sh
pytest tests/ -v
```

All tests use pytest and require only the `dev` extra dependencies (pytest, pytest-asyncio).
