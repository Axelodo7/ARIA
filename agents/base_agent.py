"""Shared agent base class: prompt + tools + Slack posting.

run_turn drives one reasoning turn with a bounded tool-call loop (hard cap of 6
chained calls, hallucinated-tool guard). post() sends a message to the agent's
channel as its persona.
"""
import json, threading
from brain.hermes_common import call_hermes, parse_tool_call, tools_block
from integrations.slack_bus import post_as


class BaseAgent:
    name = "agent"
    icon = ":robot_face:"
    channel = "#general"
    model = "hermes3"     # per-agent override; see config.AGENT_MODELS
    system_prompt = ""
    tools = []            # list of {"name", "description", "parameters"}
    tool_handlers = {}    # name -> callable(args) -> str

    def run_turn(self, task_text, history=None):
        messages = [{"role": "system", "content": self.system_prompt + tools_block(self.tools)}]
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
