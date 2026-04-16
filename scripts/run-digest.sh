#!/usr/bin/env bash
# Wrapper for cron — runs the full pipeline (fetch → rank → summarize → push).
#
# Why a wrapper instead of calling `uv run digest run` directly in crontab:
#   1. Cron starts with a minimal environment — no PATH to uv, no .env loaded.
#   2. Cron doesn't set a working directory — config/ and data/ need it.
#   3. If anything fails we want stderr captured to a known log file so you
#      can debug without digging through /var/mail.
#
# Usage:
#   crontab -e
#   0 8 * * * /home/greyson/projects/agentic/research-agent/scripts/run-digest.sh
#
# The script is intentionally simple: set up the environment, run the command,
# and get out of the way. All real logic lives in the Python pipeline.

set -euo pipefail

# --- Resolve project root (one level up from this script) ---------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"

# --- Load .env if present (secrets: OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN) ---
if [ -f .env ]; then
    set -a
    # shellcheck source=/dev/null
    source .env
    set +a
fi

# --- Ensure uv is on PATH (cron's PATH is typically just /usr/bin:/bin) -------
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:$PATH"

# --- Run the full pipeline ----------------------------------------------------
exec uv run digest run
