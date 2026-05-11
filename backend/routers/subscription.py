"""
Subscription routes — currently just /validate-receipt.

This is the bridge between iOS's local StoreKit entitlement state and the
backend's per-device metering. iOS sends the JWS from
Transaction.jwsRepresentation; we verify with Apple's cert chain and flip
device_quota.is_paid = 1 so the metering loop bypasses the free-tier limit
for the subscriber's device.

Public endpoint (no admin auth) — the JWS signature IS the auth.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.subscription_validator import (
    ReceiptValidationError,
    validate_signed_transaction,
)

router = APIRouter()
logger = logging.getLogger("statchat.subscription")


class ValidateReceiptRequest(BaseModel):
    device_id: str
    signed_transaction: str  # JWS from VerificationResult.jwsRepresentation
    environment: str = "Production"  # "Production" or "Sandbox"


@router.post("/validate-receipt")
async def validate_receipt(body: ValidateReceiptRequest):
    """Verify a StoreKit2 signed transaction and update device_quota.

    Called by iOS:
      - After a successful Product.purchase()
      - On app launch (to refresh subscription state on the backend)
      - When Transaction.updates emits (renewals, refunds, etc.)

    Returns the parsed transaction details plus whether the subscription
    is currently active. Idempotent — safe to call multiple times with the
    same signed_transaction.
    """
    if not body.device_id or not body.signed_transaction:
        raise HTTPException(400, "device_id and signed_transaction are required")

    try:
        result = validate_signed_transaction(
            device_id=body.device_id,
            signed_transaction=body.signed_transaction,
            environment_hint=body.environment,
        )
    except ReceiptValidationError as e:
        logger.warning(
            "receipt_validation_failed device_id=%s error=%s",
            body.device_id[:8] if body.device_id else "?", e,
        )
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception(
            "receipt_validation_unexpected_error device_id=%s",
            body.device_id[:8] if body.device_id else "?",
        )
        raise HTTPException(500, "receipt validation failed")

    return result
