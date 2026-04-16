"""Assemble chat messages for the LLM from prompt templates + item data.

Two kinds of messages are built here:
  1. Per-item summary — system prompt from `prompts/summarize-*.md`,
     user message carrying the item's title / abstract / venue / date.
  2. Digest intro      — system prompt from `prompts/digest-intro.md`,
     user message listing every selected item's section + title + hint.

Design notes:
  - No URL goes into the user message. The LLM writes prose only;
    the formatter fills in `<a href>` tags from SourceItem.url at
    render time. This structurally prevents URL fabrication.
  - Prompt files live in `prompts/` at the repo root — they are
    deliberately outside `src/` so non-coders can tweak tone without
    touching Python. They're loaded once at construction time and
    cached in memory for the process lifetime.
  - Section names (PAPERS / BLOGS / PODCASTS / SOCIAL) are defined
    here, keyed by `SourceItem.source_type`. Adding a new source
    type only requires extending the two maps below.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models.source_item import SourceItem, SourceType

# --- Static mappings ---------------------------------------------------------

# Which prompt file summarizes each source_type.
# rss / pubmed / biorxiv are all "papers" in V1 — the paper prompt is
# tuned for a journal-abstract style. Blogs and web sources get the
# blog prompt. Add mappings here when new source_types are introduced.
_PROMPT_BY_SOURCE_TYPE: dict[SourceType, str] = {
    "rss": "summarize-paper.md",
    "pubmed": "summarize-paper.md",
    "biorxiv": "summarize-paper.md",
    "web": "summarize-blogs.md",
}

# How each source_type surfaces in the rendered digest. Must match the
# section strings the formatter groups on.
_SECTION_BY_SOURCE_TYPE: dict[SourceType, str] = {
    "rss": "PAPERS",
    "pubmed": "PAPERS",
    "biorxiv": "PAPERS",
    "web": "BLOGS",
}

_INTRO_PROMPT_FILE = "digest-intro.md"


def section_for(item: SourceItem) -> str:
    """Return the digest section label for one item (PAPERS/BLOGS/...)."""
    return _SECTION_BY_SOURCE_TYPE[item.source_type]


# --- Prompt library ----------------------------------------------------------


@dataclass
class PromptLibrary:
    """In-memory cache of all prompt templates.

    Built once per run via `PromptLibrary.load(prompts_dir)`. Holds the
    raw markdown text of each prompt file so we don't hit disk on every
    LLM call. Cheap — the whole `prompts/` dir is a few KB.
    """

    prompts_dir: Path
    by_filename: dict[str, str]

    @classmethod
    def load(cls, prompts_dir: Path) -> PromptLibrary:
        """Read every `.md` file under `prompts_dir` into memory.

        Args:
            prompts_dir: Directory containing the prompt templates.
                Typically `<repo>/prompts/`.

        Returns:
            A PromptLibrary with every `.md` file content keyed by
            its filename (e.g. `"summarize-paper.md"`).

        Called by:
            `summarizer` once at the start of a run.
        """
        by_filename: dict[str, str] = {}
        for path in sorted(prompts_dir.glob("*.md")):
            by_filename[path.name] = path.read_text(encoding="utf-8")
        return cls(prompts_dir=prompts_dir, by_filename=by_filename)

    def get(self, filename: str) -> str:
        try:
            return self.by_filename[filename]
        except KeyError as e:
            raise FileNotFoundError(
                f"Prompt template '{filename}' not found under {self.prompts_dir}. "
                f"Available: {sorted(self.by_filename)}"
            ) from e


# --- Message builders --------------------------------------------------------


def _render_item_user_message(item: SourceItem, topic: str) -> str:
    """Build the user-role payload describing one item to the LLM.

    The LLM sees: topic, section, title, authors, venue/source id,
    published date, and whatever abstract/content we have. No URL is
    sent — the formatter renders links from SourceItem.url afterwards.
    """
    parts: list[str] = []
    parts.append(f"Topic of the digest: {topic}")
    parts.append(f"Section: {section_for(item)}")
    parts.append(f"Source: {item.source_id} ({item.source_type})")
    parts.append(f"Title: {item.title}")
    if item.authors:
        # Cap to keep the prompt tight; full author lists are rarely useful.
        shown = ", ".join(item.authors[:6])
        if len(item.authors) > 6:
            shown += f", et al. ({len(item.authors)} total)"
        parts.append(f"Authors: {shown}")
    parts.append(f"Published: {item.published_at.date().isoformat()}")

    body = item.content or item.summary
    if body:
        parts.append("")
        parts.append("Abstract / content:")
        parts.append(body.strip())
    else:
        parts.append("")
        parts.append("(No abstract or content available — summarize from title only, "
                     "and say so plainly rather than inventing detail.)")
    return "\n".join(parts)


def build_item_messages(
    item: SourceItem,
    *,
    topic: str,
    library: PromptLibrary,
) -> list[dict[str, str]]:
    """Build the chat messages for summarizing one SourceItem.

    Args:
        item: The item to summarize.
        topic: The configured digest topic; given to the LLM so its
            framing matches the reader's interest.
        library: Preloaded prompt templates.

    Returns:
        OpenAI chat messages (system + user) ready for `llm_client.complete`.

    Called by:
        `summarizer`, once per item, concurrently via `asyncio.gather`.
    """
    prompt_file = _PROMPT_BY_SOURCE_TYPE[item.source_type]
    system = library.get(prompt_file)
    user = _render_item_user_message(item, topic=topic)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_intro_messages(
    *,
    topic: str,
    digest_date_iso: str,
    entries_preview: list[tuple[str, str, str]],
    library: PromptLibrary,
) -> list[dict[str, str]]:
    """Build the chat messages for the one-paragraph digest intro.

    Args:
        topic: Configured digest topic.
        digest_date_iso: ISO date string used in the intro prompt.
        entries_preview: One triple per selected item:
            (section, title, hint). `hint` is a short excerpt from the
            item's LLM-written summary (typically the first sentence)
            — enough for the intro LLM to spot themes without re-reading
            every full summary.
        library: Preloaded prompt templates.

    Returns:
        OpenAI chat messages (system + user).

    Called by:
        `summarizer`, exactly once per run, AFTER per-item summaries
        complete (so the intro can reference actual themes, not just
        titles).
    """
    system = library.get(_INTRO_PROMPT_FILE)
    lines: list[str] = [
        f"Topic: {topic}",
        f"Date: {digest_date_iso}",
        "",
        "Items in today's digest:",
    ]
    for section, title, hint in entries_preview:
        lines.append(f"- [{section}] {title}")
        if hint:
            lines.append(f"    hint: {hint}")
    user = "\n".join(lines)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
