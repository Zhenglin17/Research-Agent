"""Tests for sources/pubmed_source.py — E-utilities fetch + XML parsing.

HTTP is mocked via respx (same pattern as test_rss_source). The XML
parsing helpers are pure functions, so the bulk of the logic is
covered without hitting the network at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from research_digest.sources.base import FetchWindow
from research_digest.sources.pubmed_source import (
    PubMedSource,
    _extract_abstract,
    _extract_authors,
    _extract_pub_date,
    _extract_title,
    _parse_efetch_xml,
)
from xml.etree import ElementTree as ET


# --- Fixtures ---------------------------------------------------------------


def _window() -> FetchWindow:
    return FetchWindow(
        start=datetime(2026, 4, 10, tzinfo=timezone.utc),
        end=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )


# Minimal-but-realistic efetch XML. Four articles exercising:
#   1. In-window, has abstract + PMCID      → kept, accessible=True
#   2. In-window, has abstract, NO PMCID    → kept, accessible=False
#   3. Out-of-window                        → dropped
#   4. In-window, missing abstract          → dropped (include_abstract=True)
_EFETCH_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>

  <PubmedArticle>
    <MedlineCitation>
      <PMID>11111</PMID>
      <Article>
        <ArticleTitle>Open-access immunotherapy paper</ArticleTitle>
        <Abstract>
          <AbstractText Label="Background">Cancer is hard.</AbstractText>
          <AbstractText Label="Results">CAR-T worked.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Smith</LastName><ForeName>Alice</ForeName></Author>
          <Author><LastName>Zhou</LastName><Initials>B</Initials></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <History>
        <PubMedPubDate PubStatus="pubmed">
          <Year>2026</Year><Month>4</Month><Day>12</Day>
        </PubMedPubDate>
      </History>
      <ArticleIdList>
        <ArticleId IdType="pubmed">11111</ArticleId>
        <ArticleId IdType="doi">10.1000/open</ArticleId>
        <ArticleId IdType="pmc">PMC9999</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>

  <PubmedArticle>
    <MedlineCitation>
      <PMID>22222</PMID>
      <Article>
        <ArticleTitle>Paywalled paper</ArticleTitle>
        <Abstract>
          <AbstractText>Short abstract without a label.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Jones</LastName><ForeName>Carol</ForeName></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <History>
        <PubMedPubDate PubStatus="pubmed">
          <Year>2026</Year><Month>4</Month><Day>13</Day>
        </PubMedPubDate>
      </History>
      <ArticleIdList>
        <ArticleId IdType="pubmed">22222</ArticleId>
        <ArticleId IdType="doi">10.1000/closed</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>

  <PubmedArticle>
    <MedlineCitation>
      <PMID>33333</PMID>
      <Article>
        <ArticleTitle>Old paper</ArticleTitle>
        <Abstract><AbstractText>Old result.</AbstractText></Abstract>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <History>
        <PubMedPubDate PubStatus="pubmed">
          <Year>2020</Year><Month>1</Month><Day>1</Day>
        </PubMedPubDate>
      </History>
      <ArticleIdList>
        <ArticleId IdType="pubmed">33333</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>

  <PubmedArticle>
    <MedlineCitation>
      <PMID>44444</PMID>
      <Article>
        <ArticleTitle>No-abstract paper</ArticleTitle>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <History>
        <PubMedPubDate PubStatus="pubmed">
          <Year>2026</Year><Month>4</Month><Day>14</Day>
        </PubMedPubDate>
      </History>
      <ArticleIdList>
        <ArticleId IdType="pubmed">44444</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>

</PubmedArticleSet>
"""


# --- Parsing helpers --------------------------------------------------------


