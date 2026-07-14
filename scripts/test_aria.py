"""Step 6.1 smoke test for ARIA's brain — text only, no audio.

Sends utterances to aria_agent.think() and prints the tool calls she makes and
her final reply. This is the foundation check: the brain and tool routing must
work before any audio/HUD is layered on.

Run from the project root (add a quoted string to test one utterance):
    python scripts/test_aria.py
    python scripts/test_aria.py "ask Ava to make a video about dark mode"

Runs with DRY_RUN forced on, so dispatch posts nothing and fires nothing for
real (it logs what it would do); channel reads still hit real Slack (read-only).
Requires Ollama running with ARIA's model (gemma3).
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Force DRY_RUN before importing config-dependent modules.
os.environ["DRY_RUN"] = "True"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

import config
from brain.hermes_common import OLLAMA_URL
from brain.aria_agent import Aria, warmup

CASES = [
    ("a) Direct answer (no tool)", "what time is it?"),
    ("b) Dispatch to Sarah", "ask Sarah how the feedback looks"),
    ("c) Status read (Bobby)", "what did Bobby find today?"),
    ("d) Approvals query", "what's in approvals?"),
    ("e) Greeting (terseness)", "hey Aria"),
]


def preflight_ollama(model):
    tags_url = OLLAMA_URL.replace("/api/chat", "/api/tags")
    try:
        resp = requests.get(tags_url, timeout=5)
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        print(f"\n[x] Cannot reach Ollama at {tags_url.replace('/api/tags','')}. "
              "Start it with `ollama serve`.\n")
        return False
    models = [m.get("name", "") for m in resp.json().get("models", [])]
    if not any(n == model or n.startswith(model) for n in models):
        print(f"\n[!] Ollama up but `{model}` not found. `ollama pull {model}`.\n")
        return False
    print(f"  Ollama reachable; {model} present.")
    return True


def run_case(aria, label, text):
    print("\n" + "=" * 68)
    print(f"{label}\n  user: \"{text}\"")
    print("=" * 68)
    t0 = time.time()
    try:
        r = aria.think(text)
    except requests.exceptions.RequestException:
        print("  [x] Lost connection to Ollama mid-think.")
        return None
    elapsed = time.time() - t0
    mark = "OK" if elapsed < 10 else "SLOW"
    print(f"  latency: {elapsed:.1f}s  [{mark} vs <10s target]")
    if r["tool_calls"]:
        print("  tool calls:")
        for c in r["tool_calls"]:
            print(f"    - {c.get('name')}({c.get('arguments', {})})")
    else:
        print("  tool calls: (none — answered directly)")
    print(f"  dispatched: {r['dispatched']}")
    print(f"  ARIA reply: {r['reply']}")
    return elapsed


def main():
    print("=" * 68)
    print("ARIA brain smoke test (text-only)")
    print(f"  DRY_RUN: {config.DRY_RUN}  |  model: {Aria.model}")
    print("=" * 68)
    print("\n--- Checking Ollama ---")
    if not preflight_ollama(Aria.model):
        return

    print("  warming up the fast model...")
    warmup()  # so the first real case isn't a cold start

    aria = Aria()  # no hud_hook — text-only

    custom = " ".join(sys.argv[1:]).strip()
    cases = [("custom", custom)] if custom else CASES
    timings = []
    for label, text in cases:
        elapsed = run_case(aria, label, text)
        if elapsed is not None:
            timings.append((label, elapsed))

    # Case (f): "what did Bobby find today?" x3 — prove non-intermittency.
    if not custom:
        print("\n" + "#" * 68)
        print("CASE (f): 'what did Bobby find today?' x3 (non-intermittency)")
        print("#" * 68)
        f_ok = True
        for i in range(1, 4):
            r = aria.think("what did Bobby find today?")
            reply = r["reply"]
            has_cr = any(x in reply for x in ("cr_A", "cr_B", "cr_D"))
            no_fab = "sonic" not in reply.lower()
            ok = has_cr and no_fab
            f_ok = f_ok and ok
            print(f"\n  run {i}: [{'PASS' if ok else 'FAIL'}]  mentions cr_={has_cr}  no-fabrication={no_fab}")
            print(f"    reply: {reply[:180]}")
        print(f"\n  (f) overall: {'PASS — all 3 deterministic + real' if f_ok else 'FAIL'}")

    # --- Bug 2: capability boundary + garble clarification ---------------------
    if not custom:
        from brain.aria_agent import _looks_unintelligible
        print("\n" + "#" * 68)
        print("CAPABILITY BOUNDARY + GARBLE HANDLING")
        print("#" * 68)

        def chk(label, ok):
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
            return ok

        # (c) garbled input -> clarification, deterministic (no model call)
        garble = "brr uh"
        gr = aria.think(garble)
        print(f"\n  (c) think({garble!r}) -> {gr['reply']!r}")
        c_ok = (("catch" in gr["reply"].lower() or "say that again" in gr["reply"].lower())
                and not gr["dispatched"] and not gr["tool_calls"])
        c1 = chk("garbled input asks for clarification (no free-association)", c_ok)
        c2 = chk("_looks_unintelligible('brr uh') is True", _looks_unintelligible("brr uh"))
        c3 = chk("_looks_unintelligible('what did Bobby find today') is False",
                 not _looks_unintelligible("what did Bobby find today"))

        # (b) calendar query -> says no access, invents nothing (behavioral)
        cal = aria.think("what's on my calendar?")
        rl = cal["reply"].lower().replace("’", "'")  # normalize gemma3's curly apostrophes
        print(f"  (b) think('what's on my calendar?') -> {cal['reply']!r}")
        no_access = any(p in rl for p in ("don't have", "do not have", "no access",
                        "not connected", "can't access", "cannot access", "don't have access",
                        "no calendar"))
        invented = any(w in rl for w in ("3pm", "3 pm", "double-book", "design review",
                       "you have a meeting", "you're booked", "at 3"))
        c4 = chk("calendar query: mentions calendar and says no access", "calendar" in rl and no_access)
        c5 = chk("calendar query: invents no meetings/events", not invented)

        print(f"\n  Bug-2 section: {'PASS' if all([c1, c2, c3, c4, c5]) else 'SOME FAILED'}")

    print("\n" + "=" * 68)
    print("LATENCY SUMMARY (target <10s per turn)")
    print("=" * 68)
    for label, e in timings:
        print(f"  {'OK ' if e < 10 else 'SLOW'}  {e:5.1f}s  {label}")
    if timings:
        print(f"  ---- avg {sum(e for _, e in timings)/len(timings):.1f}s over {len(timings)} turns")
    print("=" * 68)


if __name__ == "__main__":
    main()
