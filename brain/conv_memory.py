"""Stage 3 — conversation memory: ARIA remembers past voice turns across restarts.

Every voice turn (yours, ARIA's, and an agent's during a live conversation) is
written to SQLite as it happens, so closing main.py no longer wipes the thread.

The discipline is the whole point. A raw transcript grows without bound and would
re-introduce the truncation this system just spent a phase eliminating, so the
prompt never sees one:

    last RECENT_TURNS turns  -> verbatim
    everything older         -> ONE rolling summary, capped at SUMMARY_MAX_TOKENS
    the whole block          -> hard-capped at BLOCK_MAX_TOKENS (oldest recent
                                turns dropped first if it would overflow)

So recall is sharp on what was just said and a compressed gist of older material —
which is what fits a 6144-token window on a 4GB GPU.

The summary is produced by Python calling the model to compress the turns that
have just aged out of the verbatim window, then folding that into the previous
summary. The model writes prose; PYTHON enforces the token ceilings, picks what
gets compressed, and renders the block — the model never controls the budget.

Scope is GLOBAL, not per-session: talk to Bobby, then later ask ARIA what you and
Bobby decided, and it's there.
"""
import logging
import re
import threading

import config
from brain.hermes_common import call_hermes, estimate_tokens
from memory import store

logger = logging.getLogger("aria.conv_memory")

RECENT_TURNS = 6            # ~3 exchanges kept word-for-word
SUMMARY_MAX_TOKENS = 150    # the rolling summary of everything older
BLOCK_MAX_TOKENS = 500      # hard ceiling on the entire injected block
COMPRESS_BATCH = 4          # aged-out turns needed before we spend a model call

_OPEN = "<conversation_memory>\n"
_CLOSE = "</conversation_memory>\n\n"
_SUMMARY_LABEL = "Earlier conversation (summary): "
_RECENT_LABEL = "Recent turns:\n"

_SPEAKER_LABELS = {"user": "Ayyan", "aria": "ARIA"}

_compress_lock = threading.Lock()
_compressing = False
_pruned_this_process = False


def _label(speaker):
    return _SPEAKER_LABELS.get((speaker or "").lower(), (speaker or "?").capitalize())


def _truncate_tokens(text, max_tokens):
    """Hard-clip text to max_tokens (Python enforces the ceiling — never the model)."""
    text = " ".join((text or "").split())
    if estimate_tokens(text) <= max_tokens:
        return text
    words = text.split(" ")
    out = []
    for w in words:
        out.append(w)
        if estimate_tokens(" ".join(out)) > max_tokens:
            out.pop()
            break
    return " ".join(out).rstrip(" ,;:") + "…"


# --- Write ------------------------------------------------------------------
def record_turn(session_id, speaker, content, compress=True):
    """Persist one turn. Fire-and-forget compression keeps the voice loop snappy —
    a rolling summary lagging by one batch is harmless."""
    turn_id = store.add_conversation_turn(session_id, speaker, content)
    if turn_id and compress:
        _maybe_compress_async()
    return turn_id


def prune_old(days=None):
    """Drop turns past the retention window. Returns how many were removed."""
    return store.prune_conversations(config.CONVERSATION_MEMORY_DAYS if days is None else days)


def _prune_once():
    global _pruned_this_process
    if _pruned_this_process:
        return
    _pruned_this_process = True
    try:
        n = prune_old()
        if n:
            logger.info("pruned %d conversation turn(s) older than %d days",
                        n, config.CONVERSATION_MEMORY_DAYS)
    except Exception:
        logger.exception("conversation prune failed (non-fatal)")


# --- Compression ------------------------------------------------------------
def _pending_older_turns():
    """Turns that have aged out of the verbatim window but aren't summarized yet."""
    _, upto = store.get_conversation_summary()
    recent_ids = [t["id"] for t in store.get_recent_conversation_turns(RECENT_TURNS)]
    return store.get_conversation_turns_to_compress(upto, recent_ids)