def test_parse_efetch_xml_keeps_in_window_and_flags_accessibility() -> None:
    items = _parse_efetch_xml(
        _EFETCH_XML,
        window=_window(),
        fetched_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
        source_id="pubmed_test",
        include_abstract=True,
    )
    # 4 articles in; 2 kept (one OOW, one no-abstract dropped).
    assert len(items) == 2

    by_pmid = {i.extra["pmid"]: i for i in items}
    assert set(by_pmid) == {"11111", "22222"}

    # PMCID article → full_text_accessible + pmc_url in extra
    open_item = by_pmid["11111"]
    assert open_item.full_text_accessible is True
    assert open_item.extra["pmc_id"] == "PMC9999"
    assert open_item.extra["pmc_url"] == "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9999/"
    assert open_item.source_type == "pubmed"
    assert open_item.source_id == "pubmed_test"
    assert str(open_item.url) == "https://pubmed.ncbi.nlm.nih.gov/11111/"

    # Structured abstract is concatenated with Label: prefixes
    assert "Background: Cancer is hard." in open_item.summary
    assert "Results: CAR-T worked." in open_item.summary

    # Non-PMCID article → flag False, no pmc_* in extra
    closed_item = by_pmid["22222"]
    assert closed_item.full_text_accessible is False
    assert "pmc_id" not in closed_item.extra
    assert "pmc_url" not in closed_item.extra


def test_parse_efetch_xml_ignores_reference_list_article_ids() -> None:
    """Regression: an article without its own PMCID/DOI whose bibliography
    contains papers with PMCIDs must NOT be flagged full_text_accessible.

    The bug was `.//ArticleId[@IdType="pmc"]` matching descendants inside
    <PubmedData>/<ReferenceList>/<Reference>/<ArticleIdList> — i.e. IDs
    of cited works — instead of the article's own <PubmedData>/<ArticleIdList>.
    """
    xml = b"""<?xml version="1.0"?>
    <PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>55555</PMID>
          <Article>
            <ArticleTitle>Paper with no own PMCID but cited refs have one</ArticleTitle>
            <Abstract><AbstractText>Some result.</AbstractText></Abstract>
          </Article>
        </MedlineCitation>
        <PubmedData>
          <History>
            <PubMedPubDate PubStatus="pubmed">
              <Year>2026</Year><Month>4</Month><Day>12</Day>
            </PubMedPubDate>
          </History>
          <ArticleIdList>
            <ArticleId IdType="pubmed">55555</ArticleId>
          </ArticleIdList>
          <ReferenceList>
            <Reference>
              <Citation>Some cited paper, 2019.</Citation>
              <ArticleIdList>
                <ArticleId IdType="pubmed">7777777</ArticleId>
                <ArticleId IdType="doi">10.1000/citedref</ArticleId>
                <ArticleId IdType="pmc">PMC1234567</ArticleId>
              </ArticleIdList>
            </Reference>
          </ReferenceList>
        </PubmedData>
      </PubmedArticle>
    </PubmedArticleSet>
    """
    items = _parse_efetch_xml(
        xml,
        window=_window(),
        fetched_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
        source_id="pubmed_test",
        include_abstract=True,
    )
    assert len(items) == 1
    item = items[0]
    assert item.extra["pmid"] == "55555"
    assert item.full_text_accessible is False
    assert "pmc_id" not in item.extra
    assert "pmc_url" not in item.extra
    # Same protection for DOI — the cited reference's DOI must not leak in.
    assert "doi" not in item.extra


def test_extract_title_flattens_inline_markup() -> None:
    xml = """<PubmedArticle><MedlineCitation><Article>
        <ArticleTitle>The <i>BRCA1</i> gene in <b>cancer</b></ArticleTitle>
    </Article></MedlineCitation></PubmedArticle>"""
    node = ET.fromstring(xml)
    assert _extract_title(node) == "The BRCA1 gene in cancer"


def test_extract_abstract_none_when_missing() -> None:
    xml = """<PubmedArticle><MedlineCitation><Article>
        <ArticleTitle>T</ArticleTitle>
    </Article></MedlineCitation></PubmedArticle>"""
    node = ET.fromstring(xml)
    assert _extract_abstract(node) is None


