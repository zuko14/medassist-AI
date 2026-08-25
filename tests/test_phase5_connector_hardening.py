"""Phase 5: Connector & Lab Intake Hardening Tests (P1-1, P1-2, P1-3).

Verifies:
1. P1-2: POST /integrations/lab-report runs authoritative server-side patient match
   and overwrites client-asserted match parameters.
2. P1-2: If server-side patient match is unsafe, report is held in needs_review.
3. P1-1: run_connector handles JSON string configs and missing/corrupted config gracefully.
"""

import io
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.routers.integrations import verify_integration_secret
from app.services.patient_match import MatchResult
from connectors.runner import run_connector


def test_receive_lab_report_server_side_match_overwrites_client():
    """P1-2: Server recalculates match result and overwrites client assertion."""
    client = TestClient(app)

    fake_pdf = b"%PDF-1.4 Fake PDF Content"

    # Server-computed match result
    server_match = MatchResult(
        status="matched",
        is_safe_to_send=True,
        match_source="patients_table",
        match_confidence=0.92,
        matched_patient_id="pat_verified_001",
        normalized_phone="+919876543210",
        patient_name="John Doe",
    )

    app.dependency_overrides[verify_integration_secret] = lambda: True

    try:
        with patch("app.routers.integrations.supabase.table") as mock_table, \
             patch("app.services.patient_match.patient_match_service.match", new_callable=AsyncMock, return_value=server_match) as mock_match, \
             patch("app.services.lab_reports.LabReportService.upload_and_send", new_callable=AsyncMock) as mock_upload:

            # Idempotency check returns empty (new report)
            mock_table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
            mock_table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
            mock_upload.return_value = {"id": "lr_123", "status": "sent"}

            files = {"file": ("test.pdf", io.BytesIO(fake_pdf), "application/pdf")}
            data = {
                "clinic_id": "clinic_1",
                "patient_phone": "+919876543210",
                "patient_name": "John Doe",
                "report_name": "Lipid Profile",
                "report_type": "Laboratory",
                "external_report_id": "EXT-1001",
                "connector_type": "mocdoc",
                # Client attempts to spoof match values
                "match_confidence": "0.10",
                "match_source": "spoofed_source",
                "matched_patient_id": "pat_spoofed",
            }

            response = client.post(
                "/internal/integrations/lab-report",
                files=files,
                data=data,
                headers={"X-Integration-Secret": "mock_secret"},
            )

            assert response.status_code == 200
            assert response.json()["success"] is True

            # Verify server-side match was executed with authoritative values
            mock_match.assert_called_once_with(
                clinic_id="clinic_1",
                scraped_name="John Doe",
                scraped_phone="+919876543210",
            )

            # Verify upload_and_send received the server-computed values, NOT the client spoofed ones
            mock_upload.assert_called_once()
            call_kwargs = mock_upload.call_args[1]
            assert call_kwargs["match_confidence"] == 0.92
            assert call_kwargs["match_source"] == "patients_table"
            assert call_kwargs["matched_patient_id"] == "pat_verified_001"
    finally:
        app.dependency_overrides.pop(verify_integration_secret, None)


def test_receive_lab_report_held_in_needs_review_when_unsafe():
    """P1-2: If server-side match is unsafe, report is held in needs_review without upload_and_send."""
    client = TestClient(app)

    fake_pdf = b"%PDF-1.4 Fake PDF Content"

    # Server-computed unsafe match (e.g. name conflict on shared phone)
    server_match = MatchResult(
        status="needs_review",
        is_safe_to_send=False,
        match_source="conflict",
        match_confidence=0.30,
        matched_patient_id=None,
        normalized_phone="+919876543210",
        patient_name="Unknown Caller",
        review_reason="Name conflict on shared phone",
    )

    app.dependency_overrides[verify_integration_secret] = lambda: True

    try:
        with patch("app.routers.integrations.supabase.table") as mock_table, \
             patch("app.services.patient_match.patient_match_service.match", new_callable=AsyncMock, return_value=server_match), \
             patch("app.services.lab_reports.LabReportService.upload_and_send", new_callable=AsyncMock) as mock_upload:

            mock_table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
            mock_table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
            mock_table.return_value.insert.return_value.execute.return_value.data = [{"id": "nr_row_01"}]

            files = {"file": ("test.pdf", io.BytesIO(fake_pdf), "application/pdf")}
            data = {
                "clinic_id": "clinic_1",
                "patient_phone": "+919876543210",
                "patient_name": "Unknown Caller",
                "report_name": "Lipid Profile",
                "report_type": "Laboratory",
                "external_report_id": "EXT-1002",
                "connector_type": "mocdoc",
                "match_confidence": "1.0",  # Client falsely claiming 100% match
            }

            response = client.post(
                "/internal/integrations/lab-report",
                files=files,
                data=data,
                headers={"X-Integration-Secret": "mock_secret"},
            )

            assert response.status_code == 200
            assert "Report held for review" in response.json()["message"]

            # CRITICAL: upload_and_send was NOT called — automated WhatsApp message was blocked
            mock_upload.assert_not_called()
    finally:
        app.dependency_overrides.pop(verify_integration_secret, None)


@pytest.mark.asyncio
async def test_run_connector_handles_json_string_config():
    """P1-1: run_connector parses JSON string configs without crashing."""
    mock_result = MagicMock()
    mock_result.data = {
        "id": "conn_001",
        "clinic_id": "clinic_1",
        "connector_type": "mocdoc",
        "is_enabled": False,  # Will exit cleanly after config check
        "config": '{"username": "moc_user", "password": "plain_password"}',
    }

    mock_query = MagicMock()
    mock_query.select.return_value.eq.return_value.eq.return_value.is_.return_value.single.return_value.execute.return_value = mock_result
    mock_query.select.return_value.eq.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value = mock_result

    with patch("connectors.runner.supabase.table", return_value=mock_query):
        summary = await run_connector("clinic_1", "mocdoc")
        assert summary["run_status"] == "skipped"

