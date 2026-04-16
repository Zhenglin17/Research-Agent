# Research Digest Bot

A topic-agnostic pipeline that fetches recent content from configured sources,
deduplicates, filters, ranks, asks an LLM to write a digest, and pushes it to
Telegram on a cron schedule. Written for the researcher who wants a short daily
brief instead of five tabs of journal RSS feeds.

## Status

**Version 1: complete.** 195 tests passing. Running in production via cron.

Topic is currently configured for cancer immunotherapy/immunology (the initial
user that I write for is a biomedical researcher), but nothing in `src/` assumes 
biology — change `config/*.yaml` and the whole pipeline happily repurposes for any field.

---

## What it does

```
 ┌─ config ─────────────────────────────────────────────────────────────┐
 │ settings.yaml   topic • keywords • ranking weights • length policy   │
 │ sources.yaml    which feeds/APIs to pull + per-source knobs          │
 │ .env            OPENROUTER_API_KEY • TELEGRAM_BOT_TOKEN              │
 └──────────────────────────────────────────────────────────────────────┘
              │
              ▼
   fetch ── dedupe ── filter ── rank ── summarize ── translate ── deliver
  (async)   (SQLite)   (yaml)   (code)    (LLM)       (LLM)      (Telegram)
     │        │         │        │         │           │             │
     └────────┴─────────┴────────┴─────────┴───────────┴─────────────┘
                     artifacts/run-<id>/*.{json,md} at every stage
```

Each stage writes a JSON artifact (for machines) and a `.md` companion (for
you) to `data/artifacts/run-<id>/`, so you can open any stage and see exactly
what went in and came out — critical when tuning ranking or debugging a
weird-looking digest.

---

## Quick start

```bash
# 1. Install uv (https://docs.astral.sh/uv/) if you don't have it
uv sync                                  # create .venv, install deps
cp .env.example .env && $EDITOR .env     # fill in real secrets

# 2. Dry run — fetches + ranks, no LLM, no push. Safe to re-run often.
uv run digest dry-run

# 3. Debug run — adds LLM summary + translate, writes artifacts, no push.
uv run digest debug-run

# 4. Full run — the real thing. Pushes to every chat_id in settings.yaml.
uv run digest run
```

Three CLI modes with progressively more side effects. `dry-run` is cheap and
free; `debug-run` costs a few LLM tokens; `run` actually sends messages. Pick
the mode that matches how much you trust the current config.

---

## Pipeline stages

Everything is code-driven except summarize + translate. The LLM never touches
ranking, filtering, or dedupe — those are cheap, fast, and you can actually
debug them.

| Stage | Where | What it does |
|---|---|---|
| fetch | `sources/*.py` | Concurrent async pulls from RSS, PubMed E-utilities, bioRxiv |
| dedupe | `pipeline/dedupe_stage.py` | URL canonical → content hash → title similarity, plus SQLite cross-run history |
| filter | `ranking/filter_rules.py` | include/exclude keyword gates from `settings.yaml` |
| rank | `ranking/ranker.py` + `scoring.py` | Weighted sum of topic match + source weight + focus keywords + freshness |
| summarize | `summarization/summarizer.py` | Parallel per-entry LLM calls, one intro call, JSON-structured |
| translate | `summarization/translator.py` | Single LLM call, JSON in/out, skipped if `output.language: "en"` |
| deliver | `delivery/deliver.py` + `formatter.py` | Render HTML, split at chunk boundaries, fan-out to every `chat_id` |
| persist | `storage/history_store.py` | Record successful sends + prune rows older than `retention.history_days` |

---

## Design choices worth noting

These are the decisions that took the longest to get right, or that would
otherwise look arbitrary.

### Topic-agnostic by construction

Not a single biomedical term appears in `src/`. All domain knowledge lives in
`config/*.yaml`. For repurposing for AI research just change `topic:` to
`"LLM inference and AI agents"`, swap the sources for arXiv + a couple
blogs, rewrite `focus_keywords`. Zero code changes.

### Two-tier filtering: pre-fetch vs post-fetch

**Pre-fetch** (per-source, in `sources.yaml`):
- `query` on PubMed → sent to E-utilities, decides what to download
- `categories` + `keywords` on bioRxiv → narrows the client-side download filter

**Post-fetch** (global, in `settings.yaml`):
- `topic` + `focus_keywords` → drives ranking

This separation matters. Making `focus_keywords` more specific narrows what
*scores high*; narrowing a PubMed `query` narrows what *enters the pipeline
in the first place*. Different knobs for different goals.

### Ranking formula: multiplicative source weight, additive keyword bonuses

