"""service_status.py — quick health check for the ARIA daemon.

Answers, without you having to read the raw log:
  * Is the Windows service running?
  * When did each agent last fire, and when is its next run?
  * Is the daemon actually alive (recent heartbeat)?
  * Any errors in the last 24 hours?

    python scripts/service_status.py

Reads logs/daemon.log and queries the service via `sc query`. Works whether or
not the service is installed (it degrades gracefully to just the log view).
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

SERVICE_NAME = "ARIADaemon"   # must match scripts/install_service.md
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "daemon.log")

# Job label (as logged by "Running/Finished <label>") -> friendly agent name.
LABEL_TO_AGENT = {
    "sarah.cycle": "sarah", "bobby.daily": "bobby", "ava.daily": "ava",
    "tom.cycle": "tom", "approval_poller": "approvals",
}
HEARTBEAT_MAX_AGE_MIN = 35   # heartbeat fires every 30 min; older than this = stale

_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+(\w+)\s+(.*)$")


def parse_log(path=LOG_PATH):
    """Return [(datetime, level, message)] for every timestamped log line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read().splitlines()
    except (FileNotFoundError, OSError):
        return None
    out = []
    for line in raw:
        m = _LINE_RE.match(line)
        if not m:
            continue  # continuation lines (e.g. traceback bodies)
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        out.append((dt, m.group(2), m.group(3)))
    return out


def service_state():
    """Return a human string for the service state via `sc query`."""
    try:
        p = subprocess.run(["sc", "query", SERVICE_NAME],
                           capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return "unknown (could not run `sc query` — non-Windows?)"
    if "1060" in p.stdout or "does not exist" in (p.stdout + p.stderr).lower():
        return f"NOT INSTALLED (no service named {SERVICE_NAME})"
    m = re.search(r"STATE\s+:\s+\d+\s+(\w+)", p.stdout)
    return m.group(1) if m else "unknown (unexpected `sc query` output)"


def _ago(dt, now):
    secs = (now - dt).total_seconds()
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    if secs < 172800:
        return f"{secs / 3600:.1f}h ago"
    return f"{int(secs / 86400)}d ago"


def main():
    now = datetime.now()
    print("=" * 68)
    print(f"ARIA daemon health — {now:%Y-%m-%d %H:%M:%S}")
    print("=" * 68)
    print(f"  service ({SERVICE_NAME}): {service_state()}")

    entries = parse_log()
    if entries is None:
        print(f"\n  No log at {LOG_PATH} — the daemon has not run yet "
              "(or logs/ was cleared).")
        return
    if not entries:
        print(f"\n  Log exists but has no parseable lines yet: {LOG_PATH}")
        return

    # --- heartbeat / alive ---------------------------------------------------
    heartbeats = [(dt, msg) for dt, _lvl, msg in entries if msg.startswith("daemon alive")]
    print("\n  heartbeat:")
    if heartbeats:
        hb_dt, hb_msg = heartbeats[-1]
        stale = (now - hb_dt) > timedelta(minutes=HEARTBEAT_MAX_AGE_MIN)
        flag = "  [STALE — daemon may be stopped/hung]" if stale else "  [alive]"
        print(f"    last: {hb_dt:%Y-%m-%d %H:%M:%S} ({_ago(hb_dt, now)}){flag}")
        nxt = hb_msg.split("next runs:", 1)
        if len(nxt) == 2:
            print(f"    next runs (from heartbeat): {nxt[1].strip()}")
    else:
        print("    (no heartbeat logged yet — daemon may have just started)")

    # --- last fired per agent ------------------------------------------------
    last_finished, last_started = {}, {}
    for dt, _lvl, msg in entries:
        for label, agent in LABEL_TO_AGENT.items():
            if msg == f"Finished {label}":
                last_finished[agent] = dt
            elif msg == f"Running {label}":
                last_started[agent] = dt
    print("\n  last agent cycles (completed):")
    for agent in ("sarah", "bobby", "ava", "tom", "approvals"):
        dt = last_finished.get(agent)
        if dt:
            running = last_started.get(agent, dt) > dt  # started but not finished
            tag = "  [running now]" if running else ""
            print(f"    {agent:<10} {dt:%Y-%m-%d %H:%M:%S} ({_ago(dt, now)}){tag}")
        elif agent in last_started:
            print(f"    {agent:<10} started {_ago(last_started[agent], now)}, "
                  "not yet finished")
        else:
            print(f"    {agent:<10} (no completed run in the log)")

    # --- errors in the last 24h ---------------------------------------------
    cutoff = now - timedelta(hours=24)
    errors = [(dt, msg) for dt, lvl, msg in entries
              if dt >= cutoff and (lvl == "ERROR" or "crashed" in msg or "failed" in msg)]
    ollama_waits = [dt for dt, lvl, msg in entries
                    if dt >= cutoff and "Ollama not reachable" in msg]
    print(f"\n  errors in last 24h: {len(errors)}")
    for dt, msg in errors[-5:]:
        print(f"    {dt:%m-%d %H:%M} {msg[:90]}")
    if ollama_waits:
        print(f"  Ollama-not-reachable warnings in last 24h: {len(ollama_waits)} "
              f"(last {_ago(ollama_waits[-1], now)}) — normal right after a reboot.")

    print("\n  full log: " + LOG_PATH)


if __name__ == "__main__":
    main()
