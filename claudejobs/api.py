"""The job API.

Three groups of routes:

* **Admin** (``X-Auth-Token``) — everything a poster or bot does: create, list,
  inspect, edit, cancel, retry, answer questions, read logs.
* **Job-scoped** (``X-Job-Token``) — what a running session does through jobctl:
  heartbeat, progress, ask, finish. The token is minted when the job is claimed
  and only ever unlocks that one job.
* **Delivery** — the outbound queue the bots drain, plus reply routing.

Every request and response is appended to a markdown transcript (see
request_log.py); headers are never logged, so tokens stay out of it.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import db, models, request_log
from . import repository as repo
from .config import ConfigError, get_settings, setup_logging

log = logging.getLogger("claudejobs.api")

QUESTION_POLL_SECONDS = 1.5


# --------------------------------------------------------------------------- #
# request models
# --------------------------------------------------------------------------- #
class CreateJob(BaseModel):
    prompt: str = Field(min_length=1, description="The opening prompt for Claude")
    directory: str | None = Field(default=None,
                                  description="Where Claude runs; defaults to DEFAULT_DIRECTORY")
    title: str | None = Field(default=None, max_length=200)
    model: str | None = None
    permission_mode: str | None = None
    append_system_prompt: str | None = None
    priority: int = Field(default=100, ge=1, le=1000, description="Lower runs sooner")
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    timeout_minutes: int | None = Field(default=None, ge=1)
    scheduled_at: datetime | None = Field(default=None, description="Do not start before this time")
    source: Literal["http", "cli", "telegram", "slack"] = "http"
    source_user_id: str | None = None
    source_username: str | None = None
    source_chat_id: str | None = None
    source_thread_id: str | None = None
    source_message_id: str | None = None
    created_by: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("permission_mode")
    @classmethod
    def _known_mode(cls, value: str | None) -> str | None:
        if value and value not in models.PERMISSION_MODES:
            raise ValueError(f"must be one of: {', '.join(models.PERMISSION_MODES)}")
        return value


class UpdateJob(BaseModel):
    prompt: str | None = Field(default=None, min_length=1)
    directory: str | None = None
    title: str | None = Field(default=None, max_length=200)
    model: str | None = None
    permission_mode: str | None = None
    priority: int | None = Field(default=None, ge=1, le=1000)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    timeout_minutes: int | None = Field(default=None, ge=1)
    scheduled_at: datetime | None = None

    @field_validator("permission_mode")
    @classmethod
    def _known_mode(cls, value: str | None) -> str | None:
        if value and value not in models.PERMISSION_MODES:
            raise ValueError(f"must be one of: {', '.join(models.PERMISSION_MODES)}")
        return value


class CancelJob(BaseModel):
    reason: str = "cancelled by poster"
    actor: str | None = None


class RetryJob(BaseModel):
    actor: str | None = None


class AnswerJob(BaseModel):
    body: str = Field(min_length=1)
    answered_by: str | None = None
    question_id: int | None = None


class Progress(BaseModel):
    note: str = Field(min_length=1)


class Notify(BaseModel):
    message: str = Field(min_length=1)


class Ask(BaseModel):
    question: str = Field(min_length=1)


class Finish(BaseModel):
    status: Literal["succeeded", "failed", "cancelled"]
    summary: str | None = None
    error: str | None = None
    exit_code: int | None = None


class OutboundClaim(BaseModel):
    channel: Literal["telegram", "slack"]
    claimed_by: str
    limit: int = Field(default=10, ge=1, le=50)


class OutboundSent(BaseModel):
    provider_message_id: str | None = None
    provider_thread_id: str | None = None
    #: Where it actually landed, when that is not where it was addressed.
    chat_id: str | None = None


class OutboundFailed(BaseModel):
    error: str


class IncomingReply(BaseModel):
    channel: Literal["telegram", "slack"]
    chat_id: str
    user_id: str
    body: str = Field(min_length=1)
    username: str | None = None
    reply_to_message_id: str | None = None
    thread_id: str | None = None
    job_id: int | None = None


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging("claudejobs.api")
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        version = await asyncio.to_thread(db.ping)
        log.info("connected to %s", version.split(",")[0])
    except Exception as exc:
        # Don't die: the operator may be about to fix .env. Every DB route will
        # answer 503 with the same message until it works.
        log.error("database unavailable at startup: %s", exc)
    yield
    await asyncio.to_thread(db.close_pool)


app = FastAPI(
    title="claudejobs API",
    version="1.0.0",
    description="Queue, run and supervise Claude Code sessions.",
    lifespan=lifespan,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Append every call to the markdown transcript."""
    settings = get_settings()
    body = await request.body()
    try:
        response = await call_next(request)
    except Exception as exc:
        if settings.request_log_file:
            request_log.append_entry(
                settings.request_log_file, method=request.method,
                target=_target(request), status=500, request_body=body,
                response_body=f"{type(exc).__name__}: {exc}".encode(),
                max_mb=settings.request_log_max_mb,
            )
        raise

    payload = b"".join([chunk async for chunk in response.body_iterator])
    if settings.request_log_file:
        request_log.append_entry(
            settings.request_log_file, method=request.method, target=_target(request),
            status=response.status_code, request_body=body, response_body=payload,
            max_mb=settings.request_log_max_mb,
        )
    # The original iterator is consumed, so hand back a rebuilt response.
    return Response(content=payload, status_code=response.status_code,
                    headers=response.headers, media_type=response.media_type,
                    background=response.background)


