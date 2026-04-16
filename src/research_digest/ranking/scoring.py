"""Deterministic per-item scoring. No LLM, no I/O — all pure functions.

The composite score is a weighted sum (see RankingWeights docstring):

    score = w_topic * topic_match_ratio
          + w_source * source_weight
          + w_focus * focus_hit_count
          + w_freshness * freshness
          + w_llm_relevance * llm_relevance     # V1 constant 0

Each component returns a float. Components are intentionally simple and
independently testable — the ranker just multiplies by weights and sums.
Swapping any single component for something smarter later is a drop-in.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..config.schema import RankingWeights
from ..models.source_item import SourceItem

_WORD_RE = re.compile(r"[a-z0-9]+")

# Same tiny stopword set as dedupe_stage — kept in sync by convention, not
# by import, to avoid cross-module coupling over a 20-word constant.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "in", "on", "for", "to", "and", "or",
        "is", "are", "was", "were", "be", "by", "with", "at", "from",
        "as", "that", "this", "these", "those", "it", "its",
    }
)


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall(text.lower()) if t not in _STOPWORDS}


def _haystack(item: SourceItem) -> str:
    return " ".join([item.title, item.summary or "", item.content or ""]).lower()


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def topic_match_ratio(item: SourceItem, topic: str) -> float:
    """Fraction of non-stopword topic tokens that appear in the item.

    topic="cancer immunotherapy" → tokens {cancer, immunotherapy}. If the
    item's title+summary contain both, ratio=1.0. If one, 0.5. None, 0.0.
    Range: [0.0, 1.0]. Empty topic → 0.0.
    """
    topic_tokens = _tokens(topic)
    if not topic_tokens:
        return 0.0
    hay_tokens = _tokens(_haystack(item))
    hits = topic_tokens & hay_tokens
    return len(hits) / len(topic_tokens)


def focus_hit_count(item: SourceItem, focus_keywords: list[str]) -> float:
    """Number of focus keywords that appear (substring match) in the item.

    Returns a float (not int) so the scoring formula stays in floats.
    Not normalized: 3 focus hits is intentionally more than 1 hit. If
    that ever feels too aggressive, cap or take log here.
    """
    if not focus_keywords:
        return 0.0
    hay = _haystack(item)
    return float(sum(1 for kw in focus_keywords if kw and kw.lower() in hay))


def freshness(
    item: SourceItem,
    *,
    lookback_days: int,
    now: datetime | None = None,
) -> float:
    """Linear decay from 1.0 (published now) to 0.0 (published lookback_days ago).

    Items outside the lookback window clamp to 0.0 rather than going
    negative — they shouldn't exist here (window filtering happens in
    sources) but we stay defensive.
    """
    if lookback_days <= 0:
        return 0.0
    current = now or datetime.now(timezone.utc)
    age_days = (current - item.published_at).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    if age_days >= lookback_days:
        return 0.0
    return 1.0 - (age_days / lookback_days)


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


def compute_score(
    item: SourceItem,
    *,
    topic: str,
    focus_keywords: list[str],
    source_weight: float,
    weights: RankingWeights,
    lookback_days: int,
    now: datetime | None = None,
) -> float:
    """Weighted sum of the components. Pure function."""
    return (
        weights.topic_match * topic_match_ratio(item, topic)
        + weights.source_weight * source_weight
        + weights.focus_keyword * focus_hit_count(item, focus_keywords)
        + weights.freshness * freshness(item, lookback_days=lookback_days, now=now)
        + weights.llm_relevance * 0.0  # reserved; explicit to keep formula visible
    )
