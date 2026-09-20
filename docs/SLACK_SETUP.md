# Slack setup

This guide covers only the Slack side. It assumes the machine is already
installed and configured per `docs/SETUP.md`, and that the API
(`python -m claudejobs api`) and dispatcher are running.

## 1. Create the Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click
   **Create New App**.
2. Choose **From scratch**.
3. Name it (e.g. `claudejobs`) and pick the workspace it will live in.
4. Click **Create App**. You land on the app's settings pages, which the rest of
   this guide walks through.

## 2. Enable Socket Mode and create the app-level token

The bot uses Socket Mode: it dials out to Slack over a websocket, so the machine
needs **no public URL, no tunnel and no inbound firewall rule**, and you never
configure a Request URL for events.

1. Open **Settings -> Socket Mode** and turn **Enable Socket Mode** on.
2. Slack prompts for an app-level token. Give it a name (e.g. `socket`) and add
   the scope **`connections:write`**. Click **Generate**.
   (You can also create it later under **Settings -> Basic Information ->
   App-Level Tokens**.)
3. Copy the token — it starts with `xapp-` — into `.env`:

```dotenv
SLACK_APP_TOKEN=xapp-1-A01234567-...
```

## 3. Bot token scopes and installation

The bot posts with `chat.postMessage` and reads messages and mentions through
the Events API, so it needs to both read the conversations it lives in and write
into them.

Open **Features -> OAuth & Permissions -> Scopes -> Bot Token Scopes** and add:

| Scope | Why |
| --- | --- |
| `app_mentions:read` | Receive `app_mention` events when the bot is mentioned in a channel. |
| `channels:history` | Read messages in public channels the bot is in. |
| `groups:history` | Read messages in private channels the bot is in. |
| `im:history` | Read direct messages sent to the bot. |
| `mpim:history` | Read group direct messages the bot is in. |
| `chat:write` | Post replies, questions and notices (`chat.postMessage`). |
| `commands` | Only if you add the optional `/claudejobs` slash command (step 5). |

Then:

1. Scroll up to **OAuth Tokens for Your Workspace** and click
   **Install to Workspace**, and approve.
2. Copy the **Bot User OAuth Token** — it starts with `xoxb-` — into `.env`:

```dotenv
SLACK_BOT_TOKEN=xoxb-0000000000-0000000000-...
```

Adding scopes later always requires reinstalling the app before they take
effect.

## 4. Event subscriptions

Open **Features -> Event Subscriptions** and turn **Enable Events** on. With
Socket Mode enabled there is no Request URL field to fill in.

Under **Subscribe to bot events**, add:

| Event | Needed for |
| --- | --- |
| `app_mention` | Being addressed in a channel. |
| `message.im` | Direct messages to the bot. |
| `message.channels` | Messages in public channels the bot is in. |
| `message.groups` | Messages in private channels — add if you use the bot there. |
| `message.mpim` | Messages in group DMs — add if you use the bot there. |

Save changes, and reinstall the app if Slack asks you to.

The bot ignores messages that carry a `bot_id` or a `subtype`, so its own posts,
edits and join notices never loop back into it.

## 5. Optional: the `/claudejobs` slash command

The app registers a handler for a slash command named `/claudejobs`, but it is
**entirely optional** — plain messages work the same way (see step 7). Add it
only if you like typing Slack-native commands.

1. Open **Features -> Slash Commands -> Create New Command**.
2. Command: `/claudejobs`. Request URL is not used in Socket Mode; short
   description and usage hint are free text.
3. Save, add the `commands` scope (step 3) and reinstall the app.

Usage puts the claudejobs command inside it, e.g. `/claudejobs jobs active` or
`/claudejobs run dir:D:\work\api fix the failing tests`. With no text it shows
the help. Its answers are ephemeral (visible only to you).

## 6. Find your member id and fill the allowlist

