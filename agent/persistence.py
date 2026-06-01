"""
SQLite WAL persistence for claw-agent sessions and messages.

This module owns the on-disk representation of every conversation. The
schema, transaction discipline, and concurrency story are all defined
here so the rest of the runtime can treat persistence as an opaque
"append + load" service.

Design references
-----------------
- design.md §"agent.persistence" — public class shape and method names.
- design.md §"SQLite schema" — table layout, indexes, PRAGMAs.
- design.md §"Session_Id resolution" — first-4-hex prefix matching.
- design.md §"Title Storage" — no-op-when-non-empty UPDATE.
- design.md §"Tool argument canonicalisation" — sort_keys=True on every row.
- requirements.md §3 — sessions/messages CRUD.
- requirements.md §4.4 — title overwrite protection.
- requirements.md §10.8 — compression summary persistence.
- requirements.md §18 — WAL mode, write retry, schema migrations.

Concurrency model
-----------------
Each public method opens a fresh ``sqlite3.Connection`` for the duration
of one operation. This keeps the implementation thread-safe by
construction (sqlite3 connections are not safe to share between
threads) and matches the property in design Property 6 where many
writer threads target the same database file. Writes are wrapped in a
``BEGIN IMMEDIATE`` transaction; SQLite's WAL journal mode plus
``BEGIN IMMEDIATE`` serialises writers via the database's reserved
lock without blocking concurrent readers (Req 18.1, 18.2).

Retry policy
------------
Every write helper runs through ``_retry``, which performs at most
three attempts (one initial + two retries). The retry sleeps follow a
linear backoff of 0.1 s and 0.3 s. After the third failure the helper
raises ``PersistenceFailure`` (Req 18.3, Property 29).
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, List, Optional, TypeVar

from agent.messages import COMPRESSION_SUMMARY_TOOL_NAME, Message

__all__ = [
    "Session",
    "SqlitePersistence",
    "PersistenceFailure",
    "SessionNotFound",
    "AmbiguousSession",
]


T = TypeVar("T")


# ---------- Public exceptions ---------------------------------------------


class PersistenceFailure(Exception):
    """Raised when a SQLite write transaction fails after all retries.

    Carries the number of attempts made so the CLI dispatcher can render
    a precise diagnostic. The underlying ``sqlite3.OperationalError`` is
    chained as ``__cause__``.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 3,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        if cause is not None:
            self.__cause__ = cause


class SessionNotFound(Exception):
    """Raised by ``get_session`` when zero sessions match the prefix."""

    def __init__(self, prefix: str) -> None:
        super().__init__(f"No session matches prefix {prefix!r}")
        self.prefix = prefix


class AmbiguousSession(Exception):
    """Raised by ``get_session`` when more than one session matches.

    The caller (typically ``cli.chat_cmd``) prints the colliding short
    ids to stderr and exits with status code 2 (Req 3.10).
    """

    def __init__(self, prefix: str, matching_short_ids: List[str]) -> None:
        super().__init__(
            f"Multiple sessions match prefix {prefix!r}: "
            f"{', '.join(matching_short_ids)}"
        )
        self.prefix = prefix
        self.matching_short_ids = list(matching_short_ids)


# ---------- Session value type --------------------------------------------


@dataclass(frozen=True)
class Session:
    """In-memory representation of one row in the ``sessions`` table.

    ``short_id`` is the first 4 hexadecimal characters of ``id`` (a
    UUIDv4) per Req 3.5. The dataclass is frozen so it is hashable and
    safe to share across threads.
    """

    id: str
    short_id: str
    title: str
    created_at: int
    updated_at: int
    model: str
    total_tokens: int

    @classmethod
    def from_row(cls, row: tuple) -> "Session":
        """Build a Session from a ``SELECT id, title, created_at, ...`` row."""
        sid, title, created_at, updated_at, model, total_tokens = row
        sid_str = str(sid)
        return cls(
            id=sid_str,
            short_id=sid_str[:4],
            title=title or "",
            created_at=int(created_at),
            updated_at=int(updated_at),
            model=model,
            total_tokens=int(total_tokens),
        )


# ---------- SqlitePersistence --------------------------------------------


