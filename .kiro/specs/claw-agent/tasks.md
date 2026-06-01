# Implementation Plan: claw-agent

## Overview

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

The plan implements the `claw chat` runtime as a new top-level package `agent/` plus a thin `cli/chat_cmd.py` that wires it into the existing argparse dispatcher in `cli/main.py`. Implementation follows the synchronous main-loop design: foundation dataclasses and SQLite schema first, then core components (LLM client, tool registry/dispatcher, persistence), then higher-level orchestration (compressor, guardrails, title generator, loop), then TUI and CLI integration. Property-based tests using Hypothesis validate all 30 correctness properties from the design and live in `tests/agent/`.

Implementation language: **Python 3.11+** (matches existing project, declared in `pyproject.toml`). No new runtime dependencies are introduced; only `hypothesis` is added to the `[dev]` extras.

## Tasks

- [ ] 1. Foundation: dependencies, package skeleton, and shared dataclasses
  - [ ] 1.1 Add `hypothesis` to `[dev]` extras and create `agent/` package skeleton
    - Edit `pyproject.toml`: append `"hypothesis>=6.128.0"` to the `[project.optional-dependencies].dev` list (no runtime deps added).
    - Create `agent/__init__.py` with placeholder public exports (will be filled as modules land).
    - Create `tests/agent/__init__.py` and `tests/agent/conftest.py` with empty fixture stubs.
    - _Requirements: 14.1, 14.2_

  - [ ] 1.2 Implement `agent/messages.py` — `Message` and `ToolCall` dataclasses
    - Define `@dataclass(frozen=True) Message` with fields `role`, `content`, `tool_call_id`, `tool_name`, `tool_arguments`, `timestamp`, `id` per design §"agent.messages".
    - Implement `to_openai`, `from_openai`, `to_db_row`, `from_db_row` with deterministic JSON via `json.dumps(..., sort_keys=True, ensure_ascii=False)`.
    - Define `@dataclass(frozen=True) ToolCall` with `id`, `name`, `arguments_json`.
    - Provide a module-level `canonical_args(args_obj_or_str) -> str` helper used by both Message encoding and Tool_Hash computation.
    - _Requirements: 19.1, 19.2, 9.1, 3.4_

  - [ ] 1.3 Implement `agent/config.py` — `AgentConfig` dataclass and loaders
    - Define `AgentConfig` dataclass with all fields and defaults from design §"agent.config".
    - Implement `load_agent_config()` that reads the `agent` object from `~/.claw/config.json` via `cli.config.load_config`, validates each field, substitutes defaults for missing/invalid keys, and writes a one-line stderr warning per offending key.
    - Implement `save_agent_config(cfg)` writing back to the same path.
    - Add unit doc-strings noting validation ranges (e.g., threshold ∈ (0,1)).
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 6.1, 7.8, 8.1, 9.8, 10.1_


