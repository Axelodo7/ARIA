"""dashboard/app.py — ARIA mission control (READ-ONLY).

A single local page at http://localhost:8000 showing, at a glance:
  * service health   — is ARIADaemon running, heartbeat age, next scheduled runs
  * agent activity    — recent agent_memory rows (proposals/digests/tasks)
  * pending approvals — #approvals items with no ✅/❌ yet
  * recent errors     — ERROR/traceback lines from logs/daemon.log (last 24h)
  * business memory   — the facts in docs/memory.md that all agents read

READ-ONLY BY CONSTRUCTION — this component is a viewer and nothing else:
  * SQLite is opened through a `file:...?mode=ro` URI, so it can never lock or
    write the database the daemon is actively using.
  * It never writes aria_memory.db or docs/memory.md, never posts to Slack (only
    conversations.history reads), and never triggers an agent cycle.
  * Every panel is individually guarded: if a source is down (DB locked, Slack
    unreachable, log missing) that panel renders an "unavailable" state and the
    rest of the page still works.

Run standalone:
    python dashboard/app.py        # -> http://localhost:8000
"""
import html
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import config
import service_status                      # reuse the health-check logic
from brain.hermes_common import BUSINESS_MEMORY_PATH, load_business_memory
from integrations import slack_bus

DB_PATH = os.path.join(PROJECT_ROOT, "memory", "aria_memory.db")
PORT = 8000
REFRESH_SEC = 15
ACTIVITY_LIMIT = 30
ERROR_LIMIT = 10
_SLACK_TTL_SEC = 30    # cache the Slack read so a left-open tab doesn't hammer it

app = FastAPI(title="ARIA Mission Control")


def esc(v):
    return html.escape(str(v if v is not None else ""))


