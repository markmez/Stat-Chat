#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# StatChat health monitor — runs every 5 min via cron.
#
# Goes beyond ping/fail: auto-recovers from common staleness modes.
#
#   - server up, data fresh, feed fresh    → ping ok
#   - server up, data fresh, feed stale    → run /admin/redetect (fast)
#   - server up, data stale                → kill stuck pipeline + refresh
#   - server down                          → ping fail immediately
#
# Recovery has a 30-min cooldown so consecutive health-check runs don't
# kill an in-flight refresh that we ourselves triggered. Only pings the
# failure URL after 30+ min of continuous staleness.
# ──────────────────────────────────────────────────────────────────────

set -uo pipefail

HC_PING_URL="https://hc-ping.com/f69f410b-1774-4af4-9bb4-c57136cc59ff"
API_BASE="https://api.secondsignalapps.com"
STALE_MARKER=/tmp/statchat_health_stale_since
RECOVERY_MARKER=/tmp/statchat_health_recovery_at
RECOVERY_COOLDOWN=1800   # 30 min

# Pull ADMIN_KEY for admin endpoint calls.
if [ -f /opt/statchat/.env ]; then
    set -a
    source /opt/statchat/.env
    set +a
fi
ADMIN_KEY="${ADMIN_KEY:-}"
AUTH_HDR="Authorization: Bearer ${ADMIN_KEY}"

ping_ok() {
    curl -fsS -m 10 "$HC_PING_URL" --data-raw "$1" > /dev/null 2>&1
    rm -f "$STALE_MARKER"
}

ping_fail() {
    curl -fsS -m 10 "$HC_PING_URL/fail" --data-raw "$1" > /dev/null 2>&1
}

mark_stale() {
    if [ ! -f "$STALE_MARKER" ]; then
        date +%s > "$STALE_MARKER"
    fi
}

stale_seconds() {
    if [ -f "$STALE_MARKER" ]; then
        echo $(( $(date +%s) - $(cat "$STALE_MARKER") ))
    else
        echo 0
    fi
}

recovery_in_cooldown() {
    if [ -f "$RECOVERY_MARKER" ]; then
        local last_recovery
        last_recovery=$(cat "$RECOVERY_MARKER")
        local now
        now=$(date +%s)
        if [ $((now - last_recovery)) -lt "$RECOVERY_COOLDOWN" ]; then
            return 0
        fi
    fi
    return 1
}

mark_recovery() {
    date +%s > "$RECOVERY_MARKER"
}

# 1. API up + DB queryable?
HEALTH=$(curl -fsS -m 10 "$API_BASE/health" 2>&1)
if ! echo "$HEALTH" | grep -q '"status":"ok"'; then
    ping_fail "API health check failed: ${HEALTH:0:200}"
    exit 0
fi

# 2. Freshness check — only enforced after MSF lag window has cleared.
HOUR_UTC=$(date -u +%H)
TODAY_UTC=$(date -u +%Y-%m-%d)
YESTERDAY_UTC=$(date -u -d "yesterday" +%Y-%m-%d)

# Before 12 UTC (8 AM ET), MSF may not have published last night's box
# scores yet. Don't enforce data-freshness in that window — server is
# still healthy even if data is one day behind.
if [ "$HOUR_UTC" -lt 12 ]; then
    ping_ok "ok (pre-noon UTC, freshness unenforced)"
    exit 0
fi

# Pull current data + feed freshness.
FRESHNESS=$(curl -fsS -m 10 "$API_BASE/admin/freshness" -H "$AUTH_HDR" 2>&1)
LATEST_GAME_DATE=$(echo "$FRESHNESS" | grep -oE '"last_game_date_2026"[^"]*"[0-9-]+"' | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' || true)

# Feed freshness — most recent event game_date.
FEED_LATEST=$(curl -fsS -m 10 "$API_BASE/notable-events?limit=1" 2>/dev/null | grep -oE '"game_date":"[0-9-]+"' | head -1 | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' || true)

DATA_STALE=0
FEED_STALE=0
if [ -z "$LATEST_GAME_DATE" ] || [ "$LATEST_GAME_DATE" \< "$YESTERDAY_UTC" ]; then
    DATA_STALE=1
fi
if [ -z "$FEED_LATEST" ] || [ "$FEED_LATEST" \< "$YESTERDAY_UTC" ]; then
    FEED_STALE=1
fi

# 3. Healthy path.
if [ "$DATA_STALE" -eq 0 ] && [ "$FEED_STALE" -eq 0 ]; then
    ping_ok "ok data=${LATEST_GAME_DATE} feed=${FEED_LATEST}"
    exit 0
fi

# 4. Recovery — but only if not in cooldown. Otherwise let the in-flight
# recovery finish on its own.
mark_stale
STALE_FOR=$(stale_seconds)

if recovery_in_cooldown; then
    # Recovery already in progress from an earlier run. Don't kick off
    # another one — that would kill the live attempt. Just track and
    # report.
    cooldown_left=$(( RECOVERY_COOLDOWN - ($(date +%s) - $(cat "$RECOVERY_MARKER")) ))
    if [ "$STALE_FOR" -gt 1800 ]; then
        ping_fail "stale ${STALE_FOR}s data=${LATEST_GAME_DATE} feed=${FEED_LATEST}; recovery in cooldown ${cooldown_left}s"
    else
        ping_ok "stale ${STALE_FOR}s data=${LATEST_GAME_DATE} feed=${FEED_LATEST}; recovery cooldown ${cooldown_left}s"
    fi
    exit 0
fi

# Cooldown clear — try recovery.
mark_recovery

if [ "$DATA_STALE" -eq 1 ]; then
    # Game logs aren't current. Pipeline may be hung.
    # 1) Kill any stuck pull_live_stats process
    # 2) Trigger /admin/refresh (fire-and-forget; curl gives up after 30s
    #    but the subprocess on the server keeps running)
    echo "[healthcheck] data stale (latest=$LATEST_GAME_DATE, expected>=$YESTERDAY_UTC) — kill+refresh" >&2
    curl -fsS -m 30 -X POST "$API_BASE/admin/kill-pipeline" -H "$AUTH_HDR" > /dev/null 2>&1 || true
    curl -fsS -m 30 -X POST "$API_BASE/admin/refresh" -H "$AUTH_HDR" > /dev/null 2>&1 || true
elif [ "$FEED_STALE" -eq 1 ]; then
    # Data is current but events weren't detected. /admin/redetect runs
    # detect_all only — fast (~30s) and doesn't touch the pipeline lock.
    echo "[healthcheck] feed stale (latest=$FEED_LATEST, data=$LATEST_GAME_DATE) — redetect" >&2
    curl -fsS -m 60 -X POST "$API_BASE/admin/redetect" -H "$AUTH_HDR" > /dev/null 2>&1 || true
fi

# 5. Page only after 30+ min of continuous staleness.
if [ "$STALE_FOR" -gt 1800 ]; then
    ping_fail "stale ${STALE_FOR}s data=${LATEST_GAME_DATE} feed=${FEED_LATEST}; recovery triggered repeatedly"
else
    ping_ok "stale ${STALE_FOR}s data=${LATEST_GAME_DATE} feed=${FEED_LATEST}; recovery triggered"
fi