- [ ] 2. Core: LLM client, tool registry, persistence, title generator
  - [ ] 2.1 Implement `agent/llm_client.py` — OpenAI-compatible streaming client
    - Implement `LLMClient` with `__init__(base_url, api_key, model, timeout=120.0)`.
    - Implement `_ToolCallAccumulator` exactly as designed (index-keyed slots, never validates JSON).
    - Implement `stream_chat` using `requests.post(..., stream=True)`, parsing SSE `data: {...}` frames; invoke `on_text_delta` per non-empty content delta; check `interrupt` between deltas; finalise partial Message on interrupt; return `StreamResult(content, tool_calls, finish_reason, prompt_tokens, completion_tokens)`.
    - Implement non-streaming `chat(messages, tools=None) -> dict` for the compressor and title generator; support `max_tokens` kwarg.
    - Define `StreamResult` dataclass and `StreamStatus` lightweight value type for `on_status` callbacks.
    - _Requirements: 5.4, 5.5, 13.1, 13.4, 13.5_

  - [ ] 2.2 Implement `agent/tool_registry.py` — MCP-to-OpenAI bridge
    - `ToolRegistry(mcp: McpManager)` with `reload_from_mcp()`, `openai_tools()`, `get(name)`, `safety_class(name)`, `set_safety_override(name, cls)`.
    - Default classification for MCP tools is `_NEVER_PARALLEL`; expose constants `_NEVER_PARALLEL`, `_PARALLEL_SAFE`, `_PATH_SCOPED`.
    - `openai_tools()` returns the OpenAI function-tool format with `parameters = mcp_tool.input_schema` passed through.
    - _Requirements: 7.1, 7.2, 7.3, 8.5_

  - [ ] 2.3 Implement `agent/persistence.py` — SQLite WAL persistence
    - `SqlitePersistence` with `SCHEMA_VERSION = 1`, `DEFAULT_PATH = ~/.claw/agent.db`.
    - On every fresh connection apply `PRAGMA journal_mode=WAL; synchronous=NORMAL; foreign_keys=ON; temp_store=MEMORY`.
    - Implement `initialize()` creating `meta`, `sessions`, `messages` tables and indexes per design §"SQLite schema"; idempotent across calls.
    - Implement `create_session(model)`, `get_session(id_prefix)` (raises `SessionNotFound` / `AmbiguousSession`), `list_sessions()`, `append_messages(session_id, messages, total_tokens)` wrapping a `BEGIN IMMEDIATE; COMMIT;` transaction with retry up to 2 (linear backoff 0.1 s, 0.3 s) raising `PersistenceFailure` on the third failure.
    - Implement `load_recent_messages(session_id, limit=500)` and `update_title(session_id, title)` with the no-op-when-non-empty UPDATE.
    - Implement `persist_summary(session_id, summary_msg)` storing the compression summary message with `tool_name='__compression_summary__'`.
    - Define `Session` dataclass and `PersistenceFailure`, `SessionNotFound`, `AmbiguousSession` exceptions.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 18.1, 18.2, 18.3, 18.4, 4.4, 10.8_

  - [ ] 2.4 Implement `agent/title_generator.py` — auxiliary title call
    - `TitleGenerator(llm)` with `generate(messages) -> Optional[str]`.
    - Trigger logic lives in the loop; this module only generates: build a short prompt asking for ≤80-char title, call `llm.chat`, strip newlines, truncate to 80 chars, return `None` on empty/error.
    - _Requirements: 4.1, 4.2, 4.3_

- [ ] 3. Checkpoint — foundation and core components in place
  - Ensure all tests pass, ask the user if questions arise.


- [ ] 4. Higher-level: dispatcher, guardrails, compressor
  - [ ] 4.1 Implement `agent/guardrails.py` — Tool_Loop_Guardrails
    - Define `GuardrailLedger` dataclass with `exact_failures`, `same_tool_failures`, `idempotent_runs`.
    - Implement `tool_hash(name, args_obj_or_str) -> str` using `sha256(name + "\u0000" + canonical_args(args)).hexdigest()`.
    - Implement `GuardrailsController(mode)` with `should_dispatch(call)` returning a synthetic block `Message` when in `enforce` mode at the block band, otherwise `None`.
    - Implement `record_outcome(call, result_msg)` returning a warning `Message` when a warn band fires; raise `GuardrailHalt` on the same_tool halt band in `enforce` mode (downgraded to a warning in `warn` mode).
    - Bands per design: exact_failure 2/5, same_tool_failure 3/8, idempotent_no_progress 2/5.
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8_

  - [ ] 4.2 Implement `agent/tool_dispatch.py` — sequential + parallel + safety scheduling
    - `ToolDispatcher(registry, mcp, guardrails, max_workers, timeout_s)`.
    - `execute(batch, interrupt) -> list[Message]` performs: classify safety per design §"agent.tool_dispatch"; ask `guardrails.should_dispatch` first; submit safe calls to a `ThreadPoolExecutor`; sequentialise `_NEVER_PARALLEL`; group `_PATH_SCOPED` by non-overlapping path arguments; enforce per-call timeout via `Future.result(timeout=timeout_s)` returning a synthetic timeout `Message`; check `interrupt` event and emit interruption Message when set; on JSON decode failure post a `tool` error Message and skip the MCP call; call `guardrails.record_outcome` after each result.
    - Each tool message uses markers from design §"Error Message conventions" (`json_decode_error`, `mcp_unavailable`, `tool_timeout`).
    - _Requirements: 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 8.1, 8.2, 8.3, 8.4, 8.6_

  - [ ] 4.3 Implement `agent/compressor.py` — 5-phase context compression
    - `ContextCompressor(llm, agent_cfg, token_estimator)`.
    - `compress(history, *, topic=None, force=False) -> CompressionResult` running phases 1–5 exactly per design §"agent.compressor".
    - Phase 1 prunes tool messages with content > 800 chars to the synthetic one-line summary.
    - Phase 2 walks newest→oldest accumulating tokens until tail reaches `protected_tail_fraction * context_window`; ensure latest user message included; align boundary to never split `assistant`+`tool_calls` group.
    - Phase 3 builds the 14-section summary system+user prompt, computes `Summary_Budget = clamp(0.20 * compressed_tokens, 2000, 12000)`, calls `llm.chat(max_tokens=Summary_Budget)`; when `topic` provided prepend `Focus on: <topic>.`.
    - Phase 4 assembles `[head_system_message, summary_message, *tail]`. Summary message has marker `<!-- claw_chat:compression_summary v1 -->\n`, role `system`, and `tool_name="__compression_summary__"`; ensure the 14 section headings exist in order with `_(none)_` fill-ins for empties.
    - Phase 5 sanitises orphan tool/assistant pairs.
    - Track `recent_reductions` per controller instance: after two consecutive passes with reduction <10%, skip subsequent `force=False` passes; `force=True` always runs.
    - Define `CompressionResult` dataclass.
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 11.1, 11.2, 11.3, 11.4, 12.1, 12.2, 12.3_

