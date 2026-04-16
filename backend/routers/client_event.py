"""Public endpoint for logging client-side events (partial responses, decode errors, etc.).

Writes to query_log in the metering DB via services.metering.log_client_event.
No admin auth — called directly from iOS/Android clients. Abuse is bounded by:
  - Whitelisted event_type values
  - device_id format validation (UUID-ish)
  - Per-minute dedup at the service layer

Dashboard surfaces these via response_type='client_event'.
"""
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.metering import log_client_event

router = APIRouter()

ALLOWED_EVENT_TYPES = {
    "partial_player_card",
    "query_timeout",
    "decode_error",
    "empty_stream",
}

_DEVICE_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")


class ClientEventPayload(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64)
    event_type: str
    context: dict[str, Any] = Field(default_factory=dict)
    app_version: str | None = Field(default=None, max_length=32)
    platform_version: str | None = Field(default=None, max_length=32)


@router.post("/client-event")
async def client_event(payload: ClientEventPayload):
    if not _DEVICE_ID_RE.match(payload.device_id):
        raise HTTPException(400, "Invalid device_id format")
    if payload.event_type not in ALLOWED_EVENT_TYPES:
        raise HTTPException(400, f"Unknown event_type: {payload.event_type}")

    inserted = log_client_event(
        event_type=payload.event_type,
        context=payload.context,
        device_id=payload.device_id,
        app_version=payload.app_version,
        platform_version=payload.platform_version,
    )
    return {"status": "ok", "deduped": not inserted}
