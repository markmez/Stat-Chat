#!/bin/bash
# Deploy script: triggers a Railway deploy.
#
# The DB lives on the Railway volume and is refreshed by cron every 4 hours.
# No need to upload 223MB every deploy — ensure_db() uses the volume DB.
#
# Usage: cd backend && ./deploy.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Clean up any leftover DB copy from old deploys
if [ -f "$SCRIPT_DIR/baseball_stats_full.db" ]; then
    echo "Removing leftover DB copy from backend/..."
    rm "$SCRIPT_DIR/baseball_stats_full.db"
fi

echo "Deploying to Railway..."
railway up
