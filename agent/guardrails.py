"""
Tool-loop guardrails — `GuardrailsController` and `GuardrailLedger`.

Watches Tool_Call outcomes and emits warning / block / halt Messages when
a band's counter crosses its threshold. Per design §"agent.guardrails"
and Requirements 9.1–9.8.

Bands and thresholds:

- ``exact_failure``         — keyed by ``Tool_Hash`` (tool_name + canonical
                              arguments). warn at 2 consecutive failures,
                              block at 5.
- ``same_tool_failure``     — keyed by ``tool_name`` (regardless of args).
                              warn at 3, halt at 8.
- ``idempotent_no_progress`` — keyed by ``Tool_Hash``. warn at 2 consecutive
                              calls returning identical successful content,
                              block at 5.

Modes (Req 9.8):

- ``warn``    (default) — every band emits a warning ``system`` Message.
                          block and halt bands are downgraded to warnings;
                          no Tool_Call is ever refused and the loop is
                          never halted by guardrails.
- ``enforce`` — warn bands behave as in warn mode. Block bands cause
                ``should_dispatch`` to return a synthetic ``tool``
                Message that the dispatcher appends instead of running
                the call. The same-tool halt band raises
                :class:`GuardrailHalt` from ``record_outcome``; the loop
                catches it and exits cleanly.

A tool result is treated as a failure when ``result_msg.content`` starts
with one of the error markers from design §"Error Message conventions"
(``json_decode_error``, ``mcp_unavailable``, ``tool_timeout``). The
dispatcher may also pass an explicit ``failed`` flag to override the
content-prefix heuristic.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.messages import Message, ToolCall, canonical_args

__all__ = [
    "GuardrailHalt",
    "GuardrailLedger",
    "GuardrailsController",
    "tool_hash",
]


# Error markers used by the dispatcher when a tool result represents a
# failure (design §"Error Message conventions"). Kept here so the
# guardrails module can detect failures from the Message alone, without
# a separate flag.
_ERROR_MARKERS = ("json_decode_error", "mcp_unavailable", "tool_timeout")


# Loop-level marker prefix for guardrail Messages, matching the other
# system markers (`[claw_chat:budget_exhausted]`,
# `[claw_chat:interrupted]`, `[claw_chat:guardrail_halt]`).
_WARN_MARKER = "[claw_chat:guardrail_warn]"
_BLOCK_MARKER = "[claw_chat:guardrail_block]"
_HALT_MARKER = "[claw_chat:guardrail_halt]"


class GuardrailHalt(Exception):
    """Raised by :meth:`GuardrailsController.record_outcome` when the
    ``same_tool_failure`` halt band fires in ``enforce`` mode.

    The ``AgentLoop`` catches this exception, appends a halt-notice
    system Message and returns control to the user (Req 9.5).
    """


def tool_hash(name: str, args_obj_or_str: Any) -> str:
    """Compute the deterministic ``Tool_Hash`` for a Tool_Call.

    ``sha256(name + "\\u0000" + canonical_args(args)).hexdigest()``.

    The NUL separator prevents any name/argument boundary collision, and
    :func:`agent.messages.canonical_args` guarantees that semantically
    equal argument values produce byte-identical hash inputs (Req 19.2),
    which in turn makes ``tool_hash`` deterministic on JSON-equivalent
    inputs (Req 9.1, Property 4).
    """
    canon = canonical_args(args_obj_or_str)
    return hashlib.sha256(
        (name + "\u0000" + canon).encode("utf-8")
    ).hexdigest()


@dataclass
class GuardrailLedger:
    """In-memory ledger of Tool_Call outcomes for one Session.

    Field shapes match design §"agent.guardrails":

    - ``exact_failures``     — ``tool_hash`` → consecutive failure count.
                               Reset to zero on the first success for that
                               hash.
    - ``same_tool_failures`` — ``tool_name`` → consecutive failure count.
                               Reset to zero on the first success for that
                               name.
    - ``idempotent_runs``    — ``tool_hash`` → list of recent successful
                               result-content hashes. Truncated to the
                               idempotent block threshold to bound memory.
                               Reset whenever the next successful call
                               returns different content, or whenever any
                               failure for the same hash arrives.
    """

    exact_failures: dict[str, int] = field(default_factory=dict)
    same_tool_failures: dict[str, int] = field(default_factory=dict)
    idempotent_runs: dict[str, list[str]] = field(default_factory=dict)


class GuardrailsController:
    """Detect repeated identical or unproductive Tool_Calls.

    The controller is single-Session; the loop creates a fresh instance
    for every Session. All state lives on the ``ledger`` attribute so
    tests can inspect it directly.
    """

    # --- Band thresholds (Req 9.2-9.7, design §"agent.guardrails") ---
    EXACT_FAILURE_WARN = 2
    EXACT_FAILURE_BLOCK = 5
    SAME_TOOL_WARN = 3
    SAME_TOOL_HALT = 8
    IDEMPOTENT_WARN = 2
    IDEMPOTENT_BLOCK = 5

    _VALID_MODES = ("warn", "enforce")

    def __init__(self, mode: str = "warn") -> None:
        if mode not in self._VALID_MODES:
            raise ValueError(
                f"GuardrailsController mode must be one of "
                f"{self._VALID_MODES!r}, got {mode!r}"
            )
        self.mode = mode
        self.ledger = GuardrailLedger()

    # ── Pre-dispatch check ────────────────────────────────────────────

    def should_dispatch(self, call: ToolCall) -> Optional[Message]:
        """Decide whether ``call`` may be dispatched.

        Returns a synthetic ``tool`` Message in ``enforce`` mode when a
        block band is already armed for ``call`` (the dispatcher should
        append it in place of the real call). Returns ``None`` in all
        other cases — both when guardrails are inactive (``warn`` mode,
        Req 9.8) and when no band is armed.

        The block bands checked here are:

        - ``exact_failure`` block — the same Tool_Hash has reached
          :data:`EXACT_FAILURE_BLOCK` consecutive failures (Req 9.3).
        - ``idempotent_no_progress`` block — the same Tool_Hash has
          produced :data:`IDEMPOTENT_BLOCK` consecutive identical
          successful results (Req 9.7).
        """
        if self.mode != "enforce":
            return None

        h = tool_hash(call.name, call.arguments_json)

        # exact_failure block: refuse identical calls after the configured
        # number of consecutive failures.
        if self.ledger.exact_failures.get(h, 0) >= self.EXACT_FAILURE_BLOCK:
            return Message(
                role="tool",
                tool_call_id=call.id,
                tool_name=call.name,
                content=(
                    f"{_BLOCK_MARKER} exact_failure: tool {call.name!r} has "
                    f"failed {self.EXACT_FAILURE_BLOCK} consecutive times "
                    "with identical arguments; refusing to dispatch."
                ),
            )

        # idempotent_no_progress block: refuse identical calls when the
        # last N successful runs all returned identical content.
        runs = self.ledger.idempotent_runs.get(h, [])
        if len(runs) >= self.IDEMPOTENT_BLOCK:
            return Message(
                role="tool",
                tool_call_id=call.id,
                tool_name=call.name,
                content=(
                    f"{_BLOCK_MARKER} idempotent_no_progress: tool "
                    f"{call.name!r} has produced identical output "
                    f"{self.IDEMPOTENT_BLOCK} consecutive times with "
                    "identical arguments; refusing to dispatch."
                ),
            )

        return None

    # ── Post-dispatch outcome ledger ─────────────────────────────────

    def record_outcome(
        self,
        call: ToolCall,
        result_msg: Message,
        *,
        failed: Optional[bool] = None,
    ) -> Optional[Message]:
        """Update the ledger and report the highest band that fired.

        Parameters
        ----------
        call:
            The Tool_Call that produced the result.
        result_msg:
            The ``tool`` Message returned by the dispatcher.
        failed:
            Optional explicit failure flag. When ``None`` the controller
            infers failure from ``result_msg.content`` by checking the
            error-marker prefixes from design §"Error Message
            conventions". The dispatcher passes ``failed=True`` for
            cancelled/timeout outcomes that lack a textual marker.

        Returns
        -------
        Message | None
            A ``system`` Message describing the band that fired, or
            ``None`` when no band fired. Block / halt notices are
            downgraded to warnings when ``mode == "warn"`` (Req 9.8).

        Raises
        ------
        GuardrailHalt
            When ``mode == "enforce"`` and the same-tool halt band fires
            (``same_tool_failures[tool_name] == SAME_TOOL_HALT``,
            Req 9.5).
        """
        h = tool_hash(call.name, call.arguments_json)
        is_failure = self._is_failure(result_msg) if failed is None else bool(failed)

        if is_failure:
            return self._record_failure(call, h)
        return self._record_success(call, h, result_msg)

    # ── private helpers ──────────────────────────────────────────────

    def _record_failure(self, call: ToolCall, h: str) -> Optional[Message]:
        # Any failure invalidates the no-progress streak: the contract
        # of `idempotent_no_progress` is "consecutive identical
        # successful results" (Req 9.6).
        self.ledger.idempotent_runs.pop(h, None)

        ef_count = self.ledger.exact_failures.get(h, 0) + 1
        self.ledger.exact_failures[h] = ef_count

        stf_count = self.ledger.same_tool_failures.get(call.name, 0) + 1
        self.ledger.same_tool_failures[call.name] = stf_count

        # Halt band (Req 9.5): in enforce mode this terminates the loop
        # by raising; in warn mode we emit a downgraded warning instead
        # (Req 9.8). Halt is ranked above the block bands because it
        # ends the Iteration entirely.
        if stf_count == self.SAME_TOOL_HALT:
            if self.mode == "enforce":
                raise GuardrailHalt(
                    f"same_tool_failure halt: tool {call.name!r} has failed "
                    f"{self.SAME_TOOL_HALT} consecutive times"
                )
            return Message(
                role="system",
                content=(
                    f"{_WARN_MARKER} same_tool_failure halt (downgraded): "
                    f"tool {call.name!r} has failed {self.SAME_TOOL_HALT} "
                    "consecutive times. In 'warn' mode the loop continues."
                ),
            )

        # exact_failure block (Req 9.3).
        if ef_count == self.EXACT_FAILURE_BLOCK:
            if self.mode == "enforce":
                return Message(
                    role="system",
                    content=(
                        f"{_BLOCK_MARKER} exact_failure: tool {call.name!r} "
                        f"has failed {self.EXACT_FAILURE_BLOCK} consecutive "
                        "times with identical arguments; subsequent identical "
                        "dispatches will be refused."
                    ),
                )
            return Message(
                role="system",
                content=(
                    f"{_WARN_MARKER} exact_failure block (downgraded): tool "
                    f"{call.name!r} has failed {self.EXACT_FAILURE_BLOCK} "
                    "consecutive times with identical arguments. In 'warn' "
                    "mode the call is not refused."
                ),
            )

        # exact_failure warn (Req 9.2).
        if ef_count == self.EXACT_FAILURE_WARN:
            return Message(
                role="system",
                content=(
                    f"{_WARN_MARKER} exact_failure: tool {call.name!r} has "
                    f"failed {self.EXACT_FAILURE_WARN} consecutive times "
                    "with identical arguments."
                ),
            )

        # same_tool_failure warn (Req 9.4).
        if stf_count == self.SAME_TOOL_WARN:
            return Message(
                role="system",
                content=(
                    f"{_WARN_MARKER} same_tool_failure: tool {call.name!r} "
                    f"has failed {self.SAME_TOOL_WARN} consecutive times "
                    "across calls."
                ),
            )

        return None

    def _record_success(
        self, call: ToolCall, h: str, result_msg: Message
    ) -> Optional[Message]:
        # Reset the failure counters; the consecutive streaks are broken.
        self.ledger.exact_failures.pop(h, None)
        self.ledger.same_tool_failures.pop(call.name, None)

        content = result_msg.content if result_msg.content is not None else ""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        runs = self.ledger.idempotent_runs.get(h)
        if runs and runs[-1] == content_hash:
            runs = runs + [content_hash]
        else:
            # Either the first successful call for this hash or the
            # output changed; start a new streak.
            runs = [content_hash]

        # Bound the list at the block threshold so memory stays O(1) per
        # hash even on long-running idempotent loops.
        if len(runs) > self.IDEMPOTENT_BLOCK:
            runs = runs[-self.IDEMPOTENT_BLOCK:]
        self.ledger.idempotent_runs[h] = runs

        count = len(runs)

        # idempotent_no_progress block (Req 9.7).
        if count == self.IDEMPOTENT_BLOCK:
            if self.mode == "enforce":
                return Message(
                    role="system",
                    content=(
                        f"{_BLOCK_MARKER} idempotent_no_progress: tool "
                        f"{call.name!r} has produced identical output "
                        f"{self.IDEMPOTENT_BLOCK} consecutive times with "
                        "identical arguments; subsequent identical dispatches "
                        "will be refused."
                    ),
                )
            return Message(
                role="system",
                content=(
                    f"{_WARN_MARKER} idempotent_no_progress block "
                    f"(downgraded): tool {call.name!r} has produced "
                    f"identical output {self.IDEMPOTENT_BLOCK} consecutive "
                    "times. In 'warn' mode the call is not refused."
                ),
            )

        # idempotent_no_progress warn (Req 9.6).
        if count == self.IDEMPOTENT_WARN:
            return Message(
                role="system",
                content=(
                    f"{_WARN_MARKER} idempotent_no_progress: tool "
                    f"{call.name!r} has produced identical output "
                    f"{self.IDEMPOTENT_WARN} consecutive times with "
                    "identical arguments."
                ),
            )

        return None

    @staticmethod
    def _is_failure(result_msg: Message) -> bool:
        """Heuristic failure detection from the result Message body.

        A ``tool`` result is treated as a failure when its content begins
        with one of the markers documented in design §"Error Message
        conventions". This keeps the controller decoupled from the
        dispatcher's internal state — but the dispatcher may always
        override by passing an explicit ``failed`` flag.
        """
        content = result_msg.content or ""
        return any(content.startswith(marker) for marker in _ERROR_MARKERS)
