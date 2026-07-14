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
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import config
from brain.hermes_common import (call_hermes, parse_tool_call, tools_block,
                                 business_context_block, cap_context,
                                 append_business_fact)
from agents.base_agent import strip_reasoning
from integrations import slack_bus

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARIA_PROMPT_PATH = Path(PROJECT_ROOT) / "aria_system_prompt.md"

# Dispatch posts the task to the channel that teammate works from.
_AGENT_CHANNELS = {"sarah": "feedback", "tom": "dev", "ava": "support_intel", "bobby": "ads"}

# Tools whose output is STRUCTURED — ARIA speaks a Python-formatted version
# verbatim rather than letting the fast model rephrase (and possibly fabricate).
_STRUCTURED_RELAY = {"check_pending_approvals", "read_channel", "remember"}

# Explicit "remember this" phrasing -> save a business-memory fact deterministically
# (Stage 2), no model call. Matches "remember [that] X", "note [that] X",
# "make a note [that] X", tolerating a leading "hey jarvis" / "please".
_REMEMBER_RE = re.compile(
    r"^\s*(?:hey\s+jarvis[\s,]*)?(?:please\s+)?"
    r"(?:remember|make a note|note)\b(?:\s+that)?[\s,:]+(.+)",
    re.IGNORECASE | re.DOTALL)


def _parse_remember(text):
    """Return the fact text if `text` is an explicit remember/note command, else
    None. The fact is everything after the trigger verb."""
    m = _REMEMBER_RE.search(text or "")
    if not m:
        return None
    fact = m.group(1).strip().strip("\"'").strip()
    return fact or None

# Slack system events that are never real content (joins, topic changes, ...).
# NOTE: bot_message is deliberately NOT here — those are the agents' own posts.
_SYSTEM_SUBTYPES = {"channel_join", "channel_leave", "channel_topic",
                    "channel_purpose", "channel_name", "channel_archive",
                    "channel_unarchive"}

# Explicit dispatch phrasing ("ask Sarah ...", "tell Bobby to ...", "have Ava
# ...", "get Tom to ..."). Group 1 = agent, group 2 = the task text after it.
# Handled deterministically in code — no model call at all.
_DISPATCH_RE = re.compile(
    r"\b(?:ask|tell|have|get)\s+(sarah|bobby|ava|tom)\b(?:\s+to\b)?[\s,:\-]*(.*)",
    re.IGNORECASE | re.DOTALL)


def _parse_dispatch(text):
    """Return (agent, task) if the text is an explicit dispatch, else None."""
    m = _DISPATCH_RE.search(text or "")
    if not m:
        return None
    return m.group(1).lower(), m.group(2).strip()


def _truncate_sentences(text, n=2):
    """Collapse whitespace and keep at most the first n sentences."""
    flat = " ".join((text or "").split())
    sentences = re.split(r"(?<=[.!?])\s+", flat)
    out = " ".join(sentences[:n]).strip()
    return out + (" …" if len(sentences) > n else "")


# Status reads relay from the channel where each teammate posts their WORK.
_AGENT_STATUS_CHANNELS = {"sarah": "support_intel", "bobby": "ads",
                          "ava": "content", "tom": "dev"}
_AGENTS_ALT = r"(sarah|bobby|ava|tom)"

# "What did Bobby find/do/say", "how's Ava", "any update from Tom", etc.
_STATUS_RES = [
    re.compile(rf"what did {_AGENTS_ALT}\b\s+(?:find|do|say)", re.IGNORECASE),
    re.compile(rf"how'?s\s+{_AGENTS_ALT}\b", re.IGNORECASE),
    re.compile(rf"how is\s+{_AGENTS_ALT}\b", re.IGNORECASE),
    re.compile(rf"any update from\s+{_AGENTS_ALT}\b", re.IGNORECASE),
    re.compile(rf"anything from\s+{_AGENTS_ALT}\b", re.IGNORECASE),
    re.compile(rf"what(?:'?s| is)\s+{_AGENTS_ALT}\s+working on", re.IGNORECASE),
]

