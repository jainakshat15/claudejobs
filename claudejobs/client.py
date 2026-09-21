"""HTTP client for the job API.

Deliberately synchronous: the bots' shared command layer, jobctl and the
operator CLI all use it, and the async bot (Telegram) calls it through
``asyncio.to_thread`` rather than forcing every caller to be async.

Two clients:
  AdminClient — X-Auth-Token; used by bots and the CLI.
  JobClient   — X-Job-Token; used from inside a running job (jobctl, run_job).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


class ApiError(RuntimeError):
    """The API answered with an error, or could not be reached at all."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_connection_error(self) -> bool:
        return self.status_code is None


class _BaseClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = DEFAULT_TIMEOUT):
        settings = get_settings()
        self.base_url = (base_url or settings.api_base_url).rstrip("/")
        self._timeout = timeout
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def request(self, method: str, path: str, *, json: Any = None,
                params: Mapping[str, Any] | None = None,
                timeout: float | None = None) -> Any:
        try:
            response = self._client.request(
                method, path, json=json, params=params,
                headers=self._headers(), timeout=timeout or self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ApiError(
                f"cannot reach the job API at {self.base_url}: {exc}. "
                "Is it running? Start it with: claudejobs api"
            ) from exc

        if response.status_code >= 400:
            raise ApiError(_error_detail(response), status_code=response.status_code)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300]}"
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if isinstance(detail, list):  # FastAPI validation errors
        parts = [f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}"
                 for item in detail]
        detail = "; ".join(parts)
    return f"HTTP {response.status_code}: {detail}"


# --------------------------------------------------------------------------- #
# admin / bot client
# --------------------------------------------------------------------------- #
class AdminClient(_BaseClient):
    """Full access to the queue. Used by the bots and the operator CLI."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 *, timeout: float = DEFAULT_TIMEOUT):
        super().__init__(base_url, timeout=timeout)
        self.token = token if token is not None else get_settings().api_token

    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self.token} if self.token else {}

    # jobs ------------------------------------------------------------- #
    def create_job(self, **payload: Any) -> dict:
        return self.request("POST", "/jobs", json=payload)

    def list_jobs(self, **params: Any) -> list[dict]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.request("GET", "/jobs", params=clean)

    def get_job(self, job_id: int) -> dict:
        return self.request("GET", f"/jobs/{job_id}")

    def update_job(self, job_id: int, **payload: Any) -> dict:
        return self.request("PATCH", f"/jobs/{job_id}",
                            json={k: v for k, v in payload.items() if v is not None})

    def cancel_job(self, job_id: int, *, reason: str = "cancelled by poster",
                   actor: str | None = None) -> dict:
        return self.request("POST", f"/jobs/{job_id}/cancel",
                            json={"reason": reason, "actor": actor})

    def retry_job(self, job_id: int, *, actor: str | None = None) -> dict:
        return self.request("POST", f"/jobs/{job_id}/retry", json={"actor": actor})

    def answer(self, job_id: int, *, body: str, answered_by: str | None = None,
               question_id: int | None = None) -> dict:
        return self.request("POST", f"/jobs/{job_id}/answer",
                            json={"body": body, "answered_by": answered_by,
                                  "question_id": question_id})

    def messages(self, job_id: int, limit: int = 20) -> list[dict]:
        return self.request("GET", f"/jobs/{job_id}/messages", params={"limit": limit})

    def events(self, job_id: int, limit: int = 20) -> list[dict]:
        return self.request("GET", f"/jobs/{job_id}/events", params={"limit": limit})

    def job_log(self, job_id: int, tail: int = 40) -> dict:
        return self.request("GET", f"/jobs/{job_id}/log", params={"tail": tail})

    def stats(self) -> dict:
        return self.request("GET", "/stats")

    def health(self) -> dict:
        return self.request("GET", "/health")

    # outbound delivery (bots) ----------------------------------------- #
    def claim_outbound(self, *, channel: str, claimed_by: str, limit: int = 10) -> list[dict]:
        return self.request("POST", "/outbound/claim",
                            json={"channel": channel, "claimed_by": claimed_by, "limit": limit})

    def outbound_sent(self, outbound_id: int, *, provider_message_id: str | None,
                      provider_thread_id: str | None = None,
                      chat_id: str | None = None) -> dict:
        return self.request("POST", f"/outbound/{outbound_id}/sent",
                            json={"provider_message_id": provider_message_id,
                                  "provider_thread_id": provider_thread_id,
                                  "chat_id": chat_id})

    def outbound_failed(self, outbound_id: int, *, error: str) -> dict:
        return self.request("POST", f"/outbound/{outbound_id}/failed", json={"error": error})

    # reply routing ----------------------------------------------------- #
    def route_answer(self, *, channel: str, chat_id: str, user_id: str, body: str,
                     reply_to_message_id: str | None = None,
                     thread_id: str | None = None,
                     job_id: int | None = None,
                     username: str | None = None) -> dict:
        """Deliver a human's reply to whichever job is waiting for it."""
        return self.request("POST", "/replies", json={
            "channel": channel, "chat_id": str(chat_id), "user_id": str(user_id),
            "body": body, "reply_to_message_id": reply_to_message_id,
            "thread_id": thread_id, "job_id": job_id, "username": username,
        })


# --------------------------------------------------------------------------- #
# in-job client
# --------------------------------------------------------------------------- #
class JobClient(_BaseClient):
    """Scoped to one job, authenticated with that job's own token."""

    def __init__(self, job_id: int, token: str, base_url: str | None = None,
                 *, timeout: float = DEFAULT_TIMEOUT):
        super().__init__(base_url, timeout=timeout)
        self.job_id = job_id
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"X-Job-Token": self.token}

    def describe(self) -> dict:
        """The job's own row — how run_job learns what to launch."""
        return self.request("GET", f"/jobs/{self.job_id}/self")

    def heartbeat(self) -> dict:
        """Extend the lease; the reply says whether a cancel was requested."""
        return self.request("POST", f"/jobs/{self.job_id}/heartbeat", timeout=15.0)

    def progress(self, note: str) -> dict:
        return self.request("POST", f"/jobs/{self.job_id}/progress", json={"note": note})

    def notify(self, message: str) -> dict:
        return self.request("POST", f"/jobs/{self.job_id}/notify", json={"message": message})

    def ask(self, question: str) -> dict:
        return self.request("POST", f"/jobs/{self.job_id}/ask", json={"question": question})

    def poll_answer(self, question_id: int, *, wait_seconds: int = 25) -> dict:
        """Long-poll one question. Returns as soon as it is answered or closed."""
        return self.request(
            "GET", f"/jobs/{self.job_id}/questions/{question_id}",
            params={"wait": wait_seconds},
            timeout=wait_seconds + 15.0,
        )

    def finish(self, *, status: str, summary: str | None = None,
               error: str | None = None, exit_code: int | None = None) -> dict:
        return self.request("POST", f"/jobs/{self.job_id}/finish", json={
            "status": status, "summary": summary, "error": error, "exit_code": exit_code,
        })
