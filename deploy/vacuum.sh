#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# SQLite VACUUM — reclaims wasted space from DELETE/UPDATE churn.
# Runs weekly via cron. Requires ~2x DB size in free disk space.
# ──────────────────────────────────────────────────────────────────────

set -uo pipefail

DB=/data/baseball_stats_full.db

echo "=== VACUUM starting at $(date) ==="

# Never stop the service under a pipeline run (it would kill a run launched
# from the API, and VACUUM needs the DB to itself anyway). Same flock the
# pipeline takes; wait up to an hour, else skip this time.
exec 9>/data/pipeline.lock
if ! flock -w 3600 9; then
    echo "SKIP: pipeline still running after 1h — no VACUUM this time"
    exit 0
fi

# Check free disk space (need at least 2x DB size)
DB_SIZE_KB=$(du -k "$DB" | cut -f1)
FREE_KB=$(df --output=avail /data | tail -1 | tr -d ' ')

if [ "$FREE_KB" -lt "$((DB_SIZE_KB * 2))" ]; then
    echo "SKIP: not enough free disk (${FREE_KB}KB free, need $((DB_SIZE_KB * 2))KB)"
    exit 0
fi

SIZE_BEFORE=$(du -h "$DB" | cut -f1)

# Stop the server to get exclusive DB access for VACUUM
sudo systemctl stop statchat
sleep 2

sqlite3 "$DB" "VACUUM;"
RC=$?

# Restart the server
sudo systemctl start statchat
sleep 2

SIZE_AFTER=$(du -h "$DB" | cut -f1)

if [ $RC -eq 0 ]; then
    echo "VACUUM complete: ${SIZE_BEFORE} → ${SIZE_AFTER}"
else
    echo "VACUUM failed (exit code $RC)"
fi

echo "=== VACUUM finished at $(date) ==="
