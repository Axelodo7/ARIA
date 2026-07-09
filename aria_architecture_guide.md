# ARIA Command Center + Agent Team — Full Build Architecture

Two connected systems, no cloud LLM APIs anywhere:

1. **ARIA** — your local voice + HUD command center (wake word → STT → Hermes → TTS, with the sci-fi HUD as its face).
2. **The Agent Team** — four autonomous Hermes agents (Sarah, Tom, Ava, Bobby) that run your product/content/ads loop and communicate in Slack, so you can watch everything in one place — even while you're offline.

```
                        ┌──────────────── YOUR MACHINE (always on) ────────────────┐
 [You, by voice] ──────▶ ARIA (Hermes) ──▶ relays tasks/questions ──┐              │
        ▲                     │  HUD (pywebview, ui/index.html)     ▼              │
        └── TTS replies ◀─────┘                            ┌───────────────┐       │
                                                           │  SLACK (bus)  │       │
                                                           └──────┬────────┘       │
              ┌─────────────┬──────────────┬─────────────────────┤                 │
              ▼             ▼              ▼                     ▼                 │
           SARAH          TOM            AVA                  BOBBY               │
        (support)      (developer)    (content)              (ads)                │
        feedback  ──▶  builds/fixes ─▶ turns it into  ──▶  tests what gets        │
        patterns       ships          content              attention, scales      │
              │             │              │                     │                 │
              ▼             ▼              ▼                     ▼                 │
         users/forms    your repo     Drive+Remotion       ad platform APIs       │
                                      → Metricool                                 │
                        └──────── all post updates to Slack channels ─────────────┘
```

**The loop:** Sarah finds the problem → Tom fixes it → Ava turns it into content → Bobby tests what's actually getting attention. Each handoff is a Slack message, so the whole pipeline is visible (and controllable) from your phone.

---

## 1. Component Choices

| Layer | Choice | Notes |
|---|---|---|
| All agent brains | **Hermes** (Nous Research) via **Ollama**, local | No API keys, no per-token cost, runs offline. All five personas (ARIA + 4 agents) are the *same model* with different system prompts — cheap to run, one model in memory. |
| Message bus | **Slack** (one bot app, Socket Mode) | One bot token; each agent posts with its own `username` + `icon_emoji` via `chat.postMessage`, so Sarah/Tom/Ava/Bobby appear as distinct "people" in channels. Socket Mode means no public webhook server needed. |
| Scheduling / offline runs | **APScheduler** | Cron-style triggers per agent (Sarah hourly, Bobby daily, etc.) inside one long-running Python daemon. Your machine must stay on — "offline" means *you're* offline, not the computer. |
| Voice (ARIA only) | openWakeWord + webrtcvad + faster-whisper + **Piper TTS** (local) | Piper replaces ElevenLabs to keep the no-cloud-API rule; it's a free local neural TTS with decent British voices (e.g. `en_GB-alan-medium`). If you decide voice quality matters more than the rule, ElevenLabs slots back in — it's one function. |
| HUD | Claude Design export at `ui/index.html` via **pywebview** | Already has idle/listening/thinking/speaking states; patched to expose `window.setState()`. |
| Sarah's inputs | Slack channel for user feedback + a simple form/inbox you pipe in | Start simple: a #feedback channel where user messages land (manually forwarded, or via a Google Form → Slack integration). |
| Tom's workspace | Your git repo + local dev tools | Tom edits code in a working branch, runs tests, and posts diffs for approval. He should NOT push to main unattended (see Guardrails). |
| Ava's tools | **Google Drive API** (footage) + **Remotion** (programmatic video, Node) + **Metricool API** (scheduling/posting) | Remotion renders clips from React compositions via CLI — Ava's Python calls `npx remotion render`. Metricool's API handles cross-platform scheduling. |
| Bobby's tools | Ad platform APIs (e.g. Meta Marketing API) | Requires an approved developer app + ad account access — the most setup-heavy integration here, and the one where guardrails matter most. |
| Storage | SQLite (`memory/aria_memory.db`) | Shared memory: agent notes, handoffs, campaign stats snapshots, content queue. |

---

## 2. Project Structure

