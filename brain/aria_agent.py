"""ARIA — the voice persona and team dispatcher.

think() is the brain: it takes the user's utterance (already transcribed),
reasons with the deepseek-r1 model, and either answers directly or calls a tool.
This module deliberately imports NO audio/HUD code so it can be exercised
text-only (scripts/test_aria.py) before any of that exists — the HUD is driven
through an injectable hook (set by main.py) rather than a hard import.

Tools:
  dispatch_to_agent(agent_name, task) — post the task to the teammate's channel
      as the user and fire their --once cycle in a background thread (fire-and-
      forget; wait-and-relay is Step 7). Respects DRY_RUN.
  read_channel(channel_name, limit)   — read recent messages from a team channel.
  check_pending_approvals()           — list #approvals items still pending.
  update_hud_state(state)             — drive the HUD via the injected hook.
"""
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import config
from brain.hermes_common import call_hermes, parse_tool_call, tools_block
from agents.base_agent import strip_reasoning
from integrations import slack_bus

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARIA_PROMPT_PATH = Path(PROJECT_ROOT) / "aria_system_prompt.md"

# ARIA addresses teammates by name; these map to their primary Slack channel.
_AGENT_CHANNELS = {"sarah": "support_intel", "tom": "dev", "ava": "content", "bobby": "ads"}


def load_aria_prompt():
    """Load ARIA's system prompt from aria_system_prompt.md at runtime."""
    return ARIA_PROMPT_PATH.read_text(encoding="utf-8").strip()


TOOLS = [
    {"name": "dispatch_to_agent",
     "description": ("Hand a task to a teammate who then DOES something: Sarah "
                     "(customer support), Tom (developer), Ava (content), Bobby "
                     "(ads). Posts the task to their channel and triggers their "
                     "work cycle. Use when the user asks you to have a teammate "
                     "act. For a status question about what a teammate already "
                     "found, use read_channel instead."),
     "parameters": {"type": "object", "properties": {
         "agent_name": {"type": "string", "enum": ["Sarah", "Tom", "Ava", "Bobby"]},
         "task": {"type": "string", "description": "What you're asking them to do."}},
         "required": ["agent_name", "task"]}},
    {"name": "read_channel",
     "description": ("Read recent messages from a team Slack channel to answer "
                     "status questions. Channels: feedback, support-intel, dev, "
                     "content, ads, approvals."),
     "parameters": {"type": "object", "properties": {
         "channel_name": {"type": "string"},
         "limit": {"type": "integer", "description": "Recent messages to read (default 10)."}},
         "required": ["channel_name"]}},
    {"name": "check_pending_approvals",
     "description": "List items in #approvals still awaiting the user's decision (pending only).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "update_hud_state",
     "description": ("Set the on-screen HUD state: idle, listening, thinking, or "
                     "speaking. Silent side-effect — never mention it aloud."),
     "parameters": {"type": "object", "properties": {
         "state": {"type": "string", "enum": ["idle", "listening", "thinking", "speaking"]}},
         "required": ["state"]}},
    {"name": "deep_summarize",
     "description": ("Summarize long or complex content using the deeper "
                     "reasoning model — a whole channel's recent messages or a "
                     "block of text — when a quick skim isn't enough. Slower; "
                     "use only when depth or accuracy really matters."),
     "parameters": {"type": "object", "properties": {
         "channel_or_text": {"type": "string", "description":
             "A channel name (feedback, support-intel, dev, content, ads, "
             "approvals) to read and summarize, or text to summarize directly."}},
         "required": ["channel_or_text"]}},
]


