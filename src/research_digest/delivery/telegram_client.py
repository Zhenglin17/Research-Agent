"""Thin async Telegram Bot API client.

Why a hand-rolled httpx wrapper instead of python-telegram-bot:
  - V1 only calls one method: `sendMessage`. Pulling in the full
    python-telegram-bot library (with its own event loop, update
    polling, handlers, etc.) is wildly more than we need.
  - Using httpx keeps the dependency surface small and matches the
    style of our other outbound clients (OpenRouter, source fetchers).

What this file does (and doesn't):
  - It does: POST to `https://api.telegram.org/bot<token>/sendMessage`
    with our HTML-formatted text, handle 4xx/5xx, and report per-call
    outcome.
  - It doesn't: format digests (that's formatter.py), orchestrate
    retries across chat_ids (that's deliver.py), or know about
    Digest / SourceItem (it's a dumb transport).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config.schema import TelegramConfig
from ..config.secret_env import get_settings

log = logging.getLogger(__name__)


@dataclass
class SendResult:
    """Outcome of one sendMessage call. Used by deliver.py for logging
    and deciding whether to record_push."""

    chat_id: str
    ok: bool
    status_code: int | None  # HTTP status; None if the request never landed
    error: str | None        # human-readable reason when !ok


class TelegramClient:
    """One AsyncClient, reused across all chat_ids in a run.

    Construct once via `TelegramClient(config)`, call `send_message`
    per chat_id, then `aclose()` (or use as async context manager).
    """

    def __init__(self, config: TelegramConfig) -> None:
        self._config = config
        # Token lives in the environment, never in yaml (see secret_env.py).
        self._token = get_settings().telegram_bot_token
        self._client = httpx.AsyncClient(timeout=config.request_timeout_seconds)

    @property
    def _send_url(self) -> str:
        # Telegram's convention: bot token is part of the URL path. Avoid
        # ever logging this URL — it leaks the token.
        return f"{self._config.base_url}/bot{self._token}/sendMessage"

    async def send_message(self, chat_id: str, text: str) -> SendResult:
        """Send one text message to one chat.

        Args:
            chat_id: Telegram chat id as string (can be numeric like
                "-100123..." or "@channelname").
            text: Message body, already rendered in parse_mode's syntax
                (HTML in V1). Must be <= Telegram's 4096-char limit;
                splitting for longer messages is deliver.py's job.

        Returns:
            A SendResult. `ok=True` only when Telegram returned 200 and
            `{"ok": true}`. On any other outcome we return a result with
            `ok=False` rather than raising — deliver.py wants to keep
            going to the next chat_id instead of bailing mid-loop.

        Called by:
            `deliver.deliver_digest` — once per chat_id per message
            chunk.
        """
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": self._config.parse_mode,
            "disable_web_page_preview": self._config.disable_web_page_preview,
        }
        try:
            resp = await self._client.post(self._send_url, json=payload)
        except httpx.HTTPError as e:
            log.error("telegram send network error (chat=%s): %s", chat_id, e)
            return SendResult(chat_id=chat_id, ok=False, status_code=None, error=str(e))

        if resp.status_code != 200:
            # Telegram returns error details in the body. Log the body for
            # debugging (truncated) — it's safe, no token inside.
            body = resp.text[:500]
            log.error(
                "telegram send failed (chat=%s, status=%d): %s",
                chat_id, resp.status_code, body,
            )
            return SendResult(
                chat_id=chat_id,
                ok=False,
                status_code=resp.status_code,
                error=body,
            )

        data = resp.json()
        if not data.get("ok"):
            desc = data.get("description", "unknown")
            log.error("telegram send not ok (chat=%s): %s", chat_id, desc)
            return SendResult(
                chat_id=chat_id, ok=False, status_code=200, error=desc,
            )

        log.info("telegram send ok (chat=%s, chars=%d)", chat_id, len(text))
        return SendResult(chat_id=chat_id, ok=True, status_code=200, error=None)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
