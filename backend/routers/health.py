import os
import sqlite3

from fastapi import APIRouter

router = APIRouter()

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")


@router.get("/health")
async def health():
    # Verify the database is accessible, not just that the server is running
    if not os.path.exists(DB_PATH):
        return {"status": "degraded", "reason": "database missing"}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT 1 FROM players LIMIT 1")
        conn.close()
    except Exception as e:
        return {"status": "degraded", "reason": str(e)}
    return {"status": "ok"}
