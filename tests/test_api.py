"""End-to-end tests through the HTTP API, including the ask/answer round trip."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from claudejobs import db, models
from claudejobs import repository as repo

REPO_ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.usefixtures("configured")


def create(client, **overrides) -> dict:
    payload = {"prompt": "fix the failing tests", "directory": str(REPO_ROOT),
               "source": "telegram", "source_user_id": "u1", "source_username": "akshat",
               "source_chat_id": "c1", "source_message_id": "m1"}
    payload.update(overrides)
    response = client.post("/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def claim_and_attach(job_id: int) -> str:
    """Do what the dispatcher does, and return the job's token."""
    token = repo.new_job_token()
    with db.connection() as conn:
        repo.claim_jobs(conn, worker_id="w1", worker_host="h", limit=5, lease_seconds=180)
        repo.attach_worker(conn, job_id, pid=123, session_id=str(uuid.uuid4()),
                           job_token=token, log_path=str(REPO_ROOT / "logs" / f"job-{job_id}.log"))
    return token


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def test_admin_routes_require_the_token(api_client):
    response = api_client.post("/jobs", json={"prompt": "x", "directory": str(REPO_ROOT)},
                               headers={"X-Auth-Token": ""})
    assert response.status_code == 401
    assert "X-Auth-Token" in response.json()["detail"]


def test_wrong_token_is_refused(api_client):
    response = api_client.get("/jobs", headers={"X-Auth-Token": "not-the-token"})
    assert response.status_code == 401


def test_health_needs_no_token(api_client):
    response = api_client.get("/health", headers={"X-Auth-Token": ""})
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


# --------------------------------------------------------------------------- #
# job lifecycle
# --------------------------------------------------------------------------- #
def test_create_validates_the_directory(api_client):
    response = api_client.post("/jobs", json={"prompt": "x", "directory": "Z:/nope"})
    assert response.status_code == 400
    assert "not a directory" in response.json()["detail"]


def test_create_rejects_an_unknown_permission_mode(api_client):
    response = api_client.post("/jobs", json={"prompt": "x", "directory": str(REPO_ROOT),
                                              "permission_mode": "yolo"})
    assert response.status_code == 422


def test_create_and_read_back(api_client):
    job = create(api_client, title="nightly")
    assert job["status"] == "queued"
    assert job["title"] == "nightly"
    assert "job_token_hash" not in job          # never leaves the process

    fetched = api_client.get(f"/jobs/{job['id']}").json()
    assert fetched["id"] == job["id"]
    assert fetched["open_question"] is None

    listed = api_client.get("/jobs", params={"status": "queued"}).json()
    assert [row["id"] for row in listed] == [job["id"]]


def test_unknown_job_is_404(api_client):
    assert api_client.get("/jobs/9999").status_code == 404
    assert api_client.get("/jobs", params={"status": "nonsense"}).status_code == 400


def test_edit_before_start_then_refused_after(api_client):
    job = create(api_client)
    edited = api_client.patch(f"/jobs/{job['id']}", json={"priority": 3, "title": "urgent"})
    assert edited.status_code == 200 and edited.json()["priority"] == 3

    claim_and_attach(job["id"])
    late = api_client.patch(f"/jobs/{job['id']}", json={"priority": 9})
    assert late.status_code == 409
    assert "only queued jobs can be edited" in late.json()["detail"]


def test_cancel_queued_is_immediate_and_running_is_cooperative(api_client):
    queued = create(api_client)
    response = api_client.post(f"/jobs/{queued['id']}/cancel", json={"reason": "not needed"})
    assert response.json()["status"] == models.CANCELLED
    assert api_client.post(f"/jobs/{queued['id']}/cancel", json={}).status_code == 409

    running = create(api_client)
    token = claim_and_attach(running["id"])
    response = api_client.post(f"/jobs/{running['id']}/cancel", json={"reason": "stop"})
    assert response.json()["status"] == models.RUNNING
    assert response.json()["cancel_requested"] is True

    beat = api_client.post(f"/jobs/{running['id']}/heartbeat", headers={"X-Job-Token": token})
    assert beat.json()["cancel_requested"] is True


def test_retry_only_after_a_job_finished(api_client):
    job = create(api_client)
    token = claim_and_attach(job["id"])
    assert api_client.post(f"/jobs/{job['id']}/retry", json={}).status_code == 409

    api_client.post(f"/jobs/{job['id']}/finish", json={"status": "failed", "error": "boom"},
                    headers={"X-Job-Token": token})
    assert api_client.post(f"/jobs/{job['id']}/retry", json={}).json()["status"] == models.QUEUED


