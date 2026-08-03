"""Phase 7 WhatsApp Delivery & Template Messaging Test Suite."""

import pytest
from app.integrations.callmedex.ai.schemas import (
    MultiAudienceSummaryReport,
    StatementProvenance,
    SummaryLanguage,
    SummaryStatus,
)
from app.integrations.callmedex.whatsapp.service import WhatsAppDeliveryService
from app.integrations.callmedex.whatsapp.schemas import WhatsAppDeliveryStatus


@pytest.mark.asyncio
async def test_phase7_whatsapp_delivery_and_callback():
    """Verify WhatsApp media template assembly and HMAC-SHA256 signed callback dispatch."""
    service = WhatsAppDeliveryService()

    summary_report = MultiAudienceSummaryReport(
        patient_summary=[
            StatementProvenance(
                statement="Hemoglobin measured at 13.6 g/dL (Reference: 13.0-17.0).",
                supported_by=["HB"],
            )
        ],
        clinician_summary=[
            StatementProvenance(
                statement="HB: 13.6 g/dL [13.0-17.0] - Flag: NORMAL.",
                supported_by=["HB"],
            )
        ],
        medical_disclaimer="DISCLAIMER: Informational summary only.",
        language=SummaryLanguage.ENGLISH,
        status=SummaryStatus.SUCCESS,
        overall_confidence=0.98,
        review_flagged=False,
    )

    result = await service.deliver_report_and_summary(
        phone_number="+919966773300",
        pdf_storage_url="https://storage.callmedex.org/reports/REP-999.pdf",
        summary_report=summary_report,
        report_job_id="JOB-2026-999",
        correlation_id="CORR-2026-999",
        callback_url="https://api.callmedex.org/webhooks/report-status",
    )

    assert result.status == WhatsAppDeliveryStatus.DELIVERED
    assert result.phone_number == "+919966773300"
    assert result.message_id.startswith("wmid.callmedex.")
    assert result.callback_delivered is True
