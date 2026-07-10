"""Step 7A test — conversation mode, text-only (no audio).

Simulates opening a conversation with Bobby about the cr_B pause proposal,
running 4 turns, and exiting with "thanks". Verifies: the open intent is
detected, context is loaded from #ads, Bobby stays on-topic and references real
specifics (cr_B, ROAS numbers) across turns, the Slack mirror posts fire, and
the exit phrase ends it.

Run from the project root:
    python scripts/test_conversation_mode.py

Forces DRY_RUN=True so the #chat-bobby mirror posts are printed (that channel may
not exist yet); context reads from the real #ads channel. Needs Ollama + hermes3.
"""
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ["DRY_RUN"] = "True"   # mirror posts print; don't require #chat-bobby

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

import config
from brain.hermes_common import OLLAMA_URL
from brain import conversation

OPEN_PHRASE = "let me talk to Bobby about the cr_B pause proposal"
TURNS = [
    "Why are you recommending we pause cr_B?",
    "What has its ROAS been doing lately?",
    "How does that compare to the creatives you'd scale?",
    "So what happens to that daily budget if we pause it?",
]


def preflight():
    try:
        r = requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=5)
        r.raise_for_status()
    except requests.exceptions.RequestException:
        print("[x] Ollama not reachable. Start `ollama serve`.")
        return False
    names = [m.get("name", "") for m in r.json().get("models", [])]
    want = config.CONVERSATION_SPEED_MODEL
    # ollama tags may or may not carry a ":latest" tag; match on the base name.
    base = want.split(":")[0]
    if not any(n == want or n.startswith(base) for n in names):
        print(f"[x] conversation model {want!r} not found. `ollama pull {want}`.")
        return False
    return True


def _starts_with_id(reply, name):
    """True if `reply` opens with '<Name> here' (case-insensitive), ignoring
    leading quotes/whitespace."""
    return reply.lstrip(' "\'').lower().startswith(f"{name.lower()} here")


def _strip_id_opener(text, name):
    """Remove a leading '<Name> here[, sir].' identification line so the brevity
    count measures CONTENT sentences, not the (asked-for) opener."""
    return re.sub(rf"^\s*[\"']?{re.escape(name)} here(?:,?\s*sir)?[.!?]*\s*",
                  "", text, flags=re.IGNORECASE)


def _sentence_count(text):
    """Approximate spoken-sentence count: terminators . ! ? that are NOT decimal
    points (a digit on both sides, e.g. 2.03, doesn't end a sentence)."""
    trimmed = re.sub(r"(?<=\d)\.(?=\d)", "", text)  # drop decimal points
    return len(re.findall(r"[.!?]+", trimmed))


class _FakeSession:
    """Stand-in for ConversationSession — no Slack, no model. Lets the driver's
    retry/echo/exit policy be tested deterministically."""

    def __init__(self):
        self.agent, self.topic = "bobby", None
        self.opened = self.closed = False
        self.replies = []

    def open(self):
        self.opened = True
        return "(fake context)"

    def close(self):
        self.closed = True

    def agent_reply(self, text):
        reply = f"reply to {text}"
        self.replies.append(reply)
        return reply

    def mirror(self, user_text, agent_text):
        pass


def _scripted_capture(seq):
    """Return a capture_turn() that yields `seq` then a clean exit if exhausted."""
    it = iter(seq)

    def capture():
        try:
            return next(it)
        except StopIteration:
            return "thanks, that's all", 2.0
    return capture


def _drive_case(chk, label, seq, want_reason=None, want_reason_contains=None,
                want_spoken=(), want_not_spoken=(), want_replies=None):
    """Run run_conversation with a scripted capture + recording speak, then assert
    the reason, what ARIA spoke, and how many agent replies fired. Returns bool."""
    sess = _FakeSession()
    said = []
    reason = conversation.run_conversation(
        sess, _scripted_capture(seq), said.append,
        opening_line="Connecting you to Bobby.")
    ok = bool(reason) and sess.opened and sess.closed  # never a silent/half exit
    if want_reason is not None:
        ok = ok and reason == want_reason
    if want_reason_contains is not None:
        ok = ok and want_reason_contains in reason
    for s in want_spoken:
        ok = ok and s in said
    for s in want_not_spoken:
        ok = ok and s not in said
    if want_replies is not None:
        ok = ok and len(sess.replies) == want_replies
    return chk(f"{label}  [reason={reason!r}]", ok)


