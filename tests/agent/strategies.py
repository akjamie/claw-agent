"""
Shared Hypothesis strategies for claw-agent property-based tests.

Generators here are used across the persistence, compression, guardrails,
and config property tests. They produce data shapes that already satisfy
the runtime's structural invariants (e.g. every ``tool`` message has a
matching upstream ``assistant`` message that emitted that
``tool_call_id``) so each property test can focus on the behaviour
under examination rather than reconstructing scaffolding for every
example.

Generated strings are bounded (``max_size=200``) and recursive depth is
capped to keep example generation fast — property tests run on every
PR, so wall-clock time matters.

Validates: Requirements 19.1, 19.2 — these strategies feed the
round-trip persistence and canonical-JSON properties.
"""

from __future__ import annotations

import json
from typing import Any, List

from hypothesis import strategies as st

from agent.messages import Message, ToolCall

__all__ = [
    "message_strategy",
    "system_message_strategy",
    "user_message_strategy",
    "assistant_message_strategy",
    "tool_message_strategy",
    "tool_call_strategy",
    "conversation_history_strategy",
    "json_args_strategy",
    "agent_config_dict_strategy",
]


# ── Primitive building blocks ────────────────────────────────────────────────

# Keep generated strings reasonable so tests stay fast (≤200 chars).
_TEXT = st.text(min_size=0, max_size=200)
_NONEMPTY_TEXT = st.text(min_size=1, max_size=80)

# Identifier-ish strings for tool-call ids / tool names. ``from_regex`` with
# ``fullmatch=True`` keeps the values shaped like real provider output
# (``call_abc123``, ``mcp.tool_name``) while staying bounded.
_TOOL_CALL_ID = st.from_regex(r"[A-Za-z0-9_]{1,24}", fullmatch=True)
_TOOL_NAME = st.from_regex(r"[A-Za-z][A-Za-z0-9_.-]{0,39}", fullmatch=True)

_TIMESTAMP = st.integers(min_value=0, max_value=2_000_000_000)
_ROW_ID = st.one_of(st.none(), st.integers(min_value=1, max_value=1_000_000))


# ── JSON-encodable argument objects ──────────────────────────────────────────


def _json_scalar() -> st.SearchStrategy[Any]:
    """One leaf value in a JSON-encodable object: scalar or null."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10**9), max_value=10**9),
        # Finite floats only — ``json.dumps`` rejects NaN/inf with the
        # ``allow_nan=False`` flag the runtime uses for canonical output.
        st.floats(allow_nan=False, allow_infinity=False),
        _TEXT,
    )


# Recursive nested dicts/lists/scalars — the shape of an LLM-emitted
# ``arguments`` payload. Capped to ``max_leaves=12`` to keep canonical
# JSON encoding cheap on every example.
json_args_strategy: st.SearchStrategy[Any] = st.recursive(
    _json_scalar(),
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(_NONEMPTY_TEXT, children, max_size=5),
    ),
    max_leaves=12,
)


def _canonical_json(obj: Any) -> str:
    """Serialise an object the same way ``Message.to_db_row`` does."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


# ── ToolCall ────────────────────────────────────────────────────────────────

tool_call_strategy = st.builds(
    ToolCall,
    id=_TOOL_CALL_ID,
    name=_TOOL_NAME,
    arguments_json=json_args_strategy.map(_canonical_json),
)


# ── Per-role Message strategies ─────────────────────────────────────────────


def _assistant_tool_calls_array() -> st.SearchStrategy[str]:
    """A canonical-JSON encoding of the assistant ``tool_calls`` field.

    Mirrors the format produced by ``Message.from_openai`` for an
    assistant message that emitted tool calls: a JSON array of
    ``{"id", "name", "arguments"}`` dicts where ``arguments`` is itself
    a canonical-JSON string.
    """
    one_call = st.builds(
        lambda i, n, a: {"id": i, "name": n, "arguments": _canonical_json(a)},
        i=_TOOL_CALL_ID,
        n=_TOOL_NAME,
        a=json_args_strategy,
    )
    return st.lists(one_call, min_size=1, max_size=4).map(
        lambda items: json.dumps(items, sort_keys=True, ensure_ascii=False)
    )


