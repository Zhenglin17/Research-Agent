"""Build Source adapter instances from a parsed SourcesConfig.

Decoupling the 'how do I construct a Source from yaml' logic from the
pipeline keeps the pipeline loop tiny and makes it easy to add new source
types: write an adapter subclass, add one branch here, done.
"""

from __future__ import annotations

import logging

from ..config.sources_schema import (
    BiorxivSourceEntry,
    PubmedSourceEntry,
    RssSourceEntry,
    SourceEntry,
    SourcesConfig,
)
from .base import Source
from .biorxiv_source import BioRxivSource
from .pubmed_source import PubMedSource
from .rss_source import RSSSource

logger = logging.getLogger(__name__)


def build_sources(config: SourcesConfig) -> list[Source]:
    """Instantiate one Source per enabled, supported entry.

    Disabled entries are skipped silently. Unsupported types are skipped
    with a warning — this matters right now because `sources.yaml` may list
    pubmed entries whose adapter we haven't written yet (see roadmap M5).
    """
    built: list[Source] = []
    for entry in config.sources:
        if not entry.enabled:
            logger.debug("source skipped (disabled) id=%s", entry.id)
            continue

        source = _build_one(entry)
        if source is None:
            logger.warning(
                "source skipped (unsupported type) id=%s type=%s",
                entry.id,
                entry.type,
            )
            continue
        built.append(source)

    logger.info("sources built count=%d", len(built))
    return built


def _build_one(entry: SourceEntry) -> Source | None:
    """Dispatch one entry to its adapter class. Returns None if not yet supported."""
    if isinstance(entry, RssSourceEntry):
        return RSSSource(
            source_id=entry.id,
            name=entry.name,
            feed_url=entry.feed_url,
            weight=entry.weight,
        )
    if isinstance(entry, PubmedSourceEntry):
        return PubMedSource(
            source_id=entry.id,
            name=entry.name,
            query=entry.query,
            max_results=entry.max_results,
            api_key_env=entry.api_key_env,
            include_abstract=entry.include_abstract,
            weight=entry.weight,
        )
    if isinstance(entry, BiorxivSourceEntry):
        return BioRxivSource(
            source_id=entry.id,
            name=entry.name,
            server=entry.server,
            categories=entry.categories,
            keywords=entry.keywords,
            max_results=entry.max_results,
            weight=entry.weight,
            timeout_seconds=entry.timeout_seconds,
            lookback_override_days=entry.lookback_override_days,
            max_pages=entry.max_pages,
        )
    # Future source types land here until their adapter exists.
    return None
