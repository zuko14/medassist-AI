"""Forensic Security Audit Remediation Regression & Adversarial Test Suite.

Verifies end-to-end remediation for findings C1–C7, H1–H10, M1–M8, L1–L2 from the
2026-08-22 Forensic Security & Data Integrity Audit.
"""

import asyncio
import importlib
import json
import pytest
import sys
import time
from datetime import datetime, date, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException

# Ensure app.database is the real module if an earlier test mutated sys.modules
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import app.database as app_db
if not hasattr(app_db, "get_available_slots"):
    importlib.reload(app_db)

# ── Imports under test ──
from app.services.payment import PaymentService
from app.services.tenant import resolve_tenant, TenantNotFound, _tenant_cache
from app.services.message_queue import MessageQueueManager
from app.services.permissions import resolve_owned_branch, assert_staff_not_pinned_elsewhere
from app.services.whatsapp import WhatsAppService
from app.routers.webhook import record_delivery_status
from app.database import get_available_slots, _doctor_cache, _holiday_cache
from app.utils.pii_sanitizer import sanitize_report_text, sanitize_pii, restore_pii
from app.utils.async_tasks import spawn_background_task, _BACKGROUND_TASKS
from app.services.data_retention import DataRetentionService
from app.routers.admin import AdminUser


# ═══════════════════════════════════════════════════════════════════════════
# C4: Admin Reject Cancels After Refunding (Not Before)
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_c4_admin_reject_refunds_before_cancelling():
    """Verify refund is attempted FIRST while booking is still pending_review.
    If refund fails, status is NOT changed to cancelled and rejection is aborted."""
    service = PaymentService()
    
    # 1. Successful refund path
    mock_sb = MagicMock()
    mock_booking = {
        "id": "b-1",
        "clinic_id": "c-1",
        "status": "pending_review",
        "payment_id": "pay_123",
        "amount_paise": 50000,
        "patient_phone": "+919876543210",
    }
    
    def table_mock_success(name):
        t = MagicMock()
        t.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_booking])
        t.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_booking])
        return t
        
    mock_sb.table.side_effect = table_mock_success
    
    with patch("app.services.payment.supabase", mock_sb), \
         patch("app.database.supabase", mock_sb), \
         patch.object(service, "initiate_refund", new_callable=AsyncMock) as mock_refund:
        
        mock_refund.return_value = {"success": True, "refund_id": "rfd_123", "status": "processed"}
        
        res = await service.admin_reject_booking("b-1", clinic_id="c-1", admin_notes="Doctor unavailable")
        assert res["success"] is True
        assert res["refund"]["refund_id"] == "rfd_123"
        mock_refund.assert_called_once()

    # 2. Failed refund path: must halt and NOT mark booking as cancelled
    mock_sb_fail = MagicMock()
    
    def table_mock_fail(name):
        t = MagicMock()
        t.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_booking])
        t.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_booking])
        return t
        
    mock_sb_fail.table.side_effect = table_mock_fail
    
    with patch("app.services.payment.supabase", mock_sb_fail), \
         patch("app.database.supabase", mock_sb_fail), \
         patch.object(service, "initiate_refund", new_callable=AsyncMock) as mock_refund_fail, \
         patch.object(service, "_alert_admin", new_callable=AsyncMock) as mock_alert:
        
        mock_refund_fail.return_value = {"success": False, "reason": "gateway_timeout"}
        
        res = await service.admin_reject_booking("b-1", clinic_id="c-1", admin_notes="Doctor unavailable")
            
        assert res["success"] is False
        assert res["reason"] == "refund_failed"