def main():
    print("=" * 64)
    print("Conversation mode — text-only test (Bobby)")
    print("=" * 64)
    if not preflight():
        return

    # 1) intent-open detection
    opened = conversation.detect_open(OPEN_PHRASE)
    print(f"\ndetect_open({OPEN_PHRASE!r}) -> {opened}")
    if not opened:
        print("[x] open intent not detected."); return
    agent, topic = opened

    # priority sanity: an explicit dispatch phrase must NOT open a conversation
    print("dispatch phrase stays dispatch (no open):",
          conversation.detect_open("ask Bobby to pause cr_B") is None)

    # 2) open session + load context (reads real #ads)
    session = conversation.ConversationSession(agent, topic)
    ctx = session.open()
    ctx_has_crb = "cr_b" in ctx.lower()
    ctx_has_roas = "roas" in ctx.lower()
    print(f"\ncontext loaded: {len(ctx)} chars | has cr_B={ctx_has_crb} | has ROAS={ctx_has_roas}")

    # 3) run the 4 turns (timed — conversation-mode latency is the whole point)
    print(f"\nconversation model: {session.model}  (num_predict=80)")
    replies, times = [], []
    for q in TURNS:
        t0 = time.perf_counter()
        reply = session.agent_reply(q)
        dt = time.perf_counter() - t0
        session.mirror(q, reply)
        replies.append(reply)
        times.append(dt)
        print(f"\n  You  : {q}")
        print(f"  Bobby: {reply}")
        print(f"  [gen {dt:.1f}s]")

    # 4) exit
    exited = conversation.is_exit("thanks")
    print(f"\nis_exit('thanks') -> {exited}")
    if exited:
        session.close()

    # --- verification -------------------------------------------------------
    joined = " ".join(replies).lower()
    mentions_crb = "cr_b" in joined or "creative b" in joined
    mentions_roas = ("roas" in joined) or any(n in joined for n in ("1.38", "2.03", "1.1", "2.3"))
    print("\n" + "=" * 64)
    print("VERIFICATION")
    print("=" * 64)

    def chk(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        return ok

    r1 = chk("open intent detected", bool(opened))
    r2 = chk("context loaded with cr_B + ROAS specifics", ctx_has_crb and ctx_has_roas)
    r3 = chk("Bobby references cr_B across turns", mentions_crb)
    r4 = chk("Bobby cites ROAS / real numbers (used the context)", mentions_roas)
    r5 = chk("exit phrase 'thanks' ended the session", exited)

    # --- Improvements: identification opener + short spoken replies ------------
    print("\n" + "=" * 64)
    print("IDENTIFICATION OPENER + SHORT SPOKEN REPLIES")
    print("=" * 64)
    print(f"  Bobby reply 1 opens: {replies[0][:40]!r}")
    print(f"  Bobby reply 2 opens: {replies[1][:40]!r}")
    i1 = chk("Bobby's FIRST reply begins with 'Bobby here'", _starts_with_id(replies[0], "Bobby"))
    i2 = chk("Bobby's SECOND reply does NOT re-identify",
             not _starts_with_id(replies[1], "Bobby"))
    counts = [_sentence_count(_strip_id_opener(r, "Bobby")) for r in replies]
    print(f"  Bobby content-sentence counts (ID opener + decimals excluded): {counts}")
    i3 = chk("all Bobby replies stay within 3 content sentences (<=3)", all(c <= 3 for c in counts))
    i4 = chk("no bullet lists in replies",
             not any(re.search(r"(?m)^\s*[-*•]\s", r) for r in replies))
    print(f"  generation time per reply: {[f'{t:.1f}s' for t in times]} "
          f"(avg {sum(times)/len(times):.1f}s) on {session.model}")

    # Sarah opener — a short second conversation (one turn is enough to check ID).
    sarah = conversation.ConversationSession("sarah", None)
    sarah.open()
    sarah_first = sarah.agent_reply("What's the top support theme right now?")
    sarah.close()
    print(f"\n  Sarah reply 1 opens: {sarah_first[:40]!r}")
    i5 = chk("Sarah's FIRST reply begins with 'Sarah here'", _starts_with_id(sarah_first, "Sarah"))

    # --- Fix 1 + Fix 2: mute config + silence-hallucination classification ----
    print("\n" + "=" * 64)
    print("UX FIXES — post-TTS mute + Whisper silence-hallucination rejection")
    print("=" * 64)
    print(f"  POST_TTS_MUTE_MS = {config.POST_TTS_MUTE_MS}")
    ct = conversation.classify_turn
    ish = conversation.is_silence_hallucination
    ie = conversation.is_exit
    f0 = chk("Fix 1: POST_TTS_MUTE_MS configured (>0)", config.POST_TTS_MUTE_MS > 0)
    # silence hallucinations — whole (normalized) transcript, any duration
    fa = chk("'You' @0.5s -> silence", ct("You", 0.5) == "silence")
    fA = chk("'You' @3s -> silence (rejected regardless of duration)", ct("You", 3.0) == "silence")
    fb = chk("bare 'thanks' @2s -> silence (hallucination precedes exit)", ct("thanks", 2.0) == "silence")
    fB = chk("'thanks for the update' -> NOT a hallucination", ish("thanks for the update") is False)

    # exit detection — whole (normalized) message only
    e1 = chk("[NEW] 'thanks for the update, but tell me about cr_A' @3s -> message",
             ct("thanks for the update, but tell me about cr_A", 3.0) == "message")
    e2 = chk("[NEW] 'thanks, that's all' @2s -> exit",
             ct("thanks, that's all", 2.0) == "exit")
    e3 = chk("[NEW] 'hmm let me think about goodbye scenarios' @3s -> message",
             ct("hmm let me think about goodbye scenarios", 3.0) == "message")
    e4 = chk("'goodbye' -> exit; 'Actually, goodbye is a weird word...' -> not exit",
             ie("goodbye") and not ie("Actually, goodbye is a weird word for you to use"))
    e5 = chk("normalization equalizes variants",
             conversation._normalize("Thanks, that's all.")
             == conversation._normalize("Thanks that's all")
             == conversation._normalize(" thanks , that's all "))
    e6 = chk("real question @3s -> message", ct("what about cr_A?", 3.0) == "message")

    # --- Fix (this pass): echo rejection + no-silent-exit retry policy ----------
    print("\n" + "=" * 64)
    print("ECHO REJECTION + NO-SILENT-EXIT RETRY POLICY")
    print("=" * 64)
    recent = ["Connecting you to Bobby."]
    g1 = chk("echo: 'connecting you to bobby' after ARIA said it -> echo",
             ct("connecting you to bobby", 2.0, recent) == "echo")
    g2 = chk("echo: is_echo True for the re-transcription",
             conversation.is_echo("connecting you to bobby", recent))
    g3 = chk("echo: a real question is NOT echo",
             not conversation.is_echo("why are you pausing creative B?", recent))
    g4 = chk("echo: no recent speech -> nothing is echo",
             ct("connecting you to bobby", 2.0, None) != "echo")

    # Driver-level cases (audio-free): scripted capture + recording speak.
    d1 = _drive_case(
        chk, "repeated hallucination: 3x 'You' then exit -> fallback prompt, NOT silent exit",
        seq=[("You", 3.0), ("You", 3.0), ("You", 3.0), ("thanks, that's all", 2.0)],
        want_reason="user exit phrase",
        want_spoken=[conversation.FALLBACK_PROMPT],
        want_not_spoken=[conversation.GIVE_UP_LINE],
        want_replies=0)

    d2 = _drive_case(
        chk, "echo storm: ARIA's own 'connecting you to bobby' -> rejected, agent never called",
        seq=[("connecting you to bobby", 2.0), ("thanks, that's all", 2.0)],
        want_reason="user exit phrase",
        want_spoken=[conversation.FALLBACK_PROMPT],
        want_replies=0)

    d3 = _drive_case(
        chk, "full fail: 4x 'You' -> GIVE_UP line spoken, reason names it (no silent exit)",
        seq=[("You", 3.0)] * 4,
        want_reason_contains="unintelligible",
        want_spoken=[conversation.GIVE_UP_LINE],
        want_not_spoken=[conversation.BACK_TO_STANDBY],
        want_replies=0)

    all_ok = all([r1, r2, r3, r4, r5, i1, i2, i3, i4, i5,
                  f0, fa, fA, fb, fB, e1, e2, e3, e4, e5, e6,
                  g1, g2, g3, g4, d1, d2, d3])
    print("\n" + ("ALL PASS — coherent conversation + tightened exit/silence filters "
                  "+ echo rejection + no silent exit."
                  if all_ok else "SOME CHECKS FAILED."))


if __name__ == "__main__":
    main()
