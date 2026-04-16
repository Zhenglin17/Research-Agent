"""Per-stage artifact writer.

Each pipeline stage calls `write_stage_artifact(rc, stage_name, items)`
to drop two files into `data/artifacts/run-<id>/`:

  * <stage>.json — full structured data (machine-readable, replayable)
  * <stage>.md   — human-readable summary (open it in an editor, skim)

Having both means:
  * debugging a bad run = open the MD to spot the issue, JSON to inspect
  * rerunning a later stage on saved input is feasible without re-fetching
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..models.source_item import SourceItem
from .run_context import RunContext

logger = logging.getLogger(__name__)

_MD_PREVIEW_CHARS = 240  # summary text preview per item in the MD view


def write_stage_artifact(
    rc: RunContext,
    stage_name: str,
    items: list[SourceItem],
) -> tuple[Path, Path] | None:
    """Write <stage>.json and <stage>.md for a list of SourceItems.

    Args:
        rc: Run context (provides the artifact directory).
        stage_name: e.g. "fetched", "normalized", "deduped", "ranked".
            Becomes the file name stem.
        items: Items produced by this stage.

    Returns:
        (json_path, md_path) for logging or tests. When `rc.write_artifacts`
        is False this function is a no-op and returns (None, None).
    """
    if not rc.write_artifacts:
        return None
    rc.ensure_dirs()
    json_path = rc.artifact_dir / f"{stage_name}.json"
    md_path = rc.artifact_dir / f"{stage_name}.md"

    # Pydantic's model_dump(mode="json") turns datetimes into ISO strings,
    # HttpUrl into str, etc. — safe for json.dump out of the box.
    payload = [item.model_dump(mode="json") for item in items]
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md_path.write_text(_render_md(stage_name, items), encoding="utf-8")

    logger.info(
        "artifact written stage=%s count=%d json=%s md=%s",
        stage_name,
        len(items),
        json_path,
        md_path,
    )
    return json_path, md_path


def _render_md(stage_name: str, items: list[SourceItem]) -> str:
    """Build a human-readable markdown summary."""
    lines = [
        f"# Stage: {stage_name}",
        "",
        f"**Total items:** {len(items)}",
        "",
    ]

    # Per-source counts — a 'results summary' per §24.3 requirement.
    counts: dict[str, int] = {}
    for it in items:
        counts[it.source_id] = counts.get(it.source_id, 0) + 1
    if counts:
        lines.append("## By source")
        lines.append("")
        for sid, n in sorted(counts.items()):
            lines.append(f"- `{sid}` — {n}")
        lines.append("")

    lines.append("## Items")
    lines.append("")
    for i, it in enumerate(items, start=1):
        preview = (it.summary or it.content or "").strip().replace("\n", " ")
        if len(preview) > _MD_PREVIEW_CHARS:
            preview = preview[:_MD_PREVIEW_CHARS].rstrip() + "…"
        lines.append(f"### {i}. {it.title}")
        lines.append("")
        lines.append(f"- **Source:** `{it.source_id}` ({it.source_type})")
        lines.append(f"- **Published:** {it.published_at.isoformat()}")
        lines.append(f"- **URL:** {it.url}")
        if preview:
            lines.append("")
            lines.append(f"> {preview}")
        lines.append("")

    return "\n".join(lines)