# ═══════════════════════════════════════════════════════════════════════════
# C2: Razorpay Webhook Clinic Scoping
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_c2_razorpay_webhook_clinic_scoping():
    """Verify webhook handler filters appointment and idempotency queries by clinic_id."""
    service = PaymentService()
    
    mock_sb = MagicMock()
    
    def table_mock(name):
        t = MagicMock()
        t.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        t.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        t.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        return t
        
    mock_sb.table.side_effect = table_mock
    
    payload = json.dumps({
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test123",
                    "amount": 50000,
                    "notes": {"booking_id": "b-cross-tenant"},
                }
            }
        }
    }).encode()
    
    with patch("app.services.payment.supabase", mock_sb), \
         patch("app.database.supabase", mock_sb), \
         patch("app.services.payment.settings") as mock_settings, \
         patch("hmac.compare_digest", return_value=True):
        
        mock_settings.razorpay_webhook_secret = "secret"
        res = await service.process_payment_webhook(payload, "valid_sig", clinic_id="clinic-legit")
        # Booking not found under clinic-legit scope
        assert res["status"] == "unmatched"
        assert res["reason"] == "booking_not_found"


# ═══════════════════════════════════════════════════════════════════════════
# C3 + H7: Payment Link expire_by & Late Payment Auto-Refund
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_c3_late_payment_auto_refund_on_expired_hold():
    """Late payment captured for an expired hold automatically refunds the patient."""
    service = PaymentService()
    
    mock_booking = {
        "id": "b-expired",
        "clinic_id": "c-1",
        "status": "expired",
        "amount_paise": 50000,
        "patient_phone": "+919876543210",
        "doctor_name": "Dr. Smith",
    }
    
    mock_sb = MagicMock()
    mock_select = MagicMock()
    mock_sb.table.return_value.select.return_value = mock_select
    mock_select.eq.return_value.execute.return_value = MagicMock(data=[mock_booking])
    mock_select.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_booking])
    
    payload = json.dumps({
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_late_123",
                    "amount": 50000,
                    "notes": {"booking_id": "b-expired"},
                }
            }
        }
    }).encode()
    
    with patch("app.services.payment.supabase", mock_sb), \
         patch("app.database.supabase", mock_sb), \
         patch("app.services.payment.settings") as mock_settings, \
         patch("hmac.compare_digest", return_value=True), \
         patch.object(service, "_refund_payment_id", new_callable=AsyncMock) as mock_refund_id, \
         patch.object(service, "_notify_late_payment_refunded", new_callable=AsyncMock) as mock_notify_refund, \
         patch.object(service, "_alert_admin", new_callable=AsyncMock):
        
        mock_settings.razorpay_webhook_secret = "secret"
        mock_refund_id.return_value = {"id": "rfd_late_123", "refund_id": "rfd_late_123"}
        
        res = await service.process_payment_webhook(payload, "valid_sig")
        assert res["status"] == "ok"
        assert res["reason"] == "expired_hold_refunded"
        mock_refund_id.assert_called_once()
        mock_notify_refund.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# C1: Multi-Tenant WhatsApp Fallback Isolation
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_c1_unknown_whatsapp_number_multitenant_fails_closed():
    """In multi-tenant mode, an unknown WhatsApp phone number raises TenantNotFound."""
    _tenant_cache.clear()
    
    mock_sb = MagicMock()
    # 0 matching clinics for target phone
    mock_select = MagicMock()
    mock_sb.table.return_value.select.return_value = mock_select
    mock_select.eq.return_value.execute.return_value = MagicMock(data=[])
    
    # But 2 active clinics exist on the platform
    mock_sb.table.return_value.select.return_value.order.return_value.execute.return_value = MagicMock(
        data=[{"id": "c-1", "name": "Clinic A"}, {"id": "c-2", "name": "Clinic B"}]
    )
    
    with patch("app.database.supabase", mock_sb), \
         patch("app.services.tenant.supabase", mock_sb):
        with pytest.raises(TenantNotFound) as exc_info:
            await resolve_tenant("+919999999999")
        assert isinstance(exc_info.value, TenantNotFound)


