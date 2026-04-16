"""Tests for observability/artifact_store.py."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from research_digest.models.source_item import SourceItem
from research_digest.observability.artifact_store import write_stage_artifact
from research_digest.observability.run_context import RunContext


def _item(source_id: str, title: str, summary: str = "hello") -> SourceItem:
    return SourceItem(
        source_id=source_id,
        source_type="rss",
        title=title,
        summary=summary,
        url="https://example.com/article",
        url_canonical="https://example.com/article",
        published_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 4, 13, 12, 1, tzinfo=timezone.utc),
    )


def test_writes_both_files(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    json_p, md_p = write_stage_artifact(rc, "fetched", [_item("a", "t1")])
    assert json_p.is_file()
    assert md_p.is_file()


def test_json_is_valid_and_reloadable(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    items = [_item("a", "t1"), _item("a", "t2")]
    json_p, _ = write_stage_artifact(rc, "fetched", items)

    reloaded = json.loads(json_p.read_text(encoding="utf-8"))
    assert len(reloaded) == 2
    assert reloaded[0]["title"] == "t1"
    # datetimes should come back as ISO strings, not Python objects
    assert isinstance(reloaded[0]["published_at"], str)


def test_md_contains_per_source_counts(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    items = [_item("a", "t1"), _item("a", "t2"), _item("b", "t3")]
    _, md_p = write_stage_artifact(rc, "fetched", items)

    md = md_p.read_text(encoding="utf-8")
    assert "Total items:** 3" in md
    assert "`a` — 2" in md
    assert "`b` — 1" in md


def test_md_truncates_long_summary(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    long = "x" * 1000
    _, md_p = write_stage_artifact(rc, "fetched", [_item("a", "t", summary=long)])
    md = md_p.read_text(encoding="utf-8")
    assert "…" in md
    assert "x" * 1000 not in md  # full untruncated text should not appear


def test_empty_items_still_writes(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    json_p, md_p = write_stage_artifact(rc, "fetched", [])
    assert json_p.is_file() and md_p.is_file()
    assert json.loads(json_p.read_text()) == []
    assert "Total items:** 0" in md_p.read_text()
