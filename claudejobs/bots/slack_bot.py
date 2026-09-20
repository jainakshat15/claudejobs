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
import time
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from ..client import AdminClient, ApiError
from ..config import get_settings, setup_logging
from .common import ChatContext, _friendly, handle_command, parse_message

log = logging.getLogger("claudejobs.slack")

MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")
PUBLIC_COMMANDS = {"help", "start", "whoami"}


class SlackBot:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.bot_token, self.app_token, self.allowed_users = self.settings.require_slack()
        self.settings.require_api_token()
        self.client = AdminClient()
        self.worker_name = f"slack-{self.settings.worker_id}"
        self.app = App(token=self.bot_token, logger=logging.getLogger("slack_bolt"))
        self._stop = threading.Event()
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

        @self.app.command("/claudejobs")
        def _slash(ack, command: dict, respond) -> None:
            ack()
            ctx = ChatContext(
                channel="slack",
                chat_id=str(command.get("channel_id")),
                user_id=str(command.get("user_id")),
                username=command.get("user_name") or str(command.get("user_id")),
                default_directory=self._default_dir(str(command.get("channel_id"))),
            )
            text = (command.get("text") or "help").strip()
            parsed = parse_message(text) or ("help", "")
            if parsed[0] not in PUBLIC_COMMANDS and not self._is_allowed(ctx.user_id):
                respond(self._refusal())
                return
            respond(handle_command(parsed[0], parsed[1], ctx, self.client))

    # ------------------------------------------------------------------ #
    def _default_dir(self, channel_id: str) -> str | None:
        return self.settings.slack_channel_dirs.get(channel_id) or self.settings.default_directory or None

    def _is_allowed(self, user_id: str) -> bool:
        return user_id in self.allowed_users

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
        try:
            response = self.app.client.chat_postMessage(
                channel=row["chat_id"], text=row["body"], thread_ts=thread_ts)
        except SlackApiError as exc:
            error = exc.response.get("error", str(exc))
            if error in {"thread_not_found", "message_not_found"} and thread_ts:
                try:
                    response = self.app.client.chat_postMessage(
                        channel=row["chat_id"], text=row["body"])
                except SlackApiError as retry_exc:
                    self._delivery_failed(row, retry_exc)
                    return
            else:
                self._delivery_failed(row, exc)
                return

        ts = response.get("ts")
        try:
            self.client.outbound_sent(
                row["id"], provider_message_id=str(ts),
                provider_thread_id=str(response.get("message", {}).get("thread_ts") or thread_ts or ts))
        except ApiError as exc:
            log.error("delivered message #%s but could not record it: %s", row["id"], exc)

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
