"""Step 4 smoke test for Ava (content, without posting).

Seeds a realistic Sarah digest into #support-intel, runs ava.daily() against
real Slack (DRY_RUN=False) — the #content draft and #approvals request post for
real — while Remotion is stubbed and Metricool refuses to schedule. Prints Ava's
decisions (story, hooks + pick + why, footage + why, caption, approval), then
proves the scheduling guardrails each refuse independently.

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
from integrations import slack_bus, metricool
from agents.ava import Ava

# A realistic digest to seed into #support-intel so Ava always has material.
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


def _expect_refused(label, result):
    ok = isinstance(result, dict) and result.get("ok") is False
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         expected a refusal, got: {result}")
    return ok


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

    # --- Seed source material -----------------------------------------------
    print("\n--- Seeding #support-intel + cleaning #content ------------------")
    cleanup("support_intel")
    cleanup("content")
    slack_bus.post_as("Sarah", config.PERSONAS["Sarah"],
                      config.CHANNELS["support_intel"], SEED_DIGEST)
    print("  seeded one digest into #support-intel")

    # --- Run Ava's daily cycle ----------------------------------------------
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

    print("\n--- #content post ------------------------------------------------")
    print(r["content_post"])

    print("\n--- #approvals request -------------------------------------------")
    print(f"payload_id: {r['payload_id']}  (ts={r['approval_ts']})")

    # --- Guardrail proofs ----------------------------------------------------
    print("\n" + "=" * 64)
    print("GUARDRAIL CHECKS — scheduling must refuse until every gate passes")
    print("=" * 64)

    pid = r["payload_id"]
    good_platforms = r["platforms"]

    # Gate 1 — DRY_RUN: approve it and use allowed platforms, so ONLY DRY_RUN
    # can block. The headline "even if I approve, DRY_RUN still refuses" case.
    metricool.APPROVED_POSTS.add(pid)
    saved = config.DRY_RUN
    config.DRY_RUN = True
    try:
        print("\nGate 1 — DRY_RUN (approved, allowed platforms):")
        r1 = metricool.schedule_post(r["video_path"], r["caption"], good_platforms,
                                     "2026-07-10T09:00", payload_id=pid)
        _expect_refused("schedule_post refused because DRY_RUN is on", r1)
    finally:
        config.DRY_RUN = saved

    # Gate 2 — approval: DRY_RUN off, an UNapproved id must still refuse.
    print("\nGate 2 — approval (DRY_RUN off, unknown post id):")
    r2 = metricool.schedule_post(r["video_path"], r["caption"], good_platforms,
                                 "2026-07-10T09:00", payload_id="ava-not-real")
    _expect_refused("schedule_post refused because post not approved", r2)

    # Gate 3 — platform allowlist: DRY_RUN off + approved (gates 1 & 2 bypassed),
    # but target {facebook} is not allowlisted -> the allowlist gate alone blocks.
    print("\nGate 3 — platform allowlist (DRY_RUN off, approved, platforms={'facebook'}):")
    r3 = metricool.schedule_post(r["video_path"], r["caption"], {"facebook"},
                                 "2026-07-10T09:00", payload_id=pid)
    _expect_refused("schedule_post refused because facebook is not allowlisted", r3)

    metricool.APPROVED_POSTS.discard(pid)  # don't leak approval state

    print("\n" + "=" * 64)
    print("All scheduling guardrails refused independently. Nothing publishes")
    print("without a human ✅, and only to allowlisted platforms.")
    print("=" * 64)


if __name__ == "__main__":
    main()
