"""Dedupe stage: collapse duplicates within this run + skip already-pushed.

Runs AFTER fetch_all, BEFORE filter/rank. Pure function except for the
optional `HistoryStore` lookup for cross-run dedupe.

Three layers within a single run (see design notes on dedupe), tried in order —
first match wins:

  1. canonical URL equality
  2. content_hash equality  (sha256 of normalized title + content[:N])
  3. title Jaccard similarity over a threshold

Then a fourth check against `HistoryStore`: drop anything we've already
pushed in a prior run.

Every item that survives gets its `content_hash` field populated so later
stages (and eventually `record_push`) can use it without recomputing.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from ..config.schema import DedupeConfig
from ..models.source_item import SourceItem
from ..storage.history_store import HistoryStore

logger = logging.getLogger(__name__)

# Very small stopword set — we don't want this to become a linguistic
# project. Layer-3 only needs to collapse obvious "the foo of bar" noise.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "in", "on", "for", "to", "and", "or",
        "is", "are", "was", "were", "be", "by", "with", "at", "from",
        "as", "that", "this", "these", "those", "it", "its",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Helpers (pure, unit-testable)
# ---------------------------------------------------------------------------


def compute_content_hash(item: SourceItem, prefix_chars: int) -> str:
    """sha256 of normalized title + first N chars of content/summary.

    We prefer `content` (full body if the adapter grabbed it), fall back to
    `summary`, then empty string. Title is always included so two items
    with no body but identical titles still collide on hash (layer 2),
    while two items with same body but wildly different titles do not.
    """
    body = (item.content or item.summary or "")[:prefix_chars]
    payload = f"{_normalize_title(item.title)}\n{body}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_title(title: str) -> str:
    """Lowercase, drop punctuation, drop stopwords, collapse whitespace."""
    tokens = _WORD_RE.findall(title.lower())
    kept = [t for t in tokens if t not in _STOPWORDS]
    return " ".join(kept)


def _title_tokens(title: str) -> frozenset[str]:
    tokens = _WORD_RE.findall(title.lower())
    return frozenset(t for t in tokens if t not in _STOPWORDS)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity: |A∩B| / |A∪B|. Empty sets → 0."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Dedupe passes
# ---------------------------------------------------------------------------


@dataclass
class _DedupeStats:
    by_url: int = 0
    by_hash: int = 0
    by_title_sim: int = 0
    by_history: int = 0

    def total(self) -> int:
        return self.by_url + self.by_hash + self.by_title_sim + self.by_history


def dedupe_within_run(
    items: list[SourceItem], cfg: DedupeConfig
) -> tuple[list[SourceItem], _DedupeStats]:
    """Collapse duplicates inside a single run using all three layers.

    Order matters: URL is O(1) exact match; hash is O(1) exact match;
    title similarity is O(n) per item. We short-circuit on the cheap
    checks first. All survivors have `content_hash` populated.
    """
    stats = _DedupeStats()
    kept: list[SourceItem] = []
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    kept_token_sets: list[frozenset[str]] = []

    for item in items:
        # Layer 1: URL
        if item.url_canonical in seen_urls:
            stats.by_url += 1
            logger.debug("dedupe:url drop id=%s url=%s", item.id, item.url_canonical)
            continue

        # Compute hash once, stash it on the item.
        item.content_hash = compute_content_hash(item, cfg.content_hash_prefix_chars)

        # Layer 2: content hash
        if item.content_hash in seen_hashes:
            stats.by_hash += 1
            logger.debug("dedupe:hash drop id=%s title=%r", item.id, item.title)
            continue

        # Layer 3: title Jaccard
        tokens = _title_tokens(item.title)
        is_dup = False
        for prev_tokens in kept_token_sets:
            if jaccard(tokens, prev_tokens) >= cfg.title_similarity_threshold:
                stats.by_title_sim += 1
                logger.debug("dedupe:title drop id=%s title=%r", item.id, item.title)
                is_dup = True
                break
        if is_dup:
            continue

        kept.append(item)
        seen_urls.add(item.url_canonical)
        seen_hashes.add(item.content_hash)
        kept_token_sets.append(tokens)

    return kept, stats


def filter_already_pushed(
    items: list[SourceItem],
    store: HistoryStore,
    *,
    user_id: str,
) -> tuple[list[SourceItem], int]:
    """Drop items the given user has already seen in a prior run."""
    kept: list[SourceItem] = []
    dropped = 0
    for item in items:
        if store.has_been_pushed(
            url_canonical=item.url_canonical,
            content_hash=item.content_hash,
            user_id=user_id,
        ):
            dropped += 1
            logger.debug(
                "dedupe:history drop id=%s user=%s title=%r",
                item.id, user_id, item.title,
            )
            continue
        kept.append(item)
    return kept, dropped


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def dedupe(
    items: list[SourceItem],
    cfg: DedupeConfig,
    *,
    history: HistoryStore | None = None,
    user_id: str | None = None,
) -> list[SourceItem]:
    """Full dedupe pass. Pass `history` + `user_id` to also skip already-pushed.

    Returns the surviving items. Mutates each item's `content_hash`.
    """
    n_in = len(items)
    kept, run_stats = dedupe_within_run(items, cfg)

    history_drops = 0
    if history is not None:
        if user_id is None:
            raise ValueError("dedupe: user_id required when history is provided")
        kept, history_drops = filter_already_pushed(kept, history, user_id=user_id)
        run_stats.by_history = history_drops

    logger.info(
        "dedupe done in=%d out=%d dropped=%d "
        "(url=%d hash=%d title_sim=%d history=%d)",
        n_in, len(kept), run_stats.total(),
        run_stats.by_url, run_stats.by_hash,
        run_stats.by_title_sim, run_stats.by_history,
    )
    return kept
