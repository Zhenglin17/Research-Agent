"""Hard keyword filtering — runs before ranking.

Two rules, both case-insensitive substring match across title + summary +
content (content is usually None for RSS; included for robustness when
adapters do grab full text):

  * exclude_keywords: ANY hit → item is dropped
  * include_keywords: if the list is NON-EMPTY, at least ONE must hit;
                     empty list → rule disabled

Both are hard rules. "Soft" keyword signals (focus_keywords) live in
scoring.py — keeping hard-filter and soft-scoring separate means you can
tune either without surprising the other.

Why include is a hard filter (not a soft boost):
    Users leave include empty 99% of the time. When they fill it, the
    intent is "today I only want to see these" — a strict selector, not
    a gentle nudge. Semantically: exclude/include answer "should this
    appear at all?"; focus answers "how strongly should it rank?".
"""

from __future__ import annotations

import logging

from ..models.source_item import SourceItem

logger = logging.getLogger(__name__)


def _haystack(item: SourceItem) -> str:
    """Concatenated lowercase text we search keywords against.

    Includes content even though RSS items rarely have it — when a future
    adapter does grab full text, keywords match automatically.
    """
    parts = [item.title, item.summary or "", item.content or ""]
    return " ".join(parts).lower()


def _any_hit(haystack: str, needles: list[str]) -> bool:
    return any(n.lower() in haystack for n in needles if n)


def apply_filter(
    items: list[SourceItem],
    *,
    include_keywords: list[str],
    exclude_keywords: list[str],
) -> list[SourceItem]:
    """Return items that pass both rules. Order preserved."""
    n_in = len(items)
    kept: list[SourceItem] = []
    dropped_exclude = 0
    dropped_no_include = 0

    for item in items:
        hay = _haystack(item)

        if exclude_keywords and _any_hit(hay, exclude_keywords):
            dropped_exclude += 1
            logger.debug("filter:exclude drop id=%s title=%r", item.id, item.title)
            continue

        if include_keywords and not _any_hit(hay, include_keywords):
            dropped_no_include += 1
            logger.debug("filter:no-include drop id=%s title=%r", item.id, item.title)
            continue

        kept.append(item)

    logger.info(
        "filter done in=%d out=%d dropped_exclude=%d dropped_no_include=%d",
        n_in, len(kept), dropped_exclude, dropped_no_include,
    )
    return kept