def compress_now(force=False):
    """Fold the aged-out turns into the rolling summary. Synchronous; returns True
    if the summary was updated. Tests call this directly for determinism."""
    older = _pending_older_turns()
    if not older or (len(older) < COMPRESS_BATCH and not force):
        return False
    previous, _ = store.get_conversation_summary()
    transcript = "\n".join(f"{_label(t['speaker'])}: {t['content']}" for t in older)
    prompt = (
        "Compress this voice-assistant conversation history into ONE short "
        "paragraph a colleague could read to know what was discussed and decided.\n"
        "Keep: decisions, preferences, commitments, named things (creative ids, "
        "people, amounts). Drop: pleasantries and filler. Preserve anything the "
        "earlier summary says that still matters.\n"
        "Write at most 100 words, plain prose, no bullet points, no preamble — "
        "output ONLY the paragraph.\n\n"
        + (f"Earlier summary:\n{previous}\n\n" if previous else "")
        + f"Newer turns:\n{transcript}"
    )
    try:
        raw = call_hermes(
            [{"role": "system", "content": "You compress conversation history "
                                           "faithfully and briefly. Never invent."},
             {"role": "user", "content": prompt}],
            model=config.CONVERSATION_SPEED_MODEL, num_predict=220,
            keep_alive="30m", think=False)
    except Exception:
        logger.exception("conversation summary compression failed (non-fatal)")
        return False
    # Python owns the ceiling — a model told "100 words" cannot be trusted with it.
    summary = _truncate_tokens(raw, SUMMARY_MAX_TOKENS)
    if not summary:
        return False
    store.set_conversation_summary(summary, max(t["id"] for t in older))
    logger.info("conversation summary updated (folded %d turn(s), %d tokens)",
                len(older), estimate_tokens(summary))
    return True


def _maybe_compress_async():
    """Compress in the background so a voice turn never waits on it."""
    global _compressing
    with _compress_lock:
        if _compressing:
            return
        if len(_pending_older_turns()) < COMPRESS_BATCH:
            return
        _compressing = True

    def _work():
        global _compressing
        try:
            compress_now()
        finally:
            with _compress_lock:
                _compressing = False

    threading.Thread(target=_work, name="conv-compress", daemon=True).start()


# --- Read -------------------------------------------------------------------
def conversation_block(max_tokens=BLOCK_MAX_TOKENS):
    """The <conversation_memory> block for a prompt, or '' when there's nothing.

    Rendered deterministically and hard-capped: the summary is already bounded at
    SUMMARY_MAX_TOKENS, and the verbatim turns are added NEWEST-first until the
    budget runs out (so the oldest drop), then shown oldest->newest so the thread
    reads in order.
    """
    _prune_once()
    try:
        summary, _ = store.get_conversation_summary()
        turns = store.get_recent_conversation_turns(RECENT_TURNS)   # newest first
    except Exception:
        logger.exception("conversation memory unavailable (non-fatal)")
        return ""
    if not summary and not turns:
        return ""

    summary = _truncate_tokens(summary, SUMMARY_MAX_TOKENS)
    head = _OPEN + (f"{_SUMMARY_LABEL}{summary}\n\n" if summary else "")
    budget = max_tokens - estimate_tokens(head + _RECENT_LABEL + _CLOSE)

    kept, total = [], 0
    for t in turns:                        # newest first: drop the oldest on overflow
        line = f"{_label(t['speaker'])}: {t['content']}"
        cost = estimate_tokens(line) + 1
        if kept and total + cost > budget:
            break
        if cost > budget:                  # one huge turn — clip it rather than skip
            line = f"{_label(t['speaker'])}: {_truncate_tokens(t['content'], max(20, budget - 5))}"
            cost = estimate_tokens(line) + 1
        kept.append(line)
        total += cost
    body = _RECENT_LABEL + "\n".join(reversed(kept)) + "\n" if kept else ""
    return head + body + _CLOSE
