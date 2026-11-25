#!/usr/bin/env python3
"""Offseason feed: keeps the Notable feed alive when no games are played.

Two card families, both deterministic (house templates, no LLM), both
dated TODAY like On This Date (the date is the anchor, see
feedback-feed-event-date-semantics):

  born_today      one card a day: the most prominent player born on today's
                  month-day, leading with the birthday, then his career line.
                  An active star born today gets a second card.
  season_review   "2026 Season in Review": a queue of season-level facts
                  with all-time context ("the most since..."), built once
                  from the finalized season and published one a day.

Runs only outside the regular season (season_phase offseason / postseason /
finalizing). Zero MySportsFeeds calls; reads the stats DB only.

  python offseason_feed.py --db DB --preview       # print today's cards + the queue, write nothing
  python offseason_feed.py --db DB --build-queue   # (re)build the review queue for the final season
  python offseason_feed.py --db DB --publish       # daily: insert today's cards (cron)
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from services.franchise import get_franchise_name  # noqa: E402

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")
OFF_PHASES = {"offseason", "postseason", "finalizing"}
RETENTION_DAYS = 14
BORN_ACTIVE_MIN_PROMINENCE = 2500   # second Born Today card for an active star
REVIEW_TYPE, BORN_TYPE = "season_review", "born_today"


def et_today():
    return datetime.now(ZoneInfo("America/New_York")).date()


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def fmt_rate(x):
    s = f"{x:.3f}"
    return s[1:] if s.startswith("0") else s


def team_name(code):
    """Franchise display name, or None for codes with no mapping (19th-century
    clubs etc.), so a sentence never shows a raw code like 'PH4'."""
    code = (code or "").split("/")[0]
    try:
        n = get_franchise_name(code)
    except Exception:
        n = None
    return n if n and n != code else None


# ── Born Today ─────────────────────────────────────────────────────────────

AWARD_LABELS = [("MVP", "MVP"), ("CY", "Cy Young"), ("ROY", "Rookie of the Year"),
                ("ALL_STAR", "All-Star")]


def _career(conn, pid):
    bat = conn.execute("""
        SELECT COUNT(DISTINCT season), MIN(season), MAX(season), SUM(games),
               SUM(at_bats), SUM(hits), SUM(doubles), SUM(triples), SUM(home_runs),
               SUM(walks), SUM(hit_by_pitch), SUM(sacrifice_flies), SUM(stolen_bases),
               SUM(rbi)
        FROM season_batting_stats WHERE player_id = ?""", (pid,)).fetchone()
    pit = conn.execute("""
        SELECT COUNT(DISTINCT season), MIN(season), MAX(season), SUM(games),
               SUM(games_started), SUM(wins), SUM(losses), SUM(saves),
               SUM(ip_outs), SUM(earned_runs), SUM(strikeouts)
        FROM season_pitching_stats WHERE player_id = ?""", (pid,)).fetchone()
    teams = conn.execute("""
        SELECT team, SUM(g) FROM (
            SELECT team, games g FROM season_batting_stats WHERE player_id = ?
            UNION ALL
            SELECT team, games g FROM season_pitching_stats WHERE player_id = ?)
        WHERE team IS NOT NULL AND team != '' GROUP BY team ORDER BY 2 DESC""",
        (pid, pid)).fetchall()
    awards = dict(conn.execute(
        "SELECT award, COUNT(*) FROM awards WHERE player_id = ? GROUP BY award",
        (pid,)).fetchall())
    return bat, pit, teams, awards


def _career_sentence(conn, pid, active):
    bat, pit, teams, awards = _career(conn, pid)
    is_pitcher = (pit[3] or 0) > (bat[3] or 0) * 0.5 and (pit[8] or 0) > 300
    seasons = max(bat[0] or 0, pit[0] or 0)
    franchises = []
    for code, _ in teams:
        n = team_name(code)
        if n and n not in franchises:
            franchises.append(n)
    if is_pitcher:
        ip = (pit[8] or 0) / 3
        era = (pit[9] or 0) * 9 / ip if ip else 0
        line = f"{pit[5] or 0}-{pit[6] or 0}, {era:.2f} ERA, {(pit[10] or 0):,} strikeouts"
        if (pit[7] or 0) >= 50:
            line += f", {pit[7]} saves"
    else:
        ab, h = bat[4] or 0, bat[5] or 0
        if not ab:
            return None
        bb, hbp, sf = bat[9] or 0, bat[10] or 0, bat[11] or 0
        tb = h + (bat[6] or 0) + 2 * (bat[7] or 0) + 3 * (bat[8] or 0)
        avg, obp = h / ab, (h + bb + hbp) / max(1, ab + bb + hbp + sf)
        slg = tb / ab
        line = f"{fmt_rate(avg)}/{fmt_rate(obp)}/{fmt_rate(slg)} with {(bat[8] or 0):,} home runs and {h:,} hits"
        if (bat[12] or 0) >= 300:
            line += f" and {bat[12]:,} stolen bases"
    if not franchises:
        where = ""
    elif len(franchises) == 1:
        where = (f"In {seasons} season{'s' if seasons != 1 else ''}, all with the {franchises[0]}"
                 if not active else f"{seasons} season{'s' if seasons != 1 else ''} with the {franchises[0]}")
    else:
        where = f"{seasons} seasons, most with the {franchises[0]}"
    honors = []
    for code, label in AWARD_LABELS:
        n = awards.get(code, 0)
        if n:
            honors.append(f"{n}× {label}" if n > 1 else label)
    if awards.get("HOF"):
        honors.append("Hall of Fame")
    s = f"{where}: {line}." if where else f"Career: {line}."
    if honors:
        s += " " + ", ".join(honors) + "."
    return s, franchises[:1]


def born_today_cards(conn, today, season):
    md = today.strftime("%m-%d")
    rows = conn.execute("""
        SELECT player_id, name, birthdate, prominence_score, last_season
        FROM players WHERE substr(birthdate, 6, 5) = ? AND birthdate LIKE '____-__-__'
          AND prominence_score > 0
        ORDER BY prominence_score DESC LIMIT 25""", (md,)).fetchall()
    if not rows:
        return []
    picks = [rows[0]]
    active = [r for r in rows if (r[4] or 0) >= season and r[3] >= BORN_ACTIVE_MIN_PROMINENCE]
    if active and active[0][0] != rows[0][0]:
        picks.append(active[0])
    cards = []
    for pid, name, bdate, _, last in picks:
        is_active = (last or 0) >= season
        year = int(bdate[:4])
        built = _career_sentence(conn, pid, is_active)
        if not built:
            continue
        detail, teams = built
        headline = (f"{name} turns {today.year - year} today." if is_active
                    else f"{name} was born on this date in {year}.")
        cards.append({
            "headline": headline, "detail": detail,
            "category": "Born Today", "detection_type": BORN_TYPE,
            "game_context": "Born on this date", "player_names": [name],
            "team_names": teams, "priority": 2,
        })
    return cards


# ── Season in Review ───────────────────────────────────────────────────────
# Each family returns candidate cards with a notability score; the queue is
# ordered by score with family variety. Every claim is computed from the
# season tables (1871+) or game logs (1920+), aggregated per player-season
# so older multi-team rows count as one season.

BAT_COUNT = [("home_runs", "home runs", "hit"), ("hits", "hits", "collected"),
             ("rbi", "RBI", "drove in"), ("stolen_bases", "stolen bases", "stole"),
             ("doubles", "doubles", "hit"), ("triples", "triples", "hit"),
             ("walks", "walks", "drew"), ("runs", "runs", "scored")]
PIT_COUNT = [("strikeouts", "strikeouts", "struck out", "batters"),
             ("wins", "wins", "won", "games"), ("saves", "saves", "saved", "games")]


def _names(conn, pids):
    out = {}
    for pid in pids:
        r = conn.execute("SELECT name FROM players WHERE player_id = ?", (pid,)).fetchone()
        out[pid] = r[0] if r else pid
    return out


def _since_clause(conn, table, col, value, season, higher=True):
    """Last season (before `season`) where any player-season reached `value`.
    Returns (year, name, their_value) or None if it never happened."""
    cmp = ">=" if higher else "<="
    order = "DESC" if higher else "ASC"
    r = conn.execute(f"""
        SELECT season, player_id, v FROM (
            SELECT season, player_id, SUM({col}) v FROM {table}
            WHERE season < ? GROUP BY player_id, season)
        WHERE v {cmp} ? ORDER BY season DESC, v {order} LIMIT 1""",
        (season, value)).fetchone()
    if not r:
        return None
    return r[0], _names(conn, [r[1]])[r[1]], r[2]


def _alltime_rank(conn, table, col, value, season):
    """How many player-seasons ever (before `season`) beat `value` outright."""
    return conn.execute(f"""
        SELECT COUNT(*) FROM (SELECT SUM({col}) v FROM {table}
                              WHERE season < ? GROUP BY player_id, season) WHERE v > ?""",
        (season, value)).fetchone()[0]


def fam_count_leaders(conn, season):
    cards = []
    specs = [("season_batting_stats", c, noun, verb, None) for c, noun, verb in BAT_COUNT] + \
            [("season_pitching_stats", c, noun, verb, obj) for c, noun, verb, obj in PIT_COUNT]
    for table, col, noun, verb, obj in specs:
        top = conn.execute(f"""
            SELECT player_id, SUM({col}) v FROM {table} WHERE season = ?
            GROUP BY player_id ORDER BY v DESC LIMIT 2""", (season,)).fetchall()
        if not top or not top[0][1]:
            continue
        (pid, v), runner = top[0], (top[1] if len(top) > 1 else None)
        if runner and runner[1] == v:
            continue  # tied leaders: skip, the claim would need both names
        name = _names(conn, [pid])[pid]
        since = _since_clause(conn, table, col, v, season)
        beat = _alltime_rank(conn, table, col, v, season)
        gap = season - since[0] if since else 999
        if gap < 4 and beat > 40:
            continue  # an ordinary league lead isn't a review card
        verb_obj = f"{verb} {v} {obj}" if obj else f"{verb} {v} {noun}"
        if col == "wins":
            verb_obj = f"won {v} games"
        elif col == "saves":
            verb_obj = f"saved {v} games"
        headline = f"{name} {verb_obj} in {season}, most in the majors"
        if since is None:
            headline += f", and the most in a season in MLB history."
        elif since[2] == v:
            headline += f", and the most in a season since {since[1]} also had {v} in {since[0]}."
        else:
            headline += f", and the most in a season since {since[1]} had {since[2]} in {since[0]}."
        lead = runner[1] if runner else 0
        detail = f"The next-closest total in {season} was {lead}." if runner and v - lead >= 3 else ""
        if beat < 100:
            detail += (f" Only {beat} player-season{'s' if beat != 1 else ''} in history topped it."
                       if beat else " No one has ever topped it.")
        score = min(gap, 60) + max(0, 100 - beat) / 2
        cards.append(_review(headline, detail, [name], f"lead_{col}", score))
    return cards


def fam_rate_leaders(conn, season):
    cards = []
    games = conn.execute("""SELECT MAX(n) FROM (SELECT COUNT(*) n FROM team_game_results
        WHERE season = ? AND COALESCE(gametype,'regular') = 'regular' GROUP BY team)""",
        (season,)).fetchone()[0] or 162
    min_pa, min_outs = round(3.1 * games), games * 3
    # Batting average (qualified)
    r = conn.execute("""
        SELECT player_id, SUM(hits)*1.0/SUM(at_bats) a, SUM(plate_appearances) pa
        FROM season_batting_stats WHERE season = ? GROUP BY player_id
        HAVING pa >= ? ORDER BY a DESC LIMIT 1""", (season, min_pa)).fetchone()
    if r:
        pid, avg, _ = r
        prev = conn.execute("""
            SELECT season, player_id, a FROM (
              SELECT season, player_id, SUM(hits)*1.0/SUM(at_bats) a, SUM(at_bats)+SUM(walks) pa
              FROM season_batting_stats WHERE season < ? AND season >= 1901
              GROUP BY player_id, season HAVING pa >= 3.1*154)
            WHERE a >= ? ORDER BY season DESC LIMIT 1""", (season, round(avg, 3) - 0.0005)).fetchone()
        name = _names(conn, [pid])[pid]
        gap = season - prev[0] if prev else 999
        if gap >= 4:
            pn = _names(conn, [prev[1]])[prev[1]] if prev else None
            h = (f"{name} hit {fmt_rate(avg)} in {season} to win the batting title, the highest qualified "
                 f"average since {pn} hit {fmt_rate(prev[2])} in {prev[0]}." if prev else
                 f"{name} hit {fmt_rate(avg)} in {season}, the highest qualified average of the modern era.")
            cards.append(_review(h, "", [name], "lead_avg", min(gap, 60)))
    # ERA (qualified)
    r = conn.execute("""
        SELECT player_id, SUM(earned_runs)*27.0/SUM(ip_outs) e FROM season_pitching_stats
        WHERE season = ? GROUP BY player_id HAVING SUM(ip_outs) >= ? ORDER BY e ASC LIMIT 1""",
        (season, min_outs)).fetchone()
    if r:
        pid, era = r
        prev = conn.execute("""
            SELECT season, player_id, e FROM (
              SELECT season, player_id, SUM(earned_runs)*27.0/SUM(ip_outs) e FROM season_pitching_stats
              WHERE season < ? AND season >= 1920 GROUP BY player_id, season HAVING SUM(ip_outs) >= 162*3)
            WHERE e <= ? ORDER BY season DESC LIMIT 1""", (season, round(era, 2) + 0.005)).fetchone()
        name = _names(conn, [pid])[pid]
        gap = season - prev[0] if prev else 999
        if gap >= 4:
            pn = _names(conn, [prev[1]])[prev[1]] if prev else None
            h = (f"{name} posted a {era:.2f} ERA in {season} to lead the majors, the lowest by a qualified "
                 f"pitcher since {pn}'s {prev[2]:.2f} in {prev[0]}." if prev else
                 f"{name} posted a {era:.2f} ERA in {season}, the lowest by a qualified pitcher since 1920.")
            cards.append(_review(h, "", [name], "lead_era", min(gap, 60)))
    return cards


def fam_clubs(conn, season):
    """Power-speed and big-round-number seasons, with how rare the club is."""
    cards = []
    clubs = [
        ("HR and SB", "home_runs", "stolen_bases", [(50, 50), (40, 40), (30, 30)]),
    ]
    for label, a, b, tiers in clubs:
        for ta, tb in tiers:
            rows = conn.execute(f"""
                SELECT player_id, SUM({a}), SUM({b}) FROM season_batting_stats WHERE season = ?
                GROUP BY player_id HAVING SUM({a}) >= ? AND SUM({b}) >= ?""",
                (season, ta, tb)).fetchall()
            if not rows:
                continue
            before = conn.execute(f"""
                SELECT COUNT(*) FROM (SELECT SUM({a}) x, SUM({b}) y FROM season_batting_stats
                WHERE season < ? GROUP BY player_id, season) WHERE x >= ? AND y >= ?""",
                (season, ta, tb)).fetchone()[0]
            if ta == 30 and before > 50:
                continue  # 30-30 is common enough to skip unless rare
            for pid, x, y in rows:
                name = _names(conn, [pid])[pid]
                h = (f"{name} hit {x} home runs and stole {y} bases in {season}, "
                     f"joining the {ta}-{tb} club.")
                d = (f"It's happened only {before} other time{'s' if before != 1 else ''} in MLB history."
                     if before < 60 else "")
                cards.append(_review(h, d, [name], f"club_{ta}", 60 - min(before, 55)))
            break  # report the highest tier reached only
    return cards


def fam_month(conn, season):
    cards = []
    months = {3: "March", 4: "April", 5: "May", 6: "June", 7: "July", 8: "August", 9: "September", 10: "October"}
    for col, noun in [("home_runs", "home runs"), ("hits", "hits"), ("stolen_bases", "stolen bases")]:
        r = conn.execute(f"""
            SELECT player_id, month, {col} FROM monthly_batting_aggregates
            WHERE season = ? ORDER BY {col} DESC LIMIT 2""", (season,)).fetchall()
        if not r or (len(r) > 1 and r[0][2] == r[1][2]):
            continue
        pid, m, v = r[0]
        prev = conn.execute(f"""
            SELECT season, month, player_id, {col} FROM monthly_batting_aggregates
            WHERE season < ? AND {col} >= ? ORDER BY season DESC, month DESC LIMIT 1""",
            (season, v)).fetchone()
        gap = season - prev[0] if prev else 999
        name = _names(conn, [pid])[pid]
        h = f"{name}'s {v} {noun} in {months.get(m, m)} were the most by any player in a single month in {season}"
        if gap < 5:
            cards.append(_review(h + ".", "", [name], f"month_{col}", 8))
            continue
        if prev:
            pn = _names(conn, [prev[2]])[prev[2]]
            h += (f", and the most in a month since {pn} had {prev[3]} in "
                  f"{months.get(prev[1], prev[1])} {prev[0]}.")
        else:
            h += ", and the most in a month since at least 1920."
        cards.append(_review(h, "", [name], f"month_{col}", min(gap, 50)))
    return cards


def fam_single_game(conn, season):
    cards = []
    r = conn.execute("""SELECT player_id, date, strikeouts FROM game_pitching_logs
        WHERE season = ? ORDER BY strikeouts DESC, date LIMIT 2""", (season,)).fetchall()
    if r and not (len(r) > 1 and r[0][2] == r[1][2]):
        pid, d, k = r[0]
        prev = conn.execute("""SELECT season, player_id, strikeouts FROM game_pitching_logs
            WHERE season < ? AND strikeouts >= ? ORDER BY date DESC LIMIT 1""", (season, k)).fetchone()
        gap = season - prev[0] if prev else 999
        name = _names(conn, [pid])[pid]
        when = datetime.strptime(d, "%Y-%m-%d").strftime("%B %-d")
        h = f"{name} struck out {k} on {when}, the most in a game in {season}"
        if prev and gap >= 3:
            pn = _names(conn, [prev[1]])[prev[1]]
            h += f", and the most since {pn} struck out {prev[2]} in {prev[0]}."
        else:
            h += "."
        cards.append(_review(h, "", [name], "game_k", 10 + min(gap, 40)))
    multi = conn.execute("""SELECT player_id, date, home_runs FROM game_batting_logs
        WHERE season = ? AND home_runs >= 3 ORDER BY home_runs DESC, date""", (season,)).fetchall()
    if multi and multi[0][2] >= 4:
        pid, d, n = multi[0]
        name = _names(conn, [pid])[pid]
        ever = conn.execute("SELECT COUNT(*) FROM game_batting_logs WHERE home_runs >= 4 AND season < ?",
                            (season,)).fetchone()[0]
        when = datetime.strptime(d, "%Y-%m-%d").strftime("%B %-d")
        cards.append(_review(f"{name} hit {n} home runs in one game on {when}, the only {n}-homer game of {season}.",
                             f"It's been done {ever} times since 1920.", [name], "game_hr", 55))
    return cards


def fam_streaks(conn, season):
    cards = []
    r = conn.execute("""SELECT player_name, length, start_date, end_date FROM historical_streaks
        WHERE streak_type = 'hitting' AND end_season = ? ORDER BY length DESC LIMIT 2""",
        (season,)).fetchall()
    if r and not (len(r) > 1 and r[0][1] == r[1][1]):
        name, n, s, e = r[0]
        longer = conn.execute("""SELECT COUNT(*) FROM historical_streaks
            WHERE streak_type = 'hitting' AND length > ? AND end_season >= ?""", (n, season - 10)).fetchone()[0]
        sd = datetime.strptime(s, "%Y-%m-%d")
        ed = datetime.strptime(e, "%Y-%m-%d")
        if sd.year == ed.year:
            h = (f"{name} hit safely in {n} straight games from {sd.strftime('%B %-d')} to "
                 f"{ed.strftime('%B %-d')}, the longest hitting streak of {season}.")
        else:
            h = (f"{name} hit safely in {n} straight games, a streak that began {sd.strftime('%B %-d, %Y')} "
                 f"and ran to {ed.strftime('%B %-d')}, the longest hitting streak of {season}.")
        d = (f"Only {longer} longer streak{'s' if longer != 1 else ''} in the last decade." if longer < 10 else "")
        cards.append(_review(h, d, [name], "streak_hit", 25 + max(0, n - 20) * 2))
    return cards


def fam_team(conn, season):
    cards = []
    rows = conn.execute("""SELECT team, SUM(result='W'), SUM(result='L') FROM team_game_results
        WHERE season = ? AND COALESCE(gametype,'regular') = 'regular' GROUP BY team
        ORDER BY 2 DESC LIMIT 2""", (season,)).fetchall()
    if rows and not (len(rows) > 1 and rows[0][1] == rows[1][1]):
        t, w, l = rows[0]
        prev = conn.execute("""SELECT season, team, w FROM (
              SELECT season, team, SUM(result='W') w FROM team_game_results
              WHERE season < ? AND COALESCE(gametype,'regular') = 'regular' GROUP BY season, team)
            WHERE w >= ? ORDER BY season DESC LIMIT 1""", (season, w)).fetchone()
        tn = team_name(t) or t
        h = f"The {tn} went {w}-{l} in {season}, the best record in baseball"
        if prev and season - prev[0] >= 3:
            h += f", and the most wins since the {prev[0]} {team_name(prev[1]) or prev[1]} won {prev[2]}."
            score = 20 + min(season - prev[0], 40)
        else:
            h += "."
            score = 18
        cards.append(_review(h, "", [], "team_best", score, teams=[tn]))
    # longest winning streak
    seq = conn.execute("""SELECT team, date, game_number, result FROM team_game_results
        WHERE season = ? AND COALESCE(gametype,'regular') = 'regular' ORDER BY team, date, game_number""",
        (season,)).fetchall()
    best = (0, None, None, None)
    cur_team, run, start = None, 0, None
    for t, d, _, res in seq:
        if t != cur_team:
            cur_team, run = t, 0
        if res == "W":
            run += 1
            if run == 1:
                start = d
            if run > best[0]:
                best = (run, t, start, d)
        else:
            run = 0
    if best[0] >= 10:
        n, t, s, e = best
        tn = team_name(t) or t
        sd, ed = (datetime.strptime(x, "%Y-%m-%d") for x in (s, e))
        cards.append(_review(
            f"The {tn} won {n} straight games from {sd.strftime('%B %-d')} to {ed.strftime('%B %-d')}, "
            f"the longest winning streak of {season}.", "", [], "team_streak", 15 + (n - 10) * 3, teams=[tn]))
    return cards


def fam_plain_leaders(conn, season):
    """The season's leaders, for stats whose lead wasn't historic enough for
    fam_count_leaders (that family's card wins any overlap in build_review)."""
    cards = []
    specs = [("season_batting_stats", "home_runs", "home runs"), ("season_batting_stats", "rbi", "RBI"),
             ("season_batting_stats", "hits", "hits"), ("season_batting_stats", "stolen_bases", "stolen bases"),
             ("season_pitching_stats", "strikeouts", "strikeouts"), ("season_pitching_stats", "wins", "wins"),
             ("season_pitching_stats", "saves", "saves")]
    for table, col, noun in specs:
        top = conn.execute(f"""SELECT player_id, SUM({col}) v FROM {table} WHERE season = ?
            GROUP BY player_id ORDER BY v DESC LIMIT 2""", (season,)).fetchall()
        if len(top) < 2 or top[0][1] == top[1][1] or not top[0][1]:
            continue
        names = _names(conn, [top[0][0], top[1][0]])
        lead, nxt = top[0][1], top[1][1]
        h = f"{names[top[0][0]]} led the majors with {lead} {noun} in {season}."
        d = (f"{names[top[1][0]]} was next with {nxt}." if lead - nxt <= 2
             else f"That was {lead - nxt} more than anyone else; {names[top[1][0]]} was next with {nxt}.")
        cards.append(_review(h, d, [names[top[0][0]]], f"lead_{col}", 6 + min(lead - nxt, 6)))
    return cards


def fam_milestones(conn, season):
    """Career milestones reached during the season (historic_moments)."""
    cards = []
    nouns = {"home_runs": "home run", "hits": "hit", "rbi": "RBI", "stolen_bases": "stolen base",
             "strikeouts": "strikeout", "wins": "win", "saves": "save"}
    for pid, name, stat, thr, d, ctx in conn.execute("""SELECT player_id, player_name, stat, threshold,
            date, context FROM historic_moments WHERE season = ? ORDER BY date""", (season,)).fetchall():
        before = conn.execute("""SELECT COUNT(DISTINCT player_id) FROM historic_moments
            WHERE stat = ? AND threshold = ? AND date < ?""", (stat, thr, d)).fetchone()[0]
        when = datetime.strptime(d, "%Y-%m-%d").strftime("%B %-d")
        h = f"{name} {ctx} on {when}." if ctx else f"{name} reached {thr:,} career {nouns.get(stat, stat)}s on {when}."
        dd = f"He became the {ordinal(before + 1)} player ever to get there." if before < 40 else ""
        cards.append(_review(h, dd, [name], f"milestone_{stat}", 35 - min(before, 25) / 2))
    return cards


def fam_onbase_streak(conn, season):
    r = conn.execute("""SELECT player_name, length, start_date, end_date FROM historical_streaks
        WHERE streak_type = 'on_base' AND end_season = ? AND start_season = ?
        ORDER BY length DESC LIMIT 2""", (season, season)).fetchall()
    if not r or (len(r) > 1 and r[0][1] == r[1][1]):
        return []
    name, n, s, e = r[0]
    sd, ed = (datetime.strptime(x, "%Y-%m-%d") for x in (s, e))
    return [_review(f"{name} reached base in {n} straight games from {sd.strftime('%B %-d')} to "
                    f"{ed.strftime('%B %-d')}, the longest on-base streak of {season}.", "",
                    [name], "streak_ob", 14 + max(0, n - 30))]


def fam_first_year(conn, season):
    """Leaders among players in their first MLB season (no earlier batting or
    pitching rows). Deliberately not called 'rookies': MLB's rookie limits
    (130 AB / 50 IP) don't map onto first-season-in-the-DB."""
    cards = []
    first = """player_id NOT IN (SELECT player_id FROM season_batting_stats WHERE season < :s)
               AND player_id NOT IN (SELECT player_id FROM season_pitching_stats WHERE season < :s)"""
    for table, col, noun in [("season_batting_stats", "home_runs", "home runs"),
                             ("season_batting_stats", "hits", "hits"),
                             ("season_pitching_stats", "strikeouts", "strikeouts")]:
        top = conn.execute(f"""SELECT player_id, SUM({col}) v FROM {table} WHERE season = :s AND {first}
            GROUP BY player_id ORDER BY v DESC LIMIT 2""", {"s": season}).fetchall()
        if len(top) < 2 or top[0][1] == top[1][1] or (top[0][1] or 0) < 10:
            continue
        nm = _names(conn, [top[0][0], top[1][0]])
        cards.append(_review(
            f"{nm[top[0][0]]} led all first-year players with {top[0][1]} {noun} in {season}.",
            f"{nm[top[1][0]]} was next with {top[1][1]}.", [nm[top[0][0]]], f"first_{col}", 12))
    return cards


def fam_big_games(conn, season):
    """Rare single-game lines: 6-hit games, 9+ RBI games."""
    cards = []
    for col, thr, noun in [("hits", 6, "hits"), ("rbi", 9, "RBI")]:
        for pid, d, v in conn.execute(f"""SELECT player_id, date, {col} FROM game_batting_logs
                WHERE season = ? AND {col} >= ? ORDER BY {col} DESC, date""", (season, thr)).fetchall():
            name = _names(conn, [pid])[pid]
            since = conn.execute(f"""SELECT COUNT(*) FROM game_batting_logs WHERE {col} >= ? AND season < ?
                                     AND season >= ?""", (v, season, season - 10)).fetchone()[0]
            when = datetime.strptime(d, "%Y-%m-%d").strftime("%B %-d")
            cards.append(_review(f"{name} had {v} {noun} in one game on {when}.",
                                 f"There were {since} such games in the previous ten seasons." if since else
                                 "No one had done it in the previous ten seasons.",
                                 [name], f"game_{col}", 22 + (10 - min(since, 10))))
    return cards


def _review(headline, detail, players, family, score, teams=None):
    return {"headline": headline, "detail": detail, "category": "Season in Review",
            "detection_type": REVIEW_TYPE, "player_names": players,
            "team_names": teams or [], "family": family, "score": round(score, 1), "priority": 2}


FAMILIES = [fam_count_leaders, fam_rate_leaders, fam_clubs, fam_month,
            fam_single_game, fam_streaks, fam_team, fam_plain_leaders, fam_milestones,
            fam_onbase_streak, fam_first_year, fam_big_games]


def build_review(conn, season):
    cards = []
    for fam in FAMILIES:
        try:
            cards.extend(fam(conn, season))
        except Exception as e:
            print(f"  {fam.__name__} failed: {e}")
    # A plain leader card yields to a historic one for the same stat.
    best = {}
    for c in sorted(cards, key=lambda c: -c["score"]):
        best.setdefault(c["family"], c)
    cards = [c for c in cards if not c["family"].startswith("lead_") or best[c["family"]] is c]
    # Order: best first, but never the same family or player twice in a row
    # within a 3-card window.
    pool = sorted(cards, key=lambda c: -c["score"])
    ordered = []
    while pool:
        recent = ordered[-3:]
        pick = next((c for c in pool if all(
            c["family"].split("_")[0] != r["family"].split("_")[0] or c["family"] != r["family"]
            for r in recent) and not any(set(c["player_names"]) & set(r["player_names"]) for r in recent)),
            pool[0])
        ordered.append(pick)
        pool.remove(pick)
    return ordered


def ensure_queue(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS offseason_review_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT, season INTEGER NOT NULL, position INTEGER NOT NULL,
        family TEXT, score REAL, card TEXT NOT NULL, published_date TEXT,
        UNIQUE(season, position))""")


def store_queue(conn, season, cards):
    ensure_queue(conn)
    conn.execute("DELETE FROM offseason_review_queue WHERE season = ? AND published_date IS NULL", (season,))
    start = conn.execute("SELECT COALESCE(MAX(position), 0) FROM offseason_review_queue WHERE season = ?",
                         (season,)).fetchone()[0]
    published = {json.loads(r[0])["headline"] for r in conn.execute(
        "SELECT card FROM offseason_review_queue WHERE season = ? AND published_date IS NOT NULL", (season,))}
    n = 0
    for c in cards:
        if c["headline"] in published:
            continue
        n += 1
        conn.execute("INSERT INTO offseason_review_queue (season, position, family, score, card) VALUES (?,?,?,?,?)",
                     (season, start + n, c["family"], c["score"], json.dumps(c)))
    conn.commit()
    return n


# ── Publish ───────────────────────────────────────────────────────────────

def _flag(conn, key):
    r = conn.execute("SELECT updated_at FROM data_freshness WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def final_season(conn):
    r = conn.execute("""SELECT MAX(CAST(season AS INTEGER)) FROM data_freshness
                        WHERE key LIKE 'regular_final_%'""").fetchone()
    return r[0] if r and r[0] else None


def insert_event(conn, card, game_date):
    conn.execute("""INSERT OR IGNORE INTO notable_events
        (headline, detail, category, game_date, player_names, team_names,
         detection_type, priority, game_context, expires_at)
        VALUES (?,?,?,?,?,?,?,?,?,'')""",
        (card["headline"], card.get("detail", "").strip(), card["category"], game_date,
         json.dumps(card["player_names"]), json.dumps(card["team_names"]),
         card["detection_type"], card.get("priority", 2), card.get("game_context", "")))


def publish(conn, today):
    phase = _flag(conn, "season_phase")
    if phase not in OFF_PHASES:
        print(f"season_phase={phase!r}: in season, offseason feed idle.")
        return
    season = final_season(conn)
    gd = today.isoformat()
    for c in born_today_cards(conn, today, season or today.year):
        insert_event(conn, c, gd)
        print(f"  born_today: {c['headline']}")
    if season:
        ensure_queue(conn)
        done_today = conn.execute(
            "SELECT 1 FROM offseason_review_queue WHERE published_date = ?", (gd,)).fetchone()
        if not done_today:
            today_players = {p for (pn,) in conn.execute(
                "SELECT player_names FROM notable_events WHERE game_date = ?", (gd,)) for p in json.loads(pn or "[]")}
            nxt = None
            for qid, card in conn.execute("""SELECT id, card FROM offseason_review_queue
                    WHERE season = ? AND published_date IS NULL ORDER BY position LIMIT 5""", (season,)):
                c = json.loads(card)
                if not set(c["player_names"]) & today_players:  # the feed API would merge same-player cards
                    nxt = (qid, c)
                    break
            if nxt:
                qid, c = nxt
                c["game_context"] = f"{season} Season in Review"
                insert_event(conn, c, gd)
                conn.execute("UPDATE offseason_review_queue SET published_date = ? WHERE id = ?", (gd, qid))
                print(f"  season_review: {c['headline']}")
            else:
                print("  season_review: queue empty")
    cutoff = (today - timedelta(days=RETENTION_DAYS)).isoformat()
    conn.execute("DELETE FROM notable_events WHERE detection_type IN (?, ?) AND game_date < ?",
                 (BORN_TYPE, REVIEW_TYPE, cutoff))
    _set = "INSERT OR REPLACE INTO data_freshness (key, updated_at, season) VALUES ('offseason_feed_last', ?, ?)"
    conn.execute(_set, (gd, str(season or "")))
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--build-queue", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--date", help="override today (YYYY-MM-DD) for preview")
    a = ap.parse_args()
    conn = sqlite3.connect(a.db, timeout=600)
    today = date.fromisoformat(a.date) if a.date else et_today()
    season = final_season(conn)
    if a.preview:
        out = {"today": today.isoformat(), "season": season,
               "born_today": born_today_cards(conn, today, season or today.year),
               "review": build_review(conn, season) if season else []}
        print(json.dumps(out, indent=1, ensure_ascii=False))
    if a.build_queue:
        if not season:
            print("No finalized season yet.")
        else:
            print(f"Queued {store_queue(conn, season, build_review(conn, season))} review cards for {season}")
    if a.publish:
        publish(conn, today)
    conn.close()


if __name__ == "__main__":
    main()
