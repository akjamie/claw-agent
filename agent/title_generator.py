"""
Auxiliary title-generation call for chat sessions.

Implements :class:`TitleGenerator`, a thin wrapper around
:class:`agent.llm_client.LLMClient` that produces a short, single-line
session title from the early messages of a conversation. The result is
stored on the ``sessions`` table by the loop; this module is purely
a generator and never persists anything itself.

Design references:
- design.md §"agent.title_generator" — class and method shapes.
- requirements.md §4.1 — title generation eligibility (loop-side trigger).
- requirements.md §4.2 — output bounded to 1..80 characters inclusive.
- requirements.md §4.3 — return ``None`` on provider error/empty so the
  loop can retry on the next turn.

Behaviour summary:

- Builds a tiny two-message prompt: a system instruction plus a
  user-role payload that quotes the first up-to-4 user messages.
- Calls :meth:`LLMClient.chat` (non-streaming) with ``max_tokens=40``.
- Reads ``choices[0].message.content``, strips whitespace, removes any
  embedded newlines (Req 4.2: titles are single-line), and truncates
  to 80 characters.
- Never raises: any provider error, malformed response, or empty result
  collapses to ``None`` so the caller can simply try again next turn.
"""

from __future__ import annotations

from typing import List, Optional

from agent.llm_client import LLMClient, ProviderHTTPError
from agent.messages import Message

__all__ = ["TitleGenerator"]


# Maximum characters for a generated title, per Req 4.2.
_MAX_TITLE_CHARS = 80

# Number of leading user messages used as input to the title prompt.
# Four is enough to capture the topic of most conversations without
# inflating the token cost of an auxiliary call.
_USER_MESSAGES_FOR_PROMPT = 4

# Token budget for the title call. 40 tokens covers an 80-char title
# (≈ 20 tokens by char/4 heuristic) with comfortable headroom for
# providers that pad with whitespace or short prefixes we then strip.
_TITLE_MAX_TOKENS = 40

_SYSTEM_INSTRUCTION = (
    "You are a session title generator. Read the conversation and "
    "produce a concise title (max 80 chars, no newlines, no quotes). "
    "Output ONLY the title text."
)


class TitleGenerator:
    """Generate a short title for a chat session via an auxiliary LLM call.

    The generator is stateless across calls — construct one instance per
    ``claw chat`` invocation and reuse it. Trigger logic (when to call,
    when to skip because a title already exists) lives in the loop;
    this class only produces a candidate string.
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def generate(self, messages: List[Message]) -> Optional[str]:
        """Produce a candidate title from a conversation, or ``None`` on failure.

        The conversation's first up-to-4 user messages are quoted into a
        small prompt. The provider is asked for at most ``_TITLE_MAX_TOKENS``
        completion tokens. The returned text is stripped of whitespace
        and newlines and truncated to 80 characters.

        Returns:
            A non-empty string of length ``1..80`` on success, or
            ``None`` when the provider errors, the response is empty or
            malformed, or any other exception escapes the call. The
            method never raises (Req 4.3).
        """
        try:
            prompt_messages = self._build_prompt(messages)
            if prompt_messages is None:
                return None

            response = self._llm.chat(
                prompt_messages, max_tokens=_TITLE_MAX_TOKENS
            )

            raw_content = self._extract_content(response)
            if raw_content is None:
                return None

            cleaned = self._clean_title(raw_content)
            if not cleaned:
                return None
            return cleaned
        except ProviderHTTPError:
            # Network / HTTP failures leave the title empty so the loop
            # retries on the next user turn (Req 4.3).
            return None
        except Exception:
            # Any other unexpected error (malformed response, encoding
            # issue, etc.) collapses to None — the loop will retry and a
            # persistent failure is harmless because the title is purely
            # cosmetic.
            return None

    # ----- internal helpers --------------------------------------------

    @staticmethod
    def _build_prompt(messages: List[Message]) -> Optional[List[dict]]:
        """Assemble the [system, user] message pair sent to the LLM.

        Returns ``None`` when the conversation contains no usable user
        text — without any user content the model cannot ground the
        title and we should not bill the provider.
        """
        user_excerpts: List[str] = []
        for msg in messages:
            if msg.role != "user":
                continue
            text = (msg.content or "").strip()
            if not text:
                continue
            user_excerpts.append(text)
            if len(user_excerpts) >= _USER_MESSAGES_FOR_PROMPT:
                break

        if not user_excerpts:
            return None

        # Format excerpts as a numbered list so the model treats each as
        # a distinct turn rather than concatenating them into one prose
        # blob.
        excerpt_block = "\n".join(
            f"{i + 1}. {text}" for i, text in enumerate(user_excerpts)
        )
        user_payload = (
            "Generate a concise title for the conversation that begins "
            "with these user messages:\n\n" + excerpt_block
        )

        return [
            {"role": "system", "content": _SYSTEM_INSTRUCTION},
            {"role": "user", "content": user_payload},
        ]

    @staticmethod
    def _extract_content(response: object) -> Optional[str]:
        """Pull ``choices[0].message.content`` from a chat-completions response.

        Tolerant of missing keys and unexpected types — returns ``None``
        whenever the expected path is absent rather than raising, so
        :meth:`generate` can fall back to "no title" cleanly.
        """
        if not isinstance(response, dict):
            return None
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        message = first.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if not isinstance(content, str):
            return None
        return content

    @staticmethod
    def _clean_title(raw: str) -> str:
        """Normalise a raw model output into a single-line bounded title.

        Strips outer whitespace, replaces any embedded newline / carriage
        return with a space, collapses runs of whitespace, and truncates
        the result to 80 characters (Req 4.2).
        """
        # Replace newlines (and tabs) with spaces so a multi-line
        # response collapses to a single line.
        flattened = (
            raw.replace("\r\n", " ")
            .replace("\n", " ")
            .replace("\r", " ")
            .replace("\t", " ")
        )
        # Collapse runs of whitespace introduced by the replacement.
        collapsed = " ".join(flattened.split())
        if len(collapsed) > _MAX_TITLE_CHARS:
            collapsed = collapsed[:_MAX_TITLE_CHARS]
        return collapsed
