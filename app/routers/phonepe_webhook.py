"""PhonePe webhook receiver — one URL per clinic, authenticated per clinic.

There is deliberately no unscoped /webhooks/phonepe: the clinic in the path is
the tenant, its own webhook username/password authenticate the call, and its
own API credentials re-read the order (see PaymentService.process_phonepe_webhook).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.payment import payment_service
from app.utils.security import PersistentRateLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["payments"])

# Same throttle as the Razorpay route: a bad-auth flood must not flood the
# clinic's admin WhatsApp with alerts.
_auth_alert_limiter = PersistentRateLimiter(max_attempts=3, window_seconds=300)


@router.post("/phonepe/{clinic_id}")
async def phonepe_webhook(request: Request, clinic_id: str):
    try:
        try:
            from app.services.tenant import get_clinic_by_id

            clinic = await get_clinic_by_id(clinic_id)
        except Exception as e:
            logger.warning(f"PhonePe webhook: unknown clinic_id={clinic_id} — {e}")
            return JSONResponse(status_code=200, content={"status": "unknown_clinic"})

        raw_body = await request.body()
        client_ip = request.client.host if request.client else "unknown"
        result = await payment_service.process_phonepe_webhook(
            raw_body,
            request.headers.get("Authorization", ""),
            clinic,
            alert_limiter=_auth_alert_limiter,
            alert_key=f"phonepe:{clinic_id}:{client_ip}",
        )
        return JSONResponse(
            status_code=result.get("code", 200),
            content={"status": result.get("status", "ok")},
        )
    except Exception as exc:
        logger.exception(f"Unhandled exception in phonepe_webhook for clinic={clinic_id}: {exc}")
        return JSONResponse(status_code=500, content={"status": "error", "reason": "internal_error"})
