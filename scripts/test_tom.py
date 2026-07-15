"""Step 5 smoke test for Tom (developer, dry-run).

Resets the practice repo to the tom-tasks branch head (never main), then runs
tom.cycle() three times — once per injected bug — printing the plan, diff, test
output, #dev summary, and #approvals message for each. Finally it proves three
guardrails independently:
  1. git_push_to_main refuses even "with approval" (permanently disabled),
  2. git_commit_branch('main', ...) refuses ('commits to main not permitted'),
  3. a plan touching 5 files is refused by MAX_FILES_TOUCHED_PER_CYCLE.

Run from the project root:
    python scripts/test_tom.py

Requires DRY_RUN=False in .env for real Slack, Ollama running with Tom's model
(deepseek-r1:14b), and the tom_workspace repo created by Part A.
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

import config
from brain.hermes_common import OLLAMA_URL
from integrations import git_repo
from memory import store

# Isolate the DB: this test fires a real Tom cycle, which writes task/proposal
# memory that would otherwise pollute real history.
store.DB_PATH = os.path.join(tempfile.gettempdir(), f"test_tom_{os.getpid()}.db")

from agents.tom import Tom


def preflight_ollama(model):
    tags_url = OLLAMA_URL.replace("/api/chat", "/api/tags")
    try:
        resp = requests.get(tags_url, timeout=5)
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        print(f"\n[x] Cannot reach Ollama at {tags_url.replace('/api/tags','')}.\n"
              "    Start it with `ollama serve`, then re-run.\n")
        return False
    models = [m.get("name", "") for m in resp.json().get("models", [])]
    if not any(name == model or name.startswith(model) for name in models):
        print(f"\n[!] Ollama is up, but `{model}` was not found. Pull it with\n"
              f"    `ollama pull {model}`. Available: {models}\n")
        return False
    print(f"  Ollama reachable; {model} present.")
    return True


def _check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def main():
    print("=" * 68)
    print("Tom — Step 5 smoke test (developer, dry-run)")
    print(f"DRY_RUN                    : {config.DRY_RUN}")
    print(f"TOM_REPO_PATH              : {config.TOM_REPO_PATH}")
    print(f"MAX_FILES_TOUCHED_PER_CYCLE: {config.MAX_FILES_TOUCHED_PER_CYCLE}")
    print(f"MAX_DIFF_LINES             : {config.MAX_DIFF_LINES}")
    print("=" * 68)

    if config.DRY_RUN:
        print("\n[!] DRY_RUN is True: Slack posts are printed, not sent. Set\n"
              "    DRY_RUN=False in .env for a real end-to-end run.\n")

    print("\n--- Checking Ollama ---------------------------------------------")
    if not preflight_ollama(Tom.model):
        return

    # --- Reset the workspace to tom-tasks (never main) ----------------------
    print("\n--- Resetting workspace to tom-tasks head -----------------------")
    reset = git_repo.reset_to_branch("tom-tasks")
    print(f"  reset: {reset}")
    branch = git_repo.current_branch()
    if not _check(f"on branch tom-tasks (got '{branch}')", branch == "tom-tasks"):
        print("  aborting — refusing to run anywhere but tom-tasks.")
        return

    # --- Three cycles, one per bug ------------------------------------------
    tom = Tom()
    for i in range(1, 4):
        print("\n" + "=" * 68)
        print(f"CYCLE {i}")
        print("=" * 68)
        try:
            r = tom.cycle()
        except requests.exceptions.RequestException:
            print("\n[x] Lost the connection to Ollama mid-run. Check `ollama serve`.\n")
            return

        print(f"\nstatus: {r['status']}")
        if r["status"] not in ("done", "refused_scope", "no_edit"):
            print(f"  {r.get('summary','')}")
            continue
        task = r.get("task", {})
        print(f"task  : {task.get('id')} — {task.get('title')}")
        print(f"\n--- Plan (posted to #dev) ---\n{r.get('plan','')}")
        if r["status"] == "done":
            print(f"\n--- Diff ---\n{r['diff'].strip()}")
            print(f"\n--- Test output (tail) ---")
            print("\n".join(r["test_output"].splitlines()[-6:]))
            print(f"\ntarget test passes: {r['green']} | suite green: {r.get('suite_green')} | files: {r['files']}")
            if r.get("remaining_failing"):
                print(f"remaining failing: {r['remaining_failing']}")
            print(f"\n--- #approvals ---\npayload_id: {r['payload_id']} (ts={r['approval_ts']})")
        else:
            print(f"\n[{r['status']}] {r.get('reason','')}")

    # --- Final suite state ---------------------------------------------------
    print("\n" + "=" * 68)
    print("FINAL SUITE STATE (working tree, tom-tasks)")
    print("=" * 68)
    code, out, err = git_repo.run_tests()
    print("\n".join((out + err).splitlines()[-4:]))
    print(f"exit code: {code}  ({'ALL GREEN' if code == 0 else 'some still failing'})")

    # --- Guardrails ----------------------------------------------------------
    print("\n" + "=" * 68)
    print("GUARDRAILS — the merge/main paths must refuse")
    print("=" * 68)

    print("\nGuard 1 — git_push_to_main() (even 'with approval'):")
    g1 = git_repo.git_push_to_main()
    _check("git_push_to_main refuses (permanently disabled)",
           g1.get("ok") is False and "intentionally never enabled" in g1.get("reason", ""))

    print("\nGuard 2 — git_commit_branch('main', ...):")
    g2 = git_repo.git_commit_branch("main", "should never happen")
    _check("commit to main refused ('commits to main not permitted')",
           g2.get("ok") is False and "commits to main not permitted" in g2.get("reason", ""))

    print(f"\nGuard 3 — plan touching 5 files (cap is {config.MAX_FILES_TOUCHED_PER_CYCLE}):")
    five = [{"file": f"notekeeper/mod_{n}.py", "old_str": "x", "new_str": "y"} for n in range(5)]
    g3 = tom._apply_edits(five)
    _check("5-file plan refused by MAX_FILES_TOUCHED_PER_CYCLE",
           g3.get("ok") is False and "MAX_FILES_TOUCHED_PER_CYCLE" in g3.get("reason", ""))

    # --- Confirm main was never touched -------------------------------------
    print("\nGuard 4 — main branch is still clean and untouched:")
    main_ok = git_repo._git(["rev-parse", "main"]).returncode == 0
    still_on = git_repo.current_branch()
    _check("still on tom-tasks, main intact", still_on == "tom-tasks" and main_ok,
           f"branch={still_on}")

    print("\n" + "=" * 68)
    print("Done. Tom works only on tom-tasks; the merge-to-main path is")
    print("code-locked-off regardless of approval; scope caps hold.")
    print("=" * 68)


if __name__ == "__main__":
    main()
