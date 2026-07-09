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


# --- Secrets (loaded from environment) --------------------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")  # Socket Mode (used later)

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
    "Tom": DEFAULT_MODEL,
    "Ava": DEFAULT_MODEL,
    "Bobby": DEFAULT_MODEL,
    "ARIA": DEFAULT_MODEL,
}
