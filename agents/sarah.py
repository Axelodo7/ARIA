"""Sarah — the customer support agent.

She reads recent #feedback messages, asks Hermes (with her persona prompt loaded
from agent_prompts.md at runtime) to cluster them into a digest, and posts that
digest to #support-intel. Her one tool is save_note, which appends to a simple
JSONL file for now (SQLite comes in a later step).

She reads Slack directly in cycle() — there is deliberately no read_feedback
tool. Hermes is hit once per run; the tool loop only engages if she chooses to
call save_note.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import config
from agents.base_agent import BaseAgent, load_agent_prompt, strip_reasoning
from integrations import slack_bus

# --- Paths ------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
NOTES_PATH = _ROOT / "memory" / "sarah_notes.jsonl"

# Slack system events that are never user feedback (joins, topic changes, ...).
_SYSTEM_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive",
}


# --- Tool spec (format injected separately by tools_block) -------------------
SAVE_NOTE_TOOL = {
    "name": "save_note",
    "description": (
        "Save a short internal note for later recall — a notable pattern or "
        "something worth remembering across digests. Internal only; never shown "
        "to users."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "note": {"type": "string", "description": "The note text to save."}
        },
        "required": ["note"],
    },
}


class Sarah(BaseAgent):
    name = "Sarah"
    icon = config.PERSONAS["Sarah"]
    channel = config.CHANNELS["support_intel"]
    model = config.AGENT_MODELS["Sarah"]
    system_prompt = load_agent_prompt("Sarah")

    def __init__(self):
        self.tools = [SAVE_NOTE_TOOL]
        self.tool_handlers = {"save_note": self._save_note}

    def _save_note(self, args):
        """Append a note to memory/sarah_notes.jsonl. Returns a confirmation."""
        note = (args or {}).get("note", "").strip()
        if not note:
            return "No note text provided; nothing saved."
        NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(timezone.utc).isoformat(), "note": note}
        with open(NOTES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return f"Saved note: {note}"

    def cycle(self, limit=25):
        """Read recent #feedback, produce a digest, post it to #support-intel.

        Returns the digest text (also useful for tests/logging).
        """
        feedback_id = config.CHANNEL_IDS.get("feedback")
        if not feedback_id:
            raise RuntimeError(
                "CHANNEL_ID_FEEDBACK is not set in .env — reading Slack needs a "
                "channel ID, not '#feedback'."
            )

        # Only process feedback newer than last cycle's bookmark, so already-
        # digested messages are never re-processed. history() gives us the ts of
        # each message, which we need to advance the bookmark.
        since = self.get_bookmark()
        messages = slack_bus.history(feedback_id, limit=limit)  # newest-first dicts
        fresh = [m for m in messages
                 if (m.get("text") or "").strip()
                 and m.get("subtype") not in _SYSTEM_SUBTYPES  # skip join/leave etc.
                 and (since is None or float(m.get("ts", "0")) > float(since))]
        fresh.sort(key=lambda m: float(m.get("ts", "0")))  # oldest -> newest

        if not fresh:
            digest = "No new patterns since last digest."
            self.post(digest)
            return digest

        joined = "\n".join(f"- {m['text']}" for m in fresh)
        task = (
            "Here are the new messages from the #feedback channel (oldest "
            "first). Read them and produce your digest for #support-intel now, "
            "following your instructions.\n\n"
            f"{joined}"
        )
        digest = strip_reasoning(self.run_turn(task))
        self.post(digest)
        # Advance the bookmark only after a successful cycle.
        self.set_bookmark(fresh[-1]["ts"])
        return digest
