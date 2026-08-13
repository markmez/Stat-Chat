"""Trend & discovery engines for the feed (2026-08-12).

Three reference-class engines behind one principle — a claim is
interesting when its reference class is nearly empty:
  - league-today:  shape-free cell scan (player x context x stat x
    game-count window) scored by shrinkage + percentile against the
    league's own distribution; uniqueness claims (cardinality search).
  - own-career/season: open-ended change-point edges on a story's OWN
    per-game series (the profile slider's current-form logic) — this is
    ALSO the feed's hot-streak window finder now (replaces the PELT-table
    windows, whose season-long segments were why hot-streak cards rarely
    fired).
  - history: Pareto-dominance claim search against historical_index
    shelves ("the only pitcher since 1920 to match that line: ...").

All sentences are deterministic templates (house rule: no LLM rewrites).
State (novelty memory, claim state, drought state) lives in trend_state;
every scan candidate is logged to trend_candidates so the pick floor is
auditable (same philosophy as plan_dims_dropped).

Design log: memory project-trend-discovery-engine.md; sandbox backtest
(6 iterations with Mark) at the artifact URL recorded there.
"""
import json
import math
import sqlite3
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Shared config
# ---------------------------------------------------------------------------

K_SHRINK = 35          # PA pseudo-count toward the population mean
DAILY_TREND_CAP = 3    # trend cards per day
PLAYER_COOLDOWN_D = 7  # one trend feature per player per week
REFIRE_OPS_DELTA = 0.075  # a told story refires only if it grows this much

# context -> (family, [splits] or None=overall game logs, display label,
#             story family, is_specific_pitch)
CONTEXTS = {
    "overall":   (None, None, "overall", "form", False),
    "vs_LHP":    ("platoon", ["vs_LHP"], "vs lefties", "platoon", False),
    "vs_RHP":    ("platoon", ["vs_RHP"], "vs righties", "platoon", False),
    "RISP":      ("risp", ["RISP"], "with RISP", "risp", False),
    "fastballs": ("pitch_type", ["4-Seam", "2-Seam", "Sinker", "Cutter"], "vs fastballs", "pitch", False),
    "breaking":  ("pitch_type", ["Slider", "Curve"], "vs breaking balls", "pitch", False),
    "offspeed":  ("pitch_type", ["Change", "Split"], "vs offspeed", "pitch", False),
    "4-Seam":    ("pitch_type", ["4-Seam"], "vs four-seamers", "pitch", True),
    "Sinker":    ("pitch_type", ["Sinker"], "vs sinkers", "pitch", True),
    "Cutter":    ("pitch_type", ["Cutter"], "vs cutters", "pitch", True),
    "Slider":    ("pitch_type", ["Slider"], "vs sliders", "pitch", True),
    "Curve":     ("pitch_type", ["Curve"], "vs curveballs", "pitch", True),
    "Change":    ("pitch_type", ["Change"], "vs changeups", "pitch", True),
}
GROUP_OF = {"4-Seam": "fastballs", "2-Seam": "fastballs", "Sinker": "fastballs",
            "Cutter": "fastballs", "Slider": "breaking", "Curve": "breaking",
            "Change": "offspeed", "Split": "offspeed"}

OPS_EXPR = ("1.0*SUM(hits+walks+COALESCE(hit_by_pitch,0))"
            "/NULLIF(SUM(at_bats+walks+COALESCE(hit_by_pitch,0)+COALESCE(sacrifice_flies,0)),0)"
            " + 1.0*SUM(hits+doubles+2*triples+3*home_runs)/NULLIF(SUM(at_bats),0)")


# ---------------------------------------------------------------------------
# State + candidate log
# ---------------------------------------------------------------------------