# "What's in #ads", "anything in #dev", "how's #content looking".
_CHANNEL_RES = [
    re.compile(r"what'?s in\s+#?([a-z][a-z0-9\-_]*)", re.IGNORECASE),
    re.compile(r"anything in\s+#?([a-z][a-z0-9\-_]*)", re.IGNORECASE),
    re.compile(r"how'?s\s+#?([a-z][a-z0-9\-_]*)\s+looking", re.IGNORECASE),
]


def _parse_status_query(text):
    """Return the agent name for an explicit 'what did X find'-style query."""
    for rx in _STATUS_RES:
        m = rx.search(text or "")
        if m:
            return m.group(1).lower()
    return None


def _parse_channel_query(text):
    """Return a channel key for a 'what's in #X'-style query, else None."""
    for rx in _CHANNEL_RES:
        m = rx.search(text or "")
        if m:
            key = m.group(1).lstrip("#").replace("-", "_").lower()
            if key in config.CHANNEL_IDS:
                return key
    return None


# Short utterances that are legitimate (greetings, yes/no, ARIA's name) and must
# NOT be treated as garble — they pass through to the model.
_COMMON_SHORT = {"hi", "hey", "hello", "yo", "aria", "jarvis", "thanks", "thank",
                 "yes", "no", "yeah", "yep", "nope", "ok", "okay", "hmm", "sup",
                 "morning", "evening", "please", "stop", "wait", "hey aria"}


def _looks_unintelligible(text):
    """Heuristic: a very short fragment that matched no route is almost certainly
    garbled transcription / noise. Return True to ask the user to repeat rather
    than let the model free-associate a hallucinated answer (Bug 2). Conservative:
    only the clearly-too-short cases, and recognizable greetings/common words are
    let through. ARIA's system prompt handles longer-but-incoherent input by
    instructing her to ask for clarification."""
    t = (text or "").strip()
    if not t:
        return True
    words = t.split()
    if len(words) <= 2 and len(t) <= 12:
        toks = [re.sub(r"[^\w]", "", w.lower()) for w in words]
        toks = [tok for tok in toks if tok]
        # Let it through only if EVERY token is a recognizable common word.
        return not (toks and all(tok in _COMMON_SHORT for tok in toks))
    return False


def _truncate_status(text, max_chars=300):
    """Collapse whitespace and keep a short spoken excerpt. Slightly longer than
    2 sentences because agents (e.g. Bobby) put the key ids after their summary."""
    flat = " ".join((text or "").split())
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars].rsplit(" ", 1)[0] + " …"


def _approval_tldr(text):
    """A short label for an approval request, e.g. 'scale cr_A' from its id."""
    m = re.search(r"\[`([^`]+)`\]", text)
    if m:
        bits = m.group(1).split("-")
        core = bits[1:-1] if (len(bits) > 2 and bits[-1].isdigit()) else bits[1:]
        if core:
            return " ".join(core)
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith("tl;dr"):
            return s.split(":", 1)[1].strip()[:50]
    return "a proposal"


