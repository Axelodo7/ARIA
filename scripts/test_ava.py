"""Step 4 smoke test for Ava (content, without posting).

Seeds a realistic Sarah digest into #support-intel, runs ava.daily() against
real Slack (DRY_RUN=False) — the #content draft and #approvals request post for
real — while Remotion is stubbed and Metricool refuses to schedule. Prints Ava's
decisions, then proves:
  - the live output invents NO numbers absent from the source digest,
  - all three scheduling guardrails refuse independently,
  - the fabrication gate catches invented specifics (both the validator directly
    and the end-to-end refuse path).

Run from the project root:
    python scripts/test_ava.py

Requires DRY_RUN=False in .env for real Slack, and Ollama running with Ava's
model (deepseek-r1:14b). Ollama-down prints a clear error, not a traceback.
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # UTF-8 safe on Windows consoles
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

import config
from brain.hermes_common import OLLAMA_URL
from integrations import slack_bus, metricool, gdrive
from agents.ava import Ava, find_fabricated_claims, _fix_hashtag

# A realistic digest to seed into #support-intel. Its only numbers are "14"
# (hours), the invoice id, "5"(MB), and "2"(users) — so any other number in
# Ava's output would be fabricated.
SEED_DIGEST = """🚨 URGENT
- Possible IDOR — a user could view another user's invoice by changing the URL id. @Tom fixed and shipped it in 14 hours. "I can see another user's invoice at /invoice/1002."

BUGS/ISSUES (@Tom)
- Photo upload failed on large images — 2 users. "crashes every time I upload a photo over 5MB." (Fixed.)

FEATURE REQUESTS (@Tom)
- Dark mode — 2 users. "using this at night is rough."

STORIES & PRAISE (@Ava)
- Auto-tagging called "genuinely magic", saved hours — 1 user. "saved me hours this week."
- Onboarding praised, driving word-of-mouth — 1 user. "smoothest onboarding I've used, told two friends.\""""


def preflight_ollama(model):
    tags_url = OLLAMA_URL.replace("/api/chat", "/api/tags")
    try:
        resp = requests.get(tags_url, timeout=5)
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        print(f"\n[x] Cannot reach Ollama at {tags_url.replace('/api/tags','')}.\n"
              "    Start it with `ollama serve`, then re-run.\n")
        return False
    models = [m.get("name", "") for m in resp.json().get("models", [])]
    if not any(name == model or name.startswith(model) for name in models):
        print(f"\n[!] Ollama is up, but `{model}` was not found. Pull it with\n"
              f"    `ollama pull {model}`. Available: {models}\n")
        return False
    print(f"  Ollama reachable; {model} present.")
    return True


def cleanup(channel_key):
    cid = config.CHANNEL_IDS.get(channel_key)
    if not cid:
        print(f"  (no channel id for {channel_key}; skipping cleanup)")
        return
    deleted = 0
    for m in slack_bus.history(cid, limit=100):
        if m.get("bot_id") or m.get("subtype") == "bot_message":
            slack_bus.delete_message(cid, m["ts"])
            deleted += 1
    print(f"  cleaned #{channel_key}: deleted {deleted} bot message(s)")


def _check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def _expect_refused(label, result):
    ok = isinstance(result, dict) and result.get("ok") is False
    return _check(label, ok, "" if ok else f"expected refusal, got {result}")


