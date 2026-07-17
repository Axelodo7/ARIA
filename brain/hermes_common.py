"""Shared Hermes/Ollama helper.

One place for the Ollama chat call, tool-call parsing, and the tools-block that
teaches Hermes our tool-call format. Every persona (ARIA + the four agents)
routes through here.

call_haiku (below) is the ONE cloud path: ARIA's freeform general-assistant answers
optionally run on Claude Haiku 4.5. Everything else — every agent cycle, all
deterministic routing, conversation mode — stays on call_hermes / gemma3.
"""
import logging
import requests, json, re
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger("aria.model")

OLLAMA_URL = "http://localhost:11434/api/chat"

# --- Anthropic (Claude Haiku) for ARIA's general-assistant path -------------
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
_HAIKU_TIMEOUT = 30


class HaikuUnavailable(Exception):
    """Haiku could not answer this turn — no key, network down (the machine's TLS
    flaps), rate limit, HTTP error, or an empty/malformed body. The caller catches
    this and falls back to gemma3, so ARIA never goes silent because the cloud is."""


def _to_anthropic(messages):
    """Translate our {role: system|user|assistant|tool} message list into the
    Anthropic shape: (list of system strings, list of user/assistant turns).

    All system-role content becomes the Anthropic `system` parameter; the FIRST
    system string is the stable, cacheable prefix (business context + persona +
    tools) and the rest is volatile (conversation memory, date). A 'tool' turn
    becomes a user turn — we use the text <tool_call> protocol, not native tool_use,
    so a tool result is just more context. Adjacent same-role turns are merged and a
    leading assistant turn is dropped, since Anthropic requires user-first
    alternation."""
    system_parts, turns = [], []
    for m in messages:
        role, content = m.get("role"), m.get("content", "")
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        role = "user" if role == "tool" else role
        text = f"Tool result: {content}" if m.get("role") == "tool" else content
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": role, "content": text})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return system_parts, turns


def _log_haiku_usage(usage, label="haiku"):
    """Print the token accounting so caching can be verified: on the first call
    with a given prefix cache_write is set and cache_read is 0; on later calls the
    same prefix comes back as cache_read (billed ~10x cheaper)."""
    print(f"[{label}] input={usage.get('input_tokens')} "
          f"cache_read={usage.get('cache_read_input_tokens')} "
          f"cache_write={usage.get('cache_creation_input_tokens')} "
          f"output={usage.get('output_tokens')}")


