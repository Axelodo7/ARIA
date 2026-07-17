"""Inspect and selectively clear conversation memory (Stage 3).

Why this exists: conversation memory faithfully stores whatever was said —
including a BAD answer. gemma3 then follows the pattern it sees in context, so one
wrong reply gets repeated forever and re-recorded each time, reinforcing itself.
Measured: with a poisoned exchange in memory, a pricing question misrouted 3/3;
with those rows removed and NOTHING else changed, 0/3. Prompt instructions do not
help (an explicit "never reuse a previous answer" guard was still 3/3) — the rows
have to go.

Safety: this NEVER deletes on its own. Run it with no arguments to review; deleting
requires you to name the exact ids.

    python scripts/prune_conversation.py                 # review (default)
    python scripts/prune_conversation.py --delete 18,20  # delete those ids only
    python scripts/prune_conversation.py --reset-summary # clear the rolling summary
"""
import argparse
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from memory import store

# Replies that are a DATA DUMP rather than conversation. These are what poison the
# well: a structured relay (an approvals list, a channel read) stored verbatim
# becomes a template the model reuses for unrelated questions.
_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
_SUSPECT_RES = [
    re.compile(rf"you have {_NUM}\s+pending", re.IGNORECASE),
    re.compile(r"\bpending:\s", re.IGNORECASE),
    re.compile(r"^most recent from ", re.IGNORECASE),
    re.compile(r"\bno pending approvals\b", re.IGNORECASE),
    # A reply that merely NARRATES the bad answer is poison too — e.g. a recap
    # saying "I provided a list of seven pending proposals" still teaches the model
    # that a pricing question is answered with the approvals queue.
    re.compile(rf"{_NUM}\s+pending\s+(?:proposals?|approvals?|items?)", re.IGNORECASE),
    re.compile(r"\blist of .{0,20}pending\b", re.IGNORECASE),
    # A stored LIVE-DATA READING (AQI / temperature / pollutant) is poison of the
    # same kind: it was a fabricated or now-stale value, and once in memory the model
    # cites it as fact for a later "what's the air quality" — the "78 ppm / 86°F"
    # resurfacing bug. Live readings are ephemeral; they never belong in memory as an
    # answer. (New ones can't get in — search replies are stored by reference — so
    # this only surfaces pre-existing rows to remove.)
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:°\s*[fc]\b|°(?!\w)|degrees?\s+(?:f\b|c\b|"
               r"fahrenheit|celsius)|ppm\b|µg\b|pm\s?2\.?5\b|pm\s?10\b)", re.IGNORECASE),
    re.compile(rf"\b(?:air[\s-]?quality|aqi)\b[^.]{{0,25}}?{_NUM}", re.IGNORECASE),
]


def is_suspect(turn):
    """True if this stored reply looks like a relayed data dump (poison risk)."""
    if (turn.get("speaker") or "").lower() == "user":
        return False
    c = turn.get("content") or ""
    return any(rx.search(c) for rx in _SUSPECT_RES)


def review():
    turns = store.get_recent_conversation_turns(500)
    summary, upto = store.get_conversation_summary()
    print("=" * 78)
    print(f"Conversation memory — {store.DB_PATH}")
    print("=" * 78)
    if not turns:
        print("  (empty)")
    else:
        print(f"  {len(turns)} turns. [SUSPECT] = a relayed data dump; storing it verbatim")
        print("  lets the model reuse it as the answer to an unrelated question.\n")
        for t in reversed(turns):   # oldest first, reads like the thread
            flag = "[SUSPECT]" if is_suspect(t) else "         "
            when = (t.get("timestamp") or "")[:16].replace("T", " ")
            print(f"  {flag} id={t['id']:<4} {when}  [{t['speaker']}]")
            print(f"            {(t['content'] or '')[:150]}")
    suspects = [t["id"] for t in turns if is_suspect(t)]
    print("\n" + "-" * 78)
    print(f"  rolling summary (upto_id={upto}): "
          f"{(summary[:150] + '...') if summary else '(empty)'}")
    if summary and any(rx.search(summary) for rx in _SUSPECT_RES):
        print("  !! the SUMMARY also looks poisoned — clear it with --reset-summary")
    print("-" * 78)
    if suspects:
        print(f"\n  {len(suspects)} suspect row(s): {suspects}")
        print("  Review the text above, then delete ONLY what you agree is junk:")
        print(f"    python scripts/prune_conversation.py --delete {','.join(map(str, suspects))}")
        print("\n  Tip: a suspect reply usually has a legitimate question right before it.")
        print("  Deleting just the reply keeps the question but removes the bad pattern;")
        print("  delete both if the exchange is worthless.")
    else:
        print("\n  No suspect rows found.")


def delete(ids):
    turns = {t["id"]: t for t in store.get_recent_conversation_turns(500)}
    missing = [i for i in ids if i not in turns]
    if missing:
        print(f"  [x] no such turn id(s): {missing} — nothing deleted.")
        return 1
    print("  Deleting:")
    for i in ids:
        t = turns[i]
        print(f"    id={i} [{t['speaker']}] {(t['content'] or '')[:110]}")
    with store._connect() as conn:
        cur = conn.executemany("DELETE FROM conversation_memory WHERE id = ?",
                               [(i,) for i in ids])
        conn.commit()
    print(f"\n  Removed {cur.rowcount} row(s). {len(store.get_recent_conversation_turns(500))} "
          f"turn(s) remain.")
    _, upto = store.get_conversation_summary()
    if upto and any(i <= upto for i in ids):
        print("  NOTE: some deleted turns were already folded into the rolling summary; "
              "their gist survives there. Use --reset-summary if that matters.")
    return 0


def reset_summary():
    summary, upto = store.get_conversation_summary()
    print(f"  Clearing rolling summary (was upto_id={upto}): {(summary or '(empty)')[:120]}")
    store.set_conversation_summary("", 0)
    print("  Done. It will rebuild from the turns still on file.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Review and selectively clear conversation memory. "
                    "Never deletes unless you name ids.")
    ap.add_argument("--delete", metavar="IDS",
                    help="comma-separated turn ids to delete (e.g. 18,20,22)")
    ap.add_argument("--reset-summary", action="store_true",
                    help="clear the rolling summary (use if it absorbed a bad answer)")
    args = ap.parse_args(argv)

    if args.reset_summary:
        return reset_summary()
    if args.delete:
        try:
            ids = [int(x) for x in args.delete.split(",") if x.strip()]
        except ValueError:
            print("  [x] --delete takes integers, e.g. --delete 18,20")
            return 1
        return delete(ids)
    review()
    return 0


if __name__ == "__main__":
    sys.exit(main())
