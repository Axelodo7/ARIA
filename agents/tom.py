"""Tom — the developer agent.

cycle() works ONE failing task in the practice repo, on the tom-tasks branch:
  1. read TASKS.md and run the suite to see what's failing,
  2. pick the first task whose test is still failing,
  3. post a one-paragraph plan to #dev (model),
  4. read only the file the task names,
  5. propose a structured JSON diff {"file","old_str","new_str"} (model),
  6. apply it via write_file on tom-tasks — refusing anything over the scope
     caps (files touched, diff lines) or that edits a test,
  7. run the suite again,
  8. post a summary to #dev (plan, diff, test output, green?),
  9. file a merge-to-main approval to #approvals — which, even if approved,
     still refuses, because git_push_to_main is permanently disabled.

Tom never operates on main and never touches his own codebase. The reasoning
model does the fix; Python enforces scope and the branch guard.
"""
import re
import time

import config
from agents.base_agent import BaseAgent, load_agent_prompt, strip_reasoning
from brain.hermes_common import _first_json_object
from integrations import slack_bus, git_repo


def _parse_tasks(md):
    """Parse TASKS.md into [{id, title, test, file}] in listed order."""
    tasks = []
    for block in re.split(r"^##\s+", md, flags=re.M)[1:]:
        head = block.splitlines()[0]
        m_id = re.match(r"(TASK-\d+):\s*(.*)", head)
        m_test = re.search(r"Failing test:\s*(\S+)", block)
        m_file = re.search(r"Likely file:\s*(\S+)", block)
        if m_id and m_test and m_file:
            tasks.append({
                "id": m_id.group(1), "title": m_id.group(2).strip(),
                "test": m_test.group(1).replace("\\", "/"),
                "file": m_file.group(1).replace("\\", "/"),
            })
    return tasks


def _failing_test_ids(output):
    """Extract failing pytest node ids from run output (normalized to '/')."""
    normalized = output.replace("\\", "/")
    return set(re.findall(r"^FAILED (\S+)", normalized, re.M))


def _plan_task(task, test_src):
    return (
        "You have ONE task to fix in the notekeeper practice repo.\n\n"
        f"{task['id']}: {task['title']}\n"
        f"Failing test: {task['test']}\n"
        f"Target file: {task['file']}\n\n"
        f"The failing test's source:\n```python\n{test_src}\n```\n\n"
        "Write a ONE-PARAGRAPH plan for #dev: what you'll change, in which file, "
        "and how you'll verify (which test must go green). Plain technical "
        "English, no code yet. If you're not confident the fix is safe, say so."
    )


def _diff_task(task, file_src, test_src):
    return (
        f"Fix {task['id']}: {task['title']} in {task['file']}. Make the SMALLEST "
        "change that makes the failing test pass — no refactors, nothing "
        "unrelated.\n\n"
        f"Current contents of {task['file']}:\n```python\n{file_src}\n```\n\n"
        f"The failing test ({task['test']}):\n```python\n{test_src}\n```\n\n"
        "Output ONLY a single JSON object (no markdown fences, no prose before "
        "or after) of the form:\n"
        f'{{"file": "{task["file"]}", "old_str": "<exact snippet to replace, '
        'copied character-for-character from the file above including '
        'indentation>", "new_str": "<the replacement snippet>"}\n'
        "old_str must appear verbatim in the file and be just large enough to be "
        "unique. Do not edit any test file."
    )


