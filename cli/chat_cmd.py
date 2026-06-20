"""
CLI command: claw chat

Subcommands / flags:
  claw chat                        — Interactive TUI REPL
  claw chat -q/--query <text>      — One-shot query, print response, exit
  claw chat --session <id>         — Resume a session by 4-hex prefix
  claw chat --new                  — Start a fresh session
  claw chat --model <model_id>     — Override the configured model
  claw chat --list-sessions        — List sessions and exit
  claw chat --verbose/-v           — Enable DEBUG logging
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from typing import Optional

from cli.config import get_config_value, get_env_value
from cli.interactive_ui import print_error, print_info, print_warning
from cli.providers import PROVIDER_INFO


def register_chat_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'chat' command with argparse."""
    chat = subparsers.add_parser(
        "chat",
        help="Run an interactive AI agent",
        description="Chat with an AI agent that has access to your MCP tools.",
    )
    chat.add_argument(
        "-q", "--query",
        metavar="TEXT",
        help="One-shot query — print response and exit",
    )
    chat.add_argument(
        "--session",
        metavar="ID",
        help="Resume a session by its 4-hex short ID",
    )
    chat.add_argument(
        "--new",
        action="store_true",
        help="Start a fresh session (ignore any prior session)",
    )
    chat.add_argument(
        "--model",
        metavar="MODEL_ID",
        help="Override the configured model for this run",
    )
    chat.add_argument(
        "--list-sessions",
        action="store_true",
        help="List all saved sessions and exit",
    )
    chat.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging",
    )


