"""Metricool scheduling stub.

Real Metricool API integration is a later job. schedule_post is the only
function, and it enforces the same belt-AND-suspenders gate as
ads_platform.apply_budget_change — three checks, in order, refuse if any fails:
  (1) DRY_RUN is on                          -> refuse
  (2) payload_id not in APPROVED_POSTS        -> refuse   (needs a human ✅)
  (3) platforms not a subset of the allowlist -> refuse
Nothing gets scheduled or published without passing all three. Approving a post
(adding its id to APPROVED_POSTS) is the daemon's job later.
"""
import config

# Post ids the human has approved for scheduling. Empty by default so the gate
# refuses everything until a ✅ lands.
APPROVED_POSTS = set()


def _gate_failure(payload_id, platforms):
    """Return a refusal reason if any guardrail blocks this schedule, else None.
    Checked strictly in order: DRY_RUN, approval, platform allowlist."""
    if config.DRY_RUN:
        return "DRY_RUN is on — nothing is scheduled or published"
    if payload_id not in APPROVED_POSTS:
        return f"'{payload_id}' is not in APPROVED_POSTS (needs a human ✅)"
    bad = set(platforms) - set(config.METRICOOL_PLATFORM_ALLOWLIST)
    if bad:
        return (f"platforms {sorted(bad)} not in allowlist "
                f"{sorted(config.METRICOOL_PLATFORM_ALLOWLIST)}")
    return None


def schedule_post(video_path, caption, platforms, when, payload_id=None):
    """Schedule a post ONLY if all three guardrails pass; log and refuse
    otherwise. Returns {"ok": bool, ...}."""
    reason = _gate_failure(payload_id, platforms)
    if reason:
        print(f"[metricool] REFUSED schedule_post({payload_id}, {sorted(set(platforms))}): {reason}")
        return {"ok": False, "reason": reason, "payload_id": payload_id,
                "platforms": sorted(set(platforms))}
    # Real Metricool API call would go here.
    print(f"[metricool] SCHEDULED {video_path} -> {sorted(set(platforms))} at {when} — stub only")
    return {"ok": True, "payload_id": payload_id, "video_path": video_path,
            "platforms": sorted(set(platforms)), "when": when}
