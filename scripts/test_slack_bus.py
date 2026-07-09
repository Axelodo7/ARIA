"""Step 1 smoke test for the Slack bus.

Posts one hardcoded message as each persona to TARGET_CHANNEL, files an
approval request to #approvals, then polls for your reaction (✅ / ❌) every
POLL_EVERY seconds for up to POLL_TIMEOUT seconds and prints the final status.

Run from the project root:
    python scripts/test_slack_bus.py

To post for real (not just print), set DRY_RUN=False in your .env. Approval
polling needs the #approvals channel ID (CHANNEL_ID_APPROVALS in .env), because
Slack's reactions API works on IDs, not "#name".
"""
import os
import sys
import time

# Windows consoles default to cp1252, which can't encode the ✅/❌ emoji in our
# status output. Force UTF-8 so prints never crash regardless of the terminal.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# --- Make the project root importable when run as `python scripts/...` -------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from integrations import slack_bus

# --- Knobs you can tweak at the top -----------------------------------------
TARGET_CHANNEL = config.CHANNELS["feedback"]  # where the persona messages go
POLL_EVERY = 5        # seconds between approval checks
POLL_TIMEOUT = 120    # give up after this many seconds

# One hardcoded line per persona.
MESSAGES = [
    ("Sarah", "Test message from Sarah — support bus is live."),
    ("Tom", "Test message from Tom — developer bus is live."),
    ("Ava", "Test message from Ava — content bus is live."),
    ("Bobby", "Test message from Bobby — ads bus is live."),
]


def main():
    print("=" * 64)
    print("ARIA Slack bus — Step 1 smoke test")
    print(f"DRY_RUN       : {config.DRY_RUN}")
    print(f"Target channel: {TARGET_CHANNEL}")
    print("=" * 64)

    if config.DRY_RUN:
        print(
            "\n[!] DRY_RUN is True: messages are printed, not sent, and the\n"
            "    approval poll is skipped. Set DRY_RUN=False in .env to run\n"
            "    this against real Slack.\n"
        )

    # --- Step A: post as each persona ---------------------------------------
    print("\n--- Posting as each persona -------------------------------------")
    for name, text in MESSAGES:
        icon = config.PERSONAS[name]
        print(f"  -> posting as {name} ({icon}) to {TARGET_CHANNEL} ...")
        ts = slack_bus.post_as(name, icon, TARGET_CHANNEL, text)
        if ts:
            print(f"     ok (ts={ts})")
        elif not config.DRY_RUN:
            print("     posted (no ts returned)")

    # --- Step B: file an approval request -----------------------------------
    print("\n--- Filing an approval request to #approvals --------------------")
    payload_id = f"test-{int(time.time())}"
    description = "Bus smoke test: react ✅ to approve or ❌ to reject."
    print(f"  payload_id: {payload_id}")
    ts = slack_bus.request_approval("Bobby", description, payload_id)

    if config.DRY_RUN:
        print("\n[DRY_RUN] Approval message not actually posted — nothing to poll.")
        print("Done.")
        return

    if not ts:
        print("  [x] No message ts returned; cannot poll. Aborting.")
        return
    print(f"  approval message posted (ts={ts})")

    approvals_id = config.CHANNEL_IDS.get("approvals")
    if not approvals_id:
        print(
            "\n  [x] CHANNEL_ID_APPROVALS is not set in .env — cannot poll\n"
            "      reactions (the reactions API needs a channel ID). Set it\n"
            "      and re-run."
        )
        return

    # --- Step C: poll for the reaction --------------------------------------
    print(
        f"\n--- Polling for your reaction (every {POLL_EVERY}s, up to "
        f"{POLL_TIMEOUT}s) ---"
    )
    print("    Go to #approvals in Slack and react ✅ or ❌ to the message.")
    deadline = time.time() + POLL_TIMEOUT
    status = "pending"
    while time.time() < deadline:
        status = slack_bus.check_approval(approvals_id, ts)
        elapsed = int(POLL_TIMEOUT - (deadline - time.time()))
        print(f"  [{elapsed:>3}s] status: {status}")
        if status != "pending":
            break
        time.sleep(POLL_EVERY)

    print("\n" + "=" * 64)
    if status == "approved":
        print("RESULT: ✅ approved")
    elif status == "rejected":
        print("RESULT: ❌ rejected")
    else:
        print("RESULT: ⏳ timed out still pending (no reaction within window)")
    print("=" * 64)


if __name__ == "__main__":
    main()
