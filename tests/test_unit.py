"""Tests that need no database: parsing, formatting, prompt building, config."""

from __future__ import annotations

import os

import pytest

from claudejobs import models, request_log
from claudejobs.bots.common import (
    ASSIGNMENT_RE,
    OPTION_RE,
    RUN_OPTIONS,
    _pop_options,
    parse_message,
)
from claudejobs.prompt import build_job_instructions


# --------------------------------------------------------------------------- #
# command parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, expected", [
    ("/run fix the tests", ("run", "fix the tests")),
    ("run fix the tests", ("run", "fix the tests")),           # Slack, no slash
    ("/run@claudejobs_bot fix it", ("run", "fix it")),          # Telegram group
    ("/HELP", ("help", "")),
    ("/status 12", ("status", "12")),
    ("just a sentence", None),
    ("", None),
    ("yes please", None),                                       # an answer, not a command
])
def test_parse_message(text, expected):
    assert parse_message(text) == expected


def test_run_options_are_peeled_off_the_prompt():
    options, prompt = _pop_options(
        r'dir:D:\work\api prio:5 model:opus fix the failing auth tests',
        RUN_OPTIONS, OPTION_RE)
    assert options == {"directory": r"D:\work\api", "priority": "5", "model": "opus"}
    assert prompt == "fix the failing auth tests"


def test_quoted_option_values_may_contain_spaces():
    options, prompt = _pop_options(
        'title:"nightly cleanup" dir:"C:\\my work" run the cleanup',
        RUN_OPTIONS, OPTION_RE)
    assert options == {"title": "nightly cleanup", "directory": "C:\\my work"}
    assert prompt == "run the cleanup"


def test_unknown_prefix_is_left_in_the_prompt():
    options, prompt = _pop_options("notanoption:5 do the thing", RUN_OPTIONS, OPTION_RE)
    assert options == {}
    assert prompt == "notanoption:5 do the thing"


def test_edit_assignments_parse():
    options, leftover = _pop_options("priority=5 title=nightly", {"priority": "priority", "title": "title"},
                                     ASSIGNMENT_RE)
    assert options == {"priority": "5", "title": "nightly"}
    assert leftover == ""


# --------------------------------------------------------------------------- #
# job instructions
# --------------------------------------------------------------------------- #
def _job(**overrides):
    job = {"id": 7, "title": "fix tests", "directory": "/work/api", "prompt": "fix the tests",
           "source": "telegram", "source_chat_id": "-100", "source_username": "akshat"}
    job.update(overrides)
    return job


def test_instructions_teach_the_protocol():
    text = build_job_instructions(_job(), jobctl="py -m claudejobs.jobctl",
                                  question_timeout_minutes=90, job_timeout_minutes=120)
    assert "Job id:        7" in text
    assert "py -m claudejobs.jobctl done" in text
    assert "py -m claudejobs.jobctl ask" in text
    assert "90 minutes" in text       # question timeout is stated
    assert "120 minutes" in text      # job timeout is stated
    assert "Never wait for terminal input" in text


def test_instructions_say_nobody_is_reachable_for_http_jobs():
    text = build_job_instructions(_job(source="http", source_chat_id=None),
                                  jobctl="jobctl")
    assert "No human channel" in text
    assert "jobctl ask" not in text.split("## Rules")[0].replace("`jobctl ask`", "")


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def test_short_collapses_whitespace_and_truncates():
    assert models.short("a\n  b   c") == "a b c"
    assert models.short("x" * 100, 10) == "x" * 9 + "…"
    assert models.short(None) == ""


def test_job_line_and_detail_survive_missing_fields():
    job = {"id": 3, "status": "queued", "prompt": "do a thing", "title": None,
           "directory": "/tmp", "priority": 100, "attempts": 0, "max_attempts": 1,
           "source": "cli"}
    assert "#3" in models.job_line(job)
    detail = models.job_detail(job)
    assert "Job #3" in detail and "/tmp" in detail


def test_terminal_statuses():
    assert models.is_terminal("succeeded") and models.is_terminal("timed_out")
    assert not models.is_terminal("waiting_input")
    assert models.ACTIVE_STATUSES == {"running", "waiting_input"}


