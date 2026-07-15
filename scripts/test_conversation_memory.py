"""Stage 3 conversation-memory test.

  1. Write a conversation, simulate a restart (fresh load), confirm ARIA's prompt
     carries the prior context — including what an AGENT said, not just ARIA.
  2. A 30+ turn conversation still yields a block <=500 tokens (compression is
     working, not just appending).
  3. Combined with business memory (and agent memory on the agent path),
     prompt_eval_count stays under 5000 on every path.
  4. Conversations older than CONVERSATION_MEMORY_DAYS are pruned.

Isolated: temp DB + temp memory.md, DRY_RUN forced. Needs Ollama + gemma3.

    python scripts/test_conversation_memory.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ["DRY_RUN"] = "True"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

import config
import brain.hermes_common as HC
from memory import store

TMP_DB = os.path.join(tempfile.gettempdir(), f"test_convmem_{os.getpid()}.db")
TMP_MEM = os.path.join(tempfile.gettempdir(), f"test_convmem_{os.getpid()}.md")
for p in (TMP_DB, TMP_MEM):
    if os.path.exists(p):
        os.remove(p)
store.DB_PATH = TMP_DB
HC.BUSINESS_MEMORY_PATH = TMP_MEM
# business memory AT CAP, so the budget check is worst-case
with open(TMP_MEM, "w", encoding="utf-8") as f:
    f.write("# hdr\n\n" + "\n".join(
        f"- 2026-07-14: Fact number {i} about the ZentraIQ business milestone."
        for i in range(50)) + "\n")

from brain import conv_memory
from brain.hermes_common import (OLLAMA_URL, business_context_block, tools_block,
                                 estimate_tokens, cap_context)
from brain.aria_agent import load_aria_prompt, TOOLS
import brain.conversation as C


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def preflight():
    try:
        r = requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=5)
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException:
        print("[x] Ollama not reachable. Start `ollama serve`.")
        return False


def pe(msgs, model):
    m = [dict(x) for x in msgs]
    m[0]["content"] = f"[{uuid.uuid4()}]\n" + m[0]["content"]   # defeat prefix cache
    r = requests.post(OLLAMA_URL, json={
        "model": model, "messages": m, "stream": False,
        "options": {"temperature": 0.0, "num_predict": 1, "num_ctx": config.NUM_CTX},
    }, timeout=180)
    return r.json().get("prompt_eval_count")


def main():
    print("=" * 72)
    print("Stage 3 conversation memory (isolated temp DB + memory.md)")
    print("=" * 72)
    if not preflight():
        return

    # --- 1) survives a restart, and spans agents -----------------------------
    print("\n--- 1) write a conversation, then 'restart' (fresh load) ---")
    s1 = "aria-sessionA"
    conv_memory.record_turn(s1, "user", "We should push the pricing page live this week.", compress=False)
    conv_memory.record_turn(s1, "aria", "Noted, sir. I'd suggest Ava announce it once it's up.", compress=False)
    s2 = "bobby-sessionB"
    conv_memory.record_turn(s2, "user", "Bobby, should we pause the broad prospecting creative?", compress=False)
    conv_memory.record_turn(s2, "bobby", "Bobby here, sir. cr_B's ROAS fell to 1.38; I'd pause it.", compress=False)

    # simulate a restart: nothing in RAM, read purely from SQLite
    conv_memory._pruned_this_process = False
    block = conv_memory.conversation_block()
    print(block.strip()[:300])
    r1 = chk("block survives 'restart' (loaded from SQLite, not RAM)",
             "pricing page" in block)
    r2 = chk("block spans AGENTS too (Bobby's turn is recalled)",
             "cr_B" in block and "Bobby" in block)

    aria_prompt = (business_context_block() + load_aria_prompt()
                   + conv_memory.conversation_block() + tools_block(TOOLS))
    r3 = chk("ARIA's assembled prompt carries the prior context",
             "pricing page" in aria_prompt and "<conversation_memory>" in aria_prompt)

    # --- 2) 30+ turns -> block still <=500 tokens ----------------------------
    print("\n--- 2) 30+ turn conversation -> compression must hold the cap ---")
    for i in range(30):
        conv_memory.record_turn(s1, "user",
            f"Turn {i}: what is happening with the retargeting carousel budget and the booked-call cost this week?",
            compress=False)
        conv_memory.record_turn(s1, "aria",
            f"Turn {i}: Bobby reports cr_A at about fourteen dollars per booked call, sir, and has filed a proposal to scale it.",
            compress=False)
    total = len(store.get_recent_conversation_turns(999))
    conv_memory.compress_now(force=True)          # synchronous for determinism
    summary, upto = store.get_conversation_summary()
    block = conv_memory.conversation_block()
    tok = estimate_tokens(block)
    print(f"  turns stored: {total} | summary: {estimate_tokens(summary)} tok | block: {tok} tok")
    print(f"  summary: {summary[:160]}...")
    r4 = chk(f"block <=500 tokens with {total} turns stored (~{tok})", tok <= 500)
    r5 = chk(f"rolling summary <=150 tokens (~{estimate_tokens(summary)})",
             estimate_tokens(summary) <= 150)
    r6 = chk("summary is real compression (non-empty, covers older turns)",
             bool(summary) and upto > 0)
    r7 = chk("only the recent window is verbatim (older turns are NOT in the block)",
             "Turn 0:" not in block)

    # --- 3) budget: every path under 5000 ------------------------------------
    print("\n--- 3) prompt_eval_count with conversation memory live ---")
    a = pe([{"role": "system", "content": business_context_block() + load_aria_prompt()
             + conv_memory.conversation_block() + tools_block(TOOLS)},
            {"role": "system", "content": f"Current date: {datetime.now():%A %Y-%m-%d}."},
            {"role": "user", "content": "what did Bobby and I decide?"}],
           config.AGENT_MODELS["ARIA"])

    dense = ("*TL;DR:* cr_A landing booked calls at ~$14 each. CPA $19.72 -> $14.33 "
             "(-27%), ROAS 2.22 -> 3.19 (+44%), spend $42/day.\n\n") * 200
    cap = {}
    real = C.call_hermes
    def f(messages, **kw):
        cap["m"] = messages
        return "Bobby here, sir."
    C.call_hermes = f
    sess = C.ConversationSession("bobby", None)
    sess.context = cap_context(dense, max_tokens=config.CONVERSATION_CONTEXT_TOKEN_CAP)
    sess.agent_reply("what about cr_B?")
    C.call_hermes = real
    b = pe(cap["m"], config.CONVERSATION_SPEED_MODEL)

    print(f"  ARIA  (business mem cap + conversation memory) : {a}   headroom {6144-a}")
    print(f"  BOBBY (biz cap + #ads cap + conversation mem)  : {b}   headroom {6144-b}")
    r8 = chk(f"ARIA prompt_eval_count {a} < 5000", a < 5000)
    r9 = chk(f"BOBBY prompt_eval_count {b} < 5000", b < 5000)

    # --- 4) expiry -----------------------------------------------------------
    print(f"\n--- 4) pruning (CONVERSATION_MEMORY_DAYS={config.CONVERSATION_MEMORY_DAYS}) ---")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=config.CONVERSATION_MEMORY_DAYS + 3)).isoformat()
    import sqlite3
    conn = sqlite3.connect(store.DB_PATH)
    for i in range(5):
        conn.execute("INSERT INTO conversation_memory (timestamp, speaker, content, session_id) "
                     "VALUES (?,?,?,?)", (old_ts, "user", f"ancient turn {i}", "old-session"))
    conn.commit(); conn.close()
    before = len(store.get_recent_conversation_turns(999))
    removed = conv_memory.prune_old()
    after = len(store.get_recent_conversation_turns(999))
    print(f"  turns {before} -> {after} (pruned {removed})")
    r10 = chk("conversations older than the retention window are pruned", removed == 5)
    r11 = chk("recent conversations are NOT pruned", after == before - 5 and after > 0)

    all_ok = all([r1, r2, r3, r4, r5, r6, r7, r8, r9, r10, r11])
    print("\n" + "=" * 72)
    print("ALL PASS — conversation memory persists, compresses, fits the budget, "
          "and expires." if all_ok else "SOME CHECKS FAILED.")
    print("=" * 72)
    for p in (TMP_DB, TMP_MEM):
        try:
            os.remove(p)
        except OSError:
            pass
    return all_ok


if __name__ == "__main__":
    main()
