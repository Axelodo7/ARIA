# ARIA — System Architecture

A complete reference for how the ARIA system works: a local, voice-controlled, four-agent autonomous business team for ZentraIQ. Everything runs on Ayyan's machine. No paid LLM APIs — all reasoning is local via Ollama. Slack is the coordination and control layer. ElevenLabs handles voice output; Pollinations handles image generation.

---

## 1. The Big Picture

Two connected systems that share code, config, and Slack channels but run as separate processes:

**System 1 — The Agent Team (daemon.py)**
Four autonomous agents (Sarah, Tom, Ava, Bobby) that run on a schedule, do real work, and coordinate through Slack. Runs 24/7 as long as the machine is on. This is the "works while you're offline" layer.

**System 2 — ARIA (main.py)**
The voice command center. Wake word → speech-to-text → ARIA reasons → acts or dispatches → text-to-speech → visual HUD. This is the "talk to your team" layer. Runs when you're at the machine.

```
                          YOUR MACHINE (Windows, always-on for the daemon)
   ┌─────────────────────────────────────────────────────────────────────────┐
   │                                                                           │
   │   SYSTEM 2: ARIA (main.py)              SYSTEM 1: Agent Team (daemon.py)  │
   │   ┌───────────────────────┐             ┌───────────────────────────────┐ │
   │   │ wake word (hey jarvis)│             │ APScheduler                   │ │
   │   │   ↓                   │             │  Sarah  every 2h              │ │
   │   │ record (VAD)          │             │  Tom    02:00                 │ │
   │   │   ↓                   │             │  Ava    10:00                 │ │
   │   │ transcribe (Whisper)  │             │  Bobby  09:00                 │ │
   │   │   ↓                   │             │  approvals poller  every 5m   │ │
   │   │ ARIA brain (gemma3,   │             └───────────────┬───────────────┘ │
   │   │   local)              │                             │                 │
   │   │   ↓                   │             ┌───────────────▼───────────────┐ │
   │   │ dispatch / answer /   │────────────▶│  Sarah  Tom  Ava  Bobby       │ │
   │   │   converse            │             │  (each = Ollama + persona +   │ │
   │   │   ↓                   │             │   business context + tools)   │ │
   │   │ speak (ElevenLabs)    │◀────────────│                               │ │
   │   │   ↓                   │             └───────────────┬───────────────┘ │
   │   │ HUD (pywebview)       │                             │                 │
   │   └───────────────────────┘                             │                 │
   │                                                         │                 │
   │              ┌──────────────────────────────────────────▼──────────────┐ │
   │              │  Ollama (local model server, localhost:11434)            │ │
   │              │  gemma3 everywhere · cycles+conv+ARIA · num_ctx 6144     │ │
   │              └──────────────────────────────────────────────────────────┘ │
   └──────────────────────────────────┬────────────────────────────────────────┘
                                       │
                          ┌────────────▼────────────┐
                          │         SLACK           │  ← coordination + control
                          │  #feedback #support-intel│     (you watch + approve
                          │  #dev #content #ads      │      from your phone)
                          │  #approvals #ops         │
                          │  #chat-sarah/-bobby/etc  │
                          └─────────────────────────┘
        External (free): ElevenLabs (voice out) · Pollinations (images) · [stubbed: Meta ads, Drive, Metricool, GitHub]
```

---

## 2. Core Design Principles

These are the rules the whole system was built on. They explain *why* it's shaped the way it is.

**Principle 1 — The model classifies and converses; Python decides, counts, and assembles.**
Local models fabricate. Every task with a verifiable correct answer (counts, money math, routing, exit detection, tool-result relay) lives in Python, not the model. The model's job is narrowed to what it's genuinely good at — labelling, classifying, holding a conversation; anything that must be *correct* is computed in code. This is the single most important pattern in the system.

The sharpest example is Sarah's digest. She used to read the raw feedback and cluster *and tally* it in one shot — and gemma3 reproducibly reported a 2-message cluster as "4 users." Worse, when the model was later left to *narrate* the finished digest, it fabricated a phantom URGENT security alert by copying the worked IDOR example straight out of its own prompt. So the digest was decomposed: the model only **labels** each message with a cluster (returning message-id lists as JSON); **Python counts** `len(ids)`, so the model can no longer state a tally; and **Python assembles** the final digest text deterministically, so there is no free-text step left in which to invent an item. Same pattern as Bobby's money math — the model narrates numbers it did not compute, and for Sarah now does not even render.