def test_extract_authors_skips_incomplete_nodes() -> None:
    xml = """<PubmedArticle><MedlineCitation><Article>
        <ArticleTitle>T</ArticleTitle>
        <AuthorList>
            <Author><LastName>Smith</LastName><ForeName>Alice</ForeName></Author>
            <Author><LastName>OnlyLast</LastName></Author>
            <Author><ForeName>OnlyFirst</ForeName></Author>
        </AuthorList>
    </Article></MedlineCitation></PubmedArticle>"""
    node = ET.fromstring(xml)
    authors = _extract_authors(node)
    assert authors == ["Alice Smith", "OnlyLast"]


def test_extract_pub_date_prefers_pubmed_status_over_article_date() -> None:
    xml = """<PubmedArticle>
      <MedlineCitation><Article>
        <ArticleDate DateType="Electronic">
          <Year>2025</Year><Month>12</Month><Day>1</Day>
        </ArticleDate>
      </Article></MedlineCitation>
      <PubmedData><History>
        <PubMedPubDate PubStatus="pubmed">
          <Year>2026</Year><Month>4</Month><Day>10</Day>
        </PubMedPubDate>
      </History></PubmedData>
    </PubmedArticle>"""
    node = ET.fromstring(xml)
    dt = _extract_pub_date(node)
    assert dt == datetime(2026, 4, 10, tzinfo=timezone.utc)


def test_extract_pub_date_handles_text_month_in_pubdate() -> None:
    xml = """<PubmedArticle><MedlineCitation><Article>
        <Journal><JournalIssue><PubDate>
          <Year>2026</Year><Month>Apr</Month><Day>13</Day>
        </PubDate></JournalIssue></Journal>
    </Article></MedlineCitation></PubmedArticle>"""
    node = ET.fromstring(xml)
    assert _extract_pub_date(node) == datetime(2026, 4, 13, tzinfo=timezone.utc)


def test_extract_pub_date_returns_none_when_only_year_present() -> None:
    xml = """<PubmedArticle><MedlineCitation><Article>
        <Journal><JournalIssue><PubDate>
          <Year>2026</Year>
        </PubDate></JournalIssue></Journal>
    </Article></MedlineCitation></PubmedArticle>"""
    node = ET.fromstring(xml)
    assert _extract_pub_date(node) is None


# --- End-to-end fetch with mocked HTTP --------------------------------------


@respx.mock
async def test_fetch_runs_esearch_then_efetch() -> None:
    respx.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi").mock(
        return_value=httpx.Response(
            200, json={"esearchresult": {"idlist": ["11111", "22222", "33333", "44444"]}},
        )
    )
    respx.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi").mock(
        return_value=httpx.Response(200, content=_EFETCH_XML)
    )

    src = PubMedSource(
        source_id="pubmed_test",
        name="Test",
        query="cancer immunotherapy",
        max_results=50,
        api_key_env=None,
        include_abstract=True,
    )
    items = await src.fetch(_window())

    assert len(items) == 2
    assert {i.extra["pmid"] for i in items} == {"11111", "22222"}


@respx.mock
async def test_fetch_skips_efetch_when_esearch_empty() -> None:
    # Empty idlist: no reason to call efetch — would waste a round trip.
    esearch = respx.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": []}}),
    )
    efetch = respx.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi").mock(
        return_value=httpx.Response(200, content=b"<PubmedArticleSet/>")
    )

    src = PubMedSource(
        source_id="pubmed_test", name="Test", query="q", max_results=10,
    )
    items = await src.fetch(_window())
    assert items == []
    assert esearch.called
    assert not efetch.called


@respx.mock
async def test_fetch_sends_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBMED_KEY", "secret-key-abc")

    esearch = respx.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": []}}),
    )

    src = PubMedSource(
        source_id="t", name="T", query="q", max_results=5, api_key_env="PUBMED_KEY",
    )
    await src.fetch(_window())

    called = esearch.calls.last.request
    assert "api_key=secret-key-abc" in str(called.url)


@respx.mock
async def test_fetch_raises_on_esearch_http_error() -> None:
    respx.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi").mock(
        return_value=httpx.Response(500)
    )
    src = PubMedSource(source_id="t", name="T", query="q", max_results=5)
    with pytest.raises(httpx.HTTPStatusError):
        await src.fetch(_window())
