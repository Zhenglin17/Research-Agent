"""Tests for delivery/deliver.py — length policy + push fan-out + history record."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from research_digest.config.schema import (
    AppConfig,
    LLMConfig,
    OutputConfig,
    RetentionConfig,
    TelegramConfig,
)
from research_digest.delivery import deliver as deliver_mod
from research_digest.delivery.deliver import deliver_digest, prepare_messages
from research_digest.delivery.telegram_client import SendResult
from research_digest.models.digest import Digest, DigestEntry
from research_digest.models.source_item import SourceItem
from research_digest.storage.history_store import HistoryStore


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy-token-1234567890")
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key-1234567890")
    from research_digest.config.secret_env import get_settings
    get_settings.cache_clear()


def _item(i: int, long: bool = False) -> SourceItem:
    summary = ("abstract " * 80) if long else "abs"
    return SourceItem(
        source_id="nature-rss",
        source_type="rss",
        title=f"Paper {i}",
        summary=summary,
        url=f"https://ex.com/{i}",
        url_canonical=f"https://ex.com/{i}",
        published_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 4, 13, 12, 1, tzinfo=timezone.utc),
        content_hash=f"h{i}",
        score=float(100 - i),  # descending — item 0 is highest-ranked
    )


def _digest(n: int, long: bool = False, summary_chars: int = 20) -> Digest:
    entries = [
        DigestEntry(item=_item(i, long=long), summary="s" * summary_chars, section="PAPERS")
        for i in range(n)
    ]
    return Digest(
        topic="t",
        digest_date=date(2026, 4, 13),
        intro="Intro.",
        entries=entries,
        model_used="deepseek/deepseek-v3.2",
    )


def _app_config(
    chat_ids: list[str] | None = None,
    soft: int = 3800,
    hard: int = 4096,
    floor: int = 8,
) -> AppConfig:
    return AppConfig(
        topic="t",
        telegram=TelegramConfig(chat_ids=chat_ids or []),
        output=OutputConfig(
            telegram_single_message_soft_limit=soft,
            telegram_hard_limit=hard,
            min_items_floor=floor,
        ),
        retention=RetentionConfig(history_days=30),
        llm=LLMConfig(),
    )


# --- prepare_messages ------------------------------------------------------


async def test_prepare_messages_fits_as_is() -> None:
    digest = _digest(3, summary_chars=20)
    chunks = await prepare_messages(digest, app_config=_app_config(hard=4096))
    assert len(chunks) == 1


async def test_prepare_messages_splits_when_over_hard_limit() -> None:
    digest = _digest(3, summary_chars=400)
    chunks = await prepare_messages(
        digest, app_config=_app_config(hard=500)
    )
    assert len(chunks) >= 2
    assert all(len(c) <= 500 for c in chunks)


# --- deliver_digest fan-out + history record ------------------------------


class _FakeTelegramClient:
    """Records calls and returns programmable results per chat_id."""

    def __init__(self, results_by_chat: dict[str, list[SendResult]]) -> None:
        self._results = results_by_chat
        self._idx: dict[str, int] = {cid: 0 for cid in results_by_chat}
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> SendResult:
        self.sent.append((chat_id, text))
        i = self._idx[chat_id]
        self._idx[chat_id] += 1
        return self._results[chat_id][i]

    async def __aenter__(self) -> "_FakeTelegramClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _ok(chat_id: str) -> SendResult:
    return SendResult(chat_id=chat_id, ok=True, status_code=200, error=None)


def _fail(chat_id: str) -> SendResult:
    return SendResult(chat_id=chat_id, ok=False, status_code=400, error="nope")


async def test_deliver_records_history_when_any_chat_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    digest = _digest(2)
    app_config = _app_config(chat_ids=["ok-chat", "bad-chat"], soft=4000, floor=1)

    fake = _FakeTelegramClient({"ok-chat": [_ok("ok-chat")], "bad-chat": [_fail("bad-chat")]})
    monkeypatch.setattr(deliver_mod, "TelegramClient", lambda cfg: fake)

    store = HistoryStore(tmp_path / "h.db")
    try:
        results = await deliver_digest(digest, app_config=app_config, history_store=store)
    finally:
        store.close()

    assert results["ok-chat"][0].ok is True
    assert results["bad-chat"][0].ok is False
    # Record written once per entry because at least one chat_id fully succeeded.
    store2 = HistoryStore(tmp_path / "h.db")
    try:
        assert store2.count() == 2
    finally:
        store2.close()


async def test_deliver_skips_history_when_all_chats_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    digest = _digest(2)
    app_config = _app_config(chat_ids=["bad1", "bad2"], soft=4000, floor=1)

    fake = _FakeTelegramClient({"bad1": [_fail("bad1")], "bad2": [_fail("bad2")]})
    monkeypatch.setattr(deliver_mod, "TelegramClient", lambda cfg: fake)

    store = HistoryStore(tmp_path / "h.db")
    try:
        await deliver_digest(digest, app_config=app_config, history_store=store)
        assert store.count() == 0  # nothing recorded
    finally:
        store.close()


async def test_deliver_noop_when_no_chat_ids(tmp_path: Path) -> None:
    digest = _digest(1)
    app_config = _app_config(chat_ids=[])  # empty

    store = HistoryStore(tmp_path / "h.db")
    try:
        results = await deliver_digest(digest, app_config=app_config, history_store=store)
        assert results == {}
        assert store.count() == 0
    finally:
        store.close()
