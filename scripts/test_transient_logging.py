"""Self-healing network blips log at WARNING, not ERROR — so the dashboard's
"recent errors" means real problems.

Context: the daemon's approval poll and scheduled cycles already survive the
machine's intermittent Slack outages — a bounded retry (slack_bus._call) absorbs
short SSL-handshake flaps and DNS blips, and when an outage outlasts the ~8s retry
budget the per-agent guard catches it and the NEXT cycle recovers (measured: 242
clean polls to 5 such blips in one day). But every one of those was logged at ERROR
with a traceback and a red #ops alert, so the dashboard looked alarming for events
the system handles on its own.

Change under test: a pre-send transient that exhausted its retries logs at WARNING
("network unreachable, will retry next cycle") with NO #ops alert. A genuinely
unexpected exception keeps ERROR + traceback + red alert. Behaviour is otherwise
identical — the poll still isolates per agent and the scheduler still runs the next
cycle.

Gates:
  * is_transient_network_error classifies the retry taxonomy correctly, including
    URLError-wrapped forms, and rejects HTTPError / timeouts / ordinary bugs,
  * a transient poll failure  -> WARNING, no red #ops alert,
  * a genuine  poll failure   -> ERROR, red #ops alert fires,
  * one agent's transient does NOT stop the next agent being polled (isolation),
  * run_job routes severity the same way,
  * the REAL _post_ops downgrades its OWN network-blip failure to WARNING.

No Slack, no network, no model. Logging is captured in-memory so the real
logs/daemon.log is never written. DRY_RUN forced.

    python scripts/test_transient_logging.py
"""
import logging
import os
import socket
import ssl
import sys
import urllib.error

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ["DRY_RUN"] = "True"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
import daemon
from integrations import slack_bus
from integrations.slack_bus import is_transient_network_error

# The genuine functions, captured BEFORE any monkeypatch, so section 5 exercises
# the real _post_ops (not a re-implementation of it).
REAL_POST_OPS = daemon._post_ops


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


# --- capture daemon's logs in memory; never touch the real daemon.log ---------
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def reset(self):
        self.records = []

    def levels(self):
        return [lvl for lvl, _ in self.records]

    def text(self):
        return "\n".join(m for _, m in self.records)


CAP = _Capture()
daemon.logger.handlers = [CAP]     # replace file+console handlers for the test
daemon.logger.propagate = False


class FakeAgent:
    """Stands in for Tom/Ava/Bobby: either raises, or yields approval items."""
    def __init__(self, name, exc=None, items=None):
        self.name = name
        self._exc = exc
        self._items = items or []

    def process_pending_approvals(self):
        if self._exc is not None:
            raise self._exc
        return self._items


# Exceptions exactly as they surface after retry exhaustion.
SSL_FLAP = ssl.SSLCertVerificationError(
    "[SSL: CERTIFICATE_VERIFY_FAILED] Hostname mismatch, not valid for 'slack.com'")
SSL_WRAPPED = urllib.error.URLError(SSL_FLAP)          # what urllib actually raises
DNS_DOWN = socket.gaierror(11001, "getaddrinfo failed")
DNS_WRAPPED = urllib.error.URLError(DNS_DOWN)
HTTP_429 = urllib.error.HTTPError("https://slack.com", 429, "rate limited", {}, None)
TIMEOUT_WRAPPED = urllib.error.URLError(socket.timeout("timed out"))
GENUINE = ValueError("payload_id column is missing")   # an ordinary bug


def _raises(exc):
    """A callable that raises `exc` — for stubbing post_as."""
    def _fn(*a, **k):
        raise exc
    return _fn