def ensure_trend_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS trend_state (
        key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS trend_candidates (
        day TEXT, player TEXT, ctx TEXT, n_games INTEGER, stat TEXT,
        value REAL, pa INTEGER, pctile REAL, season_value REAL,
        score REAL, status TEXT, reason TEXT)""")
    conn.commit()


def _state(conn, key, default=None):
    row = conn.execute("SELECT value FROM trend_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _set_state(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO trend_state (key, value, updated_at) VALUES (?, ?, ?)",
                 (key, json.dumps(value), datetime.utcnow().isoformat()))


# ---------------------------------------------------------------------------
# Series + open-ended edge detection (shared with feed hot-streak cards)
# ---------------------------------------------------------------------------

def _series(conn, player_id, ctx, season, hi, limit=110):
    """Per-game component rows for a story's own series, newest first."""
    fam, splits, _, _, _ = CONTEXTS[ctx]
    if fam is None:
        sql = ("SELECT date, SUM(plate_appearances), SUM(at_bats), SUM(hits), SUM(doubles),"
               " SUM(triples), SUM(home_runs), SUM(walks), SUM(COALESCE(hit_by_pitch,0)),"
               " SUM(COALESCE(sacrifice_flies,0)) FROM game_batting_logs"
               " WHERE player_id = ? AND season = ? AND date <= ?"
               " GROUP BY date ORDER BY date DESC LIMIT ?")
        return conn.execute(sql, (player_id, season, hi, limit)).fetchall()
    marks = ", ".join("?" * len(splits))
    sql = (f"SELECT date, SUM(plate_appearances), SUM(at_bats), SUM(hits), SUM(doubles),"
           f" SUM(triples), SUM(home_runs), SUM(walks), SUM(COALESCE(hit_by_pitch,0)),"
           f" SUM(COALESCE(sacrifice_flies,0)) FROM game_split_logs"
           f" WHERE player_id = ? AND season = ? AND family = ? AND perspective = 'bat'"
           f" AND split IN ({marks}) AND date <= ?"
           f" GROUP BY date ORDER BY date DESC LIMIT ?")
    return conn.execute(sql, (player_id, season, fam, *splits, hi, limit)).fetchall()


def _agg(rows):
    """(ops, pa) over component rows; ops None when no AB."""
    pa = sum(r[1] or 0 for r in rows)
    ab = sum(r[2] or 0 for r in rows)
    h = sum(r[3] or 0 for r in rows)
    d2 = sum(r[4] or 0 for r in rows)
    t3 = sum(r[5] or 0 for r in rows)
    hr = sum(r[6] or 0 for r in rows)
    bb = sum(r[7] or 0 for r in rows)
    hbp = sum(r[8] or 0 for r in rows)
    sf = sum(r[9] or 0 for r in rows)
    if not ab:
        return None, pa
    obp_den = ab + bb + hbp + sf
    obp = (h + bb + hbp) / obp_den if obp_den else 0
    slg = (h + d2 + 2 * t3 + 3 * hr) / ab
    return obp + slg, pa


def find_edge(rows, min_recent=5, min_prior=8):
    """Open-ended change-point on a per-game series (newest first).

    Picks the split k maximizing |recent - prior| * sqrt(harmonic PA).
    Returns (k_appearances, recent_ops, recent_pa, start_date) or None.
    Never anchored to a scan lookback — this is the slider's current-form
    logic, generalized (the fix for "asked near a window, returns the
    window" that Mark called out in the sandbox)."""
    best = None
    for k in range(min_recent, len(rows) - min_prior):
        r_ops, r_pa = _agg(rows[:k])
        p_ops, p_pa = _agg(rows[k:])
        if r_ops is None or p_ops is None or r_pa < 10 or p_pa < 20:
            continue
        w = math.sqrt(1.0 / (1.0 / r_pa + 1.0 / p_pa))
        contrast = abs(r_ops - p_ops) * w
        if best is None or contrast > best[0]:
            best = (contrast, k, r_ops, r_pa, rows[k - 1][0], p_ops)
    if best is None:
        return None
    _, k, r_ops, r_pa, start, p_ops = best
    return k, round(r_ops, 3), r_pa, start, round(p_ops, 3)


def _fmt(v):
    s = f"{v:.3f}"
    return s.lstrip("0") if v < 1 else s


def _md(datestr):
    return datetime.strptime(datestr, "%Y-%m-%d").strftime("%B %-d")


# ---------------------------------------------------------------------------
# Engine 1: league-today cell scan
# ---------------------------------------------------------------------------

def _cutoff_join(n, hi, alias):
    return (f"JOIN (SELECT player_id, MIN(date) cut FROM"
            f" (SELECT player_id, date, ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY date DESC) rn"
            f"  FROM (SELECT DISTINCT player_id, date FROM game_batting_logs"
            f"        WHERE season = ? AND date <= ?))"
            f" WHERE rn <= {int(n)} GROUP BY player_id) cf"
            f" ON cf.player_id = {alias}.player_id AND {alias}.date >= cf.cut")


def _cell_rows(conn, ctx, n_games, season, hi):
    fam, splits, _, _, _ = CONTEXTS[ctx]
    if fam is None:
        win_src = (f"SELECT g.player_id, SUM(plate_appearances) pa, {OPS_EXPR} v"
                   f" FROM game_batting_logs g {_cutoff_join(n_games, hi, 'g')}"
                   f" WHERE g.season = ? AND g.date <= ?"
                   f" GROUP BY g.player_id HAVING SUM(plate_appearances) >= {n_games * 2}")
        season_src = (f"SELECT player_id, {OPS_EXPR} sv FROM game_batting_logs"
                      f" WHERE season = ? AND date <= ? GROUP BY player_id"
                      f" HAVING SUM(plate_appearances) >= 100")
        params = (season, hi, season, hi, season, hi)
    else:
        inlist = ", ".join(f"'{s}'" for s in splits)
        win_src = (f"SELECT g.player_id, SUM(plate_appearances) pa, {OPS_EXPR} v"
                   f" FROM game_split_logs g {_cutoff_join(n_games, hi, 'g')}"
                   f" WHERE g.season = ? AND g.family = '{fam}' AND g.perspective = 'bat'"
                   f" AND g.split IN ({inlist}) AND g.date <= ?"
                   f" GROUP BY g.player_id HAVING SUM(plate_appearances) >= {max(12, n_games)}")
        season_src = (f"SELECT player_id, {OPS_EXPR} sv FROM game_split_logs"
                      f" WHERE season = ? AND family = '{fam}' AND perspective = 'bat'"
                      f" AND split IN ({inlist}) AND date <= ?"
                      f" GROUP BY player_id HAVING SUM(plate_appearances) >= 40")
        params = (season, hi, season, hi, season, hi)
    sql = f"""SELECT w.player_id, p.name, ROUND(w.v,3), w.pa, ROUND(w.pr,3), ROUND(s.sv,3)
    FROM (SELECT player_id, v, pa, PERCENT_RANK() OVER (ORDER BY shrunk) pr
          FROM (SELECT player_id, v, pa, (v*pa + AVG(v) OVER ()*{K_SHRINK})/(pa+{K_SHRINK}) shrunk
                FROM ({win_src}))) w
    JOIN ({season_src}) s ON s.player_id = w.player_id
    JOIN players p ON p.player_id = w.player_id
    WHERE w.pr >= 0.975 OR w.pr <= 0.015
    ORDER BY w.pr DESC LIMIT 12"""
    return conn.execute(sql, params).fetchall()


def detect_trend_cells(conn, season, latest_date):
    """League-today scan -> pick -> edge-narrated deterministic cards."""
    ensure_trend_tables(conn)
    hi = latest_date
    prom = {r[0]: min((r[1] or 0) / 450.0, 1.0) for r in conn.execute(
        "SELECT player_id, MAX(plate_appearances) FROM season_batting_stats"
        " WHERE season = ? GROUP BY player_id", (season,))}

    raw = []
    for ctx, (fam, _s, _l, family, specific) in CONTEXTS.items():
        for n in ((10, 20, 40) if fam is None else (20, 40)):
            try:
                rows = _cell_rows(conn, ctx, n, season, hi)
            except sqlite3.OperationalError:
                continue
            for pid, name, v, pa, pr, sv in rows:
                if v is None or sv is None:
                    continue
                ext = max(pr - 0.5, 0.5 - pr) * 2
                score = ((ext ** 3) * 2.0 + min(abs(v - sv) / 0.400, 1.0) * 1.2
                         + min(pa / 80.0, 1.0) * 0.5 + prom.get(pid, 0.0) * 1.0
                         - (0.15 if specific else 0.0))
                raw.append({"pid": pid, "player": name, "ctx": ctx, "n": n, "v": v,
                            "pa": pa, "pr": pr, "sv": sv, "family": family,
                            "specific": specific, "score": score,
                            "direction": "high" if pr >= 0.5 else "low"})

    # collapse: one cell per player
    best = {}
    for c in raw:
        if c["pid"] not in best or c["score"] > best[c["pid"]]["score"]:
            best[c["pid"]] = c
    cands = sorted(best.values(), key=lambda c: -c["score"])

    fam_last = _state(conn, "trend_family_last", {})
    player_last = _state(conn, "trend_player_last", {})
    stories = _state(conn, "trend_stories", {})
    D = datetime.strptime(hi, "%Y-%m-%d").date()

    events, fams_today, n_picked = [], set(), 0
    for c in cands:
        status, reason = "picked", None
        story_key = f"{c['pid']}|{c['ctx']}|{c['direction']}"
        last_fired = stories.get(story_key)
        pen = 0.0
        lf = fam_last.get(c["family"])
        if lf:
            gap = (D - date.fromisoformat(lf)).days
            pen = 0.5 if gap <= 1 else (0.25 if gap == 2 else 0.0)
        if n_picked >= DAILY_TREND_CAP:
            status, reason = "passed", "daily cap"
        elif c["family"] in fams_today:
            status, reason = "passed", "family variety"
        elif player_last.get(c["pid"]) and (D - date.fromisoformat(player_last[c["pid"]])).days < PLAYER_COOLDOWN_D:
            status, reason = "passed", "player cooldown"
        elif last_fired and abs(c["v"] - last_fired.get("v", 0)) < REFIRE_OPS_DELTA:
            status, reason = "passed", "story already told"
        elif c["score"] - pen < 2.2:
            status, reason = "passed", f"below bar (pen {pen})"
        conn.execute("INSERT INTO trend_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (hi, c["player"], c["ctx"], c["n"], "ops", c["v"], c["pa"],
                      round(c["pr"] * 100, 1), c["sv"], round(c["score"], 3), status, reason))
        if status != "picked":
            continue

        # specific pitch must beat its group head-to-head, else fold
        ctx = c["ctx"]
        if c["specific"]:
            grp = GROUP_OF[ctx]
            g_rows = _series(conn, c["pid"], grp, season, hi)[:c["n"]]
            g_ops, g_pa = _agg(g_rows) if g_rows else (None, 0)
            if g_ops is None or abs(c["v"] - g_ops) < 0.200:
                ctx = grp
                c["v"], c["pa"] = (round(g_ops, 3), g_pa) if g_ops is not None else (c["v"], c["pa"])

        rows = _series(conn, c["pid"], ctx, season, hi)
        edge = find_edge(rows)
        if not edge:
            continue
        k, e_ops, e_pa, start, _prior = edge
        n_games = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM game_batting_logs"
            " WHERE player_id = ? AND season = ? AND date >= ? AND date <= ?",
            (c["pid"], season, start, hi)).fetchone()[0]
        label = CONTEXTS[ctx][2]
        hot = c["direction"] == "high"
        if ctx == "overall":
            before = _agg([r for r in rows if r[0] < start])[0]
            now = _agg(rows)[0]
            traj = ""
            if before and now:
                verb = "climbed" if hot else "slipped"
                traj = f" His season OPS has {verb} from {_fmt(before)} to {_fmt(now)} in that span."
            headline = (f"{c['player']}: a {_fmt(e_ops)} OPS over his last {n_games} games"
                        f" (since {_md(start)}).{traj}")
            detail = f"{e_pa} PA in the stretch."
        else:
            fold = " (all fastball types)" if (c["specific"] and ctx in GROUP_OF.values()) else ""
            headline = (f"{c['player']} {label}{fold} since {_md(start)}:"
                        f" a {_fmt(e_ops)} OPS over his last {n_games} games.")
            detail = f"{e_pa} PA in the split. Season mark {label}: {_fmt(c['sv'])}."
        events.append({
            "headline": headline, "detail": detail, "category": "Trend",
            "game_date": hi, "player_names": [c["player"]], "team_names": [],
            "detection_type": f"trend_{c['family']}", "priority": 2,
        })
        n_picked += 1
        fams_today.add(c["family"])
        fam_last[c["family"]] = hi
        player_last[c["pid"]] = hi
        stories[story_key] = {"v": c["v"], "day": hi}

    _set_state(conn, "trend_family_last", fam_last)
    _set_state(conn, "trend_player_last", player_last)
    _set_state(conn, "trend_stories", stories)
    conn.commit()
    return events


