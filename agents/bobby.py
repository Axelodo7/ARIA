"""Bobby — the ads agent.

daily() pulls campaign stats, computes each creative's CPA/CTR/ROAS as a
last-7-day vs prior-7-day comparison, posts a plain-English report to #ads, and
files a separate approval request to #approvals for every proposed money action
(scale a winner, pause a loser).

Design note: the arithmetic (deltas, classification, proposed budget delta) is
done HERE in Python and is deterministic — the reasoning model is given the
finished numbers and only writes the narrative "why". Money never depends on the
model doing math, and execution is gated in integrations/ads_platform.py.
"""
import re
import time
from datetime import datetime, timezone

import config
from agents.base_agent import BaseAgent, load_agent_prompt, strip_reasoning
from integrations import slack_bus, ads_platform
from memory import store

# Parses an (action, creative_id) pair back out of a stored proposal summary,
# e.g. "Proposed pause cr_B" -> ("pause", "cr_B").
_PROPOSAL_SUMMARY_RE = re.compile(r"Proposed (scale|pause) (cr_\w+)", re.IGNORECASE)


def _age_days(ts_iso, now):
    """Age of an ISO timestamp in days. Unparseable -> 0 (treat as brand new, i.e.
    never re-surface on a bad timestamp)."""
    try:
        t = datetime.fromisoformat(ts_iso)
    except (TypeError, ValueError):
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 86400.0

# Classification thresholds (percent change of last-7 vs prior-7 average).
_WIN_ROAS = 10      # ROAS up >= this AND ...
_WIN_CPA = -10      # ... CPA down <= this  -> winner
_LOSE_ROAS = -15    # ROAS down <= this  OR ...
_LOSE_CPA = 15      # ... CPA up >= this   -> loser


def _avg(rows, key):
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def _pct(base, recent):
    return ((recent - base) / base * 100) if base else 0.0


def analyze_creative(c):
    """Compute deltas and a recommended action for one creative. Pure/deterministic."""
    daily = c["daily"]
    half = len(daily) // 2
    prev7, last7 = daily[:half], daily[half:]

    cpa_b, cpa_r = _avg(prev7, "cpa"), _avg(last7, "cpa")
    ctr_b, ctr_r = _avg(prev7, "ctr"), _avg(last7, "ctr")
    roas_b, roas_r = _avg(prev7, "roas"), _avg(last7, "roas")
    cpa_pct, ctr_pct, roas_pct = _pct(cpa_b, cpa_r), _pct(ctr_b, ctr_r), _pct(roas_b, roas_r)
    recent_spend = _avg(last7, "spend")

    if roas_pct >= _WIN_ROAS and cpa_pct <= _WIN_CPA:
        classification, action = "winner", "scale"
        # Propose a modest bump, never above the per-action cap.
        delta = min(config.MAX_DAILY_BUDGET_DELTA, round(0.3 * recent_spend))
    elif roas_pct <= _LOSE_ROAS or cpa_pct >= _LOSE_CPA:
        classification, action = "loser", "pause"
        delta = 0.0
    else:
        classification, action = "flat", "hold"
        delta = 0.0

    return {
        "creative_id": c["creative_id"], "name": c["name"],
        "cpa_base": cpa_b, "cpa_recent": cpa_r, "cpa_pct": cpa_pct,
        "ctr_base": ctr_b, "ctr_recent": ctr_r, "ctr_pct": ctr_pct,
        "roas_base": roas_b, "roas_recent": roas_r, "roas_pct": roas_pct,
        "recent_spend": recent_spend,
        "classification": classification, "action": action, "delta": delta,
    }


