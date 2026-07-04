"""Razorpay webhook router — receives payment events from Razorpay.

SECURITY:
  - Reads raw request body BEFORE any JSON parsing.
  - Verifies X-Razorpay-Signature (HMAC-SHA256) before trusting any field.
  - Returns 400 on signature failure, 200 on success (Razorpay retries on non-2xx).
  - This is a separate router from the WhatsApp webhook to keep concerns clean.
"""

import logging

from fastapi import APIRouter, Request

from app.services.payment import payment_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["payments"])


@router.post("/razorpay")
async def razorpay_webhook(request: Request):
    """Receive and process Razorpay payment webhook events.

    Flow:
      1. Read raw body (before parsing).
      2. Extract X-Razorpay-Signature header.
      3. Delegate to PaymentService.process_payment_webhook().
      4. Return appropriate HTTP status.
    """
    # ── Step 1: Read raw body BEFORE parsing ──
    raw_body = await request.body()

    # ── Step 2: Extract signature header ──
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not signature:
        logger.warning(
            f"Razorpay webhook: NO signature header — "
            f"IP={request.client.host if request.client else 'unknown'}"
        )
        # Still delegate to payment service so it logs the failure
        result = await payment_service.process_payment_webhook(raw_body, "")
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=result.get("code", 400),
            content={"status": result.get("status", "error")},
        )

    # ── Step 3: Process through payment service ──
    result = await payment_service.process_payment_webhook(raw_body, signature)

    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=result.get("code", 200),
        content={"status": result.get("status", "ok")},
    )
