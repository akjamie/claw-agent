"""
AgentLoop — per-turn orchestrator for the claw-agent runtime.

Owns the in-memory conversation ``history``, drives one synchronous
LLM call per Iteration, dispatches Tool_Calls, and writes every
mutation to SQLite before the next Iteration begins.

Per-turn algorithm (design §"agent.loop"):

1. Append the user Message to history; persist.
2. Loop iteration_count = 1, 2, ... :
   - Halt after ``max_iterations + 1`` (the grace iteration is consumed).
   - At iteration ``max_iterations + 1`` append exactly one budget-exhaustion
     notice (Req 6.3, Property 3).
   - When ``estimated_tokens > threshold * context_window``, run the
     compressor (Req 10.2). On success, replace history and persist the
     new summary Message.
   - Stream the LLM response; on :class:`ProviderHTTPError` append a
     system error Message and break.
   - Append the assistant Message; persist.
   - On no tool_calls → break.
   - On interrupt → append ``[claw_chat:interrupted]`` notice, break.
   - Dispatch tool batch; on :class:`GuardrailHalt` append the
     ``[claw_chat:guardrail_halt]`` notice and break (Req 9.5).
3. After the loop, if the Session has no title and there are at least
   two user Messages, run :meth:`TitleGenerator.generate` and persist
   the result (Req 4.1, 4.4).

Design references:
- design.md §"agent.loop", §"Error Message conventions",
  §"Error taxonomy", §"Crash recovery".
- requirements.md §1, §2, §4, §6, §7.4, §8.6, §10.2, §10.7, §13.4,
  §16, §18.3.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Callable, List, Optional

from agent.guardrails import GuardrailHalt
from agent.llm_client import ProviderHTTPError
from agent.messages import COMPRESSION_SUMMARY_TOOL_NAME, Message, ToolCall
from agent.persistence import PersistenceFailure

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.compressor import ContextCompressor
    from agent.config import AgentConfig
    from agent.guardrails import GuardrailsController
    from agent.llm_client import LLMClient, StreamResult, StreamStatus
    from agent.persistence import Session, SqlitePersistence
    from agent.title_generator import TitleGenerator
    from agent.tool_dispatch import ToolDispatcher
    from agent.tool_registry import ToolRegistry
    from gateway.mcp_client import McpManager

__all__ = ["AgentLoop"]

logger = logging.getLogger(__name__)


# Loop-level Message markers — design §"Error Message conventions".
_BUDGET_MARKER = "[claw_chat:budget_exhausted]"
_INTERRUPT_MARKER = "[claw_chat:interrupted]"
_HALT_MARKER = "[claw_chat:guardrail_halt]"
_PROVIDER_ERROR_MARKER = "[claw_chat:provider_error]"


def _default_token_estimator(text: str) -> int:
    """Char/4 token heuristic per design §"Token estimation"."""
    return max(1, len(text) // 4)


class AgentLoop:
    """Orchestrates one chat session — composes every other agent component."""

    def __init__(
        self,
        *,
        cfg: "AgentConfig",
        llm: "LLMClient",
        mcp: "McpManager",
        registry: "ToolRegistry",
        dispatcher: "ToolDispatcher",
        compressor: "ContextCompressor",
        guardrails: "GuardrailsController",
        persistence: "SqlitePersistence",
        session: "Session",
        token_estimator: Optional[Callable[[str], int]] = None,
        title_generator: Optional["TitleGenerator"] = None,
        on_text_delta: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[["StreamStatus"], None]] = None,
        subagent_depth: int = 0,
    ) -> None:
        self._cfg = cfg
        self._llm = llm
        self._mcp = mcp
        self._registry = registry
        self._dispatcher = dispatcher
        self._compressor = compressor
        self._guardrails = guardrails
        self._persistence = persistence
        self.session = session
        self._token_estimator = token_estimator or _default_token_estimator
        self._title_generator = title_generator
        self._on_text_delta = on_text_delta
        self._on_status = on_status
        self._subagent_depth = subagent_depth
        self.history: List[Message] = []
        self._interrupt_event = threading.Event()

    # ── Interrupt control ─────────────────────────────────────────────

    @property
    def interrupt_event(self) -> threading.Event:
        """Shared :class:`threading.Event` honoured by the LLM stream
        reader and every Tool_Worker_Pool worker (Req 2.8, 8.6, 13.4)."""
        return self._interrupt_event

    def request_interrupt(self) -> None:
        """Signal the active turn to stop at the next safe boundary.

        Called by the TUI's SIGINT handler. The streaming reader checks
        the event between SSE deltas; the dispatcher checks it between
        Tool_Calls and inside each worker.
        """
        self._interrupt_event.set()

    # ── Sub-agent registration ────────────────────────────────────────

    def register_subagent_tool(self) -> None:
        """Register the ``claw_subagent`` generic tool and every enabled
        :class:`agent.config.SubAgentDef` from ``AgentConfig.subagents``.

        Called once after the loop is fully constructed.  No-op when
        ``AgentConfig.subagent_enabled`` is ``False``.  Safe to call
        multiple times — ``register_native`` replaces in-place.
        """
        if not self._cfg.subagent_enabled:
            return

        from agent.subagent import (
            SUBAGENT_TOOL_DESCRIPTION,
            SUBAGENT_TOOL_NAME,
            SUBAGENT_TOOL_SCHEMA,
            SpecializedSubAgent,
            SubAgentRunner,
        )

        # Generic claw_subagent — lets the LLM spawn ad-hoc sub-tasks.
        runner = SubAgentRunner(self)
        self._registry.register_native(
            SUBAGENT_TOOL_NAME,
            SUBAGENT_TOOL_DESCRIPTION,
            SUBAGENT_TOOL_SCHEMA,
            runner,
        )
        logger.debug("claw_subagent native tool registered (depth=0)")

        # Config-driven named sub-agents — one tool per enabled SubAgentDef.
        for defn in self._cfg.subagents:
            if not defn.enabled:
                logger.debug("skipping disabled sub-agent '%s'", defn.name)
                continue
            agent = SpecializedSubAgent(self, defn)
            self._registry.register_native(
                defn.name,
                defn.description,
                SpecializedSubAgent.build_schema(defn),
                agent,
            )
            logger.debug("sub-agent '%s' registered as native tool", defn.name)

    # ── Skill-tool registration ────────────────────────────────────────

    def register_skill_tools(self) -> None:
        """Register the ``claw_list_skills`` and ``claw_read_skill`` native tools.

        These tools let the LLM (and sub-agents) discover and read bundled
        skills during a chat session.  Safe to call multiple times —
        ``register_native`` replaces in-place.
        """
        from agent.skills import (
            SKILL_LIST_TOOL_DESCRIPTION,
            SKILL_LIST_TOOL_NAME,
            SKILL_LIST_TOOL_SCHEMA,
            SKILL_READ_TOOL_DESCRIPTION,
            SKILL_READ_TOOL_NAME,
            SKILL_READ_TOOL_SCHEMA,
            ListSkillsHandler,
            ReadSkillHandler,
        )

        self._registry.register_native(
            SKILL_LIST_TOOL_NAME,
            SKILL_LIST_TOOL_DESCRIPTION,
            SKILL_LIST_TOOL_SCHEMA,
            ListSkillsHandler(),
        )
        logger.debug("claw_list_skills native tool registered")

        self._registry.register_native(
            SKILL_READ_TOOL_NAME,
            SKILL_READ_TOOL_DESCRIPTION,
            SKILL_READ_TOOL_SCHEMA,
            ReadSkillHandler(),
        )
        logger.debug("claw_read_skill native tool registered")

    # ── Session loading ───────────────────────────────────────────────

    def load_session(self, session_id: str) -> None:
        """Resume a Session by id; refresh metadata and recent history.

        Honours the in-memory cap of 500 Messages plus an optional
        compression summary (Req 16.1, Property 17). Older Messages
        remain on disk and are fetched on demand by the compressor.
        """
        self.session = self._persistence.get_session(session_id)
        self.history = self._persistence.load_recent_messages(
            self.session.id, limit=500
        )

    # ── One-shot entry point ──────────────────────────────────────────

    def run_oneshot(self, query: str) -> int:
        """Run a single turn and return a process exit code.

        Exit codes follow design §"Error taxonomy":

        - ``0`` — success.
        - ``130`` — KeyboardInterrupt (Req 1.4 KeyboardInterrupt path).
        - ``1`` — :class:`ProviderHTTPError`,
          :class:`PersistenceFailure`, or any other exception.

        Streaming output goes to stdout via the
        :data:`on_text_delta` callback installed by the constructor; if
        none was supplied, this method installs a stdout writer so the
        user sees the response as it arrives. The Session_Id is written
        on the final line of stderr (Req 1.5).
        """
        if self._on_text_delta is None:
            self._on_text_delta = _stdout_writer

        try:
            self.run_turn(query)
        except KeyboardInterrupt:
            print("Cancelled.", file=sys.stderr)
            return 130
        except (ProviderHTTPError, PersistenceFailure) as exc:
            print(
                f"claw chat: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        except Exception as exc:  # noqa: BLE001 - top-level boundary
            print(
                f"claw chat: unrecoverable error: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1

        # Trailing newline so the shell prompt lands on a clean line.
        try:
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 - best effort
            pass
        # Session_Id on the final line of stderr (Req 1.5).
        print(self.session.short_id, file=sys.stderr)
        return 0

    # ── Per-turn driver ───────────────────────────────────────────────

    def run_turn(self, user_text: str) -> None:
        """Run one user turn through the Agent_Loop.

        Drives Iterations until the LLM stops emitting Tool_Calls, the
        Iteration_Budget is exhausted, the user interrupts, or a
        guardrail halt fires. May raise :class:`PersistenceFailure`
        when SQLite writes fail after retries; the caller (one-shot
        dispatcher or TUI) decides how to surface that.
        """
        # Each turn starts with a fresh interrupt state; an earlier turn's
        # cancellation must not leak into this one.
        self._interrupt_event.clear()

        iteration_count = 0
        budget_notice_sent = False

        # 1. User Message appended + persisted before any LLM work
        #    (design §"Crash recovery": persist before next user-visible
        #    action).
        user_msg = Message(role="user", content=user_text)
        self.history.append(user_msg)
        self._persist_messages([user_msg])

        while True:
            iteration_count += 1

            # Halt after the grace iteration has been consumed (Req 6.4).
            if iteration_count > self._cfg.max_iterations + 1:
                break

            # Grace iteration: append exactly one budget-exhaustion
            # notice (Req 6.3, Property 3) before issuing the call.
            if (
                iteration_count == self._cfg.max_iterations + 1
                and not budget_notice_sent
            ):
                notice = Message(
                    role="system",
                    content=(
                        f"{_BUDGET_MARKER} iteration budget of "
                        f"{self._cfg.max_iterations} exhausted; this is "
                        "the final response."
                    ),
                )
                self.history.append(notice)
                self._persist_messages([notice])
                budget_notice_sent = True

            # 2. Auto-compression check (Req 10.2).
            self._maybe_compress()

            # 3. Stream the LLM call.
            try:
                stream_result = self._llm.stream_chat(
                    messages=[m.to_openai() for m in self.history],
                    tools=self._registry.openai_tools(),
                    on_text_delta=self._on_text_delta,
                    on_status=self._on_status,
                    interrupt=self._interrupt_event,
                )
            except ProviderHTTPError as exc:
                err_text = f"[Error] Provider error: {exc}"
                # Surface the error to the user via the text delta callback
                if self._on_text_delta:
                    try:
                        self._on_text_delta(err_text)
                    except Exception:
                        pass
                err_msg = Message(
                    role="system",
                    content=f"{_PROVIDER_ERROR_MARKER} {exc}",
                )
                self.history.append(err_msg)
                self._persist_messages([err_msg])
                break

            # 4. Append the assistant Message; canonical-encode tool calls.
            assistant_msg = Message(
                role="assistant",
                content=stream_result.content,
                tool_arguments=_encode_tool_calls(stream_result.tool_calls),
                reasoning_content=stream_result.reasoning_content,
            )
            self.history.append(assistant_msg)
            self._persist_messages(
                [assistant_msg], stream_result=stream_result
            )

            # Surface empty response to user (no text, no tool calls)
            if not stream_result.content and not stream_result.tool_calls:
                if self._on_text_delta:
                    try:
                        self._on_text_delta(
                            f"[Warning] Empty response from model "
                            f"(finish_reason={stream_result.finish_reason!r})"
                        )
                    except Exception:
                        pass
                break

            # 5. No tool calls → final assistant turn (Req 2.3).
            if not stream_result.tool_calls:
                break
            if self._interrupt_event.is_set():
                self._append_interrupt_notice()
                break

            # 7. Dispatch tools; halt on guardrail halt band (Req 9.5).
            try:
                tool_messages = self._dispatcher.execute(
                    list(stream_result.tool_calls),
                    self._interrupt_event,
                )
            except GuardrailHalt as exc:
                halt_msg = Message(
                    role="system",
                    content=f"{_HALT_MARKER} {exc}",
                )
                self.history.append(halt_msg)
                self._persist_messages([halt_msg])
                break

            if tool_messages:
                self.history.extend(tool_messages)
                self._persist_messages(tool_messages)

        # 8. Title generation runs at most once per turn (Req 4.1).
        self._maybe_generate_title()

    # ── Internal helpers ──────────────────────────────────────────────

    def _persist_messages(
        self,
        messages: List[Message],
        *,
        stream_result: "Optional[StreamResult]" = None,
    ) -> None:
        """Append ``messages`` and refresh the Session token total."""
        total_tokens = self._compute_total_tokens(stream_result)
        self._persistence.append_messages(
            self.session.id, list(messages), total_tokens
        )
        self.session = replace(self.session, total_tokens=total_tokens)

    def _compute_total_tokens(
        self, stream_result: "Optional[StreamResult]"
    ) -> int:
        """Prefer provider-supplied usage; fall back to the estimator."""
        if stream_result is not None:
            pt = getattr(stream_result, "prompt_tokens", None)
            ct = getattr(stream_result, "completion_tokens", None)
            if isinstance(pt, int) and isinstance(ct, int):
                return pt + ct
        return sum(
            self._token_estimator(m.content or "") for m in self.history
        )

    def _maybe_compress(self) -> None:
        """Run one ``force=False`` compression pass when over threshold."""
        context_window = self._cfg.model_context_windows.get(
            self._llm.model, self._cfg.default_context_window
        )
        estimated = sum(
            self._token_estimator(m.content or "") for m in self.history
        )
        if estimated <= self._cfg.context_compression_threshold * context_window:
            return

        result = self._compressor.compress(self.history, force=False)
        if not result.succeeded:
            return

        self.history = list(result.messages)
        # Persist the new summary so disk reflects post-compression state
        # while still preserving the original messages (Req 10.8).
        for msg in self.history:
            if msg.tool_name == COMPRESSION_SUMMARY_TOOL_NAME:
                try:
                    self._persistence.persist_summary(self.session.id, msg)
                except PersistenceFailure as exc:
                    logger.warning(
                        "failed to persist compression summary: %s", exc
                    )
                break

    def _append_interrupt_notice(self) -> None:
        notice = Message(
            role="system",
            content=(
                f"{_INTERRUPT_MARKER} turn interrupted by user; tool "
                "dispatch was skipped."
            ),
        )
        self.history.append(notice)
        self._persist_messages([notice])

    def _maybe_generate_title(self) -> None:
        """Generate and persist a title once Reqs 4.1 / 4.4 are met."""
        if self._title_generator is None:
            return
        if self.session.title:  # Req 4.4 — never overwrite a non-empty title.
            return
        if _count_user_messages(self.history) < 2:
            return

        title = self._title_generator.generate(self.history)
        if not title:
            return
        try:
            self._persistence.update_title(self.session.id, title)
            self.session = replace(self.session, title=title)
        except PersistenceFailure as exc:
            logger.warning("failed to update session title: %s", exc)


# ── Module-private helpers ────────────────────────────────────────────


def _encode_tool_calls(tool_calls: List[ToolCall]) -> Optional[str]:
    """Serialize tool_calls into the canonical JSON form stored on the
    ``assistant`` Message's ``tool_arguments`` field.

    Returns ``None`` when there are no tool calls so the persistence
    column stays nullable. ``sort_keys=True`` guarantees byte-equal
    output for equal-valued inputs (Req 19.2).
    """
    if not tool_calls:
        return None
    encoded = [
        {"id": tc.id, "name": tc.name, "arguments": tc.arguments_json}
        for tc in tool_calls
    ]
    return json.dumps(encoded, sort_keys=True, ensure_ascii=False)


def _count_user_messages(history: List[Message]) -> int:
    return sum(1 for m in history if m.role == "user")


def _stdout_writer(chunk: str) -> None:
    """Default :data:`on_text_delta` for one-shot mode — never raises."""
    try:
        sys.stdout.write(chunk)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - rendering must never abort the loop
        pass