# ---------------------------------------------------------------------------
# Engine 1b: uniqueness claims (cardinality search, transition-fired)
# ---------------------------------------------------------------------------

def detect_uniqueness_claims(conn, season, latest_date):
    ensure_trend_tables(conn)
    hi = latest_date
    claims = {}

    hr = conn.execute(
        "SELECT p.name, SUM(b.home_runs) hr FROM game_batting_logs b"
        " JOIN players p ON p.player_id = b.player_id"
        " WHERE b.season = ? AND b.date <= ? GROUP BY b.player_id"
        " ORDER BY hr DESC LIMIT 5", (season, hi)).fetchall()
    for th in (50, 45, 40, 35, 30):
        if hr and sum(1 for _, v in hr if (v or 0) >= th) == 1:
            claims["hr_only"] = (
                f"{hr[0][0]} is the only player with {th}+ home runs this season.",
                f"{hr[0][0]}: {hr[0][1]} HR. Next closest: {hr[1][0]} with {hr[1][1]}.",
                [hr[0][0]])
            break

    sb = conn.execute(
        "SELECT p.name, SUM(b.stolen_bases) s FROM game_batting_logs b"
        " JOIN players p ON p.player_id = b.player_id"
        " WHERE b.season = ? AND b.date <= ? GROUP BY b.player_id"
        " ORDER BY s DESC LIMIT 5", (season, hi)).fetchall()
    for th in (70, 60, 50, 40):
        if sb and sum(1 for _, v in sb if (v or 0) >= th) == 1:
            claims["sb_only"] = (
                f"{sb[0][0]} is the only player with {th}+ stolen bases this season.",
                f"{sb[0][0]}: {sb[0][1]} SB. Next closest: {sb[1][0]} with {sb[1][1]}.",
                [sb[0][0]])
            break

    era = conn.execute(
        "SELECT p.name, ROUND(9.0*SUM(g.earned_runs)/NULLIF(SUM(g.ip_outs)/3.0,0),2) e"
        " FROM game_pitching_logs g JOIN players p ON p.player_id = g.player_id"
        " WHERE g.season = ? AND g.date <= ? GROUP BY g.player_id"
        " HAVING SUM(g.ip_outs) >= 300 ORDER BY e ASC LIMIT 5", (season, hi)).fetchall()
    for th in (1.75, 2.00, 2.25, 2.50):
        if era and sum(1 for _, v in era if v is not None and v < th) == 1:
            claims["era_only"] = (
                f"{era[0][0]} is the only qualified starter with an ERA under {th:.2f}.",
                f"{era[0][0]}: {era[0][1]} ERA. Next best: {era[1][0]} at {era[1][1]}.",
                [era[0][0]])
            break

    cats = conn.execute(
        f"SELECT p.name, ROUND({OPS_EXPR},3) o FROM game_batting_logs b"
        f" JOIN players p ON p.player_id = b.player_id"
        f" WHERE b.season = ? AND b.date <= ?"
        f" AND (p.positions = 'C' OR p.positions LIKE 'C/%'"
        f"      OR p.positions LIKE '%/C/%' OR p.positions LIKE '%/C')"
        f" GROUP BY b.player_id HAVING SUM(b.plate_appearances) >= 250"
        f" ORDER BY o DESC LIMIT 4", (season, hi)).fetchall()
    for th in (1.000, 0.950, 0.900, 0.850):
        if cats and sum(1 for _, v in cats if v is not None and v >= th) == 1:
            claims["catcher_only"] = (
                f"{cats[0][0]} is the only catcher with an OPS over {th:.3f} this season (min 250 PA).",
                f"{cats[0][0]}: {cats[0][1]} OPS. Next best catcher: {cats[1][0]} at {cats[1][1]}.",
                [cats[0][0]])
            break

    trio = conn.execute(
        "SELECT s.team, COUNT(*) cnt FROM"
        " (SELECT b.player_id, SUM(b.home_runs) hr FROM game_batting_logs b"
        "  WHERE b.season = ? AND b.date <= ? GROUP BY b.player_id HAVING hr >= 25) x"
        " JOIN season_batting_stats s ON s.player_id = x.player_id AND s.season = ?"
        " GROUP BY s.team HAVING cnt >= 3 ORDER BY cnt DESC LIMIT 3",
        (season, hi, season)).fetchall()
    if len(trio) == 1:
        from services.response_builder import _team_full_name
        try:
            tname = _team_full_name(trio[0][0].split("/")[0])
        except Exception:
            tname = trio[0][0]
        claims["team_hr_trio"] = (
            f"The {tname} are the only team with {trio[0][1]} players at 25+ home runs.",
            "No other club has more than two 25-homer hitters.", [])

    # Mark's rule (2026-08-12 QA): every feed card is pegged to the GAME
    # EVENT that made it true, and the event leads the sentence. A claim
    # that is true but unanchored (nobody did the triggering thing on
    # latest_date) stays UNFIRED — state records only fired claims, so it
    # keeps re-checking daily and fires the first day its subject anchors
    # it (e.g. the 3rd White Sox hitter's 25th HR, or the leader adding
    # HR #44 while still the only one at 40+).
    def _y(col, pid_expr, name):
        nm = name.replace("'", "''")
        row = conn.execute(
            f"SELECT SUM({col}) FROM game_batting_logs b JOIN players p"
            f" ON p.player_id = b.player_id WHERE p.name = '{nm}'"
            f" AND b.season = ? AND b.date = ?", (season, hi)).fetchone()
        return row[0] or 0

    def _ordinal(n):
        s = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{s}"

    anchored = {}
    if "hr_only" in claims:
        name, total = hr[0][0], hr[0][1]
        y = _y("home_runs", None, name)
        if y > 0:
            th = int(claims["hr_only"][0].split("only player with ")[1].split("+")[0])
            anchored["hr_only"] = (
                f"{name} hit his {_ordinal(total)} home run — he's the only player"
                f" with {th}+ this season.", claims["hr_only"][1], [name])
    if "sb_only" in claims:
        name, total = sb[0][0], sb[0][1]
        y = _y("stolen_bases", None, name)
        if y > 0:
            th = int(claims["sb_only"][0].split("only player with ")[1].split("+")[0])
            anchored["sb_only"] = (
                f"{name} stole his {_ordinal(total)} base — he's the only player"
                f" with {th}+ this season.", claims["sb_only"][1], [name])
    if "era_only" in claims:
        name = era[0][0]
        nm = name.replace("'", "''")
        st = conn.execute(
            f"SELECT SUM(strikeouts), SUM(earned_runs), SUM(ip_outs) FROM game_pitching_logs g"
            f" JOIN players p ON p.player_id = g.player_id WHERE p.name = '{nm}'"
            f" AND g.season = ? AND g.date = ? AND g.is_start = 1", (season, hi)).fetchone()
        if st and st[2]:
            ip = f"{st[2] // 3}.{st[2] % 3}" if st[2] % 3 else f"{st[2] // 3}"
            anchored["era_only"] = (
                f"{name} threw {ip} innings with {st[0]} strikeouts and {st[1]} earned"
                f" run{'s' if st[1] != 1 else ''} — he's the only qualified starter"
                f" with an ERA under {claims['era_only'][0].split('under ')[1].rstrip('.')}.",
                claims["era_only"][1], [name])
    if "catcher_only" in claims:
        name = cats[0][0]
        if _y("hits", None, name) + _y("walks", None, name) > 0:
            anchored["catcher_only"] = (
                claims["catcher_only"][0], claims["catcher_only"][1], [name])
    if "team_hr_trio" in claims:
        # anchor = a trio member crossed 25 HR on latest_date
        team_code = trio[0][0]
        members = conn.execute(
            "SELECT p.name, SUM(b.home_runs) hr FROM game_batting_logs b"
            " JOIN players p ON p.player_id = b.player_id"
            " JOIN season_batting_stats s ON s.player_id = b.player_id AND s.season = ?"
            " WHERE b.season = ? AND b.date <= ? AND s.team = ?"
            " GROUP BY b.player_id HAVING hr >= 25", (season, season, hi, team_code)).fetchall()
        for mname, mtotal in members:
            y = _y("home_runs", None, mname)
            if y > 0 and (mtotal - y) < 25 <= mtotal:
                base = claims["team_hr_trio"][0]
                base = base[0].lower() + base[1:].rstrip(".")
                anchored["team_hr_trio"] = (
                    f"{mname} hit his {_ordinal(mtotal)} home run, making {base}.",
                    claims["team_hr_trio"][1], [mname])
                break

    prev = _state(conn, "uniqueness_claims", {})
    events = []
    fired_state = dict(prev)
    for key, (headline, detail, players) in anchored.items():
        if prev.get(key) != claims[key][0]:  # not already fired for this claim text
            events.append({
                "headline": headline, "detail": detail, "category": "Only One",
                "game_date": hi, "player_names": players, "team_names": [],
                "detection_type": f"uniqueness_{key}", "priority": 2,
            })
            fired_state[key] = claims[key][0]
    # claims that turned false reset so a future re-anchor can fire again
    for key in list(fired_state):
        if key not in claims:
            del fired_state[key]
    _set_state(conn, "uniqueness_claims", fired_state)
    conn.commit()
    return events


