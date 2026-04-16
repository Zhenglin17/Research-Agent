"""Main pipeline orchestrator.

V1 is a fixed linear sequence (see design §24.7). M4 completes it:

   1. load config
   2. compute fetch window
   3. build sources from sources.yaml
   4. fetch all enabled sources concurrently        → fetched.json/md
   5. dedupe (3-layer within run + history)         → deduped.json/md
   6. filter (hard include/exclude keywords)        → filtered.json/md
   7. rank (weighted score, trim to cap)            → ranked.json/md
   8. summarize with LLM (M4)                       → digest.json/md
   9. deliver to Telegram + record push history     → delivery.json
  10. prune history rows older than retention       (maintenance)

Run modes (passed by the CLI):
  * "dry-run"   — stop after stage 7. No LLM, no Telegram. Used to
                  verify fetchers + ranking in isolation.
  * "debug-run" — run stages 1–8. Writes the digest artifact so you
                  can inspect LLM output, but does NOT push to Telegram
                  and does NOT record history. Safe to re-run.
  * "run"       — all stages. Production mode. Pushes to Telegram,
                  records pushed items, prunes history.

The prune step lives inside the pipeline on purpose: the bot is
expected to run daily via cron, so history maintenance must be part
of every normal run — not a separate command the user has to remember.
Prune failures are logged but never fail the run (§24.6 — specific,
non-fatal error handling for maintenance).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from ..config.loader import load_app_config, load_sources_config
from ..config.schema import AppConfig
from ..delivery.deliver import deliver_prepared, prepare_messages
from ..models.digest import Digest
from ..models.source_item import SourceItem
from ..observability.artifact_store import write_stage_artifact
from ..observability.run_context import RunContext
from ..ranking.filter_rules import apply_filter
from ..ranking.ranker import rank
from ..sources.base import FetchWindow, Source
from ..sources.factory import build_sources
from ..storage.history_store import HistoryStore
from ..summarization.digest_artifact import write_digest_artifact
from ..summarization.summarizer import summarize_digest
from ..summarization.translator import needs_translation, translate_digest
from .dedupe_stage import dedupe

logger = logging.getLogger(__name__)

RunMode = Literal["dry-run", "debug-run", "run"]

# V1 single-user default. History dedupe and push records both key on
# this; multi-user scoping is a V2 concern (§25.8).
_DEFAULT_USER_ID = "default"

# Prompt templates live outside `src/` so non-coders can tweak tone
# without touching Python. Resolved relative to CWD — cron scripts
# are expected to `cd /path/to/repo && ...` (standard pattern).
_PROMPTS_DIR = Path("prompts")


def compute_window(app_config: AppConfig, now: datetime | None = None) -> FetchWindow:
    """Derive the [start, end) time window from config.

    `now` is parameterized so tests can pin the clock. In production we
    pass None and use real UTC now().
    """
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=app_config.limits.lookback_days)
    return FetchWindow(start=start, end=end)


async def _fetch_one(source: Source, window: FetchWindow) -> list[SourceItem]:
    """Catch-all wrapper so one failing source doesn't kill the run."""
    try:
        return await source.fetch(window)
    except Exception as exc:  # noqa: BLE001 — per-source safety net
        logger.error(
            "source fetch failed id=%s error=%s",
            source.source_id,
            exc,
            exc_info=True,
        )
        return []


async def fetch_all(sources: list[Source], window: FetchWindow) -> list[SourceItem]:
    """Run all adapters concurrently, flatten results."""
    logger.info(
        "fetch_all start sources=%d window=[%s, %s)",
        len(sources),
        window.start.isoformat(),
        window.end.isoformat(),
    )
    results = await asyncio.gather(*(_fetch_one(s, window) for s in sources))
    items = [it for batch in results for it in batch]
    logger.info("fetch_all done total_items=%d", len(items))
    return items


def _history_db_path(rc: RunContext) -> Path:
    """Shared sqlite file, not per-run — history must persist across runs."""
    return rc.data_root / "history" / "digest_history.db"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def run_pipeline(
    rc: RunContext,
    *,
    mode: RunMode = "dry-run",
) -> dict:
    """End-to-end pipeline. Returns a summary dict for the CLI.

    Args:
        rc: Run context. Owns run_id, artifact dir, log paths.
        mode: One of "dry-run" | "debug-run" | "run". Controls how far
            down the pipeline we go. See module docstring.

    Returns:
        A dict with keys:
            - mode:            the mode we ran in
            - items_fetched:   count after stage 4
            - items_ranked:    count after stage 7 (empty list → 0)
            - digest:          Digest | None (set when mode != "dry-run")
            - delivery:        {chat_id: [SendResult]} | None (only "run")
            - pruned_rows:     int | None (only "run")

        The CLI uses this to print a short human summary.
    """
    logger.info("pipeline start run_id=%s mode=%s", rc.run_id, mode)

    app_config = load_app_config()
    sources_config = load_sources_config()
    # Wire config-derived observability flag onto the rc so every artifact
    # writer sees the same decision without threading it through every call.
    rc.write_artifacts = app_config.observability.write_artifacts
    logger.info(
        "config loaded topic=%r lookback_days=%d declared_sources=%d write_artifacts=%s",
        app_config.topic,
        app_config.limits.lookback_days,
        len(sources_config.sources),
        rc.write_artifacts,
    )

    summary: dict = {
        "mode": mode,
        "items_fetched": 0,
        "items_ranked": 0,
        "digest": None,
        "delivery": None,
        "pruned_rows": None,
    }

    # try/finally so the disk-bound cleanup of old run dirs runs on every
    # path out of the function — including early returns (no sources, all
    # filtered, etc.) and unhandled exceptions. Disk hygiene is the one
    # thing that must happen every run, no matter what.
    try:
        return await _run_stages(rc, app_config, sources_config, mode, summary)
    finally:
        _prune_run_dirs(rc, app_config.observability.max_runs_kept)