def run_chat_command(args: argparse.Namespace) -> bool:
    """Dispatch the chat command. Returns True on success."""
    _setup_logging(args.verbose)

    # ── Mutual-exclusion validation (Req 15.4, 15.5) ──────────────────
    if getattr(args, "session", None) and getattr(args, "new", False):
        print_error("--session and --new are mutually exclusive.")
        sys.exit(2)

    if getattr(args, "list_sessions", False):
        for flag in ("query", "session", "new", "model"):
            if getattr(args, flag, None):
                print_error("--list-sessions cannot be combined with other flags.")
                sys.exit(2)
        return _cmd_list_sessions()

    # ── Resolve model + provider (Req 5.1–5.7) ────────────────────────
    model_override = getattr(args, "model", None)
    try:
        base_url, api_key, model, provider = _resolve_model(model_override)
    except SystemExit:
        raise
    except Exception as exc:
        print_error(f"Configuration error: {exc}")
        sys.exit(2)

    # ── Build runtime components ───────────────────────────────────────
    from agent.config import load_agent_config
    from agent.compressor import ContextCompressor
    from agent.guardrails import GuardrailsController
    from agent.llm_client import LLMClient
    from agent.loop import AgentLoop
    from agent.persistence import (
        AmbiguousSession,
        SessionNotFound,
        SqlitePersistence,
    )
    from agent.title_generator import TitleGenerator
    from agent.tool_dispatch import ToolDispatcher
    from agent.tool_registry import ToolRegistry
    from gateway.config import load_gateway_config
    from gateway.mcp_client import McpManager

    cfg = load_agent_config()
    llm = LLMClient(base_url=base_url, api_key=api_key, model=model)

    # MCP setup (Req 7.1, 7.2, 7.10)
    gw_cfg = load_gateway_config()
    mcp = McpManager()
    for name, srv in gw_cfg.mcp_servers.items():
        if srv.enabled:
            mcp.add_server(srv)
    start_results = mcp.start_all()
    failed = [n for n, ok in start_results.items() if not ok]
    if failed:
        print_warning(f"MCP servers failed to start: {', '.join(failed)}")
    if not start_results or all(not ok for ok in start_results.values()):
        print_warning("No MCP tools available. Running without tools.")

    registry = ToolRegistry(mcp)
    registry.reload_from_mcp()

    guardrails = GuardrailsController(mode=cfg.guardrails_mode)
    dispatcher = ToolDispatcher(
        registry=registry,
        mcp=mcp,
        guardrails=guardrails,
        max_workers=cfg.max_tool_workers,
        timeout_s=cfg.tool_call_timeout_seconds,
    )

    token_estimator = lambda s: max(1, len(s) // 4)  # noqa: E731
    compressor = ContextCompressor(
        llm=llm,
        agent_cfg=cfg,
        token_estimator=token_estimator,
    )
    title_gen = TitleGenerator(llm)

    persistence = SqlitePersistence()
    persistence.initialize()

    # ── Session resolution (Req 3.8–3.11) ─────────────────────────────
    session_id_arg = getattr(args, "session", None)
    new_flag = getattr(args, "new", False)

    try:
        if session_id_arg and not new_flag:
            session = persistence.get_session(session_id_arg)
        else:
            session = persistence.create_session(model)
    except SessionNotFound as exc:
        print_error(str(exc))
        sys.exit(2)
    except AmbiguousSession as exc:
        print_error(str(exc))
        sys.exit(2)

    loop = AgentLoop(
        cfg=cfg,
        llm=llm,
        mcp=mcp,
        registry=registry,
        dispatcher=dispatcher,
        compressor=compressor,
        guardrails=guardrails,
        persistence=persistence,
        session=session,
        token_estimator=token_estimator,
        title_generator=title_gen,
    )
    loop._provider = provider  # for TUI display

    # Register the claw_subagent native tool so the LLM can delegate
    # sub-tasks to a nested agent loop (no-op when subagent_enabled=false).
    loop.register_subagent_tool()

    # Register skill tools so the LLM can discover and read bundled
    # skills during the session.
    loop.register_skill_tools()

    # Load history when resuming an existing session
    if session_id_arg and not new_flag:
        loop.load_session(session.id)

    # ── Route to one-shot or interactive mode ──────────────────────────
    query = getattr(args, "query", None)
    if query is not None:
        if not query.strip():
            print_error("Query cannot be empty.")
            sys.exit(2)
        exit_code = loop.run_oneshot(query)
        sys.exit(exit_code)

    # Interactive TUI
    from agent.tui import ChatTUI, SlashCommandDispatcher
    slash = SlashCommandDispatcher(loop=loop, persistence=persistence)
    tui = ChatTUI(loop=loop, slash=slash)
    exit_code = tui.run()
    sys.exit(exit_code)


# ── Private helpers ────────────────────────────────────────────────────────


def _setup_logging(verbose: bool) -> None:
    """Configure logging for the chat command (Req 17.1, 17.2)."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    # Suppress noisy library loggers unless verbose
    if not verbose:
        for lib in ("urllib3", "requests", "httpx", "httpcore"):
            logging.getLogger(lib).setLevel(logging.WARNING)

    # Install redaction filter (Req 17.3)
    logging.root.addFilter(_RedactionFilter())


def _resolve_model(model_override: Optional[str]) -> tuple:
    """Return (base_url, api_key, model, provider).

    Resolution order:
    1. --model flag overrides the model name but keeps the configured provider.
    2. cli.config.get_config_value("model.provider") + "model.model".
    3. Fail with exit code 2 if no model is configured (Req 5.2).
    """
    provider = get_config_value("model.provider")
    configured_model = get_config_value("model.model")

    if not provider or not configured_model:
        print_error(
            "No model configured. Run 'claw models' to set up a provider and model."
        )
        sys.exit(2)

    model = model_override or configured_model

    provider_info = PROVIDER_INFO.get(provider)
    if not provider_info:
        # Unknown provider — try to use the base_url from config directly
        base_url = get_config_value("model.base_url") or ""
        if not base_url:
            print_error(
                f"Unknown provider '{provider}'. Run 'claw models' to reconfigure."
            )
            sys.exit(2)
        api_key_env = f"{provider.upper()}_API_KEY"
    else:
        base_url = provider_info["base_url"]
        api_key_env = provider_info["api_key_env"]

    # Resolve API key (Req 5.7)
    api_key = os.environ.get(api_key_env) or get_env_value(api_key_env) or ""
    if not api_key:
        key_hint = (provider_info or {}).get("key_hint", "")
        hint_text = f" Get one at: {key_hint}" if key_hint else ""
        print_error(
            f"API key not found. Set the {api_key_env} environment variable.{hint_text}"
        )
        sys.exit(2)

    return base_url, api_key, model, provider


def _cmd_list_sessions() -> bool:
    """Print all sessions sorted by updated_at descending (Req 3.12)."""
    from agent.persistence import SqlitePersistence

    persistence = SqlitePersistence()
    persistence.initialize()
    sessions = persistence.list_sessions()

    if not sessions:
        print_info("No sessions found.")
        return True

    for s in sessions:
        try:
            ts = datetime.fromtimestamp(s.updated_at).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            ts = str(s.updated_at)
        title = s.title or "(untitled)"
        print(f"{s.short_id}  {ts}  {s.model}  {title}")

    return True


class _RedactionFilter(logging.Filter):
    """Redact API key / token values from log records (Req 17.3)."""

    _SENSITIVE_PATTERNS = ("API_KEY", "TOKEN")
    _MIN_SECRET_LEN = 16

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = self._redact_args(record.args)
        record.msg = self._redact_str(str(record.msg))
        return True

    def _redact_args(self, args):
        if isinstance(args, dict):
            return {k: self._maybe_redact(k, v) for k, v in args.items()}
        if isinstance(args, (list, tuple)):
            return type(args)(self._redact_str(str(a)) for a in args)
        return args

    def _maybe_redact(self, key: str, value) -> object:
        key_upper = key.upper()
        if any(p in key_upper for p in self._SENSITIVE_PATTERNS):
            if isinstance(value, str) and len(value) > self._MIN_SECRET_LEN:
                return "***"
        return value

    def _redact_str(self, text: str) -> str:
        # Simple heuristic: redact long bearer-token-like strings
        # that appear after "Bearer " or "key=" in log messages.
        import re
        return re.sub(
            r"(Bearer\s+|key=|token=|api_key=)([A-Za-z0-9_\-\.]{17,})",
            r"\1***",
            text,
            flags=re.IGNORECASE,
        )
