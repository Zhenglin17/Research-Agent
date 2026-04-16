"""Tests for sources/biorxiv_source.py — JSON parsing + client-side filters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx

from research_digest.sources.base import FetchWindow
from research_digest.sources.biorxiv_source import (
    BioRxivSource,
    _normalize_and_filter,
    _parse_authors,
    _parse_date,
)


def _window() -> FetchWindow:
    return FetchWindow(
        start=datetime(2026, 4, 10, tzinfo=timezone.utc),
        end=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )


def _raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "doi": "10.1101/2026.04.12.001",
        "title": "A CAR-T preprint",
        "authors": "Smith, A.; Jones, C.",
        "date": "2026-04-12",
        "version": "1",
        "category": "cancer biology",
        "abstract": "We engineered CAR-T cells...",
        "server": "biorxiv",
        "license": "cc_by",
    }
    base.update(overrides)
    return base


# --- _parse_date ------------------------------------------------------------


def test_parse_date_valid() -> None:
    assert _parse_date("2026-04-12") == datetime(2026, 4, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", [None, "", "2026/04/12", "not a date", 20260412])
def test_parse_date_invalid_returns_none(bad: Any) -> None:
    assert _parse_date(bad) is None


# --- _parse_authors ---------------------------------------------------------


def test_parse_authors_splits_on_semicolon() -> None:
    assert _parse_authors("Smith, A.; Jones, C.B.; Zhou, D.") == [
        "Smith, A.", "Jones, C.B.", "Zhou, D.",
    ]


@pytest.mark.parametrize("bad", [None, "", 42])
def test_parse_authors_invalid_returns_empty(bad: Any) -> None:
    assert _parse_authors(bad) == []


# --- _normalize_and_filter --------------------------------------------------


def _kwargs(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "window": _window(),
        "fetched_at": datetime(2026, 4, 15, tzinfo=timezone.utc),
        "source_id": "biorxiv_test",
        "categories_lower": set(),
        "keywords_lower": [],
    }
    base.update(over)
    return base


def test_normalize_keeps_happy_path_with_full_text_flag() -> None:
    item = _normalize_and_filter(_raw(), **_kwargs())
    assert item is not None
    assert item.full_text_accessible is True
    assert item.source_type == "biorxiv"
    assert item.extra["doi"] == "10.1101/2026.04.12.001"
    assert item.extra["category"] == "cancer biology"
    assert item.extra["license"] == "cc_by"
    assert str(item.url) == "https://www.biorxiv.org/content/10.1101/2026.04.12.001"


def test_normalize_uses_medrxiv_host_when_server_medrxiv() -> None:
    item = _normalize_and_filter(_raw(server="medrxiv"), **_kwargs())
    assert item is not None
    assert "www.medrxiv.org" in str(item.url)
    assert item.extra["server"] == "medrxiv"


def test_normalize_drops_out_of_window() -> None:
    assert _normalize_and_filter(_raw(date="2020-01-01"), **_kwargs()) is None


def test_normalize_drops_missing_doi() -> None:
    assert _normalize_and_filter(_raw(doi=None), **_kwargs()) is None


def test_normalize_drops_when_category_not_whitelisted() -> None:
    result = _normalize_and_filter(
        _raw(category="plant biology"),
        **_kwargs(categories_lower={"cancer biology", "immunology"}),
    )
    assert result is None


def test_normalize_keeps_when_category_whitelisted_case_insensitive() -> None:
    result = _normalize_and_filter(
        _raw(category="Cancer Biology"),
        **_kwargs(categories_lower={"cancer biology"}),
    )
    assert result is not None


def test_normalize_requires_keyword_match_when_list_nonempty() -> None:
    # Abstract mentions CAR-T; keyword "checkpoint" doesn't match.
    result = _normalize_and_filter(
        _raw(title="Unrelated work", abstract="nothing relevant"),
        **_kwargs(keywords_lower=["checkpoint"]),
    )
    assert result is None


def test_normalize_keyword_matches_against_title_too() -> None:
    result = _normalize_and_filter(
        _raw(title="Checkpoint blockade in melanoma", abstract="Methods ..."),
        **_kwargs(keywords_lower=["checkpoint"]),
    )
    assert result is not None


def test_normalize_empty_filters_keep_everything_in_window() -> None:
    result = _normalize_and_filter(
        _raw(category="obscure field", title="X", abstract="Y"),
        **_kwargs(categories_lower=set(), keywords_lower=[]),
    )
    assert result is not None


# --- BioRxivSource.fetch with mocked HTTP ----------------------------------


def _page_url(from_date: str, to_date: str, cursor: int) -> str:
    return f"https://api.biorxiv.org/details/biorxiv/{from_date}/{to_date}/{cursor}"


@respx.mock
async def test_fetch_single_page() -> None:
    page = {
        "messages": [{"status": "ok"}],
        "collection": [
            _raw(doi="10.1101/a", title="CAR-T paper"),
            _raw(doi="10.1101/b", title="Plant paper", category="plant biology"),
        ],
    }
    respx.get(_page_url("2026-04-10", "2026-04-15", 0)).mock(
        return_value=httpx.Response(200, json=page)
    )

    src = BioRxivSource(
        source_id="biorxiv_test",
        name="Test",
        server="biorxiv",
        categories=["cancer biology"],   # drops the plant paper
        keywords=["CAR-T"],
        max_results=50,
    )
    items = await src.fetch(_window())
    assert len(items) == 1
    assert items[0].extra["doi"] == "10.1101/a"


@respx.mock
async def test_fetch_paginates_until_short_page() -> None:
    # First page: 100 items (full page → adapter asks for page 2).
    page1 = {
        "collection": [
            _raw(doi=f"10.1101/p1-{i}", title="CAR-T work", abstract="x")
            for i in range(100)
        ]
    }
    # Second page: 5 items (< 100 → adapter stops after this page).
    page2 = {
        "collection": [
            _raw(doi=f"10.1101/p2-{i}", title="CAR-T work", abstract="x")
            for i in range(5)
        ]
    }
    respx.get(_page_url("2026-04-10", "2026-04-15", 0)).mock(
        return_value=httpx.Response(200, json=page1)
    )
    respx.get(_page_url("2026-04-10", "2026-04-15", 100)).mock(
        return_value=httpx.Response(200, json=page2)
    )

    src = BioRxivSource(
        source_id="biorxiv_test",
        name="Test",
        keywords=["CAR-T"],
        max_results=500,  # don't cap early
    )
    items = await src.fetch(_window())
    assert len(items) == 105


@respx.mock
async def test_fetch_stops_early_at_max_results() -> None:
    page = {
        "collection": [
            _raw(doi=f"10.1101/x-{i}", title="CAR-T work") for i in range(100)
        ]
    }
    respx.get(_page_url("2026-04-10", "2026-04-15", 0)).mock(
        return_value=httpx.Response(200, json=page)
    )

    src = BioRxivSource(
        source_id="biorxiv_test",
        name="Test",
        keywords=["CAR-T"],
        max_results=10,
    )
    items = await src.fetch(_window())
    assert len(items) == 10


@respx.mock
async def test_fetch_empty_collection_returns_empty() -> None:
    respx.get(_page_url("2026-04-10", "2026-04-15", 0)).mock(
        return_value=httpx.Response(200, json={"collection": []})
    )
    src = BioRxivSource(source_id="biorxiv_test", name="Test", max_results=50)
    assert await src.fetch(_window()) == []


@respx.mock
async def test_fetch_raises_on_http_error() -> None:
    respx.get(_page_url("2026-04-10", "2026-04-15", 0)).mock(
        return_value=httpx.Response(500)
    )
    src = BioRxivSource(source_id="biorxiv_test", name="Test", max_results=50)
    with pytest.raises(httpx.HTTPStatusError):
        await src.fetch(_window())


@respx.mock
async def test_fetch_honours_lookback_override() -> None:
    """With lookback_override_days=2 the adapter must query a narrower
    API date range than the pipeline window suggests — that's the whole
    point: bioRxiv has no search, so fewer days = fewer wasted downloads.
    Items outside the narrowed window are also dropped.
    """
    # Window is 2026-04-10 → 2026-04-15 (5 days). Override=2 means the
    # effective range is 2026-04-13 → 2026-04-15.
    from_date, to_date = "2026-04-13", "2026-04-15"
    page = {
        "collection": [
            _raw(doi="10.1101/in", title="CAR-T inside", date="2026-04-14"),
            _raw(doi="10.1101/out", title="CAR-T before", date="2026-04-11"),
        ],
    }
    respx.get(_page_url(from_date, to_date, 0)).mock(
        return_value=httpx.Response(200, json=page),
    )

    src = BioRxivSource(
        source_id="biorxiv_test",
        name="Test",
        keywords=["CAR-T"],
        max_results=50,
        lookback_override_days=2,
    )
    items = await src.fetch(_window())

    # The out-of-narrowed-window item must be filtered out even though the
    # pipeline window would have accepted it.
    assert len(items) == 1
    assert items[0].extra["doi"] == "10.1101/in"


@respx.mock
async def test_fetch_stops_at_max_pages_cap() -> None:
    """max_pages caps pagination even if every page is full and keywords
    keep matching. Prevents runaway scans on low-hit-rate days.
    """
    full_page = {
        "collection": [
            _raw(doi=f"10.1101/p-{i}", title="CAR-T work") for i in range(100)
        ]
    }
    # Register three pages; with max_pages=2 only the first two should fire.
    page0 = respx.get(_page_url("2026-04-10", "2026-04-15", 0)).mock(
        return_value=httpx.Response(200, json=full_page)
    )
    page1 = respx.get(_page_url("2026-04-10", "2026-04-15", 100)).mock(
        return_value=httpx.Response(200, json=full_page)
    )
    page2 = respx.get(_page_url("2026-04-10", "2026-04-15", 200)).mock(
        return_value=httpx.Response(200, json=full_page)
    )

    src = BioRxivSource(
        source_id="biorxiv_test",
        name="Test",
        keywords=["CAR-T"],
        max_results=10_000,  # don't let max_results short-circuit the test
        max_pages=2,
    )
    items = await src.fetch(_window())

    assert page0.called
    assert page1.called
    assert not page2.called  # hard cap honoured
    # Two full pages of 100 items each survived the filter.
    assert len(items) == 200