**Principle 2 — Nothing irreversible happens without a human ✅.**
Spending money (Bobby), publishing content (Ava), merging code (Tom) — all gated behind a Slack approval reaction. Agents propose; you approve from your phone; only then does an approved action execute. The gates are enforced in code, not just prompts, so a model hallucination can't bypass them.

**Principle 3 — No paid LLM APIs.**
All reasoning is local (Ollama). This keeps cost at zero, works offline, and keeps business data on-machine. The tradeoff is that local models are slower and less capable than Claude/GPT — which is why Principle 1 matters so much.

**Principle 4 — Slack is the nervous system.**
Every agent action, handoff, and approval flows through Slack. This gives one place to watch everything, mobile approvals, and a permanent audit log — without building a custom dashboard.

**Principle 5 — Build one layer at a time, verify before advancing.**
The system was built in strict order (Slack bus → Sarah → Bobby → Ava → Tom → daemon → voice → conversation → business context). Each layer was proven working before the next was added. This is why bugs were always isolatable.

---

## 3. Component Inventory

### Brains (one model, everywhere, local via Ollama)
**`gemma3` for everything** — all agent cycles, conversation mode, and ARIA — at **`num_ctx` 6144**. One model resident, differentiated by prompt, not by model.

| Use | Model | Why |
|---|---|---|
| Agent cycles (Sarah, Bobby, Ava, Tom) | `gemma3` | ~13 tok/s, usable on this hardware. Correctness comes from Python (Principle 1), not the model, so a heavier reasoning model isn't needed |
| Conversation mode | `gemma3` | Stays grounded in context; ~10s per reply — the local floor on this hardware |
| ARIA voice replies | `gemma3` + deterministic Python | Most of ARIA's work is deterministic routing; the model only handles freeform conversation |

**Why not `deepseek-r1:14b` (dropped).** It was the reasoning model for cycles, but on a 4GB GPU only ~23% offloaded — it ran at **~2.5 tok/s with 77% CPU spillover**, making a single cycle take **~8.5 minutes**. Collapsing every path onto gemma3 also removed the **~28.5s model-swap tax** the daemon paid each time it alternated between the cycle model and the conversation model. Measured before → after:

| Path | deepseek-r1:14b | gemma3 |
|---|---|---|
| Bobby daily cycle | ~8.5 min | **~1 min** |
| Sarah cycle | ~2.5 min | **~13 s** |

The one thing lost was deepseek's internal counting accuracy — which is exactly why Sarah's counting moved into Python (Principle 1). Nothing regressed that Python didn't then backstop.

### Hardware reality (the binding constraint)
Everything runs on a **4GB RTX A2000 laptop GPU**. That single limit drives the model decisions above:
- **Why gemma3, not something bigger** — a 14B model (deepseek-r1) barely fits and spills ~77% to CPU (2.5 tok/s). gemma3 is the largest model that stays usable here.
- **Why `num_ctx` 6144, not 8192** — at 8192 the KV cache overflowed VRAM and `ollama ps` fell to 56/44 CPU/GPU; 6144 keeps it balanced (54/46) while still clearing all prompt-truncation risk.
- **Why a cloud API stays on the table** — the one thing the hardware can't fix is conversation latency (~10s/reply is the local floor). If live-voice latency ever needs to drop, routing *just conversation mode* to a cloud API is the escape hatch (a config-level change) — at the cost of Principle 3.

### Voice pipeline (ARIA)
| Stage | Tech | Notes |
|---|---|---|
| Wake word | openWakeWord (`hey_jarvis`) | Stock model, 0.93-0.99 confidence in tests. Custom "hey Aria" is a future train job. |
| Recording | webrtcvad-wheels | VAD-gated: records until silence. Tunable aggressiveness + silence limit. |
| Speech-to-text | faster-whisper (`medium.en`, CPU int8) | ~5s transcription. Bottleneck is mic quality, not the model. |
| Reasoning | Ollama (see above) | |
| Text-to-speech | ElevenLabs (`eleven_turbo_v2`, streaming) | ~640ms first-audio. Distinct voice per agent + ARIA. |
| Visual | pywebview + Claude Design HUD | Fullscreen, frameless, 4 states: idle/listening/thinking/speaking |

### Coordination + control
- **Slack** (one bot app, Socket Mode) — agents post as distinct personas via `chat:write.customize`. Channels: `#feedback #support-intel #dev #content #ads #approvals #ops` + `#chat-{agent}` for conversations.
- **Approval flow** — agents post to `#approvals` with a payload_id; you react ✅/❌; the poller (every 5 min) checks reactions and executes approved actions.

