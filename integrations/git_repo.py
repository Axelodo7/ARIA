"""Thin subprocess-based git client, pointed at Tom's throwaway practice repo.

Everything operates inside config.TOM_REPO_PATH — Tom never touches his own
codebase. Anything that could reach `main` is gated the same defensive way as
the money/publish paths:

  - git_commit_branch refuses when the target branch is main/master.
  - git_push_to_main is PERMANENTLY stubbed to refuse-and-log; it is never
    enabled in this build, approval or not.
"""
import os
import subprocess
import sys

import config

REPO = config.TOM_REPO_PATH
_PROTECTED = {"main", "master"}


def _git(args, check=False):
    """Run a git command inside the repo. Returns the CompletedProcess."""
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=check
    )


def _abs(rel_path):
    return os.path.join(REPO, rel_path)


# --- File access ------------------------------------------------------------
def read_file(rel_path):
    """Return the contents of a file inside the repo."""
    with open(_abs(rel_path), encoding="utf-8") as f:
        return f.read()


def write_file(rel_path, contents):
    """Write contents to a file inside the repo (working tree only)."""
    path = _abs(rel_path)
    os.makedirs(os.path.dirname(path) or REPO, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(contents)
    return rel_path


# --- Inspection -------------------------------------------------------------
def current_branch():
    return _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def run_tests():
    """Run the repo's pytest suite. Returns (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-rfE", "--tb=short"],
        cwd=REPO, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def git_diff(paths=None):
    """Return the working-tree diff against HEAD.

    Pass `paths` (a list of rel paths) to scope the diff to just those files —
    useful so a cycle shows only the files it touched, not changes accumulated
    by earlier cycles in the same run.
    """
    args = ["diff", "HEAD"]
    if paths:
        args += ["--", *paths]
    return _git(args).stdout


# --- Branch operations (main is protected) ----------------------------------
def reset_to_branch(branch):
    """Force the working tree back to a branch head (used by tests to reset).

    Refuses to reset onto a protected branch so this can never disturb main.
    """
    if branch in _PROTECTED:
        msg = f"reset_to_branch refused: '{branch}' is protected"
        print(f"[git] {msg}")
        return {"ok": False, "reason": msg}
    _git(["checkout", "-f", branch])
    _git(["reset", "--hard", branch])
    _git(["clean", "-fd"])
    return {"ok": True, "branch": branch}


def git_commit_branch(branch, message):
    """Commit staged working-tree changes to a named branch — never main.

    Gate: refuses if the target branch is main/master (commits to main not
    permitted). Otherwise switches to the branch, stages everything, commits.
    """
    if branch in _PROTECTED:
        msg = "commits to main not permitted"
        print(f"[git] REFUSED git_commit_branch('{branch}'): {msg}")
        return {"ok": False, "reason": msg, "branch": branch}
    if current_branch() != branch:
        _git(["checkout", branch])
    _git(["add", "-A"])
    proc = _git(["commit", "-m", message])
    ok = proc.returncode == 0
    print(f"[git] git_commit_branch('{branch}'): {'committed' if ok else proc.stdout.strip() or proc.stderr.strip()}")
    return {"ok": ok, "branch": branch, "stdout": proc.stdout, "stderr": proc.stderr}


def git_push_to_main():
    """PERMANENTLY DISABLED. Always refuses.

    Merging/pushing to main is the one action that is code-locked-off in this
    build, regardless of any approval. It never does anything but log and refuse.
    """
    msg = "This function is intentionally never enabled in this build"
    print(f"[git] REFUSED git_push_to_main: {msg}")
    return {"ok": False, "reason": msg}
