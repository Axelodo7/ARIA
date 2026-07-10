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
# faster-whisper model size. medium.en transcribes noticeably more accurately
# than small.en (fewer dropped/garbled words on real speech), at the cost of a
# larger one-time download (~1.5GB) and a little more per-utterance latency.
# If latency is a problem on lower-end hardware, override back to "small.en"
# (or "base.en") via the WHISPER_MODEL_SIZE env var — no code change needed.
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "medium.en")

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

# --- Per-agent model overrides ----------------------------------------------
# One local model in memory by default (hermes3), but any persona can override.
# Sarah runs on a reasoning model: hermes3 reliably miscounts clusters (it
# enumerates feedback correctly but then re-estimates the totals), whereas a
# reasoning model does that arithmetic internally. Slower per run, which is
# fine at her every-2-hours cadence.
DEFAULT_MODEL = "hermes3"
AGENT_MODELS = {
    "Sarah": "deepseek-r1:14b",
    # Tom fixes code — reasoning work, so the better model (the weakest link, so
    # give it every advantage).
    "Tom": "deepseek-r1:14b",
    # Ava writes hooks and captions — creative-reasoning work, so the better model.
    "Ava": "deepseek-r1:14b",
    # Bobby is numbers-and-reasoning (CPA/ROAS deltas, scale/cut calls) — the
    # workload where the reasoning model earns its slot, same as Sarah.
    "Bobby": "deepseek-r1:14b",
    # ARIA is split: her voice-loop responses (acks, dispatch confirmations,
    # direct answers, reading approvals aloud) run on the FAST model for low
    # latency; deeper work (summarizing long channels, ambiguous routing) uses
    # ARIA_reasoning.
    "ARIA": "hermes3",
    "ARIA_reasoning": "deepseek-r1:14b",
}

# Conversation mode (Step 7A) uses a FAST model for live back-and-forth chat —
# NOT each agent's heavier scheduled-cycle model (which stays deepseek-r1:14b).
AGENT_CONVERSATION_MODELS = {
    "Sarah": "hermes3", "Bobby": "hermes3", "Ava": "hermes3", "Tom": "hermes3",
}

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
# Platforms Ava is allowed to schedule to — enforced in code (metricool.py).
METRICOOL_PLATFORM_ALLOWLIST = _get_set(
    "METRICOOL_PLATFORM_ALLOWLIST", {"instagram", "tiktok", "youtube_shorts"}
)