async def _run_stages(
    rc: RunContext,
    app_config: AppConfig,
    sources_config,
    mode: RunMode,
    summary: dict,
) -> dict:
    """Inner pipeline body. Extracted so run_pipeline can wrap it in
    try/finally for unconditional dir cleanup without nesting the logic
    under an extra indent level."""
    window = compute_window(app_config)
    sources = build_sources(sources_config)
    if not sources:
        logger.warning("no enabled+supported sources; nothing to fetch")
        return summary

    # --- Stage 4: fetch -----------------------------------------------------
    items = await fetch_all(sources, window)
    write_stage_artifact(rc, "fetched", items)
    summary["items_fetched"] = len(items)
    if not items:
        logger.info("pipeline done run_id=%s items=0 (no fetched)", rc.run_id)
        return summary

    # --- Stages 5-7: dedupe / filter / rank, sharing one HistoryStore -------
    # We open history here because dedupe reads from it (cross-run dedupe)
    # AND, in "run" mode, delivery and prune will need it too. Keeping the
    # connection open for the whole pipeline lets all three stages share it
    # without reopening the file.
    with HistoryStore(_history_db_path(rc)) as history:
        deduped = dedupe(
            items,
            app_config.dedupe,
            history=history,
            user_id=_DEFAULT_USER_ID,
        )
        write_stage_artifact(rc, "deduped", deduped)
        if not deduped:
            logger.info("pipeline done run_id=%s items=0 (all deduped)", rc.run_id)
            return summary

        filtered = apply_filter(
            deduped,
            include_keywords=app_config.include_keywords,
            exclude_keywords=app_config.exclude_keywords,
        )
        write_stage_artifact(rc, "filtered", filtered)
        if not filtered:
            logger.info("pipeline done run_id=%s items=0 (all filtered)", rc.run_id)
            return summary

        all_ranked, ranked = rank(filtered, app_config=app_config, sources_config=sources_config)
        write_stage_artifact(rc, "ranked", all_ranked)
        summary["items_ranked"] = len(ranked)

        # dry-run stops here.
        if mode == "dry-run":
            logger.info(
                "pipeline done run_id=%s mode=dry-run items=%d",
                rc.run_id, len(ranked),
            )
            return summary

        # --- Stage 8: summarize (debug-run + run) ---------------------------
        if not ranked:
            logger.info(
                "pipeline done run_id=%s items=0 before summarize",
                rc.run_id,
            )
            return summary

        digest: Digest = await summarize_digest(
            ranked,
            app_config=app_config,
            prompts_dir=_PROMPTS_DIR,
            now=datetime.now(timezone.utc),
        )

        # --- Stage 8.5: translate (if language != "en") --------------------
        if needs_translation(app_config):
            digest = await translate_digest(
                digest, app_config=app_config, prompts_dir=_PROMPTS_DIR,
            )

        write_digest_artifact(rc, digest)
        summary["digest"] = digest

        # Apply the length policy once, here — so debug-run can inspect the
        # exact bytes that would be sent (including any Level-3 LLM compress
        # or Level-4 split), and so run mode doesn't waste tokens running
        # the policy twice.
        chunks = await prepare_messages(digest, app_config=app_config)
        _write_telegram_messages_artifact(rc, chunks)

        # debug-run stops here — does not push, does not record, does not prune.
        if mode == "debug-run":
            logger.info(
                "pipeline done run_id=%s mode=debug-run entries=%d chunks=%d",
                rc.run_id, len(digest.entries), len(chunks),
            )
            return summary

        # --- Stage 9: deliver (run only) ------------------------------------
        delivery_results = await deliver_prepared(
            chunks,
            digest,
            app_config=app_config,
            history_store=history,
        )
        _write_delivery_artifact(rc, delivery_results)
        summary["delivery"] = delivery_results

        # --- Stage 10: prune history (run only; non-fatal) ------------------
        summary["pruned_rows"] = _prune_history(history, app_config)

    logger.info(
        "pipeline done run_id=%s mode=run entries=%d pruned=%s",
        rc.run_id,
        len(digest.entries) if digest else 0,
        summary["pruned_rows"],
    )
    return summary


