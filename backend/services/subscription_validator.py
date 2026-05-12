"""
StoreKit2 signed transaction verification.

Verifies the JWS produced by StoreKit2 (Transaction.jwsRepresentation on iOS)
against Apple's published certificate chain, extracts the subscription details,
and updates device_quota.is_paid / paid_expires_at so the backend's metering
loop bypasses the free-tier limit for active subscribers.

The actual cryptographic verification is delegated to Apple's official
app-store-server-library (signed_data_verifier.SignedDataVerifier). That
handles:
  - JWS signature verification using the leaf certificate from the x5c header
  - Certificate chain validation up to Apple's root CAs (G2 / G3)
  - Claim parsing into a strongly-typed JWSTransactionDecodedPayload

We add a thin app-specific layer on top: bundle-id check, product-id
allowlist, expiry check, and the DB update.
"""

import logging
import os
import sqlite3
from datetime import date, datetime, timezone
from typing import Optional

from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
)
from appstoreserverlibrary.models.Environment import Environment

from services.metering import METERING_DB_PATH

logger = logging.getLogger("statchat.subscription_validator")


def _ensure_original_transaction_id_column(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER to add original_transaction_id column to device_quota.
    Lets us look up the device by Apple's originalTransactionId claim when
    server-side notifications arrive (refunds, revokes, renewals)."""
    try:
        conn.execute("ALTER TABLE device_quota ADD COLUMN original_transaction_id TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

# --- App-specific configuration ---

BUNDLE_ID = "com.secondsignalapps.statchat"

# Product IDs from StoreKitService.swift — must match exactly.
ALLOWED_PRODUCT_IDS = {
    "com.statchat.app.monthly",
    "com.statchat.app.yearly",
}

# App Apple ID — the numeric App Store ID assigned to the app. Unknown until
# the app is published to App Store Connect. Library accepts None for
# pre-launch / sandbox-only verification.
APP_APPLE_ID: Optional[int] = (
    int(os.getenv("APP_APPLE_ID")) if os.getenv("APP_APPLE_ID") else None
)

# Apple Root CAs — both G2 (RSA, older) and G3 (ECC, current). StoreKit2
# transactions are signed with certs that chain to one of these.
_CERTS_DIR = os.path.join(os.path.dirname(__file__), "..", "certs")


def _load_apple_roots() -> list[bytes]:
    roots: list[bytes] = []
    for fname in ("AppleRootCA-G3.cer", "AppleRootCA-G2.cer"):
        path = os.path.join(_CERTS_DIR, fname)
        if os.path.exists(path):
            with open(path, "rb") as f:
                roots.append(f.read())
    return roots


_APPLE_ROOTS = _load_apple_roots()


def _make_verifier(environment: Environment) -> SignedDataVerifier:
    """Build a SignedDataVerifier for the given environment.
    Sandbox and Production transactions are signed by different chains in
    Apple's internal hierarchy but share the same root CAs we bundle.
    """
    return SignedDataVerifier(
        root_certificates=_APPLE_ROOTS,
        enable_online_checks=False,  # No OCSP / CRL hop on every verify
        environment=environment,
        bundle_id=BUNDLE_ID,
        app_apple_id=APP_APPLE_ID,
    )


class ReceiptValidationError(Exception):
    """Raised when a JWS fails to verify or doesn't represent a subscription
    we recognize. Caller maps to an HTTP 400."""


def validate_signed_transaction(
    device_id: str,
    signed_transaction: str,
    environment_hint: str = "Production",
) -> dict:
    """Verify a StoreKit2 signed transaction and update device_quota.

    Args:
        device_id: opaque device identifier (matches the same one used by
            the metering loop).
        signed_transaction: the JWS string from
            VerificationResult.jwsRepresentation on iOS.
        environment_hint: "Production" or "Sandbox" — sent by iOS based on
            transaction.environment. Determines which verifier to use.

    Returns:
        Dict with: valid, product_id, expires_at (ISO date), environment.

    Raises:
        ReceiptValidationError on any failure mode.
    """
    if not _APPLE_ROOTS:
        # Cert files missing — fail closed so we don't silently mark devices
        # as paid without verification.
        raise ReceiptValidationError(
            "Server misconfiguration: Apple root CA certs not found"
        )

    # Try the requested environment first, then fall back to the other in
    # case iOS got the hint wrong (e.g., TestFlight build but signed Production).
    # Skip Production entirely when APP_APPLE_ID isn't configured — the
    # library refuses to verify Production transactions without one, and
    # pre-launch / dev environments won't have the App Store numeric ID
    # yet. Once the app is published, set APP_APPLE_ID env var and
    # Production verification activates automatically.
    if APP_APPLE_ID is None:
        env_order = [Environment.SANDBOX]
    elif environment_hint.lower().startswith("p"):
        env_order = [Environment.PRODUCTION, Environment.SANDBOX]
    else:
        env_order = [Environment.SANDBOX, Environment.PRODUCTION]

    last_error: Optional[Exception] = None
    decoded = None
    used_env: Optional[Environment] = None
    for env in env_order:
        try:
            verifier = _make_verifier(env)
            decoded = verifier.verify_and_decode_signed_transaction(signed_transaction)
            used_env = env
            break
        except VerificationException as e:
            last_error = e
            continue
        except Exception as e:
            # Catch structural / decoding errors too (malformed JWS,
            # missing claims, etc.) so they surface as 400-class
            # ReceiptValidationError rather than 500.
            last_error = e
            continue

    if decoded is None:
        raise ReceiptValidationError(
            f"JWS verification failed: {last_error}"
        )

    # App-level claim checks beyond what the library enforces (the library
    # already checks bundle_id matches, but we double-check defensively).
    if decoded.bundleId != BUNDLE_ID:
        raise ReceiptValidationError(
            f"bundle_id mismatch: expected {BUNDLE_ID}, got {decoded.bundleId}"
        )
    if decoded.productId not in ALLOWED_PRODUCT_IDS:
        raise ReceiptValidationError(
            f"Unknown product_id: {decoded.productId}"
        )

    # Expiry — JWS expiresDate is milliseconds since epoch. Convert to a
    # date string for the metering DB. If the subscription is already
    # expired, return valid=False so iOS can refresh its state — but don't
    # raise, since "valid receipt, expired subscription" is a normal state.
    expires_ms = decoded.expiresDate or 0
    expires_dt = datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
    expires_iso = expires_dt.date().isoformat()
    is_active = expires_dt.date() >= date.today()

    # Update device_quota. Insert if the device isn't in the table yet.
    # Also store original_transaction_id so server-side notifications from
    # Apple (refunds, revokes, renewals) can look up the device by their
    # originalTransactionId claim.
    conn = sqlite3.connect(METERING_DB_PATH)
    try:
        _ensure_original_transaction_id_column(conn)
        existing = conn.execute(
            "SELECT 1 FROM device_quota WHERE device_id = ?", (device_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE device_quota "
                "SET is_paid = ?, paid_expires_at = ?, original_transaction_id = ? "
                "WHERE device_id = ?",
                (1 if is_active else 0, expires_iso, decoded.originalTransactionId, device_id),
            )
        else:
            conn.execute(
                "INSERT INTO device_quota "
                "(device_id, week_start, query_count, is_paid, paid_expires_at, original_transaction_id) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (device_id, date.today().isoformat(),
                 1 if is_active else 0, expires_iso, decoded.originalTransactionId),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "valid": is_active,
        "product_id": decoded.productId,
        "expires_at": expires_iso,
        "environment": used_env.value if used_env else None,
        "original_transaction_id": decoded.originalTransactionId,
    }


# ---------------------------------------------------------------------------
# App Store Server Notifications V2
# ---------------------------------------------------------------------------
#
# When subscription state changes server-side (refund, revoke, renewal,
# expiration, etc.), Apple POSTs a JWS-signed notification to our endpoint.
# We verify the signature with the same cert chain we use for receipts, then
# update device_quota based on the notification type.
#
# This is the canonical mechanism for catching refunds — without it, a user
# who gets refunded but never reopens the app would keep `is_paid = 1` until
# the original JWS expiry runs out (up to a year for yearly subs).
#
# Notification types we act on (others are logged but no state change):
#   REFUND            — Apple refunded the customer → revoke
#   REVOKE            — Apple revoked the subscription → revoke
#   EXPIRED           — Subscription expired without renewal → revoke
#   DID_RENEW         — Successful renewal → extend paid_expires_at
#   GRACE_PERIOD_EXPIRED — Billing grace period ended → revoke
#
# We DON'T act on DID_CHANGE_RENEWAL_STATUS (user toggled auto-renew). The
# user still has access until expiry; the subsequent EXPIRED notification
# (or DID_RENEW if they re-enable) will drive the state change.

# Notification types that should revoke the entitlement immediately.
_REVOKE_TYPES = {"REFUND", "REVOKE", "EXPIRED", "GRACE_PERIOD_EXPIRED"}

# Notification types that should refresh the entitlement with new expiry.
_RENEW_TYPES = {"DID_RENEW", "SUBSCRIBED", "OFFER_REDEEMED"}


def _decode_notification_with_fallback(signed_payload: str):
    """Verify a notification JWS, trying Production and Sandbox verifiers.
    Apple sends notifications signed by the same cert chain as transactions,
    but the environment in the payload tells us which verifier to use. We
    try both because the payload's environment field is buried inside the
    JWS body — we can't read it before verifying."""
    if not _APPLE_ROOTS:
        raise ReceiptValidationError(
            "Server misconfiguration: Apple root CA certs not found"
        )

    if APP_APPLE_ID is None:
        env_order = [Environment.SANDBOX]
    else:
        env_order = [Environment.PRODUCTION, Environment.SANDBOX]

    last_error: Optional[Exception] = None
    for env in env_order:
        try:
            verifier = _make_verifier(env)
            return verifier.verify_and_decode_notification(signed_payload), env
        except VerificationException as e:
            last_error = e
            continue
        except Exception as e:
            last_error = e
            continue
    raise ReceiptValidationError(f"notification JWS verification failed: {last_error}")


def handle_signed_notification(signed_payload: str) -> dict:
    """Verify and process an App Store Server Notification V2.

    Apple posts these to our /storekit-notification endpoint when subscription
    state changes. The signed_payload is a JWS containing notificationType,
    subtype, and data with embedded signedTransactionInfo (also JWS).

    Returns a dict describing what action was taken — purely for logging /
    response, the real side effect is the device_quota update.

    Idempotent: replaying the same notification is a no-op for revoke (already
    revoked) and updates expiry to the same value for renew.
    """
    decoded_notification, env = _decode_notification_with_fallback(signed_payload)

    notification_type = str(decoded_notification.notificationType.value
                            if hasattr(decoded_notification.notificationType, "value")
                            else decoded_notification.notificationType)
    subtype = None
    if decoded_notification.subtype is not None:
        subtype = str(decoded_notification.subtype.value
                      if hasattr(decoded_notification.subtype, "value")
                      else decoded_notification.subtype)

    # Inner transaction info is also a JWS — decode and verify it.
    inner_payload = decoded_notification.data and decoded_notification.data.signedTransactionInfo
    if not inner_payload:
        logger.info("notification type=%s subtype=%s (no signedTransactionInfo, ignored)",
                    notification_type, subtype)
        return {"action": "ignored", "reason": "no transaction info",
                "notification_type": notification_type}

    verifier = _make_verifier(env)
    inner_decoded = verifier.verify_and_decode_signed_transaction(inner_payload)

    if inner_decoded.bundleId != BUNDLE_ID:
        # Defensive — should never happen since the verifier checks bundle_id.
        raise ReceiptValidationError(
            f"notification bundle_id mismatch: {inner_decoded.bundleId}"
        )

    original_tx_id = inner_decoded.originalTransactionId
    expires_ms = inner_decoded.expiresDate or 0
    expires_dt = datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc) if expires_ms else None

    # Determine the action.
    action = "noop"
    if notification_type in _REVOKE_TYPES:
        new_is_paid = 0
        new_expires = date.today().isoformat()
        action = "revoke"
    elif notification_type in _RENEW_TYPES:
        if expires_dt is None:
            action = "ignored"
            return {"action": action, "reason": "no expiresDate on renewal",
                    "notification_type": notification_type}
        new_is_paid = 1 if expires_dt.date() >= date.today() else 0
        new_expires = expires_dt.date().isoformat()
        action = "renew"
    else:
        # Logged-only types (DID_CHANGE_RENEWAL_STATUS, etc.) — no DB change.
        logger.info("notification type=%s subtype=%s original_tx=%s (no action)",
                    notification_type, subtype, original_tx_id)
        return {"action": "noop", "notification_type": notification_type,
                "subtype": subtype, "original_transaction_id": original_tx_id}

    # Update device_quota by original_transaction_id. There may be more than
    # one device on the same subscription (user signed into multiple devices);
    # flip them all.
    conn = sqlite3.connect(METERING_DB_PATH)
    try:
        _ensure_original_transaction_id_column(conn)
        cur = conn.execute(
            "UPDATE device_quota SET is_paid = ?, paid_expires_at = ? "
            "WHERE original_transaction_id = ?",
            (new_is_paid, new_expires, original_tx_id),
        )
        conn.commit()
        rows_affected = cur.rowcount
    finally:
        conn.close()

    logger.info(
        "notification type=%s subtype=%s original_tx=%s action=%s rows=%d "
        "new_is_paid=%d new_expires=%s",
        notification_type, subtype, original_tx_id, action,
        rows_affected, new_is_paid, new_expires,
    )

    return {
        "action": action,
        "notification_type": notification_type,
        "subtype": subtype,
        "original_transaction_id": original_tx_id,
        "rows_affected": rows_affected,
        "new_is_paid": new_is_paid,
        "new_expires_at": new_expires,
    }
