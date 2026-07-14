"""Central configuration for ARIA + Agent Team.

Everything secret or environment-specific is loaded here from the environment
(via python-dotenv), and every shared constant — channel names, persona emoji —
lives here so nothing is hardcoded elsewhere in the codebase.
"""
import os

from dotenv import load_dotenv

# Load .env from the project root (same directory as this file), if present.
# Pin the path explicitly so dotenv does NOT walk up the tree and accidentally
# pick up an unrelated .env from a parent directory.
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH)


def _get_bool(name, default):
    """Parse a boolean environment variable. Accepts 1/true/yes/on (any case)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name, default):
    """Parse a numeric environment variable, falling back to default if unset
    or unparseable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_set(name, default):
    """Parse a comma-separated environment variable into a set of strings."""
    raw = os.environ.get(name)
    if raw is None:
        return set(default)
    return {item.strip() for item in raw.split(",") if item.strip()}


# --- Secrets (loaded from environment) --------------------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")  # Socket Mode (used later)

# --- Voice / TTS (ARIA — Step 6) --------------------------------------------
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")
# turbo is the low-latency model — ideal for a real-time voice assistant.
TTS_MODEL = os.environ.get("TTS_MODEL", "eleven_turbo_v2")

# --- Voice input / VAD (ARIA — Step 6) --------------------------------------
# webrtcvad aggressiveness 0-3 (higher = more aggressive at calling frames
# non-speech); silence tail (sec) that ends a recording once you stop talking.
VAD_AGGRESSIVENESS = int(_get_float("VAD_AGGRESSIVENESS", 2))
VAD_SILENCE_LIMIT_SEC = _get_float("VAD_SILENCE_LIMIT_SEC", 1.2)

# --- Wake word (ARIA — Step 6) ----------------------------------------------
# openWakeWord confidence (0-1) above which the wake word counts as detected.
WAKE_WORD_THRESHOLD = _get_float("WAKE_WORD_THRESHOLD", 0.5)

# --- Speech-to-text (ARIA — Step 6) -----------------------------------------
# faster-whisper model size. small.en is the default: medium.en is slower per
# utterance and, in practice here, didn't improve transcription (the remaining
# errors trace to the mic, not the model), so the speed wins. Bump to "medium.en"
# via the WHISPER_MODEL_SIZE env var if better hardware/mic makes it worthwhile.
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small.en")

# Stay muted this long after ARIA finishes speaking (conversation mode) before
# re-opening the mic, so residual speaker audio isn't captured as the next turn.
POST_TTS_MUTE_MS = int(_get_float("POST_TTS_MUTE_MS", 500))

# --- Global run mode --------------------------------------------------------
# DRY_RUN defaults to True: no consequential action executes for real unless
# this is explicitly turned off. Write operations to Slack are logged instead
# of sent while DRY_RUN is True.
DRY_RUN = _get_bool("DRY_RUN", True)

# --- Slack channels ---------------------------------------------------------
# One place for every channel name. Referenced by name (e.g. "#approvals") for
# posting; channel IDs (for history/reaction reads) come from the environment.
CHANNELS = {
    "feedback": "#feedback",
    "support_intel": "#support-intel",
    "dev": "#dev",
    "content": "#content",
    "ads": "#ads",
    "approvals": "#approvals",
    "ops": "#ops",          # daemon health/error channel
    "chat_sarah": "#chat-sarah",   # conversation-mode transcript channels
    "chat_bobby": "#chat-bobby",
    "chat_ava": "#chat-ava",
    "chat_tom": "#chat-tom",
}

# Channel IDs (e.g. "C0123ABCD") are required by conversations_history and
# reactions_get, which do not accept "#name". Fill these in .env once the
# channels exist. Missing IDs are simply left as None.
CHANNEL_IDS = {
    "feedback": os.environ.get("CHANNEL_ID_FEEDBACK"),
    "support_intel": os.environ.get("CHANNEL_ID_SUPPORT_INTEL"),
    "dev": os.environ.get("CHANNEL_ID_DEV"),
    "content": os.environ.get("CHANNEL_ID_CONTENT"),
    "ads": os.environ.get("CHANNEL_ID_ADS"),
    "approvals": os.environ.get("CHANNEL_ID_APPROVALS"),
    "ops": os.environ.get("CHANNEL_ID_OPS"),
    "chat_sarah": os.environ.get("CHANNEL_ID_CHAT_SARAH"),
    "chat_bobby": os.environ.get("CHANNEL_ID_CHAT_BOBBY"),
    "chat_ava": os.environ.get("CHANNEL_ID_CHAT_AVA"),
    "chat_tom": os.environ.get("CHANNEL_ID_CHAT_TOM"),
}

# --- Personas ---------------------------------------------------------------
# name -> Slack icon_emoji. Each agent posts as its own "person" via
# chat.postMessage's username + icon_emoji.
PERSONAS = {
    "Sarah": ":woman-tipping-hand:",
    "Tom": ":man-technologist:",
    "Ava": ":clapper:",
    "Bobby": ":chart_with_upwards_trend:",
    "ARIA": ":large_blue_diamond:",
}

# --- Per-agent voices (Step 7B) ---------------------------------------------
# name -> ElevenLabs Voice ID, so each teammate speaks in their OWN voice during
# conversation mode. ARIA keeps ELEVENLABS_VOICE_ID for her own lines. Any entry
# left unset (missing/empty env var) is None; speak() then falls back to ARIA's
# voice with a logged warning rather than crashing.
AGENT_VOICES = {
    "Sarah": os.environ.get("SARAH_VOICE_ID"),
    "Tom": os.environ.get("TOM_VOICE_ID"),
    "Ava": os.environ.get("AVA_VOICE_ID"),
    "Bobby": os.environ.get("BOBBY_VOICE_ID"),
}

