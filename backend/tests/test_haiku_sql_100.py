"""
100-query Haiku SQL stress test.

Covers every query shape: leaderboards, comparisons, career aggregates,
year-over-year, position filters, rookies, splits, pitching, thresholds,
milestones, team-level, derivable stats, age-based, and long-tail analytics.

Run: cd backend && python3 tests/test_haiku_sql_100.py
"""

import os
import sys
import sqlite3
import json
import re
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import anthropic
from prompts import HAIKU_SQL_PROMPT

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "baseball_stats.db",
)

client = anthropic.Anthropic()

QUERIES = {
    # ── TIER 1: Simple lookups & leaderboards (20) ──────────────────────
    "Simple lookups & leaderboards": [
        "Who led the league in home runs in 2024?",
        "Top 10 in batting average in 2024",
        "Who had the most RBI in 2025?",
        "Top 5 in OPS last season",
        "Who had the most stolen bases in 2024?",
        "Aaron Judge 2024 stats",
        "Juan Soto career stats",
        "Who struck out the most as a batter in 2024?",
        "Top 5 in walks in 2024",
        "Who had the most doubles in 2024?",
        "Most triples in a season since 2016",
        "Who had the most hits in 2024?",
        "Top 10 in OPS+ in 2024",
        "Who had the highest slugging percentage in 2024?",
        "Most games played in 2024",
        "Shohei Ohtani 2024 batting stats",
        "Top 5 in on-base percentage in 2024",
        "Who led the league in runs scored in 2024?",
        "Highest ISO in 2024",
        "Top 10 in BABIP in 2024",
    ],

    # ── TIER 2: Comparisons (10) ────────────────────────────────────────
    "Comparisons": [
        "Compare Aaron Judge and Shohei Ohtani",
        "Compare Mookie Betts and Juan Soto in 2024",
        "Compare Freddie Freeman and Vladimir Guerrero Jr. career stats",
        "How did Bryce Harper compare to Mike Trout in 2024?",
        "Compare Bobby Witt Jr. and Gunnar Henderson in 2024",
        "Ronald Acuna Jr. vs Mookie Betts 2023",
        "Compare Trea Turner and Elly De La Cruz stolen bases",
        "Gerrit Cole vs Tarik Skubal in 2024",
        "Compare Kyle Tucker and Yordan Alvarez in 2024",
        "Pete Alonso vs Matt Olson home runs 2024",
    ],

    # ── TIER 3: Pitching queries (10) ───────────────────────────────────
    "Pitching": [
        "Who had the lowest ERA in 2024?",
        "Who had the most wins in 2024?",
        "Top 5 in strikeouts for pitchers in 2024",
        "Who had the best WHIP in 2024?",
        "Most saves in 2024",
        "Best ERA+ in 2024 with 100+ innings pitched",
        "Who threw the most innings in 2024?",
        "Lowest BB/9 in 2024 with at least 150 innings pitched",
        "Which pitcher had the most quality starts in 2024?",
        "Best K/9 in 2024 among qualified starters",
    ],

    # ── TIER 4: Splits (10) ─────────────────────────────────────────────
    "Splits": [
        "Mookie Betts vs lefties in 2024",
        "Aaron Judge home stats in 2024",
        "Who had the best OPS at home in 2024?",
        "Juan Soto vs right-handed pitchers",
        "Best batting average on the road in 2024",
        "Who had the highest OPS vs left-handed pitchers in 2024?",
        "Bryce Harper home vs away 2024",
        "Best RISP batting average in 2024",
        "Freddie Freeman platoon splits 2024",
        "Who hit the most home runs at home in 2024?",
    ],

    # ── TIER 5: Position-based queries (8) ──────────────────────────────
    "Position queries": [
        "Best OPS by a shortstop in 2024",
        "Which catcher had the most home runs in 2024?",
        "Top 5 outfielders by OPS in 2024",
        "Best batting average by a second baseman in 2024",
        "Which third baseman had the most RBI in 2024?",
        "Best ERA by a left-handed pitcher in 2024",
        "Highest OPS by a first baseman ever",
        "Which center fielder stole the most bases in 2024?",
    ],

    # ── TIER 6: Rookie queries (5) ──────────────────────────────────────
    "Rookies": [
        "Which rookie had the most home runs in 2024?",
        "Top rookie batters by OPS in 2024",
        "Which rookie pitcher had the most strikeouts in 2024?",
        "Best rookie batting average in 2024",
        "How many rookies hit 20+ home runs in 2024?",
    ],

    # ── TIER 7: Thresholds & milestones (8) ─────────────────────────────
    "Thresholds & milestones": [
        "Who hit .300 with 30 home runs in 2024?",
        "Players with 200+ hits in a season since 2016",
        "Who had 100+ RBI in 2024?",
        "Pitchers with 200+ strikeouts in 2024",
        "Players who walked more than they struck out in 2024",
        "Who had 40+ doubles in 2024?",
        "Pitchers with a sub-3.00 ERA and 150+ innings in 2024",
        "How many players had 30+ home runs in 2024?",
    ],

    # ── TIER 8: Team-level queries (5) ──────────────────────────────────
    "Team queries": [
        "Which team hit the most home runs in 2024?",
        "Teams with the most stolen bases in 2024",
        "Which team had the best ERA in 2024?",
        "Yankees home run leaders 2024",
        "Dodgers pitching stats in 2024",
    ],

    # ── TIER 9: Career aggregates (8) ───────────────────────────────────
    "Career aggregates": [
        "Who has the most career home runs?",
        "Best career batting average with 5000+ at bats",
        "Most career stolen bases among active players",
        "Which active pitcher has the most career wins?",
        "Best career OPS among active players with 3000+ at bats",
        "How many players have 300+ career home runs?",
        "Most career RBI among active players",
        "Best career ERA among active pitchers with 1000+ innings",
    ],

    # ── TIER 10: Year-over-year & trends (8) ────────────────────────────
    "Year-over-year": [
        "Who had the biggest drop in batting average from 2023 to 2024?",
        "Which player improved the most in OPS from 2023 to 2024?",
        "Biggest increase in home runs from 2023 to 2024",
        "Who had the biggest drop in ERA from 2023 to 2024?",
        "Players who hit 30+ HR in both 2023 and 2024",
        "Which player had the most consistent batting average from 2023 to 2024?",
        "Biggest drop in stolen bases from 2023 to 2024",
        "Who improved their strikeout rate the most from 2023 to 2024?",
    ],

    # ── TIER 11: Age-based & derivable stats (8) ────────────────────────
    "Age & derivable": [
        "Which player had the most home runs in their age-25 season?",
        "Best batting average at age 22",
        "Who had the most hits in their age-30 season?",
        "How many 20-20 seasons were there in 2024?",
        "Who had the highest K% in 2024?",
        "Best stolen base percentage in 2024 with 20+ attempts",
        "Who had the most total bases in 2024?",
        "Most extra base hits in 2024",
    ],

    # ── TIER 12: Long tail / complex analytics (10) ─────────────────────
    "Long tail": [
        "Who has the most career home runs without ever hitting 40 in a season?",
        "Which player had the most RBI in a season where they hit under .250?",
        "What's the highest single-season OPS by a shortstop?",
        "How many players have hit 30+ HR in consecutive seasons?",
        "Who has the highest career OPS among players who debuted after 2015?",
        "Which switch hitter had the best OPS in 2024?",
        "Who had the most walks in a single season?",
        "Players who struck out fewer than 50 times with 500+ PA",
        "Which player had the longest gap between 30-HR seasons?",
        "Who had the best OPS in the second half of their career (age 33+)?",
    ],
}


