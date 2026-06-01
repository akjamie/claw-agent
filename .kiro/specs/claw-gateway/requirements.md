# Requirements Document

## Introduction

The Claw Gateway is a lightweight messaging gateway module for the claw-agent CLI. It receives messages from messaging platform adapters (starting with WeChat/Weixin), routes them through configured MCP (Model Context Protocol) servers, and returns responses. This is the MVP plumbing layer — the agent logic will be built in a later phase.

The gateway extends the `claw` CLI as a subcommand (`claw gateway`) and integrates with the existing `~/.claw/config.json` configuration system.

## Glossary

- **Gateway**: The top-level orchestrator module that coordinates platform adapters and MCP server processes.
- **Platform_Adapter**: An abstract base class (ABC) defining the interface for messaging platform integrations.
- **Weixin_Adapter**: The concrete Platform_Adapter implementation for WeChat Official Account (公众号) messaging.
- **MCP_Client**: The component that manages subprocess-based MCP server processes using stdio transport and JSON-RPC protocol.
- **MCP_Server_Process**: A single child process running an MCP-compliant tool server (e.g., SQLite MCP, GitHub MCP).
- **Gateway_Config**: The configuration dataclass stored under the `"gateway"` key in `~/.claw/config.json`.
- **CLI_Command**: The argparse-based `claw gateway` command with subcommands (start, status, init, config).
- **Incoming_Message**: A normalized dataclass representing a message received from any platform.
- **Outgoing_Message**: A normalized dataclass representing a message to be sent through a platform.
- **Message_Handler**: An async callback function that receives an Incoming_Message and returns an optional reply string.

## Requirements

### Requirement 1: Platform Adapter Abstract Base Class

**User Story:** As a developer, I want a well-defined abstract base class for platform adapters, so that new messaging platforms can be added without modifying the gateway core.

#### Acceptance Criteria

1. THE Platform_Adapter SHALL define an abstract property `platform_name` that returns a non-empty lowercase alphanumeric string identifier (maximum 32 characters) that is unique across all registered adapters.
2. THE Platform_Adapter SHALL define an abstract method `start` that accepts a Message_Handler callback (an async callable receiving an Incoming_Message and returning an optional reply string) and begins listening for messages.
3. IF `start` is called while the adapter is already running, THEN THE Platform_Adapter SHALL remain in its current running state without raising an error or creating duplicate listeners.
4. THE Platform_Adapter SHALL define an abstract method `stop` that shuts down the adapter, releases resources, and completes within 10 seconds.
5. THE Platform_Adapter SHALL define an abstract method `send_message` that accepts an Outgoing_Message and returns a boolean value of true if the message was delivered to the platform, or false if delivery failed.
6. THE Platform_Adapter SHALL define an abstract method `is_running` that returns true if the adapter is actively listening for messages, or false otherwise.
7. THE Incoming_Message SHALL contain fields for platform name (string), user identifier (string, max 128 characters), username (string, max 64 characters), text content (string), message identifier (string, max 128 characters), timestamp (float, Unix epoch seconds), and raw platform-specific payload (dictionary).
8. THE Outgoing_Message SHALL contain fields for platform name (string), user identifier (string, max 128 characters), text content (string, max 4096 characters), optional reply-to message identifier (string or null), and platform-specific extras (dictionary).

### Requirement 2: WeChat (Weixin) Platform Adapter

**User Story:** As a user, I want the gateway to receive and reply to WeChat Official Account messages, so that I can interact with the system through WeChat.

#### Acceptance Criteria

