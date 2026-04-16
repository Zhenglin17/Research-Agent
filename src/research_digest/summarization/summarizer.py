"""Main orchestrator for the LLM summarization stage.

Turns a ranked list of SourceItems into a fully-summarized `Digest`
ready for the delivery layer. Per design §24.7, this is stage 9 of the
pipeline — everything before it is pure-code filtering/ranking; the
LLM does not see anything else.

Flow:
  1. Pick today's model (rotation or fixed).
  2. Build one LLM client (shared across all calls for connection reuse).
  3. Kick off per-item summary calls concurrently via asyncio.gather.
  4. Each item becomes a `DigestEntry` (item + summary text + section).
     Items whose LLM call fails fall back to a minimal summary — the
     whole digest doesn't collapse because one abstract tripped on
     rate limits. (design §25.3: LLM failure must have a bounded fallback)
  5. Compose an intro-prompt payload from the items + first-sentence
     hints and make one more LLM call for the intro paragraph.
  6. Return a `Digest`. Formatting / length-degradation / delivery are
     downstream — this module never touches HTML or Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..config.schema import AppConfig
from ..models.digest import Digest, DigestEntry
from ..models.source_item import SourceItem
from .llm_client import build_client, complete, select_model
from .prompt_builder import (
    PromptLibrary,
    build_intro_messages,
    build_item_messages,
    section_for,
)

log = logging.getLogger(__name__)


def _first_sentence(text: str, max_chars: int = 160) -> str:
    """Cheap hint extractor for the intro prompt.

    Takes the first sentence (up to `max_chars`) of an item's summary so
    the intro LLM can spot themes without re-reading every full summary.
    No NLP — just split on the first period, question, or newline.
    """
    if not text:
        return ""
    # Prefer the first hard sentence break; fall back to a hard cap.
    for sep in (". ", "? ", "! ", "\n"):
        idx = text.find(sep)
        if 0 < idx <= max_chars:
            return text[: idx + 1].strip()
    return text[:max_chars].strip()


def _fallback_summary(item: SourceItem) -> str:
    """Minimal, non-fabricated summary used when an LLM call fails.

    We never invent content; we surface whatever the source itself gave
    us (abstract/summary) plus the title. Keeps the digest useful even
    under partial LLM outage.
    """
    base = item.summary or item.content or ""
    base = base.strip().split("\n")[0][:400]
    if base:
        return f"{item.title}. {base}"
    return item.title


async def _summarize_one(
    item: SourceItem,
    *,
    topic: str,
    library: PromptLibrary,
    client,
    model: str,
    llm_config,
) -> DigestEntry:
    """Produce one DigestEntry. Catches LLM errors to isolate failures."""
    try:
        messages = build_item_messages(item, topic=topic, library=library)
        text = await complete(
            client, model=model, messages=messages, llm_config=llm_config
        )
        if not text:
            text = _fallback_summary(item)
    except Exception as e:  # noqa: BLE001 — bounded fallback per design §25.3
        log.warning(
            "per-item summary failed, using fallback (item_id=%s, source=%s): %s",
            item.id, item.source_id, e,
        )
        text = _fallback_summary(item)
    return DigestEntry(item=item, summary=text, section=section_for(item))


async def summarize_digest(
    items: list[SourceItem],
    *,
    app_config: AppConfig,
    prompts_dir,
    now: datetime,
) -> Digest:
    """Turn ranked items into a `Digest` with LLM-written prose.

    Args:
        items: Items selected by ranking/trim — already capped to the
            target digest size. This stage does not further filter.
        app_config: Full business config. Uses `topic`, `llm`, and the
            model footer via `select_model`.
        prompts_dir: Path to the `prompts/` directory.
        now: Current moment. Its date drives the rotation pick AND the
            digest's `digest_date` field. Injected (rather than called
            internally) so tests can pin it and so every part of the
            run agrees on "today".

    Returns:
        A fully-populated `Digest`: intro + entries + model_used, ready
        for the formatter/delivery layer.

    Called by:
        `pipeline.digest_pipeline` (stage 9 of 12). Downstream stages
        are formatter → telegram_client → history persist.
    """
    today: date = now.astimezone(ZoneInfo(app_config.timezone)).date()
    model = select_model(app_config.llm, today)
    library = PromptLibrary.load(prompts_dir)
    client = build_client(app_config.llm)

    log.info(
        "summarize_digest start: items=%d model=%s topic=%s",
        len(items), model, app_config.topic,
    )

    # 1) Per-item summaries in parallel. gather preserves ordering.
    entry_tasks = [
        _summarize_one(
            it,
            topic=app_config.topic,
            library=library,
            client=client,
            model=model,
            llm_config=app_config.llm,
        )
        for it in items
    ]
    entries: list[DigestEntry] = await asyncio.gather(*entry_tasks)

    # 2) Intro call — needs per-item hints, so it runs after step 1.
    entries_preview = [
        (e.section, e.item.title, _first_sentence(e.summary)) for e in entries
    ]
    intro_messages = build_intro_messages(
        topic=app_config.topic,
        digest_date_iso=today.isoformat(),
        entries_preview=entries_preview,
        library=library,
    )
    try:
        intro = await complete(
            client,
            model=model,
            messages=intro_messages,
            llm_config=app_config.llm,
        )
        if not intro:
            intro = _default_intro(app_config.topic, len(entries))
    except Exception as e:  # noqa: BLE001
        log.warning("intro summary failed, using fallback: %s", e)
        intro = _default_intro(app_config.topic, len(entries))

    log.info(
        "summarize_digest done: entries=%d intro_chars=%d",
        len(entries), len(intro),
    )

    return Digest(
        topic=app_config.topic,
        digest_date=today,
        intro=intro,
        entries=entries,
        model_used=model,
    )


def _default_intro(topic: str, n: int) -> str:
    """Bland-but-honest intro when the LLM intro call fails."""
    return f"Today's {topic} digest: {n} items across papers, blogs, and other sources."
