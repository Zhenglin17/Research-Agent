"""Command-line entrypoint.

Deliberately thin: this module only wires together RunContext + logging +
the pipeline, then prints a short summary. Business logic belongs in the
pipeline and its stages, not here.

Subcommands:
  * dry-run    — stages 1-7 only. No LLM, no Telegram. Inspect fetchers
                 + ranking in isolation.
  * debug-run  — stages 1-8. Runs the LLM summarize step and writes the
                 digest artifact, but does NOT push to Telegram and does
                 NOT record history. Safe to iterate on prompts.
  * run        — all stages. Production mode invoked by cron. Pushes,
                 records, prunes.
"""

from __future__ import annotations

import asyncio
import logging

import typer

from ..observability.logger import setup_logging
from ..observability.run_context import RunContext
from ..pipeline.digest_pipeline import RunMode, run_pipeline

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Research Digest Bot — V1 CLI.",
)


@app.callback()
def _root() -> None:
    """Force typer into multi-command mode so `digest dry-run` is a subcommand,
    not a positional arg (typer collapses single-command apps otherwise)."""


def _run_mode(mode: RunMode) -> None:
    """Shared entry logic for all three subcommands.

    Each subcommand is a tiny wrapper that picks the mode and forwards
    here. Keeping the wiring in one place means artifact printing /
    error handling stays consistent across modes.
    """
    rc = RunContext()
    # Only pre-create the log dir — setup_logging needs it immediately.
    # The artifact dir is lazy (created by writers via ensure_dirs) so
    # we don't leave empty folders behind when observability.write_artifacts
    # is False in config.
    rc.log_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(rc)
    log = logging.getLogger("cli")

    try:
        summary = asyncio.run(run_pipeline(rc, mode=mode))
    except Exception:
        log.exception("pipeline crashed")
        raise typer.Exit(code=1)

    # Human summary — mirrors what's in the logs, but condensed.
    typer.echo("")
    typer.echo(f"mode          : {summary['mode']}")
    typer.echo(f"run_id        : {rc.run_id}")
    typer.echo(f"artifacts     : {rc.artifact_dir}")
    typer.echo(f"log file      : {rc.log_file}")
    typer.echo(f"items fetched : {summary['items_fetched']}")
    typer.echo(f"items ranked  : {summary['items_ranked']}")

    digest = summary.get("digest")
    if digest is not None:
        typer.echo(f"digest entries: {len(digest.entries)}")
        typer.echo(f"model used    : {digest.model_used}")

    delivery = summary.get("delivery")
    if delivery is not None:
        total = sum(len(rs) for rs in delivery.values())
        ok = sum(1 for rs in delivery.values() for r in rs if r.ok)
        typer.echo(f"telegram send : {ok}/{total} chunks ok "
                   f"across {len(delivery)} chat(s)")

    pruned = summary.get("pruned_rows")
    if pruned is not None:
        typer.echo(f"history prune : deleted {pruned} row(s)")


@app.command("dry-run")
def dry_run() -> None:
    """Fetch + rank. No LLM, no Telegram."""
    _run_mode("dry-run")


@app.command("debug-run")
def debug_run() -> None:
    """Fetch + rank + summarize. Writes digest artifact. No Telegram push."""
    _run_mode("debug-run")


@app.command("run")
def run() -> None:
    """Full pipeline: fetch + rank + summarize + push + record + prune."""
    _run_mode("run")


def main() -> None:
    """Console-script entrypoint (referenced from pyproject.toml)."""
    app()


if __name__ == "__main__":
    main()