The allowlist exists because this bot starts processes on the machine: a job is
a real Claude Code session with a real terminal, running in a real directory.
Anyone in the workspace could otherwise run commands on your computer. For that
reason the bot **refuses to start with an empty allowlist** — see
`Settings.require_slack()` in `claudejobs/config.py`, which raises a
`ConfigError` rather than come up unprotected.

Only `help`, `start` and `whoami` work for anyone. **Every other command
requires the allowlist**, and plain messages from people who are not on it are
ignored entirely.

Two ways to get your member id:

- In Slack, click your avatar -> **Profile** -> the **…** (More) button ->
  **Copy member ID**. It looks like `U01ABC2DEF`.
- Or message the running bot `whoami`, which answers with your channel, user id,
  username, chat id and thread.

Put the ids in `.env`, comma-separated:

```dotenv
SLACK_ALLOWED_USERS=U01ABC2DEF,U09XYZ8GHI
```

> Chicken-and-egg note: because the bot will not start with an empty list, put a
> placeholder in `SLACK_ALLOWED_USERS` for the first run, ask it `whoami`, then
> replace it with the real ids. `whoami` answers even when you are not on the
> list.

Start the bot on the 24/7 machine with:

```console
python -m claudejobs slack
```

Every change to `.env` needs a bot restart — settings are read once at startup.

## 7. How commands are typed in Slack

- **A leading slash is optional.** Slack reserves real slash commands, so the
  bot accepts both forms: `run fix the tests` and, if you added the slash
  command, `/claudejobs run fix the tests`.
- **In a direct message**, type the command on its own: `jobs active`.
- **In a channel**, mention the bot: `@claudejobs status 42`. The mention is
  stripped before the command is parsed. The bot must be invited to the channel
  (`/invite @claudejobs`).
- **Every reply is posted in a thread** — on the message you typed, or in the
  thread you typed it in.

### Command reference

Every command maps onto one HTTP route, so anything you can do from chat you can
also do with `curl` (see `docs/API.md`). Aliases are interchangeable. The
leading slash is shown for readability; it is optional in Slack.

| Command | Aliases | What it does | HTTP route |
| --- | --- | --- | --- |
| `/run <prompt>` | `/new` | Queue a job. Options may precede the prompt (see below). | `POST /jobs` |
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

