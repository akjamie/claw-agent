"""
ChatTUI and SlashCommandDispatcher — interactive REPL for ``claw chat``.

The TUI is intentionally thin. It uses :class:`prompt_toolkit.PromptSession`
for line editing/history (cross-platform arrow keys, Ctrl+A/E, etc.) and
relies on plain ``print`` to flush streamed assistant text directly to
stdout. A throttled Status_Line is rendered to stderr with a carriage
return so it overwrites itself at most twice per second (Req 13.3).

Slash commands are dispatched by :class:`SlashCommandDispatcher`, which
operates on the live :class:`AgentLoop` and :class:`SqlitePersistence`
instances supplied by :mod:`cli.chat_cmd`.

Design references:
- design.md §"agent.tui" — plain-print + stderr status line strategy.
- design.md §"Error Message conventions" — markers preserved by the loop.
- requirements.md §2.1–§2.10 — REPL behaviours, slash commands, signals.
- requirements.md §11.1–§11.4 — manual ``/compact`` semantics.
- requirements.md §13.2, §13.3, §13.5 — newline at turn end, throttle, no
  text printing on tool-only iterations (loop already short-circuits).
"""

from __future__ import annotations

import datetime
import logging
import signal
import sys
import time
from enum import Enum
from typing import TYPE_CHECKING, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory

