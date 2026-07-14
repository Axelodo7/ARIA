"""Ava — the content agent (business-context driven, pre-launch).

ZentraIQ has no real user feedback yet, so Ava's daily() cycle generates
promotional content from the business context (docs/user.md), NOT from
#support-intel. Each cycle she rotates to the next CONTENT_PILLAR (contrarian
claim -> the ZentraIQ Method -> pricing anchor -> build-in-public), so content
doesn't repeat; the last-used pillar is tracked in shared state.

Per cycle: write 3 brand-voice hooks, pick the strongest with reasoning, write a
brand-aligned abstract image prompt, generate the background via Pollinations,
render a branded story video (BrandedQuote, 1080x1920, handle shown), post the
draft to #content, and file a publish approval to #approvals.

Guardrails (all preserved):
  Level 1 (prompt): brand voice + hard "never" rules + no invented specifics.
  Level 2 (code): find_fabricated_claims() checks every numeric claim in the
    chosen hook + caption against docs/user.md; ungrounded numbers -> the plan is
    REFUSED (nothing posts) and quarantined to renders/rejected/.
Graceful fallback: Pollinations fails -> skip the cycle (never post a bg-less
  video); Remotion fails -> post the hook + caption as a text draft with a
  render-failed note and still file the copy for approval.
Nothing publishes without a ✅ — approval only marks a draft ready; Metricool
  scheduling stays stubbed.
"""
import json
import logging
import os
import re
import time

import config

logger = logging.getLogger("aria.ava")
from agents.base_agent import BaseAgent, load_agent_prompt, strip_reasoning
from brain.hermes_common import _first_json_object, load_business_context
from integrations import slack_bus, pollinations, remotion_runner
from memory import store


# --- Content pillars (rotation) ---------------------------------------------
# The four positioning angles Ava rotates through. Each MUST produce content
# distinct from the others — the briefs below are deliberately partitioned so the
# same message can't be said twice (the failure that prompted this: the agency
# price/speed comparison got reused across pillars). The agency comparison lives
# ONLY in Contrarian; the others own different territory.
CONTENT_PILLARS = [
    {"name": "Contrarian claim",
     "brief": "Challenge an industry ASSUMPTION and flip it. This is the ONE pillar "
              "that owns the agency price/speed/timeline comparison — use it here "
              "and NOWHERE else. Sharp and provocative, backed by real figures from "
              "the context. Example angle: \"Agencies quote 6 months. We ship in "
              "14 days.\""},
    {"name": "The ZentraIQ Method",
     "brief": "Describe HOW the 14-day build actually works — the AI-native workflow "
              "(built with the same Cursor / Claude Code / GPT we sell), tight "
              "honest scoping, the day-by-day process, ship-then-iterate. Make the "
              "PROCESS the story. Do NOT do a price or speed comparison and do NOT "
              "mention agencies. Use the REAL breakdown from the context — 2 days "
              "discovery, 8 days build, 4 days ship — never invent a different "
              "split. Example angle: \"Discovery: 2 days. Build: 8 days. Ship: 4 "
              "days. No scope creep, because we scope honestly.\""},
    {"name": "Pricing anchor",
     "brief": "Frame ONE tier as a value story by what you GET — the deliverables, "
              "the included AI feature, full code ownership. NO 'not $200K' / agency "
              "comparison; let the value stand on its own. Tiers (all fixed, no "
              "discounts): Launch $8K, Scale $15K, Flagship $23K, GalaxAI Builder "
              "$225/mo. Example angle: \"$8K. One core AI feature. Full code "
              "ownership. Yours in 14 days.\""},
    {"name": "Build-in-public",
     "brief": "The solo-founder-plus-AI-tools angle: the process itself as content, "
              "shipping in the open, behind-the-scenes, the honesty of building "
              "pre-launch. Not a comparison, not a price list — the story of how "
              "it's being built. Example angle: \"One founder. AI tools. Production "
              "SaaS in two weeks.\""},
]