def _build_report_task(analyses, total_recent_spend):
    """Build the model prompt: hand it the finished numbers, ask for the narrative."""
    rows = []
    for a in analyses:
        if a["action"] == "scale":
            new_spend = a["recent_spend"] + a["delta"]
            rec = (f"PROPOSED ACTION: scale by exactly +${a['delta']:.0f}/day "
                   f"(from ${a['recent_spend']:.0f}/day to ${new_spend:.0f}/day)")
        elif a["action"] == "pause":
            rec = "PROPOSED ACTION: pause"
        else:
            rec = "PROPOSED ACTION: hold (no change)"
        rows.append(
            f"- {a['name']} [{a['creative_id']}]: "
            f"CPA ${a['cpa_base']:.2f}->${a['cpa_recent']:.2f} ({a['cpa_pct']:+.0f}%), "
            f"CTR {a['ctr_base']:.2f}%->{a['ctr_recent']:.2f}% ({a['ctr_pct']:+.0f}%), "
            f"ROAS {a['roas_base']:.2f}->{a['roas_recent']:.2f} ({a['roas_pct']:+.0f}%), "
            f"recent spend ${a['recent_spend']:.0f}/day. Signal: {a['classification']}. "
            f"{rec}."
        )
    table = "\n".join(rows)
    return (
        "Here are today's campaign stats, already computed as last-7-day vs "
        "prior-7-day averages per creative. Use these EXACT numbers — do not "
        "recompute them:\n\n"
        f"{table}\n\n"
        f"Total recent spend across creatives: ${total_recent_spend:.0f}/day "
        f"(total cap ${config.MAX_TOTAL_DAILY_SPEND:.0f}/day; max single daily "
        f"change ${config.MAX_DAILY_BUDGET_DELTA:.0f}).\n\n"
        "Write your #ads report now. Frame the whole analysis through "
        "cost-per-booked-call: for this business the conversion event is a booked "
        "call (the free 30-min call), NOT a purchase — so read each creative's CPA "
        "as its cost per booked call and judge it against our $30-80 per-call "
        "target and the downstream client value ($8K-$23K per client), not raw "
        "ROAS/CTR/CPA in isolation. "
        "Lead with a one-line TL;DR in booked-call terms (e.g. 'cr_A is landing "
        "calls at ~$14 each, well under our $30-80 target — scale it'). Then per "
        "creative give the actual numbers first (cite the real CPA/CTR/ROAS from "
        "the stats above — never invent figures), then the booked-call reading and "
        "the WHY (cost-per-call vs the $30-80 target and expected client value, "
        "plus frequency fatigue? creative decay? audience overlap?), then your "
        "recommended action. When you state a scale recommendation you MUST quote "
        "the exact dollar amount from that creative's PROPOSED ACTION above — never "
        "a percentage and never a figure you make up; those exact amounts are what "
        "get filed to #approvals and the two must match. Flag small samples "
        "honestly. The formal scale/pause proposals are filed separately to "
        "#approvals — in the report just state the recommendation and its "
        "evidence. Output ONLY the report: no drafted messages, no code, no extra "
        "sections."
    )


