"""Tests for delivery/telegram_client.py — HTTP mocked via respx."""

from __future__ import annotations

import os

import httpx
import pytest
import respx

from research_digest.config.schema import TelegramConfig


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """secret_env.get_settings() requires these — set dummy values."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy-token-1234567890")
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key-1234567890")
    # get_settings() is lru_cached — clear so the test env takes effect.
    from research_digest.config.secret_env import get_settings
    get_settings.cache_clear()


def _client():
    # Local import: module reads env at instantiation through get_settings().
    from research_digest.delivery.telegram_client import TelegramClient
    return TelegramClient(TelegramConfig(base_url="https://api.telegram.org"))


@respx.mock
async def test_send_message_ok() -> None:
    route = respx.post(
        "https://api.telegram.org/botdummy-token-1234567890/sendMessage"
    ).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))

    client = _client()
    try:
        result = await client.send_message("12345", "hello")
    finally:
        await client.aclose()

    assert route.called
    assert result.ok is True
    assert result.status_code == 200
    assert result.error is None


@respx.mock
async def test_send_message_http_error() -> None:
    respx.post(
        "https://api.telegram.org/botdummy-token-1234567890/sendMessage"
    ).mock(return_value=httpx.Response(400, text="Bad Request: chat not found"))

    client = _client()
    try:
        result = await client.send_message("bad-chat", "hello")
    finally:
        await client.aclose()

    assert result.ok is False
    assert result.status_code == 400
    assert "chat not found" in (result.error or "")


@respx.mock
async def test_send_message_telegram_level_error() -> None:
    # HTTP 200 but "ok": false — Telegram-level rejection.
    respx.post(
        "https://api.telegram.org/botdummy-token-1234567890/sendMessage"
    ).mock(
        return_value=httpx.Response(
            200, json={"ok": False, "description": "message is too long"}
        )
    )

    client = _client()
    try:
        result = await client.send_message("12345", "x" * 5000)
    finally:
        await client.aclose()

    assert result.ok is False
    assert result.status_code == 200
    assert result.error == "message is too long"


@respx.mock
async def test_send_message_network_error_returns_result_not_raises() -> None:
    respx.post(
        "https://api.telegram.org/botdummy-token-1234567890/sendMessage"
    ).mock(side_effect=httpx.ConnectError("boom"))

    client = _client()
    try:
        result = await client.send_message("12345", "hello")
    finally:
        await client.aclose()

    assert result.ok is False
    assert result.status_code is None
    assert "boom" in (result.error or "")