- [ ] 5. Loop and TUI
  - [ ] 5.1 Implement `agent/loop.py` — `AgentLoop` orchestrator
    - Constructor accepts all components per design §"agent.loop".
    - Implement `run_turn(user_text)` per design pseudocode: persist user message; per-iteration check budget exhaustion (append exactly one notice at `max_iterations + 1` then run grace iteration and halt); auto-compress before each LLM call when estimated prompt tokens exceed threshold * context_window; stream completion; persist assistant message; dispatch tool batch; persist tool messages; halt on `GuardrailHalt`.
    - Implement `run_oneshot(query) -> int` returning a process exit code following the §"Error taxonomy" table.
    - Implement `load_session(session_id)` enforcing the 500-message in-memory cap (plus an optional summary message) and pulling additional pages on demand for compression.
    - Wire `self._interrupt_event = threading.Event()` shared with the dispatcher and stream reader; expose `request_interrupt()` for the TUI signal handler.
    - Trigger title generation when session title is empty and the user-message count is ≥ 2.
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 2.3, 2.8, 4.1, 4.3, 6.1, 6.2, 6.3, 6.4, 6.5, 7.4, 8.6, 10.2, 10.7, 13.4, 16.1, 16.2, 16.3, 18.3_

  - [ ] 5.2 Implement `agent/tui.py` — `ChatTUI` and `SlashCommandDispatcher`
    - `ChatTUI(loop, slash)` using `prompt_toolkit.PromptSession` for input editing/history.
    - During a turn: text deltas via `print(..., end="", flush=True)` to stdout; Status_Line on stderr with `\r\x1b[K` overwrite, throttled to ≥0.5 s between renders (≤2 Hz); on turn end emit `\n` to stdout and clear the Status_Line.
    - Detect non-TTY stderr and fall back to periodic INFO log lines.
    - `_handle_line(line)` routes: `line.startswith("/")` → `slash.dispatch(line)`; otherwise → `loop.run_turn(line)`. Empty lines do nothing.
    - Install SIGINT handler that calls `loop.request_interrupt()`; clear current input line at the prompt when no turn is running.
    - `SlashCommandDispatcher(loop, persistence)` implements `/help`, `/quit`, `/new`, `/sessions`, `/compact`, `/compact <topic>`; unknown commands print an error and continue.
    - `run() -> int` returns the process exit code.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 11.1, 11.2, 11.3, 11.4, 13.2, 13.3, 13.5_


