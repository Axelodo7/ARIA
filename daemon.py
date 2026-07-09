"""daemon.py — the 24/7 agent-team process.

This is what "works while I'm offline" actually is: a long-running scheduler
that fires each agent's cycle on its own cadence and polls #approvals for your
reactions. It ONLY automates *when* cycles run — it does not change *what*
happens inside them. DRY_RUN behaviour is untouched: Bobby still can't spend,
Ava still can't publish, Tom still can't merge.

Schedule
  Sarah  — every 2 hours   (support digests)
  Bobby  — daily 09:00     (ads report + proposals)
  Ava    — daily 10:00     (content draft + scheduling proposal)
  Tom    — nightly 02:00   (one scoped fix on the practice repo)
  Approvals poller — every 5 minutes (reads reactions on Tom/Ava/Bobby's
                     proposals and logs approved/rejected/pending)

Reliability
  Every scheduled cycle runs inside a catch-all try/except. A crash in one agent
  is logged with full traceback to logs/daemon.log (rotating, 5MB x 5) and a
  one-line alert is posted to #ops — it never stops the scheduler or the other
  agents.

Usage
  python daemon.py                # start the scheduler (runs until stopped)
  python daemon.py --once sarah    # run a single cycle now (sarah|bobby|ava|tom|approvals)

Run it under a process manager (Windows Task Scheduler / nssm, launchd, pm2)
so it survives reboots. ARIA's voice loop (main.py) is separate.

------------------------------------------------------------------------------
SLACK SETUP — before the first run:
  * Create a new channel #ops in your workspace.
  * Invite the bot to it (/invite @your-bot in #ops) — the daemon posts crash
    alerts there so you see failures without tailing logs.
  * #ops is already wired into config.CHANNELS; no channel ID is needed because
    the daemon only POSTS to it (it never reads it).
------------------------------------------------------------------------------
"""
import argparse
import logging
import os
import signal
import sys
import threading
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.blocking import BlockingScheduler

import config
from integrations import slack_bus
from agents.sarah import Sarah
from agents.tom import Tom
from agents.ava import Ava
from agents.bobby import Bobby

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")

# --- Logging (rotating file + console) --------------------------------------
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("aria.daemon")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_file = RotatingFileHandler(os.path.join(LOG_DIR, "daemon.log"),
                            maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
_file.setFormatter(_fmt)
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_fmt)
logger.addHandler(_file)
logger.addHandler(_console)

# --- Agents (one instance each, shared across jobs) -------------------------
sarah, tom, ava, bobby = Sarah(), Tom(), Ava(), Bobby()


def _post_ops(text):
    """Post a short line to #ops. Never raises — logs if it can't."""
    try:
        slack_bus.post_as("daemon", ":gear:", config.CHANNELS["ops"], text)
    except Exception:
        logger.error("Failed to post to #ops:\n%s", traceback.format_exc())


def run_job(label, fn):
    """Run one cycle with a catch-all guard so nothing can stop the scheduler."""
    logger.info("Running %s", label)
    try:
        fn()
        logger.info("Finished %s", label)
    except Exception:
        tb = traceback.format_exc()
        logger.error("Job %s crashed:\n%s", label, tb)
        last = tb.strip().splitlines()[-1] if tb.strip() else "unknown error"
        _post_ops(f":rotating_light: `{label}` failed: {last}")


def poll_approvals():
    """Read reactions on Tom/Ava/Bobby's #approvals proposals and log status.

    Each agent is polled independently so one failing poll never blocks the
    others. This only reports status — it never executes an approved action.
    """
    for agent in (tom, ava, bobby):
        try:
            for item in agent.process_pending_approvals():
                logger.info("approval [%s] %s -> %s",
                            agent.name, item.get("payload_id"), item.get("status"))
        except Exception:
            tb = traceback.format_exc()
            logger.error("Approval poll for %s crashed:\n%s", agent.name, tb)
            _post_ops(f":rotating_light: approval poll for `{agent.name}` failed: "
                      f"{tb.strip().splitlines()[-1]}")


# --- Job registry: id -> (label, callable, trigger kwargs) ------------------
JOBS = [
    ("sarah", "sarah.cycle", sarah.cycle, {"trigger": "interval", "hours": 2}),
    ("bobby", "bobby.daily", bobby.daily, {"trigger": "cron", "hour": 9}),
    ("ava", "ava.daily", ava.daily, {"trigger": "cron", "hour": 10}),
    ("tom", "tom.cycle", tom.cycle, {"trigger": "cron", "hour": 2}),
    ("approvals", "approval_poller", poll_approvals, {"trigger": "interval", "minutes": 5}),
]
_ONCE = {job_id: (label, fn) for job_id, label, fn, _ in JOBS}


def build_scheduler():
    scheduler = BlockingScheduler()
    for job_id, label, fn, trigger in JOBS:
        scheduler.add_job(lambda l=label, f=fn: run_job(l, f), id=job_id, **trigger)
    return scheduler


def print_banner(scheduler):
    now = datetime.now(scheduler.timezone)
    print("=" * 68)
    print("ARIA agent-team daemon")
    print(f"  time now : {now:%Y-%m-%d %H:%M:%S %Z}")
    print(f"  DRY_RUN  : {config.DRY_RUN}  (agents cannot spend / publish / merge)")
    print("  models   :")
    for name in ("Sarah", "Bobby", "Ava", "Tom"):
        print(f"      {name:<6} -> {config.AGENT_MODELS[name]}")
    print("  Next run for each job:")
    for job in scheduler.get_jobs():
        nxt = job.trigger.get_next_fire_time(None, now)
        print(f"      {job.id:<10} {nxt:%Y-%m-%d %H:%M:%S %Z}" if nxt else f"      {job.id:<10} (none)")
    print(f"  logs     : {os.path.join(LOG_DIR, 'daemon.log')}")
    print("=" * 68)
    sys.stdout.flush()


def _install_signal_handlers(stop_event):
    """Register handlers that ask for a clean shutdown by setting stop_event."""
    def _shutdown(signum, _frame):
        logger.info("Received signal %s — shutting down scheduler cleanly.", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)
    if hasattr(signal, "SIGBREAK"):  # Windows Ctrl-Break
        signal.signal(signal.SIGBREAK, _shutdown)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="ARIA agent-team daemon.")
    parser.add_argument("--once", metavar="AGENT", choices=sorted(_ONCE),
                        help="Run a single cycle now and exit "
                             "(sarah|bobby|ava|tom|approvals).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.once:
        label, fn = _ONCE[args.once]
        logger.info("--once %s", args.once)
        run_job(label, fn)
        return

    scheduler = build_scheduler()
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    print_banner(scheduler)
    logger.info("Daemon started; scheduler running.")

    # Run the BlockingScheduler in a worker thread so the MAIN thread stays free
    # to process shutdown signals promptly. On Windows a console signal can't
    # interrupt the scheduler's long internal wait, so a blocked main thread
    # would never see it — the short wait loop below fixes that on every OS.
    worker = threading.Thread(target=scheduler.start, name="scheduler", daemon=True)
    worker.start()
    try:
        while worker.is_alive() and not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Daemon stopped.")
    sys.exit(0)


if __name__ == "__main__":
    main()
