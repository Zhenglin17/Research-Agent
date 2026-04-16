"""Business-configuration schema.

Defines the shape of everything that lives in config/*.yaml.
No I/O here — this module only declares types. loader.py is responsible for
actually reading yaml into these models.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Sub-configs (each pipeline stage gets its own small block)
# ---------------------------------------------------------------------------


class LimitsConfig(BaseModel):
    """Bounds on how much content enters the pipeline."""

    model_config = ConfigDict(extra="forbid")

    lookback_days: int = Field(default=3, ge=1, le=30)
    max_items_per_source: int = Field(default=100, ge=1)
    max_candidates_total: int = Field(default=300, ge=1)
    max_digest_items: int = Field(default=10, ge=1, le=50)


class RankingWeights(BaseModel):
    """Weights for the deterministic scoring formula (see design §25.7).

    score = w_topic * topic_match_ratio
          + w_source * source_weight
          + w_focus * focus_keyword_hits
          + w_freshness * freshness
          + w_llm_relevance * llm_relevance   # reserved, always 0 in V1

    Note: include_keywords and exclude_keywords are HARD filters applied
    before ranking (see ranking/filter_rules.py), not scoring terms. An
    item that hits any exclude keyword, or fails to hit any include
    keyword when that list is non-empty, is dropped before it reaches
    scoring.
    """

    model_config = ConfigDict(extra="forbid")

    topic_match: float = 1.0
    source_weight: float = 0.5
    focus_keyword: float = 0.8
    freshness: float = 0.6
    llm_relevance: float = 0.0  # reserved for a later milestone


class DedupeConfig(BaseModel):
    """Thresholds for the three-layer dedupe strategy."""

    model_config = ConfigDict(extra="forbid")

    content_hash_prefix_chars: int = Field(default=500, ge=50)
    title_similarity_threshold: float = Field(default=0.85, ge=0.0, le=1.0)


class LLMConfig(BaseModel):
    """Which model to call and how.

    `model` is the everyday default (used for dry-run, debug-run, and when
    `test_models` is empty). `test_models`, if non-empty, enables daily
    rotation across that list in production `run` — so the same user can
    see different model outputs on different days and compare. Rotation
    is deterministic from the current date (no persistent state needed).
    The footer of each pushed digest prints the model that was actually
    used.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = "deepseek/deepseek-v3.2"
    test_models: list[str] = Field(default_factory=list)
    base_url: str = "https://openrouter.ai/api/v1"
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=2000, ge=256)
    request_timeout_seconds: float = Field(default=60.0, ge=5.0)


class TelegramConfig(BaseModel):
    """Bot API delivery settings.

    `chat_ids` is a list to support pushing the same digest to multiple
    recipients. Bot token is NOT here — it lives in .env (see
    config/secret_env.py). Push history is shared across chat_ids because
    V1 sends an identical digest to everyone (see design discussion).
    """

    model_config = ConfigDict(extra="forbid")

    chat_ids: list[str] = Field(default_factory=list)
    parse_mode: str = "HTML"  # Telegram supports "HTML" or "MarkdownV2"
    disable_web_page_preview: bool = True  # less clutter on phone screens
    base_url: str = "https://api.telegram.org"
    request_timeout_seconds: float = Field(default=20.0, ge=5.0)


class OutputConfig(BaseModel):
    """Digest output preferences, including Telegram length policy.

    Length degradation order when the formatted digest exceeds the soft
    single-message budget (see design §25.4):
      1. drop the lowest-ranked item (one at a time), down to min_items_floor
      2. if still too long, run an LLM compress pass that preserves all
         remaining items
      3. finally, split into two Telegram messages
    """

    model_config = ConfigDict(extra="forbid")

    language: str = "zh"  # "en" = skip translation; anything else = translate
    # Soft target handed to the LLM in the prompt; leaves room for HTML tags
    # below the hard Telegram limit of 4096 chars per message.
    telegram_single_message_soft_limit: int = Field(default=3800, ge=500, le=4096)
    telegram_hard_limit: int = 4096  # fixed by Telegram Bot API
    # Minimum items kept before switching from "drop" to "compress". See
    # OutputConfig docstring for the degradation sequence.
    min_items_floor: int = Field(default=8, ge=1)


class RetentionConfig(BaseModel):
    """How long local history is kept."""

    model_config = ConfigDict(extra="forbid")

    history_days: int = Field(default=30, ge=1)


class ObservabilityConfig(BaseModel):
    """Per-run artifact and log retention policy.

    Two knobs, intentionally coarse:

    - `write_artifacts`: when False, the pipeline skips every JSON/MD
      artifact (fetched/deduped/filtered/ranked/digest/delivery). Logs
      are still written — they're small and the only signal we have
      when production goes wrong. Flip this to False in production cron
      to keep `data/` small; leave it True for dry-run / debug-run.

    - `max_runs_kept`: at the end of every pipeline run we sweep
      `data/artifacts/` and `data/logs/` and keep only the most recent
      N runs by run_id (which starts with a UTC timestamp, so name
      sort == time sort). Applied unconditionally across modes — even
      dry-run leaves a bounded footprint on disk.
    """

    model_config = ConfigDict(extra="forbid")

    write_artifacts: bool = True
    max_runs_kept: int = Field(default=20, ge=1)


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """Top-level business configuration loaded from config/settings.yaml."""

    model_config = ConfigDict(extra="forbid")

    topic: str  # required; everything else has defaults
    timezone: str = "Asia/Shanghai"

    focus_keywords: list[str] = Field(default_factory=list)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)

    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    ranking: RankingWeights = Field(default_factory=RankingWeights)
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