```
aria/
├── main.py                      # ARIA voice loop (wake word → STT → brain → TTS)
├── daemon.py                    # Agent team daemon: scheduler + Slack listener (runs 24/7)
├── config.py                    # model names, Slack tokens, channel IDs, budget caps
├── audio/
│   ├── wake_word.py
│   ├── record.py
│   └── speak.py                 # Piper TTS
├── brain/
│   ├── hermes_common.py         # shared Ollama call + tool-call parsing/validation
│   └── aria_agent.py            # ARIA persona — your voice interface + team dispatcher
├── agents/
│   ├── base_agent.py            # shared agent class: prompt + tools + Slack posting
│   ├── sarah.py                 # support: reads #feedback, finds patterns, posts digests
│   ├── tom.py                   # developer: takes Sarah's notes, edits repo, proposes diffs
│   ├── ava.py                   # content: hooks → Drive footage → Remotion → Metricool
│   └── bobby.py                 # ads: reads campaign stats, proposes scale/cut actions
├── integrations/
│   ├── slack_bus.py             # post-as-persona, read channels, approval reactions
│   ├── gdrive.py                # list/download footage
│   ├── remotion_runner.py       # subprocess wrapper around npx remotion render
│   ├── metricool.py             # schedule/post content
│   └── ads_platform.py          # campaign stats + budget changes (behind approval gate)
├── memory/
│   ├── store.py
│   └── aria_memory.db
├── actions/
│   ├── files.py
│   ├── computer.py
│   └── hud.py                   # pywebview driver — loads ui/index.html
├── ui/
│   ├── index.html               # Claude Design export (patched with window.setState bridge)
│   └── support.js               # renamed from support.js.txt (HTML references ./support.js)
└── requirements.txt
```

---

## 3. Shared Hermes Infrastructure

```bash
ollama pull hermes3      # Hermes 3 (Llama-3.1 8B). If you have 16GB+ VRAM, consider a larger Hermes variant — Tom especially benefits.
ollama serve
```

```python
# brain/hermes_common.py
import requests, json, re

OLLAMA_URL = "http://localhost:11434/api/chat"

def call_hermes(messages, model="hermes3", temperature=0.4):
    r = requests.post(OLLAMA_URL, json={
        "model": model, "messages": messages, "stream": False,
        "options": {"temperature": temperature}
    })
    return r.json()["message"]["content"]

def parse_tool_call(text):
    m = re.search(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

def tools_block(tools):
    return f"""
You have access to the following tools. To call one, respond ONLY with:
<tool_call>{{"name": "<tool_name>", "arguments": {{...}}}}</tool_call>

<tools>
{json.dumps(tools)}
</tools>

If no tool is needed, respond in plain text."""
```

```python
# agents/base_agent.py
import json, threading
from brain.hermes_common import call_hermes, parse_tool_call, tools_block
from integrations.slack_bus import post_as

class BaseAgent:
    name = "agent"
    icon = ":robot_face:"
    channel = "#general"
    system_prompt = ""
    tools = []            # list of {"name", "description", "parameters"}
    tool_handlers = {}    # name -> callable(args) -> str

    def run_turn(self, task_text, history=None):
        messages = [{"role": "system", "content": self.system_prompt + tools_block(self.tools)}]
        messages += (history or [])
        messages.append({"role": "user", "content": task_text})
        reply = call_hermes(messages)
        for _ in range(6):  # hard cap on tool-call chains — prevents infinite loops
            call = parse_tool_call(reply)
            if not call:
                break
            if call["name"] not in self.tool_handlers:   # hallucinated-tool guard
                reply = f"(skipped unknown tool {call['name']})"
                break
            result = self.tool_handlers[call["name"]](call.get("arguments", {}))
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "tool", "content": json.dumps({"name": call["name"], "content": str(result)})})
            reply = call_hermes(messages)
        return reply

    def post(self, text):
        post_as(self.name, self.icon, self.channel, text)
```

---

## 4. The Slack Bus

Create ONE Slack app (api.slack.com/apps) with a bot token, Socket Mode enabled, and scopes: `chat:write`, `chat:write.customize`, `channels:history`, `channels:read`, `reactions:read`, `app_mentions:read`. Create channels: `#feedback`, `#support-intel`, `#dev`, `#content`, `#ads`, `#approvals`.