def main():
    print("=" * 64)
    print("Ava — Step 4 smoke test (content, without posting)")
    print(f"DRY_RUN          : {config.DRY_RUN}")
    print(f"USE_REAL_REMOTION: {config.USE_REAL_REMOTION}")
    print(f"Platform allowlist: {sorted(config.METRICOOL_PLATFORM_ALLOWLIST)}")
    print("=" * 64)

    if config.DRY_RUN:
        print("\n[!] DRY_RUN is True: Slack posts are printed, not sent. Set\n"
              "    DRY_RUN=False in .env for a real end-to-end run.\n")

    print("\n--- Checking Ollama ---------------------------------------------")
    if not preflight_ollama(Ava.model):
        return

    print("\n--- Seeding #support-intel + cleaning #content ------------------")
    cleanup("support_intel")
    cleanup("content")
    slack_bus.post_as("Sarah", config.PERSONAS["Sarah"],
                      config.CHANNELS["support_intel"], SEED_DIGEST)
    print("  seeded one digest into #support-intel")

    print("\n--- Running ava.daily() -----------------------------------------")
    print("    (reading digests, writing hooks/caption, choosing footage — this")
    print("     can take 30-60s on the reasoning model)...")
    try:
        r = Ava().daily()
    except requests.exceptions.RequestException:
        print("\n[x] Lost the connection to Ollama mid-run. Check `ollama serve`.\n")
        return
    except RuntimeError as e:
        print(f"\n[x] {e}\n")
        return

    # --- Ava's decisions -----------------------------------------------------
    print("\n" + "=" * 64)
    print("AVA'S DECISIONS")
    print("=" * 64)
    if r.get("rejected"):
        print("\n[!] Ava's live plan was REFUSED by the fabrication gate:")
        print(f"    offenders: {r['offenders']}")
        print(f"    chosen hook: \"{r.get('chosen_hook','')}\"")
        print(f"    caption    : {r.get('caption','')}")
        pid, platforms = "ava-synthetic-pid", sorted(config.METRICOOL_PLATFORM_ALLOWLIST)
    else:
        print(f"\nStory: {r['story']}")
        print("\nHooks:")
        for i, h in enumerate(r["hooks"]):
            mark = "  <-- CHOSEN" if i == r["chosen_hook_index"] else ""
            print(f"  {i}. \"{h}\"{mark}")
        print(f"\nWhy this hook: {r['hook_reasoning']}")
        print(f"\nFootage: {r['footage']['filename']} [{r['footage']['id']}] "
              f"tags={r['footage']['tags']}")
        print(f"Why this footage: {r['footage_reasoning']}")
        print(f"\nCaption: {r['caption']}")
        print(f"Hashtags: {' '.join(r['hashtags'])}")
        print(f"Rendered (stub): {r['video_path']}")
        print(f"Proposed platforms: {r['platforms']}")
        print("\n--- #content post --------------------------------------------")
        print(r["content_post"])
        print("\n--- #approvals request ---------------------------------------")
        print(f"payload_id: {r['payload_id']}  (ts={r['approval_ts']})")
        pid, platforms = r["payload_id"], r["platforms"]

    # --- Condition 1: live output invents no numbers ------------------------
    print("\n" + "=" * 64)
    print("CONDITION 1 — live output invents no numbers absent from the source")
    print("=" * 64)
    if r.get("rejected"):
        _check("live output clean", False, "plan was refused (contained fabrication)")
    else:
        live = find_fabricated_claims(f"{r['chosen_hook']} {r['caption']}", SEED_DIGEST)
        _check("no fabricated numbers in hook+caption", not live,
               "" if not live else f"found: {live}")
        for h in r["hashtags"]:
            _check(f"hashtag is a single token: {h}", " " not in h)

    # --- Scheduling guardrails (3) ------------------------------------------
    print("\n" + "=" * 64)
    print("SCHEDULING GUARDRAILS — must refuse until every gate passes")
    print("=" * 64)

    metricool.APPROVED_POSTS.add(pid)
    saved = config.DRY_RUN
    config.DRY_RUN = True
    try:
        print("\nGate 1 — DRY_RUN (approved, allowed platforms):")
        _expect_refused("schedule_post refused because DRY_RUN is on",
                        metricool.schedule_post("v.mp4", "cap", platforms,
                                                "2026-07-10T09:00", payload_id=pid))
    finally:
        config.DRY_RUN = saved

    print("\nGate 2 — approval (DRY_RUN off, unknown post id):")
    _expect_refused("schedule_post refused because post not approved",
                    metricool.schedule_post("v.mp4", "cap", platforms,
                                            "2026-07-10T09:00", payload_id="ava-not-real"))

    print("\nGate 3 — platform allowlist (DRY_RUN off, approved, platforms={'facebook'}):")
    _expect_refused("schedule_post refused because facebook is not allowlisted",
                    metricool.schedule_post("v.mp4", "cap", {"facebook"},
                                            "2026-07-10T09:00", payload_id=pid))
    metricool.APPROVED_POSTS.discard(pid)

    # --- Fabrication gate (independent proof) -------------------------------
    print("\n" + "=" * 64)
    print("FABRICATION GATE — catches invented specifics")
    print("=" * 64)

    # (a) the validator directly
    print("\nValidator (find_fabricated_claims):")
    _check("catches '3 hours' when source says only 'hours'",
           find_fabricated_claims("saved me 3 hours last week", "saved me hours this week") == ["3 hours"])
    _check("passes clean text (no numbers)",
           find_fabricated_claims("saved me hours this week", "saved me hours this week") == [])
    _check("allows a number that IS in the source (14 hours)",
           find_fabricated_claims("fixed in 14 hours", "shipped it in 14 hours") == [])
    _check("hashtag auto-fix removes spaces (#time saver -> #timesaver)",
           _fix_hashtag("#time saver") == "#timesaver")

    # (b) end-to-end refuse path with a hand-built fabricated plan
    print("\nRefuse path (_process_plan with a fabricated plan):")
    fab_source = "STORIES & PRAISE\n- Auto-tagging saved me hours this week."
    fab_plan = {
        "story": "Auto-tagging saves time",
        "hooks": ["Auto-tagging saved me 3 hours last week.", "Effortless tagging now.", "Users love auto-tagging."],
        "chosen_hook_index": 0, "hook_reasoning": "specific",
        "footage_id": "f_003", "footage_reasoning": "praise clip",
        "caption": "It saved one user 3 hours last week.",
        "hashtags": ["#time saver"], "platforms": ["instagram"],
    }
    res = Ava()._process_plan(fab_plan, fab_source, gdrive.list_footage())
    _check("fabricated plan is refused (not posted)", res.get("rejected") is True)
    _check("offenders include the invented '3 hours'",
           any("3 hours" in o for o in res.get("offenders", [])))
    _check("refused plan quarantined to renders/rejected/",
           bool(res.get("rejected_path")) and os.path.exists(res["rejected_path"]))

    print("\n" + "=" * 64)
    print("Done. Fabrication is caught at the prompt (Level 1) AND in code")
    print("(Level 2). Nothing publishes without a ✅ and only to allowlisted platforms.")
    print("=" * 64)


if __name__ == "__main__":
    main()
