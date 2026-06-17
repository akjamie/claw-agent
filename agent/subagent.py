"""
Sub-agent runner — nested AgentLoop invocation as a native tool.

:class:`SubAgentRunner` is the generic engine. It wraps the parent
:class:`agent.loop.AgentLoop` and exposes a :meth:`run` method that:

1. Guards against infinite recursion via a ``_depth`` counter bounded by
   ``AgentConfig.subagent_max_depth``.
2. Constructs a child ``AgentLoop`` sharing the parent's ``LLMClient``,
   ``ToolRegistry``, ``ToolDispatcher``, and ``McpManager``, with fresh
   ``GuardrailsController`` and ``ContextCompressor`` instances.
3. Runs the child loop with an isolated history (no parent context is
   visible unless explicitly included in ``task`` or ``system_prompt``).
4. Returns the child's final assistant text as a plain string.

:class:`SpecializedSubAgent` is the config-driven wrapper. It holds a
:class:`agent.config.SubAgentDef` (loaded from ``~/.claw/config.json``)
and delegates to ``SubAgentRunner`` with the definition's pre-baked
system prompt, model, and iteration budget.  There are **no hardcoded
sub-agent classes** — every named sub-agent is a data entry in config.

Registration
------------
:meth:`AgentLoop.register_subagent_tool` registers:

- ``claw_subagent`` — the generic tool the LLM uses to spin up any
  isolated sub-task it invents itself.
- One tool per enabled :class:`SubAgentDef` in ``AgentConfig.subagents``
  — e.g. ``claw_python_review`` if that entry exists in config.

Design notes
------------
- Child loops use :class:`_NoopPersistence` so sub-agent turns never
  appear in the user's session history.
- Child ``GuardrailsController`` instances have a fresh ledger.
- The child interrupt event is independent of the parent's.
- ``stream=True`` forwards deltas to the parent's ``on_text_delta``
  callback with a ``[<tool_name>] `` prefix.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Callable, List, Optional

from agent.llm_client import LLMClient, ProviderHTTPError
from agent.messages import Message
from agent.persistence import PersistenceFailure

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.config import AgentConfig, SubAgentDef
    from agent.loop import AgentLoop
    from agent.persistence import Session

__all__ = [
    "SubAgentRunner",
    "SpecializedSubAgent",
    "SUBAGENT_TOOL_NAME",
    "SUBAGENT_TOOL_SCHEMA",
    "SUBAGENT_TOOL_DESCRIPTION",
]

logger = logging.getLogger(__name__)

# ── Generic claw_subagent tool ────────────────────────────────────────────────

SUBAGENT_TOOL_NAME = "claw_subagent"

SUBAGENT_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": (
                "Full description of the task for the sub-agent to execute. "
                "Be specific — the sub-agent has no access to the parent "
                "conversation history."
            ),
        },
        "system_prompt": {
            "type": "string",
            "description": (
                "Optional system-level instructions prepended to the "
                "sub-agent's context (e.g. role, output format constraints)."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Optional model override (e.g. 'openai/gpt-4o-mini'). "
                "Defaults to the parent agent's model."
            ),
        },
        "max_iterations": {
            "type": "integer",
            "description": (
                "Optional iteration budget override. "
                "Defaults to agent config subagent_max_iterations."
            ),
        },
        "stream": {
            "type": "boolean",
            "description": (
                "If true, forward the sub-agent's token stream to the "
                "terminal with a '[subagent] ' prefix so the user can watch."
            ),
        },
    },
    "required": ["task"],
}

SUBAGENT_TOOL_DESCRIPTION = (
    "Delegate a self-contained task to a sub-agent that runs with its own "
    "isolated context window. Use this when a sub-task is large enough to "
    "pollute the main context, when you need independent research before "
    "acting, or when you want to draft and review work separately. "
    "The sub-agent has access to all the same MCP tools. "
    "Returns the sub-agent's final text response."
)


# ── Core runner ───────────────────────────────────────────────────────────────

class SubAgentRunner:
    """Generic engine that runs a nested AgentLoop and returns its output.

    One instance is created per parent ``AgentLoop`` and registered as
    the ``claw_subagent`` native tool.  It is also reused internally by
    :class:`SpecializedSubAgent` for config-driven named sub-agents.

    Not thread-safe across concurrent ``run`` calls — safe in practice
    because all sub-agent tools are classified ``_NEVER_PARALLEL``.
    """

    def __init__(self, parent: "AgentLoop") -> None:
        self._parent = parent
        self._depth: int = getattr(parent, "_subagent_depth", 0)

    # ── Native-tool callable ──────────────────────────────────────────

    def __call__(self, args: dict) -> str:
        task = args.get("task", "").strip()
        if not task:
            return "[subagent_error] 'task' argument is required and must not be empty."

        system_prompt: Optional[str] = args.get("system_prompt") or None
        model_override: Optional[str] = args.get("model") or None
        max_iterations_override: Optional[int] = None
        raw_max_iter = args.get("max_iterations")
        if isinstance(raw_max_iter, int) and raw_max_iter > 0:
            max_iterations_override = raw_max_iter
        stream: bool = bool(args.get("stream", False))

        return self.run(
            task,
            system_prompt=system_prompt,
            model=model_override,
            max_iterations=max_iterations_override,
            stream=stream,
        )

    # ── Core run method ───────────────────────────────────────────────

    def run(
        self,
        task: str,
        *,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_iterations: Optional[int] = None,
        stream: bool = False,
        stream_prefix: str = "[subagent] ",
    ) -> str:
        """Run a sub-agent and return its final assistant text.

        Args:
            task: The user message sent to the child loop.
            system_prompt: Optional system message prepended before task.
            model: Model identifier override; ``None`` reuses parent's.
            max_iterations: Iteration budget override.
            stream: When ``True``, forward text deltas to the parent's
                ``on_text_delta`` with ``stream_prefix``.
            stream_prefix: Terminal prefix when streaming, e.g.
                ``"[code_review] "``.

        Returns:
            Final assistant content as a plain string, or a
            ``[subagent_error] …`` string on failure.
        """
        parent = self._parent
        cfg: "AgentConfig" = parent._cfg  # noqa: SLF001

        if self._depth >= cfg.subagent_max_depth:
            return (
                f"[subagent_error] maximum sub-agent recursion depth "
                f"({cfg.subagent_max_depth}) reached."
            )

        if not cfg.subagent_enabled:
            return (
                "[subagent_error] sub-agents are disabled "
                "(agent.subagent_enabled = false in config)."
            )

        child_cfg = replace(
            cfg,
            max_iterations=max_iterations or cfg.subagent_max_iterations,
        )

        # LLM — reuse parent's or build a new one for the model override.
        child_llm = parent._llm  # noqa: SLF001
        if model is not None:
            child_llm = LLMClient(
                base_url=parent._llm.base_url,  # noqa: SLF001
                api_key=parent._llm._api_key,   # noqa: SLF001
                model=model,
            )

        from agent.guardrails import GuardrailsController
        child_guardrails = GuardrailsController(mode=cfg.guardrails_mode)

        from agent.tool_dispatch import ToolDispatcher
        child_dispatcher = ToolDispatcher(
            registry=parent._registry,  # noqa: SLF001
            mcp=parent._mcp,            # noqa: SLF001
            guardrails=child_guardrails,
            max_workers=cfg.max_tool_workers,
            timeout_s=cfg.tool_call_timeout_seconds,
        )

        from agent.compressor import ContextCompressor
        child_compressor = ContextCompressor(
            llm=child_llm,
            agent_cfg=child_cfg,
            token_estimator=parent._token_estimator,  # noqa: SLF001
        )

        child_persistence = _NoopPersistence(model=child_llm.model)
        child_session = child_persistence.session

        # Streaming callback — prefix every delta with stream_prefix.
        parent_on_delta: Optional[Callable[[str], None]] = (
            parent._on_text_delta  # noqa: SLF001
        )
        child_on_delta: Optional[Callable[[str], None]] = None
        if stream and parent_on_delta is not None:
            # Default-arg capture hack: Python closures bind free variables
            # by reference, so parent_on_delta / stream_prefix would reflect
            # rebinding on re-entry.  Binding them as default args captures
            # the current values at definition time.
            def child_on_delta(  # noqa: E306
                text: str,
                _cb: Callable[[str], None] = parent_on_delta,
                _pfx: str = stream_prefix,
            ) -> None:
                _cb(f"{_pfx}{text}")

        from agent.loop import AgentLoop
        child_loop = AgentLoop(
            cfg=child_cfg,
            llm=child_llm,
            mcp=parent._mcp,               # noqa: SLF001
            registry=parent._registry,     # noqa: SLF001
            dispatcher=child_dispatcher,
            compressor=child_compressor,
            guardrails=child_guardrails,
            persistence=child_persistence,
            session=child_session,
            token_estimator=parent._token_estimator,  # noqa: SLF001
            title_generator=None,
            on_text_delta=child_on_delta,
            on_status=None,
        )
        child_loop._subagent_depth = self._depth + 1  # noqa: SLF001

        # Re-register the generic claw_subagent on the child so it can
        # recurse (depth guard will fire at max_depth).
        child_runner = SubAgentRunner(child_loop)
        parent._registry.register_native(  # noqa: SLF001
            SUBAGENT_TOOL_NAME,
            SUBAGENT_TOOL_DESCRIPTION,
            SUBAGENT_TOOL_SCHEMA,
            child_runner,
        )

        if system_prompt:
            child_loop.history.append(
                Message(role="system", content=system_prompt)
            )

        accumulated: List[str] = []
        if not stream:
            def _collect(text: str) -> None:
                accumulated.append(text)
            child_loop._on_text_delta = _collect  # noqa: SLF001

        try:
            child_loop.run_turn(task)
        except (ProviderHTTPError, PersistenceFailure) as exc:
            logger.error("Sub-agent run_turn failed: %s: %s", type(exc).__name__, exc)
            return f"[subagent_error] {type(exc).__name__}: {exc}"
        finally:
            # Restore the generic runner on the parent registry so the
            # parent can spawn more sub-agents after this one finishes.
            parent._registry.register_native(  # noqa: SLF001
                SUBAGENT_TOOL_NAME,
                SUBAGENT_TOOL_DESCRIPTION,
                SUBAGENT_TOOL_SCHEMA,
                self,
            )

        final = _extract_final_response(child_loop.history)
        if final is None:
            if accumulated:
                return "".join(accumulated)
            return "[subagent_error] sub-agent produced no response."
        return final


# ── Config-driven specialized sub-agent ──────────────────────────────────────

class SpecializedSubAgent:
    """Data-driven native tool handler built from a :class:`SubAgentDef`.

    Rather than baking any domain knowledge into Python code, every named
    sub-agent is described entirely in ``~/.claw/config.json``:

    .. code-block:: json

        {
          "agent": {
            "subagents": [
              {
                "name": "claw_python_review",
                "description": "...",
                "system_prompt": "personas/python-review.md",
                "enabled": true
              }
            ]
          }
        }

    ``SpecializedSubAgent`` resolves the system prompt text at call time
    (inline string or file path via
    :meth:`SubAgentDef.resolve_system_prompt`), then delegates to
    :class:`SubAgentRunner` with the definition's model and iteration
    budget overrides applied.

    The tool's JSON input schema is derived from the definition: a single
    required ``task`` field plus an optional ``stream`` flag.
    """

    def __init__(self, parent: "AgentLoop", defn: "SubAgentDef") -> None:
        self._parent = parent
        self._defn = defn
        self._runner = SubAgentRunner(parent)

    @staticmethod
    def build_schema(defn: "SubAgentDef") -> dict:
        """Build the OpenAI input schema for this sub-agent's tool."""
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The task or content to pass to this sub-agent. "
                        "Be specific — the sub-agent has no access to the "
                        "parent conversation history."
                    ),
                },
                "stream": {
                    "type": "boolean",
                    "description": (
                        f"If true, forward the [{defn.name}] token stream "
                        "to the terminal so the user can watch."
                    ),
                },
            },
            "required": ["task"],
        }

    def __call__(self, args: dict) -> str:
        task = args.get("task", "").strip()
        if not task:
            return f"[{self._defn.name}_error] 'task' argument is required."

        stream: bool = bool(args.get("stream", False))
        system_prompt = self._defn.resolve_system_prompt()

        result = self._runner.run(
            task,
            system_prompt=system_prompt or None,
            model=self._defn.model,
            max_iterations=self._defn.max_iterations,
            stream=stream,
            stream_prefix=f"[{self._defn.name}] ",
        )

        # Remap generic subagent_error prefix to the tool's own name so
        # the parent LLM can distinguish which sub-agent failed.
        if result.startswith("[subagent_error]"):
            return result.replace(
                "[subagent_error]",
                f"[{self._defn.name}_error]",
                1,
            )
        return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_final_response(history: List[Message]) -> Optional[str]:
    """Return the last non-empty assistant message from ``history``."""
    for msg in reversed(history):
        if msg.role == "assistant" and msg.content:
            return msg.content
    return None


