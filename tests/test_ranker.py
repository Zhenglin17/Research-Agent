"""Tests for ranking/ranker.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from research_digest.config.schema import AppConfig, LimitsConfig, RankingWeights
from research_digest.config.sources_schema import RssSourceEntry, SourcesConfig
from research_digest.models.source_item import SourceItem
from research_digest.ranking.ranker import rank

_NOW = datetime(2026, 4, 14, tzinfo=timezone.utc)


def _item(
    source_id: str = "s1",
    title: str = "T",
    published_at: datetime | None = None,
) -> SourceItem:
    return SourceItem(
        source_id=source_id,
        source_type="rss",
        title=title,
        url="https://example.com/x",
        url_canonical=f"https://example.com/{title}",  # unique per title
        published_at=published_at or _NOW,
        fetched_at=_NOW,
    )


def _app_cfg(
    topic: str = "cancer immunotherapy",
    focus: list[str] | None = None,
    cap: int = 10,
) -> AppConfig:
    return AppConfig(
        topic=topic,
        focus_keywords=focus or [],
        limits=LimitsConfig(lookback_days=3, max_digest_items=cap),
        ranking=RankingWeights(),
    )


def _sources_cfg(*pairs: tuple[str, float]) -> SourcesConfig:
    return SourcesConfig(
        sources=[
            RssSourceEntry(
                id=sid, name=sid, type="rss",
                feed_url=f"https://x/{sid}.rss", weight=w,
            )
            for sid, w in pairs
        ]
    )


def test_rank_empty_input() -> None:
    all_ranked, selected = rank([], app_config=_app_cfg(), sources_config=_sources_cfg(("s1", 1.0)))
    assert all_ranked == []
    assert selected == []


def test_rank_writes_score_onto_items() -> None:
    items = [_item(title="One"), _item(title="Two")]
    rank(items, app_config=_app_cfg(), sources_config=_sources_cfg(("s1", 1.0)), now=_NOW)
    assert all(i.score is not None for i in items)


def test_rank_sorts_descending_by_score() -> None:
    items = [
        _item(title="Irrelevant topic"),
        _item(title="cancer immunotherapy matters"),
        _item(title="cancer research"),
    ]
    _, selected = rank(
        items,
        app_config=_app_cfg(),
        sources_config=_sources_cfg(("s1", 1.0)),
        now=_NOW,
    )
    assert selected[0].title == "cancer immunotherapy matters"
    assert selected[-1].title == "Irrelevant topic"


def test_rank_trims_to_cap() -> None:
    items = [_item(title=f"t{i}") for i in range(20)]
    all_ranked, selected = rank(
        items,
        app_config=_app_cfg(cap=5),
        sources_config=_sources_cfg(("s1", 1.0)),
        now=_NOW,
    )
    assert len(selected) == 5
    assert len(all_ranked) == 20


def test_rank_uses_source_weight() -> None:
    # Two items otherwise identical; only source_id (hence weight) differs.
    items = [
        _item(source_id="low", title="cancer"),
        _item(source_id="high", title="cancer"),
    ]
    _, selected = rank(
        items,
        app_config=_app_cfg(),
        sources_config=_sources_cfg(("low", 0.1), ("high", 5.0)),
        now=_NOW,
    )
    assert selected[0].source_id == "high"


def test_rank_freshness_breaks_content_tie() -> None:
    old = _item(title="cancer immunotherapy", published_at=_NOW - timedelta(days=2, hours=12))
    new = _item(title="cancer immunotherapy", published_at=_NOW - timedelta(hours=1))
    old.url_canonical = "https://example.com/old"
    new.url_canonical = "https://example.com/new"
    _, selected = rank(
        [old, new],
        app_config=_app_cfg(),
        sources_config=_sources_cfg(("s1", 1.0)),
        now=_NOW,
    )
    assert selected[0].published_at == new.published_at


def test_rank_focus_keywords_boost() -> None:
    with_focus = _item(title="CAR-T therapy cancer")
    without = _item(title="cancer research")
    with_focus.url_canonical = "https://a/1"
    without.url_canonical = "https://a/2"
    _, selected = rank(
        [without, with_focus],
        app_config=_app_cfg(focus=["CAR-T"]),
        sources_config=_sources_cfg(("s1", 1.0)),
        now=_NOW,
    )
    assert selected[0].title == "CAR-T therapy cancer"


def test_rank_unknown_source_id_falls_back_to_default_weight() -> None:
    # Item has source_id not in sources_config → should still rank, not crash.
    items = [_item(source_id="mystery", title="cancer")]
    _, selected = rank(
        items,
        app_config=_app_cfg(),
        sources_config=_sources_cfg(("other", 1.0)),
        now=_NOW,
    )
    assert len(selected) == 1
    assert selected[0].score is not None


def test_rank_tie_broken_by_published_at() -> None:
    # Same score-producing inputs → newer item should come first.
    older = _item(title="same", published_at=_NOW - timedelta(hours=2))
    newer = _item(title="same", published_at=_NOW - timedelta(hours=1))
    older.url_canonical = "https://a/1"
    newer.url_canonical = "https://a/2"
    _, selected = rank(
        [older, newer],
        app_config=_app_cfg(),
        sources_config=_sources_cfg(("s1", 1.0)),
        now=_NOW,
    )
    assert selected[0].published_at == newer.published_at
