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
    handle_signed_notification,
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


class StoreKitNotificationRequest(BaseModel):
    signedPayload: str  # JWS-signed App Store Server Notification V2


@router.post("/storekit-notification")
async def storekit_notification(body: StoreKitNotificationRequest):
    """Receive an App Store Server Notification V2 from Apple.

    Apple POSTs here when subscription state changes server-side (refunds,
    revokes, renewals, expirations). Configured in App Store Connect under
    App Information → App Store Server Notifications → Production/Sandbox
    Server URL.

    No admin auth — the JWS signature IS the auth. We verify against Apple's
    cert chain in handle_signed_notification.

    Must return 200 quickly; Apple retries with backoff on non-2xx. Heavy
    processing should be async — for now, all our handlers are sub-100ms
    SQLite UPDATEs so synchronous is fine.
    """
    if not body.signedPayload:
        raise HTTPException(400, "signedPayload is required")

    try:
        result = handle_signed_notification(body.signedPayload)
    except ReceiptValidationError as e:
        logger.warning("notification_validation_failed error=%s", e)
        # Return 400 — Apple will retry, but if the signature is genuinely
        # bad there's no point.
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("notification_unexpected_error")
        # Return 500 — Apple will retry, which is what we want for transient
        # errors (DB locked, etc.)
        raise HTTPException(500, "notification handling failed")

    return result
