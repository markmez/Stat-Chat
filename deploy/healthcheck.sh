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

# Before 14 UTC (10 AM EDT / 9 AM EST), MSF may not have published last
# night's box scores yet. Pipeline cascades run through 8 AM ET; 10 AM
# ET is the panic threshold — anything missing then is a real issue.
if [ "$HOUR_UTC" -lt 14 ]; then
    ping_ok "ok (pre-10am ET, freshness unenforced)"
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
    # Coverage check: catches under-detected dates that pass the
    # data-stale + feed-stale gates because *some* events exist for
    # recent dates — just far fewer than they should. Failure mode:
    # detect_all ran while MSF was mid-publish, fired only the active-
    # state detectors (streaks/current_form) on partial data, and never
    # got a second pass once data completed. /admin/coverage-check
    # scans the last few days and auto-redetects any date with full
    # game logs but a sparse event bucket. Cheap (~1-2s when nothing
    # needs fixing); only does heavy work when an under-detected date
    # is found.
    COVERAGE=$(curl -fsS -m 60 -X POST "$API_BASE/admin/coverage-check" -H "$AUTH_HDR" 2>&1 || true)
    BACKFILLED=$(echo "$COVERAGE" | grep -oE '"backfilled":\[[^]]*\]' | grep -oE '"[0-9-]+"' | tr -d '"' | tr '\n' ' ' | sed 's/ $//')
    REFRESH_NEEDED=$(echo "$COVERAGE" | grep -oE '"refresh_needed":\[[^]]*\]' | grep -oE '"[0-9-]+"' | tr -d '"' | tr '\n' ' ' | sed 's/ $//')
    if [ -n "$REFRESH_NEEDED" ]; then
        # Partial-load detected (game logs short of MLB schedule). Coverage
        # check already kicked off pull_live_stats — start the recovery
        # cooldown so we don't trample it on the next 5-min tick.
        echo "[healthcheck] coverage partial-load on: $REFRESH_NEEDED — pipeline refresh triggered" >&2
        mark_recovery
        ping_ok "ok data=${LATEST_GAME_DATE} feed=${FEED_LATEST}; partial load on ${REFRESH_NEEDED}, refresh triggered"
    elif [ -n "$BACKFILLED" ]; then
        echo "[healthcheck] coverage backfilled dates: $BACKFILLED" >&2
        ping_ok "ok data=${LATEST_GAME_DATE} feed=${FEED_LATEST}; coverage backfilled ${BACKFILLED}"
    else
        ping_ok "ok data=${LATEST_GAME_DATE} feed=${FEED_LATEST}"
    fi
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
    # Game logs aren't current. The pipeline could be:
    #   (a) genuinely stuck — kill it and trigger fresh /admin/refresh
    #   (b) running normally and just slow — leave it alone (a normal
    #       pipeline run takes 30-90 min; freshness only updates on
    #       completion, so it ALWAYS looks stale during a live run)
    #
    # Use min_age_seconds=5400 on kill-pipeline so processes younger
    # than 90 min are left alive. The response's `running` count tells
    # us how many young (in-progress) processes were preserved — if >0,
    # we skip the /admin/refresh too, since a healthy pipeline is
    # already running.
    KILL_RESPONSE=$(curl -fsS -m 30 -X POST "$API_BASE/admin/kill-pipeline?min_age_seconds=5400" -H "$AUTH_HDR" 2>&1 || true)
    KILLED=$(echo "$KILL_RESPONSE" | grep -oE '"killed":[0-9]+' | grep -oE '[0-9]+' | head -1 || echo 0)
    RUNNING=$(echo "$KILL_RESPONSE" | grep -oE '"running":[0-9]+' | grep -oE '[0-9]+' | head -1 || echo 0)

    if [ "${RUNNING:-0}" -gt 0 ]; then
        # Young pipeline still working — DON'T trigger another refresh.
        # Also clear our recovery marker so cooldown doesn't block a
        # legitimate recovery later if this run ultimately fails.
        echo "[healthcheck] data stale but young pipeline running (running=$RUNNING) — leaving alone" >&2
        rm -f "$RECOVERY_MARKER"
        ping_ok "stale ${STALE_FOR}s data=${LATEST_GAME_DATE} feed=${FEED_LATEST}; pipeline in progress, will let it finish"
        exit 0
    fi

    echo "[healthcheck] data stale (latest=$LATEST_GAME_DATE, expected>=$YESTERDAY_UTC) — killed=$KILLED, refreshing" >&2
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
