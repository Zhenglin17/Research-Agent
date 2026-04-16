"""Schema for config/sources.yaml.

Each source type gets its own pydantic model so required fields are
validated at load time (an `rss` entry without `feed_url` fails loudly).
The top-level list uses a discriminated union on the `type` field —
pydantic automatically picks the right sub-model for each yaml entry.

Adding a new source type = add a new SubSourceEntry class + include it
in the `SourceEntry` union. The factory then needs one more branch.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _BaseSourceEntry(BaseModel):
    """Fields every source has, regardless of type."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    enabled: bool = True
    weight: float = Field(default=1.0, ge=0.0)


class RssSourceEntry(_BaseSourceEntry):
    type: Literal["rss"]
    feed_url: str


class PubmedSourceEntry(_BaseSourceEntry):
    type: Literal["pubmed"]
    query: str
    max_results: int = Field(default=50, ge=1, le=500)
    # Name of an env var holding the API key (not the key itself). Keeps
    # secrets out of committed yaml. None = run without a key (lower rate).
    api_key_env: str | None = None
    include_abstract: bool = True


class BiorxivSourceEntry(_BaseSourceEntry):
    """bioRxiv / medRxiv preprint-server config.

    Unlike PubMed, the bioRxiv API has no server-side search: the
    `/details/` endpoint just lists every paper in a date range. So
    filtering is always client-side via `categories` (paper's declared
    subject area, exact match) and `keywords` (must appear in
    title/abstract, case-insensitive). Empty list = no filter at that
    level. The two filters AND together.
    """

    type: Literal["biorxiv"]
    # Same API works for both servers; this switches the URL path.
    server: Literal["biorxiv", "medrxiv"] = "biorxiv"
    # Category whitelist. Exact-match against the `category` field on
    # each paper (e.g. "cancer biology", "immunology"). Empty = accept any.
    categories: list[str] = Field(default_factory=list)
    # Keywords required (ANY match) in title+abstract. Empty = accept any.
    keywords: list[str] = Field(default_factory=list)
    max_results: int = Field(default=50, ge=1, le=500)
    # Per-source lookback override. bioRxiv has no server-side search, so
    # every extra day = ~180 more papers we download and discard locally.
    # We typically run daily via cron; a tight 2-day window is enough to
    # cover one missed run without pulling the global 3-day firehose.
    # None = fall back to the pipeline's global lookback_days.
    lookback_override_days: int | None = Field(default=None, ge=1, le=30)
    # Hard cap on pages fetched (100 items/page). Prevents runaway scans
    # on low-keyword-match days. 5 pages = up to 500 items scanned.
    max_pages: int = Field(default=5, ge=1, le=50)
    # bioRxiv's API is slow — a single page routinely takes 20–30s.
    # 60s gives a comfortable margin without forcing premature failure.
    timeout_seconds: float = Field(default=60.0, gt=0)


# Discriminated union: pydantic inspects `type` and picks the right model.
SourceEntry = Annotated[
    Union[RssSourceEntry, PubmedSourceEntry, BiorxivSourceEntry],
    Field(discriminator="type"),
]


class SourcesConfig(BaseModel):
    """Top-level structure of sources.yaml."""

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceEntry] = Field(default_factory=list)