def _target(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


@app.exception_handler(ConfigError)
async def _config_error(request: Request, exc: ConfigError):
    log.error("configuration error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def require_admin(x_auth_token: str | None = Header(default=None)) -> str:
    """Shared-secret auth for poster/bot routes. Fails closed."""
    configured = get_settings().api_token
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="API_TOKEN is not configured; refusing every request. Set it in .env.",
        )
    if not x_auth_token or not secrets.compare_digest(x_auth_token, configured):
        raise HTTPException(status_code=401, detail="bad or missing X-Auth-Token")
    return x_auth_token


def require_job(job_id: int, x_job_token: str | None = Header(default=None)) -> dict:
    """Per-job auth: the token minted when this job was claimed."""
    if not x_job_token:
        raise HTTPException(status_code=401, detail="missing X-Job-Token")
    with db.connection() as conn:
        job = repo.verify_job_token(conn, job_id, x_job_token)
    if job is None:
        raise HTTPException(status_code=401, detail="bad X-Job-Token for this job")
    return job


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _public(job: dict) -> dict:
    """Strip the token hash before a job row leaves the process."""
    return {k: v for k, v in job.items() if k != "job_token_hash"}


def _resolve_directory(raw: str | None) -> str:
    settings = get_settings()
    candidate = (raw or settings.default_directory).strip()
    if not candidate:
        raise HTTPException(
            status_code=400,
            detail="no directory given and DEFAULT_DIRECTORY is not set",
        )
    path = Path(candidate).expanduser()
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"bad directory {candidate!r}: {exc}") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {resolved}")
    roots = settings.allowed_roots
    if roots and not any(resolved == root or root in resolved.parents for root in roots):
        raise HTTPException(
            status_code=403,
            detail=f"{resolved} is outside ALLOWED_ROOTS ({', '.join(str(r) for r in roots)})",
        )
    return str(resolved)


def _load_job(conn, job_id: int) -> dict:
    job = repo.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job #{job_id}")
    return job


def _notify_poster(conn, job: dict, body: str, *, kind: str = "notice",
                   question_id: int | None = None) -> None:
    """Queue a chat message for the job's poster, if the job came from chat."""
    repo.enqueue_outbound(conn, job=job, body=body, kind=kind, question_id=question_id)


