# claudejobs — setup guide

Follow this top to bottom on a fresh machine. Every command below exists in this
repo; nothing here is aspirational.

---

## 1. What you are setting up

`claudejobs` is a Postgres-backed job queue that runs Claude Code sessions for you.
You send a prompt from Telegram or Slack; the job lands in Postgres; a dispatcher
picks it up and opens a terminal window running the Claude Code CLI in the directory
you named. Claude reports progress back into the same chat and can ask you questions
mid-job, which you answer from chat — the job waits for your reply.

Five things are running when this is healthy:

| Process | What it is | How it starts |
| --- | --- | --- |
| Postgres | Managed cloud database (Neon, Supabase, RDS). Not on your laptop. | your provider |
| API | FastAPI/uvicorn service that owns all queue reads and writes | `python -m claudejobs api` |
| Dispatcher | Claims queued jobs, opens a terminal per job, supervises heartbeats | `python -m claudejobs dispatcher` |
| Telegram bot | Chat front end (optional if you use Slack) | `python -m claudejobs telegram` |
| Slack bot | Chat front end, Socket Mode (optional if you use Telegram) | `python -m claudejobs slack` |

`python -m claudejobs all` runs every configured service as child processes in one
window, which is what you will normally use.

---

## 2. Prerequisites

Before you start, have these ready:

1. **Claude Code CLI**, installed and logged in, with `claude` on your `PATH`.
   Verify with `claude --version`. If it is installed somewhere odd, you can point at
   it later with `CLAUDE_BIN` instead of fixing `PATH`.
2. **git**, to clone this repo.
3. **Python 3.11 or newer** (`requires-python = ">=3.11"` in `pyproject.toml`).
   Section 3 covers installing it.
4. **A managed Postgres database** — Neon, Supabase, or RDS. Section 5 covers this.
   PostgreSQL 13+ is required (the schema uses built-in `gen_random_uuid`).

You do **not** need Docker, a public URL, a reverse proxy, or a local Postgres install.

---

## 3. Install Python

### Windows 11

Option A, winget (fastest):

```powershell
winget install --id Python.Python.3.12 -e
```

