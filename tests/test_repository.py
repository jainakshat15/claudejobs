"""Queue semantics against a real Postgres: claiming, leases, questions, delivery."""

from __future__ import annotations

import uuid

import pytest

from claudejobs import db, models
from claudejobs import repository as repo

pytestmark = pytest.mark.usefixtures("configured")


def make_job(conn, *, prompt="do a thing", priority=100, source="telegram", **kwargs):
    defaults = dict(directory="/work/api", source=source, source_user_id="u1",
                    source_username="akshat", source_chat_id="c1", source_message_id="m1")
    defaults.update(kwargs)
    return repo.create_job(conn, prompt=prompt, priority=priority, **defaults)


def claim(conn, limit=2, worker_id="w1"):
    return repo.claim_jobs(conn, worker_id=worker_id, worker_host="host",
                           limit=limit, lease_seconds=180)


# --------------------------------------------------------------------------- #
# claiming
# --------------------------------------------------------------------------- #
def test_claim_takes_capacity_in_priority_order(conn):
    make_job(conn, prompt="low", priority=500)
    urgent = make_job(conn, prompt="urgent", priority=1)
    make_job(conn, prompt="normal", priority=100)

    claimed = claim(conn, limit=2)
    assert [job["prompt"] for job in claimed] == ["urgent", "normal"]
    assert claimed[0]["id"] == urgent["id"]
    assert all(job["status"] == models.RUNNING for job in claimed)
    assert all(job["attempts"] == 1 for job in claimed)
    assert repo.count_active(conn, "w1") == 2


def test_claiming_is_idempotent_for_already_running_jobs(conn):
    make_job(conn)
    assert len(claim(conn, limit=5)) == 1
    assert claim(conn, limit=5) == []


def test_scheduled_jobs_wait_their_turn(conn):
    conn.execute("SET TIME ZONE 'UTC'")
    make_job(conn, prompt="later")
    conn.execute("UPDATE jobs SET scheduled_at = now() + interval '1 hour'")
    assert claim(conn) == []
    conn.execute("UPDATE jobs SET scheduled_at = now() - interval '1 minute'")
    assert len(claim(conn)) == 1


def test_cancelled_jobs_are_never_claimed(conn):
    job = make_job(conn)
    repo.request_cancel(conn, job["id"], reason="changed my mind", actor="akshat")
    assert conn.execute("SELECT status FROM jobs WHERE id = %s",
                        (job["id"],)).fetchone()["status"] == models.CANCELLED
    assert claim(conn) == []


def test_two_workers_never_claim_the_same_job(configured):
    """SKIP LOCKED is what makes a second dispatcher safe."""
    with db.connection() as setup:
        setup.execute("TRUNCATE outbound_messages, job_messages, job_events, jobs "
                      "RESTART IDENTITY CASCADE")
        for index in range(4):
            make_job(setup, prompt=f"job {index}")

    with db.connection() as first:
        taken_first = repo.claim_jobs(first, worker_id="w1", worker_host="h",
                                      limit=2, lease_seconds=180)
        # Still inside w1's open transaction: w2 must skip those rows, not block.
        with db.connection() as second:
            taken_second = repo.claim_jobs(second, worker_id="w2", worker_host="h",
                                           limit=2, lease_seconds=180)

    ids_first = {job["id"] for job in taken_first}
    ids_second = {job["id"] for job in taken_second}
    assert len(ids_first) == 2 and len(ids_second) == 2
    assert ids_first.isdisjoint(ids_second)


