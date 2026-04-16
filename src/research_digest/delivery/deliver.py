"""Delivery orchestration: Digest → sized message(s) → Telegram → history.

This is the glue that sits between the pure formatter and the dumb
Telegram transport. Its two responsibilities:

  1. Length policy: render the full digest uncompressed, then split at
     entry boundaries to fit Telegram's 4096-char hard limit. No entries
     are dropped, no LLM compression is applied.

  2. Push fan-out:
       Send each prepared chunk to every chat_id. Record one push per
       entry in HistoryStore on at-least-one success (V1 uses a single
       shared record across chat_ids — §25.8 / push-record memory).
"""

from __future__ import annotations

import logging

from ..config.schema import AppConfig
from ..models.digest import Digest
from ..storage.history_store import HistoryStore
from .formatter import render_digest, split_message
from .telegram_client import SendResult, TelegramClient

log = logging.getLogger(__name__)

# V1: single shared push record across chat_ids — the same digest text
# goes to every recipient, so deduping per-recipient would just double
# the rows without adding information. See §25.8.
_PUSH_USER_ID = "default"


# ---------------------------------------------------------------------------
# Length policy
# ---------------------------------------------------------------------------


async def prepare_messages(
    digest: Digest,
    *,
    app_config: AppConfig,
) -> list[str]:
    """Render the full digest and split into Telegram-sized chunks.

    No compression or entry-dropping — every entry is kept intact.
    Messages are split at entry boundaries (blank lines) to stay
    under Telegram's 4096-char hard limit.

    Args:
        digest: Fully summarized digest from the summarization layer.
        app_config: Business config. Reads `output.telegram_hard_limit`.

    Returns:
        A list of 1+ strings, each within the hard limit.

    Called by:
        `deliver_digest` (below), once per run.
    """
    hard = app_config.output.telegram_hard_limit
    rendered = render_digest(digest)

    if len(rendered) <= hard:
        log.info("length policy: fits in one message (chars=%d)", len(rendered))
        return [rendered]

    chunks = split_message(rendered, hard)
    log.info(
        "length policy: split into %d chunks (total_chars=%d)",
        len(chunks), len(rendered),
    )
    return chunks


# ---------------------------------------------------------------------------
# Push fan-out
# ---------------------------------------------------------------------------


async def _send_chunks_to_chat(
    client: TelegramClient, chat_id: str, chunks: list[str],
) -> list[SendResult]:
    """Send every chunk sequentially to one chat_id (preserves order)."""
    results: list[SendResult] = []
    for chunk in chunks:
        r = await client.send_message(chat_id, chunk)
        results.append(r)
        if not r.ok:
            # Stop mid-chat on failure — sending chunk 2 without chunk 1
            # would be worse than sending nothing.
            break
    return results


async def deliver_prepared(
    chunks: list[str],
    digest: Digest,
    *,
    app_config: AppConfig,
    history_store: HistoryStore,
) -> dict[str, list[SendResult]]:
    """Send already-prepared chunks → record → return per-chat results.

    Split out from `deliver_digest` so the pipeline can call
    `prepare_messages` itself, save the chunks as an artifact for
    debugging (`telegram_messages.txt`), and only THEN hand them to
    this function for actual transport — without running the length
    policy (and its potential LLM compress call) twice.

    Args:
        chunks: Output of `prepare_messages`. One or two strings.
        digest: Original digest — needed here only to walk
            `digest.entries` and record them in history after a
            successful send.
        app_config: Business config (used for `telegram.*` only).
        history_store: Opened store; written to when at least one
            chat_id's delivery fully succeeds (§25.8).

    Returns:
        Mapping `{chat_id: [SendResult, ...]}`.

    Behavior on partial failure:
        - At least one chat_id fully succeeds → record once.
        - Every chat_id failed → do not record (next run retries).
        - No transport exceptions propagate.
    """
    if not app_config.telegram.chat_ids:
        log.warning("no telegram chat_ids configured; nothing to deliver")
        return {}

    log.info(
        "delivery plan: %d chunk(s), %d recipient(s)",
        len(chunks), len(app_config.telegram.chat_ids),
    )

    results_by_chat: dict[str, list[SendResult]] = {}
    any_full_success = False

    async with TelegramClient(app_config.telegram) as client:
        for chat_id in app_config.telegram.chat_ids:
            results = await _send_chunks_to_chat(client, chat_id, chunks)
            results_by_chat[chat_id] = results
            if all(r.ok for r in results) and len(results) == len(chunks):
                any_full_success = True

    if any_full_success:
        _record_push_for_entries(digest, history_store)
    else:
        log.error("all chat_ids failed; skipping history record")

    return results_by_chat


async def deliver_digest(
    digest: Digest,
    *,
    app_config: AppConfig,
    history_store: HistoryStore,
) -> dict[str, list[SendResult]]:
    """Backwards-compatible one-shot: length policy + send + record.

    Kept for callers (and tests) that don't need the intermediate
    chunks artifact. Pipeline now prefers the two-step flow
    (`prepare_messages` → `deliver_prepared`) so it can write the
    chunks to disk between preparation and transport.
    """
    if not app_config.telegram.chat_ids:
        log.warning("no telegram chat_ids configured; nothing to deliver")
        return {}
    chunks = await prepare_messages(digest, app_config=app_config)
    return await deliver_prepared(
        chunks, digest, app_config=app_config, history_store=history_store,
    )


def _record_push_for_entries(digest: Digest, history_store: HistoryStore) -> None:
    """Write one history row per entry in the digest.

    Any single row failure (missing content_hash etc.) is logged but
    doesn't block the rest — partial history is better than none.
    """
    recorded = 0
    for entry in digest.entries:
        try:
            history_store.record_push(entry.item, user_id=_PUSH_USER_ID)
            recorded += 1
        except Exception as e:  # noqa: BLE001
            log.error(
                "failed to record push for item %s: %s",
                entry.item.id, e,
            )
    log.info("history: recorded %d/%d entries", recorded, len(digest.entries))