def generate_sql(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=HAIKU_SQL_PROMPT,
        messages=[{"role": "user", "content": question}],
    )
    sql = response.content[0].text.strip()
    sql = re.sub(r"^```(?:sql)?\s*", "", sql)
    sql = re.sub(r"\s*```$", "", sql)
    sql = re.sub(r"#[^\n]*", "", sql)
    return sql.strip()


def run_sql(sql: str) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        return [dict(r) for r in rows[:15]]
    except Exception as e:
        return [{"ERROR": str(e)}]
    finally:
        conn.close()


def main():
    results = {}
    tier_results = {}
    total_queries = sum(len(qs) for qs in QUERIES.values())
    query_num = 0

    for tier, questions in QUERIES.items():
        print(f"\n{'='*70}")
        print(f"  {tier} ({len(questions)} queries)")
        print(f"{'='*70}")
        tier_success = 0

        for q in questions:
            query_num += 1
            print(f"\n  [{query_num}/{total_queries}] Q: {q}")
            try:
                sql = generate_sql(q)
            except Exception as e:
                print(f"  ❌ API error: {e}")
                results[q] = {"sql": "", "row_count": 0, "error": str(e)}
                continue

            print(f"  SQL: {sql[:220]}{'...' if len(sql) > 220 else ''}")

            rows = run_sql(sql)
            if rows and "ERROR" in rows[0]:
                print(f"  ❌ {rows[0]['ERROR'][:120]}")
            elif rows:
                for row in rows[:2]:
                    compact = {k: v for k, v in row.items() if v is not None and v != 0}
                    print(f"  -> {json.dumps(compact, default=str)[:160]}")
                if len(rows) > 2:
                    print(f"  ... +{len(rows) - 2} more")
                print(f"  ✅ {len(rows)} rows")
                tier_success += 1
            else:
                print(f"  ⚠️  No results")

            results[q] = {
                "sql": sql,
                "row_count": len(rows),
                "error": rows[0].get("ERROR") if rows and "ERROR" in rows[0] else None,
            }
            time.sleep(0.2)

        tier_results[tier] = (tier_success, len(questions))
        pct = int(100 * tier_success / len(questions)) if questions else 0
        print(f"\n  >> {tier}: {tier_success}/{len(questions)} ({pct}%)")

    # ── Summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("  RESULTS BY TIER")
    print(f"{'='*70}")
    for tier, (s, t) in tier_results.items():
        bar = "█" * s + "░" * (t - s)
        print(f"  {bar}  {s}/{t}  {tier}")

    success = sum(1 for r in results.values() if r["error"] is None and r["row_count"] > 0)
    errors = sum(1 for r in results.values() if r["error"] is not None)
    empty = sum(1 for r in results.values() if r["error"] is None and r["row_count"] == 0)
    print(f"\n  TOTAL: {total_queries}  |  ✅ {success}  |  ❌ {errors}  |  ⚠️  {empty}")
    print(f"  Overall: {success}/{total_queries} ({int(100 * success / total_queries)}%)")

    if errors or empty:
        print(f"\n  FAILURES:")
        for q, r in results.items():
            if r["error"]:
                print(f"    ❌ {q}")
                print(f"       {r['error'][:100]}")
            elif r["row_count"] == 0:
                print(f"    ⚠️  {q}")
                print(f"       SQL: {r['sql'][:100]}")


if __name__ == "__main__":
    main()
