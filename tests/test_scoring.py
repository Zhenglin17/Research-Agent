"""Tests for ranking/scoring.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research_digest.config.schema import RankingWeights
from research_digest.models.source_item import SourceItem
from research_digest.ranking.scoring import (
    compute_score,
    focus_hit_count,
    freshness,
    topic_match_ratio,
)

_NOW = datetime(2026, 4, 14, tzinfo=timezone.utc)


def _item(
    title: str = "T",
    summary: str | None = None,
    content: str | None = None,
    published_at: datetime | None = None,
) -> SourceItem:
    return SourceItem(
        source_id="s",
        source_type="rss",
        title=title,
        summary=summary,
        content=content,
        url="https://example.com/x",
        url_canonical="https://example.com/x",
        published_at=published_at or _NOW,
        fetched_at=_NOW,
    )


# --- topic_match_ratio -------------------------------------------------------


def test_topic_match_full() -> None:
    it = _item("CAR-T cancer immunotherapy breakthrough")
    assert topic_match_ratio(it, "cancer immunotherapy") == 1.0


def test_topic_match_partial() -> None:
    it = _item("Novel cancer treatment approach")
    assert topic_match_ratio(it, "cancer immunotherapy") == 0.5


def test_topic_match_none() -> None:
    it = _item("Protein folding prediction")
    assert topic_match_ratio(it, "cancer immunotherapy") == 0.0


def test_topic_match_empty_topic_is_zero() -> None:
    assert topic_match_ratio(_item("anything"), "") == 0.0


def test_topic_match_stopwords_dropped() -> None:
    # "of", "the" are stopwords → topic tokens = {study, cancer}
    it = _item("Study of cancer biomarkers")
    assert topic_match_ratio(it, "the study of cancer") == 1.0


def test_topic_match_reads_summary_and_content() -> None:
    it = _item("Boring title", summary="covers cancer", content="discusses immunotherapy")
    assert topic_match_ratio(it, "cancer immunotherapy") == 1.0


# --- focus_hit_count ---------------------------------------------------------


def test_focus_hit_count_none() -> None:
    assert focus_hit_count(_item("quantum computing"), ["CAR-T", "PD-1"]) == 0.0


def test_focus_hit_count_multiple() -> None:
    it = _item("CAR-T and PD-1 combination therapy")
    assert focus_hit_count(it, ["CAR-T", "PD-1", "TCR"]) == 2.0


def test_focus_hit_count_case_insensitive() -> None:
    assert focus_hit_count(_item("car-t results"), ["CAR-T"]) == 1.0


def test_focus_hit_count_empty_list() -> None:
    assert focus_hit_count(_item("anything"), []) == 0.0


# --- freshness ---------------------------------------------------------------


def test_freshness_now_is_one() -> None:
    it = _item(published_at=_NOW)
    assert freshness(it, lookback_days=3, now=_NOW) == 1.0


def test_freshness_at_boundary_is_zero() -> None:
    it = _item(published_at=_NOW - timedelta(days=3))
    assert freshness(it, lookback_days=3, now=_NOW) == 0.0


def test_freshness_halfway() -> None:
    it = _item(published_at=_NOW - timedelta(days=1, hours=12))
    # 1.5 / 3 = 0.5 → freshness = 0.5
    assert freshness(it, lookback_days=3, now=_NOW) == pytest.approx(0.5)


def test_freshness_outside_window_clamps_to_zero() -> None:
    it = _item(published_at=_NOW - timedelta(days=10))
    assert freshness(it, lookback_days=3, now=_NOW) == 0.0


def test_freshness_future_publish_clamps_to_one() -> None:
    it = _item(published_at=_NOW + timedelta(hours=1))
    assert freshness(it, lookback_days=3, now=_NOW) == 1.0


def test_freshness_zero_lookback_is_zero() -> None:
    assert freshness(_item(), lookback_days=0, now=_NOW) == 0.0


# --- compute_score -----------------------------------------------------------


def _weights(**overrides: float) -> RankingWeights:
    base = {
        "topic_match": 1.0,
        "source_weight": 0.5,
        "focus_keyword": 0.8,
        "freshness": 0.6,
        "llm_relevance": 0.0,
    }
    base.update(overrides)
    return RankingWeights(**base)


def test_compute_score_all_components() -> None:
    it = _item(
        "CAR-T cancer immunotherapy results",
        published_at=_NOW,
    )
    score = compute_score(
        it,
        topic="cancer immunotherapy",
        focus_keywords=["CAR-T"],
        source_weight=1.0,
        weights=_weights(),
        lookback_days=3,
        now=_NOW,
    )
    # topic=1.0, source=1.0, focus=1, fresh=1.0
    # 1.0*1.0 + 0.5*1.0 + 0.8*1 + 0.6*1.0 + 0 = 2.9
    assert score == pytest.approx(2.9)


def test_compute_score_zero_when_nothing_matches() -> None:
    it = _item("Quantum computing news", published_at=_NOW - timedelta(days=3))
    score = compute_score(
        it,
        topic="cancer immunotherapy",
        focus_keywords=["CAR-T"],
        source_weight=0.0,
        weights=_weights(),
        lookback_days=3,
        now=_NOW,
    )
    assert score == 0.0


def test_compute_score_llm_relevance_is_reserved_zero() -> None:
    # Even with w_llm_relevance > 0, V1 contributes 0.
    it = _item("cancer", published_at=_NOW)
    score_with_llm_weight = compute_score(
        it,
        topic="cancer",
        focus_keywords=[],
        source_weight=0.0,
        weights=_weights(topic_match=0.0, source_weight=0.0,
                         focus_keyword=0.0, freshness=0.0,
                         llm_relevance=5.0),
        lookback_days=3,
        now=_NOW,
    )
    assert score_with_llm_weight == 0.0
