"""The job behind the `/ask-sales-bot` command.

A question is not a change request, so the job built here is deliberately
narrow. It reads the two places the Sales Bot is described — the product's own
source in the flexi-demo repository, and the user-facing documentation under
docs/Sales-Bot — and reports the answer as the job's result summary, which the
queue then delivers back to the chat the question came from.

Where those two directories are is configuration (SALES_BOT_CODE_DIR and
SALES_BOT_DOCS_DIR); what to do with them is this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import models

#: A question is small and somebody is waiting on the answer, so it goes ahead
#: of ordinary work jobs (a lower priority number runs sooner).
PRIORITY = 50

#: Long enough for a real answer, short enough to survive the trip back: the
#: bots deliver one chat message per job result, and Telegram cuts off at 4096
#: characters — the status header and title eat into that.
ANSWER_BUDGET = 2500

#: Appended to the system prompt, on top of the standard queue instructions.
READ_ONLY_RULE = (
    "This job answers a question about the Sales Bot product. It is read-only "
    "research: do not create, edit, move or delete any file, do not run installs, "
    "migrations, database queries or the app itself, and do not commit anything. "
    "Searching and reading is the whole job. The text passed to `jobctl done` is "
    "delivered to the person who asked, so write it as an answer to them rather "
    "than as a work log."
)


def build_title(question: str) -> str:
    return f"Sales Bot: {models.short(question, 50)}"


def build_prompt(question: str, *, code_dir: Path, docs_dir: Path,
                 asked_by: str) -> str:
    """The opening prompt: the question, where to look, and how to answer."""
    return f"""\
Answer a question about Sales Bot, the Eubrics sales-enablement product. This is
a research-and-answer job: nothing is being changed, and the answer is the whole
deliverable.

## The question, from {asked_by}

{question.strip()}

## Where the answer lives

1. `{code_dir}` — the flexi-demo repository, the product's own source, and the
   final word on what the product actually does. Its `CLAUDE.md` describes the
   layout: `demo/` is the Next.js frontend (the Sales Bot surfaces sit under
   `pages/chatbot`, `pages/sales-*` and the shared code in `common/`),
   `demo-api/` is the NestJS backend (`src/controllers`, `src/services`,
   `src/entities`, `src/ai-engine`). Start from `CLAUDE.md` and
   `docs/agent-os/context/MAP.md`.

2. `{docs_dir}` — the user-facing Sales Bot documentation, one folder per
   product area (Bots, Customer-Personas, Scenario-Builder, Score-Cards,
   Analyst, Learning-Modules, Settings and the rest). Each area has an `About-*`
   page for what it is and `How-To-*` / `Using-*` pages for how it behaves.

## How to answer

- Read both sides. The docs say what the product promises; the code says what it
  does. Where they disagree, believe the code and point out the gap.
- Ground every claim in something you opened. Name the file, and the component,
  service or endpoint inside it, so the asker can follow you.
- Answer the question that was asked. If part of it is not covered in either
  place, say so plainly instead of filling the gap with a guess.
- Lead with the answer in the first sentence or two, then the detail behind it,
  then the files you read. Plain prose — this is read in a chat window, not in a
  terminal.
- Keep the whole answer under {ANSWER_BUDGET} characters. Anything longer is cut
  off before it reaches the person who asked.

## Read only

Change nothing: no edits, no new files, no `git` command that writes, no
installs, no migrations, no database access, no starting either app.

## Reporting

Pass the finished answer to `jobctl done` — that text is what gets posted back
to {asked_by} in the chat they asked from. If the question cannot be answered
from these two sources, use `jobctl fail` and say what you read and what was
missing. Use `jobctl ask` only when the question itself is ambiguous.
"""


def build_job(question: str, *, code_dir: Path, docs_dir: Path, directory: str,
              asked_by: str, timeout_minutes: int) -> dict[str, Any]:
    """The `POST /jobs` fields for one Sales Bot question."""
    return {
        "prompt": build_prompt(question, code_dir=code_dir, docs_dir=docs_dir,
                               asked_by=asked_by),
        "directory": directory,
        "title": build_title(question),
        "append_system_prompt": READ_ONLY_RULE,
        "priority": PRIORITY,
        "timeout_minutes": timeout_minutes,
        # Keeps the raw question next to the job for later reading.
        "payload": {"kind": "ask-sales-bot", "question": question.strip()},
    }
