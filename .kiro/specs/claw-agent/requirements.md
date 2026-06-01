# Requirements Document

## Introduction

This feature adds an interactive AI agent (chat) capability to the existing `claw` CLI. Users can run `claw chat -q "<query>"` for a one-shot exchange or `claw chat` to enter an interactive Terminal UI (TUI) session that supports multi-turn conversation, slash commands, streaming output, MCP tool execution, persistent sessions, context compression, and tool-loop guardrails.

The design follows patterns established in Hermes Agent (synchronous main loop with parallel tool execution, iteration-budget control, 5-phase context compression with token-budget tail protection, registry-based tool system, and INCOSE-style guardrails). The agent reuses the gateway's `McpManager` for tool discovery and execution, shares model configuration with the rest of the `claw` CLI, and persists session state to a SQLite database at `~/.claw/agent.db`.

This iteration targets OpenAI-compatible chat-completions APIs (the same providers already wired into `claw models`: OpenRouter, Nous, MiniMax, NVIDIA). Subagent delegation, persistent memory files, native multi-provider transports, multimodal input, and a plugin system are explicitly out of scope.

## Glossary

- **Claw_Chat**: The new `claw chat` CLI subcommand and its supporting agent runtime.
- **Agent_Loop**: The synchronous main loop that builds API kwargs, calls the LLM, dispatches any tool calls, and either continues iterating or returns the final assistant text.
- **Iteration**: One complete pass through the Agent_Loop (one LLM call plus zero or more tool dispatches that result from it).
- **Iteration_Budget**: The configured maximum number of Iterations the Agent_Loop will run before halting; default value is 90.
- **Grace_Iteration**: A single additional Iteration permitted after the Iteration_Budget is exhausted, during which the Agent_Loop appends one budget-exhaustion notice to the conversation and allows the LLM one final response.
- **One_Shot_Mode**: Invocation `claw chat -q "<query>"` that sends a single user query, prints the assistant response, and exits.
- **Interactive_Mode**: Invocation `claw chat` (without `-q`) that enters a TUI REPL allowing multi-turn conversation until the user issues `/quit` or sends EOF.
- **Session**: A persisted conversation, identified by a UUID, comprising an ordered list of Messages, plus metadata (title, created_at, updated_at, model, total_tokens).
- **Session_Id**: The short UUID prefix (e.g. `a4e8`) used to refer to a Session on the command line.
- **Message**: One row in the conversation history with role `system`, `user`, `assistant`, or `tool`, plus optional `tool_call_id`, `tool_name`, and `tool_arguments` fields.
- **Tool_Registry**: The in-memory registry that holds tool definitions in OpenAI function-calling format and dispatches calls; populated at chat startup from the gateway's `McpManager`.
- **MCP_Manager**: The existing `gateway.mcp_client.McpManager` instance used to start MCP server subprocesses and call their tools.
- **Tool_Call**: A single LLM-emitted request to invoke one tool with a JSON-encoded arguments object.
- **Tool_Result**: The string content (or error) returned by executing a Tool_Call, posted back to the conversation as a `tool` Message.
- **Tool_Worker_Pool**: The `ThreadPoolExecutor` that executes Tool_Calls in parallel within one Iteration when safety classification permits.
- **Parallel_Safety_Class**: One of `_NEVER_PARALLEL`, `_PARALLEL_SAFE`, or `_PATH_SCOPED`, attached to each tool definition; determines whether the tool may run concurrently with others in the same Iteration.
- **Context_Compressor**: The component that compresses the conversation when token usage exceeds the configured threshold or the user issues `/compact`.
- **Compression_Threshold**: The fraction of the model's context-window token capacity at which automatic compression triggers; default 0.70.
- **Protected_Tail**: The trailing portion of the conversation excluded from compression, sized by token budget (not by fixed message count) and always containing the most recent user Message.
- **Summary_Budget**: The token budget allocated to the generated summary, set to 20% of the compressed region size, with a floor of 2000 tokens and a cap of 12000 tokens.
- **Compression_Result**: The artifact produced by one compression pass: a structured 14-section summary Message that replaces the compressed prefix while the Protected_Tail is preserved verbatim.
- **Tool_Loop_Guardrails**: The detector that classifies repeated identical or unproductive Tool_Calls into the bands `exact_failure`, `same_tool_failure`, and `idempotent_no_progress`.
- **Tool_Hash**: The SHA-256 hash of `(tool_name, canonical_json(arguments))` used by Tool_Loop_Guardrails to detect repeated identical Tool_Calls.
- **Slash_Command**: A line in Interactive_Mode beginning with `/` that is interpreted by Claw_Chat (not sent to the LLM): `/compact`, `/compact <topic>`, `/new`, `/quit`, `/help`, `/sessions`.
- **Status_Line**: The persistent line at the bottom of the TUI showing iteration count, active tool calls, and cumulative token usage.
- **Agent_Config**: The `agent` object inside `~/.claw/config.json`, holding `max_iterations`, `context_compression_threshold`, `max_tool_workers`, `tool_call_timeout_seconds`, and guardrail mode.

