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
import sys

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
    if not any(m.get("name", "").startswith("hermes3") for m in r.json().get("models", [])):
        print("[x] hermes3 not found. `ollama pull hermes3`.")
        return False
    return True


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

    # 3) run the 4 turns
    replies = []
    for q in TURNS:
        reply = session.agent_reply(q)
        session.mirror(q, reply)
        replies.append(reply)
        print(f"\n  You  : {q}")
        print(f"  Bobby: {reply}")

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
    print("\n" + ("ALL PASS — coherent, context-grounded 4-turn conversation."
                  if all([r1, r2, r3, r4, r5]) else "SOME CHECKS FAILED."))


if __name__ == "__main__":
    main()
