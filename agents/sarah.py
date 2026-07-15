"""Sarah — the customer support agent.

She reads recent #feedback messages and posts a clustered digest to
#support-intel. Her one tool is save_note, which appends to a simple JSONL file.

Phase 3 — label-then-count (Principle 1 applied to Sarah). The old design handed
the raw messages to the model and asked it to cluster AND tally in one shot; the
tally is a "right answer" task and gemma3 fabricated it (reproducibly reported a
2-message cluster as "4 users"). The counting is now split off the model:

  1. CLASSIFY (model): the model assigns every feedback message to a cluster,
     returning structured JSON — it lists which message *ids* belong together. It
     is explicitly told NOT to state any count.
  2. COUNT (Python): count = len(the cluster's id list), with ids validated and
     de-duplicated. The number is Python's, never the model's — so the model
     cannot inflate it, exactly like Bobby's dollar figures.
  3. ASSEMBLE (Python): the digest is rendered deterministically from the counted
     clusters, in Sarah's standard format. With the model out of the writing loop
     it cannot fabricate an item — an earlier model-narration version reproduced
     the worked IDOR example from Sarah's persona prompt as a phantom URGENT alert
     (3/3 runs), which deterministic assembly eliminates.

She reads Slack directly in cycle() — there is deliberately no read_feedback tool.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import config
from agents.base_agent import BaseAgent, load_agent_prompt, strip_reasoning
from brain.hermes_common import (CONTEXT_TOKEN_CAP, call_hermes, _first_json_object,
                                 business_context_block, estimate_tokens)
from integrations import slack_bus
from memory import store

_QUOTE_MAX_CHARS = 140

# Sections a cluster can land in, mapped to the digest's section headers. URGENT
# items appear only under URGENT (not duplicated), matching the digest examples.
_SECTIONS = ("URGENT", "BUG", "FEATURE", "PRAISE")
_SECTION_HEADERS = {
    "URGENT": "🚨 URGENT",
    "BUG": "BUGS/ISSUES (@Tom)",
    "FEATURE": "FEATURE REQUESTS (@Tom)",
    "PRAISE": "STORIES & PRAISE (@Ava)",
}


def _short_quote(text):
    """A short, REAL quote for the digest — taken from an actual message (never
    the model's paraphrase), collapsed to one line and truncated."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= _QUOTE_MAX_CHARS else flat[:_QUOTE_MAX_CHARS].rstrip() + "…"


def _cap_by_recency(fresh, max_tokens):
    """Keep the most recent messages from an oldest->newest list whose combined
    text stays within ~max_tokens, then return them still oldest->newest. At least
    one message is always kept. Uses the MEASURED token estimate — a flat
    chars/token guess under-counts dense text and lets the prompt overrun."""
    kept, total = [], 0
    for m in reversed(fresh):  # newest first
        total += estimate_tokens(m.get("text") or "") + 2   # +2 ≈ "- " bullet + newline
        if total > max_tokens and kept:
            break
        kept.append(m)
    return list(reversed(kept))  # back to oldest->newest

# --- Paths ------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
NOTES_PATH = _ROOT / "memory" / "sarah_notes.jsonl"

# Slack system events that are never user feedback (joins, topic changes, ...).
_SYSTEM_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive",
}


