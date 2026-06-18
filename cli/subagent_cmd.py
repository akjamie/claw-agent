"""
CLI command: claw subagent

Manage and invoke configured sub-agents.

Subcommands:
  claw subagent list                  — List all configured sub-agents
  claw subagent show                  — Show global sub-agent settings
  claw subagent enable                — Enable sub-agents globally
  claw subagent disable               — Disable sub-agents globally
  claw subagent config                — Interactively tweak global knobs
  claw subagent add                   — Add a new sub-agent interactively
  claw subagent remove <name>         — Remove a sub-agent by tool name
  claw subagent toggle <name>         — Enable/disable one sub-agent
  claw subagent run <name> <task...>  — Run a named sub-agent one-shot
  claw subagent schema [name]         — Print tool schema (all or one)
"""

from __future__ import annotations

import argparse
import json
import sys

from cli.interactive_ui import (
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
    prompt_text,
    prompt_yes_no,
)


# ─── claw subagent list ───────────────────────────────────────────────────────

def _cmd_list(_args: argparse.Namespace) -> bool:
    """List all configured sub-agents with their status and description."""
    from agent.config import load_agent_config

    cfg = load_agent_config()
    global_state = "✓ enabled" if cfg.subagent_enabled else "✗ disabled (globally)"

    print_header("Configured Sub-agents")
    print_info(f"Global state : {global_state}")
    print_info(f"Max depth    : {cfg.subagent_max_depth}")
    print_info(f"Max iter/run : {cfg.subagent_max_iterations}")
    print()

    # Always include the built-in generic tool first.
    print_info("  claw_subagent  [built-in]  ✓ always registered")
    print_info("    Generic task delegation — the LLM invents the task at call time.")
    print()

    if not cfg.subagents:
        print_info("  No named sub-agents configured.")
        print_info("  Run 'claw subagent add' to create one.")
        return True

    for defn in cfg.subagents:
        status = "✓ enabled" if defn.enabled else "✗ disabled"
        model_tag = f"  model={defn.model}" if defn.model else ""
        iter_tag = f"  max_iter={defn.max_iterations}" if defn.max_iterations else ""
        prompt_tag = ""
        if defn.system_prompt:
            sp = defn.system_prompt.strip()
            # Show just the file name if it's a path, else first 60 chars.
            if sp.endswith(".md") or sp.endswith(".txt") or "/" in sp or "\\" in sp:
                from pathlib import Path
                prompt_tag = f"  prompt={Path(sp).name}"
            else:
                short = sp[:60].replace("\n", " ")
                prompt_tag = f"  prompt=\"{short}{'...' if len(sp) > 60 else ''}\""

        print_info(f"  {defn.name}  [{status}]{model_tag}{iter_tag}{prompt_tag}")
        # Wrap description at 72 chars for readability.
        desc = defn.description
        if len(desc) > 72:
            desc = desc[:69] + "..."
        print_info(f"    {desc}")
        print()

    print_info("Manage: claw subagent add | remove <name> | toggle <name>")
    return True


# ─── claw subagent show ───────────────────────────────────────────────────────

def _cmd_show(_args: argparse.Namespace) -> bool:
    """Print global sub-agent settings."""
    from agent.config import load_agent_config

    cfg = load_agent_config()
    print_header("Sub-agent Global Settings")
    _print_global_cfg(cfg)
    return True


def _print_global_cfg(cfg) -> None:
    status = "✓ enabled" if cfg.subagent_enabled else "✗ disabled"
    print_info(f"  subagent_enabled       : {status}")
    print_info(f"  subagent_max_iterations: {cfg.subagent_max_iterations}")
    print_info(f"  subagent_max_depth     : {cfg.subagent_max_depth}")
    print_info(f"  configured sub-agents  : {len(cfg.subagents)}")
    print()
    print_info("Source: ~/.claw/config.json  (agent.subagents)")


# ─── claw subagent enable / disable ──────────────────────────────────────────