1. WHEN a WeChat server verification request (GET with signature, timestamp, nonce, echostr) is received, THE Weixin_Adapter SHALL verify the signature using SHA1(sort([token, timestamp, nonce])) and return the echostr as the response body with HTTP 200 and content-type text/plain.
2. IF the signature verification fails on a GET or POST request, THEN THE Weixin_Adapter SHALL respond with HTTP 403 status and body "Forbidden".
3. WHEN a POST request with a valid signature and an XML body is received, THE Weixin_Adapter SHALL parse the XML into a message dictionary containing at minimum the fields: ToUserName, FromUserName, CreateTime, MsgType, and Content (for text messages) or equivalent event fields.
4. WHEN a text-type message is parsed, THE Weixin_Adapter SHALL construct an Incoming_Message with platform set to "weixin", user_id and username set to FromUserName, content set to Content, message_id set to MsgId, and timestamp set to CreateTime, and dispatch it to the registered Message_Handler.
5. WHEN the Message_Handler returns a non-empty reply string, THE Weixin_Adapter SHALL format it as a WeChat XML text reply (containing ToUserName, FromUserName, CreateTime, MsgType, Content) and respond with HTTP 200 and content-type application/xml.
6. WHEN the Message_Handler returns no reply (None or empty string), THE Weixin_Adapter SHALL respond with HTTP 200 and the body "success".
7. WHILE the Weixin_Adapter is running, THE Weixin_Adapter SHALL serve HTTP requests on the configured port (range 1–65535, default 8080) using a daemon background thread.
8. THE Weixin_Adapter SHALL enforce a 4.5-second timeout when waiting for the Message_Handler response.
9. IF the Message_Handler does not respond within 4.5 seconds or raises an exception, THEN THE Weixin_Adapter SHALL log the error and respond with HTTP 200 and the body "success" without crashing the server.
10. IF the XML body cannot be parsed or the parsed result is empty (no recognized fields), THEN THE Weixin_Adapter SHALL respond with HTTP 200 and the body "success" without dispatching to the handler.
11. WHEN a non-text message type (e.g., image, voice, video, event) is received, THE Weixin_Adapter SHALL acknowledge with HTTP 200 and the body "success" without dispatching to the handler.

### Requirement 3: MCP Client and Server Process Management

**User Story:** As a user, I want the gateway to manage MCP server subprocesses, so that I can access tools from configured MCP servers (SQLite, GitHub) through the gateway.

#### Acceptance Criteria

1. WHEN an MCP_Server_Process is started, THE MCP_Client SHALL launch the configured command as a subprocess with stdin/stdout pipes for JSON-RPC communication.
2. WHEN an MCP_Server_Process is started on Windows, THE MCP_Client SHALL use the CREATE_NO_WINDOW creation flag to suppress console popups.
3. WHEN an MCP_Server_Process is launched, THE MCP_Client SHALL send an "initialize" JSON-RPC request with protocol version "2024-11-05" and client info containing the client name and version, followed by a "notifications/initialized" notification.
4. IF the MCP server command is not found on the system PATH, THEN THE MCP_Client SHALL log an error and return a failure status for that server without starting the subprocess.
5. IF the initialize handshake fails or returns an error, THEN THE MCP_Client SHALL terminate the subprocess and return a failure status for that server.
6. WHEN tools are requested, THE MCP_Client SHALL send a "tools/list" JSON-RPC request and return the available tools with name, description, and input schema.
7. WHEN a tool call is requested, THE MCP_Client SHALL send a "tools/call" JSON-RPC request with the tool name and arguments, and return the result content extracted from text-type content parts in the response.
8. WHEN an MCP_Server_Process is stopped, THE MCP_Client SHALL terminate the subprocess gracefully with a 5-second timeout, escalating to kill if the process does not exit.
9. THE MCP_Client SHALL support managing multiple MCP_Server_Process instances simultaneously and provide a unified tool listing across all running servers, identifying each tool by both its name and its originating server name.
10. THE MCP_Client SHALL use thread-safe locking when sending JSON-RPC requests to prevent interleaved writes on the subprocess stdin.
11. IF a JSON-RPC request receives no response because the subprocess has exited or stdout is closed, THEN THE MCP_Client SHALL return a failure result with an error message indicating the server is unavailable.
12. IF a "tools/call" JSON-RPC response contains an "isError" flag set to true, THEN THE MCP_Client SHALL return the result as failed with the error content from the response.
13. WHEN a tool call is requested by name without specifying a server, THE MCP_Client SHALL search all running servers and invoke the tool on the first server that exposes a matching tool name.

### Requirement 4: Gateway Server Orchestration

**User Story:** As a user, I want the gateway to coordinate platform adapters and MCP servers as a single unit, so that I can start and stop the entire system with one command.

#### Acceptance Criteria

1. WHEN the Gateway is started, THE Gateway SHALL load configuration from ~/.claw/config.json, start all enabled MCP servers, and then start all enabled platform adapters, in that order.
2. WHEN a platform adapter receives a message, THE Gateway SHALL pass the IncomingMessage to the message handler and return the handler's reply string to the originating adapter.
3. WHILE no agent logic is configured (MVP mode), THE Gateway SHALL reply with the received message content followed by a list of up to 5 available MCP tool names; IF no MCP tools are available, THEN THE Gateway SHALL reply with the received message content and an indication that no tools are available.
4. WHEN the Gateway is stopped, THE Gateway SHALL stop all platform adapters first, then terminate all MCP server processes.
5. THE Gateway SHALL expose a `status` method that returns a dictionary containing a boolean `running` field, a `platforms` mapping of each registered adapter name to its running boolean, and an `mcp_servers` mapping of each registered server name to its alive boolean.
6. IF a platform adapter fails to start, THEN THE Gateway SHALL log an error message identifying the adapter and continue starting the remaining adapters without interruption.
7. IF an MCP server fails to start, THEN THE Gateway SHALL log an error message identifying the server and continue starting the remaining servers without interruption.
8. WHILE the Gateway is running, THE Gateway SHALL remain active by polling at a 1-second interval until interrupted by KeyboardInterrupt or asyncio.CancelledError.
9. IF the Gateway is already running when start is invoked, THEN THE Gateway SHALL return immediately without starting duplicate adapters or servers.