# --- Read-only data access ---------------------------------------------------
def read_activity(limit=ACTIVITY_LIMIT):
    """Recent agent_memory rows, newest first. Opened READ-ONLY (mode=ro) so the
    dashboard can never lock or write the daemon's database.

    Also reads dry_run so a dry-run/test row is never shown as real work — the
    whole point of the panel is to reflect what actually happened. The column is
    probed first so an older, un-migrated database still renders."""
    if not os.path.exists(DB_PATH):
        raise RuntimeError("memory/aria_memory.db not found — the daemon hasn't run yet")
    uri = Path(DB_PATH).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_memory)")}
        dry = "dry_run" if "dry_run" in cols else "0"
        rows = conn.execute(
            f"SELECT agent, timestamp, event_type, summary, {dry} FROM agent_memory "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError as e:
        raise RuntimeError(f"agent_memory not readable ({e})")
    finally:
        conn.close()
    return rows


def read_health():
    """Service state + heartbeat + next runs + last completed cycle per agent."""
    now = datetime.now()
    state = service_status.service_state()
    entries = service_status.parse_log()
    out = {"state": state, "heartbeat": None, "ago": None, "stale": False,
           "next_runs": None, "cycles": [], "log": entries is not None}
    if not entries:
        return out
    hbs = [(dt, msg) for dt, _l, msg in entries if msg.startswith("daemon alive")]
    if hbs:
        dt, msg = hbs[-1]
        out["heartbeat"] = dt
        out["ago"] = service_status._ago(dt, now)
        out["stale"] = (now - dt) > timedelta(minutes=service_status.HEARTBEAT_MAX_AGE_MIN)
        if "next runs:" in msg:
            out["next_runs"] = msg.split("next runs:", 1)[1].strip()
    finished = {}
    for dt, _l, msg in entries:
        for label, agent in service_status.LABEL_TO_AGENT.items():
            if msg == f"Finished {label}":
                finished[agent] = dt
    out["cycles"] = [(a, finished.get(a), service_status._ago(finished[a], now) if a in finished else None)
                     for a in ("sarah", "bobby", "ava", "tom", "approvals")]
    return out


def _reaction_status(msg):
    """Pending/approved/rejected straight from the history payload's reactions —
    no extra API call per message (and ✅ wins, same as slack_bus.check_approval)."""
    names = {r.get("name") for r in (msg.get("reactions") or [])}
    if "white_check_mark" in names:
        return "approved"
    if "x" in names:
        return "rejected"
    return "pending"


_slack_cache = {"at": 0.0, "data": None}


def read_approvals():
    """#approvals items still awaiting a reaction. Read-only; cached briefly."""
    if _slack_cache["data"] is not None and (time.time() - _slack_cache["at"]) < _SLACK_TTL_SEC:
        return _slack_cache["data"]
    cid = config.CHANNEL_IDS.get("approvals")
    if not cid:
        raise RuntimeError("CHANNEL_ID_APPROVALS is not set in .env")
    items = []
    for m in slack_bus.history(cid, limit=50):     # reuse the existing read helper
        text = m.get("text") or ""
        if "APPROVAL NEEDED" not in text or _reaction_status(m) != "pending":
            continue
        pid = re.search(r"\[`([^`]+)`\]", text)
        tldr = next((ln.split(":", 1)[1].strip() for ln in text.splitlines()
                     if ln.strip().lower().startswith("tl;dr")), "")
        try:
            when = datetime.fromtimestamp(float(m.get("ts", 0)))
        except (TypeError, ValueError):
            when = None
        items.append({"payload_id": pid.group(1) if pid else "(no id)",
                      "tldr": tldr or "(no TL;DR line)",
                      "agent": m.get("username") or "an agent", "when": when})
    _slack_cache.update(at=time.time(), data=items)
    return items


def read_errors(limit=ERROR_LIMIT):
    """ERROR / traceback lines from the last 24h, newest first. Empty is good."""
    entries = service_status.parse_log()
    if entries is None:
        raise RuntimeError("logs/daemon.log not found — the daemon hasn't run yet")
    cutoff = datetime.now() - timedelta(hours=24)
    errs = [(dt, msg) for dt, lvl, msg in entries
            if dt >= cutoff and (lvl == "ERROR" or "Traceback" in msg or "crashed" in msg)]
    return list(reversed(errs[-limit:]))


def read_memory_facts():
    """docs/memory.md facts, newest first, flagged by whether they're inside the
    token cap the agents actually load."""
    text = Path(BUSINESS_MEMORY_PATH).read_text(encoding="utf-8")
    facts = [ln.rstrip() for ln in text.splitlines() if ln.strip().startswith("- ")]
    loaded = set(load_business_memory().splitlines())
    return [(ln, ln in loaded) for ln in reversed(facts)]


# --- Rendering ---------------------------------------------------------------
CSS = """
:root{--bg:#0A0A0B;--panel:#121316;--line:#22242B;--cyan:#00F0FF;--pink:#FF2D6F;
      --text:#E6E9EF;--muted:#8A909C;--mono:ui-monospace,"JetBrains Mono",Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
     font:14px/1.55 ui-sans-serif,system-ui,"Segoe UI",sans-serif}
header{padding:22px 26px 16px;border-bottom:1px solid var(--line)}
h1{margin:0;font:700 20px/1.2 var(--mono);letter-spacing:3px;text-transform:uppercase;
   color:var(--cyan);text-shadow:0 0 18px rgba(0,240,255,.35)}
.sub{margin-top:7px;color:var(--muted);font:12px/1.4 var(--mono)}
.ro{color:var(--pink);border:1px solid var(--pink);border-radius:3px;padding:1px 6px;
    font:11px var(--mono);letter-spacing:1px;margin-left:8px}
main{padding:22px 26px 40px;display:grid;gap:18px;
     grid-template-columns:repeat(auto-fit,minmax(360px,1fr))}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px 18px;
       min-width:0}
.panel.wide{grid-column:1/-1}
h2{margin:0 0 12px;font:700 12px var(--mono);letter-spacing:2.5px;text-transform:uppercase;
   color:var(--cyan);display:flex;align-items:center;gap:9px}
h2::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--cyan);
           box-shadow:0 0 9px var(--cyan)}
h2.alert{color:var(--pink)} h2.alert::before{background:var(--pink);box-shadow:0 0 9px var(--pink)}
table{width:100%;border-collapse:collapse;font:12.5px var(--mono)}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:400;letter-spacing:1px;text-transform:uppercase;font-size:11px}
tr:last-child td{border-bottom:0}
.t{color:var(--muted);white-space:nowrap}
.agent{color:var(--cyan)}
.badge{font:11px var(--mono);padding:1px 7px;border-radius:3px;border:1px solid;white-space:nowrap}
.ok{color:var(--cyan);border-color:rgba(0,240,255,.5)}
.bad{color:var(--pink);border-color:rgba(255,45,111,.5)}
.pend{color:#E8C36A;border-color:rgba(232,195,106,.45)}
.mut{color:var(--muted);border-color:var(--line)}
.kv{display:flex;justify-content:space-between;gap:14px;padding:6px 0;
    border-bottom:1px solid var(--line);font:12.5px var(--mono)}
.kv:last-child{border-bottom:0}
.kv .k{color:var(--muted)} .kv .v{text-align:right;word-break:break-word}
.unavailable{color:var(--pink);font:12.5px var(--mono);margin:0;opacity:.85}
.empty{color:var(--muted);font:12.5px var(--mono);margin:0}
.err{font:12px var(--mono);color:#FFC9D8;background:rgba(255,45,111,.07);
     border-left:2px solid var(--pink);padding:7px 9px;margin:0 0 7px;word-break:break-word}
.fact{font:12.5px var(--mono);padding:5px 0;border-bottom:1px solid var(--line);
      display:flex;gap:9px;align-items:baseline}
.fact:last-child{border-bottom:0}
.fact .txt{word-break:break-word}
.dim{opacity:.45}
"""

_EVENT_CLASS = {"proposal_approved": "ok", "digest_posted": "ok",
                "proposal_rejected": "bad", "task_refused": "bad",
                "proposal_filed": "pend", "task_attempted": "mut"}


def panel(title, body, alert=False, wide=False):
    cls = "panel wide" if wide else "panel"
    h2 = "alert" if alert else ""
    return f'<section class="{cls}"><h2 class="{h2}">{esc(title)}</h2>{body}</section>'


def safe_panel(title, fn, alert=False, wide=False):
    """Render a panel, or an 'unavailable' state — never take the page down."""
    try:
        return panel(title, fn(), alert=alert, wide=wide)
    except Exception as e:
        msg = f"unavailable — {type(e).__name__}: {str(e)[:180]}"
        return panel(title, f'<p class="unavailable">{esc(msg)}</p>', alert=alert, wide=wide)


def health_body():
    h = read_health()
    state = h["state"]
    scls = "ok" if state == "RUNNING" else "bad"
    rows = [f'<div class="kv"><span class="k">service (ARIADaemon)</span>'
            f'<span class="v"><span class="badge {scls}">{esc(state)}</span></span></div>']
    if h["heartbeat"]:
        hcls = "bad" if h["stale"] else "ok"
        htxt = "STALE" if h["stale"] else "ALIVE"
        rows.append(f'<div class="kv"><span class="k">heartbeat</span><span class="v">'
                    f'<span class="badge {hcls}">{htxt}</span> '
                    f'{esc(h["heartbeat"].strftime("%H:%M:%S"))} · {esc(h["ago"])}</span></div>')
    else:
        rows.append('<div class="kv"><span class="k">heartbeat</span>'
                    '<span class="v">(none logged yet)</span></div>')
    if h["next_runs"]:
        rows.append(f'<div class="kv"><span class="k">next runs</span>'
                    f'<span class="v">{esc(h["next_runs"])}</span></div>')
    for agent, dt, ago in h["cycles"]:
        v = f'{dt:%m-%d %H:%M} · {esc(ago)}' if dt else '<span class="dim">no completed run</span>'
        rows.append(f'<div class="kv"><span class="k">last {esc(agent)}</span>'
                    f'<span class="v">{v}</span></div>')
    return "".join(rows)


def approvals_body():
    items = read_approvals()
    if not items:
        return '<p class="empty">No pending approvals.</p>'
    out = ['<table><tr><th>agent</th><th>tl;dr</th><th>payload id</th><th>filed</th></tr>']
    for i in items:
        when = f'{i["when"]:%m-%d %H:%M}' if i["when"] else "—"
        out.append(f'<tr><td class="agent">{esc(i["agent"])}</td>'
                   f'<td>{esc(i["tldr"])}</td>'
                   f'<td class="t">{esc(i["payload_id"])}</td>'
                   f'<td class="t">{esc(when)}</td></tr>')
    return "".join(out) + "</table>"


def activity_body():
    rows = read_activity()
    if not rows:
        return '<p class="empty">No agent activity recorded yet.</p>'
    out = ['<table><tr><th>when</th><th>agent</th><th>event</th><th>summary</th></tr>']
    for agent, ts, event, summary, dry in rows:
        when = esc((ts or "")[:16].replace("T", " "))
        cls = _EVENT_CLASS.get(event, "mut")
        # A dry-run row is a test artefact, not real work — say so plainly.
        tag = ' <span class="badge mut">dry</span>' if dry else ""
        row_cls = ' class="dim"' if dry else ""
        out.append(f'<tr{row_cls}><td class="t">{when}</td>'
                   f'<td class="agent">{esc(agent)}</td>'
                   f'<td><span class="badge {cls}">{esc(event)}</span>{tag}</td>'
                   f'<td>{esc(summary)}</td></tr>')
    return "".join(out) + "</table>"


def errors_body():
    errs = read_errors()
    if not errs:
        return '<p class="empty">No errors in the last 24 hours.</p>'
    return "".join(f'<p class="err">{esc(f"{dt:%m-%d %H:%M}")} — {esc(msg[:220])}</p>'
                   for dt, msg in errs)


def memory_body():
    facts = read_memory_facts()
    if not facts:
        return '<p class="empty">No facts yet — ARIA adds them when you say "remember ...".</p>'
    out = []
    for line, is_loaded in facts:
        badge = ('<span class="badge ok">loaded</span>' if is_loaded
                 else '<span class="badge mut">over cap</span>')
        dim = "" if is_loaded else " dim"
        out.append(f'<div class="fact{dim}">{badge}<span class="txt">{esc(line[2:])}</span></div>')
    return "".join(out)


@app.get("/", response_class=HTMLResponse)
def index():
    body = (
        safe_panel("Service health", health_body)
        + safe_panel("Pending approvals", approvals_body)
        + safe_panel("Agent activity", activity_body, wide=True)
        + safe_panel("Recent errors (24h)", errors_body, alert=True)
        + safe_panel("Business memory", memory_body)
    )
    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SEC}">
<title>ARIA — Mission Control</title><style>{CSS}</style></head>
<body>
<header>
  <h1>ARIA — Mission Control</h1>
  <div class="sub">updated {esc(updated)} · auto-refresh {REFRESH_SEC}s
    <span class="ro">READ-ONLY</span></div>
</header>
<main>{body}</main>
</body></html>"""


if __name__ == "__main__":
    import uvicorn
    print(f"ARIA mission control -> http://localhost:{PORT}  (read-only; Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