def _cmd_enable(_args: argparse.Namespace) -> bool:
    return _set_global_enabled(True)


def _cmd_disable(_args: argparse.Namespace) -> bool:
    return _set_global_enabled(False)


def _set_global_enabled(value: bool) -> bool:
    from dataclasses import replace
    from agent.config import load_agent_config, save_agent_config

    cfg = load_agent_config()
    if cfg.subagent_enabled == value:
        print_info(f"Sub-agents are already {'enabled' if value else 'disabled'}.")
        return True
    cfg = replace(cfg, subagent_enabled=value)
    if save_agent_config(cfg):
        print_success(f"Sub-agents {'enabled' if value else 'disabled'}.")
        return True
    print_error("Failed to save configuration.")
    return False


# ─── claw subagent config ─────────────────────────────────────────────────────

def _cmd_config(_args: argparse.Namespace) -> bool:
    """Interactively update global sub-agent knobs."""
    from dataclasses import replace
    from agent.config import load_agent_config, save_agent_config

    cfg = load_agent_config()
    print_header("Sub-agent Global Settings")
    _print_global_cfg(cfg)
    print()

    try:
        enabled = prompt_yes_no("Enable sub-agents?", default=cfg.subagent_enabled)

        raw_iter = prompt_text(
            "Max iterations per sub-agent",
            default=str(cfg.subagent_max_iterations),
        ).strip()
        try:
            max_iter = int(raw_iter)
            if max_iter <= 0:
                raise ValueError
        except ValueError:
            print_error(f"Invalid value '{raw_iter}'; keeping {cfg.subagent_max_iterations}.")
            max_iter = cfg.subagent_max_iterations

        raw_depth = prompt_text(
            "Max recursion depth",
            default=str(cfg.subagent_max_depth),
        ).strip()
        try:
            max_depth = int(raw_depth)
            if max_depth <= 0:
                raise ValueError
        except ValueError:
            print_error(f"Invalid value '{raw_depth}'; keeping {cfg.subagent_max_depth}.")
            max_depth = cfg.subagent_max_depth

    except (KeyboardInterrupt, EOFError):
        print()
        print_info("Cancelled.")
        return False

    cfg = replace(cfg, subagent_enabled=enabled,
                  subagent_max_iterations=max_iter, subagent_max_depth=max_depth)
    if save_agent_config(cfg):
        print()
        print_success("Configuration saved.")
        _print_global_cfg(cfg)
        return True
    print_error("Failed to save configuration.")
    return False


# ─── claw subagent add ────────────────────────────────────────────────────────

def _cmd_add(_args: argparse.Namespace) -> bool:
    """Interactively add a new named sub-agent to config."""
    from agent.config import SubAgentDef, load_agent_config, save_agent_config

    print_header("Add Sub-agent")
    try:
        name = prompt_text("Tool name (e.g. claw_sql_review)").strip()
        if not name:
            print_error("Name is required.")
            return False
        # Normalise: spaces → underscores, lowercase.
        name = name.replace(" ", "_").lower()
        if not name.startswith("claw_"):
            name = f"claw_{name}"

        cfg = load_agent_config()
        existing = [d.name for d in cfg.subagents]
        if name in existing:
            print_error(f"A sub-agent named '{name}' already exists. Use 'toggle' or 'remove' first.")
            return False

        description = prompt_text(
            "Description (tells the LLM when to call this tool)"
        ).strip()
        if not description:
            print_error("Description is required.")
            return False

        system_prompt = prompt_text(
            "System prompt (inline text or path to .md file, blank to skip)",
            default="",
        ).strip()

        model = prompt_text(
            "Model override (blank to use parent's model)",
            default="",
        ).strip() or None

        raw_iter = prompt_text(
            "Max iterations (blank for global default)",
            default="",
        ).strip()
        max_iterations: int | None = None
        if raw_iter:
            try:
                max_iterations = int(raw_iter)
                if max_iterations <= 0:
                    raise ValueError
            except ValueError:
                print_warning(f"Invalid '{raw_iter}'; using global default.")
                max_iterations = None

    except (KeyboardInterrupt, EOFError):
        print()
        print_info("Cancelled.")
        return False

    defn = SubAgentDef(
        name=name,
        description=description,
        system_prompt=system_prompt,
        model=model,
        max_iterations=max_iterations,
        enabled=True,
    )

    from dataclasses import replace as dc_replace
    cfg = dc_replace(cfg, subagents=list(cfg.subagents) + [defn])
    if save_agent_config(cfg):
        print_success(f"Sub-agent '{name}' added.")
        print_info("It will be registered as a native tool on the next 'claw chat' session.")
        return True
    print_error("Failed to save configuration.")
    return False


