"""Conversation mode (Step 7A): a live back-and-forth session with one agent.

The user opens a session ("let me talk to Bobby"), ARIA loads that agent's recent
work context ONCE, and then relays turns: user speaks -> agent replies (fast
conversation model) -> ARIA's voice reads it -> both turns mirrored to
#chat-<agent>. History is kept in RAM for the session only (the Slack channel is
the permanent record). Audio I/O lives in main.py; this module is audio-free so
it's testable text-only.
"""
import re
import uuid
from collections import deque

import config
from brain import conv_memory
from brain.hermes_common import call_hermes, business_context_block, cap_context
from agents.base_agent import load_agent_prompt, strip_reasoning
from integrations import slack_bus

USER_NAME = "Ayyan"

_AGENTS = ("sarah", "bobby", "ava", "tom")
_WORK_CHANNELS = {"sarah": "support_intel", "bobby": "ads", "ava": "content", "tom": "dev"}
_CHAT_CHANNELS = {"sarah": "chat_sarah", "bobby": "chat_bobby", "ava": "chat_ava", "tom": "chat_tom"}

_SYSTEM_SUBTYPES = {"channel_join", "channel_leave", "channel_topic",
                    "channel_purpose", "channel_name", "channel_archive", "channel_unarchive"}

_AG = r"(sarah|bobby|ava|tom)"

# Conversation-open detection is signal-based, not phrase-based, so garbled live
# transcription still triggers it. It fires on the CORE SIGNAL: an agent name +
# ANY conversational-intent verb, regardless of exact wording or tense — so
# "we talked to Bobby about X" (a mistranscription of "let me talk to Bobby")
# still opens, not just the tidy canonical phrasings.
_AGENT_RE = re.compile(rf"\b{_AG}\b", re.IGNORECASE)
_CONV_INTENT_RE = re.compile(
    r"\b(?:talk|talked|talking|chat|chatting|chatted|speak|speaking|spoke|"
    r"conversation|connect|connecting)\b", re.IGNORECASE)
# Priority rule: explicit COMMAND dispatch ("ask/tell/have/get <agent> ...")
# must DISPATCH (fire-and-forget), never open a conversation — so we bail out of
# detect_open on these and let aria.think handle the dispatch. This mirrors the
# dispatch pattern in aria_agent._parse_dispatch so the two stay in lockstep.
_DISPATCH_VERB_RE = re.compile(rf"\b(?:ask|tell|have|get)\s+{_AG}\b", re.IGNORECASE)

def _normalize(text):
    """Lowercase, drop apostrophes, turn other punctuation into spaces, and
    collapse whitespace — so "Thanks, that's all.", "Thanks that's all", and
    " thanks , that's all " all normalize to "thanks thats all"."""
    t = (text or "").lower().replace("'", "").replace("’", "")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Exit phrases — matched ONLY when they ARE the whole (normalized) message, so an
# exit word inside longer speech ("thanks for the update, but ...") is NOT an exit.
# The natural sign-offs ("thank you", "that'll be all", ...) matter most for the
# FOLLOW-UP window (classify_followup), which honours an exit before treating a
# short phrase as noise. Conversation mode still checks the hallucination set first
# (classify_turn), so adding these here doesn't change when it exits.
_EXIT_PHRASES = {_normalize(p) for p in (
    "thanks", "thanks that's all", "that's all", "we're done", "back to aria",
    "goodbye", "thanks that is all", "we are done", "bye",
    "thank you", "thank you sir", "thanks sir", "that'll be all",
    "that will be all", "that is all", "no that's all", "nothing else",
    "that's it", "that's all for now", "all done", "we're good", "i'm done")}


def detect_open(text):
    """Return (agent, topic) if `text` opens a conversation, else None.

    Opens when the text names an agent AND carries a conversational-intent verb
    (talk/talked/chat/speak/connect/... any tense) — robust to imperfect
    transcription. Explicit command dispatch ("ask/tell/have/get <agent> ...")
    takes priority and returns None so it dispatches instead. `topic` is whatever
    follows 'about ...', else whatever follows the agent name, else None.
    """
    t = text or ""
    if _DISPATCH_VERB_RE.search(t):
        return None  # command dispatch wins — let aria.think fire-and-forget it
    am = _AGENT_RE.search(t)
    if not am or not _CONV_INTENT_RE.search(t):
        return None
    agent = am.group(1).lower()
    topic_m = re.search(r"\babout\s+(.+)$", t, re.IGNORECASE)
    if topic_m:
        topic = topic_m.group(1).strip().rstrip(".?!")
    else:
        topic = t[am.end():].strip(" ,:-").rstrip(".?!").strip() or None
    return agent, topic


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


