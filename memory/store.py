"""SQLite-backed shared memory.

Two things live here:
  - agent_bookmarks: a per-agent Slack read bookmark, so agents only process
    messages newer than the last one they handled.
  - agent_memory (Stage 1 memory): each agent's own past decisions and outcomes
    (proposals filed and their approve/reject verdict, digests posted, tasks
    attempted/refused). Every summary is written in Python — never by the model —
    and read back into the agent's next cycle so it stops repeating itself.

The database file is created on first use.
"""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aria_memory.db")

# Valid agent_memory event types (kept here as the single source of truth).
MEMORY_EVENT_TYPES = (
    "proposal_filed", "proposal_approved", "proposal_rejected",
    "digest_posted", "task_attempted", "task_refused",
)


def _connect():
    """Open the DB, ensuring the schema exists."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_bookmarks (
            agent_name        TEXT PRIMARY KEY,
            last_processed_ts TEXT,
            updated_at        TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_memory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            agent      TEXT,
            timestamp  TEXT,
            event_type TEXT,
            summary    TEXT,
            payload_id TEXT
        )
        """
    )
    return conn


def get_bookmark(agent_name):
    """Return the last processed Slack ts for this agent, or None if unset."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_processed_ts FROM agent_bookmarks WHERE agent_name = ?",
            (agent_name,),
        ).fetchone()
    return row[0] if row else None


def set_bookmark(agent_name, ts):
    """Record `ts` as the last processed Slack ts for this agent."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_bookmarks (agent_name, last_processed_ts, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(agent_name) DO UPDATE SET
                last_processed_ts = excluded.last_processed_ts,
                updated_at        = excluded.updated_at
            """,
            (agent_name, ts, now),
        )
        conn.commit()
    return ts


# --- Agent memory (Stage 1) -------------------------------------------------
_MEMORY_COLS = ("id", "agent", "timestamp", "event_type", "summary", "payload_id")


def add_memory(agent, event_type, summary, payload_id=None):
    """Record one agent-memory event. `summary` is a short Python-written line
    (never model-generated). Returns the new row id. Timestamp is UTC ISO now."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO agent_memory (agent, timestamp, event_type, summary, payload_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent, now, event_type, summary, payload_id),
        )
        conn.commit()
    return cur.lastrowid


def update_memory_by_payload(payload_id, event_type):
    """Transition the memory row(s) for `payload_id` to a new event_type — used
    when the approvals poller sees a ✅/❌ (proposal_filed -> approved/rejected).
    Returns the number of rows updated (0 if none match). Idempotent."""
    if not payload_id:
        return 0
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE agent_memory SET event_type = ? WHERE payload_id = ?",
            (event_type, payload_id),
        )
        conn.commit()
    return cur.rowcount


def get_recent_memories(agent, limit=10):
    """Return this agent's most recent memories, NEWEST FIRST, as a list of dicts
    with keys id/agent/timestamp/event_type/summary/payload_id."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, agent, timestamp, event_type, summary, payload_id "
            "FROM agent_memory WHERE agent = ? ORDER BY id DESC LIMIT ?",
            (agent, limit),
        ).fetchall()
    return [dict(zip(_MEMORY_COLS, r)) for r in rows]
