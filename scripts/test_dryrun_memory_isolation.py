"""A dry run must never write the live docs/memory.md.

Why this exists: append_business_fact had no run-mode gate, so ANY dry run that
reached it — a test, a `--once` dry cycle, a misfiring `remember` tool call —
wrote straight into real business memory. That is not hypothetical: while fixing
the remember misfire, a fabricated "fact" ("ZentraIQ offers three pricing
tiers...") landed in the user's docs/memory.md and had to be pruned by hand. Every
agent then reads that file into its prompt as truth, so one bad line becomes
something the whole team believes.

The gate mirrors agent_memory's dry_run column: the write still happens and still
round-trips, it just lands in an isolated namespace (docs/memory.dryrun.md).

Gates:
  * a dry-run write does NOT touch the live file — asserted on the file's BYTES,
    not on a return value,
  * it lands in the dry-run overlay instead, and a dry run can read it back,
  * a REAL run never sees a dry-run fact (no leak in the other direction),
  * a dry run still sees the real facts, so prompts stay production-sized (the
    prompt_eval budgets are all measured under DRY_RUN),
  * a test that aims BUSINESS_MEMORY_PATH at its own temp file is left alone,
  * ARIA's real remember path, end to end, cannot pollute the live file.

This test deliberately targets the LIVE path — that is the only way to prove the
protection. It never runs a write with DRY_RUN off, and it restores everything it
touches.

    python scripts/test_dryrun_memory_isolation.py
"""
import hashlib
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

from pathlib import Path

import config
from memory import store

TMP_DB = os.path.join(tempfile.gettempdir(), f"test_dryiso_{os.getpid()}.db")
store.DB_PATH = TMP_DB

import brain.hermes_common as HC
from brain.hermes_common import (append_business_fact, load_business_memory,
                                 DRY_RUN_MEMORY_PATH, _LIVE_BUSINESS_MEMORY_PATH)
from brain.aria_agent import Aria

LIVE = Path(_LIVE_BUSINESS_MEMORY_PATH)
OVERLAY = Path(DRY_RUN_MEMORY_PATH)


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def digest(p):
    """Hash of the live file's exact bytes — the only honest 'untouched' check."""
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "(absent)"


def main():
    print("=" * 74)
    print("Dry-run isolation — the live docs/memory.md is never written by a test")
    print("=" * 74)
    print(f"  live    : {LIVE}")
    print(f"  overlay : {OVERLAY}")
    if not config.DRY_RUN:
        print("[x] DRY_RUN is not on; this test must not run writes for real.")
        return False

    # Preserve anything already on disk so this test can't destroy real state.
    saved_overlay = OVERLAY.read_text(encoding="utf-8") if OVERLAY.exists() else None
    if OVERLAY.exists():
        OVERLAY.unlink()
    before = digest(LIVE)
    live_facts_before = HC._read_facts(LIVE)
    print(f"  live sha256 before: {before[:16]}…  ({len(live_facts_before)} real fact(s))")

    try:
        # --- 1) the write goes to the overlay, not the live file --------------
        print("\n--- 1) a dry-run remember, aimed at the DEFAULT (live) path ---")
        fake = "ARIA can file taxes and pilot a helicopter"
        line = append_business_fact(fake)
        print(f"  returned : {line!r}")
        w1 = chk("the live file's bytes are UNCHANGED", digest(LIVE) == before)
        w2 = chk("the fabricated fact is NOT in the live file",
                 fake not in (LIVE.read_text(encoding="utf-8") if LIVE.exists() else ""))
        w3 = chk("it landed in the dry-run overlay instead",
                 OVERLAY.exists() and fake in OVERLAY.read_text(encoding="utf-8"))
        w4 = chk("the write is still reported honestly (line returned)",
                 bool(line) and fake in line)

        # --- 2) a dry run reads back its own fact, AND the real ones ----------
        print("\n--- 2) what a DRY run sees ---")
        dry_block = load_business_memory()
        d1 = chk("the dry run can read its own fact back (round-trip)",
                 fake in dry_block)
        d2 = chk("the dry run still sees the REAL facts (prompt stays production-sized)",
                 all(f.split(": ", 1)[-1][:40] in dry_block for f in live_facts_before)
                 if live_facts_before else True)
        d3 = chk("dry-run facts come first (newest)",
                 dry_block.splitlines()[0].endswith(fake + ".") if dry_block else False)

        # --- 3) a REAL run must never see a dry-run fact ----------------------
        print("\n--- 3) what a REAL run sees (read-only — no write happens here) ---")
        config.DRY_RUN = False
        try:
            real_block = load_business_memory()
        finally:
            config.DRY_RUN = True
        r1 = chk("a REAL run does NOT see the dry-run fact", fake not in real_block)
        r2 = chk("a REAL run still sees the real facts",
                 len(HC._read_facts(LIVE)) == len(live_facts_before))

        # --- 4) an explicitly-aimed path is left alone ------------------------
        print("\n--- 4) a test's own temp path is NOT redirected ---")
        tmp_mem = Path(tempfile.gettempdir()) / f"test_dryiso_{os.getpid()}.md"
        real_path, HC.BUSINESS_MEMORY_PATH = HC.BUSINESS_MEMORY_PATH, tmp_mem
        try:
            append_business_fact("a temp-file fact")
            t1 = chk("the write lands in the temp file (isolation already achieved)",
                     tmp_mem.exists() and "a temp-file fact" in tmp_mem.read_text(encoding="utf-8"))
            t2 = chk("...and is readable back from it (test round-trip intact)",
                     "a temp-file fact" in load_business_memory())
        finally:
            HC.BUSINESS_MEMORY_PATH = real_path
            tmp_mem.unlink(missing_ok=True)

        # --- 5) end to end: ARIA's real remember path -------------------------
        print("\n--- 5) ARIA's remember path, end to end, on the live default ---")
        before_e2e = digest(LIVE)
        res = Aria().think("remember that we signed a $500k contract with Initech")
        print(f"  reply: {res['reply']!r}")
        e1 = chk("ARIA still confirms the save", res["reply"] == "Noted, sir.")
        e2 = chk("the live file is STILL untouched", digest(LIVE) == before_e2e)
        e3 = chk("the fact went to the overlay",
                 "Initech" in OVERLAY.read_text(encoding="utf-8"))

        all_ok = all([w1, w2, w3, w4, d1, d2, d3, r1, r2, t1, t2, e1, e2, e3])
    finally:
        # Restore the overlay to exactly how it was found.
        if saved_overlay is None:
            OVERLAY.unlink(missing_ok=True)
        else:
            OVERLAY.write_text(saved_overlay, encoding="utf-8")
        try:
            os.remove(TMP_DB)
        except OSError:
            pass

    final = digest(LIVE)
    print(f"\n  live sha256 after : {final[:16]}…")
    all_ok = all_ok and chk("live docs/memory.md is byte-identical to the start",
                            final == before)
    print("\n" + "=" * 74)
    print("ALL PASS — dry runs cannot pollute live business memory."
          if all_ok else "SOME CHECKS FAILED.")
    print("=" * 74)
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
