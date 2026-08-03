"""Phase R1 Bug Fix Regression Tests (Production Hardening Phase R1)."""

import os
import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from app.integrations.callmedex.callbacks.handler import CallMedexCallbackHandler
from app.integrations.callmedex.api.schemas import (
    CallbackStatusPayload,
    TaskStatus,
    ConnectorType,
    ProcessReportRequest,
    PatientIdentity,
    ReportType,
)
from app.integrations.callmedex.config.settings import callmedex_settings
from app.integrations.callmedex.workers.runner import CallMedexWorkerRunner
from app.integrations.callmedex.storage.provider import LocalStorageProvider


@pytest.mark.asyncio
async def test_r1_callback_handler_production_real_post_success(monkeypatch):
    """Verify that in app_env='production', send_status_callback performs real HTTP POST."""
    monkeypatch.setattr(callmedex_settings, "app_env", "production")
    handler = CallMedexCallbackHandler(secret="test_secret_123")

    payload = CallbackStatusPayload(
        task_id="task_prod_001",
        clinic_id="clinic_01",
        connector_type=ConnectorType.MOCDOC,
        external_report_id="REP-PROD-001",
        status=TaskStatus.COMPLETED,
        correlation_id="corr-prod-001",
    )

    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("httpx.AsyncClient", return_value=mock_client):
        success = await handler.send_status_callback(payload)
        assert success is True
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args.kwargs
        assert "headers" in call_kwargs
        assert "X-Signature-256" in call_kwargs["headers"]
        assert call_kwargs["headers"]["X-Correlation-ID"] == "corr-prod-001"


@pytest.mark.asyncio
async def test_r1_callback_handler_production_real_post_failure_500(monkeypatch):
    """Verify that in app_env='production', HTTP 500 error returns callback_delivered=False."""
    monkeypatch.setattr(callmedex_settings, "app_env", "production")
    handler = CallMedexCallbackHandler(secret="test_secret_123")

    payload = CallbackStatusPayload(
        task_id="task_prod_500",
        clinic_id="clinic_01",
        connector_type=ConnectorType.MOCDOC,
        external_report_id="REP-PROD-500",
        status=TaskStatus.COMPLETED,
        correlation_id="corr-prod-500",
    )

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("httpx.AsyncClient", return_value=mock_client):
        success = await handler.send_status_callback(payload)
        assert success is False


@pytest.mark.asyncio
async def test_r1_worker_runner_step8_captures_callback_failure(monkeypatch):
    """Verify worker runner step 8 invokes send_status_callback and captures callback_delivered=False on 500."""
    monkeypatch.setattr(callmedex_settings, "app_env", "production")
    monkeypatch.setattr(callmedex_settings.integration_secret, "_secret_value", "real_prod_integration_secret_999")
    monkeypatch.setattr(callmedex_settings.hmac_signature_secret, "_secret_value", "real_prod_hmac_secret_999")
    monkeypatch.setattr(callmedex_settings.bearer_token, "_secret_value", "real_prod_bearer_token_999")
    runner = CallMedexWorkerRunner()

    mock_connector = runner.container.mocdoc_connector
    mock_connector.open_login_page = AsyncMock(return_value=True)
    mock_connector.login = AsyncMock(return_value=True)
    mock_connector.search_by_barcode = AsyncMock(return_value=MagicMock())
    mock_connector.download_report = AsyncMock(return_value=b"%PDF-1.4 test report")
    mock_connector.validate_report = AsyncMock(return_value=True)
    mock_connector.logout = AsyncMock(return_value=True)

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Callback endpoint unreachable"

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    request = ProcessReportRequest(
        clinic_id="clinic_r1_test",
        connector_type=ConnectorType.MOCDOC,
        external_report_id="MOC-R1-TEST",
        patient=PatientIdentity(patient_phone="+919876543210", patient_name="R1 Test"),
        report_name="Complete Blood Count",
        report_type=ReportType.LABORATORY,
    )

    with patch("httpx.AsyncClient", return_value=mock_client):
        response = await runner.execute_report_job(request)
        assert response.success is True
        assert response.callback_delivered is False


@pytest.mark.asyncio
async def test_r1_storage_provider_sanitizes_path_traversal():
    """Verify LocalStorageProvider strips path separators and '..' to prevent path traversal."""
    provider = LocalStorageProvider()
    traversal_report_id = "../../etc/passwd"
    traversal_filename = "../malicious_script.sh"
    file_bytes = b"sample pdf bytes content"

    filepath = await provider.save_temp_report(
        traversal_report_id, file_bytes, traversal_filename
    )

    try:
        # Filepath must be inside download_dir
        abs_download_dir = os.path.abspath(provider.download_dir)
        abs_filepath = os.path.abspath(filepath)
        assert abs_filepath.startswith(abs_download_dir)
        # Safe filename should not contain '..'
        basename = os.path.basename(filepath)
        assert ".." not in basename
        assert os.path.exists(filepath)
    finally:
        await provider.cleanup_temp_report(filepath)
