"""Rank items: compute score for each, sort descending, trim to a cap.

This module is the thin orchestrator on top of scoring.py. It's where the
score gets written back onto the item (`item.score`) so downstream stages
and the ranked artifact carry the value without a side map.

Per-source weights come from sources.yaml (the `weight` field on each
source entry). We build a small lookup so the ranker stays indifferent
to source-config shape.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..config.schema import AppConfig
from ..config.sources_schema import SourcesConfig
from ..models.source_item import SourceItem
from .scoring import compute_score

logger = logging.getLogger(__name__)


def _source_weights(sources_config: SourcesConfig) -> dict[str, float]:
    """source_id → configured weight. Unknown source_ids fall back to 1.0."""
    return {s.id: s.weight for s in sources_config.sources}


def rank(
    items: list[SourceItem],
    *,
    app_config: AppConfig,
    sources_config: SourcesConfig,
    now: datetime | None = None,
) -> tuple[list[SourceItem], list[SourceItem]]:
    """Score every item, sort descending by score, return (all_sorted, selected).

    Mutates `item.score` in place. Returns two lists:
      - all_sorted: every item with its score, sorted desc. Written to the
        ranked artifact so you can see where cut-off fell.
      - selected: the top `max_digest_items` that go forward to summarization.
    """
    if not items:
        logger.info("rank done in=0 out=0")
        return [], []

    weight_map = _source_weights(sources_config)
    lookback = app_config.limits.lookback_days
    cap = app_config.limits.max_digest_items

    for item in items:
        src_w = weight_map.get(item.source_id, 1.0)
        item.score = compute_score(
            item,
            topic=app_config.topic,
            focus_keywords=app_config.focus_keywords,
            source_weight=src_w,
            weights=app_config.ranking,
            lookback_days=lookback,
            now=now,
        )

    # Sort by score desc. Ties: newer first (published_at desc) so the
    # ordering is at least stable and human-sensible when scores collide.
    all_sorted = sorted(
        items,
        key=lambda it: (it.score or 0.0, it.published_at),
        reverse=True,
    )

    selected = all_sorted[:cap]
    logger.info(
        "rank done in=%d out=%d top_score=%.3f bottom_score=%.3f",
        len(items),
        len(selected),
        selected[0].score or 0.0,
        selected[-1].score or 0.0,
    )
    return all_sorted, selected
