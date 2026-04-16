"""Tests for config/sources_schema.py — discriminated-union validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from research_digest.config.sources_schema import (
    BiorxivSourceEntry,
    PubmedSourceEntry,
    RssSourceEntry,
    SourcesConfig,
)


def test_rss_entry_valid() -> None:
    entry = RssSourceEntry(
        id="x", name="X", type="rss", feed_url="https://example.com/x.rss"
    )
    assert entry.enabled is True  # default
    assert entry.weight == 1.0    # default


def test_rss_entry_missing_feed_url_fails() -> None:
    with pytest.raises(ValidationError):
        RssSourceEntry(id="x", name="X", type="rss")  # type: ignore[call-arg]


def test_pubmed_entry_valid() -> None:
    entry = PubmedSourceEntry(
        id="p", name="P", type="pubmed", query="cancer immunotherapy"
    )
    assert entry.max_results == 50
    assert entry.api_key_env is None


def test_pubmed_entry_missing_query_fails() -> None:
    with pytest.raises(ValidationError):
        PubmedSourceEntry(id="p", name="P", type="pubmed")  # type: ignore[call-arg]


def test_biorxiv_entry_valid_with_defaults() -> None:
    entry = BiorxivSourceEntry(id="b", name="B", type="biorxiv")
    assert entry.server == "biorxiv"
    assert entry.categories == []
    assert entry.keywords == []
    assert entry.max_results == 50


def test_biorxiv_entry_rejects_unknown_server() -> None:
    with pytest.raises(ValidationError):
        BiorxivSourceEntry(id="b", name="B", type="biorxiv", server="arxiv")  # type: ignore[arg-type]


def test_sources_config_dispatches_by_type() -> None:
    cfg = SourcesConfig(
        sources=[
            {"id": "a", "name": "A", "type": "rss", "feed_url": "https://x/a.rss"},
            {"id": "b", "name": "B", "type": "pubmed", "query": "q"},
            {"id": "c", "name": "C", "type": "biorxiv", "keywords": ["x"]},
        ]
    )
    assert isinstance(cfg.sources[0], RssSourceEntry)
    assert isinstance(cfg.sources[1], PubmedSourceEntry)
    assert isinstance(cfg.sources[2], BiorxivSourceEntry)


def test_sources_config_unknown_type_fails() -> None:
    with pytest.raises(ValidationError):
        SourcesConfig(sources=[{"id": "x", "name": "X", "type": "mystery"}])


def test_sources_config_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        SourcesConfig(
            sources=[
                {
                    "id": "x",
                    "name": "X",
                    "type": "rss",
                    "feed_url": "https://x/a.rss",
                    "surprise": 1,
                }
            ]
        )


def test_rss_entry_negative_weight_fails() -> None:
    with pytest.raises(ValidationError):
        RssSourceEntry(
            id="x", name="X", type="rss", feed_url="https://x/a.rss", weight=-1.0
        )