# --------------------------------------------------------------------------- #
# job routes (admin)
# --------------------------------------------------------------------------- #
@app.post("/jobs", status_code=201, dependencies=[Depends(require_admin)])
def create_job(payload: CreateJob) -> dict:
    settings = get_settings()
    directory = _resolve_directory(payload.directory)
    with db.connection() as conn:
        job = repo.create_job(
            conn,
            prompt=payload.prompt,
            directory=directory,
            title=payload.title or models.short(payload.prompt, 60),
            model=payload.model or settings.default_model or None,
            permission_mode=payload.permission_mode or settings.default_permission_mode,
            append_system_prompt=payload.append_system_prompt,
            payload=payload.payload,
            priority=payload.priority,
            max_attempts=payload.max_attempts or settings.default_max_attempts,
            timeout_minutes=payload.timeout_minutes,
            scheduled_at=payload.scheduled_at,
            source=payload.source,
            source_user_id=payload.source_user_id,
            source_username=payload.source_username,
            source_chat_id=payload.source_chat_id,
            source_thread_id=payload.source_thread_id,
            source_message_id=payload.source_message_id,
            created_by=payload.created_by or payload.source_username,
        )
    log.info("job #%s queued by %s (%s)", job["id"], job.get("created_by"), job["source"])
    return _public(job)


@app.get("/jobs", dependencies=[Depends(require_admin)])
def list_jobs(status: str | None = None, source: str | None = None,
              source_user_id: str | None = None, worker_id: str | None = None,
              limit: int = 20, offset: int = 0) -> list[dict]:
    """``status`` accepts one value, a comma-separated list, or 'active'/'open'."""
    statuses: list[str] | None = None
    if status:
        if status in {"active", "open"}:
            statuses = sorted(models.ACTIVE_STATUSES | {models.QUEUED})
        else:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            unknown = [s for s in statuses if s not in models.ALL_STATUSES]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown status {unknown}; valid: {', '.join(models.ALL_STATUSES)}",
                )
    with db.connection() as conn:
        rows = repo.list_jobs(conn, statuses=statuses, source=source,
                              source_user_id=source_user_id, worker_id=worker_id,
                              limit=min(limit, 100), offset=max(offset, 0))
    return [_public(row) for row in rows]


@app.get("/jobs/{job_id}", dependencies=[Depends(require_admin)])
def get_job(job_id: int) -> dict:
    with db.connection() as conn:
        job = _load_job(conn, job_id)
        question = repo.open_question(conn, job_id)
    result = _public(job)
    result["open_question"] = question
    return result


@app.patch("/jobs/{job_id}", dependencies=[Depends(require_admin)])
def update_job(job_id: int, payload: UpdateJob) -> dict:
    updates = payload.model_dump(exclude_none=True)
    if "directory" in updates:
        updates["directory"] = _resolve_directory(updates["directory"])
    with db.connection() as conn:
        job = _load_job(conn, job_id)
        if job["status"] not in models.EDITABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"job #{job_id} is {job['status']}; only queued jobs can be edited. "
                       "Cancel it and submit a new one.",
            )
        updated = repo.update_job(conn, job_id, updates, actor="api")
        if updated is None:
            raise HTTPException(status_code=409, detail=f"job #{job_id} started before the edit landed")
    return _public(updated)


@app.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_admin)])
def cancel_job(job_id: int, payload: CancelJob) -> dict:
    with db.connection() as conn:
        job = _load_job(conn, job_id)
        if models.is_terminal(job["status"]):
            raise HTTPException(status_code=409,
                                detail=f"job #{job_id} already finished ({job['status']})")
        updated = repo.request_cancel(conn, job_id, reason=payload.reason, actor=payload.actor)
    return _public(updated)


@app.post("/jobs/{job_id}/retry", dependencies=[Depends(require_admin)])
def retry_job(job_id: int, payload: RetryJob) -> dict:
    with db.connection() as conn:
        job = _load_job(conn, job_id)
        if not models.is_terminal(job["status"]):
            raise HTTPException(status_code=409,
                                detail=f"job #{job_id} is {job['status']}; only finished jobs can be retried")
        updated = repo.retry_job(conn, job_id, actor=payload.actor)
    return _public(updated)