```
score = w_topic   × topic_match_ratio        # [0, 1], token intersection
      + w_source  × source_weight            # (global) × (per-source)
      + w_focus   × focus_hit_count          # integer count, case-insensitive substring
      + w_fresh   × freshness                # 1.0 now → 0.0 lookback_days ago
      + w_llm_rel × 0.0                      # reserved for V2; always 0
```

`w_*` come from `settings.yaml:ranking`, per-source `source_weight` from each
entry in `sources.yaml`. So Nature Immunology (weight=1.2) × global
`source_weight=0.5` contributes 0.6 per item regardless of content, on top of
the content-driven terms.

### Three-layer dedupe

1. **URL canonical match**: strips `utm_*`, `gclid`, fragments. Catches
   simple cross-post cases.
2. **`sha256(title + body[:500])` content hash**: same paper indexed
   differently by two feeds.
3. **Title similarity**: word-bag overlap above threshold. Catches
   title variants ("A new...", "Discovery of a new...").

Plus cross-run dedupe via a SQLite `digest_history` table — after a push
succeeds, every entry gets recorded; subsequent runs check here so yesterday's
top paper doesn't show up again today. Also the old history is auto removed.

### `full_text_accessible` flag — a V1 contract for V2's benefit

Every `SourceItem` carries a `full_text_accessible: bool`. V1 doesn't use it
itself. V2's follow-up-question layer will need to know which papers it can
actually fetch the full text of. Rules per adapter:

- **RSS** (Nature, Cell, Science, …): `False`. Paywalled.
- **PubMed**: `True` if the efetch XML carries a `<ArticleId IdType="pmc">`
  element (deposited in PubMed Central, openly readable).
- **bioRxiv / medRxiv**: always `True`. Preprint server, everything open.

A 📖 marker shows in the Telegram output only when the flag is true, so you
can see at a glance which items V2 will be able to deep-read later.

### Model rotation without state

`llm.test_models` in `settings.yaml` is a list. If populated, the model for
each run is picked deterministically by `date.toordinal() % len(models)`. No
counter, no database, no "which one did I use yesterday" bookkeeping — the
date *is* the state. Delete the list or comment it out to fall back to a
single default model. Every digest footer shows which model actually wrote
it, so A/B-comparing across days is trivial.

### Short-prefix focus keywords

Matching is **case-insensitive substring**, not token-level, not
hyphen-normalized. `"B7-H3"` won't match an abstract that writes `"B7H3"`
without a hyphen. Strategy: use the **shortest unambiguous prefix** as the
keyword. `"B7"` covers B7-H3, B7H3, B7-H4, B7H4, B7 family, B7x, B7-1…
`"palmitoyl"` covers palmitoylation, palmitoylated, palmitoyltransferase,
depalmitoylation. Saves listing every spelling variant, at minor risk of
false positives (low in a focused domain).

### Timezone: local for display, UTC for logic

`timezone: America/New_York` (configurable) drives:
- Digest date displayed in the Telegram header
- Which slot the model rotation lands on

Everything else — `published_at`, `fetched_at`, artifact folder names,
lookback windows — uses UTC. UTC is the safe machine coordinate; local time
is for the human reading the message on their phone.

### Length policy: render + split

A previous version of this pipeline had a four-level length degradation
(drop entries → LLM compress → split). It got cut. The current policy is:

- Render the full digest
- If it fits under `telegram_hard_limit` (4096), send as one message
- Otherwise, split at entry boundaries (`\n\n` preferred) into as many
  messages as needed

LLM-driven compression gave unstable outputs (sometimes JSON-invalid, always
lossy) and entry-dropping hid the point of ranking. "Just split" is boring
and correct.

### Graceful LLM failure

- Per-entry summary fails → minimal fallback summary, other entries unaffected
- Intro call fails → bland-but-honest "N items today" string
- Translate fails entirely → keep English digest, flag in logs

The digest always goes out. An LLM hiccup degrades quality, it doesn't cancel
the push.

### Observability as a first-class module

Every stage emits (a) INFO logs with counts and timings and (b) a
`<stage>.json` + `<stage>.md` artifact. The Markdown companions exist so you
can *actually read* what the pipeline saw at each step — the JSON is for
programmatic use. This is the difference between "my digest seems weird" and
"my digest has this one weird entry, here's exactly where in the pipeline it
came from."

Per-run isolation: one run = one `run-<timestamp>-<short_hash>/` folder
under `data/artifacts/`, one `run-<id>.log` under `data/logs/`. Retention
prunes both beyond `max_runs_kept=20`.

### Artifact/Telegram layout isomorphism

