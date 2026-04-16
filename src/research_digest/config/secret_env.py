"""Environment-level settings: secrets and per-machine overrides.

These values come from the OS environment (typically via a local .env file)
and are distinct from business config in config/*.yaml. Anything sensitive
or machine-specific belongs here; anything that describes *what the bot does*
belongs in yaml.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Typed accessor for everything loaded from the environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # tolerate unrelated env vars on the host
        case_sensitive=False,
    )

    openrouter_api_key: str = Field(..., min_length=10)
    telegram_bot_token: str = Field(..., min_length=10)
    log_level: LogLevel = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    First call reads the .env file and validates all required secrets exist.
    Subsequent calls in the same process return the cached instance.
    """
    return Settings()  # type: ignore[call-arg]
