"""Tests for integration connectors — API endpoint, parsers, and idempotency."""

import pytest
from unittest.mock import patch

# ═══════════════════════════════════════════════════════════════════════════════
# Parser Tests — These test the regex extraction from MocDoc table cells
# ═══════════════════════════════════════════════════════════════════════════════


class TestPatientCellParser:
    """Test _parse_patient_cell() with real MocDoc data from screenshots."""

    def test_parse_standard_patient(self):
        from connectors.mocdoc.worker import _parse_patient_cell

        cell_text = (
            "Mrs.C Varalakshmi\n"
            "Gender: F Age: 60 years\n"
            "ID: VAM-39927 Mobile: +918121363550"
        )
        result = _parse_patient_cell(cell_text)

        assert result["patient_name"] == "Mrs.C Varalakshmi"
        assert result["vam_id"] == "VAM-39927"
        assert result["phone"] == "+918121363550"

    def test_parse_second_patient(self):
        from connectors.mocdoc.worker import _parse_patient_cell

        cell_text = (
            "Mrs.Jayalakshmi\n"
            "Gender: F Age: 61 years\n"
            "ID: VAM-48829 Mobile: +917382959863"
        )
        result = _parse_patient_cell(cell_text)

        assert result["patient_name"] == "Mrs.Jayalakshmi"
        assert result["vam_id"] == "VAM-48829"
        assert result["phone"] == "+917382959863"

    def test_parse_patient_with_spaces(self):
        from connectors.mocdoc.worker import _parse_patient_cell

        cell_text = (
            "Mrs.Ismat Perveen\n"
            "Gender: F Age: 45 years\n"
            "ID: VAM-48824 Mobile: +919804824365"
        )
        result = _parse_patient_cell(cell_text)

        assert result["patient_name"] == "Mrs.Ismat Perveen"
        assert result["vam_id"] == "VAM-48824"
        assert result["phone"] == "+919804824365"

    def test_parse_no_phone(self):
        from connectors.mocdoc.worker import _parse_patient_cell

        cell_text = "Mr.John Doe\n" "Gender: M Age: 35 years\n" "ID: VAM-50001"
        result = _parse_patient_cell(cell_text)

        assert result["patient_name"] == "Mr.John Doe"
        assert result["vam_id"] == "VAM-50001"
        assert result["phone"] is None

    def test_parse_phone_without_plus(self):
        from connectors.mocdoc.worker import _parse_patient_cell

        cell_text = (
            "Mrs.Test Patient\n"
            "Gender: F Age: 30 years\n"
            "ID: VAM-99999 Mobile: 919876543210"
        )
        result = _parse_patient_cell(cell_text)

        assert result["phone"] == "+919876543210"

    def test_parse_no_vam_id(self):
        from connectors.mocdoc.worker import _parse_patient_cell

        cell_text = "Mrs.Unknown\n" "Gender: F Age: 25 years\n" "Mobile: +910000000000"
        result = _parse_patient_cell(cell_text)

        assert result["patient_name"] == "Mrs.Unknown"
        assert result["vam_id"] is None
        assert result["phone"] == "+910000000000"

    def test_parse_empty_text(self):
        from connectors.mocdoc.worker import _parse_patient_cell

        result = _parse_patient_cell("")
        assert result["patient_name"] == ""
        assert result["vam_id"] is None
        assert result["phone"] is None


