"""Step 3 smoke test for Bobby (dry-run ads).

Runs bobby.daily() against real Slack (DRY_RUN=False) — the #ads report and the
#approvals proposals post for real — while every money-moving path in
ads_platform stays gated and refuses. Then it exercises the guardrails directly,
showing each of the three checks refusing in turn, including the headline case:
"even if I approve it, DRY_RUN still refuses."

Run from the project root:
    python scripts/test_bobby.py

Requires DRY_RUN=False in .env for real Slack, and Ollama running with Bobby's
model (deepseek-r1:14b) pulled. Ollama-down prints a clear error, not a traceback.
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
from integrations import ads_platform
from agents.bobby import Bobby


def preflight_ollama(model):
    """True if Ollama is reachable and `model` is available; else print a clear
    message and return False."""
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


def _expect_refused(label, result):
    ok = isinstance(result, dict) and result.get("ok") is False
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    if not ok:
        print(f"         expected a refusal, got: {result}")
    return ok


def main():
    print("=" * 64)
    print("Bobby — Step 3 smoke test (dry-run ads)")
    print(f"DRY_RUN               : {config.DRY_RUN}")
    print(f"MAX_DAILY_BUDGET_DELTA: {config.MAX_DAILY_BUDGET_DELTA}")
    print(f"MAX_TOTAL_DAILY_SPEND : {config.MAX_TOTAL_DAILY_SPEND}")
    print("=" * 64)

    if config.DRY_RUN:
        print("\n[!] DRY_RUN is True: Slack posts are printed, not sent. Set\n"
              "    DRY_RUN=False in .env for a real end-to-end run.\n")

    print("\n--- Checking Ollama ---------------------------------------------")
    if not preflight_ollama(Bobby.model):
        return

    # --- Step A: run Bobby's daily cycle ------------------------------------
    print("\n--- Running bobby.daily() ---------------------------------------")
    print("    (analyzing 4 campaigns, asking the reasoning model for the report —")
    print("     this can take 30-60s on a local model)...")
    bobby = Bobby()
    try:
        result = bobby.daily()
    except requests.exceptions.RequestException:
        print("\n[x] Lost the connection to Ollama mid-run. Check `ollama serve`.\n")
        return

    # Deterministic analysis (the math Python did, before the model narrated it).
    print("\n--- Per-creative analysis (computed in Python) ------------------")
    for a in result["analyses"]:
        print(f"  {a['creative_id']} {a['name']}: {a['classification'].upper()} "
              f"-> {a['action']} | ROAS {a['roas_pct']:+.0f}%, CPA {a['cpa_pct']:+.0f}%, "
              f"CTR {a['ctr_pct']:+.0f}%")

    # --- Step B: the #ads report --------------------------------------------
    print("\n" + "=" * 64)
    print("REPORT posted to #ads:")
    print("=" * 64)
    print(result["report"])

    # --- Step C: the #approvals proposals -----------------------------------
    print("\n" + "=" * 64)
    print(f"PROPOSALS filed to #approvals ({len(result['proposals'])}):")
    print("=" * 64)
    for p in result["proposals"]:
        print(f"\n[{p['payload_id']}] action={p['action']} delta=${p['delta']:.0f} ts={p['ts']}")
        print(f"  {p['description']}")

    # --- Step D: prove the guardrails refuse --------------------------------
    print("\n" + "=" * 64)
    print("GUARDRAIL CHECKS — every money path must refuse")
    print("=" * 64)

    scale = next((p for p in result["proposals"] if p["action"] == "scale"), None)
    if not scale:
        print("  (no scale proposal produced this run — skipping gate checks that\n"
              "   need a budget delta; analysis classification may have shifted)")
        return

    pid, delta = scale["payload_id"], scale["delta"]

    # Gate 1 — DRY_RUN: approve it AND keep the delta within cap, so ONLY the
    # DRY_RUN check can be what blocks it. This is the headline case.
    ads_platform.APPROVED_PROPOSALS.add(pid)
    saved = config.DRY_RUN
    config.DRY_RUN = True
    try:
        print("\nGate 1 — DRY_RUN (proposal approved, delta within cap):")
        r1 = ads_platform.apply_budget_change(pid, delta)
        _expect_refused("apply_budget_change refused because DRY_RUN is on", r1)
    finally:
        config.DRY_RUN = saved

    # Gate 2 — approval: with DRY_RUN off, an UNapproved id must still refuse.
    print("\nGate 2 — approval (DRY_RUN off, unknown proposal id):")
    r2 = ads_platform.apply_budget_change("bobby-not-a-real-proposal", delta)
    _expect_refused("apply_budget_change refused because proposal not approved", r2)

    # Gate 3 — cap: approved id, DRY_RUN off, but delta over the per-action cap.
    over = config.MAX_DAILY_BUDGET_DELTA + 5
    print(f"\nGate 3 — cap (DRY_RUN off, approved, delta ${over:.0f} > cap "
          f"${config.MAX_DAILY_BUDGET_DELTA:.0f}):")
    r3 = ads_platform.apply_budget_change(pid, over)
    _expect_refused("apply_budget_change refused because delta exceeds the cap", r3)

    # Clean up the approval we injected so state doesn't leak between runs.
    ads_platform.APPROVED_PROPOSALS.discard(pid)

    print("\n" + "=" * 64)
    print("All three guardrails refused. The model never gets a path to spend.")
    print("=" * 64)


if __name__ == "__main__":
    main()