class Aria:
    name = "ARIA"
    model = config.AGENT_MODELS["ARIA"]                     # fast default
    reasoning_model = config.AGENT_MODELS["ARIA_reasoning"]  # deeper, slower

    def __init__(self, hud_hook=None):
        self.system_prompt = load_aria_prompt()
        # main.py injects hud.set_state here; text-only tests leave it None.
        self.hud_hook = hud_hook
        self.last_hud_state = None
        self.tool_handlers = {
            "dispatch_to_agent": self._dispatch_to_agent,
            "read_channel": self._read_channel,
            "check_pending_approvals": self._check_pending_approvals,
            "update_hud_state": self._update_hud_state,
            "deep_summarize": self._deep_summarize,
        }

    # --- The brain ----------------------------------------------------------
    def think(self, user_text, conversation_history=None, system_prompt=None,
              reasoning=False):
        """Reason over the user's utterance. Returns
        {reply, tool_calls, dispatched}. Runs on the fast model by default;
        pass reasoning=True to route the whole turn through the slow model when
        the request genuinely needs deeper thought."""
        model = self.reasoning_model if reasoning else self.model
        prompt = (system_prompt or self.system_prompt) + tools_block(TOOLS)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "system", "content": f"Current date and time: {datetime.now():%A %Y-%m-%d %H:%M:%S}."},
        ]
        messages += (conversation_history or [])
        messages.append({"role": "user", "content": user_text})

        tool_calls, dispatched = [], False
        reply = strip_reasoning(call_hermes(messages, model=model))
        for _ in range(6):  # bounded tool-call loop
            call = parse_tool_call(reply)
            if not call:
                break
            name = call.get("name")
            tool_calls.append(call)
            if name == "dispatch_to_agent":
                dispatched = True
            if name not in self.tool_handlers:
                reply = f"(skipped unknown tool {name})"
                break
            result = self.tool_handlers[name](call.get("arguments", {}) or {})
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "tool",
                             "content": json.dumps({"name": name, "content": str(result)})})
            reply = strip_reasoning(call_hermes(messages, model=model))

        return {"reply": reply, "tool_calls": tool_calls, "dispatched": dispatched}

    # --- Tools --------------------------------------------------------------
    def _dispatch_to_agent(self, args):
        agent = str(args.get("agent_name") or args.get("agent") or "").strip().lower()
        task = str(args.get("task") or args.get("message") or "").strip()
        if agent not in _AGENT_CHANNELS:
            return f"Unknown agent '{agent}'. Valid: Sarah, Tom, Ava, Bobby."
        channel = config.CHANNELS[_AGENT_CHANNELS[agent]]
        slack_bus.post_as("You", ":bust_in_silhouette:", channel,
                          f"[via ARIA] {task}" if task else "[via ARIA] (no task text)")
        self._fire_cycle_async(agent)
        return (f"Handed off to {agent.capitalize()}. Task posted to {channel}; "
                f"their cycle is running in the background.")

    def _fire_cycle_async(self, agent):
        """Fire the agent's --once cycle without blocking. Respects DRY_RUN."""
        if config.DRY_RUN:
            print(f"[DRY_RUN] would fire {agent}'s --once cycle in the background")
            return

        def _run():
            try:
                subprocess.run([sys.executable, os.path.join(PROJECT_ROOT, "daemon.py"),
                                "--once", agent], cwd=PROJECT_ROOT, capture_output=True)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _read_channel(self, args):
        raw = str(args.get("channel_name") or args.get("channel") or "").strip()
        key = raw.lstrip("#").replace("-", "_").lower()
        limit = int(args.get("limit", 10) or 10)
        cid = config.CHANNEL_IDS.get(key)
        if not cid:
            return f"Unknown/unconfigured channel '{raw}'. Known: {sorted(config.CHANNEL_IDS)}"
        texts = [t.strip() for t in slack_bus.read_channel(cid, limit=limit) if t and t.strip()]
        return "\n".join(f"- {t}" for t in texts) if texts else "(no messages in that channel)"

    def _check_pending_approvals(self, args):
        cid = config.CHANNEL_IDS.get("approvals")
        if not cid:
            return "Approvals channel is not configured (CHANNEL_ID_APPROVALS)."
        pending = []
        for m in slack_bus.history(cid, limit=50):
            text = m.get("text") or ""
            if "APPROVAL NEEDED" not in text:
                continue  # only real approval requests — skip joins/renames/mentions
            if slack_bus.check_approval(cid, m.get("ts")) == "pending":
                pending.append(text.splitlines()[0])
        if not pending:
            return "No pending approvals."
        return "Pending approvals:\n" + "\n".join(f"- {p}" for p in pending)

    def _update_hud_state(self, args):
        state = str(args.get("state", "")).strip()
        if state not in ("idle", "listening", "thinking", "speaking"):
            return f"Invalid HUD state '{state}'."
        if self.hud_hook:
            try:
                self.hud_hook(state)
            except Exception:
                pass
        self.last_hud_state = state
        return f"HUD -> {state}"

    def _deep_summarize(self, args):
        """Summarize a channel's recent messages or a text block using the
        deeper reasoning model (reasoning=True equivalent for this one task)."""
        target = str(args.get("channel_or_text") or args.get("channel")
                     or args.get("text") or "").strip()
        key = target.lstrip("#").replace("-", "_").lower()
        cid = config.CHANNEL_IDS.get(key)
        if cid:
            content = "\n".join(t for t in slack_bus.read_channel(cid, limit=30) if t.strip())
            label = f"#{target.lstrip('#')}"
        else:
            content, label = target, "the provided text"
        if not content.strip():
            return f"Nothing to summarize in {label}."
        messages = [
            {"role": "system", "content": "You are a precise summarizer. Give a "
             "concise, accurate summary — keep key points, decisions, and numbers "
             "exact; do not invent anything."},
            {"role": "user", "content": f"Summarize {label}:\n\n{content}"},
        ]
        return strip_reasoning(call_hermes(messages, model=self.reasoning_model))
