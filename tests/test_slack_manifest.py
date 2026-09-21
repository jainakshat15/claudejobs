"""The generated Slack manifest must stay in step with the commands that exist.

These run without a database or a Slack workspace.
"""

from __future__ import annotations

import yaml

from claudejobs import products
from claudejobs.bots.common import (
    COMMANDS,
    SLACK_RESERVED,
    UMBRELLA_COMMAND,
    slack_commands,
    strip_command_prefix,
)
from claudejobs.slack_manifest import build_manifest, render


def declared(prefix: str = "") -> list[str]:
    return [entry["command"] for entry in slack_commands(prefix)]


# --------------------------------------------------------------------------- #
# the manifest describes commands that actually exist
# --------------------------------------------------------------------------- #
def test_every_declared_command_has_a_handler():
    for command in declared():
        name = strip_command_prefix(command)
        assert name == UMBRELLA_COMMAND or name in COMMANDS, f"{command} has no handler"


def test_every_product_gets_a_command():
    names = set(declared())
    for product in products.CATALOGUE:
        expected = f"/{product.command.replace('_', '-')}"
        assert expected in names, f"{product.name} is missing from the manifest"


def test_the_umbrella_command_is_declared():
    assert f"/{UMBRELLA_COMMAND}" in declared()


# --------------------------------------------------------------------------- #
# Slack's own rules
# --------------------------------------------------------------------------- #
def test_no_command_collides_with_a_slack_builtin():
    """Slack owns /status, /remind and friends; an app cannot register them."""
    for command in declared():
        assert command.lstrip("/") not in SLACK_RESERVED, f"{command} is reserved by Slack"


def test_the_reserved_name_is_replaced_by_its_alias():
    names = declared()
    assert "/status" not in names          # Slack's own "set my status"
    assert "/job" in names                 # same handler, different spelling
    assert strip_command_prefix("/job") in COMMANDS


def test_a_prefix_frees_the_reserved_names():
    names = declared("cj-")
    assert all(name.startswith("/cj-") for name in names), names
    assert "/cj-status" in names           # no longer collides
    assert strip_command_prefix("/cj-status", "cj-") == "status"


def test_prefixed_commands_round_trip_to_their_handler():
    for prefix in ("", "cj-", "claudejobs-"):
        for command in declared(prefix):
            name = strip_command_prefix(command, prefix)
            assert name == UMBRELLA_COMMAND or name in COMMANDS, (prefix, command)


def test_command_names_use_hyphens_not_underscores():
    """Slack shows the command as typed, and /ask_sales_bot reads badly."""
    for command in declared():
        assert "_" not in command, command


# --------------------------------------------------------------------------- #
# the document itself
# --------------------------------------------------------------------------- #
def test_manifest_is_valid_yaml_with_the_fields_slack_needs():
    manifest = yaml.safe_load(render())

    assert manifest["display_information"]["name"]
    assert len(manifest["display_information"]["description"]) <= 140   # Slack's limit
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert "commands" in manifest["oauth_config"]["scopes"]["bot"]
    assert "chat:write" in manifest["oauth_config"]["scopes"]["bot"]
    assert "message.im" in manifest["settings"]["event_subscriptions"]["bot_events"]

    commands = manifest["features"]["slash_commands"]
    assert len(commands) == len(declared())
    for entry in commands:
        assert entry["command"].startswith("/")
        assert entry["description"]
        assert entry["should_escape"] is False


def test_rendered_manifest_matches_the_built_one():
    built = build_manifest(prefix="cj-")
    parsed = yaml.safe_load(render(prefix="cj-"))
    assert parsed == built


def test_quotes_and_backslashes_survive_the_yaml_writer():
    """Usage hints contain Windows paths and quotes; they must not break it."""
    manifest = yaml.safe_load(render())
    hints = {entry["command"]: entry.get("usage_hint", "") for entry in
             manifest["features"]["slash_commands"]}
    assert "<what to do>" in hints["/run"]
    assert hints["/reply"] == "<job id> <your answer>"


def test_committed_manifest_is_up_to_date():
    """deploy/slack-app-manifest.yml is generated; regenerate it when commands change."""
    from claudejobs.config import REPO_ROOT

    path = REPO_ROOT / "deploy" / "slack-app-manifest.yml"
    if not path.exists():
        return
    committed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert committed["features"]["slash_commands"] == slack_commands(), (
        "run: python -m claudejobs slack-manifest --out deploy/slack-app-manifest.yml"
    )
