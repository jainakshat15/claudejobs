# claudejobs operations runbook

Everything an on-call operator needs: what runs, how to drive it, how a job moves through the
system, and what to do when it does not. Paths and defaults below come from `.env.example` and
`claudejobs/config.py`; real environment variables always beat `.env`.

## Processes

Four long-running processes, all started the same way from the repo root.

| Process | Command | What it does | Needs |
| --- | --- | --- | --- |
| API | `python -m claudejobs api` | Serves the HTTP API on `API_HOST:API_PORT` (default `127.0.0.1:8000`). Writes the request transcript. Every state change in the system goes through it | `DATABASE_URL`, `API_TOKEN` |
| Dispatcher | `python -m claudejobs dispatcher` | Polls the queue, sweeps dead leases/timeouts/expired questions, claims jobs and opens a terminal per job | `DATABASE_URL` (direct, not via the API) |
| Telegram bot | `python -m claudejobs telegram` | Long-polls Telegram for commands, and drains the outbound queue for `channel = 'telegram'` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS` |
| Slack bot | `python -m claudejobs slack` | Socket Mode client for commands, and drains the outbound queue for `channel = 'slack'` | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_ALLOWED_USERS` |

```bash
python -m claudejobs api
python -m claudejobs dispatcher
python -m claudejobs telegram
python -m claudejobs slack
```

`all` runs every **configured** service as a child process in one window — always `api` and
`dispatcher`, plus `telegram` if `TELEGRAM_BOT_TOKEN` is set and `slack` if both Slack tokens
are set. It gives the API a 1.5 s head start, then 0.3 s between the rest. Ctrl+C stops all of
them; if any child exits on its own, the parent stops the others and returns that exit code.

```bash
python -m claudejobs all
```

The API and dispatcher are independent: the dispatcher talks to Postgres directly, so it keeps
claiming and launching jobs even while the API is down. The jobs it launches, however, cannot
report back until the API returns.

