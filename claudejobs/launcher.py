"""Open a terminal window and start one job's worker inside it.

Why a generated script instead of a plain command line?

``wt.exe`` hands the new tab to an already-running Windows Terminal process, so
the child does **not** reliably inherit the dispatcher's environment, and the
same is true of gnome-terminal (which proxies through gnome-terminal-server).
Writing a tiny per-job script that sets its own variables sidesteps both, and
keeps the job token off the command line where other users could read it from
the process list.

Scripts live in the OS temp directory with owner-only permissions and are
cleaned up by the dispatcher.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

WINDOWS_TERMINALS = ("wt", "cmd")
LINUX_TERMINALS = (
    ("gnome-terminal", ["--title", "{title}", "--"]),
    ("konsole", ["--title", "{title}", "-e"]),
    ("xfce4-terminal", ["--title", "{title}", "-x"]),
    ("alacritty", ["-t", "{title}", "-e"]),
    ("kitty", ["--title", "{title}"]),
    ("xterm", ["-T", "{title}", "-e"]),
)


class LaunchError(RuntimeError):
    """The job's terminal could not be started."""


def run_dir() -> Path:
    """Owner-only scratch directory for launch scripts."""
    path = Path(tempfile.gettempdir()) / "claudejobs"
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def cleanup_old_scripts(max_age_hours: int = 24) -> int:
    """Remove launch scripts left behind by finished jobs."""
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        for script in run_dir().glob("job-*"):
            try:
                if script.stat().st_mtime < cutoff:
                    script.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError as exc:
        log.debug("could not clean launch scripts: %s", exc)
    return removed


def _write_script(job_id: int, *, directory: str, token: str, settings: Settings,
                  python_exe: str, repo_root: Path) -> Path:
    """Write the per-job launch script and return its path."""
    env = {
        "CLAUDEJOBS_JOB_ID": str(job_id),
        "CLAUDEJOBS_JOB_TOKEN": token,
        "CLAUDEJOBS_API_URL": settings.api_base_url,
        # Works whether or not the package was pip-installed into the venv.
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
    }
    title = f"claudejobs #{job_id}"

    if os.name == "nt":
        path = run_dir() / f"job-{job_id}.cmd"
        lines = ["@echo off", f"title {title}"]
        lines += [f'set "{key}={value}"' for key, value in env.items()]
        lines += [
            f'cd /d "{directory}"',
            f'"{python_exe}" -m claudejobs.run_job',
            "set EXITCODE=%ERRORLEVEL%",
            "echo.",
            "echo [claudejobs] worker exited with code %EXITCODE%",
        ]
        if settings.keep_terminal_open:
            lines.append("pause")
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    else:
        path = run_dir() / f"job-{job_id}.sh"
        lines = ["#!/bin/sh"]
        lines += [f"export {key}='{value}'" for key, value in env.items()]
        lines += [
            f"cd '{directory}' || exit 1",
            f"'{python_exe}' -m claudejobs.run_job",
            "EXITCODE=$?",
            'printf "\\n[claudejobs] worker exited with code %s\\n" "$EXITCODE"',
        ]
        if settings.keep_terminal_open:
            lines.append('printf "[press enter to close]"; read _')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    if os.name != "nt":
        os.chmod(path, 0o700)  # the token lives in this file
    return path


def _windows_command(script: Path, directory: str, mode: str, title: str) -> list[str]:
    wt = shutil.which("wt.exe") or shutil.which("wt")
    if mode in {"auto", "wt"} and wt:
        # No leading-dash tokens after the '--', so wt cannot mistake any of
        # them for its own options.
        return [wt, "-d", directory, "--title", title, "--", "cmd", "/c", str(script)]
    if mode == "wt" and not wt:
        raise LaunchError("TERMINAL_MODE=wt but wt.exe is not installed "
                          "(install Windows Terminal or use TERMINAL_MODE=cmd)")
    return ["cmd", "/c", "start", title, "/D", directory, "cmd", "/c", str(script)]


def _posix_command(script: Path, mode: str, title: str) -> list[str]:
    if sys.platform == "darwin" and mode in {"auto", "macos"}:
        return ["open", "-a", "Terminal", str(script)]

    candidates = LINUX_TERMINALS
    if mode not in {"auto", "macos"}:
        candidates = tuple(entry for entry in LINUX_TERMINALS if entry[0] == mode)
        if not candidates:
            raise LaunchError(f"TERMINAL_MODE={mode} is not a terminal this launcher knows")

    for terminal, template in candidates:
        binary = shutil.which(terminal)
        if binary:
            flags = [part.format(title=title) for part in template]
            return [binary, *flags, str(script)]

    raise LaunchError(
        "no terminal emulator found. Install one (gnome-terminal, konsole, xterm ...) "
        "or set TERMINAL_MODE=headless to run jobs without a window."
    )


def launch_job(job_id: int, *, directory: str, token: str, settings: Settings,
               log_path: Path, python_exe: str | None = None,
               repo_root: Path | None = None) -> subprocess.Popen:
    """Start the worker for ``job_id``. Returns the spawned process handle.

    In terminal mode the handle is the terminal launcher, which usually exits
    immediately — the job's real liveness signal is its heartbeat, not this pid.
    """
    from .config import REPO_ROOT

    python = python_exe or sys.executable
    root = repo_root or REPO_ROOT
    mode = settings.terminal_mode
    title = f"claudejobs #{job_id}"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "headless":
        env = os.environ.copy()
        env.update({
            "CLAUDEJOBS_JOB_ID": str(job_id),
            "CLAUDEJOBS_JOB_TOKEN": token,
            "CLAUDEJOBS_API_URL": settings.api_base_url,
            "PYTHONPATH": str(root),
            "PYTHONUNBUFFERED": "1",
        })
        # Claude's own output goes to its own file: the worker writes structured
        # events to log_path through a logging handler, and two independent
        # handles on one file interleave badly.
        handle = log_path.with_name(f"{log_path.stem}.out{log_path.suffix}").open(
            "a", encoding="utf-8")
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.Popen(
            [python, "-m", "claudejobs.run_job"],
            cwd=directory, env=env, stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=creation,
            start_new_session=(os.name != "nt"),
        )

    script = _write_script(job_id, directory=directory, token=token, settings=settings,
                           python_exe=python, repo_root=root)
    command = (_windows_command(script, directory, mode, title) if os.name == "nt"
               else _posix_command(script, mode, title))
    log.debug("launching job #%s: %s", job_id, " ".join(command))
    try:
        return subprocess.Popen(command, cwd=directory)
    except OSError as exc:
        raise LaunchError(f"could not start a terminal for job #{job_id}: {exc}") from exc
