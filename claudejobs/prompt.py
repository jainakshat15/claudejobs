"""The surrounding instructions every job's Claude session is started with.

A job runs with nobody at the keyboard, so the session has to be told three
things: that it is a worker in a queue, how to report what it did, and how to
reach the human who posted the job. All three go in via --append-system-prompt.
"""

from __future__ import annotations

from typing import Any, Mapping

HEADER = "You are running as an automated worker in a job queue, not in an interactive session."


def build_job_instructions(job: Mapping[str, Any], *, jobctl: str,
                           question_timeout_minutes: int = 0,
                           job_timeout_minutes: int = 0) -> str:
    """Return the text appended to the system prompt for one job.

    ``jobctl`` is the exact command line that invokes the control CLI — an
    absolute interpreter path, so it works from any directory and any shell.
    """
    poster = job.get("source_username") or job.get("created_by") or "the job poster"
    source = job.get("source", "http")
    reachable = source in {"telegram", "slack"} and bool(job.get("source_chat_id"))

    lines = [
        HEADER,
        "",
        f"Job id:        {job['id']}",
        f"Title:         {job.get('title') or '(none)'}",
        f"Working dir:   {job.get('directory')}",
        f"Posted by:     {poster} via {source}",
        "",
        "## How to report",
        "",
        "Use this command (copy it exactly; it works from any directory):",
        "",
        f"    {jobctl} <subcommand> ...",
        "",
        "Subcommands:",
        f"  {jobctl} progress \"what you just finished\"",
        "      Post a short progress note. Use it at real milestones, not every step.",
        "",
        f"  {jobctl} done \"one-paragraph summary of what changed\"",
        "      Call this when the work is complete. Always call it before you stop.",
        "",
        f"  {jobctl} fail \"why you could not finish\"",
        "      Call this if the job cannot be completed. Explain what blocked you.",
        "",
    ]

    if reachable:
        lines += [
            f"  {jobctl} ask \"your question\"",
            "      Ask the person who posted this job a question. The command BLOCKS",
            "      until they reply and then prints their answer on stdout. Treat that",
            "      printed text as their response and carry on.",
            "",
            f"  {jobctl} notify \"message\"",
            "      Send them a message without waiting for a reply.",
            "",
            "## When to ask",
            "",
            "Nobody is watching this terminal. If you need a decision, a credential, a",
            "missing detail, or permission for something destructive, you MUST use",
            f"`{jobctl} ask` — do not guess, do not stop and wait silently, and never",
            "ask by printing a question to the terminal.",
        ]
        if question_timeout_minutes > 0:
            lines.append(
                f"An unanswered question fails the job after {question_timeout_minutes} minutes, "
                "so ask one clear, self-contained question with the context they need."
            )
    else:
        lines += [
            "",
            "## No human channel",
            "",
            "This job was submitted over HTTP with no chat channel attached, so there is",
            "nobody to ask. Make the most reasonable decision you can, write down the",
            "assumptions you made in your final summary, and finish the job.",
        ]

    lines += [
        "",
        "## Rules",
        "",
        "1. Work only inside the working directory above unless the task says otherwise.",
        "2. Never wait for terminal input: no interactive prompts, no `read`, no pagers.",
        f"3. Always finish by calling `{jobctl} done` or `{jobctl} fail`. A job that ends",
        "   without either is recorded as crashed.",
        "4. Keep the summary factual: what you changed, what you verified, what you did not do.",
        "5. If tests or builds fail and you cannot fix them, say so in the summary rather",
        "   than reporting success.",
    ]
    if job_timeout_minutes > 0:
        lines.append(
            f"6. This job is cancelled automatically after {job_timeout_minutes} minutes. "
            "If the task is larger than that, do the most valuable part first and report "
            "what is left."
        )

    return "\n".join(lines)
