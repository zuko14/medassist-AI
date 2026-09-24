"""CallMedex Callback Handler (Phase 3 Implementation)."""

import hmac
import hashlib
import logging
import httpx
from typing import Optional
from app.integrations.callmedex.callbacks.base import BaseCallbackHandler
from app.integrations.callmedex.api.schemas import CallbackStatusPayload
from app.integrations.callmedex.config.settings import callmedex_settings

logger = logging.getLogger(__name__)


class CallMedexCallbackHandler(BaseCallbackHandler):
    """Handles sending HMAC-signed status callback webhooks to CallMedex."""

    def __init__(self, secret: Optional[str] = None):
        self.secret = secret or callmedex_settings.hmac_signature_secret.get_secret_value()

    async def send_status_callback(
        self, payload: CallbackStatusPayload
    ) -> bool:
        """Dispatch signed status callback to callback endpoint."""
        raw_body = payload.model_dump_json().encode("utf-8")
        signature = hmac.new(
            self.secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()

        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        headers = {
            "Content-Type": "application/json",
            "X-Signature-256": signature,
            "X-Correlation-ID": payload.correlation_id,
            "X-Timestamp": ts,
        }

        logger.info(
            f"Dispatching Callback [Task: {payload.task_id} | Status: {payload.status}] "
            f"to {callmedex_settings.callmedex_callback_url}"
        )

        target_url = callmedex_settings.callmedex_callback_url

        # Offline sandbox/test mode transport bypass
        if callmedex_settings.app_env in ("test", "sandbox", "development"):
            logger.info(f"Sandbox Callback Delivered (Signed HMAC OK) for task {payload.task_id}")
            return True


        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    target_url,
                    content=raw_body,
                    headers=headers,
                )
                if response.status_code == 200:
                    logger.info(f"Callback delivered successfully for task {payload.task_id}")
                    return True
                else:
                    logger.warning(
                        f"Callback HTTP {response.status_code} for task {payload.task_id}: {response.text[:100]}"
                    )
                    return False
        except Exception as e:
            logger.error(f"Callback transport failed for task {payload.task_id}: {e}")
            return False


    # ── Per-event callbacks to CallMedex's dedicated routes ─────────────────
    # POST {CALLMEDEX_BASE_URL}/api/v1/integrations/mediassist/callbacks/<event>
    # Only meaningful for jobs CallMedex created: report_job_id is CallMedex's
    # own id (its routes 404 anything else). No environment bypass — a
    # configured CALLMEDEX_BASE_URL is the only switch. Never raises.

    async def _post_report_event(self, event: str, body: dict, correlation_id: str) -> bool:
        from app.integrations.callmedex.api import client as callmedex_client

        resp = await callmedex_client.request(
            "POST",
            f"/callbacks/{event}",
            json_body=body,
            idem_key=callmedex_client.idempotency_key(body["report_job_id"], event),
            correlation_id=correlation_id,
        )
        ok = resp is not None and resp.status_code == 200
        logger.info(
            f"CallMedex callback {event} for report_job {body['report_job_id']}: "
            f"{'delivered' if ok else 'NOT delivered'}"
            f"{f' (HTTP {resp.status_code})' if resp is not None else ''}"
        )
        return ok

    async def send_report_accepted(self, report_job_id: str, occurred_at: str, correlation_id: str) -> bool:
        return await self._post_report_event(
            "report-accepted", {"report_job_id": report_job_id, "occurred_at": occurred_at}, correlation_id
        )

    async def send_report_processing(self, report_job_id: str, occurred_at: str, correlation_id: str) -> bool:
        return await self._post_report_event(
            "report-processing", {"report_job_id": report_job_id, "occurred_at": occurred_at}, correlation_id
        )

    async def send_report_delivered(
        self,
        report_job_id: str,
        occurred_at: str,
        message_id: Optional[str],
        analysis_payload: dict,
        correlation_id: str,
    ) -> bool:
        return await self._post_report_event(
            "report-delivered",
            {
                "report_job_id": report_job_id,
                "occurred_at": occurred_at,
                "delivered_channel": "whatsapp",
                "message_id": message_id,
                "analysis": analysis_payload,
            },
            correlation_id,
        )

    async def send_report_failed(
        self,
        report_job_id: str,
        occurred_at: str,
        failure_reason: str,
        details: Optional[str],
        correlation_id: str,
    ) -> bool:
        return await self._post_report_event(
            "report-failed",
            {
                "report_job_id": report_job_id,
                "occurred_at": occurred_at,
                "failure_reason": failure_reason,
                "details": (details or "")[:500] or None,
            },
            correlation_id,
        )

    async def verify_signature(
        self, raw_body: bytes, signature_header: str
    ) -> bool:
        """Verify HMAC-SHA256 signature of incoming webhooks."""
        expected_sig = hmac.new(
            self.secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected_sig, signature_header)


def build_analysis_payload(summary_report=None, canonical_report=None) -> dict:
    """`analysis` object for report-delivered, shaped like CallMedex's
    ReportAnalysisPayload. Only what the pipeline actually produced — no
    health_score or recommendations are invented (both nullable/empty in the
    contract). Empty summaries when OCR/AI failed and the fallback delivered."""
    abnormal = []
    if canonical_report is not None:
        for t in canonical_report.tests:
            status = getattr(t.flag, "value", str(t.flag))
            if status in ("high", "low", "critical"):
                abnormal.append({
                    "marker": t.display_name,
                    "value": f"{t.value:g} {t.unit}".strip(),
                    "status": status,
                    "reference_range": t.reference_range,
                })
    return {
        "plain_language_summary": (
            " ".join(s.statement for s in summary_report.patient_summary) if summary_report else ""
        ),
        "doctor_clinical_summary": (
            " ".join(s.statement for s in summary_report.clinician_summary) if summary_report else ""
        ),
        "health_score": None,
        "abnormal_flags": abnormal,
        "recommendations": [],
    }