# ---------------------------------------------------------------------------
# Engine 1c: HR drought watch + drought breaks
# ---------------------------------------------------------------------------

def detect_droughts(conn, season, latest_date):
    ensure_trend_tables(conn)
    hi = latest_date
    rows = conn.execute("""
        SELECT p.name, dr.player_id, dr.gp FROM
          (SELECT b.player_id,
                  SUM(CASE WHEN b.date > COALESCE(
                      (SELECT MAX(b2.date) FROM game_batting_logs b2
                       WHERE b2.player_id = b.player_id AND b2.season = ?
                         AND b2.home_runs > 0 AND b2.date <= ?), '1900-01-01')
                      THEN 1 ELSE 0 END) gp,
                  SUM(b.plate_appearances) pa
           FROM game_batting_logs b WHERE b.season = ? AND b.date <= ?
           GROUP BY b.player_id HAVING pa >= 350) dr
        JOIN players p ON p.player_id = dr.player_id
        WHERE dr.gp >= 30 ORDER BY dr.gp DESC LIMIT 12""",
        (season, hi, season, hi)).fetchall()
    cur = {pid: gp for _, pid, gp in rows}
    names = {pid: nm for nm, pid, _ in rows}
    prev = _state(conn, "hr_droughts", {})
    events = []

    for pid, gp in cur.items():
        if prev.get(pid, 0) < 40 <= gp:
            events.append({
                "headline": f"{names[pid]} has gone {gp} games without a home run.",
                "detail": "Longest active homerless streaks among everyday players.",
                "category": "Drought", "game_date": hi,
                "player_names": [names[pid]], "team_names": [],
                "detection_type": "hr_drought_watch", "priority": 3,
            })

    # breaks: in drought >= 30 as of yesterday's state, homered on latest_date
    for nm, pid, hr_today in conn.execute(
            "SELECT p.name, b.player_id, SUM(b.home_runs) FROM game_batting_logs b"
            " JOIN players p ON p.player_id = b.player_id"
            " WHERE b.season = ? AND b.date = ? GROUP BY b.player_id"
            " HAVING SUM(b.home_runs) > 0", (season, hi)).fetchall():
        gp_before = prev.get(pid)
        if gp_before and gp_before >= 30:
            plural = "s" if hr_today > 1 else ""
            events.append({
                "headline": (f"{nm} ended a {gp_before}-game homerless streak"
                             f" with {hr_today} home run{plural}."),
                "detail": "His first since before the drought began.",
                "category": "Drought Over", "game_date": hi,
                "player_names": [nm], "team_names": [],
                "detection_type": "hr_drought_break", "priority": 2,
            })

    _set_state(conn, "hr_droughts", cur)
    conn.commit()
    return events


