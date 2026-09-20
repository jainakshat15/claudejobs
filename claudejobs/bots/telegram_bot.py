"""Telegram front end.

Long-polls Telegram (outbound connections only — no public URL, no port to
open), turns messages into commands, and drains the outbound queue so questions
from running jobs reach the person who posted them.

Answering a question works two ways:
  * reply to the bot's question message — routed by message id, so it lands on
    the right job even when several of your jobs are waiting;
  * /reply <job id> <answer> — works from anywhere, including another chat.

Run with:  claudejobs telegram
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from telegram import Update
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from ..client import AdminClient, ApiError
from ..config import get_settings, setup_logging
from .common import COMMANDS, ChatContext, handle_command, parse_message

log = logging.getLogger("claudejobs.telegram")

MAX_MESSAGE_CHARS = 3800  # Telegram's limit is 4096; leave room for formatting
#: Telegram only accepts these characters in a command name, so a hyphenated
#: command (/ask-sales-bot) can only be registered under its underscore name.
#: Telegram itself sends such a message as the "/ask" entity, which is why that
#: alias exists; common.parse_message reads the real name off the message text.
TELEGRAM_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
#: Commands anyone may use — they expose nothing but the caller's own ids.
PUBLIC_COMMANDS = {"help", "start", "whoami"}


def _chunks(text: str, size: int = MAX_MESSAGE_CHARS) -> list[str]:
    if len(text) <= size:
        return [text]
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > size:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts or [text]


class TelegramBot:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.token, self.allowed_users = self.settings.require_telegram()
        self.settings.require_api_token()
        self.client = AdminClient()
        self.worker_name = f"telegram-{self.settings.worker_id}"
        self._delivery_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    def _context(self, update: Update) -> ChatContext:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        chat_dirs = self.settings.telegram_chat_dirs
        reply_to = message.reply_to_message if message else None
        return ChatContext(
            channel="telegram",
            chat_id=str(chat.id),
            user_id=str(user.id),
            username=user.username or user.full_name or str(user.id),
            message_id=str(message.message_id) if message else None,
            thread_id=str(message.message_thread_id) if message and message.message_thread_id else None,
            reply_to_message_id=str(reply_to.message_id) if reply_to else None,
            default_directory=chat_dirs.get(str(chat.id)) or self.settings.default_directory or None,
        )

    def _is_allowed(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id in self.allowed_users)

    async def _reply(self, update: Update, text: str) -> None:
        message = update.effective_message
        for chunk in _chunks(text):
            try:
                await message.reply_text(chunk, disable_web_page_preview=True)
            except TelegramError as exc:
                log.error("could not reply in chat %s: %s", update.effective_chat.id, exc)
                return

    # ------------------------------------------------------------------ #
    async def on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return
        parsed = parse_message(message.text)
        if parsed is None:
            return
        command, args = parsed

        if command not in PUBLIC_COMMANDS and not self._is_allowed(update):
            log.warning("refused /%s from user %s (%s)", command,
                        update.effective_user.id, update.effective_user.username)
            await self._reply(
                update,
                "Not authorised. Send /whoami and ask the owner of this machine to add "
                "your user id to TELEGRAM_ALLOWED_USERS.")
            return

        ctx = self._context(update)
        reply = await asyncio.to_thread(handle_command, command, args, ctx, self.client)
        await self._reply(update, reply)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A plain message: most likely an answer to a job's question."""
        message = update.effective_message
        if message is None or not message.text or not self._is_allowed(update):
            return

        ctx = self._context(update)
        replying_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == context.bot.id
        )
        is_private = update.effective_chat.type == ChatType.PRIVATE
        if not replying_to_bot and not is_private:
            return  # don't grab every message in a group

        try:
            result = await asyncio.to_thread(
                self.client.route_answer,
                channel="telegram", chat_id=ctx.chat_id, user_id=ctx.user_id,
                username=ctx.username, body=message.text,
                reply_to_message_id=ctx.reply_to_message_id, thread_id=ctx.thread_id,
            )
        except ApiError as exc:
            if exc.status_code == 404 and not replying_to_bot:
                return  # nothing waiting and nobody asked us: stay quiet
            from .common import _friendly
            await self._reply(update, _friendly(exc))
            return

        await self._reply(update, f"✅ Sent to job #{result['job_id']}.")

    # ------------------------------------------------------------------ #
    async def deliver_outbound(self, application: Application) -> None:
        """Deliver queued questions and notices until the bot shuts down."""
        log.info("outbound delivery loop started")
        while True:
            try:
                rows = await asyncio.to_thread(
                    self.client.claim_outbound, channel="telegram",
                    claimed_by=self.worker_name, limit=10)
                for row in rows:
                    await self._deliver_one(application, row)
            except ApiError as exc:
                log.warning("delivery poll failed: %s", exc)
            except asyncio.CancelledError:
                log.info("outbound delivery loop stopped")
                raise
            except Exception:
                log.exception("unexpected error in the delivery loop")
            await asyncio.sleep(self.settings.outbound_poll_seconds)

    async def _deliver_one(self, application: Application, row: dict[str, Any]) -> None:
        kwargs: dict[str, Any] = {
            "chat_id": int(row["chat_id"]) if row["chat_id"].lstrip("-").isdigit() else row["chat_id"],
            "text": _chunks(row["body"])[0],
            "disable_web_page_preview": True,
        }
        if row.get("thread_id"):
            kwargs["message_thread_id"] = int(row["thread_id"])
        if row.get("reply_to_message_id"):
            kwargs["reply_to_message_id"] = int(row["reply_to_message_id"])

        try:
            sent = await application.bot.send_message(**kwargs)
        except BadRequest as exc:
            # The message we wanted to reply to is gone (deleted, or too old).
            log.info("retrying message #%s without the reply reference: %s", row["id"], exc)
            kwargs.pop("reply_to_message_id", None)
            try:
                sent = await application.bot.send_message(**kwargs)
            except TelegramError as retry_exc:
                await self._delivery_failed(row, retry_exc)
                return
        except TelegramError as exc:
            await self._delivery_failed(row, exc)
            return

        await asyncio.to_thread(
            self.client.outbound_sent, row["id"],
            provider_message_id=str(sent.message_id),
            provider_thread_id=str(sent.message_thread_id) if sent.message_thread_id else None)

    async def _delivery_failed(self, row: dict[str, Any], exc: Exception) -> None:
        log.error("could not deliver message #%s to chat %s: %s",
                  row["id"], row["chat_id"], exc)
        try:
            await asyncio.to_thread(self.client.outbound_failed, row["id"], error=str(exc))
        except ApiError as api_exc:
            log.error("could not record the delivery failure: %s", api_exc)

    # ------------------------------------------------------------------ #
    def build(self) -> Application:
        application = (Application.builder()
                       .token(self.token)
                       .post_init(self._on_start)
                       .post_shutdown(self._on_stop)
                       .build())
        commands = sorted(name for name in COMMANDS if TELEGRAM_COMMAND_RE.match(name))
        application.add_handler(CommandHandler(commands, self.on_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))
        application.add_error_handler(self._on_error)
        return application

    async def _on_start(self, application: Application) -> None:
        self._delivery_task = asyncio.create_task(self.deliver_outbound(application))
        log.info("telegram bot ready; %s user(s) allowed", len(self.allowed_users))

    async def _on_stop(self, application: Application) -> None:
        if self._delivery_task:
            self._delivery_task.cancel()
            try:
                await self._delivery_task
            except asyncio.CancelledError:
                pass
        self.client.close()

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("unhandled telegram error", exc_info=context.error)


def run() -> None:
    """Entry point for ``claudejobs telegram``."""
    setup_logging("claudejobs.telegram")
    bot = TelegramBot()
    bot.build().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    run()