### Integrations (real vs stubbed)
| Integration | Status | Used by |
|---|---|---|
| ElevenLabs (voice) | ✅ real | ARIA + all agents |
| Pollinations (images) | ✅ real, free | Ava (Stage 2) |
| Slack | ✅ real | everything |
| Ollama | ✅ real, local | all brains |
| Meta ads API | ⏳ stubbed (`ads_platform.py`) | Bobby |
| Google Drive | ⏳ stubbed (`gdrive.py`) | Ava |
| Metricool | ⏳ stubbed (`metricool.py`) | Ava |
| GitHub/git repo | ⏳ toy repo (`tom_workspace/`) | Tom |

---

## 4. The Four Agents

Each agent = the same Ollama server + a persona prompt (`agent_prompts.md`) + shared business context (`docs/user.md`) + a specific tool set + code-enforced guardrails.

### Sarah — Customer Support
- **Schedule:** every 2 hours
- **Reads:** `#feedback` (new user messages, bookmark-filtered so she never re-processes old feedback)
- **Does:** clusters feedback, writes a digest to `#support-intel` with 🚨 URGENT / BUGS (@Tom) / FEATURE REQUESTS (@Tom) / STORIES & PRAISE (@Ava)
- **Model:** `gemma3` (like everyone). Counting is no longer the model's job — she labels each message with a cluster, then Python counts the ids and assembles the digest deterministically (Principle 1), so the old "reasoning model for accurate counts" requirement is gone.
- **Guardrail:** read/write internal only; never contacts users directly
- **Business context effect:** replies in ZentraIQ's founder-to-founder voice, knows the pricing tiers

### Bobby — Ads Analyst
- **Schedule:** daily 09:00
- **Reads:** campaign stats (currently stubbed realistic data)
- **Does:** computes CPA/CTR/ROAS vs trailing 7 days, reasons in **cost-per-booked-call** (the ZentraIQ conversion event, $30-80 target), posts a report to `#ads`, files scale/pause proposals to `#approvals`
- **Model:** `gemma3`
- **Guardrails (belt + suspenders):** `apply_budget_change` refuses unless (1) DRY_RUN off, (2) proposal_id approved, (3) delta ≤ MAX_DAILY_BUDGET_DELTA. Money math is deterministic Python — the model only narrates.
- **Business context effect:** judges ads against booked-call cost + downstream client value ($8K-$23K), not generic e-commerce metrics