# ─── claw subagent remove ─────────────────────────────────────────────────────

def _cmd_remove(args: argparse.Namespace) -> bool:
    """Remove a named sub-agent from config."""
    from dataclasses import replace as dc_replace
    from agent.config import load_agent_config, save_agent_config

    name = args.name.strip()
    cfg = load_agent_config()
    match = [d for d in cfg.subagents if d.name == name]
    if not match:
        print_error(f"No sub-agent named '{name}'.")
        _print_names(cfg)
        return False

    if not prompt_yes_no(f"Remove sub-agent '{name}'?", default=False):
        print_info("Cancelled.")
        return False

    updated = [d for d in cfg.subagents if d.name != name]
    cfg = dc_replace(cfg, subagents=updated)
    if save_agent_config(cfg):
        print_success(f"Sub-agent '{name}' removed.")
        return True
    print_error("Failed to save configuration.")
    return False


# ─── claw subagent toggle ─────────────────────────────────────────────────────

def _cmd_toggle(args: argparse.Namespace) -> bool:
    """Toggle enabled/disabled for one named sub-agent."""
    from dataclasses import replace as dc_replace
    from agent.config import load_agent_config, save_agent_config

    name = args.name.strip()
    cfg = load_agent_config()
    idx = next((i for i, d in enumerate(cfg.subagents) if d.name == name), None)
    if idx is None:
        print_error(f"No sub-agent named '{name}'.")
        _print_names(cfg)
        return False

    defn = cfg.subagents[idx]
    new_defn = dc_replace(defn, enabled=not defn.enabled)
    updated = list(cfg.subagents)
    updated[idx] = new_defn
    cfg = dc_replace(cfg, subagents=updated)
    if save_agent_config(cfg):
        state = "enabled" if new_defn.enabled else "disabled"
        print_success(f"Sub-agent '{name}' {state}.")
        return True
    print_error("Failed to save configuration.")
    return False


def _print_names(cfg) -> None:
    if cfg.subagents:
        print_info("Configured: " + ", ".join(d.name for d in cfg.subagents))
    else:
        print_info("No named sub-agents configured. Run 'claw subagent add'.")


# ─── claw subagent run ────────────────────────────────────────────────────────

