# Implementation Plan: Claw Gateway

## Overview

The claw-gateway module provides a lightweight messaging gateway for the claw-agent CLI. It receives messages from platform adapters (starting with WeChat/Weixin), routes them through configured MCP servers, and returns responses. The core implementation files already exist; this plan covers hardening, gap-filling, and comprehensive test coverage.

## Tasks

- [x] 1. Harden configuration layer and data models
  - [x] 1.1 Add port validation and malformed-JSON handling to `cli/gateway/config.py`
    - Add port range validation (1–65535) in `GatewayConfig.from_dict()` clamping to default 8080 if out of range
    - Handle malformed JSON gracefully in `load_gateway_config()` — return default config without raising
    - Ensure `PlatformConfig.from_dict` does not mutate the input dict (use `dict(data)` copy before popping `enabled`)
    - _Requirements: 5.7, 5.9, 5.8_

  - [x] 1.2 Add port input validation to `claw gateway config` wizard in `cli/gateway_cmd.py`
    - When user enters non-integer or out-of-range port (< 1 or > 65535), display warning and retain previous value
    - _Requirements: 6.10_

- [x] 2. Harden WeChat adapter and XML handling
  - [x] 2.1 Add constant-time signature comparison in `verify_signature` in `cli/gateway/weixin.py`
    - Replace plain `==` with `hmac.compare_digest()` for timing-safe comparison
    - _Requirements: 8.5_

  - [x] 2.2 Handle empty body and edge cases in `WeixinRequestHandler.do_POST`
    - Ensure empty POST body (Content-Length 0 or missing) returns HTTP 200 "success" without dispatch
    - Ensure non-text MsgType (image, voice, video, event) returns HTTP 200 "success"
    - _Requirements: 2.10, 2.11_

  - [x] 2.3 Verify XML CDATA handling preserves special characters in `build_text_reply`
    - Confirm that `&`, `<`, `>` inside CDATA sections are not escaped
    - Add inline comment documenting CDATA safety guarantee
    - _Requirements: 7.5_

- [x] 3. Harden MCP client subprocess management
  - [x] 3.1 Add `shutil.which()` pre-check in `McpServerProcess.start()` in `cli/gateway/mcp_client.py`
    - Before launching subprocess, verify command exists on PATH using `shutil.which()`
    - If not found, log error and return False without attempting Popen
    - _Requirements: 3.4_

  - [x] 3.2 Handle `isError` flag and subprocess-exit scenarios in `McpServerProcess.call_tool`
    - Ensure `isError: true` in JSON-RPC result returns `McpToolResult(success=False, error=content)`
    - Ensure closed stdout / exited process returns failure result with descriptive error
    - _Requirements: 3.11, 3.12_

  - [x] 3.3 Implement `find_tool` + call-by-name-without-server in `McpManager`
    - When `call_tool` is invoked without explicit server_name (or via a convenience method), search all servers for matching tool name and invoke on first match
    - _Requirements: 3.13_

