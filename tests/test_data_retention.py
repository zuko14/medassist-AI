"""Tests for Data Retention Service (app/services/data_retention.py).

Verifies:
  - Configuration defaults match NMC (7 years) and DPDP (30 days)
  - Anonymization structure logic and storage scrubbing
  - Correct column name file_path is updated (not file_url)
  - Supabase Storage object deletion call
"""

from unittest.mock import MagicMock, patch
import pytest

from app.services.data_retention import (
    DataRetentionService,
    CLINICAL_RETENTION_YEARS,
    CONVERSATION_PURGE_DAYS,
)


class TestDataRetention:
    """Test suite for DataRetentionService configuration and helpers."""

    def test_retention_defaults(self):
        assert CLINICAL_RETENTION_YEARS == 7
        assert CONVERSATION_PURGE_DAYS == 30

    @pytest.mark.asyncio
    async def test_service_initialization(self):
        service = DataRetentionService()
        assert service is not None

    @pytest.mark.asyncio
    async def test_anonymize_clinical_records_scrubs_file_path_and_storage(self):
        """Regression test for Finding #3: file_path column & storage PDF deletion."""
        service = DataRetentionService()
        clinic_id = "test-clinic-123"
        phone = "+919876543210"
        sample_path = "+919876543210/uuid_report.pdf"

        mock_supabase = MagicMock()

        # Patients lookup mock
        mock_patients_table = MagicMock()
        mock_patients_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "patient-1", "name": "John Doe"}]
        )

        # Appointments update mock
        mock_appts_table = MagicMock()
        mock_appts_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "appt-1"}]
        )

        # Lab reports select & update mocks
        mock_reports_table = MagicMock()
        mock_reports_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "report-1", "file_path": sample_path}]
        )
        mock_reports_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "report-1"}]
        )

        # Prescriptions update mock
        mock_rx_table = MagicMock()
        mock_rx_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )

        def table_router(name):
            if name == "patients":
                return mock_patients_table
            elif name == "appointments":
                return mock_appts_table
            elif name == "lab_reports":
                return mock_reports_table
            elif name == "prescriptions":
                return mock_rx_table
            return MagicMock()

        mock_supabase.table.side_effect = table_router

        # Storage mock
        mock_bucket = MagicMock()
        mock_supabase.storage.from_.return_value = mock_bucket

        with patch("app.services.data_retention.supabase", mock_supabase):
            result = await service.anonymize_clinical_records(clinic_id, phone)

            # 1. Verify Storage removal was invoked with sample_path containing patient phone
            mock_supabase.storage.from_.assert_called_with("lab-reports")
            mock_bucket.remove.assert_called_once_with([sample_path])

            # 2. Verify update call on lab_reports passed file_path (not file_url)
            update_payload = mock_reports_table.update.call_args[0][0]
            assert "file_path" in update_payload
            assert update_payload["file_path"] == "[REDACTED]"
            assert "file_url" not in update_payload
            assert update_payload["patient_phone"] == "[REDACTED]"

            assert result["lab_reports_anonymized"] == 1
            assert len(result["errors"]) == 0
