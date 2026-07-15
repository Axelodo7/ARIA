"""Structured relays must be remembered by REFERENCE, not stored verbatim.

The failure this closes: ARIA answered "what are the pricing tiers?" with the
pending-approvals list. Not a router bug and not a tool misfire — the raw relay
("You have 7 pending: ...") had been stored in conversation memory as ARIA's
answer, and gemma3 then reused that pattern for unrelated questions. Measured: 3/3
misroute with the raw dump in memory, 0/3 without it, and an explicit "never reuse
a previous answer" instruction did NOT help. Each repeat got recorded too, so it
reinforced itself.

Fix: think() returns `memory_note` for relay replies; callers persist
`memory_note or reply`. The user still HEARS the whole list — only the stored trace
changes.

Gates:
  * a relay reply carries a memory_note, and it contains none of the raw data,
  * what lands in conversation memory is the reference, not the dump,
  * with relays recorded that way, a pricing question does NOT parrot (repeated,
    since the original failure was a context-pattern effect),
  * continuity survives: "what did we just do?" can still see the approvals check,
  * a normal (non-relay) reply is still stored verbatim.

Isolated: temp DB. DRY_RUN forced. Reads #approvals/#ads (read-only). Needs Ollama.

    python scripts/test_relay_not_stored.py
"""
import os
import sys
import tempfile

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
from memory import store

TMP_DB = os.path.join(tempfile.gettempdir(), f"test_relay_{os.getpid()}.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
store.DB_PATH = TMP_DB

from brain import conv_memory
from brain.aria_agent import Aria
from brain.hermes_common import OLLAMA_URL

REPEATS = 3
SESSION = "test-relay"
PRICE_TOKENS = ("8k", "$8", "15k", "$15", "23k", "$23", "225")


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def preflight():
    try:
        requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=5).raise_for_status()
        return True
    except requests.exceptions.RequestException:
        print("[x] Ollama not reachable. Start `ollama serve`.")
        return False


def turn(aria, text):
    """One turn, persisted exactly as main.py does it."""
    r = aria.think(text)
    conv_memory.record_turn(SESSION, "user", text, compress=False)
    conv_memory.record_turn(SESSION, "aria", r.get("memory_note") or r["reply"],
                            compress=False)
    return r


def main():
    print("=" * 74)
    print("Structured relays are remembered by reference, not stored verbatim")
    print("=" * 74)
    if not preflight():
        return
    aria = Aria()

    # --- 1) the relay reply carries a data-free note -------------------------
    print("\n--- 1) ask for the approvals list (a structured relay) ---")
    r = turn(aria, "what's pending")
    print(f"  spoken reply : {r['reply'][:100]}")
    print(f"  memory_note  : {r.get('memory_note')!r}")
    note = r.get("memory_note") or ""
    n1 = chk("a relay reply carries a memory_note", bool(note))
    n2 = chk("the note contains none of the raw data (no 'pending:', no creative ids)",
             "pending:" not in note.lower() and "cr_" not in note.lower())
    n3 = chk("the user still HEARS the full list (reply unchanged)",
             "pending" in r["reply"].lower())

    # --- 2) what actually landed in memory -----------------------------------
    stored = store.get_recent_conversation_turns(2)
    aria_row = next((t for t in stored if t["speaker"] == "aria"), None)
    print(f"\n--- 2) stored in conversation_memory ---\n  {aria_row['content']!r}")
    s1 = chk("memory holds the REFERENCE, not the dump",
             aria_row["content"] == note)
    s2 = chk("the raw approvals data is NOT in conversation memory",
             "pending:" not in aria_row["content"].lower())

    # --- 3) a status relay too ------------------------------------------------
    r2 = turn(aria, "what did Bobby find today?")
    print(f"\n--- 3) status relay ---\n  memory_note : {r2.get('memory_note')!r}")
    t1 = chk("a status readout is also stored by reference",
             bool(r2.get("memory_note")) and "cr_" not in (r2.get("memory_note") or ""))

    # --- 3b) continuity, checked while the relays are still in the verbatim
    #         window (RECENT_TURNS=6 — the pricing turns below would push them out)
    blk = conv_memory.conversation_block()
    print(f"\n--- 3b) continuity ---")
    c1 = chk("memory still shows the approvals check happened",
             "approvals" in blk.lower())
    c2 = chk("...and shows Bobby's status was relayed", "bobby" in blk.lower())
    c3 = chk("but the block carries no raw dump", "pending:" not in blk.lower())

    # --- 4) THE POINT: no parroting into a pricing question -------------------
    print(f"\n--- 4) now ask about pricing x{REPEATS} (relays are in memory) ---")
    bad, nopr = [], []
    for _ in range(REPEATS):
        r3 = turn(aria, "What are the pricing tiers of Zentra IQ?")
        rep = r3["reply"].lower()
        if "pending" in rep:
            bad.append(r3["reply"][:80])
        elif not any(p in rep for p in PRICE_TOKENS):
            nopr.append(r3["reply"][:80])
        print(f"    {r3['reply'][:92]}")
    p1 = chk(f"pricing never parrots the approvals list ({REPEATS} runs)", not bad)
    p2 = chk("pricing answers come from docs/user.md", not nopr)
    for b in bad:
        print(f"    PARROTED: {b}")

    # --- 5) still no raw dump anywhere in the block ---------------------------
    blk2 = conv_memory.conversation_block()
    print("\n--- 5) the live block after all of that ---")
    c4 = chk("no raw approvals dump anywhere in the block",
             "pending:" not in blk2.lower())

    # --- 6) normal replies are still stored verbatim --------------------------
    r4 = turn(aria, "what are the pricing tiers")
    last = store.get_recent_conversation_turns(1)[0]
    v1 = chk("a NON-relay reply is still stored verbatim (no note)",
             r4.get("memory_note") is None and last["content"] == r4["reply"])

    all_ok = all([n1, n2, n3, s1, s2, t1, c1, c2, c3, p1, p2, c4, v1])
    print("\n" + "=" * 74)
    print("ALL PASS — relays remembered by reference; no template left to parrot."
          if all_ok else "SOME CHECKS FAILED.")
    print("=" * 74)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass
    return all_ok


if __name__ == "__main__":
    main()
