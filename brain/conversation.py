"""Conversation mode (Step 7A): a live back-and-forth session with one agent.

The user opens a session ("let me talk to Bobby"), ARIA loads that agent's recent
work context ONCE, and then relays turns: user speaks -> agent replies (fast
conversation model) -> ARIA's voice reads it -> both turns mirrored to
#chat-<agent>. History is kept in RAM for the session only (the Slack channel is
the permanent record). Audio I/O lives in main.py; this module is audio-free so
it's testable text-only.
"""
import re

import config
from brain.hermes_common import call_hermes
from agents.base_agent import load_agent_prompt, strip_reasoning
from integrations import slack_bus

USER_NAME = "Ayyan"

_AGENTS = ("sarah", "bobby", "ava", "tom")
_WORK_CHANNELS = {"sarah": "support_intel", "bobby": "ads", "ava": "content", "tom": "dev"}
_CHAT_CHANNELS = {"sarah": "chat_sarah", "bobby": "chat_bobby", "ava": "chat_ava", "tom": "chat_tom"}

_SYSTEM_SUBTYPES = {"channel_join", "channel_leave", "channel_topic",
                    "channel_purpose", "channel_name", "channel_archive", "channel_unarchive"}

_AG = r"(sarah|bobby|ava|tom)"
# Intent-open patterns (deterministic). Longer/more-specific first.
_OPEN_RES = [
    re.compile(rf"have a conversation with {_AG}\b", re.IGNORECASE),
    re.compile(rf"i want to talk to {_AG}\b", re.IGNORECASE),
    re.compile(rf"i want to chat with {_AG}\b", re.IGNORECASE),
    re.compile(rf"let me talk to {_AG}\b", re.IGNORECASE),
    re.compile(rf"\btalk to {_AG}\b", re.IGNORECASE),
    re.compile(rf"chat with {_AG}\b", re.IGNORECASE),
]

def _normalize(text):
    """Lowercase, drop apostrophes, turn other punctuation into spaces, and
    collapse whitespace — so "Thanks, that's all.", "Thanks that's all", and
    " thanks , that's all " all normalize to "thanks thats all"."""
    t = (text or "").lower().replace("'", "").replace("’", "")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Exit phrases — matched ONLY when they ARE the whole (normalized) message, so an
# exit word inside longer speech ("thanks for the update, but ...") is NOT an exit.
_EXIT_PHRASES = {_normalize(p) for p in (
    "thanks", "thanks that's all", "that's all", "we're done", "back to aria",
    "goodbye", "thanks that is all", "we are done", "bye")}


def detect_open(text):
    """Return (agent, topic) if `text` opens a conversation, else None. `topic`
    is whatever follows 'about ...', or None."""
    for rx in _OPEN_RES:
        m = rx.search(text or "")
        if m:
            agent = m.group(1).lower()
            topic_m = re.search(r"\babout\s+(.+)$", text, re.IGNORECASE)
            topic = topic_m.group(1).strip().rstrip(".?!") if topic_m else None
            return agent, topic
    return None


def is_exit(text):
    """True ONLY when the whole (normalized) message is an exit phrase — never
    when an exit word merely appears inside a longer utterance."""
    return _normalize(text) in _EXIT_PHRASES


# Whisper's silence hallucinations — matched on the whole (normalized) transcript.
_HALLUCINATION_PHRASES = {_normalize(p) for p in
                          ("you", "thank you", "thanks", "you know", "bye")}


def is_silence_hallucination(text, duration_sec=None):
    """True if the ENTIRE (normalized) transcript is one of Whisper's silence
    hallucinations — rejected REGARDLESS of duration. If Whisper heard only
    'You' / 'thanks' / 'you know' and nothing else, it's noise, even when VAD held
    the mic open on ambient sound. A LONGER transcript that merely contains such a
    word ('thanks for the update') is real speech and is kept.

    `duration_sec` is accepted for call-site compatibility; the exact-whole-
    transcript match supersedes the old < 1.5s length gate.
    """
    return _normalize(text) in _HALLUCINATION_PHRASES