# --- Distinctness / brand-voice backstop (Principle 1) ----------------------
# Deterministic checks on the CHOSEN hook — the prompt is the first line, this is
# the wall. Filler phrases are banned outright; the agency-comparison angle is
# reserved for the Contrarian pillar and rejected in any other.
_BANNED_FILLER = ("cut through the noise", "unlock", "transform", "revolutionize")
_AGENCY_MARKERS = ("agenc", "$200k", "200k", "6 month", "6-month", "six month")


def _contains_filler(hook):
    """Banned filler phrases present in `hook` (lowercased substring match)."""
    h = (hook or "").lower()
    return [p for p in _BANNED_FILLER if p in h]


def _contains_agency_markers(hook):
    """True if `hook` uses the agency price/speed/timeline comparison angle."""
    h = (hook or "").lower()
    return any(m in h for m in _AGENCY_MARKERS)


def _distinctness_violations(hook, pillar_name):
    """Reasons `hook` breaks the distinctness/brand rules, or [] if it's clean.
    Filler is always banned; agency-comparison markers are banned unless this is
    the Contrarian pillar (which owns that angle)."""
    problems = [f"banned filler '{p}'" for p in _contains_filler(hook)]
    if "contrarian" not in (pillar_name or "").lower() and _contains_agency_markers(hook):
        problems.append("agency-comparison angle (reserved for the Contrarian pillar)")
    return problems


def _hooks_of(plan):
    """The up-to-3 non-empty hook strings from a plan dict."""
    return [str(h) for h in (plan.get("hooks") or []) if str(h).strip()][:3]


def _chosen_index_of(plan, hooks):
    """The plan's chosen index, clamped to a valid position (default 0)."""
    idx = plan.get("chosen_hook_index", 0)
    return idx if isinstance(idx, int) and 0 <= idx < len(hooks) else 0


def _chosen_hook_of(plan):
    hooks = _hooks_of(plan)
    return hooks[_chosen_index_of(plan, hooks)] if hooks else ""


def _pick_clean_hook(plans, pillar_name):
    """First (plan, index) across `plans` whose hook has no violations, or None."""
    for plan in plans:
        for i, h in enumerate(_hooks_of(plan)):
            if not _distinctness_violations(h, pillar_name):
                return plan, i
    return None
# Shared-state key holding the last-used pillar index (separate from any ts
# bookmark). Ava no longer reads Slack for source material.
_PILLAR_STATE_KEY = "Ava:pillar"

# Appended to whatever image prompt the model writes, to guarantee the brand look
# even if the model's prompt is weak. Dark, technical, cyan/pink, abstract.
_BRAND_IMAGE_SUFFIX = (
    "dark deep black background, cyan and magenta-pink glowing accents, minimal, "
    "abstract, technical, high detail, cinematic, no text, no logos, no people"
)


# --- Fabrication detection (unchanged machinery, now grounded to user.md) ----
_UNIT = (r"hours?|hrs?|minutes?|mins?|seconds?|secs?|days?|weeks?|months?|years?|"
         r"users?|customers?|clients?|people|percent|dollars?|times?|x")
# A "numeric claim": a currency amount, a percentage, or a number followed by a
# unit word (3 hours, 4 users, 40%, $15).
_CLAIM_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?"                                 # $15, $1,000
    r"|\d[\d,]*(?:\.\d+)?\s?%"                                 # 40%, 40 %
    r"|\d[\d,]*(?:\.\d+)?[\s-]?(?:" + _UNIT + r")\b",          # 3 hours, 4-users
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Canonicalize unit words so "day"/"days" (etc.) compare equal. A numeric claim is
# grounded only if the SAME number appears in the source with a COMPATIBLE unit /
# currency / percent — not merely as a bare digit somewhere unrelated.
_UNIT_CANON = {
    "hour": "hour", "hours": "hour", "hr": "hour", "hrs": "hour",
    "minute": "minute", "minutes": "minute", "min": "minute", "mins": "minute",
    "second": "second", "seconds": "second", "sec": "second", "secs": "second",
    "day": "day", "days": "day", "week": "week", "weeks": "week",
    "month": "month", "months": "month", "year": "year", "years": "year",
    "user": "user", "users": "user", "customer": "customer", "customers": "customer",
    "client": "client", "clients": "client", "people": "people",
    "percent": "percent", "dollar": "dollar", "dollars": "dollar",
    "time": "time", "times": "time", "x": "x",
}


