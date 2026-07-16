"""Speech cleaning — the last stop before any text reaches TTS.

Slack is a WRITTEN medium. The agents post *bold*, `code`, :clapper:, cr_A,
<https://x|links> — all correct on screen, all gibberish through a voice: "asterisk
T L semicolon D R asterisk", "colon clapper colon", "C R underscore A". So every
line ARIA or a teammate speaks passes through clean_for_speech() first, which turns
written formatting into speakable prose and guarantees the result is never empty.

Pure and audio-free on purpose: no sounddevice, no ElevenLabs, so the rules are
testable without hardware (scripts/test_speech_output.py). audio.speak calls it as
the single choke point — no caller can forget to.
"""
import re

from brain.hermes_common import strip_tool_syntax

# Bug C — a turn that produces nothing must not produce SILENCE. Silence is
# indistinguishable from a crash, from a missed wake word, from a dead mic: the
# user is left staring at the HUD with no idea whether to repeat themselves.
EMPTY_REPLY_FALLBACK = "I didn't catch that, sir — could you repeat?"

# Slack link syntax: <https://x|label> / <#C123|ads> -> the label; a bare URL or a
# raw <@U123> mention has no speakable form at all, so it goes.
_LINK_LABEL_RE = re.compile(r"<[#@!]?[^<>|]*\|([^<>]*)>")
_BARE_LINK_RE = re.compile(r"<https?://[^<>|\s]+>")
_MENTION_RE = re.compile(r"<[#@!][^<>]*>")

# :clapper: -> nothing. The lookbehind is load-bearing: without it the ':30:' in a
# timestamp like "10:30:15" looks exactly like an emoji code. Requiring a leading
# letter and no adjacent word character keeps clock times and ratios (A:B:C) intact.
_EMOJI_RE = re.compile(r"(?<!\w):[a-z][a-z0-9_+\-]*:(?!\w)", re.IGNORECASE)

# Bobby's creative ids. "cr_A" is a written token — spoken, it wants to be
# "creative A". Runs BEFORE the generic snake_case rule, which would give the
# flatter "C R A".
_CREATIVE_RE = re.compile(r"\bcr_([A-Za-z])\b")

# Generic snake_case (support_intel, chat_bobby, dark_mode) -> spoken as words.
# MUST run before the formatting strip below, which would otherwise weld the
# halves together ("supportintel").
_SNAKE_RE = re.compile(r"\b([A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)\b")

# "~$14" / "~30%" is an APPROXIMATION, not a strikethrough. Dropping the tilde
# silently promotes an estimate to an exact figure, so it becomes a word.
_APPROX_RE = re.compile(r"~(?=[\d$£€])")

_FENCE_RE = re.compile(r"```+")
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*•+]|\d+[.)])\s+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^[ \t]*>+\s*", re.MULTILINE)
_HEADER_RE = re.compile(r"^[ \t]*#{1,6}\s+", re.MULTILINE)
# Whatever formatting survives the rules above is decoration with no spoken form.
_FORMAT_CHARS_RE = re.compile(r"[*_~`]+")


def _join_lines(text):
    """Collapse to one spoken line, giving an unpunctuated line a full stop so TTS
    doesn't run two separate thoughts together in one breath."""
    out = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if out and out[-1][-1] not in ".!?,;:-—":
            out[-1] += "."
        out.append(line)
    return " ".join(out).strip()


def clean_for_speech(text):
    """Turn written text (Slack markup, tool syntax, ids) into speakable prose.

    Returns '' when nothing speakable survives — callers should fall back to
    EMPTY_REPLY_FALLBACK rather than stay silent. Order matters throughout; each
    rule below documents what it must run before or after.
    """
    if not text:
        return ""
    t = strip_tool_syntax(text)                 # machine syntax never reaches the ear
    t = _FENCE_RE.sub(" ", t)
    t = _LINK_LABEL_RE.sub(r"\1", t)
    t = _BARE_LINK_RE.sub(" ", t)
    t = _MENTION_RE.sub(" ", t)
    t = _EMOJI_RE.sub(" ", t)                   # before snake_case: :white_check_mark:
    t = _APPROX_RE.sub("about ", t)             # before the tilde is stripped as markup
    t = _CREATIVE_RE.sub(r"creative \1", t)     # before the generic snake_case rule
    t = _SNAKE_RE.sub(lambda m: m.group(1).replace("_", " "), t)
    t = _BULLET_RE.sub("", t)
    t = _QUOTE_RE.sub("", t)
    t = _HEADER_RE.sub("", t)
    t = _FORMAT_CHARS_RE.sub("", t)             # last: everything meaningful is out
    return _join_lines(t)


def speakable(text):
    """clean_for_speech(), but never empty — the fallback line stands in for
    silence. This is what actually goes to the voice."""
    return clean_for_speech(text) or EMPTY_REPLY_FALLBACK
