"""
Quick experiment: Can Haiku generate correct SQL from the schema description?

Tests three categories:
1. Queries the interceptor ALREADY handles (baseline — should match interceptor output)
2. Queries that are CLOSE to interceptor patterns but might miss
3. Queries that NO parser would catch (the long tail)

Run: cd backend && python tests/test_haiku_sql.py
"""

import os
import sys
import sqlite3
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import anthropic
from prompts import HAIKU_SQL_PROMPT

# Use the full project root DB (240MB with all tables), not the stripped iOS one
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "baseball_stats.db")

client = anthropic.Anthropic()

# Test queries in three tiers
QUERIES = {
    "TIER 1 — Interceptor handles (baseline)": [
        "Who led the league in home runs in 2024?",
        "Compare Aaron Judge and Juan Soto",
        "Aaron Judge career stats",
        "Who hit .300 with 30 home runs?",
        "Top 5 in OPS last season",
        "Mookie Betts vs lefties",
        "Yankees home run leaders 2024",
        "Who had the most strikeouts as a pitcher in 2024?",
    ],
    "TIER 2 — Edge cases that might miss interceptor": [
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
    "TIER 3 — Long tail (no parser would catch)": [
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
    # Strip markdown fences if Haiku wraps them
    import re
    sql = re.sub(r'^```(?:sql)?\s*', '', sql)
    sql = re.sub(r'\s*```$', '', sql)
    return sql.strip()


def run_sql(sql: str) -> list:
    """Execute SQL against the local DB and return results."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        return [dict(r) for r in rows[:15]]  # Cap output
    except Exception as e:
        return [{"ERROR": str(e)}]
    finally:
        conn.close()


def main():
    results = {}

    for tier, questions in QUERIES.items():
        print(f"\n{'='*70}")
        print(f"  {tier}")
        print(f"{'='*70}")

        for q in questions:
            print(f"\n  Q: {q}")
            sql = generate_sql(q)
            print(f"  SQL: {sql[:200]}{'...' if len(sql) > 200 else ''}")

            rows = run_sql(sql)
            if rows and "ERROR" in rows[0]:
                print(f"  ❌ {rows[0]['ERROR']}")
            elif rows:
                # Show first few results compactly
                for i, row in enumerate(rows[:5]):
                    # Filter to interesting columns
                    compact = {k: v for k, v in row.items() if v is not None and v != 0}
                    print(f"  → {json.dumps(compact, default=str)[:150]}")
                if len(rows) > 5:
                    print(f"  ... and {len(rows) - 5} more rows")
                print(f"  ✅ {len(rows)} rows returned")
            else:
                print(f"  ⚠️  No results")

            results[q] = {"sql": sql, "row_count": len(rows), "error": rows[0].get("ERROR") if rows and "ERROR" in rows[0] else None}

    # Summary
    print(f"\n{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")
    total = sum(len(qs) for qs in QUERIES.values())
    success = sum(1 for r in results.values() if r["error"] is None and r["row_count"] > 0)
    errors = sum(1 for r in results.values() if r["error"] is not None)
    empty = sum(1 for r in results.values() if r["error"] is None and r["row_count"] == 0)
    print(f"  Total: {total}  |  Success: {success}  |  Errors: {errors}  |  Empty: {empty}")


if __name__ == "__main__":
    main()
