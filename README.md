# claudejobs

Queue Claude Code sessions in Postgres and run them on one always-on machine,
driven from Telegram or Slack.

You send a prompt from your phone. A job goes into Postgres. A dispatcher on the
laptop picks it up, opens a terminal, and runs Claude Code in the directory you
named. Claude reports progress as it goes — and when it needs a decision, it asks
you, in the same chat, and waits for your answer before carrying on.

```
  you (Telegram / Slack)
          │  /run dir:D:\work\api fix the failing auth tests
          ▼
   ┌──────────────┐        ┌────────────────┐        ┌──────────────────────┐
   │   bot        │───────▶│   HTTP API     │───────▶│  Postgres            │
   │  (commands)  │◀───────│  (FastAPI)     │◀───────│  jobs, messages,     │
   └──────────────┘        └────────────────┘        │  events, outbox      │
          ▲                        ▲                 └──────────────────────┘
          │  questions,            │  ask / progress / done           │
          │  progress, results     │                                  │ claim
          │                 ┌──────────────┐                          ▼
          └─────────────────│  jobctl      │◀───────────────┌──────────────────┐
                            │ (in-session) │                │   dispatcher     │
                            └──────────────┘                │  2 jobs at a time│
                                    ▲                       └──────────────────┘
                                    │                                 │ launches
                            ┌───────────────────────────────────────┐ │
                            │  terminal per job: Claude Code        │◀┘
                            └───────────────────────────────────────┘
```

## What makes it usable unattended

- **Claude can ask you things.** `jobctl ask "which branch?"` blocks the session,
  sends the question to the chat it came from, and returns your answer on stdout.
  With several jobs waiting at once, replies are routed by the message you reply
  to (or `/reply <job id> <answer>`), so answers never land on the wrong job.
- **Nothing runs forever.** Every job holds a lease and heartbeats. A closed
  terminal, a slept machine or a crashed session is noticed and the job is
  retried or failed — never left "running" forever.
- **The poster stays in control.** Cancel, retry, re-prioritise, or edit a job
  that hasn't started, from chat or the CLI.
- **Two dispatchers are safe.** Claims use `FOR UPDATE SKIP LOCKED`, so a second
  machine can share the queue without ever double-running a job.
- **Everything is on the record.** Status history in `job_events`, the whole
  Claude↔human conversation in `job_messages`, per-job logs on disk, and a
  markdown transcript of every HTTP call.

## Quick start

Full instructions, including installing Python and creating the database, are in
**[SETUP.md](SETUP.md)**. The short version:

```bash
git clone <your-repo-url> claude-server && cd claude-server
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # Windows: copy .env.example .env
python -m claudejobs secret     # put this in .env as API_TOKEN
# add DATABASE_URL, DEFAULT_DIRECTORY and a bot token to .env

python -m claudejobs migrate up
python -m claudejobs selfcheck
python -m claudejobs all
```

Then message your bot:

```
/run dir:D:\work\api fix the failing auth tests
/jobs          /status 12       /cancel 12
/reply 12 use the main branch
/help
```

## Commands

Every chat command maps onto one HTTP route, so anything you can do from your
phone you can also do with curl or the CLI.

| Chat | CLI | HTTP |
| --- | --- | --- |
| `/run <prompt>` | `claudejobs submit "<prompt>" --dir PATH` | `POST /jobs` |
| `/jobs [status]` | `claudejobs jobs --status active` | `GET /jobs` |
| `/status <id>` | `claudejobs status <id>` | `GET /jobs/{id}` |
| `/cancel <id>` | `claudejobs cancel <id>` | `POST /jobs/{id}/cancel` |
| `/retry <id>` | `claudejobs retry <id>` | `POST /jobs/{id}/retry` |
| `/edit <id> key=value` | — | `PATCH /jobs/{id}` |
| `/reply <id> <answer>` | `claudejobs answer <id> "text"` | `POST /replies` |
| `/log <id>` | — | `GET /jobs/{id}/log` |
| `/stats` | `claudejobs stats` | `GET /stats` |

## Documentation

| Document | What's in it |
| --- | --- |
| [SETUP.md](SETUP.md) | Install to first job, on Windows and Linux |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit, and why they are built this way |
| [docs/API.md](docs/API.md) | Every route, with request and response shapes |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Running it: tuning, monitoring, failure recovery |
| [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md) | Creating the bot and the allowlist |
| [docs/SLACK_SETUP.md](docs/SLACK_SETUP.md) | Slack app, Socket Mode, scopes |
| [deploy/README.md](deploy/README.md) | Running it 24/7 as a service |

## Layout

```
claudejobs/
  api.py          HTTP API: job CRUD, poster controls, job-scoped endpoints
  dispatcher.py   claims jobs, launches terminals, sweeps dead ones
  launcher.py     cross-platform "open a terminal and run this"
  run_job.py      runs inside each job's terminal: heartbeat, cancel, report
  jobctl.py       what the Claude session calls: progress / ask / done / fail
  prompt.py       the instructions every job is started with
  repository.py   every SQL statement in the system
  bots/           Telegram and Slack front ends over a shared command layer
migrations/       numbered .sql files, applied by `claudejobs migrate up`
tests/            83 tests; the database ones need a throwaway Postgres
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest                                  # unit tests only

docker run -d --name claudejobs-test -e POSTGRES_PASSWORD=test \
    -e POSTGRES_DB=claudejobs -p 55432:5432 postgres:16-alpine
export CLAUDEJOBS_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/claudejobs
pytest                                  # adds the database and API tests
```

## Security

This service starts processes on the machine it runs on. Treat it accordingly:

- The API binds `127.0.0.1` by default and every route but `/health` needs
  `API_TOKEN`. Don't expose it without a firewall or VPN in front.
- Both bots refuse to start without an allowlist of user ids.
- `ALLOWED_ROOTS` limits which directories jobs may run in.
- Each running job gets its own token, stored hashed, that unlocks only that job.
- `DEFAULT_PERMISSION_MODE=bypassPermissions` means Claude never stops to ask
  permission — which is what unattended operation needs, and also means a job can
  run any command in its directory. Use `ALLOWED_ROOTS`, and keep the allowlists
  short.
