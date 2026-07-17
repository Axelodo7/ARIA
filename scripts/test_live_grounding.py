"""A specific live/external value (AQI, weather, temperature) is ALWAYS grounded in
a real search — never answered from the model's knowledge or from stale memory.

Three real failures this closes:
  T2  "what about South Brunswick" (a weather follow-up) matched no route, fell to
      the freeform model, which declined web access AND surfaced stale figures from
      conversation memory instead of searching.
  T3  a request for the AQI produced "the AQI is currently 100" with no search
      behind it — a fabricated number the guard never saw, because the fabrication
      guard only runs on the search-narration path.

Two fixes, both tested here:
  * follow-up routing: "what about X" right after a search searches again, carrying
    the prior topic ("NJ weather" + "what about South Brunswick" -> "South Brunswick
    weather"), so a follow-up is grounded exactly like the original;
  * the BACKSTOP (the hard guarantee): any current-reading claim (AQI/temperature/
    pollutant + a number) that reaches the freeform finalize WITHOUT a search is
    blocked — ARIA searches then, grounding the answer or declining, and NEVER
    speaks the model's guess. This holds even when the model fabricates.

Gates:
  * "air quality in X" -> a search query; a "what about Y" follow-up -> a search
    query carrying the topic; a business follow-up ("what about pricing") -> not a
    search,
  * the live-value detector blocks readings and spares pricing/math/counts,
  * air-quality query: a real (stubbed) search -> the fetched value; a failed search
    -> "I couldn't find that, sir", never a number,
  * a follow-up lookup routes to search (no "web access" decline, no stale value),
  * the backstop: even when the model fabricates "AQI 100", the spoken answer is the
    grounded value (or a clean decline) — never 100,
  * no routing regression (pricing / dispatch).

Tavily stubbed, backend pinned gemma3 (the logic is backend-agnostic and this keeps
it free + deterministic). Isolated temp DB, DRY_RUN forced. Needs Ollama for the
end-to-end freeform checks.

    python scripts/test_live_grounding.py
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ["DRY_RUN"] = "True"
os.environ["ARIA_GENERAL_MODEL"] = "gemma3"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

import config
from memory import store

TMP_DB = os.path.join(tempfile.gettempdir(), f"test_live_{os.getpid()}.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
store.DB_PATH = TMP_DB

from brain.aria_agent import (Aria, _parse_web_search, _followup_search_query,
                              _asserts_live_value)
from brain.hermes_common import OLLAMA_URL
from integrations import web_search as WS

AQI_42 = {"answer": "The air quality index in Newark is 42 right now, considered good.",
          "results": [{"content": "Newark AQI is 42 today, good."}]}
SB_WEATHER = {"answer": "South Brunswick is 25°C and sunny right now.",
              "results": [{"content": "South Brunswick: 25 C, clear."}]}


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def preflight():
    try:
        requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=5).raise_for_status()
        return True
    except requests.exceptions.RequestException:
        print("[x] Ollama not reachable — end-to-end checks skipped.")
        return False


def main():
    print("=" * 74)
    print("Live values are grounded in search — never guessed, never stale")
    print("=" * 74)

    # --- 1) routing (deterministic) -----------------------------------------
    print("\n--- 1) routing ---")
    r1 = chk("'air quality in Newark' -> a search query",
             _parse_web_search("air quality in Newark") is not None)
    r2 = chk("follow-up 'what about South Brunswick' carries the topic",
             _followup_search_query("what about South Brunswick", "NJ weather")
             == "South Brunswick weather")
    r3 = chk("a business follow-up ('what about the pricing') is NOT a search",
             _followup_search_query("what about the pricing", "NJ weather") is None)
    r4 = chk("no follow-up without a prior search",
             _followup_search_query("what about Newark", None) is None)

    # --- 2) the live-value backstop detector --------------------------------
    print("\n--- 2) live-value detector (blocks readings, spares the rest) ---")
    block = ["The AQI is currently 100.", "It's 86°F right now, sir.",
             "AQI was 78 ppm, temp 86°F", "Air quality index of 55.",
             "The temperature is 72 degrees fahrenheit."]
    allow = ["Paris, sir.", "The Scale tier is eight thousand pounds.",
             "Twelve times eight is 96.", "You have 6 pending, sir.",
             "A right angle has 90 degrees.", "I couldn't find that, sir."]
    d1 = chk("every current-reading claim is flagged", all(_asserts_live_value(t) for t in block))
    bad = [t for t in allow if _asserts_live_value(t)]
    for t in bad:
        print(f"    FALSE POSITIVE: {t!r}")
    d2 = chk("pricing / math / counts / declines are NOT flagged", not bad)

    matcher_ok = all([r1, r2, r3, r4, d1, d2])

    if not preflight():
        print("\n(deterministic checks passed; run with Ollama for the rest.)")
        return matcher_ok

    aria = Aria()

    # --- 3) air-quality query: grounded, or clean decline -------------------
    print("\n--- 3) 'air quality in Newark' ---")
    WS._fetch = lambda q: AQI_42
    r = aria.think("what's the air quality in Newark")
    print(f"    search ok -> {r['reply'][:80]!r}")
    a1 = chk("the fetched value (42) is spoken", 42.0 in WS._numeric_values(r["reply"]))
    WS._fetch = lambda q: None                      # search fails (network/no key)
    r = aria.think("what's the air quality index in Trenton")
    print(f"    search down -> {r['reply']!r}")
    a2 = chk("a failed search declines, never invents a number",
             r["reply"] == WS.COULDNT_FIND and not WS._numeric_values(r["reply"]))

    # --- 4) a weather follow-up routes to search (T2) -----------------------
    print("\n--- 4) weather, then 'what about South Brunswick' ---")
    WS._fetch = lambda q: {"answer": "Newark is 20°C.", "results": [{"content": "Newark 20 C."}]}
    aria.think("pull up Newark weather")            # sets the topic
    WS._fetch = lambda q: SB_WEATHER
    r = aria.think("what about South Brunswick")
    print(f"    {r['reply'][:80]!r}")
    f1 = chk("the follow-up SEARCHED (not a 'web access' decline)",
             any(t["name"] == "web_search" for t in r["tool_calls"]))
    f2 = chk("it reflects the fresh result, not stale memory",
             "25" in r["reply"] or "south brunswick" in r["reply"].lower())
    f3 = chk("no 'web access' decline", "web access" not in r["reply"].lower())

    # --- 5) THE BACKSTOP: even a fabricating model can't speak a live guess ---
    print("\n--- 5) backstop: model fabricates 'AQI 100' on the freeform path ---")
    real_call = aria._assistant_call
    aria._assistant_call = lambda *a, **k: "The AQI is currently 100, sir."
    try:
        WS._fetch = lambda q: AQI_42                # a real reading exists: 42
        r = aria.think("so how's the air out there today")   # reaches freeform
        print(f"    grounded  -> {r['reply'][:80]!r}")
        b1 = chk("the fabricated 100 is NOT spoken", 100.0 not in WS._numeric_values(r["reply"]))
        b2 = chk("the grounded value (42) is spoken instead",
                 42.0 in WS._numeric_values(r["reply"]))
        b3 = chk("the turn is recorded as a web_search (grounded, by reference)",
                 any(t["name"] == "web_search" for t in r["tool_calls"]))
        WS._fetch = lambda q: None                  # and if the search can't ground it
        r = aria.think("so how's the air out there right now")
        print(f"    no ground -> {r['reply']!r}")
        b4 = chk("with no result, it DECLINES — never the fabricated number",
                 r["reply"] == WS.COULDNT_FIND and 100.0 not in WS._numeric_values(r["reply"]))
    finally:
        aria._assistant_call = real_call

    # --- 6) no regression ---------------------------------------------------
    print("\n--- 6) routing unchanged ---")
    rp = aria.think("what are the pricing tiers")
    g1 = chk("pricing still answers (no spurious search/decline)",
             any(p in rp["reply"].lower() for p in ("8k", "$8", "15k", "23k", "225"))
             and not any(t["name"] == "web_search" for t in rp["tool_calls"]))
    rd = aria.think("ask Bobby to scale cr_A")
    g2 = chk("dispatch still works", rd.get("dispatched"))

    all_ok = all([matcher_ok, a1, a2, f1, f2, f3, b1, b2, b3, b4, g1, g2])
    print("\n" + "=" * 74)
    print("ALL PASS — live values are always grounded; a guess can never be spoken."
          if all_ok else "SOME CHECKS FAILED.")
    print("=" * 74)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