## Requirements

### Requirement 1: One-shot chat invocation

**User Story:** As a CLI user, I want to ask the agent a single question from a shell script, so that I can integrate it into pipelines without an interactive session.

#### Acceptance Criteria

1. WHEN the user runs `claw chat -q "<query>"` with a non-empty query, THE Claw_Chat SHALL run the Agent_Loop with that query as the initial user Message and write the final assistant text to standard output.
2. WHEN the user runs `claw chat -q ""` (empty query), THE Claw_Chat SHALL print an error message to standard error and exit with status code 2.
3. WHEN One_Shot_Mode completes successfully, THE Claw_Chat SHALL exit with status code 0.
4. IF the Agent_Loop in One_Shot_Mode terminates due to an unrecoverable error (provider HTTP error, tool subsystem failure, or unhandled exception), THEN THE Claw_Chat SHALL print a single-line error description to standard error and exit with status code 1.
5. WHEN One_Shot_Mode is invoked without `--session` or `--new`, THE Claw_Chat SHALL create a new Session, persist its messages, and print the resulting Session_Id on the final line of standard error.

### Requirement 2: Interactive TUI mode

**User Story:** As a CLI user, I want a multi-turn chat REPL in my terminal, so that I can iterate on a task with the agent.

#### Acceptance Criteria

1. WHEN the user runs `claw chat` without `-q`, THE Claw_Chat SHALL launch the Interactive_Mode REPL using `prompt_toolkit`.
2. WHILE Interactive_Mode is active, THE Claw_Chat SHALL display a prompt, read one user line at a time, and dispatch each line either as a Slash_Command or as a user Message to the Agent_Loop.
3. WHEN the user submits a non-empty, non-slash line, THE Claw_Chat SHALL append it as a user Message and run one Agent_Loop turn until the next assistant text reply (i.e., until no further Tool_Calls are emitted) is produced.
4. WHILE the Agent_Loop is producing an assistant response, THE Claw_Chat SHALL stream output chunks to the terminal as they arrive from the provider.
5. WHILE the Agent_Loop is running, THE Claw_Chat SHALL render the Status_Line containing the current iteration count, the count and names of in-flight Tool_Calls, and the cumulative session token usage.
6. WHEN the user submits the Slash_Command `/quit` or sends EOF (Ctrl+D / Ctrl+Z), THE Claw_Chat SHALL exit Interactive_Mode with status code 0.
7. WHEN the user submits the Slash_Command `/help`, THE Claw_Chat SHALL print the list of supported Slash_Commands with one-line descriptions and continue Interactive_Mode.
8. WHEN the user presses Ctrl+C while the Agent_Loop is running, THE Claw_Chat SHALL set an interrupt flag, propagate cancellation to all in-flight Tool_Worker_Pool tasks, append an interruption notice as a tool/system Message, and return control to the REPL prompt.
9. WHEN the user presses Ctrl+C at the REPL prompt with no pending Agent_Loop, THE Claw_Chat SHALL clear the current input line and continue Interactive_Mode.
10. THE Claw_Chat SHALL run on Windows, macOS, and Linux without requiring platform-specific installation steps beyond `pip install`.

### Requirement 3: Session persistence and resumption

**User Story:** As a CLI user, I want my conversations saved so I can pick them up later or audit them.

#### Acceptance Criteria

