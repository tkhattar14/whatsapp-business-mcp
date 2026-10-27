"""
Store for conversations, messages, tickets and relay state.

This is the source of truth: every message is written here first, then copied
to the SharePoint Excel log by the sync loop. If SharePoint is unreachable,
rows simply stay unsynced and are retried later.

Two backends run the same SQL:
  - PostgreSQL when DATABASE_URL is set (production - survives redeploys, backed up)
  - SQLite file at SUPPORT_DB_PATH otherwise (local development and tests)
"""

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

logger = logging.getLogger("whatsapp-support.store")

# Conversation states
STATE_BOT = "bot"      # the intake bot is asking the customer questions
STATE_AGENT = "agent"  # a human agent is handling it; the bot stays silent

# {pk} and {int} are filled in per backend
_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    phone            TEXT PRIMARY KEY,
    name             TEXT,
    state            TEXT NOT NULL DEFAULT 'bot',
    handoff_at       {int},
    last_customer_at {int},
    last_agent_at    {int},
    step             TEXT,
    answers          TEXT,
    updated_at       {int} NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id         {pk},
    ref        TEXT UNIQUE,
    phone      TEXT NOT NULL,
    name       TEXT,
    answers    TEXT NOT NULL,
    status     TEXT,
    created_at {int} NOT NULL,
    synced     {int} NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id        {pk},
    wamid     TEXT UNIQUE,
    phone     TEXT NOT NULL,
    name      TEXT,
    direction TEXT NOT NULL,
    sender    TEXT NOT NULL,
    msg_type  TEXT,
    body      TEXT,
    ts        {int} NOT NULL,
    state     TEXT,
    synced    {int} NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_unsynced ON messages(synced, id);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone, ts);
CREATE INDEX IF NOT EXISTS idx_tickets_unsynced ON tickets(synced, id);

-- Relay mode: which resident each message forwarded to the agent belongs to
CREATE TABLE IF NOT EXISTS relay_links (
    wamid      TEXT PRIMARY KEY,
    phone      TEXT NOT NULL,
    ref        TEXT,
    payload    TEXT,
    created_at {int} NOT NULL
);

-- Relay mode: agent messages already handled (Meta retries webhooks)
CREATE TABLE IF NOT EXISTS seen_events (
    wamid      TEXT PRIMARY KEY,
    created_at {int} NOT NULL
);