### Requirement 5: Gateway Configuration

**User Story:** As a user, I want to configure the gateway through a structured JSON config, so that I can set up platforms and MCP servers without editing code.

#### Acceptance Criteria

1. THE Gateway_Config SHALL be stored under the `"gateway"` key in `~/.claw/config.json`.
2. THE Gateway_Config SHALL contain a `host` field (string), a `port` field (integer in the range 1–65535, default 8080), a `platforms` dictionary, and an `mcp_servers` dictionary.
3. WHEN a platform entry is loaded, THE Gateway_Config SHALL parse it into a PlatformConfig with name, enabled flag (boolean, default false), and a settings dictionary containing all remaining key-value pairs from the entry.
4. WHEN an MCP server entry is loaded, THE Gateway_Config SHALL parse it into an McpServerConfig with name (string), command (string), args (list of strings, default empty), env (dictionary of string key-value pairs, default empty), and enabled flag (boolean, default true).
5. THE Gateway_Config SHALL provide a default configuration with host "0.0.0.0", port 8080, WeChat platform (disabled) with empty app_id/app_secret/token/encoding_aes_key settings, SQLite MCP server (enabled, command "uvx"), and GitHub MCP server (disabled, command "npx").
6. WHEN the configuration is saved, THE Gateway_Config SHALL serialize all fields back to JSON and write to the config file, preserving any non-gateway keys already present in the file.
7. IF the `"gateway"` key is missing from an existing config.json or the file does not exist, THEN THE Gateway_Config SHALL return a GatewayConfig with default field values (host "0.0.0.0", port 8080, empty platforms dictionary, empty mcp_servers dictionary).
8. THE Gateway_Config SHALL support round-trip serialization: loading a saved config and saving it again SHALL produce a JSON structure that, when parsed, is semantically equal to the original (identical keys, values, and nesting).
9. IF the configuration file contains malformed JSON, THEN THE Gateway_Config SHALL return an empty configuration equivalent to no gateway key being present, without raising an unhandled exception.

### Requirement 6: CLI Command Integration

**User Story:** As a user, I want to manage the gateway through `claw gateway` subcommands, so that I can start, configure, and inspect the gateway from the command line.

#### Acceptance Criteria

1. THE CLI_Command SHALL register a `gateway` subcommand under the main `claw` parser with subcommands: start, status, init, config.
2. WHEN `claw gateway start` is executed, THE CLI_Command SHALL load the gateway config from `~/.claw/config.json`, display the enabled platforms and MCP servers, and start the Gateway server blocking until terminated by SIGINT (Ctrl+C).
3. WHEN `claw gateway start --verbose` is executed, THE CLI_Command SHALL enable DEBUG-level logging output with timestamps in the format `HH:MM:SS [LEVEL] logger: message`.
4. WHEN `claw gateway status` is executed, THE CLI_Command SHALL display the current configuration listing: host, port, each platform name with its enabled/disabled state, and each MCP server name with its enabled/disabled state and command.
5. WHEN `claw gateway init` is executed and no gateway config exists, THE CLI_Command SHALL write the default gateway configuration (host 0.0.0.0, port 8080, weixin platform disabled, sqlite MCP server enabled, github MCP server disabled) to `~/.claw/config.json`.
6. WHEN `claw gateway init` is executed and a gateway config already exists and `--force` is not specified, THE CLI_Command SHALL prompt for yes/no confirmation before overwriting, and cancel the operation without modifying the file if the user declines.
7. WHEN `claw gateway config` is executed, THE CLI_Command SHALL present sequential prompts to configure: gateway port (integer between 1 and 65535), WeChat platform enable/disable and credentials (app_id, app_secret, token, encoding_aes_key), and MCP server enable/disable settings, then save the result to `~/.claw/config.json`.
8. WHEN `claw gateway` is executed without a subcommand, THE CLI_Command SHALL display the gateway status as the default action (equivalent to `claw gateway status`).
9. IF `claw gateway start` is executed and no gateway config exists in `~/.claw/config.json`, THE CLI_Command SHALL start the server with default settings and display a warning indicating no platforms are enabled.
10. WHEN `claw gateway config` is executed and the user enters a non-integer or out-of-range value (less than 1 or greater than 65535) for port, THE CLI_Command SHALL display a warning and retain the previous port value.

