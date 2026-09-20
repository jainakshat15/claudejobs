"""Markdown transcript of every HTTP request and response.

One file, appended to, with a heading per call: timestamp, method, path, status,
then the request and response bodies as fenced code blocks. Headers are never
written, so the auth tokens stay out of the log.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_lock = threading.Lock()

#: Bodies larger than this are truncated in the log.
MAX_BODY_CHARS = 4000


def fenced(raw: bytes) -> str:
    """Render a body as a fenced block, pretty-printing JSON when possible."""
    if not raw:
        return "_(empty)_"
    text = raw.decode("utf-8", errors="replace")
    lang = ""
    try:
        text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        lang = "json"
    except ValueError:
        pass
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + f"\n… truncated, {len(text)} chars total"
    # The fence must be longer than any run of backticks inside the body.
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def _rotate_if_needed(path: Path, max_mb: int) -> None:
    if max_mb <= 0 or not path.exists():
        return
    if path.stat().st_size < max_mb * 1024 * 1024:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archived = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
    try:
        path.rename(archived)
        log.info("rotated request log to %s", archived.name)
    except OSError as exc:  # never let logging break a request
        log.warning("could not rotate request log: %s", exc)


def append_entry(path: Path, *, method: str, target: str, status: int,
                 request_body: bytes, response_body: bytes, max_mb: int = 20) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    entry = (
        f"## {stamp} — `{method} {target}` → {status}\n\n"
        f"**Request**\n\n{fenced(request_body)}\n\n"
        f"**Response**\n\n{fenced(response_body)}\n\n"
        "---\n\n"
    )
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(path, max_mb)
            is_new = not path.exists() or path.stat().st_size == 0
            with path.open("a", encoding="utf-8") as handle:
                if is_new:
                    handle.write("# Request log\n\n")
                handle.write(entry)
        except OSError as exc:
            log.warning("could not write request log: %s", exc)