Both bots refuse to start with an empty allowlist. That is deliberate — see
[Security notes](#security-notes).

## Everyday operator commands

All of these are `python -m claudejobs <command>`. The `submit`/`jobs`/`status`/`cancel`/
`retry`/`answer`/`stats` commands go through the HTTP API with `API_TOKEN`, so the API must be
running; `selfcheck`, `migrate` and `secret` do not need it.

```bash
# Check config, database, schema, Claude CLI, terminal, log dir, API, bots.
python -m claudejobs selfcheck

# Schema.
python -m claudejobs migrate status
python -m claudejobs migrate up
python -m claudejobs migrate up --allow-drift    # accept edited, already-applied migrations

# Queue a job from the shell (source is recorded as "cli").
python -m claudejobs submit "Add retry handling to the payments client" --dir D:\work\web
python -m claudejobs submit "Run the nightly docs sync" --dir D:\work\docs \
    --title "docs sync" --model sonnet --mode acceptEdits --priority 10 --timeout 60 --user akshat

# Look around.
python -m claudejobs jobs
python -m claudejobs jobs --status active --limit 50
python -m claudejobs jobs --status failed,timed_out
python -m claudejobs status 42
python -m claudejobs stats

# Intervene.
python -m claudejobs cancel 42 --reason "wrong branch"
python -m claudejobs retry 42
python -m claudejobs answer 42 "exponential, cap at 30s"

# Generate a value for API_TOKEN in .env.
python -m claudejobs secret
```

`selfcheck` prints `PASS` / `WARN` / `FAIL` per check and exits 1 if anything fatal failed. The
API check, the two bot checks and (on Linux/macOS) the terminal check are warnings only. A
missing `.env`, an unset `API_TOKEN`, an unreachable database, pending migrations, a missing
`claude` CLI, an unwritable `LOG_DIR` — and on Windows, no `wt.exe` or `cmd` — are failures.

`cancel` on a queued job finishes it immediately. On a running job it only *requests* the
cancel; the command prints `(cancel requested)` and the worker acts on its next heartbeat.

`retry` only works on a finished job and raises `max_attempts` to at least `attempts + 1`, so a
job that used up its attempts gets one more.

## How a job flows through the system

1. **Queued.** `POST /jobs` (from the CLI, a bot or any HTTP client) resolves and validates the
   directory against `ALLOWED_ROOTS`, inserts a `jobs` row with `status = 'queued'`, and records
   a `created` event.
2. **Claimed.** The dispatcher's next tick claims it (see below), flipping it to `running`,
   incrementing `attempts`, and setting `lease_expires_at = now() + JOB_LEASE_SECONDS`.
3. **Launched.** The dispatcher mints a job token, generates a session id, stores
   `sha256(token)` in `jobs.job_token_hash` **before** spawning anything, writes a per-job launch
   script into the OS temp directory (owner-only), and opens a terminal that runs
   `python -m claudejobs.run_job` with `CLAUDEJOBS_JOB_ID`, `CLAUDEJOBS_JOB_TOKEN` and
   `CLAUDEJOBS_API_URL` in its environment.
4. **Running.** `run_job` fetches `GET /jobs/{id}/self`, starts a heartbeat thread, and runs
   `claude` with the job's prompt plus the queue instructions.
5. **Talking back.** Claude uses `jobctl` — `progress`, `notify`, `ask`, `done`, `fail`,
   `status` — all authenticated with that one job token. `ask` parks the job in
   `waiting_input` until a human answers.
6. **Finished.** Whoever reports first wins: `jobctl done`/`fail`, or `run_job` from the exit
   code. The API records the terminal status, cancels any open question, and queues a summary
   message to the poster's chat if the job came from one.

### What the dispatcher does on each tick

A tick is `_sweep()` then `_dispatch()`, repeated every `POLL_INTERVAL_SECONDS` (default 5).
On the first tick and roughly every hour after (every 720 ticks), it also deletes launch scripts
older than 24 hours. A failing tick never kills the dispatcher: a `ConfigError` backs off for
`min(30, poll * 5)` seconds, any other exception for `min(30, poll * 2)`, and the next tick
re-reads everything from the database.

`_sweep()`, in order:

1. **Expire leases.** Active jobs with `lease_expires_at < now()`. Open questions are cancelled.
   If `attempts < max_attempts` and the job was not timing out and had no cancel requested, it
   goes back to `queued` with `error = 'worker stopped responding; requeued'` and its worker
   fields, token hash and session id cleared. Otherwise it goes terminal: `timed_out` if
   `cancel_reason = 'timeout'`, `cancelled` if a cancel was requested, else `failed`. Either way
   a chat notice is queued.
2. **Mark timeouts.** Active jobs started more than `COALESCE(timeout_minutes,
   JOB_TIMEOUT_MINUTES)` minutes ago get `cancel_requested = true, cancel_reason = 'timeout'`.
   This asks; it does not kill. The worker sees it on its next heartbeat.
3. **Expire questions.** Open questions older than `QUESTION_TIMEOUT_MINUTES` are marked
   `expired` and their job is **failed** with `no answer within N minutes: <question>`, which
   frees the slot. Skipped entirely when the setting is 0.
4. **Requeue stale outbound.** `outbound_messages` rows still `claimed` more than 300 seconds
   after `claimed_at` go back to `pending`.

`_dispatch()`:

- counts this worker's `running` + `waiting_input` jobs;
- `capacity = MAX_CONCURRENT_JOBS - active`; returns immediately if that is `<= 0`;
- claims up to `capacity` rows where `status = 'queued' AND cancel_requested = false AND
  scheduled_at <= now()`, ordered by `priority ASC, scheduled_at ASC, id ASC`, using
  `FOR UPDATE SKIP LOCKED`;
- launches each one. A launch failure (`LaunchError`/`OSError`) finishes the job as `failed`
  with `could not launch: ...` and notifies the poster — it is not retried by the dispatcher.

### What run_job does

- **Heartbeat.** A daemon thread posts `POST /jobs/{id}/heartbeat` every
  `max(5, HEARTBEAT_INTERVAL_SECONDS)` seconds. Each success pushes `lease_expires_at` out to
  `now() + JOB_LEASE_SECONDS`. Failures are counted and logged as
  `heartbeat failed (N in a row)` but the thread keeps trying — it never gives up on its own.
- **Cancel handling.** The heartbeat reply carries `cancel_requested` and `cancel_reason`. On
  the first `true`, `run_job` logs it, **stops the heartbeat thread**, and kills Claude: on
  Windows `taskkill /F /T /PID` (Claude runs node with children), elsewhere `SIGTERM`, then a
  hard kill after a 15 second grace period.
- **Finish reporting.** Cancelled → `cancelled`. Exit code 0 → `succeeded` with the summary
  *"session ended without calling `jobctl done`; treating a clean exit as success"*. Any other
  exit code → `failed` with `Claude exited with code N`. If Claude already reported through
  `jobctl`, the API keeps that first result and records a `finish_ignored` event. If the final
  report cannot be delivered at all, `run_job` logs it and exits — the dispatcher reaps the job
  when its lease expires.

### Leases

| Setting | Default | Role |
| --- | --- | --- |
| `HEARTBEAT_INTERVAL_SECONDS` | 30 | How often the worker says it is alive |
| `JOB_LEASE_SECONDS` | 180 | How long that proof is good for |

Each heartbeat sets `lease_expires_at = now() + JOB_LEASE_SECONDS`. The dispatcher reaps any
active job whose lease is in the past. So a healthy job renews six times per lease at the
defaults, and a dead one is noticed between `JOB_LEASE_SECONDS` and
`JOB_LEASE_SECONDS + POLL_INTERVAL_SECONDS` after its last heartbeat.

Config **refuses to load** if `JOB_LEASE_SECONDS <= HEARTBEAT_INTERVAL_SECONDS`; aim for at
least 3x. Raise the lease (not lower the heartbeat) if workers get reaped while healthy — a
laptop that sleeps, or a saturated machine, can miss several heartbeats in a row.

One subtlety worth knowing: once the worker sees a cancel it stops heartbeating. If killing
Claude and reporting the outcome takes longer than the remaining lease, the sweep finalises the
job first — and `finish_job` then ignores the worker's later report.

## Capacity and tuning

| Setting | Default | Effect |
| --- | --- | --- |
| `MAX_CONCURRENT_JOBS` | 2 | Slots **per worker**. Counts `running` **and** `waiting_input` |
| `POLL_INTERVAL_SECONDS` | 5 | Dispatcher tick interval |
| `JOB_LEASE_SECONDS` / `HEARTBEAT_INTERVAL_SECONDS` | 180 / 30 | How fast a dead worker is noticed |
| `JOB_TIMEOUT_MINUTES` | 240 | Default wall-clock limit; 0 disables. Per-job `timeout_minutes` overrides it |
| `QUESTION_TIMEOUT_MINUTES` | 180 | How long a job may sit in `waiting_input`; 0 means forever |
| `DEFAULT_MAX_ATTEMPTS` | 1 | Attempts for new jobs. 1 means no automatic retry |
| `DB_POOL_MAX` | 5 | Per-process pool ceiling — the API, dispatcher and bots each open their own |
| `OUTBOUND_POLL_SECONDS` | 3 | How often each bot drains the outbound queue |

**`MAX_CONCURRENT_JOBS` counts jobs waiting on a human.** A job parked in `waiting_input` holds
its slot until somebody answers or `QUESTION_TIMEOUT_MINUTES` expires. With the default of 2, two
unanswered questions stall the whole queue. If jobs ask questions often, raise
`MAX_CONCURRENT_JOBS`, or lower `QUESTION_TIMEOUT_MINUTES` so stuck jobs release their slot
sooner.

The limit is per worker: each dispatcher counts only jobs whose `worker_id` matches its own
(`WORKER_ID`, defaulting to the hostname), so two machines with `MAX_CONCURRENT_JOBS=2` run four
jobs between them. Give each machine a distinct `WORKER_ID`.

Every slot is a real terminal running a real Claude session — raise it for CPU/RAM/API headroom,
not for queue depth.

Lower `POLL_INTERVAL_SECONDS` for a snappier pickup (it is also the sweep interval, so dead
workers and timeouts are noticed sooner); raise it to cut idle database traffic. The whole tick
is a handful of indexed queries, so 1–2 seconds is fine on a local Postgres and wasteful on a
metered hosted one.

## Monitoring

### Logs

| Where | What |
| --- | --- |
| `LOG_DIR/job-<id>.log` (default `logs/jobs/job-42.log`) | Per-job wrapper events from `run_job`: start, Claude's pid, cancels, exit code, finish-report failures. In `TERMINAL_MODE=headless` Claude's own output lands here too. Also served by `GET /jobs/{id}/log` and the bots' `/log` command |
| `REQUEST_LOG_FILE` (default `logs/requests.md`) | Markdown transcript of every HTTP request and response body, with status. Headers are never written, so tokens stay out. Rotates at `REQUEST_LOG_MAX_MB` (default 20) to `requests-YYYYMMDD-HHMMSS.md`; bodies over 4000 chars are truncated |
| Console of each service | `setup_logging` at `LOG_LEVEL` (default INFO). The dispatcher logs every claim, launch, sweep action and error; the API logs job creation, progress and finishes; the bots log delivery failures. `httpx`/`httpcore` are pinned to WARNING |
| Each job's terminal window | Claude's live output. `KEEP_TERMINAL_OPEN=true` leaves it up after the job ends |

### Queue state

```bash
python -m claudejobs stats
curl -s http://127.0.0.1:8000/stats -H "X-Auth-Token: $API_TOKEN"
curl -s http://127.0.0.1:8000/health
```

`/stats` gives per-status counts (from the `job_queue_stats` view), `open_questions`,
`pending_outbound` (`pending` + `claimed`), `active_workers` and the serving process's
`max_concurrent_jobs` / `worker_id`. `/health` needs no auth and reports the database and
whether the `claude` CLI is resolvable.

### Useful SQL

The `job_queue_stats` view is `status, jobs, oldest_created_at, avg_runtime_seconds` grouped by
status:

```sql
SELECT status, jobs, oldest_created_at, round(avg_runtime_seconds) AS avg_seconds
FROM job_queue_stats
ORDER BY jobs DESC;
```

What is running right now, and how stale each lease is:

```sql
SELECT id, status, worker_id, worker_pid, attempts || '/' || max_attempts AS attempt,
       now() - last_heartbeat_at AS since_heartbeat,
       lease_expires_at < now()  AS lease_expired,
       cancel_requested, left(title, 40) AS title
FROM jobs
WHERE status IN ('running', 'waiting_input')
ORDER BY id;
```

Jobs parked on a human, oldest first:

```sql
SELECT j.id, j.status, j.source, j.source_username,
       now() - m.created_at AS waiting_for,
       left(m.body, 80) AS question
FROM job_messages m
JOIN jobs j ON j.id = m.job_id
WHERE m.kind = 'question' AND m.status = 'open'
ORDER BY m.created_at;
```

Undelivered chat messages and why:

```sql
SELECT id, job_id, channel, chat_id, kind, status, attempts, claimed_by, last_error
FROM outbound_messages
WHERE status IN ('pending', 'claimed', 'failed')
ORDER BY id;
```

Recent failures with their last few events:

```sql
SELECT j.id, j.status, j.finished_at, j.exit_code, left(j.error, 120) AS error
FROM jobs j
WHERE j.status IN ('failed', 'timed_out')
  AND j.finished_at > now() - interval '24 hours'
ORDER BY j.finished_at DESC;

SELECT created_at, kind, from_status, to_status, actor, left(detail, 100) AS detail
FROM job_events
WHERE job_id = 42
ORDER BY id;
```

How often the dispatcher is reaping workers (a rising count means something is wrong with the
machine, not the jobs):

```sql
SELECT date_trunc('hour', created_at) AS hour, count(*)
FROM job_events
WHERE kind = 'lease_expired' AND created_at > now() - interval '7 days'
GROUP BY 1 ORDER BY 1 DESC;
```

## Failure modes and recovery

### 1. Worker stopped responding (lease expiry)

**Symptom.** `dispatcher: job #42 lost its worker -> queued` (or `-> failed`) in the console; a
`lease_expired` event on the job; `error = 'worker stopped responding; requeued'` or
`'worker stopped responding (terminal closed, machine slept, or Claude crashed)'`. The poster
gets a chat notice.

**Cause.** No successful heartbeat for `JOB_LEASE_SECONDS`. The terminal was closed, the machine
slept, Claude crashed hard, or the API was unreachable for longer than the lease.

**Fix.** If `attempts < max_attempts` the dispatcher already requeued it with a fresh token — do
nothing. If it went terminal, look at `logs/jobs/job-42.log` for how far Claude got, then
`python -m claudejobs retry 42`. If this is recurring, either the machine is sleeping (disable
sleep) or the lease is too tight for the load — raise `JOB_LEASE_SECONDS`, or raise
`DEFAULT_MAX_ATTEMPTS` so requeues happen automatically.

### 2. Job stuck in waiting_input

**Symptom.** A job sits in `waiting_input` holding a slot; `/stats` shows `open_questions > 0`;
nothing else starts.

**Cause.** Claude called `jobctl ask` and nobody answered. The job stays parked until an answer
arrives or `QUESTION_TIMEOUT_MINUTES` (default 180) passes, at which point the dispatcher marks
the question `expired` and **fails** the job with `no answer within 180 minutes: <question>`.
With the setting at 0 it waits forever.

**Fix.** Read the question and answer it:

```bash
python -m claudejobs status 42
python -m claudejobs answer 42 "exponential, cap at 30s"
```

Or cancel it to free the slot: `python -m claudejobs cancel 42 --reason "no longer needed"`.
If it already expired, `retry 42`. To stop this stalling the queue, raise
`MAX_CONCURRENT_JOBS` or lower `QUESTION_TIMEOUT_MINUTES`.

### 3. Outbound messages not delivered

**Symptom.** `/stats` shows a rising `pending_outbound`; nothing arrives in Telegram/Slack; jobs
still run normally.

**Cause.** The bot for that channel is not running, or it died mid-delivery leaving rows
`claimed`, or the provider rejected the send. Note the API never talks to Telegram/Slack
directly — it only enqueues.

**Fix.** Start the bot (`python -m claudejobs telegram` / `slack`). Rows a dead bot left
`claimed` are returned to `pending` automatically after **300 seconds**, by the dispatcher's
sweep and by every `POST /outbound/claim`, so a restarted bot picks them up without
intervention. For genuine send errors, check `outbound_messages.last_error`: a row goes back to
`pending` for each failure and only becomes `failed` once `attempts >= max_attempts` (default
5). Re-send a dead row by hand:

```sql
UPDATE outbound_messages
SET status = 'pending', attempts = 0, claimed_by = NULL, claimed_at = NULL
WHERE id = 108;
```

If the bot refused to start, it is usually the allowlist — `TELEGRAM_ALLOWED_USERS` or
`SLACK_ALLOWED_USERS` empty is a hard refusal, not a warning.

### 4. API down while a job is running

**Symptom.** `heartbeat failed (N in a row): cannot reach the job API at http://127.0.0.1:8000`
in `logs/jobs/job-42.log`. `jobctl` prints `cannot reach the job API ... Is it running? Start it
with: claudejobs api`.

**Cause.** The API process stopped or was restarted. Claude keeps running — it is a separate
process — but nothing it reports can be recorded.

**Fix.** Restart the API. The heartbeat thread never gives up, so it reconnects on its own and
the lease is extended again. `jobctl ask` survives too: it retries the long-poll up to 12 times
with a 5 second backoff before returning exit code 4. What matters is the clock — if the API is
down for longer than `JOB_LEASE_SECONDS` (default 180 s), the dispatcher reaps the job even
though Claude is still working, and the eventual finish report is ignored as
`finish_ignored`. Restart within the lease window; if you cannot, expect to `retry` the job.

### 5. Database unreachable at startup

**Symptom.** The API logs `database unavailable at startup: ...` but keeps serving; every route
that touches the database answers **503** with the `ConfigError` text, while `GET /health`
returns 200 with `"ok": false` and the error in `database`. The dispatcher logs
`configuration problem: Could not connect to Postgres: ...` each tick and backs off. The bots
and CLI surface the same 503.

**Cause.** `DATABASE_URL` missing or wrong, the hosted database is asleep or down, or TLS is not
requested (hosted Postgres usually needs `?sslmode=require`).

**Fix.** Fix `.env`, then `python -m claudejobs selfcheck` — it reports the database, the schema
and the API in one pass. The API does not need restarting for the connection itself (the pool is
opened lazily on the next request), but it does cache settings per process, so **restart every
service after editing `.env`**. If `migrate status` shows pending versions, run
`python -m claudejobs migrate up`.

### 6. Terminal window closed manually

**Symptom.** The window vanishes; the job stays `running` for up to `JOB_LEASE_SECONDS`, then
turns up in the logs as a lease expiry.

**Cause.** Closing the window kills `run_job` and its heartbeat with no chance to report. `run_job`
handles Ctrl+C (it kills Claude and reports `cancelled`), but a closed window is not Ctrl+C.

**Fix.** Nothing immediate — the lease sweep handles it exactly as in failure mode 1: requeued if
attempts remain, otherwise `failed`. Note that Claude itself may survive a closed window; check
for stray processes before retrying, or the retry will fight the original. Prefer
`python -m claudejobs cancel <id>` over closing the window: that is cooperative, kills the whole
Claude process tree, and records a proper outcome.

### 7. Dispatcher restarted while jobs run

**Symptom.** On startup: `N job(s) from a previous run are still active: #42, #43` followed by
`they keep their slot until they finish or their lease expires`.

**Cause.** Normal. Running jobs live in their own terminals and report to the API, not to the
dispatcher. The dispatcher holds no in-memory state about them.

**Fix.** Nothing. Adopted jobs keep their `worker_id`, so they still count against this worker's
`MAX_CONCURRENT_JOBS` and are still swept for lease expiry and timeouts. A restart is safe at any
point, including mid-launch: a job whose row was claimed but whose terminal never started simply
fails its lease and is requeued.

### 8. Duplicate dispatchers

**Symptom.** Two dispatcher processes (a forgotten window, or `all` plus a standalone
dispatcher). Feared: the same job running twice.

**Cause.** It cannot happen. `claim_jobs` selects with `FOR UPDATE SKIP LOCKED`, so a second
claimer skips locked rows rather than blocking or duplicating, and the `UPDATE` only matches rows
still `queued`.

**Fix.** No corruption to repair — but do stop the extra process, because capacity is counted per
`worker_id`. Two dispatchers sharing one `WORKER_ID` each see the same active count and will
launch up to `MAX_CONCURRENT_JOBS` **each**, overshooting the machine's limit. One dispatcher per
`WORKER_ID`; give a second machine its own.

## Backup and data retention

| Table | Back up | Why |
| --- | --- | --- |
| `jobs` | **yes** | The queue itself: prompts, directories, outcomes, summaries |
| `job_events` | **yes** | The audit trail — who did what to which job, and when |
| `job_messages` | **yes** | Every question, answer and progress note |
| `outbound_messages` | optional | Transient delivery queue; keep it if you need to prove a message was sent (`provider_message_id`, `sent_at`) |
| `schema_migrations` | yes | Versions and checksums; without it `migrate up` re-applies everything |

A plain dump is enough — there is nothing outside Postgres except log files:

```bash
pg_dump "$DATABASE_URL" -t jobs -t job_events -t job_messages -t outbound_messages \
        -t schema_migrations -Fc -f claudejobs-$(date +%Y%m%d).dump
```

Log files (`LOG_DIR`, `REQUEST_LOG_FILE`) are worth keeping for incident review but are not
needed to restore service. Job tokens are never in a backup in usable form — only their SHA-256
hashes are stored, and they are cleared when a job finishes or is retried.

**Nothing prunes old rows automatically.** No process deletes jobs, events, messages or outbound
rows; the only automatic cleanup in the system is launch scripts older than 24 hours in the temp
directory, and request-log rotation at `REQUEST_LOG_MAX_MB`. Trim terminal jobs on your own
schedule — `job_events`, `job_messages` and `outbound_messages` all reference `jobs` with
`ON DELETE CASCADE`, so deleting the job removes its history in one statement:

```sql
-- Drop finished jobs older than 90 days (cascades to events, messages, outbound rows).
DELETE FROM jobs
WHERE status IN ('succeeded', 'failed', 'cancelled', 'timed_out')
  AND finished_at < now() - interval '90 days';
```

Check what you are about to remove first:

```sql
SELECT status, count(*), min(finished_at), max(finished_at)
FROM jobs
WHERE status IN ('succeeded', 'failed', 'cancelled', 'timed_out')
  AND finished_at < now() - interval '90 days'
GROUP BY status;
```

## Security notes

**The API starts processes on this machine.** Anything that can authenticate to it can run
Claude Code, with the permission mode below, in any allowed directory. Treat `API_TOKEN` as a
remote-code-execution credential.

- **Bind address.** `API_HOST` defaults to `127.0.0.1`, which keeps the API private to the
  machine. Binding anything else logs a warning at startup (`this API starts processes on this
  machine. Only bind a public interface behind a firewall or VPN.`) and is otherwise unprotected
  — there is no TLS, no rate limiting and no per-user accounts, just the one shared token.
- **`API_TOKEN` fails closed.** If it is unset, every admin route returns 503 instead of running
  open. `selfcheck` also rejects the `change-me...` placeholder. Generate one with
  `python -m claudejobs secret`.
- **Bot allowlists.** `TELEGRAM_ALLOWED_USERS` (numeric ids) and `SLACK_ALLOWED_USERS` (member
  ids) gate every non-public command. An empty list is a refusal to start, not a warning —
  *"Refusing to start a bot that anyone could use to run commands on this machine."* Only
  `/help`, `/start` and `/whoami` are open; use `/whoami` to learn an id before adding it.
- **`ALLOWED_ROOTS`.** Jobs may only run inside these directories (`;`-separated on Windows,
  `:` elsewhere). The API resolves the requested directory and rejects anything outside them with
  403, on create and on edit. **Empty means any directory on the machine is allowed** — set it.
- **Per-job tokens.** Each job gets a fresh `token_urlsafe(32)`; only `sha256(token)` is stored,
  the plaintext is passed through a per-job launch script in the temp directory (owner-only on
  POSIX) rather than on a command line where other users could read it from the process list, and
  it unlocks exactly one job. Retrying a job or requeuing it after a lease expiry clears the hash,
  which immediately invalidates the old token.
- **Request transcript.** `logs/requests.md` records full request and response bodies —
  prompts, answers, summaries — but never headers, so tokens are not in it. Protect it like any
  other log of your source directories.
- **`DEFAULT_PERMISSION_MODE=bypassPermissions`** is the shipped default, and it means what it
  says: Claude runs **without prompting for anything**. It will edit, create and delete files and
  execute shell commands in the job's directory with no human in the loop, on whatever prompt was
  queued — including a prompt that arrived from a chat message. That is what makes unattended
  jobs possible, and it is the reason the allowlists and `ALLOWED_ROOTS` matter. Anyone on the
  allowlist effectively has a shell on this machine. Use `acceptEdits` if you want risky shell
  commands to still prompt, and accept that such a job will stall unattended until its lease or
  timeout kills it.
