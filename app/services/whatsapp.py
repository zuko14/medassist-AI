"""WhatsApp Cloud API service for sending messages (Multi-Tenant Scoped).

INSTRUMENTED for outbound message accounting.
Every Meta API call is logged to the outbound_message_ledger via
message_accounting.log_outbound(). Logging is fire-and-forget — a
failed INSERT never blocks or delays message delivery.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
import httpx

from app.config import settings

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
        """Extract Meta API credentials from clinic config with global settings fallback."""
        config = clinic.get("config", {}) if isinstance(clinic, dict) else {}
        token = config.get("meta_access_token") or settings.whatsapp_token
        phone_id = config.get("meta_phone_number_id") or settings.whatsapp_phone_number_id

        if not token or not phone_id:
            logger.error(
                f"Missing WhatsApp credentials for clinic {clinic.get('id') if isinstance(clinic, dict) else 'unknown'}"
            )
            raise ValueError("Missing WhatsApp credentials")

        return token, phone_id

    def _extract_clinic_id(self, clinic: dict) -> Optional[str]:
        """Safely extract clinic_id for accounting. Returns None if unavailable."""
        if isinstance(clinic, dict):
            cid = clinic.get("id")
            if cid and cid != "default":
                return str(cid)
        return None

    async def _log_to_ledger(
        self,
        clinic: dict,
        phone: str,
        message_type: str,
        source_service: str,
        send_success: bool,
        meta_message_id: Optional[str] = None,
        template_name: Optional[str] = None,
    ) -> None:
        """Fire-and-forget ledger write. NEVER raises."""
        clinic_id = self._extract_clinic_id(clinic)
        if not clinic_id:
            return  # Cannot attribute to a tenant — skip ledger

        try:
            from app.services.message_accounting import log_outbound

            # Fire-and-forget: create_task so the caller is never blocked
            asyncio.create_task(
                log_outbound(
                    clinic_id=clinic_id,
                    recipient_phone=phone,
                    message_type=message_type,
                    source_service=source_service,
                    send_success=send_success,
                    meta_message_id=meta_message_id,
                    template_name=template_name,
                )
            )
        except Exception as e:
            # Absolute safety net — logging must never affect message delivery
            logger.debug(f"Ledger dispatch failed (non-fatal): {e}")

    async def _make_request(self, clinic: dict, endpoint: str, payload: dict) -> dict:
        """Make HTTP request to WhatsApp API with retry."""
        try:
            token, phone_id = self._get_credentials(clinic)
        except ValueError:
            return {}

        url = f"{WHATSAPP_API_BASE}/{phone_id}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            for attempt in range(3):
                try:
                    response = await client.post(
                        url, headers=headers, json=payload, timeout=10.0
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 2:
                            response.raise_for_status()
                        delay = float(response.headers.get("Retry-After", 2 ** attempt))
                        logger.warning(
                            f"Meta {response.status_code}, retrying in {delay}s "
                            f"(attempt {attempt + 1}/3)"
                        )
                        await asyncio.sleep(min(delay, 30))
                        continue
                    response.raise_for_status()
                    return response.json()
                except httpx.HTTPStatusError as e:
                    logger.error(
                        f"WhatsApp API error (attempt {attempt + 1}): {e.response.text}"
                    )
                    raise
                except httpx.RequestError as e:
                    logger.error(f"WhatsApp request error (attempt {attempt + 1}): {e}")
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2 ** attempt)

        return {}

    def _extract_meta_message_id(self, response: dict) -> Optional[str]:
        """Extract wamid from Meta API response."""
        messages = response.get("messages", [])
        if messages and isinstance(messages, list) and len(messages) > 0:
            return messages[0].get("id")
        return None

    async def send_text(
        self, clinic: dict, phone: str, message: str,
        _source: str = "conversation",
        _capture: Optional[dict] = None,
    ) -> bool:
        """Send a simple text message."""
        # Check session expiry before sending
        if not await self._can_send_freeform(clinic, phone):
            logger.warning(
                f"Cannot send freeform message to {self._mask_phone(phone)}: session expired"
            )
            return False

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "text",
            "text": {"body": message},
        }

        try:
            result = await self._make_request(clinic, "messages", payload)
            meta_msg_id = self._extract_meta_message_id(result)
            if _capture is not None:
                _capture["meta_message_id"] = meta_msg_id
            logger.info(f"Sent text message to {self._mask_phone(phone)}")

            # ── Accounting ──
            await self._log_to_ledger(
                clinic, phone, "text", _source,
                send_success=True, meta_message_id=meta_msg_id,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send text message: {e}")
            await self._log_to_ledger(
                clinic, phone, "text", _source, send_success=False,
            )
            return False

    async def send_template(
        self,
        clinic: dict,
        phone: str,
        template_name: str,
        language: str = "en",
        components: Optional[list] = None,
        _source: str = "conversation",
        _capture: Optional[dict] = None,
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
                "components": components or [],
            },
        }

        try:
            result = await self._make_request(clinic, "messages", payload)
            meta_msg_id = self._extract_meta_message_id(result)
            if _capture is not None:
                _capture["meta_message_id"] = meta_msg_id
            logger.info(f"Sent template '{template_name}' to {self._mask_phone(phone)}")

            # ── Accounting ──
            await self._log_to_ledger(
                clinic, phone, "template", _source,
                send_success=True, meta_message_id=meta_msg_id,
                template_name=template_name,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send template message: {e}")
            await self._log_to_ledger(
                clinic, phone, "template", _source,
                send_success=False, template_name=template_name,
            )
            return False

    async def send_interactive_buttons(
        self,
        clinic: dict,
        phone: str,
        body: str,
        buttons: list[dict],
        header: Optional[str] = None,
        _source: str = "conversation",
    ) -> bool:
        """Send interactive button message."""
        if not await self._can_send_freeform(clinic, phone):
            logger.warning(
                f"Cannot send interactive message to {self._mask_phone(phone)}: session expired"
            )
            return False

        formatted_buttons = []
        for i, btn in enumerate(buttons[:3]):
            formatted_buttons.append(
                {
                    "type": "reply",
                    "reply": {
                        "id": btn.get("id", f"btn_{i}"),
                        "title": btn.get("title", "Option")[:20],
                    },
                }
            )

        interactive = {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": formatted_buttons},
        }

        if header:
            interactive["header"] = {"type": "text", "text": header}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "interactive",
            "interactive": interactive,
        }

        try:
            result = await self._make_request(clinic, "messages", payload)
            meta_msg_id = self._extract_meta_message_id(result)
            logger.info(f"Sent interactive buttons to {self._mask_phone(phone)}")

            # ── Accounting ──
            await self._log_to_ledger(
                clinic, phone, "interactive_buttons", _source,
                send_success=True, meta_message_id=meta_msg_id,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send interactive buttons: {e}")
            await self._log_to_ledger(
                clinic, phone, "interactive_buttons", _source, send_success=False,
            )
            return False

    async def send_interactive_list(
        self,
        clinic: dict,
        phone: str,
        body: str,
        button_text: str,
        sections: list[dict],
        header: Optional[str] = None,
        _source: str = "conversation",
    ) -> bool:
        """Send interactive list message."""
        if not await self._can_send_freeform(clinic, phone):
            logger.warning(
                f"Cannot send list message to {self._mask_phone(phone)}: session expired"
            )
            return False

        formatted_sections = []
        for section in sections:
            rows = []
            for row in section.get("rows", []):
                rows.append(
                    {
                        "id": row.get("id", "row_0"),
                        "title": row.get("title", "Option")[:24],
                        "description": row.get("description", "")[:72],
                    }
                )

            formatted_sections.append(
                {"title": section.get("title", "Options")[:24], "rows": rows}
            )

        interactive = {
            "type": "list",
            "body": {"text": body},
            "action": {"button": button_text[:20], "sections": formatted_sections},
        }

        if header:
            interactive["header"] = {"type": "text", "text": header}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "interactive",
            "interactive": interactive,
        }

        try:
            result = await self._make_request(clinic, "messages", payload)
            meta_msg_id = self._extract_meta_message_id(result)
            logger.info(f"Sent interactive list to {self._mask_phone(phone)}")

            # ── Accounting ──
            await self._log_to_ledger(
                clinic, phone, "interactive_list", _source,
                send_success=True, meta_message_id=meta_msg_id,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send interactive list: {e}")
            await self._log_to_ledger(
                clinic, phone, "interactive_list", _source, send_success=False,
            )
            return False

    async def send_location(
        self, clinic: dict, phone: str, lat: float, lng: float, name: str, address: str,
        _source: str = "conversation",
    ) -> bool:
        """Send location message."""
        if not await self._can_send_freeform(clinic, phone):
            logger.warning(
                f"Cannot send location to {self._mask_phone(phone)}: session expired"
            )
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
                "address": address,
            },
        }

        try:
            result = await self._make_request(clinic, "messages", payload)
            meta_msg_id = self._extract_meta_message_id(result)
            logger.info(f"Sent location to {self._mask_phone(phone)}")

            # ── Accounting ──
            await self._log_to_ledger(
                clinic, phone, "location", _source,
                send_success=True, meta_message_id=meta_msg_id,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send location: {e}")
            await self._log_to_ledger(
                clinic, phone, "location", _source, send_success=False,
            )
            return False

    async def upload_media(
        self, clinic: dict, file_bytes: bytes, filename: str, content_type: str
    ) -> str:
        """Upload file to Meta media endpoint and return media_id."""
        try:
            token, phone_id = self._get_credentials(clinic)
        except ValueError:
            return ""

        url = f"{WHATSAPP_API_BASE}/{phone_id}/media"
        async with httpx.AsyncClient() as client:
            for attempt in range(2):
                try:
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        files={"file": (filename, file_bytes, content_type)},
                        data={"messaging_product": "whatsapp"},
                        timeout=30.0,
                    )
                    response.raise_for_status()
                    return response.json()["id"]
                except httpx.HTTPStatusError as e:
                    logger.error(
                        f"WhatsApp Media API error (attempt {attempt + 1}): {e.response.text}"
                    )
                    if attempt == 1:
                        raise
                except Exception as e:
                    logger.error(
                        f"WhatsApp Media request error (attempt {attempt + 1}): {e}"
                    )
                    if attempt == 1:
                        raise
        return ""

    async def send_document(
        self, clinic: dict, phone: str, media_id: str, filename: str, caption: str = "",
        _source: str = "conversation",
        _capture: Optional[dict] = None,
    ) -> bool:
        """Send a document message."""
        if not await self._can_send_freeform(clinic, phone):
            logger.warning(
                f"Cannot send document to {self._mask_phone(phone)}: session expired"
            )
            return False

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "document",
            "document": {"id": media_id, "filename": filename, "caption": caption},
        }

        try:
            result = await self._make_request(clinic, "messages", payload)
            meta_msg_id = self._extract_meta_message_id(result)
            if _capture is not None:
                _capture["meta_message_id"] = meta_msg_id
            logger.info(f"Sent document to {self._mask_phone(phone)}")

            # ── Accounting ──
            await self._log_to_ledger(
                clinic, phone, "document", _source,
                send_success=True, meta_message_id=meta_msg_id,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send document: {e}")
            await self._log_to_ledger(
                clinic, phone, "document", _source, send_success=False,
            )
            return False

    async def mark_as_read(self, clinic: dict, message_id: str) -> bool:
        """Mark a message as read."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }

        try:
            await self._make_request(clinic, "messages", payload)
            logger.info(f"Marked message {message_id} as read")
            # mark_as_read is NOT logged to the billing ledger — it's a
            # status update, not a billable outbound message.
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
                # Never messaged us => no customer-service window was ever
                # opened. Meta rejects freeform here (131047). Returning True
                # is why MocDoc walk-ins never received reports.
                return False

            expires_at = conv.get("session_expires_at")
            if not expires_at:
                return False

            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))

            return datetime.now(timezone.utc) < expires_at
        except Exception as e:
            logger.error(f"Error checking session expiry: {e}")
            return True


# Global instance
whatsapp_service = WhatsAppService()