```python
# integrations/slack_bus.py
import os
from slack_sdk import WebClient

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

PERSONAS = {
    "Sarah": ":woman-tipping-hand:",
    "Tom":   ":man-technologist:",
    "Ava":   ":clapper:",
    "Bobby": ":chart_with_upwards_trend:",
    "ARIA":  ":large_blue_diamond:",
}

def post_as(name, icon, channel, text):
    client.chat_postMessage(channel=channel, text=text,
                            username=name, icon_emoji=icon)

def read_channel(channel_id, limit=50):
    resp = client.conversations_history(channel=channel_id, limit=limit)
    return [m.get("text", "") for m in resp["messages"]]

def request_approval(agent_name, channel, description, payload_id):
    """Post an action for human sign-off. Approve by reacting ✅ to the message."""
    resp = client.chat_postMessage(
        channel="#approvals",
        username=agent_name, icon_emoji=PERSONAS.get(agent_name, ":robot_face:"),
        text=f"*APPROVAL NEEDED* [`{payload_id}`]\n{description}\n_React ✅ to approve, ❌ to reject._"
    )
    return resp["ts"]  # daemon polls reactions on this timestamp

def check_approval(channel_id, ts):
    resp = client.reactions_get(channel=channel_id, timestamp=ts)
    reactions = {r["name"] for r in resp["message"].get("reactions", [])}
    if "white_check_mark" in reactions: return "approved"
    if "x" in reactions: return "rejected"
    return "pending"
```

**This approval pattern is the backbone of the whole system.** Agents draft and propose; consequential actions execute only after you react ✅ in #approvals — from your phone, in seconds. That keeps "watch it all happen in one place" AND "nothing irreversible happens without me."

---

## 5. The Four Agents

Full system prompts live in `agent_prompts.md` (separate file). Here's each agent's shape:

### Sarah — Support (`agents/sarah.py`)
- **Trigger:** every 2 hours (APScheduler) + on-demand via ARIA/Slack mention.
- **Reads:** `#feedback` channel (where user messages/form responses land).
- **Does:** clusters recurring issues, quantifies them ("4 users this week hit X"), drafts empathetic replies for you to send, posts a digest to `#support-intel` tagging Tom when something is a bug/feature and Ava when something is praise/story-worthy.
- **Tools:** `read_feedback`, `save_note`, `post_digest`.
- **No approval gate needed** — she only reads and writes internal notes. If you later wire her to reply to users directly, THAT goes behind the gate.

### Tom — Developer (`agents/tom.py`)
- **Trigger:** when Sarah posts a digest containing bugs/features + nightly maintenance run.
- **Does:** picks the top item, writes a plan, edits code in a `tom/` working branch, runs your test suite, posts the diff + test results to `#dev`.
- **Tools:** `read_repo_file`, `write_repo_file`, `run_tests`, `git_commit_branch`, `post_diff`.
- **Approval gate:** merging/pushing to main and deploying are ✅-gated. Honest note: an 8B local model writing unattended production code is the weakest link in this whole system — expect Tom to be genuinely useful for small, well-scoped fixes and to need your review on anything structural. The gate isn't bureaucracy; it's what makes Tom safe to run at all.

### Ava — Content (`agents/ava.py`)
- **Trigger:** daily at a set hour + when Sarah flags story-worthy feedback.
- **Does:** writes 3 hook options → picks footage from a Drive folder (`gdrive.list_footage`) → fills a Remotion composition's props (caption text, clip path) → `remotion_runner.render()` → posts the rendered video + caption to `#content` → schedules via Metricool **after ✅ approval**.
- **Tools:** `list_footage`, `download_clip`, `render_video`, `request_post_approval`, `schedule_post`.
- **Approval gate:** every public post. Auto-posting AI content with no human glance is how accounts get banned and brands get embarrassed — the ✅ takes you five seconds.