# --- Follow-up window (audio-free driver; main.py supplies capture + handle) --
# After ARIA speaks, the mic stays open for a short window so a follow-up needs no
# wake word. classify_followup differs from classify_turn in ORDER: it honours an
# EXIT phrase before the noise checks, because here the user WANTS "thank you" /
# "that's all" to end the session, and a false exit only drops to idle (harmless).
# A garbled/empty capture is 'noise' — ignored, the window stays open — so a stray
# sound never fires a turn (uses the same _looks_unintelligible guard as the wake
# path). ARIA's own speech heard back is 'echo', ignored the same way.
def classify_followup(text, recent_agent_speech=None):
    """'exit' | 'echo' | 'noise' | 'message' for a captured follow-up."""
    from brain.aria_agent import _looks_unintelligible  # lazy: avoid an import cycle
    if not text or not text.strip():
        return "noise"
    if is_exit(text):
        return "exit"
    if is_echo(text, recent_agent_speech):
        return "echo"
    if is_silence_hallucination(text) or _looks_unintelligible(text):
        return "noise"
    return "message"


# Consecutive noise/echo captures before the window gives up and returns to idle —
# so a persistently noisy room can't hold the mic open forever.
MAX_FOLLOWUP_NOISE = 3


def run_followup_window(capture, handle, should_stop=None, logger=None,
                        max_noise=MAX_FOLLOWUP_NOISE, initial_recent=None):
    """Drive the post-reply follow-up window until it ends. Audio-free and testable.

      capture()  -> (text, got_speech). got_speech False means the VAD window timed
                    out with no speech onset (silence) — return to idle. True means
                    speech was captured and transcribed to `text`.
      handle(text) -> the spoken reply (str) or None. Routes/answers/persists one
                    continued turn exactly as a wake-word turn would; the returned
                    reply is tracked so ARIA's own voice heard back counts as echo.
      should_stop() truthy -> shut down.

    Returns a named reason (never exits silently). Each real turn RESETS the window
    (natural back-and-forth); silence, an exit phrase, or too much consecutive noise
    ends it and the caller returns to wake-word/idle.
    """
    # Seed with the reply that opened the window (the wake-word turn's answer) so
    # ARIA's own voice heard back on the FIRST follow-up capture counts as echo.
    recent = deque([r for r in (initial_recent or ()) if r], maxlen=3)
    consecutive_noise = 0
    while not (should_stop and should_stop()):
        text, got_speech = capture()
        if not got_speech:
            reason = "no follow-up (window elapsed in silence)"
            break
        kind = classify_followup(text, recent)
        if kind == "exit":
            reason = "user exit phrase"
            break
        if kind in ("noise", "echo"):
            consecutive_noise += 1
            note = (f"  (follow-up ignored {kind} {text!r}; "
                    f"{consecutive_noise}/{max_noise})")
            print(note)
            if logger:
                logger.info(note.strip())
            if consecutive_noise >= max_noise:
                reason = f"too much {kind} ({consecutive_noise} in a row)"
                break
            continue                    # keep the window open
        consecutive_noise = 0
        reply = handle(text)            # continued turn — speaks its own reply
        if reply:
            recent.append(reply)
    else:
        reason = "stop signal (shutdown)"
    print(f"  [follow-up window closed] {reason}")
    if logger:
        logger.info("[FOLLOW-UP CLOSED: %s]", reason)
    return reason


# --- Turn-loop driver (audio-free; main.py supplies capture + speak) ----------
# Spoken lines and the retry policy for unintelligible input.
FALLBACK_PROMPT = "I didn't catch that, sir. Say again?"
GIVE_UP_LINE = "I'm having trouble hearing you, sir. Returning to standby."
BACK_TO_STANDBY = "Back to standby."
# After this many CONSECUTIVE silence/echo turns we give up (each of the first
# few prompts the user to try again; the Nth ends the session gracefully).
MAX_CONSECUTIVE_SILENCE = 4


def resolve_agent_voice(name, logger=None):
    """Return the agent's ElevenLabs voice ID, or None (ARIA's default voice) if
    it's unset — logging a warning in that case rather than crashing."""
    voice = config.AGENT_VOICES.get(name)
    if not voice:
        msg = (f"[voice] no voice ID configured for {name} "
               f"(set {name.upper()}_VOICE_ID in .env); using ARIA's voice.")
        print(msg)
        if logger:
            logger.warning(msg)
        return None
    return voice


