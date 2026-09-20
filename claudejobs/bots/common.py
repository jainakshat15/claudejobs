"""The command layer shared by the Telegram and Slack bots.

Everything here is synchronous and platform-agnostic: a command string plus a
ChatContext goes in, the text to send back comes out. The async bot calls it
through ``asyncio.to_thread``.

Each command maps onto one HTTP route, so anything you can do from chat you can
also do with curl (see docs/API.md).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from .. import ask_sales_bot, models
from ..client import AdminClient, ApiError
from ..config import ConfigError, get_settings

log = logging.getLogger(__name__)

#: `key:value` or `key:"value with spaces"` options in front of a /run prompt.
OPTION_RE = re.compile(r'^\s*([A-Za-z_]+):("(?P<quoted>[^"]*)"|(?P<bare>\S+))\s*')
ASSIGNMENT_RE = re.compile(r'^\s*([A-Za-z_]+)=("(?P<quoted>[^"]*)"|(?P<bare>\S+))\s*')

RUN_OPTIONS = {
    "dir": "directory", "directory": "directory", "path": "directory",
    "model": "model",
    "mode": "permission_mode", "permissions": "permission_mode",
    "prio": "priority", "priority": "priority",
    "timeout": "timeout_minutes",
    "title": "title",
    "attempts": "max_attempts",
}

EDIT_FIELDS = {
    "prompt": str, "directory": str, "title": str, "model": str,
    "permission_mode": str, "mode": str, "priority": int, "prio": int,
    "timeout_minutes": int, "timeout": int, "max_attempts": int, "attempts": int,
}


@dataclass
class ChatContext:
    """Who is talking to us, and where."""

    channel: str                 # "telegram" | "slack"
    chat_id: str
    user_id: str
    username: str
    message_id: str | None = None
    thread_id: str | None = None
    #: The chat message this one replies to, when the platform tells us.
    reply_to_message_id: str | None = None
    default_directory: str | None = None


HELP = """claudejobs — run Claude Code sessions from chat

Tap a command or type it. <angle brackets> are yours to fill in.

━ RUN WORK ━