@app.post("/jobs/{job_id}/answer", dependencies=[Depends(require_admin)])
def answer_job(job_id: int, payload: AnswerJob) -> dict:
    """Answer the question a job is waiting on."""
    with db.connection() as conn:
        _load_job(conn, job_id)
        question = (repo.get_question(conn, payload.question_id) if payload.question_id
                    else repo.open_question(conn, job_id))
        if question is None or question["job_id"] != job_id:
            raise HTTPException(status_code=404, detail=f"job #{job_id} is not waiting for an answer")
        if question["status"] != "open":
            raise HTTPException(status_code=409,
                                detail=f"that question is already {question['status']}")
        answered = repo.answer_question(conn, question["id"], body=payload.body,
                                        answered_by=payload.answered_by)
        if answered is None:
            raise HTTPException(status_code=409, detail="the question closed while you were answering")
    return {"job_id": job_id, "question_id": answered["id"], "status": "answered"}


@app.get("/jobs/{job_id}/messages", dependencies=[Depends(require_admin)])
def job_messages(job_id: int, limit: int = 20) -> list[dict]:
    with db.connection() as conn:
        _load_job(conn, job_id)
        return repo.list_messages(conn, job_id, limit=min(limit, 200))


@app.get("/jobs/{job_id}/events", dependencies=[Depends(require_admin)])
def job_events(job_id: int, limit: int = 20) -> list[dict]:
    with db.connection() as conn:
        _load_job(conn, job_id)
        return repo.list_events(conn, job_id, limit=min(limit, 200))


@app.get("/jobs/{job_id}/log", dependencies=[Depends(require_admin)])
def job_log(job_id: int, tail: int = 40) -> dict:
    """Last lines of the worker log for a job."""
    with db.connection() as conn:
        job = _load_job(conn, job_id)
    path = Path(job["log_path"]) if job.get("log_path") else get_settings().job_log_path(job_id)
    if not path.exists():
        return {"job_id": job_id, "path": str(path), "lines": [],
                "detail": "no log file yet"}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read {path}: {exc}") from exc
    return {"job_id": job_id, "path": str(path), "lines": lines[-min(tail, 500):]}


# --------------------------------------------------------------------------- #
# job-scoped routes (jobctl / run_job)
# --------------------------------------------------------------------------- #
@app.get("/jobs/{job_id}/self")
def describe_self(job: dict = Depends(require_job)) -> dict:
    """How the worker learns what to launch."""
    return _public(job)


@app.post("/jobs/{job_id}/heartbeat")
def job_heartbeat(job: dict = Depends(require_job)) -> dict:
    settings = get_settings()
    with db.connection() as conn:
        updated = repo.heartbeat(conn, job["id"], settings.job_lease_seconds)
    if updated is None:
        # The job was finished or reaped while the worker was running.
        return {"job_id": job["id"], "status": job["status"], "cancel_requested": True,
                "detail": "job is no longer active; stop work"}
    return {"job_id": updated["id"], "status": updated["status"],
            "cancel_requested": updated["cancel_requested"],
            "cancel_reason": updated["cancel_reason"]}


@app.post("/jobs/{job_id}/progress")
def job_progress(payload: Progress, job: dict = Depends(require_job)) -> dict:
    with db.connection() as conn:
        note = repo.add_note(conn, job["id"], payload.note, author="claude")
    log.info("job #%s progress: %s", job["id"], models.short(payload.note, 120))
    return {"job_id": job["id"], "note_id": note["id"]}


@app.post("/jobs/{job_id}/notify")
def job_notify(payload: Notify, job: dict = Depends(require_job)) -> dict:
    """Send the poster a message without waiting for a reply."""
    with db.connection() as conn:
        repo.add_note(conn, job["id"], payload.message, author="claude")
        queued = repo.enqueue_outbound(
            conn, job=job, kind="notify",
            body=f"💬 Job #{job['id']}: {payload.message}",
        )
    return {"job_id": job["id"], "delivered": queued is not None,
            "detail": None if queued else "this job has no chat channel; message recorded only"}


@app.post("/jobs/{job_id}/ask")
def job_ask(payload: Ask, job: dict = Depends(require_job)) -> dict:
    """Ask the poster a question and park the job until they answer."""
    with db.connection() as conn:
        fresh = _load_job(conn, job["id"])
        if models.is_terminal(fresh["status"]):
            raise HTTPException(status_code=409,
                                detail=f"job is {fresh['status']}; cannot ask a question")
        question = repo.ask_question(conn, job["id"], payload.question)
        if question["body"] == payload.question:
            body = (f"❓ Job #{job['id']} needs input\n"
                    f"{job.get('title') or models.short(job.get('prompt'), 60)}\n\n"
                    f"{payload.question}\n\n"
                    f"Reply to this message, or send: /reply {job['id']} <your answer>")
            queued = repo.enqueue_outbound(conn, job=fresh, body=body, kind="question",
                                           question_id=question["id"])
        else:
            queued = None  # an earlier question is still open; reuse it
    return {"job_id": job["id"], "question_id": question["id"],
            "status": question["status"],
            "delivered": queued is not None,
            "reused_open_question": question["body"] != payload.question}


