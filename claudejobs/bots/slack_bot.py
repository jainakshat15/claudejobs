"""Slack front end (Socket Mode).

Socket Mode means the bot dials out to Slack over a websocket, so the laptop
needs no public URL, no tunnel and no inbound firewall rule.

Commands are the same as in Telegram and can be typed with or without a leading
slash — Slack reserves real slash commands, so `run fix the tests` and
`/claudejobs run fix the tests` both work. In a channel, mention the bot.

Answers are routed by thread: every message about a job is posted in the thread
of the request that created it, so replying in that thread reaches the right job
even when several of yours are waiting.

Run with:  claudejobs slack
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from ..client import AdminClient, ApiError
from ..config import get_settings, setup_logging
from .common import (
    UMBRELLA_COMMAND,
    ChatContext,
    _friendly,
    handle_command,
    parse_message,
    strip_command_prefix,
)

log = logging.getLogger("claudejobs.slack")

MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")
#: Slack routes only this app's own commands to us, so matching them all is
#: safe — and means the manifest decides which exist, not this file.
ANY_SLASH_COMMAND = re.compile(r"^/[A-Za-z0-9_-]+$")
#: Anything Slack sends that no listener above claims; acked quietly so Bolt
#: stops logging it as an unhandled 404.
ANY_EVENT = re.compile(r".+")
PUBLIC_COMMANDS = {"help", "start", "whoami"}

#: chat.postMessage answers with one of these when the bot is not a member of
#: the conversation — which is every DM between two other people, and every
#: channel it was never invited to. Slash commands work there anyway, so a job
#: can easily be started somewhere its answers cannot be posted.
UNREACHABLE = {"channel_not_found", "not_in_channel", "is_archived",
               "channel_is_archived", "user_not_in_channel"}
#: The thread we meant to reply in has been deleted; post at top level instead.
THREAD_GONE = {"thread_not_found", "message_not_found"}


class SlackBot:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.bot_token, self.app_token, self.allowed_users = self.settings.require_slack()
        self.settings.require_api_token()
        self.client = AdminClient()
        self.worker_name = f"slack-{self.settings.worker_id}"
        self.app = App(token=self.bot_token, logger=logging.getLogger("slack_bolt"))
        self._stop = threading.Event()
        #: user id -> the bot's own DM with them, looked up once per process.
        self._dm_channels: dict[str, str] = {}
        self._register()

    # ------------------------------------------------------------------ #
    def _register(self) -> None:
        @self.app.event("app_mention")
        def _mention(event: dict, say) -> None:
            self.on_text(event, say)

        @self.app.event("message")
        def _message(event: dict, say) -> None:
            # Ignore our own messages, edits and joins.
            if event.get("bot_id") or event.get("subtype"):
                return
            self.on_text(event, say)

        # Slack only delivers commands that are registered to this app, so one
        # catch-all matcher covers whichever ones you declared in the manifest:
        # adding a command there needs no change here.
        @self.app.command(ANY_SLASH_COMMAND)
        def _slash(ack, command: dict, respond) -> None:
            ack()
            self.on_slash(command, respond)

        # Slack sends more than we subscribe to by name. Acking the rest keeps
        # "unhandled request" 404s out of the log; the listeners above still run.
        @self.app.event(ANY_EVENT)
        def _other(event: dict) -> None:
            log.debug("ignored %s event", event.get("type"))

    def on_slash(self, command: dict, respond) -> None:
        """Handle one slash command, whatever name it was registered under.

        Both spellings work: `/claudejobs run fix the tests` puts the command in
        the text, while `/run fix the tests` puts it in the command name.
        """
        origin = str(command.get("channel_id") or "")
        user_id = str(command.get("user_id") or "")
        # Slash commands work in any conversation, including DMs the bot is not
        # part of; a job started there must still be able to reach its poster.
        chat_id = self._reachable_channel(origin, user_id)
        ctx = ChatContext(
            channel="slack",
            chat_id=chat_id,
            user_id=user_id,
            username=command.get("user_name") or user_id,
            default_directory=self._default_dir(origin),
        )
        raw = str(command.get("command") or "")
        text = (command.get("text") or "").strip()
        name = strip_command_prefix(raw, self.settings.slack_command_prefix)

        if name in {UMBRELLA_COMMAND, "cj", ""}:
            parsed = parse_message(text) or ("help", "")
        else:
            parsed = parse_message(f"/{name} {text}".strip())
            if parsed is None:
                respond(f"`{raw}` is registered in Slack but is not a claudejobs "
                        f"command. Type `/{UMBRELLA_COMMAND} help` to see them all.")
                return

        if parsed[0] not in PUBLIC_COMMANDS and not self._is_allowed(ctx.user_id):
            respond(self._refusal())
            return
        reply = handle_command(parsed[0], parsed[1], ctx, self.client)
        if chat_id != origin:
            reply += ("\n\n_This conversation is not one I can post in, so this "
                      "job's questions and results will arrive in our DM._")
        respond(reply)

    # ------------------------------------------------------------------ #
    def _reachable_channel(self, channel_id: str, user_id: str) -> str:
        """The conversation a job started here should post its answers in.

        Slack runs a slash command wherever it is typed — including a DM
        between two other people, or a DM with another app. The bot is not a
        member of those, so `chat.postMessage` answers `channel_not_found` and
        every question the job asks is lost. Their DM with the bot is the one
        conversation it can always reach, so that is where those jobs report.

        Channels and group DMs are left alone: the bot may simply need an
        invite, and `_deliver_one` falls back to the DM if it never gets one.
        """
        if not channel_id.startswith("D"):
            return channel_id
        return self._dm_channel(user_id) or channel_id

    def _dm_channel(self, user_id: str) -> str | None:
        """The bot's own DM with this user, opening it on first use."""
        if not user_id:
            return None
        known = self._dm_channels.get(user_id)
        if known:
            return known
        try:
            response = self.app.client.conversations_open(users=user_id)
        except SlackApiError as exc:
            # Missing im:write is the usual cause — see docs/SLACK_SETUP.md.
            log.error("could not open a DM with %s: %s", user_id,
                      exc.response.get("error", exc))
            return None
        channel_id = str(response.get("channel", {}).get("id") or "")
        if channel_id:
            self._dm_channels[user_id] = channel_id
        return channel_id or None

    def _default_dir(self, channel_id: str) -> str | None:
        return self.settings.slack_channel_dirs.get(channel_id) or self.settings.default_directory or None

    def _is_allowed(self, user_id: str) -> bool:
        # SLACK_ALLOWED_USERS=* opens the bot to the whole workspace.
        return self.settings.slack_open_to_everyone or user_id in self.allowed_users

    def _refusal(self) -> str:
        return ("Not authorised. Type `whoami` and ask the owner of this machine to add "
                "your Slack member id to SLACK_ALLOWED_USERS.")

    def _context(self, event: dict) -> ChatContext:
        channel_id = str(event.get("channel"))
        thread = event.get("thread_ts") or event.get("ts")
        return ChatContext(
            channel="slack",
            chat_id=channel_id,
            user_id=str(event.get("user")),
            username=str(event.get("user_profile", {}).get("display_name") or event.get("user")),
            message_id=str(event.get("ts")),
            thread_id=str(thread) if thread else None,
            reply_to_message_id=str(event.get("thread_ts")) if event.get("thread_ts") else None,
            default_directory=self._default_dir(channel_id),
        )

    # ------------------------------------------------------------------ #
    def on_text(self, event: dict, say) -> None:
        text = MENTION_RE.sub("", event.get("text") or "").strip()
        if not text:
            return
        ctx = self._context(event)
        thread_ts = event.get("thread_ts") or event.get("ts")

        parsed = parse_message(text)
        if parsed:
            command, args = parsed
            if command not in PUBLIC_COMMANDS and not self._is_allowed(ctx.user_id):
                log.warning("refused %s from %s", command, ctx.user_id)
                say(text=self._refusal(), thread_ts=thread_ts)
                return
            reply = handle_command(command, args, ctx, self.client)
            say(text=reply, thread_ts=thread_ts)
            return

        # Not a command — treat it as an answer to a waiting job.
        if not self._is_allowed(ctx.user_id):
            return
        try:
            result = self.client.route_answer(
                channel="slack", chat_id=ctx.chat_id, user_id=ctx.user_id,
                username=ctx.username, body=text,
                reply_to_message_id=ctx.reply_to_message_id, thread_id=ctx.thread_id)
        except ApiError as exc:
            # Only explain ourselves if they were replying in a job's thread.
            if exc.status_code == 404 and not event.get("thread_ts"):
                return
            say(text=_friendly(exc), thread_ts=thread_ts)
            return
        say(text=f"✅ Sent to job #{result['job_id']}.", thread_ts=thread_ts)

    # ------------------------------------------------------------------ #
    def deliver_outbound(self) -> None:
        """Background thread: push queued questions and notices into Slack."""
        log.info("outbound delivery loop started")
        while not self._stop.is_set():
            try:
                rows = self.client.claim_outbound(channel="slack",
                                                  claimed_by=self.worker_name, limit=10)
                for row in rows:
                    self._deliver_one(row)
            except ApiError as exc:
                log.warning("delivery poll failed: %s", exc)
            except Exception:
                log.exception("unexpected error in the delivery loop")
            self._stop.wait(self.settings.outbound_poll_seconds)
        log.info("outbound delivery loop stopped")

    def _deliver_one(self, row: dict[str, Any]) -> None:
        # Keep every message about a job in the thread of the original request.
        thread_ts = row.get("thread_id") or row.get("reply_to_message_id")
        # Each attempt is a strictly smaller claim than the one before — drop
        # the thread, then drop the channel — so this loop always terminates.
        attempts = [(str(row["chat_id"]), thread_ts, row["body"])]
        last_error: Exception | None = None

        while attempts:
            channel, thread, body = attempts.pop(0)
            try:
                response = self.app.client.chat_postMessage(
                    channel=channel, text=body, thread_ts=thread)
            except SlackApiError as exc:
                last_error = exc
                error = exc.response.get("error", str(exc))
                if error in THREAD_GONE and thread:
                    attempts.append((channel, None, body))
                elif error in UNREACHABLE:
                    fallback = self._dm_channel(str(row.get("user_id") or ""))
                    if fallback and fallback != channel:
                        log.warning("message #%s: cannot post in %s (%s); "
                                    "sending it to the poster's DM instead",
                                    row["id"], channel, error)
                        attempts.append((fallback, None, self._redirect_note(channel) + body))
                continue

            ts = response.get("ts")
            try:
                self.client.outbound_sent(
                    row["id"], provider_message_id=str(ts),
                    provider_thread_id=str(response.get("message", {}).get("thread_ts")
                                           or thread or ts),
                    chat_id=channel)
            except ApiError as exc:
                log.error("delivered message #%s but could not record it: %s", row["id"], exc)
            return

        self._delivery_failed(row, last_error or RuntimeError("no delivery attempt succeeded"))

    @staticmethod
    def _redirect_note(channel: str) -> str:
        """One line explaining why this landed in the DM and not where it was asked."""
        where = f"in <#{channel}>" if channel.startswith(("C", "G")) else "where you asked"
        invite = " — invite me there with `/invite @claudejobs`" if where.startswith("in") else ""
        return f"_I could not post this {where}{invite}._\n\n"

    def _delivery_failed(self, row: dict[str, Any], exc: Exception) -> None:
        log.error("could not deliver message #%s to %s: %s", row["id"], row["chat_id"], exc)
        try:
            self.client.outbound_failed(row["id"], error=str(exc))
        except ApiError as api_exc:
            log.error("could not record the delivery failure: %s", api_exc)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        delivery = threading.Thread(target=self.deliver_outbound, name="slack-delivery",
                                    daemon=True)
        delivery.start()
        handler = SocketModeHandler(self.app, self.app_token)
        if self.settings.slack_open_to_everyone:
            log.warning(
                "slack bot connecting OPEN TO THE WHOLE WORKSPACE (SLACK_ALLOWED_USERS=*): "
                "anyone who can message this app can run jobs on %s",
                self.settings.worker_id)
        else:
            log.info("slack bot connecting; %s user(s) allowed", len(self.allowed_users))
        try:
            handler.start()  # blocks until interrupted
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
        finally:
            self._stop.set()
            delivery.join(timeout=5)
            try:
                handler.close()
            except Exception:
                pass
            self.client.close()


def run() -> None:
    """Entry point for ``claudejobs slack``."""
    setup_logging("claudejobs.slack")
    SlackBot().start()


if __name__ == "__main__":
    run()
