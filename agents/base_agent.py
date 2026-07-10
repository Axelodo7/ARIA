"""Shared agent base class + prompt/reasoning helpers.

run_turn drives one reasoning turn with a bounded tool-call loop (hard cap of 6
chained calls, hallucinated-tool guard). post() sends a message to the agent's
channel as its persona. The module also hosts two helpers every agent shares:
load_agent_prompt (prompts live in agent_prompts.md, never inlined) and
strip_reasoning (drop a reasoning model's <think> tokens before posting).
"""
import json, re, threading
from pathlib import Path

import config
from brain.hermes_common import call_hermes, parse_tool_call, tools_block
from integrations import slack_bus
from integrations.slack_bus import post_as
from memory import store

# agent_prompts.md sits at the project root (one level up from agents/).
PROMPTS_PATH = Path(__file__).resolve().parent.parent / "agent_prompts.md"


def load_agent_prompt(agent_name, path=PROMPTS_PATH):
    """Extract an agent's system prompt from agent_prompts.md at runtime.

    Prompts are the single source of truth in that file: each lives in the first
    fenced code block after its "## <Name> — ..." heading. Never inline them
    into Python.
    """
    text = Path(path).read_text(encoding="utf-8")
    pattern = re.compile(
        r"^##\s+" + re.escape(agent_name) + r"\b[^\n]*\n+```[^\n]*\n(.*?)^```",
        re.DOTALL | re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        raise ValueError(
            f"Could not find a prompt block for '{agent_name}' in {path}"
        )
    return m.group(1).strip()


def strip_reasoning(text):
    """Strip a reasoning model's thinking tokens, leaving only the final answer.

    Reasoning models (e.g. deepseek-r1) emit their chain of thought inside
    <think>...</think> before the answer. Keep only what follows the reasoning
    so nothing but the final message reaches Slack.
    """
    if not text:
        return text
    # Remove complete <think>...</think> blocks.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # If an unmatched </think> remains (opening tag lost/truncated), keep only
    # what's after the last one — that's the final answer.
    if re.search(r"</think>", text, flags=re.IGNORECASE):
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    # Drop any stray opening tag.
    text = re.sub(r"<think>", "", text, flags=re.IGNORECASE)
    return text.strip()


class BaseAgent:
    name = "agent"
    icon = ":robot_face:"
    channel = "#general"
    model = "hermes3"     # per-agent override; see config.AGENT_MODELS
    system_prompt = ""
    tools = []            # list of {"name", "description", "parameters"}
    tool_handlers = {}    # name -> callable(args) -> str

    def run_turn(self, task_text, history=None):
        # Only teach the tool-call format when the agent actually has tools —
        # otherwise the empty <tools> block just invites confusion.
        system = self.system_prompt + (tools_block(self.tools) if self.tools else "")
        messages = [{"role": "system", "content": system}]
        messages += (history or [])
        messages.append({"role": "user", "content": task_text})
        reply = call_hermes(messages, model=self.model)
        for _ in range(6):  # hard cap on tool-call chains — prevents infinite loops
            call = parse_tool_call(reply)
            if not call:
                break
            if call["name"] not in self.tool_handlers:   # hallucinated-tool guard
                reply = f"(skipped unknown tool {call['name']})"
                break
            result = self.tool_handlers[call["name"]](call.get("arguments", {}))
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "tool", "content": json.dumps({"name": call["name"], "content": str(result)})})
            reply = call_hermes(messages, model=self.model)
        return reply

    def post(self, text):
        post_as(self.name, self.icon, self.channel, text)

    # --- Slack read bookmark (so agents don't re-process old messages) -------
    def get_bookmark(self):
        """Return this agent's last processed Slack ts, or None."""
        return store.get_bookmark(self.name)

    def set_bookmark(self, ts):
        """Record this agent's last processed Slack ts."""
        return store.set_bookmark(self.name, ts)

    def process_pending_approvals(self):
        """Read this agent's own #approvals messages and report their status.

        For now this only READS and reports (approved / rejected / pending) — it
        never executes the approved action. Every real action path
        (apply_budget_change, schedule_post, git_push_to_main) stays
        dry-run/gate-locked, so the daemon polling this is safe by construction.
        Returns a list of {payload_id, ts, status}.
        """
        approvals_id = config.CHANNEL_IDS.get("approvals")
        if not approvals_id:
            return []
        results = []
        for m in slack_bus.history(approvals_id, limit=50):
            if m.get("username") != self.name:
                continue  # only this agent's own proposals
            ts = m.get("ts")
            status = slack_bus.check_approval(approvals_id, ts)
            match = re.search(r"\[`?([^\]`]+)`?\]", m.get("text", ""))
            results.append({"payload_id": match.group(1) if match else None,
                            "ts": ts, "status": status})
        return results