from agent.persistence import PersistenceFailure
from cli.interactive_ui import (
    print_error,
    print_info,
    print_success,
    print_warning,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.llm_client import StreamStatus
    from agent.loop import AgentLoop
    from agent.persistence import SqlitePersistence

__all__ = [
    "ChatTUI",
    "SlashCommandDispatcher",
    "SlashResult",
]

logger = logging.getLogger(__name__)


# Throttle interval between Status_Line renders (≤2 Hz per Req 13.3).
_STATUS_THROTTLE_SECONDS = 0.5

_HELP_TEXT = (
    "Available commands:\n"
    "  /help              Show this help text\n"
    "  /quit              Exit Claw Chat\n"
    "  /new               Start a new session with the same model\n"
    "  /sessions          List all stored sessions\n"
    "  /compact [topic]   Compress conversation context (optional topic focus)\n"
)

_WELCOME_TEMPLATE = (
    "✨ Claw Chat\n"
    "Session: {short_id} | Model: {model} | Provider: {provider}\n"
    "Type /help for commands, /quit to exit.\n"
)


class SlashResult(Enum):
    """Outcome of one :meth:`SlashCommandDispatcher.dispatch` call."""

    CONTINUE = "continue"
    QUIT = "quit"
    SWITCH_SESSION = "switch_session"


class SlashCommandDispatcher:
    """Routes ``/...`` lines from the REPL to handlers.

    Each handler returns a :class:`SlashResult`. The dispatcher never
    raises for known error conditions (persistence failure, compression
    failure, unknown command) — those are surfaced via stderr and a
    :data:`SlashResult.CONTINUE` so the user can retry.
    """

    def __init__(
        self,
        loop: "AgentLoop",
        persistence: "SqlitePersistence",
    ) -> None:
        self._loop = loop
        self._persistence = persistence

    def is_slash(self, line: str) -> bool:
        """Whether ``line`` is a slash command."""
        return line.startswith("/")

    def dispatch(self, line: str) -> SlashResult:
        """Parse ``line`` and invoke the matching handler."""
        parts = line[1:].strip().split(maxsplit=1)
        if not parts:
            print_error("Empty slash command. Type /help for commands.")
            return SlashResult.CONTINUE
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("help", "?"):
            sys.stdout.write(_HELP_TEXT)
            sys.stdout.flush()
            return SlashResult.CONTINUE
        if cmd in ("quit", "exit"):
            return SlashResult.QUIT
        if cmd == "new":
            return self._cmd_new()
        if cmd == "sessions":
            return self._cmd_sessions()
        if cmd == "compact":
            return self._cmd_compact(rest or None)

        print_error(f"Unknown command: /{cmd}. Type /help for commands.")
        return SlashResult.CONTINUE

    # ── Handlers ─────────────────────────────────────────────────────────

    def _cmd_new(self) -> SlashResult:
        model = self._loop.session.model
        try:
            session = self._persistence.create_session(model)
        except PersistenceFailure as exc:
            print_error(f"Failed to create session: {exc}")
            return SlashResult.CONTINUE
        self._loop.session = session
        self._loop.history = []
        print_success(f"Started new session: {session.short_id}")
        return SlashResult.SWITCH_SESSION

    def _cmd_sessions(self) -> SlashResult:
        try:
            sessions = self._persistence.list_sessions()
        except Exception as exc:  # noqa: BLE001 - best-effort listing
            print_error(f"Failed to list sessions: {exc}")
            return SlashResult.CONTINUE
        if not sessions:
            print_info("No sessions found.")
            return SlashResult.CONTINUE
        for s in sessions:
            ts = _format_iso(s.updated_at)
            title = s.title or "(untitled)"
            sys.stdout.write(f"{s.short_id}  {ts}  {s.model}  {title}\n")
        sys.stdout.flush()
        return SlashResult.CONTINUE

    def _cmd_compact(self, topic: Optional[str]) -> SlashResult:
        compressor = getattr(self._loop, "_compressor", None)
        if compressor is None:
            print_error("Compressor unavailable.")
            return SlashResult.CONTINUE
        try:
            result = compressor.compress(
                self._loop.history, topic=topic, force=True
            )
        except Exception as exc:  # noqa: BLE001 - Req 11.4
            print_error(f"Compression failed: {exc}")
            return SlashResult.CONTINUE
        if not result.succeeded:
            reason = result.skipped_reason or "no compressible content"
            print_error(f"Compression failed: {reason}")
            return SlashResult.CONTINUE

        self._loop.history = list(result.messages)
        print_success(
            f"Compressed context: {result.before_tokens} → "
            f"{result.after_tokens} tokens"
            + (f" (focus: {topic})" if topic else "")
        )
        return SlashResult.CONTINUE


class _StatusLine:
    """Throttled stderr Status_Line renderer (Req 13.3)."""

    def __init__(self, throttle: float = _STATUS_THROTTLE_SECONDS) -> None:
        self._throttle = throttle
        self._last_render = 0.0
        self._enabled = _stderr_is_tty()

    def render(self, text: str) -> None:
        """Overwrite the previous Status_Line with ``text``.

        Skipped silently when stderr is not a TTY (piped or redirected),
        which Req 13.3 permits — the loop emits INFO-level log records
        instead in that environment.
        """
        if not self._enabled:
            return
        now = time.monotonic()
        if (now - self._last_render) < self._throttle:
            return
        self._last_render = now
        try:
            sys.stderr.write(f"\r\x1b[K{text}")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 - rendering must not abort the loop
            pass

    def clear(self) -> None:
        """Erase the Status_Line (called between turns and at exit)."""
        if not self._enabled:
            return
        try:
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass


class ChatTUI:
    """Interactive REPL — owns the prompt loop and Status_Line lifecycle."""

    def __init__(
        self,
        loop: "AgentLoop",
        slash: SlashCommandDispatcher,
    ) -> None:
        self._loop = loop
        self._slash = slash
        self._status = _StatusLine()
        self._prompt_session: PromptSession = PromptSession(
            history=InMemoryHistory()
        )
        # Per-turn iteration heuristic. We treat each transition from
        # "stream just finished" → "stream emitted again" as the
        # boundary of a new Iteration.
        self._iter_count = 0
        self._iter_active = False

    # ── Public entry point ───────────────────────────────────────────────

    def run(self) -> int:
        """Run the REPL until the user quits or sends EOF."""
        self._print_welcome()

        prev_handler = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, self._on_sigint)
        except (ValueError, OSError):
            # Not on the main thread or SIGINT unavailable on this OS
            # variant — the per-line KeyboardInterrupt handling below
            # still gives the user a clean cancel path.
            prev_handler = None

        # Wire streaming callbacks once. Both attributes are documented
        # in agent.loop.AgentLoop.__init__.
        self._loop._on_text_delta = _stdout_writer  # noqa: SLF001
        self._loop._on_status = self._on_status  # noqa: SLF001

        try:
            return self._repl()
        finally:
            self._status.clear()
            if prev_handler is not None:
                try:
                    signal.signal(signal.SIGINT, prev_handler)
                except (ValueError, OSError, TypeError):
                    pass

    # ── Internals ────────────────────────────────────────────────────────

    def _repl(self) -> int:
        while True:
            try:
                line = self._prompt_session.prompt(">>> ")
            except KeyboardInterrupt:
                # Ctrl+C at the prompt: clear input line, continue
                # (Req 2.9). prompt_toolkit already wiped the buffer.
                continue
            except EOFError:
                # Ctrl+D / Ctrl+Z (Req 2.6).
                sys.stdout.write("\n")
                sys.stdout.flush()
                return 0

            line = line.strip()
            if not line:
                continue

            if self._slash.is_slash(line):
                try:
                    result = self._slash.dispatch(line)
                except Exception as exc:  # noqa: BLE001 - keep REPL alive
                    print_error(f"Slash command error: {exc}")
                    continue
                if result == SlashResult.QUIT:
                    return 0
                continue

            self._run_one_turn(line)

    def _run_one_turn(self, line: str) -> None:
        self._iter_count = 0
        self._iter_active = False
        try:
            self._loop.run_turn(line)
        except PersistenceFailure as exc:
            # Req 18.3 in interactive mode: warn and return to REPL.
            print_warning(f"Persistence failure: {exc}")
        except KeyboardInterrupt:
            # Defensive: prompt_toolkit may surface a late SIGINT here
            # if our handler did not run. Mirror its effect so the
            # in-flight turn winds down cleanly (Req 2.8).
            self._loop.request_interrupt()
        except Exception as exc:  # noqa: BLE001 - keep REPL alive
            logger.exception("Unhandled exception during turn")
            print_error(f"Turn error: {type(exc).__name__}: {exc}")
        finally:
            # Trailing newline + clear Status_Line at end of turn
            # (Req 13.2).
            try:
                sys.stdout.write("\n")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass
            self._status.clear()

    def _on_status(self, status: "StreamStatus") -> None:
        # Heuristic: each stream call is one Iteration. The first
        # status of a new stream starts a new iter; the status that
        # carries a non-empty ``finish_reason`` ends it.
        if not self._iter_active:
            self._iter_count += 1
            self._iter_active = True
        if status.finish_reason:
            self._iter_active = False
        text = (
            f"[iter {self._iter_count} | "
            f"tools {status.tool_call_count} | "
            f"tokens {self._loop.session.total_tokens}]"
        )
        self._status.render(text)

    def _on_sigint(self, signum, frame) -> None:  # noqa: ARG002
        """SIGINT handler — tell the loop to stop at the next safe point."""
        self._loop.request_interrupt()

    def _print_welcome(self) -> None:
        sys.stdout.write(
            _WELCOME_TEMPLATE.format(
                short_id=self._loop.session.short_id,
                model=self._loop.session.model,
                provider=getattr(self._loop, "_provider", ""),
            )
        )
        sys.stdout.flush()


# ── Module-private helpers ───────────────────────────────────────────────


def _stdout_writer(chunk: str) -> None:
    """Default ``on_text_delta`` for the TUI — never raises."""
    try:
        sys.stdout.write(chunk)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - rendering must not abort the loop
        pass


def _stderr_is_tty() -> bool:
    try:
        return bool(sys.stderr.isatty())
    except Exception:  # noqa: BLE001
        return False


def _format_iso(unix_ts: int) -> str:
    """Format a unix epoch as ISO-8601 local time, seconds precision."""
    try:
        return datetime.datetime.fromtimestamp(int(unix_ts)).isoformat(
            timespec="seconds"
        )
    except (ValueError, OverflowError, OSError):
        return str(unix_ts)