def classify_turn(text, duration_sec):
    """Classify a captured turn: 'silence' (drop / extend), 'exit', or 'message'
    (send to the agent). The hallucination check runs BEFORE the exit check, so a
    short 'thanks' on silence is treated as silence, never as an exit."""
    if not text or not text.strip():
        return "silence"
    if is_silence_hallucination(text, duration_sec):
        return "silence"
    if is_exit(text):
        return "exit"
    return "message"


class ConversationSession:
    """One ephemeral chat session with an agent. History lives in RAM only."""

    def __init__(self, agent, topic=None):
        self.agent = agent.lower()
        self.name = self.agent.capitalize()
        self.topic = topic
        self.model = config.AGENT_CONVERSATION_MODELS.get(self.name, "hermes3")
        self.persona = load_agent_prompt(self.name)
        self.history = []       # [{role, content}] — RAM only, dropped on close
        self.context = ""       # loaded once in open()
        self._chat_channel = config.CHANNELS[_CHAT_CHANNELS[self.agent]]
        self._work_cid = config.CHANNEL_IDS.get(_WORK_CHANNELS[self.agent])

    # --- lifecycle ----------------------------------------------------------
    def open(self):
        """Announce the session in #chat-<agent> and load context once."""
        self._post_agent(f"{USER_NAME} started a conversation. Loading agent context...")
        self.context = self._load_context()
        return self.context

    def close(self):
        self._post_agent("Conversation ended.")

    # --- the turn -----------------------------------------------------------
    def agent_reply(self, user_text):
        """Produce the agent's reply to a user turn and append both to history."""
        system = (
            self.persona
            + "\n\nYou are in a LIVE VOICE CONVERSATION with the user. Reply "
            "conversationally in one to three sentences (it's read aloud). Use the "
            "SPECIFIC facts and numbers from the CONTEXT below — quote real values "
            "(creative ids, ROAS, CPA, counts); never invent figures. If the "
            "context doesn't cover something, say so briefly.\n\n"
            f"CONTEXT (loaded once at the start of this conversation):\n{self.context}"
        )
        messages = [{"role": "system", "content": system}]
        messages += self.history
        messages.append({"role": "user", "content": user_text})
        reply = strip_reasoning(
            call_hermes(messages, model=self.model, num_predict=200, keep_alive="30m"))
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def mirror(self, user_text, agent_text):
        """Post the user turn then the agent turn to #chat-<agent>, attributed."""
        slack_bus.post_as(USER_NAME, ":bust_in_silhouette:", self._chat_channel, user_text)
        self._post_agent(agent_text)

    # --- helpers ------------------------------------------------------------
    def _post_agent(self, text):
        slack_bus.post_as(self.name, config.PERSONAS.get(self.name, ":robot_face:"),
                          self._chat_channel, text)

    def _load_context(self):
        parts = []
        if self._work_cid:
            texts = []
            for m in slack_bus.history(self._work_cid, limit=20):  # newest first
                t = (m.get("text") or "").strip()
                if not t or m.get("subtype") in _SYSTEM_SUBTYPES:
                    continue
                if re.fullmatch(r"(?:<@\w+>\s*)+", t):
                    continue  # bare @-mentions
                texts.append(t)
            if texts:
                parts.append("Recent activity in your work channel (newest first):\n\n"
                             + "\n\n".join(texts))
            if self.topic:
                match = self._search_topic(self.topic)
                if match:
                    parts.append(f"The user specifically referenced '{self.topic}'. "
                                 f"Most relevant recent message:\n{match}")
        return "\n\n".join(parts) if parts else "(no recent context available)"

    def _search_topic(self, topic):
        keywords = set(re.findall(r"[a-z0-9_]+", topic.lower()))
        if not keywords or not self._work_cid:
            return None
        best, best_score = None, 0
        for m in slack_bus.history(self._work_cid, limit=50):
            text = m.get("text") or ""
            words = set(re.findall(r"[a-z0-9_]+", text.lower()))
            score = len(keywords & words)
            if score > best_score:
                best, best_score = text, score
        return best if best_score > 0 else None
