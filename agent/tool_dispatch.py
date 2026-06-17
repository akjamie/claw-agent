"""
Tool dispatcher — sequential + parallel + safety scheduling.

Implements :class:`ToolDispatcher`, which executes one batch of LLM-emitted
:class:`agent.messages.ToolCall` objects against the gateway's
:class:`gateway.mcp_client.McpManager`. The dispatcher handles four
concerns:

1. **Pre-dispatch guardrails.** :meth:`GuardrailsController.should_dispatch`
   is called for every Tool_Call before it is admitted; a synthetic block
   Message replaces the call when guardrails refuse it (Req 9.3, 9.7).
2. **Safety classification.** Calls are scheduled per design
   §"agent.tool_dispatch":

   - any ``_NEVER_PARALLEL`` tool in the batch → all calls run
     sequentially in LLM-emitted order (Req 8.3);
   - all calls ``_PARALLEL_SAFE`` → fully parallel via the worker pool
     (Req 8.2);
   - mixed / ``_PATH_SCOPED`` → conservative sequential fallback for
     this iteration. The design's path-overlap grouping (Req 8.4) is a
     future enhancement; treating ``_PATH_SCOPED`` as sequential is
     correct (never violates the safety contract), only suboptimal.

3. **Per-call timeout.** Each worker is awaited via
   ``Future.result(timeout=timeout_s)``; on
   :class:`concurrent.futures.TimeoutError` the dispatcher posts a
   synthetic ``tool_timeout`` Message and cancels the future (Req 7.8,
   7.9). Python cannot kill the running thread; the synthetic Message is
   the contract the loop and guardrails see.

4. **Interrupt propagation.** The shared ``threading.Event`` is checked
   between calls (sequential mode) and inside each worker before the MCP
   call (Req 8.6). A single aggregate interruption Message — using the
   loop-level marker ``[claw_chat:interrupted]`` from design §"Error
   Message conventions" — is appended once when an interrupt is seen.

Per-call outcomes are wrapped as ``tool`` Messages whose first content
line uses one of the three error markers when the call failed:
``json_decode_error``, ``mcp_unavailable``, ``tool_timeout``. After every
real result, :meth:`GuardrailsController.record_outcome` is invoked; any
warning Message it produces is appended after the tool result, and a
:class:`GuardrailHalt` is propagated so the loop can perform a clean halt
(Req 9.5).

Design references:
- design.md §"agent.tool_dispatch" — public API and scheduling rules.
- design.md §"Error Message conventions" — markers and Message shapes.
- requirements.md §7.4-7.10, §8.1-8.6 — discovery, parsing, timeout,
  parallelism, interrupt.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeout,
)
from typing import List, Optional

# GuardrailHalt is not imported — it is intentionally propagated up
# through ``record_outcome`` to the loop; the dispatcher does not
# reference the type name directly.
from agent.guardrails import GuardrailsController
from agent.messages import Message, ToolCall
from agent.tool_registry import (
    ToolRegistry,
    _NEVER_PARALLEL,
    _PARALLEL_SAFE,
)
from gateway.mcp_client import McpManager

__all__ = ["ToolDispatcher"]

logger = logging.getLogger(__name__)


# Loop-level marker for the aggregate interruption notice (design
# §"Error Message conventions"). Kept here, alongside the dispatcher's
# other markers, so the interrupt path is grep-discoverable.
_INTERRUPT_MARKER = "[claw_chat:interrupted]"

# Per-result error markers. Identical to the prefixes
# :class:`agent.guardrails.GuardrailsController` watches for, so a tool
# Message produced here is automatically recognised as a failure by the
# guardrail ledger (no explicit ``failed`` flag needed for these three).
_JSON_DECODE_ERROR = "json_decode_error"
_MCP_UNAVAILABLE = "mcp_unavailable"
_TOOL_TIMEOUT = "tool_timeout"

# Cap traceback / error tail at 1 KB per design §"Error Message
# conventions" so a malformed MCP response can't blow up history size.
_ERROR_TAIL_LIMIT = 1024


class ToolDispatcher:
    """Dispatch a batch of Tool_Calls against the MCP_Manager.

    The dispatcher is constructed once per :class:`agent.loop.AgentLoop`
    and reused for every Iteration. Threads are not shared across batches
    — each :meth:`execute` call creates and tears down its own
    :class:`concurrent.futures.ThreadPoolExecutor`. This keeps the
    cancellation story simple (the executor's lifetime is the batch's
    lifetime) at the cost of a small per-batch construction overhead.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        mcp: McpManager,
        guardrails: GuardrailsController,
        max_workers: int = 4,
        timeout_s: int = 300,
    ) -> None:
        self._registry = registry
        self._mcp = mcp
        self._guardrails = guardrails
        # Defensive coercion: Agent_Config defaults guard these too, but
        # this module is also imported directly by tests that may pass
        # raw values. Coercing here avoids ValueError in the executor
        # constructor on a misconfigured ``0``.
        self._max_workers = max(1, int(max_workers) if max_workers else 4)
        self._timeout_s = max(1, int(timeout_s) if timeout_s else 300)

    # ── Public API ───────────────────────────────────────────────────────

    def execute(
        self, batch: List[ToolCall], interrupt: threading.Event
    ) -> List[Message]:
        """Run one Iteration's Tool_Call batch.

        Returns a list of ``tool`` Messages (one per dispatched call,
        possibly preceded or followed by guardrail / interrupt
        ``system`` Messages). The list is in LLM emission order for
        per-call results; aggregate notices (interruption, guardrail
        warnings) are appended at the position they were generated.

        Raises
        ------
        GuardrailHalt
            Propagated from
            :meth:`GuardrailsController.record_outcome` when the
            same-tool halt band fires in ``enforce`` mode (Req 9.5). The
            caller (:class:`agent.loop.AgentLoop`) catches it and
            performs a clean halt; partial results accumulated before
            the halt are lost — by design, since the loop's halt notice
            replaces them.
        """
        results: List[Message] = []
        if not batch:
            return results

        # 1. Pre-dispatch guardrail block check. Block Messages replace
        #    the call (the dispatcher does not run it). Allowed calls
        #    are accumulated in submission order.
        admitted: List[ToolCall] = []
        for call in batch:
            block_msg = self._guardrails.should_dispatch(call)
            if block_msg is not None:
                results.append(block_msg)
                continue
            admitted.append(call)

        if not admitted:
            return results

        # 2. Classify the batch by Parallel_Safety_Class. The default
        #    for any unknown tool is ``_NEVER_PARALLEL`` (Req 8.5),
        #    which biases mixed batches toward sequential execution
        #    even before we explicitly check.
        classes = [self._registry.safety_class(c.name) for c in admitted]
        if any(cls == _NEVER_PARALLEL for cls in classes):
            mode = "sequential"
        elif all(cls == _PARALLEL_SAFE for cls in classes):
            mode = "parallel"
        else:
            # Pure ``_PATH_SCOPED`` (or a mix without any
            # ``_NEVER_PARALLEL``). The design's optimal scheduling
            # groups by non-overlapping paths (Req 8.4); this
            # iteration takes the conservative correct fallback and
            # runs them sequentially. Future work can layer a
            # path-overlap grouper on top of ``_run_parallel``.
            mode = "sequential"

        # 3. Run the batch and remember whether an interrupt was seen.
        if mode == "parallel":
            interrupted = self._run_parallel(admitted, interrupt, results)
        else:
            interrupted = self._run_sequential(admitted, interrupt, results)

        # 4. One aggregate interrupt notice per Iteration (Req 8.6).
        if interrupted:
            results.append(
                Message(
                    role="system",
                    content=(
                        f"{_INTERRUPT_MARKER} tool dispatch interrupted by "
                        "user (Ctrl+C); remaining tool calls were cancelled."
                    ),
                )
            )

        return results

    # ── Scheduling modes ─────────────────────────────────────────────────

    def _run_sequential(
        self,
        calls: List[ToolCall],
        interrupt: threading.Event,
        results: List[Message],
    ) -> bool:
        """Run calls one-at-a-time, in LLM-emitted order.

        Each call is submitted to a single-worker pool so the per-call
        timeout via ``Future.result(timeout=...)`` still applies. Python
        cannot forcibly cancel a running tool thread, but the synthetic
        ``tool_timeout`` Message we post is the contract the loop
        observes (Req 7.9).

        Returns ``True`` when ``interrupt`` was set before all calls were
        dispatched, in which case the caller appends a single aggregate
        interruption notice.
        """
        with ThreadPoolExecutor(max_workers=1) as pool:
            for call in calls:
                if interrupt.is_set():
                    return True

                future = pool.submit(self._invoke_one, call, interrupt)
                msg = self._await_future(future, call)

                # The worker may have short-circuited because interrupt
                # was set after submission; treat as aggregate-cancelled.
                if msg is None:
                    return True

                results.append(msg)
                self._record_and_collect(call, msg, results)
        return False

    def _run_parallel(
        self,
        calls: List[ToolCall],
        interrupt: threading.Event,
        results: List[Message],
    ) -> bool:
        """Run all calls concurrently and gather results in submission order.

        Submission order is preserved because ``zip(calls, futures)``
        walks both lists in parallel; results are appended in the order
        the LLM emitted the calls, not the order workers finished
        (Req 8.2). Per-call timeouts are enforced with
        ``Future.result(timeout=...)``; on timeout the future is
        cancelled (best-effort) and a synthetic ``tool_timeout`` Message
        is posted.

        Returns ``True`` if any call observed the interrupt event.
        """
        interrupted = False
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures: List[Future] = [
                pool.submit(self._invoke_one, call, interrupt) for call in calls
            ]

            for call, future in zip(calls, futures):
                if interrupt.is_set():
                    # Best-effort cancel for the rest; in-flight workers
                    # see the interrupt and return early on their own.
                    future.cancel()
                    interrupted = True
                    continue

                msg = self._await_future(future, call)
                if msg is None:
                    # Worker short-circuited on interrupt.
                    interrupted = True
                    continue

                results.append(msg)
                self._record_and_collect(call, msg, results)
        return interrupted

    # ── Worker + result handling ─────────────────────────────────────────

    def _invoke_one(
        self, call: ToolCall, interrupt: threading.Event
    ) -> Optional[Message]:
        """Worker body: parse args, call MCP, wrap the outcome.

        Returns ``None`` when the worker observes the interrupt event
        before completing the call; the caller treats this as part of
        the aggregate cancellation. Returns a ``tool`` Message in every
        other case — successful call, JSON parse failure, MCP failure,
        or unexpected exception inside the MCP client.

        The arguments parser accepts either ``{}`` (treated as empty
        args) or a JSON object; arrays / scalars / non-dict values are
        rejected as a JSON decode error so the MCP layer never receives
        a payload it cannot use.
        """
        # Honour interrupt before any expensive work.
        if interrupt.is_set():
            return None

        # 1. Parse arguments. Per Req 7.5, malformed JSON becomes a
        #    normal tool error and the MCP call is skipped.
        args_json = call.arguments_json or ""
        if args_json.strip() == "":
            args: object = {}
        else:
            try:
                args = json.loads(args_json)
            except (json.JSONDecodeError, ValueError) as exc:
                return self._json_error_message(call, str(exc))

        if not isinstance(args, dict):
            return self._json_error_message(
                call, "tool arguments must decode to a JSON object"
            )

        # Re-check interrupt: parsing arguments can race with Ctrl+C in
        # parallel mode if many tools share one batch.
        if interrupt.is_set():
            return None

        # 2a. Native (non-MCP) tool — invoke the registered callable directly.
        native_handler = self._registry.get_native_handler(call.name)
        if native_handler is not None:
            try:
                result_text = native_handler(args)
            except Exception as exc:  # noqa: BLE001 — defensive boundary
                logger.exception(
                    "Native tool handler for %r raised", call.name
                )
                return self._mcp_error_message(
                    call, f"{type(exc).__name__}: {exc}"
                )
            return Message(
                role="tool",
                tool_call_id=call.id,
                tool_name=call.name,
                content=result_text,
            )

        # 2b. MCP tool — call the gateway manager.
        # The manager returns an McpToolResult for every error path; we only
        # catch unexpected exceptions defensively (e.g., a transport bug).
        try:
            result = self._mcp.call_tool_by_name(call.name, args)
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            logger.exception(
                "Unexpected error invoking MCP tool %r", call.name
            )
            return self._mcp_error_message(
                call, f"{type(exc).__name__}: {exc}"
            )

        if not result.success:
            return self._mcp_error_message(
                call, result.error or result.content or "tool failed"
            )

        # 3. Successful call. Tool result content is whatever the MCP
        #    server emitted; we do not transform it.
        return Message(
            role="tool",
            tool_call_id=call.id,
            tool_name=call.name,
            content=result.content,
        )

    def _await_future(
        self, future: Future, call: ToolCall
    ) -> Optional[Message]:
        """Block on ``future`` honouring the per-call timeout.

        Returns the worker's Message, or a synthetic ``tool_timeout``
        Message on :class:`FutureTimeout`. Returns ``None`` only when
        the worker itself returned ``None`` (interrupt short-circuit).
        Unexpected exceptions from the worker are wrapped as MCP errors
        so the loop sees a normal tool failure rather than a crash.
        """
        try:
            return future.result(timeout=self._timeout_s)
        except FutureTimeout:
            future.cancel()
            return self._timeout_message(call)
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            logger.exception(
                "Tool worker for %r raised; converting to mcp_unavailable",
                call.name,
            )
            return self._mcp_error_message(
                call, f"{type(exc).__name__}: {exc}"
            )

    def _record_and_collect(
        self,
        call: ToolCall,
        msg: Message,
        results: List[Message],
    ) -> None:
        """Update the guardrail ledger and append any band Message.

        :class:`GuardrailHalt` is intentionally not caught: the loop
        catches it at the outer boundary and performs a clean halt with
        the dedicated halt notice. See Req 9.5.
        """
        outcome = self._guardrails.record_outcome(call, msg)
        if outcome is not None:
            results.append(outcome)

    # ── Synthetic Message constructors ───────────────────────────────────

    def _json_error_message(self, call: ToolCall, detail: str) -> Message:
        """Build the ``json_decode_error`` ``tool`` Message (Req 7.5)."""
        return Message(
            role="tool",
            tool_call_id=call.id,
            tool_name=call.name,
            content=f"{_JSON_DECODE_ERROR}: {_clip(detail)}",
        )

    def _mcp_error_message(self, call: ToolCall, detail: str) -> Message:
        """Build the ``mcp_unavailable`` ``tool`` Message (Req 7.7)."""
        return Message(
            role="tool",
            tool_call_id=call.id,
            tool_name=call.name,
            content=f"{_MCP_UNAVAILABLE}: {_clip(detail)}",
        )

    def _timeout_message(self, call: ToolCall) -> Message:
        """Build the ``tool_timeout`` ``tool`` Message (Req 7.9)."""
        return Message(
            role="tool",
            tool_call_id=call.id,
            tool_name=call.name,
            content=(
                f"{_TOOL_TIMEOUT}: tool {call.name!r} did not complete "
                f"within {self._timeout_s}s; the worker was cancelled."
            ),
        )


def _clip(text: str, limit: int = _ERROR_TAIL_LIMIT) -> str:
    """Trim a long error tail to ``limit`` chars, preserving the head.

    Design §"Error Message conventions" caps the trailing detail at
    1 KB so a misbehaving MCP server can't bloat the conversation
    history (and therefore the next prompt). The clip preserves the
    head, which usually carries the most actionable information.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "... [truncated]"
