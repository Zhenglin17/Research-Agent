"""Tests for pipeline/dedupe_stage.py."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from research_digest.config.schema import DedupeConfig
from research_digest.models.source_item import SourceItem
from research_digest.pipeline.dedupe_stage import (
    compute_content_hash,
    dedupe,
    dedupe_within_run,
    filter_already_pushed,
    jaccard,
)
from research_digest.storage.history_store import HistoryStore


def _item(
    url: str,
    title: str,
    content: str | None = None,
    summary: str | None = None,
) -> SourceItem:
    return SourceItem(
        source_id="s",
        source_type="rss",
        title=title,
        summary=summary,
        content=content,
        url=url,
        url_canonical=url,
        published_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
    )


def _cfg(threshold: float = 0.85) -> DedupeConfig:
    return DedupeConfig(title_similarity_threshold=threshold)


# --- jaccard -----------------------------------------------------------------


def test_jaccard_identical_is_one() -> None:
    assert jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0


def test_jaccard_disjoint_is_zero() -> None:
    assert jaccard(frozenset({"a"}), frozenset({"b"})) == 0.0


def test_jaccard_empty_is_zero() -> None:
    assert jaccard(frozenset(), frozenset({"a"})) == 0.0


def test_jaccard_partial_overlap() -> None:
    # {a,b,c} ∩ {b,c,d} = {b,c} (2), union = 4 → 0.5
    assert jaccard(frozenset("abc"), frozenset("bcd")) == 0.5


# --- compute_content_hash ----------------------------------------------------


def test_content_hash_is_deterministic() -> None:
    i1 = _item("https://x/a", "Same Title", content="body")
    i2 = _item("https://y/b", "Same Title", content="body")
    assert compute_content_hash(i1, 500) == compute_content_hash(i2, 500)


def test_content_hash_differs_on_title() -> None:
    i1 = _item("https://x/a", "Title One", content="body")
    i2 = _item("https://x/a", "Title Two", content="body")
    assert compute_content_hash(i1, 500) != compute_content_hash(i2, 500)


def test_content_hash_uses_summary_when_content_missing() -> None:
    i1 = _item("https://x/a", "T", summary="hello world")
    i2 = _item("https://y/a", "T", summary="hello world")
    assert compute_content_hash(i1, 500) == compute_content_hash(i2, 500)


# --- dedupe_within_run: layer 1 (URL) ---------------------------------------


def test_dedupe_drops_by_canonical_url() -> None:
    items = [
        _item("https://example.com/a", "Paper A"),
        _item("https://example.com/a", "Totally Different Title"),
    ]
    kept, stats = dedupe_within_run(items, _cfg())
    assert len(kept) == 1
    assert stats.by_url == 1


# --- dedupe_within_run: layer 2 (content hash) -------------------------------


def test_dedupe_drops_by_content_hash() -> None:
    items = [
        _item("https://a.com/x", "Same Title", content="identical body text"),
        _item("https://b.com/y", "Same Title", content="identical body text"),
    ]
    kept, stats = dedupe_within_run(items, _cfg())
    assert len(kept) == 1
    assert stats.by_hash == 1


def test_dedupe_populates_content_hash_on_survivors() -> None:
    items = [_item("https://x/a", "T1"), _item("https://x/b", "T2")]
    kept, _ = dedupe_within_run(items, _cfg())
    assert all(i.content_hash is not None for i in kept)


# --- dedupe_within_run: layer 3 (title similarity) --------------------------


def test_dedupe_drops_by_title_similarity() -> None:
    items = [
        _item("https://a.com/x", "Novel CAR-T therapy results in leukemia patients"),
        # Same meaningful tokens, different order/content → high Jaccard.
        _item("https://b.com/y", "CAR-T results novel therapy leukemia patients"),
    ]
    kept, stats = dedupe_within_run(items, _cfg(threshold=0.8))
    assert len(kept) == 1
    assert stats.by_title_sim == 1


def test_dedupe_keeps_dissimilar_titles() -> None:
    items = [
        _item("https://a.com/x", "Quantum computing breakthrough announced"),
        _item("https://b.com/y", "CAR-T therapy clinical trial results"),
    ]
    kept, stats = dedupe_within_run(items, _cfg())
    assert len(kept) == 2
    assert stats.total() == 0


# --- dedupe_within_run: precedence ------------------------------------------


def test_dedupe_url_checked_before_hash() -> None:
    # Same URL, different titles → should be counted as by_url, not by_hash.
    items = [
        _item("https://x/a", "Title One", content="body alpha"),
        _item("https://x/a", "Title Two", content="body beta"),
    ]
    _, stats = dedupe_within_run(items, _cfg())
    assert stats.by_url == 1
    assert stats.by_hash == 0


# --- filter_already_pushed ---------------------------------------------------


def test_filter_already_pushed_drops_seen(tmp_path: Path) -> None:
    with HistoryStore(tmp_path / "h.db") as store:
        seen = _item("https://seen/x", "Old")
        seen.content_hash = "hhh"
        store.record_push(seen, user_id="u1")

        incoming = [
            _item("https://seen/x", "Old"),        # URL match
            _item("https://new/y", "Fresh"),       # neither match
        ]
        for it in incoming:
            it.content_hash = compute_content_hash(it, 500)
        kept, dropped = filter_already_pushed(incoming, store, user_id="u1")
        assert dropped == 1
        assert len(kept) == 1
        assert kept[0].title == "Fresh"


def test_filter_already_pushed_scoped_per_user(tmp_path: Path) -> None:
    with HistoryStore(tmp_path / "h.db") as store:
        seen = _item("https://seen/x", "Old")
        seen.content_hash = "hhh"
        store.record_push(seen, user_id="u1")

        incoming = [_item("https://seen/x", "Old")]
        incoming[0].content_hash = "hhh"
        # u2 has never seen this item.
        kept, dropped = filter_already_pushed(incoming, store, user_id="u2")
        assert dropped == 0
        assert len(kept) == 1


# --- dedupe (public) --------------------------------------------------------


def test_dedupe_without_history() -> None:
    items = [
        _item("https://a/x", "One"),
        _item("https://a/x", "One again — dup by URL"),
        _item("https://b/y", "Two"),
    ]
    out = dedupe(items, _cfg())
    assert len(out) == 2


def test_dedupe_with_history(tmp_path: Path) -> None:
    with HistoryStore(tmp_path / "h.db") as store:
        prior = _item("https://seen/x", "Already pushed")
        prior.content_hash = compute_content_hash(prior, 500)
        store.record_push(prior, user_id="u1")

        items = [
            _item("https://seen/x", "Already pushed"),
            _item("https://new/y", "Fresh one"),
        ]
        out = dedupe(items, _cfg(), history=store, user_id="u1")
        assert [i.title for i in out] == ["Fresh one"]


def test_dedupe_requires_user_id_with_history(tmp_path: Path) -> None:
    import pytest

    with HistoryStore(tmp_path / "h.db") as store:
        try:
            dedupe([_item("https://a/x", "T")], _cfg(), history=store)
        except ValueError:
            return
        pytest.fail("expected ValueError when user_id is omitted with history")


def test_dedupe_empty_input() -> None:
    assert dedupe([], _cfg()) == []
