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

Run it under a process manager so it survives reboots — on Windows the supported
path is NSSM; see scripts/install_service.md for the exact install/start/stop
steps. Check health without tailing the log with scripts/service_status.py.
ARIA's voice loop (main.py) is separate and stays manual.

Service-safety (added for running head-less as a Windows service):
  * All output goes to logs/daemon.log via the logger (survives with no console).
  * On boot the service usually starts BEFORE Ollama; the daemon waits for Ollama
    (with backoff) instead of crashing, then starts scheduling.
  * A heartbeat line is logged every 30 minutes with each job's next run time.
  * On a service stop it shuts the scheduler down cleanly, giving a running cycle
    a bounded window to finish (writes are atomic, so a cut-off cycle is safe).

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

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

import config
from brain.hermes_common import OLLAMA_URL
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
    """Post a short line to #ops. Never raises — logs if it can't. A network blip
    while posting is itself a self-healing transient (WARNING); any other failure
    means alerting is actually broken (ERROR)."""
    try:
        slack_bus.post_as("daemon", ":gear:", config.CHANNELS["ops"], text)
    except Exception as e:
        if slack_bus.is_transient_network_error(e):
            logger.warning("Couldn't reach #ops (network unreachable: %s); "
                           "alert not sent.", type(e).__name__)
        else:
            logger.error("Failed to post to #ops:\n%s", traceback.format_exc())


def _report_cycle_failure(label, exc):
    """Log an exception from a scheduled cycle or approval poll, choosing severity
    by CAUSE rather than treating every failure as a crash.

    A pre-send transient that exhausted its bounded retries — an SSL handshake flap
    or a DNS/network-down blip — is something the next cycle recovers from on its
    own (measured: 242 clean polls to 5 such blips in one day). It is logged at
    WARNING with no #ops alert, so the dashboard's error list stays reserved for
    real problems. A genuinely unexpected exception keeps the ERROR + traceback +
    red #ops alert. This changes ONLY logging severity — the recovery behaviour
    (return, let the scheduler run the next cycle) is identical either way.
    """
    if slack_bus.is_transient_network_error(exc):
        logger.warning("%s: network unreachable (%s) — nothing was sent to Slack; "
                       "will retry next cycle.", label, type(exc).__name__)
        return
    tb = traceback.format_exc()
    logger.error("%s crashed:\n%s", label, tb)
    last = tb.strip().splitlines()[-1] if tb.strip() else "unknown error"
    _post_ops(f":rotating_light: `{label}` failed: {last}")


def run_job(label, fn):
    """Run one cycle with a catch-all guard so nothing can stop the scheduler."""
    logger.info("Running %s", label)
    try:
        fn()
        logger.info("Finished %s", label)
    except Exception as e:
        _report_cycle_failure(label, e)


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
        except Exception as e:
            _report_cycle_failure(f"approval poll for {agent.name}", e)


# --- Startup dependency guard: wait for Ollama ------------------------------
_OLLAMA_TAGS_URL = OLLAMA_URL.replace("/api/chat", "/api/tags")


def _ollama_reachable(timeout=4):
    """True if Ollama answers at localhost:11434. Never raises."""
    try:
        return requests.get(_OLLAMA_TAGS_URL, timeout=timeout).status_code == 200
    except requests.exceptions.RequestException:
        return False


def wait_for_ollama(stop_event, initial_delay=5, max_delay=60):
    """Block until Ollama is reachable, backing off between tries. On boot the
    service almost always starts before Ollama — this holds scheduling until it's
    up rather than letting the first cycles fail (or crashing). Returns True when
    reachable, or False if a stop is requested first. Never raises on a connection
    error; it just keeps retrying with a capped exponential backoff."""
    if _ollama_reachable():
        logger.info("Ollama reachable at %s.", _OLLAMA_TAGS_URL)
        return True
    delay, attempt = initial_delay, 0
    while not stop_event.is_set():
        attempt += 1
        logger.warning("Ollama not reachable at localhost:11434 (attempt %d) — it "
                       "likely hasn't started yet. Waiting %ds before retry; agent "
                       "cycles are held until it is up.", attempt, delay)
        if stop_event.wait(delay):
            return False   # stop requested during the wait
        if _ollama_reachable():
            logger.info("Ollama is now reachable (after %d attempt(s)) — proceeding.",
                        attempt)
            return True
        delay = min(max_delay, delay * 2)
    return False


# --- Heartbeat --------------------------------------------------------------
def _heartbeat(scheduler):
    """Log one 'still alive' line with each job's next fire time, so the log can
    confirm the service is actually running in the quiet gaps between cycles."""
    now = datetime.now(scheduler.timezone)
    parts = []
    for job in scheduler.get_jobs():
        if job.id == "heartbeat":
            continue
        nxt = job.trigger.get_next_fire_time(None, now)
        parts.append(f"{job.id} {nxt:%H:%M}" if nxt else f"{job.id} (none)")
    logger.info("daemon alive — next runs: %s", ", ".join(parts) or "(no jobs)")


# --- Graceful shutdown ------------------------------------------------------
_SHUTDOWN_GRACE_SEC = 25


def _graceful_shutdown(scheduler):
    """Stop scheduling immediately, then give a running cycle a bounded window to
    finish so it isn't cut off mid-write. State writes are atomic (SQLite commits,
    single Slack posts, and bookmarks advanced only after a cycle succeeds), so
    even if the window is exceeded nothing is left corrupted — the cycle simply
    re-runs on its next schedule."""
    if not scheduler.running:
        return
    logger.info("Shutdown: pausing scheduler (no new cycles), waiting up to %ds for "
                "any running cycle to finish...", _SHUTDOWN_GRACE_SEC)
    try:
        scheduler.pause()
    except Exception:
        pass
    finished = threading.Event()

    def _sd():
        try:
            scheduler.shutdown(wait=True)   # blocks until running jobs complete
        except Exception:
            pass
        finally:
            finished.set()

    threading.Thread(target=_sd, name="shutdown", daemon=True).start()
    if finished.wait(_SHUTDOWN_GRACE_SEC):
        logger.info("Scheduler shut down cleanly.")
    else:
        logger.warning("A cycle was still running after %ds — exiting anyway; atomic "
                       "writes mean no state is left corrupted.", _SHUTDOWN_GRACE_SEC)


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
    # Heartbeat is a health log, not an agent cycle — the agent schedules above
    # are untouched. It logs every 30 minutes with the next run times.
    scheduler.add_job(lambda: _heartbeat(scheduler), id="heartbeat",
                      trigger="interval", minutes=30)
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

    # Startup dependency guard: on boot the service usually starts before Ollama.
    # Wait (with backoff) until it's reachable rather than letting the first cycles
    # fail — but bail out cleanly if a stop is requested during the wait.
    if not wait_for_ollama(stop_event):
        logger.info("Stop requested before Ollama became reachable; exiting.")
        return
    logger.info("Daemon started; scheduler running.")

    # Run the BlockingScheduler in a worker thread so the MAIN thread stays free
    # to process shutdown signals promptly. On Windows a console signal can't
    # interrupt the scheduler's long internal wait, so a blocked main thread
    # would never see it — the short wait loop below fixes that on every OS.
    worker = threading.Thread(target=scheduler.start, name="scheduler", daemon=True)
    worker.start()
    _heartbeat(scheduler)   # an immediate 'alive' line so the log shows life at once
    try:
        while worker.is_alive() and not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    _graceful_shutdown(scheduler)
    logger.info("Daemon stopped.")
    sys.exit(0)


if __name__ == "__main__":
    main()