/run <prompt>
  Queue a job. Example:
  /run fix the failing auth tests
  Options go before the prompt:
  dir:<path> — where to run (defaults to this chat's directory)
  model:<sonnet|opus|haiku>
  mode:<bypassPermissions|acceptEdits>
  prio:<1-1000> — lower runs sooner
  timeout:<minutes>
  title:"short name"
  Example: /run dir:D:\\work\\api prio:10 fix the auth tests

/ask_sales_bot <question>
  Ask about the Sales Bot product. Reads the flexi-demo repo and the
  Sales Bot docs, then answers here. Changes nothing. Example:
  /ask_sales_bot how does a rep get scored on a call?

━ SEE WHAT IS HAPPENING ━

/jobs [status] [n] — recent jobs, newest first
  status: queued, running, waiting_input, succeeded, failed,
  cancelled, timed_out, or active for everything still going
/status <id> — everything about one job
/log <id> [lines] — tail that job's worker log
/messages <id> — its questions, answers and notes
/events <id> — its state changes

━ STEER A JOB ━

/reply <id> <answer> — answer a job that is waiting on you
  (or just reply to the message it asked in)
/cancel <id> [reason] — stop a job, queued or running
/retry <id> — put a finished job back in the queue
/edit <id> key=value — change a job that has not started
  keys: prompt, directory, title, model, mode, priority,
  timeout, max_attempts

━ THIS MACHINE ━

/stats — queue depth and busy workers
/health — API, database and claude-on-PATH check
/whoami — your ids, for the allowlist
/help — this message

Every command maps onto one HTTP route; see docs/API.md."""


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #
def _pop_options(text: str, mapping: dict[str, str], pattern: re.Pattern) -> tuple[dict[str, str], str]:
    """Peel `key:value` (or `key=value`) pairs off the front of ``text``."""
    options: dict[str, str] = {}
    rest = text
    while True:
        match = pattern.match(rest)
        if not match:
            break
        key = match.group(1).lower()
        if key not in mapping:
            break
        value = match.group("quoted")
        if value is None:
            value = match.group("bare")
        options[mapping[key]] = value
        rest = rest[match.end():]
    return options, rest.strip()


def _job_id(token: str) -> int:
    cleaned = token.lstrip("#")
    if not cleaned.isdigit():
        raise ValueError(f"{token!r} is not a job id — try /jobs to see the ids")
    return int(cleaned)


def _split(args: str) -> tuple[str, str]:
    parts = args.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_help(client: AdminClient, ctx: ChatContext, args: str) -> str:
    return HELP


def cmd_whoami(client: AdminClient, ctx: ChatContext, args: str) -> str:
    return (f"channel:  {ctx.channel}\n"
            f"user id:  {ctx.user_id}\n"
            f"username: {ctx.username}\n"
            f"chat id:  {ctx.chat_id}\n"
            f"thread:   {ctx.thread_id or '—'}\n\n"
            f"Add the user id to "
            f"{'TELEGRAM_ALLOWED_USERS' if ctx.channel == 'telegram' else 'SLACK_ALLOWED_USERS'} "
            f"in .env to allow it to run jobs.")


def cmd_run(client: AdminClient, ctx: ChatContext, args: str) -> str:
    options, prompt = _pop_options(args, RUN_OPTIONS, OPTION_RE)
    if not prompt:
        return "Nothing to do. Usage: /run <what you want done>\nSee /help for options."

    directory = options.pop("directory", None) or ctx.default_directory
    if not directory:
        return ("No directory for this chat. Add one with dir:<path>, or set "
                "DEFAULT_DIRECTORY (or a per-chat mapping) in .env.")

    payload: dict[str, Any] = {
        "prompt": prompt,
        "directory": directory,
        "source": ctx.channel,
        "source_user_id": ctx.user_id,
        "source_username": ctx.username,
        "source_chat_id": ctx.chat_id,
        "source_thread_id": ctx.thread_id,
        "source_message_id": ctx.message_id,
        "created_by": ctx.username,
    }
    for key in ("model", "permission_mode", "title"):
        if key in options:
            payload[key] = options[key]
    for key in ("priority", "timeout_minutes", "max_attempts"):
        if key in options:
            try:
                payload[key] = int(options[key])
            except ValueError:
                return f"{key} must be a number, got {options[key]!r}"

    job = client.create_job(**payload)
    return (f"⏳ Job #{job['id']} queued\n"
            f"{job.get('title')}\n"
            f"in {job['directory']}\n\n"
            f"Track it with /status {job['id']}")


def cmd_ask_sales_bot(client: AdminClient, ctx: ChatContext, args: str) -> str:
    """Queue a read-only question about the Sales Bot product.

    Unlike /run this takes no options: the question is the whole argument, and
    the two directories it reads are configuration rather than something a chat
    message gets to choose.
    """
    question = args.strip()
    if not question:
        return ("Ask me something about Sales Bot.\n"
                "Usage: /ask-sales-bot <question>\n"
                "example: /ask-sales-bot how does a rep get scored on a call?")

    settings = get_settings()
    code_dir, docs_dir, directory = settings.require_sales_bot()
    payload = ask_sales_bot.build_job(
        question,
        code_dir=code_dir, docs_dir=docs_dir, directory=directory,
        asked_by=ctx.username,
        timeout_minutes=settings.sales_bot_timeout_minutes,
    )
    payload.update({
        "source": ctx.channel,
        "source_user_id": ctx.user_id,
        "source_username": ctx.username,
        "source_chat_id": ctx.chat_id,
        "source_thread_id": ctx.thread_id,
        "source_message_id": ctx.message_id,
        "created_by": ctx.username,
    })

    job = client.create_job(**payload)
    return (f"🔎 Looking into that — job #{job['id']}.\n"
            f"Reading {code_dir} and {docs_dir}; the answer comes back here when "
            f"it's ready.\n\n"
            f"Track it with /status {job['id']}")


def cmd_jobs(client: AdminClient, ctx: ChatContext, args: str) -> str:
    status, rest = _split(args)
    limit = 10
    if rest.strip().isdigit():
        limit = min(int(rest.strip()), 25)
    elif status.isdigit():
        limit, status = min(int(status), 25), ""

    jobs = client.list_jobs(status=status or None, limit=limit)
    if not jobs:
        return "No jobs match." if status else "The queue is empty."
    header = f"{len(jobs)} job(s)" + (f" with status {status}" if status else "")
    return header + ":\n" + "\n".join(models.job_line(job) for job in jobs)


def cmd_status(client: AdminClient, ctx: ChatContext, args: str) -> str:
    job_id = _job_id(_split(args)[0] or "")
    job = client.get_job(job_id)
    question = job.get("open_question")
    return models.job_detail(job, questions=[question] if question else [])


def cmd_cancel(client: AdminClient, ctx: ChatContext, args: str) -> str:
    token, reason = _split(args)
    job_id = _job_id(token)
    job = client.cancel_job(job_id, reason=reason or f"cancelled by {ctx.username}",
                            actor=ctx.username)
    if job["status"] == models.CANCELLED:
        return f"🚫 Job #{job_id} cancelled before it started."
    return (f"🚫 Cancel requested for job #{job_id}. Its terminal is told to stop at the "
            f"next heartbeat; you'll get a message when it does.")


def cmd_retry(client: AdminClient, ctx: ChatContext, args: str) -> str:
    job_id = _job_id(_split(args)[0] or "")
    job = client.retry_job(job_id, actor=ctx.username)
    return f"⏳ Job #{job_id} is back in the queue (attempt {job['attempts'] + 1} next)."


def cmd_edit(client: AdminClient, ctx: ChatContext, args: str) -> str:
    token, rest = _split(args)
    job_id = _job_id(token)
    options, leftover = _pop_options(rest, {k: k for k in EDIT_FIELDS}, ASSIGNMENT_RE)
    if not options:
        return ("Usage: /edit <id> key=value ...\n"
                "keys: prompt directory title model permission_mode priority "
                "timeout_minutes max_attempts")
    if leftover:
        return f"Don't know what to do with: {leftover!r}. Use key=value pairs."

    payload: dict[str, Any] = {}
    aliases = {"mode": "permission_mode", "prio": "priority",
               "timeout": "timeout_minutes", "attempts": "max_attempts"}
    for key, value in options.items():
        field = aliases.get(key, key)
        if EDIT_FIELDS[key] is int:
            try:
                payload[field] = int(value)
            except ValueError:
                return f"{key} must be a number, got {value!r}"
        else:
            payload[field] = value

    job = client.update_job(job_id, **payload)
    return (f"✏️ Job #{job_id} updated: {', '.join(sorted(payload))}\n"
            f"{models.job_line(job)}")


def cmd_reply(client: AdminClient, ctx: ChatContext, args: str) -> str:
    token, rest = _split(args)
    job_id: int | None = None
    body = args.strip()
    if token.lstrip("#").isdigit():
        job_id, body = int(token.lstrip("#")), rest.strip()
    if not body:
        return "Usage: /reply <job id> <your answer>"

    result = client.route_answer(channel=ctx.channel, chat_id=ctx.chat_id,
                                 user_id=ctx.user_id, username=ctx.username,
                                 body=body, job_id=job_id,
                                 reply_to_message_id=ctx.reply_to_message_id,
                                 thread_id=ctx.thread_id)
    return f"✅ Sent to job #{result['job_id']}. It has picked up where it left off."


def cmd_log(client: AdminClient, ctx: ChatContext, args: str) -> str:
    token, rest = _split(args)
    job_id = _job_id(token)
    tail = int(rest) if rest.strip().isdigit() else 25
    result = client.job_log(job_id, tail=min(tail, 100))
    if not result["lines"]:
        return (f"No log for job #{job_id} yet ({result.get('detail', '')}).\n"
                f"Expected at: {result['path']}")
    body = "\n".join(result["lines"])
    return f"Last {len(result['lines'])} line(s) of job #{job_id}:\n{body}"


def cmd_messages(client: AdminClient, ctx: ChatContext, args: str) -> str:
    job_id = _job_id(_split(args)[0] or "")
    messages = client.messages(job_id, limit=15)
    if not messages:
        return f"Job #{job_id} has no messages yet."
    icons = {"question": "❓", "answer": "💬", "note": "📝"}
    lines = [f"{icons.get(m['kind'], '•')} [{m['kind']}] {models.short(m['body'], 160)}"
             for m in reversed(messages)]
    return f"Job #{job_id} conversation:\n" + "\n".join(lines)


def cmd_events(client: AdminClient, ctx: ChatContext, args: str) -> str:
    job_id = _job_id(_split(args)[0] or "")
    events = client.events(job_id, limit=15)
    if not events:
        return f"Job #{job_id} has no events."
    lines = [f"• {e['kind']}"
             + (f" ({e['from_status']} → {e['to_status']})" if e.get("to_status") else "")
             + (f": {models.short(e['detail'], 100)}" if e.get("detail") else "")
             for e in reversed(events)]
    return f"Job #{job_id} history:\n" + "\n".join(lines)


def cmd_stats(client: AdminClient, ctx: ChatContext, args: str) -> str:
    stats = client.stats()
    counts = "  ".join(f"{models.icon(status)}{status}={count}"
                       for status, count in stats["jobs_by_status"].items() if count)
    workers = ", ".join(f"{worker}: {jobs}" for worker, jobs in stats["active_workers"].items())
    return (f"Queue: {counts or 'empty'}\n"
            f"Open questions: {stats['open_questions']}\n"
            f"Undelivered messages: {stats['pending_outbound']}\n"
            f"Slots per worker: {stats['max_concurrent_jobs']}\n"
            f"Busy workers: {workers or 'none'}")


def cmd_health(client: AdminClient, ctx: ChatContext, args: str) -> str:
    health = client.health()
    return (f"API: {'ok' if health['ok'] else 'degraded'}\n"
            f"Database: {health['database']}\n"
            f"claude on PATH: {'yes' if health['claude_on_path'] else 'NO — jobs will fail'}")


COMMANDS: dict[str, Callable[[AdminClient, ChatContext, str], str]] = {
    "help": cmd_help, "start": cmd_help,
    "run": cmd_run, "new": cmd_run,
    # Telegram's command entity stops at the first hyphen, so a typed
    # "/ask-sales-bot ..." arrives as "/ask"; parse_message recovers the full
    # name from the message text, and "ask" on its own is a usable shorthand.
    "ask_sales_bot": cmd_ask_sales_bot, "sales_bot": cmd_ask_sales_bot,
    "salesbot": cmd_ask_sales_bot, "ask": cmd_ask_sales_bot,
    "jobs": cmd_jobs, "list": cmd_jobs, "queue": cmd_jobs,
    "status": cmd_status, "job": cmd_status,
    "cancel": cmd_cancel, "stop": cmd_cancel,
    "retry": cmd_retry,
    "edit": cmd_edit,
    "reply": cmd_reply, "answer": cmd_reply,
    "log": cmd_log, "logs": cmd_log,
    "messages": cmd_messages,
    "events": cmd_events,
    "stats": cmd_stats,
    "health": cmd_health,
    "whoami": cmd_whoami,
}


def handle_command(command: str, args: str, ctx: ChatContext,
                   client: AdminClient) -> str:
    """Run one command and return the reply text. Never raises."""
    handler = COMMANDS.get(command.lower().lstrip("/"))
    if handler is None:
        return f"Unknown command /{command}. Try /help."
    try:
        return handler(client, ctx, args)
    except (ValueError, ConfigError) as exc:
        return f"⚠️ {exc}"
    except ApiError as exc:
        return _friendly(exc)
    except Exception as exc:  # a bot must never die because of one message
        log.exception("command /%s failed", command)
        return f"⚠️ Something went wrong running /{command}: {exc}"


def _friendly(exc: ApiError) -> str:
    if exc.is_connection_error:
        return ("⚠️ The job API isn't answering. It may be restarting — try again in a "
                "moment. If it persists, check that `claudejobs api` is running.")
    text = str(exc)
    detail = text.split(": ", 1)[1] if ": " in text else text
    if exc.status_code == 401:
        return "⚠️ The bot's API token was rejected. Check API_TOKEN in .env."
    if exc.status_code == 404:
        return f"⚠️ {detail}"
    if exc.status_code in {400, 403, 409}:
        return f"⚠️ {detail}"
    return f"⚠️ {text}"


def parse_message(text: str) -> tuple[str, str] | None:
    """Split an incoming chat message into (command, args).

    Accepts '/run ...' and plain 'run ...' so the same commands work in Slack,
    where a leading slash belongs to Slack's own command system, and reads a
    hyphen as an underscore so '/ask-sales-bot ...' finds ask_sales_bot.
    Returns None when the message isn't a command at all (it may be an answer to
    a question).
    """
    stripped = text.strip()
    if not stripped:
        return None
    first, _, rest = stripped.partition(" ")
    name = first.lstrip("/").lower()
    # Telegram sends "/run@botname" in groups.
    name = name.split("@", 1)[0]
    # Commands are written with hyphens (/ask-sales-bot) but named with
    # underscores, because that is all Telegram accepts in a command name.
    name = name.replace("-", "_")
    if name in COMMANDS:
        return name, rest.strip()
    return None
