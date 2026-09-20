"""Job status vocabulary and the human-readable rendering used everywhere.

Bots, the CLI and the API all format jobs through these helpers so a job looks
the same in Telegram, in Slack and on the terminal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

# --------------------------------------------------------------------------- #
# statuses
# --------------------------------------------------------------------------- #
QUEUED = "queued"
RUNNING = "running"
WAITING_INPUT = "waiting_input"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TIMED_OUT = "timed_out"

ALL_STATUSES = (QUEUED, RUNNING, WAITING_INPUT, SUCCEEDED, FAILED, CANCELLED, TIMED_OUT)

#: Jobs in these states are finished; nothing will move them again.
TERMINAL_STATUSES = frozenset({SUCCEEDED, FAILED, CANCELLED, TIMED_OUT})

#: Jobs in these states occupy a worker slot.
ACTIVE_STATUSES = frozenset({RUNNING, WAITING_INPUT})

#: Only queued jobs can still be edited by their poster.
EDITABLE_STATUSES = frozenset({QUEUED})

STATUS_ICONS = {
    QUEUED: "⏳",
    RUNNING: "▶️",
    WAITING_INPUT: "❓",
    SUCCEEDED: "✅",
    FAILED: "❌",
    CANCELLED: "🚫",
    TIMED_OUT: "⌛",
}

SOURCES = ("http", "cli", "telegram", "slack")

PERMISSION_MODES = (
    "bypassPermissions",
    "acceptEdits",
    "auto",
    "plan",
    "default",
)


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def icon(status: str) -> str:
    return STATUS_ICONS.get(status, "•")


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def _age(value: Any) -> str:
    """'3m ago' style relative time; tolerant of None and naive datetimes."""
    if not isinstance(value, datetime):
        return "—"
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - moment).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d ago"


def duration(start: Any, end: Any) -> str:
    if not isinstance(start, datetime):
        return "—"
    finish = end if isinstance(end, datetime) else datetime.now(timezone.utc)
    start_aware = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    finish_aware = finish if finish.tzinfo else finish.replace(tzinfo=timezone.utc)
    seconds = max(0, int((finish_aware - start_aware).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def short(text: Any, limit: int = 80) -> str:
    """One-line preview of a prompt or summary."""
    if text is None:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def job_line(job: Mapping[str, Any]) -> str:
    """One job on one line, for list views."""
    title = job.get("title") or short(job.get("prompt"), 60)
    return (f"{icon(job['status'])} #{job['id']} [{job['status']}] {title} "
            f"— {_age(job.get('created_at'))}")


def job_detail(job: Mapping[str, Any], *, questions: list[Mapping[str, Any]] | None = None) -> str:
    """Full job view, shared by /status in both bots and the CLI."""
    lines = [
        f"{icon(job['status'])} Job #{job['id']} — {job['status']}",
        f"title:     {job.get('title') or short(job.get('prompt'), 60)}",
        f"directory: {job.get('directory')}",
        f"model:     {job.get('model') or 'default'}   "
        f"permissions: {job.get('permission_mode') or 'default'}",
        f"priority:  {job.get('priority')}   attempt {job.get('attempts')}/{job.get('max_attempts')}",
        f"created:   {_age(job.get('created_at'))} by "
        f"{job.get('source_username') or job.get('created_by') or 'unknown'}"
        f" via {job.get('source')}",
    ]
    if job.get("started_at"):
        lines.append(f"started:   {_age(job.get('started_at'))}"
                     f" (ran {duration(job.get('started_at'), job.get('finished_at'))})")
    if job.get("worker_id"):
        lines.append(f"worker:    {job.get('worker_id')}")
    if job.get("session_id"):
        lines.append(f"session:   {job.get('session_id')}  (resume: claude --resume {job.get('session_id')})")
    if job.get("last_heartbeat_at"):
        lines.append(f"heartbeat: {_age(job.get('last_heartbeat_at'))}")
    if job.get("cancel_requested"):
        lines.append(f"cancel:    requested — {job.get('cancel_reason') or 'no reason given'}")
    if job.get("result_summary"):
        lines.append(f"result:    {job['result_summary']}")
    if job.get("error"):
        lines.append(f"error:     {job['error']}")
    if job.get("exit_code") is not None:
        lines.append(f"exit code: {job['exit_code']}")
    for question in questions or []:
        if question.get("status") == "open":
            lines.append(f"\n❓ waiting on you: {question['body']}"
                         f"\n   answer with: /reply {job['id']} <your answer>")
    return "\n".join(lines)
