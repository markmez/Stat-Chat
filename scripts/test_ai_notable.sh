#!/bin/bash
# Quick test of the AI notable events feature against production.
# Run from anywhere — just needs curl.
#
# Usage:
#   ./scripts/test_ai_notable.sh snapshot   # Just see the data snapshot (no Sonnet call)
#   ./scripts/test_ai_notable.sh full       # Full run: snapshot + Sonnet insights (dry run)

ADMIN_KEY="I9-NNJ-GBen3SZ-wf8JkZX5-_zvvt8Qri2EtTxWUo-I"
BASE_URL="https://stat-chat-production.up.railway.app"

case "${1:-snapshot}" in
  snapshot)
    echo "Fetching data snapshot (no AI call)..."
    curl -s "$BASE_URL/admin/ai-notable-snapshot" \
      -H "Authorization: Bearer $ADMIN_KEY" | python3 -m json.tool
    ;;
  full)
    echo "Running full AI notable detection (dry run)..."
    curl -s -X POST "$BASE_URL/admin/ai-notable?dry_run=true" \
      -H "Authorization: Bearer $ADMIN_KEY" | python3 -m json.tool
    ;;
  *)
    echo "Usage: $0 [snapshot|full]"
    ;;
esac