class _NoopPersistence:
    """Minimal stub so child AgentLoops don't write to the user's SQLite DB.

    Implements every method that :class:`agent.loop.AgentLoop` and
    :class:`agent.compressor.ContextCompressor` call on the persistence
    object, including ``persist_summary`` — omitting it would cause an
    ``AttributeError`` when context compression fires in a child loop.
    """

    def __init__(self, model: str) -> None:
        from agent.persistence import Session

        sid = str(uuid.uuid4())
        now = int(time.time())
        self.session = Session(
            id=sid,
            short_id=sid[:4],
            title="",
            created_at=now,
            updated_at=now,
            model=model,
            total_tokens=0,
        )

    def append_messages(
        self,
        session_id: str,  # noqa: ARG002
        messages: list,
        total_tokens: int,  # noqa: ARG002
    ) -> list:
        return messages

    def get_session(self, session_id: str) -> "Session":  # noqa: ARG002
        return self.session

    def create_session(self, model: str) -> "Session":  # noqa: ARG002
        return self.session

    def load_recent_messages(
        self,
        session_id: str,  # noqa: ARG002
        *,
        limit: int = 500,  # noqa: ARG002
    ) -> list:
        return []

    def update_title(
        self,
        session_id: str,  # noqa: ARG002
        title: str,  # noqa: ARG002
    ) -> None:
        pass

    def persist_summary(
        self,
        session_id: str,  # noqa: ARG002
        message: object,  # noqa: ARG002
    ) -> None:
        """No-op: compression summaries are not persisted for sub-agent sessions."""
        pass

    def initialize(self) -> None:
        pass

    def list_sessions(self) -> list:
        return []
