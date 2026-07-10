"""Conversation mode (Step 7A): a live back-and-forth session with one agent.

The user opens a session ("let me talk to Bobby"), ARIA loads that agent's recent
work context ONCE, and then relays turns: user speaks -> agent replies (fast
conversation model) -> ARIA's voice reads it -> both turns mirrored to
#chat-<agent>. History is kept in RAM for the session only (the Slack channel is
the permanent record). Audio I/O lives in main.py; this module is audio-free so
it's testable text-only.
"""
import re
from collections import deque

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


ECHO_OVERLAP_THRESHOLD = 0.7  # word-overlap ratio above which a turn is ARIA's own echo


def is_echo(text, recent_agent_speech, threshold=ECHO_OVERLAP_THRESHOLD):
    """True if `text` is (mostly) a re-transcription of something ARIA just said —
    i.e. ARIA's TTS bled into the mic and Whisper heard her own words back.

    Compares the user transcript's word set against each recent agent utterance;
    if their overlap covers >= `threshold` of the transcript's words, it's an echo.
    Measuring overlap relative to the (short) transcript means a captured line that
    is a subset of a longer agent line ("connecting you to bobby" inside
    "Connecting you to Bobby.") still scores 1.0, while a genuine question that
    happens to share a word or two stays well under the bar."""
    words = set(_normalize(text).split())
    if not words:
        return False
    for utt in recent_agent_speech or ():
        u = set(_normalize(utt).split())
        if u and len(words & u) / len(words) >= threshold:
            return True
    return False


def classify_turn(text, duration_sec, recent_agent_speech=None):
    """Classify a captured turn: 'silence' (drop / retry), 'echo' (ARIA's own TTS
    heard back — rejected like silence), 'exit', or 'message' (send to the agent).
    The hallucination check runs BEFORE the exit check, so a short 'thanks' on
    silence is treated as silence, never an exit; echo is checked before exit too,
    so an echoed sign-off word can't be mistaken for a real exit."""
    if not text or not text.strip():
        return "silence"
    if is_silence_hallucination(text, duration_sec):
        return "silence"
    if is_echo(text, recent_agent_speech):
        return "echo"
    if is_exit(text):
        return "exit"
    return "message"


# --- Turn-loop driver (audio-free; main.py supplies capture + speak) ----------
# Spoken lines and the retry policy for unintelligible input.
FALLBACK_PROMPT = "I didn't catch that, sir. Say again?"
GIVE_UP_LINE = "I'm having trouble hearing you, sir. Returning to standby."
BACK_TO_STANDBY = "Back to standby."
# After this many CONSECUTIVE silence/echo turns we give up (each of the first
# few prompts the user to try again; the Nth ends the session gracefully).
MAX_CONSECUTIVE_SILENCE = 4


def run_conversation(session, capture_turn, speak, logger=None,
                     opening_line=None, should_stop=None):
    """Drive one conversation session's turn loop. Audio-free and fully testable:

      capture_turn() -> (text, duration_sec)   one recorded+transcribed turn
      speak(text)                              read a line aloud (blocking)
      logger                                   optional logging.Logger
      opening_line                             spoken before the first capture
                                               (e.g. "Connecting you to Bobby.")
      should_stop()                            truthy -> shut the loop down

    Handles the fallback/retry policy for silence & echo, and ALWAYS ends with a
    named exit reason (spoken + printed + logged as "[EXIT REASON: ...]"). It never
    exits silently — a silent exit was the original bug. Returns the reason string.
    """
    recent = deque(maxlen=3)   # ARIA's last few utterances, for echo rejection

    def say(text):
        speak(text)
        recent.append(text)    # track so the same words heard back count as echo

    if opening_line:
        say(opening_line)
    session.open()
    if logger:
        logger.info("conversation opened with %s (topic=%s)", session.agent, session.topic)

    consecutive = 0
    reason = None
    try:
        while not (should_stop and should_stop()):
            text, duration = capture_turn()
            kind = classify_turn(text, duration, recent)

            if kind in ("silence", "echo"):
                consecutive += 1
                note = f"  (ignored {kind} {text!r}; attempt {consecutive}/{MAX_CONSECUTIVE_SILENCE})"
                print(note)
                if logger:
                    logger.info(note.strip())
                if consecutive >= MAX_CONSECUTIVE_SILENCE:
                    reason = f"unintelligible input ({consecutive} consecutive silence/echo turns)"
                    break
                say(FALLBACK_PROMPT)      # option 2: prompt, then re-open the mic
                continue

            consecutive = 0
            if kind == "exit":
                reason = "user exit phrase"
                break

            # message -> agent turn
            print(f"  you->{session.agent}: {text}")
            reply = session.agent_reply(text)
            session.mirror(text, reply)
            print(f"  {session.agent}: {reply}")
            if logger:
                logger.info("%s: %s", session.agent, reply)
            say(reply)
        else:
            reason = "stop signal (shutdown)"
    except Exception as exc:  # never let the loop die silently
        reason = f"error: {exc!r}"
        if logger:
            logger.exception("conversation-mode error")
    finally:
        session.close()

    if reason is None:
        reason = "ended"
    print(f"  [conversation ended] {reason}")
    if logger:
        logger.info("[EXIT REASON: %s]", reason)

    # Closing line: the give-up message already names standby; otherwise a plain
    # hand-back. Guarded so a give-up doesn't also say "Back to standby".
    say(GIVE_UP_LINE if reason.startswith("unintelligible") else BACK_TO_STANDBY)
    return reason


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
        first_turn = not self.history  # nothing said yet -> this is turn 1
        opener = (
            f'This is the FIRST turn of the conversation. You MUST begin your reply '
            f'with a brief, natural identification line — "{self.name} here." or '
            f'"{self.name} here, sir." — then a short pause, then address the topic. '
            "Do this ONLY on this first turn.\n\n"
            if first_turn else
            "You have already introduced yourself earlier in this conversation. Do "
            "NOT identify yourself again — go straight to answering.\n\n"
        )
        system = (
            self.persona
            + "\n\nYou are in a LIVE VOICE CONVERSATION with the user. "
            + opener
            + "Your spoken replies must be 2-3 sentences MAXIMUM — never more than "
            "three sentences, and shorter is better. If the user wants more detail, "
            "they will ask a follow-up. Do NOT use bullet lists or enumerate item by "
            "item — this is spoken dialogue, not a written report. Use the SPECIFIC "
            "facts and "
            "numbers from the CONTEXT below — quote real values (creative ids, ROAS, "
            "CPA, counts); never invent figures. If the context doesn't cover "
            "something, say so briefly.\n\n"
            f"CONTEXT (loaded once at the start of this conversation):\n{self.context}"
        )
        messages = [{"role": "system", "content": system}]
        messages += self.history
        messages.append({"role": "user", "content": user_text})
        reply = strip_reasoning(
            call_hermes(messages, model=self.model, num_predict=120, keep_alive="30m"))
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