### Ava — Content
- **Schedule:** daily 10:00
- **Reads:** `#support-intel` (Sarah's digests, bookmark-filtered)
- **Does:** writes 3 hooks, picks one, generates a brand-aligned background (Pollinations), composites branding + hook text (Remotion), posts draft to `#content`, files a publish approval
- **Model:** `gemma3`
- **Guardrails:** every public post ✅-gated; a **fabrication filter** (deterministic) rejects any number/stat in the caption that isn't in the source; platform allowlist enforced in code
- **Business context effect:** writes in the contrarian-engineer brand voice; content types = contrarian claims, ZentraIQ Method, process breakdowns, pricing anchors

### Tom — Developer
- **Schedule:** nightly 02:00
- **Reads:** `TASKS.md` + failing tests in the target repo (currently `tom_workspace/` toy repo)
- **Does:** picks one failing task, posts a plan to `#dev`, edits on a working branch only, runs tests, posts diff + results, files a merge approval
- **Model:** `gemma3`
- **Guardrails:** `git_push_to_main` permanently refuses (code-locked); commits to `main` refused; MAX_FILES_TOUCHED + MAX_DIFF_LINES caps; `old_str` must match exactly once (prevents file corruption). Tom refuses rather than guess when a fix is beyond him.
- **Reality:** weakest link on a local model — good for small scoped fixes, refuses the hard ones (which is the safe behavior)

---

## 5. ARIA — The Command Center

ARIA is the voice interface. Her intelligence is mostly **deterministic Python routing**, with the model only handling open-ended conversation. This is Principle 1 applied to the assistant herself — it's what makes her reliable despite running on a small local model.

### Request routing (in order of priority)
1. **Explicit dispatch** — "ask/tell/have/get Bobby to X" → posts task to the agent's channel, fires their cycle, returns "Handed off to Bobby." (deterministic, instant, no model call)
2. **Conversation open** — "talk to / chat with / let me talk to Bobby" (any tense, robust to garbling) → enters live conversation mode with that agent
3. **Status query** — "what did Bobby find" / "how's the feedback" → deterministically reads that agent's channel and relays the latest real message
4. **Approvals query** — "what's in approvals" → deterministically reads `#approvals`, lists pending items
5. **Freeform** — anything else → the model answers (business questions from `docs/user.md`, general chat)

### The voice loop (main.py)
```
idle → [wake word "hey jarvis"] → listening → [play "Yes?" ack]
     → record (VAD) → thinking → transcribe → route (above)
     → dispatch: "Handed off to X" | answer: speak reply | converse: enter conversation mode
     → back to idle
```
Every state drives the HUD (`window.setState`). Per-turn errors are caught, logged, spoken as "Sorry sir, something went wrong," and return to idle — never a silent crash.

### Conversation mode (talking to an agent)
- Opens a session with the agent; loads their work-channel context ONCE at start (plus any topic-specific match)
- Agent replies in **their own voice** (ElevenLabs), opening with a one-time identification ("Bobby here, sir.")
- Replies capped at 2-3 sentences for voice (num_predict cap + prompt rule)
- Every turn mirrored to `#chat-{agent}` in Slack (permanent record)
- **Silence-hallucination filter:** Whisper's "You"/"thank you" on silence is rejected (exact whole-transcript match)
- **Echo filter:** if a turn ≥70% overlaps something ARIA just said (speaker bleed), rejected
- **Exit:** "thanks, that's all" / "goodbye" / "we're done" / 30s silence → "Back to standby"
- **Failsafe:** after N missed turns, graceful give-up; runs on the fast `gemma3` model

---

## 6. The Approval System (the safety core)

This is what makes the whole thing safe to run unattended. The pattern is identical across Bobby (money), Ava (publishing), Tom (code):

```
1. Agent decides an action is warranted (in code, from real data)
2. Agent posts an APPROVAL NEEDED message to #approvals with a unique payload_id
3. You see it on your phone (Slack notification)
4. You react ✅ (approve) or ❌ (reject)
5. The approvals poller (every 5 min) reads reactions
6. On ✅: the action executes — but ONLY through a code-gated function that
   re-checks DRY_RUN, the approval, and any hard caps before doing anything
7. On ❌ or no reaction: nothing happens
```

The critical detail: **the model never has a direct path to the irreversible action.** Even if Bobby's prompt were fully compromised, `apply_budget_change` still refuses unless all three code checks pass. The prompt is the first line; the code gate is the wall.

---

## 7. Business Context (docs/user.md)

A ~1000-word file describing ZentraIQ (offerings, pricing, brand voice, ad economics, hard rules, priorities). Loaded once at startup and prepended to **every** agent's prompt — cycles AND conversation AND ARIA — as a `<business_context>` block, before their persona.

This is what turns generic agents into ZentraIQ's team:
- Bobby reasons in booked-calls ($30-80 target), not generic ROAS
- Sarah replies in the founder-to-founder voice with real pricing
- Ava writes in the contrarian-engineer brand voice
- All agents respect the hard "never" rules (no fabricated guarantees, no margin leaks, no age-as-selling-point, no public disputes, fixed prices)

Order in every prompt: `business context → persona → tools/task`. Persona wins on conflict.

---

## 8. Data & State

| What | Where | Notes |
|---|---|---|
| Agent bookmarks | `memory/aria_memory.db` (SQLite) | Per-agent "last processed" marker so Sarah/Ava don't re-process old content |
| Conversation history | RAM, session-scoped | Dropped when the conversation ends; Slack is the permanent record |
| Secrets | `.env` (gitignored) | Slack tokens, ElevenLabs key + voice IDs, channel IDs. NEVER in `.env.example` (that's placeholders only) |
| Logs | `logs/daemon.log`, `logs/aria.log` | Rotating; every exit/error logged with reason |
| Generated media | `renders/` (gitignored) | Pollinations images, Remotion videos |
| Prompts | `agent_prompts.md`, `aria_system_prompt.md`, `docs/user.md` | Loaded at runtime, never inlined in code |

---

## 9. Project Structure

```
aria/
├── main.py                  # ARIA voice loop (System 2)
├── daemon.py                # Agent team scheduler (System 1)
├── config.py                # env-loaded config, models, channels, caps, DRY_RUN
├── docs/
│   └── user.md              # business context (loaded into every prompt)
├── agent_prompts.md         # Sarah/Tom/Ava/Bobby personas
├── aria_system_prompt.md    # ARIA persona
├── audio/
│   ├── wake_word.py         # openWakeWord
│   ├── record.py            # VAD-gated recording
│   ├── transcribe.py        # faster-whisper (resident)
│   └── speak.py             # ElevenLabs (voice_id per agent)
├── brain/
│   ├── hermes_common.py     # Ollama call, tool parsing, business-context loader
│   ├── aria_agent.py        # ARIA routing + reasoning
│   ├── conversation.py      # conversation-mode driver (open/exit/echo/hallucination)
│   └── base_agent.py        # shared agent class (run_turn, guards, prompt assembly)
├── agents/
│   ├── sarah.py  tom.py  ava.py  bobby.py
├── actions/
│   └── hud.py               # pywebview HUD driver
├── integrations/
│   ├── slack_bus.py         # post-as-persona, read, approvals
│   ├── pollinations.py      # free AI image gen
│   ├── remotion_runner.py   # video render (branded compositions)
│   ├── ads_platform.py      # STUB — Bobby's data + gated spend
│   ├── gdrive.py            # STUB — Ava's footage
│   └── metricool.py         # STUB — Ava's scheduling
├── ui/
│   ├── index.html           # Claude Design HUD (exposes window.setState)
│   └── support.js
├── video/                   # Remotion Node project (branded compositions)
├── assets/
│   └── aria_ack.wav         # pre-generated "Yes?" ack
├── memory/aria_memory.db    # bookmarks
├── logs/                    # daemon.log, aria.log
├── renders/                 # generated images + videos (gitignored)
├── scripts/                 # test_*.py for every layer
├── .env                     # secrets (gitignored)
├── .env.example             # placeholders (tracked)
└── requirements.txt
```

---

## 10. How to Run It

**The agent team (System 1):**
```
python daemon.py            # runs 24/7; agents fire on schedule
python daemon.py --once bobby   # fire one agent immediately (testing)
```
Requires: Ollama running, `DRY_RUN=False` in `.env` for real posts, machine stays awake.

**ARIA voice (System 2):**
```
python main.py              # wake word → "hey jarvis" → talk
```
Requires: Ollama running, a working mic (the current weak link), ElevenLabs key in `.env`.

**Testing any layer:** `scripts/test_*.py` — each layer has an isolated test (slack bus, each agent, conversation mode, TTS, STT, VAD, HUD, wake word, Pollinations, branded video).

---

## 11. Known Limits & Next Steps

**Current limits:**
- **Microphone** — the laptop mic produces garbled/empty captures ~half the time. A $30 wired USB mic solves it. This is the single biggest friction point and it's hardware, not code.
- **Conversation latency** — ~10s per reply is the local-model floor on the 4GB GPU. Fine for background cycles; noticeable in live voice. The only real fix is routing conversation mode to a cloud API (see *Hardware reality*), at the cost of the no-paid-API principle.
- **Prompt truncation — *fixed.*** Pinning `num_ctx` to 6144 and capping every channel read that feeds a prompt (~2000 tokens) means no prompt approaches the window; Sarah's digest, the path most at risk, no longer truncates.
- **Stubbed integrations** — Bobby (ads), Ava (Drive/Metricool), Tom (real repo) run on stub/toy data. Wiring real services is the "operate on the real business" step.

**Natural next builds (in rough priority):**
1. Real integrations, one at a time (Meta ads → real Bobby; Drive+Metricool → real Ava publishing; a real repo → real Tom)
2. `memory.md` — agents remembering decisions across time (the course's next file)
3. Stage 2 branded video pipeline completion (Ava's promotional content)
4. Custom "hey Aria" wake word (train openWakeWord)
5. Distinct-voice polish + wait-and-relay conversation improvements

---

## 12. The One-Paragraph Summary

ARIA is a local, zero-cost, voice-controlled team of four AI agents that run ZentraIQ's support, dev, content, and ads work autonomously on a schedule, coordinating through Slack so everything is watchable and controllable from a phone. It's safe to run unattended because every irreversible action (spending, publishing, merging) is gated behind a human ✅ enforced in code, and reliable despite small local models because everything with a right answer is computed in Python rather than left to the model. You talk to it by voice ("hey Jarvis"), it dispatches work or holds real conversations with individual agents in their own voices, and every agent knows ZentraIQ's business from a shared context file — so they operate as your team, not as generic bots.