def _cmd_run(args: argparse.Namespace) -> bool:
    """Run a named sub-agent one-shot and stream output to stdout."""
    from agent.config import load_agent_config
    from agent.compressor import ContextCompressor
    from agent.guardrails import GuardrailsController
    from agent.llm_client import LLMClient
    from agent.loop import _default_token_estimator as token_estimator
    from agent.loop import AgentLoop
    from agent.subagent import _NoopPersistence, SpecializedSubAgent, SubAgentRunner
    from agent.title_generator import TitleGenerator
    from agent.tool_dispatch import ToolDispatcher
    from agent.tool_registry import ToolRegistry
    from cli.config import get_config_value, get_env_value
    from cli.providers import PROVIDER_INFO
    from gateway.config import load_gateway_config
    from gateway.mcp_client import McpManager

    name = args.name.strip()
    task = " ".join(args.task).strip()
    if not task:
        print_error("Task cannot be empty.")
        return False

    cfg = load_agent_config()
    if not cfg.subagent_enabled:
        print_warning("Sub-agents are disabled. Run 'claw subagent enable' first.")
        return False

    # Locate the definition (or use generic runner if name == "claw_subagent").
    defn = None
    if name != "claw_subagent":
        defn = next((d for d in cfg.subagents if d.name == name), None)
        if defn is None:
            print_error(f"No sub-agent named '{name}'.")
            _print_names(cfg)
            return False
        if not defn.enabled:
            print_warning(f"Sub-agent '{name}' is disabled. Run 'claw subagent toggle {name}' to enable.")
            return False

    # Resolve model + API key.
    model_override = getattr(args, "model", None)
    provider = get_config_value("model.provider")
    configured_model = get_config_value("model.model")
    if not provider or not configured_model:
        print_error("No model configured. Run 'claw models' first.")
        return False
    model = model_override or configured_model
    provider_info = PROVIDER_INFO.get(provider)
    if not provider_info:
        print_error(f"Unknown provider '{provider}'. Run 'claw models' to reconfigure.")
        return False
    base_url = provider_info["base_url"]
    api_key_env = provider_info["api_key_env"]
    api_key = get_env_value(api_key_env) or ""
    if not api_key:
        print_error(f"API key not found. Set the {api_key_env} environment variable.")
        return False

    llm = LLMClient(base_url=base_url, api_key=api_key, model=model)

    gw_cfg = load_gateway_config()
    mcp = McpManager()
    for srv_name, srv in gw_cfg.mcp_servers.items():
        if srv.enabled:
            mcp.add_server(srv)
    start_results = mcp.start_all()
    failed = [n for n, ok in start_results.items() if not ok]
    if failed:
        print_warning(f"MCP servers failed to start: {', '.join(failed)}")

    registry = ToolRegistry(mcp)
    registry.reload_from_mcp()

    guardrails = GuardrailsController(mode=cfg.guardrails_mode)
    dispatcher = ToolDispatcher(
        registry=registry, mcp=mcp, guardrails=guardrails,
        max_workers=cfg.max_tool_workers, timeout_s=cfg.tool_call_timeout_seconds,
    )
    compressor = ContextCompressor(llm=llm, agent_cfg=cfg, token_estimator=token_estimator)
    title_gen = TitleGenerator(llm)
    persistence = _NoopPersistence(model)
    session = persistence.session

    parent_loop = AgentLoop(
        cfg=cfg, llm=llm, mcp=mcp, registry=registry,
        dispatcher=dispatcher, compressor=compressor,
        guardrails=guardrails, persistence=persistence,
        session=session, token_estimator=token_estimator,
        title_generator=title_gen, on_text_delta=_stdout_writer,
    )
    parent_loop._provider = provider  # noqa: SLF001

    # Register all enabled named sub-agents on the parent loop's registry
    # so they are available if a sub-agent invokes another sub-agent.
    parent_loop.register_subagent_tool()

    sys_prompt_override: str | None = getattr(args, "system_prompt", None)
    max_iter_override: int | None = getattr(args, "max_iterations", None)

    try:
        if defn is not None:
            agent = SpecializedSubAgent(parent_loop, defn)
            result = agent({"task": task, "stream": True})
        else:
            runner = SubAgentRunner(parent_loop)
            result = runner.run(
                task,
                system_prompt=sys_prompt_override,
                model=model_override,
                max_iterations=max_iter_override,
                stream=True,
            )
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return False
    finally:
        mcp.stop_all()

    if not result.endswith("\n"):
        print()
    if result.startswith("[subagent_error]") or (
        result.startswith("[") and "_error]" in result.split("]", 1)[0]
    ):
        print_error(result)
        return False
    return True


