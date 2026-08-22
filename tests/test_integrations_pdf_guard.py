"""Tests for PDF validation guard at the /api/integrations/lab-reports ingestion choke point."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings

client = TestClient(app)


def test_reject_non_pdf_file():
    """POST non-PDF body to /api/integrations/lab-reports should return 400."""
    headers = {
        "X-Integration-Secret": settings.integration_secret or "test-secret",
    }
    data = {
        "clinic_id": "test-clinic-id",
        "connector_type": "mocdoc",
        "external_report_id": "VAM-100_1",
        "patient_phone": "+919876543210",
        "patient_name": "Test Patient",
        "report_name": "CBC",
    }
    # Upload HTML / error text instead of %PDF
    files = {
        "file": ("report.pdf", b"<html><body>Session Expired</body></html>", "application/pdf"),
    }

    with patch("app.routers.integrations.supabase") as mock_supabase:
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.execute.return_value = MagicMock(data=[])

        with patch.object(settings, "integration_secret", "test-secret"):
            headers = {"X-Integration-Secret": "test-secret"}
            response = client.post(
                "/internal/integrations/lab-report",
                headers=headers,
                data=data,
                files=files,
            )

        assert response.status_code == 400
        assert "not a valid PDF" in response.json()["detail"]


def test_reject_empty_file():
    """POST empty file should return 400."""
    headers = {
        "X-Integration-Secret": settings.integration_secret or "test-secret",
    }
    data = {
        "clinic_id": "test-clinic-id",
        "connector_type": "mocdoc",
        "external_report_id": "VAM-100_1",
        "patient_phone": "+919876543210",
        "patient_name": "Test Patient",
        "report_name": "CBC",
    }
    files = {
        "file": ("report.pdf", b"", "application/pdf"),
    }

    with patch("app.routers.integrations.supabase") as mock_supabase:
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.execute.return_value = MagicMock(data=[])

        with patch.object(settings, "integration_secret", "test-secret"):
            headers = {"X-Integration-Secret": "test-secret"}
            response = client.post(
                "/internal/integrations/lab-report",
                headers=headers,
                data=data,
                files=files,
            )

        assert response.status_code == 400
        assert "Empty file" in response.json()["detail"]