def run_conversation(session, capture_turn, speak, logger=None,
                     opening_line=None, should_stop=None):
    """Drive one conversation session's turn loop. Audio-free and fully testable:

      capture_turn() -> (text, duration_sec)   one recorded+transcribed turn
      speak(text, voice_id=None)               read a line aloud (blocking); the
                                               agent's replies pass the agent's
                                               voice, ARIA's own lines pass None
      logger                                   optional logging.Logger
      opening_line                             spoken before the first capture
                                               (e.g. "Connecting you to Bobby.")
      should_stop()                            truthy -> shut the loop down

    Handles the fallback/retry policy for silence & echo, and ALWAYS ends with a
    named exit reason (spoken + printed + logged as "[EXIT REASON: ...]"). It never
    exits silently — a silent exit was the original bug. Returns the reason string.
    """
    recent = deque(maxlen=3)   # ARIA's last few utterances, for echo rejection
    # The agent's own voice (falls back to ARIA's, with a warning, if unset).
    agent_voice = resolve_agent_voice(session.name, logger)

    def say(text, voice_id=None):
        speak(text, voice_id)
        recent.append(text)    # track so the same words heard back count as echo

    if opening_line:
        say(opening_line)      # ARIA's line -> ARIA's voice
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
            say(reply, agent_voice)   # agent's reply (incl. "Bobby here.") -> agent voice
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


def _id_opener_re(name):
    """Match a leading '<Name> here[, sir].' identification line (any trailing
    punctuation/dash)."""
    return re.compile(
        rf'^\s*["\']?{re.escape(name)}\s+here(?:,?\s*sir)?\s*[.!?,:;–—-]*\s*',
        re.IGNORECASE)


def _has_identification(text, name):
    return bool(_id_opener_re(name).match(text or ""))


def _strip_identification(text, name):
    return _id_opener_re(name).sub("", text or "", count=1).lstrip()


# --- No-execution guard (conversation mode has NO tool access) ----------------
# Agents in conversation mode can only discuss and advise — there is no path from
# here to spending, scaling, pausing, publishing, or merging. This rule goes into
# EVERY conversation system prompt so no agent claims to have acted.
_NO_EXECUTION_RULE = (
    "HARD RULE — YOU CANNOT ACT FROM THIS CONVERSATION. You have NO ability to "
    "execute, apply, schedule, spend, publish, or merge anything here: this is a "
    "spoken discussion with no tool access. You can ONLY discuss and advise. If "
    "the user asks you to do something, say you'll file it as a proposal for "
    "approval — NEVER claim you have done it or are doing it. Never say "
    "'executing', 'applying', 'done', or 'updated', and never state a new system "
    "state (a new budget, a new total, a published post) as fact. Real actions "
    "happen only later, through the human approval flow — never through this chat.\n\n"
)

# Appended to force a rewrite if a first draft slips into execution language.
_EXEC_REGEN_DIRECTIVE = (
    "\n\nYOUR PREVIOUS DRAFT CLAIMED TO EXECUTE OR COMPLETE AN ACTION. That is "
    "false — you cannot act from this conversation. Rewrite to ADVISE ONLY: "
    "recommend the action and say you'll file it as a proposal for approval. Do "
    "not use 'executing', 'applying', 'done', or 'updated', and do not state any "
    "new budget/total/state as if it happened."
)

# Deterministic safe reply if even the regenerated draft still claims execution —
# guarantees the spoken/mirrored reply never asserts an action was taken.
_EXEC_SAFE_FALLBACK = (
    "I can't action that from this conversation, sir — I can only advise. I'll "
    "file it as a proposal to #approvals for your approval."
)

# Patterns that mark a reply CLAIMING it did or is doing an action (vs. advising
# it). Tuned to catch claims, not discussion: "I recommend scaling cr_A" is fine;
# "Executing scale" / "I've scaled it" / "now at $55/day" are not.
_EXEC_PATTERNS = [
    re.compile(r"\bexecut(?:ing|ed)\b", re.IGNORECASE),
    re.compile(r"\bI(?:'m| am)\s+(?:executing|applying|scaling|pausing|"
               r"publishing|merging|scheduling|spending|bumping|raising)\b",
               re.IGNORECASE),
    re.compile(r"\bI(?:'ve| have)\s+(?:scaled|applied|paused|published|merged|"
               r"scheduled|spent|executed|updated|increased|set|changed|bumped|"
               r"raised|done)\b", re.IGNORECASE),
    re.compile(r"\bnow\s+(?:at|running at|sitting at|set to)\s+\$?\d", re.IGNORECASE),
    re.compile(r"\b(?:total|budget|spend|daily spend)\b[^.]{0,25}\bnow\b[^.]{0,10}\$?\d",
               re.IGNORECASE),
    re.compile(r"\b(?:it'?s|that'?s|it is|that is|all)\s+(?:done|applied|updated|"
               r"scheduled|published|live)\b", re.IGNORECASE),
    re.compile(r"\bconsider it (?:done|scaled|applied|handled|paused)\b", re.IGNORECASE),
]