1. THE Claw_Chat SHALL store sessions and messages in a SQLite database located at `~/.claw/agent.db`.
2. WHEN the database file does not exist at startup, THE Claw_Chat SHALL create the file and initialize the schema before processing any user input.
3. THE Claw_Chat SHALL maintain a `sessions` table with columns `id` (TEXT primary key, UUID), `title` (TEXT), `created_at` (INTEGER unix epoch seconds), `updated_at` (INTEGER unix epoch seconds), `model` (TEXT), and `total_tokens` (INTEGER).
4. THE Claw_Chat SHALL maintain a `messages` table with columns `id` (INTEGER primary key autoincrement), `session_id` (TEXT, foreign key to `sessions.id`), `role` (TEXT, one of `system`, `user`, `assistant`, `tool`), `content` (TEXT), `tool_call_id` (TEXT nullable), `tool_name` (TEXT nullable), `tool_arguments` (TEXT JSON-encoded, nullable), and `timestamp` (INTEGER unix epoch seconds).
5. WHEN a new Session is created, THE Claw_Chat SHALL generate a UUIDv4, derive the Session_Id as the first 4 hexadecimal characters of the UUID, and insert a row into `sessions`.
6. WHEN an Iteration of the Agent_Loop produces one or more new Messages, THE Claw_Chat SHALL persist those Messages and update the parent Session's `updated_at` and `total_tokens` within a single SQLite transaction before the next Iteration begins.
7. WHEN persistence is retried for a Message that has already been written, THE Claw_Chat SHALL detect the duplicate by primary key or content+timestamp signature and SHALL NOT create a duplicate row.
8. WHEN the user runs `claw chat --session <id>` and `<id>` matches the prefix of exactly one existing Session, THE Claw_Chat SHALL load that Session's Messages in order and continue the conversation in the chosen mode (one-shot or interactive).
9. IF `claw chat --session <id>` matches zero Sessions, THEN THE Claw_Chat SHALL print an error message to standard error and exit with status code 2.
10. IF `claw chat --session <id>` matches more than one Session, THEN THE Claw_Chat SHALL print an error message listing the matching Session_Ids to standard error and exit with status code 2.
11. WHEN the user runs `claw chat --new`, THE Claw_Chat SHALL ignore any prior Session selection and create a fresh Session.
12. WHEN the user runs `claw chat --list-sessions`, THE Claw_Chat SHALL print one line per Session containing Session_Id, `updated_at` formatted as ISO-8601 local time, model, and title (or `(untitled)` when title is empty), sorted by `updated_at` descending, and exit with status code 0.

### Requirement 4: Auto-generated session titles

**User Story:** As a CLI user, I want each session to get a meaningful title automatically so I can find old sessions in `--list-sessions`.

#### Acceptance Criteria

1. WHEN a Session has no title and the conversation has reached at least 2 user Messages, THE Claw_Chat SHALL generate a title from the conversation and persist it on the Session row.
2. THE Claw_Chat SHALL produce titles between 1 and 80 characters in length, inclusive.
3. WHEN title generation fails (provider error or empty response), THE Claw_Chat SHALL leave the Session title empty and SHALL retry generation after the next user Message.
4. WHERE the user has explicitly set a title via a future configuration mechanism, THE Claw_Chat SHALL NOT overwrite a non-empty existing title.

### Requirement 5: Model selection and provider compatibility

**User Story:** As a CLI user, I want the chat agent to use the same model I've configured for the rest of `claw`, so I don't have to set it twice.

#### Acceptance Criteria

1. WHEN `claw chat` is invoked without `--model`, THE Claw_Chat SHALL use the model returned by `cli.config.get_selected_model()` and the provider returned by `cli.config.get_selected_provider()`.
2. IF no model is configured at startup, THEN THE Claw_Chat SHALL print an instruction to run `claw models` and exit with status code 2.
3. WHEN the user passes `--model <model_id>`, THE Claw_Chat SHALL use `<model_id>` for the duration of the invocation, overriding the configured model, and SHALL NOT write the override to `~/.claw/config.json`.
4. THE Claw_Chat SHALL resolve the provider's base URL and API key environment variable using `cli.providers.PROVIDER_INFO`, mirroring the resolution path used by `claw models`.
5. THE Claw_Chat SHALL call providers via the OpenAI-compatible chat-completions API.
6. THE Claw_Chat SHALL support the same providers already declared in `cli.providers.PROVIDER_INFO`: OpenRouter, Nous, MiniMax, and NVIDIA.
7. IF the configured provider's API key is missing from `~/.claw/.env`, project `.env`, and the process environment, THEN THE Claw_Chat SHALL print the missing-key environment variable name and the provider's `key_hint` URL and exit with status code 2.

### Requirement 6: Iteration budget and grace iteration