That is 14 rows covering all 22 names in the `COMMANDS` dict in
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
@claudejobs run dir:D:\work\api prio:10 fix the failing auth tests
```

If neither `dir:` nor a per-channel mapping nor `DEFAULT_DIRECTORY` gives a
directory, the bot says so and queues nothing.

### Statuses accepted by `/jobs`

`queued`, `running`, `waiting_input`, `succeeded`, `failed`, `cancelled`,
`timed_out`, `active`.

### Keys accepted by `/edit`

`prompt`, `directory`, `title`, `model`, `permission_mode` (alias `mode`),
`priority` (alias `prio`), `timeout_minutes` (alias `timeout`), `max_attempts`
(alias `attempts`). Written as `key=value` pairs, e.g.:

```text
edit 42 prio=5 title="auth tests"
```

## 8. Threading and answering questions

Every message about a job — its questions, notices and the final result — is
posted **in the thread of the request that created it**. That is what makes
answers unambiguous in Slack: reply in that thread and the answer reaches the
right job even when several of your jobs are waiting at once.

Two ways to answer:

1. **Type the answer in the job's thread.** Anything that isn't a recognised
   command is treated as an answer.
2. **`reply <job id> <answer>`** — works from anywhere, including another
   channel or a DM:

   ```text
   reply 42 yes, drop the column
   ```

The API resolves an answer to a job in this order, most explicit first
(`route_reply` in `claudejobs/api.py`):

1. an explicit job id (`reply 42 yes`);
2. the chat message the human replied to (in Slack, the thread the message was
   posted into — its `thread_ts`);
3. the thread the answer was typed in;
4. the person's only open question.

With several jobs waiting and none of those signals — for example a bare answer
typed at channel top level rather than in a thread — the API refuses rather than
guess, and answers with the count and the waiting ids, e.g. `3 of your jobs are
waiting (#41, #42, #45). Say which one: /reply <job id> <your answer>`.

If **none** of your jobs are waiting, a bare message typed outside a thread is
ignored silently; the same message inside a thread gets `none of your jobs are
waiting for an answer right now`. A question that somebody already answered
gives `that question is already answered`.

If the original thread no longer exists, delivery falls back to posting the
message at channel top level rather than dropping it.

## 9. Per-channel default directories

Give a channel its own default working directory with `SLACK_CHANNEL_DIRS`, a
JSON object mapping channel id to path. On Windows the backslashes must be
escaped, because the value is JSON:

```dotenv
SLACK_CHANNEL_DIRS={"C01234567": "D:\\work\\web", "C07654321": "D:\\repos\\api"}
```

The channel id is the `chat id` line from `whoami`, asked in that channel. A
`dir:<path>` option on `run` beats this mapping, and this mapping beats
`DEFAULT_DIRECTORY`.

## 10. Troubleshooting

**`invalid_auth`**

The bot token is wrong, was revoked, or belongs to a different workspace. Copy
the current **Bot User OAuth Token** (`xoxb-…`) from **OAuth & Permissions**
into `SLACK_BOT_TOKEN` and restart. If the failure happens while connecting
rather than while posting, it is the app-level token: check `SLACK_APP_TOKEN`
starts with `xapp-` and has the `connections:write` scope.

**`missing_scope`**

Slack names the scope it wanted in the error. Add it under **OAuth &
Permissions -> Bot Token Scopes** and **reinstall the app**, then restart the
bot. The usual ones:

| Error mentions | Add |
| --- | --- |
| `chat:write` | Posting replies and questions. |
| `channels:history` / `groups:history` | Reading public / private channel messages. |
| `im:history` / `mpim:history` | Reading DMs / group DMs. |
| `app_mentions:read` | Receiving mentions. |
| `commands` | The optional `/claudejobs` slash command. |

**The bot receives nothing**

- Check the bot events in **Event Subscriptions** (step 4) — `message.channels`
  and `message.im` in particular; a bot with only `app_mention` will never see
  plain messages.
- Invite the bot to the channel: `/invite @claudejobs`. It cannot read a channel
  it is not a member of, whatever scopes it has.
- In a channel, mention the bot. Without `message.channels` subscribed, only
  mentions arrive.
- Confirm the process is running and printed
  `slack bot connecting; N user(s) allowed`.
- `python -m claudejobs selfcheck` reports whether Slack is configured and how
  many users are allowed.

**"Not authorised. Type `whoami` and ask the owner of this machine to add your
Slack member id to SLACK_ALLOWED_USERS."**

Your member id is not in `SLACK_ALLOWED_USERS`. Type `whoami`, add the
**user id** (not the channel id) to the comma-separated list in `.env`, restart
the bot.

**The bot refuses to start**

- `SLACK_BOT_TOKEN and SLACK_APP_TOKEN are both required for Socket Mode` —
  both must be set; Socket Mode needs the pair.
- `SLACK_ALLOWED_USERS is empty` — the deliberate refusal described in step 6.
- `API_TOKEN is unset or still the placeholder` — the bot talks to the local API
  and needs the shared secret from `.env`.

**Socket Mode disconnects**

The Bolt Socket Mode handler reconnects by itself, so short drops are normal and
the delivery loop keeps retrying on its own schedule
(`OUTBOUND_POLL_SECONDS`, default 3). Persistent failure usually means one of:

- the app-level token was revoked or Socket Mode was turned off in the app
  settings — regenerate the `xapp-` token and update `SLACK_APP_TOKEN`;
- the machine's outbound websocket traffic to Slack is blocked by a proxy or
  firewall;
- the machine slept. Configure the 24/7 machine not to sleep (see
  `docs/SETUP.md`).

Undelivered messages are not lost: they stay queued and `stats` reports the
count under **Undelivered messages**. A message that keeps failing is marked
failed and logged as `could not deliver message #N to <channel>: …`.