# ---------------------------------------------------------------------------
# Engine 2: edge-based current form (replaces PELT-window hot streaks)
# ---------------------------------------------------------------------------

def detect_form_edges(conn, season, latest_date):
    """Feed hot/cold form cards with windows found by open-ended change-point
    on each player's own series — not the PELT table, whose season-long
    segments were why these cards rarely fired."""
    ensure_trend_tables(conn)
    hi = latest_date
    played = [r[0] for r in conn.execute(
        "SELECT DISTINCT b.player_id FROM game_batting_logs b"
        " JOIN season_batting_stats s ON s.player_id = b.player_id AND s.season = ?"
        " WHERE b.season = ? AND b.date = ? AND s.plate_appearances >= 200",
        (season, season, hi)).fetchall()]
    told = _state(conn, "form_stories", {})
    # Cross-engine dedup: a player featured by the trend scan (any family)
    # within the cooldown window doesn't also get a Current Form card —
    # Merrill drew both on day one.
    trend_last = _state(conn, "trend_player_last", {})
    D = datetime.strptime(hi, "%Y-%m-%d").date()
    played = [pid for pid in played
              if not (trend_last.get(pid)
                      and (D - date.fromisoformat(trend_last[pid])).days < PLAYER_COOLDOWN_D)]
    cands = []
    for pid in played:
        rows = _series(conn, pid, "overall", season, hi)
        edge = find_edge(rows, min_recent=7, min_prior=15)
        if not edge:
            continue
        k, e_ops, e_pa, start, prior = edge
        diff = e_ops - prior
        if abs(diff) < 0.250 or k > 35:
            continue
        prev_row = told.get(str(pid))
        if prev_row and prev_row.get("start") == start and abs(e_ops - prev_row.get("ops", 0)) < REFIRE_OPS_DELTA:
            continue
        cands.append((abs(diff) * math.sqrt(e_pa), pid, e_ops, e_pa, start, diff, k))
    cands.sort(reverse=True)
    events = []
    for _w, pid, e_ops, e_pa, start, diff, k in cands[:2]:
        name = conn.execute("SELECT name FROM players WHERE player_id = ?", (pid,)).fetchone()[0]
        n_games = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM game_batting_logs"
            " WHERE player_id = ? AND season = ? AND date >= ? AND date <= ?",
            (pid, season, start, hi)).fetchone()[0]
        hot = diff > 0
        word = "hottest" if hot else "coldest"
        headline = (f"{name}: a {_fmt(e_ops)} OPS over his last {n_games} games"
                    f" (since {_md(start)}).")
        detail = (f"One of the {word} stretches in baseball right now — "
                  f"{'+' if hot else ''}{int(round(diff * 1000))} points of OPS"
                  f" versus his play before it ({e_pa} PA).")
        events.append({
            "headline": headline, "detail": detail,
            "category": "Current Form", "game_date": hi,
            "player_names": [name], "team_names": [],
            "detection_type": "form_edge_hot" if hot else "form_edge_cold",
            "priority": 2,
        })
        told[str(pid)] = {"start": start, "ops": e_ops, "day": hi}
    _set_state(conn, "form_stories", told)
    conn.commit()
    return events


