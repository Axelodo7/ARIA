"""Web search for ARIA (Level 1) — Tavily.

Tavily (tavily.com) is built for LLM use: one POST returns a pre-summarized
`answer` plus a few clean result snippets. That summarization is what makes this
safe for gemma3 — Python takes the clean answer, caps it HARD to protect the
context window, and (Principle 1) pulls the specific value out itself where it can,
so the model has less to mangle.

search() NEVER raises and NEVER guesses. No key, no network (the machine's TLS
flaps), a bad status, or empty results all return None, and the caller says
"I couldn't find that, sir." rather than inventing an answer.
"""
import re

import requests

import config
from brain.hermes_common import cap_context, estimate_tokens

TAVILY_URL = "https://api.tavily.com/search"
_TIMEOUT = 8               # generous enough to ride out a slow response, not hang
_MAX_RESULTS = 3

# Search snippets are web text — dense and unbounded. Cap the grounding block the
# model narrates from with the TOKEN-AWARE estimator (a char count under-counts
# dense content — measured lesson). The narration prompt is small otherwise, so
# this keeps it far under num_ctx=6144 with room to spare.
SEARCH_RESULT_TOKEN_CAP = 350

COULDNT_FIND = "I couldn't find that, sir."


def _fit(text, max_tokens):
    """Guarantee `text` is within max_tokens by trimming whole WORDS off the end.

    cap_context trims by line and keeps the first line whole — but a Tavily `answer`
    is often ONE long sentence, so a line-based cap can leave it over budget. This is
    the belt: word-level truncation caps even a single long line. Uses the same
    token-aware estimator (a char count under-counts dense text — measured lesson)."""
    if estimate_tokens(text) <= max_tokens:
        return text
    words = text.split()
    lo, hi = 0, len(words)
    while lo < hi:                       # largest prefix that fits, minus room for "…"
        mid = (lo + hi + 1) // 2
        if estimate_tokens(" ".join(words[:mid])) <= max_tokens - 2:
            lo = mid
        else:
            hi = mid - 1
    return (" ".join(words[:lo]).rstrip() + " …") if lo else words[0][:max_tokens]


def _fetch(query):
    """Low-level Tavily call. Returns the raw JSON dict, or None on ANY failure
    (missing key, network/TLS/DNS failure, timeout, non-200, unparseable body).
    Never raises — a search that can't complete must degrade to 'couldn't find'."""
    if not config.TAVILY_API_KEY:
        return None
    try:
        r = requests.post(
            TAVILY_URL,
            json={"api_key": config.TAVILY_API_KEY, "query": query,
                  "include_answer": True, "search_depth": "basic",
                  "max_results": _MAX_RESULTS},
            timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.exceptions.RequestException, ValueError):
        return None


# --- Python value extraction (Principle 1) -----------------------------------
# Where the answer is a specific value, pull it out in code so the model narrates a
# fact it cannot fumble. Each returns a short spoken phrase or None. Order matters:
# the caller tries them against Tavily's `answer` first (cleanest text).
_TEMP_RE = re.compile(r"(-?\d{1,3})\s*(?:°|degrees?\b|deg\b)\s*(F|C|Fahrenheit|Celsius)?",
                      re.IGNORECASE)
_AQI_RE = re.compile(r"\b(?:AQI|air quality index)\b[^\d]{0,15}(\d{1,3})", re.IGNORECASE)
_PRICE_RE = re.compile(r"([$£€])\s?(\d[\d,]*(?:\.\d+)?)")


def _extract_value(text):
    """Return a short, unambiguous spoken phrase for a salient value in `text`
    (temperature, AQI, price), or None. Kept deliberately narrow — a wrong guess
    is worse than none, so it only fires on clearly-labelled values."""
    if not text:
        return None
    m = _AQI_RE.search(text)
    if m:
        return f"the air quality index is {m.group(1)}"
    m = _TEMP_RE.search(text)
    if m:
        unit = (m.group(2) or "").upper()
        unit = "Fahrenheit" if unit in ("F", "FAHRENHEIT") else \
               "Celsius" if unit in ("C", "CELSIUS") else ""
        return f"it's {m.group(1)} degrees{(' ' + unit) if unit else ''}"
    m = _PRICE_RE.search(text)
    if m:
        return f"the price is {m.group(1)}{m.group(2)}"
    return None


def search(query):
    """Fetch and normalise a search result, or None if nothing usable came back.

    Returns a dict:
      answer    — Tavily's summarized answer (str, possibly ''),
      snippets  — up to _MAX_RESULTS clean result snippets (list[str]),
      grounding — the TOKEN-CAPPED block the model narrates from (str),
      value     — a Python-extracted spoken phrase for a specific value, or None.
    None means: no key, network failure, or no answer AND no snippets — the caller
    must decline, never invent.
    """
    query = (query or "").strip()
    if not query:
        return None
    data = _fetch(query)
    if not data:
        return None

    answer = (data.get("answer") or "").strip()
    snippets = []
    for r in (data.get("results") or [])[:_MAX_RESULTS]:
        c = " ".join((r.get("content") or "").split())
        if c:
            snippets.append(c)
    if not answer and not snippets:
        return None

    # Extract the specific value from the cleanest text available (answer first).
    value = _extract_value(answer) or _extract_value(" ".join(snippets))

    # Build the grounding block the model narrates from, answer first so the HEAD-
    # keeping cap retains it, then snippets until the token budget is spent.
    # NOTE: no NUMBERED labels ("Source 1:") — those digits would land in the
    # grounding's number set and weaken the fabrication guard, which treats every
    # number in the grounding as a supported fact. Bullets carry no numbers.
    parts = []
    if value:
        parts.append(f"Key fact: {value}.")
    if answer:
        parts.append(f"Answer: {answer}")
    for s in snippets:
        parts.append(f"- {s}")
    grounding = cap_context("\n".join(parts), max_tokens=SEARCH_RESULT_TOKEN_CAP)
    grounding = _fit(grounding, SEARCH_RESULT_TOKEN_CAP)   # cap even a single long line

    return {"answer": answer, "snippets": snippets, "grounding": grounding,
            "value": value}


