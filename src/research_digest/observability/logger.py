"""Project-wide logging setup.

One call to `setup_logging(run_context)` at process start configures the
root logger with:

  * a console handler (stderr) for humans watching the run live
  * a file handler writing to `data/logs/run-<id>.log` for post-hoc review

After that, every other module just does::

    import logging
    logger = logging.getLogger(__name__)

and calls `logger.info(...)` / `logger.debug(...)` — no per-module handler
setup, no `print`.

The log level is read from the `LOG_LEVEL` env var (via `Settings`), so
`.env` is the single knob for verbosity.
"""

from __future__ import annotations

import logging
from logging import Formatter, StreamHandler
from logging.handlers import RotatingFileHandler

from ..config.secret_env import get_settings
from .run_context import RunContext

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(run_context: RunContext) -> None:
    """Configure root logger; idempotent within a single process.

    Args:
        run_context: Provides the log file path. `ensure_dirs()` must have
            been called beforehand so the log directory exists.

    Called by: the CLI entrypoint, exactly once per run, before any stage
    starts. Safe to call again (e.g. in tests) — existing handlers are
    cleared first so we don't double-log.
    """
    settings = get_settings()
    level = getattr(logging, settings.log_level)

    root = logging.getLogger()
    root.setLevel(level)

    # Wipe any handlers from a prior call (tests, re-entry) to avoid
    # duplicate lines on the console.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    # Rotating just in case a run somehow produces a huge log; 5 MB cap is
    # generous for V1 runs but prevents a runaway loop from filling disk.
    file_handler = RotatingFileHandler(
        run_context.log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Quiet down chatty third-party libs; their DEBUG is almost never useful.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    root.info("logging initialized run_id=%s level=%s", run_context.run_id, settings.log_level)
