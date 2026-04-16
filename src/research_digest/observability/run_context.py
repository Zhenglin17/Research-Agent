"""Per-run identity and artifact layout.

One `RunContext` is created at the top of each pipeline run (in the CLI
entrypoint) and passed down to every stage that needs to write logs or
artifacts. Centralizing this means:

  * every stage agrees on the same run id and timestamp
  * artifact paths are decided in one place, not reassembled per stage
  * tests can construct a RunContext pointing at a tmp directory

No business logic lives here — this is a dumb container plus a couple of
path helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _default_run_id() -> str:
    # Human-scannable prefix + short uuid suffix. Prefix sorts chronologically
    # in `ls`, suffix disambiguates runs started in the same second.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid4().hex[:6]}"


@dataclass
class RunContext:
    """Identity and filesystem layout for a single pipeline run.

    Not frozen: `write_artifacts` is populated by the pipeline AFTER the
    config file is loaded, which happens after the CLI builds the rc.
    We need to be able to update it in place. (Nothing else mutates.)
    """

    run_id: str = field(default_factory=_default_run_id)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data_root: Path = Path("data")

    # Flipped off in production cron mode to skip JSON/MD artifacts. Each
    # artifact writer checks this flag on its own — see §"observability"
    # in config/settings.yaml and the guards inside
    # artifact_store.write_stage_artifact / digest_artifact.write_digest_artifact
    # / pipeline._write_delivery_artifact.
    write_artifacts: bool = True

    # ------------------------------------------------------------------
    # Path helpers. Kept as properties so the layout is defined in exactly
    # one place; stages just ask the context where to write.
    # ------------------------------------------------------------------

    @property
    def log_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / f"run-{self.run_id}.log"

    @property
    def artifact_dir(self) -> Path:
        return self.data_root / "artifacts" / f"run-{self.run_id}"

    def ensure_dirs(self) -> None:
        """Create log and artifact directories if they don't exist.

        Called once by the CLI entrypoint right after the context is built.
        Safe to call multiple times. When `write_artifacts` is False we
        skip creating the artifact dir — no point leaving empty folders.
        """
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.write_artifacts:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