def warmup(model=None):
    """Fire a tiny prompt so the fast model is resident before the first real
    call (avoids a cold-start on the user's first utterance)."""
    try:
        call_hermes([{"role": "user", "content": "ok"}],
                    model=model or config.AGENT_MODELS["ARIA"],
                    num_predict=1, keep_alive="30m")
    except Exception:
        pass


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
    {"name": "remember",
     "description": ("Save a durable fact about the business (ZentraIQ) that the "
                     "whole team should know going forward — a milestone, a "
                     "launched feature, a policy change. Use when the user tells "
                     "you to remember or note something. Python timestamps and "
                     "stores it; you provide only the fact text."),
     "parameters": {"type": "object", "properties": {
         "fact": {"type": "string", "description": "The fact to remember, one short sentence."}},
         "required": ["fact"]}},
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
        # Whether dispatch actually fires the agent's --once cycle. True in
        # production; tests can set False to post-without-fire for verification.
        self.fire_cycles = True
        self.tool_handlers = {
            "dispatch_to_agent": self._dispatch_to_agent,
            "read_channel": self._read_channel,
            "check_pending_approvals": self._check_pending_approvals,
            "update_hud_state": self._update_hud_state,
            "deep_summarize": self._deep_summarize,
            "remember": self._remember,
        }

    # --- The brain ----------------------------------------------------------
    def think(self, user_text, conversation_history=None, system_prompt=None,
              reasoning=False):
        """Reason over the user's utterance. Returns {reply, tool_calls,
        dispatched}. Fast model by default; pass reasoning=True to route the
        whole turn through the slow model when depth is genuinely needed."""
        # Stage 2 — explicit "remember/note that X" saves a business-memory fact
        # deterministically (Python writes the timestamped line), no model call.
        fact = _parse_remember(user_text)
        if fact:
            reply = self._remember({"fact": fact})
            return {"reply": reply, "dispatched": False,
                    "tool_calls": [{"name": "remember", "arguments": {"fact": fact}}]}

        # Fix #1 — explicit dispatch is deterministic: extract agent + task and
        # dispatch directly, no model call at all. Instant and always correct.
        parsed = _parse_dispatch(user_text)
        if parsed:
            agent, task = parsed
            self._dispatch_to_agent({"agent_name": agent, "task": task})
            return {"reply": f"Handed off to {agent.capitalize()}.",
                    "tool_calls": [{"name": "dispatch_to_agent",
                                    "arguments": {"agent_name": agent.capitalize(), "task": task}}],
                    "dispatched": True}

        # Deterministic status query ("what did Bobby find") — read that agent's
        # channel directly, no model call, so it can never fabricate.
        agent = _parse_status_query(user_text)
        if agent:
            ch = _AGENT_STATUS_CHANNELS[agent]
            return {"reply": self._status_read(agent), "dispatched": False,
                    "tool_calls": [{"name": "read_channel",
                                    "arguments": {"channel_name": ch, "limit": 5}}]}

        # Deterministic channel-direct query ("what's in #ads"). Approvals routes
        # to the pending-approvals relay; other channels relay their last message.
        ckey = _parse_channel_query(user_text)
        if ckey:
            if ckey == "approvals":
                return {"reply": self._check_pending_approvals({}), "dispatched": False,
                        "tool_calls": [{"name": "check_pending_approvals", "arguments": {}}]}
            return {"reply": self._read_channel({"channel_name": ckey}), "dispatched": False,
                    "tool_calls": [{"name": "read_channel", "arguments": {"channel_name": ckey}}]}

        # Garbled / unintelligible very-short input that matched no route — ask the
        # user to repeat rather than free-associating a hallucinated answer (Bug 2).
        if _looks_unintelligible(user_text):
            return {"reply": "I didn't catch that, sir. Could you say that again?",
                    "tool_calls": [], "dispatched": False}

        model = self.reasoning_model if reasoning else self.model
        # Fix #2 — hard brevity cap on the fast path; keep the model warm.
        num_predict = None if reasoning else 64
        keep_alive = None if reasoning else "30m"

        # Business context first, then ARIA's persona, then the tool-call format.
        prompt = (business_context_block()
                  + (system_prompt or self.system_prompt) + tools_block(TOOLS))
        messages = [
            {"role": "system", "content": prompt},
            {"role": "system", "content": f"Current date and time: {datetime.now():%A %Y-%m-%d %H:%M:%S}."},
        ]
        messages += (conversation_history or [])
        messages.append({"role": "user", "content": user_text})

        tool_calls, dispatched = [], False
        reply = strip_reasoning(call_hermes(messages, model=model, num_predict=num_predict, keep_alive=keep_alive))
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
            # Fix #3 — structured tools speak their Python-formatted result
            # verbatim; the model never rephrases it (no fabrication).
            if name in _STRUCTURED_RELAY:
                return {"reply": str(result), "tool_calls": tool_calls, "dispatched": dispatched}
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "tool",
                             "content": json.dumps({"name": name, "content": str(result)})})
            reply = strip_reasoning(call_hermes(messages, model=model, num_predict=num_predict, keep_alive=keep_alive))

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
        """Fire the agent's --once cycle without blocking. Skips when DRY_RUN or
        fire_cycles is off (post-without-fire for verification)."""
        if config.DRY_RUN or not self.fire_cycles:
            print(f"[skip-fire] {agent}'s --once cycle not fired "
                  f"(dry_run={config.DRY_RUN}, fire_cycles={self.fire_cycles})")
            return

        def _run():
            try:
                subprocess.run([sys.executable, os.path.join(PROJECT_ROOT, "daemon.py"),
                                "--once", agent], cwd=PROJECT_ROOT, capture_output=True)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _status_read(self, agent):
        """Relay the most recent message AUTHORED BY that agent from their work
        channel. Deterministic — no model, so it can't fabricate."""
        name = agent.capitalize()
        cid = config.CHANNEL_IDS.get(_AGENT_STATUS_CHANNELS[agent])
        if not cid:
            return f"I can't reach {name}'s channel right now."
        for m in slack_bus.history(cid, limit=5):
            if m.get("username") != name:
                continue  # skip joins, mentions, other authors — only this agent
            text = (m.get("text") or "").strip()
            if not text:
                continue
            return f"Most recent from {name}: {_truncate_status(text)}"
        return f"Nothing recent from {name}."

    def _read_channel(self, args):
        """Relay the MOST RECENT message from a channel verbatim (2 sentences),
        so nothing is paraphrased or invented."""
        raw = str(args.get("channel_name") or args.get("channel") or "").strip()
        key = raw.lstrip("#").replace("-", "_").lower()
        cid = config.CHANNEL_IDS.get(key)
        if not cid:
            return f"I don't have a channel called '{raw}'."
        for m in slack_bus.history(cid, limit=10):  # newest first
            text = (m.get("text") or "").strip()
            if not text or m.get("subtype") in _SYSTEM_SUBTYPES:
                continue  # skip empty / system (join/leave) messages
            if re.fullmatch(r"(?:<@\w+>\s*)+", text):
                continue  # skip bare @-mentions with no real content
            agent = m.get("username") or "a teammate"
            return f"The most recent from {agent}: {_truncate_sentences(text, 2)}"
        return f"Nothing recent in #{raw.lstrip('#')}."

    def _check_pending_approvals(self, args):
        """Return a Python-formatted spoken list of pending approvals — the model
        never rephrases this, so it can't fabricate."""
        cid = config.CHANNEL_IDS.get("approvals")
        if not cid:
            return "Approvals isn't configured."
        items = []
        for m in slack_bus.history(cid, limit=50):
            text = m.get("text") or ""
            if "APPROVAL NEEDED" not in text:
                continue  # only real requests — skip joins/renames/mentions
            if slack_bus.check_approval(cid, m.get("ts")) == "pending":
                items.append(f"{m.get('username') or 'an agent'} — {_approval_tldr(text)}")
        if not items:
            return "No pending approvals."
        return f"You have {len(items)} pending: " + "; ".join(items) + "."

    def _remember(self, args):
        """Save a business-memory fact to docs/memory.md. Python writes the
        timestamp + formatting; the model (or deterministic parser) supplies only
        the fact text. Confirmation is spoken verbatim (in _STRUCTURED_RELAY)."""
        fact = str(args.get("fact") or args.get("text") or "").strip()
        return "Noted, sir." if append_business_fact(fact) else \
            "There was nothing to note, sir."

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
            # Phase 2: cap the channel dump at ~2000 tokens (newest kept) so even
            # a 30-message summarize can't push the prompt toward the window.
            content = cap_context(
                "\n".join(t for t in slack_bus.read_channel(cid, limit=30) if t.strip()))
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
