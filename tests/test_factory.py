"""Tests for sources/factory.py."""

from __future__ import annotations

from research_digest.config.sources_schema import SourcesConfig
from research_digest.sources.factory import build_sources
from research_digest.sources.pubmed_source import PubMedSource
from research_digest.sources.rss_source import RSSSource


def _cfg(*entries: dict) -> SourcesConfig:
    return SourcesConfig(sources=list(entries))


def test_build_sources_builds_enabled_rss() -> None:
    cfg = _cfg(
        {"id": "x", "name": "X", "type": "rss", "feed_url": "https://x/x.rss"}
    )
    built = build_sources(cfg)
    assert len(built) == 1
    assert isinstance(built[0], RSSSource)
    assert built[0].source_id == "x"


def test_build_sources_skips_disabled() -> None:
    cfg = _cfg(
        {
            "id": "x",
            "name": "X",
            "type": "rss",
            "feed_url": "https://x/x.rss",
            "enabled": False,
        }
    )
    assert build_sources(cfg) == []


def test_build_sources_builds_enabled_pubmed() -> None:
    cfg = _cfg(
        {"id": "p", "name": "P", "type": "pubmed", "query": "cancer", "weight": 1.2}
    )
    built = build_sources(cfg)
    assert len(built) == 1
    assert isinstance(built[0], PubMedSource)
    assert built[0].source_id == "p"
    assert built[0].query == "cancer"
    assert built[0].weight == 1.2


def test_build_sources_filters_mixed_config() -> None:
    cfg = _cfg(
        {"id": "a", "name": "A", "type": "rss", "feed_url": "https://x/a.rss"},
        {
            "id": "b",
            "name": "B",
            "type": "rss",
            "feed_url": "https://x/b.rss",
            "enabled": False,
        },
        {"id": "c", "name": "C", "type": "pubmed", "query": "q"},
    )
    built = build_sources(cfg)
    # Both `a` (rss) and `c` (pubmed) are enabled and supported; `b` is disabled.
    assert [s.source_id for s in built] == ["a", "c"]


def test_build_sources_passes_weight_through() -> None:
    cfg = _cfg(
        {
            "id": "x",
            "name": "X",
            "type": "rss",
            "feed_url": "https://x/x.rss",
            "weight": 1.5,
        }
    )
    built = build_sources(cfg)
    assert built[0].weight == 1.5
