"""Razorpay webhook receiver — per-clinic signature-verified payment events."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.payment import payment_service, get_razorpay_creds
from app.utils.security import PersistentRateLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["payments"])

# Throttles the WhatsApp admin alert sent on every bad-signature webhook —
# an unauthenticated attacker can otherwise flood the hospital's own WhatsApp
# number and burn Meta API quota by repeatedly POSTing garbage here.
_signature_alert_limiter = PersistentRateLimiter(max_attempts=3, window_seconds=300)


@router.post("/razorpay/{clinic_id}")
async def razorpay_webhook(clinic_id: str, request: Request):
    """Receive and process Razorpay payment webhook events for a specific clinic.

    Flow:
      1. Resolve the clinic from the database using clinic_id.
      2. Extract the per-clinic razorpay_webhook_secret (falls back to global settings).
      3. Read raw body (before parsing).
      4. Extract X-Razorpay-Signature header.
      5. Delegate to PaymentService.process_payment_webhook() with the resolved secret.
      6. Return appropriate HTTP status.
    """
    try:
        from app.services.tenant import get_clinic_by_id

        clinic = await get_clinic_by_id(clinic_id)
    except Exception as e:
        logger.warning(f"Razorpay webhook: unknown clinic_id={clinic_id} — {e}")
        return JSONResponse(status_code=200, content={"status": "unknown_clinic"})

    _, _, webhook_secret = get_razorpay_creds(clinic)

    raw_body = await request.body()

    signature = request.headers.get("X-Razorpay-Signature", "")
    client_ip = request.client.host if request.client else "unknown"

    if not signature:
        logger.warning(
            f"Razorpay webhook: NO signature header — "
            f"clinic={clinic_id} IP={client_ip}"
        )

    result = await payment_service.process_payment_webhook(
        raw_body,
        signature,
        webhook_secret=webhook_secret,
        alert_limiter=_signature_alert_limiter,
        alert_key=f"{clinic_id}:{client_ip}",
    )

    return JSONResponse(
        status_code=result.get("code", 200),
        content={"status": result.get("status", "ok")},
    )
