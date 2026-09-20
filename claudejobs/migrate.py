"""SQL migration runner.

Migrations are plain .sql files in migrations/, named ``NNNN_description.sql``
and applied in filename order. Each one runs inside a transaction and is
recorded in ``schema_migrations`` with a checksum, so:

* re-running ``migrate up`` is a no-op once everything is applied;
* editing a migration that has already run is detected and refused, because the
  database no longer matches the file (add a new migration instead).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import MIGRATIONS_DIR
from .db import connection

log = logging.getLogger(__name__)

CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """A migration is missing, changed, or failed to apply."""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()[:16]


def discover(directory: Path | None = None) -> list[Migration]:
    """Every migration file on disk, in apply order."""
    folder = directory or MIGRATIONS_DIR
    if not folder.is_dir():
        raise MigrationError(f"migrations directory not found: {folder}")
    migrations = [
        Migration(version=path.stem, path=path, sql=path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*.sql"))
    ]
    if not migrations:
        raise MigrationError(f"no .sql files in {folder}")
    return migrations


def applied_versions(conn) -> dict[str, str]:
    conn.execute(CREATE_TRACKING_TABLE)
    rows = conn.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {row["version"]: row["checksum"] for row in rows}


def _check_drift(migrations: list[Migration], applied: dict[str, str]) -> list[str]:
    drifted = []
    by_version = {m.version: m for m in migrations}
    for version, checksum in applied.items():
        migration = by_version.get(version)
        if migration is None:
            drifted.append(f"{version}: applied to the database but the file is gone")
        elif migration.checksum != checksum:
            drifted.append(f"{version}: file changed since it was applied")
    return drifted


def migrate_up(directory: Path | None = None, allow_drift: bool = False) -> list[str]:
    """Apply every pending migration. Returns the versions applied now."""
    migrations = discover(directory)
    newly_applied: list[str] = []

    with connection() as conn:
        applied = applied_versions(conn)
        drift = _check_drift(migrations, applied)
        if drift and not allow_drift:
            raise MigrationError(
                "Applied migrations no longer match the files:\n  "
                + "\n  ".join(drift)
                + "\nAdd a new migration rather than editing an applied one. "
                "Use --allow-drift to proceed anyway (it only updates checksums)."
            )

        for migration in migrations:
            if migration.version in applied:
                continue
            log.info("applying %s", migration.version)
            # psycopg sends parameterless statements with the simple query
            # protocol, so a whole migration file runs in one round trip —
            # inside this connection's transaction.
            conn.execute(migration.sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (migration.version, migration.checksum),
            )
            newly_applied.append(migration.version)

        if drift and allow_drift:
            for migration in migrations:
                if migration.version in applied:
                    conn.execute(
                        "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
                        (migration.checksum, migration.version),
                    )

    return newly_applied


def status(directory: Path | None = None) -> list[tuple[str, str]]:
    """(version, state) for every migration file, plus any orphans."""
    migrations = discover(directory)
    with connection() as conn:
        applied = applied_versions(conn)

    rows = [(m.version, "applied" if m.version in applied else "pending") for m in migrations]
    known = {m.version for m in migrations}
    rows.extend((version, "MISSING FILE") for version in applied if version not in known)
    return rows
