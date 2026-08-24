"""WhatsApp Cloud API service for sending messages (Multi-Tenant Scoped).

INSTRUMENTED for outbound message accounting.
Every Meta API call is logged to the outbound_message_ledger via
message_accounting.log_outbound(). Logging is fire-and-forget — a
failed INSERT never blocks or delays message delivery.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com/v21.0"


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
        """Log outbound message send attempt to ledger in background task."""
        try:
            clinic_id = self._extract_clinic_id(clinic)
            if not clinic_id:
                return

            from app.services.message_accounting import log_outbound

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
        """Make HTTP request to WhatsApp API with retry + exponential backoff + jitter."""
        try:
            token, phone_id = self._get_credentials(clinic)
        except ValueError:
            return {}

        url = f"{WHATSAPP_API_BASE}/{phone_id}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # Document sends (or templates with document headers) involve larger payloads or Meta fetching external media — give more time and retries
        is_document = (
            payload.get("type") == "document"
            or any(
                c.get("type") == "header" and any(p.get("type") == "document" for p in c.get("parameters", []))
                for c in payload.get("template", {}).get("components", [])
            )
        )
        timeout = 20.0 if is_document else 10.0
        max_attempts = 4 if is_document else 3

        async with httpx.AsyncClient() as client:
            for attempt in range(max_attempts):
                try:
                    response = await client.post(
                        url, headers=headers, json=payload, timeout=timeout
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == max_attempts - 1:
                            response.raise_for_status()
                        # Exponential backoff with jitter to avoid thundering herd
                        base_delay = float(response.headers.get("Retry-After", 2 ** attempt))
                        jitter = random.uniform(0, base_delay * 0.5)
                        delay = min(base_delay + jitter, 30)
                        # Extract fbtrace_id for Meta support escalation
                        fbtrace = ""
                        try:
                            err_body = response.json()
                            fbtrace = err_body.get("error", {}).get("fbtrace_id", "")
                        except Exception:
                            pass
                        logger.warning(
                            f"Meta {response.status_code}, retrying in {delay:.1f}s "
                            f"(attempt {attempt + 1}/{max_attempts})"
                            f"{f' fbtrace_id={fbtrace}' if fbtrace else ''}"
                        )
                        await asyncio.sleep(delay)
                        continue
                    response.raise_for_status()
                    return response.json()
                except httpx.HTTPStatusError as e:
                    fbtrace = ""
                    try:
                        err_body = e.response.json()
                        fbtrace = err_body.get("error", {}).get("fbtrace_id", "")
                    except Exception:
                        pass
                    logger.error(
                        f"WhatsApp API error (attempt {attempt + 1}): {e.response.text}"
                        f"{f' fbtrace_id={fbtrace}' if fbtrace else ''}"
                    )
                    raise
                except httpx.RequestError as e:
                    logger.error(f"WhatsApp request error (attempt {attempt + 1}): {e}")
                    if attempt == max_attempts - 1:
                        raise
                    await asyncio.sleep(2 ** attempt + random.uniform(0, 1))

        return {}

    def _extract_meta_message_id(self, response: dict) -> Optional[str]:
        """Extract wamid from Meta API response."""
        messages = response.get("messages", [])
        if messages and isinstance(messages, list) and len(messages) > 0:
            return messages[0].get("id")
        return None

    async def _can_send_freeform(self, clinic: dict, phone: str) -> bool:
        """Check if patient is within 24h customer service window."""
        try:
            from app.services.conversation import get_last_patient_message_timestamp

            last_msg_time = await get_last_patient_message_timestamp(phone)
            if not last_msg_time:
                return False

            now = datetime.now(timezone.utc)
            delta = now - last_msg_time
            return delta.total_seconds() < 24 * 3600
        except Exception as e:
            logger.warning(f"Could not check 24h window for {self._mask_phone(phone)}: {e}")
            return False

    async def send_text(
        self,
        clinic: dict,
        phone: str,
        message: str,
        _source: str = "conversation",
        _capture: Optional[dict] = None,
    ) -> bool:
        """Send a freeform text message (only if within 24h window)."""
        if not await self._can_send_freeform(clinic, phone):
            logger.warning(
                f"Cannot send freeform text to {self._mask_phone(phone)}: session expired"
            )
            return False

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "text",
            "text": {"preview_url": False, "body": message},
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
            await self._log_to_ledger(clinic, phone, "text", _source, send_success=False)
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
        """Send a pre-approved template message (for 24h+ sessions).

        Resilient delivery matrix:
        1. Tries configured template name, falling back to registered template aliases.
        2. Tries candidate language codes (en vs en_US).
        3. Tries full components (header + body), falling back to body-only.
        """
        clean_template_name = (template_name or "").strip()
        if not clean_template_name:
            logger.error("send_template called with empty template_name")
            return False

        # Candidate template names to check in case of naming variance
        candidate_templates = [clean_template_name]
        for alias in ("lab_report_ready", "lab_report_delivery", "lab_report_summary", "callmedex_lab_report_summary"):
            if alias not in candidate_templates:
                candidate_templates.append(alias)

        # Prepare candidate language codes to handle en vs en_US vs en_GB matching in Meta
        candidate_languages = [language]
        if language in ("en", "en_US", "en_GB"):
            for l in ("en", "en_US", "en_GB"):
                if l not in candidate_languages:
                    candidate_languages.append(l)

        has_header = any(c.get("type") == "header" for c in (components or []))
        body_only = [c for c in (components or []) if c.get("type") != "header"] if has_header else None

        last_error = None
        for tmpl in candidate_templates:
            for lang in candidate_languages:
                # ── Attempt 1: Full template with all components (including header) ──
                payload = {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": phone,
                    "type": "template",
                    "template": {
                        "name": tmpl,
                        "language": {"code": lang},
                        "components": components or [],
                    },
                }
                try:
                    result = await self._make_request(clinic, "messages", payload)
                    meta_msg_id = self._extract_meta_message_id(result)
                    if _capture is not None:
                        _capture["meta_message_id"] = meta_msg_id
                    logger.info(f"Sent template '{tmpl}' ({lang}) to {self._mask_phone(phone)}")

                    await self._log_to_ledger(
                        clinic, phone, "template", _source,
                        send_success=True, meta_message_id=meta_msg_id,
                        template_name=tmpl,
                    )
                    return True
                except Exception as e:
                    last_error = e
                    logger.debug(
                        f"Template '{tmpl}' ({lang}) with header attempt failed: {e}"
                    )

                # ── Attempt 2: If had header and failed, try body-only with this language ──
                if has_header and body_only is not None:
                    payload["template"]["components"] = body_only
                    try:
                        result = await self._make_request(clinic, "messages", payload)
                        meta_msg_id = self._extract_meta_message_id(result)
                        if _capture is not None:
                            _capture["meta_message_id"] = meta_msg_id
                            _capture["header_stripped"] = True
                        logger.info(
                            f"Sent template '{tmpl}' ({lang}, body-only) to "
                            f"{self._mask_phone(phone)}"
                        )
                        await self._log_to_ledger(
                            clinic, phone, "template", _source,
                            send_success=True, meta_message_id=meta_msg_id,
                            template_name=tmpl,
                        )
                        return True
                    except Exception as body_err:
                        last_error = body_err
                        logger.debug(
                            f"Template '{tmpl}' ({lang}, body-only) failed: {body_err}"
                        )

        logger.error(f"Failed to send template after trying {candidate_templates} across {candidate_languages}: {last_error}")
        await self._log_to_ledger(
            clinic, phone, "template", _source,
            send_success=False, template_name=clean_template_name,
        )

        # Propagate server errors so caller transient detection can queue retry
        if last_error:
            err_text = str(last_error)
            resp = getattr(last_error, "response", None)
            is_server_error = False
            if resp is not None:
                is_server_error = resp.status_code >= 500
            elif "500" in err_text or "Server Error" in err_text:
                is_server_error = True

            if is_server_error:
                raise last_error

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
        """Upload file to Meta media endpoint and return media_id.

        Uses 3 attempts with exponential backoff + jitter.
        """
        try:
            token, phone_id = self._get_credentials(clinic)
        except ValueError:
            return ""

        url = f"{WHATSAPP_API_BASE}/{phone_id}/media"
        mime_type = content_type or "application/pdf"
        max_attempts = 3
        async with httpx.AsyncClient() as client:
            for attempt in range(max_attempts):
                try:
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        data={"messaging_product": "whatsapp"},
                        files={"file": (filename, file_bytes, mime_type)},
                        timeout=30.0,
                    )
                    response.raise_for_status()
                    media_id = response.json()["id"]
                    logger.info(f"Media uploaded successfully: {media_id}")
                    return media_id
                except httpx.HTTPStatusError as e:
                    fbtrace = ""
                    try:
                        fbtrace = e.response.json().get("error", {}).get("fbtrace_id", "")
                    except Exception:
                        pass
                    logger.error(
                        f"WhatsApp Media API error (attempt {attempt + 1}/{max_attempts}): "
                        f"{e.response.text}{f' fbtrace_id={fbtrace}' if fbtrace else ''}"
                    )
                    if attempt == max_attempts - 1:
                        return ""
                    await asyncio.sleep(2 ** attempt + random.uniform(0, 1))
                except Exception as e:
                    logger.error(
                        f"WhatsApp Media request error (attempt {attempt + 1}/{max_attempts}): {e}"
                    )
                    if attempt == max_attempts - 1:
                        return ""
                    await asyncio.sleep(2 ** attempt + random.uniform(0, 1))
        return ""

    async def send_document(
        self, clinic: dict, phone: str, media_id: str, filename: str, caption: str = "",
        _source: str = "conversation",
        _capture: Optional[dict] = None,
        _fallback_file_bytes: Optional[bytes] = None,
        _fallback_content_type: str = "application/pdf",
    ) -> bool:
        """Send a document message with automatic link→upload fallback.

        If media_id is a URL (link-based delivery) and Meta returns a 500 error,
        automatically falls back to uploading the file via upload_media and
        resending with document.id — provided _fallback_file_bytes is supplied.
        """
        if not await self._can_send_freeform(clinic, phone):
            logger.warning(
                f"Cannot send document to {self._mask_phone(phone)}: session expired"
            )
            return False

        is_link = media_id.startswith("http://") or media_id.startswith("https://")

        doc_obj = {"filename": filename}
        if caption:
            doc_obj["caption"] = caption
        if is_link:
            doc_obj["link"] = media_id
        else:
            doc_obj["id"] = media_id

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "document",
            "document": doc_obj,
        }

        try:
            result = await self._make_request(clinic, "messages", payload)
            meta_msg_id = self._extract_meta_message_id(result)
            if _capture is not None:
                _capture["meta_message_id"] = meta_msg_id
            logger.info(f"Sent document to {self._mask_phone(phone)} via {'link' if is_link else 'media_id'}")

            # ── Accounting ──
            await self._log_to_ledger(
                clinic, phone, "document", _source,
                send_success=True, meta_message_id=meta_msg_id,
            )
            return True
        except Exception as e:
            # ── Automatic fallback: link → upload ──
            # If we sent with document.link and Meta returned a server error,
            # try uploading the file directly and resend with document.id.
            if is_link and _fallback_file_bytes:
                err_text = str(e)
                resp = getattr(e, "response", None)
                is_server_error = False
                if resp is not None:
                    is_server_error = resp.status_code >= 500
                elif "500" in err_text or "Server Error" in err_text:
                    is_server_error = True

                if is_server_error:
                    logger.warning(
                        f"Document link delivery failed (Meta 500) — falling back to media upload "
                        f"for {self._mask_phone(phone)}"
                    )
                    try:
                        uploaded_id = await self.upload_media(
                            clinic, _fallback_file_bytes, filename, _fallback_content_type
                        )
                        if uploaded_id:
                            # Rebuild payload with media ID instead of link
                            fallback_doc = {"filename": filename, "id": uploaded_id}
                            if caption:
                                fallback_doc["caption"] = caption
                            fallback_payload = {
                                "messaging_product": "whatsapp",
                                "recipient_type": "individual",
                                "to": phone,
                                "type": "document",
                                "document": fallback_doc,
                            }
                            result = await self._make_request(clinic, "messages", fallback_payload)
                            meta_msg_id = self._extract_meta_message_id(result)
                            if _capture is not None:
                                _capture["meta_message_id"] = meta_msg_id
                                _capture["delivery_method"] = "upload_fallback"
                            logger.info(
                                f"Sent document to {self._mask_phone(phone)} via upload fallback"
                            )
                            await self._log_to_ledger(
                                clinic, phone, "document", _source,
                                send_success=True, meta_message_id=meta_msg_id,
                            )
                            return True
                    except Exception as fallback_err:
                        logger.error(
                            f"Upload fallback also failed for {self._mask_phone(phone)}: {fallback_err}"
                        )

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
            logger.error(f"Error checking session expiry (failing closed to template): {e}")
            return False


# Global instance
whatsapp_service = WhatsAppService()