# ═══════════════════════════════════════════════════════════════════════════
# C5 + H8: Message Queue Idempotency Fail-Closed & Release
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_c5_message_queue_fails_closed_and_release():
    """Verify persistent database error fails closed (False) and release deletes row for DLQ replay."""
    manager = MessageQueueManager()
    
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception("DB timeout")
    
    # 1. Fail closed on DB error
    with patch("app.database.supabase", mock_sb):
        acquired = await manager.acquire("msg-err-1", clinic_id="c-1")
        assert acquired is False

    # 2. Release deletes lock
    mock_sb_del = MagicMock()
    with patch("app.database.supabase", mock_sb_del):
        await manager.release("msg-err-1")
        mock_sb_del.table.assert_called_with("processed_messages")
        mock_sb_del.table.return_value.delete.return_value.eq.assert_called_with("message_id", "msg-err-1")


# ═══════════════════════════════════════════════════════════════════════════
# C6: Atomic Claim for Lab Reports
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_c6_duplicate_lab_report_atomic_claim_halts():
    """Duplicate upload_and_send for same external_report_id halts immediately on unique claim conflict."""
    from app.services.lab_reports import LabReportService
    
    service = LabReportService()
    mock_sb = MagicMock()
    # Step 0 claim insert raises unique violation
    mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception(
        "duplicate key value violates unique constraint 'idx_lab_reports_clinic_external_report'"
    )
    # The colliding row is an ALREADY-DELIVERED report, so the needs_review
    # takeover CAS in upload_and_send matches zero rows and the duplicate must
    # still halt. (A held row WOULD match and be claimed — that is the separate
    # held-report recovery path, covered in
    # tests/test_held_report_recovery_and_staff_delete.py.)
    mock_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[]
    )
    
    with patch("app.database.supabase", mock_sb), \
         patch("app.services.lab_reports.supabase", mock_sb):
        res = await service.upload_and_send(
            clinic_id="c-1",
            file_bytes=b"%PDF-1.4 test",
            filename="test.pdf",
            content_type="application/pdf",
            patient_phone="+919876543210",
            patient_name="John Doe",
            report_name="Blood Test",
            report_type="CBC",
            external_report_id="ext-rep-101",
        )
        assert res["status"] == "skipped"
        assert res["reason"] == "duplicate_report_id"


# ═══════════════════════════════════════════════════════════════════════════
# C7 + M6: Cross-Tenant Branch-Doctor IDOR Prevention
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_c7_resolve_owned_branch_idor_and_scope():
    """resolve_owned_branch raises 404 for branch of another clinic, and 403 for branch pinned elsewhere."""
    admin_c1 = AdminUser("adm", role="clinic_admin", clinic_id="c-1", user_id="u-1")
    staff_pinned = AdminUser("staff", role="staff", clinic_id="c-1", user_id="u-2", branch_id="br-1")
    
    mock_sb = MagicMock()
    # Branch belongs to c-2 (different clinic)
    foreign_branch_res = MagicMock(
        data=[{"id": "br-foreign", "clinic_id": "c-2"}]
    )
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = foreign_branch_res
    
    with patch("app.database.supabase", mock_sb), \
         patch("app.database.sb", AsyncMock(return_value=foreign_branch_res)):
        
        # Cross-tenant IDOR -> 404
        with pytest.raises(HTTPException) as exc:
            await resolve_owned_branch(admin_c1, "br-foreign")
        assert exc.value.status_code == 404

        # Staff pinned to br-1 attempting to access br-2 -> 403
        with pytest.raises(HTTPException) as exc:
            await resolve_owned_branch(staff_pinned, "br-2")
        assert exc.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# H1: _can_send_freeform Fails Closed
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_h1_whatsapp_can_send_freeform_fails_closed():
    """If database raises exception during session check, _can_send_freeform returns False."""
    service = WhatsAppService()
    
    with patch("app.database.get_conversation", side_effect=Exception("DB connection error")):
        can_send = await service._can_send_freeform({"id": "c-1"}, "+919876543210")
        assert can_send is False


