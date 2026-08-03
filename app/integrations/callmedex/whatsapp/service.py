"""Phase 7 WhatsApp Delivery & Template Messaging Service."""

import uuid
import logging
import httpx
from typing import Optional
from datetime import datetime, timezone
from app.integrations.callmedex.ai.schemas import MultiAudienceSummaryReport
from app.integrations.callmedex.callbacks.handler import CallMedexCallbackHandler
from app.integrations.callmedex.api.schemas import CallbackStatusPayload, TaskStatus, ConnectorType
from app.integrations.callmedex.config.settings import callmedex_settings
from app.integrations.callmedex.whatsapp.schemas import (
    WhatsAppDeliveryResult,
    WhatsAppDeliveryStatus,
    WhatsAppTemplatePayload,
)

logger = logging.getLogger(__name__)


class WhatsAppDeliveryService:
    """Phase 7 WhatsApp Delivery Service.

    Consumes PDF report + MultiAudienceSummaryReport from Phase 6.
    Assembles and sends Meta WhatsApp Cloud API template/document message.
    Dispatches HMAC-SHA256 signed callback via CallMedexCallbackHandler.
    """

    def __init__(self, callback_handler: Optional[CallMedexCallbackHandler] = None):
        self._callback_handler = callback_handler or CallMedexCallbackHandler()

    async def _send_meta_whatsapp_cloud_api(
        self,
        phone_number: str,
        payload: WhatsAppTemplatePayload,
    ) -> tuple[WhatsAppDeliveryStatus, str]:
        """Execute HTTP POST request to Meta WhatsApp Cloud API endpoint."""
        token = callmedex_settings.whatsapp_api_token.get_secret_value()
        phone_id = callmedex_settings.whatsapp_phone_number_id

        if not token or not phone_id or token in ("dev_whatsapp_token", "change_in_prod"):
            logger.info("WhatsApp Cloud API credentials not configured/placeholder — test simulation mode active")
            msg_id = f"wmid.callmedex.sim.{uuid.uuid4().hex[:12]}"
            return WhatsAppDeliveryStatus.DELIVERED, msg_id

        url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        body_payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone_number,
            "type": "template",
            "template": {
                "name": "lab_report_summary",
                "language": {"code": payload.language_code},
                "components": [
                    {
                        "type": "header",
                        "parameters": [
                            {
                                "type": "document",
                                "document": {"link": payload.header_pdf_url, "filename": "LabReport.pdf"},
                            }
                        ],
                    },
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": payload.body_text_summary[:1024]},
                            {"type": "text", "text": payload.disclaimer_text[:1024]},
                        ],
                    },
                ],
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, headers=headers, json=body_payload)
                res.raise_for_status()
                data = res.json()
                msg_id = data.get("messages", [{}])[0].get("id", f"wmid.callmedex.{uuid.uuid4().hex[:12]}")
                logger.info(f"Successfully sent Meta WhatsApp message to {phone_number} | msg_id={msg_id}")
                return WhatsAppDeliveryStatus.DELIVERED, msg_id
        except Exception as err:
            logger.error(f"Meta WhatsApp Cloud API delivery failed for {phone_number}: {err}")
            return WhatsAppDeliveryStatus.FAILED, ""

    async def deliver_report_and_summary(
        self,
        phone_number: str,
        pdf_storage_url: str,
        summary_report: MultiAudienceSummaryReport,
        report_job_id: str,
        correlation_id: str,
        callback_url: Optional[str] = None,
    ) -> WhatsAppDeliveryResult:
        """Format Meta WhatsApp Cloud API template payload, execute real send, and dispatch signed callback."""
        logger.info(f"Phase 7 WhatsApp Delivery: Sending report '{report_job_id}' to '{phone_number}'")

        patient_text = " ".join([stmt.statement for stmt in summary_report.patient_summary])

        payload = WhatsAppTemplatePayload(
            to=phone_number,
            language_code=summary_report.language.value,
            header_pdf_url=pdf_storage_url,
            body_text_summary=patient_text,
            disclaimer_text=summary_report.medical_disclaimer,
        )

        delivery_status, message_id = await self._send_meta_whatsapp_cloud_api(phone_number, payload)

        callback_ok = False
        if callback_url:
            task_status = (
                TaskStatus.COMPLETED
                if (summary_report.status.value in ["success", "flagged_for_review"] and delivery_status == WhatsAppDeliveryStatus.DELIVERED)
                else TaskStatus.FAILED
            )
            callback_payload = CallbackStatusPayload(
                task_id=report_job_id,
                clinic_id="visakha-multispeciality-clinics",
                connector_type=ConnectorType.MOCDOC,
                external_report_id=report_job_id,
                status=task_status,
                error_message=None if task_status == TaskStatus.COMPLETED else f"WhatsApp delivery status: {delivery_status.value}",
                correlation_id=correlation_id,
            )
            callback_ok = await self._callback_handler.send_status_callback(callback_payload)

        delivery_result = WhatsAppDeliveryResult(
            message_id=message_id or f"wmid.callmedex.failed.{uuid.uuid4().hex[:8]}",
            status=delivery_status,
            phone_number=phone_number,
            callback_delivered=callback_ok,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            f"Phase 7 WhatsApp Delivery Complete: msg_id={delivery_result.message_id} | "
            f"status={delivery_result.status.value} | callback_ok={callback_ok}"
        )
        return delivery_result
