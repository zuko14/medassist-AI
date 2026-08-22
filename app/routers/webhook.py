"""Webhook router for WhatsApp Cloud API (Multi-Tenant) — Security Hardened."""

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException, Query, BackgroundTasks
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.models.message import WhatsAppWebhookPayload
from app.services.conversation import conversation_manager
from app.services.whatsapp import whatsapp_service
from app.services.tenant import resolve_tenant
from app.utils.validators import normalize_phone
from app.utils.security import verify_webhook_signature

# Supabase-native atomic idempotency + per-phone asyncio lock
from app.services.message_queue import message_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


@router.get("")
async def verify_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
):
    """Verify webhook for Meta WhatsApp Cloud API."""
    # We use a global verify token for all clinics
    if hub_mode == "subscribe" and hub_verify_token in (
        settings.whatsapp_verify_token,
        settings.meta_verify_token,
    ):
        logger.info("Webhook verified successfully")
        return PlainTextResponse(content=hub_challenge)

    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive webhook events from WhatsApp Cloud API.

    Security: Validates X-Hub-Signature-256 header before processing.
    Meta signs every payload with HMAC-SHA256 using your App Secret.
    This prevents attackers from injecting fake payloads.
    """
    # ── Step 1: Read raw body BEFORE parsing JSON ──
    raw_body = await request.body()

    # ── Step 2: Verify Meta signature ──
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_webhook_signature(raw_body, signature, settings.meta_app_secret):
        logger.warning(
            f"Webhook signature verification FAILED — "
            f"IP={request.client.host if request.client else 'unknown'}"
        )
        # Return 200 to not reveal verification logic to attacker,
        # but do NOT process the payload.
        return {"status": "ok"}

    try:
        body = await request.json()
        logger.debug(f"Received webhook: {body}")

        # Parse payload
        payload = WhatsAppWebhookPayload(**body)

        for entry in payload.entry:
            for change in entry.changes:
                if change.value.statuses:
                    for status in change.value.statuses:
                        background_tasks.add_task(record_delivery_status, status)

                if change.value.messages:
                    metadata = change.value.metadata
                    display_phone = metadata.get("display_phone_number")

                    if not display_phone:
                        logger.error(
                            "No display_phone_number found in webhook metadata"
                        )
                        continue

                    for message in change.value.messages:
                        background_tasks.add_task(
                            process_message_safe, message, display_phone, body
                        )
                        logger.info(f"Queued message {message.id} to BackgroundTasks")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        # Never expose stack traces in webhook responses
        return {"status": "error"}


async def record_delivery_status(status: dict) -> None:
    """Persist a Meta delivery receipt (sent/delivered/read/failed).

    Without this a report reads status='sent' while Meta already reported it
    undeliverable — invisible to staff.
    """
    wamid = status.get("id")
    state = status.get("status")
    if not wamid or not state:
        return
    err = (status.get("errors") or [{}])[0]
    try:
        from app.database import supabase
        supabase.table("lab_reports").update({
            "delivery_status": state,
            "delivery_error": err.get("title") or err.get("message") if err else None,
            "delivery_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("whatsapp_message_id", wamid).execute()
        if state == "failed":
            logger.error(f"Meta delivery FAILED for wamid {wamid}: {err}")
    except Exception as e:
        logger.warning(f"Could not record delivery status for {wamid}: {e}")


async def process_message_safe(message, display_phone: str, raw_payload: dict):
    """Wrapper that catches failures and logs them to a dead-letter queue.

    In a hospital bot, silently dropping a patient message is unacceptable.
    If processing fails (e.g. mid-restart crash), the raw payload is saved
    to Supabase `failed_messages` table for manual retry or investigation.
    """
    try:
        await process_message(message, display_phone)
    except Exception as e:
        logger.error(f"Message processing failed, saving to dead-letter queue: {e}")
        try:
            import json
            from app.database import supabase

            supabase.table("failed_messages").insert(
                {
                    "phone": getattr(message, "from_", "unknown"),
                    "display_phone": display_phone,
                    "payload": json.dumps(raw_payload) if raw_payload else "{}",
                    "error": str(e)[:500],
                    "status": "pending",
                }
            ).execute()
            logger.info("Failed message saved to dead-letter queue for retry")
        except Exception as dlq_err:
            # If even the dead-letter queue fails, at least we logged the error above
            logger.error(f"Dead-letter queue write also failed: {dlq_err}")


async def process_message(message, display_phone: str):
    """Process incoming WhatsApp message."""
    try:
        message_id = message.id

        # Resolve tenant clinic first to capture clinic attribution
        clinic = await resolve_tenant(display_phone)
        clinic_id = clinic.get("id") if clinic else None

        # ── Primary Idempotency: Atomic Supabase INSERT (closes the race window) ──
        acquired = await message_queue.acquire(message_id, clinic_id=clinic_id)
        if not acquired:
            logger.info(
                f"Webhook: duplicate message {message_id} dropped by atomic queue"
            )
            return
        # ── End Idempotency Gate ────────────────────────────────────────────────

        phone = normalize_phone(message.from_)
        message_type = message.type

        # Mark as read (fire-and-forget)
        import asyncio

        asyncio.create_task(whatsapp_service.mark_as_read(clinic, message_id))

        # Extract message content based on type
        content = ""
        interactive_data = None

        if message_type == "text" and message.text:
            content = message.text.body
        elif message_type == "button" and message.button:
            content = message.button.text
            interactive_data = {"id": message.button.payload, "type": "button"}
        elif message_type == "interactive" and message.interactive:
            if message.interactive.button_reply:
                reply = message.interactive.button_reply
                content = reply.get("title", "")
                interactive_data = {"id": reply.get("id"), "type": "button_reply"}
            elif message.interactive.list_reply:
                reply = message.interactive.list_reply
                content = reply.get("title", "")
                interactive_data = {"id": reply.get("id"), "type": "list_reply"}

        # Truncate content for safe logging (prevent log injection)
        safe_phone = phone[:6] + "..." if len(phone) > 6 else phone
        safe_content = content[:50].replace("\n", " ") if content else ""
        logger.info(
            f"[{clinic['name']}] Processing message from {safe_phone}: {safe_content}"
        )

        # Process through conversation manager
        await conversation_manager.handle_message(
            clinic=clinic,
            phone=phone,
            message=content,
            message_type=message_type,
            message_id=message_id,
            interactive_data=interactive_data,
        )

    except Exception as e:
        logger.error(f"Error processing message: {e}")
        raise  # Re-raise so process_message_safe can catch and log to DLQ


@router.post("/test")
async def test_webhook(phone: str, message: str, display_phone: str = None):
    """Test endpoint for simulating incoming messages.

    SECURITY: Only available in development/local environments.
    """
    # Block in production — this endpoint bypasses signature verification
    if settings.app_env == "production":
        raise HTTPException(
            status_code=403,
            detail="Test endpoint is disabled in production. Set APP_ENV=development to use.",
        )

    try:
        if not display_phone:
            # Fallback for testing backward compat
            display_phone = settings.hospital_phone

        clinic = await resolve_tenant(display_phone)
        phone = normalize_phone(phone)

        await conversation_manager.handle_message(
            clinic=clinic,
            phone=phone,
            message=message,
            message_type="text",
            message_id="test_" + str(hash(message + phone)),
        )
        return {
            "status": "ok",
            "message": f"Processed test message from {phone} to {clinic['name']}",
        }
    except Exception as e:
        logger.error(f"Error in test webhook: {e}")
        raise HTTPException(status_code=500, detail="Test failed")