# --------------------------------------------------------------------------- #
# leases
# --------------------------------------------------------------------------- #
def test_heartbeat_extends_the_lease(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    conn.execute("UPDATE jobs SET lease_expires_at = now() + interval '5 seconds' WHERE id = %s",
                 (job["id"],))
    updated = repo.heartbeat(conn, job["id"], 180)
    assert updated is not None
    row = conn.execute("SELECT lease_expires_at > now() + interval '2 minutes' AS extended "
                       "FROM jobs WHERE id = %s", (job["id"],)).fetchone()
    assert row["extended"] is True


def test_heartbeat_reports_a_cancel_request(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    repo.request_cancel(conn, job["id"], reason="stop please", actor="akshat")
    assert repo.heartbeat(conn, job["id"], 180)["cancel_requested"] is True


def test_dead_worker_is_retried_then_failed(conn):
    make_job(conn, max_attempts=2)
    job = claim(conn, limit=1)[0]
    conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 minute'")

    requeued = repo.expire_leases(conn)
    assert requeued[0]["status"] == models.QUEUED
    assert "stopped responding" in requeued[0]["error"]

    job = claim(conn, limit=1)[0]               # second and final attempt
    assert job["attempts"] == 2
    conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 minute'")
    failed = repo.expire_leases(conn)
    assert failed[0]["status"] == models.FAILED


def test_lease_expiry_after_timeout_records_timed_out(conn):
    make_job(conn, max_attempts=3)
    claim(conn, limit=1)
    repo.mark_timeouts(conn, default_timeout_minutes=0)  # nothing yet: no elapsed time
    conn.execute("UPDATE jobs SET started_at = now() - interval '10 hours'")
    marked = repo.mark_timeouts(conn, default_timeout_minutes=60)
    assert marked[0]["cancel_reason"] == "timeout"

    conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 minute'")
    reaped = repo.expire_leases(conn)
    assert reaped[0]["status"] == models.TIMED_OUT     # not retried, not just 'failed'


def test_finish_after_timeout_request_is_recorded_as_timed_out(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    conn.execute("UPDATE jobs SET started_at = now() - interval '10 hours'")
    repo.mark_timeouts(conn, default_timeout_minutes=60)
    finished = repo.finish_job(conn, job["id"], status=models.CANCELLED, exit_code=1)
    assert finished["status"] == models.TIMED_OUT


def test_first_terminal_status_wins(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    repo.finish_job(conn, job["id"], status=models.SUCCEEDED, summary="all done", actor="claude")
    # The wrapper then reports a non-zero exit code: recorded, but not overriding.
    again = repo.finish_job(conn, job["id"], status=models.FAILED, exit_code=3, actor="wrapper")
    assert again["status"] == models.SUCCEEDED
    assert again["result_summary"] == "all done"
    assert again["exit_code"] == 3


def test_retry_clears_worker_state(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    repo.attach_worker(conn, job["id"], pid=42, session_id=str(uuid.uuid4()),
                       job_token="secret", log_path="/tmp/x.log")
    repo.finish_job(conn, job["id"], status=models.FAILED, error="boom")

    retried = repo.retry_job(conn, job["id"], actor="akshat")
    assert retried["status"] == models.QUEUED
    assert retried["worker_id"] is None and retried["job_token_hash"] is None
    assert retried["error"] is None
    assert retried["max_attempts"] > retried["attempts"]


def test_only_queued_jobs_can_be_edited(conn):
    job = make_job(conn)
    edited = repo.update_job(conn, job["id"], {"priority": 5, "title": "new"}, actor="akshat")
    assert edited["priority"] == 5 and edited["title"] == "new"

    claim(conn, limit=1)
    assert repo.update_job(conn, job["id"], {"priority": 9}, actor="akshat") is None


# --------------------------------------------------------------------------- #
# questions and answers
# --------------------------------------------------------------------------- #
def test_question_round_trip(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]

    question = repo.ask_question(conn, job["id"], "which branch?")
    assert question["status"] == "open"
    assert repo.get_job(conn, job["id"])["status"] == models.WAITING_INPUT

    answered = repo.answer_question(conn, question["id"], body="main", answered_by="akshat")
    assert answered["status"] == "answered"
    assert repo.get_job(conn, job["id"])["status"] == models.RUNNING

    kinds = [m["kind"] for m in repo.list_messages(conn, job["id"])]
    assert "answer" in kinds and "question" in kinds


def test_a_second_question_reuses_the_open_one(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    first = repo.ask_question(conn, job["id"], "which branch?")
    second = repo.ask_question(conn, job["id"], "actually, which environment?")
    assert second["id"] == first["id"]          # one open question at a time
    assert second["body"] == "which branch?"


def test_answering_twice_is_refused(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    question = repo.ask_question(conn, job["id"], "which branch?")
    assert repo.answer_question(conn, question["id"], body="main", answered_by="a")
    assert repo.answer_question(conn, question["id"], body="develop", answered_by="b") is None


def test_unanswered_question_eventually_fails_the_job(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    repo.ask_question(conn, job["id"], "which branch?")
    conn.execute("UPDATE job_messages SET created_at = now() - interval '5 hours'")

    expired = repo.expire_questions(conn, timeout_minutes=60)
    assert len(expired) == 1
    assert expired[0]["job"]["status"] == models.FAILED
    assert "no answer within" in expired[0]["job"]["error"]


def test_finishing_a_job_closes_its_open_question(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    question = repo.ask_question(conn, job["id"], "which branch?")
    repo.finish_job(conn, job["id"], status=models.SUCCEEDED, summary="done anyway")
    assert repo.get_question(conn, question["id"])["status"] == "cancelled"


def test_open_questions_are_listed_per_person(conn):
    """The multiple-jobs-per-poster case the bots have to disambiguate."""
    for index in range(2):
        make_job(conn, prompt=f"job {index}")
    jobs = claim(conn, limit=2)
    for job in jobs:
        repo.ask_question(conn, job["id"], f"question for {job['id']}")

    waiting = repo.open_questions_for_user(conn, source="telegram", source_user_id="u1")
    assert len(waiting) == 2
    assert {q["job_id"] for q in waiting} == {job["id"] for job in jobs}
    assert repo.open_questions_for_user(conn, source="telegram", source_user_id="someone-else") == []


# --------------------------------------------------------------------------- #
# outbound delivery
# --------------------------------------------------------------------------- #
def test_outbound_claim_send_and_route_back(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    question = repo.ask_question(conn, job["id"], "which branch?")
    queued = repo.enqueue_outbound(conn, job=job, body="needs input", kind="question",
                                   question_id=question["id"])
    assert queued["status"] == "pending"

    claimed = repo.claim_outbound(conn, channel="telegram", claimed_by="bot1", limit=5)
    assert [row["id"] for row in claimed] == [queued["id"]]
    assert repo.claim_outbound(conn, channel="telegram", claimed_by="bot2", limit=5) == []

    repo.mark_outbound_sent(conn, queued["id"], provider_message_id="555")

    found = repo.find_question_by_provider_message(
        conn, channel="telegram", chat_id="c1", provider_message_id="555")
    assert found["id"] == question["id"]


def test_http_jobs_have_nowhere_to_deliver(conn):
    job = make_job(conn, source="http", source_chat_id=None)
    assert repo.enqueue_outbound(conn, job=job, body="hello") is None


def test_failed_delivery_retries_then_gives_up(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    queued = repo.enqueue_outbound(conn, job=job, body="hello")
    conn.execute("UPDATE outbound_messages SET max_attempts = 2")

    repo.claim_outbound(conn, channel="telegram", claimed_by="bot1", limit=5)
    assert repo.mark_outbound_failed(conn, queued["id"], error="chat not found")["status"] == "pending"
    repo.claim_outbound(conn, channel="telegram", claimed_by="bot1", limit=5)
    assert repo.mark_outbound_failed(conn, queued["id"], error="chat not found")["status"] == "failed"


def test_crashed_bot_leaves_messages_recoverable(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    repo.enqueue_outbound(conn, job=job, body="hello")
    repo.claim_outbound(conn, channel="telegram", claimed_by="bot1", limit=5)
    conn.execute("UPDATE outbound_messages SET claimed_at = now() - interval '10 minutes'")

    assert repo.requeue_stale_outbound(conn, older_than_seconds=300) == 1
    assert len(repo.claim_outbound(conn, channel="telegram", claimed_by="bot2", limit=5)) == 1


# --------------------------------------------------------------------------- #
# tokens and stats
# --------------------------------------------------------------------------- #
def test_job_tokens_are_stored_hashed(conn):
    make_job(conn)
    job = claim(conn, limit=1)[0]
    token = repo.new_job_token()
    repo.attach_worker(conn, job["id"], pid=1, session_id=str(uuid.uuid4()),
                       job_token=token, log_path="/tmp/j.log")

    stored = repo.get_job(conn, job["id"])["job_token_hash"]
    assert stored != token and len(stored) == 64
    assert repo.verify_job_token(conn, job["id"], token)["id"] == job["id"]
    assert repo.verify_job_token(conn, job["id"], "wrong") is None
    assert repo.verify_job_token(conn, job["id"], "") is None


def test_stats_counts_every_status(conn):
    make_job(conn)
    make_job(conn)
    claim(conn, limit=1)
    stats = repo.stats(conn)
    assert stats["jobs_by_status"]["queued"] == 1
    assert stats["jobs_by_status"]["running"] == 1
    assert set(stats["jobs_by_status"]) == set(models.ALL_STATUSES)
    assert stats["active_workers"] == {"w1": 1}


def test_events_record_the_job_history(conn):
    job = make_job(conn)
    claim(conn, limit=1)
    repo.finish_job(conn, job["id"], status=models.SUCCEEDED, summary="done")
    kinds = [event["kind"] for event in repo.list_events(conn, job["id"])]
    assert {"created", "claimed", "finished"} <= set(kinds)