def call_haiku(messages, max_tokens=None, temperature=0.4, cache=True,
               return_usage=False):
    """Answer via Claude Haiku 4.5 (Anthropic Messages API). Returns the text (or
    (text, usage) when return_usage). Raises HaikuUnavailable on ANY failure — the
    caller falls back to gemma3.

    Prompt caching: the stable system prefix (business context ~1750 tokens +
    persona + tools, sent every call) is marked cache_control ephemeral, so repeated
    reads bill at ~$0.10/MTok instead of ~$1/MTok. Volatile content (conversation
    memory, the date, the user's turn) sits after the breakpoint and is never cached.
    """
    if not config.ANTHROPIC_API_KEY:
        raise HaikuUnavailable("no ANTHROPIC_API_KEY")

    system_parts, turns = _to_anthropic(messages)
    system_blocks = []
    for i, part in enumerate(system_parts):
        block = {"type": "text", "text": part}
        if cache and i == 0:            # cache the stable prefix only
            block["cache_control"] = {"type": "ephemeral"}
        system_blocks.append(block)
    if not turns:                        # Anthropic requires a first user turn
        turns = [{"role": "user", "content": ""}]

    cap = config.HAIKU_MAX_TOKENS
    payload = {"model": config.HAIKU_MODEL,
               "max_tokens": min(max_tokens or cap, cap),
               "temperature": temperature,
               "messages": turns}
    if system_blocks:
        payload["system"] = system_blocks
    headers = {"x-api-key": config.ANTHROPIC_API_KEY,
               "anthropic-version": ANTHROPIC_VERSION,
               "content-type": "application/json"}

    try:
        r = requests.post(ANTHROPIC_URL, json=payload, headers=headers,
                          timeout=_HAIKU_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise HaikuUnavailable(f"network: {type(e).__name__}")
    if r.status_code != 200:
        raise HaikuUnavailable(f"http {r.status_code}")
    try:
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        usage = data.get("usage", {}) or {}
    except (ValueError, KeyError, TypeError) as e:
        raise HaikuUnavailable(f"bad response: {type(e).__name__}")
    _log_haiku_usage(usage)
    if not text.strip():
        raise HaikuUnavailable("empty content")
    return (text, usage) if return_usage else text

# Cap any Slack channel / tool result that feeds a prompt so no single prompt can
# approach the context window. MEASURED: at 2000, Sarah's fallback run_turn prompt
# (business ctx + persona + agent memory + tools + feedback, all at cap) hit 5505
# — inside num_ctx but with only ~640 tokens left to generate a digest. 1300 puts
# every path under 5000 with real headroom, and still admits ~10 feedback messages
# per cycle (she runs every 2h, so that is ample for real volume).
CONTEXT_TOKEN_CAP = 1300

# --- Token estimation (MEASURED, not assumed) --------------------------------
# A flat chars/token ratio is unsafe here because content density varies wildly.
# Measured against gemma3's real tokenizer:
#     number-dense Slack reports ("CPA $19.72 -> $14.33 (-27%)")  ~2.3 chars/token
#     plain prose (user feedback)                                 ~4.8 chars/token
# The old len//4 therefore UNDER-counted dense #ads content by 43% — so the cap
# meant to prevent truncation was itself letting oversized prompts through, and
# llama.cpp silently dropped half the window (prompt_eval_count collapsing to
# ~num_ctx/2). Even 2.5 chars/token still under-counts dense content by 9%.
# Counting BPE-shaped units (words + punctuation) and adding a margin
# over-estimates slightly on BOTH prose and dense text — the safe direction for a
# guard whose entire job is to keep prompts inside the window.
_TOKEN_UNIT_RE = re.compile(r"\w+|[^\w\s]")
_UNIT_TO_TOKEN = 1.3


def estimate_tokens(text):
    """Conservative token estimate: never under-counts in practice (measured
    ~+16% on dense text, ~+28% on prose vs gemma3's real tokenizer)."""
    if not text:
        return 0
    return int(len(_TOKEN_UNIT_RE.findall(text)) * _UNIT_TO_TOKEN)


def cap_context(text, max_tokens=CONTEXT_TOKEN_CAP):
    """Truncate `text` to at most ~max_tokens, keeping the HEAD (for newest-first
    channel dumps that keeps the newest content). Trims whole lines using the
    measured token estimate above — not a char guess. Callers whose content is
    oldest-first (e.g. Sarah's feedback) should select by recency BEFORE joining."""
    if not text or estimate_tokens(text) <= max_tokens:
        return text
    marker = "\n… [truncated to fit context window]"
    budget = max_tokens - estimate_tokens(marker)
    kept, total = [], 0
    for line in text.splitlines():
        cost = estimate_tokens(line) + 1        # +1 for the newline
        if kept and total + cost > budget:
            break
        kept.append(line)
        total += cost
    if not kept:                                 # one giant line — hard-cut it
        return text[: max(1, budget * 2)].rstrip() + marker
    return "\n".join(kept).rstrip() + marker

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

# --- Dry-run isolation -------------------------------------------------------
# The live file, captured before anything can reassign BUSINESS_MEMORY_PATH above.
# The thing being protected has to be identified by LOCATION, not by "did the
# caller pass a path?": tests isolate themselves by REASSIGNING that global, so
# `path is None` says nothing about whether the real file is the target.
_LIVE_BUSINESS_MEMORY_PATH = BUSINESS_MEMORY_PATH

# Where a dry run's facts go instead — the file analogue of agent_memory's dry_run
# column. Same contract: the write still happens and still round-trips, it just
# lands in a namespace that can never be mistaken for, or mutate, real business
# memory. This is not hypothetical: a misfiring `remember` tool call wrote a
# fabricated "fact" into the real docs/memory.md during testing, and it had to be
# pruned out by hand. Gitignored — it is scratch, not history.
DRY_RUN_MEMORY_PATH = _LIVE_BUSINESS_MEMORY_PATH.with_name("memory.dryrun.md")


def _dry_run_overlay(target):
    """The isolated file a dry run must use INSTEAD of `target`, or None when
    there is nothing to protect — a real run, or a caller that already aimed the
    path elsewhere (a test's temp file IS the isolation; redirecting that would
    only break its read-back)."""
    if not config.DRY_RUN:
        return None
    try:
        if Path(target).resolve() != _LIVE_BUSINESS_MEMORY_PATH.resolve():
            return None
    except OSError:
        return None
    return DRY_RUN_MEMORY_PATH


def _read_facts(path):
    """The '- ...' fact lines of `path` in file order (chronological). Only bullet
    lines are facts; headers, comments and blanks are ignored. Missing -> []."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    return [ln.rstrip() for ln in text.splitlines() if ln.strip().startswith("- ")]


def load_business_memory(path=None, max_tokens=BUSINESS_MEMORY_MAX_TOKENS):
    """Return the most recent business facts that fit within max_tokens, NEWEST
    FIRST. Missing/empty -> ''. Read fresh (never cached).

    Under DRY_RUN the live file is read READ-ONLY and any dry-run facts are laid
    over the top (they are newer, so they come first). A dry run therefore sees
    exactly what production sees, PLUS whatever it remembered itself — which keeps
    the prompt the same size it will be in production, and those prompt_eval
    budgets are measured under DRY_RUN. A REAL run never sees a dry-run fact.
    """
    target = Path(path or BUSINESS_MEMORY_PATH)
    facts = _read_facts(target)
    overlay = _dry_run_overlay(target)
    if overlay:
        facts += _read_facts(overlay)       # appended last == newest
    if not facts:
        return ""
    kept, total = [], 0
    for ln in reversed(facts):  # append order is chronological -> reverse = newest first
        cost = estimate_tokens(ln) + 1          # +1 for the newline
        if kept and total + cost > max_tokens:
            break
        kept.append(ln)
        total += cost
    return "\n".join(kept)


def append_business_fact(fact, path=None):
    """Append one timestamped fact line to business memory and return the line
    written (or None if `fact` is empty). PYTHON writes the date and formatting;
    the caller supplies ONLY the fact text — never the timestamp or the file I/O.

    A dry run NEVER writes the live docs/memory.md; the write is redirected to
    DRY_RUN_MEMORY_PATH. Returns the line either way, so a dry run still reports
    honestly what it recorded — it recorded it, just not into real memory.
    """
    fact = (fact or "").strip()
    if not fact:
        return None
    if fact[-1] not in ".!?":
        fact += "."
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"- {date}: {fact}"
    target = Path(path or BUSINESS_MEMORY_PATH)
    p = _dry_run_overlay(target) or target
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


# --- Tool syntax must never be SPOKEN ----------------------------------------
# A tool call is machine syntax. MEASURED (Bug A): "give me the pricing tiers"
# made the model emit <tool_call>{"name": "remember", ...}, num_predict=64 cut it
# off mid-JSON, parse_tool_call found no closing tag and returned None — so the raw
# text became the reply and was read aloud until the sentence simply stopped.
# parse_tool_call is the happy path; these are the backstop. Parse it, or strip it,
# but never speak it.
_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL | re.IGNORECASE)
_TOOL_CALL_OPEN_RE = re.compile(r"<tool_call>.*$", re.DOTALL | re.IGNORECASE)
_TOOL_CALL_STRAY_RE = re.compile(r"</?tool_call>", re.IGNORECASE)
# A bare envelope with no tags — {"name": "remember", "arguments": {...}}
_TOOL_ENVELOPE_RE = re.compile(r'^\s*\{\s*"(?:name|arguments)"\s*:.*$', re.DOTALL)


def looks_like_tool_syntax(text):
    """True if a reply is TRYING to be a tool call — including a truncated one that
    parse_tool_call cannot decode. Used to retry with a bigger token budget before
    the text would otherwise be spoken verbatim."""
    t = text or ""
    return bool(_TOOL_CALL_STRAY_RE.search(t) or _TOOL_ENVELOPE_RE.match(t))


def strip_tool_syntax(text):
    """Remove tool-call syntax from a reply bound for the user.

    Handles complete blocks, an UNTERMINATED opener (the truncation case), stray
    tags, and a bare JSON envelope. Internal newlines are preserved — speech
    cleaning downstream still needs the line structure.
    """
    if not text:
        return ""
    t = _TOOL_CALL_BLOCK_RE.sub(" ", text)
    t = _TOOL_CALL_OPEN_RE.sub(" ", t)      # truncated: no closing tag ever came
    t = _TOOL_CALL_STRAY_RE.sub(" ", t)
    if _TOOL_ENVELOPE_RE.match(t):
        t = ""
    return t.strip()


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
