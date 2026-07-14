"""Shared Hermes/Ollama helper.

One place for the Ollama chat call, tool-call parsing, and the tools-block that
teaches Hermes our tool-call format. Every persona (ARIA + the four agents)
routes through here.
"""
import requests, json, re
from datetime import datetime, timezone
from pathlib import Path

import config

OLLAMA_URL = "http://localhost:11434/api/chat"

# Phase 2: cap any Slack channel / tool result that feeds a prompt so no single
# prompt can approach the context window. ~2000 tokens ≈ ~8000 chars.
CONTEXT_TOKEN_CAP = 2000
_CHARS_PER_TOKEN = 4


def cap_context(text, max_tokens=CONTEXT_TOKEN_CAP):
    """Truncate `text` to ~max_tokens (keeping the HEAD — for newest-first channel
    dumps that keeps the newest content). Cuts on a line boundary and appends a
    marker. Callers whose content is oldest-first (e.g. Sarah's feedback) should
    select by recency BEFORE joining rather than relying on this."""
    if not text:
        return text
    max_chars = max_tokens * _CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = head.rsplit("\n", 1)[0] if "\n" in head else head
    return cut.rstrip() + "\n… [truncated to fit context window]"

# Shared business context — read ONCE from docs/user.md and cached. Prepended to
# every agent's + ARIA's system prompt so they all reason with the same knowledge
# of the business. Kept in a file (never inlined) so it's editable without code
# changes. docs/user.md sits at the project root (two levels up from brain/).
BUSINESS_CONTEXT_PATH = Path(__file__).resolve().parent.parent / "docs" / "user.md"
_business_context = None


def load_business_context(path=BUSINESS_CONTEXT_PATH):
    """Return docs/user.md, read once and cached. Missing file -> '' (no crash)."""
    global _business_context
    if _business_context is None:
        try:
            _business_context = Path(path).read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            _business_context = ""
    return _business_context


# --- Business memory (Stage 2) ----------------------------------------------
# docs/memory.md holds timestamped facts about ZentraIQ that change over time
# (milestones, launched features, policy changes). Unlike user.md this is read
# FRESH each call (never cached) because ARIA appends to it at runtime and the
# agent daemon must see the latest. Loaded newest-first, hard-capped so it can't
# erode the num_ctx-6144 headroom (agent memory already takes up to ~600 tokens).
BUSINESS_MEMORY_PATH = Path(__file__).resolve().parent.parent / "docs" / "memory.md"
BUSINESS_MEMORY_MAX_TOKENS = 400


def load_business_memory(path=None, max_tokens=BUSINESS_MEMORY_MAX_TOKENS):
    """Return the most recent docs/memory.md fact lines that fit within
    max_tokens, NEWEST FIRST. Only bullet lines ('- ...') are facts; headers,
    comments, and blanks are ignored. Missing/empty file -> ''. Read fresh."""
    try:
        text = Path(path or BUSINESS_MEMORY_PATH).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""
    facts = [ln.rstrip() for ln in text.splitlines() if ln.strip().startswith("- ")]
    if not facts:
        return ""
    max_chars = max_tokens * _CHARS_PER_TOKEN
    kept, total = [], 0
    for ln in reversed(facts):  # append order is chronological -> reverse = newest first
        if kept and total + len(ln) + 1 > max_chars:
            break
        kept.append(ln)
        total += len(ln) + 1
    return "\n".join(kept)


def append_business_fact(fact, path=None):
    """Append one timestamped fact line to docs/memory.md and return the line
    written (or None if `fact` is empty). PYTHON writes the date and formatting;
    the caller supplies ONLY the fact text — never the timestamp or the file I/O."""
    fact = (fact or "").strip()
    if not fact:
        return None
    if fact[-1] not in ".!?":
        fact += "."
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"- {date}: {fact}"
    p = Path(path or BUSINESS_MEMORY_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(prefix + line + "\n")
    return line


def business_context_block(path=BUSINESS_CONTEXT_PATH):
    """The <business_context> preamble to prepend to a system prompt: docs/user.md
    plus (Stage 2) the recent facts from docs/memory.md, capped and newest-first.
    '' when both are empty. Framed as shared knowledge to apply (not recite), with
    an explicit note that the agent's own persona wins on any conflict."""
    ctx = load_business_context(path)
    mem = load_business_memory()
    if not ctx and not mem:
        return ""
    body = ctx
    if mem:
        body = ((body + "\n\n") if body else "") + (
            "Recent developments you should know about (most recent first):\n" + mem)
    return (
        "<business_context>\n"
        "The following is shared knowledge about the business you work for. Apply "
        "it to your own role — do NOT recite it verbatim. It is background, not a "
        "script. If anything here ever conflicts with your persona or task "
        "instructions below, THOSE take precedence.\n\n"
        f"{body}\n"
        "</business_context>\n\n"
    )


def call_hermes(messages, model=None, temperature=0.4, num_predict=None,
                keep_alive=None, think=None, num_ctx=None):
    # model is passed down per-agent (see config.AGENT_MODELS). Falls back to
    # config.DEFAULT_MODEL (gemma3) when a caller doesn't specify one.
    # num_predict caps generated tokens (used for ARIA's terse fast replies);
    # keep_alive keeps the model resident so cold starts don't recur.
    # think: set False to disable native reasoning on thinking models (e.g.
    # qwen3.5) — otherwise they burn the whole token budget on a `thinking` field
    # and return empty content. Only sent when explicitly set, so non-thinking
    # models (hermes3) are unaffected.
    options = {"temperature": temperature}
    if num_predict is not None:
        options["num_predict"] = num_predict
    # Phase 2: pin the context window on every call (defaults to config.NUM_CTX,
    # currently 6144). Passed here so it applies to every persona and path uniformly.
    options["num_ctx"] = num_ctx if num_ctx is not None else config.NUM_CTX
    payload = {"model": model or config.DEFAULT_MODEL, "messages": messages,
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
