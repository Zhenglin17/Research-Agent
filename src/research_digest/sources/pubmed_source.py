"""PubMed E-utilities source adapter.

Why a custom adapter instead of RSS: PubMed publishes an official API
(E-utilities) with structured metadata — PMID, DOI, PMCID, proper
abstracts, authors — that no RSS feed exposes. Using the API gives us:

  * Reliable date filtering (mindate/maxdate) instead of parsing messy
    pubDate strings.
  * Explicit PMCID signal for the V2 full-text-accessibility flag.
  * A 500-result cap per query vs RSS's typical last-20-items limit.

Flow (two HTTP calls per fetch, regardless of result count):

  1. `esearch.fcgi` with the configured query + date window → JSON with
     a list of PMIDs. Date filter uses `datetype=edat` (Entrez-date:
     when the record appeared in PubMed), which is the most reliable
     structured date PubMed exposes.

  2. `efetch.fcgi` with the PMID list (comma-joined) → XML with title,
     abstract, authors, article IDs (DOI, PMCID) for each PMID.

Rate limits: 3 req/sec without an API key, 10 req/sec with one. Two
calls per run stays under both easily. API key is optional and read
from the env var named by `api_key_env` in sources.yaml — never from
the yaml itself, to keep secrets out of git.

URL policy: PubMed doesn't give us publisher URLs directly. We
construct the PubMed landing page URL (`https://pubmed.ncbi.nlm.nih.gov/
<PMID>/`) as the canonical link since it's stable and always works.
The publisher URL (via DOI) lands in `extra["doi"]` for V2.

Full-text accessibility: an article is flagged `full_text_accessible`
iff efetch returns a PMCID for it. That means the paper is in PubMed
Central (NIH's open-access repo) and V2's fetcher can pull XML full
text without auth. Most recent papers from paywalled journals won't
have this; OA journals (PLOS, eLife, Nature Communications OA tier,
BMC) and NIH-funded work usually will.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from ..models.source_item import SourceItem
from .base import FetchWindow, Source

logger = logging.getLogger(__name__)

_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_DEFAULT_USER_AGENT = "research-digest-bot/0.1 (+pubmed-fetcher)"


class PubMedSource(Source):
    """Pull recent PubMed records matching a fixed search query."""

    def __init__(
        self,
        *,
        source_id: str,
        name: str,
        query: str,
        max_results: int = 50,
        api_key_env: str | None = None,
        include_abstract: bool = True,
        weight: float = 1.0,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.source_id = source_id
        self.name = name
        self.query = query
        self.max_results = max_results
        self.api_key_env = api_key_env
        self.include_abstract = include_abstract
        self.weight = weight
        self.timeout_seconds = timeout_seconds

    # --- public API ---------------------------------------------------------

    async def fetch(self, window: FetchWindow) -> list[SourceItem]:
        """Run esearch → efetch → parse → return SourceItems.

        The contract from `Source.fetch` applies: items must fall inside
        `window`, must carry `source_id`/`source_type`, and an empty
        result returns `[]` rather than raising.
        """
        logger.info(
            "pubmed fetch start source=%s query=%r max=%d",
            self.source_id, self.query, self.max_results,
        )
        api_key = self._resolve_api_key()

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            follow_redirects=True,
        ) as client:
            pmids = await self._esearch(client, window, api_key)
            if not pmids:
                logger.info("pubmed fetch done source=%s kept=0 (no PMIDs)", self.source_id)
                return []
            xml_bytes = await self._efetch(client, pmids, api_key)

        fetched_at = datetime.now(timezone.utc)
        items = _parse_efetch_xml(
            xml_bytes,
            window=window,
            fetched_at=fetched_at,
            source_id=self.source_id,
            include_abstract=self.include_abstract,
        )
        logger.info(
            "pubmed fetch done source=%s pmids=%d kept=%d accessible=%d",
            self.source_id,
            len(pmids),
            len(items),
            sum(1 for i in items if i.full_text_accessible),
        )
        return items

    # --- internals ----------------------------------------------------------

    def _resolve_api_key(self) -> str | None:
        """Read the API key from the env var named in sources.yaml.

        Returning None is the common case — we run unauthenticated, just
        slower. We deliberately read from the env every fetch (not at
        __init__) so key rotations take effect without restarting.
        """
        if not self.api_key_env:
            return None
        key = os.environ.get(self.api_key_env)
        if not key:
            logger.warning(
                "pubmed api_key_env=%s set but env var missing; running without key",
                self.api_key_env,
            )
            return None
        return key

    async def _esearch(
        self, client: httpx.AsyncClient, window: FetchWindow, api_key: str | None,
    ) -> list[str]:
        """First call: search → PMID list (JSON response, easy to parse)."""
        params: dict[str, Any] = {
            "db": "pubmed",
            "term": self.query,
            "retmode": "json",
            "retmax": self.max_results,
            "mindate": window.start.strftime("%Y/%m/%d"),
            "maxdate": window.end.strftime("%Y/%m/%d"),
            "datetype": "edat",  # Entrez-date: structured, matches 'when it showed up'
        }
        if api_key:
            params["api_key"] = api_key

        resp = await client.get(f"{_EUTILS_BASE}/esearch.fcgi", params=params)
        resp.raise_for_status()
        data = resp.json()
        return list(data.get("esearchresult", {}).get("idlist", []))

    async def _efetch(
        self, client: httpx.AsyncClient, pmids: list[str], api_key: str | None,
    ) -> bytes:
        """Second call: bulk fetch → XML bytes for `_parse_efetch_xml`."""
        params: dict[str, Any] = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
        }
        if api_key:
            params["api_key"] = api_key

        resp = await client.get(f"{_EUTILS_BASE}/efetch.fcgi", params=params)
        resp.raise_for_status()
        return resp.content


# ---------------------------------------------------------------------------
# XML parsing — pure functions, no network, so unit tests can exercise them
# directly with a recorded XML fixture.
# ---------------------------------------------------------------------------


def _parse_efetch_xml(
    xml_bytes: bytes,
    *,
    window: FetchWindow,
    fetched_at: datetime,
    source_id: str,
    include_abstract: bool,
) -> list[SourceItem]:
    """Walk every `<PubmedArticle>` in the efetch response → SourceItems.

    Drops articles missing any of: title, parseable date, abstract (when
    `include_abstract`). Silently drops malformed entries — one bad record
    shouldn't lose the whole response. Logs the drop count so we can spot
    systematic problems.
    """
    root = ET.fromstring(xml_bytes)
    items: list[SourceItem] = []
    dropped_no_title = 0
    dropped_no_date = 0
    dropped_no_abstract = 0
    dropped_out_of_window = 0

    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//MedlineCitation/PMID")
        if pmid is None:
            continue

        title = _extract_title(article)
        if not title:
            dropped_no_title += 1
            continue

        published = _extract_pub_date(article)
        if published is None:
            dropped_no_date += 1
            continue
        if not (window.start <= published < window.end):
            dropped_out_of_window += 1
            continue

        abstract = _extract_abstract(article)
        if include_abstract and not abstract:
            # Without an abstract there's almost nothing for the LLM to
            # summarize. Drop rather than push a title-only stub.
            dropped_no_abstract += 1
            continue

        # Scope ID lookup to the article's *own* ArticleIdList. Using `.//`
        # would also match <ArticleId> nodes inside <PubmedData>/<ReferenceList>
        # — i.e. IDs of papers this article cites — producing false-positive
        # PMCID/DOI matches (a paper with no PMCID of its own gets flagged
        # full_text_accessible=True because one of its references is in PMC).
        doi = article.findtext('./PubmedData/ArticleIdList/ArticleId[@IdType="doi"]')
        pmc_id = article.findtext('./PubmedData/ArticleIdList/ArticleId[@IdType="pmc"]')
        authors = _extract_authors(article)

        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        extra: dict[str, Any] = {"pmid": pmid}
        if doi:
            extra["doi"] = doi
        if pmc_id:
            extra["pmc_id"] = pmc_id
            extra["pmc_url"] = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/"

        items.append(
            SourceItem(
                source_id=source_id,
                source_type="pubmed",
                title=title,
                summary=abstract,
                content=None,  # V1 never stores full text; V2 fetcher will
                authors=authors,
                url=url,
                url_canonical=url,  # PubMed landing pages have no tracking params
                published_at=published,
                fetched_at=fetched_at,
                full_text_accessible=pmc_id is not None,
                extra=extra,
            )
        )

    if dropped_no_title or dropped_no_date or dropped_no_abstract or dropped_out_of_window:
        logger.info(
            "pubmed parse drops source=%s no_title=%d no_date=%d no_abstract=%d oow=%d",
            source_id,
            dropped_no_title, dropped_no_date, dropped_no_abstract, dropped_out_of_window,
        )
    return items


def _extract_title(article: ET.Element) -> str:
    """Join all text fragments under `<ArticleTitle>`.

    PubMed sometimes puts markup inside titles (`<i>gene</i>`, MathML
    for formulas). `itertext()` flattens to plain text.
    """
    node = article.find(".//Article/ArticleTitle")
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def _extract_abstract(article: ET.Element) -> str | None:
    """Concatenate every `<AbstractText>` child (structured abstracts have many).

    Structured abstracts split into Background/Methods/Results/Conclusions
    nodes, each tagged with `Label=`. We prefix each fragment with its
    label so the LLM sees the structure; unlabeled abstracts read as a
    single paragraph.
    """
    nodes = article.findall(".//Article/Abstract/AbstractText")
    if not nodes:
        return None

    parts: list[str] = []
    for n in nodes:
        text = "".join(n.itertext()).strip()
        if not text:
            continue
        label = n.attrib.get("Label")
        parts.append(f"{label}: {text}" if label else text)

    joined = "\n".join(parts).strip()
    return joined or None


def _extract_authors(article: ET.Element) -> list[str]:
    """Build "First Last" strings from `<Author>` nodes; skip collectives."""
    authors: list[str] = []
    for a in article.findall(".//AuthorList/Author"):
        last = a.findtext("LastName")
        fore = a.findtext("ForeName") or a.findtext("Initials")
        if last and fore:
            authors.append(f"{fore} {last}")
        elif last:
            authors.append(last)
        # Collective authors (`<CollectiveName>`) are skipped — they're
        # usually consortium names, not useful in a byline.
    return authors


def _extract_pub_date(article: ET.Element) -> datetime | None:
    """Best-effort publication date → UTC datetime.

    PubMed stores dates in several places. We try in this priority order:

      1. `<PubMedPubDate PubStatus="pubmed">` — when the record entered
         PubMed. Always structured (Year/Month/Day). Matches the
         `datetype=edat` filter we use in esearch, so these will always
         fall inside the requested window.
      2. `<ArticleDate DateType="Electronic">` — online publication.
         Also always structured.
      3. `<PubDate>` inside `<Journal>/<JournalIssue>` — journal-issue
         date. Often partial (only Year, or Year+Month); we return None
         in those cases rather than guess a day.

    All datetimes are UTC-anchored at 00:00 — PubMed doesn't publish
    wall-clock times, only calendar dates.
    """
    node = article.find('.//PubMedPubDate[@PubStatus="pubmed"]')
    if node is not None:
        dt = _ymd_from_node(node)
        if dt is not None:
            return dt

    node = article.find('.//ArticleDate[@DateType="Electronic"]')
    if node is not None:
        dt = _ymd_from_node(node)
        if dt is not None:
            return dt

    node = article.find(".//Journal/JournalIssue/PubDate")
    if node is not None:
        return _ymd_from_node(node)  # may still be None if only Year present

    return None


def _ymd_from_node(node: ET.Element) -> datetime | None:
    """Read `<Year>/<Month>/<Day>` children. Missing day/month → None.

    We intentionally do NOT default missing day=1 or month=1: that would
    shove Q1-dated records into the lookback window incorrectly.
    """
    year = node.findtext("Year")
    month = node.findtext("Month")
    day = node.findtext("Day")
    if not (year and month and day):
        return None
    try:
        return datetime(int(year), _month_to_int(month), int(day), tzinfo=timezone.utc)
    except (ValueError, KeyError):
        return None


_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_to_int(m: str) -> int:
    """Handle both numeric ('04') and abbreviated text ('Apr') month values.

    PubMed's PubDate uses text months ('Apr'); PubMedPubDate uses numeric
    ('4'). Raises KeyError for unrecognized strings — caller returns None.
    """
    m = m.strip()
    if m.isdigit():
        return int(m)
    return _MONTH_NAMES[m[:3].lower()]
