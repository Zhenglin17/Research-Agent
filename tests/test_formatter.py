"""Tests for delivery/formatter.py — pure HTML rendering."""

from __future__ import annotations

from datetime import date, datetime, timezone

from research_digest.delivery.formatter import (
    first_author_short,
    render_digest,
    render_with_top_n,
    short_date,
    source_label,
    split_message,
)
from research_digest.models.digest import Digest, DigestEntry
from research_digest.models.source_item import SourceItem


def _item(
    title: str,
    source_id: str = "nature-rss",
    source_type: str = "rss",
    source_name: str | None = None,
    url: str = "https://ex.com/a",
    full_text_accessible: bool = False,
    authors: list[str] | None = None,
    extra: dict | None = None,
) -> SourceItem:
    return SourceItem(
        source_id=source_id,
        source_type=source_type,  # type: ignore[arg-type]
        source_name=source_name,
        title=title,
        summary="abs",
        url=url,
        url_canonical=url,
        authors=authors or [],
        published_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 4, 13, 12, 1, tzinfo=timezone.utc),
        content_hash="h",
        score=1.0,
        full_text_accessible=full_text_accessible,
        extra=extra or {},
    )


def _digest(entries: list[DigestEntry], intro: str = "Today's picks.") -> Digest:
    return Digest(
        topic="cancer immunotherapy",
        digest_date=date(2026, 4, 13),
        intro=intro,
        entries=entries,
        model_used="deepseek/deepseek-v3.2",
    )


def test_source_label_maps_type_to_canonical_name() -> None:
    assert source_label(_item("x", source_type="pubmed")) == "PubMed"
    assert source_label(_item("x", source_type="biorxiv", extra={"server": "biorxiv"})) == "bioRxiv"
    assert source_label(_item("x", source_type="biorxiv", extra={"server": "medrxiv"})) == "medRxiv"
    # RSS prefers the adapter-provided friendly name...
    assert source_label(_item("x", source_type="rss", source_name="Nature Cancer")) == "Nature Cancer"
    # ...and falls back to source_id when no friendly name is set.
    assert source_label(_item("x", source_type="rss", source_id="immunity")) == "immunity"


def test_short_date_is_locale_independent() -> None:
    out = short_date(date(2026, 4, 15))
    assert out == "Apr 15"


def test_first_author_short_handles_name_formats() -> None:
    assert first_author_short([]) is None
    assert first_author_short(["Jiajun Liu"]) == "Liu"  # "First Last"
    assert first_author_short(["Du, J."]) == "Du"  # "Last, Initial"
    assert first_author_short(["Jiajun Liu", "Fenhong Qian"]) == "Liu et al."
    assert first_author_short(["Madonna"]) == "Madonna"  # single token


def test_meta_line_shows_source_date_and_author() -> None:
    item = _item(
        "Paper",
        source_type="pubmed",
        authors=["Jiajun Liu", "Fenhong Qian"],
    )
    entry = DigestEntry(item=item, summary="body", section="PAPERS")
    out = render_digest(_digest([entry]))
    # Meta line sits between the title and the summary body.
    assert "🏷 <b>PubMed</b>" in out
    assert "📅 Apr 13" in out
    assert "👥 Liu et al." in out


def test_meta_line_omits_author_segment_when_no_authors() -> None:
    entry = DigestEntry(item=_item("Paper", authors=[]), summary="body", section="PAPERS")
    out = render_digest(_digest([entry]))
    assert "👥" not in out  # author emoji never rendered


def test_render_digest_includes_header_intro_entry_footer() -> None:
    entries = [DigestEntry(item=_item("Paper A"), summary="Finding X.", section="PAPERS")]
    out = render_digest(_digest(entries))

    assert "🔬 cancer immunotherapy — 2026-04-13" in out
    assert "Today&#x27;s picks." in out or "Today's picks." in out  # html.escape keeps '
    assert "📋 <b>Papers</b>" in out
    assert "<b>Paper A</b>" in out
    assert 'href="https://ex.com/a"' in out
    assert "Finding X." in out
    assert "✨ Summarized by: deepseek/deepseek-v3.2" in out
    assert "Zhenglin17/Research-Agent" in out  # footer link to project repo


def test_render_groups_by_section_in_canonical_order() -> None:
    blog = DigestEntry(item=_item("B-title"), summary="b", section="BLOGS")
    paper = DigestEntry(item=_item("P-title"), summary="p", section="PAPERS")
    # Supply in reversed order — PAPERS must still render before BLOGS.
    out = render_digest(_digest([blog, paper]))
    assert out.index("📋 <b>Papers</b>") < out.index("📝 <b>Blogs</b>")


def test_render_escapes_html_in_llm_text() -> None:
    # LLM might produce "<0.05" or "a & b". These must not reach Telegram raw.
    entries = [
        DigestEntry(item=_item("Trial <pivotal>"), summary="p<0.05 and a&b", section="PAPERS")
    ]
    out = render_digest(_digest(entries, intro="Intro <with brackets>"))
    assert "<pivotal>" not in out  # the '<' in title got escaped
    assert "&lt;pivotal&gt;" in out
    assert "p&lt;0.05 and a&amp;b" in out
    assert "&lt;with brackets&gt;" in out


def test_render_with_top_n_drops_tail() -> None:
    a = DigestEntry(item=_item("A"), summary="sa", section="PAPERS")
    b = DigestEntry(item=_item("B"), summary="sb", section="PAPERS")
    c = DigestEntry(item=_item("C"), summary="sc", section="PAPERS")
    out = render_with_top_n(_digest([a, b, c]), 2)
    assert "A" in out and "B" in out
    assert "<b>C</b>" not in out  # C dropped


def test_render_marks_full_text_accessible_and_shows_legend() -> None:
    accessible = DigestEntry(
        item=_item("Open paper", full_text_accessible=True),
        summary="s1", section="PAPERS",
    )
    paywalled = DigestEntry(
        item=_item("Closed paper", url="https://ex.com/b", full_text_accessible=False),
        summary="s2", section="PAPERS",
    )
    out = render_digest(_digest([accessible, paywalled]))

    assert "📖 1. <b>Open paper</b>" in out
    assert "📌 2. <b>Closed paper</b>" in out
    # Legend appears because at least one entry is accessible.
    assert "📖 = full text accessible" in out


def test_render_hides_legend_when_no_accessible_items() -> None:
    paywalled = DigestEntry(
        item=_item("Closed paper", full_text_accessible=False),
        summary="s", section="PAPERS",
    )
    out = render_digest(_digest([paywalled]))
    assert "📖" not in out


def test_split_message_returns_single_when_under_limit() -> None:
    msg = "short message"
    assert split_message(msg, hard_limit=4096) == [msg]


def test_split_message_prefers_blank_line_boundary() -> None:
    msg = "A" * 100 + "\n\n" + "B" * 100
    [first, second] = split_message(msg, hard_limit=150)
    assert first == "A" * 100
    assert second == "B" * 100


def test_split_message_falls_back_to_newline() -> None:
    # No blank-line boundary — only single newlines.
    msg = "A" * 80 + "\n" + "B" * 80
    chunks = split_message(msg, hard_limit=100)
    assert len(chunks) == 2
    assert chunks[0].startswith("A")
    assert chunks[1].startswith("B")


def test_split_message_hard_cut_when_no_boundary() -> None:
    msg = "X" * 300  # no newlines at all
    chunks = split_message(msg, hard_limit=100)
    assert len(chunks) == 3
    assert all(len(c) <= 100 for c in chunks)
