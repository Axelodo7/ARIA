"""What ARIA SAYS — three bugs heard live, guarded here.

Bug A — raw tool-call JSON was spoken aloud. "give me the pricing tiers" made the
  model emit <tool_call>{"name": "remember", ...}; num_predict=64 cut it off
  mid-JSON, parse_tool_call found no closing tag and returned None, and the raw
  text became the reply. Two fixes: `remember` is no longer offered to the model
  (Principle 1 — _parse_remember decides), and tool syntax is stripped from any
  reply bound for the user.
Bug B — Slack markdown read as gibberish: "asterisk T L semicolon D R asterisk",
  "colon clapper colon", "C R underscore A". Fixed by clean_for_speech().
Bug C — a turn produced silence, indistinguishable from a crash.

The model checks are REPEATED: the original misfire was stochastic, so a single
green run would prove nothing.

Isolated: temp DB. DRY_RUN forced. No audio device is touched (sounddevice and the
ElevenLabs client are both stubbed). The model section needs Ollama.

    python scripts/test_speech_output.py
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ["DRY_RUN"] = "True"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

import config
from memory import store

TMP_DB = os.path.join(tempfile.gettempdir(), f"test_speech_{os.getpid()}.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
store.DB_PATH = TMP_DB

# Isolate business memory too. append_business_fact is NOT gated on DRY_RUN, so a
# misfiring `remember` writes straight into the real docs/memory.md — that is not
# hypothetical: it happened while measuring this fix, and a fabricated "fact" had
# to be pruned out of the user's file by hand. The DB alone is not enough isolation.
from brain import hermes_common as HC

TMP_MEM = os.path.join(tempfile.gettempdir(), f"test_speech_mem_{os.getpid()}.md")
HC.BUSINESS_MEMORY_PATH = TMP_MEM

from brain.speech import (clean_for_speech, speakable, EMPTY_REPLY_FALLBACK,
                          _EMOJI_RE, _BULLET_RE)
from brain.hermes_common import strip_tool_syntax, looks_like_tool_syntax, OLLAMA_URL
from brain.aria_agent import Aria, _parse_remember, _tool_vetoed

REPEATS = 3
PRICING_QS = ["give me the pricing tiers",
              "what are the pricing tiers",
              "how much does Scale cost"]
PRICE_TOKENS = ("8k", "$8", "15k", "$15", "23k", "$23", "225")

# A realistic Ava draft, exactly as it comes back from Slack.
AVA_DRAFT = (
    "*TL;DR* — hook v2 is ready :clapper:\n"
    "- _Pillar_: `Method` — the 2/8/4 split\n"
    "- Posting to <#C123|content> after review by <@U456>\n"
    "See ~$0 spend so far. Ref: cr_A vs cr_B in support_intel."
)


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def preflight():
    try:
        requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=5).raise_for_status()
        return True
    except requests.exceptions.RequestException:
        print("[x] Ollama not reachable. Start `ollama serve`.")
        return False


# --- Stubs: prove the choke point without a speaker or an API key -------------
class _FakeStream:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write(self, chunk):
        pass


class _FakeTTS:
    def __init__(self, sink):
        self.sink = sink

    def stream(self, voice, text=None, model_id=None, output_format=None):
        self.sink.append(text)          # what ElevenLabs would actually receive
        return iter([b"\x00\x00"])


class _FakeClient:
    def __init__(self, sink):
        self.text_to_speech = _FakeTTS(sink)


def capture_tts(text):
    """Return the string speak() would hand to ElevenLabs for `text`."""
    from audio import speak as speak_mod
    sink = []
    real_client, real_stream = speak_mod._client_or_none, speak_mod.sd.RawOutputStream
    speak_mod._client_or_none = lambda: _FakeClient(sink)
    speak_mod.sd.RawOutputStream = lambda **kw: _FakeStream()
    try:
        speak_mod.speak(text)
    finally:
        speak_mod._client_or_none, speak_mod.sd.RawOutputStream = real_client, real_stream
    return sink[0] if sink else ""


def main():
    print("=" * 76)
    print("Spoken output — no tool syntax, no Slack markup, never silent")
    print("=" * 76)

    # --- 1) Bug B: Slack markdown -> speakable prose -------------------------
    print("\n--- 1) an Ava draft, cleaned for speech ---")
    out = clean_for_speech(AVA_DRAFT)
    print(f"  {out}")
    b1 = chk("no formatting characters survive (* _ ~ `)",
             not any(c in out for c in "*_~`"))
    # Colons themselves are fine and must stay ("Pillar: Method") — it's the
    # :emoji_code: shape that has no spoken form.
    b2 = chk("no :emoji_code: survives",
             ":clapper:" not in out and not _EMOJI_RE.search(out))
    b3 = chk("cr_A is spoken as 'creative A'",
             "creative A" in out and "cr_A" not in out)
    b4 = chk("snake_case is spoken as words ('support intel')",
             "support intel" in out and "support_intel" not in out)
    b5 = chk("Slack link/mention syntax is gone",
             "<#C123" not in out and "<@U456" not in out and "content" in out)
    b6 = chk("'~$0' is spoken as an approximation, not a strikethrough",
             "about $0" in out)
    b7 = chk("the real content survives", "hook v2" in out and "2/8/4" in out)

    # --- 2) things that only LOOK like markup --------------------------------
    print("\n--- 2) the cleaner doesn't eat real content ---")
    t1 = chk("clock times survive (':30:' is not an emoji code)",
             clean_for_speech("Cycle ran at 10:30:15 today") == "Cycle ran at 10:30:15 today")
    t2 = chk("plain prose is untouched",
             clean_for_speech("You have 6 pending, sir.") == "You have 6 pending, sir.")
    t3 = chk("unpunctuated lines get a stop so TTS doesn't run them together",
             clean_for_speech("First thought\nSecond thought.") == "First thought. Second thought.")

    # --- 3) Bug A: tool syntax never reaches the ear -------------------------
    print("\n--- 3) tool syntax is stripped, including a TRUNCATED call ---")
    full = '<tool_call>{"name": "remember", "arguments": {"fact": "x"}}</tool_call>'
    # Exactly the shape that was spoken: cut off by num_predict, no closing tag.
    truncated = 'Certainly, sir. <tool_call>{"name": "remember", "arguments": {"fac'
    a1 = chk("a complete tool call strips to nothing", strip_tool_syntax(full) == "")
    a2 = chk("a TRUNCATED tool call strips too (no closing tag)",
             "tool_call" not in strip_tool_syntax(truncated)
             and "arguments" not in strip_tool_syntax(truncated))
    a3 = chk("prose around a tool call is kept",
             strip_tool_syntax(truncated).startswith("Certainly, sir."))
    a4 = chk("a truncated call is recognised as tool syntax (triggers the retry)",
             looks_like_tool_syntax(truncated) and looks_like_tool_syntax(full))
    a5 = chk("a bare JSON envelope strips too",
             strip_tool_syntax('{"name": "remember", "arguments": {}}') == "")
    a6 = chk("normal prose is not mistaken for tool syntax",
             not looks_like_tool_syntax("The Scale tier is $15k, sir."))

    # --- 4) Bug C: never silent ----------------------------------------------
    print("\n--- 4) an empty reply becomes an audible fallback ---")
    c1 = chk("a formatting-only reply falls back", speakable("*_~`*") == EMPTY_REPLY_FALLBACK)
    c2 = chk("whitespace falls back", speakable("   \n  ") == EMPTY_REPLY_FALLBACK)
    c3 = chk("None falls back", speakable(None) == EMPTY_REPLY_FALLBACK)
    c4 = chk("a tool-call-only reply falls back (Bug A + C together)",
             speakable(full) == EMPTY_REPLY_FALLBACK)
    c5 = chk("real text is NOT replaced", speakable("All quiet, sir.") == "All quiet, sir.")

    # --- 5) the choke point: what ElevenLabs actually receives ---------------
    print("\n--- 5) end-to-end through speak() (audio + API stubbed) ---")
    sent = capture_tts(AVA_DRAFT)
    print(f"  TTS input : {sent}")
    e1 = chk("TTS input has no '*'", "*" not in sent)
    e2 = chk("TTS input has no '_'", "_" not in sent)
    e3 = chk("TTS input has no ':emoji_code:'",
             ":clapper:" not in sent and not _EMOJI_RE.search(sent))
    e4 = chk("TTS input has no tool syntax", "tool_call" not in capture_tts(full))
    e5 = chk("speak() substitutes the fallback rather than going silent",
             capture_tts("") == EMPTY_REPLY_FALLBACK)

    # --- 6) remember fires on COMMANDS only (no model) -----------------------
    print("\n--- 6) the remember matcher is command-only (deterministic) ---")
    r1 = chk("questions never match",
             not any(_parse_remember(q) for q in PRICING_QS + [
                 "what should I remember about pricing",
                 "remember what I said about pricing?",
                 "do you remember the Scale tier?"]))
    r2 = chk("explicit commands DO match",
             _parse_remember("remember that we launched the Scale tier") ==
             "we launched the Scale tier"
             and _parse_remember("note that Bobby paused cr_D") == "Bobby paused cr_D")

    # The veto is the structural guarantee: the model may PROPOSE remember, but it
    # only runs if the user actually issued a remember command. Removing the tool
    # from TOOLS was tried instead and measurably displaced the misfire onto
    # read_channel (6/10), so the gate lives here, not in the tool list.
    print("\n--- 6b) Python vetoes remember unless the USER commanded it ---")
    v1 = chk("remember proposed on a question is vetoed",
             all(_tool_vetoed("remember", q) for q in PRICING_QS))
    v2 = chk("remember on a real command is NOT vetoed",
             not _tool_vetoed("remember", "remember that we hit 10 clients"))
    v3 = chk("the veto is scoped to remember (read_channel unaffected)",
             not _tool_vetoed("read_channel", "what's in ads"))

    # --- 7) THE POINT: a pricing question, through the real model ------------
    print(f"\n--- 7) pricing questions x{REPEATS} — no remember, no <tool_call> ---")
    m1 = m2 = m3 = True
    if not preflight():
        print("  [skip] model checks skipped — Ollama unreachable.")
        m1 = m2 = m3 = None
    else:
        # SEED conversation memory. This is not decoration: the tool misfire only
        # reproduces with a transcript in the prompt. Measured while building this
        # fix — with the seed, pricing questions misfired to a tool 6/10; without
        # it, 0/10 on the identical code. An unseeded run here would be a false
        # green that lets the real bug walk straight through.
        store.add_conversation_turn("s", "user",
            "Tell me about the Praising Theatre of Central IQ.")
        store.add_conversation_turn("s", "aria",
            "I apologize, sir, but there appears to be a misunderstanding. There is "
            "no “Praising Theatre of Central IQ.” That is not a real entity. As you "
            "know, we are ZentraIQ, an AI-native MVP studio.")
        aria = Aria()
        fired, leaked, no_price = [], [], []
        for q in PRICING_QS:
            for _ in range(REPEATS):
                r = aria.think(q)
                reply, names = r["reply"], [t.get("name") for t in r["tool_calls"]]
                if "remember" in names:
                    fired.append((q, names))
                if "tool_call" in reply or '"arguments"' in reply:
                    leaked.append((q, reply[:80]))
                if not any(p in reply.lower() for p in PRICE_TOKENS):
                    no_price.append((q, reply[:80]))
            print(f"    {q!r} -> {r['reply'][:84]}")
        for q, n in fired:
            print(f"    REMEMBER FIRED: {q!r} -> {n}")
        for q, s in leaked:
            print(f"    SYNTAX LEAKED: {q!r} -> {s}")
        for q, s in no_price:
            print(f"    NO PRICING: {q!r} -> {s}")
        m1 = chk(f"no remember tool fired ({len(PRICING_QS) * REPEATS} runs)", not fired)
        m2 = chk("no tool-call syntax in any spoken reply", not leaked)
        m3 = chk("every answer carries real pricing from docs/user.md", not no_price)
        # The proof that matters: a fabricated fact would be ON DISK. This asserts
        # the file itself, not just the absence of a tool_calls entry.
        m4 = chk("business memory was never written to",
                 not os.path.exists(TMP_MEM) or not open(TMP_MEM, encoding="utf-8").read().strip())
        m3 = m3 and m4

    # --- 8) the full pricing answer: complete, and clean at the TTS input ----
    print(f"\n--- 8) 'give me all the pricing tiers' x{REPEATS} — the real answer ---")
    g1 = g2 = g3 = True
    if m1 is None:
        print("  [skip] needs Ollama.")
        g1 = g2 = g3 = None
    else:
        cut, dirty, missing = [], [], []
        for _ in range(REPEATS):
            reply = aria.think("give me all the pricing tiers")["reply"]
            sent = capture_tts(reply)           # exactly what ElevenLabs receives
            # Truncation shows up as a reply that just stops — no terminal
            # punctuation. That is what "…investor-ready demos or" looked like.
            if not sent.rstrip().endswith((".", "!", "?", '"', "”")):
                cut.append(sent[-60:])
            if any(ch in sent for ch in "*_#`~") or _BULLET_RE.search(sent):
                dirty.append(sent[:70])
            # All four price points present = the list actually finished.
            if not all(p in sent.replace(",", "") for p in ("8", "15", "23", "225")):
                missing.append(sent[:70])
        print(f"  TTS input : {sent}")
        for s in cut:
            print(f"    TRUNCATED: …{s}")
        for s in dirty:
            print(f"    MARKDOWN LEAKED: {s}")
        g1 = chk(f"the pricing answer completes — no mid-sentence cut ({REPEATS} runs)",
                 not cut)
        g2 = chk("zero *, _, #, `, ~ or list bullets reach the TTS input", not dirty)
        g3 = chk("all four tiers survive (8K / 15K / 23K / 225)", not missing)

    # --- 9) markdown shapes gemma3 actually emits ---------------------------
    print("\n--- 9) markdown the model writes unprompted ---")
    md = ("## Pricing\n"
          "*   **Launch — $8K:** first-time founders\n"
          "- __Scale — $15K:__ funded startups\n"
          "1. `Flagship — $23K` established teams\n"
          "---\n"
          "See [our site](https://zentraiq.com) or #ads.")
    mdo = clean_for_speech(md)
    print(f"  {mdo}")
    k1 = chk("no markdown characters survive at all",
             not any(ch in mdo for ch in "*_#`") and not _BULLET_RE.search(mdo))
    k2 = chk("the horizontal rule is gone", "---" not in mdo)
    k3 = chk("a markdown link speaks its label, not its URL",
             "our site" in mdo and "https" not in mdo)
    k4 = chk("'#ads' speaks as 'ads'", "ads" in mdo)
    k5 = chk("the tiers survive the cleaning",
             all(t in mdo for t in ("Launch", "Scale", "Flagship", "$8K", "$23K")))

    results = [b1, b2, b3, b4, b5, b6, b7, t1, t2, t3, a1, a2, a3, a4, a5, a6,
               c1, c2, c3, c4, c5, e1, e2, e3, e4, e5, r1, r2, v1, v2, v3, m1, m2, m3,
               g1, g2, g3, k1, k2, k3, k4, k5]
    all_ok = all(r for r in results if r is not None)
    print("\n" + "=" * 76)
    print("ALL PASS — ARIA speaks prose, never syntax, and never nothing."
          if all_ok else "SOME CHECKS FAILED.")
    print("=" * 76)
    for p in (TMP_DB, TMP_MEM):
        try:
            os.remove(p)
        except OSError:
            pass
    return all_ok


if __name__ == "__main__":
    main()