class Tom(BaseAgent):
    name = "Tom"
    icon = config.PERSONAS["Tom"]
    channel = config.CHANNELS["dev"]
    model = config.AGENT_MODELS["Tom"]
    system_prompt = load_agent_prompt("Tom")
    # No model-callable tools: Tom proposes edits as JSON; Python applies them
    # under scope caps, and the merge path is code-locked-off.

    def cycle(self):
        """Work one failing task on tom-tasks. Returns a dict for tests/logging."""
        branch = git_repo.current_branch()
        if branch in ("main", "master"):
            msg = f"Refusing to work on protected branch '{branch}'. Aborting."
            self.post(msg)
            return {"status": "refused_branch", "branch": branch, "summary": msg}

        tasks = _parse_tasks(git_repo.read_file("TASKS.md"))
        _, out, err = git_repo.run_tests()
        failing = _failing_test_ids(out + "\n" + err)
        task = next((t for t in tasks if t["test"] in failing), None)
        if task is None:
            msg = "No failing tasks — the suite is green. Nothing to do."
            self.post(msg)
            return {"status": "noop", "summary": msg, "failing": sorted(failing)}

        test_file = task["test"].split("::")[0]
        test_src = git_repo.read_file(test_file)
        file_src = git_repo.read_file(task["file"])

        # 3. Plan first.
        plan = strip_reasoning(self.run_turn(_plan_task(task, test_src)))
        self.post(f":memo: *Plan — {task['id']}: {task['title']}*\n{plan}")

        # 5. Propose a structured diff.
        raw = strip_reasoning(self.run_turn(_diff_task(task, file_src, test_src)))
        obj = _first_json_object(raw)
        edits = self._normalize_edits(obj)
        if not edits:
            summary = (f":warning: *{task['id']}* — couldn't produce a valid edit. "
                       "Flagging for human review rather than guessing.")
            self.post(summary)
            return {"status": "no_edit", "task": task, "plan": plan, "summary": summary}

        # 6. Apply under scope caps (branch already guarded above).
        applied = self._apply_edits(edits)
        if not applied["ok"]:
            summary = f":no_entry: *{task['id']}* — refusing to apply: {applied['reason']}"
            self.post(summary)
            return {"status": "refused_scope", "task": task, "plan": plan,
                    "reason": applied["reason"], "summary": summary, "edits": edits}

        # 7. Verify. "green" means TOM'S target test now passes — not the whole
        # suite, which may still have other known-failing tasks he hasn't reached.
        diff_text = git_repo.git_diff(applied["files"])
        code, out2, err2 = git_repo.run_tests()
        test_output = (out2 + err2).strip()
        failing_after = _failing_test_ids(out2 + "\n" + err2)
        green = task["test"] not in failing_after   # target test fixed?
        suite_green = code == 0

        # 8. Summary to #dev.
        summary = self._format_summary(task, plan, diff_text, test_output, green,
                                       sorted(failing_after))
        self.post(summary)

        # 9. Merge-to-main approval (which, if approved, STILL refuses).
        payload_id = f"tom-merge-{task['id']}-{int(time.time())}"
        approval_desc = (
            f"TL;DR: merge {task['id']} fix to main. Target test: "
            f"{'PASSES ✅' if green else 'STILL FAILS ❌'}. Files: {applied['files']}. "
            f"NOTE: the merge path (git_push_to_main) is disabled in this build, "
            f"so approving this is a no-op. [{payload_id}]"
        )
        approval_ts = slack_bus.request_approval("Tom", approval_desc, payload_id)

        return {
            "status": "done", "task": task, "plan": plan, "diff": diff_text,
            "test_output": test_output, "green": green, "suite_green": suite_green,
            "remaining_failing": sorted(failing_after), "summary": summary,
            "edits": edits, "files": applied["files"],
            "payload_id": payload_id, "approval_ts": approval_ts,
        }

    @staticmethod
    def _normalize_edits(obj):
        """Coerce the model's JSON into a list of {file, old_str, new_str}."""
        if not isinstance(obj, dict):
            return []
        if isinstance(obj.get("edits"), list):
            candidates = obj["edits"]
        elif "file" in obj and "old_str" in obj:
            candidates = [obj]
        else:
            return []
        edits = []
        for e in candidates:
            if isinstance(e, dict) and e.get("file") and "old_str" in e and "new_str" in e:
                edits.append({"file": str(e["file"]).replace("\\", "/"),
                              "old_str": str(e["old_str"]), "new_str": str(e["new_str"])})
        return edits

    def _apply_edits(self, edits):
        """Apply edits to the working tree, refusing anything out of scope.

        Checks, in order: no test files, files-touched cap, diff-lines cap, and
        that each old_str is actually present. Nothing is written unless all
        checks pass.
        """
        files = [e["file"] for e in edits]
        if any(f.startswith("tests/") for f in files):
            return {"ok": False, "reason": "edits must fix source files, not tests"}
        distinct = sorted(set(files))
        if len(distinct) > config.MAX_FILES_TOUCHED_PER_CYCLE:
            return {"ok": False, "reason":
                    f"plan would touch {len(distinct)} files, over "
                    f"MAX_FILES_TOUCHED_PER_CYCLE={config.MAX_FILES_TOUCHED_PER_CYCLE}"}
        diff_lines = sum(len(e["old_str"].splitlines()) + len(e["new_str"].splitlines())
                         for e in edits)
        if diff_lines > config.MAX_DIFF_LINES:
            return {"ok": False, "reason":
                    f"diff is {diff_lines} lines, over MAX_DIFF_LINES={config.MAX_DIFF_LINES}"}
        # Pre-check every old_str matches EXACTLY once before writing anything —
        # an ambiguous snippet could patch the wrong line (e.g. self._save(notes)
        # appears in both add() and delete()).
        contents = {}
        for e in edits:
            src = git_repo.read_file(e["file"])
            count = src.count(e["old_str"]) if e["old_str"] else 0
            if count != 1:
                return {"ok": False, "reason":
                        f"old_str must match exactly once in {e['file']} (found {count})"}
            contents[e["file"]] = src
        for e in edits:
            new_content = contents[e["file"]].replace(e["old_str"], e["new_str"], 1)
            contents[e["file"]] = new_content
            git_repo.write_file(e["file"], new_content)
        return {"ok": True, "files": distinct}

    @staticmethod
    def _format_summary(task, plan, diff_text, test_output, green, remaining_failing):
        target = task["test"].split("::")[-1]
        status = (f"target test `{target}` now PASSES ✅" if green
                  else f"target test `{target}` STILL FAILS ❌")
        others = [t for t in remaining_failing if t != task["test"]]
        suite_note = ("suite fully green." if not remaining_failing
                      else f"suite still has {len(remaining_failing)} failing "
                           f"(other known tasks): {others or remaining_failing}")
        tail = "\n".join(test_output.splitlines()[-8:])  # last few lines of pytest
        return (
            f":hammer_and_wrench: *{task['id']}: {task['title']}* — {status}\n"
            f"({suite_note})\n\n"
            f"*Plan:* {plan}\n\n"
            f"*Diff (this cycle's files, vs HEAD):*\n```\n{diff_text.strip() or '(no diff)'}\n```\n"
            f"*Test output (tail):*\n```\n{tail}\n```\n"
            f"_Merge-to-main filed to #approvals — but the merge path is disabled "
            f"in this build, so nothing lands on main regardless._"
        )
