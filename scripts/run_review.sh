#!/bin/bash
# run_review.sh
# -------------
# Orchestrates the full portfolio review pipeline.
# Called by cron at 9:45 AM and 3:45 PM ET on trading days.
#
# Usage:
#   ./scripts/run_review.sh morning
#   ./scripts/run_review.sh afternoon
#
# Environment (loaded from .env):
#   ANTHROPIC_API_KEY   required
#   LIVE_TRADING        true|false (default false)
#   TELEGRAM_BOT_TOKEN  optional — enables Telegram delivery
#   TELEGRAM_CHAT_ID    optional

set -euo pipefail

SESSION="${1:-morning}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
TIMESTAMP="$(date +"%Y-%m-%d_%H%M")"
REVIEWS_DIR="$REPO_DIR/reviews"
LOG_FILE="$REVIEWS_DIR/${TIMESTAMP}_${SESSION}.txt"

mkdir -p "$REVIEWS_DIR" "$REPO_DIR/logs"

# ── Load .env ────────────────────────────────────────────────────────────────
if [ -f "$REPO_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +a
fi

log() { echo "[run_review $(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

log "=== Portfolio Review — $SESSION — $TIMESTAMP ==="

# ── Trading day check ────────────────────────────────────────────────────────
if ! python "$SCRIPT_DIR/market_schedule.py" --check-today >> "$LOG_FILE" 2>&1; then
  log "Not a trading day. Exiting."
  exit 0
fi

# ── Step 1: Fetch live positions from Robinhood MCP ──────────────────────────
log "Fetching live positions via Robinhood MCP..."
POSITIONS_FILE="$REPO_DIR/positions.json"

# claude -p runs headlessly using the auth token stored in ~/.claude/
# If the MCP session has expired, this will error — caught below.
if ! claude -p "$REPO_DIR/research/fetch_positions.md" > "$POSITIONS_FILE" 2>> "$LOG_FILE"; then
  log "ERROR: claude -p failed. Check Robinhood MCP auth (run: claude mcp list)."
  python "$SCRIPT_DIR/notify.py" \
    --message "Portfolio review FAILED ($SESSION): could not fetch positions. Robinhood MCP auth may have expired." \
    --session "$SESSION" 2>/dev/null || true
  exit 1
fi

# Validate the output is real JSON with a positions key
if ! python -c "
import json, sys
try:
    d = json.load(open('$POSITIONS_FILE'))
    assert 'positions' in d, 'missing positions key'
    print(f'[run_review] {len(d[\"positions\"])} position(s) loaded.')
except Exception as e:
    print(f'[run_review] ERROR: {e}', file=sys.stderr)
    sys.exit(1)
" 2>> "$LOG_FILE"; then
  log "ERROR: positions.json is invalid or missing. Robinhood MCP may have returned an error."
  python "$SCRIPT_DIR/notify.py" \
    --message "Portfolio review FAILED ($SESSION): positions.json invalid. Check MCP auth." \
    --session "$SESSION" 2>/dev/null || true
  exit 1
fi

# ── Step 2: Run portfolio review ─────────────────────────────────────────────
log "Running portfolio review..."
cd "$REPO_DIR"

LIVE_FLAG=""
if [ "${LIVE_TRADING:-false}" = "true" ]; then
  log "LIVE_TRADING=true — engine exits will be executed."
  LIVE_FLAG="--live"
fi

if ! python research/portfolio_review.py \
    --positions "$POSITIONS_FILE" \
    $LIVE_FLAG \
    2>> "$LOG_FILE" | tee -a "$LOG_FILE"; then
  log "ERROR: portfolio_review.py failed."
  python "$SCRIPT_DIR/notify.py" \
    --message "Portfolio review FAILED ($SESSION): portfolio_review.py errored. See logs." \
    --session "$SESSION" 2>/dev/null || true
  exit 1
fi

# ── Step 3: Deliver via Telegram ─────────────────────────────────────────────
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  log "Sending to Telegram..."
  python "$SCRIPT_DIR/notify.py" \
    --file "$LOG_FILE" \
    --session "$SESSION" \
    2>> "$LOG_FILE" || log "Warning: Telegram delivery failed (review still saved locally)."
else
  log "Telegram not configured. Review saved to: $LOG_FILE"
fi

log "=== Done ==="
