"""
Langfuse tracing integration for claw-agent — ``TurnTracer``.

Records one Langfuse trace per user turn, with a ``generation`` observation
for each LLM call iteration.  Token usage is captured from the provider
response when available.

Usage
-----

The tracer is used via ``enter`` / ``exit`` / ``record_generation``:

.. code-block:: python

    tracer = TurnTracer(
        session_id=self.session.id,
        model=self._llm.model,
        provider=getattr(self, "_provider", ""),
        user_text=user_text,
    )
    tracer.enter()
    # ... for each LLM call:
    tracer.record_generation(
        iteration=i,
        model=self._llm.model,
        input_messages=[m.to_openai() for m in self.history],
        tools=self._registry.openai_tools(),
        output=stream_result.content,
        prompt_tokens=stream_result.prompt_tokens,
        completion_tokens=stream_result.completion_tokens,
    )
    tracer.exit()

Prerequisites
-------------

The ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, and
``LANGFUSE_BASE_URL`` environment variables must be set (typically via
``~/.claw/.env``).  When they are missing the Langfuse SDK logs a warning
and all calls become no-ops — the application continues normally.

Design references
-----------------
- Langfuse skill: https://github.com/langfuse/skills (instrumentation.md)
- Langfuse Python SDK v4 API: ``langfuse.Langfuse.start_observation``
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TurnTracer:
    """Records one Langfuse trace for a single user conversation turn.

    Each turn (one user message + all LLM iterations) is recorded as one
    trace.  Every ``stream_chat`` call is recorded as a ``generation``
    observation nested under the trace.

    Sub-agents create their own ``AgentLoop`` (and therefore their own
    ``TurnTracer`` instances), so their traces are independent — they are
    *not* linked to the parent trace in this initial version.

    The trace carries ``session_id`` (via ``propagate_attributes``) so
    Langfuse's Sessions view can group all turns from the same chat
    session together.
    """

    def __init__(
        self,
        session_id: str,
        model: str,
        provider: str,
        user_text: str,
    ) -> None:
        """Create a new tracer for one user turn.

        Args:
            session_id: The claw-agent session UUID (becomes Langfuse
                ``session_id`` for the Sessions view).
            model: Model identifier (e.g. ``"openai/gpt-4o-mini"``).
            provider: Provider slug (e.g. ``"openrouter"``).
            user_text: The user's message that started this turn.
        """
        self._session_id = session_id
        self._model = model
        self._provider = provider
        self._user_text = user_text
        self._root: Any = None
        self._propagate_ctx: Any = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def enter(self) -> None:
        """Start the trace.  Call at the beginning of the turn.

        Uses ``propagate_attributes`` to set ``session_id`` on the trace
        so Langfuse groups turns into sessions, then creates a root
        observation (``span``) that doubles as the trace container.
        """
        from langfuse import get_client, propagate_attributes

        client = get_client()
        self._propagate_ctx = propagate_attributes(
            session_id=self._session_id,
        )
        self._propagate_ctx.__enter__()
        self._root = client.start_observation(
            name="claw-chat-turn",
            as_type="span",
            input={"user_message": self._user_text},
            metadata={
                "model": self._model,
                "provider": self._provider,
                "framework": "claw-agent",
            },
        )

    def exit(self) -> None:
        """End the trace and flush events.  Call at the end of the turn."""
        if self._root is not None:
            try:
                self._root.end()
            except Exception as exc:
                logger.warning("Failed to end Langfuse root span: %s", exc)
        if self._propagate_ctx is not None:
            try:
                self._propagate_ctx.__exit__(None, None, None)
            except Exception as exc:
                logger.warning(
                    "Failed to exit Langfuse propagate context: %s", exc
                )
        self._flush()

    # ── Generation recording ───────────────────────────────────────────────

    def record_generation(
        self,
        *,
        iteration: int,
        model: str,
        input_messages: List[dict],
        tools: Optional[List[dict]],
        output: str,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        finish_reason: Optional[str] = None,
    ) -> None:
        """Record one LLM call as a generation observation.

        The generation is nested under the root span.  Token usage is
        captured when the provider supplies it.

        Args:
            iteration: The 1-based iteration number within this turn.
            model: Full model identifier as sent to the provider.
            input_messages: The OpenAI-format messages array.
            tools: The OpenAI-format ``tools`` array, or ``None``.
            output: The text content of the LLM response.
            prompt_tokens: Input token count, or ``None``.
            completion_tokens: Output token count, or ``None``.
            finish_reason: Finish reason, or ``None``.
        """
        if self._root is None:
            return

        inp: Dict[str, Any] = {"messages": input_messages}
        if tools:
            inp["tools"] = tools

        # Build usage_details only when values are present.
        usage: Optional[Dict[str, int]] = None
        if prompt_tokens is not None or completion_tokens is not None:
            usage = {}
            if prompt_tokens is not None:
                usage["input"] = prompt_tokens
            if completion_tokens is not None:
                usage["output"] = completion_tokens
            if finish_reason:
                usage["finish_reason"] = finish_reason  # type: ignore[assignment]

        try:
            gen = self._root.start_observation(
                name=f"iteration-{iteration}",
                as_type="generation",
                model=model,
                input=inp,
                output=output or None,
                usage_details=usage,
            )
            # The generation is immediately ended so it appears as a
            # completed observation in Langfuse rather than remaining
            # open.
            gen.end()
        except Exception as exc:
            logger.warning("Failed to record Langfuse generation: %s", exc)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _flush(self) -> None:
        """Flush buffered events to the Langfuse API."""
        try:
            from langfuse import get_client

            get_client().flush()
        except Exception as exc:
            logger.warning("Failed to flush Langfuse events: %s", exc)