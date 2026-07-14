"""Show docs/memory.md with token counts so you can prune stale facts by hand.

This tool NEVER edits the file — it only reports. It shows every fact, marks which
ones actually load into agent prompts (newest-first, within the token cap) versus
which fall off the end, and the total. To forget a fact, delete its line yourself.

    python scripts/prune_memory.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from pathlib import Path

from brain.hermes_common import (BUSINESS_MEMORY_PATH, BUSINESS_MEMORY_MAX_TOKENS,
                                 load_business_memory)


def main():
    path = Path(BUSINESS_MEMORY_PATH)
    print("=" * 68)
    print(f"Business memory — {path}")
    print("=" * 68)
    if not path.exists():
        print("(no memory file yet — ARIA creates it on the first 'remember ...')")
        return

    text = path.read_text(encoding="utf-8")
    facts = [ln.rstrip() for ln in text.splitlines() if ln.strip().startswith("- ")]
    total_tokens = len(text) // 4
    loaded = load_business_memory()
    loaded_set = set(loaded.splitlines())
    loaded_tokens = len(loaded) // 4

    print(f"  facts on file : {len(facts)}")
    print(f"  file size     : ~{total_tokens} tokens")
    print(f"  prompt cap    : {BUSINESS_MEMORY_MAX_TOKENS} tokens (newest-first)")
    print(f"  loaded now    : {len(loaded_set)} facts (~{loaded_tokens} tokens) "
          f"reach the agents")
    if not facts:
        print("\n  (no fact lines yet)")
        return

    print("\n  Facts (newest first). [LOADED] reaches agents; [dropped] is over the cap:")
    for ln in reversed(facts):  # newest first
        tag = "[LOADED] " if ln in loaded_set else "[dropped]"
        print(f"    {tag} {ln}")

    dropped = len(facts) - len(loaded_set)
    if dropped > 0:
        print(f"\n  {dropped} fact(s) fall beyond the {BUSINESS_MEMORY_MAX_TOKENS}-token "
              f"cap and are NOT seen by agents.")
    print(f"\n  To prune: edit {path} and delete stale lines. (No auto-pruning.)")


if __name__ == "__main__":
    main()
