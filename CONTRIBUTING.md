# Contributing

Thanks for taking a look. Bug reports and small focused pull requests are both
welcome.

## Getting set up

```bash
git clone https://github.com/YOUR-USERNAME/claudejobs.git && cd claudejobs
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

You do **not** need Postgres, Claude Code, or a bot token to run the unit tests.

## Running the tests

```bash
pytest                    # unit tests only; the database tests skip themselves
```

For the full suite, give it a throwaway Postgres:

```bash
docker run -d --name claudejobs-test \
    -e POSTGRES_PASSWORD=test -e POSTGRES_DB=claudejobs \
    -p 55432:5432 postgres:16-alpine

export CLAUDEJOBS_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/claudejobs
pytest                    # now runs the queue and API tests too
```

The schema is created from the real migration files, and every test starts from
empty tables. CI runs exactly this on Linux, plus a unit-only pass on Windows —
service containers aren't available on Windows runners, and the launcher code is
platform-specific enough to be worth checking there anyway.

## Linting

```bash
ruff check claudejobs tests
```

Lint is enforced in CI; formatting is not. There is no auto-formatter on this
codebase, because several modules use hand-aligned SQL and dict literals that a
formatter would spread over more lines than they deserve. Match the surrounding
style.

## What a good change looks like

- **A test that fails without your fix.** The queue's behaviour lives in
  `tests/test_repository.py` (concurrency, leases, questions) and
  `tests/test_api.py` (routes end to end); pure logic goes in
  `tests/test_unit.py`.
- **SQL stays in `repository.py`.** That's deliberate: the concurrency rules are
  readable in one place. Please don't scatter queries into route handlers.
- **Schema changes are new migration files.** Never edit an applied one — the
  runner compares checksums and will refuse. Add `migrations/000N_what.sql`.
- **Docs updated alongside.** If you add or change a route, update
  `docs/API.md`; if you add a setting, add it to `.env.example` with a comment
  explaining what it does and what a sane value looks like.
- **Error messages that say what to do.** The house style is that a failure
  names the setting or command that fixes it — see `config.py` for examples.

## Things to be careful with

- **Launching.** `launcher.py` and `claude_cli.py` work around two real
  platform traps: terminals that don't inherit the environment, and `.cmd`
  shims that truncate arguments at the first newline. Both are commented; please
  read before changing.
- **Anything touching permissions, allowlists or tokens.** Say in the PR what
  the security consequence is. See [SECURITY.md](SECURITY.md).

## Pull requests

Keep them focused, explain what you changed and how you checked it, and say
which platform you tested on. If you found a security issue, please report it
privately instead — see [SECURITY.md](SECURITY.md).
