"""Stage 2 business-memory test.

Verifies docs/memory.md as shared, timestamped business facts:

  1. "remember we landed our first client" -> a timestamped line is appended
     DETERMINISTICALLY (no model call), and ARIA confirms "Noted, sir."
  2. After writing a fact, Sarah's cycle prompt contains it (business memory is
     appended to the <business_context> block).
  3. 50 facts -> the loaded block truncates to <=400 tokens, newest first.
  4. Combined: Bobby's cycle prompt with BOTH agent memory AND business memory
     loaded still has prompt_eval_count < 6144 (no truncation reintroduced).

Runs on an ISOLATED temp DB and temp memory.md (never touches the real files),
DRY_RUN forced. Needs Ollama + gemma3.

    python scripts/test_business_memory.py
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
from pathlib import Path

import config
from memory import store
import brain.hermes_common as HC

# --- Isolate: temp DB + temp memory.md before any agent/ARIA uses them --------
TMP_DB = os.path.join(tempfile.gettempdir(), f"test_bizmem_{os.getpid()}.db")
TMP_MEM = os.path.join(tempfile.gettempdir(), f"test_bizmem_{os.getpid()}.md")
for p in (TMP_DB, TMP_MEM):
    if os.path.exists(p):
        os.remove(p)
store.DB_PATH = TMP_DB
HC.BUSINESS_MEMORY_PATH = TMP_MEM

from brain.hermes_common import (OLLAMA_URL, load_business_memory,
                                 append_business_fact, business_context_block)
import brain.aria_agent as AA
import agents.base_agent as BA
import agents.sarah as SR
from brain.aria_agent import Aria
from agents.bobby import Bobby


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
    print("Stage 2 business-memory test (isolated temp DB + memory.md)")
    print(f"  memory.md: {TMP_MEM}")
    print("=" * 68)
    if not preflight(Bobby.model):
        return

    # 1) Deterministic remember (no model call) --------------------------------
    print("\n--- 'remember we landed our first client' ---")
    aria = Aria()
    calls = {"n": 0}
    real_aria_call = AA.call_hermes

    def counting_call(*a, **k):
        calls["n"] += 1
        return real_aria_call(*a, **k)

    AA.call_hermes = counting_call
    try:
        res = aria.think("remember we landed our first client")
    finally:
        AA.call_hermes = real_aria_call

    print(f"  reply: {res['reply']!r}  tool_calls: {res['tool_calls']}  model_calls: {calls['n']}")
    c1 = chk("ARIA confirms 'Noted, sir.'", res["reply"] == "Noted, sir.")
    c1b = chk("no model call was made (deterministic)", calls["n"] == 0)
    c1c = chk("a 'remember' tool call is reported",
              any(t.get("name") == "remember" for t in res["tool_calls"]))
    mem_text = Path(TMP_MEM).read_text(encoding="utf-8") if os.path.exists(TMP_MEM) else ""
    line_ok = bool(re.search(r"^- \d{4}-\d{2}-\d{2}: we landed our first client\.$",
                             mem_text, re.MULTILINE))
    print(f"  memory.md line: {[l for l in mem_text.splitlines() if l.startswith('- ')]}")
    c1d = chk("a timestamped fact line was appended to memory.md", line_ok)

    # 2) Sarah's cycle prompt contains the fact --------------------------------
    print("\n--- Sarah cycle sees the fact in her prompt ---")
    captured = {}
    real_sr_call = SR.call_hermes

    def cap_sr(messages, **kw):
        captured.setdefault("system", messages[0]["content"])
        return real_sr_call(messages, **kw)

    SR.call_hermes = cap_sr
    try:
        SR.Sarah().cycle()
    finally:
        SR.call_hermes = real_sr_call
    sysp = captured.get("system", "")
    c2 = chk("Sarah's cycle prompt contains the remembered fact",
             "we landed our first client" in sysp)
    c2b = chk("...under the 'Recent developments' business-memory framing",
              "Recent developments you should know about" in sysp)

    # 3) 50 facts -> block truncates to <=400 tokens, newest first -------------
    print("\n--- 50 facts -> <=400 token block, newest first ---")
    fifty = [f"- 2026-07-14: Fact number {i} about the ZentraIQ business milestone."
             for i in range(50)]
    Path(TMP_MEM).write_text("# Business memory\n\n" + "\n".join(fifty) + "\n",
                             encoding="utf-8")
    block = load_business_memory()
    approx = len(block) // 4
    first_line = block.splitlines()[0] if block else ""
    print(f"  loaded ~{approx} tokens; first (newest) line: {first_line!r}")
    c3 = chk(f"business-memory block <=400 tokens with 50 facts (~{approx})", approx <= 400)
    c3b = chk("newest fact is first (number 49 leads the block)", "number 49" in first_line)

    # 4) Combined: agent memory + business memory, prompt_eval < 6144 ----------
    print("\n--- Combined budget: agent memory + business memory ---")
    # seed a few Bobby agent-memory rows so run_turn also injects <your_recent_history>
    for cid, delta in (("cr_A", 13), ("cr_D", 10)):
        store.add_memory("Bobby", "proposal_filed", f"Proposed scale {cid} +${delta}/day",
                         f"bobby-scale-{cid}-seed")
    store.add_memory("Bobby", "proposal_rejected", "Proposed pause cr_B", "bobby-pause-cr_B-seed")

    captured2 = {}
    real_ba_call = BA.call_hermes

    def cap_ba(messages, **kw):
        captured2["messages"] = messages
        return real_ba_call(messages, **kw)

    BA.call_hermes = cap_ba
    try:
        Bobby().daily()
    finally:
        BA.call_hermes = real_ba_call

    sysp2 = captured2["messages"][0]["content"]
    has_agent_mem = "<your_recent_history>" in sysp2
    has_biz_mem = "Recent developments you should know about" in sysp2
    print(f"  Bobby prompt has agent memory={has_agent_mem}, business memory={has_biz_mem}")
    c4a = chk("Bobby's prompt carries BOTH agent memory and business memory",
              has_agent_mem and has_biz_mem)
    pe = measure_prompt_eval(captured2["messages"], Bobby.model, config.NUM_CTX)
    print(f"  prompt_eval_count (both memories loaded) = {pe}  (num_ctx={config.NUM_CTX})")
    c4b = chk(f"prompt_eval_count {pe} < 6144 with both memories", pe is not None and pe < 6144)

    # --- summary ---------------------------------------------------------------
    all_ok = all([c1, c1b, c1c, c1d, c2, c2b, c3, c3b, c4a, c4b])
    print("\n" + "=" * 68)
    print("ALL PASS — business memory writes, loads, caps, and fits alongside "
          "agent memory." if all_ok else "SOME CHECKS FAILED.")
    print("=" * 68)
    for p in (TMP_DB, TMP_MEM):
        try:
            os.remove(p)
        except OSError:
            pass
    return all_ok


if __name__ == "__main__":
    main()