`digest.md` (the artifact) and the Telegram message use the same rendering
code path (`delivery/formatter.py`). If one shows a 📖 marker, the other
does. If one has a section header, so does the other. No "I see it on my
phone but not in the log" surprises.

---

## Configuration

Two YAML files. Business config only no private information.

### `config/settings.yaml`

- `topic`, `timezone`
- `focus_keywords` / `exclude_keywords` / `include_keywords`
- `limits`: `lookback_days`, `max_digest_items`, …
- `ranking`: weights for each scoring term
- `dedupe`: content hash prefix length, title similarity threshold
- `llm`: model id, optional `test_models` rotation list, temperature, token caps
- `telegram`: `chat_ids`, parse mode, timeout
- `output`: language (`"en"` skips translate entirely), length caps
- `retention`: history_days
- `observability`: `write_artifacts`, `max_runs_kept`

### `config/sources.yaml`

One entry per source. Common fields: `id`, `name`, `type`, `enabled`,
`weight`. Per-type fields:

- **rss**: `feed_url`
- **pubmed**: `query` (E-utilities syntax), `max_results`,
  `api_key_env`, `include_abstract`
- **biorxiv**: `server`, `categories`, `keywords`, `max_results`,
  `lookback_override_days`, `max_pages`

Adding a new source type means writing one adapter under `sources/` that
conforms to the interface in `sources/base.py` and a pydantic model for its
config. The rest of the pipeline sees a uniform `SourceItem`.

### `.env`

Only secrets. `OPENROUTER_API_KEY` is required; `TELEGRAM_BOT_TOKEN` is
required for `run` mode; `PUBMED_API_KEY` is optional (PubMed runs
unauthenticated if unset, at lower rate limits).

---

## Prompts

Plain Markdown files in `prompts/`. No code change needed to edit them.

- `summarize-paper.md` — per-entry paper summary
- `digest-intro.md` — the opening paragraph
- `translate.md` — English → Chinese translation
- `summarize-blogs.md`, `summarize-podcast.md`, `summarize-tweets.md` —
  future types
- `compress-digest.md` — unused in V1, left as scaffolding

---

## Cron deployment

```bash
# Edit the file with your project path, then:
crontab -l | cat scripts/crontab.example - | crontab -

# Or manually:
crontab -e
# paste: 0 8 * * * /your/path/scripts/run-digest.sh
```

`scripts/run-digest.sh` is a wrapper that cds to the project root, loads
`.env`, puts `uv` on PATH, and runs `digest run`. Cron's default environment
is so minimal that calling `uv run digest run` directly from crontab won't
find uv — use the wrapper.

For a user in a non-system timezone (e.g., you're in NY but your server is
UTC), add `TZ=America/New_York` at the top of your crontab.

---

## Development

```bash
uv run pytest                  # 195 tests, ~0.5s
uv run pytest tests/test_scoring.py -v   # just one file
```

Tests cover: all three source adapters (mocked HTTP), dedupe/filter/rank
stages, prompt + LLM client (mocked), formatter (snapshot-style for HTML),
Telegram client (mocked httpx), history store (real SQLite), end-to-end
pipeline (mocked external calls).

No pre-commit hooks. Ruff is optional but consistent.

Project layout:

```
src/research_digest/
├── config/         pydantic schemas + YAML loading
├── models/         SourceItem, Digest, DigestEntry — the data flowing through
├── sources/        one file per adapter + base interface + factory
├── pipeline/       digest_pipeline.py orchestrates everything; stages as submodules
├── ranking/        scoring.py (pure math) + ranker.py (orchestrator) + filter_rules.py
├── summarization/  llm_client.py + prompt_builder.py + summarizer.py + translator.py
├── delivery/       formatter.py + deliver.py + telegram_client.py
├── storage/        history_store.py (SQLite)
├── observability/  logger.py + run_context.py + artifact_store.py
└── entrypoints/    cli.py — typer commands
```

---

## Version 2 (planned)

The architectural hooks are already in place for:

- **Follow-up questions on pushed items**: `full_text_accessible` flag already
  travels with every entry; V2's fetcher dispatches on `source_type` and uses
  `extra["pmc_url"]` / `extra["doi"]` to pull full text on demand.
- **Translation model split**: translation is a mechanical operation; latency
  > quality. V2 may pin translate to a fast model (gpt-5.4-mini is ~10× faster
  than deepseek-v3.2 at translation) while summarization keeps the rotation.
  Also you can set the output language to "en" simply means no translation - 
  very simple.
- **`llm_relevance` score term**: reserved in `RankingWeights` as weight 0.
  Plug in a cheap LLM relevance call and raise the weight.

V1 deliberately does *not* implement these. It's a fixed linear pipeline;
keeping it boring made it easy to debug.
