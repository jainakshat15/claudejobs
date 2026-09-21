"""Settings for every claudejobs process, read from the environment and .env.

Real environment variables take precedence over .env, so a service manager can
override any single value without editing the file.

Nothing here raises at import time: a missing DATABASE_URL only matters to the
processes that talk to Postgres, and jobctl needs neither the database nor the
bot tokens. Call the ``require_*`` helpers at the point of use so the error
message names the setting and the command that needed it.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from . import products

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


# --------------------------------------------------------------------------- #
# env parsing helpers
# --------------------------------------------------------------------------- #
def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _csv(name: str) -> list[str]:
    return [part.strip() for part in _str(name).split(",") if part.strip()]


def _json_dict(name: str) -> dict[str, str]:
    raw = _str(name)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be valid JSON, got {raw!r}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a JSON object, got {type(value).__name__}")
    return {str(k): str(v) for k, v in value.items()}


def _path(name: str, default: str) -> Path:
    """Resolve a path setting; relative values hang off the repo root."""
    raw = _str(name, default)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path)


def _roots(name: str) -> list[Path]:
    return [Path(p).expanduser().resolve()
            for p in _str(name).split(os.pathsep) if p.strip()]


def _sibling(name: str, default: str) -> Path:
    """A directory setting whose relative default sits next to this checkout.

    The product sources (a product's repository, its documentation tree) are
    separate checkouts kept alongside claudejobs, not inside it.
    """
    raw = _str(name, default)
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT.parent / path).resolve()


def _common_parent(*paths: Path) -> str:
    """The deepest directory containing all of ``paths`` — where a job that has
    to read several of them can run."""
    try:
        return str(Path(os.path.commonpath([str(p) for p in paths])))
    except ValueError:      # different drives on Windows
        return str(paths[0])


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    # database
    database_url: str
    db_pool_min: int
    db_pool_max: int
    db_statement_timeout_ms: int

    # api
    api_host: str
    api_port: int
    api_token: str
    api_base_url: str
    request_log_file: Path | None
    request_log_max_mb: int

    # dispatcher
    max_concurrent_jobs: int
    poll_interval_seconds: int
    job_lease_seconds: int
    heartbeat_interval_seconds: int
    job_timeout_minutes: int
    question_timeout_minutes: int
    default_max_attempts: int
    worker_id: str

    # claude launch
    claude_bin: str
    default_permission_mode: str
    default_model: str
    terminal_mode: str
    keep_terminal_open: bool
    log_dir: Path
    log_level: str
    allowed_roots: list[Path]
    default_directory: str

    # products a chat question can be asked about (/ask-sales-bot, /ask-od)
    products: tuple[products.Product, ...]

    # bots
    telegram_bot_token: str
    telegram_allowed_users: set[int]
    telegram_chat_dirs: dict[str, str]
    slack_bot_token: str
    slack_app_token: str
    slack_allowed_users: set[str]
    slack_channel_dirs: dict[str, str]
    outbound_poll_seconds: int

    # ----------------------------------------------------------------- #
    # guards — call these where the setting is actually needed
    # ----------------------------------------------------------------- #
    def require_database_url(self) -> str:
        if not self.database_url:
            raise ConfigError(
                "DATABASE_URL is not set. Put your Postgres connection string in "
                f"{REPO_ROOT / '.env'} (see .env.example) or export it in the "
                "environment."
            )
        return self.database_url

    def require_api_token(self) -> str:
        if not self.api_token or self.api_token.startswith("change-me"):
            raise ConfigError(
                "API_TOKEN is unset or still the placeholder. Generate one with:\n"
                '  python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
                "and put it in .env as API_TOKEN=..."
            )
        return self.api_token

    def require_telegram(self) -> tuple[str, set[int]]:
        if not self.telegram_bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set — see docs/TELEGRAM_SETUP.md")
        if not self.telegram_allowed_users:
            raise ConfigError(
                "TELEGRAM_ALLOWED_USERS is empty. Refusing to start a bot that "
                "anyone could use to run commands on this machine. Send /whoami "
                "to the bot to learn your numeric id, then list it in .env."
            )
        return self.telegram_bot_token, self.telegram_allowed_users

    def require_slack(self) -> tuple[str, str, set[str]]:
        if not self.slack_bot_token or not self.slack_app_token:
            raise ConfigError(
                "SLACK_BOT_TOKEN and SLACK_APP_TOKEN are both required for Socket "
                "Mode — see docs/SLACK_SETUP.md"
            )
        if not self.slack_allowed_users:
            raise ConfigError(
                "SLACK_ALLOWED_USERS is empty. Refusing to start a bot that anyone "
                "in the workspace could use to run commands on this machine."
            )
        return self.slack_bot_token, self.slack_app_token, self.slack_allowed_users

    def require_product(self, key: str) -> products.Product:
        """One configured product, with both of its directories checked."""
        product = next((p for p in self.products if p.key == key), None)
        if product is None:
            raise ConfigError(f"no product named {key!r} is configured")
        for suffix, path, what in (
            ("CODE_DIR", product.code_dir, "source lives"),
            ("DOCS_DIR", product.docs_dir, "documentation lives"),
        ):
            if not path.is_dir():
                raise ConfigError(
                    f"{path} is not a directory, so there is nothing for "
                    f"/{product.command.replace('_', '-')} to read. Set "
                    f"{product.env_prefix}_{suffix} in .env to where the "
                    f"{product.name} {what}."
                )
        return product

    def job_log_path(self, job_id: int) -> Path:
        return self.log_dir / f"job-{job_id}.log"


def _products() -> tuple[products.Product, ...]:
    """Resolve every catalogue entry against the environment.

    Each product reads <PREFIX>_CODE_DIR, <PREFIX>_DOCS_DIR, <PREFIX>_DIRECTORY
    and <PREFIX>_TIMEOUT_MINUTES — SALES_BOT_CODE_DIR, OD_CODE_DIR and so on.
    """
    resolved = []
    for product in products.CATALOGUE:
        prefix = product.env_prefix
        code_dir = _sibling(f"{prefix}_CODE_DIR", str(product.code_dir))
        docs_dir = _sibling(f"{prefix}_DOCS_DIR", str(product.docs_dir))
        resolved.append(replace(
            product,
            code_dir=code_dir,
            docs_dir=docs_dir,
            # A job has to read both, so it runs where the two meet unless it
            # is sent somewhere explicitly.
            directory=(_str(f"{prefix}_DIRECTORY")
                       or _common_parent(code_dir, docs_dir)),
            timeout_minutes=max(1, _int(f"{prefix}_TIMEOUT_MINUTES",
                                        product.timeout_minutes)),
        ))
    return tuple(resolved)


def load_settings() -> Settings:
    """Read .env (if present) and build a Settings object."""
    load_dotenv(REPO_ROOT / ".env")

    request_log = _str("REQUEST_LOG_FILE", "logs/requests.md")
    lease = _int("JOB_LEASE_SECONDS", 180)
    heartbeat = _int("HEARTBEAT_INTERVAL_SECONDS", 30)
    if lease <= heartbeat:
        raise ConfigError(
            f"JOB_LEASE_SECONDS ({lease}) must be greater than "
            f"HEARTBEAT_INTERVAL_SECONDS ({heartbeat}), otherwise healthy jobs "
            "get reaped between heartbeats. Aim for at least 3x."
        )

    return Settings(
        database_url=_str("DATABASE_URL"),
        db_pool_min=max(1, _int("DB_POOL_MIN", 1)),
        db_pool_max=max(1, _int("DB_POOL_MAX", 5)),
        db_statement_timeout_ms=_int("DB_STATEMENT_TIMEOUT_MS", 15000),
        api_host=_str("API_HOST", "127.0.0.1"),
        api_port=_int("API_PORT", 8000),
        api_token=_str("API_TOKEN"),
        api_base_url=_str("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        request_log_file=(_path("REQUEST_LOG_FILE", request_log) if request_log else None),
        request_log_max_mb=_int("REQUEST_LOG_MAX_MB", 20),
        max_concurrent_jobs=max(1, _int("MAX_CONCURRENT_JOBS", 2)),
        poll_interval_seconds=max(1, _int("POLL_INTERVAL_SECONDS", 5)),
        job_lease_seconds=lease,
        heartbeat_interval_seconds=heartbeat,
        job_timeout_minutes=_int("JOB_TIMEOUT_MINUTES", 240),
        question_timeout_minutes=_int("QUESTION_TIMEOUT_MINUTES", 180),
        default_max_attempts=max(1, _int("DEFAULT_MAX_ATTEMPTS", 1)),
        worker_id=_str("WORKER_ID") or socket.gethostname(),
        claude_bin=_str("CLAUDE_BIN"),
        default_permission_mode=_str("DEFAULT_PERMISSION_MODE", "bypassPermissions"),
        default_model=_str("DEFAULT_MODEL"),
        terminal_mode=_str("TERMINAL_MODE", "auto").lower(),
        keep_terminal_open=_bool("KEEP_TERMINAL_OPEN", True),
        log_dir=_path("LOG_DIR", "logs/jobs"),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        allowed_roots=_roots("ALLOWED_ROOTS"),
        default_directory=_str("DEFAULT_DIRECTORY"),
        products=_products(),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_users={int(u) for u in _csv("TELEGRAM_ALLOWED_USERS") if u.lstrip("-").isdigit()},
        telegram_chat_dirs=_json_dict("TELEGRAM_CHAT_DIRS"),
        slack_bot_token=_str("SLACK_BOT_TOKEN"),
        slack_app_token=_str("SLACK_APP_TOKEN"),
        slack_allowed_users=set(_csv("SLACK_ALLOWED_USERS")),
        slack_channel_dirs=_json_dict("SLACK_CHANNEL_DIRS"),
        outbound_poll_seconds=max(1, _int("OUTBOUND_POLL_SECONDS", 3)),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings. Call get_settings.cache_clear() in tests."""
    return load_settings()


def setup_logging(service: str) -> logging.Logger:
    """Consistent console logging for every long-running process."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Uvicorn/httpx are chatty at INFO; keep the queue's own logs readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger(service)
