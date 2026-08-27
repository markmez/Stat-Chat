"""
Nightly answer-correctness judge — the "did we answer the question asked?" watchdog.

Motivation (2026-08-03): the dangerous failure class isn't ungrounded answers
(knowledge_miss already flags those) — it's DATA-REACHED-WRONG-SLICE answers:
"Derek Jeter longest hitting streak" returning the league-wide 2026 board,
"David Ortiz stats in 2004" returning his 2016 season. No routing metric can
see those; only reading the (question, answer) pair can. This job has Haiku
read yesterday's pairs and flag mismatches. Flags surface as `answer_judge`
rows via log_server_error → /admin/dashboard alert cards.

Runs nightly from /etc/cron.d/statchat on the Lightsail box:
    /opt/statchat/venv/bin/python /opt/statchat/repo/backend/scripts/judge_answers.py \
        --env-file /opt/statchat/.env

Idempotent: judgments keyed by query_log rowid (INSERT OR IGNORE), so reruns
and overlapping date windows are safe. Cost: ~$0.001/answer, so even hundreds
of queries/day is pennies.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import requests

JUDGE_MODEL = os.getenv("ROUTING_MODEL", "claude-haiku-4-5-20251001")

# Data-tier response types worth judging. knowledge_miss/analytical/contextual
# are excluded: they're LLM prose answers where "addresses the question" is
# near-guaranteed and wrongness isn't detectable without ground truth.
JUDGED_TYPES = ("query engine", "intercepted", "mapped", "haiku", "planner")

# Internal/test devices whose traffic shouldn't be judged.
EXCLUDED_DEVICES = ("wild-query-eval", "local-test", "system")

JUDGE_PROMPT = """You review answers from a baseball stats app. Formatting tags like [STATGRID], [LEADERBOARD], [SUGGEST], [TIP], [SEEALSO], [SUBTITLE], [CONTEXT], [DIDYOUMEAN], [TEAMCARD:XXX] are normal UI markup — ignore them.

Given the user's question and the app's answer, decide ONE thing: does the answer address the question that was asked?

Reply ok=false ONLY for these mismatches:
- Wrong subject (different player or team than asked)
- Wrong stat (asked for HRs, answered with steals)
- Wrong time frame or scope (asked 2004, answered 2016; asked career, answered this season; asked one player, answered a league-wide leaderboard)
- Wrong direction (asked worst/coldest/fewest, answered best/hottest/most)
- Deflects or refuses even though a direct stat lookup was clearly asked

Reply ok=true for everything else — including answers that honestly say the data isn't available, partial answers that acknowledge their scope, and "0 players matched" style results.

Baseball domain notes (do NOT flag these):
- "steals" means stolen bases; answering stolen bases is correct.
- "whiffs", "Ks", "punchouts" colloquially mean strikeouts; answering strikeouts is correct.
- "dingers", "bombs", "jacks", "taters" mean home runs.
- When a shorthand name matches several players, resolving to the most prominent one is intended behavior, not an error.
- The answer text you see is TRUNCATED to its first 600 characters. Never flag an answer for an incomplete or too-short list (e.g. "only 9 of 11 games shown", "top 50 shows fewer rows") — the missing rows are cut by truncation, not by the app. Judge only what the visible portion gets WRONG, never what it appears to omit.

Output strict JSON only: {"ok": true} or {"ok": false, "issue": "<one short sentence>"}"""


def load_env_file(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def judge_one(api_key: str, question: str, answer: str) -> dict:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={
            "model": JUDGE_MODEL,
            "max_tokens": 150,
            "system": JUDGE_PROMPT,
            "messages": [{"role": "user", "content":
                          f"QUESTION: {question}\n\nANSWER: {answer}"}],
        },
        timeout=30,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
        return {"ok": bool(parsed.get("ok")), "issue": parsed.get("issue", "")}
    except json.JSONDecodeError:
        return {"ok": True, "issue": f"unparseable judge output: {text[:80]}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default="")
    ap.add_argument("--date", default="", help="UTC date YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    load_env_file(args.env_file)
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("judge_answers: no ANTHROPIC_API_KEY", file=sys.stderr)
        return 1
    db_path = args.db or os.getenv("METERING_DB_PATH", "/data/metering.db")
    day = args.date or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS answer_judgments (
            log_rowid INTEGER UNIQUE,
            timestamp TEXT,
            query_text TEXT,
            response_type TEXT,
            ok INTEGER,
            issue TEXT,
            judged_at TEXT
        )
    """)
    type_ph = ",".join("?" for _ in JUDGED_TYPES)
    dev_ph = ",".join("?" for _ in EXCLUDED_DEVICES)
    rows = conn.execute(
        f"""SELECT rowid, timestamp, query_text, response_type, response_preview
            FROM query_log
            WHERE DATE(timestamp) = ?
              AND response_type IN ({type_ph})
              AND response_preview IS NOT NULL
              AND device_id NOT IN ({dev_ph})
              AND rowid NOT IN (SELECT log_rowid FROM answer_judgments)
            ORDER BY timestamp LIMIT ?""",
        (day, *JUDGED_TYPES, *EXCLUDED_DEVICES, args.limit),
    ).fetchall()

    print(f"judge_answers: {len(rows)} answers to judge for {day}")
    flagged = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for rowid, ts, qtext, rtype, preview in rows:
        try:
            verdict = judge_one(api_key, qtext, preview)
        except Exception as e:
            print(f"  judge error on rowid {rowid}: {e}", file=sys.stderr)
            continue
        conn.execute(
            "INSERT OR IGNORE INTO answer_judgments "
            "(log_rowid, timestamp, query_text, response_type, ok, issue, judged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rowid, ts, qtext, rtype, 1 if verdict["ok"] else 0,
             verdict.get("issue", ""), now_iso),
        )
        if not verdict["ok"]:
            flagged.append((rtype, qtext, verdict.get("issue", "")))
            print(f"  FLAG [{rtype}] {qtext!r} — {verdict.get('issue','')}")
    conn.commit()
    conn.close()

    if flagged:
        # Surface on /admin/dashboard alert cards.
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
            from services.metering import log_server_error
            sample = "; ".join(f"[{t}] {q!r}: {i}" for t, q, i in flagged[:5])
            log_server_error(
                source="answer_judge",
                error_type="wrong_answer_flags",
                error_message=f"{len(flagged)} answer(s) flagged for {day}",
                context={"date": day, "flags": sample[:900]},
            )
        except Exception as e:
            print(f"  could not log alert: {e}", file=sys.stderr)

    print(f"judge_answers: done — {len(flagged)} flagged / {len(rows)} judged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
