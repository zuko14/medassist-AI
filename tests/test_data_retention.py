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

    @pytest.mark.asyncio
    async def test_dpdp_anonymization_strictly_scoped_to_target_clinic(self):
        """T4.4: Deletion / anonymization for clinic A does not touch clinic B."""
        service = DataRetentionService()
        clinic_a = "clinic-alpha-111"
        phone = "+919876543210"

        mock_supabase = MagicMock()
        mock_patients = MagicMock()
        mock_patients.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "p-1", "clinic_id": clinic_a, "phone": phone}]
        )

        mock_appts = MagicMock()
        mock_appts.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "appt-1", "clinic_id": clinic_a}]
        )

        def router(name):
            if name == "patients":
                return mock_patients
            elif name == "appointments":
                return mock_appts
            return MagicMock()

        mock_supabase.table.side_effect = router

        with patch("app.services.data_retention.supabase", mock_supabase):
            await service.anonymize_clinical_records(clinic_a, phone)

            # Check that appointments update included .eq("clinic_id", clinic_a)
            eq_calls = mock_appts.update.return_value.eq.call_args_list
            assert any(call[0] == ("clinic_id", clinic_a) for call in eq_calls)

    @pytest.mark.asyncio
    async def test_nmc_clinical_structure_preserved_during_anonymization(self):
        """T4.4: NMC 7-year retention preserves clinical fields while scrubbing PII."""
        service = DataRetentionService()
        clinic_id = "clinic-beta-222"
        phone = "+919123456789"

        mock_supabase = MagicMock()
        mock_patients = MagicMock()
        mock_patients.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "p-2", "name": "Jane Doe"}]
        )
        mock_appts = MagicMock()

        def router(name):
            if name == "patients":
                return mock_patients
            elif name == "appointments":
                return mock_appts
            return MagicMock()

        mock_supabase.table.side_effect = router

        with patch("app.services.data_retention.supabase", mock_supabase):
            result = await service.anonymize_clinical_records(clinic_id, phone)

            update_dict = mock_appts.update.call_args[0][0]
            # PII must be redacted
            assert update_dict["patient_name"] == "[REDACTED]"
            assert update_dict["patient_phone"] == "[REDACTED]"
            # Clinical fields (doctor_name, appointment_date, status) must NOT be in the update dict (i.e. preserved)
            assert "doctor_name" not in update_dict
            assert "appointment_date" not in update_dict
            assert "status" not in update_dict
            assert len(result["errors"]) == 0
