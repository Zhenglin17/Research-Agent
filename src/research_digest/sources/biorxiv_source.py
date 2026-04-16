"""bioRxiv / medRxiv preprint source adapter.

Why add preprints: bioRxiv and medRxiv are **open-access** — every paper
has a freely downloadable PDF and full text. That makes them the only
source in V1 where V2's follow-up-question feature can reliably fetch
the full article. All items returned here carry
`full_text_accessible=True` and get a 📖 marker in the Telegram digest.

API shape — different from PubMed:

  * One endpoint:
      GET https://api.biorxiv.org/details/<server>/<from>/<to>/<cursor>
    where <server> is "biorxiv" or "medrxiv", dates are YYYY-MM-DD,
    and <cursor> is the pagination offset.

  * NO server-side search. The endpoint just lists every paper posted
    in the date range. So for a focused topic digest, we pull the full
    date range and filter client-side by `categories` (paper's own
    subject tag) and `keywords` (title+abstract substring match).

  * Pagination: up to 100 results per call; repeat with cursor += 100
    until `count == 0` or we hit `max_results` kept.

Full-text access: hardcoded True. Not a per-item check — bioRxiv's
whole premise is open preprints. V2's fetcher will dispatch on
`source_type == "biorxiv"` and pull the PDF/XML from biorxiv.org.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..models.source_item import SourceItem
from .base import FetchWindow, Source

logger = logging.getLogger(__name__)

_API_BASE = "https://api.biorxiv.org/details"
_PAGE_SIZE = 100  # bioRxiv's per-call cap; not user-tunable
_DEFAULT_USER_AGENT = "research-digest-bot/0.1 (+biorxiv-fetcher)"


class BioRxivSource(Source):
    """Pull recent preprints from bioRxiv or medRxiv, filtered locally."""

    def __init__(
        self,
        *,
        source_id: str,
        name: str,
        server: str = "biorxiv",
        categories: list[str] | None = None,
        keywords: list[str] | None = None,
        max_results: int = 50,
        weight: float = 1.0,
        timeout_seconds: float = 60.0,
        lookback_override_days: int | None = None,
        max_pages: int = 5,
    ) -> None:
        self.source_id = source_id
        self.name = name
        self.server = server
        # Normalize to lowercase once so per-item comparison stays hot-path simple.
        self.categories_lower: set[str] = {c.lower() for c in (categories or [])}
        self.keywords_lower: list[str] = [k.lower() for k in (keywords or [])]
        self.max_results = max_results
        self.weight = weight
        self.timeout_seconds = timeout_seconds
        self.lookback_override_days = lookback_override_days
        self.max_pages = max_pages

    async def fetch(self, window: FetchWindow) -> list[SourceItem]:
        # Narrow the API date range if the caller (yaml) asked for a tighter
        # lookback than the pipeline-wide window. bioRxiv charges ~25s + ~180
        # papers per extra day because there's no server-side search, so
        # shrinking here is the cheapest optimization available. Cross-run
        # dedupe (SQLite) handles overlap between daily runs.
        effective_window = self._effective_window(window)
        lookback_note = (
            f"override={self.lookback_override_days}d"
            if self.lookback_override_days is not None
            else "global"
        )
        logger.info(
            "biorxiv fetch start source=%s server=%s categories=%d keywords=%d max=%d lookback=%s max_pages=%d",
            self.source_id, self.server,
            len(self.categories_lower), len(self.keywords_lower),
            self.max_results, lookback_note, self.max_pages,
        )

        from_date = effective_window.start.strftime("%Y-%m-%d")
        # Window end is exclusive; the API treats `<to>` as inclusive, so
        # using window.end's date may pull a few boundary-day items, which
        # the per-item window check below filters back out.
        to_date = effective_window.end.strftime("%Y-%m-%d")

        fetched_at = datetime.now(timezone.utc)
        items: list[SourceItem] = []
        cursor = 0
        scanned_total = 0
        pages_fetched = 0
        stop_reason = "exhausted"  # set by whichever exit branch fires first

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            follow_redirects=True,
        ) as client:
            while len(items) < self.max_results:
                if pages_fetched >= self.max_pages:
                    stop_reason = "max_pages"
                    break
                batch = await self._fetch_page(client, from_date, to_date, cursor)
                pages_fetched += 1
                if not batch:
                    break
                scanned_total += len(batch)
                for raw in batch:
                    kept = _normalize_and_filter(
                        raw,
                        window=effective_window,
                        fetched_at=fetched_at,
                        source_id=self.source_id,
                        categories_lower=self.categories_lower,
                        keywords_lower=self.keywords_lower,
                    )
                    if kept is not None:
                        items.append(kept)
                        if len(items) >= self.max_results:
                            stop_reason = "max_results"
                            break
                # A short page means we've exhausted the range.
                if len(batch) < _PAGE_SIZE:
                    break
                cursor += _PAGE_SIZE

        logger.info(
            "biorxiv fetch done source=%s scanned=%d kept=%d pages=%d stop=%s",
            self.source_id, scanned_total, len(items), pages_fetched, stop_reason,
        )
        return items

    def _effective_window(self, window: FetchWindow) -> FetchWindow:
        """Apply the per-source lookback override, if any.

        Returns the window we should actually query the API with and check
        items against. `lookback_override_days=None` → unchanged global
        window; otherwise shrink `start` to end − override days (but never
        widen beyond the pipeline window).
        """
        if self.lookback_override_days is None:
            return window
        narrowed_start = window.end - timedelta(days=self.lookback_override_days)
        # Never go earlier than the pipeline's own start; the override is
        # intended as a tightening, not an expansion.
        effective_start = max(window.start, narrowed_start)
        return FetchWindow(start=effective_start, end=window.end)

    async def _fetch_page(
        self, client: httpx.AsyncClient, from_date: str, to_date: str, cursor: int,
    ) -> list[dict[str, Any]]:
        """Fetch one 100-item page. Returns the raw `collection` list."""
        url = f"{_API_BASE}/{self.server}/{from_date}/{to_date}/{cursor}"
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return list(data.get("collection", []))


# ---------------------------------------------------------------------------
# Pure normalization/filter — easy to unit-test with a canned dict.
# ---------------------------------------------------------------------------


def _normalize_and_filter(
    raw: dict[str, Any],
    *,
    window: FetchWindow,
    fetched_at: datetime,
    source_id: str,
    categories_lower: set[str],
    keywords_lower: list[str],
) -> SourceItem | None:
    """Convert one raw bioRxiv record → SourceItem, or None if filtered out.

    Filters applied, in order:
      1. required fields present (doi, title, date)
      2. parseable date inside the fetch window
      3. category whitelist (skipped if the whitelist is empty)
      4. keyword match in title+abstract (skipped if list is empty)

    Each drop path is silent — the caller logs aggregate counts.
    """
    doi = raw.get("doi")
    title = (raw.get("title") or "").strip()
    if not doi or not title:
        return None

    published = _parse_date(raw.get("date"))
    if published is None:
        return None
    if not (window.start <= published < window.end):
        return None

    category_raw = (raw.get("category") or "").strip()
    if categories_lower and category_raw.lower() not in categories_lower:
        return None

    abstract = (raw.get("abstract") or "").strip()
    if keywords_lower:
        haystack = f"{title}\n{abstract}".lower()
        if not any(k in haystack for k in keywords_lower):
            return None

    authors = _parse_authors(raw.get("authors"))
    server = raw.get("server") or "biorxiv"
    # bioRxiv's stable landing page for a DOI. Works for medRxiv too if
    # we swap the host — do that based on `server`.
    host = "www.medrxiv.org" if server == "medrxiv" else "www.biorxiv.org"
    url = f"https://{host}/content/{doi}"

    extra: dict[str, Any] = {
        "doi": doi,
        "server": server,
        "category": category_raw or None,
    }
    version = raw.get("version")
    if version is not None:
        extra["version"] = str(version)
    license_ = raw.get("license")
    if license_:
        extra["license"] = license_

    return SourceItem(
        source_id=source_id,
        source_type="biorxiv",
        title=title,
        summary=abstract or None,
        content=None,  # V1 keeps abstract-only; V2 fetcher pulls full text
        authors=authors,
        url=url,
        url_canonical=url,
        published_at=published,
        fetched_at=fetched_at,
        # Hardcoded True: every bioRxiv/medRxiv preprint is openly accessible.
        full_text_accessible=True,
        extra={k: v for k, v in extra.items() if v is not None},
    )


def _parse_date(raw: Any) -> datetime | None:
    """Parse bioRxiv's 'YYYY-MM-DD' date string to UTC midnight.

    Returns None on missing/malformed input rather than raising — one bad
    record shouldn't nuke the whole page.
    """
    if not isinstance(raw, str):
        return None
    try:
        y, m, d = raw.split("-")
        return datetime(int(y), int(m), int(d), tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _parse_authors(raw: Any) -> list[str]:
    """bioRxiv ships authors as a single semicolon-separated string.

    Example: `"Smith, A.; Jones, C.B.; Zhou, D."` — we just split and trim
    and let each string through as-is rather than reorder into "First Last"
    (the lastname-first form is already informative for a byline).
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    return [a.strip() for a in raw.split(";") if a.strip()]