**User Story:** As a CLI user, I want the agent to stop running away on its own, so that runaway loops don't burn tokens or time.

#### Acceptance Criteria

1. THE Claw_Chat SHALL read `agent.max_iterations` from `~/.claw/config.json` and SHALL default to 90 when the value is missing or non-positive.
2. WHILE the Agent_Loop is running, THE Claw_Chat SHALL increment an iteration counter once per Iteration.
3. WHEN the iteration counter reaches the Iteration_Budget, THE Claw_Chat SHALL append exactly one budget-exhaustion notice as a system Message describing the limit and inviting a final summary.
4. WHEN the budget-exhaustion notice has been appended, THE Claw_Chat SHALL run exactly one Grace_Iteration, after which it SHALL halt the Agent_Loop regardless of whether further Tool_Calls were emitted.
5. WHEN the Agent_Loop halts due to the Iteration_Budget, THE Claw_Chat SHALL print a one-line warning to standard error stating that the budget was exhausted.

### Requirement 7: MCP tool discovery and execution

**User Story:** As a CLI user, I want the chat agent to use the same MCP tools the gateway uses, so that I can drive my MCP servers from the terminal.

#### Acceptance Criteria

1. WHEN Claw_Chat starts, THE Claw_Chat SHALL load gateway configuration via `gateway.config.load_gateway_config()` and instantiate an MCP_Manager.
2. WHEN Claw_Chat starts, THE Claw_Chat SHALL register every MCP server whose `enabled` flag is true and SHALL invoke `MCP_Manager.start_all()`.
3. WHEN MCP servers have been started, THE Claw_Chat SHALL collect tool definitions via `MCP_Manager.get_all_tools()` and register each one in the Tool_Registry as an OpenAI-format function object whose `name`, `description`, and `parameters` are derived from the MCP tool's `name`, `description`, and `input_schema`.
4. WHEN the LLM emits a Tool_Call, THE Claw_Chat SHALL look up the tool in the Tool_Registry, parse the JSON `arguments`, and dispatch to `MCP_Manager.call_tool_by_name(tool_name, arguments)`.
5. WHEN a Tool_Call's `arguments` field cannot be parsed as JSON, THE Claw_Chat SHALL post a `tool` Message containing the parse error description and SHALL NOT invoke the MCP_Manager.
6. WHEN a Tool_Call returns a successful Tool_Result, THE Claw_Chat SHALL append a `tool` Message whose `content` is the Tool_Result's text, `tool_call_id` is the LLM-supplied call id, and `tool_name` is the invoked tool's name.
7. WHEN a Tool_Call returns a failure Tool_Result, THE Claw_Chat SHALL append a `tool` Message whose `content` describes the error and SHALL allow the Agent_Loop to continue.
8. THE Claw_Chat SHALL apply a per-Tool_Call timeout equal to `agent.tool_call_timeout_seconds` from Agent_Config, defaulting to 300 seconds when the value is missing or non-positive.
9. IF a Tool_Call exceeds its timeout, THEN THE Claw_Chat SHALL cancel the worker, append a `tool` Message containing a timeout notice, and continue the Agent_Loop.
10. WHEN no MCP server is enabled or all start attempts fail, THE Claw_Chat SHALL run the Agent_Loop with an empty Tool_Registry and SHALL print a one-line warning to standard error.

### Requirement 8: Parallel tool execution

**User Story:** As a CLI user, I want the agent to run independent tool calls in parallel, so that multi-step workflows are faster.

#### Acceptance Criteria

1. THE Claw_Chat SHALL maintain a Tool_Worker_Pool sized by `agent.max_tool_workers` from Agent_Config, defaulting to 4 when the value is missing or non-positive.
2. WHEN one Iteration produces multiple Tool_Calls and every involved tool's Parallel_Safety_Class is `_PARALLEL_SAFE`, THE Claw_Chat SHALL submit them concurrently to the Tool_Worker_Pool.
3. WHEN one Iteration produces multiple Tool_Calls and at least one involved tool's Parallel_Safety_Class is `_NEVER_PARALLEL`, THE Claw_Chat SHALL execute all Tool_Calls in that Iteration sequentially in the order received from the LLM.
4. WHEN one Iteration produces multiple Tool_Calls and the tools are classified `_PATH_SCOPED`, THE Claw_Chat SHALL run concurrently only those Tool_Calls whose path arguments do not overlap, and SHALL run overlapping ones sequentially.
5. WHEN MCP tools are registered without explicit safety metadata, THE Claw_Chat SHALL classify them as `_NEVER_PARALLEL` by default.
6. WHEN the user presses Ctrl+C during parallel execution, THE Claw_Chat SHALL set the interrupt flag observed by every worker, cancel pending submissions, and post a single interruption Message representing the aggregate cancellation.

