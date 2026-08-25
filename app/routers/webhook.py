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

from app.database import supabase
from app.services.message_queue import message_queue
from app.services.metrics import metrics
from app.utils.correlation import set_correlation_id

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
                    phone_number_id = metadata.get("phone_number_id")  # Immutable Meta ID

                    if not display_phone:
                        logger.error(
                            "No display_phone_number found in webhook metadata"
                        )
                        continue

                    for message in change.value.messages:
                        phone = normalize_phone(getattr(message, "from_", ""))
                        clinic_id = None
                        try:
                            clinic = await resolve_tenant(display_phone, phone_number_id=phone_number_id)
                            if clinic:
                                clinic_id = clinic.get("id")
                        except Exception:
                            pass

                        # ── Durable Ingestion Boundary: Persist BEFORE returning HTTP 200 ──
                        is_new, _ = await message_queue.ingest(
                            message_id=message.id,
                            phone=phone,
                            display_phone=display_phone,
                            payload=body,
                            clinic_id=clinic_id,
                            phone_number_id=phone_number_id,
                        )

                        if is_new:
                            metrics.inc_counter("kriya_inbound_messages_total", 1, {"status": "received"})
                            background_tasks.add_task(
                                process_message_safe, message, display_phone, body, phone_number_id
                            )
                            logger.info(f"Durable queue: ingested & dispatched {message.id}")
                        else:
                            metrics.inc_counter("kriya_inbound_messages_total", 1, {"status": "duplicate"})
                            logger.info(f"Durable queue: dropped duplicate {message.id}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        # Never expose stack traces in webhook responses
        return {"status": "error"}


_DELIVERY_RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}


async def record_delivery_status(status: dict) -> None:
    """Persist a Meta delivery receipt with monotonic rank enforcement (H2).

    Without this a report reads status='sent' while Meta already reported it
    undeliverable — invisible to staff. Out-of-order receipts (e.g. delivered after read)
    are ignored.
    """
    wamid = status.get("id")
    state = status.get("status")
    if not wamid or not state:
        return
    err = (status.get("errors") or [{}])[0]
    new_rank = _DELIVERY_RANK.get(state, 0)
    try:
        from app.database import supabase
        # unscoped: global Meta callback lookup by unique whatsapp_message_id
        curr_row = supabase.table("lab_reports").select("delivery_status").eq("whatsapp_message_id", wamid).execute()
        if curr_row and curr_row.data:
            old_state = curr_row.data[0].get("delivery_status") or ""
            old_rank = _DELIVERY_RANK.get(old_state, 0)
            if old_rank > new_rank and state != "failed":
                logger.info(
                    f"Ignoring out-of-order delivery status {state} (rank {new_rank}) for {wamid} which is already {old_state} (rank {old_rank})"
                )
                return

        # unscoped: global Meta callback update by unique whatsapp_message_id
        supabase.table("lab_reports").update({
            "delivery_status": state,
            "delivery_error": err.get("title") or err.get("message") if err else None,
            "delivery_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("whatsapp_message_id", wamid).execute()
        if state == "failed":
            logger.error(f"Meta delivery FAILED for wamid {wamid}: {err}")
    except Exception as e:
        logger.warning(f"Could not record delivery status for {wamid}: {e}")


async def process_message_safe(message, display_phone: str, raw_payload: dict, phone_number_id: str = None):
    """Wrapper that claims, processes, and marks completed or failed in durable queue.

    In a hospital bot, silently dropping a patient message is unacceptable.
    If processing fails, the message is marked failed_retryable with bounded exponential backoff
    or moved to dead_letter if retries are exhausted.
    """
    message_id = getattr(message, "id", "unknown")
    try:
        await process_message(message, display_phone, phone_number_id)
        await message_queue.mark_completed(message_id)
    except Exception as e:
        logger.error(f"Message processing failed for {message_id}: {e}")
        await message_queue.mark_failed(message_id, str(e), max_retries=3)
        try:
            await message_queue.release(message_id)
        except Exception as rel_err:
            logger.warning(f"Failed to release message lock for {message_id}: {rel_err}")


async def process_message(message, display_phone: str, phone_number_id: str = None):
    """Process incoming WhatsApp message."""
    try:
        message_id = message.id

        # Resolve tenant clinic first to capture clinic attribution
        try:
            clinic = await resolve_tenant(display_phone, phone_number_id=phone_number_id)
        except Exception as ten_err:
            logger.error(f"Could not resolve tenant for display_phone={display_phone} phone_number_id={phone_number_id}: {ten_err}")
            raise

        if not clinic:
            logger.error(f"Tenant resolution returned None for display_phone={display_phone}")
            return

        clinic_id = clinic.get("id")

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

        # Mark as read (fire-and-forget, with strong task reference)
        from app.utils.async_tasks import spawn_background_task

        spawn_background_task(
            whatsapp_service.mark_as_read(clinic, message_id),
            name=f"mark_as_read_{message_id}",
        )

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
            f"[{clinic.get('name', 'Clinic')}] Processing message from {safe_phone}: {safe_content}"
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
