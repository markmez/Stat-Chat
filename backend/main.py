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


@asynccontextmanager
async def lifespan(app: FastAPI):
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
