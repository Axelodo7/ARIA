"""Stage 1 agent-memory test.

Verifies that an agent records its own decisions/outcomes in agent_memory and
reads them back into its next cycle's prompt:

  1. Bobby's cycle writes a proposal_filed row per proposal.
  2. An ❌ rejection (via the approvals poller) flips that row to proposal_rejected.
  3. Bobby's next cycle injects the rejection into <your_recent_history>, and he
     either doesn't re-propose the rejected item or explicitly justifies it.
  4. The history block stays <=600 tokens even with 50 rows.
  5. prompt_eval_count stays under num_ctx (6144) — no truncation reintroduced.

Runs on an ISOLATED temp DB (never touches memory/aria_memory.db), DRY_RUN forced
(no real Slack writes; ads stats are a stub). Needs Ollama + gemma3.

    python scripts/test_agent_memory.py
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

# --- Isolate: point the store at a throwaway DB before any agent uses it ------
TMP_DB = os.path.join(tempfile.gettempdir(), f"test_agent_memory_{os.getpid()}.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
store.DB_PATH = TMP_DB

from brain.hermes_common import OLLAMA_URL
import brain.hermes_common as HC
import agents.base_agent as BA
from agents.base_agent import format_recent_history
from agents.bobby import Bobby
from integrations import slack_bus


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


def measure_prompt_eval(messages, model, num_ctx):
    """Re-send an assembled prompt (num_predict=1) purely to read how many tokens
    Ollama actually ingested — i.e. that nothing was truncated at the window."""
    r = requests.post(OLLAMA_URL, json={
        "model": model, "messages": messages, "stream": False,
        "options": {"temperature": 0.0, "num_predict": 1, "num_ctx": num_ctx},
    }, timeout=180)
    return r.json().get("prompt_eval_count")


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def main():
    print("=" * 68)
    print("Stage 1 agent-memory test (isolated temp DB)")
    print(f"  DB: {TMP_DB}")
    print("=" * 68)
    bobby = Bobby()
    if not preflight(bobby.model):
        return

    # 1) First cycle writes a proposal_filed row per proposal --------------------
    print("\n--- Bobby cycle #1 ---")
    res1 = bobby.daily()
    n_props = len(res1["proposals"])
    mems = store.get_recent_memories("Bobby", 50)
    filed = [m for m in mems if m["event_type"] == "proposal_filed"]
    print(f"  proposals filed this cycle: {n_props}")
    for m in filed:
        print(f"    memory: [{m['event_type']}] {m['summary']}  ({m['payload_id']})")
    c1 = chk(f"a proposal_filed row per proposal (rows={len(filed)}, proposals={n_props})",
             n_props > 0 and len(filed) == n_props)
    pause = next((m for m in filed if "pause" in m["summary"].lower()), None)
    c1b = chk("a pause proposal was recorded in memory", pause is not None)
    if not pause:
        print("\nCannot continue without a pause proposal to reject.")
        return

    # 2) Simulate an ❌ rejection through the approvals poller --------------------
    print("\n--- Simulate ❌ rejection on the pause proposal (via poller) ---")
    target = pause["payload_id"]
    fake_approvals = [{"username": "Bobby", "ts": "111.111",
                       "text": f"*APPROVAL NEEDED* [`{target}`]\nTL;DR: pause cr_B."}]
    orig_hist, orig_check = slack_bus.history, slack_bus.check_approval
    slack_bus.history = lambda cid, limit=50: fake_approvals
    slack_bus.check_approval = lambda cid, ts: "rejected"
    try:
        bobby.process_pending_approvals()
    finally:
        slack_bus.history, slack_bus.check_approval = orig_hist, orig_check
    row = next((m for m in store.get_recent_memories("Bobby", 50)
                if m["payload_id"] == target), None)
    print(f"  {target} -> {row['event_type'] if row else None}")
    c2 = chk("rejected proposal updated to proposal_rejected",
             row is not None and row["event_type"] == "proposal_rejected")

    # 3) Next cycle injects the rejection into the prompt ------------------------
    print("\n--- Bobby cycle #2 (capturing the assembled prompt) ---")
    captured = {}
    real_call = BA.call_hermes

    def capture_call(messages, **kw):
        captured["messages"] = messages
        return real_call(messages, **kw)

    BA.call_hermes = capture_call
    try:
        res2 = bobby.daily()
    finally:
        BA.call_hermes = real_call

    sysp = captured["messages"][0]["content"]
    c3a = chk("cycle-2 prompt contains a <your_recent_history> block",
              "<your_recent_history>" in sysp)
    c3b = chk("cycle-2 prompt shows the pause cr_B proposal as REJECTED",
              "REJECTED by Ayyan" in sysp and "pause cr_b" in sysp.lower())
    c3rule = chk("cycle-2 prompt carries the do-not-re-propose rule",
                 "materially changed" in sysp)

    # Behavioral outcome is enforced DETERMINISTICALLY (Principle 1): the rejected
    # pause cr_B must not be re-filed, and the report must justify holding off.
    re_filed_pause = any(p["action"] == "pause" and p["creative_id"] == "cr_B"
                         for p in res2["proposals"])
    justified = ("holding off" in res2["report"].lower()
                 and "cr_b" in res2["report"].lower())
    print(f"  [info] cr_B re-proposed this cycle={re_filed_pause}; "
          f"report justifies holding off={justified}")
    c3c = chk("does NOT re-propose the rejected pause cr_B", not re_filed_pause)
    c3d = chk("report explicitly justifies holding off on the rejected item", justified)

    # 5) prompt_eval_count stays under the window -------------------------------
    print("\n--- Context-budget checks ---")
    pe = measure_prompt_eval(captured["messages"], bobby.model, config.NUM_CTX)
    print(f"  prompt_eval_count (cycle-2, with memory) = {pe}  (num_ctx={config.NUM_CTX})")
    c5 = chk(f"prompt_eval_count {pe} < 6144 (no truncation reintroduced)",
             pe is not None and pe < 6144)

    # 4) History block stays <=600 tokens with 50 rows --------------------------
    big = [{"timestamp": "2026-07-13T00:00:00", "event_type": "proposal_rejected",
            "summary": f"Proposed pause creative number {i} which is bleeding badly",
            "payload_id": f"p{i}"} for i in range(50)]
    blk = format_recent_history(big)
    approx = len(blk) // 4
    print(f"  50-row history block ~= {approx} tokens")
    c4 = chk(f"history block <=600 tokens even with 50 rows (~{approx})", approx <= 600)

    # --- summary ---------------------------------------------------------------
    all_ok = all([c1, c1b, c2, c3a, c3b, c3rule, c3c, c3d, c4, c5])
    print("\n" + "=" * 68)
    print("ALL PASS — agent memory records, updates, and injects correctly."
          if all_ok else "SOME CHECKS FAILED.")
    print("=" * 68)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass
    return all_ok


if __name__ == "__main__":
    main()