- [ ] 6. CLI integration
  - [ ] 6.1 Implement `cli/chat_cmd.py` — argparse registration and dispatcher
    - `register_chat_parser(subparsers)` adds the `chat` parser with flags `-q/--query`, `--session`, `--new`, `--model`, `--list-sessions`, `--verbose/-v` per design §"cli.chat_cmd".
    - `run_chat_command(args) -> bool` validates flag combinations (reject `--session` with `--new`; reject `--list-sessions` combined with other flags), resolves provider/model via `cli.config.get_selected_provider/get_selected_model` and `cli.providers.PROVIDER_INFO`, resolves API key via `cli.config.get_env_value`, builds `McpManager` from `gateway.config.load_gateway_config()` and starts it, instantiates `AgentConfig`, `LLMClient`, `ToolRegistry`, `ToolDispatcher`, `GuardrailsController`, `ContextCompressor`, `SqlitePersistence`, `TitleGenerator`, and `AgentLoop`.
    - Route to `loop.run_oneshot` for `--query` or to `ChatTUI(...).run()` for the interactive path.
    - Handle the `--list-sessions` short-circuit.
    - Configure logging: `--verbose` sets root logger to DEBUG; otherwise INFO. Install the redaction filter required by Property 30.
    - Catch top-level `ConfigError`, `KeyboardInterrupt`, and `Exception` per design §"CLI-level error handling" with the documented exit codes.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 3.8, 3.9, 3.10, 3.11, 3.12, 5.1, 5.2, 5.3, 5.4, 5.6, 5.7, 14.5, 15.1, 15.2, 15.3, 15.4, 15.5, 17.1, 17.2, 17.3, 17.4, 7.1, 7.2, 7.10_

  - [ ] 6.2 Wire `chat` into `cli/main.py`
    - Import `register_chat_parser, run_chat_command` from `cli.chat_cmd`.
    - Call `register_chat_parser(subparsers)` next to the existing `register_gateway_parser(subparsers)` invocation.
    - Add `args.command == "chat"` dispatch branch returning the result of `run_chat_command(args)`.
    - _Requirements: 15.1, 15.2_

  - [ ] 6.3 Export public API from `agent/__init__.py`
    - Re-export `AgentLoop`, `AgentConfig`, `Message`, `ToolCall`, `Session`, `ContextCompressor`, `GuardrailsController`, `ToolDispatcher`, `ToolRegistry`, `LLMClient`, `SqlitePersistence`, `TitleGenerator`.
    - _Requirements: 14.1_

- [ ] 7. Checkpoint — runtime fully wired
  - Ensure all tests pass, ask the user if questions arise.


- [ ] 8. Property-based tests — strategies and persistence properties
  - [ ] 8.1 Create shared Hypothesis strategies in `tests/agent/strategies.py`
    - Strategies for `Message` (across all role types, with valid `tool_call_id`/`tool_name` shapes), `ToolCall`, conversation histories that satisfy assistant→tool linkage invariants, JSON-encodable argument objects, and `AgentConfig` dicts with mixed valid/invalid keys.
    - _Requirements: 19.1, 19.2_

  - [ ]* 8.2 Write property test for round-trip persistence
    - **Property 1: Round-trip persistence**
    - **Validates: Requirements 19.1, 19.2**

  - [ ]* 8.3 Write property test for Tool_Hash determinism (canonical JSON)
    - **Property 4: Tool_Hash determinism (canonical JSON)**
    - **Validates: Requirements 9.1, 19.2**

  - [ ]* 8.4 Write property test for SQLite WAL concurrent writers
    - **Property 6: SQLite WAL allows concurrent writers without corruption**
    - **Validates: Requirements 18.1, 18.2**

  - [ ]* 8.5 Write property test for idempotent message append
    - **Property 14: Idempotent message append**
    - **Validates: Requirements 3.7**

  - [ ]* 8.6 Write property test for atomic batch metadata updates
    - **Property 15: Atomic batch metadata updates**
    - **Validates: Requirements 3.6**

  - [ ]* 8.7 Write property test for session prefix lookup correctness
    - **Property 16: Session prefix lookup correctness**
    - **Validates: Requirements 3.5, 3.8, 3.10**

  - [ ]* 8.8 Write property test for idempotent schema initialization
    - **Property 28: Idempotent schema initialization**
    - **Validates: Requirements 18.4**

  - [ ]* 8.9 Write property test for persistence retry bound
    - **Property 29: Persistence retry is bounded at three attempts**
    - **Validates: Requirements 18.3**

