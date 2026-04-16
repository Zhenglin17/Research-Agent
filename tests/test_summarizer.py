"""Tests for summarization/summarizer.py — LLM mocked at llm_client boundary."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from research_digest.config.schema import AppConfig, LLMConfig
from research_digest.models.source_item import SourceItem
from research_digest.summarization import summarizer as summ_mod
from research_digest.summarization.summarizer import summarize_digest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy-token-1234567890")
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key-1234567890")
    from research_digest.config.secret_env import get_settings
    get_settings.cache_clear()


def _item(title: str = "Paper A") -> SourceItem:
    return SourceItem(
        source_id="nature-rss",
        source_type="rss",
        title=title,
        summary="original abstract",
        url="https://ex.com/a",
        url_canonical="https://ex.com/a",
        published_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 4, 13, 12, 1, tzinfo=timezone.utc),
        content_hash="h",
        score=1.0,
    )


async def test_summarize_digest_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every LLM call returns canned text; result is a fully populated Digest."""
    calls: list[list[dict]] = []

    async def fake_complete(client, *, model, messages, llm_config):
        calls.append(messages)
        # Distinguish intro from per-item by looking at the system prompt.
        if "intro paragraph" in messages[0]["content"].lower():
            return "Intro paragraph."
        return "Summary of paper."

    monkeypatch.setattr(summ_mod, "complete", fake_complete)
    monkeypatch.setattr(summ_mod, "build_client", lambda cfg: object())

    app_config = AppConfig(topic="cancer immunotherapy")
    digest = await summarize_digest(
        [_item("Paper A"), _item("Paper B")],
        app_config=app_config,
        prompts_dir=Path("prompts"),
        now=datetime(2026, 4, 13, tzinfo=timezone.utc),
    )

    assert digest.topic == "cancer immunotherapy"
    assert digest.digest_date == date(2026, 4, 13)
    assert digest.intro == "Intro paragraph."
    assert len(digest.entries) == 2
    assert digest.entries[0].summary == "Summary of paper."
    assert digest.entries[0].section == "PAPERS"
    assert digest.model_used == "deepseek/deepseek-v3.2"
    # 2 per-item + 1 intro = 3 LLM calls
    assert len(calls) == 3


async def test_summarize_digest_item_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-item exception → fallback summary; other items unaffected."""
    call_idx = {"n": 0}

    async def fake_complete(client, *, model, messages, llm_config):
        if "intro paragraph" in messages[0]["content"].lower():
            return "Intro."
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            raise RuntimeError("rate-limited")
        return "Good summary."

    monkeypatch.setattr(summ_mod, "complete", fake_complete)
    monkeypatch.setattr(summ_mod, "build_client", lambda cfg: object())

    digest = await summarize_digest(
        [_item("Paper A"), _item("Paper B")],
        app_config=AppConfig(topic="t"),
        prompts_dir=Path("prompts"),
        now=datetime(2026, 4, 13, tzinfo=timezone.utc),
    )

    # One entry used fallback (derived from item.summary), the other got LLM text.
    summaries = [e.summary for e in digest.entries]
    assert any("original abstract" in s for s in summaries)
    assert any("Good summary." in s for s in summaries)


async def test_summarize_digest_intro_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_complete(client, *, model, messages, llm_config):
        if "intro paragraph" in messages[0]["content"].lower():
            raise RuntimeError("intro LLM down")
        return "Per-item summary."

    monkeypatch.setattr(summ_mod, "complete", fake_complete)
    monkeypatch.setattr(summ_mod, "build_client", lambda cfg: object())

    digest = await summarize_digest(
        [_item()],
        app_config=AppConfig(topic="cancer immunotherapy"),
        prompts_dir=Path("prompts"),
        now=datetime(2026, 4, 13, tzinfo=timezone.utc),
    )

    # Fallback intro mentions topic and item count.
    assert "cancer immunotherapy" in digest.intro
    assert "1" in digest.intro


def test_select_model_rotation() -> None:
    from research_digest.summarization.llm_client import select_model

    cfg = LLMConfig(model="default", test_models=["m1", "m2", "m3"])
    # Deterministic from date — same date must yield same model.
    d = date(2026, 4, 13)
    assert select_model(cfg, d) == select_model(cfg, d)
    # Rotates across dates.
    picks = {select_model(cfg, date(2026, 4, i)) for i in range(1, 15)}
    assert picks == {"m1", "m2", "m3"}


def test_select_model_empty_uses_default() -> None:
    from research_digest.summarization.llm_client import select_model

    cfg = LLMConfig(model="default", test_models=[])
    assert select_model(cfg, date(2026, 4, 13)) == "default"