# --------------------------------------------------------------------------- #
# request transcript
# --------------------------------------------------------------------------- #
def test_fenced_pretty_prints_json():
    assert '"a": 1' in request_log.fenced(b'{"a":1}')


def test_fenced_escapes_bodies_containing_backticks():
    block = request_log.fenced(b'{"prompt": "run ```code``` please"}')
    assert block.startswith("````")


def test_fenced_truncates_huge_bodies():
    body = ("x" * (request_log.MAX_BODY_CHARS + 500)).encode()
    assert "truncated" in request_log.fenced(body)


def test_append_entry_writes_and_rotates(tmp_path):
    path = tmp_path / "requests.md"
    request_log.append_entry(path, method="POST", target="/jobs", status=201,
                             request_body=b'{"prompt":"hi"}', response_body=b'{"id":1}')
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Request log")
    assert "`POST /jobs` → 201" in text

    path.write_text("x" * 2_000_000, encoding="utf-8")
    request_log.append_entry(path, method="GET", target="/health", status=200,
                             request_body=b"", response_body=b"{}", max_mb=1)
    assert len(list(tmp_path.glob("requests-*.md"))) == 1   # old file archived
    assert path.read_text(encoding="utf-8").startswith("# Request log")


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def test_lease_must_exceed_heartbeat(monkeypatch):
    from claudejobs import config

    monkeypatch.setenv("JOB_LEASE_SECONDS", "30")
    monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", "30")
    with pytest.raises(config.ConfigError, match="greater than"):
        config.load_settings()


def test_bad_boolean_is_rejected(monkeypatch):
    from claudejobs import config

    monkeypatch.setenv("KEEP_TERMINAL_OPEN", "maybe")
    with pytest.raises(config.ConfigError, match="true or false"):
        config.load_settings()


def test_chat_dirs_must_be_json(monkeypatch):
    from claudejobs import config

    monkeypatch.setenv("TELEGRAM_CHAT_DIRS", "not json")
    with pytest.raises(config.ConfigError, match="valid JSON"):
        config.load_settings()


def test_placeholder_api_token_is_refused(monkeypatch):
    from claudejobs import config

    monkeypatch.setenv("API_TOKEN", "change-me-to-a-long-random-string")
    settings = config.load_settings()
    with pytest.raises(config.ConfigError, match="placeholder"):
        settings.require_api_token()


def test_telegram_requires_an_allowlist(monkeypatch):
    from claudejobs import config

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")
    settings = config.load_settings()
    with pytest.raises(config.ConfigError, match="ALLOWED_USERS is empty"):
        settings.require_telegram()


# --------------------------------------------------------------------------- #
# locating the Claude CLI
# --------------------------------------------------------------------------- #
def test_claude_bin_may_be_a_plain_path(tmp_path):
    from claudejobs.claude_cli import resolve_claude_command

    binary = tmp_path / ("claude.exe" if os.name == "nt" else "claude")
    binary.write_text("", encoding="utf-8")
    command, single_line = resolve_claude_command(str(binary))
    assert command == [str(binary)]
    assert single_line is False


def test_claude_bin_may_carry_arguments(tmp_path):
    """So you can point at `node cli.js` instead of a shim."""
    from claudejobs.claude_cli import resolve_claude_command

    script = tmp_path / "cli.js"
    script.write_text("", encoding="utf-8")
    runner = tmp_path / ("node.exe" if os.name == "nt" else "node")
    runner.write_text("", encoding="utf-8")

    command, single_line = resolve_claude_command(f'"{runner}" "{script}"')
    assert command == [str(runner), str(script)]
    assert single_line is False


def test_missing_claude_bin_is_reported_clearly():
    from claudejobs.claude_cli import ClaudeNotFound, resolve_claude_command

    with pytest.raises(ClaudeNotFound, match="does not exist"):
        resolve_claude_command("/nowhere/claude-does-not-exist")


@pytest.mark.skipif(os.name != "nt", reason="shim handling is Windows-only")
def test_native_exe_is_preferred_over_a_shim(tmp_path):
    from claudejobs.claude_cli import resolve_claude_command

    (tmp_path / "claude.cmd").write_text("@echo off\n", encoding="utf-8")
    native = tmp_path / "claude.exe"
    native.write_text("", encoding="utf-8")

    command, single_line = resolve_claude_command(str(tmp_path / "claude.cmd"))
    assert command == [str(native)]
    assert single_line is False


