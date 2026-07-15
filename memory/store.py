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
from datetime import datetime, timedelta, timezone

import config

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
    # Stage 3: every voice turn, so conversations survive a main.py restart.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_memory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  TEXT,
            speaker    TEXT,
            content    TEXT,
            session_id TEXT
        )
        """
    )
    # The rolling summary of turns older than the verbatim window. Keyed by scope
    # ('global') because ARIA must recall agent conversations too, not just her own.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_summary (
            scope      TEXT PRIMARY KEY,
            summary    TEXT,
            upto_id    INTEGER,
            updated_at TEXT
        )
        """
    )
    _migrate(conn)
    return conn


def _migrate(conn):
    """Idempotent schema top-ups for databases created by an earlier version."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_memory)")}
    if "dry_run" not in cols:
        # Marks rows written by a dry run. Without this, a DRY_RUN test that fires
        # an agent cycle writes proposal_filed rows that look identical to real
        # ones — and Bobby's suppression would then silence a REAL cycle over a
        # proposal that was never actually posted to Slack.
        conn.execute("ALTER TABLE agent_memory ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0")
        conn.commit()


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


def add_memory(agent, event_type, summary, payload_id=None, dry_run=None):
    """Record one agent-memory event. `summary` is a short Python-written line
    (never model-generated). Returns the new row id. Timestamp is UTC ISO now.

    Rows are stamped with the run mode (defaulting to config.DRY_RUN) so a dry run
    can never be mistaken for real work — see get_recent_memories."""
    now = datetime.now(timezone.utc).isoformat()
    dry = int(bool(config.DRY_RUN if dry_run is None else dry_run))
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO agent_memory (agent, timestamp, event_type, summary, payload_id, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent, now, event_type, summary, payload_id, dry),
        )
        conn.commit()
    return cur.lastrowid


def update_memory_by_payload(payload_id, event_type):
    """Transition the memory row(s) for `payload_id` to a new event_type — used
    when the approvals poller sees a ✅/❌ (proposal_filed -> approved/rejected).
    Scoped to the current run mode so a dry run can't rewrite real history.
    Returns the number of rows updated (0 if none match). Idempotent."""
    if not payload_id:
        return 0
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE agent_memory SET event_type = ? WHERE payload_id = ? AND dry_run = ?",
            (event_type, payload_id, int(bool(config.DRY_RUN))),
        )
        conn.commit()
    return cur.rowcount


def get_recent_memories(agent, limit=10, mode_isolated=True):
    """Return this agent's most recent memories, NEWEST FIRST, as a list of dicts
    with keys id/agent/timestamp/event_type/summary/payload_id.

    Isolated by run mode by default: a REAL run never sees dry-run rows, and a dry
    run never sees real ones. This matters because Bobby's suppression reads this —
    a dry-run proposal was never posted to Slack, so it must not silence a real
    cycle (and vice versa). Pass mode_isolated=False to inspect everything.
    """
    sql = ("SELECT id, agent, timestamp, event_type, summary, payload_id "
           "FROM agent_memory WHERE agent = ?")
    params = [agent]
    if mode_isolated:
        sql += " AND dry_run = ?"
        params.append(int(bool(config.DRY_RUN)))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(zip(_MEMORY_COLS, r)) for r in rows]


# --- Conversation memory (Stage 3) ------------------------------------------
_TURN_COLS = ("id", "timestamp", "speaker", "content", "session_id")
_SUMMARY_SCOPE = "global"


def add_conversation_turn(session_id, speaker, content):
    """Record one voice turn. `speaker` is 'user', 'aria', or an agent name.
    Returns the new row id."""
    content = (content or "").strip()
    if not content:
        return None
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO conversation_memory (timestamp, speaker, content, session_id) "
            "VALUES (?, ?, ?, ?)", (now, speaker, content, session_id))
        conn.commit()
    return cur.lastrowid


def get_recent_conversation_turns(limit=6):
    """The most recent turns across ALL sessions, NEWEST FIRST — global on purpose,
    so ARIA can recall what you and an agent discussed, not just her own turns."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, speaker, content, session_id FROM conversation_memory "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(zip(_TURN_COLS, r)) for r in rows]


def get_conversation_turns_to_compress(upto_id, exclude_ids, limit=60):
    """Turns not yet folded into the summary and not in the verbatim window —
    i.e. the ones that have just aged out. OLDEST FIRST."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, speaker, content, session_id FROM conversation_memory "
            "WHERE id > ? ORDER BY id ASC LIMIT ?", (upto_id or 0, limit)).fetchall()
    turns = [dict(zip(_TURN_COLS, r)) for r in rows]
    keep = set(exclude_ids or ())
    return [t for t in turns if t["id"] not in keep]


def get_conversation_summary(scope=_SUMMARY_SCOPE):
    """Return (summary_text, upto_id). ('', 0) when nothing is summarized yet."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT summary, upto_id FROM conversation_summary WHERE scope = ?",
            (scope,)).fetchone()
    return (row[0] or "", row[1] or 0) if row else ("", 0)


def set_conversation_summary(summary, upto_id, scope=_SUMMARY_SCOPE):
    """Store the rolling summary and how far it covers."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO conversation_summary (scope, summary, upto_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
                summary    = excluded.summary,
                upto_id    = excluded.upto_id,
                updated_at = excluded.updated_at
            """, (scope, summary, upto_id, now))
        conn.commit()
    return upto_id


def prune_conversations(days):
    """Delete turns older than `days`. Returns the number removed. The summary is
    left alone — it's the compressed trace of what those turns said."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM conversation_memory WHERE timestamp < ?", (cutoff,))
        conn.commit()
    return cur.rowcount
