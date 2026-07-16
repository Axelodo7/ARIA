"""Capability boundary: a web/browser/internet request is DECLINED, never answered
by dumping a random channel.

The failure this closes: "pull up the air quality index in New Jersey in a separate
tab" — a request for web access ARIA doesn't have — made the model reach for the
nearest tool (read_channel) and read back an unrelated message, "The most recent
from You: [via ARIA] Let's schedule a call with Eva." Same class as the
pricing->remember and pricing->approvals misroutes: when the model can't do what
was asked, it grabs whatever tool it has.

Two guards:
  * a web/browser/internet/tab/search request hits a deterministic capability
    decline BEFORE the model — so it can never substitute a channel read,
  * the model may run read_channel ONLY when the text refers to a channel or a
    teammate; any other off-topic question can no longer trigger a channel dump.

Gates:
  * the web matcher catches the class (browser, tab, internet, web search, URL) and
    leaves business questions alone,
  * the reported request declines, deterministically, with no tool call (repeated),
  * a NON-web off-topic question ("capital of France") does not dump a channel
    (repeated — the misfire was stochastic),
  * genuine channel/status queries still reach read_channel,
  * "open #ads in a new tab" declines (can't open a tab) rather than reading #ads.

Isolated temp DB, DRY_RUN forced. The matcher checks need no model; the routing
checks need Ollama + gemma3 and read #ads/#support (read-only).

    python scripts/test_web_capability.py
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

TMP_DB = os.path.join(tempfile.gettempdir(), f"test_webcap_{os.getpid()}.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
store.DB_PATH = TMP_DB

from brain.aria_agent import (Aria, _parse_web_request, _has_channel_intent,
                              _CAPABILITY_DECLINE)
from brain.hermes_common import OLLAMA_URL

REPEATS = 3
THE_REPORTED = "pull up the air quality index in New Jersey in a separate tab"

WEB_REQUESTS = [
    THE_REPORTED,
    "open a browser and search for competitor pricing",
    "search the web for AQI data",
    "google the weather in Newark",
    "look that up online for me",
    "go to zentraiq.com and check the homepage",
    "open a new tab",
]
NOT_WEB = [                       # legit business / team queries — must NOT decline
    "what are the pricing tiers",
    "what's in #ads",
    "what did Bobby find today",
    "how is the Scale tier different from Launch",
    "remember we signed our first client",
    "what's pending",
]
OFF_TOPIC_NON_WEB = [             # unfulfillable, but NOT web — must not dump a channel
    "what is the capital of France",
    "what's twelve times eight",
]


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def preflight():
    try:
        requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=5).raise_for_status()
        return True
    except requests.exceptions.RequestException:
        print("[x] Ollama not reachable — model routing checks skipped.")
        return False


def main():
    print("=" * 74)
    print("Capability boundary — web requests decline; no channel dump on off-topic")
    print("=" * 74)

    # --- 1) the matchers (deterministic, no model) --------------------------
    print("\n--- 1) web-request matcher ---")
    m1 = chk("every web/browser/internet request matches",
             all(_parse_web_request(q) for q in WEB_REQUESTS))
    miss = [q for q in NOT_WEB if _parse_web_request(q)]
    for q in miss:
        print(f"    OVER-MATCHED: {q!r}")
    m2 = chk("no business/team query is mistaken for a web request", not miss)

    print("\n--- 2) channel-intent gate (governs model read_channel) ---")
    has = ["what's in #ads", "what did Bobby post", "the latest from the content channel",
           "anything Sarah dropped in feedback"]
    hasnt = ["what is the capital of France", THE_REPORTED, "what's twelve times eight"]
    i1 = chk("genuine channel/teammate references have channel intent",
             all(_has_channel_intent(q) for q in has))
    i2 = chk("off-topic questions do NOT have channel intent",
             not any(_has_channel_intent(q) for q in hasnt))

    if not preflight():
        print("\n(matcher checks passed; run with Ollama up for the routing checks.)")
        return all([m1, m2, i1, i2])

    aria = Aria()

    # --- 3) the reported request declines, no tool (repeated) ---------------
    print(f"\n--- 3) the reported web request x{REPEATS} ---")
    declined, tooled = 0, []
    for _ in range(REPEATS):
        r = aria.think(THE_REPORTED)
        if r["reply"] == _CAPABILITY_DECLINE:
            declined += 1
        if r["tool_calls"]:
            tooled.append([t.get("name") for t in r["tool_calls"]])
        print(f"    tools={[t.get('name') for t in r['tool_calls']]}  {r['reply'][:64]!r}")
    w1 = chk(f"declines every time ({REPEATS}/{REPEATS})", declined == REPEATS)
    w2 = chk("never calls a tool (no channel read)", not tooled)

    # --- 4) a NON-web off-topic question must not dump a channel -------------
    print(f"\n--- 4) off-topic non-web x{REPEATS} (must not dump a channel) ---")
    dumped = []
    for q in OFF_TOPIC_NON_WEB:
        for _ in range(REPEATS):
            r = aria.think(q)
            names = [t.get("name") for t in r["tool_calls"]]
            if "read_channel" in names or "most recent from" in r["reply"].lower():
                dumped.append((q, r["reply"][:60]))
        print(f"    {q!r} -> {r['reply'][:60]!r}")
    for q, s in dumped:
        print(f"    CHANNEL DUMP: {q!r} -> {s}")
    o1 = chk(f"no off-topic question dumped a channel ({len(OFF_TOPIC_NON_WEB)*REPEATS} runs)",
             not dumped)

    # --- 5) genuine channel/status queries still work -----------------------
    print("\n--- 5) legit channel/status reads still route to read_channel ---")
    r_ch = aria.think("what's in #ads")
    r_st = aria.think("what did Bobby find today")
    l1 = chk("'what's in #ads' still reads the channel",
             any(t.get("name") == "read_channel" for t in r_ch["tool_calls"]))
    l2 = chk("'what did Bobby find' still relays his status",
             any(t.get("name") == "read_channel" for t in r_st["tool_calls"]))
    print(f"    #ads   -> {r_ch['reply'][:60]!r}")
    print(f"    Bobby  -> {r_st['reply'][:60]!r}")

    # --- 6) web precedence: "open #ads in a new tab" declines ---------------
    print("\n--- 6) 'open #ads in a new tab' -> decline (can't open a tab) ---")
    r_mix = aria.think("open the ads channel in a new tab")
    x1 = chk("a channel-in-a-tab request declines, not reads",
             r_mix["reply"] == _CAPABILITY_DECLINE and not r_mix["tool_calls"])
    print(f"    -> {r_mix['reply'][:64]!r}")

    all_ok = all([m1, m2, i1, i2, w1, w2, o1, l1, l2, x1])
    print("\n" + "=" * 74)
    print("ALL PASS — web requests decline; off-topic never dumps a channel; real "
          "channel reads still work." if all_ok else "SOME CHECKS FAILED.")
    print("=" * 74)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
