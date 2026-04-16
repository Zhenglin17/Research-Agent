"""Tests for the retention-prune stage inside pipeline/digest_pipeline.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from research_digest.config.schema import AppConfig, RetentionConfig
from research_digest.pipeline.digest_pipeline import _prune_history
from research_digest.storage.history_store import HistoryStore


def test_prune_returns_deleted_count(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    try:
        cfg = AppConfig(topic="t", retention=RetentionConfig(history_days=30))
        # Empty table → prune returns 0, no crash.
        assert _prune_history(store, cfg) == 0
    finally:
        store.close()


def test_prune_is_non_fatal_on_error() -> None:
    """If prune_older_than raises, we must swallow it and return 0 — the
    digest has already been delivered."""
    bad = MagicMock(spec=HistoryStore)
    bad.prune_older_than.side_effect = RuntimeError("db locked")
    cfg = AppConfig(topic="t", retention=RetentionConfig(history_days=30))

    result = _prune_history(bad, cfg)  # must not raise

    assert result == 0
    bad.prune_older_than.assert_called_once_with(30)
