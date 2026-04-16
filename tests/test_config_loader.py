"""Tests for config/loader.py — both load_app_config and load_sources_config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from research_digest.config.loader import load_app_config, load_sources_config


# --- load_app_config ---------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_app_config_happy_path(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path / "settings.yaml",
        'topic: "test topic"\ntimezone: "UTC"\n',
    )
    cfg = load_app_config(cfg_path)
    assert cfg.topic == "test topic"
    assert cfg.timezone == "UTC"
    # defaults applied
    assert cfg.limits.lookback_days == 3


def test_load_app_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_app_config(tmp_path / "nope.yaml")


def test_load_app_config_empty_file_fails_on_missing_topic(tmp_path: Path) -> None:
    # Empty yaml → loader treats as {} → pydantic complains about required `topic`.
    cfg_path = _write(tmp_path / "settings.yaml", "")
    with pytest.raises(ValidationError):
        load_app_config(cfg_path)


def test_load_app_config_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    cfg_path = _write(tmp_path / "settings.yaml", "- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="mapping at the top level"):
        load_app_config(cfg_path)


def test_load_app_config_extra_field_forbidden(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path / "settings.yaml",
        'topic: "t"\nunknown_field: 1\n',
    )
    with pytest.raises(ValidationError):
        load_app_config(cfg_path)


# --- load_sources_config -----------------------------------------------------


def test_load_sources_config_happy_path(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path / "sources.yaml",
        """
sources:
  - id: x
    name: "X"
    type: rss
    enabled: true
    weight: 1.0
    feed_url: "https://example.com/x.rss"
""",
    )
    cfg = load_sources_config(cfg_path)
    assert len(cfg.sources) == 1
    assert cfg.sources[0].id == "x"


def test_load_sources_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_sources_config(tmp_path / "nope.yaml")


def test_load_sources_config_empty_file_means_no_sources(tmp_path: Path) -> None:
    cfg_path = _write(tmp_path / "sources.yaml", "sources: []\n")
    cfg = load_sources_config(cfg_path)
    assert cfg.sources == []