def main():
    print("=" * 74)
    print("Transient network blips -> WARNING; real bugs -> ERROR + #ops alert")
    print("=" * 74)

    # --- 1) the classifier ---------------------------------------------------
    print("\n--- 1) is_transient_network_error taxonomy ---")
    c1 = chk("SSL handshake flap is transient", is_transient_network_error(SSL_FLAP))
    c2 = chk("...also when wrapped in URLError (what urllib raises)",
             is_transient_network_error(SSL_WRAPPED))
    c3 = chk("DNS getaddrinfo failure is transient (raw and wrapped)",
             is_transient_network_error(DNS_DOWN) and is_transient_network_error(DNS_WRAPPED))
    c4 = chk("connection refused is transient",
             is_transient_network_error(ConnectionRefusedError("refused")))
    c5 = chk("HTTP 429 is NOT transient (real answer from Slack)",
             not is_transient_network_error(HTTP_429))
    c6 = chk("a timeout is NOT transient (may have been delivered)",
             not is_transient_network_error(TIMEOUT_WRAPPED))
    c7 = chk("an ordinary bug is NOT transient", not is_transient_network_error(GENUINE))

    # A recorder replaces _post_ops for the poll/run_job sections so no red alert
    # touches Slack; section 5 restores the real one to test it directly.
    posted = []
    daemon._post_ops = lambda text: posted.append(text)

    # --- 2) poll_approvals: transient -> WARNING, no alert -------------------
    print("\n--- 2) a transient poll failure ---")
    daemon.tom = FakeAgent("Tom", exc=SSL_WRAPPED)
    daemon.ava = FakeAgent("Ava", items=[{"payload_id": "ava-1", "status": "approved"}])
    daemon.bobby = FakeAgent("Bobby", items=[])
    CAP.reset(); posted.clear()
    daemon.poll_approvals()
    print(f"  levels={CAP.levels()}  ops_alerts={len(posted)}")
    t1 = chk("the SSL flap is logged at WARNING, not ERROR",
             "WARNING" in CAP.levels() and "ERROR" not in CAP.levels())
    t2 = chk("the WARNING says network unreachable / will retry",
             "network unreachable" in CAP.text() and "retry next cycle" in CAP.text())
    t3 = chk("NO red #ops alert was raised for the blip", not posted)
    t4 = chk("isolation held: Ava was still polled after Tom's blip",
             any("ava-1" in m for m in CAP.text().splitlines()))

    # --- 3) poll_approvals: a genuine bug -> ERROR + alert -------------------
    print("\n--- 3) a genuine poll failure ---")
    daemon.tom = FakeAgent("Tom", exc=GENUINE)
    daemon.ava = FakeAgent("Ava", items=[])
    daemon.bobby = FakeAgent("Bobby", items=[])
    CAP.reset(); posted.clear()
    daemon.poll_approvals()
    print(f"  levels={CAP.levels()}  ops_alerts={posted}")
    g1 = chk("a real bug is logged at ERROR", "ERROR" in CAP.levels())
    g2 = chk("...with a traceback (the ValueError text is present)",
             "ValueError" in CAP.text())
    g3 = chk("a red #ops alert IS raised",
             len(posted) == 1 and ":rotating_light:" in posted[0])

    # --- 4) run_job routes severity the same way ----------------------------
    print("\n--- 4) run_job ---")
    CAP.reset(); posted.clear()
    daemon.run_job("sarah_cycle", _raises(DNS_WRAPPED))
    j1 = chk("a transient job failure -> WARNING, no alert",
             "WARNING" in CAP.levels() and "ERROR" not in CAP.levels() and not posted)
    CAP.reset(); posted.clear()
    daemon.run_job("sarah_cycle", _raises(GENUINE))
    j2 = chk("a genuine job failure -> ERROR + alert",
             "ERROR" in CAP.levels() and len(posted) == 1)

    # --- 5) the REAL _post_ops when Slack itself is unreachable --------------
    print("\n--- 5) _post_ops (the real one) when #ops can't be reached ---")
    daemon._post_ops = REAL_POST_OPS          # restore the genuine function
    orig_post_as = slack_bus.post_as
    try:
        slack_bus.post_as = _raises(SSL_WRAPPED)
        CAP.reset()
        daemon._post_ops(":rotating_light: something happened")
        print(f"  blip while posting -> levels={CAP.levels()}")
        p1 = chk("a network blip while posting to #ops -> WARNING (not ERROR)",
                 "WARNING" in CAP.levels() and "ERROR" not in CAP.levels())
        p1b = chk("...and it never raises (daemon can't die posting an alert)", True)

        slack_bus.post_as = _raises(GENUINE)
        CAP.reset()
        daemon._post_ops(":rotating_light: something happened")
        print(f"  genuine post failure -> levels={CAP.levels()}")
        p2 = chk("a real failure to reach #ops -> ERROR", "ERROR" in CAP.levels())
    finally:
        slack_bus.post_as = orig_post_as

    all_ok = all([c1, c2, c3, c4, c5, c6, c7, t1, t2, t3, t4, g1, g2, g3,
                  j1, j2, p1, p1b, p2])
    print("\n" + "=" * 74)
    print("ALL PASS — the dashboard's errors are now real problems, not blips."
          if all_ok else "SOME CHECKS FAILED.")
    print("=" * 74)
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