- [ ] 9. Property-based tests — compressor, guardrails, scheduling
  - [ ]* 9.1 Write property test for compression preserves last user message
    - **Property 2: Compression preserves the last user message**
    - **Validates: Requirements 10.4**

  - [ ]* 9.2 Write property test for anti-thrashing skip with manual bypass
    - **Property 5: Anti-thrashing skip with manual bypass**
    - **Validates: Requirements 10.7, 11.3**

  - [ ]* 9.3 Write property test for compression tail-fraction token budget
    - **Property 7: Compression tail-fraction token budget**
    - **Validates: Requirements 10.3**

  - [ ]* 9.4 Write property test for summary budget clamp
    - **Property 8: Summary budget clamp**
    - **Validates: Requirements 10.5**

  - [ ]* 9.5 Write property test for Phase-5 sanitisation eliminates orphans
    - **Property 9: Phase-5 sanitisation eliminates orphan tool/assistant pairs**
    - **Validates: Requirements 10.6**

  - [ ]* 9.6 Write property test for compression preserves pre-compression history in DB
    - **Property 10: Compression preserves pre-compression history in DB**
    - **Validates: Requirements 10.8**

  - [ ]* 9.7 Write property test for 14-section summary template
    - **Property 11: 14-section summary template emitted with `_(none)_` for empties**
    - **Validates: Requirements 12.1, 12.2**

  - [ ]* 9.8 Write property test for guardrail counters fire at exact thresholds
    - **Property 12: Guardrail counters fire at exact thresholds**
    - **Validates: Requirements 9.2, 9.3, 9.4, 9.5, 9.6, 9.7**

  - [ ]* 9.9 Write property test for guardrails mode switch
    - **Property 13: Guardrails mode switch**
    - **Validates: Requirements 9.8**

  - [ ]* 9.10 Write property test for safety-class scheduling
    - **Property 25: Safety-class scheduling**
    - **Validates: Requirements 8.2, 8.3, 8.4, 8.5**

  - [ ]* 9.11 Write property test for tool timeout cancellation
    - **Property 26: Tool timeout cancellation**
    - **Validates: Requirements 7.8, 7.9**

  - [ ]* 9.12 Write property test for invalid JSON tool args
    - **Property 27: Invalid JSON tool args produce error Message and skip MCP call**
    - **Validates: Requirements 7.5**


- [ ] 10. Property-based tests — loop, TUI, streaming, config, logging
  - [ ]* 10.1 Write property test for iteration budget bound and single budget notice
    - **Property 3: Iteration budget bound and single budget notice**
    - **Validates: Requirements 6.3, 6.4**

  - [ ]* 10.2 Write property test for bounded in-memory history with strict session scoping
    - **Property 17: Bounded in-memory history with strict session scoping**
    - **Validates: Requirements 16.1, 16.3**

  - [ ]* 10.3 Write property test for title update monotonicity and length bound
    - **Property 18: Title update monotonicity and length bound**
    - **Validates: Requirements 4.1, 4.2, 4.4**

  - [ ]* 10.4 Write property test for config loader is total
    - **Property 19: Config loader is total**
    - **Validates: Requirements 14.3, 14.4**

  - [ ]* 10.5 Write property test for stream delta concatenation
    - **Property 20: Stream delta concatenation**
    - **Validates: Requirements 2.4, 13.1**

  - [ ]* 10.6 Write property test for status line render throttle
    - **Property 21: Status line render throttle**
    - **Validates: Requirements 13.3**

  - [ ]* 10.7 Write property test for loop terminates on empty tool_calls
    - **Property 22: Loop terminates on empty tool_calls**
    - **Validates: Requirements 2.3**

  - [ ]* 10.8 Write property test for REPL dispatch routing
    - **Property 23: REPL dispatch routing**
    - **Validates: Requirements 2.2**

  - [ ]* 10.9 Write property test for interrupt terminates loop and pending workers
    - **Property 24: Interrupt terminates loop and pending workers**
    - **Validates: Requirements 2.8, 8.6, 13.4**

  - [ ]* 10.10 Write property test for secret redaction in logs
    - **Property 30: Secret redaction in logs**
    - **Validates: Requirements 17.3**