system_message_strategy = st.builds(
    Message,
    role=st.just("system"),
    content=_TEXT,
    tool_call_id=st.none(),
    tool_name=st.none(),
    tool_arguments=st.none(),
    timestamp=_TIMESTAMP,
    id=_ROW_ID,
)

user_message_strategy = st.builds(
    Message,
    role=st.just("user"),
    content=_TEXT,
    tool_call_id=st.none(),
    tool_name=st.none(),
    tool_arguments=st.none(),
    timestamp=_TIMESTAMP,
    id=_ROW_ID,
)

# Assistant messages either end the turn with plain text (tool_arguments
# is None) or emit a tool_calls array (tool_arguments carries the
# canonical-JSON encoding).
assistant_message_strategy = st.builds(
    Message,
    role=st.just("assistant"),
    content=_TEXT,
    tool_call_id=st.none(),
    tool_name=st.none(),
    tool_arguments=st.one_of(st.none(), _assistant_tool_calls_array()),
    timestamp=_TIMESTAMP,
    id=_ROW_ID,
)

# Tool messages always carry tool_call_id + tool_name; tool_arguments may
# echo the canonical args back for traceability or be omitted entirely.
tool_message_strategy = st.builds(
    Message,
    role=st.just("tool"),
    content=_TEXT,
    tool_call_id=_TOOL_CALL_ID,
    tool_name=_TOOL_NAME,
    tool_arguments=st.one_of(st.none(), json_args_strategy.map(_canonical_json)),
    timestamp=_TIMESTAMP,
    id=_ROW_ID,
)

message_strategy = st.one_of(
    system_message_strategy,
    user_message_strategy,
    assistant_message_strategy,
    tool_message_strategy,
)


# ── Conversation histories with tool linkage invariants ─────────────────────


@st.composite
def conversation_history_strategy(draw) -> List[Message]:
    """Generate a conversation that satisfies the assistant→tool linkage.

    Shape: ``[system?, (user, assistant, [assistant + tool*]?)+]``. Every
    ``tool`` message in the result carries a ``tool_call_id`` that
    matches a prior ``assistant`` message's ``tool_calls`` entry — the
    invariant the 5-phase compressor's Phase 5 sanitiser relies on
    (Req 10.6) and that ``Message.from_openai`` round-trips depend on.
    """
    messages: List[Message] = []

    # Optional leading system message.
    if draw(st.booleans()):
        messages.append(draw(system_message_strategy))

    num_turns = draw(st.integers(min_value=1, max_value=4))
    for _ in range(num_turns):
        # Each turn starts with a user message.
        messages.append(draw(user_message_strategy))

        # Then 1–3 assistant steps, the last of which is always a plain
        # text reply (no tool_calls) to terminate the turn.
        num_steps = draw(st.integers(min_value=1, max_value=3))
        for step in range(num_steps):
            is_last_step = step == num_steps - 1
            emit_tools = (not is_last_step) and draw(st.booleans())

            if emit_tools:
                # Emit assistant message with tool_calls + matching tool replies.
                num_calls = draw(st.integers(min_value=1, max_value=3))
                calls = []
                for _ in range(num_calls):
                    calls.append(
                        {
                            "id": draw(_TOOL_CALL_ID),
                            "name": draw(_TOOL_NAME),
                            "arguments": _canonical_json(draw(json_args_strategy)),
                        }
                    )
                tool_args_field = json.dumps(
                    calls, sort_keys=True, ensure_ascii=False
                )
                messages.append(
                    Message(
                        role="assistant",
                        content=draw(_TEXT),
                        tool_arguments=tool_args_field,
                        timestamp=draw(_TIMESTAMP),
                        id=draw(_ROW_ID),
                    )
                )
                # Tool replies in the same order as the calls.
                for call in calls:
                    messages.append(
                        Message(
                            role="tool",
                            content=draw(_TEXT),
                            tool_call_id=call["id"],
                            tool_name=call["name"],
                            tool_arguments=call["arguments"],
                            timestamp=draw(_TIMESTAMP),
                            id=draw(_ROW_ID),
                        )
                    )
            else:
                # Plain assistant text response.
                messages.append(
                    Message(
                        role="assistant",
                        content=draw(_TEXT),
                        tool_arguments=None,
                        timestamp=draw(_TIMESTAMP),
                        id=draw(_ROW_ID),
                    )
                )
                # A plain text reply ends the assistant's contribution
                # for this turn.
                break

    return messages