### Requirement 9: Tool-loop guardrails

**User Story:** As a CLI user, I want the agent to stop calling the same failing tool over and over, so that I don't waste a session in a loop.

#### Acceptance Criteria

1. WHEN a Tool_Call is dispatched, THE Claw_Chat SHALL compute the Tool_Hash and record the result outcome (success or failure) in an in-memory ledger keyed by Session.
2. WHEN the same Tool_Hash has produced 2 consecutive failure outcomes within the current Session, THE Claw_Chat SHALL append a warning system Message describing the repeated failure (`exact_failure` warn band).
3. WHEN the same Tool_Hash has produced 5 consecutive failure outcomes within the current Session, THE Claw_Chat SHALL refuse to dispatch the next identical Tool_Call, post a `tool` Message describing the block, and continue the Agent_Loop (`exact_failure` block band).
4. WHEN any Tool_Call to the same `tool_name` (regardless of arguments) has produced 3 consecutive failure outcomes, THE Claw_Chat SHALL append a warning system Message (`same_tool_failure` warn band).
5. WHEN any Tool_Call to the same `tool_name` has produced 8 consecutive failure outcomes, THE Claw_Chat SHALL halt the Agent_Loop, append a halt notice, and return control to the user (`same_tool_failure` halt band).
6. WHEN a tool tagged idempotent has been called 2 consecutive times with identical arguments and the Tool_Result content is identical to the prior call's content, THE Claw_Chat SHALL append a warning system Message (`idempotent_no_progress` warn band).
7. WHEN such idempotent-no-progress detection reaches 5 consecutive matching calls, THE Claw_Chat SHALL refuse the next identical Tool_Call and post a `tool` Message describing the block (`idempotent_no_progress` block band).
8. THE Claw_Chat SHALL read the guardrail mode from `agent.guardrails_mode` in Agent_Config, where the value `warn` (default) emits warning Messages without blocking, and the value `enforce` activates the block and halt bands defined above.

### Requirement 10: Context compression — automatic trigger

**User Story:** As a CLI user, I want long conversations to keep working even when they exceed the model's context window, so that I don't have to manually truncate.

#### Acceptance Criteria

1. THE Claw_Chat SHALL read `agent.context_compression_threshold` from Agent_Config and SHALL default to 0.70 when the value is missing or outside the open interval (0.0, 1.0).
2. WHEN the cumulative prompt token estimate exceeds the Compression_Threshold multiplied by the active model's context-window size, THE Claw_Chat SHALL trigger one Context_Compressor pass before the next LLM call.
3. THE Claw_Chat SHALL select the Protected_Tail by accumulating the most recent Messages from newest to oldest until the Protected_Tail token total reaches a configured fraction of the context window, and SHALL stop accumulating once that fraction is met.
4. THE Claw_Chat SHALL ensure the most recent user Message is contained in the Protected_Tail.
5. THE Claw_Chat SHALL compute the Summary_Budget as 20 percent of the compressed-region token total, clamped to a minimum of 2000 tokens and a maximum of 12000 tokens.
6. THE Context_Compressor SHALL execute the 5-phase algorithm in order: phase 1 prunes verbose Tool_Result bodies; phase 2 determines the cut boundary between compressed region and Protected_Tail; phase 3 generates a summary using the active model and the Summary_Budget; phase 4 assembles the resulting Message list as `[system_message, summary_message, *protected_tail]`; phase 5 sanitizes orphaned tool/assistant pairs so every `tool` Message has a matching upstream `assistant` Message with the same `tool_call_id`.
7. WHEN compression has been triggered twice in the current Session and neither pass reduced the post-compression token total by more than 10 percent, THE Claw_Chat SHALL skip subsequent automatic compressions for the remainder of the Session and SHALL warn the user once on standard error.
8. WHEN compression succeeds, THE Claw_Chat SHALL persist the resulting summary Message and SHALL retain the full pre-compression history in the SQLite database.

### Requirement 11: Context compression — manual trigger

**User Story:** As a CLI user, I want to compress the context on demand and optionally focus on a topic, so that I can shape what the agent remembers.

