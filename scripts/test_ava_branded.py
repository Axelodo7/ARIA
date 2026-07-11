"""Stage 2 Part B test — Ava's branded content pipeline, 3 consecutive cycles.

Runs Ava's full daily() cycle 3 times to confirm:
  - the content pillar ROTATES each cycle (no repeats),
  - hooks are brand-voice and don't repeat across cycles,
  - a Pollinations background is generated and a branded story video is rendered
    to renders/branded/ each cycle,
  - the fabrication filter still refuses ungrounded numbers,
  - no hard-"never"-rule violations slip into the chosen hook/caption.

DRY_RUN=True (Slack drafts/approvals print instead of posting), but the render
is REAL (USE_REAL_REMOTION=True) so you can watch the actual videos, and
Pollinations is live. Ava's model is deepseek-r1:14b, so each cycle takes a few
minutes — 3 cycles is ~10 minutes.

    python scripts/test_ava_branded.py
"""
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ["DRY_RUN"] = "True"            # Slack writes print, not post
os.environ["USE_REAL_REMOTION"] = "True"  # real branded render
os.environ["USE_POLLINATIONS"] = "True"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from agents.ava import Ava, CONTENT_PILLARS, find_fabricated_claims, _PILLAR_STATE_KEY
from brain.hermes_common import load_business_context
from memory import store

# Patterns that would indicate a hard-"never"-rule violation in public copy.
RED_FLAGS = [
    r"\bguarantee", r"\b100k\b", r"\$?\s?50k\s*mrr", r"\buptime\b",
    r"\bhipaa[- ]?certified\b", r"\bsoc\s?2[- ]?certified\b",
    r"\b\d{1,2}[- ]year[- ]old\b", r"\btestimonial", r"\bcase stud",
]


def _reset_pillar():
    """Force the next cycle to start at pillar index 0 for a deterministic run."""
    store.set_bookmark(_PILLAR_STATE_KEY, str(len(CONTENT_PILLARS) - 1))


def _run_cycle(n):
    print("\n" + "#" * 66)
    print(f"# CYCLE {n}")
    print("#" * 66)
    t0 = time.time()
    r = Ava().daily()
    dt = time.time() - t0

    print(f"\n  pillar        : {r.get('pillar')}")
    print("  hooks:")
    for i, h in enumerate(r.get("hooks", [])):
        mark = " <-- CHOSEN" if i == r.get("chosen_hook_index") else ""
        print(f"     [{i}] {h}{mark}")
    print(f"  why           : {r.get('hook_reasoning')}")
    print(f"  caption       : {r.get('caption')}")
    print(f"  image prompt  : {r.get('image_prompt')}")

    bg = r.get("background_path")
    vid = r.get("video_path")
    print(f"  background     : {bg}  (exists={bool(bg) and os.path.exists(bg)})")
    if r.get("rejected"):
        print(f"  REJECTED       : {r.get('reason')}")
    elif r.get("skipped"):
        print(f"  SKIPPED        : {r.get('reason')}")
    elif r.get("render_failed"):
        print("  RENDER FAILED  : posted copy-only text draft, approval still filed")
    else:
        size = os.path.getsize(vid) / 1024 if vid and os.path.exists(vid) else 0
        print(f"  video rendered : {vid}  ({size:.0f} KB, exists={bool(vid) and os.path.exists(vid)})")
        print(f"  approval       : payload {r.get('payload_id')}")
    print(f"  [cycle {n} took {dt:.0f}s]")
    return r


def main():
    print("=" * 66)
    print("Ava branded content pipeline — 3 consecutive cycles")
    print("=" * 66)
    print(f"  DRY_RUN={config.DRY_RUN}  USE_REAL_REMOTION={config.USE_REAL_REMOTION}  "
          f"USE_POLLINATIONS={config.USE_POLLINATIONS}")

    _reset_pillar()
    results = [_run_cycle(i) for i in (1, 2, 3)]

    # --- verification -------------------------------------------------------
    print("\n" + "=" * 66)
    print("VERIFICATION")
    print("=" * 66)

    def chk(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        return ok

    pillars = [r.get("pillar") for r in results]
    chosen = [r.get("chosen_hook") for r in results]
    print(f"  pillars used : {pillars}")

    v1 = chk("pillar rotates (3 distinct pillars)", len(set(pillars)) == 3)
    v2 = chk("chosen hooks don't repeat across cycles", len(set(chosen)) == 3)

    rendered = [r for r in results if not r.get("rejected") and not r.get("skipped")
                and not r.get("render_failed")]
    v3 = chk("all 3 cycles rendered a video to renders/branded/",
             len(rendered) == 3 and all(os.path.exists(r["video_path"]) for r in rendered))

    # brand voice: no exclamation marks / emoji-hype in chosen hooks
    v4 = chk("chosen hooks are brand-voice (no exclamation marks)",
             all("!" not in (h or "") for h in chosen))

    # hard-rule red flags in chosen hook + caption
    violations = []
    for r in results:
        blob = f"{r.get('chosen_hook','')} {r.get('caption','')}".lower()
        for pat in RED_FLAGS:
            if re.search(pat, blob):
                violations.append((r.get("pillar"), pat))
    v5 = chk(f"no hard-rule violations in copy (found: {violations})", not violations)

    # fabrication filter intact — grounded numbers pass, invented ones refused
    ctx = load_business_context()
    grounded = find_fabricated_claims("We ship in 14 days for $8K.", ctx) == []
    fab_plan = {"hooks": ["We onboarded 5000 users in 48 hours", "b", "c"],
                "chosen_hook_index": 0, "hook_reasoning": "r",
                "image_prompt": "abstract", "caption": "trusted by 900 founders"}
    fab_res = Ava()._process_plan(fab_plan, CONTENT_PILLARS[0])
    v6 = chk("fabrication filter: grounded passes, invented (5000 users/48 hours) refused",
             grounded and fab_res.get("rejected") is True and bool(fab_res.get("offenders")))
    print(f"        -> refused offenders: {fab_res.get('offenders')}")

    ok = all([v1, v2, v3, v4, v5, v6])
    print("\n" + ("ALL PASS — rotating pillars, brand-voice hooks, real renders, "
                  "fabrication filter + hard rules intact."
                  if ok else "SOME CHECKS FAILED."))


if __name__ == "__main__":
    main()
