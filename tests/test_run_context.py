"""Tests for observability/run_context.py."""

from __future__ import annotations

import re
from pathlib import Path

from research_digest.observability.run_context import RunContext


def test_run_id_format() -> None:
    rc = RunContext()
    # 20260414T180351Z-997a6e
    assert re.match(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$", rc.run_id), rc.run_id


def test_run_ids_are_unique() -> None:
    ids = {RunContext().run_id for _ in range(20)}
    assert len(ids) == 20


def test_path_layout_uses_run_id(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    assert rc.log_file == tmp_path / "logs" / f"run-{rc.run_id}.log"
    assert rc.artifact_dir == tmp_path / "artifacts" / f"run-{rc.run_id}"


def test_ensure_dirs_creates_both(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    rc.ensure_dirs()
    assert rc.log_dir.is_dir()
    assert rc.artifact_dir.is_dir()


def test_ensure_dirs_idempotent(tmp_path: Path) -> None:
    rc = RunContext(data_root=tmp_path)
    rc.ensure_dirs()
    rc.ensure_dirs()  # should not raise
    assert rc.artifact_dir.is_dir()


def test_started_at_is_timezone_aware() -> None:
    rc = RunContext()
    assert rc.started_at.tzinfo is not None
