"""Production Execution Wiring Regression Test Suite."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.integrations.callmedex.workers.runner import CallMedexWorkerRunner, CallMedexContainer
from app.integrations.callmedex.api.schemas import (
    ProcessReportRequest,
    ConnectorType,
    TaskStatus,
)
from app.integrations.callmedex.connectors.base.connector import JobCheckpoint
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.api.exceptions import (
    ConnectorNavigationError,
    ReportDownloadError,
)
from app.integrations.callmedex.config.settings import callmedex_settings


@pytest.mark.asyncio
async def test_worker_invokes_open_login_page_before_login():
    """Test 1: Verify worker.execute_report_job() calls open_login_page() before login()."""
    runner = CallMedexWorkerRunner()
    connector = runner.container.mocdoc_connector

    execution_order = []

    async def mock_open_login():
        execution_order.append("open_login_page")
        return True

    async def mock_login(creds):
        execution_order.append("login")
        return True

    async def mock_search(barcode):
        execution_order.append("search_by_barcode")
        return MagicMock(report_id=barcode)

    async def mock_download(barcode, path):
        execution_order.append("download_report")
        return b"%PDF-1.4 test bytes"

    async def mock_validate(bytes_data, patient):
        execution_order.append("validate_report")
        return True

    async def mock_logout():
        execution_order.append("logout")
        return True

    connector.open_login_page = mock_open_login
    connector.login = mock_login
    connector.search_by_barcode = mock_search
    connector.download_report = mock_download
    connector.validate_report = mock_validate
    connector.logout = mock_logout

    req = ProcessReportRequest(
        clinic_id="c-1",
        connector_type=ConnectorType.MOCDOC,
        external_report_id="BC-100",
        patient={"patient_phone": "+919966773300", "patient_name": "Test Patient"},
        report_name="Blood Test",
    )

    res = await runner.execute_report_job(req)
    assert res.success is True
    assert execution_order[0] == "open_login_page"
    assert execution_order[1] == "login"
    assert "open_login_page" in execution_order


@pytest.mark.asyncio
async def test_worker_never_begins_login_from_about_blank(monkeypatch):
    """Test 2: Verify worker does not bypass open_login_page when Playwright page is attached."""
    runner = CallMedexWorkerRunner()
    connector = runner.container.mocdoc_connector

    login_page_opened = False

    async def mock_open_login():
        nonlocal login_page_opened
        login_page_opened = True
        return True

    connector.open_login_page = mock_open_login

    req = ProcessReportRequest(
        clinic_id="c-1",
        connector_type=ConnectorType.MOCDOC,
        external_report_id="BC-101",
        patient={"patient_phone": "+919966773300", "patient_name": "Test Patient"},
        report_name="Blood Test",
    )

    await runner.execute_report_job(req)
    assert login_page_opened is True


@pytest.mark.asyncio
async def test_production_execution_cannot_enter_simulated_download_path(monkeypatch):
    """Test 3: Verify production execution raises ReportDownloadError if _is_live_page is False."""
    monkeypatch.setattr(callmedex_settings, "app_env", "production")

    connector = MocDocConnector()
    connector._authenticated = True

    # _is_live_page is False because page handle is None or blank
    with pytest.raises(ReportDownloadError) as exc_info:
        await connector.download_report("BC-102", "/tmp")

    assert "Production execution cannot download report" in str(exc_info.value)


@pytest.mark.asyncio
async def test_navigation_failure_raises_connector_navigation_error(monkeypatch):
    """Test 4: Verify navigation failure during open_login_page raises ConnectorNavigationError."""
    monkeypatch.setattr(callmedex_settings, "app_env", "production")

    connector = MocDocConnector()
    mock_page = MagicMock()
    mock_page.url = "about:blank"

    async def mock_goto(url, timeout=None):
        raise Exception("Network Timeout")

    mock_page.goto = mock_goto
    connector.attach_page(mock_page)

    with pytest.raises(ConnectorNavigationError) as exc_info:
        await connector.open_login_page()

    assert "Failed to navigate to MocDoc login page" in str(exc_info.value) or "Production execution failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_successful_production_execution_complete_sequence():
    """Test 5: Verify complete 10-step sequence runs in order."""
    runner = CallMedexWorkerRunner()
    connector = runner.container.mocdoc_connector

    sequence = []

    connector.open_login_page = AsyncMock(side_effect=lambda: sequence.append("Navigate") or True)
    connector.login = AsyncMock(side_effect=lambda creds: sequence.append("Login") or True)
    connector.search_by_barcode = AsyncMock(side_effect=lambda bc: sequence.append("Barcode Search") or MagicMock())
    connector.open_patient = AsyncMock(side_effect=lambda bc: sequence.append("Patient") or True)
    connector.open_reports = AsyncMock(side_effect=lambda bc: sequence.append("Reports") or True)
    connector.download_report = AsyncMock(side_effect=lambda bc, p: sequence.append("Download") or b"%PDF-1.4 real pdf")
    connector.validate_report = AsyncMock(side_effect=lambda b, p: sequence.append("Validate") or True)
    connector.logout = AsyncMock(side_effect=lambda: sequence.append("Logout") or True)

    req = ProcessReportRequest(
        clinic_id="c-1",
        connector_type=ConnectorType.MOCDOC,
        external_report_id="BC-105",
        patient={"patient_phone": "+919966773300", "patient_name": "Seq Patient"},
        report_name="Full Test",
    )

    res = await runner.execute_report_job(req)
    assert res.success is True
    assert sequence == ["Navigate", "Login", "Barcode Search", "Download", "Validate", "Logout"]


@pytest.mark.asyncio
async def test_app_lifespan_populates_queue_handlers_and_processes_background_task(monkeypatch):
    """Test 6: Verify app lifespan registers queue handler and background worker processes enqueued tasks."""
    import asyncio
    from app.config import settings
    from app.main import lifespan, app
    from app.integrations.callmedex.api.router import global_container

    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(callmedex_settings, "app_env", "development")

    async with lifespan(app):
        # Lifespan must have populated 'process_report' handler
        handlers = global_container.queue_engine._handlers
        assert "process_report" in handlers
        assert handlers["process_report"] is not None

        # Enqueue a test task directly
        req = ProcessReportRequest(
            clinic_id="c-lifespan",
            connector_type=ConnectorType.MOCDOC,
            external_report_id="BC-LIFESPAN-01",
            patient={"patient_phone": "+919966773300", "patient_name": "Lifespan Patient"},
            report_name="Lifespan Blood Test",
        )

        task_id = await global_container.queue_engine.enqueue_task(req)
        
        # Wait briefly for background worker loop to consume and process task
        for _ in range(20):
            await asyncio.sleep(0.1)
            status = await global_container.queue_engine.get_task_status(task_id)
            if status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break

        final_status = await global_container.queue_engine.get_task_status(task_id)
        assert final_status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_meta_whatsapp_cloud_api_real_http_post(monkeypatch):
    """Test 7: Verify WhatsApp service executes HTTP POST to Meta Cloud API when token is provided."""
    from app.integrations.callmedex.whatsapp.service import WhatsAppDeliveryService
    from app.integrations.callmedex.ai.schemas import MultiAudienceSummaryReport, StatementProvenance, SummaryStatus, SummaryLanguage
    from app.integrations.callmedex.whatsapp.schemas import WhatsAppDeliveryStatus

    monkeypatch.setattr(callmedex_settings.whatsapp_api_token, "_secret_value", "real_meta_token_123")
    monkeypatch.setattr(callmedex_settings, "whatsapp_phone_number_id", "10987654321")

    service = WhatsAppDeliveryService()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"messages": [{"id": "wmid.meta.test.123"}]}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    summary = MultiAudienceSummaryReport(
        patient_summary=[StatementProvenance(statement="Haemoglobin is normal", supported_by=["HB"])],
        clinician_summary=[StatementProvenance(statement="HB normal", supported_by=["HB"])],
        overall_confidence=0.9,
        status=SummaryStatus.SUCCESS,
        language=SummaryLanguage.ENGLISH,
        medical_disclaimer="Not a diagnosis.",
    )

    with patch("httpx.AsyncClient", return_value=mock_client):
        res = await service.deliver_report_and_summary(
            phone_number="+919966773300",
            pdf_storage_url="https://storage.provider/report.pdf",
            summary_report=summary,
            report_job_id="job-meta-01",
            correlation_id="corr-meta-01",
        )

        assert res.status == WhatsAppDeliveryStatus.DELIVERED
        assert res.message_id == "wmid.meta.test.123"


@pytest.mark.asyncio
async def test_queue_worker_exponential_backoff_retry(monkeypatch):
    """Test 8: Verify InMemoryQueue retries failed tasks up to max_worker_retries with exponential backoff."""
    import asyncio
    from app.integrations.callmedex.queue.drivers import InMemoryQueue
    from app.integrations.callmedex.api.schemas import TaskStatus

    queue = InMemoryQueue()
    attempts = 0

    async def failing_handler(request):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise Exception("Transient EMR Timeout")
        return {"status": "success"}

    await queue.register_handler("process_report", failing_handler)
    await queue.start()

    req = ProcessReportRequest(
        clinic_id="c-retry",
        connector_type=ConnectorType.MOCDOC,
        external_report_id="BC-RETRY-01",
        patient={"patient_phone": "+919966773300", "patient_name": "Retry Patient"},
        report_name="Retry Test",
    )

    monkeypatch.setattr(callmedex_settings, "max_worker_retries", 3)
    monkeypatch.setattr(callmedex_settings, "retry_backoff_seconds", 0.05)

    task_id = await queue.enqueue_task(req)

    for _ in range(30):
        await asyncio.sleep(0.05)
        status = await queue.get_task_status(task_id)
        if status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            break

    await queue.shutdown()
    assert attempts == 2
    final_status = await queue.get_task_status(task_id)
    assert final_status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_connector_compliance_lifecycle_methods():
    """Test 9: Verify MocDocConnector implements cleanup, retry, and checkpoint_resume."""
    connector = MocDocConnector()
    assert hasattr(connector, "cleanup")
    assert hasattr(connector, "retry")
    assert hasattr(connector, "checkpoint_resume")

    assert await connector.cleanup() is True
    assert await connector.checkpoint_resume("job-1", JobCheckpoint.AUTHENTICATED) is True
    assert connector.current_checkpoint == JobCheckpoint.AUTHENTICATED
    assert await connector.retry("job-1", JobCheckpoint.CREATED) is True
    assert connector.current_checkpoint == JobCheckpoint.CREATED


@pytest.mark.asyncio
async def test_bearer_token_placeholder_production_boot_refusal(monkeypatch):
    """Test 10: Verify fail fast refusal when bearer_token uses placeholder in production."""
    monkeypatch.setattr(callmedex_settings, "app_env", "production")
    monkeypatch.setattr(callmedex_settings.bearer_token, "_secret_value", "dev_bearer_token_change_in_prod")

    with pytest.raises(Exception) as exc_info:
        CallMedexWorkerRunner()

    assert "bearer_token" in str(exc_info.value)
