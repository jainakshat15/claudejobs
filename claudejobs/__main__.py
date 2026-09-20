"""claudejobs — one entry point for every service and operator task.

    claudejobs selfcheck        check config, database, Claude CLI, terminals
    claudejobs migrate up       create or update the schema
    claudejobs api              run the HTTP API
    claudejobs dispatcher       claim jobs and launch terminals
    claudejobs telegram         run the Telegram bot
    claudejobs slack            run the Slack bot
    claudejobs all              run everything configured, in one window

    claudejobs submit "prompt" --dir PATH      queue a job from the shell
    claudejobs jobs [--status active]          list jobs
    claudejobs status <id> | cancel <id> | retry <id> | answer <id> "text"
    claudejobs stats
    claudejobs secret           print a fresh random token for .env
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import time

from . import models
from .config import REPO_ROOT, ConfigError, get_settings, setup_logging


# --------------------------------------------------------------------------- #
# services
# --------------------------------------------------------------------------- #
def cmd_api(args) -> int:
    from .api import run
    run()
    return 0


def cmd_dispatcher(args) -> int:
    from .dispatcher import run
    run()
    return 0


def cmd_telegram(args) -> int:
    from .bots.telegram_bot import run
    run()
    return 0


def cmd_slack(args) -> int:
    from .bots.slack_bot import run
    run()
    return 0


def cmd_all(args) -> int:
    """Run every configured service as a child process, in one window."""
    settings = get_settings()
    services = ["api", "dispatcher"]
    if settings.telegram_bot_token:
        services.append("telegram")
    if settings.slack_bot_token and settings.slack_app_token:
        services.append("slack")

    print(f"starting: {', '.join(services)}   (Ctrl+C stops all of them)")
    children: list[tuple[str, subprocess.Popen]] = []
    for service in services:
        proc = subprocess.Popen([sys.executable, "-m", "claudejobs", service], cwd=REPO_ROOT)
        children.append((service, proc))
        time.sleep(1.5 if service == "api" else 0.3)  # let the API bind first

    exit_code = 0
    try:
        while True:
            for name, proc in children:
                code = proc.poll()
                if code is not None:
                    print(f"\n{name} exited with code {code}; stopping the rest")
                    exit_code = code or 1
                    raise KeyboardInterrupt
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for name, proc in children:
            if proc.poll() is None:
                proc.terminate()
        for name, proc in children:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return exit_code


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
def cmd_migrate(args) -> int:
    from .migrate import MigrationError, migrate_up, status

    setup_logging("claudejobs.migrate")
    try:
        if args.action == "up":
            applied = migrate_up(allow_drift=args.allow_drift)
            if applied:
                print(f"applied: {', '.join(applied)}")
            else:
                print("database is already up to date")
        else:
            for version, state in status():
                marker = "✓" if state == "applied" else ("!" if state != "pending" else " ")
                print(f" {marker} {version}: {state}")
    except MigrationError as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
# operator commands
# --------------------------------------------------------------------------- #
def _client():
    from .client import AdminClient
    return AdminClient()


def cmd_submit(args) -> int:
    from .client import ApiError

    try:
        with _client() as client:
            job = client.create_job(
                prompt=args.prompt, directory=args.dir, title=args.title,
                model=args.model, permission_mode=args.mode, priority=args.priority,
                timeout_minutes=args.timeout, source="cli",
                created_by=args.user or os.environ.get("USERNAME") or os.environ.get("USER"),
            )
    except ApiError as exc:
        print(f"could not queue the job: {exc}", file=sys.stderr)
        return 1
    print(f"queued job #{job['id']} in {job['directory']}")
    return 0


def cmd_jobs(args) -> int:
    from .client import ApiError

    try:
        with _client() as client:
            jobs = client.list_jobs(status=args.status, limit=args.limit)
    except ApiError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    if not jobs:
        print("no jobs")
        return 0
    for job in jobs:
        print(models.job_line(job))
    return 0


def cmd_status(args) -> int:
    from .client import ApiError

    try:
        with _client() as client:
            job = client.get_job(args.job_id)
            question = job.get("open_question")
            print(models.job_detail(job, questions=[question] if question else []))
    except ApiError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    return 0


def cmd_cancel(args) -> int:
    from .client import ApiError

    try:
        with _client() as client:
            job = client.cancel_job(args.job_id, reason=args.reason, actor="cli")
    except ApiError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print(f"job #{args.job_id} is now {job['status']}"
          + ("" if models.is_terminal(job["status"]) else " (cancel requested)"))
    return 0


def cmd_retry(args) -> int:
    from .client import ApiError

    try:
        with _client() as client:
            client.retry_job(args.job_id, actor="cli")
    except ApiError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print(f"job #{args.job_id} re-queued")
    return 0


def cmd_answer(args) -> int:
    from .client import ApiError

    try:
        with _client() as client:
            client.answer(args.job_id, body=args.text, answered_by="cli")
    except ApiError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print(f"answer delivered to job #{args.job_id}")
    return 0


def cmd_stats(args) -> int:
    from .client import ApiError

    try:
        with _client() as client:
            stats = client.stats()
    except ApiError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    for status, count in stats["jobs_by_status"].items():
        if count:
            print(f"  {models.icon(status)} {status:<14} {count}")
    print(f"  open questions: {stats['open_questions']}")
    print(f"  undelivered messages: {stats['pending_outbound']}")
    print(f"  slots per worker: {stats['max_concurrent_jobs']}")
    for worker, jobs in stats["active_workers"].items():
        print(f"  worker {worker}: {jobs} active")
    return 0


def cmd_secret(args) -> int:
    print(secrets.token_urlsafe(32))
    return 0


# --------------------------------------------------------------------------- #
# selfcheck
# --------------------------------------------------------------------------- #
def cmd_selfcheck(args) -> int:
    """Check everything a working install needs, and say how to fix what's missing."""
    problems = 0

    def report(ok: bool, label: str, detail: str = "", *, fatal: bool = True) -> None:
        nonlocal problems
        mark = "PASS" if ok else ("FAIL" if fatal else "WARN")
        print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
        if not ok and fatal:
            problems += 1

    env_file = REPO_ROOT / ".env"
    report(env_file.exists(), ".env present", str(env_file) if env_file.exists()
           else "copy .env.example to .env and fill it in")

    try:
        settings = get_settings()
    except ConfigError as exc:
        report(False, "settings load", str(exc))
        return 1
    report(True, "settings load", f"worker {settings.worker_id}, "
                                  f"{settings.max_concurrent_jobs} slot(s)")

    try:
        settings.require_api_token()
        report(True, "API_TOKEN set")
    except ConfigError as exc:
        report(False, "API_TOKEN set", str(exc).splitlines()[0])

    # database + schema
    try:
        settings.require_database_url()
        from . import db
        version = db.ping().split(" on ")[0]
        report(True, "database reachable", version)
        try:
            from .migrate import status as migration_status
            pending = [v for v, state in migration_status() if state != "applied"]
            report(not pending, "schema up to date",
                   "run: claudejobs migrate up" if pending else "all migrations applied")
        except Exception as exc:
            report(False, "schema up to date", str(exc))
    except Exception as exc:
        report(False, "database reachable", str(exc).splitlines()[0])

    # claude CLI — resolved the same way a job will resolve it
    from .claude_cli import ClaudeNotFound, resolve_claude_command

    try:
        command, single_line = resolve_claude_command(settings.claude_bin)
        report(True, "claude CLI found", " ".join(command))
        if single_line:
            report(False, "claude CLI is a native executable",
                   "found a .cmd/.bat shim, which cannot receive multi-line arguments. "
                   "Jobs still run (prompts are passed as files), but installing the "
                   "native Claude Code executable is better.", fatal=False)
    except ClaudeNotFound as exc:
        report(False, "claude CLI found", str(exc))

    # terminal
    if settings.terminal_mode == "headless":
        report(True, "terminal", "headless mode; jobs run without a window")
    elif os.name == "nt":
        terminal = shutil.which("wt.exe") or shutil.which("cmd")
        report(bool(terminal), "terminal available", terminal or "no wt.exe or cmd found")
    else:
        from .launcher import LINUX_TERMINALS
        found = next((name for name, _ in LINUX_TERMINALS if shutil.which(name)), None)
        report(bool(found) or sys.platform == "darwin", "terminal available",
               found or "install gnome-terminal/konsole/xterm, or set TERMINAL_MODE=headless",
               fatal=False)

    # writable log directory
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.log_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report(True, "log directory writable", str(settings.log_dir))
    except OSError as exc:
        report(False, "log directory writable", str(exc))

    # API
    try:
        from .client import AdminClient, ApiError
        with AdminClient() as client:
            health = client.health()
        report(bool(health.get("ok")), "API responding",
               f"{settings.api_base_url} (database: {health.get('database')})")
    except Exception as exc:
        report(False, "API responding",
               f"{exc}" if not isinstance(exc, ApiError) else "start it with: claudejobs api",
               fatal=False)

    # bots
    report(bool(settings.telegram_bot_token), "telegram configured",
           f"{len(settings.telegram_allowed_users)} allowed user(s)"
           if settings.telegram_bot_token else "TELEGRAM_BOT_TOKEN empty (bot disabled)",
           fatal=False)
    report(bool(settings.slack_bot_token and settings.slack_app_token), "slack configured",
           f"{len(settings.slack_allowed_users)} allowed user(s)"
           if settings.slack_bot_token else "SLACK_BOT_TOKEN empty (bot disabled)",
           fatal=False)

    print()
    if problems:
        print(f"{problems} problem(s) to fix — see SETUP.md")
        return 1
    print("everything checks out")
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claudejobs",
        description="Queue and run Claude Code sessions from Telegram or Slack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("api", help="run the HTTP API").set_defaults(func=cmd_api)
    sub.add_parser("dispatcher", help="claim jobs and launch terminals").set_defaults(func=cmd_dispatcher)
    sub.add_parser("telegram", help="run the Telegram bot").set_defaults(func=cmd_telegram)
    sub.add_parser("slack", help="run the Slack bot").set_defaults(func=cmd_slack)
    sub.add_parser("all", help="run every configured service").set_defaults(func=cmd_all)
    sub.add_parser("selfcheck", help="check configuration and connectivity").set_defaults(func=cmd_selfcheck)
    sub.add_parser("secret", help="print a random token for .env").set_defaults(func=cmd_secret)

    migrate = sub.add_parser("migrate", help="create or update the database schema")
    migrate.add_argument("action", choices=["up", "status"], nargs="?", default="up")
    migrate.add_argument("--allow-drift", action="store_true",
                         help="accept edited migrations that were already applied")
    migrate.set_defaults(func=cmd_migrate)

    submit = sub.add_parser("submit", help="queue a job from the shell")
    submit.add_argument("prompt")
    submit.add_argument("--dir", help="working directory (default: DEFAULT_DIRECTORY)")
    submit.add_argument("--title")
    submit.add_argument("--model")
    submit.add_argument("--mode", help="permission mode")
    submit.add_argument("--priority", type=int, default=100)
    submit.add_argument("--timeout", type=int, help="minutes")
    submit.add_argument("--user", help="who to record as the creator")
    submit.set_defaults(func=cmd_submit)

    jobs = sub.add_parser("jobs", help="list jobs")
    jobs.add_argument("--status", help="queued, running, active, failed ...")
    jobs.add_argument("--limit", type=int, default=20)
    jobs.set_defaults(func=cmd_jobs)

    status_cmd = sub.add_parser("status", help="show one job")
    status_cmd.add_argument("job_id", type=int)
    status_cmd.set_defaults(func=cmd_status)

    cancel = sub.add_parser("cancel", help="cancel a job")
    cancel.add_argument("job_id", type=int)
    cancel.add_argument("--reason", default="cancelled from the CLI")
    cancel.set_defaults(func=cmd_cancel)

    retry = sub.add_parser("retry", help="requeue a finished job")
    retry.add_argument("job_id", type=int)
    retry.set_defaults(func=cmd_retry)

    answer = sub.add_parser("answer", help="answer a job that is waiting for input")
    answer.add_argument("job_id", type=int)
    answer.add_argument("text")
    answer.set_defaults(func=cmd_answer)

    sub.add_parser("stats", help="queue summary").set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"\nconfiguration problem:\n  {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
