"""
Tool registry — bridges MCP tool definitions into the OpenAI function-tool
format expected by chat-completions providers.

The registry is built once at chat startup from a fully-started
:class:`gateway.mcp_client.McpManager`. After ``reload_from_mcp()`` the
registry holds one :class:`ToolDef` per discovered tool and can render the
OpenAI ``tools=[...]`` payload directly.

Design references:
- design.md §"agent.tool_registry" — public API and OpenAI mapping rules.
- design.md §"Components and Interfaces" — bridge from MCP to function-tool
  schemas with ``parameters = input_schema`` passed through unchanged.
- requirements.md §7.1, §7.2, §7.3 — discovery/registration + OpenAI shape.
- requirements.md §8.5 — MCP tools without explicit safety metadata default
  to ``_NEVER_PARALLEL``.

Safety classification:
The MCP protocol exposes no safety metadata, so every freshly registered
MCP tool is classified ``_NEVER_PARALLEL`` (Req 8.5). A future skill or
config layer can opt specific tools into ``_PARALLEL_SAFE`` or
``_PATH_SCOPED`` via :meth:`ToolRegistry.set_safety_override`. Overrides
survive a subsequent ``reload_from_mcp()``: when a tool with a known
override re-appears, its overridden classification is preserved; tools
that disappear from MCP simply drop out of the registry but their
override entries are retained so re-discovery later restores the choice.

This module is pure with respect to logging and I/O — the only I/O it
performs is via the injected ``McpManager``, which itself encapsulates
MCP subprocess interaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from gateway.mcp_client import McpManager, McpTool

__all__ = [
    "ToolDef",
    "ToolRegistry",
    "_NEVER_PARALLEL",
    "_PARALLEL_SAFE",
    "_PATH_SCOPED",
]


# ── Safety-class constants ────────────────────────────────────────────────────
#
# Names match design.md §"Components and Interfaces" → §"agent.tool_registry"
# and the Glossary's `Parallel_Safety_Class` definition. Kept as module-level
# strings so the dispatcher can compare by identity.

_NEVER_PARALLEL = "never_parallel"
_PARALLEL_SAFE = "parallel_safe"
_PATH_SCOPED = "path_scoped"

_VALID_SAFETY_CLASSES = frozenset({_NEVER_PARALLEL, _PARALLEL_SAFE, _PATH_SCOPED})


@dataclass(frozen=True)
class ToolDef:
    """One registered tool, in the form the agent runtime needs internally.

    Fields mirror the relevant subset of :class:`gateway.mcp_client.McpTool`
    plus the resolved safety classification.

    - ``name`` is the tool's MCP name and is also the key used by the
      LLM in tool-call ``function.name`` payloads.
    - ``description`` and ``input_schema`` come straight from the MCP
      tool definition. ``input_schema`` is a JSON-Schema fragment.
    - ``server_name`` records which MCP server exposed the tool; useful
      for diagnostics and for future per-server scheduling rules.
    - ``safety_class`` is one of the module-level safety constants and
      governs concurrency in :class:`agent.tool_dispatch.ToolDispatcher`.

    The dataclass is frozen so registry entries are hashable and safe to
    share across worker threads without copying.
    """

    name: str
    description: str
    input_schema: dict
    server_name: str
    safety_class: str


class ToolRegistry:
    """In-memory registry of MCP tools with OpenAI-format rendering.

    Construction does not touch the MCP manager; call
    :meth:`reload_from_mcp` once the manager has finished starting its
    server subprocesses (typically after ``McpManager.start_all()``).
    """

    def __init__(self, mcp: McpManager) -> None:
        self._mcp = mcp
        self._tools: Dict[str, ToolDef] = {}
        # Persistent override map keyed by tool name. Entries survive
        # `reload_from_mcp` so that a tool that briefly disappears (e.g.,
        # an MCP server crash + restart) reattaches to its override.
        self._safety_overrides: Dict[str, str] = {}

    # ── Loading ───────────────────────────────────────────────────────────────

    def reload_from_mcp(self) -> None:
        """Replace the registry's contents with the current MCP tool set.

        For each tool returned by ``McpManager.get_all_tools()``:
        - build a :class:`ToolDef` whose ``input_schema`` is passed
          through unchanged (Req 7.3);
        - resolve the safety class as the override (if any) or
          ``_NEVER_PARALLEL`` by default (Req 8.5).

        Names colliding across servers are resolved last-write-wins. The
        upstream ``McpManager.find_tool`` already follows this rule, so
        the registry stays consistent with how dispatch resolves tools.
        """
        new_tools: Dict[str, ToolDef] = {}
        for mcp_tool in self._mcp.get_all_tools():
            tool_def = self._build_tool_def(mcp_tool)
            new_tools[tool_def.name] = tool_def
        self._tools = new_tools

    def _build_tool_def(self, mcp_tool: McpTool) -> ToolDef:
        """Construct a :class:`ToolDef` from one :class:`McpTool`.

        Pulls the safety class from the override map when present;
        otherwise defaults to ``_NEVER_PARALLEL`` per Req 8.5. The
        ``input_schema`` field falls back to the empty JSON-Schema
        ``{}`` when the MCP server omits it, which the OpenAI-compatible
        providers accept as "no parameters".
        """
        safety = self._safety_overrides.get(mcp_tool.name, _NEVER_PARALLEL)
        # Defensive copy: the registry must not be perturbed if a caller
        # later mutates the original schema dict on the McpTool.
        schema = dict(mcp_tool.input_schema) if mcp_tool.input_schema else {}
        return ToolDef(
            name=mcp_tool.name,
            description=mcp_tool.description or "",
            input_schema=schema,
            server_name=mcp_tool.server_name or "",
            safety_class=safety,
        )

    # ── Lookup ────────────────────────────────────────────────────────────────

    def get(self, tool_name: str) -> Optional[ToolDef]:
        """Return the :class:`ToolDef` for ``tool_name`` or ``None``."""
        return self._tools.get(tool_name)

    def safety_class(self, tool_name: str) -> str:
        """Return the safety classification for ``tool_name``.

        Resolution order (so an override takes effect even before the
        first ``reload_from_mcp()``):

        1. The override map, when an entry exists.
        2. The registered :class:`ToolDef` if the tool has been loaded.
        3. ``_NEVER_PARALLEL`` as the conservative default (Req 8.5).
        """
        if tool_name in self._safety_overrides:
            return self._safety_overrides[tool_name]
        tool = self._tools.get(tool_name)
        if tool is not None:
            return tool.safety_class
        return _NEVER_PARALLEL

    # ── Mutation ──────────────────────────────────────────────────────────────

    def set_safety_override(self, name: str, cls: str) -> None:
        """Override the safety classification for tool ``name``.

        ``cls`` MUST be one of :data:`_NEVER_PARALLEL`, :data:`_PARALLEL_SAFE`,
        or :data:`_PATH_SCOPED`. Raises :class:`ValueError` otherwise so
        callers can't accidentally smuggle a typo into the dispatcher's
        scheduling logic.

        If the tool is already loaded into the registry, its
        :class:`ToolDef` is rebuilt with the new safety class so callers
        of :meth:`get` see the updated value immediately.
        """
        if cls not in _VALID_SAFETY_CLASSES:
            raise ValueError(
                f"unknown safety class '{cls}'; expected one of "
                f"{sorted(_VALID_SAFETY_CLASSES)!r}"
            )
        self._safety_overrides[name] = cls
        existing = self._tools.get(name)
        if existing is not None:
            self._tools[name] = ToolDef(
                name=existing.name,
                description=existing.description,
                input_schema=existing.input_schema,
                server_name=existing.server_name,
                safety_class=cls,
            )

    # ── Rendering ─────────────────────────────────────────────────────────────

    def openai_tools(self) -> List[dict]:
        """Render the registry as the OpenAI ``tools`` array.

        The returned list has one entry per registered tool, in the
        canonical OpenAI shape::

            {
                "type": "function",
                "function": {
                    "name": "<tool name>",
                    "description": "<description>",
                    "parameters": <input_schema passed through>
                }
            }

        Returning a freshly-built list (and freshly-built inner dicts)
        means callers can mutate the result without affecting the
        registry's internal state.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    # `parameters` is the JSON-Schema fragment from MCP,
                    # passed through unchanged per Req 7.3. We copy the
                    # dict so the outer payload is self-contained.
                    "parameters": dict(tool.input_schema),
                },
            }
            for tool in self._tools.values()
        ]

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, tool_name: object) -> bool:
        return isinstance(tool_name, str) and tool_name in self._tools