@app.get("/jobs/{job_id}/questions/{question_id}")
async def poll_question(job_id: int, question_id: int, wait: int = 25,
                        job: dict = Depends(require_job)) -> dict:
    """Long-poll a question: returns as soon as it is answered, or when ``wait`` runs out.

    jobctl calls this in a loop, so a slow human costs nothing but idle polls.
    """
    deadline = asyncio.get_event_loop().time() + max(0, min(wait, 60))
    while True:
        state = await asyncio.to_thread(_question_state, job_id, question_id)
        if state["status"] != "open" or state.get("cancel_requested"):
            return state
        if asyncio.get_event_loop().time() >= deadline:
            state["timed_out_waiting"] = True
            return state
        await asyncio.sleep(QUESTION_POLL_SECONDS)


def _question_state(job_id: int, question_id: int) -> dict:
    with db.connection() as conn:
        question = repo.get_question(conn, question_id)
        if question is None or question["job_id"] != job_id:
            raise HTTPException(status_code=404, detail="no such question for this job")
        answer = None
        if question["status"] == "answered":
            rows = conn.execute(
                """
                SELECT body, author, created_at FROM job_messages
                WHERE parent_id = %s AND kind = 'answer' ORDER BY id DESC LIMIT 1
                """,
                (question_id,),
            ).fetchone()
            answer = rows["body"] if rows else ""
        job = repo.get_job(conn, job_id)
    return {
        "job_id": job_id,
        "question_id": question_id,
        "status": question["status"],
        "answer": answer,
        "answered_by": question.get("answered_by"),
        "cancel_requested": bool(job and job["cancel_requested"]),
        "job_status": job["status"] if job else None,
    }


@app.post("/jobs/{job_id}/finish")
def job_finish(payload: Finish, job: dict = Depends(require_job)) -> dict:
    with db.connection() as conn:
        finished = repo.finish_job(
            conn, job["id"], status=payload.status, summary=payload.summary,
            error=payload.error, exit_code=payload.exit_code, actor="claude",
        )
        if finished:
            _notify_poster(conn, finished, _finish_message(finished), kind="finished")
    log.info("job #%s finished: %s", job["id"], finished["status"] if finished else "unknown")
    return _public(finished) if finished else {"job_id": job["id"], "status": "unknown"}


def _finish_message(job: dict) -> str:
    detail = job.get("result_summary") or job.get("error") or "no summary given"
    return (f"{models.icon(job['status'])} Job #{job['id']} {job['status']}\n"
            f"{job.get('title') or ''}\n\n{detail}\n\n"
            f"Details: /status {job['id']}")


# --------------------------------------------------------------------------- #
# delivery + routing (bots)
# --------------------------------------------------------------------------- #
@app.post("/outbound/claim", dependencies=[Depends(require_admin)])
def outbound_claim(payload: OutboundClaim) -> list[dict]:
    with db.connection() as conn:
        repo.requeue_stale_outbound(conn)
        return repo.claim_outbound(conn, channel=payload.channel,
                                   claimed_by=payload.claimed_by, limit=payload.limit)