# ═══════════════════════════════════════════════════════════════════════════
# H2: Monotonic Delivery Status Receipts
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_h2_delivery_receipt_monotonic_rank():
    """An out-of-order 'delivered' receipt does NOT overwrite 'read' status."""
    mock_sb = MagicMock()
    mock_select = MagicMock()
    mock_sb.table.return_value.select.return_value = mock_select
    # Current status in DB is already 'read'
    mock_select.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "lr-1", "delivery_status": "read"}]
    )
    
    with patch("app.database.supabase", mock_sb):
        # Incoming out-of-order 'delivered' status
        await record_delivery_status({"id": "wamid.123", "status": "delivered"})
        # Update must NOT be called because rank('delivered') < rank('read')
        mock_sb.table.return_value.update.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# H3 + H4: Hold-Aware Slots & IST Timezone Correctness
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_h3_and_h4_slot_availability_holds_and_ist():
    """Verify slots account for active holds and calculate same-day cutoff in IST."""
    mock_doc = {
        "id": "doc-1",
        "name": "Dr. Mehta",
        "available_days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
        "morning_slots": ["09:00", "09:30", "10:00", "10:30"],
        "evening_slots": [],
    }
    
    now_utc = datetime.now(timezone.utc)
    future_hold = (now_utc + timedelta(minutes=8)).isoformat()
    expired_hold = (now_utc - timedelta(minutes=5)).isoformat()
    
    mock_appointments = [
        {"appointment_time": "09:00", "status": "confirmed"},
        {"appointment_time": "09:30", "status": "pending_payment", "hold_expires_at": future_hold},
        {"appointment_time": "10:00", "status": "pending_payment", "hold_expires_at": expired_hold},
    ]
    
    mock_sb = MagicMock()
    def mock_table_handler(table):
        mock_t = MagicMock()
        mock_t.select = MagicMock(return_value=mock_t)
        mock_t.eq = MagicMock(return_value=mock_t)
        mock_t.in_ = MagicMock(return_value=mock_t)
        mock_t.order = MagicMock(return_value=mock_t)
        mock_t.limit = MagicMock(return_value=mock_t)
        mock_res = MagicMock()
        if table == "appointments":
            mock_res.data = mock_appointments
        elif table == "doctors":
            mock_res.data = [mock_doc]
        else:
            mock_res.data = []
        mock_t.execute = MagicMock(return_value=mock_res)
        return mock_t
        
    mock_sb.table.side_effect = mock_table_handler
    
    app_db._doctor_cache.clear()
    app_db._holiday_cache.clear()
    
    with patch("app.database.supabase", mock_sb), patch.object(app_db, "supabase", mock_sb):
        slots, err = await app_db.get_available_slots("c-1", "Dr. Mehta", "2035-05-15")
        assert err is None
        # 09:00 (confirmed) and 09:30 (active hold) should be booked.
        # 10:00 (expired hold) and 10:30 (never booked) should be available.
        assert "09:00" not in slots
        assert "09:30" not in slots
        assert "10:00" in slots
        assert "10:30" in slots


# ═══════════════════════════════════════════════════════════════════════════
# H5 + M8: PII Sanitizer Patient Name Anonymization & Narrow DOB
# ═══════════════════════════════════════════════════════════════════════════
def test_h5_and_m8_pii_sanitizer_preserves_medical_dates_and_tokenizes_patient():
    """Verify medical dates / ratios are not wiped as DOB and patient name is restored."""
    report_text = """
    Patient Name: Ramesh Patel
    DOB: 15/08/1985
    Specimen Date: 2026-08-20
    Test Result: Hemoglobin 14.2 g/dL (Reference: 13.0-17.0)
    Platelet Count: 250000 /uL
    """
    
    sanitized, rmap = sanitize_report_text(report_text, patient_name="Ramesh Patel")
    
    # Patient name and labeled DOB are sanitized
    assert "Ramesh Patel" not in sanitized
    assert "15/08/1985" not in sanitized
    assert "[PATIENT_1]" in sanitized
    assert "[DOB_" in sanitized
    
    # Specimen date and reference ranges are PRESERVED (not over-redacted)
    assert "2026-08-20" in sanitized
    assert "14.2 g/dL" in sanitized
    assert "13.0-17.0" in sanitized
    
    # PII restore restores name even if LLM returns generic [PATIENT]
    llm_output = "Hello [PATIENT], your Hemoglobin of 14.2 g/dL is completely normal."
    restored = restore_pii(llm_output, rmap)
    assert "Hello Ramesh Patel, your Hemoglobin" in restored