def grounding_tokens(result):
    """Token estimate of a result's grounding block — for the budget test."""
    return estimate_tokens(result["grounding"]) if result else 0


# --- Fabrication guard (Principle 1, point 5) --------------------------------
# gemma3 narrates the grounding, but it must not add FACTS. The facts that matter
# in a search answer — and the ones the model most dangerously invents — are
# numbers: temperatures, AQI, prices, scores. This guard requires every number the
# narration asserts to be backed by the grounding, comparing digits AND
# number-words so "72" in the results legitimately backs "seventy-two degrees",
# while an invented "75" (or a model-computed Celsius conversion the results never
# gave) is caught and the caller falls back to the grounded answer.
_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
         "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_DIGITS_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-z]+")


def _digit_values(text):
    out = set()
    for tok in _DIGITS_RE.findall(text):
        try:
            f = float(tok.replace(",", ""))
            out.add(f)
        except ValueError:
            pass
    return out


def _word_values(text):
    """Integer values written as number-words ('seventy-two' -> 72,
    'two hundred fifty' -> 250)."""
    out, cur, used = set(), 0, False
    for tok in _WORD_RE.findall(text.lower()):
        if tok in _ONES:
            cur += _ONES[tok]; used = True
        elif tok in _TENS:
            cur += _TENS[tok]; used = True
        elif tok == "hundred":
            cur = (cur or 1) * 100; used = True
        elif tok == "thousand":
            cur = (cur or 1) * 1000; used = True
        elif tok == "and" and used:
            continue
        else:
            if used:
                out.add(float(cur))
            cur, used = 0, False
    if used:
        out.add(float(cur))
    return out


def _numeric_values(text):
    return _digit_values(text) | _word_values(text)


def narration_fabricates(narration, grounding):
    """True if `narration` asserts a numeric fact the `grounding` doesn't support.

    Numbers only — non-numeric embellishment isn't policed here; the numbers are
    the facts that matter and the ones the model most dangerously invents. A value
    counts as supported if it appears in the grounding as a digit or a number-word
    (small float tolerance for prices)."""
    g = _numeric_values(grounding)
    for v in _numeric_values(narration):
        if not any(abs(v - gv) < 0.01 for gv in g):
            return True
    return False


# --- Unit conversion (Principle 1: Python does the math, never the model) -----
# A follow-up like "give me those in Fahrenheit" must NOT be answered by the model
# doing mental math on the previous turn's numbers — that is arithmetic it wasn't
# handed, the pattern that produces confident wrong values (and, measured, a
# hallucinated "#feedback" source). So Python extracts the previous search's
# temperatures and converts them itself, and the caller narrates the result
# deterministically. A temperature needs an explicit C/F unit to be picked up, so a
# bare AQI or rainfall figure is never mistaken for one.
_TEMP_EXTRACT_RE = re.compile(
    r"(-?\d{1,3}(?:\.\d+)?)\s*(?:°\s*|degrees?\s+|deg\s+)?"
    r"(C|F|Celsius|Centigrade|Fahrenheit)\b", re.IGNORECASE)


def extract_temperatures(text):
    """Every temperature in `text` as (value, unit), unit normalised to 'C'/'F'."""
    return [(float(m.group(1)), m.group(2)[0].upper())
            for m in _TEMP_EXTRACT_RE.finditer(text or "")]


def convert_temperature(value, from_unit, to_unit):
    """Convert a single temperature between C and F. Same unit -> unchanged."""
    f, t = from_unit.upper()[0], to_unit.upper()[0]
    if f == t:
        return value
    if f == "C" and t == "F":
        return value * 9 / 5 + 32
    if f == "F" and t == "C":
        return (value - 32) * 5 / 9
    return value


def temperatures_to_unit(text, target_unit):
    """Every temperature in `text`, converted to target_unit, as de-duplicated
    rounded ints in first-seen order. [] when there is nothing to convert."""
    t = target_unit.upper()[0]
    seen, out = set(), []
    for value, unit in extract_temperatures(text):
        conv = round(convert_temperature(value, unit, t))
        if conv not in seen:
            seen.add(conv)
            out.append(conv)
    return out


def narrate_conversion(values, target_unit):
    """A deterministic spoken line for converted temperatures — built in Python so
    the model never touches it. None when there is nothing to say."""
    if not values:
        return None
    unit = "Fahrenheit" if target_unit.upper().startswith("F") else "Celsius"
    nums = [str(v) for v in values]
    if len(nums) == 1:
        return f"That's {nums[0]} degrees {unit}, sir."
    body = f"{', '.join(nums[:-1])} and {nums[-1]}"
    return f"Those are {body} degrees {unit}, sir."
