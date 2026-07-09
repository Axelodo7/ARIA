"""Ava — the content agent.

daily() reads Sarah's recent #support-intel digests for source material, asks
the reasoning model to turn ONE story into a short-form video plan (3 hooks, a
chosen hook with reasoning, the best-matching footage clip, a caption), validates
the plan, renders via the Remotion stub, posts the draft to #content (showing
rejected hooks for transparency), and files a scheduling approval to #approvals.

Anti-fabrication is belt-AND-suspenders, like the money guardrails:
  Level 1 (prompt): the model is told never to introduce a number/figure not
    verbatim in the source digest, and that hashtags must be single tokens.
  Level 2 (code): before anything posts, find_fabricated_claims() checks every
    numeric claim in the chosen hook + caption against the source text. If any
    number isn't in the source, the plan is REFUSED (nothing posts), and the
    plan is quarantined to renders/rejected/ with the reason.

Nothing is ever published here. Scheduling only happens through
metricool.schedule_post, which is separately gated.
"""
import json
import os
import re
import time

import config
from agents.base_agent import BaseAgent, load_agent_prompt, strip_reasoning
from brain.hermes_common import _first_json_object
from integrations import slack_bus, gdrive, remotion_runner


# --- Fabrication detection --------------------------------------------------
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
    digest. Non-empty means the model invented a specific that isn't there.
    """
    src_nums = _numbers_in(source)
    offenders = []
    for m in _CLAIM_RE.finditer(text or ""):
        claim = m.group(0)
        num = _NUM_RE.search(claim)
        if num and num.group(0).replace(",", "") not in src_nums:
            offenders.append(claim.strip())
    return offenders


def _fix_hashtag(tag):
    """Force a hashtag to a single token: strip internal whitespace, ensure a
    leading '#'."""
    t = re.sub(r"\s+", "", str(tag))
    if t and not t.startswith("#"):
        t = "#" + t
    return t


def _build_plan_task(source_material, footage):
    footage_lines = "\n".join(
        f"- id={f['id']} | {f['filename']} | {f['duration_sec']}s | tags={f['tags']}"
        for f in footage
    )
    allowed = sorted(config.METRICOOL_PLATFORM_ALLOWLIST)
    return (
        "SOURCE MATERIAL — recent #support-intel digests from Sarah (URGENT / "
        "BUGS / FEATURE REQUESTS / STORIES & PRAISE). Choose ONE story worth "
        "turning into a short-form video:\n\n"
        f"{source_material}\n\n"
        "AVAILABLE FOOTAGE — choose the ONE clip whose tags best fit your story:\n"
        f"{footage_lines}\n\n"
        f"Allowed platforms (choose a subset): {allowed}\n\n"
        "Do your reasoning, then output ONLY a single JSON object — no markdown "
        "fences, no prose before or after — with EXACTLY these keys:\n"
        "{\n"
        '  "story": "one sentence: the moment you are turning into content",\n'
        '  "hooks": ["hook 1", "hook 2", "hook 3"],\n'
        '  "chosen_hook_index": 0,\n'
        '  "hook_reasoning": "why this hook wins",\n'
        '  "footage_id": "an id from the list above",\n'
        '  "footage_reasoning": "why this clip fits the story",\n'
        '  "caption": "short first-person caption from the founder",\n'
        '  "hashtags": ["#tag1", "#tag2"],\n'
        f'  "platforms": {allowed}\n'
        "}\n\n"
        "Rules: exactly 3 hooks, each specific and concrete (not 'we work hard "
        "for you'); chosen_hook_index is 0, 1, or 2; footage_id MUST be one of "
        "the ids listed; never fabricate user quotes, metrics, or testimonials — "
        "use only what's in the source material, and anonymize users; platforms "
        "must be a subset of the allowed list.\n"
        "CRITICAL — no invented specifics: You must never introduce a number, "
        "duration, statistic, timeframe, dollar figure, percentage, count, or "
        "specific proper noun that is not verbatim in the source digest. If the "
        "source says 'hours', write 'hours' — never a specific figure like "
        "'3 hours'. If the source says 'multiple users', write 'multiple users' "
        "— never '4 users'. When in doubt, quote the source's exact language. "
        "Hashtags must be single tokens with no spaces (e.g. '#timeSaver' not "
        "'#time saver')."
    )


class Ava(BaseAgent):
    name = "Ava"
    icon = config.PERSONAS["Ava"]
    channel = config.CHANNELS["content"]
    model = config.AGENT_MODELS["Ava"]
    system_prompt = load_agent_prompt("Ava")
    # No model-callable tools: Ava proposes a plan; publishing is gated in code.

    def daily(self):
        """Turn a #support-intel story into a content draft; post to #content and
        file a scheduling approval. Returns a dict of everything for tests/logging."""
        si_id = config.CHANNEL_IDS.get("support_intel")
        if not si_id:
            raise RuntimeError(
                "CHANNEL_ID_SUPPORT_INTEL is not set in .env — read_channel "
                "needs a channel ID, not '#support-intel'."
            )
        digests = slack_bus.read_channel(si_id, limit=10)
        source = "\n\n".join(d for d in digests if d and d.strip()) or "(no recent digests)"
        footage = gdrive.list_footage()

        plan = _first_json_object(strip_reasoning(self.run_turn(_build_plan_task(source, footage))))
        if not isinstance(plan, dict):
            raise RuntimeError("Ava did not return a parseable JSON plan.")
        return self._process_plan(plan, source, footage)

    def _process_plan(self, plan, source, footage):
        """Validate a plan, then either post it (clean) or refuse it (fabrication).

        Split out from daily() so the refuse path is testable with a hand-built
        plan, without needing the model to actually misbehave.
        """
        # --- Normalize / validate the model's choices ------------------------
        hooks = [str(h) for h in (plan.get("hooks") or []) if str(h).strip()][:3]
        if not hooks:
            raise RuntimeError("Ava's plan contained no hooks.")
        idx = plan.get("chosen_hook_index", 0)
        if not isinstance(idx, int) or not (0 <= idx < len(hooks)):
            idx = 0
        chosen_hook = hooks[idx]
        rejected_hooks = [h for i, h in enumerate(hooks) if i != idx]

        story = str(plan.get("story", "")).strip()
        footage_id = plan.get("footage_id")
        clip = gdrive.get_footage(footage_id) if footage_id else None
        if clip is None:  # invalid id — fall back to tag/keyword match
            clip = self._pick_footage_fallback(footage, f"{story} {chosen_hook}")
            footage_id = clip["id"]
        footage_reasoning = str(plan.get("footage_reasoning", "")).strip()

        caption = str(plan.get("caption", "")).strip()
        hashtags = plan.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = hashtags.split()
        hashtags = [_fix_hashtag(t) for t in hashtags if str(t).strip()]

        proposed = [p for p in (plan.get("platforms") or []) if p in config.METRICOOL_PLATFORM_ALLOWLIST]
        if not proposed:
            proposed = sorted(config.METRICOOL_PLATFORM_ALLOWLIST)

        # --- Level-2 fabrication gate (before any render/post) ---------------
        offenders = find_fabricated_claims(f"{chosen_hook} {caption}", source)
        if offenders:
            reason = (f"Fabricated specifics not in source digest: {offenders}. "
                      f"Ava's prompt forbids inventing numbers/figures.")
            rejected_path = self._quarantine(plan, source, offenders, reason)
            print(f"[ava] REFUSED to post — {reason}")
            print(f"[ava] quarantined plan -> {rejected_path}")
            return {
                "rejected": True, "reason": reason, "offenders": offenders,
                "rejected_path": rejected_path, "story": story,
                "chosen_hook": chosen_hook, "caption": caption,
                "hooks": hooks, "chosen_hook_index": idx,
            }

        # --- Clean: download + render (both stubbed) -------------------------
        clip_path = os.path.join(config.RENDER_DIR, f"clip_{footage_id}.mp4")
        gdrive.download_clip(footage_id, clip_path)
        video_path = os.path.join(config.RENDER_DIR, f"ava_{int(time.time())}.mp4")
        remotion_runner.render(
            "CaptionedClip",
            {"hook": chosen_hook, "caption": caption, "clip": clip_path},
            video_path,
        )

        # --- Post the draft + file the scheduling approval -------------------
        content_post = self._format_content_post(
            story, chosen_hook, plan.get("hook_reasoning", ""), rejected_hooks,
            clip, footage_reasoning, caption, hashtags, video_path, proposed,
        )
        self.post(content_post)

        payload_id = f"ava-post-{footage_id}-{int(time.time())}"
        approval_desc = (
            f"TL;DR: schedule new video to {proposed}.\n"
            f"Story: {story}\nHook: \"{chosen_hook}\"\nCaption: {caption}\n"
            f"Video: {video_path} (footage {clip['filename']}) [{payload_id}]"
        )
        approval_ts = slack_bus.request_approval("Ava", approval_desc, payload_id)

        return {
            "rejected": False, "story": story, "hooks": hooks,
            "chosen_hook_index": idx, "chosen_hook": chosen_hook,
            "rejected_hooks": rejected_hooks,
            "hook_reasoning": plan.get("hook_reasoning", ""),
            "footage": clip, "footage_reasoning": footage_reasoning,
            "caption": caption, "hashtags": hashtags, "platforms": proposed,
            "video_path": video_path, "payload_id": payload_id,
            "approval_ts": approval_ts, "content_post": content_post,
        }

    @staticmethod
    def _pick_footage_fallback(footage, text):
        words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
        def score(f):
            tag_words = set(re.findall(r"[a-z0-9]+", " ".join(f["tags"]).lower()))
            return len(tag_words & words)
        return max(footage, key=score)

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

    def _format_content_post(self, story, chosen_hook, hook_reasoning, rejected,
                             clip, footage_reasoning, caption, hashtags,
                             video_path, platforms):
        rejected_block = "\n".join(f"   • \"{h}\"" for h in rejected) or "   (none)"
        tags = " ".join(hashtags)
        return (
            f":clapper: *New content draft* — {story}\n\n"
            f"*Hook (chosen):* \"{chosen_hook}\"\n"
            f"   ↳ why: {hook_reasoning}\n"
            f"*Rejected hooks:*\n{rejected_block}\n\n"
            f"*Footage:* {clip['filename']} [{clip['id']}] — tags {clip['tags']}\n"
            f"   ↳ why: {footage_reasoning}\n\n"
            f"*Caption:* {caption}\n"
            f"*Hashtags:* {tags}\n"
            f"*Rendered:* {video_path}\n"
            f"*Proposed platforms:* {platforms}\n\n"
            f"_Filed for approval — see #approvals. Nothing publishes without a ✅._"
        )
