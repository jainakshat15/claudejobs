"""Test fixtures.

Unit tests run anywhere. The database tests need a throwaway Postgres; point
CLAUDEJOBS_TEST_DATABASE_URL at one and they run, otherwise they skip:

    docker run -d --name claudejobs-test -e POSTGRES_PASSWORD=test \
        -e POSTGRES_DB=claudejobs -p 55432:5432 postgres:16-alpine

    export CLAUDEJOBS_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/claudejobs
    pytest

The schema is created once per session from the real migration files, and every
test starts from empty tables.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

TEST_DB_ENV = "CLAUDEJOBS_TEST_DATABASE_URL"
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get(TEST_DB_ENV)
    if not url:
        pytest.skip(f"{TEST_DB_ENV} is not set; skipping database tests")
    return url


@pytest.fixture(scope="session")
def configured(database_url: str):
    """Point the whole package at the test database."""
    os.environ["DATABASE_URL"] = database_url
    os.environ["API_TOKEN"] = "test-token-not-a-placeholder"
    os.environ["API_BASE_URL"] = "http://127.0.0.1:8000"
    os.environ["REQUEST_LOG_FILE"] = ""          # no transcript during tests
    os.environ["LOG_DIR"] = str(REPO_ROOT / "logs" / "test-jobs")
    os.environ["ALLOWED_ROOTS"] = ""             # any directory
    os.environ["DEFAULT_DIRECTORY"] = str(REPO_ROOT)
    os.environ["MAX_CONCURRENT_JOBS"] = "2"
    os.environ["JOB_LEASE_SECONDS"] = "180"
    os.environ["HEARTBEAT_INTERVAL_SECONDS"] = "30"

    from claudejobs import config, db

    config.get_settings.cache_clear()
    db.close_pool()

    from claudejobs.migrate import migrate_up

    migrate_up()
    yield config.get_settings()
    db.close_pool()


@pytest.fixture
def conn(configured):
    """A connection with a clean set of tables."""
    from claudejobs import db

    with db.connection() as connection:
        connection.execute(
            "TRUNCATE outbound_messages, job_messages, job_events, jobs RESTART IDENTITY CASCADE"
        )
    with db.connection() as connection:
        yield connection


@pytest.fixture
def api_client(configured):
    """FastAPI test client with clean tables."""
    from fastapi.testclient import TestClient

    from claudejobs import db
    from claudejobs.api import app

    with db.connection() as connection:
        connection.execute(
            "TRUNCATE outbound_messages, job_messages, job_events, jobs RESTART IDENTITY CASCADE"
        )
    with TestClient(app) as client:
        client.headers.update({"X-Auth-Token": os.environ["API_TOKEN"]})
        yield client
