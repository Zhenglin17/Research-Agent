"""Tests for sources/rss_source.py — URL canonicalization + fetch parsing.

HTTP is mocked via respx so these tests never touch the network.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import pytest
import respx

from research_digest.sources.base import FetchWindow
from research_digest.sources.rss_source import (
    RSSSource,
    _canonicalize_url,
    _struct_time_to_utc,
)

# --- _canonicalize_url -------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # plain URL unchanged
        ("https://example.com/a", "https://example.com/a"),
        # utm_* stripped
        (
            "https://example.com/a?utm_source=x&utm_medium=y&id=5",
            "https://example.com/a?id=5",
        ),
        # gclid / fbclid stripped
        ("https://example.com/a?gclid=abc&q=1", "https://example.com/a?q=1"),
        # fragment cleared
        ("https://example.com/a#section", "https://example.com/a"),
        # only tracking params → no query remains
        ("https://example.com/a?utm_source=x", "https://example.com/a"),
        # order of remaining params preserved
        (
            "https://example.com/a?b=2&utm_source=x&c=3",
            "https://example.com/a?b=2&c=3",
        ),
    ],
)
def test_canonicalize_url(raw: str, expected: str) -> None:
    assert _canonicalize_url(raw) == expected


# --- _struct_time_to_utc -----------------------------------------------------


def test_struct_time_to_utc_none_returns_none() -> None:
    assert _struct_time_to_utc(None) is None


def test_struct_time_to_utc_returns_aware_datetime() -> None:
    t = time.struct_time((2026, 4, 13, 12, 30, 0, 0, 0, 0))
    dt = _struct_time_to_utc(t)
    assert dt == datetime(2026, 4, 13, 12, 30, tzinfo=timezone.utc)


# --- RSSSource.fetch ---------------------------------------------------------


_FEED_URL = "https://feed.example.com/test.rss"

_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <link>https://example.com</link>
  <description>A test feed</description>
  <item>
    <title>In-window entry</title>
    <link>https://example.com/a?utm_source=x&amp;id=1</link>
    <pubDate>Mon, 13 Apr 2026 12:00:00 GMT</pubDate>
    <description>Recent article</description>
  </item>
  <item>
    <title>Out-of-window old entry</title>
    <link>https://example.com/b</link>
    <pubDate>Wed, 01 Jan 2020 12:00:00 GMT</pubDate>
    <description>Old article</description>
  </item>
  <item>
    <title>No-date entry</title>
    <link>https://example.com/c</link>
    <description>Missing pubDate</description>
  </item>
</channel>
</rss>
"""


def _window() -> FetchWindow:
    # 2026-04-10 -> 2026-04-14 covers the recent entry, excludes the 2020 one.
    return FetchWindow(
        start=datetime(2026, 4, 10, tzinfo=timezone.utc),
        end=datetime(2026, 4, 14, tzinfo=timezone.utc),
    )


@respx.mock
async def test_fetch_keeps_in_window_entry() -> None:
    respx.get(_FEED_URL).mock(return_value=httpx.Response(200, content=_RSS_XML))

    src = RSSSource(source_id="t", name="Test", feed_url=_FEED_URL)
    items = await src.fetch(_window())

    assert len(items) == 1
    item = items[0]
    assert item.title == "In-window entry"
    assert item.source_id == "t"
    assert item.source_type == "rss"
    # URL canonicalization happened
    assert item.url_canonical == "https://example.com/a?id=1"
    # Published time is timezone-aware UTC
    assert item.published_at.tzinfo is not None


@respx.mock
async def test_fetch_filters_out_of_window() -> None:
    respx.get(_FEED_URL).mock(return_value=httpx.Response(200, content=_RSS_XML))
    src = RSSSource(source_id="t", name="Test", feed_url=_FEED_URL)
    items = await src.fetch(_window())
    titles = [i.title for i in items]
    assert "Out-of-window old entry" not in titles


@respx.mock
async def test_fetch_skips_entries_without_date() -> None:
    respx.get(_FEED_URL).mock(return_value=httpx.Response(200, content=_RSS_XML))
    src = RSSSource(source_id="t", name="Test", feed_url=_FEED_URL)
    items = await src.fetch(_window())
    titles = [i.title for i in items]
    assert "No-date entry" not in titles


@respx.mock
async def test_fetch_raises_on_http_error() -> None:
    respx.get(_FEED_URL).mock(return_value=httpx.Response(500))
    src = RSSSource(source_id="t", name="Test", feed_url=_FEED_URL)
    with pytest.raises(httpx.HTTPStatusError):
        await src.fetch(_window())


@respx.mock
async def test_fetch_returns_empty_for_empty_feed() -> None:
    empty = """<?xml version="1.0"?><rss version="2.0"><channel>
    <title>Empty</title></channel></rss>"""
    respx.get(_FEED_URL).mock(return_value=httpx.Response(200, content=empty))
    src = RSSSource(source_id="t", name="Test", feed_url=_FEED_URL)
    assert await src.fetch(_window()) == []
