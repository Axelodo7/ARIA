"""Shared Hermes/Ollama helper.

One place for the Ollama chat call, tool-call parsing, and the tools-block that
teaches Hermes our tool-call format. Every persona (ARIA + the four agents)
routes through here.
"""
import requests, json, re

OLLAMA_URL = "http://localhost:11434/api/chat"


def call_hermes(messages, model=None, temperature=0.4, num_predict=None,
                keep_alive=None, think=None):
    # model is passed down per-agent (see config.AGENT_MODELS). Falls back to
    # hermes3 for backward compatibility when a caller doesn't specify one.
    # num_predict caps generated tokens (used for ARIA's terse fast replies);
    # keep_alive keeps the model resident so cold starts don't recur.
    # think: set False to disable native reasoning on thinking models (e.g.
    # qwen3.5) — otherwise they burn the whole token budget on a `thinking` field
    # and return empty content. Only sent when explicitly set, so non-thinking
    # models (hermes3) are unaffected.
    options = {"temperature": temperature}
    if num_predict is not None:
        options["num_predict"] = num_predict
    payload = {"model": model or "hermes3", "messages": messages,
               "stream": False, "options": options}
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    if think is not None:
        payload["think"] = think
    r = requests.post(OLLAMA_URL, json=payload)
    return r.json()["message"]["content"]


def _first_json_object(s, start=0):
    """Decode the first balanced JSON object appearing at/after `start`.

    Uses raw_decode so nested braces and strings containing '}' are handled
    correctly (a plain non-greedy regex would truncate at the first '}').
    """
    idx = s.find("{", start)
    while idx != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        idx = s.find("{", idx + 1)
    return None


def parse_tool_call(text):
    """Parse a tool call from a model reply, tolerating two formats.

    Preferred:  <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    Fallback :  tool_name: {...}   (the shorthand Hermes often emits despite
                instructions). The shorthand's object becomes `arguments`.

    Returns a dict {"name", "arguments"} or None. Only the first call in the
    text is returned; run_turn handles chained calls across turns.
    """
    if not text:
        return None

    # Preferred XML form.
    m = re.search(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)
    if m:
        obj = _first_json_object(m.group(1))
        if isinstance(obj, dict) and "name" in obj:
            obj.setdefault("arguments", {})
            return obj

    # Fallback: `tool_name: { ... }` at the start of a line (tolerating simple
    # markdown bullet/quote prefixes like "- ", "* ", "> ").
    m = re.search(r"(?:^|\n)[ \t>*\-]*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?=\{)", text)
    if m:
        obj = _first_json_object(text, m.end())
        if isinstance(obj, dict):
            # If the object is itself a full {"name","arguments"} envelope,
            # pass it through rather than nesting it.
            if "name" in obj and "arguments" in obj:
                obj.setdefault("arguments", {})
                return obj
            return {"name": m.group(1), "arguments": obj}

    return None


def tools_block(tools):
    return f"""
You have access to the following tools. To call one, respond with EXACTLY this
format and nothing else — the <tool_call> wrapper is required:
<tool_call>{{"name": "<tool_name>", "arguments": {{...}}}}</tool_call>

Example — to call a tool named save_note:
<tool_call>{{"name": "save_note", "arguments": {{"note": "Two users asked for dark mode this week."}}}}</tool_call>

Do NOT use the shorthand `tool_name: {{...}}` and do NOT wrap the call in
markdown code fences. One tool call per message.

<tools>
{json.dumps(tools)}
</tools>

If no tool is needed, respond in plain text."""