def _numbers_in(text):
    """Set of normalized number tokens present in text (commas stripped)."""
    return {m.group(0).replace(",", "") for m in _NUM_RE.finditer(text or "")}


def _claim_signature(claim):
    """A semantic signature for a numeric claim: currency / percent / (number,unit).
    Two claims with the same signature mean the same thing; a shared bare digit in
    different contexts does NOT. Returns None if there's no number."""
    c = (claim or "").strip().lower()
    num = _NUM_RE.search(c)
    if not num:
        return None
    n = num.group(0).replace(",", "")
    if c.startswith("$"):
        return ("cur", n)
    if "%" in c:
        return ("pct", n)
    unit_m = re.search(r"[a-z]+", c[num.end():])   # first word after the number
    unit = _UNIT_CANON.get(unit_m.group(0), unit_m.group(0)) if unit_m else ""
    return ("unit", unit, n)


def _source_signatures(source):
    """The set of semantic signatures for every numeric claim in the source."""
    sigs = set()
    for m in _CLAIM_RE.finditer(source or ""):
        sig = _claim_signature(m.group(0))
        if sig:
            sigs.add(sig)
    return sigs


def find_fabricated_claims(text, source):
    """Return numeric claims in `text` NOT grounded in `source` by a SEMANTICALLY
    COMPATIBLE match — the same number with a compatible currency / percent / unit
    context, not just the same digit appearing anywhere in the file.

    An empty list means every figure is properly grounded. A claim whose digit
    appears in the source ONLY in an unrelated context (e.g. "2 days" when the
    source's only 2 is "<2s load") is treated as ungrounded AND logged as a
    WARNING, so coincidental grounding is surfaced instead of silently allowed.
    """
    src_sigs = _source_signatures(source)
    src_nums = _numbers_in(source)
    offenders = []
    for m in _CLAIM_RE.finditer(text or ""):
        claim = m.group(0).strip()
        sig = _claim_signature(claim)
        if sig and sig in src_sigs:
            continue  # grounded in a compatible context — pass
        num = _NUM_RE.search(claim)
        digit = num.group(0).replace(",", "") if num else None
        if digit and digit in src_nums:
            # The digit exists in the source but only in an incompatible context.
            logger.warning(
                "coincidental grounding rejected: %r — the number %s appears in "
                "docs/user.md but NOT as '%s'; passing it would be a digit "
                "coincidence, not a grounded fact.", claim, digit, claim)
        offenders.append(claim)
    return offenders


def _brand_image_prompt(model_prompt):
    """Augment the model's image prompt with the fixed brand-style suffix."""
    base = (model_prompt or "").strip().rstrip(".")
    if not base:
        base = "abstract technical neural network"
    return f"{base}, {_BRAND_IMAGE_SUFFIX}"


def _regen_directive(problems, pillar):
    """A stronger appendix telling the model exactly what its previous chosen hook
    got wrong, to steer the single regeneration attempt."""
    d = ("\n\nYOUR PREVIOUS CHOSEN HOOK VIOLATED THE RULES: " + "; ".join(problems)
         + ". Rewrite ALL THREE hooks from scratch. Use NONE of these filler "
         "phrases: 'cut through the noise', 'unlock', 'transform', 'revolutionize'.")
    if "contrarian" not in pillar["name"].lower():
        d += (f" Because this is the '{pillar['name']}' pillar (NOT Contrarian), do "
              "NOT mention agencies, $200K, or 6 months, and do NOT make a "
              "price-vs-price or speed comparison.")
    d += " Stay strictly on this pillar's angle: " + pillar["brief"]
    return d


