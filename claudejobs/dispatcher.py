"""The dispatcher: claim queued jobs, launch a terminal for each, supervise them.

One tick does five things:

1. reap jobs whose worker stopped heartbeating (retry or fail them),
2. ask jobs that have run past their limit to stop,
3. fail jobs whose question to a human went unanswered for too long,
4. take back outbound messages a crashed bot left claimed,
5. claim up to (MAX_CONCURRENT_JOBS - running) jobs and launch them.

Run several dispatchers on different machines if you like: claims use
FOR UPDATE SKIP LOCKED, and each machine counts only its own running jobs.
"""

from __future__ import annotations

import logging
import platform
import signal
import time
import uuid
from types import FrameType

from . import db, models
from . import repository as repo
from .config import ConfigError, get_settings, setup_logging
from .launcher import LaunchError, cleanup_old_scripts, launch_job

log = logging.getLogger("claudejobs.dispatcher")

SCRIPT_CLEANUP_EVERY_TICKS = 720  # ~1 hour at the default 5s poll


class Dispatcher:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.worker_id = self.settings.worker_id
        self.host = platform.node()
        self._stop = False
        self._ticks = 0

    # ------------------------------------------------------------------ #
    def request_stop(self, signum: int, frame: FrameType | None) -> None:
        log.info("signal %s received; finishing this tick and stopping", signum)
        self._stop = True

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        log.info("dispatcher %s starting: up to %s concurrent job(s), polling every %ss",
                 self.worker_id, self.settings.max_concurrent_jobs,
                 self.settings.poll_interval_seconds)
        self._adopt_orphans()

        while not self._stop:
            try:
                self.tick()
            except ConfigError as exc:
                log.error("configuration problem: %s", exc)
                time.sleep(min(30, self.settings.poll_interval_seconds * 5))
                continue
            except Exception:
                # A bad tick must never take the dispatcher down; the next one
                # re-reads state from the database anyway.
                log.exception("unexpected error during tick")
                time.sleep(min(30, self.settings.poll_interval_seconds * 2))
                continue
            time.sleep(self.settings.poll_interval_seconds)

        log.info("dispatcher stopped. Jobs already running keep going in their own "
                 "terminals and will report back when they finish.")

    # ------------------------------------------------------------------ #
    def _adopt_orphans(self) -> None:
        """Report what this worker already had running when it started."""
        try:
            with db.connection() as conn:
                active = repo.list_jobs(conn, statuses=sorted(models.ACTIVE_STATUSES),
                                        worker_id=self.worker_id, limit=50)
        except Exception as exc:
            log.warning("could not read existing jobs: %s", exc)
            return
        if active:
            log.info("%s job(s) from a previous run are still active: %s",
                     len(active), ", ".join(f"#{job['id']}" for job in active))
            log.info("they keep their slot until they finish or their lease expires")

    def tick(self) -> None:
        self._ticks += 1
        if self._ticks % SCRIPT_CLEANUP_EVERY_TICKS == 1:
            cleanup_old_scripts()

        self._sweep()
        self._dispatch()

    # ------------------------------------------------------------------ #
    def _sweep(self) -> None:
        settings = self.settings
        with db.connection() as conn:
            for job in repo.expire_leases(conn):
                log.warning("job #%s lost its worker -> %s", job["id"], job["status"])
                if job["status"] == models.QUEUED:
                    text = (f"⚠️ Job #{job['id']} lost contact with its terminal and was "
                            f"put back in the queue (attempt {job['attempts']}/{job['max_attempts']}).")
                else:
                    text = (f"{models.icon(job['status'])} Job #{job['id']} {job['status']}: "
                            f"{job.get('error') or 'the worker stopped responding'}")
                repo.enqueue_outbound(conn, job=job, body=text, kind="lease_expired")

            for job in repo.mark_timeouts(conn, settings.job_timeout_minutes):
                limit = job["timeout_minutes"] or settings.job_timeout_minutes
                log.warning("job #%s passed its %s minute limit; asking it to stop",
                            job["id"], limit)
                repo.enqueue_outbound(
                    conn, job=job, kind="timeout",
                    body=f"⌛ Job #{job['id']} hit its {limit} minute limit and is being stopped.")

            for expired in repo.expire_questions(conn, settings.question_timeout_minutes):
                job = expired["job"]
                log.warning("job #%s failed: question unanswered", job["id"])
                repo.enqueue_outbound(
                    conn, job=job, kind="question_expired",
                    body=(f"⌛ Job #{job['id']} was waiting on your answer and gave up after "
                          f"{settings.question_timeout_minutes} minutes.\n"
                          f"Question was: {models.short(expired['question']['body'], 200)}\n"
                          f"Start it again with: /retry {job['id']}"))

            taken_back = repo.requeue_stale_outbound(conn)
            if taken_back:
                log.info("re-queued %s undelivered message(s)", taken_back)

    # ------------------------------------------------------------------ #
    def _dispatch(self) -> None:
        settings = self.settings
        with db.connection() as conn:
            active = repo.count_active(conn, self.worker_id)
            capacity = settings.max_concurrent_jobs - active
            if capacity <= 0:
                return
            jobs = repo.claim_jobs(conn, worker_id=self.worker_id, worker_host=self.host,
                                   limit=capacity, lease_seconds=settings.job_lease_seconds)

        for job in jobs:
            self._launch(job)

    def _launch(self, job: dict) -> None:
        settings = self.settings
        job_id = job["id"]
        token = repo.new_job_token()
        session_id = str(uuid.uuid4())
        log_path = settings.job_log_path(job_id)

        # Store the launch details first: the worker authenticates with this
        # token the moment it starts, so the row has to be ready before it runs.
        with db.connection() as conn:
            repo.attach_worker(conn, job_id, pid=None, session_id=session_id,
                               job_token=token, log_path=str(log_path))

        try:
            process = launch_job(job_id, directory=job["directory"], token=token,
                                 settings=settings, log_path=log_path)
        except (LaunchError, OSError) as exc:
            log.error("job #%s could not be launched: %s", job_id, exc)
            with db.connection() as conn:
                failed = repo.finish_job(conn, job_id, status=models.FAILED,
                                         error=f"could not launch: {exc}", actor="dispatcher")
                if failed:
                    repo.enqueue_outbound(conn, job=failed, kind="failed",
                                          body=f"❌ Job #{job_id} could not be started: {exc}")
            return

        with db.connection() as conn:
            repo.set_worker_pid(conn, job_id, process.pid)
            repo.add_event(conn, job_id, "launched", actor=self.worker_id,
                           detail=f"{settings.terminal_mode} terminal, pid {process.pid}")
            fresh = repo.get_job(conn, job_id)
            repo.enqueue_outbound(
                conn, job=fresh, kind="started",
                body=(f"▶️ Job #{job_id} started on {self.worker_id}\n"
                      f"{job.get('title') or models.short(job['prompt'], 60)}\n"
                      f"in {job['directory']}"))
        log.info("job #%s launched (pid %s, %s)", job_id, process.pid, settings.terminal_mode)


def run() -> None:
    """Entry point for ``claudejobs dispatcher``."""
    setup_logging("claudejobs.dispatcher")
    Dispatcher().run_forever()


if __name__ == "__main__":
    run()
