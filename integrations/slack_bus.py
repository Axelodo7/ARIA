"""Slack bus — the message bus for ARIA's agent team.

One Slack bot app (Socket Mode) backs every persona. Each agent posts under its
own username + icon_emoji so Sarah/Tom/Ava/Bobby appear as distinct people in
channels. Consequential actions go through an approval message in #approvals
that a human signs off by reacting with an emoji.

All channel names and persona emoji come from config.py — nothing is hardcoded
here. While config.DRY_RUN is True, write operations are logged instead of sent.
"""
from slack_sdk import WebClient

import config

client = WebClient(token=config.SLACK_BOT_TOKEN)


def post_as(name, icon, channel, text):
    """Post `text` to `channel` as persona `name` with `icon` (icon_emoji).

    `channel` may be a "#name" string or a channel ID. Returns the message ts
    on success, or None in dry-run mode. Falls back to config.PERSONAS for the
    icon if `icon` is falsy.
    """
    icon = icon or config.PERSONAS.get(name, ":robot_face:")
    if config.DRY_RUN:
        print(f"[DRY_RUN] post_as {name} {icon} -> {channel}: {text!r}")
        return None
    resp = client.chat_postMessage(
        channel=channel, text=text, username=name, icon_emoji=icon
    )
    return resp["ts"]


def read_channel(channel_id, limit=50):
    """Return the most recent message texts (newest first) from a channel.

    Requires a channel ID (not "#name") — that's what conversations_history
    accepts. Returns a list of message text strings.
    """
    resp = client.conversations_history(channel=channel_id, limit=limit)
    return [m.get("text", "") for m in resp["messages"]]


def history(channel_id, limit=50):
    """Return raw message dicts (newest first) — includes ts, bot_id, etc.

    Useful when you need more than the text (e.g. deleting messages by ts).
    """
    resp = client.conversations_history(channel=channel_id, limit=limit)
    return resp["messages"]


def delete_message(channel_id, ts):
    """Delete a single message by timestamp. Respects DRY_RUN.

    The bot can only delete messages it authored (which is every persona post,
    since they all go out under the one bot token).
    """
    if config.DRY_RUN:
        print(f"[DRY_RUN] delete_message {channel_id} ts={ts}")
        return
    client.chat_delete(channel=channel_id, ts=ts)


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
        print(f"[DRY_RUN] request_approval {agent_name} [{payload_id}]: {description!r}")
        return None
    resp = client.chat_postMessage(
        channel=config.CHANNELS["approvals"],
        text=text,
        username=agent_name,
        icon_emoji=icon,
    )
    return resp["ts"]


def check_approval(channel_id, ts):
    """Return the approval state of the message at `ts` in `channel_id`.

    "approved" if a ✅ reaction is present, "rejected" if ❌, otherwise
    "pending". ✅ takes precedence if somehow both are present.
    """
    resp = client.reactions_get(channel=channel_id, timestamp=ts)
    reactions = {r["name"] for r in resp["message"].get("reactions", [])}
    if "white_check_mark" in reactions:
        return "approved"
    if "x" in reactions:
        return "rejected"
    return "pending"