# ---------------------------------------------------------------------------
# Stage 10: retention prune
# ---------------------------------------------------------------------------


def _prune_run_dirs(rc: RunContext, max_runs_kept: int) -> None:
    """Keep only the N most recent run artifact dirs + log files.

    Sweeps two locations:
      * `data/artifacts/run-<id>/` — whole directories
      * `data/logs/run-<id>.log`   — individual log files

    `run_id` starts with a UTC timestamp (YYYYMMDDTHHMMSSZ-<hex>), so a
    plain descending name sort IS a chronological sort — no stat(2)
    calls needed. We keep the top `max_runs_kept` by name and remove
    the rest.

    Non-fatal: any delete error is logged and ignored; partial cleanup
    is fine, and the run has already done its real work. A permanent
    problem (permissions) becomes obvious from the log volume.
    """
    try:
        _prune_entries(rc.data_root / "artifacts", max_runs_kept, is_dir=True)
        _prune_entries(rc.data_root / "logs", max_runs_kept, is_dir=False)
    except Exception as e:  # noqa: BLE001
        logger.error("run-dir prune failed (non-fatal): %s", e)


def _prune_entries(parent: Path, max_kept: int, *, is_dir: bool) -> None:
    """Delete all but the top-`max_kept` entries in `parent` (sorted desc).

    Only considers entries whose name starts with "run-" — leaves any
    unrelated files alone (e.g. a README a user might drop in).
    """
    if not parent.exists():
        return

    candidates = [
        p for p in parent.iterdir()
        if p.name.startswith("run-") and (p.is_dir() if is_dir else p.is_file())
    ]
    if len(candidates) <= max_kept:
        return

    # Name-sort desc: newest first. Keep head, delete tail.
    candidates.sort(key=lambda p: p.name, reverse=True)
    to_delete = candidates[max_kept:]
    for p in to_delete:
        try:
            if is_dir:
                _rmtree(p)
            else:
                p.unlink()
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to delete %s: %s", p, e)

    logger.info(
        "run-dir prune: kept=%d deleted=%d in %s",
        max_kept, len(to_delete), parent,
    )


def _rmtree(path: Path) -> None:
    """Recursive delete — stdlib's shutil.rmtree wrapped so tests can
    monkey-patch this without touching shutil globally."""
    import shutil
    shutil.rmtree(path)


def _prune_history(history: HistoryStore, app_config: AppConfig) -> int:
    """Delete digest_history rows older than the configured retention.

    Non-fatal: if this raises, we log and return 0 — the digest has
    already been delivered, and accumulating a few extra rows until
    tomorrow's run is harmless.

    Called by:
        `run_pipeline` (mode="run" only), as the last stage before
        the history store is closed.
    """
    days = app_config.retention.history_days
    try:
        deleted = history.prune_older_than(days)
        logger.info("history prune ok retention_days=%d deleted=%d", days, deleted)
        return deleted
    except Exception as e:  # noqa: BLE001
        logger.error("history prune failed (non-fatal): %s", e)
        return 0


# ---------------------------------------------------------------------------
# Delivery artifact (lives here, not in formatter, because the shape is
# orchestration output, not a renderable product)
# ---------------------------------------------------------------------------


def _write_telegram_messages_artifact(rc: RunContext, chunks: list[str]) -> None:
    """Dump the final length-policy output to `telegram_messages.txt`.

    This is the byte-for-byte payload the Telegram transport will send
    (or would have sent, in debug-run). Writing it lets you diff the
    pre-formatter `digest.md` against the post-formatter + post-length-
    policy text — the only way to see Level-3 LLM compress or Level-4
    split effects without actually pushing.

    Multiple chunks are joined with a clearly marked separator so the
    file stays a single artifact rather than N numbered files.
    """
    if not rc.write_artifacts:
        return
    rc.ensure_dirs()
    path = rc.artifact_dir / "telegram_messages.txt"
    sep = "\n\n" + ("-" * 40) + " chunk boundary " + ("-" * 40) + "\n\n"
    path.write_text(sep.join(chunks), encoding="utf-8")
    logger.info(
        "artifact written stage=telegram_messages chunks=%d path=%s",
        len(chunks), path,
    )


def _write_delivery_artifact(rc: RunContext, delivery_results: dict) -> None:
    """Dump per-chat_id send results to `delivery.json` for debugging.

    SendResult is a dataclass — we hand-serialize it because pydantic
    isn't involved. If delivery results grow more structure, promote
    this to a proper helper alongside `write_digest_artifact`.
    """
    if not rc.write_artifacts:
        return
    rc.ensure_dirs()
    path = rc.artifact_dir / "delivery.json"
    serializable = {
        chat_id: [
            {
                "chat_id": r.chat_id,
                "ok": r.ok,
                "status_code": r.status_code,
                "error": r.error,
            }
            for r in results
        ]
        for chat_id, results in delivery_results.items()
    }
    path.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("artifact written stage=delivery path=%s", path)
