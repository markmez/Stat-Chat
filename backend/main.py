"""
StatChat Backend — FastAPI application entry point.

Run locally:
    cd backend/
    uvicorn main:app --reload

Or from project root:
    uvicorn backend.main:app --reload
"""

import os
import sys
import urllib.request
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

# Make the project root importable so backend modules can reach schema_description.py
_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _root not in sys.path:
    sys.path.insert(0, _root)

from routers import health, query          # noqa: E402
from services.metering import init_metering_db  # noqa: E402


DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats.db")
DB_DOWNLOAD_URL = os.getenv(
    "DB_DOWNLOAD_URL",
    "https://stat-chat.s3.us-east-2.amazonaws.com/baseball_stats.db",
)


def ensure_db():
    """Download baseball_stats.db from S3 if it's not already on the volume."""
    if os.path.exists(DB_PATH):
        print(f"Database found at {DB_PATH}")
        return
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    print(f"Downloading database from {DB_DOWNLOAD_URL} ...")
    urllib.request.urlretrieve(DB_DOWNLOAD_URL, DB_PATH)
    print(f"Database downloaded ({os.path.getsize(DB_PATH) // 1_000_000} MB)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_db()
    init_metering_db()
    yield


app = FastAPI(
    title="StatChat Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(query.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
