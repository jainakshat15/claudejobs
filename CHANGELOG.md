# Changelog

Notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-09-20

First release.

### Added

- **Job queue in Postgres.** One `jobs` table holding the job payload, queue
  state, worker state and the chat message the job came from; supporting tables
  for history (`job_events`), the Claude/human conversation (`job_messages`) and
  undelivered chat messages (`outbound_messages`).
- **HTTP API** (`claudejobs api`) — job CRUD, poster controls (cancel, retry,
  edit-before-start, answer), and job-scoped routes a running session calls with
  its own token.
- **Dispatcher** (`claudejobs dispatcher`) — claims `MAX_CONCURRENT_JOBS` at a
  time using `FOR UPDATE SKIP LOCKED`, opens a terminal per job, and reaps jobs
  whose worker stopped heartbeating.
- **Two-way conversation.** `jobctl ask` blocks the session, delivers the
  question to the chat the job came from, and returns the human's answer on
  stdout. Replies route by replied-to message, by thread, or by explicit job id;
  ambiguous answers are refused rather than guessed.
- **Telegram and Slack bots** over one shared command layer, each command
  mapping to a single HTTP route.
- **Operator CLI** — `selfcheck`, `migrate`, `submit`, `jobs`, `status`,
  `cancel`, `retry`, `answer`, `stats`, `secret`, and `all` to run every
  configured service in one window.
- **Safety rails** — per-job tokens stored hashed, `ALLOWED_ROOTS`, bot
  allowlists that refuse to start empty, leases and job timeouts, and a markdown
  transcript of every HTTP call.
- **Documentation** — setup, architecture, API reference, operations runbook,
  and per-platform bot guides.

[Unreleased]: https://github.com/jainakshat15/claudejobs/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jainakshat15/claudejobs/releases/tag/v1.0.0
