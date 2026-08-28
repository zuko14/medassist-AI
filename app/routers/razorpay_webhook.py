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


# KA-05: REMOVED pathless @router.post("/razorpay") — all webhooks MUST
# be scoped to a specific clinic via the URL path.
@router.post("/razorpay/{clinic_id}")
async def razorpay_webhook(request: Request, clinic_id: str):
    """Receive and process Razorpay payment webhook events.

    Only:
      - /webhooks/razorpay/{clinic_id} (multi-tenant clinic-specific endpoint)

    KA-05: The pathless /webhooks/razorpay route has been removed. A webhook
    without an explicit clinic_id cannot be securely attributed to a tenant.

    Flow:
      1. Reject sentinel values ('default', 'none', etc.) — these are not real clinic IDs.
      2. Resolve the clinic from the database using clinic_id.
      3. Extract the per-clinic razorpay_webhook_secret (falls back to global settings).
      4. Read raw body (before parsing).
      5. Extract X-Razorpay-Signature header.
      6. Delegate to PaymentService.process_payment_webhook() with the resolved secret and clinic_id.
      7. Return appropriate HTTP status.
    """
    # KA-05: Reject sentinel values that would produce an unscoped query
    if not clinic_id or str(clinic_id).strip().lower() in ("default", "none", "null", ""):
        logger.warning(f"Razorpay webhook: rejected sentinel clinic_id='{clinic_id}'")
        return JSONResponse(status_code=400, content={"status": "invalid_clinic_id"})

    effective_clinic_id = clinic_id
    try:
        try:
            from app.services.tenant import get_clinic_by_id

            clinic = await get_clinic_by_id(effective_clinic_id)
        except Exception as e:
            logger.warning(f"Razorpay webhook: unknown clinic_id={effective_clinic_id} — {e}")
            return JSONResponse(status_code=200, content={"status": "unknown_clinic"})

        # Resolved clinic UUID (None if synthetic fallback/default to avoid invalid UUID syntax in DB queries)
        resolved_clinic_id = clinic.get("id") if (clinic and clinic.get("id") != "default") else None
        _, _, webhook_secret = get_razorpay_creds(clinic)

        raw_body = await request.body()

        signature = request.headers.get("X-Razorpay-Signature", "")
        client_ip = request.client.host if request.client else "unknown"

        if not signature:
            logger.warning(
                f"Razorpay webhook: NO signature header — "
                f"clinic={effective_clinic_id} IP={client_ip}"
            )

        result = await payment_service.process_payment_webhook(
            raw_body,
            signature,
            webhook_secret=webhook_secret,
            alert_limiter=_signature_alert_limiter,
            alert_key=f"{resolved_clinic_id or effective_clinic_id}:{client_ip}",
            clinic_id=resolved_clinic_id,
        )

        return JSONResponse(
            status_code=result.get("code", 200),
            content={"status": result.get("status", "ok")},
        )
    except Exception as exc:
        logger.exception(f"Unhandled exception in razorpay_webhook for clinic={effective_clinic_id}: {exc}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "reason": "internal_error"},
        )
