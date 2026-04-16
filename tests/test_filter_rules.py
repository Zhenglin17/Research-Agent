"""Tests for ranking/filter_rules.py."""

from __future__ import annotations

from datetime import datetime, timezone

from research_digest.models.source_item import SourceItem
from research_digest.ranking.filter_rules import apply_filter


def _item(title: str, summary: str | None = None, content: str | None = None) -> SourceItem:
    return SourceItem(
        source_id="s",
        source_type="rss",
        title=title,
        summary=summary,
        content=content,
        url="https://example.com/x",
        url_canonical="https://example.com/x",
        published_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
    )


def test_no_keywords_passes_everything() -> None:
    items = [_item("A"), _item("B")]
    out = apply_filter(items, include_keywords=[], exclude_keywords=[])
    assert len(out) == 2


def test_exclude_drops_on_title_hit() -> None:
    items = [_item("Retracted paper on X"), _item("Valid paper on Y")]
    out = apply_filter(items, include_keywords=[], exclude_keywords=["retracted"])
    assert [i.title for i in out] == ["Valid paper on Y"]


def test_exclude_matches_summary() -> None:
    items = [
        _item("Clean title", summary="this is an erratum for a previous study"),
        _item("Actually relevant", summary="real content"),
    ]
    out = apply_filter(items, include_keywords=[], exclude_keywords=["erratum"])
    assert [i.title for i in out] == ["Actually relevant"]


def test_exclude_matches_content() -> None:
    items = [
        _item("Clean title", content="full body mentions retracted somewhere"),
        _item("Totally unrelated", content="clean body"),
    ]
    out = apply_filter(items, include_keywords=[], exclude_keywords=["retracted"])
    assert [i.title for i in out] == ["Totally unrelated"]


def test_exclude_is_case_insensitive() -> None:
    items = [_item("RETRACTED notice")]
    out = apply_filter(items, include_keywords=[], exclude_keywords=["retracted"])
    assert out == []


def test_empty_include_does_not_filter() -> None:
    # Empty include list = rule disabled, NOT "nothing matches".
    items = [_item("A"), _item("B")]
    out = apply_filter(items, include_keywords=[], exclude_keywords=[])
    assert len(out) == 2


def test_include_requires_at_least_one_hit() -> None:
    items = [
        _item("CAR-T therapy results"),
        _item("Quantum computing news"),
    ]
    out = apply_filter(items, include_keywords=["CAR-T"], exclude_keywords=[])
    assert [i.title for i in out] == ["CAR-T therapy results"]


def test_include_any_keyword_is_enough() -> None:
    items = [
        _item("CAR-T therapy"),
        _item("PD-1 inhibitor study"),
        _item("Unrelated topic"),
    ]
    out = apply_filter(
        items, include_keywords=["CAR-T", "PD-1"], exclude_keywords=[]
    )
    assert {i.title for i in out} == {"CAR-T therapy", "PD-1 inhibitor study"}


def test_exclude_beats_include_when_both_hit() -> None:
    items = [_item("CAR-T therapy retracted by journal")]
    out = apply_filter(
        items, include_keywords=["CAR-T"], exclude_keywords=["retracted"]
    )
    assert out == []


def test_preserves_order() -> None:
    items = [_item("A matches"), _item("B no match"), _item("C matches")]
    out = apply_filter(
        items, include_keywords=["matches"], exclude_keywords=[]
    )
    assert [i.title for i in out] == ["A matches", "C matches"]


def test_empty_input() -> None:
    assert apply_filter([], include_keywords=["x"], exclude_keywords=["y"]) == []
