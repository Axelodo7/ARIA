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
| Daemon service | `ARIADaemon` (must match `scripts/service_status.py`) |
| Ollama service | `OllamaServe` (from step 0) |

---

## 0. Install Ollama as a service (so it's up at boot)

The daemon's brains are local Ollama models, and the ARIA service starts **at
boot, before you log in** — but Ollama, by default, only starts when *you* log
in. The daemon tolerates this (it waits for Ollama with backoff instead of
crashing — you'll see `Ollama not reachable ... waiting` in the log), yet the
agents stay idle until Ollama exists. For genuinely headless operation, run
Ollama as its own service too, and have the ARIA service depend on it.

> This uses `nssm.exe`. If you haven't got it yet, do **step 1** first, then come
> back here.

### 0a. Stop the Ollama desktop app from also serving

The Ollama tray app serves on the same port (`11434`); if it launches at login it
collides with the service. **Quit the tray app**, and in its settings turn **off
"Start Ollama on login"**. The service replaces it as the single instance.

### 0b. Install the OllamaServe service

Run in an **Administrator** PowerShell. These are the real paths on this machine —
verify `ollama.exe` with `(Get-Command ollama).Source` if unsure.

```powershell
nssm install OllamaServe "C:\Users\Ayyan\AppData\Local\Programs\Ollama\ollama.exe" serve
nssm set OllamaServe AppDirectory "C:\Users\Ayyan"
nssm set OllamaServe Start SERVICE_AUTO_START

# CRITICAL: a service runs as LocalSystem, whose model folder is EMPTY. Point it
# at the models you already pulled as your user — otherwise every cycle fails
# with "model ... not found" even though `ollama list` works in your own shell.
nssm set OllamaServe AppEnvironmentExtra "OLLAMA_MODELS=C:\Users\Ayyan\.ollama\models" "OLLAMA_HOST=127.0.0.1:11434"

# capture + rotate output (Ollama keeps its own logs too; this is a safety net)
nssm set OllamaServe AppStdout "C:\Users\Ayyan\Desktop\aria\logs\ollama.stdout.log"
nssm set OllamaServe AppStderr "C:\Users\Ayyan\Desktop\aria\logs\ollama.stdout.log"
nssm set OllamaServe AppRotateFiles 1
nssm set OllamaServe AppRotateOnline 1
nssm set OllamaServe AppRotateBytes 10485760

# graceful stop (Ctrl+C) + auto-restart if it ever exits
nssm set OllamaServe AppStopMethodConsole 15000
nssm set OllamaServe AppExit Default Restart
nssm set OllamaServe AppThrottle 10000
nssm set OllamaServe Description "Ollama local model server for ARIA"

nssm start OllamaServe
```

### 0c. Verify it's serving your models

```powershell
ollama list          # should list gemma3, deepseek-r1:14b, etc.
```

If the list is **empty**, `OLLAMA_MODELS` is pointing at the wrong place — fix the
path in step 0b and `nssm restart OllamaServe`.

> **Check the GPU still works under the service.** A LocalSystem service runs in
> "session 0", where GPU access sometimes falls back to CPU. Trigger one model
> load (`ollama run gemma3 "hi"`), then `ollama ps` — `PROCESSOR` should still
> show a GPU share (as it does when you run Ollama normally). If it's CPU-only,
> either run this service under **your user account** instead
> (`nssm set OllamaServe ObjectName ".\Ayyan" "<password>"`, which needs the
> "log on as a service" right), or keep Ollama as a login-start app and accept
> that the ARIA daemon waits (gracefully) until you log in.

If you skip section 0 entirely, the ARIA daemon still runs — its agents just wait
(logging `Ollama not reachable`) until you log in and Ollama starts.

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

# depend on the Ollama service from step 0: Windows starts OllamaServe first and
# won't start ARIADaemon until it is running. (Skip this line if you did NOT
# install OllamaServe — otherwise ARIADaemon would refuse to start.)
nssm set ARIADaemon DependOnService OllamaServe
```

**Belt and suspenders on ordering.** The dependency guarantees Windows launches
`OllamaServe` *before* `ARIADaemon`. But "running" only means Ollama's process
started — not that it has finished loading and is answering on `11434`. The
daemon's built-in `wait_for_ollama` covers that last gap (it waits for a real
response with backoff). So even if you skip the dependency, the daemon still
tolerates a slow Ollama; the dependency just makes the ordering explicit and
avoids a burst of `Ollama not reachable` lines at every boot.

> `sc` equivalent (if you prefer): `sc config ARIADaemon depend= OllamaServe`
> (the space after `depend=` is required, and it replaces the whole depend list).
>
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

`OllamaServe` is an independent service — the same verbs manage it
(`nssm start|stop|restart|status OllamaServe`). Stopping `ARIADaemon` leaves
Ollama running; if you stop `OllamaServe`, the daemon's next cycles log
`Ollama not reachable` and wait until it's back. If you ever `nssm remove`
`OllamaServe`, first clear the dependency so `ARIADaemon` can still start:
`nssm reset ARIADaemon DependOnService` (or `sc config ARIADaemon depend= ""`).

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
7. **Cycles fail with `model ... not found` but `ollama list` works in your shell**
   — the classic LocalSystem trap: the `OllamaServe` service is looking in the
   empty system-profile model folder. Confirm `OLLAMA_MODELS` is set on the
   service (`nssm get OllamaServe AppEnvironmentExtra`) and points at
   `C:\Users\Ayyan\.ollama\models`, then `nssm restart OllamaServe`.
8. **`ARIADaemon` refuses to start with a dependency error** — you set
   `DependOnService OllamaServe` but `OllamaServe` isn't installed (or is
   disabled). Either install it (step 0) or clear the dependency:
   `nssm reset ARIADaemon DependOnService`.
9. **Ollama runs but only on CPU under the service** — session-0 GPU limitation;
   see the GPU note in step 0c (run `OllamaServe` under your user account, or keep
   Ollama as a login-start app).

Run `daemon.py` by hand once to sanity-check the environment before blaming the
service: `python C:\Users\Ayyan\Desktop\aria\daemon.py` (Ctrl+C to stop).