def _build_pillar_task(pillar, extra=""):
    not_contrarian = "contrarian" not in pillar["name"].lower()
    agency_line = (
        "This is NOT the Contrarian pillar: do NOT use any agency comparison or "
        "price-vs-price / speed-vs-speed framing (no 'agencies', no '$200K', no "
        "'6 months') — stay on THIS pillar's specific angle above.\n"
        if not_contrarian else
        "This IS the Contrarian pillar: the agency price/speed/timeline comparison "
        "belongs here — use it.\n"
    )
    return (
        f"Today's content pillar: {pillar['name']}.\n{pillar['brief']}\n\n"
        "Create ONE short-form promotional video for ZentraIQ's social channels, "
        "drawn ENTIRELY from the business context above — this is pre-launch "
        "positioning, there is no user feedback to draw on.\n\n"
        "Brand voice: direct, confident, slightly contrarian; short punchy "
        "sentences; data over adjectives; NO exclamation marks, NO emoji, NO "
        "hype. Sound like a smart engineer tired of agencies ripping founders off.\n\n"
        "DISTINCTNESS — Each pillar produces content DISTINCT from the others. "
        "The agency-comparison angle (agencies, $50K-$200K, 6-month timelines) "
        "belongs ONLY to the Contrarian pillar — never reuse it in another pillar. "
        "Never reuse the same core message across pillars. Avoid filler phrases "
        "('cut through the noise', 'unlock', 'transform', 'revolutionize') — every "
        "word earns its place.\n"
        f"{agency_line}\n"
        "Do your reasoning, then output ONLY a single JSON object — no markdown "
        "fences, no prose before or after — with EXACTLY these keys:\n"
        "{\n"
        '  "hooks": ["hook 1", "hook 2", "hook 3"],\n'
        '  "chosen_hook_index": 0,\n'
        '  "hook_reasoning": "one or two sentences: why this hook is strongest",\n'
        '  "image_prompt": "an ABSTRACT background prompt tied loosely to the '
        'hook theme — dark, technical, minimal, deep black, cyan/pink accents; '
        'NOT literal or stock-photo; no text, no logos, no people",\n'
        '  "caption": "a short first-person caption from the founder"\n'
        "}\n\n"
        "Rules: exactly 3 hooks, each a single punchy line (~12 words max). "
        "chosen_hook_index is 0, 1, or 2.\n"
        "GROUNDING — every number, price, timeframe, or claim MUST come from the "
        "business context: the $8K/$15K/$23K/$225 pricing, the 14-day build, and "
        "'traditional agencies charge $50K-$200K' are allowed. NEVER invent user "
        "counts, testimonials, revenue figures, growth stats, or timeframes that "
        "aren't in the context.\n"
        "HARD RULES — never: performance guarantees beyond the site's claims; "
        "'$50K MRR' / '100K users' style promises; uptime or certification claims; "
        "fabricated case studies or client names; the founder's age as a selling "
        "point; a competitor's specific pricing; a promised calendar date (always "
        "'14 days from kickoff'). No discounts — prices are fixed."
        f"{extra}"
    )