#### Acceptance Criteria

1. WHEN the user submits the Slash_Command `/compact`, THE Claw_Chat SHALL run one Context_Compressor pass with the default summary instructions and SHALL print a one-line confirmation including before/after token counts.
2. WHEN the user submits the Slash_Command `/compact <topic>`, THE Claw_Chat SHALL run one Context_Compressor pass whose summary instruction prepends the topic and instructs the summarizer to prioritize information related to `<topic>`.
3. WHEN a manual `/compact` is invoked, THE Claw_Chat SHALL bypass the anti-thrashing skip rule defined in Requirement 10 and SHALL always perform the compression.
4. IF a manual `/compact` fails (provider error, insufficient compressible content), THEN THE Claw_Chat SHALL print a one-line error to standard error, leave the conversation unchanged, and continue Interactive_Mode.

### Requirement 12: Compression summary template

**User Story:** As an agent author, I want compression summaries to follow a consistent structure, so that subsequent LLM turns can reliably extract the right information.

#### Acceptance Criteria

1. THE Context_Compressor SHALL produce a summary Message whose body contains exactly the following 14 sections, each introduced by a level-2 Markdown heading and emitted in this order: `## Active Task`, `## Completed Actions`, `## Blocked`, `## User Preferences`, `## Files & Resources`, `## Decisions Made`, `## Open Questions`, `## Errors Encountered`, `## Tools Used`, `## Key Findings`, `## Next Steps`, `## Out of Scope`, `## References`, `## Notes`.
2. WHEN a section has no relevant content from the compressed region, THE Context_Compressor SHALL emit the heading followed by the literal line `_(none)_`.
3. THE Context_Compressor SHALL emit the summary Message with role `system` and SHALL prefix the body with a marker line that identifies it as a compression summary.

### Requirement 13: Streaming responses and status line

**User Story:** As a CLI user, I want to see the agent's response as it's generated, so that the wait feels responsive.

#### Acceptance Criteria

1. WHEN the active provider supports OpenAI-compatible streaming, THE Claw_Chat SHALL request streamed completions and SHALL flush each text delta to the terminal as it arrives.
2. WHEN streaming completes for one Iteration, THE Claw_Chat SHALL emit a newline before printing the Status_Line update.
3. WHILE streaming is active in Interactive_Mode, THE Claw_Chat SHALL update the Status_Line at most twice per second.
4. WHEN streaming is interrupted by Ctrl+C, THE Claw_Chat SHALL stop reading further deltas, finalize the partial assistant Message in memory, persist what has been received, and return to the REPL prompt.
5. WHEN streaming returns no text but a non-empty `tool_calls` array, THE Claw_Chat SHALL skip terminal printing and proceed directly to tool dispatch.

### Requirement 14: Configuration loading and defaults

**User Story:** As a CLI user, I want sensible defaults but the ability to tune the agent, so that I don't have to configure it just to get started.

#### Acceptance Criteria

1. THE Claw_Chat SHALL read Agent_Config from the `agent` object inside `~/.claw/config.json` (with project-local `config.json` taking precedence per existing `cli.config` rules).
2. THE Claw_Chat SHALL accept the following Agent_Config keys: `max_iterations` (integer), `context_compression_threshold` (number in (0,1)), `max_tool_workers` (integer), `tool_call_timeout_seconds` (integer), and `guardrails_mode` (string, one of `warn` or `enforce`).
3. WHEN any Agent_Config key is missing, THE Claw_Chat SHALL substitute the default values defined in Requirements 6, 7, 8, 9, and 10.
4. WHEN any Agent_Config key has a value of the wrong type or outside its valid range, THE Claw_Chat SHALL substitute the default for that key and SHALL print a one-line warning to standard error naming the offending key.
5. THE Claw_Chat SHALL read provider API keys via `cli.config.get_env_value(<api_key_env>)`, which already searches `os.environ`, project `.env`, and `~/.claw/.env` in that order.

### Requirement 15: CLI argument surface

**User Story:** As a CLI user, I want the chat command's flags to be discoverable through the standard `--help`, so that I can learn options without reading docs.

#### Acceptance Criteria

