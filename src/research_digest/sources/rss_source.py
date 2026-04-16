"""RSS / Atom source adapter.

First concrete implementation of `Source`. Uses httpx (async) to fetch the
feed bytes and feedparser to parse them into entries. Every entry becomes
one `SourceItem`.

Why RSS first: most target journals (Nature, Cell, Science, NEJM, JCO, ...)
publish stable RSS/Atom feeds. No API key, no quota, no auth dance — lowest
friction for the first end-to-end fetch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import struct_time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from ..models.source_item import SourceItem
from .base import FetchWindow, Source

logger = logging.getLogger(__name__)

# Tracking parameters we strip during URL canonicalization. Not exhaustive —
# just the common ones. Extend as we discover more in the wild.
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_EXACT = {"gclid", "fbclid", "mc_cid", "mc_eid", "ref", "ref_src"}

_DEFAULT_USER_AGENT = "research-digest-bot/0.1 (+rss-fetcher)"


def _canonicalize_url(url: str) -> str:
    """Strip tracking params and normalize for dedupe.

    Layer-1 dedupe key (see design §25.6). Deliberately simple: we don't
    resolve redirects or fold case — that would require extra HTTP calls
    and risk false merges.
    """
    parts = urlsplit(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.startswith(_TRACKING_PARAM_PREFIXES) and k not in _TRACKING_PARAM_EXACT
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def _struct_time_to_utc(t: struct_time | None) -> datetime | None:
    """feedparser gives struct_time in UTC (already converted). Wrap with tz."""
    if t is None:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc)


class RSSSource(Source):
    """Pull entries from a single RSS or Atom feed URL."""

    def __init__(
        self,
        *,
        source_id: str,
        name: str,
        feed_url: str,
        weight: float = 1.0,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.source_id = source_id
        self.name = name
        self.feed_url = feed_url
        self.weight = weight
        self.timeout_seconds = timeout_seconds

    async def fetch(self, window: FetchWindow) -> list[SourceItem]:
        logger.info("rss fetch start source=%s url=%s", self.source_id, self.feed_url)

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            follow_redirects=True,
        ) as client:
            resp = await client.get(self.feed_url)
            resp.raise_for_status()
            raw_bytes = resp.content

        parsed = feedparser.parse(raw_bytes)
        if parsed.bozo:
            # feedparser sets bozo=1 for malformed feeds but often still
            # yields usable entries. Log once, keep going.
            logger.warning(
                "rss feed flagged bozo source=%s reason=%s",
                self.source_id,
                getattr(parsed, "bozo_exception", "unknown"),
            )

        fetched_at = datetime.now(timezone.utc)
        items: list[SourceItem] = []
        skipped_no_date = 0
        skipped_out_of_window = 0

        for entry in parsed.entries:
            published = _struct_time_to_utc(
                entry.get("published_parsed") or entry.get("updated_parsed")
            )
            if published is None:
                skipped_no_date += 1
                continue
            if not (window.start <= published < window.end):
                skipped_out_of_window += 1
                continue

            url = entry.get("link")
            title = entry.get("title")
            if not url or not title:
                # A feed entry with neither a link nor a title is unusable.
                continue

            summary = entry.get("summary") or None
            authors = [a.get("name", "") for a in entry.get("authors", []) if a.get("name")]

            items.append(
                SourceItem(
                    source_id=self.source_id,
                    source_type="rss",
                    title=title,
                    summary=summary,
                    content=None,  # RSS rarely carries full text; leave to enrich later
                    authors=authors,
                    url=url,
                    url_canonical=_canonicalize_url(url),
                    published_at=published,
                    fetched_at=fetched_at,
                )
            )

        logger.info(
            "rss fetch done source=%s kept=%d skipped_no_date=%d skipped_out_of_window=%d",
            self.source_id,
            len(items),
            skipped_no_date,
            skipped_out_of_window,
        )
        return items
