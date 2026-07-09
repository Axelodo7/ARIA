# Claude Code Kickoff Prompt

Paste this as your first message in Claude Code, run from an empty `aria/` project folder containing the four reference files.

---

I'm building an AI system with two parts, fully specified in the attached docs. Read all four files before writing any code:

1. `aria_architecture_guide.md` — the complete architecture. Follow its project structure, component choices, and especially its **Build Order (section 7)** exactly. Do not skip ahead.
2. `aria_system_prompt.md` — the system prompt for ARIA, the voice/HUD assistant persona. Load it at runtime; don't inline it into Python strings.
3. `agent_prompts.md` — system prompts for the four Slack agents (Sarah, Tom, Ava, Bobby). Same rule: load from file.
4. `ui/index.html` + `ui/support.js` — the finished HUD interface (do not redesign it). It exposes a global `window.setState("idle"|"listening"|"thinking"|"speaking")` which `actions/hud.py` drives via pywebview's `evaluate_js`. Preserve that contract.

Key constraints:
- **No cloud LLM APIs.** All reasoning is Hermes via a local Ollama server (`http://localhost:11434`). Assume `ollama pull hermes3` is done.
- **Approval gates are mandatory**, implemented in code, not just prompts: Tom's merges/deploys, Ava's posts/scheduling, and all of Bobby's budget actions execute only after a ✅ reaction on their #approvals Slack message. Bobby's hard spend caps live in `config.py` and are enforced in `integrations/ads_platform.py` regardless of what the model says.
- Secrets (Slack token, Google credentials, Metricool key) come from environment variables / a `.env` (gitignored) — never hardcoded.
- Every external integration gets a **dry-run mode** flag in `config.py` that logs the action instead of executing it. Default everything to dry-run.

Start with Build Order step 1 only: the Slack bus (`integrations/slack_bus.py` + a test script) — personas posting to channels, approval react-polling. Show me it working with hardcoded messages before we touch any agent logic. Then we proceed step by step.

Also generate: `requirements.txt`, `.env.example`, `.gitignore`, and a `README.md` explaining how to run `daemon.py` (agent team) and `main.py` (ARIA voice loop) separately.
