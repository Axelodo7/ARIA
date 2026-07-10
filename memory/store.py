"""SQLite-backed shared memory.

For now this holds one thing: a per-agent Slack read bookmark, so agents only
process messages newer than the last one they handled instead of re-digesting
the same content every cycle. The database file is created on first use.
"""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aria_memory.db")


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