class Bobby(BaseAgent):
    name = "Bobby"
    icon = config.PERSONAS["Bobby"]
    channel = config.CHANNELS["ads"]
    model = config.AGENT_MODELS["Bobby"]
    system_prompt = load_agent_prompt("Bobby")
    # No model-callable tools: Bobby proposes; money actions execute only via the
    # gated functions in ads_platform after a human ✅.

    def daily(self):
        """Analyze campaigns, post the #ads report, file approval proposals.

        Returns {"report", "proposals", "analyses"} for tests/logging.
        """
        stats = ads_platform.get_campaign_stats()
        analyses = [analyze_creative(c) for c in stats]
        total_recent_spend = sum(a["recent_spend"] for a in analyses)

        report = strip_reasoning(self.run_turn(_build_report_task(analyses, total_recent_spend)))

        # Principle 1: "don't re-raise something already settled or already in the
        # queue" is a right answer, so it's enforced in CODE, not left to the
        # prompt. Note the skips in the report so the narrative matches what's
        # actually filed.
        decisions = self._proposal_decisions(analyses)
        skipped = [(a, decisions[(a["action"], a["creative_id"])][0])
                   for a in analyses
                   if (a["action"], a["creative_id"]) in decisions
                   and decisions[(a["action"], a["creative_id"])][0].startswith("skip")]
        if skipped:
            rej = "; ".join(f"{a['action']} {a['creative_id']}"
                            for a, why in skipped if why == "skip_rejected")
            pend = "; ".join(f"{a['action']} {a['creative_id']}"
                             for a, why in skipped if why == "skip_pending")
            if rej:
                report += (f"\n\n_Holding off on re-proposing {rej} — previously "
                           "rejected and the data hasn't materially changed._")
            if pend:
                report += (f"\n\n_Not re-filing {pend} — already awaiting your "
                           "decision in #approvals._")
        self.post(report)

        proposals = self._file_proposals(analyses, decisions)
        return {"report": report, "proposals": proposals, "analyses": analyses}

    def _already_open(self, limit=40):
        """(action, creative_id) -> (event_type, timestamp) for the LATEST memory
        row about that proposal. 'Open' means the human hasn't acted on it:
        proposal_filed is still sitting in #approvals, proposal_rejected is a no.
        An approved proposal is settled and leaves no entry here, so a later
        proposal for the same creative is free to file."""
        out = {}
        for m in store.get_recent_memories(self.name, limit=limit):   # newest first
            mm = _PROPOSAL_SUMMARY_RE.match(m.get("summary") or "")
            if not mm:
                continue
            key = (mm.group(1).lower(), mm.group(2))
            if key in out:
                continue                       # newest wins; older rows are history
            event = m.get("event_type")
            if event in ("proposal_filed", "proposal_rejected"):
                out[key] = (event, m.get("timestamp"))
            else:
                out[key] = (None, None)        # e.g. approved -> settled, may re-file
        return {k: v for k, v in out.items() if v[0]}

    def _proposal_decisions(self, analyses, now=None):
        """What to do with each actionable analysis, decided deterministically:
            file          — nothing open for it
            skip_rejected — the human said no; never nag
            skip_pending  — an identical proposal is already awaiting a decision
            resurface     — pending and un-actioned past STALE_PROPOSAL_DAYS, so
                            raise it ONCE more, flagged, then go quiet again
        """
        now = now or datetime.now(timezone.utc)
        open_map = self._already_open()
        out = {}
        for a in analyses:
            if a["action"] not in ("scale", "pause"):
                continue                       # flat -> hold, never proposed
            key = (a["action"], a["creative_id"])
            event, ts = open_map.get(key, (None, None))
            if event == "proposal_rejected":
                out[key] = ("skip_rejected", ts)
            elif event == "proposal_filed":
                stale = _age_days(ts, now) >= config.STALE_PROPOSAL_DAYS
                out[key] = ("resurface" if stale else "skip_pending", ts)
            else:
                out[key] = ("file", None)
        return out

    def _file_proposals(self, analyses, decisions=None):
        """File one approval request per money action, each with a distinct id.
        Honours _proposal_decisions: a rejected action is never re-filed, and one
        already awaiting a decision isn't re-filed either — unless it has gone
        stale, in which case it's raised once more with a note saying so."""
        if decisions is None:
            decisions = self._proposal_decisions(analyses)
        run_stamp = int(time.time())
        proposals = []
        for a in analyses:
            verdict, since = decisions.get((a["action"], a["creative_id"]), ("file", None))
            if verdict.startswith("skip"):
                continue
            if a["action"] == "scale":
                payload_id = f"bobby-scale-{a['creative_id']}-{run_stamp}"
                desc = (
                    f"TL;DR: scale {a['name']} by +${a['delta']:.0f}/day.\n"
                    f"ROAS {a['roas_base']:.2f}->{a['roas_recent']:.2f} "
                    f"({a['roas_pct']:+.0f}%), CPA ${a['cpa_base']:.2f}->"
                    f"${a['cpa_recent']:.2f} ({a['cpa_pct']:+.0f}%) vs trailing 7 days. "
                    f"[creative {a['creative_id']}]"
                )
            elif a["action"] == "pause":
                payload_id = f"bobby-pause-{a['creative_id']}-{run_stamp}"
                desc = (
                    f"TL;DR: pause {a['name']}.\n"
                    f"ROAS {a['roas_base']:.2f}->{a['roas_recent']:.2f} "
                    f"({a['roas_pct']:+.0f}%), CPA ${a['cpa_base']:.2f}->"
                    f"${a['cpa_recent']:.2f} ({a['cpa_pct']:+.0f}%), CTR "
                    f"{a['ctr_base']:.2f}%->{a['ctr_recent']:.2f}% "
                    f"({a['ctr_pct']:+.0f}%) — bleeding vs trailing 7 days. "
                    f"[creative {a['creative_id']}]"
                )
            else:
                continue  # flat -> hold, no proposal

            if verdict == "resurface":
                # Un-actioned past the stale window: raise it once more, clearly
                # flagged, so a forgotten proposal can't vanish. Filing resets the
                # clock, so it goes quiet again for another window.
                since_date = (since or "")[:10]
                desc += (f"\n:hourglass: STILL PENDING since {since_date} — no "
                         f"decision yet, re-surfacing once.")

            ts = slack_bus.request_approval("Bobby", desc, payload_id)
            # Stage 1 memory: record the filed proposal with a deterministic
            # (Python-written) summary, linked by payload_id for the verdict later.
            if a["action"] == "scale":
                summary = f"Proposed scale {a['creative_id']} +${a['delta']:.0f}/day"
            else:
                summary = f"Proposed pause {a['creative_id']}"
            store.add_memory("Bobby", "proposal_filed", summary, payload_id)
            proposals.append({
                "payload_id": payload_id, "action": a["action"],
                "creative_id": a["creative_id"], "delta": a["delta"],
                "ts": ts, "description": desc, "resurfaced": verdict == "resurface",
            })
        return proposals
