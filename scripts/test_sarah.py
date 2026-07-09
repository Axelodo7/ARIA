"""Step 2 smoke test for Sarah (text-only).

Posts a batch of hardcoded fake user feedback to #feedback as fake "users",
runs Sarah's cycle (read #feedback -> Hermes -> digest -> #support-intel), and
prints the digest.

Run from the project root:
    python scripts/test_sarah.py

Requires:
  - DRY_RUN=False in .env to actually post/read Slack (else it just prints).
  - Ollama running at localhost:11434 with `hermes3` pulled. If it's not
    reachable, this prints a clear error rather than a traceback.
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")  # UTF-8 safe on Windows consoles
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

import config
from integrations import slack_bus
from brain.hermes_common import OLLAMA_URL
from agents.sarah import Sarah

FEEDBACK_CHANNEL = config.CHANNELS["feedback"]

# Fake "users" and their feedback — a spread of bugs, feature asks, and praise,
# including one alarming item so Sarah's URGENT handling is exercised.
FAKE_FEEDBACK = [
    ("user_maya",   "Love the app but it crashes every time I upload a photo over 5MB. Third time this week."),
    ("user_darnell","The export button does nothing on Safari. Works on Chrome. Kind of a dealbreaker for me."),
    ("user_priya",  "Photo upload keeps failing for big images — had to resize manually. Please fix!"),
    ("user_tom_h",  "Honestly the onboarding is the smoothest I've used. Told two friends about it already."),
    ("user_glen",   "Would really love a dark mode. My eyes are begging you."),
    ("user_sasha",  "Can we get dark mode please? Using this at night is rough."),
    ("user_kev",    "SECURITY: I think I can see another user's invoice at /invoice/1002 by changing the number. This is bad."),
    ("user_lena",   "Saved me hours this week. The auto-tagging is genuinely magic. Thank you."),
]


def preflight_ollama():
    """Return True if Ollama is reachable and hermes3 is available, else print
    a clear message and return False."""
    tags_url = OLLAMA_URL.replace("/api/chat", "/api/tags")
    try:
        resp = requests.get(tags_url, timeout=5)
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        print(
            "\n[x] Cannot reach Ollama at "
            f"{tags_url.replace('/api/tags','')}.\n"
            "    Start it with `ollama serve` and make sure `hermes3` is pulled\n"
            "    (`ollama pull hermes3`), then re-run.\n"
        )
        return False
    models = [m.get("name", "") for m in resp.json().get("models", [])]
    if not any(name.startswith("hermes3") for name in models):
        print(
            "\n[!] Ollama is up, but no `hermes3` model was found. Pull it with\n"
            "    `ollama pull hermes3`. Models currently available:\n"
            f"    {models or '(none)'}\n"
        )
        return False
    print(f"  Ollama reachable; hermes3 present. Models: {models}")
    return True


def cleanup(channel_key):
    """Delete bot-authored (test) messages from a channel for a clean slate.

    Only touches messages the bot posted (bot_id / bot_message subtype), so
    genuine user feedback from real Slack accounts is never removed.
    """
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


def main():
    print("=" * 64)
    print("Sarah — Step 2 smoke test")
    print(f"DRY_RUN         : {config.DRY_RUN}")
    print(f"Feedback channel: {FEEDBACK_CHANNEL}")
    print("=" * 64)

    if config.DRY_RUN:
        print(
            "\n[!] DRY_RUN is True: fake feedback is printed, not posted, and\n"
            "    Sarah will read whatever is actually in #feedback (likely\n"
            "    nothing). Set DRY_RUN=False in .env for a real end-to-end run.\n"
        )

    # --- Preflight: is the model server up? ---------------------------------
    print("\n--- Checking Ollama ---------------------------------------------")
    if not preflight_ollama():
        return

    # --- Step 0: clean up prior test messages -------------------------------
    print("\n--- Cleaning up prior test messages -----------------------------")
    cleanup("feedback")
    cleanup("support_intel")

    # --- Step A: post fake feedback as fake users ---------------------------
    print("\n--- Posting fake feedback to #feedback --------------------------")
    for username, text in FAKE_FEEDBACK:
        print(f"  -> {username}: {text[:60]}{'...' if len(text) > 60 else ''}")
        slack_bus.post_as(username, ":bust_in_silhouette:", FEEDBACK_CHANNEL, text)
    print(f"  posted {len(FAKE_FEEDBACK)} messages.")

    if not config.DRY_RUN:
        # Give Slack a moment to make the messages queryable via history.
        time.sleep(2)

    # --- Step B: run Sarah's cycle ------------------------------------------
    print("\n--- Running sarah.cycle() ---------------------------------------")
    print("    (reading #feedback, asking Hermes for a digest — this can take")
    print("     several seconds on a local model)...")
    sarah = Sarah()
    try:
        digest = sarah.cycle()
    except requests.exceptions.RequestException:
        print(
            "\n[x] Lost the connection to Ollama mid-run. Check `ollama serve`\n"
            "    is still up and re-run.\n"
        )
        return
    except RuntimeError as e:
        print(f"\n[x] {e}\n")
        return

    # --- Step C: show the digest --------------------------------------------
    print("\n" + "=" * 64)
    print("DIGEST posted to #support-intel:")
    print("=" * 64)
    print(digest)
    print("=" * 64)


if __name__ == "__main__":
    main()
