"""Shared Hermes/Ollama helper.

One place for the Ollama chat call, tool-call parsing, and the tools-block that
teaches Hermes our tool-call format. Every persona (ARIA + the four agents)
routes through here.
"""
import requests, json, re

OLLAMA_URL = "http://localhost:11434/api/chat"


def call_hermes(messages, model="hermes3", temperature=0.4):
    r = requests.post(OLLAMA_URL, json={
        "model": model, "messages": messages, "stream": False,
        "options": {"temperature": temperature}
    })
    return r.json()["message"]["content"]


def parse_tool_call(text):
    m = re.search(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def tools_block(tools):
    return f"""
You have access to the following tools. To call one, respond ONLY with:
<tool_call>{{"name": "<tool_name>", "arguments": {{...}}}}</tool_call>

<tools>
{json.dumps(tools)}
</tools>

If no tool is needed, respond in plain text."""