- [x] 4. Checkpoint — Ensure core modules are consistent
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Create test infrastructure and unit tests
  - [x] 5.1 Create `tests/conftest.py` with shared fixtures
    - Define `weixin_token` fixture (fixed token string)
    - Define `sample_text_xml` fixture (valid WeChat text message XML bytes)
    - Define `sample_event_xml` fixture (valid WeChat event XML bytes)
    - Define `gateway_config_dict` fixture (sample config dict with one platform + one MCP server)
    - _Requirements: 9.7_

  - [x] 5.2 Create `tests/test_weixin_signature.py` — unit tests for `verify_signature`
    - Test valid signature (SHA1 of sorted token+timestamp+nonce matches)
    - Test invalid signature (computed hash does not match)
    - Test each parameter individually set to empty string returns False
    - Test all-empty parameters returns False
    - _Requirements: 9.1, 8.1, 8.2, 8.3, 8.4_

  - [x] 5.3 Create `tests/test_weixin_xml.py` — unit tests for XML parsing and reply building
    - Test `parse_xml_message` with valid text message XML returns dict with expected keys
    - Test `parse_xml_message` with valid event XML returns dict with MsgType "event"
    - Test `parse_xml_message` with empty bytes returns empty dict
    - Test `parse_xml_message` with non-XML input returns empty dict without exception
    - Test `build_text_reply` produces valid XML with CDATA sections and correct field values
    - Test round-trip: `parse_xml_message(build_text_reply(...).encode())` returns non-empty dict
    - _Requirements: 9.2, 9.3, 7.1, 7.2, 7.3, 7.4_

  - [x] 5.4 Create `tests/test_gateway_config.py` — unit tests for config serialization
    - Test `GatewayConfig.from_dict(data).to_dict()` round-trip equality with full config
    - Test default config values when gateway key is missing
    - Test malformed JSON handling returns default config
    - Test port validation clamps out-of-range values
    - _Requirements: 9.4, 5.7, 5.8, 5.9_

  - [x] 5.5 Create `tests/test_mcp_client.py` — unit tests for MCP client with mock subprocess
    - Mock `subprocess.Popen` stdin/stdout to simulate JSON-RPC responses
    - Test `list_tools` returns `McpTool` objects parsed from mock response
    - Test `call_tool` sends correct JSON-RPC request and returns `McpToolResult`
    - Test `call_tool` with `isError: true` returns failure result
    - Test `start()` with command not found returns False
    - _Requirements: 9.5, 3.1, 3.6, 3.7, 3.12_

- [x] 6. Create integration tests for WeChat adapter HTTP handling
  - [x] 6.1 Create `tests/test_weixin_adapter.py` — integration tests with real HTTP server
    - Start `WeixinAdapter` on a random available port with a mock handler
    - Test GET with valid signature returns HTTP 200 and echostr
    - Test GET with invalid signature returns HTTP 403
    - Test POST with valid signature and text XML returns HTTP 200 with XML reply
    - Test POST with invalid signature returns HTTP 403
    - Test POST with empty body returns HTTP 200 "success"
    - Test POST with non-text MsgType returns HTTP 200 "success"
    - Tear down server after tests
    - _Requirements: 9.6, 2.1, 2.2, 2.4, 2.5, 2.6_

- [x] 7. Checkpoint — Run full test suite
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Property-based tests for correctness properties
  - [ ]* 8.1 Write property test for signature verification correctness
    - **Property 1: Signature Verification Correctness**
    - For any token T, timestamp TS, nonce N: `verify_signature(T, SHA1(sort([T, TS, N])), TS, N)` returns True
    - **Validates: Requirements 8.1, 8.2**

  - [ ]* 8.2 Write property test for XML round-trip
    - **Property 2: XML Round-Trip**
    - For any from_user, to_user, content strings: `parse_xml_message(build_text_reply(from_user, to_user, content).encode())` returns non-empty dict containing those values
    - **Validates: Requirements 7.3, 7.4**

  - [ ]* 8.3 Write property test for config round-trip
    - **Property 3: Config Round-Trip**
    - For any valid config dict D: `GatewayConfig.from_dict(D).to_dict()` produces dict semantically equal to D
    - **Validates: Requirements 5.8**

- [x] 9. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- The core implementation files already exist (`config.py`, `platform_base.py`, `weixin.py`, `mcp_client.py`, `server.py`, `gateway_cmd.py`, `main.py`)
- Tasks focus on hardening edge cases, adding validation, and building comprehensive test coverage
- Each task references specific requirements for traceability
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- All tests use pytest and run via `pytest tests/ -v`
- Python is the implementation language (matching existing codebase)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "2.3", "3.1", "5.1"] },
    { "id": 1, "tasks": ["1.2", "2.2", "3.2", "3.3"] },
    { "id": 2, "tasks": ["5.2", "5.3", "5.4", "5.5"] },
    { "id": 3, "tasks": ["6.1"] },
    { "id": 4, "tasks": ["8.1", "8.2", "8.3"] }
  ]
}
```
