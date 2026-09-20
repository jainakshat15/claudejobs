"""jobctl — the CLI a running Claude session uses to talk to the queue.

Claude is told about these commands in its system prompt (see prompt.py):

    jobctl progress "refactored the client, tests next"
    jobctl ask "which branch should I target?"     # blocks, prints the answer
    jobctl notify "heads up: the migration will lock the table"
    jobctl done "upgraded the client and all tests pass"
    jobctl fail "the staging credentials are rejected"
    jobctl status

It authenticates with the job's own token, taken from the environment the
dispatcher set up, so it can only ever affect its own job.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from .client import ApiError, JobClient
from .config import get_settings

POLL_WINDOW_SECONDS = 25
RETRY_BACKOFF_SECONDS = 5
MAX_CONNECTION_RETRIES = 12


def _client() -> JobClient:
    try:
        job_id = int(os.environ["CLAUDEJOBS_JOB_ID"])
        token = os.environ["CLAUDEJOBS_JOB_TOKEN"]
    except (KeyError, ValueError):
        raise SystemExit(
            "jobctl only works inside a job started by the dispatcher "
            "(CLAUDEJOBS_JOB_ID / CLAUDEJOBS_JOB_TOKEN are not set)."
        )
    api_url = os.environ.get("CLAUDEJOBS_API_URL") or get_settings().api_base_url
    return JobClient(job_id, token, api_url)


def cmd_progress(client: JobClient, args) -> int:
    client.progress(args.note)
    print(f"progress noted for job #{client.job_id}")
    return 0


def cmd_notify(client: JobClient, args) -> int:
    result = client.notify(args.message)
    if result.get("delivered"):
        print("message sent to the job poster")
    else:
        print("no chat channel for this job; the message was recorded on the job only")
    return 0


def cmd_done(client: JobClient, args) -> int:
    client.finish(status="succeeded", summary=args.summary)
    print(f"job #{client.job_id} marked succeeded")
    return 0


def cmd_fail(client: JobClient, args) -> int:
    client.finish(status="failed", error=args.reason)
    print(f"job #{client.job_id} marked failed")
    return 0


def cmd_status(client: JobClient, args) -> int:
    from . import models

    job = client.describe()
    print(models.job_detail(job))
    return 0


def cmd_ask(client: JobClient, args) -> int:
    """Ask the poster and block until they answer.

    Exit codes: 0 answered (answer on stdout), 2 closed without an answer,
    3 timed out, 4 the API could not be reached.
    """
    settings = get_settings()
    limit_minutes = args.timeout or settings.question_timeout_minutes or 720
    deadline = time.time() + limit_minutes * 60

    try:
        asked = client.ask(args.question)
    except ApiError as exc:
        print(f"could not ask: {exc}", file=sys.stderr)
        return 4

    question_id = asked["question_id"]
    if asked.get("reused_open_question"):
        print("note: this job already had an unanswered question; waiting on that one",
              file=sys.stderr)
    if not asked.get("delivered"):
        print("warning: this job has no chat channel, so nobody will see the question",
              file=sys.stderr)

    failures = 0
    while time.time() < deadline:
        try:
            state = client.poll_answer(question_id, wait_seconds=POLL_WINDOW_SECONDS)
            failures = 0
        except ApiError as exc:
            failures += 1
            if failures >= MAX_CONNECTION_RETRIES:
                print(f"gave up waiting for an answer: {exc}", file=sys.stderr)
                return 4
            time.sleep(RETRY_BACKOFF_SECONDS)
            continue

        if state["status"] == "answered":
            print(state.get("answer") or "")
            return 0
        if state["status"] in {"expired", "cancelled"}:
            print(f"the question was {state['status']} without an answer", file=sys.stderr)
            return 2
        if state.get("cancel_requested"):
            print("this job has been cancelled; stop work and exit", file=sys.stderr)
            return 2

    print(f"no answer within {limit_minutes} minutes", file=sys.stderr)
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobctl",
        description="Report status and talk to the poster of the job you are running.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    progress = sub.add_parser("progress", help="post a short progress note")
    progress.add_argument("note")
    progress.set_defaults(func=cmd_progress)

    ask = sub.add_parser("ask", help="ask the poster a question and wait for the answer")
    ask.add_argument("question")
    ask.add_argument("--timeout", type=int, default=None,
                     help="minutes to wait (default: QUESTION_TIMEOUT_MINUTES)")
    ask.set_defaults(func=cmd_ask)

    notify = sub.add_parser("notify", help="message the poster without waiting")
    notify.add_argument("message")
    notify.set_defaults(func=cmd_notify)

    done = sub.add_parser("done", help="mark the job succeeded")
    done.add_argument("summary")
    done.set_defaults(func=cmd_done)

    fail = sub.add_parser("fail", help="mark the job failed")
    fail.add_argument("reason")
    fail.set_defaults(func=cmd_fail)

    status = sub.add_parser("status", help="show this job's current state")
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = _client()
    try:
        return args.func(client, args)
    except ApiError as exc:
        print(f"jobctl: {exc}", file=sys.stderr)
        return 4
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
