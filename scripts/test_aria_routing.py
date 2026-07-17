"""ARIA routing test — business questions must NOT land on the approvals reader.

The bug this guards: "What about the pricing tiers of ZentraIQ?" was answered with
the pending-approvals list. The router was innocent; the MODEL was picking the
check_pending_approvals tool, and it only did so when conversation memory was in
the prompt — so this test seeds conversation memory, otherwise it would pass
while the real bug walked straight through.

Gates:
  * pricing/cost questions -> freeform answer with REAL pricing from docs/user.md,
    never the approvals list,
  * genuine approvals questions -> still route to the approvals reader,
  * repeated, because the original misfire was stochastic (1/5 even after a
    partial fix) — a single green run would prove nothing.

Isolated: temp DB (conversation memory is seeded, the real DB is untouched).
DRY_RUN forced. Needs Ollama + gemma3. Reads #approvals (read-only).

    python scripts/test_aria_routing.py
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ["DRY_RUN"] = "True"
# Pin the general-assistant backend to gemma3: this test asserts the LOCAL
# model's behaviour / phrasing (or measures its prompt_eval), so it must not
# depend on whether ANTHROPIC_API_KEY is set. The Haiku path is covered by
# scripts/test_haiku_path.py.
os.environ["ARIA_GENERAL_MODEL"] = "gemma3"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

import config
from memory import store

TMP_DB = os.path.join(tempfile.gettempdir(), f"test_routing_{os.getpid()}.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
store.DB_PATH = TMP_DB

from brain.aria_agent import Aria, _parse_approvals_query
from brain.hermes_common import OLLAMA_URL

REPEATS = 3
PRICING_QS = ["what are the pricing tiers",
              "how much does Scale cost",
              "what's your pricing",
              "What about the pricing tiers of ZentraIQ?"]
APPROVAL_QS = ["what's in approvals", "what's pending", "anything to approve"]
# Real figures from docs/user.md — the answer must come from business context.
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


def main():
    print("=" * 72)
    print("ARIA routing — pricing questions must not hit the approvals reader")
    print("=" * 72)
    if not preflight():
        return

    # Seed conversation memory: the misfire ONLY reproduced with a prior exchange
    # in the prompt, so an empty-memory test would be a false green.
    store.add_conversation_turn("s", "user", "Tell me about the Praising Theatre of Central IQ.")
    store.add_conversation_turn("s", "aria",
        "I apologize, sir, but there appears to be a misunderstanding. There is no "
        "“Praising Theatre of Central IQ.” That is not a real entity. As you "
        "know, we are ZentraIQ, an AI-native MVP studio.")

    aria = Aria()

    # NOTE ON SCOPE — read before trusting a green run.
    # This asserts the ROUTER and the TOOL surface: with clean memory, a pricing
    # question must reach the freeform path and answer from docs/user.md.
    # It does NOT and CANNOT assert immunity to a POISONED memory. Measured: once a
    # bad "pricing -> approvals list" exchange is stored, gemma3 reproduces it 3/3
    # from context with no tool call at all, and an explicit "never reuse a previous
    # answer" instruction does not help (also 3/3). In-context examples beat
    # instructions on this model. The remedy is data, not code:
    # scripts/prune_conversation.py. Seeding the poison here would just assert a
    # permanent failure, so this test deliberately stops at the router.

    # --- pure matcher checks (no model) --------------------------------------
    print("\n--- the approvals matcher is narrow (deterministic) ---")
    m1 = chk("business questions do NOT match the approvals intent",
             not any(_parse_approvals_query(q) for q in PRICING_QS))
    m2 = chk("genuine approvals questions DO match",
             all(_parse_approvals_query(q) for q in APPROVAL_QS))

    # --- pricing questions ---------------------------------------------------
    print(f"\n--- pricing/cost questions x{REPEATS} (conversation memory seeded) ---")
    bad_route, no_price = [], []
    for q in PRICING_QS:
        for _ in range(REPEATS):
            r = aria.think(q)
            reply = r["reply"].lower()
            tools = [t.get("name") for t in r["tool_calls"]]
            if "check_pending_approvals" in tools or "pending" in reply:
                bad_route.append((q, r["reply"][:70]))
            elif not any(p in reply for p in PRICE_TOKENS):
                no_price.append((q, r["reply"][:70]))
        print(f"    {q!r} -> {r['reply'][:88]}")
    for q, s in bad_route:
        print(f"    MISROUTED: {q!r} -> {s}")
    for q, s in no_price:
        print(f"    NO PRICING: {q!r} -> {s}")
    p1 = chk(f"no pricing question hit the approvals list ({len(PRICING_QS)*REPEATS} runs)",
             not bad_route)
    p2 = chk("every pricing answer contains real pricing from docs/user.md", not no_price)

    # --- approvals questions still work --------------------------------------
    print("\n--- genuine approvals questions still route to the approvals reader ---")
    ok_appr = True
    for q in APPROVAL_QS:
        r = aria.think(q)
        routed = any(t.get("name") == "check_pending_approvals" for t in r["tool_calls"])
        ok_appr = ok_appr and routed
        print(f"    {q!r} -> {r['reply'][:70]}")
    a1 = chk("approvals questions reach the approvals reader", ok_appr)

    all_ok = all([m1, m2, p1, p2, a1])
    print("\n" + "=" * 72)
    print("ALL PASS — business questions answer from business context; approvals "
          "questions read #approvals." if all_ok else "SOME CHECKS FAILED.")
    print("=" * 72)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass
    return all_ok


if __name__ == "__main__":
    main()
