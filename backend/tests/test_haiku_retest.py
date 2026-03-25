"""Quick retest of previously failed/problematic queries."""
import os, sys, re, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
import anthropic
from prompts import HAIKU_SQL_PROMPT

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "baseball_stats.db")
client = anthropic.Anthropic()

queries = [
    "What team had the biggest difference between home and away batting average in 2024?",
    "Best career OBP among active players",
    "Who has the most career stolen bases among active players?",
    "Which pitcher had the most strikeouts per 9 innings with at least 150 innings pitched?",
]

for q in queries:
    print(f"\nQ: {q}")
    r = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1024, system=HAIKU_SQL_PROMPT, messages=[{"role": "user", "content": q}])
    sql = r.content[0].text.strip()
    sql = re.sub(r"^```(?:sql)?\s*", "", sql)
    sql = re.sub(r"\s*```$", "", sql)
    sql = re.sub(r"#[^\n]*", "", sql)
    sql = sql.strip()
    print(f"SQL: {sql[:400]}")
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()[:5]
        conn.close()
        if rows:
            for row in rows:
                print(f"  -> {dict(zip(cols, row))}")
            print(f"  ✅ {len(rows)}+ rows")
        else:
            print("  ⚠️  EMPTY")
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
