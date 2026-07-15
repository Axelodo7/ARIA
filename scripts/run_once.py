"""run_once.py — fire ONE agent cycle by hand, safely, while ARIADaemon is live.

    python scripts/run_once.py ava
    python scripts/run_once.py sarah --force

Why not `python daemon.py --once ava`?
  `--once` does NOT start a second scheduler (it runs one job and returns), so
  that part is fine. The problem is that importing/running daemon.py attaches a
  RotatingFileHandler to logs/daemon.log at module scope — the very file the
  running service already holds open. When either process hits the 5 MB rollover
  it renames daemon.log, which FAILS on Windows while the other holds a handle.
  It also has no mutual exclusion, so it can start a cycle while the service is
  already running one.

This script avoids all of that:
  * It never imports daemon.py, so logs/daemon.log is never opened. Its own
    output goes to logs/run_once.log.
  * It takes an OS-level exclusive lock, so two manual runs can't overlap (the
    lock is released automatically even if the process is killed).
  * It refuses to start if the service is currently mid-cycle (read from
    daemon.log), or if Ollama is down, or if that agent's scheduled run is
    imminent — each overridable with --force.
  * It runs exactly one cycle in-process: no scheduler, no extra agents.

It still honours .env (DRY_RUN etc.) — with DRY_RUN=False this posts to Slack and
files real approvals, exactly as a scheduled cycle would.
"""
import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
for _p in (PROJECT_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests

import config
import service_status                      # reuse the log parser (read-only)
from brain.hermes_common import OLLAMA_URL

LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
RUN_LOG = os.path.join(LOG_DIR, "run_once.log")     # deliberately NOT daemon.log
LOCK_PATH = os.path.join(LOG_DIR, "run_once.lock")

# agent -> (import path, class name, cycle method). The approvals poller is
# intentionally absent: the service already polls every 5 minutes, so there's
# nothing to gain by firing it by hand.
AGENTS = {
    "sarah": ("agents.sarah", "Sarah", "cycle"),
    "bobby": ("agents.bobby", "Bobby", "daily"),
    "ava":   ("agents.ava", "Ava", "daily"),
    "tom":   ("agents.tom", "Tom", "cycle"),
}
IN_FLIGHT_WINDOW_MIN = 30    # a "Running" older than this is a crashed leftover
IMMINENT_MIN = 5             # refuse if the scheduled run is this close

os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("aria")        # agents log under aria.* — capture those too
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh = RotatingFileHandler(RUN_LOG, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_ch)
log = logging.getLogger("aria.run_once")


# --- Safety checks -----------------------------------------------------------
def acquire_lock(path=LOCK_PATH):
    """Exclusive cross-process lock. Returns the held file object, or None if
    another manual run has it. The OS drops the lock if we're killed, so there
    are no stale lock files to clean up."""
    f = open(path, "a+b")
    try:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    except ImportError:          # non-Windows: best-effort, no lock available
        pass
    return f


def ollama_up(timeout=4):
    try:
        url = OLLAMA_URL.replace("/api/chat", "/api/tags")
        return requests.get(url, timeout=timeout).status_code == 200
    except requests.exceptions.RequestException:
        return False


def daemon_cycle_in_flight(entries):
    """(label, started_at) if the service started a cycle it hasn't finished, else
    None. Read straight from daemon.log — we only read it, never open it for write."""
    state = {}
    for dt, _lvl, msg in entries or ():
        if msg.startswith("Running "):
            state[msg[len("Running "):]] = ("running", dt)
        elif msg.startswith("Finished "):
            state[msg[len("Finished "):]] = ("done", dt)
        elif msg.startswith("Job ") and "crashed" in msg:
            parts = msg.split()
            if len(parts) > 1:
                state[parts[1]] = ("done", dt)   # crashed == no longer in flight
    now = datetime.now()
    for label, (st, dt) in state.items():
        if st == "running" and (now - dt) < timedelta(minutes=IN_FLIGHT_WINDOW_MIN):
            return label, dt
    return None


def minutes_to_scheduled(entries, agent):
    """Minutes until this agent's next scheduled run, parsed from the newest
    heartbeat line. None if unknown/stale."""
    hbs = [(dt, msg) for dt, _l, msg in entries or () if msg.startswith("daemon alive")]
    if not hbs:
        return None
    hb_dt, hb_msg = hbs[-1]
    now = datetime.now()
    if (now - hb_dt) > timedelta(minutes=service_status.HEARTBEAT_MAX_AGE_MIN):
        return None      # heartbeat is stale; the schedule line can't be trusted
    m = re.search(rf"\b{re.escape(agent)} (\d{{2}}):(\d{{2}})", hb_msg)
    if not m:
        return None
    nxt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if nxt < now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds() / 60.0


def preflight(agent, force):
    """Return True if it's safe to run. Each block is overridable with --force."""
    ok = True
    if not ollama_up():
        log.error("Ollama is not reachable at localhost:11434 — the cycle would "
                  "just fail. Start it (or the OllamaServe service) first.")
        ok = False   # not force-able: the cycle cannot work without it
        return ok

    entries = service_status.parse_log()
    in_flight = daemon_cycle_in_flight(entries)
    if in_flight:
        label, started = in_flight
        msg = (f"ARIADaemon is mid-cycle right now ({label}, started "
               f"{started:%H:%M:%S}) — running a second cycle would fight it for "
               f"Ollama and could double-post.")
        if force:
            log.warning("%s Proceeding anyway (--force).", msg)
        else:
            log.error("%s Wait for it to finish, or re-run with --force.", msg)
            ok = False

    mins = minutes_to_scheduled(entries, agent)
    if mins is not None and mins < IMMINENT_MIN:
        msg = (f"{agent}'s scheduled run is only {mins:.1f} min away — you'd get "
               f"two cycles back to back.")
        if force:
            log.warning("%s Proceeding anyway (--force).", msg)
        else:
            log.error("%s Wait for the scheduled run, or re-run with --force.", msg)
            ok = False
    return ok


# --- Run ---------------------------------------------------------------------
def run_cycle(agent):
    module_path, cls_name, method = AGENTS[agent]
    mod = __import__(module_path, fromlist=[cls_name])
    instance = getattr(mod, cls_name)()
    log.info("Running %s.%s (manual single-shot)", agent, method)
    result = getattr(instance, method)()
    log.info("Finished %s.%s (manual single-shot)", agent, method)
    return result


def summarize(agent, result):
    if not isinstance(result, dict):
        print(f"\n  {agent}: done. (returned: {str(result)[:200]})")
        return
    print(f"\n  {agent}: done.")
    for key in ("pillar", "chosen_hook", "status", "green", "rejected", "skipped",
                "reason", "payload_id", "video_path"):
        if key in result and result[key] not in (None, ""):
            print(f"    {key:<13} {str(result[key])[:150]}")
    if isinstance(result.get("proposals"), list):
        print(f"    proposals     {len(result['proposals'])} filed")
        for p in result["proposals"]:
            print(f"      - {p.get('action')} {p.get('creative_id')} [{p.get('payload_id')}]")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Fire one agent cycle by hand, safely alongside ARIADaemon.")
    ap.add_argument("agent", choices=sorted(AGENTS),
                    help="which agent's cycle to run once")
    ap.add_argument("--force", action="store_true",
                    help="override the mid-cycle / imminent-schedule guards")
    args = ap.parse_args(argv)

    print("=" * 68)
    print(f"run_once — single {args.agent} cycle (no scheduler, no service restart)")
    print(f"  DRY_RUN : {config.DRY_RUN}"
          f"{'  (nothing posts for real)' if config.DRY_RUN else '  — THIS POSTS TO SLACK FOR REAL'}")
    print(f"  log     : {RUN_LOG}")
    print("=" * 68)

    lock = acquire_lock()
    if lock is None:
        log.error("Another run_once is already in progress (lock held: %s). "
                  "Wait for it to finish.", LOCK_PATH)
        return 1
    try:
        if not preflight(args.agent, args.force):
            return 1
        try:
            result = run_cycle(args.agent)
        except Exception:
            log.exception("Manual %s cycle crashed", args.agent)
            return 1
        summarize(args.agent, result)
        return 0
    finally:
        lock.close()   # OS releases the lock here (and on kill)


if __name__ == "__main__":
    sys.exit(main())
