"""SQLite-backed store of already-pushed digest items.

Only this module knows SQL. Everyone else calls the `HistoryStore` methods.

Purpose
-------
We record every item that actually made it to Telegram. Later runs query
this table to avoid pushing the same item twice — that's the only cross-run
dedupe layer V1 has (see design notes on dedupe strategy and push idempotency).

We deliberately do NOT record "items we fetched but didn't push". See the
design discussion: recording fetch-only would risk silently hiding content
whose ranking improved later.

Storage layout
--------------
One SQLite file at `data/history/digest_history.db`. Single table, two
indexes on the fields dedupe looks up. Schema migrations are not a concern
in V1 — if we ever change the schema we can just delete the file.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research_digest.models.source_item import SourceItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS digest_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url_canonical TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    title_norm    TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    pushed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_url  ON digest_history(url_canonical);
CREATE INDEX IF NOT EXISTS idx_history_hash ON digest_history(content_hash);
"""


class HistoryStore:
    """Thin wrapper over a SQLite connection. Safe to reuse within one run."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        # Make sure the parent directory exists; sqlite3 won't create it.
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: pipeline is async; we access from the
        # event loop thread only, but asyncio may schedule callbacks that
        # look like "different threads" to sqlite. In practice we never
        # share this connection across real OS threads.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # Return rows as dict-like objects so callers can do row["url_canonical"].
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    # --- schema ------------------------------------------------------------

    def init_schema(self) -> None:
        """Create the table + indexes if they don't exist yet. Idempotent."""
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- reads -------------------------------------------------------------

    def has_been_pushed(
        self,
        *,
        url_canonical: str,
        content_hash: str | None,
        user_id: str,
    ) -> bool:
        """Return True if this item was already pushed to this user.

        Match rule: url_canonical OR content_hash. Either is enough to call
        it a duplicate — dedupe layers 1 and 2 from §25.6 collapse into one
        cross-run query. title-similarity (layer 3) is not used across runs
        because it would need a full table scan.
        """
        if content_hash is None:
            # No hash yet (shouldn't happen post-dedupe, but be defensive).
            row = self._conn.execute(
                "SELECT 1 FROM digest_history "
                "WHERE user_id = ? AND url_canonical = ? LIMIT 1",
                (user_id, url_canonical),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM digest_history "
                "WHERE user_id = ? "
                "  AND (url_canonical = ? OR content_hash = ?) LIMIT 1",
                (user_id, url_canonical, content_hash),
            ).fetchone()
        return row is not None

    # --- writes ------------------------------------------------------------

    def record_push(self, item: SourceItem, *, user_id: str) -> None:
        """Record that `item` was successfully pushed to `user_id`.

        Called from delivery layer AFTER Telegram confirmed delivery. Never
        call this speculatively — a row here means "the user saw it".
        """
        if item.content_hash is None:
            raise ValueError(
                f"record_push: item {item.id} has no content_hash; "
                "dedupe stage should have filled it in."
            )
        self._conn.execute(
            "INSERT INTO digest_history "
            "(url_canonical, content_hash, title_norm, user_id, pushed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                item.url_canonical,
                item.content_hash,
                _normalize_title(item.title),
                user_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    # --- maintenance -------------------------------------------------------

    def prune_older_than(self, days: int) -> int:
        """Delete rows older than `days`. Returns number of rows deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM digest_history WHERE pushed_at < ?", (cutoff,)
        )
        self._conn.commit()
        return cur.rowcount

    def count(self) -> int:
        """Total rows. Handy for logs and tests."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM digest_history"
        ).fetchone()
        return int(row["n"])

    # --- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _normalize_title(title: str) -> str:
    """Lowercase + collapse whitespace. Keep it simple — layer 3 dedupe
    within a run does the heavier normalization."""
    return " ".join(title.lower().split())
