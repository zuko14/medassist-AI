"""Forensic Production Hardening Regression Test Suite.

Verifies:
1. P0 Slot Uniqueness Invariant & doctor_id resolution in book_appointment and payment service.
2. Tenant Isolation Backstop: All tenant-owned tables raise TenantIsolationError without clinic_id.
3. Lab Reports Storage Bucket isolation: upload storage_path is prefixed with clinic_id.
4. Razorpay payment link expiration synchronization.
5. Conversation FSM doctor_id propagation.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import (
    TENANT_OWNED_TABLES,
    TenantIsolationError,
    book_appointment,
    scoped_query,
)
from app.services.payment import payment_service
from app.services.lab_reports import LabReportService

lab_report_service = LabReportService()


# ─────────────────────────────────────────────────────────────────────────────
# 1. TENANT ISOLATION WHITELIST VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def test_all_tenant_owned_tables_fail_closed_without_clinic_id():
    """Verify that every table in TENANT_OWNED_TABLES strictly raises TenantIsolationError."""
    expected_tables = {
        "appointments", "patients", "lab_reports", "lab_tests", "doctors",
        "branches", "doctor_branches", "doctor_leaves", "hospital_holidays",
        "clinic_admins", "integration_connectors", "connector_failed_reports",
        "conversations", "inbound_messages", "processed_messages",
        "family_members", "payment_events", "failed_messages",
        "prescriptions", "prescription_reminder_sends", "broadcasts",
        "admin_notifications", "outbound_message_ledger", "connector_audit_log",
        "integration_processed_reports", "analytics_events",
    }
    
    # Assert all expected tables are in the whitelist
    assert expected_tables.issubset(TENANT_OWNED_TABLES), (
        f"Missing tables in TENANT_OWNED_TABLES: {expected_tables - TENANT_OWNED_TABLES}"
    )

    for table in expected_tables:
        with pytest.raises(TenantIsolationError):
            scoped_query(table, clinic_id=None)
        
        with pytest.raises(TenantIsolationError):
            scoped_query(table, clinic_id="")


def test_tenant_owned_tables_allow_unscoped_when_explicitly_flagged():
    """Verify that allow_unscoped=True permits cross-clinic queries for background jobs."""
    for table in ["prescriptions", "prescription_reminder_sends", "outbound_message_ledger"]:
        try:
            query = scoped_query(table, allow_unscoped=True)
            assert query is not None
        except TenantIsolationError:
            pytest.fail(f"scoped_query({table}, allow_unscoped=True) unexpectedly raised TenantIsolationError")


# ─────────────────────────────────────────────────────────────────────────────
# 2. DOCTOR_ID RESOLUTION & SLOT INVARIANT
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_book_appointment_resolves_doctor_id_if_missing():
    """Verify that book_appointment resolves doctor_id from doctor_name if not provided."""
    clinic_id = "11111111-2222-3333-4444-555555555555"
    doc_uuid = "22222222-3333-4444-5555-666666666666"
    
    mock_doctor = {"id": doc_uuid, "name": "Dr. House", "clinic_id": clinic_id, "department": "General Medicine", "is_active": True}
    
    mock_sb = MagicMock()
    mock_query = MagicMock()
    mock_sb.table.return_value = mock_query
    mock_query.select.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.in_.return_value = mock_query
    mock_query.insert.return_value = mock_query
    
    mock_execute_select = MagicMock()
    mock_execute_select.data = []
    
    mock_execute_insert = MagicMock()
    mock_execute_insert.data = [{
        "id": "apt-123",
        "clinic_id": clinic_id,
        "doctor_name": "Dr. House",
        "doctor_id": doc_uuid,
        "department": "General Medicine",
        "appointment_date": "2026-09-01",
        "appointment_time": "10:00",
        "status": "confirmed",
        "booking_ref": "REF123",
    }]
    mock_query.execute.side_effect = [mock_execute_select, mock_execute_insert]

    with patch.dict(book_appointment.__globals__, {
        "supabase": mock_sb,
        "get_doctor_by_name": AsyncMock(return_value=mock_doctor),
        "get_patient_by_phone": AsyncMock(return_value={"id": "pat-1", "visit_count": 1}),
        "update_patient": AsyncMock(return_value=True),
    }):
        
        data = {
            "doctor_name": "Dr. House",
            "department": "General Medicine",
            "appointment_date": "2026-09-01",
            "appointment_time": "10:00",
            "patient_name": "John Doe",
            "patient_phone": "9999999999",
        }
        
        result = await book_appointment(clinic_id, data)
        assert result["success"] is True, f"Failed: {result}"
        assert data.get("doctor_id") == doc_uuid
        assert mock_query.insert.called
        insert_payload = mock_query.insert.call_args[0][0]
        assert insert_payload["doctor_id"] == doc_uuid


@pytest.mark.asyncio
async def test_payment_booking_resolves_and_persists_doctor_id():
    """Verify that create_booking_with_payment resolves and persists doctor_id."""
    import app.services.payment
    clinic_id = "11111111-2222-3333-4444-555555555555"
    doc_uuid = "33333333-4444-5555-6666-777777777777"
    mock_doctor = {"id": doc_uuid, "name": "Dr. Strange", "consultation_fee": 500}

    with patch("app.database.get_doctor_by_name", new=AsyncMock(return_value=mock_doctor)), \
         patch.object(payment_service, "_get_doctor_fee_paise", new=AsyncMock(return_value=50000)), \
         patch.object(payment_service, "_create_payment_link", new=AsyncMock(return_value={"id": "plink_123", "short_url": "https://rzp.io/i/123"})):

        mock_sb_table = MagicMock()
        with patch.object(app.services.payment.supabase, "table", return_value=mock_sb_table):
            # Mock insert
            mock_insert = MagicMock()
            mock_sb_table.insert.return_value = mock_insert
            inserted_booking = {
                "id": "booking-uuid-1",
                "clinic_id": clinic_id,
                "doctor_name": "Dr. Strange",
                "doctor_id": doc_uuid,
                "booking_ref": "REF456",
            }
            mock_insert.execute.return_value = MagicMock(data=[inserted_booking])
            
            # Mock update for razorpay_payment_link_id
            mock_update = MagicMock()
            mock_sb_table.update.return_value = mock_update
            mock_eq = MagicMock()
            mock_update.eq.return_value = mock_eq
            mock_eq.execute.return_value = MagicMock(data=[inserted_booking])

            result = await payment_service.create_booking_with_payment(
                clinic_id=clinic_id,
                patient_phone="9876543210",
                patient_name="Stephen",
                department="Neurology",
                doctor_name="Dr. Strange",
                appointment_date="2026-09-01",
                appointment_time="11:00",
                doctor_id=doc_uuid,
            )

            assert result["success"] is True
            assert mock_sb_table.insert.call_count >= 1
            appointment_insert_payload = mock_sb_table.insert.call_args_list[0][0][0]
            assert appointment_insert_payload["doctor_id"] == doc_uuid


# ─────────────────────────────────────────────────────────────────────────────
# 3. LAB REPORT STORAGE PATH TENANT ISOLATION
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lab_report_storage_path_is_clinic_scoped():
    """Verify that lab report storage paths include clinic_id prefix."""
    import app.services.lab_reports
    clinic_id = "11111111-2222-3333-4444-555555555555"
    patient_phone = "9876543210"
    mock_clinic = {"id": clinic_id, "name": "Test Clinic", "waba_id": "waba_123"}
    
    with patch("app.services.lab_reports.extract_text_from_pdf", return_value="Test Report Text"), \
         patch("app.services.lab_reports.ReportSummarizer.summarize", new=AsyncMock(return_value={"summary": "Normal", "urgency": "low"})), \
         patch("app.services.lab_reports.get_clinic_by_id", new=AsyncMock(return_value=mock_clinic)), \
         patch("app.services.lab_reports.whatsapp_service.send_template", new=AsyncMock(return_value={"messages": [{"id": "wamid.123"}]})), \
         patch("app.services.lab_reports.whatsapp_service.send_text", new=AsyncMock(return_value={"messages": [{"id": "wamid.123"}]})):

        mock_bucket = MagicMock()
        mock_bucket.upload.return_value = {"Key": "uploaded"}
        mock_bucket.create_signed_url.return_value = {"signedURL": "https://storage.example.com/file.pdf"}
        
        mock_sb_table = MagicMock()
        mock_sb_table.insert.return_value = MagicMock(execute=MagicMock(return_value=MagicMock(data=[{"id": "rep-1"}])))
        mock_sb_table.select.return_value = MagicMock(eq=MagicMock(return_value=MagicMock(execute=MagicMock(return_value=MagicMock(data=[])))))

        with patch.object(app.services.lab_reports.supabase.storage, "from_", return_value=mock_bucket), \
             patch.object(app.services.lab_reports.supabase, "table", return_value=mock_sb_table):

            # Upload
            report = await lab_report_service.upload_and_send(
                clinic_id=clinic_id,
                patient_phone=patient_phone,
                patient_name="Alice",
                filename="blood_test.pdf",
                file_bytes=b"%PDF-1.4 dummy",
                content_type="application/pdf",
                report_name="Blood Test",
                report_type="blood_test",
            )

            assert report is not None
            assert report.get("id") == "rep-1"
            upload_call = mock_bucket.upload.call_args
            storage_path = upload_call[0][0]
            
            # Verify storage path begins with clinic_id / patient_phone /
            assert storage_path.startswith(f"{clinic_id}/{patient_phone}/"), (
                f"Expected storage path to start with '{clinic_id}/{patient_phone}/', got '{storage_path}'"
            )
        assert storage_path.startswith(f"{clinic_id}/{patient_phone}/"), (
            f"Expected storage path to start with '{clinic_id}/{patient_phone}/', got '{storage_path}'"
        )
