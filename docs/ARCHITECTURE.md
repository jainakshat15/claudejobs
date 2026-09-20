# Architecture

How the pieces fit together, and why they are built this way. For running the
system see [OPERATIONS.md](OPERATIONS.md); for the routes see [API.md](API.md).

## The problem

Claude Code is interactive: it runs in a terminal, and when it needs a decision
it asks the person sitting there. Running it unattended breaks that assumption
in two directions:

- nobody is at the keyboard, so a question is a silent deadlock;
- nobody is watching, so a crashed session looks exactly like a slow one.

Everything below exists to solve those two problems, plus the ordinary queueing
that follows once jobs outnumber the machine's capacity.

## Components

| Process | Module | Responsibility |
| --- | --- | --- |
| API | `claudejobs/api.py` | The only thing that touches the database on behalf of others |
| Dispatcher | `claudejobs/dispatcher.py` | Claims queued jobs, launches terminals, sweeps dead ones |
| Worker | `claudejobs/run_job.py` | One per job, inside the terminal: heartbeat, cancel, report |
| Control CLI | `claudejobs/jobctl.py` | What the Claude session itself calls |
| Bots | `claudejobs/bots/` | Chat front ends over a shared command layer |

They share nothing but Postgres and the API. Any of them can restart at any
time without taking the others down, which is the point: a 24/7 machine will
lose power, sleep, and be updated halfway through a job.

## Data model

One table holds both the job definition and the worker state, because a job's
"where is it running and is it still alive" is inseparable from its identity.

```
jobs
├── payload        prompt, directory, model, permission_mode, payload jsonb
├── queue state    status, priority, attempts/max_attempts, scheduled_at
├── worker state   worker_id, worker_pid, session_id, job_token_hash,
│                  last_heartbeat_at, lease_expires_at
├── origin         source, source_user_id, source_chat_id, source_message_id
├── control        cancel_requested, cancel_reason
└── outcome        result_summary, error, exit_code, finished_at

job_events         append-only history: created, claimed, launched, question_asked,
                   finished, lease_expired ...
job_messages       questions, answers and notes — the Claude ↔ human conversation
outbound_messages  what the bots still have to deliver, and what they delivered
```

The `origin` columns are what make a question answerable. A job remembers the
channel, chat, user and message it came from, so a session running hours later
can still reach the person who asked for it.

## Job lifecycle

```
                    ┌──────────┐
   POST /jobs ─────▶│  queued  │◀──────────── retry ────────────┐
                    └────┬─────┘                                │
           dispatcher claims (SKIP LOCKED)                      │
                         ▼                                      │
                    ┌──────────┐   jobctl ask    ┌───────────────┴──┐
                    │ running  │────────────────▶│  waiting_input   │
                    └────┬─────┘◀──── answer ────└──────────────────┘
                         │
     ┌───────────────────┼────────────────────┬──────────────────┐
     ▼                   ▼                    ▼                  ▼
┌──────────┐      ┌──────────┐         ┌───────────┐      ┌───────────┐
│succeeded │      │  failed  │         │ cancelled │      │ timed_out │
└──────────┘      └──────────┘         └───────────┘      └───────────┘
```

`succeeded`, `failed`, `cancelled` and `timed_out` are terminal. The first
terminal status wins: if Claude reported success through `jobctl done` and the
process later exits non-zero, the exit code is recorded but the outcome stands.

## Concurrency and liveness

**Claiming.** The dispatcher counts its own active jobs, then claims up to the
remaining capacity in a single statement:

```sql
WITH picked AS (
    SELECT id FROM jobs
    WHERE status = 'queued' AND cancel_requested = false AND scheduled_at <= now()
    ORDER BY priority, scheduled_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT %s
)
UPDATE jobs ... FROM picked WHERE jobs.id = picked.id RETURNING *
```

`SKIP LOCKED` is what allows a second dispatcher — on another laptop, or an
accidental second copy on this one — to take different rows rather than block or
double-run. Capacity is counted per `worker_id`, so two machines each run their
own `MAX_CONCURRENT_JOBS`.

**Leases.** A claimed job gets `lease_expires_at = now() + JOB_LEASE_SECONDS`.
The worker extends it every `HEARTBEAT_INTERVAL_SECONDS`. If the terminal is
closed, the machine sleeps, or Claude crashes hard, the heartbeat stops, the
lease expires, and the next dispatcher tick either retries the job (if attempts
remain) or fails it with a plain explanation. This is why the lease must be
several times the heartbeat interval — the config refuses to start otherwise.

**Waiting counts as busy.** A job parked on a question still occupies a slot,
because its terminal is still open. `QUESTION_TIMEOUT_MINUTES` is the backstop
that eventually frees it.

