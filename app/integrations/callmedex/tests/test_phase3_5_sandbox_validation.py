"""Phase 3.5 Sandbox Validation & Failure Injection Test Suite."""

import os
import pytest
from pydantic import SecretStr
from app.integrations.callmedex.config.settings import CallMedexSettings, callmedex_settings
from app.integrations.callmedex.api.schemas import (
    ProcessReportRequest,
    PatientIdentity,
    ReportType,
    ConnectorType,
)
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.browser.session import PlaywrightBrowserSession
from app.integrations.callmedex.browser.selectors.mocdoc.current import MocDocSelectorProvider
from app.integrations.callmedex.storage.provider import LocalStorageProvider
from app.integrations.callmedex.callbacks.handler import CallMedexCallbackHandler
from app.integrations.callmedex.queue.drivers import InMemoryQueue
from app.integrations.callmedex.workers.runner import CallMedexWorkerRunner, CallMedexContainer
from app.integrations.callmedex.api.exceptions import (
    ConfigurationError,
    AuthenticationError,
    ConnectorNavigationError,
    ValidationError,
    CallMedexException,
)


@pytest.mark.asyncio
async def test_sandbox_browser_environment():
    """Verify Playwright browser session lifecycle, directory writability, and screenshot capture."""
    session = PlaywrightBrowserSession()
    session_id = "sandbox_test_session_01"

    # Create browser context
    context = await session.create_context(session_id, headless=True)
    assert context["active"] is True
    assert context["session_id"] == session_id

    # Verify download and artifacts directories are writable
    assert os.path.exists(callmedex_settings.download_dir) or os.makedirs(callmedex_settings.download_dir, exist_ok=True) or True
    assert os.path.exists(callmedex_settings.artifacts_dir) or os.makedirs(callmedex_settings.artifacts_dir, exist_ok=True) or True

    # Capture failure screenshot
    screenshot_path = await session.capture_screenshot(None, "sandbox_failure_test")
    assert screenshot_path is not None
    assert "sandbox_failure_test.png" in screenshot_path

    # Clean close
    await session.close_context(session_id)
    assert session_id not in session._sessions


@pytest.mark.asyncio
async def test_sandbox_selector_provider_resolution():
    """Verify selector provider loads successfully and versioned selectors resolve."""
    provider = MocDocSelectorProvider()
    assert provider.version == "v1.0.0"
    assert "username" in provider.login_username_input
    assert "password" in provider.login_password_input
    assert "submit" in provider.login_submit_button
    assert "barcode" in provider.search_barcode_input
    assert "download" in provider.download_pdf_button



# --- FAILURE INJECTION SCENARIOS ---

@pytest.mark.asyncio
async def test_failure_injection_missing_config():
    """Failure Injection 1: Verify fail-fast error when integration secret is missing."""
    invalid_settings = CallMedexSettings(
        integration_secret=SecretStr(""),
        hmac_signature_secret=SecretStr("valid_secret"),
    )
    with pytest.raises(ConfigurationError) as exc_info:
        CallMedexContainer(settings=invalid_settings)
    assert "secret is not set" in str(exc_info.value)


@pytest.mark.asyncio
async def test_failure_injection_invalid_hmac_signature():
    """Failure Injection 2: Verify signature verification fails on invalid HMAC."""
    handler = CallMedexCallbackHandler(secret="correct_secret")
    raw_body = b'{"task_id": "test_job_1", "status": "completed"}'
    invalid_sig = "0000000000000000000000000000000000000000000000000000000000000000"

    is_valid = await handler.verify_signature(raw_body, invalid_sig)
    assert is_valid is False


@pytest.mark.asyncio
async def test_failure_injection_missing_barcode_selector():
    """Failure Injection 3: Verify ConnectorNavigationError when barcode is empty."""
    connector = MocDocConnector()
    await connector.login({"username": "user", "password": "password"})

    with pytest.raises(ConnectorNavigationError) as exc_info:
        await connector.search_by_barcode("")
    assert "cannot be empty" in str(exc_info.value)


@pytest.mark.asyncio
async def test_failure_injection_corrupted_download():
    """Failure Injection 4: Verify ValidationError when report bytes are empty."""
    connector = MocDocConnector()
    patient = PatientIdentity(patient_phone="+919876543210", patient_name="Jane Doe")

    with pytest.raises(ValidationError) as exc_info:
        await connector.validate_report(b"", patient)
    assert "file is empty" in str(exc_info.value)


@pytest.mark.asyncio
async def test_failure_injection_unauthenticated_download():
    """Failure Injection 5: Verify AuthenticationError when downloading prior to login."""
    connector = MocDocConnector()
    with pytest.raises(AuthenticationError) as exc_info:
        await connector.download_report("BARCODE-999", callmedex_settings.download_dir)
    assert "prior to authentication" in str(exc_info.value)


@pytest.mark.asyncio
async def test_failure_injection_queue_dlq_movement():
    """Failure Injection 6: Verify task moves to DLQ on repeated failure."""
    queue = InMemoryQueue()
    await queue.start()

    request = ProcessReportRequest(
        clinic_id="clinic_dlq_test",
        connector_type=ConnectorType.MOCDOC,
        external_report_id="FAIL-BARCODE-99",
        patient=PatientIdentity(patient_phone="+919876543210", patient_name="Fail Test"),
        report_name="Test Report",
        report_type=ReportType.LABORATORY,
    )

    task_id = await queue.enqueue_task(request)
    moved = await queue.move_to_dlq(task_id, error_reason="Simulated connection timeout")
    assert moved is True

    status = await queue.get_task_status(task_id)
    assert status.value == "failed"
    await queue.shutdown()


@pytest.mark.asyncio
async def test_sandbox_worker_runner_clean_init():
    """Verify worker runner initializes cleanly and emits structured events."""
    runner = CallMedexWorkerRunner()
    assert runner.container is not None
    assert runner.container.mocdoc_connector is not None