class TestTestDetailsParser:
    """Test _parse_test_details() with real MocDoc expanded row data."""

    def test_parse_standard_test(self):
        from connectors.mocdoc.worker import _parse_test_details

        expanded_text = (
            "COMPLETE BLOOD COUNT - 3P    No: 29220    08/07/2026    Track Sample\n"
            "SampleID: 260700007335"
        )
        result = _parse_test_details(expanded_text)

        assert result["report_no"] == "29220"
        assert result["sample_id"] == "260700007335"

    def test_parse_no_sample_id(self):
        from connectors.mocdoc.worker import _parse_test_details

        result = _parse_test_details("Some test   No: 12345")
        assert result["report_no"] == "12345"
        assert result["sample_id"] is None

    def test_parse_empty_text(self):
        from connectors.mocdoc.worker import _parse_test_details

        result = _parse_test_details("")
        assert result["report_no"] is None
        assert result["sample_id"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# Base Connector Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestReportMetadata:
    """Test ReportMetadata model."""

    def test_metadata_creation(self):
        from connectors.base import ReportMetadata

        meta = ReportMetadata(
            patient_name="Mrs.C Varalakshmi",
            patient_phone="+918121363550",
            report_name="COMPLETE BLOOD COUNT - 3P",
            report_type="Laboratory",
            external_report_id="VAM-39927_29220",
            vam_id="VAM-39927",
            report_no="29220",
        )

        assert meta.patient_name == "Mrs.C Varalakshmi"
        assert meta.external_report_id == "VAM-39927_29220"
        assert "VAM-39927_29220" in repr(meta)
        # Phone should be masked in repr
        assert "+918121363550" not in repr(meta)
        assert "3550" in repr(meta)


# ═══════════════════════════════════════════════════════════════════════════════
# Runner Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPasswordEncryption:
    """Test Fernet encryption/decryption for connector passwords."""

    def test_encrypt_decrypt_roundtrip(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        original = "my_mocdoc_password_123!"

        f = Fernet(key.encode())
        encrypted = f.encrypt(original.encode()).decode()
        decrypted = f.decrypt(encrypted.encode()).decode()

        assert decrypted == original
        assert encrypted != original

    def test_wrong_key_fails(self):
        from cryptography.fernet import Fernet

        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()

        f1 = Fernet(key1.encode())
        encrypted = f1.encrypt(b"password").decode()

        f2 = Fernet(key2.encode())
        with pytest.raises(Exception):
            f2.decrypt(encrypted.encode())


# ═══════════════════════════════════════════════════════════════════════════════
# Integration API Tests (mock the database and LabReportService)
# ═══════════════════════════════════════════════════════════════════════════════


class TestIntegrationEndpoint:
    """Test the /internal/integrations/lab-report endpoint."""

    @pytest.fixture
    def mock_settings(self):
        with patch("app.routers.integrations.settings") as mock:
            mock.integration_secret = "test-secret-key"
            yield mock

    def test_missing_secret_returns_401(self, mock_settings):
        """Requests without X-Integration-Secret should be rejected."""
        from fastapi.testclient import TestClient
        from app.routers.integrations import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.post(
            "/internal/integrations/lab-report",
            data={"clinic_id": "test", "patient_phone": "+91test"},
        )
        assert response.status_code == 401

    def test_wrong_secret_returns_401(self, mock_settings):
        """Requests with wrong secret should be rejected."""
        from fastapi.testclient import TestClient
        from app.routers.integrations import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.post(
            "/internal/integrations/lab-report",
            headers={"X-Integration-Secret": "wrong-secret"},
            data={"clinic_id": "test", "patient_phone": "+91test"},
        )
        assert response.status_code == 401


class TestCrossPathReportDeduplication:
    """A report already delivered via one intake path (e.g. CallMedex's dedicated
    WhatsApp number) must never be re-sent via another path (e.g. the processing
    center's own number) for the same (clinic_id, external_report_id)."""

    @pytest.fixture
    def mock_settings(self):
        with patch("app.routers.integrations.settings") as mock:
            mock.integration_secret = "test-secret-key"
            yield mock

    def test_generic_connector_skips_report_already_delivered_by_callmedex(self, mock_settings):
        from fastapi.testclient import TestClient
        from app.routers.integrations import router
        from fastapi import FastAPI
        from unittest.mock import MagicMock

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        with patch("app.routers.integrations.supabase") as mock_integrations_supabase, \
             patch("app.services.lab_reports.supabase") as mock_lab_reports_supabase, \
             patch("app.services.whatsapp.whatsapp_service") as mock_whatsapp:
            # No prior record in integration_processed_reports (this connector's own
            # dedup table) — the report only exists because CallMedex processed it.
            mock_integrations_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )
            # But lab_reports already has a row for this barcode, delivered via CallMedex.
            mock_lab_reports_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{
                    "id": "lr-1",
                    "clinic_id": "clinic-A",
                    "external_report_id": "BC-DUP-1",
                    "source": "callmedex",
                    "status": "sent",
                }]
            )

            response = client.post(
                "/internal/integrations/lab-report",
                headers={"X-Integration-Secret": "test-secret-key"},
                data={
                    "clinic_id": "clinic-A",
                    "patient_phone": "+919876543210",
                    "patient_name": "Test Patient",
                    "report_name": "CBC",
                    "external_report_id": "BC-DUP-1",
                    "connector_type": "mocdoc",
                },
                files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["already_processed"] is True
        mock_whatsapp.send_text.assert_not_called()
        mock_whatsapp.send_document.assert_not_called()
