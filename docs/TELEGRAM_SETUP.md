# Telegram setup

This guide covers only the Telegram side. It assumes the machine is already
installed and configured per `docs/SETUP.md`, and that the API
(`python -m claudejobs api`) and dispatcher are running.

The bot uses long polling: it dials out to Telegram, so the machine needs no
public URL, no tunnel and no inbound firewall rule.

## 1. Create the bot with BotFather

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Give it a display name (anything, e.g. `Claude Jobs`).
4. Give it a username. It must be unique and end in `bot`, e.g.
   `studio_claudejobs_bot`.
5. BotFather replies with a token that looks like
   `123456789:AAH...`. Copy it.
6. Put it in `.env` on the 24/7 machine:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Leaving `TELEGRAM_BOT_TOKEN` empty disables the Telegram bot entirely.

## 2. Find your numeric user id and fill the allowlist

The allowlist exists because this bot starts processes on the machine: a job is
a real Claude Code session with a real terminal, running in a real directory.
Anyone who can message the bot could otherwise run commands on your computer.
For that reason the bot **refuses to start with an empty allowlist** — see
`Settings.require_telegram()` in `claudejobs/config.py`, which raises a
`ConfigError` rather than come up unprotected.

Only `/whoami`, `/help` and `/start` work for anyone. **Every other command
requires the allowlist.**

1. Start the bot once with the token set (the allowlist can be empty only if you
   set a placeholder first — see the note below):

   ```console
   python -m claudejobs telegram
   ```

2. Open your bot in Telegram (search for its username) and press Start, then
   send:

   ```text
   /whoami
   ```

3. It replies with your channel, **user id**, username, chat id and thread. Copy
   the numeric user id.
4. Stop the bot (`Ctrl+C`), put the id in `.env`, and start it again:

   ```dotenv
   TELEGRAM_ALLOWED_USERS=123456789
   ```

   Several people, comma-separated:

   ```dotenv
   TELEGRAM_ALLOWED_USERS=123456789,987654321
   ```

> Chicken-and-egg note: because the bot will not start with an empty list, put
> any non-empty placeholder (e.g. your best guess at your own id, or `0`) in
> `TELEGRAM_ALLOWED_USERS` for the first run, send `/whoami`, then replace it
> with the real id. `/whoami` answers even when you are not on the list.

Every change to `.env` needs a bot restart — settings are read once at startup.

## 3. Optional: using the bot in a group

1. Add the bot to the group (group settings -> Add members -> its username).
2. In BotFather, send `/setprivacy`, pick the bot, and choose **Disable**.
   With privacy mode enabled, Telegram hides most group messages from bots;
   disabling it is what lets the bot see plain replies to its own question
   messages in a group.
3. In a group the bot deliberately ignores ordinary chatter. It acts on:
   - commands (`/status 42`, also the `/status@your_bot` form Telegram adds);
   - plain messages that are a **reply to one of the bot's own messages**.

   In a one-to-one chat with the bot, every plain message is treated as a
   possible answer to a waiting job.

Give a group its own default working directory with `TELEGRAM_CHAT_DIRS`, a
JSON object mapping chat id to path. On Windows the backslashes must be escaped,
because the value is JSON:

```dotenv
TELEGRAM_CHAT_DIRS={"-1001234567890": "D:\\work\\web", "123456789": "D:\\repos\\api"}
```

The chat id is the `chat id` line from `/whoami`, sent in that chat. Group ids
start with `-`. A `dir:<path>` option on `/run` beats this mapping, and this
mapping beats `DEFAULT_DIRECTORY`.

## 4. Command reference

Every command maps onto one HTTP route, so anything you can do from chat you can
also do with `curl` (see `docs/API.md`). Aliases are interchangeable.

| Command | Aliases | What it does | HTTP route |
| --- | --- | --- | --- |
| `/run <prompt>` | `/new` | Queue a job. Options may precede the prompt (see below). | `POST /jobs` |
| `/ask-sales-bot <question>` | `/salesbot` | Answer a question about the Sales Bot product from its source and its docs, read-only. | `POST /jobs` |
| `/ask-od <question>` | `/od` | The same for Organizational Development. | `POST /jobs` |
| `/ask <question>` | — | Asks which product you mean, and lists them. | — (local) |
| `/jobs [status] [n]` | `/list`, `/queue` | List recent jobs. `n` defaults to 10, capped at 25. | `GET /jobs` |
| `/status <id>` | `/job` | Everything about one job, including its open question. | `GET /jobs/{id}` |
| `/log <id> [lines]` | `/logs` | Tail that job's worker log. `lines` defaults to 25, capped at 100. | `GET /jobs/{id}/log` |
| `/messages <id>` | — | Questions, answers and notes for a job (last 15). | `GET /jobs/{id}/messages` |
| `/events <id>` | — | State changes for a job (last 15). | `GET /jobs/{id}/events` |
| `/reply <id> <answer>` | `/answer` | Answer a job that is waiting for input. | `POST /replies` |
| `/cancel <id> [reason]` | `/stop` | Stop a queued or running job. | `POST /jobs/{id}/cancel` |
| `/retry <id>` | — | Requeue a finished job. | `POST /jobs/{id}/retry` |
| `/edit <id> key=value` | — | Change a job that hasn't started. | `PATCH /jobs/{id}` |
| `/stats` | — | Queue depth, open questions, busy workers. | `GET /stats` |
| `/health` | — | API, database and `claude`-on-PATH check. | `GET /health` |
| `/whoami` | — | Your channel, user id, username, chat id and thread. Public. | — (local) |
| `/help` | `/start` | The built-in help text. Public. | — (local) |

