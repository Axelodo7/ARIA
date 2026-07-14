"""Stage-fix test — Ava's four content pillars must produce DISTINCT content.

Runs 4 cycles so every pillar fires (Contrarian -> Method -> Pricing anchor ->
Build-in-public), prints the chosen hook for each, and gates on:
  * all four pillars fired,
  * four genuinely different core messages (low pairwise word overlap),
  * no banned filler phrases in any chosen hook,
  * the agency price/speed comparison appears at most once — and only in Contrarian.

Isolated: temp DB (fresh pillar rotation, clean memory), Pollinations disabled
(no network/render — the cycle stops after the hook is chosen but still returns
it), DRY_RUN on (no Slack posts). Needs Ollama + gemma3.

    python scripts/test_content_pillars.py
"""
import os
import re
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

# --- Isolate: fresh temp DB + no network render ------------------------------
TMP_DB = os.path.join(tempfile.gettempdir(), f"test_pillars_{os.getpid()}.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
store.DB_PATH = TMP_DB
config.USE_POLLINATIONS = False   # cycle stops after choosing the hook; no HTTP/render

from brain.hermes_common import OLLAMA_URL
from agents.ava import (Ava, CONTENT_PILLARS, _contains_filler,
                        _contains_agency_markers, _BANNED_FILLER)

# Content-word overlap: two hooks that share most non-trivial words are the same
# core message. Stopwords + the ever-present domain terms are dropped so the
# comparison is on the DISTINCTIVE words.
_STOP = {"we", "our", "in", "the", "a", "for", "and", "to", "of", "it", "is",
         "with", "you", "your", "that", "on", "at", "by", "an", "are", "be",
         "this", "no", "not", "do"}


def _content_words(h):
    return {w for w in re.findall(r"[a-z0-9$%]+", (h or "").lower()) if w not in _STOP}


def _jaccard(a, b):
    A, B = _content_words(a), _content_words(b)
    return len(A & B) / len(A | B) if (A and B) else 0.0


def preflight(model):
    try:
        r = requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=5)
        r.raise_for_status()
    except requests.exceptions.RequestException:
        print("[x] Ollama not reachable. Start `ollama serve`.")
        return False
    names = [m.get("name", "") for m in r.json().get("models", [])]
    base = model.split(":")[0]
    if not any(n == model or n.startswith(base) for n in names):
        print(f"[x] model {model!r} not found. `ollama pull {model}`.")
        return False
    return True


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def main():
    print("=" * 72)
    print("Ava content-pillar distinctness — 4-cycle test (isolated temp DB)")
    print("=" * 72)
    if not preflight(Ava.model):
        return

    ava = Ava()
    results = []
    for n in range(len(CONTENT_PILLARS)):
        r = ava.daily()
        results.append((r["pillar"], r["chosen_hook"]))
        print(f"\n  cycle {n + 1}: [{r['pillar']}]")
        print(f"    chosen hook: {r['chosen_hook']!r}")

    print("\n" + "=" * 72)
    print("GATE")
    print("=" * 72)
    pillars = [p for p, _ in results]
    hooks = [h for _, h in results]

    g1 = chk(f"all four pillars fired {sorted(set(pillars))}",
             set(pillars) == {p["name"] for p in CONTENT_PILLARS})

    filler_hits = [(p, h, _contains_filler(h)) for p, h in results if _contains_filler(h)]
    for p, h, f in filler_hits:
        print(f"    filler in [{p}]: {f} -> {h!r}")
    g2 = chk("no banned filler phrases in any chosen hook", not filler_hits)

    agency = [(p, h) for p, h in results if _contains_agency_markers(h)]
    for p, h in agency:
        print(f"    agency-comparison in [{p}]: {h!r}")
    g3 = chk("agency comparison appears at most once, only in Contrarian",
             len(agency) <= 1 and (not agency or "contrarian" in agency[0][0].lower()))

    worst = (0.0, None)
    for i in range(len(hooks)):
        for j in range(i + 1, len(hooks)):
            jac = _jaccard(hooks[i], hooks[j])
            if jac > worst[0]:
                worst = (jac, (pillars[i], pillars[j]))
    print(f"    highest pairwise word-overlap: {worst[0]:.2f} between {worst[1]}")
    g4 = chk("four genuinely different core messages (max pairwise overlap < 0.5)",
             worst[0] < 0.5)

    all_ok = all([g1, g2, g3, g4])
    print("\n" + ("ALL PASS — four distinct, on-pillar hooks; no filler; agency "
                  "comparison contained to Contrarian." if all_ok
                  else "SOME CHECKS FAILED."))
    try:
        os.remove(TMP_DB)
    except OSError:
        pass
    return all_ok


if __name__ == "__main__":
    main()
