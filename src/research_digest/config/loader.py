"""Load business config from YAML into typed pydantic models.

Thin glue layer: file I/O + yaml parsing + pydantic validation. No defaults,
no path-searching magic — callers pass an explicit path so behavior stays
predictable in tests and in cron runs.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .schema import AppConfig
from .sources_schema import SourcesConfig

DEFAULT_CONFIG_PATH = Path("config/settings.yaml")
DEFAULT_SOURCES_PATH = Path("config/sources.yaml")


def load_app_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Read a YAML file and return a validated AppConfig.

    Args:
        path: Path to the yaml file. Defaults to config/settings.yaml relative
              to the current working directory.

    Returns:
        Fully validated AppConfig.

    Raises:
        FileNotFoundError: If the file does not exist — the message points
            the user at the expected path so they can create it.
        yaml.YAMLError: If the file is not valid YAML.
        pydantic.ValidationError: If the YAML content does not match AppConfig.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Config file not found at {path!s}. "
            f"Create it (see config/settings.example.yaml for a starting point)."
        )

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        # Empty file — yaml.safe_load returns None; treat as "no fields given"
        # and let pydantic raise a clear error about missing required `topic`.
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Config at {path!s} must contain a YAML mapping at the top level, "
            f"got {type(raw).__name__}."
        )

    return AppConfig(**raw)


def load_sources_config(path: Path | str = DEFAULT_SOURCES_PATH) -> SourcesConfig:
    """Read sources.yaml and return a validated SourcesConfig.

    Same contract as `load_app_config`, just a different schema. Split into
    its own function so each config file has an obvious, single loader.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Sources file not found at {path!s}. "
            f"Create it with at least `sources: []`."
        )

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Sources config at {path!s} must be a YAML mapping at the top level, "
            f"got {type(raw).__name__}."
        )

    return SourcesConfig(**raw)
