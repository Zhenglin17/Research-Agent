"""Shape of a finished digest — what the delivery layer receives.

A digest is what gets sent to Telegram. It's assembled by the
summarization + delivery layers from three pieces:

  1. the set of ranked SourceItems the pipeline selected
  2. the LLM-written per-item summary text (one paragraph each)
  3. the LLM-written intro paragraph

`DigestEntry` pairs each SourceItem with its LLM summary. `Digest` holds
the ordered list of entries plus metadata the formatter needs (intro,
topic, date, model used for the footer).

Intentionally thin — no formatting decisions live here. Formatter turns
a Digest into Telegram HTML; this module just carries data.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from .source_item import SourceItem


class DigestEntry(BaseModel):
    """One item in the final digest.

    `section` groups items by content type in the formatted output
    (PAPERS / BLOGS / PODCASTS / SOCIAL). It's derived from the
    SourceItem's source_type at build time — not a user-editable field.
    """

    model_config = ConfigDict(extra="forbid")

    item: SourceItem
    summary: str          # LLM-written paragraph about this item
    section: str          # "PAPERS" | "BLOGS" | "PODCASTS" | "SOCIAL"


class Digest(BaseModel):
    """The fully-summarized, orderable digest ready for formatting."""

    model_config = ConfigDict(extra="forbid")

    topic: str                              # echoed in the header
    digest_date: date                       # date used in the header
    intro: str                              # LLM-written intro paragraph
    entries: list[DigestEntry] = Field(default_factory=list)
    model_used: str                         # printed in the footer
