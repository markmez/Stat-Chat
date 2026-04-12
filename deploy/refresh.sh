#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# StatChat pipeline refresh — called by cron.
# Runs the MSF pipeline, then checks data integrity.
# Pings Healthchecks.io on success/failure.
# ──────────────────────────────────────────────────────────────────────

set -uo pipefail

# Load env vars (API keys)
set -a
source /opt/statchat/.env
set +a

VENV=/opt/statchat/venv/bin/python3
PIPELINE=/opt/statchat/repo/backend/data_pipeline/pull_live_stats.py
INTEGRITY=/opt/statchat/repo/backend/data_pipeline/fix_doubleheader_schema.py
DB=/data/baseball_stats_full.db
HC_PING_URL="https://hc-ping.com/d3f0c82b-235a-477f-8ed5-3f6ac4c6daa7"

LOCK="/tmp/statchat_detection.lock"
PIPELINE_LOCK="/tmp/statchat_pipeline.lock"

# Prevent concurrent pipeline runs
if [ -f "$PIPELINE_LOCK" ]; then
    # Check if the lock is stale (older than 90 minutes)
    if [ "$(find "$PIPELINE_LOCK" -mmin +90 2>/dev/null)" ]; then
        echo "Removing stale pipeline lock (>90 min old)"
        rm -f "$PIPELINE_LOCK"
    else
        # 10 AM ET backup (14:00 UTC) is the last daytime run — retry up to 3 times
        CURRENT_HOUR=$(date -u +%H)
        if [ "$CURRENT_HOUR" = "14" ]; then
            RETRY=0
            while [ -f "$PIPELINE_LOCK" ] && [ $RETRY -lt 3 ]; do
                RETRY=$((RETRY + 1))
                echo "Pipeline locked — backup slot retry $RETRY/3 in 30 minutes..."
                sleep 1800
                if [ "$(find "$PIPELINE_LOCK" -mmin +90 2>/dev/null)" ]; then
                    echo "Removing stale pipeline lock after retry"
                    rm -f "$PIPELINE_LOCK"
                    break
                fi
            done
            if [ -f "$PIPELINE_LOCK" ]; then
                echo "Pipeline still locked after 3 retries — giving up"
                exit 0
            fi
        else
            echo "Pipeline already running (lock exists) — skipping"
            exit 0
        fi
    fi
fi

echo ""
echo "=== Pipeline refresh starting at $(date) ==="

# Lock pipeline + detection
touch "$PIPELINE_LOCK"
touch "$LOCK"
trap 'rm -f "$LOCK" "$PIPELINE_LOCK"' EXIT

# Run pipeline (pass through any extra args like --full-refresh)
if $VENV $PIPELINE --db $DB "$@"; then
    echo "=== Pipeline completed at $(date) ==="
else
    echo "=== Pipeline FAILED at $(date) ==="
    curl -fsS -m 10 "$HC_PING_URL/fail" --data-raw "Pipeline failed" || true
    exit 1
fi

# Records tables rebuilt weekly via cron (Sunday), not on every pipeline run

# Run data integrity check
echo "=== Running integrity check ==="
INTEGRITY_OUTPUT=$($VENV $INTEGRITY --db $DB --check-only 2>&1)
echo "$INTEGRITY_OUTPUT"

if echo "$INTEGRITY_OUTPUT" | grep -q "integrity issues found"; then
    # Integrity issues — ping failure with details
    echo "=== INTEGRITY CHECK FAILED ==="
    curl -fsS -m 10 "$HC_PING_URL/fail" --data-raw "$INTEGRITY_OUTPUT" || true
else
    # All good — ping success
    curl -fsS -m 10 "$HC_PING_URL" --data-raw "OK - $(date)" || true
fi

echo "=== Refresh complete at $(date) ==="
