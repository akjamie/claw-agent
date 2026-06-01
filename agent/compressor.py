"""
Context compression — `ContextCompressor` and the 5-phase pipeline.

The compressor reduces a long conversation history down to a short summary
plus a verbatim Protected_Tail before the next LLM call. The pipeline
follows design.md §"agent.compressor" exactly:

    Phase 1 — prune verbose ``tool`` Messages in the to-be-compressed prefix
    Phase 2 — pick the boundary between compressed prefix and Protected_Tail
              (token-budget driven, never splits assistant+tool_calls groups)
    Phase 3 — generate the 14-section summary via an LLM call with a clamped
              ``max_tokens`` budget
    Phase 4 — assemble ``[head_system_message?, *head_protected, summary, *tail]``
              and ensure all 14 section headings exist in order with
              ``_(none)_`` fill-ins for empties
    Phase 5 — sanitise orphan ``tool`` Messages whose ``tool_call_id`` no
              longer matches any upstream assistant tool-call

Anti-thrashing (Req 10.7, Property 5): the controller maintains the last two
``after_tokens / before_tokens`` ratios from ``force=False`` passes. After
two consecutive passes that each reduce the history by less than 10%
(ratio > 0.90), subsequent ``force=False`` calls return the input
unchanged. Manual ``/compact`` always passes ``force=True`` and bypasses
the skip rule (Req 11.3).

Design references:
- design.md §"agent.compressor" — phase semantics, boundaries, marker.
- design.md §"Tool argument canonicalisation" — Phase 1 preserves canonical
  ``tool_arguments`` even when the body is replaced.
- requirements.md §10, §11, §12 — auto-trigger, manual trigger, template.
- agent.messages.COMPRESSION_SUMMARY_TOOL_NAME — sentinel preventing the
  summary message from being re-pruned by Phase 1 on subsequent passes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Callable, List, Optional

from agent.messages import COMPRESSION_SUMMARY_TOOL_NAME, Message

__all__ = [
    "CompressionResult",
    "ContextCompressor",
]


# Threshold above which Phase 1 prunes a ``tool`` Message body (chars).
_PRUNE_BYTE_THRESHOLD = 800

# How many additional non-system messages to protect at the head of the
# conversation (the very first user/assistant exchange is usually load-
# bearing context the summary cannot recover).
_PROTECT_FIRST_N = 1

# Anti-thrashing: track the last two reduction ratios; skip when both
# exceed this value (i.e., reduction < 10%).
_THRASH_RATIO = 0.90
_THRASH_HISTORY = 2


@dataclass
class CompressionResult:
    """Outcome of one ``compress`` call.

    ``messages`` is the post-compression history when ``succeeded`` is
    ``True``, or the original history (unchanged) when ``succeeded`` is
    ``False``. ``before_tokens`` and ``after_tokens`` are computed via
    the injected ``token_estimator`` so unit tests can assert the
    per-pass reduction.

    ``skipped_reason`` carries one of:
    - ``"anti_thrashing"`` — two prior ``force=False`` passes failed to
      reduce by ≥10%, so this pass was skipped (Req 10.7).
    - ``"below_threshold"`` — nothing in the history was compressible
      (history too short, or the head + tail already covers everything).
    - ``None`` — the pass ran. When combined with ``succeeded=False``,
      the LLM call failed and the original history is preserved
      (Req 11.4).
    """

    messages: List[Message]
    before_tokens: int
    after_tokens: int
    succeeded: bool
    skipped_reason: Optional[str] = None


class ContextCompressor:
    """5-phase context compression engine.

    One instance per Session: the anti-thrashing ledger lives on
    ``self._recent_reductions`` and persists across calls. The class
    holds no I/O state of its own; persistence and the loop's history
    buffer are the caller's concern.
    """

    # Exact 14-section template per Req 12.1 (order is significant —
    # Property 11 asserts byte-equal order).
    SECTIONS: tuple = (
        "## Active Task",
        "## Completed Actions",
        "## Blocked",
        "## User Preferences",
        "## Files & Resources",
        "## Decisions Made",
        "## Open Questions",
        "## Errors Encountered",
        "## Tools Used",
        "## Key Findings",
        "## Next Steps",
        "## Out of Scope",
        "## References",
        "## Notes",
    )

    # Marker line that prefixes every compression-summary body
    # (Req 12.3, design §"Error Message conventions").
    SUMMARY_MARKER = "<!-- claw_chat:compression_summary v1 -->\n"

    # Empty-section fill-in (Req 12.2).
    _NONE_PLACEHOLDER = "_(none)_"

    def __init__(
        self,
        llm: Any,
        agent_cfg: Any,
        token_estimator: Callable[[str], int],
    ) -> None:
        """Construct a fresh compressor.

        Args:
            llm: An :class:`agent.llm_client.LLMClient` (or any duck-typed
                object with a ``chat(messages, *, max_tokens=...)`` method
                returning the OpenAI chat-completions JSON).
            agent_cfg: An :class:`agent.config.AgentConfig`. The fields
                actually consulted are
                ``protected_tail_fraction``, ``default_context_window``,
                ``model_context_windows``, ``summary_fraction``,
                ``summary_floor_tokens``, and ``summary_cap_tokens``.
            token_estimator: A callable mapping content text to an
                approximate token count. The agent uses
                ``lambda s: max(1, len(s) // 4)``.
        """
        self._llm = llm
        self._cfg = agent_cfg
        self._token_estimator = token_estimator
        # Last `_THRASH_HISTORY` reduction ratios from ``force=False``
        # passes (Req 10.7, Property 5).
        self._recent_reductions: List[float] = []

    # ── Public API ──────────────────────────────────────────────────────

    def compress(
        self,
        history: List[Message],
        *,
        topic: Optional[str] = None,
        force: bool = False,
    ) -> CompressionResult:
        """Run the 5-phase compression pipeline.

        ``force=True`` (manual ``/compact``) always runs the pipeline
        and bypasses the anti-thrashing skip rule (Req 11.3).

        ``force=False`` (auto-trigger) is skipped when the last two
        reductions were each below 10% (Req 10.7).

        On any internal failure (including an LLM provider error during
        Phase 3), the original ``history`` is returned unchanged with
        ``succeeded=False`` so the caller can leave the conversation
        intact and warn the user (Req 11.4).
        """
        before_tokens = self._total_tokens(history)

        # Anti-thrashing skip — checked before any work, only for auto
        # passes.
        if not force and self._should_skip_thrashing():
            return CompressionResult(
                messages=list(history),
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                succeeded=False,
                skipped_reason="anti_thrashing",
            )

        # Trivial / nothing-to-compress histories. We still record the
        # ratio (1.0) for ``force=False`` passes so the anti-thrashing
        # counter advances on truly stuck sessions.
        if len(history) < 2:
            self._record_ratio(1.0, force=force)
            return CompressionResult(
                messages=list(history),
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                succeeded=False,
                skipped_reason="below_threshold",
            )

        context_window = self._context_window()

        # Phase 2: determine the head/tail boundaries.
        head_end, tail_start = self._select_boundaries(history, context_window)

        head = list(history[:head_end])
        prefix = list(history[head_end:tail_start])
        tail = list(history[tail_start:])

        if not prefix:
            self._record_ratio(1.0, force=force)
            return CompressionResult(
                messages=list(history),
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                succeeded=False,
                skipped_reason="below_threshold",
            )

        # Phase 1: prune verbose tool bodies inside the prefix only.
        # Head and tail are preserved verbatim per design §"agent.compressor".
        prefix = [self._prune_tool_message(m) for m in prefix]

        # Phase 3: generate the summary via the LLM.
        compressed_tokens = self._total_tokens(prefix)
        summary_budget = self._summary_budget(compressed_tokens)

        try:
            raw_summary = self._call_llm_summary(prefix, topic, summary_budget)
        except Exception:
            # Provider error → leave history unchanged (Req 11.4,
            # design §"Error taxonomy" — CompressionFailed). We do not
            # update the anti-thrashing ledger, since this pass made no
            # progress for reasons unrelated to the input.
            return CompressionResult(
                messages=list(history),
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                succeeded=False,
                skipped_reason=None,
            )

        # Phase 4: enforce the 14-section template, prefix the marker,
        # build the summary Message, and assemble the result list.
        body = self._ensure_14_sections(raw_summary)
        summary_msg = Message(
            role="system",
            content=self.SUMMARY_MARKER + body,
            tool_name=COMPRESSION_SUMMARY_TOOL_NAME,
        )
        assembled = head + [summary_msg] + tail

        # Phase 5: drop ``tool`` Messages whose ``tool_call_id`` has no
        # matching upstream assistant tool-call entry.
        sanitized = self._sanitize_orphan_tools(assembled)

        after_tokens = self._total_tokens(sanitized)

        # Anti-thrashing ledger (Req 10.7).
        ratio = (after_tokens / before_tokens) if before_tokens > 0 else 1.0
        self._record_ratio(ratio, force=force)

        return CompressionResult(
            messages=sanitized,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            succeeded=True,
            skipped_reason=None,
        )

    # ── Phase 2: boundary selection ─────────────────────────────────────

    def _select_boundaries(
        self, history: List[Message], context_window: int
    ) -> tuple[int, int]:
        """Return ``(head_end, tail_start)`` index boundaries.

        - ``history[:head_end]`` is the protected head.
        - ``history[head_end:tail_start]`` is the prefix to compress.
        - ``history[tail_start:]`` is the Protected_Tail.

        The head consists of the leading system Message (if present) plus
        the next :data:`_PROTECT_FIRST_N` non-system messages. The tail is
        chosen by walking newest→oldest accumulating tokens until the
        running total reaches ``protected_tail_fraction * context_window``.
        The tail is then extended (if needed) to include the most recent
        ``user`` Message and to never start mid-group of a
        ``assistant``-with-``tool_calls`` followed by its ``tool``
        replies.
        """
        n = len(history)
        if n == 0:
            return 0, 0

        # ---- Head ---------------------------------------------------
        head_end = 0
        if history[0].role == "system":
            head_end = 1
        protected_extra = 0
        while head_end < n and protected_extra < _PROTECT_FIRST_N:
            if history[head_end].role != "system":
                protected_extra += 1
            head_end += 1

        # ---- Tail ---------------------------------------------------
        target_tail_tokens = max(1, int(self._cfg.protected_tail_fraction * context_window))
        tail_start = n
        tail_tokens = 0
        for i in range(n - 1, -1, -1):
            tail_tokens += self._token_estimator(history[i].content or "")
            tail_start = i
            if tail_tokens >= target_tail_tokens:
                break

        # Ensure the most recent user Message is in the tail (Req 10.4,
        # Property 2).
        last_user_idx = -1
        for i in range(n - 1, -1, -1):
            if history[i].role == "user":
                last_user_idx = i
                break
        if last_user_idx >= 0 and last_user_idx < tail_start:
            tail_start = last_user_idx

        # Align boundary: never start the tail with a ``tool`` Message
        # whose upstream assistant lives in the prefix. Walk back over
        # any ``tool`` Messages at the boundary so the assistant comes
        # with them (design §"agent.compressor", Property 9).
        while tail_start > 0 and history[tail_start].role == "tool":
            tail_start -= 1
        # Also walk back to include the assistant that owns those tool
        # calls — once tail_start points at an assistant, we are done.
        # (The walk above has already consumed leading tool messages;
        # we rely on the dispatcher's ordering invariant of
        # assistant-then-tool, so a single back-step from a tool
        # already lands on its assistant.)

        # The head must not overlap the tail. If the tail extended past
        # the head boundary, drop the head entirely so we always emit
        # ``[*head, summary, *tail]`` with non-overlapping slices.
        if tail_start < head_end:
            head_end = tail_start

        return head_end, tail_start

    # ── Phase 1: prune verbose tool bodies ──────────────────────────────

    @staticmethod
    def _prune_tool_message(m: Message) -> Message:
        """Replace verbose ``tool`` Messages with a one-line synthetic summary.

        Skipped for non-tool Messages and for the compression-summary
        sentinel (``tool_name == "__compression_summary__"``) so a prior
        summary never gets re-pruned.

        Format: ``[{tool_name}] {first_120_chars}... ({n_lines} lines,
        {n_chars} chars)``. The original ``tool_call_id``,
        ``tool_arguments``, and ``timestamp`` are preserved verbatim so
        Phase 5's orphan-detection still works on the pruned message.
        """
        if m.role != "tool":
            return m
        if m.tool_name == COMPRESSION_SUMMARY_TOOL_NAME:
            return m
        content = m.content or ""
        if len(content) <= _PRUNE_BYTE_THRESHOLD:
            return m

        n_chars = len(content)
        # Count lines including the trailing partial line.
        n_lines = content.count("\n") + 1
        # Collapse newlines so the synthetic preview stays one line.
        flat = content.replace("\r", " ").replace("\n", " ")
        first_120 = flat[:120]
        tn = m.tool_name or "tool"
        new_content = (
            f"[{tn}] {first_120}... ({n_lines} lines, {n_chars} chars)"
        )
        return replace(m, content=new_content)

    # ── Phase 3: summary generation ─────────────────────────────────────

    def _summary_budget(self, compressed_tokens: int) -> int:
        """Compute ``max_tokens`` for the summary call (Req 10.5).

        ``Summary_Budget = clamp(summary_fraction * compressed_tokens,
        summary_floor_tokens, summary_cap_tokens)`` — equivalently the
        formula tested by Property 8 with the documented default
        coefficients (0.20 / 2000 / 12000).
        """
        target = int(self._cfg.summary_fraction * max(0, compressed_tokens))
        return max(
            self._cfg.summary_floor_tokens,
            min(self._cfg.summary_cap_tokens, target),
        )

    def _build_summary_prompt(
        self, prefix: List[Message], topic: Optional[str]
    ) -> List[dict]:
        """Build the ``[system, user]`` chat-completions payload.

        The system prompt enforces the 14-section template and the
        ``_(none)_`` rule (Req 12.1, 12.2). When ``topic`` is provided
        we prepend ``Focus on: <topic>.`` so the summariser weights the
        named subject (Req 11.2).
        """
        section_lines = "\n".join(self.SECTIONS)
        system_text = (
            "You are a context-compression assistant. Summarise the "
            "conversation segment supplied by the user into the 14 "
            "Markdown sections below, using level-2 headings exactly as "
            "shown and emitting them in this order:\n\n"
            f"{section_lines}\n\n"
            "Rules:\n"
            "1. Use every heading exactly as written (verbatim level-2 "
            "Markdown heading).\n"
            "2. For any section that has no relevant content from the "
            "supplied conversation, emit the heading followed by the "
            f"literal line: {self._NONE_PLACEHOLDER}\n"
            "3. Be concise. Preserve specific identifiers, file paths, "
            "tool names, error messages, and decisions verbatim where "
            "possible.\n"
            "4. Do not invent facts; only summarise what is present in "
            "the supplied messages.\n"
            "5. Output only the 14 sections — no preamble, no closing "
            "remarks, no extra headings."
        )
        if topic:
            system_text = f"Focus on: {topic}.\n\n" + system_text

        user_text = self._format_messages_for_summary(prefix)
        return [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]

    @staticmethod
    def _format_messages_for_summary(prefix: List[Message]) -> str:
        """Render the prefix as readable text for the summariser.

        We emit one stanza per Message tagged with role and (for tool
        Messages) the tool name. Empty content is preserved as an
        explicit placeholder so the LLM does not accidentally treat the
        adjacent stanzas as one merged turn.
        """
        parts: List[str] = []
        for m in prefix:
            role_label = m.role.upper()
            content = m.content if m.content else "(empty)"
            if m.role == "tool":
                tn = m.tool_name or "(unknown tool)"
                parts.append(f"[{role_label} {tn}]\n{content}")
            elif m.role == "assistant" and m.tool_arguments:
                # Surface tool-call intent so the summariser can populate
                # the ``## Tools Used`` section accurately.
                parts.append(
                    f"[{role_label} (with tool_calls)]\n{content}\n"
                    f"tool_calls: {m.tool_arguments}"
                )
            else:
                parts.append(f"[{role_label}]\n{content}")
        return "\n\n".join(parts)

    def _call_llm_summary(
        self,
        prefix: List[Message],
        topic: Optional[str],
        budget: int,
    ) -> str:
        """Invoke the LLM and return the raw summary text.

        Any exception raised by the underlying client is propagated to
        ``compress``, which converts it into a no-op CompressionResult
        (design §"Error taxonomy" — CompressionFailed). An empty or
        malformed response body returns ``""``, which Phase 4 then
        backfills with all 14 ``_(none)_`` sections.
        """
        messages = self._build_summary_prompt(prefix, topic)
        response = self._llm.chat(messages, max_tokens=budget)
        if not isinstance(response, dict):
            return ""
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        msg = first.get("message")
        if not isinstance(msg, dict):
            return ""
        content = msg.get("content")
        return content if isinstance(content, str) else ""

    # ── Phase 4: 14-section template enforcement ────────────────────────

    def _ensure_14_sections(self, raw_summary: str) -> str:
        """Reflow the LLM output so all 14 headings appear in order.

        Walk ``raw_summary`` once collecting the body for every heading
        we recognise, then re-emit the body in :data:`SECTIONS` order.
        Missing sections are filled with :data:`_NONE_PLACEHOLDER`
        (Req 12.2). A heading whose body is whitespace-only is also
        treated as missing.

        The output is deterministic per input and does not depend on
        the LLM emitting headings in the right order or even at all
        (Property 11).
        """
        sections_set = set(self.SECTIONS)
        section_bodies: dict[str, str] = {}

        current: Optional[str] = None
        buffer: List[str] = []
        for line in raw_summary.splitlines():
            stripped = line.strip()
            if stripped in sections_set:
                if current is not None:
                    section_bodies[current] = "\n".join(buffer).strip()
                current = stripped
                buffer = []
            else:
                if current is not None:
                    buffer.append(line)
                # Lines before the first recognised heading are dropped;
                # they would otherwise leak into ``## Active Task``.
        if current is not None:
            section_bodies[current] = "\n".join(buffer).strip()

        out_parts: List[str] = []
        for heading in self.SECTIONS:
            body = section_bodies.get(heading, "").strip()
            if not body:
                body = self._NONE_PLACEHOLDER
            out_parts.append(f"{heading}\n{body}")
        return "\n\n".join(out_parts)

    # ── Phase 5: orphan tool/assistant pair sanitisation ────────────────

    @staticmethod
    def _sanitize_orphan_tools(messages: List[Message]) -> List[Message]:
        """Drop ``tool`` Messages whose ``tool_call_id`` has no upstream owner.

        Walks left-to-right collecting every ``tool_call_id`` emitted by
        an upstream ``assistant`` Message, then keeps a ``tool`` Message
        only when its ``tool_call_id`` appears in that set. Assistant
        Messages are left unchanged — design §"agent.compressor" picks
        the simpler "drop orphan tools, leave assistants intact"
        strategy (Property 9).

        The seen-id set is built defensively: malformed
        ``tool_arguments`` JSON is silently skipped because the encoder
        guarantees canonical JSON and any deviation is an upstream bug
        that the dispatcher already surfaces as a tool error.
        """
        seen_ids: set = set()
        result: List[Message] = []
        for msg in messages:
            if msg.role == "tool":
                if msg.tool_call_id and msg.tool_call_id in seen_ids:
                    result.append(msg)
                # else: orphan → drop
                continue

            if msg.role == "assistant" and msg.tool_arguments:
                try:
                    decoded = json.loads(msg.tool_arguments)
                except (json.JSONDecodeError, ValueError, TypeError):
                    decoded = None
                if isinstance(decoded, list):
                    for entry in decoded:
                        if not isinstance(entry, dict):
                            continue
                        tcid = entry.get("id")
                        if isinstance(tcid, str) and tcid:
                            seen_ids.add(tcid)
            result.append(msg)
        return result

    # ── Anti-thrashing helpers ──────────────────────────────────────────

    def _should_skip_thrashing(self) -> bool:
        """Whether the next ``force=False`` pass must be skipped (Req 10.7)."""
        if len(self._recent_reductions) < _THRASH_HISTORY:
            return False
        recent = self._recent_reductions[-_THRASH_HISTORY:]
        return all(r > _THRASH_RATIO for r in recent)

    def _record_ratio(self, ratio: float, *, force: bool) -> None:
        """Append a reduction ratio to the rolling window.

        ``force=True`` passes do not contribute to the anti-thrashing
        window — the manual ``/compact`` is intentionally allowed to
        thrash (Req 11.3). ``force=False`` ratios are appended and the
        window is truncated to the last :data:`_THRASH_HISTORY` entries.
        """
        if force:
            return
        self._recent_reductions.append(ratio)
        if len(self._recent_reductions) > _THRASH_HISTORY:
            self._recent_reductions = self._recent_reductions[-_THRASH_HISTORY:]

    # ── Generic helpers ────────────────────────────────────────────────

    def _total_tokens(self, messages: List[Message]) -> int:
        """Sum the token estimates of every message body."""
        total = 0
        for m in messages:
            total += self._token_estimator(m.content or "")
        return total

    def _context_window(self) -> int:
        """Resolve the active model's context window.

        Falls back to ``default_context_window`` when the model has no
        per-model entry (Req 14 — config defaults) or when the LLM
        client does not expose a ``model`` attribute.
        """
        model = getattr(self._llm, "model", "")
        per_model = self._cfg.model_context_windows.get(model)
        if isinstance(per_model, int) and per_model > 0:
            return per_model
        return self._cfg.default_context_window
