"""ARIA Level 1 web search — real results, narrated, never fabricated.

ARIA can now answer questions that need current/external info by searching (Tavily)
and narrating REAL results. The hard requirements, all tested here:

  * a weather/AQI query returns the fetched value and gemma3 narrates it — with no
    number the results don't contain (Principle 1: Python extracts, gemma3 narrates,
    a numeric guard blocks fabrication, a grounded fallback catches anything it
    can't verify),
  * search failure / no network / empty results -> "I couldn't find that, sir",
    never an invented answer,
  * a pure browser request ("open a new tab") still declines — search can't do that,
  * prompt_eval_count with a search result loaded stays well under num_ctx (6144),
    and the grounding block is hard-capped with the TOKEN-aware estimator,
  * existing routing is intact: pricing -> business answer, "what's in #ads" ->
    channel read, dispatch -> handoff, off-topic -> no channel dump.

Tavily is STUBBED (web_search._fetch), so no real API key is needed and results are
deterministic. Isolated temp DB, DRY_RUN forced. Narration + budget + regression
checks need Ollama + gemma3.

    python scripts/test_web_search.py
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

TMP_DB = os.path.join(tempfile.gettempdir(), f"test_websearch_{os.getpid()}.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
store.DB_PATH = TMP_DB

from brain.aria_agent import (Aria, _parse_web_search, _parse_browser_control,
                              _BROWSER_DECLINE)
from brain.hermes_common import OLLAMA_URL
from integrations import web_search as WS

REPEATS = 3

# Canned Tavily response — AQI 42, the ONLY salient number, so any other number a
# narration asserts is provably invented.
CANNED_AQI = {
    "answer": "The air quality index in Newark, New Jersey is 42 right now, which is good.",
    "results": [
        {"content": "Newark, New Jersey air quality index is 42 today, considered good."},
        {"content": "Air quality in Newark is good, with low pollen."},
    ],
}
# A deliberately LONG response to exercise the token cap.
CANNED_BIG = {
    "answer": "Summary. " + ("The market moved on macro news. " * 40),
    "results": [{"content": ("Detailed analysis paragraph. " * 60)},
                {"content": ("More context and background. " * 60)},
                {"content": ("Even more supporting detail. " * 60)}],
}


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def preflight():
    try:
        requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=5).raise_for_status()
        return True
    except requests.exceptions.RequestException:
        print("[x] Ollama not reachable — narration/budget/regression checks skipped.")
        return False


def measure_prompt_eval(messages):
    r = requests.post(OLLAMA_URL, json={
        "model": config.AGENT_MODELS["ARIA"], "messages": messages, "stream": False,
        "options": {"temperature": 0.0, "num_predict": 1, "num_ctx": config.NUM_CTX},
    }, timeout=180)
    return r.json().get("prompt_eval_count")


def main():
    print("=" * 74)
    print("ARIA Level 1 web search — real, narrated, never fabricated")
    print("=" * 74)

    # --- 1) routing (deterministic, no model) -------------------------------
    print("\n--- 1) web-search intent routing (no collision with team routes) ---")
    searches = ["what's the weather in Newark", "look up the price of Bitcoin",
                "pull up the air quality index in New Jersey in a separate tab",
                "what's happening with the stock market today"]
    not_searches = ["what are the pricing tiers", "what's in #ads",
                    "what did Bobby find today", "ask Bobby to scale cr_A",
                    "what's pending", "what's the latest from Bobby"]
    r1 = chk("web questions produce a search query",
             all(_parse_web_search(q) for q in searches))
    collided = [q for q in not_searches if _parse_web_search(q)]
    for q in collided:
        print(f"    COLLISION: {q!r} -> {_parse_web_search(q)!r}")
    r2 = chk("no team/business query is hijacked as a web search", not collided)
    r3 = chk("pure browser control is detected", _parse_browser_control("open a new tab"))

    # --- 2) Python value extraction (Principle 1) ---------------------------
    print("\n--- 2) Python extracts the specific value ---")
    e1 = chk("AQI", WS._extract_value("today's AQI is 42") == "the air quality index is 42")
    e2 = chk("temperature", "72" in (WS._extract_value("it is 72 degrees F") or ""))
    e3 = chk("price", "$1,299" in (WS._extract_value("priced at $1,299.99") or ""))

    # --- 3) fabrication guard (Principle 1, point 5) ------------------------
    print("\n--- 3) numeric fabrication guard ---")
    g = "Key fact: the air quality index is 42. Answer: AQI in Newark is 42."
    f1 = chk("a digit that IS in the grounding is fine",
             not WS.narration_fabricates("The AQI is 42, sir", g))
    f2 = chk("the same value as a WORD is fine ('forty-two' <- 42)",
             not WS.narration_fabricates("It's forty-two, sir", g))
    f3 = chk("an invented value is caught",
             WS.narration_fabricates("The AQI is 55, sir", g))
    f4 = chk("an extra invented number is caught (model-added 'fact')",
             WS.narration_fabricates("AQI 42, and PM2.5 is 12", g))
    f5 = chk("non-numeric narration passes", not WS.narration_fabricates("It's good, sir", g))

    # --- 4) search() normalization + HARD token cap -------------------------
    print("\n--- 4) search() normalizes and caps the grounding ---")
    WS._fetch = lambda q: CANNED_AQI
    res = WS.search("air quality in New Jersey")
    s1 = chk("a normal result has grounding + extracted value",
             res and res["value"] == "the air quality index is 42" and "42" in res["grounding"])
    WS._fetch = lambda q: CANNED_BIG
    big = WS.search("stock market")
    toks = WS.grounding_tokens(big)
    print(f"    big-result grounding = {toks} tokens (cap {WS.SEARCH_RESULT_TOKEN_CAP})")
    s2 = chk("a huge result is capped to the token budget",
             toks <= WS.SEARCH_RESULT_TOKEN_CAP)
    WS._fetch = lambda q: {"answer": "", "results": []}
    s3 = chk("empty results -> None (caller will decline)", WS.search("x") is None)
    WS._fetch = lambda q: None
    s4 = chk("a fetch failure -> None (network down / no key)", WS.search("x") is None)

    matcher_ok = all([r1, r2, r3, e1, e2, e3, f1, f2, f3, f4, f5, s1, s2, s3, s4])

    if not preflight():
        print("\n(deterministic checks passed; run with Ollama for the rest.)")
        return matcher_ok

    aria = Aria()

    # --- 5) end to end: a weather/AQI query, narrated, NOT fabricated -------
    print(f"\n--- 5) the reported AQI query x{REPEATS} (stubbed AQI=42) ---")
    WS._fetch = lambda q: CANNED_AQI
    grounding = WS.search("the air quality index in New Jersey")["grounding"]
    bad_val, no_val, fabricated = 0, 0, 0
    for _ in range(REPEATS):
        r = aria.think("pull up the air quality index in New Jersey in a separate tab")
        spoken = r["reply"]
        vals = WS._numeric_values(spoken)
        if 42.0 not in vals:
            no_val += 1
        if WS.narration_fabricates(spoken, grounding):
            fabricated += 1
        print(f"    {spoken[:96]!r}")
    n1 = chk(f"the fetched value (42) is spoken every time ({REPEATS}/{REPEATS})", no_val == 0)
    n2 = chk("no run asserts a number the results don't contain (no fabrication)",
             fabricated == 0)
    n3 = chk("the browser-tab part is declined, the lookup still done",
             "open browser tabs" in r["reply"] and "looked it up" in r["reply"])

    # --- 5b) search answers go through the speech cleaner -------------------
    print("\n--- 5b) a markup-laden result is cleaned for TTS ---")
    from brain.speech import clean_for_speech
    WS._fetch = lambda q: {
        "answer": "**Newark AQI** is _42_ today. See [details](https://x.com).",
        "results": [{"content": "AQI 42 in Newark, good."}]}
    r_md = aria.think("what's the air quality in Newark right now")
    cleaned = clean_for_speech(r_md["reply"])       # exactly what speak() applies
    sp1 = chk("no markdown/link characters reach the TTS input",
              not any(c in cleaned for c in "*_[]()") and "https" not in cleaned)
    print(f"    cleaned -> {cleaned[:80]!r}")
    WS._fetch = lambda q: CANNED_AQI

    # --- 6) graceful failure -> decline, never invent -----------------------
    print("\n--- 6) search failure -> 'couldn't find that' ---")
    WS._fetch = lambda q: None                     # network down / no key
    r_fail = aria.think("what's the weather in Newark right now")
    print(f"    {r_fail['reply']!r}")
    x1 = chk("a failed search declines cleanly", r_fail["reply"] == WS.COULDNT_FIND)
    x2 = chk("...with no invented number", not WS._numeric_values(r_fail["reply"]))
    WS._fetch = lambda q: CANNED_AQI               # restore for later checks

    # --- 7) pure browser control still declines -----------------------------
    print("\n--- 7) 'open a new tab' -> decline (search can't do that) ---")
    r_b = aria.think("open a new tab")
    b1 = chk("pure browser control declines, no search",
             r_b["reply"] == _BROWSER_DECLINE and
             not any(t.get("name") == "web_search" for t in r_b["tool_calls"]))
    print(f"    {r_b['reply']!r}")

    # --- 8) token budget: narration prompt stays under 6144 -----------------
    print("\n--- 8) prompt_eval_count with a search result loaded ---")
    WS._fetch = lambda q: CANNED_BIG               # the big (capped) result
    big_res = WS.search("stock market today")
    narration_msgs = [
        {"role": "system", "content":
            "You are ARIA reading a web search result out loud. Narrate ONLY the "
            "SEARCH RESULT.\n\nSEARCH RESULT:\n" + big_res["grounding"]},
        {"role": "user", "content": "what's happening with the stock market today"},
    ]
    pe = measure_prompt_eval(narration_msgs)
    print(f"    prompt_eval_count = {pe}  (num_ctx={config.NUM_CTX})")
    t1 = chk(f"narration prompt {pe} well under {config.NUM_CTX}",
             pe is not None and pe < config.NUM_CTX)
    t2 = chk("...with comfortable headroom (< 3000)", pe is not None and pe < 3000)
    WS._fetch = lambda q: CANNED_AQI

    # --- 9) NO REGRESSION on existing routing -------------------------------
    print("\n--- 9) existing routing unchanged ---")
    r_price = aria.think("what are the pricing tiers")
    reg1 = chk("pricing -> business answer, not a search or channel read",
               any(p in r_price["reply"].lower() for p in ("8k", "$8", "15k", "23k", "225"))
               and not any(t.get("name") in ("web_search", "read_channel")
                           for t in r_price["tool_calls"]))
    print(f"    pricing -> {r_price['reply'][:70]!r}")
    r_ads = aria.think("what's in #ads")
    reg2 = chk("'what's in #ads' -> channel read (not a search)",
               any(t.get("name") == "read_channel" for t in r_ads["tool_calls"]))
    r_disp = aria.think("ask Bobby to scale cr_A")
    reg3 = chk("dispatch -> handoff", r_disp.get("dispatched"))
    dumped = 0
    for _ in range(REPEATS):
        rr = aria.think("what is the capital of France")
        if any(t.get("name") in ("read_channel", "web_search") for t in rr["tool_calls"]):
            dumped += 1
    reg4 = chk("off-topic -> no channel dump and no spurious search", dumped == 0)
    print(f"    France -> {rr['reply'][:60]!r}")

    all_ok = all([matcher_ok, n1, n2, n3, sp1, x1, x2, b1, t1, t2,
                  reg1, reg2, reg3, reg4])
    print("\n" + "=" * 74)
    print("ALL PASS — search returns real results, narrates without fabricating, "
          "fails gracefully, fits the budget, and breaks no routing."
          if all_ok else "SOME CHECKS FAILED.")
    print("=" * 74)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