@app.post("/outbound/{outbound_id}/sent", dependencies=[Depends(require_admin)])
def outbound_sent(outbound_id: int, payload: OutboundSent) -> dict:
    with db.connection() as conn:
        row = repo.mark_outbound_sent(conn, outbound_id,
                                      provider_message_id=payload.provider_message_id,
                                      provider_thread_id=payload.provider_thread_id,
                                      chat_id=payload.chat_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no outbound message #{outbound_id}")
    return row


@app.post("/outbound/{outbound_id}/failed", dependencies=[Depends(require_admin)])
def outbound_failed(outbound_id: int, payload: OutboundFailed) -> dict:
    with db.connection() as conn:
        row = repo.mark_outbound_failed(conn, outbound_id, error=payload.error)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no outbound message #{outbound_id}")
    return row


@app.post("/replies", dependencies=[Depends(require_admin)])
def route_reply(payload: IncomingReply) -> dict:
    """Work out which waiting job a human's reply belongs to, and answer it.

    Resolution order, most explicit first:
      1. an explicit job id (``/reply 42 yes``)
      2. the chat message the human replied to (Telegram reply-to, Slack thread)
      3. the thread the answer was typed in
      4. the person's only open question

    With several jobs waiting and no other signal, this refuses and says which
    ids are open rather than guessing.
    """
    with db.connection() as conn:
        question = None
        if payload.job_id is not None:
            job = repo.get_job(conn, payload.job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no job #{payload.job_id}")
            question = repo.open_question(conn, payload.job_id)
            if question is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"job #{payload.job_id} is not waiting for an answer (it is {job['status']})",
                )
        if question is None and payload.reply_to_message_id:
            question = repo.find_question_by_provider_message(
                conn, channel=payload.channel, chat_id=payload.chat_id,
                provider_message_id=payload.reply_to_message_id)
        if question is None and payload.thread_id:
            question = repo.find_question_by_thread(
                conn, channel=payload.channel, chat_id=payload.chat_id,
                thread_id=payload.thread_id)
        if question is None:
            waiting = repo.open_questions_for_user(
                conn, source=payload.channel, source_user_id=payload.user_id)
            if not waiting:
                raise HTTPException(
                    status_code=404,
                    detail="none of your jobs are waiting for an answer right now",
                )
            if len(waiting) > 1:
                ids = ", ".join(f"#{q['job_id']}" for q in waiting)
                raise HTTPException(
                    status_code=409,
                    detail=f"{len(waiting)} of your jobs are waiting ({ids}). "
                           f"Say which one: /reply <job id> <your answer>",
                )
            question = waiting[0]

        if question["status"] != "open":
            raise HTTPException(status_code=409,
                                detail=f"that question is already {question['status']}")

        answered = repo.answer_question(
            conn, question["id"], body=payload.body,
            answered_by=payload.username or payload.user_id)
        if answered is None:
            raise HTTPException(status_code=409, detail="the question closed while you were answering")
        job = repo.get_job(conn, question["job_id"])

    log.info("job #%s answered by %s", question["job_id"], payload.username or payload.user_id)
    return {"job_id": question["job_id"], "question_id": question["id"],
            "status": "answered", "job_status": job["status"] if job else None,
            "question": question["body"]}


# --------------------------------------------------------------------------- #
# ops
# --------------------------------------------------------------------------- #
@app.get("/stats", dependencies=[Depends(require_admin)])
def queue_stats() -> dict:
    settings = get_settings()
    with db.connection() as conn:
        payload = repo.stats(conn)
    payload["max_concurrent_jobs"] = settings.max_concurrent_jobs
    payload["worker_id"] = settings.worker_id
    return payload


@app.get("/health")
def health() -> dict:
    """Unauthenticated liveness probe — no queue contents are exposed."""
    settings = get_settings()
    result: dict[str, Any] = {
        "ok": True,
        "version": "1.0.0",
        "claude_on_path": bool(settings.claude_bin or shutil.which("claude")),
        "database": "unknown",
    }
    try:
        db.ping()
        result["database"] = "ok"
    except Exception as exc:
        result["ok"] = False
        result["database"] = f"error: {exc}"
    return result


def run() -> None:
    """Entry point for ``claudejobs api``."""
    import uvicorn

    settings = get_settings()
    setup_logging("claudejobs.api")
    if settings.api_host not in {"127.0.0.1", "localhost", "::1"}:
        log.warning(
            "API_HOST is %s — this API starts processes on this machine. "
            "Only bind a public interface behind a firewall or VPN.", settings.api_host,
        )
    uvicorn.run(app, host=settings.api_host, port=settings.api_port,
                log_level=settings.log_level.lower())
