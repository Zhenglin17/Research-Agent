"""Thin async LLM client wrapping OpenRouter via the OpenAI SDK.

Why OpenRouter via the OpenAI SDK:
  - Same protocol as OpenAI's API, so `openai.AsyncOpenAI(base_url=...)`
    works with zero extra glue.
  - Switching models is a one-line change (model id string); switching
    providers is a `base_url` change. No SDK swap needed.

What this file does (and doesn't):
  - It does: build a client from config, run a single chat completion,
    and pick today's model when rotation is configured.
  - It doesn't: know anything about prompts, digests, or source items.
    prompt_builder / summarizer own that. Keeping this file stupid makes
    it trivial to mock in tests.

Daily model rotation (design §25 / M4 Phase A):
  - `llm.test_models` empty  → always use `llm.model`.
  - `llm.test_models` set    → pick one deterministically from today's
    date, so the same date always yields the same model across retries
    and across multiple chat_ids. No persistent state needed.
"""

from __future__ import annotations

import logging
from datetime import date

from openai import AsyncOpenAI

from ..config.schema import LLMConfig
from ..config.secret_env import get_settings

log = logging.getLogger(__name__)


def select_model(llm_config: LLMConfig, today: date) -> str:
    """Pick the model to use for a given date.

    Args:
        llm_config: Parsed `llm:` section from settings.yaml.
        today: Date driving rotation. Caller passes it in (rather than
            calling `date.today()` internally) so tests can inject a
            fixed date and so the whole run uses one consistent pick.

    Returns:
        A model id string suitable for OpenAI-style `model=` parameter,
        e.g. `"deepseek/deepseek-v3.2"`.

    Called by:
        `summarizer.summarize_digest` — once per run, before any LLM
        call — then the chosen id is reused for every per-item and
        intro call so the footer accurately reflects what produced the
        text.
    """
    if not llm_config.test_models:
        return llm_config.model
    # toordinal() is a monotonically increasing integer, one per day.
    # Modulo gives a stable index across retries on the same day.
    idx = today.toordinal() % len(llm_config.test_models)
    return llm_config.test_models[idx]


def build_client(llm_config: LLMConfig) -> AsyncOpenAI:
    """Construct an AsyncOpenAI client pointed at OpenRouter.

    Args:
        llm_config: Parsed `llm:` config. Provides `base_url` and
            `request_timeout_seconds`. API key is pulled from the
            environment (.env), never from yaml.

    Returns:
        An AsyncOpenAI instance ready to call `.chat.completions.create`.

    Called by:
        `summarizer` (once per run, shared across all per-item and
        intro calls to reuse the underlying HTTP connection pool).
    """
    settings = get_settings()
    return AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url=llm_config.base_url,
        timeout=llm_config.request_timeout_seconds,
    )


async def complete(
    client: AsyncOpenAI,
    *,
    model: str,
    messages: list[dict[str, str]],
    llm_config: LLMConfig,
    max_tokens_override: int | None = None,
) -> str:
    """Run one chat completion and return the assistant's text.

    Args:
        client: A client from `build_client`. Reused across calls.
        model: Model id from `select_model`.
        messages: OpenAI chat format — list of `{role, content}` dicts.
            Built by `prompt_builder`.
        llm_config: Provides temperature and max_output_tokens.
        max_tokens_override: If set, use this instead of
            llm_config.max_output_tokens. Needed when a step (e.g.
            translation) requires more output than the default.

    Returns:
        The assistant message content, stripped of leading/trailing
        whitespace. On empty response, returns empty string — caller
        decides how to degrade.

    Raises:
        openai.APIError (and subclasses) on network / HTTP failures.
        The caller (summarizer) catches these and falls back gracefully
        so one bad item doesn't sink the whole digest.

    Called by:
        `summarizer`, concurrently via `asyncio.gather` — one call per
        item plus one call for the intro.
    """
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        temperature=llm_config.temperature,
        max_tokens=max_tokens_override or llm_config.max_output_tokens,
    )
    choice = resp.choices[0]
    text = (choice.message.content or "").strip()
    if not text:
        log.warning("llm returned empty content (model=%s)", model)
    return text
