"""Slack bus — the message bus for ARIA's agent team.

One Slack bot app (Socket Mode) backs every persona. Each agent posts under its
own username + icon_emoji so Sarah/Tom/Ava/Bobby appear as distinct people in
channels. Consequential actions go through an approval message in #approvals
that a human signs off by reacting with an emoji.

All channel names and persona emoji come from config.py — nothing is hardcoded
here. While config.DRY_RUN is True, write operations are logged instead of sent.
"""
import logging
import socket
import ssl
import sys
import time
import urllib.error

from slack_sdk import WebClient

import config

client = WebClient(token=config.SLACK_BOT_TOKEN)
logger = logging.getLogger("aria.slack")

# --- Bounded retry for transient TLS/connection failures ---------------------
# This machine intermittently fails the TLS handshake to slack.com ("Hostname
# mismatch") in short episodes that self-recover within minutes — a whole agent
# cycle shouldn't die on a hiccup that would succeed seconds later.
#
# SAFETY: we retry ONLY failures raised while establishing the connection or
# during the TLS handshake, i.e. definitively BEFORE any request bytes reached
# Slack. That is what makes retrying a chat_postMessage safe here — the message
# provably never arrived, so a retry cannot duplicate it. Certificate
# verification itself is untouched; nothing here weakens it.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = (2, 6)     # sleep after attempts 1 and 2 -> ~8s worst case


def _is_pre_send_transient(exc):
    """True only for connection/handshake failures that happened before the
    request was transmitted.

    Deliberately NOT retried:
      * SlackApiError    — a real answer from Slack (invalid_auth,
                           channel_not_found); retrying would mask the bug.
      * urllib HTTPError — a real HTTP status (429/5xx) with its own semantics.
      * timeouts         — ambiguous: the request may already have been
                           delivered, so a retry could double-post.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False          # HTTPError subclasses URLError — must be excluded first
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason if exc.reason is not None else exc
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return False          # ambiguous even when wrapped in a URLError
    # Each of these can only occur before any request bytes are sent:
    #   cert verification -> TLS handshake; gaierror -> DNS never resolved;
    #   refused -> the TCP connection was never established.
    return isinstance(exc, (ssl.SSLCertVerificationError, socket.gaierror,
                            ConnectionRefusedError))


def _call(label, fn, **kwargs):
    """Invoke a Slack API call, retrying only pre-send transient failures."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return fn(**kwargs)
        except Exception as e:
            if not _is_pre_send_transient(e) or attempt == _RETRY_ATTEMPTS:
                raise
            delay = _RETRY_BACKOFF[attempt - 1]
            logger.warning(
                "%s: transient connection/TLS failure (attempt %d/%d) — %s: %s. "
                "Nothing was sent to Slack; retrying in %ds.",
                label, attempt, _RETRY_ATTEMPTS, type(e).__name__,
                str(e)[:120], delay)
            time.sleep(delay)


def _safe_print(line):
    """print() that never crashes on a non-encodable char (e.g. emoji/arrows on
    a cp1252 Windows console). Diagnostic output only, so lossy is fine."""
    try:
        print(line)
    except UnicodeEncodeError:
        enc = (sys.stdout.encoding or "ascii")
        print(line.encode(enc, "replace").decode(enc))


def post_as(name, icon, channel, text):
    """Post `text` to `channel` as persona `name` with `icon` (icon_emoji).

    `channel` may be a "#name" string or a channel ID. Returns the message ts
    on success, or None in dry-run mode. Falls back to config.PERSONAS for the
    icon if `icon` is falsy.
    """
    icon = icon or config.PERSONAS.get(name, ":robot_face:")
    if config.DRY_RUN:
        _safe_print(f"[DRY_RUN] post_as {name} {icon} -> {channel}: {text!r}")
        return None
    resp = _call("post_as", client.chat_postMessage,
                 channel=channel, text=text, username=name, icon_emoji=icon)
    return resp["ts"]


def read_channel(channel_id, limit=50, since_ts=None):
    """Return recent message texts (newest first) from a channel.

    Requires a channel ID (not "#name") — that's what conversations_history
    accepts. If `since_ts` is given, only messages strictly newer than that ts
    are returned, so callers can skip content they've already processed.
    Returns a list of message text strings.
    """
    resp = _call("read_channel", client.conversations_history,
                 channel=channel_id, limit=limit)
    messages = resp["messages"]
    if since_ts is not None:
        messages = [m for m in messages if float(m.get("ts", "0")) > float(since_ts)]
    return [m.get("text", "") for m in messages]


def history(channel_id, limit=50):
    """Return raw message dicts (newest first) — includes ts, bot_id, etc.

    Useful when you need more than the text (e.g. deleting messages by ts).
    """
    resp = _call("history", client.conversations_history,
                 channel=channel_id, limit=limit)
    return resp["messages"]


def delete_message(channel_id, ts):
    """Delete a single message by timestamp. Respects DRY_RUN.

    The bot can only delete messages it authored (which is every persona post,
    since they all go out under the one bot token).
    """
    if config.DRY_RUN:
        print(f"[DRY_RUN] delete_message {channel_id} ts={ts}")
        return
    _call("delete_message", client.chat_delete, channel=channel_id, ts=ts)


def request_approval(agent_name, description, payload_id):
    """Post an action to #approvals for human sign-off.

    Approve by reacting :white_check_mark: (✅) to the message, reject with
    :x: (❌). Returns the message ts, which the daemon polls via
    check_approval. Returns None in dry-run mode.
    """
    icon = config.PERSONAS.get(agent_name, ":robot_face:")
    text = (
        f"*APPROVAL NEEDED* [`{payload_id}`]\n{description}\n"
        f"_React :white_check_mark: to approve, :x: to reject._"
    )
    if config.DRY_RUN:
        _safe_print(f"[DRY_RUN] request_approval {agent_name} [{payload_id}]: {description!r}")
        return None
    resp = _call("request_approval", client.chat_postMessage,
                 channel=config.CHANNELS["approvals"],
                 text=text,
                 username=agent_name,
                 icon_emoji=icon)
    return resp["ts"]


def check_approval(channel_id, ts):
    """Return the approval state of the message at `ts` in `channel_id`.

    "approved" if a ✅ reaction is present, "rejected" if ❌, otherwise
    "pending". ✅ takes precedence if somehow both are present.
    """
    resp = _call("check_approval", client.reactions_get,
                 channel=channel_id, timestamp=ts)
    reactions = {r["name"] for r in resp["message"].get("reactions", [])}
    if "white_check_mark" in reactions:
        return "approved"
    if "x" in reactions:
        return "rejected"
    return "pending"
