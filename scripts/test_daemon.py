"""Step 6 smoke test for the daemon (orchestration only).

Part A — lifecycle: start `python daemon.py`, wait 30s to confirm it comes up
and stays alive with no crash, then send a shutdown signal and confirm it exits.
Part B — --once: for each agent, run `python daemon.py --once <agent>` and
confirm the daemon actually dispatches that agent's cycle (logs "Running
<label>"). The cycle is killed once dispatch is confirmed so we don't wait on
the reasoning model.

Run from the project root:
    python scripts/test_daemon.py

Uses DRY_RUN=True for the subprocesses so nothing posts to Slack during the test.
"""
import os
import signal
import subprocess
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON = os.path.join(PROJECT_ROOT, "daemon.py")
_IS_WIN = os.name == "nt"
_NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WIN else 0


def _env():
    env = dict(os.environ)
    env["DRY_RUN"] = "True"          # no real Slack posts during the test
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def _send_shutdown(proc):
    if _IS_WIN:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.terminate()  # SIGTERM


def test_lifecycle():
    print("\n--- Part A: lifecycle (start, 30s alive, clean shutdown) --------")
    out_path = os.path.join(PROJECT_ROOT, "logs", "_test_daemon_stdout.txt")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    passed = True
    with open(out_path, "w", encoding="utf-8") as f:
        proc = subprocess.Popen([sys.executable, "-u", DAEMON], cwd=PROJECT_ROOT,
                                stdout=f, stderr=subprocess.STDOUT,
                                env=_env(), creationflags=_NEW_GROUP)
        print("  daemon started; watching for 30s...")
        time.sleep(30)
        alive = proc.poll() is None
        passed &= _check("daemon alive after 30s (no startup crash)", alive)
        if not alive:
            print("  daemon exited early; output below:")
            print(open(out_path, encoding="utf-8").read())
            return False

        _send_shutdown(proc)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        exited = proc.poll() is not None
        passed &= _check("daemon exited after shutdown signal", exited)
        passed &= _check("clean exit (code 0)", proc.returncode == 0,
                         f"exit={proc.returncode}")

    out = open(out_path, encoding="utf-8").read()
    passed &= _check("startup banner printed (DRY_RUN + next runs)",
                     "DRY_RUN" in out and "Next run for each job" in out)
    passed &= _check("no traceback during idle run",
                     "Traceback (most recent call last)" not in out)
    return passed


def run_once(agent, label, timeout=30):
    """Start `daemon.py --once <agent>`, confirm it dispatches the cycle, kill it."""
    proc = subprocess.Popen([sys.executable, "-u", DAEMON, "--once", agent],
                            cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            env=_env(), creationflags=_NEW_GROUP)
    seen = False
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    try:
        for line in proc.stdout:
            if "Running" in line and label in line:
                seen = True
                break
    finally:
        watchdog.cancel()
        proc.kill()
        proc.wait()
    return seen


def test_once():
    print("\n--- Part B: --once dispatches each agent's cycle ----------------")
    passed = True
    for agent, label in [("sarah", "sarah.cycle"), ("bobby", "bobby.daily"),
                         ("ava", "ava.daily"), ("tom", "tom.cycle")]:
        ok = run_once(agent, label)
        passed &= _check(f"--once {agent} dispatches {label}", ok)
    return passed


def main():
    print("=" * 68)
    print("Daemon — Step 6 smoke test (orchestration only)")
    print(f"  daemon: {DAEMON}")
    print(f"  platform: {'windows' if _IS_WIN else 'posix'}")
    print("=" * 68)

    a = test_lifecycle()
    b = test_once()

    print("\n" + "=" * 68)
    print(f"RESULT: lifecycle={'PASS' if a else 'FAIL'}  --once={'PASS' if b else 'FAIL'}")
    print("=" * 68)


if __name__ == "__main__":
    main()
