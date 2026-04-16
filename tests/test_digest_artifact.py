"""Tests for summarization/digest_artifact.py — Digest JSON + MD output."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from research_digest.models.digest import Digest, DigestEntry
from research_digest.models.source_item import SourceItem
from research_digest.observability.run_context import RunContext
from research_digest.summarization.digest_artifact import write_digest_artifact


def _digest() -> Digest:
    item = SourceItem(
        source_id="nature-rss",
        source_type="rss",
        title="Paper A",
        summary="abs",
        url="https://ex.com/a",
        url_canonical="https://ex.com/a",
        published_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 4, 13, 12, 1, tzinfo=timezone.utc),
        content_hash="h",
        score=1.5,
    )
    entry = DigestEntry(item=item, summary="LLM-written paragraph.", section="PAPERS")
    return Digest(
        topic="cancer immunotherapy",
        digest_date=date(2026, 4, 13),
        intro="Intro text.",
        entries=[entry],
        model_used="deepseek/deepseek-v3.2",
    )


def test_writes_json_and_md(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    json_p, md_p = write_digest_artifact(rc, _digest())

    assert json_p.is_file() and md_p.is_file()
    assert json_p.name == "digest.json"
    assert md_p.name == "digest.md"


def test_json_round_trips_digest_shape(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    json_p, _ = write_digest_artifact(rc, _digest())

    data = json.loads(json_p.read_text(encoding="utf-8"))
    assert data["topic"] == "cancer immunotherapy"
    assert data["digest_date"] == "2026-04-13"
    assert data["model_used"] == "deepseek/deepseek-v3.2"
    assert len(data["entries"]) == 1
    assert data["entries"][0]["summary"] == "LLM-written paragraph."
    assert data["entries"][0]["section"] == "PAPERS"


def test_md_shows_intro_entries_and_model(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    _, md_p = write_digest_artifact(rc, _digest())

    md = md_p.read_text(encoding="utf-8")
    assert "cancer immunotherapy" in md
    assert "deepseek/deepseek-v3.2" in md
    assert "Intro text." in md
    assert "Paper A" in md
    assert "LLM-written paragraph." in md
    assert "📌 1." in md
    # Meta line should be rendered as a Markdown blockquote with date.
    assert "> 🏷" in md
    assert "📅 Apr 13" in md
    # Footer: both lines present, GitHub URL included.
    assert "Summarized by: deepseek/deepseek-v3.2" in md
    assert "Zhenglin17/Research-Agent" in md