### Bobby — Ads (`agents/bobby.py`)
- **Trigger:** daily, after ad platforms refresh stats.
- **Does:** pulls campaign metrics, computes CPA/ROAS/CTR deltas vs. trailing 7 days, writes a plain-English report to `#ads` ("Creative B's CPA is 40% lower — recommend shifting $15/day from A"), then files scale/cut/pause proposals to `#approvals`.
- **Tools:** `get_campaign_stats`, `propose_budget_change`, `apply_budget_change` (only callable on approved proposals), `pause_creative`.
- **Hard limits in code, not just prompt:** max daily budget delta, max total daily spend cap in `config.py`, and `apply_budget_change` refuses anything not matching an approved proposal ID. Money moves are the one place where a hallucination is expensive — the model never gets direct, ungated access to spend.

### ARIA — dispatcher (`brain/aria_agent.py`)
Your existing voice persona gains one tool: `dispatch_to_agent(agent, task)` — posts a task into the right Slack channel as you. "Aria, ask Bobby how the campaigns did this week" → task posted → Bobby's next cycle answers in `#ads` → ARIA reads it back to you. HUD states unchanged (thinking while dispatching/waiting).

---

## 6. The Daemon (offline operation)

```python
# daemon.py — runs 24/7, this is what "works while I'm offline" actually is
from apscheduler.schedulers.blocking import BlockingScheduler
from agents.sarah import Sarah
from agents.tom import Tom
from agents.ava import Ava
from agents.bobby import Bobby

sarah, tom, ava, bobby = Sarah(), Tom(), Ava(), Bobby()
sched = BlockingScheduler()

sched.add_job(sarah.cycle, "interval", hours=2)
sched.add_job(tom.nightly, "cron", hour=2)
sched.add_job(ava.daily,  "cron", hour=10)
sched.add_job(bobby.daily, "cron", hour=9)
sched.add_job(lambda: [a.process_pending_approvals() for a in (tom, ava, bobby)],
              "interval", minutes=5)   # poll ✅ reactions, execute approved actions

if __name__ == "__main__":
    sched.start()
```

Run `python daemon.py` under a process manager (Task Scheduler on Windows, launchd on Mac, or just `pm2`/`nssm`) so it survives reboots. ARIA's voice loop (`main.py`) is separate — run it when you're at the machine.

---

## 7. Build Order (matters even more now)

1. **Slack bus first.** One bot, personas posting to channels, approval react-polling working. Test with hardcoded messages — no AI yet.
2. **Sarah, text-only.** Feed her fake feedback in #feedback, verify pattern digests land in #support-intel. She's the safest agent and proves the whole agent+Slack pattern.
3. **Bobby in dry-run mode.** Stats reading + proposals only; `apply_budget_change` stubbed to log instead of execute. Run him like this for at least a week before wiring real budget changes.
4. **Ava without posting.** Drive → Remotion render → video in #content. Add Metricool scheduling (gated) only after renders look right.
5. **Tom last.** He's the hardest to make reliable. Start him on trivial tasks (typo fixes, copy changes) in a branch.
6. **ARIA integration.** Voice dispatch + HUD. This is polish — the team works without it.
7. Each step: get it working manually before adding its scheduler job.

---

## 8. requirements.txt

```
slack-sdk
apscheduler
requests
openwakeword
webrtcvad
pyaudio
faster-whisper
piper-tts
pyautogui
fpdf2
numpy
pywebview
google-api-python-client
google-auth-oauthlib
```
(Remotion is a Node dependency — `npm i remotion @remotion/cli` in a `video/` subproject.)

---

## 9. Honest Realism Notes

- **Hermes 8B is the floor, not the ceiling.** Sarah and Bobby (summarize, classify, compute-and-report) will work well. Ava is mid (hooks are hit-or-miss; templates help). Tom on an 8B model will disappoint on anything beyond small scoped fixes. If this machine has a real GPU, run the largest Hermes variant that fits — it changes Tom from "demo" to "useful."
- **One model, five personas, one GPU:** Ollama queues requests, so simultaneous agent cycles serialize. Fine at these schedules; just don't schedule everyone at the same minute.
- **Ad platform access is the long pole.** Meta's Marketing API needs a developer app, review, and an active ad account — start Bobby against exported CSV stats if API access stalls, and he's still useful.
- **The approval gates are the design, not a limitation.** The pitch "it all happens while I'm offline" stays true — agents do all the work offline; the ✅ is you steering for five seconds a day from your phone.
