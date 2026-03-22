"""Webhook router for WhatsApp Cloud API."""

import logging
from fastapi import APIRouter, Request, HTTPException, Query, BackgroundTasks
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.models.message import WhatsAppWebhookPayload
from app.services.conversation import conversation_manager
from app.services.whatsapp import whatsapp_service
from app.utils.validators import normalize_phone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


@router.get("")
async def verify_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge")
):
    """Verify webhook for Meta WhatsApp Cloud API."""
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        logger.info("Webhook verified successfully")
        return PlainTextResponse(content=hub_challenge)

    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive webhook events from WhatsApp Cloud API."""
    try:
        body = await request.json()
        logger.debug(f"Received webhook: {body}")

        # Parse payload
        payload = WhatsAppWebhookPayload(**body)

        for entry in payload.entry:
            for change in entry.changes:
                if change.value.messages:
                    for message in change.value.messages:
                        background_tasks.add_task(process_message, message, background_tasks)

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        # Never expose stack traces in webhook responses
        return {"status": "error"}


async def process_message(message, background_tasks: BackgroundTasks):
    """Process incoming WhatsApp message."""
    try:
        phone = normalize_phone(message.from_)
        message_id = message.id
        message_type = message.type
        
        background_tasks.add_task(whatsapp_service.mark_as_read, message_id)

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

        logger.info(f"Processing message from {phone[:6]}...: {content[:50]}")

        # Process through conversation manager
        await conversation_manager.handle_message(
            phone=phone,
            message=content,
            message_type=message_type,
            message_id=message_id,
            interactive_data=interactive_data
        )

    except Exception as e:
        logger.error(f"Error processing message: {e}")
        # Don't raise - webhook should return 200


@router.post("/test")
async def test_webhook(phone: str, message: str):
    """Test endpoint for simulating incoming messages."""
    try:
        phone = normalize_phone(phone)
        await conversation_manager.handle_message(
            phone=phone,
            message=message,
            message_type="text",
            message_id="test_" + str(hash(message + phone))
        )
        return {"status": "ok", "message": f"Processed test message from {phone}"}
    except Exception as e:
        logger.error(f"Error in test webhook: {e}")
        raise HTTPException(status_code=500, detail="Test failed")