class Ava(BaseAgent):
    name = "Ava"
    icon = config.PERSONAS["Ava"]
    channel = config.CHANNELS["content"]
    model = config.AGENT_MODELS["Ava"]
    system_prompt = load_agent_prompt("Ava")
    # No model-callable tools: Ava proposes a plan; publishing is gated in code.

    # --- pillar rotation ----------------------------------------------------
    def _next_pillar_index(self):
        """The next pillar index, rotating through CONTENT_PILLARS."""
        raw = store.get_bookmark(_PILLAR_STATE_KEY)
        try:
            last = int(raw)
        except (TypeError, ValueError):
            last = -1
        return (last + 1) % len(CONTENT_PILLARS)

    def daily(self):
        """Generate one branded promotional video from the next content pillar and
        file it for approval. Returns a dict for tests/logging."""
        pillar_idx = self._next_pillar_index()
        pillar = CONTENT_PILLARS[pillar_idx]
        # Advance the rotation pointer immediately so pillars rotate every cycle,
        # regardless of whether Pollinations/Remotion later succeed.
        store.set_bookmark(_PILLAR_STATE_KEY, str(pillar_idx))
        print(f"[ava] pillar #{pillar_idx}: {pillar['name']}")

        plan = self._generate_valid_plan(pillar)
        return self._process_plan(plan, pillar)

    def _ask_for_plan(self, task):
        """One model call -> parsed JSON plan. Raises if it isn't parseable."""
        plan = _first_json_object(strip_reasoning(self.run_turn(task)))
        if not isinstance(plan, dict):
            raise RuntimeError("Ava did not return a parseable JSON plan.")
        return plan

    def _generate_valid_plan(self, pillar):
        """Generate a plan whose CHOSEN hook passes the distinctness/brand backstop
        (Principle 1 — don't trust the prompt alone). If the first chosen hook
        violates, regenerate once with a stronger directive; if that still fails,
        fall back to the best non-violating candidate hook across both attempts."""
        plan = self._ask_for_plan(_build_pillar_task(pillar))
        problems = _distinctness_violations(_chosen_hook_of(plan), pillar["name"])
        if not problems:
            return plan

        print(f"[ava] chosen hook violated rules {problems}; regenerating once.")
        plan2 = self._ask_for_plan(
            _build_pillar_task(pillar, extra=_regen_directive(problems, pillar)))
        if not _distinctness_violations(_chosen_hook_of(plan2), pillar["name"]):
            return plan2

        clean = _pick_clean_hook([plan2, plan], pillar["name"])
        if clean is not None:
            src_plan, idx = clean
            fallback = dict(src_plan)
            fallback["chosen_hook_index"] = idx
            print(f"[ava] regen still violated; fell back to a clean candidate hook.")
            return fallback

        print("[ava] no clean candidate hook available; using the regenerated plan.")
        return plan2

    def _process_plan(self, plan, pillar):
        """Validate a plan, run the fabrication gate, then generate + render +
        post (with graceful fallbacks). Split out so the refuse/fallback paths
        are testable with a hand-built plan."""
        hooks = [str(h) for h in (plan.get("hooks") or []) if str(h).strip()][:3]
        if not hooks:
            raise RuntimeError("Ava's plan contained no hooks.")
        idx = plan.get("chosen_hook_index", 0)
        if not isinstance(idx, int) or not (0 <= idx < len(hooks)):
            idx = 0
        chosen_hook = hooks[idx]
        rejected_hooks = [h for i, h in enumerate(hooks) if i != idx]
        hook_reasoning = str(plan.get("hook_reasoning", "")).strip()
        caption = str(plan.get("caption", "")).strip()
        image_prompt = _brand_image_prompt(str(plan.get("image_prompt", "")))

        base = {"pillar": pillar["name"], "hooks": hooks,
                "chosen_hook_index": idx, "chosen_hook": chosen_hook,
                "rejected_hooks": rejected_hooks, "hook_reasoning": hook_reasoning,
                "caption": caption, "image_prompt": image_prompt}

        # --- Level-2 fabrication gate (before any generation/post) -----------
        source = load_business_context()
        offenders = find_fabricated_claims(f"{chosen_hook} {caption}", source)
        if offenders:
            reason = (f"Fabricated specifics not grounded in the business context: "
                      f"{offenders}. Ava's prompt forbids inventing numbers/claims.")
            rejected_path = self._quarantine(plan, source, offenders, reason)
            print(f"[ava] REFUSED to post — {reason}")
            print(f"[ava] quarantined plan -> {rejected_path}")
            return {**base, "rejected": True, "reason": reason,
                    "offenders": offenders, "rejected_path": rejected_path}

        # --- Background via Pollinations; fail -> skip the cycle --------------
        bg_path = pollinations.generate_image(image_prompt, width=1080, height=1920)
        if not bg_path:
            msg = "Pollinations background failed — skipping this cycle (no bg-less video)."
            print(f"[ava] {msg}")
            return {**base, "rejected": False, "skipped": True, "reason": msg,
                    "background_path": None, "video_path": None}

        # --- Render the branded story video; fail -> text draft + approval ----
        out_dir = os.path.join(config.RENDER_DIR, "branded")
        video_path = os.path.join(out_dir, f"ava_{int(time.time())}.mp4")
        rendered = remotion_runner.render_branded_video(
            hook_text=chosen_hook, background_path=bg_path, format="story",
            output_path=video_path, show_handle=True)
        render_failed = rendered is None

        content_post = self._format_content_post(
            pillar, chosen_hook, hook_reasoning, rejected_hooks, image_prompt,
            caption, rendered, render_failed)
        self.post(content_post)

        payload_id = f"ava-post-{int(time.time())}"
        if render_failed:
            approval_desc = (
                f"TL;DR: schedule copy ({pillar['name']}) — VIDEO RENDER FAILED, "
                f"copy only.\nHook: \"{chosen_hook}\"\nCaption: {caption} [{payload_id}]")
        else:
            approval_desc = (
                f"TL;DR: schedule branded story video ({pillar['name']}).\n"
                f"Hook: \"{chosen_hook}\"\nCaption: {caption}\n"
                f"Video: {rendered} [{payload_id}]")
        approval_ts = slack_bus.request_approval("Ava", approval_desc, payload_id)
        # Stage 1 memory: record the filed publish proposal (Python-written summary).
        store.add_memory("Ava", "proposal_filed",
                         f"Proposed publish content ({pillar['name']})"
                         + (" [render failed]" if render_failed else ""), payload_id)

        return {**base, "rejected": False, "skipped": False,
                "background_path": bg_path, "video_path": rendered,
                "render_failed": render_failed, "payload_id": payload_id,
                "approval_ts": approval_ts, "content_post": content_post}

    @staticmethod
    def _quarantine(plan, source, offenders, reason):
        """Write a refused plan to renders/rejected/ for later inspection."""
        rej_dir = os.path.join(config.RENDER_DIR, "rejected")
        os.makedirs(rej_dir, exist_ok=True)
        path = os.path.join(rej_dir, f"rejected_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"reason": reason, "offenders": offenders,
                       "source": source, "plan": plan}, f, indent=2, ensure_ascii=False)
        return path

    def _format_content_post(self, pillar, chosen_hook, hook_reasoning, rejected,
                             image_prompt, caption, video_path, render_failed):
        rejected_block = "\n".join(f"   • \"{h}\"" for h in rejected) or "   (none)"
        if render_failed:
            video_line = (":warning: *Render failed* — copy filed for approval as a "
                          "text draft; video to follow.")
        else:
            video_line = f"*Rendered (story 1080x1920):* {video_path}"
        return (
            f":clapper: *New content draft* — pillar: *{pillar['name']}*\n\n"
            f"*Hook (chosen):* \"{chosen_hook}\"\n"
            f"   ↳ why: {hook_reasoning}\n"
            f"*Rejected hooks:*\n{rejected_block}\n\n"
            f"*Caption:* {caption}\n"
            f"*Image prompt:* {image_prompt}\n"
            f"{video_line}\n\n"
            f"_Filed for approval — see #approvals. Nothing publishes without a ✅._"
        )
