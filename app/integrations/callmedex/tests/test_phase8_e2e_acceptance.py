"""Phase 8 End-to-End Production Acceptance Test Suite."""

import pytest
from app.integrations.callmedex.workers.runner import CallMedexWorkerRunner
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.browser.integrity import validate_pdf_download
from app.integrations.callmedex.ocr.engine import CanonicalOCRPipeline
from app.integrations.callmedex.ai.reasoning import ClinicalReasoningEngine
from app.integrations.callmedex.ai.generator import MultiAudienceSummaryGenerator
from app.integrations.callmedex.whatsapp.service import WhatsAppDeliveryService
from app.integrations.callmedex.whatsapp.schemas import WhatsAppDeliveryStatus
from app.integrations.callmedex.config.settings import callmedex_settings


@pytest.mark.asyncio
async def test_phase8_end_to_end_production_chain():
    """Verify complete end-to-end production acceptance pipeline.

    Production Execution Chain:
    CallMedex Booking -> Barcode -> MocDoc Browser Automation -> PDF Download ->
    Canonical OCR -> Layer 1 Reasoning -> Layer 2 Summary & Provenance ->
    WhatsApp Delivery -> Signed Callback Dispatch -> CallMedex Updated.
    """
    # 1. Step 1-4: MocDoc Login & Connector initialization
    connector = MocDocConnector()
    login_ok = await connector.login({"username": "callmedex_prod_user", "password": "secure_prod_password"})
    assert login_ok is True

    # 2. Step 5-6: Barcode Search
    barcode = "260700009225"
    metadata = await connector.search_by_barcode(barcode)
    assert metadata is not None
    assert metadata.report_id == barcode

    # 3. Step 7-9: Download Report PDF & Validate Integrity
    pdf_bytes = await connector.download_report(barcode, callmedex_settings.download_dir)
    assert pdf_bytes is not None

    integrity = validate_pdf_download(pdf_bytes)
    assert integrity.is_valid is True
    assert integrity.pdf_signature_valid is True
    assert len(integrity.sha256_checksum) == 64

    # 4. Phase 5: Canonical OCR Pipeline
    ocr_pipeline = CanonicalOCRPipeline()
    canonical_report = ocr_pipeline.process_pdf(
        pdf_bytes=pdf_bytes,
        report_id=barcode,
        patient_id="VAM-50380",
        barcode=barcode,
    )
    assert len(canonical_report.tests) >= 1

    # 5. Phase 6: Layer 1 Clinical Reasoning & Layer 2 Summary Engine
    reasoning_engine = ClinicalReasoningEngine()
    summary_generator = MultiAudienceSummaryGenerator()

    reasoning_result = reasoning_engine.analyze_report(canonical_report)
    summary_report = summary_generator.generate_summary(canonical_report, reasoning_result)

    assert summary_report.status.value in ["success", "flagged_for_review"]
    assert len(summary_report.patient_summary) >= 1
    assert summary_report.patient_summary[0].supported_by == ["HB"]
    assert "DISCLAIMER:" in summary_report.medical_disclaimer

    # 6. Phase 7: Meta WhatsApp Delivery & HMAC Signed Callback Dispatch
    whatsapp_service = WhatsAppDeliveryService()
    delivery_result = await whatsapp_service.deliver_report_and_summary(
        phone_number="+919966773300",
        pdf_storage_url="https://storage.callmedex.org/reports/260700009225.pdf",
        summary_report=summary_report,
        report_job_id="JOB-E2E-2026-001",
        correlation_id="CORR-E2E-2026-001",
        callback_url="https://api.callmedex.org/webhooks/report-status",
    )

    assert delivery_result.status == WhatsAppDeliveryStatus.DELIVERED
    assert delivery_result.callback_delivered is True

    # 7. Step 10: Logout & Clean up
    logout_ok = await connector.logout()
    assert logout_ok is True