## The question round trip

The piece that makes unattended work practical:

```
Claude       jobctl              API                 outbox      bot         you
  │  ask "which branch?"          │                   │           │           │
  ├────────────▶│ POST …/jobs/{id}/ask                │           │           │
  │             ├────────────────▶│ job → waiting_input           │           │
  │             │                 ├─ enqueue ────────▶│           │           │
  │             │                 │                   │◀─ claim ──┤           │
  │             │                 │                   │           ├─ send ───▶│
  │             │                 │◀─ record message id ──────────┤           │
  │             │ GET …/questions/{qid}?wait=25 (long poll)       │           │
  │             ├────────────────▶│                   │           │           │
  │             │                 │◀── POST /replies ─────────────┤◀─ reply ──┤
  │             │                 │ answer stored, job → running  │           │
  │             │◀─ answer ───────┤                   │           │           │
  │◀─ stdout ───┤                 │                   │           │           │
```

`jobctl ask` blocks and prints the human's answer on stdout, so from Claude's
point of view asking a person is just a command that takes a while to return.

**Routing, most explicit first** (`POST /replies`):

1. an explicit job id — `/reply 42 use main`;
2. the message being replied to — we stored the provider's message id when the
   bot delivered the question;
3. the thread the answer was typed in — this is how Slack works by default;
4. the person's only open question.

With several jobs waiting and no other signal, the API refuses and lists the
waiting job ids rather than guessing. Getting an answer onto the wrong job is
worse than asking again.

## Why an outbound queue instead of the API calling Telegram

The API never talks to a chat platform. It writes a row; whichever bot owns that
channel claims it (`SKIP LOCKED` again), delivers it, and writes back the
provider message id.

- The bot can be down, restarting, or rate-limited without losing a question.
- The provider message id lands in the same row used for reply routing.
- The API keeps no chat credentials and no network dependency on a third party.
- A crashed bot leaves rows `claimed`; they return to `pending` after five
  minutes and another bot (or the same one, restarted) picks them up.

## Launching a terminal

Two platform details drove this design.

**Environment is not reliably inherited.** `wt.exe` hands the new tab to an
already-running Windows Terminal process, and gnome-terminal proxies through
gnome-terminal-server. Neither reliably passes the launching process's
environment to the child. So each job gets a small generated script in the OS
temp directory, owner-only, that sets its own variables and runs the worker. It
also keeps the job token off the command line, where any local process could
read it.

**Shell shims truncate arguments.** A `.cmd`/`.bat` shim — what an npm install
of Claude Code puts on PATH — runs through `cmd.exe`, which cuts the command
line at the first newline. The job instructions are always multi-line, so a
naive launch would hand Claude a fragment with no `--session-id` and no prompt,
silently. `claude_cli.resolve_claude_command` therefore prefers a native
executable, then `node cli.js` read out of the shim, and only as a last resort
uses the shim itself — in which case the prompt and instructions are written to
files and passed as one-line pointers. `claudejobs selfcheck` reports which path
is in use.

## What the session is told

Every job starts with `--append-system-prompt` carrying generated instructions
(`prompt.py`): its job id, working directory, who posted it, the exact `jobctl`
command line, and the rules — report progress, ask rather than guess, never wait
on terminal input, always finish with `done` or `fail`. Jobs submitted over HTTP
with no chat channel are told plainly that nobody can be asked, and to record
their assumptions in the summary instead.

## Trust boundaries

| Secret | Who holds it | Scope |
| --- | --- | --- |
| `API_TOKEN` | bots, CLI, dispatcher | Everything |
| job token | one running job | That job only; sent once at launch, stored as sha256 |
| bot tokens | the bot process | Its own chat platform |
| `DATABASE_URL` | API, dispatcher | Full database |

A running Claude session can reach the API, so it gets its own narrow
credential: with it, a job can report on itself and talk to its own poster, and
nothing else. It cannot list the queue, cancel other jobs, or read another job's
conversation.

## Deliberate limitations

- **Polling, not `LISTEN/NOTIFY`.** A five-second poll costs one trivial query
  and survives dropped connections, which managed Postgres does regularly.
  Jobs take minutes; five seconds of latency is free.
- **The API is not multi-tenant.** One shared admin token, and an allowlist per
  bot. Everyone with the token can see and control every job.
- **Terminal output is not captured in terminal mode.** Claude's TUI needs a
  real terminal, so the per-job log holds the worker's events rather than the
  session transcript. Headless mode captures everything to
  `LOG_DIR/job-<id>.out.log` instead.
- **Retries re-run the prompt from scratch.** A retried job starts a fresh
  session rather than resuming the old one, so prompts should be written to be
  safe to repeat.
