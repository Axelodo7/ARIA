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
import os
import re
import time

import config
from agents.base_agent import BaseAgent, load_agent_prompt, strip_reasoning
from brain.hermes_common import _first_json_object, load_business_context
from integrations import slack_bus, pollinations, remotion_runner
from memory import store


# --- Content pillars (rotation) ---------------------------------------------
# The four positioning angles Ava rotates through. The actual hooks are written
# by the model FROM the business context (already in Ava's system prompt); these
# briefs just steer which angle each cycle takes.
CONTENT_PILLARS = [
    {"name": "Contrarian claim",
     "brief": "Challenge how traditional agencies work — 6-month timelines, "
              "$50K-$200K quotes, scope creep — against ZentraIQ's 14-day, "
              "fixed-price, AI-native model. Sharp, provocative, backed by the "
              "real pricing/claims from the context."},
    {"name": "The ZentraIQ Method",
     "brief": "Break down HOW the 14-day AI-native build actually works — tight "
              "scoping, building with the same AI tools (Cursor, Claude Code, "
              "GPT) that are sold, ship-then-iterate. Make the process the story."},
    {"name": "Pricing anchor",
     "brief": "Tell the tier structure as a value story: Launch $8K, Scale $15K, "
              "Flagship $23K, GalaxAI Builder $225/mo — anchored against the "
              "$50K-$200K traditional agencies charge. Fixed price, no discounts."},
    {"name": "Build-in-public",
     "brief": "The solo founder + AI tools shipping fast narrative — behind the "
              "scenes, code, deployments, the honesty of building in public. "
              "Compelling because it's real and pre-launch."},
]
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


def _numbers_in(text):
    """Set of normalized number tokens present in text (commas stripped)."""
    return {m.group(0).replace(",", "") for m in _NUM_RE.finditer(text or "")}


def find_fabricated_claims(text, source):
    """Return numeric claims in `text` whose number does NOT appear in `source`.

    An empty list means every number/figure in `text` is grounded in the source
    (docs/user.md). Non-empty means the model invented a specific that isn't there.
    """
    src_nums = _numbers_in(source)
    offenders = []
    for m in _CLAIM_RE.finditer(text or ""):
        claim = m.group(0)
        num = _NUM_RE.search(claim)
        if num and num.group(0).replace(",", "") not in src_nums:
            offenders.append(claim.strip())
    return offenders


def _brand_image_prompt(model_prompt):
    """Augment the model's image prompt with the fixed brand-style suffix."""
    base = (model_prompt or "").strip().rstrip(".")
    if not base:
        base = "abstract technical neural network"
    return f"{base}, {_BRAND_IMAGE_SUFFIX}"


def _build_pillar_task(pillar):
    return (
        f"Today's content pillar: {pillar['name']}.\n{pillar['brief']}\n\n"
        "Create ONE short-form promotional video for ZentraIQ's social channels, "
        "drawn ENTIRELY from the business context above — this is pre-launch "
        "positioning, there is no user feedback to draw on.\n\n"
        "Brand voice: direct, confident, slightly contrarian; short punchy "
        "sentences; data over adjectives; NO exclamation marks, NO emoji, NO "
        "hype. Sound like a smart engineer tired of agencies ripping founders off.\n\n"
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

        plan = _first_json_object(strip_reasoning(self.run_turn(_build_pillar_task(pillar))))
        if not isinstance(plan, dict):
            raise RuntimeError("Ava did not return a parseable JSON plan.")
        return self._process_plan(plan, pillar)

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
