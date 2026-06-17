"""
Agent configuration — `AgentConfig` dataclass and load/save helpers.

`AgentConfig` mirrors the `agent` object inside `~/.claw/config.json` (with
project-local `config.json` taking precedence per the rules in `cli.config`).

Validation ranges per design §"agent.config" and Requirements 6.1, 7.8,
8.1, 9.8, 10.1, 14.1–14.4:

- ``max_iterations``                 — positive int (default 90)
- ``context_compression_threshold``  — float in the open interval (0.0, 1.0)
                                       (default 0.70)
- ``protected_tail_fraction``        — float in (0.0, 1.0) (default 0.30)
- ``max_tool_workers``               — positive int (default 4)
- ``tool_call_timeout_seconds``      — positive int (default 300)
- ``guardrails_mode``                — exactly ``"warn"`` or ``"enforce"``
                                       (default ``"warn"``)
- ``default_context_window``         — positive int (default 32_768)
- ``model_context_windows``          — dict[str, positive int] (default {})
- ``summary_floor_tokens``           — positive int (default 2_000)
- ``summary_cap_tokens``             — positive int (default 12_000)
- ``summary_fraction``               — float in (0.0, 1.0) (default 0.20)
- ``subagent_enabled``               — bool (default True)
- ``subagent_max_iterations``        — positive int (default 20)
- ``subagent_max_depth``             — positive int (default 3)
- ``subagents``                      — list of SubAgentDef (default: [python-review])

Missing keys silently fall back to the defaults defined here (Req 14.3).
Invalid keys (wrong type or out-of-range) also fall back to the default,
but emit one stderr warning naming the offending key (Req 14.4).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Dict, List, Optional


# Sentinel returned by validators when the user-supplied value is rejected.
_INVALID = object()


@dataclass
class SubAgentDef:
    """Config-driven definition of one named sub-agent tool.

    Each entry in ``AgentConfig.subagents`` becomes a native tool
    registered with the LLM.  The LLM calls it automatically when the
    task matches the ``description``.

    Fields
    ------
    name:
        Tool name as the LLM sees it (e.g. ``claw_python_review``).
        Must be a valid identifier (no spaces).
    description:
        One or two sentences telling the LLM *when* to call this tool
        and what it returns.  This is the most important field for
        auto-triggering.
    system_prompt:
        The system message prepended to the sub-agent's context.
        Can be an inline string or a path to a ``.md``/``.txt`` file
        (resolved relative to ``~/.claw/`` first, then as an absolute /
        CWD-relative path).
    model:
        Optional model override for this sub-agent.  ``None`` means
        reuse the parent's model.
    max_iterations:
        Optional iteration budget override.  ``None`` uses
        ``AgentConfig.subagent_max_iterations``.
    enabled:
        When ``False`` the tool is not registered and is invisible to
        the LLM.  Defaults to ``True``.
    """

    name: str
    description: str
    system_prompt: str = ""
    model: Optional[str] = None
    max_iterations: Optional[int] = None
    enabled: bool = True

    def resolve_system_prompt(self) -> str:
        """Return the system prompt text, loading from file if needed.

        If ``system_prompt`` looks like a file path (ends in ``.md`` or
        ``.txt``, or contains a path separator) the file is read.
        Resolution order:

        1. ``~/.claw/<value>``
        2. Absolute / CWD-relative path as given.

        Returns the raw string (inline or file contents), or an empty
        string if the file cannot be read.
        """
        sp = self.system_prompt.strip()
        if not sp:
            return ""

        lsp = sp.lower()
        looks_like_path = (
            lsp.endswith(".md")
            or lsp.endswith(".txt")
            or "/" in sp
            or "\\" in sp
        )
        if not looks_like_path:
            return sp  # inline text

        # Try ~/.claw/<value> first so users can drop prompts next to config.
        from cli.config import get_claw_home
        candidate = get_claw_home() / sp
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8").strip()
            except OSError:
                pass

        # Fall back to the path as given (absolute or CWD-relative).
        p = Path(sp)
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8").strip()
            except OSError:
                pass

        return ""  # file not found — sub-agent runs without a system prompt

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
        }
        if self.system_prompt:
            d["system_prompt"] = self.system_prompt
        if self.model is not None:
            d["model"] = self.model
        if self.max_iterations is not None:
            d["max_iterations"] = self.max_iterations
        return d

    @classmethod
    def from_dict(cls, raw: Any, index: int) -> Optional["SubAgentDef"]:
        """Parse one subagents list entry; return ``None`` on validation error."""
        if not isinstance(raw, dict):
            print(
                f"warning: agent config subagents[{index}] is not a dict; skipping",
                file=sys.stderr,
            )
            return None
        name = raw.get("name", "").strip()
        if not name:
            print(
                f"warning: agent config subagents[{index}] missing 'name'; skipping",
                file=sys.stderr,
            )
            return None
        description = raw.get("description", "").strip()
        if not description:
            print(
                f"warning: agent config subagents[{index}] ('{name}') missing 'description'; skipping",
                file=sys.stderr,
            )
            return None
        system_prompt = raw.get("system_prompt", "") or ""
        model = raw.get("model") or None
        raw_max_iter = raw.get("max_iterations")
        max_iterations: Optional[int] = None
        if raw_max_iter is not None:
            if isinstance(raw_max_iter, bool) or not isinstance(raw_max_iter, int) or raw_max_iter <= 0:
                print(
                    f"warning: agent config subagents[{index}] ('{name}') "
                    f"invalid max_iterations; using default",
                    file=sys.stderr,
                )
            else:
                max_iterations = raw_max_iter
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = True
        return cls(
            name=name,
            description=description,
            system_prompt=str(system_prompt),
            model=model,
            max_iterations=max_iterations,
            enabled=enabled,
        )


def _default_subagents() -> List[SubAgentDef]:
    """Return the built-in default sub-agent list.

    The python-review sub-agent references the bundled persona file via a
    relative path so it works without any user configuration.  The path
    stored in config is just ``personas/python-review.md``; ``resolve_system_prompt``
    resolves it against ``~/.claw/`` first, then falls back to the installed
    package file.
    """
    # Compute package-relative path so it still resolves when installed.
    _pkg_persona = Path(__file__).parent.parent / "cli" / "personas" / "python-review.md"
    _persona_ref = str(_pkg_persona) if _pkg_persona.is_file() else "personas/python-review.md"

    return [
        SubAgentDef(
            name="claw_python_review",
            description=(
                "Delegate a Python code review to a specialist sub-agent. "
                "Call this automatically after writing, modifying, or generating "
                "Python code that will be executed or committed. "
                "Returns a structured review with verdict and actionable fixes."
            ),
            system_prompt=_persona_ref,
            enabled=True,
        ),
    ]


@dataclass
class AgentConfig:
    """Runtime configuration for the chat agent.

    All defaults match design §"agent.config". See module docstring for
    the validation ranges enforced by :func:`load_agent_config`.
    """

    max_iterations: int = 90
    context_compression_threshold: float = 0.70
    protected_tail_fraction: float = 0.30
    max_tool_workers: int = 4
    tool_call_timeout_seconds: int = 300
    guardrails_mode: str = "warn"
    default_context_window: int = 32_768
    model_context_windows: Dict[str, int] = field(default_factory=dict)
    summary_floor_tokens: int = 2_000
    summary_cap_tokens: int = 12_000
    summary_fraction: float = 0.20
    subagent_enabled: bool = True
    subagent_max_iterations: int = 20
    subagent_max_depth: int = 3
    subagents: List[SubAgentDef] = field(default_factory=_default_subagents)


# ── Validators ────────────────────────────────────────────────────────────────
#
# Each returns either a normalised value of the right type, or the _INVALID
# sentinel. None of the validators raise.


def _validate_positive_int(raw: Any) -> Any:
    # bool is a subclass of int — reject it explicitly so True/False don't
    # masquerade as 1/0.
    if isinstance(raw, bool):
        return _INVALID
    if isinstance(raw, int) and raw > 0:
        return raw
    return _INVALID


def _validate_open_unit_float(raw: Any) -> Any:
    """Accept a number strictly between 0.0 and 1.0 (open interval)."""
    if isinstance(raw, bool):
        return _INVALID
    if isinstance(raw, (int, float)):
        value = float(raw)
        if 0.0 < value < 1.0:
            return value
    return _INVALID


def _validate_guardrails_mode(raw: Any) -> Any:
    if raw == "warn" or raw == "enforce":
        return raw
    return _INVALID


def _validate_model_context_windows(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return _INVALID
    out: Dict[str, int] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            return _INVALID
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            return _INVALID
        out[k] = v
    return out


# Map field name -> validator. Fields not listed are left at the dataclass
# default and are not currently configurable from the JSON file.
def _validate_bool(raw: Any) -> Any:
    if isinstance(raw, bool):
        return raw
    return _INVALID


def _validate_subagents(raw: Any) -> Any:
    """Parse the ``subagents`` list; return validated list or ``_INVALID``."""
    if not isinstance(raw, list):
        return _INVALID
    result: List[SubAgentDef] = []
    for i, entry in enumerate(raw):
        defn = SubAgentDef.from_dict(entry, i)
        if defn is not None:
            result.append(defn)
    # An empty list is valid — the user explicitly wants no sub-agents.
    return result


_VALIDATORS = {
    "max_iterations": _validate_positive_int,
    "context_compression_threshold": _validate_open_unit_float,
    "protected_tail_fraction": _validate_open_unit_float,
    "max_tool_workers": _validate_positive_int,
    "tool_call_timeout_seconds": _validate_positive_int,
    "guardrails_mode": _validate_guardrails_mode,
    "default_context_window": _validate_positive_int,
    "model_context_windows": _validate_model_context_windows,
    "summary_floor_tokens": _validate_positive_int,
    "summary_cap_tokens": _validate_positive_int,
    "summary_fraction": _validate_open_unit_float,
    "subagent_enabled": _validate_bool,
    "subagent_max_iterations": _validate_positive_int,
    "subagent_max_depth": _validate_positive_int,
    "subagents": _validate_subagents,
}


def _warn(key: str) -> None:
    """Emit a one-line stderr warning for an offending agent-config key."""
    print(
        f"warning: agent config key '{key}' has an invalid value; using default",
        file=sys.stderr,
    )


# ── Public API ────────────────────────────────────────────────────────────────


def load_agent_config() -> AgentConfig:
    """Load :class:`AgentConfig` from ``~/.claw/config.json``.

    Reads the ``agent`` sub-object via :func:`cli.config.load_config` (which
    already honours the project-local ``config.json`` precedence rule).
    Missing keys fall back to defaults silently. Keys with the wrong type or
    out-of-range values also fall back to defaults, but each offending key
    triggers one ``warning: ...`` line on stderr (Req 14.4).
    """
    # Imported lazily so test fixtures can monkeypatch cli.config.load_config
    # without a circular import at module load time.
    from cli.config import load_config

    full_cfg = load_config() or {}
    agent_cfg = full_cfg.get("agent")
    if not isinstance(agent_cfg, dict):
        # Either missing entirely or malformed (e.g. a list). Treat malformed
        # as "no agent config" rather than warning per-field, matching the
        # missing-key path of Req 14.3.
        return AgentConfig()

    overrides: Dict[str, Any] = {}
    for f in fields(AgentConfig):
        if f.name not in agent_cfg:
            continue  # missing → silently use the default
        validator = _VALIDATORS.get(f.name)
        if validator is None:
            continue
        validated = validator(agent_cfg[f.name])
        if validated is _INVALID:
            _warn(f.name)
            continue
        overrides[f.name] = validated

    return replace(AgentConfig(), **overrides)


def save_agent_config(cfg: AgentConfig) -> bool:
    """Write ``cfg`` back to the ``agent`` sub-object of ``~/.claw/config.json``.

    Returns ``True`` on success, ``False`` if the underlying write failed.
    Other top-level keys (e.g. ``model``) are preserved.
    """
    from cli.config import load_config, save_config

    full_cfg = load_config() or {}
    full_cfg["agent"] = {
        "max_iterations": cfg.max_iterations,
        "context_compression_threshold": cfg.context_compression_threshold,
        "protected_tail_fraction": cfg.protected_tail_fraction,
        "max_tool_workers": cfg.max_tool_workers,
        "tool_call_timeout_seconds": cfg.tool_call_timeout_seconds,
        "guardrails_mode": cfg.guardrails_mode,
        "default_context_window": cfg.default_context_window,
        "model_context_windows": dict(cfg.model_context_windows),
        "summary_floor_tokens": cfg.summary_floor_tokens,
        "summary_cap_tokens": cfg.summary_cap_tokens,
        "summary_fraction": cfg.summary_fraction,
        "subagent_enabled": cfg.subagent_enabled,
        "subagent_max_iterations": cfg.subagent_max_iterations,
        "subagent_max_depth": cfg.subagent_max_depth,
        "subagents": [sa.to_dict() for sa in cfg.subagents],
    }
    return save_config(full_cfg)


__all__ = ["AgentConfig", "SubAgentDef", "load_agent_config", "save_agent_config"]
