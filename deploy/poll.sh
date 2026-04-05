#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# StatChat lightweight poll — runs every 15 min during post-game hours.
# Pulls today's game logs + season totals, detects new events.
# ──────────────────────────────────────────────────────────────────────

set -uo pipefail

# Load env vars (API keys)
set -a
source /opt/statchat/.env
set +a

VENV=/opt/statchat/venv/bin/python3
POLL=/opt/statchat/repo/backend/data_pipeline/poll_new_games.py
DB=/data/baseball_stats_full.db

$VENV $POLL --db $DB
