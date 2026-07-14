# Running the ARIA daemon as a Windows service (NSSM)

This makes `daemon.py` (the agent team — Sarah/Bobby/Ava/Tom + the approvals
poller) run in the background as a proper Windows service: it starts on boot,
survives reboots and logoff, and needs no PowerShell window kept open.

> This is **only** for `daemon.py`. `main.py` (ARIA's voice loop) stays manual —
> it needs your mic, speakers, and display, so run it by hand when you're at the
> machine.

Everything below uses these two paths — adjust if yours differ:

| | value |
|---|---|
| Project root | `C:\Users\Ayyan\Desktop\aria` |
| Service name | `ARIADaemon` (must match `scripts/service_status.py`) |

---

## 0. Prerequisite: Ollama must come up on its own too

The daemon's brains are local Ollama models. The service will start **at boot,
before you log in** — and Ollama, by default, only starts when *you* log in. The
daemon handles this gracefully (it waits for Ollama with backoff instead of
crashing — you'll see `Ollama not reachable ... waiting` lines in the log until
it's up), but the agents stay idle until Ollama exists.

For true headless operation, make Ollama start at boot too. Simplest: install a
second NSSM service for it —

```
nssm install OllamaServe "C:\Users\Ayyan\AppData\Local\Programs\Ollama\ollama.exe" serve
nssm set OllamaServe Start SERVICE_AUTO_START
nssm start OllamaServe
```

(Adjust the `ollama.exe` path — find it with `(Get-Command ollama).Source`.) If
you skip this, the ARIA daemon still runs; its agents just wait until you log in
and Ollama starts.

---

## 1. Download NSSM

1. Get it from <https://nssm.cc/download> (the "latest release" zip).
2. Unzip and copy `win64\nssm.exe` somewhere permanent, e.g. `C:\nssm\nssm.exe`.
3. Either add that folder to PATH, or just call it by full path (`C:\nssm\nssm.exe`)
   in the commands below.

---

## 2. Find your Python path

Use the SAME interpreter you run `python daemon.py` with. In PowerShell:

```
(Get-Command python).Source
```

Typically: `C:\Users\Ayyan\AppData\Local\Programs\Python\Python312\python.exe`
(If you use a virtualenv, point at that venv's `python.exe` instead.)

---

## 3. Install and configure the service

Run these in an **Administrator** PowerShell (service install needs admin). Each
`set` is one setting; run them all.

```powershell
# create the service: <python.exe> <full path to daemon.py>
nssm install ARIADaemon "C:\Users\Ayyan\AppData\Local\Programs\Python\Python312\python.exe" "C:\Users\Ayyan\Desktop\aria\daemon.py"

# working directory = project root (so .env, docs/, memory/ resolve)
nssm set ARIADaemon AppDirectory "C:\Users\Ayyan\Desktop\aria"

# start automatically on boot
nssm set ARIADaemon Start SERVICE_AUTO_START

# capture raw stdout/stderr (print()s + any crash BEFORE logging starts).
# NOTE: this goes to daemon.STDOUT.log, NOT daemon.log — see "Why two log files".
nssm set ARIADaemon AppStdout "C:\Users\Ayyan\Desktop\aria\logs\daemon.stdout.log"
nssm set ARIADaemon AppStderr "C:\Users\Ayyan\Desktop\aria\logs\daemon.stdout.log"

# rotate that stdout file so it can't grow forever (10 MB)
nssm set ARIADaemon AppRotateFiles 1
nssm set ARIADaemon AppRotateOnline 1
nssm set ARIADaemon AppRotateBytes 10485760

# graceful stop: NSSM sends Ctrl+C; the daemon catches it, finishes any running
# cycle (up to ~25s) and shuts the scheduler down cleanly. Give it 30s.
nssm set ARIADaemon AppStopMethodConsole 30000

# if the process ever exits on its own, restart it (10s throttle to avoid a
# crash loop). It normally only exits when you stop the service.
nssm set ARIADaemon AppExit Default Restart
nssm set ARIADaemon AppThrottle 10000

# a friendly description
nssm set ARIADaemon Description "ARIA agent-team daemon (Sarah/Bobby/Ava/Tom)"
```

> To open NSSM's GUI editor for the same settings instead:
> `nssm edit ARIADaemon`.

### Why two log files
`daemon.py`'s Python logger owns **`logs\daemon.log`** (structured, timestamped,
self-rotating at 5 MB × 5). NSSM must NOT also write to that same file — it would
fight the logger for the handle and break rotation on Windows. So NSSM's raw
stdout/stderr goes to **`logs\daemon.stdout.log`** instead. Day to day, read
`daemon.log` (or run `service_status.py`); check `daemon.stdout.log` only if the
service dies so early that nothing reached `daemon.log`.

---

## 4. Start / stop / restart / status / uninstall

```powershell
nssm start   ARIADaemon
nssm stop    ARIADaemon      # graceful (Ctrl+C -> clean scheduler shutdown)
nssm restart ARIADaemon
nssm status  ARIADaemon      # SERVICE_RUNNING / SERVICE_STOPPED
sc query     ARIADaemon      # same, via Windows

# remove it entirely (stop first)
nssm stop      ARIADaemon
nssm remove    ARIADaemon confirm
```

---

## 5. Confirm it's actually working

```powershell
# 1) the daemon's own health summary (no log-reading required)
python C:\Users\Ayyan\Desktop\aria\scripts\service_status.py

# 2) watch the live log — you want the startup banner, an "Ollama reachable"
#    line, then a "daemon alive — next runs: ..." heartbeat every 30 minutes
Get-Content C:\Users\Ayyan\Desktop\aria\logs\daemon.log -Wait -Tail 20
```

Healthy startup in `daemon.log` looks like:

```
... INFO Ollama reachable at http://localhost:11434/api/tags.
... INFO Daemon started; scheduler running.
... INFO daemon alive — next runs: sarah 19:40, bobby 09:00, ava 10:00, tom 02:00, approvals 17:45
```

`service_status.py` will show the service `RUNNING`, an `[alive]` heartbeat, when
each agent last fired and next runs, and any errors in the last 24h.

---

## 6. If it won't start / stay running

Work down this list:

1. **`service_status.py` says NOT INSTALLED** — the `nssm install` step didn't
   run as Administrator, or the name is misspelled. Re-run step 3 in an admin shell.
2. **Service starts then immediately stops** — almost always a bad path. Check
   **`logs\daemon.stdout.log`** for a Python traceback (e.g. wrong `python.exe`,
   wrong `daemon.py` path, a missing dependency in that interpreter). Verify the
   two quoted paths in `nssm install` exist exactly.
3. **Log shows repeated `Ollama not reachable ... waiting`** — Ollama isn't up.
   Expected briefly after a reboot; if it never clears, start Ollama (or install
   the `OllamaServe` service from step 0). The daemon is fine — it's waiting, not
   crashed.
4. **Nothing in `daemon.log` at all, service "running"** — it may be crashing
   before logging initializes (import error). Look in `daemon.stdout.log` and in
   **Event Viewer → Windows Logs → Application** for NSSM/Python errors.
5. **Agents never fire but heartbeat is alive** — check `DRY_RUN` in `.env`
   (must be `False` for real Slack posts) and that Slack channel IDs are set.
   Cycles that error are logged to `daemon.log` and posted to `#ops`.
6. **Service won't stop / hangs on stop** — the graceful window is ~25s; NSSM
   escalates to a kill after `AppStopMethodConsole` (30s). Because all writes are
   atomic, a killed cycle is safe and simply re-runs on its next schedule.

Run `daemon.py` by hand once to sanity-check the environment before blaming the
service: `python C:\Users\Ayyan\Desktop\aria\daemon.py` (Ctrl+C to stop).