def _stdout_writer(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


# ─── claw subagent schema ─────────────────────────────────────────────────────

def _cmd_schema(args: argparse.Namespace) -> bool:
    """Print tool schema for a named sub-agent, or all if none specified."""
    from agent.config import load_agent_config
    from agent.subagent import (
        SUBAGENT_TOOL_DESCRIPTION,
        SUBAGENT_TOOL_NAME,
        SUBAGENT_TOOL_SCHEMA,
        SpecializedSubAgent,
    )

    name: str | None = getattr(args, "name", None)
    cfg = load_agent_config()

    def _print_schema(tool_name: str, description: str, parameters: dict) -> None:
        payload = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": description,
                "parameters": parameters,
            },
        }
        print(json.dumps(payload, indent=2))

    if name is None or name == SUBAGENT_TOOL_NAME:
        _print_schema(SUBAGENT_TOOL_NAME, SUBAGENT_TOOL_DESCRIPTION, SUBAGENT_TOOL_SCHEMA)
        if name is not None:
            return True

    for defn in cfg.subagents:
        if name is not None and defn.name != name:
            continue
        _print_schema(
            defn.name,
            defn.description,
            SpecializedSubAgent.build_schema(defn),
        )
        if name is not None:
            return True

    if name is not None:
        print_error(f"No sub-agent named '{name}'.")
        return False
    return True


# ─── Parser registration ──────────────────────────────────────────────────────

def register_subagent_parser(subparsers: argparse._SubParsersAction) -> None:
    sa_parser = subparsers.add_parser(
        "subagent",
        help="Manage configured sub-agents",
        description=(
            "List, add, remove, and invoke config-driven sub-agents. "
            "Sub-agents are defined in ~/.claw/config.json under agent.subagents."
        ),
    )
    sa_sub = sa_parser.add_subparsers(dest="subagent_action")

    # list
    sa_sub.add_parser("list", help="List all configured sub-agents")

    # show
    sa_sub.add_parser("show", help="Show global sub-agent settings")

    # enable / disable
    sa_sub.add_parser("enable", help="Enable sub-agents globally")
    sa_sub.add_parser("disable", help="Disable sub-agents globally")

    # config
    sa_sub.add_parser("config", help="Interactively update global settings")

    # add
    sa_sub.add_parser("add", help="Add a new named sub-agent interactively")

    # remove
    rem = sa_sub.add_parser("remove", help="Remove a named sub-agent")
    rem.add_argument("name", help="Tool name to remove (e.g. claw_python_review)")

    # toggle
    tog = sa_sub.add_parser("toggle", help="Enable/disable a named sub-agent")
    tog.add_argument("name", help="Tool name to toggle")

    # run
    run_parser = sa_sub.add_parser("run", help="Run a named sub-agent one-shot")
    run_parser.add_argument("name", help="Tool name (e.g. claw_python_review) or 'claw_subagent'")
    run_parser.add_argument("task", nargs="+", help="Task description")
    run_parser.add_argument("--model", metavar="MODEL_ID", default=None,
                            help="Override model for this run")
    run_parser.add_argument("--max-iterations", dest="max_iterations", type=int,
                            default=None, metavar="N")
    run_parser.add_argument("--system-prompt", dest="system_prompt", default=None,
                            metavar="TEXT", help="System prompt (claw_subagent only)")

    # schema
    schema_parser = sa_sub.add_parser("schema", help="Print tool schema as JSON")
    schema_parser.add_argument("name", nargs="?", default=None,
                               help="Tool name; omit to print all schemas")


def run_subagent_command(args: argparse.Namespace) -> bool:
    action = getattr(args, "subagent_action", None)
    dispatch = {
        "list":    _cmd_list,
        "show":    _cmd_show,
        "enable":  _cmd_enable,
        "disable": _cmd_disable,
        "config":  _cmd_config,
        "add":     _cmd_add,
        "remove":  _cmd_remove,
        "toggle":  _cmd_toggle,
        "run":     _cmd_run,
        "schema":  _cmd_schema,
    }
    fn = dispatch.get(action or "list")
    return fn(args) if fn else _cmd_list(args)