def contains_execution_language(text):
    """True if a conversation reply claims to HAVE executed / BE executing an
    action, or states a new system state as fact. Deterministic backstop to the
    prompt rule — advisory language ('I'd scale', 'I'll file a proposal') is fine.

    Curly apostrophes are normalized first: gemma3 routinely emits 'I've' with a
    U+2019, which the straight-quote patterns would otherwise miss."""
    t = (text or "").replace("’", "'")
    return any(p.search(t) for p in _EXEC_PATTERNS)


class ConversationSession:
    """One ephemeral chat session with an agent. History lives in RAM only."""

    def __init__(self, agent, topic=None):
        self.agent = agent.lower()
        self.name = self.agent.capitalize()
        self.topic = topic
        # Tags this conversation's turns in conversation_memory. Retrieval is
        # global (ARIA must see agent conversations too), so it's a label, not a filter.
        self.session_id = f"{self.agent}-{uuid.uuid4().hex[:8]}"
        self.model = (config.AGENT_CONVERSATION_MODELS.get(self.name)
                      or config.CONVERSATION_SPEED_MODEL)
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
            business_context_block()   # shared business knowledge first, then persona
            + self.persona
            + "\n\nYou are in a LIVE VOICE CONVERSATION with the user. "
            + _NO_EXECUTION_RULE      # you can advise but NOT act from here
            + opener
            + "Your spoken replies must be 2-3 sentences MAXIMUM — never more than "
            "three sentences, and shorter is better. Do NOT open with filler like "
            "'Okay', 'Let's look at those', or 'Sure' — go straight to the answer. "
            "If the user wants more detail, "
            "they will ask a follow-up. Do NOT use bullet lists or enumerate item by "
            "item — this is spoken dialogue, not a written report. Use the SPECIFIC "
            "facts and "
            "numbers from the CONTEXT below — quote real values (creative ids, ROAS, "
            "CPA, counts); never invent figures. If the context doesn't cover "
            "something, say so briefly.\n\n"
            # Stage 3: what was actually said before, across restarts and across
            # agents. Hard-capped, so it can't grow the prompt over time.
            + conv_memory.conversation_block()
            + f"CONTEXT (loaded once at the start of this conversation):\n{self.context}"
        )

        def _generate(extra_system=""):
            # think=False: conversation mode never wants native reasoning tokens —
            # on thinking models they'd consume the whole budget and return empty
            # content; on non-thinking models it's a harmless no-op.
            messages = [{"role": "system", "content": system + extra_system}]
            messages += self.history
            messages.append({"role": "user", "content": user_text})
            return strip_reasoning(
                call_hermes(messages, model=self.model, num_predict=100,
                            keep_alive="30m", think=False))

        reply = _generate()
        # Deterministic backstop to the no-execution rule: if the draft claims to
        # have acted, regenerate ONCE with a stronger directive; if it STILL claims
        # execution, fall back to a safe advisory so the reply never asserts an
        # action was taken (conversation mode has no tool access — see Bug 1).
        if contains_execution_language(reply):
            regen = _generate(_EXEC_REGEN_DIRECTIVE)
            reply = regen if not contains_execution_language(regen) else _EXEC_SAFE_FALLBACK
        # Enforce the identification rule DETERMINISTICALLY — the speed model is
        # swappable and sometimes small, so don't trust it to comply: exactly one
        # identification line, on the first turn only. Add it if the model forgot;
        # strip a repeat if the model added one on a later turn.
        if first_turn:
            if not _has_identification(reply, self.name):
                reply = f"{self.name} here, sir. {reply.lstrip()}"
        else:
            reply = _strip_identification(reply, self.name)
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        # Stage 3: persist both sides so this survives a restart, and so ARIA can
        # later answer "what did Bobby and I decide?". RAM history stays as-is for
        # the live session; this is the durable record.
        conv_memory.record_turn(self.session_id, "user", user_text)
        conv_memory.record_turn(self.session_id, self.agent, reply)
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
                # Cap the channel dump (keeps newest, since texts are newest-first).
                # Conversation mode uses its OWN tighter cap: measured, the shared
                # 2000-token cap pushed this prompt past num_ctx and it silently
                # truncated. Only bounds conversational grounding — the scheduled
                # cycles read live stats and are unaffected.
                activity = cap_context("\n\n".join(texts),
                                       max_tokens=config.CONVERSATION_CONTEXT_TOKEN_CAP)
                parts.append("Recent activity in your work channel (newest first):\n\n"
                             + activity)
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
