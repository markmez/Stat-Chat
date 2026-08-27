"""Fetch probable starting pitchers for today's+tomorrow's slate from the
MSF lineup endpoint into upcoming_games.{home,away}_probable. Defensive:
the endpoint shape is unverified, so we search the payload recursively for
starting-pitcher entries and stash one raw sample in trend_state
('probables_sample') for inspection when parsing finds nothing.
Run: /admin/update-probables (detached), or after the nightly pipeline.
"""
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_pipeline"))
from pull_live_stats import msf_get, retro_team  # noqa: E402

DB = os.environ.get("DB_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "baseball_stats.db"))


def _find_starters(obj, out):
    """Recursively collect (teamAbbrev, pitcherName) for probable/starting
    pitchers anywhere in the payload."""
    if isinstance(obj, dict):
        pos = str(obj.get("position") or obj.get("lineupPosition") or "")
        player = obj.get("player") or {}
        if pos.upper() in ("P", "SP", "PITCHER", "STARTING_PITCHER") and isinstance(player, dict):
            nm = " ".join(x for x in (player.get("firstName"), player.get("lastName")) if x)
            team = (obj.get("team") or {}).get("abbreviation") if isinstance(obj.get("team"), dict) else None
            if nm:
                out.append((team, nm))
        for v in obj.values():
            _find_starters(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_starters(v, out)


def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout = 30000")
    for col in ("home_probable", "away_probable"):
        try:
            conn.execute(f"ALTER TABLE upcoming_games ADD COLUMN {col} TEXT")
        except Exception:
            pass
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    games = conn.execute(
        "SELECT game_id, date, home, away FROM upcoming_games"
        " WHERE date IN (?, ?) ORDER BY date", (today, tomorrow)).fetchall()
    season_str = f"{date.today().year}-regular"
    print(f"probables: {len(games)} games for {today}/{tomorrow}")
    got, sample_saved = 0, False
    for gid, gd, home, away in games:
        try:
            data = msf_get(f"{season_str}/games/{gid}/lineup.json")
        except Exception as e:
            print(f"  {gid}: fetch failed: {e}")
            continue
        starters = []
        _find_starters(data, starters)
        hp = ap = None
        for team_ab, nm in starters:
            code = retro_team(team_ab) if team_ab else None
            if code == home and not hp:
                hp = nm
            elif code == away and not ap:
                ap = nm
        if not (hp or ap) and starters:
            # team attribution failed; two starters in order is the best guess
            names = [n for _, n in starters]
            if len(names) >= 2:
                ap, hp = names[0], names[1]
        if not (hp or ap) and not sample_saved and data:
            conn.execute("INSERT OR REPLACE INTO trend_state VALUES (?,?,?)",
                         ("probables_sample", json.dumps(data, default=str)[:4000],
                          datetime.utcnow().isoformat()))
            sample_saved = True
        if hp or ap:
            conn.execute("UPDATE upcoming_games SET home_probable=?, away_probable=?"
                         " WHERE game_id=?", (hp, ap, gid))
            got += 1
        conn.commit()
        time.sleep(1.2)
    conn.execute("INSERT OR REPLACE INTO trend_state VALUES (?,?,?)",
                 ("probables_status", json.dumps({"games": len(games), "with_starters": got,
                  "day": today}), datetime.utcnow().isoformat()))
    conn.commit()
    print(f"probables: starters found for {got}/{len(games)} games")


if __name__ == "__main__":
    main()
