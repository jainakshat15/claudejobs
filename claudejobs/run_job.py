"""The worker that runs inside each job's terminal.

Responsibilities, in order:

1. Ask the API what this job is (the job token only unlocks its own row).
2. Heartbeat on a background thread so the dispatcher can tell it is alive, and
   notice a cancel request coming back in the heartbeat reply.
3. Run Claude Code with the job's prompt and the queue instructions appended.
4. Report the outcome — without overwriting a result Claude already reported
   through jobctl.

Started by launcher.py with three environment variables: CLAUDEJOBS_JOB_ID,
CLAUDEJOBS_JOB_TOKEN and CLAUDEJOBS_API_URL.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .claude_cli import resolve_claude_command
from .client import ApiError, JobClient
from .config import get_settings
from .prompt import build_job_instructions

log = logging.getLogger("claudejobs.run_job")

GRACE_SECONDS = 15


def _log_to_file(path: Path) -> None:
    """Wrapper events go to the job log; Claude's own output stays on screen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("[claudejobs] %(message)s"))
    root.addHandler(console)


class Heartbeat(threading.Thread):
    """Keeps the lease alive and watches for a cancel request."""

    def __init__(self, client: JobClient, interval: int):
        super().__init__(name="heartbeat", daemon=True)
        self.client = client
        self.interval = max(5, interval)
        self.cancel_requested = threading.Event()
        self.cancel_reason = ""
        self._stop = threading.Event()
        self.consecutive_failures = 0

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                reply = self.client.heartbeat()
                self.consecutive_failures = 0
            except ApiError as exc:
                self.consecutive_failures += 1
                log.warning("heartbeat failed (%s in a row): %s",
                            self.consecutive_failures, exc)
                continue
            if reply.get("cancel_requested"):
                self.cancel_reason = reply.get("cancel_reason") or "cancelled"
                log.warning("cancel requested: %s", self.cancel_reason)
                self.cancel_requested.set()
                return

    def stop(self) -> None:
        self._stop.set()


def build_claude_argv(job: dict, *, settings, jobctl_command: str) -> list[str]:
    """Assemble the Claude Code command line for this job."""
    claude_command, single_line_args = resolve_claude_command(settings.claude_bin)

    instructions = build_job_instructions(
        job, jobctl=jobctl_command,
        question_timeout_minutes=settings.question_timeout_minutes,
        job_timeout_minutes=job.get("timeout_minutes") or settings.job_timeout_minutes,
    )
    if job.get("append_system_prompt"):
        instructions = (f"{instructions}\n\n## Extra instructions for this job\n\n"
                        f"{job['append_system_prompt']}")

    prompt = job["prompt"]
    if single_line_args:
        # A shell shim would truncate these at the first newline, so hand them
        # over as files and pass one-line pointers instead.
        instructions, prompt = _spill_to_files(job, settings, instructions, prompt)

    argv = [*claude_command]
    if job.get("permission_mode"):
        argv += ["--permission-mode", job["permission_mode"]]
    if job.get("model"):
        argv += ["--model", job["model"]]
    argv += ["--append-system-prompt", instructions]
    if job.get("session_id"):
        argv += ["--session-id", str(job["session_id"])]
    extra = job.get("payload", {}).get("extra_args")
    if isinstance(extra, list):
        argv += [str(arg) for arg in extra]
    argv.append(prompt)  # positional = opening prompt
    return argv


def _spill_to_files(job: dict, settings, instructions: str, prompt: str) -> tuple[str, str]:
    """Write the long text to files and return single-line replacements."""
    folder = settings.log_dir
    folder.mkdir(parents=True, exist_ok=True)

    instructions_path = folder / f"job-{job['id']}-instructions.md"
    instructions_path.write_text(instructions, encoding="utf-8")
    pointer = (f"Your operating instructions are in {instructions_path}. "
               "Read that file first and follow it exactly; it defines how you report "
               "status and how to reach the person who posted this job.")

    if "\n" in prompt:
        prompt_path = folder / f"job-{job['id']}-prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        prompt = (f"Your task is described in {prompt_path}. Read that file and carry out "
                  "what it asks.")
    log.info("passed the prompt and instructions as files (shell shim in use)")
    return pointer, prompt


