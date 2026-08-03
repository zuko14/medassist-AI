"""Phase 3 Isolated Integration Unit & Lifecycle Test Suite."""

import os
import pytest
from app.integrations.callmedex.config.settings import CallMedexSettings, callmedex_settings
from app.integrations.callmedex.api.schemas import (
    ProcessReportRequest,
    PatientIdentity,
    ReportType,
    ConnectorType,
)
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.connectors.base.connector import JobCheckpoint
from app.integrations.callmedex.workers.runner import CallMedexWorkerRunner, CallMedexContainer
from app.integrations.callmedex.callbacks.handler import CallMedexCallbackHandler
from app.integrations.callmedex.queue.drivers import InMemoryQueue
from app.integrations.callmedex.storage.provider import LocalStorageProvider
from app.integrations.callmedex.api.exceptions import ConfigurationError, AuthenticationError


@pytest.mark.asyncio
async def test_connector_capabilities():
    """Verify MocDocConnector advertises correct capability declarations."""
    connector = MocDocConnector()
    caps = connector.capabilities
    assert caps.browser_required is True
    assert caps.supports_barcode_search is True
    assert caps.supports_pdf is True
    assert caps.supports_retry is True


@pytest.mark.asyncio
async def test_connector_lifecycle_and_checkpoints():
    """Verify 9-step connector lifecycle and recovery checkpoint progression."""
    connector = MocDocConnector()
    assert connector.current_checkpoint == JobCheckpoint.CREATED

    # Login
    login_ok = await connector.login({"username": "test_user", "password": "test_password"})
    assert login_ok is True
    assert connector.current_checkpoint == JobCheckpoint.AUTHENTICATED

    # Search by barcode
    metadata = await connector.search_by_barcode("BARCODE-12345")
    assert metadata is not None
    assert metadata.report_id == "BARCODE-12345"
    assert connector.current_checkpoint == JobCheckpoint.REPORT_LOCATED

    # Download report PDF
    pdf_bytes = await connector.download_report("BARCODE-12345", callmedex_settings.download_dir)
    assert pdf_bytes is not None
    assert b"%PDF" in pdf_bytes
    assert connector.current_checkpoint == JobCheckpoint.PDF_DOWNLOADED

    # Validate report
    patient = PatientIdentity(patient_phone="+919876543210", patient_name="Jane Doe")
    valid = await connector.validate_report(pdf_bytes, patient)
    assert valid is True
    assert connector.current_checkpoint == JobCheckpoint.VALIDATED

    # Logout
    logout_ok = await connector.logout()
    assert logout_ok is True


@pytest.mark.asyncio
async def test_worker_runner_execution():
    """Verify full end-to-end report job execution via CallMedexWorkerRunner."""
    container = CallMedexContainer()
    runner = CallMedexWorkerRunner(container)

    request = ProcessReportRequest(
        clinic_id="clinic_test_99",
        connector_type=ConnectorType.MOCDOC,
        external_report_id="MOC-990011",
        patient=PatientIdentity(patient_phone="+919876543210", patient_name="Test Patient"),
        report_name="Complete Blood Count",
        report_type=ReportType.LABORATORY,
    )

    response = await runner.execute_report_job(request, correlation_id="test-trace-12345")
    assert response.success is True
    assert response.task_id is not None
    assert "MOC-990011" in response.message


@pytest.mark.asyncio
async def test_queue_driver_operations():
    """Verify InMemoryQueue enqueuing and status retrieval."""
    queue = InMemoryQueue()
    await queue.start()

    request = ProcessReportRequest(
        clinic_id="clinic_test_99",
        connector_type=ConnectorType.MOCDOC,
        external_report_id="MOC-887766",
        patient=PatientIdentity(patient_phone="+919876543210", patient_name="Test Patient"),
        report_name="Lipid Profile",
        report_type=ReportType.LABORATORY,
    )

    task_id = await queue.enqueue_task(request)
    assert task_id is not None

    status = await queue.get_task_status(task_id)
    assert status.value == "pending"

    await queue.shutdown()


@pytest.mark.asyncio
async def test_hmac_signature_verification():
    """Verify HMAC-SHA256 signature generation and verification in callback handler."""
    handler = CallMedexCallbackHandler(secret="test_hmac_secret_key")
    raw_body = b'{"task_id": "12345", "status": "completed"}'

    import hmac, hashlib
    valid_sig = hmac.new(b"test_hmac_secret_key", raw_body, hashlib.sha256).hexdigest()

    assert await handler.verify_signature(raw_body, valid_sig) is True
    assert await handler.verify_signature(raw_body, "invalid_signature") is False


@pytest.mark.asyncio
async def test_storage_provider_lifecycle():
    """Verify LocalStorageProvider temp report saving, reading, and cleanup."""
    storage = LocalStorageProvider()
    test_bytes = b"%PDF-1.4 Mock PDF Content"
    filepath = await storage.save_temp_report("REPORT-001", test_bytes, "test.pdf")

    assert os.path.exists(filepath)
    read_bytes = await storage.get_temp_report(filepath)
    assert read_bytes == test_bytes

    cleaned = await storage.cleanup_temp_report(filepath)
    assert cleaned is True
    assert not os.path.exists(filepath)
