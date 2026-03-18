#!/bin/bash
# Copies stat_config.json from shared/ (source of truth) to both consumers.
# Run after editing shared/stat_config.json.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$SCRIPT_DIR/stat_config.json"

cp "$SRC" "$SCRIPT_DIR/../ios/BaseballStatsEngine/Resources/stat_config.json"
cp "$SRC" "$SCRIPT_DIR/../backend/services/stat_config.json"

echo "Synced stat_config.json → iOS Resources + backend services"
