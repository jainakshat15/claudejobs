# Security

## What this software does

Read this before you install it. claudejobs exists to run Claude Code sessions
on your machine in response to chat messages. By design it:

- **starts processes on the host** with the privileges of the user running the
  dispatcher;
- runs them **without a human approving each action** — the default
  `DEFAULT_PERMISSION_MODE=bypassPermissions` is what makes unattended operation
  work, and it means a job can run any command Claude decides to run;
- **acts on instructions from a chat platform**, so anyone who can message the
  bot can, within the allowlist, cause code to run on that machine.

That is the intended behaviour, not a flaw. Treat the machine running it as one
that executes untrusted-ish input, and configure the controls below.

## Running it safely

| Control | Setting | Why |
| --- | --- | --- |
| Keep the API private | `API_HOST=127.0.0.1` (default) | Every route but `/health` requires `API_TOKEN`, but the API starts processes — don't put it on a network |
| Fence the filesystem | `ALLOWED_ROOTS=/path/one:/path/two` | Jobs are refused outside these directories |
| Short allowlists | `TELEGRAM_ALLOWED_USERS`, `SLACK_ALLOWED_USERS` | Both bots refuse to start empty; only listed ids may run jobs |
| A real token | `API_TOKEN` from `claudejobs secret` | Fails closed: an unset token makes every route return 503 |
| Consider a tamer mode | `DEFAULT_PERMISSION_MODE=acceptEdits` | Claude edits files freely but stops before risky shell commands. Unattended, a stopped job stalls until its question timeout — safer, less autonomous |
| Isolate the host | — | A dedicated machine or VM, with credentials scoped to what the jobs genuinely need |

Secrets live in `.env`, which is gitignored. Per-job tokens are stored as
sha256 hashes and unlock only their own job. Auth headers are never written to
the request transcript.

## What is not protected against

Stated plainly so you can judge the risk:

- **An allowlisted user is fully trusted.** There are no per-user permissions,
  no approval step, and no audit of who may touch which directory beyond
  `ALLOWED_ROOTS`.
- **One shared admin token.** Anyone holding `API_TOKEN` can see and control
  every job.
- **Prompt injection reaches a shell.** A job that reads a hostile repository,
  issue or web page may be steered by its contents, and it runs with
  `bypassPermissions`. Do not point jobs at untrusted code you would not run
  yourself.
- **Chat platform compromise is game over.** An attacker with the bot token, or
  control of an allowlisted account, can queue jobs.
- **No sandboxing.** Jobs are ordinary processes on the host with your user's
  access to the filesystem, network, SSH keys and cloud credentials.

## Reporting a vulnerability

Please report privately rather than in a public issue: open a
[draft security advisory](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
on this repository (Security → Report a vulnerability).

Include what an attacker can achieve, the steps to reproduce it, and the
version or commit. A first response should come within a week.

If the report concerns Claude Code itself rather than this queue, send it to
Anthropic at https://www.anthropic.com/responsible-disclosure-policy instead.

## Supported versions

The latest release on `main` is the only supported version; fixes go there
rather than to older tags.