def _kill(process: subprocess.Popen) -> None:
    """Stop Claude as politely as the platform allows, then insist."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            # Claude runs under node with children of its own; kill the tree.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, check=False)
        else:
            process.send_signal(signal.SIGTERM)
    except OSError as exc:
        log.warning("could not signal Claude: %s", exc)

    try:
        process.wait(timeout=GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        log.warning("Claude did not stop within %ss; killing", GRACE_SECONDS)
        try:
            process.kill()
        except OSError:
            pass


def main() -> int:
    settings = get_settings()

    try:
        job_id = int(os.environ["CLAUDEJOBS_JOB_ID"])
        token = os.environ["CLAUDEJOBS_JOB_TOKEN"]
    except (KeyError, ValueError):
        print("run_job must be started by the dispatcher "
              "(CLAUDEJOBS_JOB_ID and CLAUDEJOBS_JOB_TOKEN are required)", file=sys.stderr)
        return 2

    api_url = os.environ.get("CLAUDEJOBS_API_URL") or settings.api_base_url
    log_path = settings.job_log_path(job_id)
    _log_to_file(log_path)

    client = JobClient(job_id, token, api_url)
    try:
        job = client.describe()
    except ApiError as exc:
        log.error("cannot load job #%s: %s", job_id, exc)
        return 2

    log.info("job #%s starting in %s", job_id, job["directory"])
    jobctl_command = f'"{sys.executable}" -m claudejobs.jobctl'

    try:
        argv = build_claude_argv(job, settings=settings, jobctl_command=jobctl_command)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        _safe_finish(client, status="failed", error=str(exc))
        return 2

    heartbeat = Heartbeat(client, settings.heartbeat_interval_seconds)
    heartbeat.start()

    started = time.time()
    exit_code: int | None = None
    try:
        process = subprocess.Popen(argv, cwd=job["directory"])
    except OSError as exc:
        log.error("could not start Claude: %s", exc)
        heartbeat.stop()
        _safe_finish(client, status="failed", error=f"could not start Claude: {exc}")
        return 2

    log.info("claude pid %s", process.pid)
    try:
        while True:
            try:
                exit_code = process.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                if heartbeat.cancel_requested.is_set():
                    log.warning("stopping Claude: %s", heartbeat.cancel_reason)
                    _kill(process)
                    exit_code = process.poll()
                    break
    except KeyboardInterrupt:
        log.warning("interrupted at the terminal; stopping Claude")
        _kill(process)
        exit_code = process.poll()
    finally:
        heartbeat.stop()

    elapsed = int(time.time() - started)
    log.info("claude exited with %s after %ss", exit_code, elapsed)

    if heartbeat.cancel_requested.is_set():
        _safe_finish(client, status="cancelled", exit_code=exit_code,
                     error=f"cancelled: {heartbeat.cancel_reason}")
    elif exit_code == 0:
        _safe_finish(client, status="succeeded", exit_code=0,
                     summary="session ended without calling `jobctl done`; "
                             "treating a clean exit as success")
    else:
        _safe_finish(client, status="failed", exit_code=exit_code,
                     error=f"Claude exited with code {exit_code}")

    return exit_code or 0


def _safe_finish(client: JobClient, *, status: str, summary: str | None = None,
                 error: str | None = None, exit_code: int | None = None) -> None:
    """Report the outcome; the API ignores it if Claude already reported one."""
    try:
        client.finish(status=status, summary=summary, error=error, exit_code=exit_code)
    except ApiError as exc:
        log.error("could not report the final status (the dispatcher will reap this "
                  "job when its lease expires): %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