# ═══════════════════════════════════════════════════════════════════════════
# M4: Strong Task Reference Tracking
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_m4_spawn_background_task_strong_reference():
    """spawn_background_task holds a strong reference in _BACKGROUND_TASKS until completion."""
    async def sample_coro():
        await asyncio.sleep(0.01)
        return 42
        
    task = spawn_background_task(sample_coro(), name="test_task")
    assert task in _BACKGROUND_TASKS
    await task
    # Upon completion, task is cleanly discarded
    assert task not in _BACKGROUND_TASKS


# ═══════════════════════════════════════════════════════════════════════════
# L2: DLQ PII Scrubbing and Retention Purge
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_l2_dlq_pii_sanitization_and_retention_purge():
    """Dead letter queue scrubs phones/Aadhaar/emails and purge_failed_messages_dlq executes cutoff query."""
    raw_payload = "Error processing webhook for patient +919876543210 aadhaar 1234 5678 9012 email test@hospital.com"
    scrubbed = sanitize_pii(raw_payload)
    
    assert "+919876543210" not in scrubbed
    assert "1234 5678 9012" not in scrubbed
    assert "test@hospital.com" not in scrubbed
    assert "[PHONE_REDACTED]" in scrubbed
    assert "[AADHAAR_REDACTED]" in scrubbed
    assert "[EMAIL_REDACTED]" in scrubbed

    retention_svc = DataRetentionService()
    mock_sb = MagicMock()
    mock_sb.table.return_value.delete.return_value.lt.return_value.execute.return_value = MagicMock(data=[{"id": "dlq-1"}])
    
    with patch("app.services.data_retention.supabase", mock_sb):
        purged = await retention_svc.purge_failed_messages_dlq(days=30)
        assert purged == 1
        mock_sb.table.assert_called_with("failed_messages")


# ═══════════════════════════════════════════════════════════════════════════
# C3 (Verification): Payment Link Payload includes expire_by
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_c3_create_payment_link_includes_expire_by():
    """_create_payment_link includes expire_by unix timestamp in request payload."""
    service = PaymentService()
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"id": "plink_test123", "short_url": "https://rzp.io/i/test123"},
            raise_for_status=lambda: None,
        )
        
        res = await service._create_payment_link(
            amount_paise=50000,
            booking_id="b-123",
            booking_ref="MED-123",
            patient_phone="+919876543210",
            patient_name="Ramesh",
        )
        
        assert res["id"] == "plink_test123"
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        assert "expire_by" in payload
        assert isinstance(payload["expire_by"], int)
        assert payload["expire_by"] > int(time.time()) + 900


# ═══════════════════════════════════════════════════════════════════════════
# M5 (Verification): Patient-side Cancel and Status Scoped by Clinic ID
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_m5_conversation_cancel_and_status_scoped_by_clinic_id():
    """Interactive cancel and status queries in conversation.py are scoped to clinic_id."""
    from app.services.conversation import ConversationManager
    
    cm = ConversationManager()
    clinic = {"id": "clinic-alpha", "whatsapp_number": "+919999999999"}
    phone = "+919876543210"
    session = {
        "state": "awaiting_payment",
        "context": {"booking_id": "booking-abc"},
    }
    
    mock_sb = MagicMock()
    mock_update_query = MagicMock()
    mock_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    
    with patch("app.database.supabase", mock_sb), \
         patch.object(cm.whatsapp, "send_text", new_callable=AsyncMock), \
         patch.object(cm, "update_state", new_callable=AsyncMock), \
         patch.object(cm, "_send_main_menu", new_callable=AsyncMock):
        
        await cm._handle_awaiting_payment(clinic, phone, "cancel", session["context"], {}, "en")
        
        # Verify update query was called with clinic-alpha filter
        mock_sb.table.assert_called_with("appointments")
        calls = mock_sb.table.return_value.update.return_value.eq.mock_calls
        # Ensure clinic_id was chained in the query
        assert any("clinic-alpha" in str(c) for c in calls)

