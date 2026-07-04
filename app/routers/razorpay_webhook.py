"""Razorpay webhook router — receives payment events from Razorpay.

SECURITY:
  - Reads raw request body BEFORE any JSON parsing.
  - Verifies X-Razorpay-Signature (HMAC-SHA256) before trusting any field.
  - Returns 400 on signature failure, 200 on success (Razorpay retries on non-2xx).

MULTI-TENANT ROUTING:
  Each clinic registers its OWN webhook URL in the Razorpay Dashboard:
    https://your-domain.com/webhooks/razorpay/{clinic_id}

  The clinic_id path parameter allows us to:
    1. Look up the clinic from the database.
    2. Extract the per-clinic razorpay_webhook_secret from clinic.config.
    3. Verify the signature using that specific secret.

  This means 100 clinics → 100 different Razorpay accounts → ONE server.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.payment import payment_service, get_razorpay_creds

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["payments"])


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
    # ── Step 1: Resolve clinic ──
    try:
        from app.services.tenant import get_clinic_by_id
        clinic = await get_clinic_by_id(clinic_id)
    except Exception as e:
        logger.warning(f"Razorpay webhook: unknown clinic_id={clinic_id} — {e}")
        # Return 200 so Razorpay doesn't keep retrying for a permanently unknown clinic.
        return JSONResponse(status_code=200, content={"status": "unknown_clinic"})

    # ── Step 2: Extract per-clinic webhook secret ──
    _, _, webhook_secret = get_razorpay_creds(clinic)

    # ── Step 3: Read raw body BEFORE parsing ──
    raw_body = await request.body()

    # ── Step 4: Extract signature header ──
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not signature:
        logger.warning(
            f"Razorpay webhook: NO signature header — "
            f"clinic={clinic_id} IP={request.client.host if request.client else 'unknown'}"
        )

    # ── Step 5: Process through payment service (with per-clinic secret) ──
    result = await payment_service.process_payment_webhook(
        raw_body, signature, webhook_secret=webhook_secret
    )

    return JSONResponse(
        status_code=result.get("code", 200),
        content={"status": result.get("status", "ok")},
    )