# --- Classification pass ----------------------------------------------------
# A focused, persona-free prompt: the model's ONLY job is to group messages by id.
# It must not count — Python does that from the id lists it returns.
_CLASSIFY_SYSTEM = (
    "You are a support-feedback classifier. You are given user feedback messages, "
    "each prefixed with a number (its id). Group the messages so that every "
    "message reporting the SAME underlying issue, the SAME feature request, or the "
    "SAME kind of praise lands in one cluster. Assign EVERY message to exactly one "
    "cluster; never put a message in two clusters.\n\n"
    "For each cluster output:\n"
    '  "section": one of "URGENT" (security/legal/data-exposure/distressed user), '
    '"BUG", "FEATURE", or "PRAISE"\n'
    '  "label": a short human label for the cluster (e.g. "Dark mode", "Export '
    'button broken")\n'
    '  "message_ids": the list of message numbers in this cluster\n\n'
    "Do NOT state any counts or totals — only list the message_ids; the counting "
    "is done downstream. Output ONLY a single JSON object, no prose, no code "
    "fences:\n"
    '{"clusters": [{"section": "FEATURE", "label": "Dark mode", "message_ids": '
    "[1, 2, 3]}]}"
)


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

        # Phase 2: cap the feedback block at ~CONTEXT_TOKEN_CAP tokens so a busy
        # #feedback can never push her prompt toward the window. Keep the MOST
        # RECENT messages (drop oldest first), then restore oldest->newest order.
        fresh = _cap_by_recency(fresh, CONTEXT_TOKEN_CAP)

        # Phase 3: classify (model) -> count (Python) -> assemble (Python).
        clusters = self._classify_and_count(fresh)
        if clusters:
            digest = self._assemble_digest(clusters)
        else:
            # Classification failed to parse — fall back to the single-shot digest
            # rather than dropping the cycle. Logged-worthy, but never a crash.
            print("[sarah] classification produced no clusters — falling back to "
                  "single-shot digest (counts unverified this cycle).")
            joined = "\n".join(f"- {m['text']}" for m in fresh)
            task = (
                "Here are the new messages from the #feedback channel (oldest "
                "first). Read them and produce your digest for #support-intel now, "
                "following your instructions.\n\n"
                f"{joined}"
            )
            digest = strip_reasoning(self.run_turn(task))

        self.post(digest)
        # Stage 1 memory: record the digest with the (deterministic) cluster
        # labels she reported, so she can see what she's already surfaced.
        if clusters:
            labels = ", ".join(f"{c['label']} ({c['count']})" for c in clusters)
            store.add_memory("Sarah", "digest_posted", f"Posted digest: {labels}"[:240])
        # Advance the bookmark only after a successful cycle.
        self.set_bookmark(fresh[-1]["ts"])
        return digest

    # --- Phase 3: label-then-count ------------------------------------------
    def _classify_and_count(self, fresh):
        """Classify each message into a cluster (model), then compute each
        cluster's count in Python. Returns a list of
        {section, label, count, ids, quote}, or [] if classification didn't parse.

        The count is len(validated unique ids) — the model never states it, so it
        cannot inflate a cluster (the '4 users' failure). Each message id is used
        at most once across clusters, and the representative quote is pulled from
        the ACTUAL message text, not the model's output.
        """
        numbered = "\n".join(f"{i}. {m.get('text', '')}"
                             for i, m in enumerate(fresh, start=1))
        # Business context + Stage 2 memory lead the classifier's system prompt so
        # Sarah's cycle knows recent developments; the classify instructions follow
        # and remain the operative task.
        raw = strip_reasoning(call_hermes(
            [{"role": "system", "content": business_context_block() + _CLASSIFY_SYSTEM},
             {"role": "user", "content": f"Feedback messages:\n{numbered}"}],
            model=self.model, think=False))
        obj = _first_json_object(raw)
        if not isinstance(obj, dict) or not isinstance(obj.get("clusters"), list):
            return []

        n = len(fresh)
        seen = set()          # a message id belongs to at most one cluster
        clusters = []
        for c in obj["clusters"]:
            if not isinstance(c, dict):
                continue
            ids = []
            for x in (c.get("message_ids") or []):
                xi = x if isinstance(x, int) else None
                if xi is None:
                    try:
                        xi = int(str(x).strip())
                    except (TypeError, ValueError):
                        continue
                if 1 <= xi <= n and xi not in seen:
                    ids.append(xi)
                    seen.add(xi)
            if not ids:
                continue
            section = str(c.get("section", "")).strip().upper()
            if section not in _SECTIONS:
                section = "BUG"   # safe default bucket for an odd label
            label = str(c.get("label", "")).strip() or "(unlabeled)"
            clusters.append({
                "section": section,
                "label": label,
                "count": len(ids),                       # <-- Python computes it
                "ids": ids,
                "quote": _short_quote(fresh[ids[0] - 1].get("text", "")),
            })
        return clusters

    def _assemble_digest(self, clusters):
        """Render the digest deterministically from the counted clusters, in
        Sarah's standard section format. The body is a right answer given the
        clusters, so Python builds it — the model never writes free digest text,
        which is what stops it reproducing the persona's example items as phantom
        feedback. Counts come straight from Python's tally."""
        blocks = []
        for sec in _SECTIONS:
            items = [c for c in clusters if c["section"] == sec]
            if not items:
                continue  # omit empty sections (never a fabricated URGENT)
            lines = [_SECTION_HEADERS[sec]]
            for c in items:
                if sec == "URGENT":
                    lines.append(f'- {c["label"]}. "{c["quote"]}"')
                else:
                    plural = "user" if c["count"] == 1 else "users"
                    lines.append(f'- {c["label"]} — {c["count"]} {plural}. "{c["quote"]}"')
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) if blocks else "No new patterns since last digest."