-- Relay mode: forwards waiting until the agent's 24-hour window is open again
CREATE TABLE IF NOT EXISTS relay_queue (
    id         {pk},
    payload    TEXT NOT NULL,
    created_at {int} NOT NULL
)
"""

# Tables in dependency-free order (used by the SQLite -> PostgreSQL copy script)
TABLES = ("conversations", "tickets", "messages", "relay_links", "seen_events", "relay_queue")

# Columns added after the first release, for upgrading existing databases
_ADDED_COLUMNS = {"conversations": ("step", "answers"), "tickets": ("ref", "status"), "relay_links": ("payload",)}

# Conversation fields that may be explicitly cleared by passing None
_CLEARABLE = {"handoff_at", "step", "answers"}


def is_postgres_url(target: str) -> bool:
    return target.startswith(("postgres://", "postgresql://"))


class Database:
    """One connection, serialised by a lock, running '?'-placeholder SQL on SQLite or PostgreSQL."""

    def __init__(self, target: str):
        self.postgres = is_postgres_url(target)
        self._target = target
        self._lock = threading.RLock()
        self._in_transaction = False
        self._connect()

    def _connect(self) -> None:
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row

            self._conn = psycopg.connect(self._target, autocommit=True, row_factory=dict_row, connect_timeout=15)
        else:
            directory = os.path.dirname(self._target)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self._conn = sqlite3.connect(self._target, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def _run(self, fn):
        """Run fn(); on PostgreSQL, reconnect once if the connection was dropped (outside transactions)."""
        try:
            return fn()
        except Exception as e:
            if not self.postgres or self._in_transaction or not _is_connection_error(e):
                raise
            logger.warning("Database connection lost, reconnecting: %s", e)
            self._connect()
            return fn()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a statement; returns the number of rows changed."""
        with self._lock:
            return self._run(lambda: self._conn.execute(self._sql(sql), tuple(params)).rowcount)

    def executemany(self, sql: str, rows: List[Sequence[Any]]) -> None:
        with self._lock:
            def run():
                cursor = self._conn.cursor()
                cursor.executemany(self._sql(sql), [tuple(r) for r in rows])
            self._run(run)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return self._run(lambda: [dict(r) for r in self._conn.execute(self._sql(sql), tuple(params)).fetchall()])

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self.postgres:
                with self._conn.transaction():
                    self._in_transaction = True
                    try:
                        yield
                    finally:
                        self._in_transaction = False
            else:
                self._conn.execute("BEGIN")
                self._in_transaction = True
                try:
                    yield
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                finally:
                    self._in_transaction = False

    def create_schema(self) -> None:
        with self._lock:
            if self.postgres:
                schema = _SCHEMA.format(pk="BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", int="BIGINT")
                for table, columns in _ADDED_COLUMNS.items():
                    exists = self.one("SELECT to_regclass(?) AS t", (table,))
                    if exists and exists["t"]:
                        for column in columns:
                            self.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} TEXT")
                for statement in schema.split(";"):
                    if statement.strip():
                        self.execute(statement)
            else:
                for table, columns in _ADDED_COLUMNS.items():
                    existing = {r["name"] for r in self.query(f"PRAGMA table_info({table})")}
                    if existing:
                        for column in columns:
                            if column not in existing:
                                self.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
                self._conn.executescript(_SCHEMA.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT", int="INTEGER"))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _is_connection_error(error: Exception) -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError))


class SupportStore:
    def __init__(self, target: str):
        """target: a PostgreSQL URL (postgresql://...) or a SQLite file path."""
        self.db = Database(target)
        self.db.create_schema()

    @property
    def backend(self) -> str:
        return "postgres" if self.db.postgres else "sqlite"

    def close(self) -> None:
        self.db.close()

    # ================================
    # CONVERSATIONS
    # ================================

    def get_conversation(self, phone: str) -> Optional[Dict[str, Any]]:
        return self.db.one("SELECT * FROM conversations WHERE phone = ?", (phone,))

    def upsert_conversation(self, phone: str, **fields: Any) -> Dict[str, Any]:
        """Create the conversation if needed and update the given fields."""
        fields = {k: v for k, v in fields.items() if v is not None or k in _CLEARABLE}
        fields["updated_at"] = int(time.time())
        assignments = ", ".join(f"{column} = ?" for column in fields)
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO conversations (phone, state, updated_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (phone, STATE_BOT, fields["updated_at"]),
            )
            self.db.execute(f"UPDATE conversations SET {assignments} WHERE phone = ?", (*fields.values(), phone))
            return self.db.one("SELECT * FROM conversations WHERE phone = ?", (phone,))

    def list_conversations(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.db.query("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,))

    # ================================
    # MESSAGES
    # ================================

    def add_message(
        self,
        phone: str,
        direction: str,
        sender: str,
        msg_type: str,
        body: str,
        ts: Optional[int] = None,
        wamid: Optional[str] = None,
        name: Optional[str] = None,
        state: Optional[str] = None,
    ) -> bool:
        """Store a message. Returns False if a message with this WhatsApp ID was already stored
        (Meta retries webhooks, so duplicates are expected)."""
        changed = self.db.execute(
            """INSERT INTO messages (wamid, phone, name, direction, sender, msg_type, body, ts, state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
            (wamid, phone, name, direction, sender, msg_type, body, ts or int(time.time()), state),
        )
        return changed == 1

    def has_message(self, wamid: str) -> bool:
        return self.db.one("SELECT 1 AS found FROM messages WHERE wamid = ?", (wamid,)) is not None

    def get_messages(self, phone: Optional[str] = None, limit: int = 10000) -> List[Dict[str, Any]]:
        if phone:
            return self.db.query("SELECT * FROM messages WHERE phone = ? ORDER BY ts, id LIMIT ?", (phone, limit))
        return self.db.query("SELECT * FROM messages ORDER BY ts, id LIMIT ?", (limit,))

    def get_unsynced(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self.db.query("SELECT * FROM messages WHERE synced = 0 ORDER BY id LIMIT ?", (limit,))

    def mark_synced(self, ids: List[int]) -> None:
        if ids:
            self.db.executemany("UPDATE messages SET synced = 1 WHERE id = ?", [(i,) for i in ids])

    # ================================
    # TICKETS (one per completed intake)
    # ================================

    def create_ticket(
        self, phone: str, name: Optional[str], answers: Dict[str, str], prefix: str, status: Optional[str]
    ) -> Dict[str, Any]:
        """Save a ticket and give it a reference like MMF-00042."""
        with self.db.transaction():
            row = self.db.one(
                "INSERT INTO tickets (phone, name, answers, status, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
                (phone, name, json.dumps(answers, ensure_ascii=False), status, int(time.time())),
            )
            ticket_id = row["id"]
            self.db.execute("UPDATE tickets SET ref = ? WHERE id = ?", (format_ticket_ref(prefix, ticket_id), ticket_id))
            return _ticket(self.db.one("SELECT * FROM tickets WHERE id = ?", (ticket_id,)))

    def get_ticket(self, ref: str) -> Optional[Dict[str, Any]]:
        row = self.db.one("SELECT * FROM tickets WHERE ref = ?", (ref,))
        return _ticket(row) if row else None

    def set_ticket_status(self, ref: str, status: str) -> None:
        self.db.execute("UPDATE tickets SET status = ? WHERE ref = ?", (status, ref))

    def list_tickets(self, limit: int = 1000, unsynced_only: bool = False) -> List[Dict[str, Any]]:
        if unsynced_only:
            rows = self.db.query("SELECT * FROM tickets WHERE synced = 0 ORDER BY id LIMIT ?", (limit,))
        else:
            rows = self.db.query("SELECT * FROM tickets ORDER BY id DESC LIMIT ?", (limit,))
        return [_ticket(row) for row in rows]

    def mark_tickets_synced(self, ids: List[int]) -> None:
        if ids:
            self.db.executemany("UPDATE tickets SET synced = 1 WHERE id = ?", [(i,) for i in ids])

    def latest_ticket(self, phone: str) -> Optional[Dict[str, Any]]:
        row = self.db.one("SELECT * FROM tickets WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,))
        return _ticket(row) if row else None

    # ================================
    # RELAY MODE
    # ================================

    def add_relay_link(self, wamid: str, phone: str, ref: Optional[str], payload: Dict[str, Any]) -> None:
        """Remember a forward: who it belongs to, and its content in case Meta later reports it undelivered."""
        self.db.execute(
            """INSERT INTO relay_links (wamid, phone, ref, payload, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (wamid) DO UPDATE SET phone = excluded.phone, ref = excluded.ref,
                   payload = excluded.payload, created_at = excluded.created_at""",
            (wamid, phone, ref, json.dumps(payload, ensure_ascii=False), int(time.time())),
        )

    def get_relay_link(self, wamid: str) -> Optional[Dict[str, Any]]:
        return self.db.one("SELECT * FROM relay_links WHERE wamid = ?", (wamid,))

    def mark_seen(self, wamid: Optional[str]) -> bool:
        """Record an event ID. Returns False if it was already seen."""
        if not wamid:
            return True
        changed = self.db.execute(
            "INSERT INTO seen_events (wamid, created_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (wamid, int(time.time())),
        )
        return changed == 1

    def enqueue_relay(self, payload: Dict[str, Any]) -> int:
        """Queue a forward for later. Returns how many are now waiting."""
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO relay_queue (payload, created_at) VALUES (?, ?)",
                (json.dumps(payload, ensure_ascii=False), int(time.time())),
            )
            return self.count_relay_queue()

    def count_relay_queue(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM relay_queue")["n"]

    def take_relay_queue(self) -> List[Dict[str, Any]]:
        """Remove and return all queued forwards, oldest first."""
        with self.db.transaction():
            rows = self.db.query("SELECT id, payload FROM relay_queue ORDER BY id")
            if rows:
                self.db.execute("DELETE FROM relay_queue WHERE id <= ?", (rows[-1]["id"],))
        return [json.loads(row["payload"]) for row in rows]


def format_ticket_ref(prefix: str, ticket_id: int) -> str:
    return f"{prefix}-{ticket_id:05d}"


def _ticket(row: Dict[str, Any]) -> Dict[str, Any]:
    ticket = dict(row)
    ticket["answers"] = json.loads(ticket["answers"])
    return ticket
