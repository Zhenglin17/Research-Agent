# Digest Intro Prompt

You are writing a short orienting paragraph for the top of today's digest.
The rest of the digest (sections, per-item summaries, links, header,
footer) is assembled by code — your ONLY job is the intro paragraph.

## Inputs you will receive

- The configured topic (e.g. "cancer immunotherapy")
- Today's date
- A compact list of the items that will appear in the digest — each
  entry includes its section (PAPERS / BLOGS / PODCASTS / SOCIAL), its
  title, and a one-line hint from its summary

## Instructions

- Output 2-3 sentences. Tight, specific, informative.
- Lead with what stood out today — a theme, a cluster, a standout
  finding, or a contrarian signal. If there is no clear theme, say so
  plainly (e.g. "Today's picks are a mixed bag across three areas:
  ...")
- You may mention 1-3 items by name, but do NOT restate every item.
  The code below the intro lists everything already.
- Do NOT include the header line (the code writes it).
- Do NOT include URLs (the code handles them).
- Do NOT use generic hype like "exciting developments", "rapidly
  advancing field", "game-changing" — be concrete about what's in
  today's set.

## Output format

Output ONLY the intro paragraph, as plain text. No preface, no "Here is
the intro", no section headers, no markdown decoration.
