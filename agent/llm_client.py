"""
OpenAI-compatible chat-completions client (HTTP + SSE).

Implements :class:`LLMClient`, the streaming and non-streaming chat client
used by the agent loop (streaming), the context compressor (non-streaming
with ``max_tokens``), and the title generator (non-streaming, no tools).
The client targets any provider that speaks OpenAI's chat-completions
protocol — currently OpenRouter, Nous, MiniMax, and NVIDIA per
``cli.providers.PROVIDER_INFO``.

Design references:
- design.md §"agent.llm_client" — class and method shapes.
- design.md §"Stream accumulator" — :class:`_ToolCallAccumulator` semantics.
- requirements.md §5.4, §5.5 — OpenAI-compatible chat completions.
- requirements.md §13.1, §13.4, §13.5 — streaming, interrupt-finalisation,
  and tool-calls-only short-circuit.

Implementation notes:

- Built directly on ``requests`` (already a runtime dep). No ``openai`` SDK.
- Streaming uses ``requests.post(..., stream=True)`` and parses the SSE
  framing manually (``data: {...}`` lines, terminated by ``data: [DONE]``).
  This is portable across every provider in PROVIDER_INFO and avoids the
  SDK's hard pin on a specific OpenAI host.
- The accumulator never parses JSON arguments. Tool-arguments validation
  lives in :mod:`agent.tool_dispatch`, where a parse failure becomes a
  normal ``json_decode_error`` tool Message (Req 7.5) instead of crashing
  the reader.
- The ``interrupt`` ``threading.Event`` is checked between SSE frames so
  a Ctrl+C in the TUI causes the stream to finalise the partial assistant
  message and return promptly (Req 13.4).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests

from agent.messages import ToolCall

__all__ = [
    "LLMClient",
    "StreamResult",
    "StreamStatus",
    "ProviderHTTPError",
]


class ProviderHTTPError(RuntimeError):
    """Raised when the provider returns a non-2xx response.

    Carries ``status_code`` and the (possibly truncated) response body so
    the loop can convert it into a single-line user-visible message per
    design §"Error taxonomy".
    """

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"HTTP {status_code}: {body[:300]}")
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True)
class StreamStatus:
    """Lightweight value type passed to ``on_status`` callbacks.

    ``LLMClient`` itself does not throttle these emissions — it fires one
    after each text or tool-call delta and lets the caller throttle to
    ≤2 Hz per Req 13.3 (the TUI's Status_Line is the throttling consumer).
    """

    content_chars: int = 0
    tool_call_count: int = 0
    finish_reason: Optional[str] = None


@dataclass(frozen=True)
class StreamResult:
    """Final state of one streaming chat-completions call."""

    content: str
    tool_calls: list[ToolCall]
    finish_reason: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    reasoning_content: Optional[str] = None  # DeepSeek thinking mode


class _ToolCallAccumulator:
    """Builds full :class:`ToolCall` objects from streamed SSE deltas.

    Per the OpenAI streaming protocol, tool-call deltas arrive piecewise:

    - ``id`` and ``function.name`` typically appear in the first delta
      for a given ``index`` and never change afterwards;
    - ``function.arguments`` arrives as a stream of JSON-text fragments
      that must be concatenated in arrival order.

    The accumulator stores per-index slots and concatenates arguments
    verbatim. JSON validation is *not* performed here; the dispatcher
    decides whether the final string parses (Req 7.5).
    """

    def __init__(self) -> None:
        # index -> {"id": str, "name": str, "arguments": str}
        self._slots: dict[int, dict[str, str]] = {}

    def feed_delta(self, delta_tool_calls: Any) -> None:
        """Merge one ``delta.tool_calls`` array into the accumulator.

        Tolerates malformed or partial entries (non-dict items, missing
        ``index``) by skipping them — the surrounding stream reader
        treats malformed frames as best-effort.
        """
        if not isinstance(delta_tool_calls, list):
            return
        for entry in delta_tool_calls:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("index")
            if not isinstance(idx, int):
                continue
            slot = self._slots.setdefault(
                idx, {"id": "", "name": "", "arguments": ""}
            )
            entry_id = entry.get("id")
            if isinstance(entry_id, str) and entry_id:
                slot["id"] = entry_id
            fn = entry.get("function")
            if isinstance(fn, dict):
                fn_name = fn.get("name")
                if isinstance(fn_name, str) and fn_name:
                    slot["name"] = fn_name
                fn_args = fn.get("arguments")
                if isinstance(fn_args, str):
                    slot["arguments"] += fn_args

    def count(self) -> int:
        """Number of distinct tool-call indices seen so far."""
        return len(self._slots)

    def finalize(self) -> list[ToolCall]:
        """Return :class:`ToolCall` instances sorted by SSE index."""
        return [
            ToolCall(id=v["id"], name=v["name"], arguments_json=v["arguments"])
            for _, v in sorted(self._slots.items())
        ]


class LLMClient:
    """OpenAI-compatible chat-completions HTTP client.

    The client is stateless across calls — each call composes a fresh
    ``requests`` request. Construct one instance per ``claw chat``
    invocation; share it freely between the loop, the compressor, and
    the title generator.

    Args:
        base_url: Provider base URL, e.g. ``https://openrouter.ai/api/v1``.
            The trailing ``/`` is stripped so callers may pass either form.
        api_key: Bearer token sent in the ``Authorization`` header.
        model: Model identifier, e.g. ``openai/gpt-4o-mini``.
        timeout: Per-call socket timeout in seconds, default 120.0.
            Streaming reads use ``(timeout, None)`` so a slow stream does
            not abort mid-response, but the initial connect still respects
            the timeout.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # Strip the "models/" prefix that Gemini's models API returns but
        # the OpenAI-compatible endpoint does not accept.
        self._model = model.removeprefix("models/")
        self._timeout = float(timeout)
        # Build a requests.Session that honours HTTP_PROXY / HTTPS_PROXY /
        # NO_PROXY from the environment (loaded from ~/.claw/.env at startup).
        self._session = _build_session()

    # ----- Public properties used by the loop / compressor / title ------

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key(self) -> str:
        return self._api_key

    # ----- Request assembly ---------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _build_body(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        *,
        stream: bool,
        max_tokens: Optional[int] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if tools:
            # Pass through unchanged: the registry already produces
            # OpenAI-format function tool descriptors.
            body["tools"] = tools
        if stream:
            body["stream"] = True
            # stream_options.include_usage is supported by OpenRouter/Nous
            # but not by all providers (e.g. Gemini ignores or rejects it).
            # Only send it for non-Gemini endpoints.
            if "generativelanguage.googleapis.com" not in self._base_url:
                body["stream_options"] = {"include_usage": True}
        if max_tokens is not None and max_tokens > 0:
            body["max_tokens"] = int(max_tokens)
        return body


    # ----- Non-streaming chat -------------------------------------------

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        *,
        max_tokens: Optional[int] = None,
    ) -> dict:
        """One non-streaming chat-completions request.

        Used by the compressor (to generate the 14-section summary with a
        clamped ``max_tokens`` budget) and the title generator (no tools,
        small token budget). Returns the parsed top-level JSON object so
        callers can pull ``choices[0].message.content`` directly.

        Raises :class:`ProviderHTTPError` on any non-2xx response.
        """
        body = self._build_body(
            messages, tools, stream=False, max_tokens=max_tokens
        )
        try:
            resp = self._session.post(
                self._endpoint(),
                headers=self._headers(),
                json=body,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            # Surface network errors as ProviderHTTPError so the loop's
            # error-taxonomy table can route them uniformly.
            raise ProviderHTTPError(0, f"network error: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderHTTPError(resp.status_code, resp.text or "")

        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderHTTPError(
                resp.status_code, f"invalid JSON body: {exc}"
            ) from exc


    # ----- Streaming chat -----------------------------------------------

    def stream_chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        *,
        on_text_delta: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[StreamStatus], None]] = None,
        interrupt: Optional[threading.Event] = None,
        max_tokens: Optional[int] = None,
    ) -> StreamResult:
        """One streaming chat-completions request.

        Iterates the SSE response line by line, decoding each
        ``data: {...}`` frame as JSON and feeding deltas into the
        accumulators. The textual ``content`` is emitted to
        ``on_text_delta`` as it arrives so the TUI can flush it directly
        to stdout (Req 13.1). After every delta — text or tool-call —
        ``on_status`` is invoked with a fresh :class:`StreamStatus`; the
        caller is responsible for throttling rendering (Req 13.3).

        ``interrupt`` is checked between SSE lines. When set, the
        method stops reading further frames, finalises whatever has
        been accumulated, and returns the partial result with
        ``finish_reason="interrupted"`` (Req 13.4). Closing the
        underlying response is handled by the ``with`` context.

        On any non-2xx HTTP status the method raises
        :class:`ProviderHTTPError`. Malformed individual SSE frames are
        skipped silently — the stream is best-effort and a single bad
        frame must not abort an otherwise valid response.
        """
        body = self._build_body(
            messages, tools, stream=True, max_tokens=max_tokens
        )

        # Use a (connect, read) timeout pair: respect the configured
        # timeout for the initial connection but keep reads open for as
        # long as the server keeps the SSE stream alive.
        timeout = (self._timeout, None)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        accumulator = _ToolCallAccumulator()
        finish_reason: str = ""
        prompt_tokens: Optional[int] = None
        completion_tokens: Optional[int] = None

        try:
            resp = self._session.post(
                self._endpoint(),
                headers=self._headers(),
                json=body,
                stream=True,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise ProviderHTTPError(0, f"network error: {exc}") from exc

        with resp:
            if resp.status_code >= 400:
                # Read the (small) error body for diagnostics without
                # streaming, then surface it.
                body_text = ""
                try:
                    body_text = resp.text or ""
                except requests.RequestException:
                    body_text = "<unreadable error body>"
                raise ProviderHTTPError(resp.status_code, body_text)

            # Default UTF-8 — the OpenAI SSE protocol mandates UTF-8 and
            # some providers omit the charset hint.
            if not resp.encoding:
                resp.encoding = "utf-8"

            for raw_line in resp.iter_lines(decode_unicode=True):
                # Honour Ctrl+C between lines (Req 13.4).
                if interrupt is not None and interrupt.is_set():
                    finish_reason = "interrupted"
                    break

                # iter_lines yields '' for the blank separator between
                # SSE events. Skip those and any other blank padding.
                if not raw_line:
                    continue

                line = raw_line.strip()
                if not line:
                    continue

                # SSE comments (lines beginning with ':') are heartbeats.
                if line.startswith(":"):
                    continue

                if not line.startswith("data:"):
                    # Some providers emit `event:` lines or other SSE
                    # field names; ignore anything that's not a data
                    # frame.
                    continue

                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break

                try:
                    frame = json.loads(payload)
                except (json.JSONDecodeError, ValueError):
                    # Best-effort: a single garbled frame must not kill
                    # the stream.
                    continue

                if not isinstance(frame, dict):
                    continue

                # Capture usage from any frame that carries it. The
                # final usage frame (when the provider sends one) often
                # has `choices == []`, so handle it before iterating
                # choices.
                usage = frame.get("usage")
                if isinstance(usage, dict):
                    pt = usage.get("prompt_tokens")
                    ct = usage.get("completion_tokens")
                    if isinstance(pt, int):
                        prompt_tokens = pt
                    if isinstance(ct, int):
                        completion_tokens = ct

                choices = frame.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue

                first = choices[0]
                if not isinstance(first, dict):
                    continue

                # finish_reason may arrive on an interim frame or only
                # on the final frame; record the most recent non-null
                # value.
                fr = first.get("finish_reason")
                if isinstance(fr, str) and fr:
                    finish_reason = fr

                delta = first.get("delta")
                if not isinstance(delta, dict):
                    continue

                content_delta = delta.get("content")
                if isinstance(content_delta, str) and content_delta:
                    content_parts.append(content_delta)
                    if on_text_delta is not None:
                        try:
                            on_text_delta(content_delta)
                        except Exception:
                            pass

                # Capture DeepSeek reasoning_content (thinking mode)
                reasoning_delta = delta.get("reasoning_content")
                if isinstance(reasoning_delta, str) and reasoning_delta:
                    reasoning_parts.append(reasoning_delta)

                tool_call_delta = delta.get("tool_calls")
                if tool_call_delta is not None:
                    accumulator.feed_delta(tool_call_delta)

                if on_status is not None:
                    try:
                        on_status(
                            StreamStatus(
                                content_chars=sum(len(p) for p in content_parts),
                                tool_call_count=accumulator.count(),
                                finish_reason=finish_reason or None,
                            )
                        )
                    except Exception:
                        pass

        return StreamResult(
            content="".join(content_parts),
            tool_calls=accumulator.finalize(),
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_content="".join(reasoning_parts) or None,
        )


def _build_session() -> requests.Session:
    """Build a requests.Session that honours proxy env vars.

    Reads HTTP_PROXY, HTTPS_PROXY, and NO_PROXY from the process
    environment (which is populated from ~/.claw/.env at startup by
    cli/main.py). The session's ``trust_env=True`` default already
    picks these up, but we also set them explicitly on the session's
    ``proxies`` dict so they take effect even when the env vars were
    set after the session was created.
    """
    session = requests.Session()
    session.trust_env = True  # honour env-var proxies

    proxies: dict[str, str] = {}
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    if proxies:
        session.proxies.update(proxies)

    # NO_PROXY is handled automatically by requests when trust_env=True,
    # but we also set it explicitly for clarity.
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if no_proxy:
        session.proxies["no_proxy"] = no_proxy

    return session
