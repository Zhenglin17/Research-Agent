"""Post-summarization translation step.

Takes a fully-assembled English Digest and translates the prose fields
(intro + per-entry summaries) into the configured target language via a
single LLM call. Titles, URLs, and metadata stay untouched.

Why a separate step (not inline in the summarize prompt):
  - English artifacts (ranked.md) are preserved for quality review.
  - Translation quality is independently tunable (swap prompt, swap model).
  - When language="en", the whole module is skipped — zero cost.

The LLM receives a JSON payload with intro + summaries and returns the
same shape translated. Structured I/O keeps parsing trivial and avoids
the fragility of regex-splitting free-form translated text back into
per-entry chunks.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config.schema import AppConfig
from ..models.digest import Digest
from .llm_client import build_client, complete
from .prompt_builder import PromptLibrary

log = logging.getLogger(__name__)

_TRANSLATE_PROMPT_FILE = "translate.md"


def needs_translation(app_config: AppConfig) -> bool:
    """Check whether the configured language requires a translation pass."""
    return app_config.output.language.lower() not in ("en", "english")


async def translate_digest(
    digest: Digest,
    *,
    app_config: AppConfig,
    prompts_dir: Path,
) -> Digest:
    """Translate a Digest's prose in-place and return it.

    Args:
        digest: The English digest from summarize_digest().
        app_config: Reads output.language and llm settings.
        prompts_dir: Directory containing translate.md.

    Returns:
        The same Digest object with intro and entry summaries replaced
        by their translated versions. If the LLM call fails, the
        original English text is kept (graceful degradation).

    Called by:
        digest_pipeline, between summarize and prepare_messages.
    """
    target_lang = app_config.output.language
    model = digest.model_used

    library = PromptLibrary.load(prompts_dir)
    system_prompt = library.get(_TRANSLATE_PROMPT_FILE)

    payload = {
        "target_language": target_lang,
        "intro": digest.intro,
        "summaries": [e.summary for e in digest.entries],
    }
    user_message = (
        "Translate the following digest JSON. Return ONLY valid JSON with "
        'the same structure: {"intro": "...", "summaries": ["...", ...]}. '
        "Keep every title, URL, and technical term as specified in your "
        "instructions.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    log.info(
        "translate start: language=%s entries=%d model=%s",
        target_lang, len(digest.entries), model,
    )

    # Translation output is roughly the same token count as the input.
    # 10 detailed entries can easily reach 12k+ tokens in the translated
    # JSON. 16000 gives comfortable headroom.
    translate_max_tokens = max(app_config.llm.max_output_tokens * 4, 16000)

    client = build_client(app_config.llm)
    try:
        raw = await complete(
            client, model=model, messages=messages, llm_config=app_config.llm,
            max_tokens_override=translate_max_tokens,
        )
        translated = _parse_response(raw, expected_count=len(digest.entries))
    except Exception as e:  # noqa: BLE001
        log.warning("translation failed, keeping English text: %s", e)
        return digest

    digest.intro = translated["intro"]
    for entry, translated_summary in zip(digest.entries, translated["summaries"]):
        entry.summary = translated_summary

    log.info("translate done: intro_chars=%d", len(digest.intro))
    return digest


def _parse_response(raw: str, *, expected_count: int) -> dict:
    """Extract the translated JSON from the LLM response.

    Handles common LLM quirks: markdown code fences around JSON,
    leading/trailing whitespace.
    """
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.index("\n")
        text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    data = json.loads(text)

    if not isinstance(data.get("intro"), str):
        raise ValueError("translated JSON missing 'intro' string")
    summaries = data.get("summaries")
    if not isinstance(summaries, list) or len(summaries) != expected_count:
        raise ValueError(
            f"expected {expected_count} summaries, got "
            f"{len(summaries) if isinstance(summaries, list) else type(summaries)}"
        )
    return data