That is 17 rows covering all 28 names in the `COMMANDS` dict in
`claudejobs/bots/common.py`.

### Options for `/run`

Options go **before** the prompt, as `key:value`, or `key:"value with spaces"`.

| Option | Aliases | Meaning |
| --- | --- | --- |
| `dir:<path>` | `directory:`, `path:` | Where to run the job. |
| `model:<name>` | — | `sonnet`, `opus`, `haiku` (or a full model id). |
| `prio:<1-1000>` | `priority:` | Lower runs sooner. |
| `mode:<mode>` | `permissions:` | `bypassPermissions`, `acceptEdits`, … |
| `timeout:<minutes>` | — | Wall-clock limit for this job. |
| `title:"short name"` | — | A short label for listings. |
| `attempts:<n>` | — | Maximum attempts before the job is failed. |

```text
/run dir:D:\work\api prio:10 fix the failing auth tests
```

If neither `dir:` nor a per-chat mapping nor `DEFAULT_DIRECTORY` gives a
directory, the bot says so and queues nothing.

### Statuses accepted by `/jobs`

`queued`, `running`, `waiting_input`, `succeeded`, `failed`, `cancelled`,
`timed_out`, `active`.

### Keys accepted by `/edit`

`prompt`, `directory`, `title`, `model`, `permission_mode` (alias `mode`),
`priority` (alias `prio`), `timeout_minutes` (alias `timeout`), `max_attempts`
(alias `attempts`). Written as `key=value` pairs, e.g.:

```text
/edit 42 prio=5 title="auth tests"
```

## 5. Answering a job's question

When a job needs input, the bot posts the question into the chat that created
the job. There are two ways to answer.

1. **Reply to the bot's question message** (Telegram's reply feature) and type
   the answer. This carries the message id, so it lands on the right job.
2. **`/reply <job id> <answer>`** — works from anywhere, including another chat:

   ```text
   /reply 42 yes, drop the column
   ```

The API resolves an answer to a job in this order, most explicit first
(`route_reply` in `claudejobs/api.py`):

1. an explicit job id (`/reply 42 yes`);
2. the chat message the human replied to (the Telegram reply-to message id);
3. the thread the answer was typed in;
4. the person's only open question.

### With several jobs waiting

This is the case to know about. If you type a bare answer that is **not** a
reply-to and carries no job id, and more than one of your jobs is waiting, the
API refuses rather than guess. It answers with the count and the waiting ids,
e.g. `3 of your jobs are waiting (#41, #42, #45). Say which one: /reply <job id>
<your answer>`. Use the reply-to gesture, or name the id with `/reply <id>`.

If **none** of your jobs are waiting, a bare message in a private chat is
ignored silently; a bare message that was a reply to one of the bot's messages
gets `none of your jobs are waiting for an answer right now`. A question that
somebody already answered gives `that question is already answered`.

## 6. Troubleshooting

**The bot doesn't answer at all.**

- Is the process running? `python -m claudejobs telegram` must be up on the
  24/7 machine; check its console for a `telegram bot ready; N user(s) allowed`
  line.
- Wrong token: a bad `TELEGRAM_BOT_TOKEN` makes the bot fail on startup, or you
  are messaging a different bot than the one the token belongs to. Confirm the
  username matches what BotFather gave you.
- Not on the allowlist: only `/whoami`, `/help` and `/start` answer for
  non-allowlisted users; other commands answer with the refusal below, and plain
  messages from non-allowlisted users are ignored entirely — with no reply.
- `python -m claudejobs selfcheck` reports whether Telegram is configured and
  how many users are allowed.

**"Not authorised. Send /whoami and ask the owner of this machine to add your
user id to TELEGRAM_ALLOWED_USERS."**

Your numeric id is not in `TELEGRAM_ALLOWED_USERS`. Send `/whoami`, add the
**user id** (not the chat id) to the comma-separated list in `.env`, restart the
bot.

**The bot refuses to start.**

- `TELEGRAM_BOT_TOKEN is not set` — fill it in `.env`.
- `TELEGRAM_ALLOWED_USERS is empty` — the deliberate refusal described in
  step 2. Add at least one id.
- `API_TOKEN is unset or still the placeholder` — the bot talks to the local API
  and needs the shared secret from `.env`.

**Messages in a group never reach the bot.**

Privacy mode. Run BotFather's `/setprivacy` for this bot and choose **Disable**,
then remove and re-add the bot to the group so the change takes effect. Remember
that even then the bot only acts on commands and on replies to its own messages
in groups.

**Questions from jobs never arrive / delivery errors in the log.**

- The delivery loop logs `could not deliver message #N to chat X: …`. Common
  causes: the bot was removed from the chat, or the chat id in the job's record
  no longer exists.
- If the original request message was deleted or is too old to reply to,
  delivery is retried automatically **without** the reply reference — the
  message still arrives, just not as a reply. The log shows
  `retrying message #N without the reply reference`.
- Messages that still fail are marked failed; `/stats` shows the count of
  undelivered messages.
- `⚠️ The job API isn't answering` means the bot is up but `claudejobs api` is
  not. `⚠️ The bot's API token was rejected` means `API_TOKEN` in `.env` does
  not match the one the API is using.
