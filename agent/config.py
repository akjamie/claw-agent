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

Missing keys silently fall back to the defaults defined here (Req 14.3).
Invalid keys (wrong type or out-of-range) also fall back to the default,
but emit one stderr warning naming the offending key (Req 14.4).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields, replace
from typing import Any, Dict


# Sentinel returned by validators when the user-supplied value is rejected.
_INVALID = object()


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
    }
    return save_config(full_cfg)


__all__ = ["AgentConfig", "load_agent_config", "save_agent_config"]
