#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# StatChat health monitor — runs every 5 min via cron.
# Curls the /health endpoint and pings Healthchecks.io.
# ──────────────────────────────────────────────────────────────────────

HC_PING_URL="https://hc-ping.com/f69f410b-1774-4af4-9bb4-c57136cc59ff"

# Check if API is responding and DB is queryable
RESPONSE=$(curl -fsS -m 10 https://api.secondsignalapps.com/health 2>&1)

if echo "$RESPONSE" | grep -q '"status":"ok"'; then
    curl -fsS -m 10 "$HC_PING_URL" > /dev/null 2>&1
else
    curl -fsS -m 10 "$HC_PING_URL/fail" --data-raw "Health check failed: $RESPONSE" > /dev/null 2>&1
fi
