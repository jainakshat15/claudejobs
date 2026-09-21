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
| `commands` | Only if you register slash commands (step 5). The generated manifest includes this scope. |

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

## 5. Slash commands

Slash commands are **optional** — plain messages work the same way (step 7) —
but they give you Slack's autocomplete, which is the easiest way to discover
what the bot can do.

The bot handles whatever commands you register: it matches every slash command
Slack routes to this app and reads the command name itself. So the list below is
decided by your Slack app configuration, not by the code.

### Register them all at once, with a manifest

Filling in **Create New Command** once per command is slow and easy to get
wrong. Generate the manifest instead — it declares every command, along with the
scopes, events and Socket Mode settings from the previous steps:

```bash
python -m claudejobs slack-manifest
```

A copy is committed at [`../deploy/slack-app-manifest.yml`](../deploy/slack-app-manifest.yml).
Regenerate it after adding a product, because each product gets its own command:

```bash
python -m claudejobs slack-manifest --out deploy/slack-app-manifest.yml
```

To apply it: **api.slack.com/apps -> your app -> App Manifest**, switch to the
YAML tab, paste, save, then reinstall the app when Slack asks (the scopes
change). Editing the manifest is the same as editing every settings page at
once, so check the diff Slack shows you before confirming.

### What gets registered

| Command | Does |
| --- | --- |
| `/claudejobs <command> ...` | Anything — the umbrella form |
| `/run`, `/ask`, `/ask-sales-bot`, `/ask-od` | Start work or ask a question |
| `/jobs`, `/job`, `/log`, `/messages`, `/events` | See what is happening |
| `/reply`, `/cancel`, `/retry`, `/edit` | Steer a job |
| `/stats`, `/health`, `/whoami`, `/help` | This machine, and help |

Both spellings reach the same handler: `/run fix the tests` and
`/claudejobs run fix the tests` do the same thing. Answers are ephemeral —
visible only to you — while a job's own messages are posted to the channel.

### Two Slack rules worth knowing

**`/status` is Slack's own command** (it sets your Slack status), and an app
cannot take it. The manifest registers **`/job <id>`** instead, which is an
existing alias for the same thing. Slack owns roughly thirty such names —
`/remind`, `/topic`, `/invite`, `/search`, `/mute` and so on.

**Slash commands do not work inside message threads.** Slack only allows its own
built-ins there. This matters when a job asks you something, because that
conversation happens in a thread: answer it by **typing a plain reply in the
thread** (no slash), which the bot routes to the right job. `/reply <id> <answer>`
works fine from the main channel view.

### If a name is already taken

Another installed app may already own `/run` or `/jobs`; Slack will refuse those
entries. Register everything under a prefix instead:

```bash
python -m claudejobs slack-manifest --prefix cj-
```

That produces `/cj-run`, `/cj-jobs`, `/cj-status` and so on — no collisions, and
typing `/cj` lists them together. Then tell the bot to expect it, in `.env`:

```
SLACK_COMMAND_PREFIX=cj-
```

The bot strips the prefix before dispatching, so everything else behaves the
same. Note that `/help` output still shows the unprefixed names.

## 6. Find your member id and fill the allowlist

The allowlist exists because this bot starts processes on the machine: a job is
a real Claude Code session with a real terminal, running in a real directory.
Anyone in the workspace could otherwise run commands on your computer. For that
reason the bot **refuses to start with an empty allowlist** — see
`Settings.require_slack()` in `claudejobs/config.py`, which raises a
`ConfigError` rather than come up unprotected.

Only `help`, `start` and `whoami` work for anyone. **Every other command
requires the allowlist**, and plain messages from people who are not on it are
ignored entirely. To skip the list and let the whole workspace in, see
[Opening it to the whole workspace](#opening-it-to-the-whole-workspace) below.

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

### Opening it to the whole workspace

To let everyone in the workspace use the bot, set the wildcard:

```dotenv
SLACK_ALLOWED_USERS=*
```

Then any member who can message the app can queue jobs, cancel other people's
jobs and read their logs. The bot logs a warning at startup saying so, because
this is the one setting that decides who can run commands on the machine.

Be deliberate about it. It is reasonable inside a small, trusted team where the
worker machine holds nothing sensitive; it is a bad idea in a workspace with
guests, external partners, or anyone whose account you would not hand a terminal
to. The other rails still apply — `ALLOWED_ROOTS` limits which directories jobs
may touch, and everything is recorded in `job_events` — but they limit *where*
work happens, not *who* asks for it.

An empty value is still refused: that way an unconfigured install fails closed
rather than silently accepting the whole workspace.

Start the bot on the 24/7 machine with:

```console
python -m claudejobs slack
```

Every change to `.env` needs a bot restart — settings are read once at startup.

## 7. How commands are typed in Slack

- **A leading slash is optional.** All three forms do the same thing:
  `run fix the tests` as a plain message, `/run fix the tests` if you registered
  the slash commands (step 5), and `/claudejobs run fix the tests` through the
  umbrella command.
- **In a thread, use plain text.** Slack does not deliver app slash commands
  typed inside a thread, so answer a job's question by replying normally.
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
| `commands` | Slash commands, if you registered any (step 5). |

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
