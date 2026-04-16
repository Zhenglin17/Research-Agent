"""Unified content unit that flows through the whole pipeline.

Every source adapter (RSS, PubMed, bioRxiv, ...) normalizes its raw payload
into `SourceItem` before handing it to the pipeline. From that point on,
dedupe / filter / ranking / summarization code only ever sees this type —
no source-specific shapes leak downstream.

V1 keeps this flat on purpose: one model + a `source_type` discriminator +
an `extra` escape hatch for source-specific fields. Splitting into Paper /
BlogPost / News subclasses would be premature; we can do it later without
touching pipeline code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

SourceType = Literal["rss", "pubmed", "biorxiv", "web"]


class SourceItem(BaseModel):
    """A single piece of content, normalized across all sources."""

    model_config = ConfigDict(extra="forbid")

    # Identity ---------------------------------------------------------------
    id: str = Field(default_factory=lambda: uuid4().hex)
    source_id: str  # matches an entry in sources.yaml
    source_type: SourceType

    # Content ----------------------------------------------------------------
    title: str
    summary: str | None = None  # short abstract / description
    content: str | None = None  # full text when available (often None for RSS)
    authors: list[str] = Field(default_factory=list)

    # Links ------------------------------------------------------------------
    url: HttpUrl  # as published by the source
    url_canonical: str  # tracking params stripped; dedupe key (layer 1)

    # Time -------------------------------------------------------------------
    # Both must be timezone-aware. published_at drives the lookback window
    # and the freshness component of the ranking score.
    published_at: datetime
    fetched_at: datetime

    # Dedupe fingerprints ----------------------------------------------------
    # Left as None here; dedupe_stage populates content_hash from
    # sha256(title + content[:N]). Keeping the field on the model means we
    # can serialize deduped artifacts without a second schema.
    content_hash: str | None = None

    # Ranking ----------------------------------------------------------------
    # Populated by ranking stage. None before ranking runs; kept on the model
    # so artifacts and LLM prompts can see the score without a side map.
    score: float | None = None

    # V2 interface -----------------------------------------------------------
    # High-confidence promise that V2's full-text fetcher can retrieve the
    # complete article through a free public API or URL — no auth, no
    # paywall. Not a runtime-verified guarantee: set by each adapter based
    # on source-level knowledge (bioRxiv preprints → True, paywalled RSS →
    # False) or per-item metadata (PubMed → True iff PMCID present).
    # Formatter renders a 📖 marker next to True items so the user knows
    # which entries they can ask follow-up questions about without having
    # to fetch full text themselves.
    full_text_accessible: bool = False

    # Escape hatch -----------------------------------------------------------
    # Source-specific fields (e.g. PubMed PMID, bioRxiv DOI) live here so
    # the main schema stays stable. Nothing in ranking / summarization
    # should depend on `extra` — it's for debugging and future expansion.
    extra: dict[str, Any] = Field(default_factory=dict)