# --------------------------------------------------------------------------- #
# job-token auth
# --------------------------------------------------------------------------- #
def test_job_token_unlocks_only_its_own_job(api_client):
    first = create(api_client)
    second = create(api_client)
    token = claim_and_attach(first["id"])

    assert api_client.get(f"/jobs/{first['id']}/self",
                          headers={"X-Job-Token": token}).status_code == 200
    assert api_client.get(f"/jobs/{second['id']}/self",
                          headers={"X-Job-Token": token}).status_code == 401
    assert api_client.get(f"/jobs/{first['id']}/self",
                          headers={"X-Job-Token": "guessed"}).status_code == 401
    assert api_client.get(f"/jobs/{first['id']}/self").status_code == 401


def test_self_describes_what_the_worker_must_launch(api_client):
    job = create(api_client, model="sonnet", permission_mode="acceptEdits")
    token = claim_and_attach(job["id"])
    described = api_client.get(f"/jobs/{job['id']}/self", headers={"X-Job-Token": token}).json()
    assert described["prompt"] == "fix the failing tests"
    assert described["model"] == "sonnet"
    assert described["permission_mode"] == "acceptEdits"
    assert described["session_id"]


# --------------------------------------------------------------------------- #
# the ask / answer round trip
# --------------------------------------------------------------------------- #
def test_ask_parks_the_job_and_queues_a_chat_message(api_client):
    job = create(api_client)
    token = claim_and_attach(job["id"])

    asked = api_client.post(f"/jobs/{job['id']}/ask", json={"question": "which branch?"},
                            headers={"X-Job-Token": token}).json()
    assert asked["delivered"] is True
    assert api_client.get(f"/jobs/{job['id']}").json()["status"] == models.WAITING_INPUT

    pending = api_client.post("/outbound/claim",
                              json={"channel": "telegram", "claimed_by": "bot"}).json()
    assert len(pending) == 1
    assert "which branch?" in pending[0]["body"]
    assert f"/reply {job['id']}" in pending[0]["body"]


def test_answer_reaches_the_waiting_job(api_client):
    job = create(api_client)
    token = claim_and_attach(job["id"])
    asked = api_client.post(f"/jobs/{job['id']}/ask", json={"question": "which branch?"},
                            headers={"X-Job-Token": token}).json()

    waiting = api_client.get(f"/jobs/{job['id']}/questions/{asked['question_id']}",
                             params={"wait": 0}, headers={"X-Job-Token": token}).json()
    assert waiting["status"] == "open" and waiting["timed_out_waiting"] is True

    routed = api_client.post("/replies", json={
        "channel": "telegram", "chat_id": "c1", "user_id": "u1", "username": "akshat",
        "body": "use main"}).json()
    assert routed["job_id"] == job["id"]

    answered = api_client.get(f"/jobs/{job['id']}/questions/{asked['question_id']}",
                              params={"wait": 0}, headers={"X-Job-Token": token}).json()
    assert answered["status"] == "answered"
    assert answered["answer"] == "use main"
    assert api_client.get(f"/jobs/{job['id']}").json()["status"] == models.RUNNING


def test_reply_routing_picks_the_right_job_when_several_are_waiting(api_client):
    """The case that matters: one person, two jobs, both asking."""
    jobs = []
    for index in range(2):
        job = create(api_client, prompt=f"job {index}")
        token = claim_and_attach(job["id"])
        asked = api_client.post(f"/jobs/{job['id']}/ask",
                                json={"question": f"question {index}?"},
                                headers={"X-Job-Token": token}).json()
        jobs.append((job, token, asked["question_id"]))

    # A bare answer is ambiguous and is refused with the waiting ids.
    ambiguous = api_client.post("/replies", json={
        "channel": "telegram", "chat_id": "c1", "user_id": "u1", "body": "main"})
    assert ambiguous.status_code == 409
    assert f"#{jobs[0][0]['id']}" in ambiguous.json()["detail"]
    assert f"#{jobs[1][0]['id']}" in ambiguous.json()["detail"]

    # Naming the job works...
    second = jobs[1][0]
    assert api_client.post("/replies", json={
        "channel": "telegram", "chat_id": "c1", "user_id": "u1",
        "job_id": second["id"], "body": "answer for the second"}).status_code == 200

    # ...and so does replying to the delivered question message.
    delivered = api_client.post("/outbound/claim",
                                json={"channel": "telegram", "claimed_by": "bot"}).json()
    first_message = next(row for row in delivered if row["job_id"] == jobs[0][0]["id"])
    api_client.post(f"/outbound/{first_message['id']}/sent",
                    json={"provider_message_id": "9001"})

    routed = api_client.post("/replies", json={
        "channel": "telegram", "chat_id": "c1", "user_id": "u1",
        "reply_to_message_id": "9001", "body": "answer for the first"}).json()
    assert routed["job_id"] == jobs[0][0]["id"]

    for job, token, question_id in jobs:
        state = api_client.get(f"/jobs/{job['id']}/questions/{question_id}",
                               params={"wait": 0}, headers={"X-Job-Token": token}).json()
        assert state["status"] == "answered"


