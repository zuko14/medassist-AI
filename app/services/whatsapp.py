"""WhatsApp Cloud API service for sending messages (Multi-Tenant Scoped)."""

import logging
from datetime import datetime, timezone
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com/v18.0"


class WhatsAppService:
    """Service for sending WhatsApp messages via Meta Cloud API."""

    def _mask_phone(self, phone: str) -> str:
        """Mask phone number for logging."""
        if len(phone) > 4:
            return phone[:3] + "X" * (len(phone) - 7) + phone[-4:]
        return "XXXX"

    def _get_credentials(self, clinic: dict) -> tuple[str, str]:
        """Extract Meta API credentials from clinic config."""
        config = clinic.get("config", {})
        token = config.get("meta_access_token")
        phone_id = config.get("meta_phone_number_id")
        
        if not token or not phone_id:
            logger.error(f"Missing WhatsApp credentials for clinic {clinic.get('id')}")
            raise ValueError("Missing WhatsApp credentials")
            
        return token, phone_id

    async def _make_request(self, clinic: dict, endpoint: str, payload: dict) -> dict:
        """Make HTTP request to WhatsApp API with retry."""
        try:
            token, phone_id = self._get_credentials(clinic)
        except ValueError:
            return {}
            
        url = f"{WHATSAPP_API_BASE}/{phone_id}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            for attempt in range(2):  # 2 retries
                try:
                    response = await client.post(
                        url,
                        headers=headers,
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

    async def send_text(self, clinic: dict, phone: str, message: str) -> bool:
        """Send a simple text message."""
        # Check session expiry before sending
        if not await self._can_send_freeform(clinic, phone):
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
            await self._make_request(clinic, "messages", payload)
            logger.info(f"Sent text message to {self._mask_phone(phone)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send text message: {e}")
            return False

    async def send_template(
        self,
        clinic: dict,
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
            await self._make_request(clinic, "messages", payload)
            logger.info(f"Sent template '{template_name}' to {self._mask_phone(phone)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send template message: {e}")
            return False

    async def send_interactive_buttons(
        self,
        clinic: dict,
        phone: str,
        body: str,
        buttons: list[dict],
        header: Optional[str] = None
    ) -> bool:
        """Send interactive button message."""
        if not await self._can_send_freeform(clinic, phone):
            logger.warning(f"Cannot send interactive message to {self._mask_phone(phone)}: session expired")
            return False

        formatted_buttons = []
        for i, btn in enumerate(buttons[:3]):
            formatted_buttons.append({
                "type": "reply",
                "reply": {
                    "id": btn.get("id", f"btn_{i}"),
                    "title": btn.get("title", "Option")[:20]
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
            await self._make_request(clinic, "messages", payload)
            logger.info(f"Sent interactive buttons to {self._mask_phone(phone)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send interactive buttons: {e}")
            return False

    async def send_interactive_list(
        self,
        clinic: dict,
        phone: str,
        body: str,
        button_text: str,
        sections: list[dict],
        header: Optional[str] = None
    ) -> bool:
        """Send interactive list message."""
        if not await self._can_send_freeform(clinic, phone):
            logger.warning(f"Cannot send list message to {self._mask_phone(phone)}: session expired")
            return False

        formatted_sections = []
        for section in sections:
            rows = []
            for row in section.get("rows", []):
                rows.append({
                    "id": row.get("id", "row_0"),
                    "title": row.get("title", "Option")[:24],
                    "description": row.get("description", "")[:72]
                })

            formatted_sections.append({
                "title": section.get("title", "Options")[:24],
                "rows": rows
            })

        interactive = {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_text[:20],
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
            await self._make_request(clinic, "messages", payload)
            logger.info(f"Sent interactive list to {self._mask_phone(phone)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send interactive list: {e}")
            return False

    async def send_location(
        self,
        clinic: dict,
        phone: str,
        lat: float,
        lng: float,
        name: str,
        address: str
    ) -> bool:
        """Send location message."""
        if not await self._can_send_freeform(clinic, phone):
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
            await self._make_request(clinic, "messages", payload)
            logger.info(f"Sent location to {self._mask_phone(phone)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send location: {e}")
            return False

    async def mark_as_read(self, clinic: dict, message_id: str) -> bool:
        """Mark a message as read."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id
        }

        try:
            await self._make_request(clinic, "messages", payload)
            logger.info(f"Marked message {message_id} as read")
            return True
        except Exception as e:
            logger.error(f"Failed to mark message as read: {e}")
            return False

    async def _can_send_freeform(self, clinic: dict, phone: str) -> bool:
        """Check if we can send freeform messages (within 24h window)."""
        from app.database import get_conversation

        try:
            conv = await get_conversation(clinic["id"], phone)
            if not conv:
                return True

            expires_at = conv.get("session_expires_at")
            if not expires_at:
                return True

            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))

            return datetime.now(timezone.utc) < expires_at
        except Exception as e:
            logger.error(f"Error checking session expiry: {e}")
            return True


# Global instance
whatsapp_service = WhatsAppService()
