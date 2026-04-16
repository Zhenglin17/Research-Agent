"""Source adapter contract.

Every concrete source (RSS, PubMed, bioRxiv, ...) implements `Source`.
The pipeline only knows about this abstract type, which means:

  * adding a new source type = adding one subclass, no pipeline edits
  * swapping sources in/out is a yaml edit
  * tests can fake a source by subclassing `Source` and returning canned items

Adapters own the responsibility of normalizing their raw payload into
`SourceItem`s — pipeline downstream never sees source-specific shapes.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime

from ..models.source_item import SourceItem


@dataclass(frozen=True)
class FetchWindow:
    """Closed-open time window [start, end) in UTC.

    The pipeline computes this once from `config.limits.lookback_days` and
    passes the same window to every source, so all adapters filter against
    the same cutoff.
    """

    start: datetime  # inclusive
    end: datetime    # exclusive


class Source(abc.ABC):
    """Abstract base class for all source adapters."""

    #: Stable identifier, must match an entry in sources.yaml. Used as
    #: SourceItem.source_id for ranking and debugging.
    source_id: str

    #: Display name for logs and digest output.
    name: str

    #: Per-source ranking weight (read from sources.yaml). Higher = more
    #: trusted. Pipeline reads this when computing the score.
    weight: float

    @abc.abstractmethod
    async def fetch(self, window: FetchWindow) -> list[SourceItem]:
        """Fetch items published within `window` and return them normalized.

        Contract for implementations:
          * Must return only items whose `published_at` is in [start, end).
          * Must set `source_id` and `source_type` on every returned item.
          * Must not raise on empty results — return [] instead.
          * May raise on network / parse errors; the pipeline catches per
            source so one dead feed does not kill the run.
        """
        raise NotImplementedError