1. THE Claw_Chat SHALL register a `chat` subcommand on the root `claw` argparse parser via the same registration pattern used by `register_gateway_parser`.
2. THE Claw_Chat SHALL accept the flags `-q/--query <text>`, `--session <id>`, `--new`, `--model <model_id>`, and `--list-sessions`.
3. WHEN the user runs `claw chat --help`, THE Claw_Chat SHALL print an argparse-generated help text that lists every flag from acceptance criterion 2 with a one-line description.
4. IF both `--session <id>` and `--new` are supplied, THEN THE Claw_Chat SHALL print an error to standard error and exit with status code 2.
5. IF `--list-sessions` is supplied together with any other chat flag (other than positional/help flags), THEN THE Claw_Chat SHALL print an error to standard error and exit with status code 2.

### Requirement 16: Conversation memory and large-history handling

**User Story:** As a CLI user, I want very long sessions to remain usable without exhausting RAM, so that I can keep using the same session for a long time.

#### Acceptance Criteria

1. WHEN a Session resumes with more than 500 stored Messages, THE Claw_Chat SHALL load Messages from SQLite using a paged cursor and SHALL hold no more than the most recent 500 Messages plus any compression summary Message in memory at once.
2. WHEN older Messages must be referenced (for example, during compression that needs more history), THE Claw_Chat SHALL fetch additional pages from SQLite on demand.
3. THE Claw_Chat SHALL never load Messages from a different Session than the active one into in-memory state during a single invocation.

### Requirement 17: Logging and diagnostics

**User Story:** As a CLI user debugging strange behavior, I want to enable verbose logging, so that I can see what the agent is doing internally.

#### Acceptance Criteria

1. THE Claw_Chat SHALL accept a `--verbose` / `-v` flag on the `chat` subcommand that sets the root logger to `DEBUG`.
2. WHEN `--verbose` is not supplied, THE Claw_Chat SHALL emit at most `INFO`-level log records to standard error and SHALL suppress library `DEBUG` records.
3. THE Claw_Chat SHALL never log API key values, even at `DEBUG` level, and SHALL redact any string longer than 16 characters whose key name matches `*API_KEY*` or `*TOKEN*` in log records emitted from Claw_Chat modules.
4. WHEN a Tool_Call is dispatched, THE Claw_Chat SHALL log at `DEBUG` level the tool name, server name, argument keys (not values), and elapsed time on completion.

### Requirement 18: Database integrity and concurrency

**User Story:** As a CLI user who occasionally runs two `claw chat` instances by accident, I want the database to stay consistent, so that I don't lose conversations.

#### Acceptance Criteria

1. THE Claw_Chat SHALL open the SQLite database with `journal_mode=WAL` and `synchronous=NORMAL`.
2. WHEN two `claw chat` processes write to the same database concurrently, THE Claw_Chat SHALL serialize writes via SQLite's locking and SHALL NOT corrupt the schema.
3. WHEN a write transaction fails (lock timeout, disk full), THE Claw_Chat SHALL roll back the transaction, print a one-line warning to standard error, retry the transaction up to two additional times with linear backoff, and on third failure abort the current Iteration with status code 1 in One_Shot_Mode or return control to the REPL prompt in Interactive_Mode.
4. THE Claw_Chat SHALL apply schema migrations idempotently by checking a `schema_version` row in a `meta` table before applying upgrades.

### Requirement 19: Round-trip persistence

**User Story:** As a developer, I want to be confident that a session re-loaded from SQLite produces the exact conversation that was saved, so that I can trust resumption.

#### Acceptance Criteria

1. FOR ALL Sessions written by Claw_Chat, loading the Session from SQLite and serializing it back to the in-memory Message list SHALL produce a Message list whose ordered `(role, content, tool_call_id, tool_name, tool_arguments)` tuples are equal to the originally persisted tuples (round-trip property).
2. THE Claw_Chat SHALL store `tool_arguments` as JSON text using a stable key ordering (e.g., `json.dumps(..., sort_keys=True)`), so that re-encoding produces byte-identical output for equal-valued objects.

## Out of Scope (this iteration)

The following items are intentionally not addressed by this requirements set and SHALL be specified in a future iteration if needed:

- Subagent delegation (a `delegate_task` tool that spawns child agent loops).
- Persistent agent memory files (`MEMORY.md`, `USER.md`, etc.).
- Native multi-provider transports beyond OpenAI-compatible chat completions (Anthropic native API, AWS Bedrock, Google Gemini native, etc.).
- Multimodal input (image, audio, video).
- A plugin system for user-defined tools beyond MCP.
- Authoring or editing skills via the chat agent.
