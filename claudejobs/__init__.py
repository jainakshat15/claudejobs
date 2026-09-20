"""claudejobs — a Postgres-backed job queue that runs Claude Code sessions.

Modules
-------
config       Settings loaded from the environment / .env
db           Connection pool helpers
migrate      SQL migration runner (``claudejobs migrate up``)
models       Job status constants and human-readable formatting
repository   Every SQL statement in the system lives here
api          FastAPI app: job CRUD, poster controls, job-scoped endpoints
dispatcher   Claims queued jobs and launches a terminal for each one
launcher     Cross-platform "open a terminal and run this" helper
run_job      Wrapper that runs *inside* each job terminal
jobctl       CLI a running Claude session uses to report status and ask questions
prompt       The surrounding instructions injected into every job
client       Small HTTP client shared by the bots, jobctl and the CLI
bots         Telegram and Slack front ends over a shared command layer
"""

__version__ = "1.0.0"