@pytest.mark.skipif(os.name != "nt", reason="shim handling is Windows-only")
def test_shim_alone_forces_single_line_arguments(tmp_path, monkeypatch):
    """cmd.exe truncates arguments at the first newline, so we must know."""
    from claudejobs import claude_cli

    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\r\nnot-a-real-shim %*\r\n", encoding="utf-8")
    # Nothing better available: no claude.exe and no node on PATH.
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _name: None)

    command, single_line = claude_cli.resolve_claude_command(str(shim))
    assert command == [str(shim)]
    assert single_line is True


@pytest.mark.skipif(os.name != "nt", reason="shim handling is Windows-only")
def test_npm_shim_is_rewritten_to_node(tmp_path, monkeypatch):
    from claudejobs import claude_cli

    package = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code"
    package.mkdir(parents=True)
    (package / "cli.js").write_text("", encoding="utf-8")
    shim = tmp_path / "claude.cmd"
    shim.write_text('@"%~dp0\\node.exe" "%~dp0\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n',
                    encoding="utf-8")
    monkeypatch.setattr(claude_cli.shutil, "which",
                        lambda name: r"C:\node\node.exe" if name == "node" else None)

    command, single_line = claude_cli.resolve_claude_command(str(shim))
    assert command == [r"C:\node\node.exe", str(package / "cli.js")]
    assert single_line is False


def test_shim_fallback_spills_long_text_into_files(tmp_path, monkeypatch):
    """The prompt and instructions must survive even through a shim."""
    from claudejobs import run_job
    from claudejobs.config import load_settings

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_BIN", "")
    settings = load_settings()
    monkeypatch.setattr(run_job, "resolve_claude_command",
                        lambda _bin: (["claude.cmd"], True))

    job = {"id": 5, "prompt": "line one\nline two", "directory": str(tmp_path),
           "title": "t", "source": "telegram", "source_chat_id": "c1",
           "permission_mode": "bypassPermissions", "model": None,
           "session_id": "11111111-1111-1111-1111-111111111111", "payload": {}}
    argv = run_job.build_claude_argv(job, settings=settings, jobctl_command="jobctl")

    assert all("\n" not in arg for arg in argv), "a shim would truncate this"
    assert (tmp_path / "job-5-instructions.md").exists()
    assert (tmp_path / "job-5-prompt.md").exists()
    assert "line two" in (tmp_path / "job-5-prompt.md").read_text(encoding="utf-8")
    assert str(tmp_path / "job-5-instructions.md") in argv[argv.index("--append-system-prompt") + 1]


def test_normal_launch_passes_the_prompt_directly(tmp_path, monkeypatch):
    from claudejobs import run_job
    from claudejobs.config import load_settings

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    settings = load_settings()
    monkeypatch.setattr(run_job, "resolve_claude_command", lambda _bin: (["claude"], False))

    job = {"id": 6, "prompt": "do the thing", "directory": str(tmp_path), "title": "t",
           "source": "http", "permission_mode": "acceptEdits", "model": "sonnet",
           "session_id": "22222222-2222-2222-2222-222222222222", "payload": {}}
    argv = run_job.build_claude_argv(job, settings=settings, jobctl_command="jobctl")

    assert argv[0] == "claude"
    assert argv[-1] == "do the thing"
    assert "--permission-mode" in argv and "acceptEdits" in argv
    assert "--model" in argv and "sonnet" in argv
    assert argv[argv.index("--session-id") + 1] == job["session_id"]


# --------------------------------------------------------------------------- #
# migrations on disk
# --------------------------------------------------------------------------- #
def test_migrations_are_discoverable_and_checksummed():
    from claudejobs.migrate import Migration, discover

    migrations = discover()
    assert migrations, "no migration files found"
    assert migrations[0].version.startswith("0001")
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)

    first = migrations[0]
    assert first.checksum == Migration(first.version, first.path, first.sql).checksum
    assert first.checksum != Migration(first.version, first.path, first.sql + " ").checksum
