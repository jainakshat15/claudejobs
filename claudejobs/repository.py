"""Every SQL statement in the system.

Keeping the queries in one module means the concurrency rules live in one place:
claims use ``FOR UPDATE SKIP LOCKED`` so two dispatchers never take the same job,
and leases (heartbeat + expiry) decide when a job counts as dead.

All functions take an open connection as their first argument; the caller owns
the transaction (see ``db.connection``).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime
from typing import Any, Mapping, Sequence

from psycopg.types.json import Json

from . import models

log = logging.getLogger(__name__)

JOB_COLUMNS = "*"


# --------------------------------------------------------------------------- #
# job tokens
# --------------------------------------------------------------------------- #
def new_job_token() -> str:
    """Secret handed to one running job so it can call its own endpoints."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def add_event(conn, job_id: int, kind: str, *, detail: str | None = None,
              from_status: str | None = None, to_status: str | None = None,
              actor: str | None = None, meta: Mapping[str, Any] | None = None) -> None:
    conn.execute(
        """
        INSERT INTO job_events (job_id, kind, detail, from_status, to_status, actor, meta)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (job_id, kind, detail, from_status, to_status, actor, Json(dict(meta or {}))),
    )


def list_events(conn, job_id: int, limit: int = 50) -> list[dict]:
    return conn.execute(
        "SELECT * FROM job_events WHERE job_id = %s ORDER BY id DESC LIMIT %s",
        (job_id, limit),
    ).fetchall()


# --------------------------------------------------------------------------- #
# job creation and lookup
# --------------------------------------------------------------------------- #
def create_job(conn, *, prompt: str, directory: str, title: str | None = None,
               model: str | None = None, permission_mode: str | None = None,
               append_system_prompt: str | None = None,
               payload: Mapping[str, Any] | None = None,
               priority: int = 100, max_attempts: int = 1,
               timeout_minutes: int | None = None,
               scheduled_at: datetime | None = None,
               source: str = "http", source_user_id: str | None = None,
               source_username: str | None = None, source_chat_id: str | None = None,
               source_thread_id: str | None = None, source_message_id: str | None = None,
               created_by: str | None = None) -> dict:
    row = conn.execute(
        """
        INSERT INTO jobs (prompt, directory, title, model, permission_mode,
                          append_system_prompt, payload, priority, max_attempts,
                          timeout_minutes, scheduled_at, source, source_user_id,
                          source_username, source_chat_id, source_thread_id,
                          source_message_id, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                COALESCE(%s, now()), %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (prompt, directory, title, model, permission_mode, append_system_prompt,
         Json(dict(payload or {})), priority, max_attempts, timeout_minutes,
         scheduled_at, source, source_user_id, source_username, source_chat_id,
         source_thread_id, source_message_id, created_by),
    ).fetchone()
    add_event(conn, row["id"], "created", to_status=models.QUEUED,
              actor=created_by or source, detail=models.short(prompt, 200))
    return row


def get_job(conn, job_id: int) -> dict | None:
    return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def get_job_for_update(conn, job_id: int) -> dict | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,)
    ).fetchone()


def get_job_by_public_id(conn, public_id: str) -> dict | None:
    return conn.execute("SELECT * FROM jobs WHERE public_id = %s", (public_id,)).fetchone()


