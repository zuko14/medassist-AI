"""Systematic Regression & Adversarial Invariant Tests from Section Y of Forensic Audit.

Tests:
1. Defect 7 (P1-7): _check_payment_link_status returning unknown/raising -> booking is NOT expired.
2. Defect 10 (P3): check_in_appointment called twice -> token number remains unchanged.
3. Defect 11 (P2-2): invalidate_tenant_cache clears both whatsapp_number and phone_number_id keys.
4. Defect 12 (P2-3): assign_doctor_to_branch with cross-tenant doctor_id -> 404.
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.services.payment import PaymentService
from app.services.tenant import invalidate_tenant_cache, _tenant_cache, _set_cached_item
from app.database import check_in_appointment
from app.routers.admin import require_permission, AdminUser


@pytest.mark.asyncio
async def test_expiry_skips_when_payment_link_check_fails():
    """Defect 7 (P1-7): Razorpay link check error/unknown skips expiry to prevent false expiration."""
    service = PaymentService()

    fake_stale_booking = {
        "id": "bk_stale_999",
        "clinic_id": "00000000-0000-0000-0000-000000000001",
        "status": "pending_payment",
        "payment_link_id": "plink_test_999",
        "hold_expires_at": "2026-08-25T10:00:00Z",
        "patient_phone": "+919876543210",
    }

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value.lt.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[fake_stale_booking]
    )

    with patch("app.services.payment.supabase.table", return_value=mock_table), \
         patch.object(service, "_check_payment_link_status", new_callable=AsyncMock, return_value={"status": "unknown", "payment_id": ""}), \
         patch("app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value={"id": "00000000-0000-0000-0000-000000000001", "config": {}}):

        count = await service.expire_stale_bookings()

        # Should be 0 expired because status was unknown and skipped safely
        assert count == 0
        # Verify update to 'expired' was NEVER executed
        for call in mock_table.update.call_args_list:
            assert call[0][0] != {"status": "expired"}


@pytest.mark.asyncio
async def test_check_in_appointment_idempotent_token():
    """Defect 10 (P3): check_in_appointment called twice preserves the existing token."""
    import app.database as db_mod

    test_appt_id = "00000000-0000-0000-0000-000000000123"
    test_clinic_id = "00000000-0000-0000-0000-000000000001"

    existing_record = {
        "id": test_appt_id,
        "clinic_id": test_clinic_id,
        "doctor_name": "Dr. Sarah",
        "appointment_date": "2026-08-25",
        "token_number": 7,
        "queue_status": "waiting",
    }

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[existing_record]
    )

    mock_supabase = MagicMock()
    mock_supabase.table.return_value = mock_table

    with patch.object(db_mod, "supabase", mock_supabase):
        res1 = await db_mod.check_in_appointment(test_clinic_id, test_appt_id)
        assert res1 is not None
        assert res1["token_number"] == 7

        # Second call
        res2 = await db_mod.check_in_appointment(test_clinic_id, test_appt_id)
        assert res2 is not None
        assert res2["token_number"] == 7

        # Verify update was NOT called because token already existed
        mock_table.update.assert_not_called()


def test_invalidate_tenant_cache_purges_both_phone_and_phone_number_id():
    """Defect 11 (P2-2): invalidate_tenant_cache clears both phone and phone_number_id keys."""
    _tenant_cache.clear()

    clinic_data = {
        "id": "clinic_100",
        "name": "Life Hospital",
        "whatsapp_number": "+919876543210",
        "phone_number_id": "PNID_123456",
    }

    _set_cached_item(_tenant_cache, "+919876543210", clinic_data)
    _set_cached_item(_tenant_cache, "PNID_123456", clinic_data)

    assert "+919876543210" in _tenant_cache
    assert "PNID_123456" in _tenant_cache

    # Invalidate by phone number
    invalidate_tenant_cache(whatsapp_number="+919876543210")

    # Both keys must be cleared
    assert "+919876543210" not in _tenant_cache
    assert "PNID_123456" not in _tenant_cache


def test_assign_doctor_to_branch_cross_tenant_rejected():
    """Defect 12 (P2-3): assign_doctor_to_branch with cross-tenant doctor_id returns 404."""
    from app.routers.admin import verify_credentials

    client = TestClient(app)

    admin_user = AdminUser("admin")
    admin_user.role = "clinic_admin"
    admin_user.clinic_id = "clinic_a"
    admin_user.permissions = ["DOCTOR_BRANCH_ASSIGN"]

    mock_branch = {
        "id": "branch_1",
        "clinic_id": "clinic_a",
        "name": "Branch 1",
    }

    app.dependency_overrides[verify_credentials] = lambda: admin_user

    try:
        with patch("app.routers.admin.resolve_owned_branch", return_value=mock_branch), \
             patch("app.routers.admin.supabase.table") as mock_table:

            # Doctor lookup in clinic_a returns empty (doctor belongs to another clinic)
            mock_table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )

            payload = {
                "doctor_id": "doc_cross_tenant_999",
                "session": "morning",
            }

            response = client.post("/admin/branches/branch_1/doctors", json=payload)
            assert response.status_code == 404
            assert "Doctor not found" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(verify_credentials, None)
