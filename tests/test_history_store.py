"""Tests for storage/history_store.py."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from research_digest.models.source_item import SourceItem
from research_digest.storage.history_store import HistoryStore


def _item(
    source_id: str = "s",
    url: str = "https://example.com/a",
    title: str = "T",
    content_hash: str | None = "h1",
) -> SourceItem:
    return SourceItem(
        source_id=source_id,
        source_type="rss",
        title=title,
        url=url,
        url_canonical=url,
        published_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        content_hash=content_hash,
    )


def test_init_creates_db_file(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "h.db"
    s = HistoryStore(db)
    assert db.is_file()
    assert s.count() == 0
    s.close()


def test_record_and_query_by_url(tmp_path: Path) -> None:
    with HistoryStore(tmp_path / "h.db") as s:
        s.record_push(_item(url="https://a/x"), user_id="u1")
        assert s.has_been_pushed(
            url_canonical="https://a/x", content_hash="anything", user_id="u1"
        )
        assert not s.has_been_pushed(
            url_canonical="https://a/y", content_hash="different", user_id="u1"
        )


def test_query_by_content_hash(tmp_path: Path) -> None:
    with HistoryStore(tmp_path / "h.db") as s:
        s.record_push(_item(url="https://a/x", content_hash="hhh"), user_id="u1")
        # Different URL but same hash counts as duplicate.
        assert s.has_been_pushed(
            url_canonical="https://a/z", content_hash="hhh", user_id="u1"
        )


def test_scoped_per_user(tmp_path: Path) -> None:
    with HistoryStore(tmp_path / "h.db") as s:
        s.record_push(_item(), user_id="u1")
        assert s.has_been_pushed(
            url_canonical="https://example.com/a",
            content_hash="h1",
            user_id="u1",
        )
        # Another user hasn't seen it.
        assert not s.has_been_pushed(
            url_canonical="https://example.com/a",
            content_hash="h1",
            user_id="u2",
        )


def test_record_push_requires_hash(tmp_path: Path) -> None:
    with HistoryStore(tmp_path / "h.db") as s:
        with pytest.raises(ValueError):
            s.record_push(_item(content_hash=None), user_id="u1")


def test_has_been_pushed_with_none_hash(tmp_path: Path) -> None:
    # Defensive path: url-only lookup still works when hash is missing.
    with HistoryStore(tmp_path / "h.db") as s:
        s.record_push(_item(url="https://a/x"), user_id="u1")
        assert s.has_been_pushed(
            url_canonical="https://a/x", content_hash=None, user_id="u1"
        )


def test_prune_older_than(tmp_path: Path) -> None:
    with HistoryStore(tmp_path / "h.db") as s:
        # Insert a row with a backdated pushed_at.
        s._conn.execute(
            "INSERT INTO digest_history "
            "(url_canonical, content_hash, title_norm, user_id, pushed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("https://old/x", "hx", "old", "u1", "2020-01-01T00:00:00+00:00"),
        )
        s._conn.commit()
        s.record_push(_item(url="https://new/y"), user_id="u1")  # fresh
        assert s.count() == 2

        deleted = s.prune_older_than(days=30)
        assert deleted == 1
        assert s.count() == 1


def test_context_manager_closes(tmp_path: Path) -> None:
    with HistoryStore(tmp_path / "h.db") as s:
        s.record_push(_item(), user_id="u1")
    # After exit, reopen: data persists, connection was closed cleanly.
    with HistoryStore(tmp_path / "h.db") as s2:
        assert s2.count() == 1
