"""The products a chat question can be asked about, and the job that answers one.

Every product has the same shape: a repository holding the code that decides
what it actually does, and a documentation tree describing what it promises. A
question about any of them is therefore the same job — read both, answer in the
chat it was asked in, change nothing — so what differs between products is data,
not code.

Adding one means adding an entry to CATALOGUE below. Its two directories are
then configurable as <KEY>_CODE_DIR and <KEY>_DOCS_DIR, which config.py resolves
(see Settings.products).
"""

from __future__ import annotations

from dataclasses import dataclass
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

#: Reading across a repository and a documentation tree is slower than a
#: one-file question, and nowhere near a build job.
DEFAULT_TIMEOUT_MINUTES = 20

#: Appended to the system prompt, on top of the standard queue instructions.
READ_ONLY_RULE = (
    "This job answers a question about an Eubrics product. It is read-only "
    "research: do not create, edit, move or delete any file, do not run installs, "
    "migrations, database queries or the app itself, and do not commit anything. "
    "Searching and reading is the whole job. The text passed to `jobctl done` is "
    "delivered to the person who asked, so write it as an answer to them rather "
    "than as a work log."
)


@dataclass(frozen=True)
class Product:
    """One product, and where its two sources of truth live.

    ``code_dir`` and ``docs_dir`` hold relative defaults in CATALOGUE; config.py
    replaces them with resolved absolute paths and fills in ``directory``, the
    working directory a job that has to read both can run in.
    """

    key: str                     # "sales-bot"
    name: str                    # "Sales Bot"
    command: str                 # "ask_sales_bot"
    aliases: tuple[str, ...]
    code_dir: Path
    docs_dir: Path
    code_hint: str               # how to navigate the source
    docs_hint: str               # how the documentation is laid out
    example: str                 # a question worth showing in the help
    directory: str = ""
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES

    @property
    def env_prefix(self) -> str:
        """SALES_BOT, for SALES_BOT_CODE_DIR and friends."""
        return self.key.upper().replace("-", "_")

    @property
    def names(self) -> tuple[str, ...]:
        """Every chat command that reaches this product."""
        return (self.command, *self.aliases)


CATALOGUE: tuple[Product, ...] = (
    Product(
        key="sales-bot",
        name="Sales Bot",
        command="ask_sales_bot",
        aliases=("sales_bot", "salesbot"),
        code_dir=Path("flexi-demo"),
        docs_dir=Path("docs/Sales-Bot"),
        code_hint=(
            "the flexi-demo repository, the product's own source and the final word on "
            "what it actually does. `demo/` is the Next.js frontend (the Sales Bot "
            "surfaces sit under `pages/chatbot`, `pages/sales-*`, with shared code in "
            "`common/`); `demo-api/` is the NestJS backend (`src/controllers`, "
            "`src/services`, `src/entities`, `src/ai-engine`). Start from `CLAUDE.md` "
            "and `docs/agent-os/context/MAP.md`."
        ),
        docs_hint=(
            "the user-facing Sales Bot documentation, one folder per product area "
            "(Bots, Customer-Personas, Scenario-Builder, Score-Cards, Analyst, "
            "Learning-Modules, Settings and the rest). Each area has an `About-*` page "
            "for what it is and `How-To-*` / `Using-*` pages for how it behaves."
        ),
        example="how does a rep get scored on a call?",
    ),
    Product(
        key="od",
        name="Organizational Development",
        command="ask_od",
        aliases=("od",),
        code_dir=Path("OD"),
        docs_dir=Path("docs/Organizational-Development"),
        code_hint=(
            "the od-application workspace, the product's own source and the final word "
            "on what it actually does. It holds two independent repositories: "
            "`eubrics-flexi/` (Next.js 14, App Router, code under `src/`) and `leadx/` "
            "(NestJS on Nx — `apps/api`, `apps/cronjob`, `apps/frontend`, with shared "
            "DTOs in `libs/`). Start from the root `CLAUDE.md`, then the `CLAUDE.md` "
            "inside whichever repository you end up in; cross-repo notes live in "
            "`docs/` (architecture, domain, api-contract, decisions). Both app folders "
            "are gitignored at the workspace root and re-admitted to search by the "
            "root `.ignore` file — if a search turns up nothing inside them, that is "
            "why, and the fix is never to edit `.gitignore`."
        ),
        docs_hint=(
            "the user-facing Organizational Development documentation, one folder per "
            "product area (Journeys, Modules, Module-Cycles, Assessments, Batches, "
            "Challenges, AI-Roleplays, Pulse-Survey, Score-Cards, Sessions, Analyst, "
            "Learner and the rest). Each area has an `About-*` page for what it is and "
            "`How-To-*` / `Using-*` pages for how it behaves."
        ),
        example="how does a journey cycle unlock its modules?",
    ),
)


def find(key: str) -> Product | None:
    return next((product for product in CATALOGUE if product.key == key), None)


def build_title(product: Product, question: str) -> str:
    return f"{product.name}: {models.short(question, 50)}"


def build_prompt(product: Product, question: str, *, asked_by: str) -> str:
    """The opening prompt: the question, where to look, and how to answer."""
    return f"""\
Answer a question about {product.name}, an Eubrics product. This is a
research-and-answer job: nothing is being changed, and the answer is the whole
deliverable.

## The question, from {asked_by}

{question.strip()}

## Where the answer lives

1. `{product.code_dir}` — {product.code_hint}

2. `{product.docs_dir}` — {product.docs_hint}

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
installs, no migrations, no database access, no starting any of the apps.

## Reporting

Pass the finished answer to `jobctl done` — that text is what gets posted back
to {asked_by} in the chat they asked from. If the question cannot be answered
from these two sources, use `jobctl fail` and say what you read and what was
missing. Use `jobctl ask` only when the question itself is ambiguous.
"""


def build_job(product: Product, question: str, *, asked_by: str) -> dict[str, Any]:
    """The `POST /jobs` fields for one product question."""
    return {
        "prompt": build_prompt(product, question, asked_by=asked_by),
        "directory": product.directory,
        "title": build_title(product, question),
        "append_system_prompt": READ_ONLY_RULE,
        "priority": PRIORITY,
        "timeout_minutes": product.timeout_minutes,
        # Keeps the raw question next to the job for later reading.
        "payload": {"kind": f"ask-{product.key}", "question": question.strip()},
    }
