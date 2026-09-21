"""Generate the Slack app manifest.

Slack can create or update an app from a manifest, which beats filling in the
"Create New Command" form once per command. Generating it from the command
catalogue means it cannot drift: add a product to products.CATALOGUE, re-run
this, and its /ask-… command is declared too.

    python -m claudejobs slack-manifest
    python -m claudejobs slack-manifest --prefix cj- --out deploy/slack-app-manifest.yml

Paste the result into https://api.slack.com/apps → your app → App Manifest.
"""

from __future__ import annotations

from typing import Any

from .bots.common import slack_commands

#: Least privilege for what the bot actually calls: it posts messages, and it
#: reads the conversations it is in so replies to a job's question arrive.
BOT_SCOPES = [
    "app_mentions:read",    # respond when mentioned in a channel
    "channels:history",     # read replies in public channels it is in
    "chat:write",           # post questions, progress and results
    "commands",             # slash commands
    "groups:history",       # ... and in private channels it is in
    "im:history",           # ... and in DMs
    "im:write",             # open a DM with the poster, for jobs started
                            # somewhere the bot cannot post (see slack_bot.py)
    "mpim:history",         # ... and in group DMs
]

BOT_EVENTS = [
    "app_mention",
    "message.channels",
    "message.groups",
    "message.im",
    "message.mpim",
]

DESCRIPTION = "Queue Claude Code jobs from Slack, and answer the questions they ask you back."


def build_manifest(*, app_name: str = "claudejobs", prefix: str = "") -> dict[str, Any]:
    return {
        "display_information": {
            "name": app_name,
            "description": DESCRIPTION,
        },
        "features": {
            # Without the messages tab, Slack tells anyone who opens a DM that
            # the app "has turned off direct messaging" — and a DM is where a
            # job's questions and answers are easiest to keep track of.
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {
                "display_name": app_name,
                "always_online": True,
            },
            "slash_commands": slack_commands(prefix),
        },
        "oauth_config": {
            "scopes": {"bot": BOT_SCOPES},
        },
        "settings": {
            "event_subscriptions": {"bot_events": BOT_EVENTS},
            "interactivity": {"is_enabled": False},
            "org_deploy_enabled": False,
            # Socket Mode: the bot dials out, so no public request URL is needed.
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


# --------------------------------------------------------------------------- #
# a small YAML writer — the manifest shape is fixed, so this beats a dependency
# --------------------------------------------------------------------------- #
def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _dump(value: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.extend(_dump(item, indent + 1))
            elif isinstance(item, (dict, list)):
                lines.append(f"{pad}{key}: {'{}' if isinstance(item, dict) else '[]'}")
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                rendered = _dump(item, indent + 1)
                # First key sits on the dash line.
                first = rendered[0].strip()
                lines.append(f"{pad}- {first}")
                lines.extend(rendered[1:])
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    return lines


def to_yaml(manifest: dict[str, Any]) -> str:
    return "\n".join(_dump(manifest)) + "\n"


def render(*, app_name: str = "claudejobs", prefix: str = "",
           header: bool = True) -> str:
    """The manifest as YAML, ready to paste into Slack."""
    body = to_yaml(build_manifest(app_name=app_name, prefix=prefix))
    if not header:
        return body
    note = (
        "# Slack app manifest for claudejobs — generated, do not edit by hand.\n"
        "#\n"
        "#   python -m claudejobs slack-manifest"
        + (f" --prefix {prefix}" if prefix else "")
        + "\n#\n"
        "# Paste into https://api.slack.com/apps → your app → App Manifest (YAML),\n"
        "# then reinstall the app when Slack asks: the scopes change.\n"
    )
    return note + body
