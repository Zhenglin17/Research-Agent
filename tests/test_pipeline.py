"""Tests for pipeline/digest_pipeline.py — window math + per-source safety net."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from research_digest.config.schema import AppConfig, LimitsConfig
from research_digest.models.source_item import SourceItem
from research_digest.pipeline.digest_pipeline import (
    _fetch_one,
    compute_window,
    fetch_all,
)
from research_digest.sources.base import FetchWindow, Source


# --- compute_window ----------------------------------------------------------


def test_compute_window_width_matches_lookback() -> None:
    cfg = AppConfig(topic="t", limits=LimitsConfig(lookback_days=7))
    now = datetime(2026, 4, 14, tzinfo=timezone.utc)
    w = compute_window(cfg, now=now)
    assert w.end == now
    assert w.end - w.start == timedelta(days=7)


def test_compute_window_uses_real_now_by_default() -> None:
    cfg = AppConfig(topic="t")  # default lookback_days=3
    w = compute_window(cfg)
    assert (w.end - w.start) == timedelta(days=3)
    assert w.end.tzinfo is not None


# --- fake sources for async tests -------------------------------------------


class _StaticSource(Source):
    """Returns a fixed list of items; never raises."""

    def __init__(self, source_id: str, items: list[SourceItem]) -> None:
        self.source_id = source_id
        self.name = source_id
        self.weight = 1.0
        self._items = items

    async def fetch(self, window: FetchWindow) -> list[SourceItem]:
        return list(self._items)


class _ExplodingSource(Source):
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self.name = source_id
        self.weight = 1.0

    async def fetch(self, window: FetchWindow) -> list[SourceItem]:
        raise RuntimeError("kaboom")


def _item(sid: str, title: str) -> SourceItem:
    return SourceItem(
        source_id=sid,
        source_type="rss",
        title=title,
        url="https://example.com/x",
        url_canonical="https://example.com/x",
        published_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
    )


def _window() -> FetchWindow:
    return FetchWindow(
        start=datetime(2026, 4, 10, tzinfo=timezone.utc),
        end=datetime(2026, 4, 14, tzinfo=timezone.utc),
    )


# --- _fetch_one --------------------------------------------------------------


async def test_fetch_one_returns_items_on_success() -> None:
    src = _StaticSource("s", [_item("s", "t1"), _item("s", "t2")])
    out = await _fetch_one(src, _window())
    assert len(out) == 2


async def test_fetch_one_swallows_exception_returns_empty() -> None:
    src = _ExplodingSource("bad")
    out = await _fetch_one(src, _window())
    assert out == []


# --- fetch_all ---------------------------------------------------------------


async def test_fetch_all_aggregates_across_sources() -> None:
    a = _StaticSource("a", [_item("a", "1"), _item("a", "2")])
    b = _StaticSource("b", [_item("b", "3")])
    out = await fetch_all([a, b], _window())
    assert {i.title for i in out} == {"1", "2", "3"}


async def test_fetch_all_one_bad_source_does_not_kill_run() -> None:
    a = _StaticSource("good", [_item("good", "1")])
    b = _ExplodingSource("bad")
    out = await fetch_all([a, b], _window())
    assert [i.title for i in out] == ["1"]


async def test_fetch_all_empty_sources_returns_empty() -> None:
    assert await fetch_all([], _window()) == []