# Linear backoff schedule between retries: sleep _RETRY_DELAYS[i] before
# attempt (i + 2). After three total attempts (one initial + two retries)
# the operation raises ``PersistenceFailure`` (Property 29).
_RETRY_DELAYS = (0.1, 0.3)
_MAX_ATTEMPTS = 3


class SqlitePersistence:
    """SQLite (WAL) backing store for sessions and messages.

    A single ``SqlitePersistence`` instance is safe to share across
    threads; every public method opens its own connection. The default
    database path is ``~/.claw/agent.db`` (Req 3.1); a different path
    may be passed for tests or alternative deployments.
    """

    SCHEMA_VERSION = 1
    DEFAULT_PATH = Path.home() / ".claw" / "agent.db"

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else self.DEFAULT_PATH
        # Guards `initialize` from racing against itself across threads.
        self._init_lock = threading.Lock()

    # ----- Connection helpers --------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh connection and apply the per-connection PRAGMAs.

        The PRAGMAs follow design §"SQLite schema":
        ``journal_mode=WAL``, ``synchronous=NORMAL``, ``foreign_keys=ON``,
        ``temp_store=MEMORY``. ``isolation_level=None`` puts the
        connection into autocommit mode so we can manage transactions
        manually with ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``.

        ``check_same_thread=False`` is set because we never share a
        connection across threads — we open a new one per public method
        call — but the flag is required to avoid spurious complaints
        from sqlite3 when a connection happens to be garbage-collected
        in a different thread than the one that opened it.
        """
        # Ensure the parent directory exists; sqlite3 will otherwise fail
        # to create the file. Using mkdir with exist_ok keeps the call
        # idempotent (Req 3.2).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _retry(self, operation: Callable[[], T]) -> T:
        """Run a write operation, retrying on ``OperationalError`` up to 3x.

        Linear backoff of 0.1 s after the first failure and 0.3 s after
        the second matches Req 18.3. A one-line warning is written to
        stderr per failed attempt so users see the issue surface
        without burying it. After three total attempts the helper
        raises ``PersistenceFailure`` (Property 29).
        """
        attempt = 0
        last_exc: Optional[BaseException] = None
        while attempt < _MAX_ATTEMPTS:
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                last_exc = exc
                attempt += 1
                # One-line warning per failed attempt (Req 18.3).
                print(
                    f"claw chat: sqlite write failed "
                    f"(attempt {attempt}/{_MAX_ATTEMPTS}): {exc}",
                    file=sys.stderr,
                )
                if attempt >= _MAX_ATTEMPTS:
                    break
                # Linear backoff. attempt is 1-indexed here, so we sleep
                # _RETRY_DELAYS[attempt - 1].
                time.sleep(_RETRY_DELAYS[attempt - 1])
        raise PersistenceFailure(
            f"sqlite transaction failed after {_MAX_ATTEMPTS} attempts: {last_exc}",
            attempts=_MAX_ATTEMPTS,
            cause=last_exc,
        )

    # ----- Schema management ---------------------------------------------

    def initialize(self) -> None:
        """Create tables and indexes if missing; idempotent across calls.

        Implements design §"SQLite schema" and Req 18.4. Uses
        ``CREATE TABLE IF NOT EXISTS`` so repeat invocations are
        no-ops, and ``INSERT OR IGNORE`` for the schema-version row so
        the meta row is set on first run but never overwritten. The
        whole sequence runs inside one transaction so an interrupted
        first-run leaves the database in either the empty state or
        fully initialised — never half-built (Property 28).
        """
        with self._init_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id           TEXT PRIMARY KEY,
                        title        TEXT NOT NULL DEFAULT '',
                        created_at   INTEGER NOT NULL,
                        updated_at   INTEGER NOT NULL,
                        model        TEXT NOT NULL,
                        total_tokens INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sessions_updated_at "
                    "ON sessions(updated_at DESC)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id      TEXT NOT NULL
                                        REFERENCES sessions(id) ON DELETE CASCADE,
                        role            TEXT NOT NULL CHECK (
                                            role IN ('system','user','assistant','tool')
                                        ),
                        content         TEXT NOT NULL DEFAULT '',
                        tool_call_id    TEXT,
                        tool_name       TEXT,
                        tool_arguments  TEXT,
                        timestamp       INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_session_id "
                    "ON messages(session_id, id)"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    ("schema_version", str(self.SCHEMA_VERSION)),
                )
                conn.execute("COMMIT")
            except BaseException:
                _safe_rollback(conn)
                raise
            finally:
                conn.close()

    # ----- Session operations --------------------------------------------

    def create_session(self, model: str) -> Session:
        """Insert a new session row with a fresh UUIDv4 and return it.

        Implements Req 3.5: the UUID is stored verbatim and the
        user-visible ``short_id`` is derived as the first 4 hex chars.
        """
        sid = str(uuid.uuid4())
        now = int(time.time())

        def op() -> Session:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO sessions "
                    "(id, title, created_at, updated_at, model, total_tokens) "
                    "VALUES (?, '', ?, ?, ?, 0)",
                    (sid, now, now, model),
                )
                conn.execute("COMMIT")
            except BaseException:
                _safe_rollback(conn)
                raise
            finally:
                conn.close()
            return Session(
                id=sid,
                short_id=sid[:4],
                title="",
                created_at=now,
                updated_at=now,
                model=model,
                total_tokens=0,
            )

        return self._retry(op)

    def get_session(self, id_prefix: str) -> Session:
        """Look up a session by its leading hex prefix.

        Uses ``substr(id, 1, length(?)) = ? COLLATE NOCASE`` so any
        prefix length works — typically 4 (the displayed short id) but
        users can paste a longer prefix for disambiguation.

        Raises:
            SessionNotFound: zero rows match (Req 3.9).
            AmbiguousSession: more than one row matches (Req 3.10).
        """
        if not id_prefix:
            raise SessionNotFound(id_prefix)
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT id, title, created_at, updated_at, model, total_tokens "
                "FROM sessions "
                "WHERE substr(id, 1, length(?)) = ? COLLATE NOCASE",
                (id_prefix, id_prefix),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            raise SessionNotFound(id_prefix)
        if len(rows) > 1:
            short_ids = [str(r[0])[:4] for r in rows]
            raise AmbiguousSession(id_prefix, short_ids)
        return Session.from_row(rows[0])

    def list_sessions(self) -> List[Session]:
        """Return every session row sorted by ``updated_at`` descending.

        Implements the storage half of Req 3.12; the CLI rendering
        layer formats the timestamps and adds the ``(untitled)``
        placeholder.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT id, title, created_at, updated_at, model, total_tokens "
                "FROM sessions "
                "ORDER BY updated_at DESC, id ASC"
            )
            return [Session.from_row(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def update_title(self, session_id: str, title: str) -> None:
        """Set the session title only when the existing title is empty.

        Per Req 4.4 the runtime never overwrites a non-empty title.
        The single ``UPDATE ... WHERE id=? AND title=''`` statement
        encodes the no-op-when-non-empty rule directly in SQL so the
        check and the write share one atomic step.
        """

        def op() -> None:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE sessions SET title = ? "
                    "WHERE id = ? AND title = ''",
                    (title, session_id),
                )
                conn.execute("COMMIT")
            except BaseException:
                _safe_rollback(conn)
                raise
            finally:
                conn.close()

        self._retry(op)

    # ----- Message operations --------------------------------------------

    def append_messages(
        self,
        session_id: str,
        messages: List[Message],
        total_tokens: int,
    ) -> List[Message]:
        """Append messages and update session metadata atomically.

        The whole call runs inside one ``BEGIN IMMEDIATE; ... COMMIT;``
        transaction (Req 3.6). Each Message is checked for idempotency
        first: if a Message arrives with ``id`` already set and a row
        with that id exists for the same session, the existing row is
        kept and no insert is performed (Req 3.7, Property 14). New
        rows fill in ``timestamp`` from ``int(time.time())`` when the
        Message left it at the sentinel value 0.

        After all rows are inserted, the parent session's
        ``updated_at`` and ``total_tokens`` are refreshed in the same
        transaction so the metadata never drifts ahead of the messages
        it counts (Property 15).
        """

        def op() -> List[Message]:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                now = int(time.time())
                persisted: List[Message] = []
                for msg in messages:
                    # Idempotency check: Property 14 / Req 3.7.
                    if msg.id is not None:
                        cur = conn.execute(
                            "SELECT 1 FROM messages "
                            "WHERE id = ? AND session_id = ?",
                            (msg.id, session_id),
                        )
                        if cur.fetchone() is not None:
                            persisted.append(msg)
                            continue
                    # Fill in timestamp if the Message left it at the
                    # sentinel 0 ("fill at insert time" — see
                    # agent.messages.Message docstring).
                    msg_with_ts = (
                        msg if msg.timestamp else replace(msg, timestamp=now)
                    )
                    row = msg_with_ts.to_db_row(session_id)
                    cur = conn.execute(
                        "INSERT INTO messages "
                        "(session_id, role, content, tool_call_id, "
                        "tool_name, tool_arguments, timestamp) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        row,
                    )
                    new_id = cur.lastrowid
                    persisted.append(replace(msg_with_ts, id=new_id))

                conn.execute(
                    "UPDATE sessions SET updated_at = ?, total_tokens = ? "
                    "WHERE id = ?",
                    (now, int(total_tokens), session_id),
                )
                conn.execute("COMMIT")
                return persisted
            except BaseException:
                _safe_rollback(conn)
                raise
            finally:
                conn.close()

        return self._retry(op)

    def load_recent_messages(
        self, session_id: str, limit: int = 500
    ) -> List[Message]:
        """Return the most recent ``limit`` messages, oldest first.

        The query selects newest-first (``ORDER BY id DESC LIMIT ?``)
        and the result is reversed in Python so the returned list is
        chronological. Newest-first SQL ordering is what the index
        ``idx_messages_session_id`` supports efficiently (design
        §"SQLite schema").

        ``limit`` of 0 returns an empty list. Negative limits are
        clamped to 0 to avoid SQLite errors.
        """
        if limit < 0:
            limit = 0
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT id, session_id, role, content, tool_call_id, "
                "tool_name, tool_arguments, timestamp FROM messages "
                "WHERE session_id = ? "
                "ORDER BY id DESC "
                "LIMIT ?",
                (session_id, int(limit)),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [Message.from_db_row(row) for row in reversed(rows)]

    def persist_summary(self, session_id: str, summary_msg: Message) -> None:
        """Insert a compression-summary message for a session.

        The summary is always written with
        ``tool_name == "__compression_summary__"`` so the compressor's
        Phase 1 pruner can recognise and skip it on subsequent passes
        (design §"agent.compressor"). If the caller passed in a
        summary_msg with a different (or missing) tool_name, we
        rewrite it on the way in to enforce the invariant.

        The session's ``updated_at`` is bumped so listings sort the
        compressed session ahead of stale ones, but ``total_tokens``
        is intentionally left unchanged — compression does not by
        itself add new conversation tokens, and the caller will issue
        a follow-up ``append_messages`` on the next turn that supplies
        the post-compression total. Compression preserves the full
        pre-compression history on disk (Req 10.8, Property 10).
        """
        forced = (
            summary_msg
            if summary_msg.tool_name == COMPRESSION_SUMMARY_TOOL_NAME
            else replace(summary_msg, tool_name=COMPRESSION_SUMMARY_TOOL_NAME)
        )

        def op() -> None:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                now = int(time.time())
                msg_with_ts = (
                    forced if forced.timestamp else replace(forced, timestamp=now)
                )
                row = msg_with_ts.to_db_row(session_id)
                conn.execute(
                    "INSERT INTO messages "
                    "(session_id, role, content, tool_call_id, "
                    "tool_name, tool_arguments, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                conn.execute("COMMIT")
            except BaseException:
                _safe_rollback(conn)
                raise
            finally:
                conn.close()

        self._retry(op)


# ---------- private helpers ------------------------------------------------


def _safe_rollback(conn: sqlite3.Connection) -> None:
    """Best-effort ROLLBACK that swallows secondary errors.

    Used in every transaction's ``except`` arm. If the rollback itself
    raises (for example because the connection is already closed) we
    intentionally ignore that — the original exception is the one the
    caller cares about and is re-raised by the surrounding ``raise``.
    """
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
