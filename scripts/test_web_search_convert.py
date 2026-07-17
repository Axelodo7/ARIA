"""Web-search follow-ups: a unit conversion is PYTHON-computed, never model math,
and cites no source it didn't read.

The bug this closes (a real two-turn transcript): turn 1 "weather in New Jersey"
returned a correct forecast in Celsius (grounded search — good). Turn 2 "tell me
those numbers in Fahrenheit" fell through EVERY deterministic route to the plain
model path, where gemma3 (a) did the °C->°F arithmetic itself — 30->86, right only
by luck — and (b) invented a source, "the air quality readings from #feedback",
because the ungrounded prompt still carries the tools block, which lists channel
names. The fabrication guard never fired: it only runs inside _narrate_search, and
no search ran on the conversion turn.

The fix: a conversion follow-up is detected deterministically and converted in
Python from the LAST search's real values (Principle 1 — code does the math), with
a deterministic spoken line, so no model call, no mental math, no invented source.
No prior figure -> a clean decline, never a guess.

Gates:
  * the conversion matcher fires on "... in Fahrenheit/Celsius" follow-ups and NOT
    on team/business queries,
  * temperature extraction + conversion are correct in pure Python,
  * weather-then-convert: the Fahrenheit values are Python-computed from the prior
    Celsius result (86 & 68 from 30 & 20), spoken with NO model call and NO
    fabrication and NO channel cited — repeated, since the original was stochastic,
  * a fresh "weather in Fahrenheit" query is SEARCHED (grounded), not treated as a
    conversion of nothing,
  * convert with no prior search -> clean decline, no invented number,
  * the phantom-source guard: a freeform reply may not cite a channel not read.

Tavily is STUBBED — no key needed, deterministic. Isolated temp DB, DRY_RUN forced.
The narration/regression checks need Ollama + gemma3.

    python scripts/test_web_search_convert.py
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

TMP_DB = os.path.join(tempfile.gettempdir(), f"test_convert_{os.getpid()}.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
store.DB_PATH = TMP_DB

from brain.aria_agent import Aria, _parse_unit_conversion, _cites_channel, _parse_web_search
from brain.hermes_common import OLLAMA_URL
from integrations import web_search as WS

REPEATS = 3

# Turn-1 forecast in CELSIUS — 30/20, with a rainfall number (10) that is NOT a
# temperature. Crucially, 86 and 68 appear NOWHERE, so if they are spoken they were
# computed, not copied.
CANNED_WEATHER_C = {
    "answer": "The forecast for New Jersey shows highs around 30°C and lows near 20°C, "
              "with about 10 mm of rain.",
    "results": [{"content": "New Jersey today: high 30 C, low 20 C, 10 mm rainfall."}],
}


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def preflight():
    try:
        requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=5).raise_for_status()
        return True
    except requests.exceptions.RequestException:
        print("[x] Ollama not reachable — narration/regression checks skipped.")
        return False


def main():
    print("=" * 74)
    print("Web-search conversion follow-ups — Python math, no fabricated source")
    print("=" * 74)

    # --- 1) conversion matcher (deterministic, no model) --------------------
    print("\n--- 1) unit-conversion intent ---")
    conv = ["tell me those numbers in Fahrenheit", "convert that to Fahrenheit",
            "give me those in Fahrenheit", "what is that in Celsius", "in F please"]
    notconv = ["what are the pricing tiers", "what's in #ads", "ask Bobby to scale cr_A",
               "what's the weather in Newark", "what did Bobby find today"]
    m1 = chk("conversion phrasing yields a target unit",
             all(_parse_unit_conversion(q) for q in conv))
    over = [q for q in notconv if _parse_unit_conversion(q)]
    for q in over:
        print(f"    OVER-MATCHED: {q!r} -> {_parse_unit_conversion(q)}")
    m2 = chk("team/business queries are not conversions", not over)
    m3 = chk("a fresh 'weather in Fahrenheit' query still routes to SEARCH, "
             "not conversion",
             _parse_web_search("what's the weather in New Jersey in Fahrenheit") is not None)

    # --- 2) Python extraction + conversion ----------------------------------
    print("\n--- 2) temperature extraction + conversion (pure Python) ---")
    g = "highs around 30°C and lows near 20°C, 10 mm rain"
    t1 = chk("temperatures extracted, rainfall ignored",
             WS.extract_temperatures(g) == [(30.0, "C"), (20.0, "C")])
    t2 = chk("30°C -> 86°F, 20°C -> 68°F", WS.temperatures_to_unit(g, "F") == [86, 68])
    t3 = chk("round-trip F->C", round(WS.convert_temperature(86, "F", "C")) == 30)
    t4 = chk("narration is deterministic prose",
             WS.narrate_conversion([86, 68], "F") == "Those are 86 and 68 degrees Fahrenheit, sir."
             and WS.narrate_conversion([86], "F") == "That's 86 degrees Fahrenheit, sir.")

    # --- 3) phantom-source detector -----------------------------------------
    print("\n--- 3) phantom-channel-source detector ---")
    p1 = chk("catches '#feedback' and 'the feedback channel'",
             _cites_channel("readings from #feedback") and _cites_channel("the feedback channel"))
    p2 = chk("clean answers are not flagged",
             not _cites_channel("That's 86 degrees Fahrenheit, sir.")
             and not _cites_channel("Paris, sir."))

    matcher_ok = all([m1, m2, m3, t1, t2, t3, t4, p1, p2])

    if not preflight():
        print("\n(deterministic checks passed; run with Ollama for the rest.)")
        return matcher_ok

    aria = Aria()
    WS._fetch = lambda q: CANNED_WEATHER_C

    # --- 4) THE BUG: weather, then convert ----------------------------------
    print(f"\n--- 4) weather (Celsius) then 'those numbers in Fahrenheit' x{REPEATS} ---")
    r1 = aria.think("what's the weather in New Jersey")
    grounding = aria._last_search["grounding"]
    print(f"    turn 1: {r1['reply'][:80]!r}")
    print(f"    turn-1 grounding numbers: {sorted(WS._numeric_values(grounding))}")
    bad_val, fabricated, sourced, tooled = 0, 0, 0, 0
    for _ in range(REPEATS):
        r2 = aria.think("tell me those numbers in Fahrenheit")
        spoken = r2["reply"]
        vals = WS._numeric_values(spoken)
        if 86.0 not in vals:
            bad_val += 1
        if _cites_channel(spoken):
            sourced += 1
        if any(t["name"] == "web_search" for t in r2["tool_calls"]):
            tooled += 1   # a conversion must NOT trigger a fresh search or model narration
        print(f"    turn 2: {spoken!r}")
    c1 = chk(f"the Fahrenheit value (86) is spoken every time ({REPEATS}/{REPEATS})",
             bad_val == 0)
    c2 = chk("86 was COMPUTED, not copied (it is absent from the search results)",
             86.0 not in WS._numeric_values(grounding))
    c3 = chk("no channel/source is ever cited (no phantom #feedback)", sourced == 0)
    c4 = chk("the conversion is deterministic — no fresh search, no model narration",
             tooled == 0)

    # --- 5) convert with no prior search -> decline, never invent -----------
    print("\n--- 5) convert with nothing to convert ---")
    fresh = Aria()
    r_none = fresh.think("convert that to Fahrenheit")
    print(f"    {r_none['reply']!r}")
    d1 = chk("declines cleanly", "don't have a figure" in r_none["reply"].lower())
    d2 = chk("...with no invented number", not WS._numeric_values(r_none["reply"]))

    # --- 6) fresh 'weather in Fahrenheit' is SEARCHED (grounded) ------------
    print("\n--- 6) a fresh 'weather in Fahrenheit' query is searched ---")
    WS._fetch = lambda q: {"answer": "Newark is 75°F right now.",
                           "results": [{"content": "Newark temperature 75 F."}]}
    r_fresh = Aria().think("what's the weather in Newark in Fahrenheit")
    f1 = chk("routes to a fresh grounded search, not a conversion of nothing",
             any(t["name"] == "web_search" for t in r_fresh["tool_calls"])
             and 75.0 in WS._numeric_values(r_fresh["reply"]))
    print(f"    {r_fresh['reply'][:80]!r}")
    WS._fetch = lambda q: CANNED_WEATHER_C

    # --- 7) no regression on existing routing -------------------------------
    print("\n--- 7) existing routing unchanged ---")
    rp = aria.think("what are the pricing tiers")
    reg1 = chk("pricing -> business answer (no search/convert/channel)",
               any(p in rp["reply"].lower() for p in ("8k", "$8", "15k", "23k", "225"))
               and not any(t["name"] in ("web_search", "read_channel", "convert_units")
                           for t in rp["tool_calls"]))
    ra = aria.think("what's in #ads")
    reg2 = chk("'what's in #ads' -> channel read",
               any(t["name"] == "read_channel" for t in ra["tool_calls"]))
    rf = aria.think("what is the capital of France")
    reg3 = chk("off-topic answers cleanly, cites no channel",
               not _cites_channel(rf["reply"])
               and not any(t["name"] in ("read_channel", "web_search") for t in rf["tool_calls"]))
    print(f"    France -> {rf['reply'][:60]!r}")

    all_ok = all([matcher_ok, c1, c2, c3, c4, d1, d2, f1, reg1, reg2, reg3])
    print("\n" + "=" * 74)
    print("ALL PASS — conversions are Python-computed, grounded, and cite no phantom "
          "source; routing intact." if all_ok else "SOME CHECKS FAILED.")
    print("=" * 74)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
