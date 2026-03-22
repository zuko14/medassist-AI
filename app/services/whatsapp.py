"""WhatsApp Cloud API service for sending messages."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com/v18.0"


class WhatsAppService:
    """Service for sending WhatsApp messages via Meta Cloud API."""

    def __init__(self):
        self.token = settings.whatsapp_token
        self.phone_number_id = settings.whatsapp_phone_number_id
        self.base_url = f"{WHATSAPP_API_BASE}/{self.phone_number_id}"
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

    def _mask_phone(self, phone: str) -> str:
        """Mask phone number for logging."""
        if len(phone) > 4:
            return phone[:3] + "X" * (len(phone) - 7) + phone[-4:]
        return "XXXX"

    async def _make_request(self, endpoint: str, payload: dict) -> dict:
        """Make HTTP request to WhatsApp API with retry."""
        url = f"{self.base_url}/{endpoint}"

        async with httpx.AsyncClient() as client:
            for attempt in range(2):  # 2 retries
                try:
                    response = await client.post(
                        url,
                        headers=self.headers,
                        json=payload,
                        timeout=10.0
                    )
                    response.raise_for_status()
                    return response.json()
                except httpx.HTTPStatusError as e:
                    logger.error(f"WhatsApp API error (attempt {attempt + 1}): {e.response.text}")
                    if attempt == 1:
                        raise
                except Exception as e:
                    logger.error(f"WhatsApp request error (attempt {attempt + 1}): {e}")
                    if attempt == 1:
                        raise

        return {}

    async def send_text(self, phone: str, message: str) -> bool:
        """Send a simple text message."""
        # Check session expiry before sending
        if not await self._can_send_freeform(phone):
            logger.warning(f"Cannot send freeform message to {self._mask_phone(phone)}: session expired")
            return False

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "text",
            "text": {"body": message}
        }

        try:
            result = await self._make_request("messages", payload)
            logger.info(f"Sent text message to {self._mask_phone(phone)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send text message: {e}")
            return False

    async def send_template(
        self,
        phone: str,
        template_name: str,
        language: str = "en",
        components: Optional[list] = None
    ) -> bool:
        """Send a pre-approved template message (for 24h+ sessions)."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components or []
            }
        }

        try:
            result = await self._make_request("messages", payload)
            logger.info(f"Sent template '{template_name}' to {self._mask_phone(phone)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send template message: {e}")
            return False

    async def send_interactive_buttons(
        self,
        phone: str,
        body: str,
        buttons: list[dict],
        header: Optional[str] = None
    ) -> bool:
        """Send interactive button message."""
        if not await self._can_send_freeform(phone):
            logger.warning(f"Cannot send interactive message to {self._mask_phone(phone)}: session expired")
            return False

        # Format buttons for WhatsApp API (max 3 buttons)
        formatted_buttons = []
        for i, btn in enumerate(buttons[:3]):
            formatted_buttons.append({
                "type": "reply",
                "reply": {
                    "id": btn.get("id", f"btn_{i}"),
                    "title": btn.get("title", "Option")[:20]  # Max 20 chars
                }
            })

        interactive = {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": formatted_buttons}
        }

        if header:
            interactive["header"] = {"type": "text", "text": header}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "interactive",
            "interactive": interactive
        }

        try:
            result = await self._make_request("messages", payload)
            logger.info(f"Sent interactive buttons to {self._mask_phone(phone)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send interactive buttons: {e}")
            return False

    async def send_interactive_list(
        self,
        phone: str,
        body: str,
        button_text: str,
        sections: list[dict],
        header: Optional[str] = None
    ) -> bool:
        """Send interactive list message."""
        if not await self._can_send_freeform(phone):
            logger.warning(f"Cannot send list message to {self._mask_phone(phone)}: session expired")
            return False

        # Format sections for WhatsApp API
        formatted_sections = []
        for section in sections:
            rows = []
            for row in section.get("rows", []):
                rows.append({
                    "id": row.get("id", "row_0"),
                    "title": row.get("title", "Option")[:24],  # Max 24 chars
                    "description": row.get("description", "")[:72]  # Max 72 chars
                })

            formatted_sections.append({
                "title": section.get("title", "Options")[:24],
                "rows": rows
            })

        interactive = {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_text[:20],  # Max 20 chars
                "sections": formatted_sections
            }
        }

        if header:
            interactive["header"] = {"type": "text", "text": header}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "interactive",
            "interactive": interactive
        }

        try:
            result = await self._make_request("messages", payload)
            logger.info(f"Sent interactive list to {self._mask_phone(phone)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send interactive list: {e}")
            return False

    async def send_location(
        self,
        phone: str,
        lat: float,
        lng: float,
        name: str,
        address: str
    ) -> bool:
        """Send location message."""
        if not await self._can_send_freeform(phone):
            logger.warning(f"Cannot send location to {self._mask_phone(phone)}: session expired")
            return False

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "location",
            "location": {
                "latitude": lat,
                "longitude": lng,
                "name": name,
                "address": address
            }
        }

        try:
            result = await self._make_request("messages", payload)
            logger.info(f"Sent location to {self._mask_phone(phone)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send location: {e}")
            return False

    async def mark_as_read(self, message_id: str) -> bool:
        """Mark a message as read."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id
        }

        try:
            result = await self._make_request("messages", payload)
            logger.info(f"Marked message {message_id} as read")
            return True
        except Exception as e:
            logger.error(f"Failed to mark message as read: {e}")
            return False

    async def _can_send_freeform(self, phone: str) -> bool:
        """Check if we can send freeform messages (within 24h window)."""
        from app.database import get_conversation

        try:
            conv = await get_conversation(phone)
            if not conv:
                return True  # New conversation, allow

            expires_at = conv.get("session_expires_at")
            if not expires_at:
                return True

            # Parse expiry time
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))

            return datetime.now(timezone.utc) < expires_at
        except Exception as e:
            logger.error(f"Error checking session expiry: {e}")
            return True  # Allow on error to prevent blocking


# Global instance
whatsapp_service = WhatsAppService()