# ── AgentConfig dict generators ─────────────────────────────────────────────

# Per-key valid-value strategies. Ranges match the validators in
# ``agent.config`` (positive ints, open-unit floats, exact mode strings).
_VALID_AGENT_CONFIG_VALUES: dict[str, st.SearchStrategy[Any]] = {
    "max_iterations": st.integers(min_value=1, max_value=10_000),
    "context_compression_threshold": st.floats(
        min_value=0.0001,
        max_value=0.9999,
        allow_nan=False,
        allow_infinity=False,
        exclude_min=False,
        exclude_max=False,
    ),
    "protected_tail_fraction": st.floats(
        min_value=0.0001,
        max_value=0.9999,
        allow_nan=False,
        allow_infinity=False,
    ),
    "max_tool_workers": st.integers(min_value=1, max_value=64),
    "tool_call_timeout_seconds": st.integers(min_value=1, max_value=3600),
    "guardrails_mode": st.sampled_from(["warn", "enforce"]),
    "default_context_window": st.integers(min_value=1, max_value=2_000_000),
    "model_context_windows": st.dictionaries(
        _NONEMPTY_TEXT,
        st.integers(min_value=1, max_value=2_000_000),
        max_size=4,
    ),
    "summary_floor_tokens": st.integers(min_value=1, max_value=10_000),
    "summary_cap_tokens": st.integers(min_value=1, max_value=100_000),
    "summary_fraction": st.floats(
        min_value=0.0001,
        max_value=0.9999,
        allow_nan=False,
        allow_infinity=False,
    ),
}

# Values that should fail every validator: zero, negatives, out-of-range
# floats, wrong types, and the bool sentinels (which are explicitly
# rejected so they don't masquerade as ints).
_INVALID_VALUE = st.one_of(
    st.just(0),
    st.just(-1),
    st.just(0.0),
    st.just(1.0),
    st.just(1.5),
    st.just(True),
    st.just(False),
    st.none(),
    st.text(max_size=20).filter(lambda s: s not in {"warn", "enforce"}),
    st.lists(st.integers(), max_size=3),
)


@st.composite
def agent_config_dict_strategy(draw) -> dict:
    """A dict shaped like the ``agent`` block of ``~/.claw/config.json``.

    Each known key is independently included or omitted; included keys
    are independently filled with a valid or invalid value. The
    resulting dict exercises both the happy-path defaulting (Req 14.3)
    and the invalid-value warning path (Req 14.4) of
    :func:`agent.config.load_agent_config`.
    """
    keys = list(_VALID_AGENT_CONFIG_VALUES.keys())
    out: dict[str, Any] = {}

    for key in keys:
        if not draw(st.booleans()):
            continue  # key omitted → loader uses default
        if draw(st.booleans()):
            out[key] = draw(_VALID_AGENT_CONFIG_VALUES[key])
        else:
            out[key] = draw(_INVALID_VALUE)

    # Optionally toss in a few unknown keys; the loader must ignore them.
    if draw(st.booleans()):
        for i in range(draw(st.integers(min_value=0, max_value=3))):
            out[f"_unknown_{i}"] = draw(
                st.one_of(_TEXT, st.integers(), st.booleans())
            )

    return out
