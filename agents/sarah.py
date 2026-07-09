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
import re
from datetime import datetime, timezone
from pathlib import Path

import config
from agents.base_agent import BaseAgent
from integrations.slack_bus import read_channel

# --- Paths ------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_PATH = _ROOT / "agent_prompts.md"
NOTES_PATH = _ROOT / "memory" / "sarah_notes.jsonl"


def _strip_reasoning(text):
    """Strip a reasoning model's thinking tokens, leaving only the final answer.

    Sarah runs on a reasoning model (deepseek-r1), which emits its chain of
    thought inside <think>...</think> before the answer. Keep only what follows
    the reasoning so nothing but the digest reaches Slack.
    """
    if not text:
        return text
    # Remove complete <think>...</think> blocks.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # If an unmatched </think> remains (opening tag lost/truncated), keep only
    # what's after the last one — that's the final answer.
    if re.search(r"</think>", text, flags=re.IGNORECASE):
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    # Drop any stray opening tag.
    text = re.sub(r"<think>", "", text, flags=re.IGNORECASE)
    return text.strip()


def load_agent_prompt(agent_name, path=PROMPTS_PATH):
    """Extract an agent's system prompt from agent_prompts.md at runtime.

    Prompts are the single source of truth in that file: each lives in the first
    fenced code block after its "## <Name> — ..." heading. Never inline them
    into Python.
    """
    text = Path(path).read_text(encoding="utf-8")
    pattern = re.compile(
        r"^##\s+" + re.escape(agent_name) + r"\b[^\n]*\n+```[^\n]*\n(.*?)^```",
        re.DOTALL | re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        raise ValueError(
            f"Could not find a prompt block for '{agent_name}' in {path}"
        )
    return m.group(1).strip()


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
                "CHANNEL_ID_FEEDBACK is not set in .env — read_channel needs a "
                "channel ID, not '#feedback'."
            )

        messages = read_channel(feedback_id, limit=limit)
        items = [m.strip() for m in messages if m and m.strip()]

        if not items:
            digest = "No new patterns since last digest."
            self.post(digest)
            return digest

        joined = "\n".join(f"- {m}" for m in items)
        task = (
            "Here are the most recent messages from the #feedback channel "
            "(newest first). Read them and produce your digest for "
            "#support-intel now, following your instructions.\n\n"
            f"{joined}"
        )
        digest = _strip_reasoning(self.run_turn(task))
        self.post(digest)
        return digest
