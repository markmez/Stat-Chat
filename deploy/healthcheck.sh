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

# Pings the MSF cron monitor — its notification rule is already set up
# in the Healthchecks.io account. /ok on clean ticks keeps the monitor
# "Up" (suppresses the no-longer-needed cron-down alerts that fired
# when refresh.sh's cron timing slipped). /fail fires only on persistent
# coverage issues per the COVERAGE_PERSIST_THRESHOLD logic below — the
# actionable signal.
HC_PING_URL="https://hc-ping.com/d3f0c82b-235a-477f-8ed5-3f6ac4c6daa7"
API_BASE="https://api.secondsignalapps.com"
STALE_MARKER=/tmp/statchat_health_stale_since
RECOVERY_MARKER=/tmp/statchat_health_recovery_at
COVERAGE_ISSUE_MARKER=/tmp/statchat_coverage_issue_since
RECOVERY_COOLDOWN=1800   # 30 min
COVERAGE_PERSIST_THRESHOLD=1800   # 30 min — coverage issue persisting this long pages

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

# 2. Freshness check — only enforced after the last morning cron should
# have finished. Thresholds match the cron schedule:
#   Weekday last morning cron: 10:00 AM ET (14:00 UTC), ~30 min runtime
#     → enforce at 14:45 UTC (10:45 AM EDT / 9:45 AM EST)
#   Weekend last morning cron: 11:30 AM ET (15:30 UTC), ~30 min runtime
#     → enforce at 16:15 UTC (12:15 PM EDT / 11:15 AM EST)
# Enforcing earlier would fire false alarms during a healthy in-progress
# refresh.
HOUR_UTC=$(date -u +%H)
MIN_UTC=$(date -u +%M)
DOW_UTC=$(date -u +%u)   # 1=Mon..7=Sun (ISO week day)
TODAY_UTC=$(date -u +%Y-%m-%d)
YESTERDAY_UTC=$(date -u -d "yesterday" +%Y-%m-%d)

if [ "$DOW_UTC" -le 5 ]; then
    THRESHOLD_HOUR=14
    THRESHOLD_MIN=45
    WINDOW_LABEL="pre-10:45am ET (weekday)"
else
    THRESHOLD_HOUR=16
    THRESHOLD_MIN=15
    WINDOW_LABEL="pre-12:15pm ET (weekend)"
fi
NOW_MINUTES=$(( 10#$HOUR_UTC * 60 + 10#$MIN_UTC ))
THRESHOLD_MINUTES=$(( THRESHOLD_HOUR * 60 + THRESHOLD_MIN ))
if [ "$NOW_MINUTES" -lt "$THRESHOLD_MINUTES" ]; then
    ping_ok "ok (${WINDOW_LABEL}, freshness unenforced)"
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
        # Partial-load detected (game logs short of MLB schedule).
        # Coverage-check already kicked off pull_live_stats async.
        #
        # Track persistence — only PAGE (ping_fail) once the issue has
        # persisted past COVERAGE_PERSIST_THRESHOLD (30 min). Multiple
        # healthcheck ticks within that window are silent (ping_ok) so
        # we don't spam during normal self-heal latency.
        if [ ! -f "$COVERAGE_ISSUE_MARKER" ]; then
            date +%s > "$COVERAGE_ISSUE_MARKER"
        fi
        ISSUE_FOR=$(( $(date +%s) - $(cat "$COVERAGE_ISSUE_MARKER") ))
        mark_recovery   # block stale-path recovery from trampling pull_live_stats
        if [ "$ISSUE_FOR" -gt "$COVERAGE_PERSIST_THRESHOLD" ]; then
            # Self-heal has been attempting for 30+ min and still hasn't
            # closed the gap. Page — this needs a human.
            ping_fail "MSF data coverage incomplete for ${REFRESH_NEEDED} — self-heal running ${ISSUE_FOR}s, still partial. Check /data/refresh.log + MSF status."
        else
            ping_ok "ok data=${LATEST_GAME_DATE} feed=${FEED_LATEST}; partial load on ${REFRESH_NEEDED}, self-healing (${ISSUE_FOR}s)"
        fi
        exit 0
    fi

    # No active coverage issue — clear the persistence marker.
    rm -f "$COVERAGE_ISSUE_MARKER"

    if [ -n "$BACKFILLED" ]; then
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