def list_jobs(conn, *, statuses: Sequence[str] | None = None, source: str | None = None,
              source_user_id: str | None = None, worker_id: str | None = None,
              limit: int = 20, offset: int = 0) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if statuses:
        clauses.append("status = ANY(%s)")
        params.append(list(statuses))
    if source:
        clauses.append("source = %s")
        params.append(source)
    if source_user_id:
        clauses.append("source_user_id = %s")
        params.append(source_user_id)
    if worker_id:
        clauses.append("worker_id = %s")
        params.append(worker_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    return conn.execute(
        f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT %s OFFSET %s", params
    ).fetchall()


def update_job(conn, job_id: int, updates: Mapping[str, Any], actor: str | None = None) -> dict | None:
    """Edit a queued job. Returns the updated row, or None if it is no longer queued."""
    allowed = {"prompt", "directory", "title", "model", "permission_mode",
               "append_system_prompt", "priority", "max_attempts", "timeout_minutes",
               "scheduled_at"}
    fields = {k: v for k, v in updates.items() if k in allowed and v is not None}
    if not fields:
        return get_job(conn, job_id)

    assignments = ", ".join(f"{name} = %s" for name in fields)
    params = list(fields.values()) + [job_id, list(models.EDITABLE_STATUSES)]
    row = conn.execute(
        f"UPDATE jobs SET {assignments} WHERE id = %s AND status = ANY(%s) RETURNING *",
        params,
    ).fetchone()
    if row:
        add_event(conn, job_id, "edited", actor=actor,
                  detail=", ".join(sorted(fields)), meta={"fields": sorted(fields)})
    return row


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def count_active(conn, worker_id: str | None = None) -> int:
    """Jobs currently occupying a slot (running or waiting on their poster)."""
    if worker_id:
        row = conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE status = ANY(%s) AND worker_id = %s",
            (list(models.ACTIVE_STATUSES), worker_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE status = ANY(%s)",
            (list(models.ACTIVE_STATUSES),),
        ).fetchone()
    return int(row["n"])


def claim_jobs(conn, *, worker_id: str, worker_host: str, limit: int,
               lease_seconds: int) -> list[dict]:
    """Atomically take up to ``limit`` queued jobs for this worker.

    SKIP LOCKED means a second dispatcher (or a second poll overlapping the
    first) picks different rows instead of blocking or double-running a job.
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        """
        WITH picked AS (
            SELECT id
            FROM jobs
            WHERE status = 'queued'
              AND cancel_requested = false
              AND scheduled_at <= now()
            ORDER BY priority ASC, scheduled_at ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT %s
        )
        UPDATE jobs j
        SET status            = 'running',
            worker_id         = %s,
            worker_host       = %s,
            attempts          = j.attempts + 1,
            started_at        = COALESCE(j.started_at, now()),
            last_heartbeat_at = now(),
            lease_expires_at  = now() + make_interval(secs => %s),
            error             = NULL
        FROM picked
        WHERE j.id = picked.id
        RETURNING j.*
        """,
        (limit, worker_id, worker_host, lease_seconds),
    ).fetchall()
    for row in rows:
        add_event(conn, row["id"], "claimed", from_status=models.QUEUED,
                  to_status=models.RUNNING, actor=worker_id,
                  detail=f"attempt {row['attempts']}/{row['max_attempts']}")
    return rows


def attach_worker(conn, job_id: int, *, pid: int | None, session_id: str,
                  job_token: str, log_path: str) -> None:
    """Record the launch details produced when the terminal was spawned."""
    conn.execute(
        """
        UPDATE jobs
        SET worker_pid = %s, session_id = %s, job_token_hash = %s, log_path = %s
        WHERE id = %s
        """,
        (pid, session_id, hash_token(job_token), log_path, job_id),
    )


def set_worker_pid(conn, job_id: int, pid: int | None) -> None:
    conn.execute("UPDATE jobs SET worker_pid = %s WHERE id = %s", (pid, job_id))


def verify_job_token(conn, job_id: int, token: str) -> dict | None:
    """Return the job if the token matches the hash stored at launch."""
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM jobs WHERE id = %s AND job_token_hash = %s",
        (job_id, hash_token(token)),
    ).fetchone()


def heartbeat(conn, job_id: int, lease_seconds: int) -> dict | None:
    """Extend the lease. Returns the job so the worker can see a cancel request."""
    return conn.execute(
        """
        UPDATE jobs
        SET last_heartbeat_at = now(),
            lease_expires_at  = now() + make_interval(secs => %s)
        WHERE id = %s AND status = ANY(%s)
        RETURNING *
        """,
        (lease_seconds, job_id, list(models.ACTIVE_STATUSES)),
    ).fetchone()


def finish_job(conn, job_id: int, *, status: str, summary: str | None = None,
               error: str | None = None, exit_code: int | None = None,
               actor: str | None = None) -> dict | None:
    """Move a job to a terminal state.

    The first terminal state wins: if Claude already reported 'succeeded' via
    jobctl, a later non-zero exit code from the wrapper is recorded but does not
    overwrite the outcome. A job cancelled because it ran out of time is
    recorded as timed_out.
    """
    current = get_job_for_update(conn, job_id)
    if current is None:
        return None

    if models.is_terminal(current["status"]):
        conn.execute(
            "UPDATE jobs SET exit_code = COALESCE(exit_code, %s) WHERE id = %s",
            (exit_code, job_id),
        )
        add_event(conn, job_id, "finish_ignored", actor=actor,
                  detail=f"already {current['status']}; reported {status}")
        return get_job(conn, job_id)

    final = status
    if status == models.CANCELLED and current.get("cancel_reason") == "timeout":
        final = models.TIMED_OUT

    row = conn.execute(
        """
        UPDATE jobs
        SET status = %s, result_summary = COALESCE(%s, result_summary),
            error = COALESCE(%s, error), exit_code = COALESCE(%s, exit_code),
            finished_at = now(), lease_expires_at = NULL
        WHERE id = %s
        RETURNING *
        """,
        (final, summary, error, exit_code, job_id),
    ).fetchone()
    cancel_open_questions(conn, job_id, reason="job finished")
    add_event(conn, job_id, "finished", from_status=current["status"], to_status=final,
              actor=actor, detail=summary or error, meta={"exit_code": exit_code})
    return row


def request_cancel(conn, job_id: int, *, reason: str, actor: str | None = None) -> dict | None:
    """Cancel a job: immediate if queued, cooperative if it is already running."""
    current = get_job_for_update(conn, job_id)
    if current is None or models.is_terminal(current["status"]):
        return current

    if current["status"] == models.QUEUED:
        row = conn.execute(
            """
            UPDATE jobs
            SET status = 'cancelled', cancel_requested = true, cancel_reason = %s,
                finished_at = now(), lease_expires_at = NULL
            WHERE id = %s
            RETURNING *
            """,
            (reason, job_id),
        ).fetchone()
        add_event(conn, job_id, "cancelled", from_status=models.QUEUED,
                  to_status=models.CANCELLED, actor=actor, detail=reason)
        return row

    row = conn.execute(
        "UPDATE jobs SET cancel_requested = true, cancel_reason = %s WHERE id = %s RETURNING *",
        (reason, job_id),
    ).fetchone()
    cancel_open_questions(conn, job_id, reason="job cancelled")
    add_event(conn, job_id, "cancel_requested", actor=actor, detail=reason)
    return row


def retry_job(conn, job_id: int, *, actor: str | None = None,
              extra_attempts: int = 1) -> dict | None:
    """Put a finished job back in the queue with a fresh attempt allowance."""
    current = get_job_for_update(conn, job_id)
    if current is None or not models.is_terminal(current["status"]):
        return None
    row = conn.execute(
        """
        UPDATE jobs
        SET status = 'queued', cancel_requested = false, cancel_reason = NULL,
            max_attempts = GREATEST(max_attempts, attempts + %s),
            queued_at = now(), scheduled_at = now(), started_at = NULL,
            finished_at = NULL, error = NULL, exit_code = NULL,
            worker_id = NULL, worker_host = NULL, worker_pid = NULL,
            session_id = NULL, job_token_hash = NULL,
            last_heartbeat_at = NULL, lease_expires_at = NULL
        WHERE id = %s
        RETURNING *
        """,
        (extra_attempts, job_id),
    ).fetchone()
    add_event(conn, job_id, "retry", from_status=current["status"],
              to_status=models.QUEUED, actor=actor)
    return row


# --------------------------------------------------------------------------- #
# sweeps — called by the dispatcher on every tick
# --------------------------------------------------------------------------- #
def expire_leases(conn) -> list[dict]:
    """Jobs whose worker stopped heartbeating: retry them or fail them."""
    dead = conn.execute(
        """
        SELECT * FROM jobs
        WHERE status = ANY(%s) AND lease_expires_at IS NOT NULL AND lease_expires_at < now()
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        """,
        (list(models.ACTIVE_STATUSES),),
    ).fetchall()

    results: list[dict] = []
    for job in dead:
        cancel_open_questions(conn, job["id"], reason="worker stopped responding")
        timed_out = job.get("cancel_reason") == "timeout"
        can_retry = job["attempts"] < job["max_attempts"] and not timed_out and not job["cancel_requested"]
        if can_retry:
            row = conn.execute(
                """
                UPDATE jobs
                SET status = 'queued', worker_id = NULL, worker_host = NULL,
                    worker_pid = NULL, job_token_hash = NULL, session_id = NULL,
                    lease_expires_at = NULL, scheduled_at = now(),
                    error = 'worker stopped responding; requeued'
                WHERE id = %s
                RETURNING *
                """,
                (job["id"],),
            ).fetchone()
            add_event(conn, job["id"], "lease_expired", from_status=job["status"],
                      to_status=models.QUEUED, actor="dispatcher",
                      detail="no heartbeat; requeued for another attempt")
        else:
            final = models.TIMED_OUT if timed_out else (
                models.CANCELLED if job["cancel_requested"] else models.FAILED)
            row = conn.execute(
                """
                UPDATE jobs
                SET status = %s, finished_at = now(), lease_expires_at = NULL,
                    error = COALESCE(error, %s)
                WHERE id = %s
                RETURNING *
                """,
                (final,
                 "worker stopped responding (terminal closed, machine slept, or Claude crashed)",
                 job["id"]),
            ).fetchone()
            add_event(conn, job["id"], "lease_expired", from_status=job["status"],
                      to_status=final, actor="dispatcher", detail="no heartbeat")
        results.append(row)
    return results


def mark_timeouts(conn, default_timeout_minutes: int) -> list[dict]:
    """Ask jobs that have run too long to stop. The worker sees this on its next
    heartbeat and kills Claude; if it never does, the lease sweep finalises it."""
    rows = conn.execute(
        """
        UPDATE jobs
        SET cancel_requested = true, cancel_reason = 'timeout'
        WHERE status = ANY(%s)
          AND cancel_requested = false
          AND started_at IS NOT NULL
          AND COALESCE(timeout_minutes, %s) > 0
          AND started_at < now() - make_interval(mins => COALESCE(timeout_minutes, %s))
        RETURNING *
        """,
        (list(models.ACTIVE_STATUSES), default_timeout_minutes, default_timeout_minutes),
    ).fetchall()
    for row in rows:
        add_event(conn, row["id"], "timeout", actor="dispatcher",
                  detail=f"exceeded {row['timeout_minutes'] or default_timeout_minutes} minutes")
    return rows


def expire_questions(conn, timeout_minutes: int) -> list[dict]:
    """Unanswered questions eventually fail the job so the slot comes back."""
    if timeout_minutes <= 0:
        return []
    questions = conn.execute(
        """
        SELECT * FROM job_messages
        WHERE kind = 'question' AND status = 'open'
          AND created_at < now() - make_interval(mins => %s)
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        """,
        (timeout_minutes,),
    ).fetchall()

    expired: list[dict] = []
    for question in questions:
        conn.execute(
            "UPDATE job_messages SET status = 'expired' WHERE id = %s", (question["id"],)
        )
        job = finish_job(
            conn, question["job_id"], status=models.FAILED,
            error=f"no answer within {timeout_minutes} minutes: {models.short(question['body'], 120)}",
            actor="dispatcher",
        )
        if job:
            expired.append({"question": question, "job": job})
    return expired


# --------------------------------------------------------------------------- #
# questions, answers and notes — the Claude <-> poster channel
# --------------------------------------------------------------------------- #
def ask_question(conn, job_id: int, body: str) -> dict:
    """Record a question from the running job and park the job on it."""
    existing = conn.execute(
        """
        SELECT * FROM job_messages
        WHERE job_id = %s AND kind = 'question' AND status = 'open'
        ORDER BY id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if existing:
        # One open question at a time keeps reply routing unambiguous.
        return existing

    question = conn.execute(
        """
        INSERT INTO job_messages (job_id, kind, body, author, status)
        VALUES (%s, 'question', %s, 'claude', 'open')
        RETURNING *
        """,
        (job_id, body),
    ).fetchone()
    conn.execute(
        "UPDATE jobs SET status = 'waiting_input' WHERE id = %s AND status = 'running'",
        (job_id,),
    )
    add_event(conn, job_id, "question_asked", to_status=models.WAITING_INPUT,
              actor="claude", detail=models.short(body, 200),
              meta={"question_id": question["id"]})
    return question


def get_question(conn, question_id: int) -> dict | None:
    return conn.execute(
        "SELECT * FROM job_messages WHERE id = %s AND kind = 'question'", (question_id,)
    ).fetchone()


def open_question(conn, job_id: int) -> dict | None:
    return conn.execute(
        """
        SELECT * FROM job_messages
        WHERE job_id = %s AND kind = 'question' AND status = 'open'
        ORDER BY id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()


def open_questions_for_user(conn, *, source: str, source_user_id: str) -> list[dict]:
    """Every question currently waiting on one person, newest first.

    This is what makes a bare reply work when somebody has exactly one job
    waiting — and what lets us ask them to disambiguate when they have several.
    """
    return conn.execute(
        """
        SELECT m.*, j.title, j.prompt, j.directory
        FROM job_messages m
        JOIN jobs j ON j.id = m.job_id
        WHERE m.kind = 'question' AND m.status = 'open'
          AND j.source = %s AND j.source_user_id = %s
        ORDER BY m.id DESC
        """,
        (source, source_user_id),
    ).fetchall()


def answer_question(conn, question_id: int, *, body: str, answered_by: str | None) -> dict | None:
    """Store the poster's answer and let the job carry on."""
    question = conn.execute(
        """
        UPDATE job_messages
        SET status = 'answered', answered_at = now(), answered_by = %s
        WHERE id = %s AND kind = 'question' AND status = 'open'
        RETURNING *
        """,
        (answered_by, question_id),
    ).fetchone()
    if question is None:
        return None

    conn.execute(
        """
        INSERT INTO job_messages (job_id, kind, body, author, parent_id)
        VALUES (%s, 'answer', %s, %s, %s)
        """,
        (question["job_id"], body, answered_by, question["id"]),
    )
    conn.execute(
        "UPDATE jobs SET status = 'running' WHERE id = %s AND status = 'waiting_input'",
        (question["job_id"],),
    )
    add_event(conn, question["job_id"], "question_answered", to_status=models.RUNNING,
              actor=answered_by, detail=models.short(body, 200),
              meta={"question_id": question["id"]})
    question["answer"] = body
    return question


def cancel_open_questions(conn, job_id: int, *, reason: str) -> None:
    conn.execute(
        """
        UPDATE job_messages SET status = 'cancelled'
        WHERE job_id = %s AND kind = 'question' AND status = 'open'
        """,
        (job_id,),
    )


def add_note(conn, job_id: int, body: str, author: str = "claude") -> dict:
    note = conn.execute(
        """
        INSERT INTO job_messages (job_id, kind, body, author)
        VALUES (%s, 'note', %s, %s)
        RETURNING *
        """,
        (job_id, body, author),
    ).fetchone()
    add_event(conn, job_id, "note", actor=author, detail=models.short(body, 200))
    return note


def list_messages(conn, job_id: int, limit: int = 50) -> list[dict]:
    return conn.execute(
        "SELECT * FROM job_messages WHERE job_id = %s ORDER BY id DESC LIMIT %s",
        (job_id, limit),
    ).fetchall()


# --------------------------------------------------------------------------- #
# outbound delivery queue
# --------------------------------------------------------------------------- #
def enqueue_outbound(conn, *, job: Mapping[str, Any], body: str, kind: str = "notice",
                     question_id: int | None = None) -> dict | None:
    """Queue a message for whichever bot owns this job's source channel.

    Jobs created over plain HTTP have nowhere to deliver to; those return None.
    """
    channel = job.get("source")
    chat_id = job.get("source_chat_id")
    if channel not in {"telegram", "slack"} or not chat_id:
        return None

    return conn.execute(
        """
        INSERT INTO outbound_messages
            (job_id, question_id, channel, chat_id, thread_id, reply_to_message_id, kind, body)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (job["id"], question_id, channel, str(chat_id), job.get("source_thread_id"),
         job.get("source_message_id"), kind, body),
    ).fetchone()


def claim_outbound(conn, *, channel: str, claimed_by: str, limit: int = 10) -> list[dict]:
    return conn.execute(
        """
        WITH picked AS (
            SELECT id FROM outbound_messages
            WHERE channel = %s AND status = 'pending'
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
        )
        UPDATE outbound_messages o
        SET status = 'claimed', claimed_by = %s, claimed_at = now(), attempts = o.attempts + 1
        FROM picked
        WHERE o.id = picked.id
        RETURNING o.*
        """,
        (channel, limit, claimed_by),
    ).fetchall()


def mark_outbound_sent(conn, outbound_id: int, *, provider_message_id: str | None,
                       provider_thread_id: str | None = None) -> dict | None:
    """Record delivery — the provider message id is what routes replies back."""
    return conn.execute(
        """
        UPDATE outbound_messages
        SET status = 'sent', sent_at = now(), provider_message_id = %s,
            provider_thread_id = %s, last_error = NULL
        WHERE id = %s
        RETURNING *
        """,
        (provider_message_id, provider_thread_id, outbound_id),
    ).fetchone()


def mark_outbound_failed(conn, outbound_id: int, *, error: str) -> dict | None:
    """Back to pending for another try, or dead after max_attempts."""
    return conn.execute(
        """
        UPDATE outbound_messages
        SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
            last_error = %s, claimed_by = NULL, claimed_at = NULL
        WHERE id = %s
        RETURNING *
        """,
        (error, outbound_id),
    ).fetchone()


def requeue_stale_outbound(conn, *, older_than_seconds: int = 300) -> int:
    """A bot that died mid-delivery leaves rows claimed; take them back."""
    rows = conn.execute(
        """
        UPDATE outbound_messages
        SET status = 'pending', claimed_by = NULL, claimed_at = NULL
        WHERE status = 'claimed' AND claimed_at < now() - make_interval(secs => %s)
        RETURNING id
        """,
        (older_than_seconds,),
    ).fetchall()
    return len(rows)


# --------------------------------------------------------------------------- #
# reply routing
# --------------------------------------------------------------------------- #
def find_question_by_provider_message(conn, *, channel: str, chat_id: str,
                                      provider_message_id: str) -> dict | None:
    """Map 'the message this reply points at' back to the question we sent."""
    row = conn.execute(
        """
        SELECT m.*
        FROM outbound_messages o
        JOIN job_messages m ON m.id = o.question_id
        WHERE o.channel = %s AND o.chat_id = %s AND o.provider_message_id = %s
          AND m.kind = 'question'
        ORDER BY o.id DESC
        LIMIT 1
        """,
        (channel, str(chat_id), str(provider_message_id)),
    ).fetchone()
    return row


def find_question_by_thread(conn, *, channel: str, chat_id: str, thread_id: str) -> dict | None:
    """Slack: an answer typed in the thread where the question was posted."""
    return conn.execute(
        """
        SELECT m.*
        FROM outbound_messages o
        JOIN job_messages m ON m.id = o.question_id
        WHERE o.channel = %s AND o.chat_id = %s
          AND COALESCE(o.provider_thread_id, o.provider_message_id) = %s
          AND m.kind = 'question' AND m.status = 'open'
        ORDER BY o.id DESC
        LIMIT 1
        """,
        (channel, str(chat_id), str(thread_id)),
    ).fetchone()


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #
def stats(conn) -> dict[str, Any]:
    by_status = {
        row["status"]: row["jobs"]
        for row in conn.execute("SELECT status, jobs FROM job_queue_stats").fetchall()
    }
    pending_outbound = conn.execute(
        "SELECT count(*) AS n FROM outbound_messages WHERE status IN ('pending', 'claimed')"
    ).fetchone()["n"]
    open_questions = conn.execute(
        "SELECT count(*) AS n FROM job_messages WHERE kind = 'question' AND status = 'open'"
    ).fetchone()["n"]
    workers = conn.execute(
        """
        SELECT worker_id, count(*) AS jobs
        FROM jobs WHERE status = ANY(%s) AND worker_id IS NOT NULL
        GROUP BY worker_id ORDER BY worker_id
        """,
        (list(models.ACTIVE_STATUSES),),
    ).fetchall()
    return {
        "jobs_by_status": {status: by_status.get(status, 0) for status in models.ALL_STATUSES},
        "open_questions": open_questions,
        "pending_outbound": pending_outbound,
        "active_workers": {row["worker_id"]: row["jobs"] for row in workers},
    }