# ---------------------------------------------------------------------------
# Engine 3: history claim search on historical_index shelves
# ---------------------------------------------------------------------------

def detect_history_claims(conn, season, latest_date):
    """Pareto-dominance claim search: a pitcher's own first-N-starts line is
    the claim; company = historical player-seasons that match-or-beat it
    (>= K, <= BB). Fires the day the Nth start completes. No authored
    thresholds — interestingness is the emptiness of the reference class."""
    ensure_trend_tables(conn)
    hi = latest_date
    events = []
    told = _state(conn, "history_claims", {})

    starters_today = conn.execute(
        "SELECT DISTINCT player_id FROM game_pitching_logs"
        " WHERE season = ? AND date = ? AND is_start = 1", (season, hi)).fetchall()
    for (pid,) in starters_today:
        n_starts = conn.execute(
            "SELECT COUNT(*) FROM game_pitching_logs"
            " WHERE player_id = ? AND season = ? AND is_start = 1 AND date <= ?",
            (pid, season, hi)).fetchone()[0]
        if not (2 <= n_starts <= 25):
            continue
        shelf = f"pitcher_first_{n_starts}_starts"
        row = conn.execute(
            "SELECT player_name, value, value2, detail FROM historical_index"
            " WHERE scan_type = ? AND season = ? AND player_id = ? LIMIT 1",
            (shelf, season, pid)).fetchone()
        if not row:
            continue
        name, k, bb, detail_line = row
        # pre-filter: ~5 K/start is the floor of "maybe historic"; the
        # dominance count below is the real judge
        if (k or 0) < max(12, 5 * n_starts):
            continue
        story_key = f"{pid}|{shelf}"
        if told.get(story_key):
            continue
        comps = conn.execute(
            "SELECT DISTINCT player_name, season FROM historical_index"
            " WHERE scan_type = ? AND season < ? AND value >= ? AND value2 <= ?"
            " ORDER BY season", (shelf, season, k, bb)).fetchall()
        if len(comps) > 8:
            continue
        line = f"{k} strikeouts and {bb} walk{'s' if bb != 1 else ''} through his first {n_starts} starts"
        if not comps:
            headline = f"{name} has {line} — no pitcher since 1920 has matched that opening."
            detail = detail_line or ""
        elif len(comps) == 1:
            headline = (f"{name} has {line}. The only pitcher since 1920 to match it:"
                        f" {comps[0][0]}, {comps[0][1]}.")
            detail = detail_line or ""
        else:
            names_list = ", ".join(f"{n2} ({y})" for n2, y in comps[:5])
            headline = (f"{name} has {line} — matched by only {len(comps)}"
                        f" pitchers since 1920.")
            detail = f"The company: {names_list}."
        events.append({
            "headline": headline, "detail": detail, "category": "Rare Company",
            "game_date": hi, "player_names": [name] + [c[0] for c in comps[:3]],
            "team_names": [], "detection_type": f"history_claim_{shelf}",
            "priority": 1,
        })
        told[story_key] = hi

    # Batter short-burst combos vs the window shelves (the Acuña card):
    # window ends ON latest_date, so the completing game is the anchor.
    for pid, name in conn.execute(
            "SELECT b.player_id, p.name FROM game_batting_logs b"
            " JOIN players p ON p.player_id = b.player_id"
            " WHERE b.season = ? AND b.date = ? AND b.home_runs > 0",
            (season, hi)).fetchall():
        for K in (4, 6, 10):
            w = conn.execute(
                "SELECT SUM(home_runs), SUM(stolen_bases) FROM"
                " (SELECT home_runs, stolen_bases FROM game_batting_logs"
                "  WHERE player_id = ? AND season = ? AND date <= ?"
                "  ORDER BY date DESC LIMIT ?)", (pid, season, hi, K)).fetchone()
            hr_w, sb_w = w[0] or 0, w[1] or 0
            if hr_w < 4 or sb_w < 2:
                continue
            story_key = f"{pid}|w{K}hrsb|{hr_w}|{sb_w}"
            if told.get(story_key):
                continue
            comps = conn.execute(
                "SELECT DISTINCT player_name, season FROM historical_index"
                " WHERE scan_type = ? AND season < ? AND value >= ? AND value2 >= ?"
                " ORDER BY season", (f"window{K}_hr_sb", season, hr_w, sb_w)).fetchall()
            if len(comps) > 6:
                continue
            if comps:
                names_list = ", ".join(f"{n2} ({y})" for n2, y in comps[:5])
                headline = (f"With {hr_w} home runs and {sb_w} steals in his last {K}"
                            f" games, {name} joins {names_list} as the only players"
                            f" since 1920 to do it in a {K}-game span.")
            else:
                headline = (f"{name} has {hr_w} home runs and {sb_w} steals in his"
                            f" last {K} games — no player since 1920 has matched"
                            f" that in a {K}-game span.")
            events.append({
                "headline": headline, "detail": "", "category": "Rare Company",
                "game_date": hi, "player_names": [name] + [c[0] for c in comps[:3]],
                "team_names": [], "detection_type": f"history_claim_window{K}_hr_sb",
                "priority": 1,
            })
            told[story_key] = hi
            break  # one burst card per player per day (smallest qualifying K wins)

    _set_state(conn, "history_claims", told)
    conn.commit()
    return events