def test_answering_when_nothing_is_waiting(api_client):
    response = api_client.post("/replies", json={
        "channel": "telegram", "chat_id": "c1", "user_id": "u1", "body": "hello?"})
    assert response.status_code == 404
    assert "not waiting" in response.json()["detail"] or "none of your jobs" in response.json()["detail"]


def test_answer_route_refuses_a_closed_question(api_client):
    job = create(api_client)
    token = claim_and_attach(job["id"])
    api_client.post(f"/jobs/{job['id']}/ask", json={"question": "which branch?"},
                    headers={"X-Job-Token": token})
    assert api_client.post(f"/jobs/{job['id']}/answer", json={"body": "main"}).status_code == 200
    assert api_client.post(f"/jobs/{job['id']}/answer", json={"body": "again"}).status_code == 404


# --------------------------------------------------------------------------- #
# progress, notes and finishing
# --------------------------------------------------------------------------- #
def test_progress_and_notify_are_recorded(api_client):
    job = create(api_client)
    token = claim_and_attach(job["id"])
    api_client.post(f"/jobs/{job['id']}/progress", json={"note": "tests are green"},
                    headers={"X-Job-Token": token})
    notified = api_client.post(f"/jobs/{job['id']}/notify", json={"message": "heads up"},
                               headers={"X-Job-Token": token}).json()
    assert notified["delivered"] is True

    messages = api_client.get(f"/jobs/{job['id']}/messages").json()
    assert {m["body"] for m in messages} >= {"tests are green", "heads up"}


def test_finish_notifies_the_poster_and_is_final(api_client):
    job = create(api_client)
    token = claim_and_attach(job["id"])
    finished = api_client.post(f"/jobs/{job['id']}/finish",
                               json={"status": "succeeded", "summary": "upgraded the client",
                                     "exit_code": 0},
                               headers={"X-Job-Token": token}).json()
    assert finished["status"] == models.SUCCEEDED

    queued = api_client.post("/outbound/claim",
                             json={"channel": "telegram", "claimed_by": "bot"}).json()
    assert any("upgraded the client" in row["body"] for row in queued)

    # A late report from the wrapper must not overwrite it.
    late = api_client.post(f"/jobs/{job['id']}/finish",
                           json={"status": "failed", "exit_code": 3},
                           headers={"X-Job-Token": token}).json()
    assert late["status"] == models.SUCCEEDED


def test_cannot_ask_after_the_job_finished(api_client):
    job = create(api_client)
    token = claim_and_attach(job["id"])
    api_client.post(f"/jobs/{job['id']}/finish", json={"status": "succeeded"},
                    headers={"X-Job-Token": token})
    response = api_client.post(f"/jobs/{job['id']}/ask", json={"question": "too late?"},
                               headers={"X-Job-Token": token})
    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# ops
# --------------------------------------------------------------------------- #
def test_stats_and_events(api_client):
    job = create(api_client)
    claim_and_attach(job["id"])
    stats = api_client.get("/stats").json()
    assert stats["jobs_by_status"]["running"] == 1
    assert stats["max_concurrent_jobs"] == 2

    events = api_client.get(f"/jobs/{job['id']}/events").json()
    assert {event["kind"] for event in events} >= {"created", "claimed"}


def test_log_endpoint_is_honest_when_there_is_no_file(api_client):
    job = create(api_client)
    result = api_client.get(f"/jobs/{job['id']}/log").json()
    assert result["lines"] == []
    assert "no log file yet" in result["detail"]
