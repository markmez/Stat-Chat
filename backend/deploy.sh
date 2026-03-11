#!/bin/bash
# Deploy script: copies the latest DB into backend/ for Docker build,
# then triggers a Railway deploy.
#
# Usage: cd backend && ./deploy.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DB_SOURCE="$PROJECT_ROOT/baseball_stats_full.db"
DB_DEST="$SCRIPT_DIR/baseball_stats_full.db"

if [ ! -f "$DB_SOURCE" ]; then
    echo "ERROR: $DB_SOURCE not found. Build the DB first."
    exit 1
fi

echo "Copying database to backend/ for Docker build..."
cp "$DB_SOURCE" "$DB_DEST"
echo "Done ($(du -h "$DB_DEST" | cut -f1) copied)"

echo ""
echo "Database is ready for Railway deploy."
echo "Push to your Railway-connected branch, or run: railway up"