### Requirement 7: WeChat XML Message Parsing and Reply Building

**User Story:** As a developer, I want reliable XML parsing and reply construction for WeChat messages, so that the adapter correctly handles the WeChat protocol.

#### Acceptance Criteria

1. WHEN a valid WeChat XML message body is provided as a byte string, THE Parser SHALL extract all child element tags and their text values into a dictionary, storing an empty string for elements that have no text content.
2. IF the XML body is empty, malformed, or cannot be parsed, THEN THE Parser SHALL return an empty dictionary without raising an exception.
3. WHEN a text reply is built with from_user, to_user, and content parameters, THE Reply_Builder SHALL produce valid WeChat XML containing ToUserName, FromUserName, CreateTime (integer Unix timestamp at time of call), MsgType set to "text", and Content fields, each string value wrapped in CDATA sections.
4. WHEN the Reply_Builder output is fed back to the Parser, THE Parser SHALL successfully extract all fields into a non-empty dictionary (round-trip structural validity).
5. IF any parameter passed to the Reply_Builder contains XML-special characters (ampersand, angle brackets), THEN THE Reply_Builder SHALL preserve those characters verbatim inside CDATA sections without escaping them.

### Requirement 8: WeChat Signature Verification

**User Story:** As a developer, I want correct signature verification for WeChat callbacks, so that the adapter only processes authentic requests from WeChat servers.

#### Acceptance Criteria

1. WHEN token, signature, timestamp, and nonce are all provided and non-empty, THE Signature_Verifier SHALL compute SHA1 of the lexicographically sorted concatenation of [token, timestamp, nonce] encoded as UTF-8, produce a lowercase hexadecimal digest, and compare it to the provided signature using case-insensitive string comparison.
2. WHEN the computed hash matches the provided signature, THE Signature_Verifier SHALL return true.
3. WHEN the computed hash does not match the provided signature, THE Signature_Verifier SHALL return false.
4. IF any of token, signature, timestamp, or nonce is null, undefined, or an empty string (zero-length), THEN THE Signature_Verifier SHALL return false without computing the hash.
5. WHEN performing the signature comparison, THE Signature_Verifier SHALL use a constant-time comparison or plain equality check that does not short-circuit on the first mismatched character, to prevent timing-based side-channel attacks.

### Requirement 9: Testing

**User Story:** As a developer, I want comprehensive tests for the gateway module, so that I can verify correctness and catch regressions.

#### Acceptance Criteria

1. THE Test_Suite SHALL include unit tests for `verify_signature` covering: a valid signature (SHA1 of sorted token+timestamp+nonce matches), an invalid signature (computed hash does not match), and each parameter individually set to an empty string (token, signature, timestamp, nonce), verifying that each case returns False.
2. THE Test_Suite SHALL include unit tests for `parse_xml_message` covering: a valid text message XML (returns dict with ToUserName, FromUserName, CreateTime, MsgType, Content, MsgId), a valid event message XML with MsgType "event" and Event field, empty bytes input (returns empty dict), and non-XML input such as plain text (returns empty dict without raising an exception).
3. THE Test_Suite SHALL include unit tests for `build_text_reply` verifying that the returned string is valid XML containing ToUserName, FromUserName, CreateTime, MsgType set to "text", and Content elements with the provided from_user, to_user, and content values wrapped in CDATA sections.
4. THE Test_Suite SHALL include unit tests for `GatewayConfig` round-trip serialization verifying that `GatewayConfig.from_dict(data).to_dict()` produces a dict equal to the original input for a config containing at least one platform entry and one mcp_server entry.
5. THE Test_Suite SHALL include unit tests for `McpManager` that use a mock subprocess (patching `subprocess.Popen` stdin/stdout) to verify: `list_tools` returns `McpTool` objects parsed from a JSON-RPC response, and `call_tool` sends a `tools/call` JSON-RPC request and returns an `McpToolResult` with content extracted from the response.
6. THE Test_Suite SHALL include integration tests for `WeixinRequestHandler` HTTP handling using direct HTTP requests to a locally-bound test server, covering: GET with valid signature returns 200 and echostr, GET with invalid signature returns 403, POST with valid signature and text XML returns 200 with XML reply, and POST with invalid signature returns 403.
7. THE Test_Suite SHALL be executable via `pytest tests/` from the project root with exit code 0 when all tests pass.
