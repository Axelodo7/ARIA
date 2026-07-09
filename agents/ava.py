"""Ava — the content agent.

daily() reads Sarah's recent #support-intel digests for source material, asks
the reasoning model to turn ONE story into a short-form video plan (3 hooks, a
chosen hook with reasoning, the best-matching footage clip, a caption), renders
via the Remotion stub, posts the draft to #content (showing rejected hooks for
transparency), and files a scheduling approval to #approvals.

Nothing is ever published here. Scheduling only happens through
metricool.schedule_post, which is gated (DRY_RUN / approval / platform allowlist).
The model returns a plan; Python validates it and drives the pipeline.
"""
import os
import re
import time

import config
from agents.base_agent import BaseAgent, load_agent_prompt, strip_reasoning
from brain.hermes_common import _first_json_object
from integrations import slack_bus, gdrive, remotion_runner


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
        "must be a subset of the allowed list."
    )


def _pick_footage_fallback(footage, text):
    """If the model picks an invalid footage id, choose by tag/keyword overlap."""
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    def score(f):
        tag_words = set(re.findall(r"[a-z0-9]+", " ".join(f["tags"]).lower()))
        return len(tag_words & words)
    return max(footage, key=score)


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

        # --- Validate / normalize the model's choices (never trust blindly) ---
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
        if clip is None:  # model picked an invalid id — fall back to tag match
            clip = _pick_footage_fallback(footage, f"{story} {chosen_hook}")
            footage_id = clip["id"]
        footage_reasoning = str(plan.get("footage_reasoning", "")).strip()

        caption = str(plan.get("caption", "")).strip()
        hashtags = plan.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = hashtags.split()
        hashtags = [str(t) for t in hashtags]

        # Platforms: keep only allowed ones; default to the full allowlist.
        proposed = [p for p in (plan.get("platforms") or []) if p in config.METRICOOL_PLATFORM_ALLOWLIST]
        if not proposed:
            proposed = sorted(config.METRICOOL_PLATFORM_ALLOWLIST)

        # --- Download footage + render (both stubbed) -------------------------
        clip_path = os.path.join(config.RENDER_DIR, f"clip_{footage_id}.mp4")
        gdrive.download_clip(footage_id, clip_path)
        video_path = os.path.join(config.RENDER_DIR, f"ava_{int(time.time())}.mp4")
        remotion_runner.render(
            "CaptionedClip",
            {"hook": chosen_hook, "caption": caption, "clip": clip_path},
            video_path,
        )

        # --- Post the draft to #content (transparent: show rejected hooks) ----
        content_post = self._format_content_post(
            story, chosen_hook, plan.get("hook_reasoning", ""), rejected_hooks,
            clip, footage_reasoning, caption, hashtags, video_path, proposed,
        )
        self.post(content_post)

        # --- File a scheduling approval to #approvals -------------------------
        payload_id = f"ava-post-{footage_id}-{int(time.time())}"
        approval_desc = (
            f"TL;DR: schedule new video to {proposed}.\n"
            f"Story: {story}\nHook: \"{chosen_hook}\"\nCaption: {caption}\n"
            f"Video: {video_path} (footage {clip['filename']}) [{payload_id}]"
        )
        approval_ts = slack_bus.request_approval("Ava", approval_desc, payload_id)

        return {
            "story": story, "hooks": hooks, "chosen_hook_index": idx,
            "chosen_hook": chosen_hook, "rejected_hooks": rejected_hooks,
            "hook_reasoning": plan.get("hook_reasoning", ""),
            "footage": clip, "footage_reasoning": footage_reasoning,
            "caption": caption, "hashtags": hashtags, "platforms": proposed,
            "video_path": video_path, "payload_id": payload_id,
            "approval_ts": approval_ts, "content_post": content_post,
        }

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