- [ ] 11. Example, edge, and integration tests
  - [ ]* 11.1 Write CLI surface example tests in `tests/agent/test_chat_cmd_examples.py`
    - Cover Reqs 1.1, 1.5, 2.1, 2.6, 2.7, 5.1, 5.3, 15.1, 15.2, 15.3, 17.1, 17.4 with argparse fixtures and a fake `AgentLoop`.
    - _Requirements: 1.1, 1.5, 2.1, 2.6, 2.7, 5.1, 5.3, 15.1, 15.2, 15.3, 17.1, 17.4_

  - [ ]* 11.2 Write CLI edge-case tests
    - Cover empty `-q ""` (Req 1.2), unrecoverable error exit (Req 1.4), session-not-found (Req 3.9), missing model config (Req 5.2), missing API key (Req 5.7), failed `/compact` (Req 11.4), `--session + --new` rejection (Req 15.4), `--list-sessions` combined with other flags (Req 15.5).
    - _Requirements: 1.2, 1.4, 3.9, 5.2, 5.7, 11.4, 15.4, 15.5_

  - [ ]* 11.3 Write SQLite schema introspection test in `tests/agent/test_persistence_schema.py`
    - Use `PRAGMA table_info` to assert column names and types for `sessions` and `messages` exactly match Req 3.3 and 3.4.
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [ ]* 11.4 Write provider configuration parametrised test
    - Iterate `cli.providers.PROVIDER_INFO` entries and verify each maps to a usable base URL and api_key_env via `LLMClient` construction (no real network).
    - _Requirements: 5.4, 5.6_

  - [ ]* 11.5 Write integration test that exercises an OpenAI-compatible HTTP target
    - Use a stdlib `http.server` fake to assert the request URL ends in `/chat/completions` and the body contains the `messages` array; covers streaming and non-streaming paths.
    - _Requirements: 5.5, 13.1, 13.5_

  - [ ]* 11.6 Write list-sessions example test
    - Seed two sessions with controlled `updated_at` and verify `--list-sessions` formats and sorts the output per Req 3.12.
    - _Requirements: 3.12_

- [ ] 12. Final checkpoint — Ensure all tests pass
  - Run `ruff check agent/ cli/chat_cmd.py tests/agent/`.
  - Run `pytest tests/agent/ -v`.
  - Ensure all tests pass, ask the user if questions arise.


## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. They cover all property tests, example tests, edge-case tests, and integration tests.
- Each task references specific requirements clauses for traceability. Multi-clause references are listed comma-separated.
- Checkpoints (tasks 3, 7, 12) ensure incremental validation between major phases.
- Property tests validate the 30 universal correctness properties from the design; example tests validate specific scenarios and edge cases; integration tests validate cross-component behavior.
- The runtime introduces no new production dependencies — only `hypothesis` is added to the `[dev]` extras for property-based testing.
- All implementation tasks target Python 3.11+ as declared in `pyproject.toml`. Existing modules are reused (`cli.config`, `cli.providers`, `gateway.config`, `gateway.mcp_client.McpManager`).
- Lint with `ruff check agent/ cli/chat_cmd.py tests/agent/` and run tests with `pytest tests/agent/ -v`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["2.1", "2.2", "2.3", "4.1", "8.1"] },
    { "id": 3, "tasks": ["2.4", "4.2", "4.3"] },
    { "id": 4, "tasks": ["5.1"] },
    { "id": 5, "tasks": ["5.2", "6.3"] },
    { "id": 6, "tasks": ["6.1"] },
    { "id": 7, "tasks": ["6.2"] },
    { "id": 8, "tasks": ["8.2", "8.4", "9.1", "9.8", "9.10", "10.1", "10.2", "10.3", "10.4", "10.5", "10.6", "10.8", "10.10"] },
    { "id": 9, "tasks": ["8.3", "8.5", "9.2", "9.9", "9.11", "10.7"] },
    { "id": 10, "tasks": ["8.6", "9.3", "9.12", "10.9"] },
    { "id": 11, "tasks": ["8.7", "9.4"] },
    { "id": 12, "tasks": ["8.8", "9.5"] },
    { "id": 13, "tasks": ["8.9", "9.6"] },
    { "id": 14, "tasks": ["9.7"] },
    { "id": 15, "tasks": ["11.1", "11.2", "11.3", "11.4", "11.5", "11.6"] }
  ]
}
```
