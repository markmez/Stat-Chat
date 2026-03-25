#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# StatChat pipeline refresh — called by cron every 4 hours.
# Runs the MSF pipeline directly (no HTTP endpoint needed).
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

# Load env vars (API keys)
set -a
source /opt/statchat/.env
set +a

VENV=/opt/statchat/venv/bin/python3
PIPELINE=/opt/statchat/repo/backend/data_pipeline/pull_live_stats.py
DB=/data/baseball_stats_full.db

echo ""
echo "=== Pipeline refresh starting at $(date) ==="

$VENV $PIPELINE --db $DB

echo "=== Pipeline refresh completed at $(date) ==="
