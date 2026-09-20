"""Working out how to invoke the Claude Code CLI safely.

Windows detail that silently breaks jobs if ignored: a ``.cmd``/``.bat`` shim
(what an npm install of Claude Code puts on PATH) is executed through cmd.exe,
and cmd.exe truncates the command line at the first newline. The job
instructions are always multi-line, so through a shim Claude would receive a
fragment — no --session-id, no prompt — with no error anywhere.

So we resolve, in order of preference:

1. a native executable (claude.exe on Windows, claude on Linux/macOS);
2. node + the CLI's cli.js, read out of the shim;
3. the shim itself, and then we keep every argument to a single line by putting
   the long text in files and passing one-line pointers to them.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

SHIM_SUFFIXES = {".cmd", ".bat"}
#: Matches the JS entry point inside an npm-generated shim.
JS_PATH_RE = re.compile(r'"?([^"\s]*cli\.js)"?')


class ClaudeNotFound(FileNotFoundError):
    """The Claude Code CLI could not be located."""


def _expand_shim_path(raw: str, shim_dir: Path) -> Path:
    """npm shims write paths as %~dp0\\node_modules\\... — resolve that."""
    cleaned = raw.replace("%~dp0", str(shim_dir) + os.sep).replace("%dp0%", str(shim_dir) + os.sep)
    cleaned = cleaned.replace('"', "").strip()
    return Path(os.path.normpath(cleaned))


def _node_command_from_shim(shim: Path) -> list[str] | None:
    """Turn an npm shim into a direct `node cli.js` invocation."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for match in JS_PATH_RE.finditer(text):
        script = _expand_shim_path(match.group(1), shim.parent)
        if script.exists():
            return [node, str(script)]
    return None


def _split_command(value: str) -> list[str]:
    """Allow CLAUDE_BIN to carry arguments, e.g. `node C:\\path\\cli.js`."""
    parts = shlex.split(value, posix=(os.name != "nt"))
    return [part.strip('"') for part in parts if part.strip('"')]


def resolve_claude_command(claude_bin: str | None = None) -> tuple[list[str], bool]:
    """Return (command prefix, single_line_args_required).

    ``single_line_args_required`` is True when the command must be invoked
    through a shell shim that cannot carry newlines in its arguments.
    """
    if claude_bin:
        if Path(claude_bin).exists():
            command = [claude_bin]
        else:
            command = _split_command(claude_bin)
            if not command:
                raise ClaudeNotFound(f"CLAUDE_BIN is set but empty: {claude_bin!r}")
            head = command[0]
            if not Path(head).exists() and not shutil.which(head):
                raise ClaudeNotFound(
                    f"CLAUDE_BIN points at {head!r}, which does not exist. Set it to the "
                    "full path of the Claude Code executable, or leave it empty to use PATH."
                )
    else:
        found = shutil.which("claude")
        if not found:
            raise ClaudeNotFound(
                "the `claude` CLI is not on PATH. Install Claude Code and sign in, or set "
                "CLAUDE_BIN in .env to the full path of the executable."
            )
        command = [found]

    if os.name != "nt":
        return command, False

    head = Path(command[0])
    if head.suffix.lower() not in SHIM_SUFFIXES:
        return command, False

    # A shim: look for something that takes arguments faithfully instead.
    native = head.with_suffix(".exe")
    if native.exists():
        log.info("using %s instead of the %s shim", native.name, head.suffix)
        return [str(native), *command[1:]], False

    on_path = shutil.which("claude.exe")
    if on_path:
        log.info("using %s instead of the %s shim", on_path, head.suffix)
        return [on_path, *command[1:]], False

    node_command = _node_command_from_shim(head)
    if node_command:
        log.info("running Claude through node: %s", node_command[1])
        return [*node_command, *command[1:]], False

    log.warning(
        "%s is a %s shim, which cannot carry multi-line arguments. Falling back to "
        "passing the prompt and instructions as files. Installing the native Claude "
        "Code executable (claude.exe) avoids this.", head, head.suffix)
    return command, True


def describe() -> str:
    """One line for selfcheck output."""
    try:
        command, single_line = resolve_claude_command(None)
    except ClaudeNotFound as exc:
        return f"not found — {exc}"
    suffix = " (via a shell shim: prompts are passed as files)" if single_line else ""
    return " ".join(command) + suffix
