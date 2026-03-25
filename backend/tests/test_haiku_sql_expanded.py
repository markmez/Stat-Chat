"""
Expanded Haiku SQL regression test — 50+ queries.

Covers:
- Original 30 queries (regression)
- Age derivation (age column is NULL)
- Primary position filtering
- Rookie definition (MLB eligibility rules)
- Rate stat minimums (PA >= 400, ip_outs >= 486)
- SQLite integer division (CAST to REAL)
- Career aggregates
- Year-over-year comparisons
- Multi-table joins

Run: cd backend && python tests/test_haiku_sql_expanded.py
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

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "baseball_stats.db")

client = anthropic.Anthropic()

QUERIES = {
    "TIER 1 — Interceptor baseline (regression)": [
        "Who led the league in home runs in 2024?",
        "Compare Aaron Judge and Juan Soto",
        "Aaron Judge career stats",
        "Who hit .300 with 30 home runs?",
        "Top 5 in OPS last season",
        "Mookie Betts vs lefties",
        "Yankees home run leaders 2024",
        "Who had the most strikeouts as a pitcher in 2024?",
    ],
    "TIER 2 — Edge cases (regression)": [
        "Which team had the best ERA in 2024?",
        "Who had the most walks in a single season?",
        "Players who struck out fewer than 50 times with 500+ PA",
        "Who stole the most bases last year?",
        "Best OPS+ among switch hitters",
        "Left-handed pitchers with the most wins in 2024",
        "Which rookie had the most home runs in 2024?",
        "Highest batting average by a catcher",
        "Who had the best WHIP in 2025?",
        "Teams with the most stolen bases in 2024",
    ],
    "TIER 3 — Long tail (regression)": [
        "Who had the biggest drop in batting average from 2023 to 2024?",
        "Which player had the most hits in their age-25 season?",
        "What's the highest single-season OPS by a shortstop?",
        "How many players have hit 30+ HR in consecutive seasons?",
        "Who has the best career batting average among active players with 3000+ at bats?",
        "Which player improved the most in OPS from 2023 to 2024?",
        "Who has the most career home runs without ever hitting 40 in a season?",
        "What team had the biggest difference between home and away batting average in 2024?",
        "Which player had the most RBI in a season where they hit under .250?",
        "How many 20-20 seasons (20 HR and 20 SB) were there in 2024?",
        "Who has the highest career OPS among players who debuted after 2015?",
        "Which pitcher had the most strikeouts per 9 innings with at least 150 innings pitched?",
    ],
    "TIER 4 — Prompt fix regressions": [
        # Age derivation (age column is NULL)
        "Which player had the most home runs in their age-30 season?",
        "Who had the best batting average at age 22?",
        # Primary position
        "Best OPS by a first baseman in 2024",
        "Which second baseman had the most home runs in 2024?",
        "Top 5 catchers by OPS in 2024",
        # Rookie definition (MLB eligibility)
        "Which rookie had the best ERA in 2024?",
        "Top rookie batters by OPS in 2024",
        # Rate stat minimums
        "Who had the lowest ERA in 2024?",
        "Best WHIP among starting pitchers in 2024",
        "Highest batting average in 2024",
        # SQLite integer division
        "Who has the best career batting average with 5000+ at bats?",
        "Best career OBP among active players",
        # Reserved words / alias safety
        "Biggest drop in home runs from 2023 to 2024",
    ],
    "TIER 5 — New territory": [
        # Pitching queries
        "Who threw the most innings in 2024?",
        "Which pitcher had the most saves in 2024?",
        "Best ERA+ in 2024 with 100+ innings pitched",
        "Which pitcher walked the fewest batters per 9 innings in 2024?",
        # Cross-table / complex
        "Which player had both 20+ home runs and 20+ stolen bases in the same season since 2020?",
        "Players who hit .300 with an OPS over 1.000 in 2024",
        "Who had the most hits in a single season since 2016?",
        "Which team hit the most home runs in 2024?",
        "Top 5 players by total bases in 2024",
        "Who had the most extra base hits in 2024?",
        # Splits and advanced
        "Best OPS at home in 2024",
        "Who had the highest batting average vs right-handed pitchers in 2024?",
        # Career milestones
        "How many players have 300+ career home runs?",
        "Who has the most career stolen bases among active players?",
        "Which active pitcher has the most career wins?",
    ],
}


def generate_sql(question: str) -> str:
    """Ask Haiku to generate SQL for a question."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=HAIKU_SQL_PROMPT,
        messages=[{"role": "user", "content": question}],
    )
    sql = response.content[0].text.strip()
    sql = re.sub(r'^```(?:sql)?\s*', '', sql)
    sql = re.sub(r'\s*```$', '', sql)
    sql = re.sub(r'#[^\n]*', '', sql)
    return sql.strip()


def run_sql(sql: str) -> list:
    """Execute SQL against the local DB and return results."""
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

    for tier, questions in QUERIES.items():
        print(f"\n{'='*70}")
        print(f"  {tier}")
        print(f"{'='*70}")
        tier_success = 0
        tier_total = len(questions)

        for q in questions:
            print(f"\n  Q: {q}")
            try:
                sql = generate_sql(q)
            except Exception as e:
                print(f"  ❌ API error: {e}")
                results[q] = {"sql": "", "row_count": 0, "error": str(e)}
                continue

            print(f"  SQL: {sql[:250]}{'...' if len(sql) > 250 else ''}")

            rows = run_sql(sql)
            if rows and "ERROR" in rows[0]:
                print(f"  ❌ {rows[0]['ERROR']}")
            elif rows:
                for i, row in enumerate(rows[:3]):
                    compact = {k: v for k, v in row.items() if v is not None and v != 0}
                    print(f"  -> {json.dumps(compact, default=str)[:160]}")
                if len(rows) > 3:
                    print(f"  ... and {len(rows) - 3} more rows")
                print(f"  ✅ {len(rows)} rows returned")
                tier_success += 1
            else:
                print(f"  ⚠️  No results")

            results[q] = {
                "sql": sql,
                "row_count": len(rows),
                "error": rows[0].get("ERROR") if rows and "ERROR" in rows[0] else None,
            }

            # Small delay to avoid rate limiting
            time.sleep(0.3)

        tier_results[tier] = (tier_success, tier_total)
        print(f"\n  Tier result: {tier_success}/{tier_total}")

    # Summary
    print(f"\n{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")
    for tier, (s, t) in tier_results.items():
        pct = int(100 * s / t) if t else 0
        print(f"  {tier}: {s}/{t} ({pct}%)")

    total = sum(len(qs) for qs in QUERIES.values())
    success = sum(1 for r in results.values() if r["error"] is None and r["row_count"] > 0)
    errors = sum(1 for r in results.values() if r["error"] is not None)
    empty = sum(1 for r in results.values() if r["error"] is None and r["row_count"] == 0)
    print(f"\n  TOTAL: {total}  |  ✅ Success: {success}  |  ❌ Errors: {errors}  |  ⚠️  Empty: {empty}")
    print(f"  Overall: {success}/{total} ({int(100*success/total)}%)")

    # List failures
    failures = [q for q, r in results.items() if r["error"] is not None or r["row_count"] == 0]
    if failures:
        print(f"\n  FAILED QUERIES:")
        for q in failures:
            r = results[q]
            status = f"ERROR: {r['error']}" if r["error"] else "EMPTY"
            print(f"    - {q} [{status}]")


if __name__ == "__main__":
    main()
