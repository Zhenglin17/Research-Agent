"""Tests for the observability flag + run-dir retention.

Covers:
  * artifact writers honor `rc.write_artifacts=False` (no files written)
  * `ensure_dirs()` skips artifact dir when flag is False
  * `_prune_run_dirs` keeps the N most recent runs and deletes the rest
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from research_digest.models.digest import Digest, DigestEntry
from research_digest.models.source_item import SourceItem
from research_digest.observability.artifact_store import write_stage_artifact
from research_digest.observability.run_context import RunContext
from research_digest.pipeline.digest_pipeline import _prune_run_dirs
from research_digest.summarization.digest_artifact import write_digest_artifact


def _item(url: str = "https://ex.com/a") -> SourceItem:
    return SourceItem(
        source_id="s",
        source_type="rss",
        title="t",
        summary="abs",
        url=url,
        url_canonical=url,
        published_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 4, 13, 12, 1, tzinfo=timezone.utc),
    )


def _digest() -> Digest:
    return Digest(
        topic="t",
        digest_date=date(2026, 4, 13),
        intro="i",
        entries=[DigestEntry(item=_item(), summary="s", section="PAPERS")],
        model_used="m",
    )


# --- write_artifacts=False guards -------------------------------------------


def test_ensure_dirs_skips_artifact_dir_when_disabled(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path, write_artifacts=False)
    rc.ensure_dirs()
    assert rc.log_dir.is_dir()
    assert not rc.artifact_dir.exists()


def test_write_stage_artifact_noop_when_disabled(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path, write_artifacts=False)
    result = write_stage_artifact(rc, "fetched", [_item()])
    assert result is None
    # Artifact dir must not have been created behind our back.
    assert not rc.artifact_dir.exists()


def test_write_digest_artifact_noop_when_disabled(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path, write_artifacts=False)
    result = write_digest_artifact(rc, _digest())
    assert result is None
    assert not rc.artifact_dir.exists()


def test_write_stage_artifact_still_writes_when_enabled(tmp_path: Path) -> None:
    # Baseline — flipping back on restores the previous behaviour.
    rc = RunContext(data_root=tmp_path, write_artifacts=True)
    result = write_stage_artifact(rc, "fetched", [_item()])
    assert result is not None
    json_p, md_p = result
    assert json_p.is_file() and md_p.is_file()


# --- _prune_run_dirs -------------------------------------------------------


def _seed_run_dirs(data_root: Path, n: int) -> list[str]:
    """Create N fake run artifact dirs + log files. Returns the run_ids
    in chronological (ascending) order — so r_ids[-1] is the newest."""
    r_ids = [f"20260401T000000Z-{i:06x}" for i in range(n)]
    artifacts_root = data_root / "artifacts"
    logs_root = data_root / "logs"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)
    for rid in r_ids:
        d = artifacts_root / f"run-{rid}"
        d.mkdir()
        (d / "ranked.json").write_text("{}", encoding="utf-8")
        (logs_root / f"run-{rid}.log").write_text("log\n", encoding="utf-8")
    return r_ids


def test_prune_keeps_top_n_and_deletes_rest(tmp_path: Path) -> None:
    r_ids = _seed_run_dirs(tmp_path, n=5)
    rc = RunContext(data_root=tmp_path)

    _prune_run_dirs(rc, max_runs_kept=2)

    kept_dirs = sorted(p.name for p in (tmp_path / "artifacts").iterdir())
    kept_logs = sorted(p.name for p in (tmp_path / "logs").iterdir())
    # Top 2 by descending name = the two most recent (largest suffixes).
    assert kept_dirs == [f"run-{r_ids[-2]}", f"run-{r_ids[-1]}"]
    assert kept_logs == [f"run-{r_ids[-2]}.log", f"run-{r_ids[-1]}.log"]


def test_prune_no_op_when_under_limit(tmp_path: Path) -> None:
    _seed_run_dirs(tmp_path, n=3)
    rc = RunContext(data_root=tmp_path)

    _prune_run_dirs(rc, max_runs_kept=10)

    assert len(list((tmp_path / "artifacts").iterdir())) == 3
    assert len(list((tmp_path / "logs").iterdir())) == 3


def test_prune_ignores_non_run_entries(tmp_path: Path) -> None:
    _seed_run_dirs(tmp_path, n=3)
    # Drop a stray file that doesn't match the run-* naming.
    (tmp_path / "artifacts" / "README.md").write_text("keep me", encoding="utf-8")
    rc = RunContext(data_root=tmp_path)

    _prune_run_dirs(rc, max_runs_kept=1)

    # README survives; only 1 run dir remains.
    assert (tmp_path / "artifacts" / "README.md").is_file()
    run_dirs = [p for p in (tmp_path / "artifacts").iterdir() if p.name.startswith("run-")]
    assert len(run_dirs) == 1


def test_prune_handles_missing_root(tmp_path: Path) -> None:
    # No data/logs or data/artifacts yet — must not raise.
    rc = RunContext(data_root=tmp_path / "does_not_exist")
    _prune_run_dirs(rc, max_runs_kept=5)  # no exception = pass