# --- Per-agent model overrides ----------------------------------------------
# Phase 1 decision (measured): ONE model everywhere — gemma3. deepseek-r1:14b was
# dropped: on a 4GB GPU it ran at ~2.5 tok/s (8.5-min cycles) because only ~23% of
# the 9GB model offloaded to the GPU. gemma3 runs ~6x faster (~15.7 tok/s) and is
# usable. Agents are now differentiated by PROMPT, not by model — which also
# eliminates the ~28.5s model-swap tax that hit whenever the daemon alternated
# between deepseek (cycles) and gemma3 (conversation). The reasoning-vs-fast split
# is gone: gemma3 fills both roles.
DEFAULT_MODEL = "gemma3"
AGENT_MODELS = {
    "Sarah": "gemma3",
    "Tom": "gemma3",
    "Ava": "gemma3",
    "Bobby": "gemma3",
    # ARIA and ARIA_reasoning collapse onto the same model now; the key is kept so
    # aria_agent's reasoning_model lookup still resolves without a code change.
    "ARIA": "gemma3",
    "ARIA_reasoning": "gemma3",
}

# --- Context window ---------------------------------------------------------
# Phase 2: raise the context window from Ollama's 4096 default to 8192, passed in
# options on every /api/chat call (more reliable than a separate Modelfile tag —
# it applies unconditionally and needs no special model name). This kills the
# latent Sarah truncation risk (her fixed prompt overhead is ~2900 tokens; at
# 4096 she'd truncate around ~13 detailed feedback messages). If 8192 overflows
# the 4GB GPU and ollama ps shows PROCESSOR falling to majority-CPU, step this
# down to 6144.
#
# MEASURED on this 4GB GPU: at 8192, ollama ps showed 56%/44% CPU/GPU (majority
# CPU) — the KV cache overflowed VRAM and spilled to CPU. Stepped down to 6144,
# which keeps the model majority-GPU. 6144 still clears the Sarah truncation risk
# (fixed overhead ~2900 + the ~2000-token context cap = ~4900, well under 6144).
NUM_CTX = int(_get_float("NUM_CTX", 6144))

# Conversation mode (Step 7A) uses a FAST LOCAL model for live back-and-forth
# chat — NOT each agent's heavier scheduled-cycle model (deepseek-r1:14b stays).
# CONVERSATION_SPEED_MODEL is the single knob for the speed/accuracy trade: swap
# it to compare local models without touching code. Measured on this box with a
# real #ads context (num_predict=60, think disabled):
#   qwen3.5:0.8b  ~3-4s/reply but FABRICATES (wrong ROAS %, invented budgets) and
#                 confuses who it's talking to — too small to stay grounded.
#   gemma3        ~5s warm, numbers accurate and grounded — best speed/accuracy.
#   hermes3       ~15-20s, accurate but too slow for live voice.
# gemma3 wins: ~3-4x faster than hermes3 while staying grounded. All local.
CONVERSATION_SPEED_MODEL = os.environ.get("CONVERSATION_SPEED_MODEL", "gemma3")
# Optional per-agent overrides; any agent NOT listed falls back to
# CONVERSATION_SPEED_MODEL. Leave empty to run every agent on the speed model.
AGENT_CONVERSATION_MODELS = {}

# --- Budget guardrails ------------------------------------------------------
# Hard caps enforced in CODE (integrations/ads_platform.py), not just in the
# prompt. The model never gets a path to spend that bypasses these.
MAX_DAILY_BUDGET_DELTA = _get_float("MAX_DAILY_BUDGET_DELTA", 15)   # per action
MAX_TOTAL_DAILY_SPEND = _get_float("MAX_TOTAL_DAILY_SPEND", 200)    # across all

# --- Developer / repo (Tom) -------------------------------------------------
# Tom works on a THROWAWAY practice repo, never his own codebase. Absolute path
# so subprocess git calls have an unambiguous working directory.
TOM_REPO_PATH = os.path.abspath(
    os.environ.get("TOM_REPO_PATH")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tom_workspace")
)
# Scope caps — Tom refuses any plan/diff exceeding these. No unlimited changes.
MAX_FILES_TOUCHED_PER_CYCLE = int(_get_float("MAX_FILES_TOUCHED_PER_CYCLE", 2))
MAX_DIFF_LINES = int(_get_float("MAX_DIFF_LINES", 40))

# --- Content / publishing (Ava) ---------------------------------------------
# Real Remotion render is gated behind this; default stub just writes a fake MP4.
USE_REAL_REMOTION = _get_bool("USE_REAL_REMOTION", False)
# Where rendered clips (stub or real) are written.
RENDER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renders")
# AI image generation via Pollinations.ai (free, no API key). When True, Ava can
# generate images to feed the Remotion pipeline; when False the integration is
# skipped so callers fall back. Generated PNGs land in RENDER_DIR/generated/.
USE_POLLINATIONS = _get_bool("USE_POLLINATIONS", True)
# Platforms Ava is allowed to schedule to — enforced in code (metricool.py).
METRICOOL_PLATFORM_ALLOWLIST = _get_set(
    "METRICOOL_PLATFORM_ALLOWLIST", {"instagram", "tiktok", "youtube_shorts"}
)
