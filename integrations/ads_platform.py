"""Ads platform stub.

Real Meta Marketing API integration is a later job. For now this returns
hardcoded-but-realistic campaign stats and enforces the money guardrails so the
rest of Bobby's pipeline can be built and tested safely.

apply_budget_change and pause_creative are the ONLY paths that could move money.
Both run the same three checks, in order, and refuse if any fails:
  (1) DRY_RUN is on            -> refuse
  (2) not in APPROVED_PROPOSALS -> refuse   (needs a human ✅)
  (3) delta exceeds the cap    -> refuse
This is the belt-AND-suspenders design: the model never gets a path to spend
that bypasses these checks. Approving a proposal (adding its id to
APPROVED_PROPOSALS) is the daemon's job later; nothing here does it automatically.
"""
import datetime
import math

import config

# --- Approval state ---------------------------------------------------------
# Proposal ids (for budget changes) and creative ids (for pauses) the human has
# approved. Populated by the daemon on a ✅ reaction; empty by default so the
# approval gate refuses everything until then.
APPROVED_PROPOSALS = set()

# Deterministic anchor so the stub's 14-day window doesn't drift day to day.
ANCHOR = datetime.date(2026, 7, 9)


# --- Fake data generation ---------------------------------------------------
def _dates(n=14):
    return [(ANCHOR - datetime.timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _series(start, end, n=14, wiggle=0.0):
    """Linear start->end over n points with a small deterministic wiggle, so the
    numbers look like real daily data rather than a clean line."""
    out = []
    for i in range(n):
        t = i / (n - 1)
        val = start + (end - start) * t + wiggle * math.sin(i * 1.7)
        out.append(round(val, 3))
    return out


def _creative(cid, name, spend, impressions, ctr, cpa, roas):
    dates = _dates()
    daily = [
        {"date": d, "spend": round(s, 2), "impressions": int(im),
         "ctr": round(c, 2), "cpa": round(cp, 2), "roas": round(r, 2)}
        for d, s, im, c, cp, r in zip(dates, spend, impressions, ctr, cpa, roas)
    ]
    return {"creative_id": cid, "name": name, "daily": daily}


def get_campaign_stats():
    """Return ~4 fake campaigns, each with 14 daily rows.

    Designed with clear signals: A and D winning, B bleeding, C flat.
    """
    n = 14
    return [
        # A — clear WINNER: CPA falling, CTR & ROAS rising.
        _creative(
            "cr_A", "Retargeting — carousel",
            spend=_series(34, 44, n, 1.2),
            impressions=_series(13000, 16000, n, 260),
            ctr=_series(1.6, 2.8, n, 0.05),
            cpa=_series(22, 12, n, 0.4),
            roas=_series(1.8, 3.6, n, 0.06),
        ),
        # B — clear LOSER: CPA rising, CTR & ROAS falling (frequency fatigue:
        # impressions up but engagement down).
        _creative(
            "cr_B", "Broad prospecting — single image",
            spend=_series(38, 42, n, 1.0),
            impressions=_series(18000, 23000, n, 300),
            ctr=_series(1.9, 1.0, n, 0.04),
            cpa=_series(20, 34, n, 0.6),
            roas=_series(2.3, 1.1, n, 0.05),
        ),
        # C — FLAT: everything roughly steady within noise.
        _creative(
            "cr_C", "Brand awareness — static",
            spend=_series(30, 30, n, 0.6),
            impressions=_series(14000, 14200, n, 180),
            ctr=_series(1.5, 1.48, n, 0.03),
            cpa=_series(18, 18.4, n, 0.3),
            roas=_series(2.0, 2.03, n, 0.04),
        ),
        # D — moderate WINNER: gentler improvement than A.
        _creative(
            "cr_D", "Video hook — UGC",
            spend=_series(28, 33, n, 0.9),
            impressions=_series(15000, 17000, n, 220),
            ctr=_series(1.8, 2.2, n, 0.04),
            cpa=_series(19, 15, n, 0.4),
            roas=_series(2.0, 2.7, n, 0.05),
        ),
    ]


# --- Money guardrails -------------------------------------------------------
def _gate_failure(fn, approval_key, delta):
    """Return a refusal reason string if any guardrail blocks this action, else
    None. Checked strictly in order: DRY_RUN, then approval, then the cap."""
    if config.DRY_RUN:
        return "DRY_RUN is on — no real spend/pause changes execute"
    if approval_key not in APPROVED_PROPOSALS:
        return f"'{approval_key}' is not in APPROVED_PROPOSALS (needs a human ✅)"
    if delta > config.MAX_DAILY_BUDGET_DELTA:
        return (f"delta ${delta:.2f} exceeds MAX_DAILY_BUDGET_DELTA "
                f"${config.MAX_DAILY_BUDGET_DELTA:.2f}")
    return None


def apply_budget_change(proposal_id, delta):
    """Apply a budget change ONLY if all three guardrails pass. Logs and refuses
    otherwise. Returns {"ok": bool, ...}."""
    reason = _gate_failure("apply_budget_change", proposal_id, delta)
    if reason:
        print(f"[ads] REFUSED apply_budget_change({proposal_id}, {delta:+.2f}/day): {reason}")
        return {"ok": False, "reason": reason, "proposal_id": proposal_id, "delta": delta}
    # Real Meta API call would go here.
    print(f"[ads] APPLIED apply_budget_change({proposal_id}, {delta:+.2f}/day) — stub only")
    return {"ok": True, "proposal_id": proposal_id, "delta": delta}


def pause_creative(creative_id):
    """Pause a creative ONLY if all three guardrails pass. Pausing has no budget
    delta, so the cap check (with delta 0) is trivially satisfied."""
    reason = _gate_failure("pause_creative", creative_id, 0.0)
    if reason:
        print(f"[ads] REFUSED pause_creative({creative_id}): {reason}")
        return {"ok": False, "reason": reason, "creative_id": creative_id}
    # Real Meta API call would go here.
    print(f"[ads] PAUSED pause_creative({creative_id}) — stub only")
    return {"ok": True, "creative_id": creative_id}
