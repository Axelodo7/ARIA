"""Bobby proposal-dedup test — don't re-file what's already open.

The bug this guards: #approvals accumulated the same three Bobby proposals three
times over (07-10 09:06, 07-10 13:49, 07-14 15:54). They were NOT duplicates —
each cycle filed a legitimately distinct payload_id. The fault was that nothing
checked whether an identical (action, creative) was ALREADY awaiting a decision,
so every cycle re-raised it.

Gates the four deterministic branches:
    file          — nothing open -> propose normally
    skip_pending  — identical proposal already awaiting a decision -> stay quiet
    skip_rejected — the human said no -> never nag
    resurface     — un-actioned past STALE_PROPOSAL_DAYS -> raise ONCE, flagged
plus: an APPROVED proposal is settled, so a later one may file again.

Isolated: temp DB, DRY_RUN forced. No model and no Slack writes — the decision
logic is pure Python, which is the point.

    python scripts/test_proposal_dedup.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ["DRY_RUN"] = "True"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import sqlite3

import config
from memory import store

TMP_DB = os.path.join(tempfile.gettempdir(), f"test_dedup_{os.getpid()}.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
store.DB_PATH = TMP_DB

from agents.bobby import Bobby, analyze_creative
from integrations import ads_platform

ANALYSES = [analyze_creative(c) for c in ads_platform.get_campaign_stats()]
ACTIONABLE = [(a["action"], a["creative_id"]) for a in ANALYSES
              if a["action"] in ("scale", "pause")]


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def reset():
    # store._connect() owns the schema — going through it also creates the tables
    # on first use, which a raw sqlite3.connect would not.
    with store._connect() as conn:
        conn.execute("DELETE FROM agent_memory")
        conn.commit()


def seed(event, summary, payload_id, age_days=0):
    """Insert a memory row aged `age_days` into the past."""
    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    with store._connect() as conn:
        conn.execute("INSERT INTO agent_memory (agent, timestamp, event_type, summary, payload_id) "
                     "VALUES (?,?,?,?,?)", ("Bobby", ts, event, summary, payload_id))
        conn.commit()


def main():
    print("=" * 72)
    print("Bobby proposal dedup — don't re-file what's already open")
    print(f"  actionable this cycle: {ACTIONABLE}")
    print(f"  STALE_PROPOSAL_DAYS = {config.STALE_PROPOSAL_DAYS}")
    print("=" * 72)
    bobby = Bobby()

    # --- 1) nothing open -> file normally ------------------------------------
    reset()
    d = bobby._proposal_decisions(ANALYSES)
    print(f"\n  1) clean slate           -> {[(k[1], v[0]) for k, v in d.items()]}")
    r1 = chk("with nothing open, every actionable proposal files",
             all(v[0] == "file" for v in d.values()) and len(d) == len(ACTIONABLE))
    filed = bobby._file_proposals(ANALYSES, d)
    r1b = chk(f"{len(ACTIONABLE)} proposals filed on a clean slate",
              len(filed) == len(ACTIONABLE))

    # --- 2) the actual bug: identical proposal already pending ---------------
    #     (the rows just written by step 1 are still 'proposal_filed')
    d2 = bobby._proposal_decisions(ANALYSES)
    print(f"  2) same cycle re-run     -> {[(k[1], v[0]) for k, v in d2.items()]}")
    r2 = chk("a proposal already awaiting a decision is NOT re-filed",
             all(v[0] == "skip_pending" for v in d2.values()))
    filed2 = bobby._file_proposals(ANALYSES, d2)
    r2b = chk("re-running the cycle files NOTHING (this is the pile-up bug)",
              filed2 == [])

    # --- 3) rejected -> never nag (Stage 1 behaviour preserved) --------------
    reset()
    seed("proposal_rejected", "Proposed pause cr_B", "bobby-pause-cr_B-1")
    d3 = bobby._proposal_decisions(ANALYSES)
    print(f"  3) cr_B pause rejected   -> {[(k[1], v[0]) for k, v in d3.items()]}")
    r3 = chk("a rejected proposal is never re-filed",
             d3[("pause", "cr_B")][0] == "skip_rejected")
    r3b = chk("other creatives are unaffected by cr_B's rejection",
              d3[("scale", "cr_A")][0] == "file")

    # --- 4) stale pending -> resurface ONCE, flagged -------------------------
    reset()
    seed("proposal_filed", "Proposed scale cr_A +$13/day", "bobby-scale-cr_A-old",
         age_days=config.STALE_PROPOSAL_DAYS + 1)
    seed("proposal_filed", "Proposed pause cr_B", "bobby-pause-cr_B-new", age_days=1)
    d4 = bobby._proposal_decisions(ANALYSES)
    print(f"  4) cr_A pending 8d, cr_B pending 1d -> {[(k[1], v[0]) for k, v in d4.items()]}")
    r4 = chk(f"pending > {config.STALE_PROPOSAL_DAYS}d is re-surfaced",
             d4[("scale", "cr_A")][0] == "resurface")
    r4b = chk(f"pending < {config.STALE_PROPOSAL_DAYS}d stays quiet",
              d4[("pause", "cr_B")][0] == "skip_pending")
    filed4 = bobby._file_proposals(ANALYSES, d4)
    resurfaced = [p for p in filed4 if p.get("resurfaced")]
    r4c = chk("the re-surfaced proposal carries a 'STILL PENDING since' note",
              len(resurfaced) == 1 and "STILL PENDING since" in resurfaced[0]["description"])
    print(f"      note: ...{resurfaced[0]['description'][-70:]!r}" if resurfaced else "")

    # --- 5) re-surfacing resets the clock -> quiet again ---------------------
    d5 = bobby._proposal_decisions(ANALYSES)
    print(f"  5) right after re-surfacing -> {[(k[1], v[0]) for k, v in d5.items()]}")
    r5 = chk("after re-surfacing it goes quiet again (clock reset, not a loop)",
             d5[("scale", "cr_A")][0] == "skip_pending")

    # --- 6) approved is settled -> may propose again -------------------------
    reset()
    seed("proposal_approved", "Proposed scale cr_A +$13/day", "bobby-scale-cr_A-done", age_days=2)
    d6 = bobby._proposal_decisions(ANALYSES)
    print(f"  6) cr_A scale approved   -> {[(k[1], v[0]) for k, v in d6.items()]}")
    r6 = chk("an APPROVED proposal is settled, so a later one may file",
             d6[("scale", "cr_A")][0] == "file")

    all_ok = all([r1, r1b, r2, r2b, r3, r3b, r4, r4b, r4c, r5, r6])
    print("\n" + "=" * 72)
    print("ALL PASS — open proposals aren't re-filed; rejected never nags; stale "
          "re-surfaces once; approved is settled." if all_ok else "SOME CHECKS FAILED.")
    print("=" * 72)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass
    return all_ok


if __name__ == "__main__":
    main()
