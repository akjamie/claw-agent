"""
Message and ToolCall dataclasses for the claw-agent runtime.

This module is the foundation of the agent runtime's data layer. It defines
the in-memory shape of conversation messages, their canonical JSON encoding,
and the bidirectional mapping between Message instances and SQLite rows
or OpenAI-format chat-completions payloads.

Design references:
- design.md §"agent.messages" — Message/ToolCall shapes and encoding rules.
- design.md §"Tool argument canonicalisation" — sort_keys=True invariant.
- requirements.md §19.1, §19.2 — round-trip persistence + JSON canonicalisation.
- requirements.md §9.1 — Tool_Hash determinism uses canonical_args.
- requirements.md §3.4 — messages table column layout.

The module is pure: no I/O, no logging, no side effects. Every function
is deterministic given its inputs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "Message",
    "ToolCall",
    "canonical_args",
    "COMPRESSION_SUMMARY_TOOL_NAME",
]


# Sentinel tool_name attached to compression-summary system messages so they
# are never re-pruned by Phase 1 of the compressor. Defined here because
# both the persistence layer and the compressor reference it.
COMPRESSION_SUMMARY_TOOL_NAME = "__compression_summary__"


def canonical_args(args_obj_or_str: Any) -> str:
    """Return a deterministic JSON-text representation of tool arguments.

    Used by both `Message` encoding (for the `tool_arguments` field) and by
    `agent.guardrails.tool_hash` to ensure that semantically-equal argument
    values produce byte-identical strings regardless of dict insertion
    order or whitespace.

    Behavior:
    - dict / list / scalar input → ``json.dumps(obj, sort_keys=True,
      ensure_ascii=False)``.
    - str input that parses as JSON → re-encode the parsed value with
      ``sort_keys=True``.
    - str input that does not parse as JSON → returned unchanged. The
      raw string is preserved so the dispatcher can later post a precise
      ``json_decode_error`` Message (Req 7.5) instead of swallowing the
      malformed payload here.
    - ``None`` → empty string ``""``.

    Validates: Req 9.1 (Tool_Hash deterministic input), Req 19.2
    (byte-equal canonical JSON for equal-valued objects).
    """
    if args_obj_or_str is None:
        return ""
    if isinstance(args_obj_or_str, str):
        try:
            parsed = json.loads(args_obj_or_str)
        except (json.JSONDecodeError, ValueError):
            return args_obj_or_str
        return json.dumps(parsed, sort_keys=True, ensure_ascii=False)
    return json.dumps(args_obj_or_str, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class ToolCall:
    """One LLM-emitted request to invoke a tool.

    `arguments_json` is the raw JSON text emitted by the model. It is
    intentionally stored verbatim — JSON validation happens in the
    dispatcher, where a parse failure becomes a normal tool-error
    Message rather than a crash inside the streaming reader.
    """

    id: str
    name: str
    arguments_json: str = ""


@dataclass(frozen=True)
class Message:
    """One row of conversation history.

    Field semantics:
    - ``role`` is one of ``system``, ``user``, ``assistant``, ``tool``.
    - ``content`` is the text payload; never ``None`` (the empty string is
      used for assistant messages that emitted only ``tool_calls``).
    - ``tool_call_id`` / ``tool_name`` are populated on ``tool`` messages;
      may also be set on system messages produced by the compressor
      (``tool_name == "__compression_summary__"``).
    - ``tool_arguments`` is canonical JSON text (sort_keys=True). On an
      ``assistant`` message that emits tool calls, it carries the JSON
      encoding of ``[{"id","name","arguments"}, ...]``. On a ``tool``
      message it may carry the canonical arguments echoed back for
      traceability. ``None`` when not used.
    - ``timestamp`` is unix-epoch seconds. ``0`` is a sentinel meaning
      "fill at insert time" — the persistence layer substitutes
      ``int(time.time())`` before the row is written.
    - ``id`` is the SQLite ``rowid`` for the persisted row, or ``None``
      until persisted.

    The dataclass is frozen so Messages are hashable and safe to share
    across threads (the dispatcher passes them between worker futures
    and the loop).
    """

    role: str
    content: str = ""
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_arguments: Optional[str] = None
    timestamp: int = 0
    id: Optional[int] = None
    reasoning_content: Optional[str] = None  # DeepSeek thinking mode

    # ----- OpenAI chat-completions format ---------------------------------

    def to_openai(self) -> dict:
        """Emit the canonical OpenAI request format for this Message.

        - system / user → ``{"role": ..., "content": ...}``
        - assistant without tool calls → same shape as user/system
        - assistant with tool calls → adds the ``tool_calls`` array
          decoded from ``tool_arguments``
        - tool → ``{"role": "tool", "content": ..., "tool_call_id": ...,
          "name": ...}``

        The output contains only OpenAI-recognised keys; agent-private
        metadata such as ``id`` and ``timestamp`` is dropped.
        """
        if self.role == "tool":
            return {
                "role": "tool",
                "content": self.content,
                "tool_call_id": self.tool_call_id or "",
                "name": self.tool_name or "",
            }

        if self.role == "assistant" and self.tool_arguments:
            tool_calls = _decode_assistant_tool_calls(self.tool_arguments)
            if tool_calls:
                d = {
                    "role": "assistant",
                    "content": self.content,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in tool_calls
                    ],
                }
                if self.reasoning_content:
                    d["reasoning_content"] = self.reasoning_content
                return d

        d = {"role": self.role, "content": self.content}
        if self.role == "assistant" and self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        return d

    @classmethod
    def from_openai(cls, raw: dict) -> "Message":
        """Build a Message from an OpenAI-format dict.

        Inverse of `to_openai` for assistant + tool roles. The optional
        ``tool_calls`` array is canonicalised into ``tool_arguments``
        via ``canonical_args`` so byte-equal output is produced for
        semantically-equal inputs (Req 19.2).
        """
        role = raw.get("role", "user")
        content = raw.get("content")
        if content is None:
            content = ""

        tool_call_id: Optional[str] = None
        tool_name: Optional[str] = None
        tool_arguments: Optional[str] = None

        if role == "tool":
            tool_call_id = raw.get("tool_call_id")
            tool_name = raw.get("name")
        elif role == "assistant":
            raw_calls = raw.get("tool_calls") or []
            if raw_calls:
                encoded = []
                for tc in raw_calls:
                    fn = tc.get("function") or {}
                    encoded.append(
                        {
                            "id": tc.get("id", "") or "",
                            "name": fn.get("name", "") or "",
                            # Arguments are kept as a string verbatim from
                            # the model; if the provider has already
                            # parsed them into a dict, re-encode
                            # canonically.
                            "arguments": _coerce_arguments_field(
                                fn.get("arguments", "")
                            ),
                        }
                    )
                tool_arguments = json.dumps(
                    encoded, sort_keys=True, ensure_ascii=False
                )

        return cls(
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
        )

    # ----- SQLite row format ----------------------------------------------

    def to_db_row(self, session_id: str) -> tuple:
        """Return the parameter tuple for an INSERT into ``messages``.

        Column order matches the ``messages`` table definition in
        design.md §"SQLite schema", excluding the autoincrement ``id``:
        ``(session_id, role, content, tool_call_id, tool_name,
        tool_arguments, timestamp)``.

        ``tool_arguments`` is re-canonicalised here so any caller that
        constructs a Message with non-canonical JSON still ends up with
        a deterministic row (Req 19.2). ``None`` round-trips as ``None``.
        """
        canonical_tool_args: Optional[str]
        if self.tool_arguments is None:
            canonical_tool_args = None
        else:
            canonical_tool_args = canonical_args(self.tool_arguments)

        return (
            session_id,
            self.role,
            self.content,
            self.tool_call_id,
            self.tool_name,
            canonical_tool_args,
            int(self.timestamp),
        )

    @classmethod
    def from_db_row(cls, row: tuple) -> "Message":
        """Reconstruct a Message from a ``messages`` table row.

        Expected column order (matching ``SELECT * FROM messages``):
        ``(id, session_id, role, content, tool_call_id, tool_name,
        tool_arguments, timestamp)``. The ``session_id`` field is dropped
        because Messages do not carry it in memory; the active session
        is tracked by the caller.

        Round-trip guarantee: for any Message ``m`` previously inserted
        via ``to_db_row``, ``Message.from_db_row(loaded_row)`` returns a
        Message whose ``(role, content, tool_call_id, tool_name,
        tool_arguments)`` tuple equals ``m``'s (Req 19.1).
        """
        if len(row) < 8:
            raise ValueError(
                f"messages row must have 8 columns, got {len(row)}: {row!r}"
            )
        (
            row_id,
            _session_id,
            role,
            content,
            tool_call_id,
            tool_name,
            tool_arguments,
            timestamp,
        ) = row[:8]

        return cls(
            role=role,
            content=content if content is not None else "",
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            timestamp=int(timestamp) if timestamp is not None else 0,
            id=int(row_id) if row_id is not None else None,
        )


# ---------- private helpers ------------------------------------------------


def _coerce_arguments_field(value: Any) -> str:
    """Normalise the ``function.arguments`` field of a tool call to a string.

    OpenAI-compatible providers transmit ``arguments`` as a JSON string,
    but a few clients pre-parse it into a dict. We accept either and
    always store a JSON string so downstream encoders can pass the
    payload through verbatim.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _decode_assistant_tool_calls(tool_arguments: str) -> list:
    """Decode the assistant ``tool_arguments`` JSON into a list of dicts.

    Returns an empty list when the field is absent, empty, malformed, or
    not a JSON array. Defensive: a malformed payload here must never
    crash the OpenAI-format encoder, since the dispatcher already
    posts a tool-error Message for the same condition (Req 7.5).
    """
    if not tool_arguments:
        return []
    try:
        decoded = json.loads(tool_arguments)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    out = []
    for entry in decoded:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "id": entry.get("id", "") or "",
                "name": entry.get("name", "") or "",
                "arguments": _coerce_arguments_field(
                    entry.get("arguments", "")
                ),
            }
        )
    return out