Option B, installer from [python.org](https://www.python.org/downloads/windows/):
download the 64-bit installer and — this is the step people miss — tick
**"Add python.exe to PATH"** on the first screen before clicking Install Now.

Close and reopen your terminal, then verify:

```powershell
python --version
pip --version
```

If `python` opens the Microsoft Store instead of printing a version, the PATH entry is
missing: re-run the installer, choose Modify, and enable "Add python.exe to PATH".

**Windows Terminal is recommended.** The dispatcher opens one window per job, and it
prefers `wt.exe` (Windows Terminal) so each job gets a titled tab instead of a separate
`cmd` window. Install it from the Microsoft Store, or:

```powershell
winget install --id Microsoft.WindowsTerminal -e
```

Without it, jobs still run — the launcher falls back to `cmd`.

### Linux

Debian / Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version
```

Fedora / RHEL:

```bash
sudo dnf install -y python3 python3-pip git
python3 --version
```

If your distro ships Python older than 3.11, install a newer one (for example
`sudo apt install -y python3.12 python3.12-venv`) and use that interpreter in step 4.

On a Linux desktop you also want a terminal emulator the launcher recognises:
`gnome-terminal`, `konsole`, `xfce4-terminal`, `alacritty`, `kitty`, or `xterm`.
On a headless server you will set `TERMINAL_MODE=headless` instead (see section 11).

---

## 4. Get the code and create a virtualenv

```bash
git clone <your-repo-url> claude-server
cd claude-server
```

Create the virtualenv:

```powershell
# Windows (PowerShell)
python -m venv .venv
```

```bash
# Linux
python3 -m venv .venv
```

Activate it. Do this in every new terminal you use for claudejobs:

```powershell
# Windows — PowerShell
.\.venv\Scripts\Activate.ps1
```

If PowerShell refuses with a script-execution error, allow it for that session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

```bash
# Windows — Git Bash
source .venv/Scripts/activate
```

```bash
# Linux
source .venv/bin/activate
```

Install the dependencies:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

That installs fastapi, uvicorn, psycopg, pydantic, python-dotenv, httpx,
python-telegram-bot and slack-bolt.

**Alternative:** `pip install -e .` reads `pyproject.toml` instead and additionally
installs two console scripts, so you can type `claudejobs ...` instead of
`python -m claudejobs ...`, and `jobctl` becomes available on `PATH`:

```bash
pip install -e .
```

Both work. This guide uses the `python -m claudejobs` form throughout because it works
either way. For development extras (pytest) use `pip install -r requirements-dev.txt`.

---

## 5. Create the database

You need one managed Postgres database and its connection string. Provider UIs change
often, so these steps are deliberately generic.

**Neon** (https://neon.tech):

1. Sign up and create a project. Pick the region closest to this machine.
2. Neon creates a database for you and shows a connection string on the dashboard.
3. Copy the **psql / connection string** value. It looks like
   `postgresql://user:password@ep-something.region.aws.neon.tech/neondb?sslmode=require`.

**Supabase** (https://supabase.com):

1. Sign up and create a project; set and save the database password.
2. Project Settings → Database → Connection string → URI.
3. Replace the `[YOUR-PASSWORD]` placeholder with the password you set.

**Any provider — the one thing that matters:** the string must end with
`?sslmode=require` (or include `sslmode=require` among its query parameters). Hosted
Postgres refuses plaintext connections, and the failure message you get without it is
a connection error, not an obvious SSL error. If your provider's copy button gives you
a string without it, append it yourself:

```
postgresql://user:password@host/dbname?sslmode=require
```

Note the connection limit on free tiers — often 10–20 total. Two services here share
the pool, which is why `DB_POOL_MAX` defaults to 5.

This string goes into `.env` as `DATABASE_URL` in the next step. Nothing else needs it.

---

## 6. Configure .env

Copy the template. It is heavily commented; read it alongside this table.

```powershell
# Windows (PowerShell)
Copy-Item .env.example .env
```

```bash
# Linux / Git Bash
cp .env.example .env
```

`.env` lives at the repo root and is read by every service. It is in `.gitignore` —
never commit it.

### Settings you must change

| Setting | What to put there |
| --- | --- |
| `DATABASE_URL` | The connection string from section 5, including `?sslmode=require`. |
| `API_TOKEN` | A long random string. Generate one with `python -m claudejobs secret` and paste the output. Anything still starting with `change-me` is rejected. |
| `DEFAULT_DIRECTORY` | Where Claude runs when the chat message doesn't name a directory, e.g. `D:\work` or `/home/you/work`. Commented out by default; uncomment it. Without it, a `/run` with no `dir:` is refused. |
| `ALLOWED_ROOTS` | Safety rail: jobs may only run inside these directories. Separator is `;` on Windows, `:` on Linux. Commented out by default, and **empty means any directory is allowed** — set it. |
| Telegram **or** Slack credentials | At least one chat platform. Telegram needs `TELEGRAM_BOT_TOKEN` **and** `TELEGRAM_ALLOWED_USERS`. Slack needs `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` **and** `SLACK_ALLOWED_USERS`. A bot with an empty allow-list refuses to start, by design. See section 9. |

### Settings worth reviewing

| Setting | Default | Why you might change it |
| --- | --- | --- |
| `MAX_CONCURRENT_JOBS` | `2` | How many Claude sessions run at once on this machine. Each one is a real terminal window. |
| `DEFAULT_PERMISSION_MODE` | `bypassPermissions` | `bypassPermissions` never prompts, which is what unattended jobs need. `acceptEdits` still prompts for risky shell commands. `plan` and `default` are interactive and **will stall** an unattended job. |
| `JOB_TIMEOUT_MINUTES` | `240` | Wall-clock limit per job; `0` disables it. |
| `QUESTION_TIMEOUT_MINUTES` | `180` | How long a question to you may sit unanswered before the job fails and frees its slot. `0` means wait forever. |
| `TERMINAL_MODE` | `auto` | `auto` picks Windows Terminal then `cmd` on Windows, or gnome-terminal/konsole/xfce4-terminal/alacritty/kitty/xterm on Linux. You can force one by name. `headless` runs jobs with no window at all and sends Claude's output to the job log — required on a headless server, see section 11. |
| `KEEP_TERMINAL_OPEN` | `true` | Keeps the job window open after it finishes so you can read the final screen. Set `false` once you trust it. |
| `CLAUDE_BIN` | (empty) | Full path to the Claude Code CLI if it isn't on `PATH`. Prefer the native executable (`claude.exe` on Windows): a `.cmd`/`.bat` shim cannot receive multi-line arguments, and jobs launched through one have their prompt and instructions passed as files instead. May also be a command with arguments, e.g. `node C:\...\cli.js`. |
| `DEFAULT_MODEL` | (empty) | `sonnet`, `opus`, `haiku`, or a full model id, for jobs that don't name one. |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | Leave `127.0.0.1`. This API can start processes; only bind a public interface behind a firewall or VPN. |
| `WORKER_ID` | hostname | Only matters if you run dispatchers on more than one machine. |

Leave the rest (`DB_POOL_*`, `POLL_INTERVAL_SECONDS`, `JOB_LEASE_SECONDS`,
`HEARTBEAT_INTERVAL_SECONDS`, `LOG_DIR`, `REQUEST_LOG_FILE`, …) at their defaults until
you have a reason. One constraint is enforced at startup: `JOB_LEASE_SECONDS` must be
greater than `HEARTBEAT_INTERVAL_SECONDS` — aim for at least 3x.

**Real environment variables always win over `.env`.** That is deliberate: a systemd
unit or a Task Scheduler action can override any single value without editing the file.
It also means a stale `DATABASE_URL` exported in your shell will silently beat the one
in `.env` — worth checking if a value seems to be ignored.

---

## 7. Create the schema

With the virtualenv active and `.env` filled in:

```bash
python -m claudejobs migrate up
```

Expect `applied: 0001_init` on a fresh database, or `database is already up to date` if
you run it twice. This creates the `jobs`, `job_events` and related tables plus a
`schema_migrations` tracking table.

Confirm:

```bash
python -m claudejobs migrate status
```

```
 ✓ 0001_init: applied
```

Anything other than `applied` means something is off — see section 13.

---

## 8. Verify

```bash
python -m claudejobs selfcheck
```

A healthy run, with Telegram configured and Slack not, looks like this:

```
[PASS] .env present — D:\claude-server\.env
[PASS] settings load — worker my-laptop, 2 slot(s)
[PASS] API_TOKEN set
[PASS] database reachable — PostgreSQL 16.4
[PASS] schema up to date — all migrations applied
[PASS] claude CLI found — C:\Users\you\.local\bin\claude.exe
[PASS] terminal available — C:\Windows\System32\wt.exe
[PASS] log directory writable — D:\claude-server\logs\jobs
[WARN] API responding — start it with: claudejobs api
[PASS] telegram configured — 1 allowed user(s)
[WARN] slack configured — SLACK_BOT_TOKEN empty (bot disabled)

everything checks out
```

Checklist of what each line means:

- `.env present` — the file exists at the repo root.
- `settings load` — `.env` parsed without errors; shows your worker id and job slots.
- `API_TOKEN set` — not empty, and not still the `change-me` placeholder.
- `database reachable` — a real round trip to Postgres; prints the server version.
- `schema up to date` — every migration file has been applied.
- `claude CLI found` — the exact command jobs will launch (from `PATH` or `CLAUDE_BIN`).
  If Claude Code was installed with npm, you may also see a `WARN` line saying a
  `.cmd`/`.bat` shim was found: jobs still run, but their prompt is passed as a file
  because a shim truncates multi-line arguments. Installing the native executable
  clears it.
- `terminal available` — the terminal the launcher will use. In `TERMINAL_MODE=headless`
  this line reads `terminal — headless mode; jobs run without a window` instead.
- `log directory writable` — it actually wrote and deleted a probe file in `LOG_DIR`.
- `API responding` — hits the API's `/health`.
- `telegram configured` / `slack configured` — token present, and how many users are
  allow-listed.

**`API responding` WARNs until the API is running**, which is expected the first time —
you have not started anything yet. It is a warning, not a failure: it does not count
toward the problem total, and `everything checks out` still prints. The same is true of
the two bot lines when a platform is deliberately disabled. Once you start the API
(section 10), re-run `selfcheck` and that line should turn `PASS` with your
`API_BASE_URL` and `database: ok`.

Any `[FAIL]` line is a real problem. The command exits non-zero and prints
`N problem(s) to fix` at the end. Fix those before continuing.

---

## 9. Set up the bots

You need at least one. Both can run at the same time.

**Telegram.** Create a bot with [@BotFather](https://t.me/BotFather), copy the token it
gives you into `TELEGRAM_BOT_TOKEN`, then message your new bot `/whoami` to learn your
numeric Telegram user id and put that in `TELEGRAM_ALLOWED_USERS` (comma-separated for
several people). The full walkthrough, including group chats and
`TELEGRAM_CHAT_DIRS`, is in [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md).

**Slack.** The Slack bot uses Socket Mode, so you need no public URL and no tunnel. You
will create a Slack app, take the `xoxb-…` bot token into `SLACK_BOT_TOKEN`, create an
app-level token with the `connections:write` scope into `SLACK_APP_TOKEN`, and list the
Slack member ids (`U…`) allowed to run jobs in `SLACK_ALLOWED_USERS`. Full steps,
including the `/claudejobs` slash command and `SLACK_CHANNEL_DIRS`, are in
[docs/SLACK_SETUP.md](docs/SLACK_SETUP.md).

Both bots refuse to start with an empty allow-list. That is intentional — anyone who can
message the bot can run commands on this machine.

---

## 10. First run

Start everything in one window:

```bash
python -m claudejobs all
```

You should see, roughly in this order:

```
starting: api, dispatcher, telegram   (Ctrl+C stops all of them)
... claudejobs.api: connected to PostgreSQL 16.4
... Uvicorn running on http://127.0.0.1:8000
... claudejobs.dispatcher: dispatcher my-laptop starting: up to 2 concurrent job(s), polling every 5s
... claudejobs.bots.telegram_bot: telegram bot ready; 1 user(s) allowed
```

`all` starts only the services you configured: the API and dispatcher always, Telegram
if `TELEGRAM_BOT_TOKEN` is set, Slack if both Slack tokens are set. If any child exits,
it stops the rest and tells you which one died. Ctrl+C stops everything.

Now go to your chat app and send:

```
/help
```

You get the command list back: `/run`, `/jobs`, `/status`, `/log`, `/messages`,
`/events`, `/reply`, `/cancel`, `/retry`, `/edit`, `/stats`, `/health`, `/whoami`,
`/help`. Then queue a real job:

```
/run list the files in this directory and tell me what this project is
```

Or with options in front of the prompt:

```
/run dir:D:\work\api prio:10 fix the failing auth tests
```

What happens next:

1. The bot replies with the queued job number.
2. Within `POLL_INTERVAL_SECONDS` (5s by default) the dispatcher claims it and a new
   terminal window opens, titled `claudejobs #<id>`, running Claude Code in the job's
   directory.
3. Claude posts progress back into the chat as it works, and if it needs a decision it
   asks — reply in chat with `/reply <id> <answer>`, or just reply to the bot's message.
4. When it finishes, the window stays open (because `KEEP_TERMINAL_OPEN=true`) showing
   `[claudejobs] worker exited with code 0`, and the chat gets the result.

Check from the shell at any time:

```bash
python -m claudejobs jobs
python -m claudejobs status <id>
python -m claudejobs stats
```

### Running the services separately

When something misbehaves, run each service in its own window so its logs are not
interleaved. Activate the virtualenv in each one first.

```bash
# window 1
python -m claudejobs api

# window 2
python -m claudejobs dispatcher

# window 3
python -m claudejobs telegram

# window 4
python -m claudejobs slack
```

Start the API first — the dispatcher, the bots and the per-job workers all talk to it
over `API_BASE_URL`.

---

## 11. Run it 24/7

Here is the trade-off, stated plainly: **jobs open terminal windows, and terminal
windows need an interactive desktop session.** A service that runs without a logged-in
desktop cannot show you a window. So you choose one of two setups:

- **Interactive**: run claudejobs inside a logged-in desktop session, and keep
  `TERMINAL_MODE=auto`. You see every job in its own window. The machine must stay
  logged in.
- **Headless**: set `TERMINAL_MODE=headless` and run claudejobs as a true background
  service. No windows; Claude's raw output goes to the per-job log under `LOG_DIR`
  (`logs/jobs/job-<id>.log` by default), which you read with `/log <id>` from chat. This
  is the right choice for a server.

Do not mix them: a background service with `TERMINAL_MODE=auto` will either fail to
launch a terminal or open windows onto a desktop nobody is looking at.

### Windows 11 — Task Scheduler

1. Open **Task Scheduler** → Create Task (not "Create Basic Task").
2. **General** tab: name it `claudejobs`. Select **"Run only when user is logged on."**
   Do **not** select "Run whether user is logged on or not" — that runs the task in a
   non-interactive session, and the per-job terminal windows will not appear on your
   desktop. That option is only correct if you have set `TERMINAL_MODE=headless`.
3. **Triggers** tab: New → Begin the task **At log on**, for your user. (Use "At
   startup" only in the headless setup described below.)
4. **Actions** tab: New → Start a program.
   - Program/script: `D:\claude-server\.venv\Scripts\python.exe`
   - Add arguments: `-m claudejobs all`
   - Start in: `D:\claude-server`
5. **Conditions** tab: untick "Start the task only if the computer is on AC power" for a
   laptop that runs on battery.
6. **Settings** tab: tick "If the task fails, restart every 1 minute", and untick "Stop
   the task if it runs longer than..." — this is meant to run forever.

For an unattended machine, also enable automatic sign-in so the desktop session exists
after a reboot (Windows: `netplwiz`, untick "Users must enter a user name and
password"), and disable sleep — a sleeping machine stops heartbeating and its running
jobs get reaped:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

If you would rather have a real service with no logged-in user, set
`TERMINAL_MODE=headless` in `.env`, then you may use "Run whether user is logged on or
not" with an "At startup" trigger. You lose the per-job windows and read `logs/jobs/`
instead.

### Linux — systemd user units

User units, plus lingering so they start at boot without you logging in:

```bash
loginctl enable-linger $USER
mkdir -p ~/.config/systemd/user
```

Create `~/.config/systemd/user/claudejobs.service`:

```ini
[Unit]
Description=claudejobs (API, dispatcher, bots)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/you/claude-server
ExecStart=/home/you/claude-server/.venv/bin/python -m claudejobs all
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now claudejobs
systemctl --user status claudejobs
journalctl --user -u claudejobs -f
```

Splitting into separate units (`claudejobs-api`, `claudejobs-dispatcher`,
`claudejobs-telegram`, `claudejobs-slack`, each with its own `ExecStart` subcommand and
the dispatcher/bots ordered `After=claudejobs-api.service`) gives you independent
restarts and cleaner logs; the `deploy/` folder, if present in your checkout, holds
ready-made copies of these unit files.

**The desktop-session caveat applies here too.** `enable-linger` starts your units at
boot with no graphical session, so `TERMINAL_MODE=auto` has no display to open a window
on and the launcher fails with "no terminal emulator found". On a headless server, set
`TERMINAL_MODE=headless` — that is the correct configuration, not a workaround. If you
do want windows on a Linux desktop, drop the lingering, keep the units, and let them
start with your graphical login instead.

---

## 12. Updating

Stop the services (Ctrl+C, or `systemctl --user stop claudejobs`, or End Task), then:

```bash
git pull
```

Activate the virtualenv, then:

```bash
pip install -r requirements.txt   # or: pip install -e .
python -m claudejobs migrate up
python -m claudejobs selfcheck
```

Start again:

```bash
python -m claudejobs all
```

Or `systemctl --user restart claudejobs` on Linux. Check `.env.example` after a pull —
new settings appear there first, and your `.env` will not have them.

---

## 13. Troubleshooting

Run `python -m claudejobs selfcheck` first; it names most of these directly.

| Symptom | Cause and fix |
| --- | --- |
| `DATABASE_URL is not set. Put your Postgres connection string in ...` | `.env` is missing, in the wrong directory, or `DATABASE_URL` is blank. `.env` must be at the repo root, next to `pyproject.toml`. |
| `Could not connect to Postgres: ...` with the hint about `?sslmode=require` | Your connection string is missing `sslmode=require`, or the host/password is wrong. Hosted Postgres refuses plaintext connections. Append `?sslmode=require` and re-run `selfcheck`. |
| `API_TOKEN is unset or still the placeholder` | `.env` still has `API_TOKEN=change-me-to-a-long-random-string`. Run `python -m claudejobs secret` and paste the output in. |
| ``the `claude` CLI is not on PATH. Install Claude Code, or set CLAUDE_BIN in .env`` | The job window opens and immediately fails. Either install Claude Code so `claude --version` works in the same shell, or set `CLAUDE_BIN` to its full path — preferring the native `claude.exe` over an npm `.cmd` shim. |
| `no terminal emulator found. Install one (gnome-terminal, konsole, xterm ...) or set TERMINAL_MODE=headless` | Linux with no supported terminal, or a background service with no desktop session. Install one of the listed emulators, or set `TERMINAL_MODE=headless` (correct on a server). |
| `TERMINAL_MODE=wt but wt.exe is not installed` | You forced Windows Terminal but it isn't there. Install Windows Terminal, or set `TERMINAL_MODE=cmd`, or go back to `auto`. |
| `TELEGRAM_ALLOWED_USERS is empty. Refusing to start a bot that anyone could use...` (same for `SLACK_ALLOWED_USERS`) | The bot will not start with an open allow-list. Send `/whoami` to the bot to get your id and list it in `.env`. |
| `SLACK_BOT_TOKEN and SLACK_APP_TOKEN are both required for Socket Mode` | You set only one of the two. Socket Mode needs the `xoxb-…` bot token *and* an `xapp-…` app-level token with `connections:write`. |
| `cannot reach the job API at http://127.0.0.1:8000: ... Is it running?` | The API is not up, crashed, or `API_BASE_URL` points somewhere else. Start it with `python -m claudejobs api` and check that `API_PORT` matches `API_BASE_URL`. |
| `bad or missing X-Auth-Token` (HTTP 401) | A bot, `jobctl`, or the CLI is using a different `API_TOKEN` than the API. They all read the same `.env`; a stale exported env var overrides it, so check your shell environment too. |
| Job sits at `queued` forever | The dispatcher is not running, or it has no free slots. Check that `python -m claudejobs dispatcher` is up, look at `python -m claudejobs stats` for `slots per worker` versus active jobs, and raise `MAX_CONCURRENT_JOBS` if you are simply at capacity. |
| Job fails with `worker stopped responding (terminal closed, machine slept, or Claude crashed)` | The worker stopped heartbeating for longer than `JOB_LEASE_SECONDS`. Usual causes: you closed the job's terminal window, the machine slept, or Claude crashed. Disable sleep for a 24/7 box; check `logs/jobs/job-<id>.log` for what Claude was doing. |
| `no directory given and DEFAULT_DIRECTORY is not set` (HTTP 400) | The `/run` had no `dir:` option and there is no fallback. Uncomment `DEFAULT_DIRECTORY` in `.env`, or always pass `dir:<path>`. |
| `<path> is outside ALLOWED_ROOTS (...)` (HTTP 400) | The safety rail rejected the directory. Add its parent to `ALLOWED_ROOTS` (`;`-separated on Windows, `:` on Linux) and restart the API. |
| `Not authorised. Send /whoami and ask the owner of this machine to add ...` | Your chat account is not in the allow-list. `/help`, `/start` and `/whoami` work for anyone; everything else needs your id in `TELEGRAM_ALLOWED_USERS` or `SLACK_ALLOWED_USERS`. Restart the bot after editing. |
| `Applied migrations no longer match the files` | A migration file was edited after it was applied. Add a new migration instead. If you genuinely know the change is harmless, `python -m claudejobs migrate up --allow-drift` updates the stored checksums. |
| `JOB_LEASE_SECONDS (x) must be greater than HEARTBEAT_INTERVAL_SECONDS (y)` | Startup refuses this combination because healthy jobs would get reaped between heartbeats. Make the lease at least 3x the heartbeat interval. |
| `run_job must be started by the dispatcher (CLAUDEJOBS_JOB_ID and CLAUDEJOBS_JOB_TOKEN are required)` | You ran `python -m claudejobs.run_job` by hand. It is not an operator command — queue a job with `/run` or `python -m claudejobs submit "..."` and let the dispatcher start it. |
| Job window opens, Claude starts, then hangs waiting on a prompt | `DEFAULT_PERMISSION_MODE` is `plan` or `default`, both of which are interactive. Unattended jobs need `bypassPermissions` (or `acceptEdits` if you accept prompts on risky shell commands). |
| Everything passes but no windows appear on a machine running as a service | The service has no interactive desktop session. Either switch Task Scheduler to "Run only when user is logged on" / drop systemd lingering, or set `TERMINAL_MODE=headless` and read `logs/jobs/`. See section 11. |

Logs to check, in order: the service window or `journalctl --user -u claudejobs`, then
`logs/jobs/job-<id>.log` for a specific job, then `logs/requests.md` for the full HTTP
transcript (`REQUEST_LOG_FILE`).
