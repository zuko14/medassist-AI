"""Phase 2: Tenant Isolation on Resend & Refund Endpoints Tests.

Verifies:
1. P0-2: POST /admin/lab-reports/{id}/resend strictly enforces clinic boundaries.
   Cross-tenant attempts are rejected (404/403).
2. P0-3: POST /admin/bookings/{id}/refund strictly enforces clinic boundaries
   and resolves per-clinic Razorpay credentials.
"""

import uuid
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import AdminUser, verify_credentials, require_admin


def test_resend_lab_report_cross_tenant_rejected():
    """P0-2: Clinic A admin cannot resend a lab report belonging to Clinic B."""
    client = TestClient(app)

    clinic_a_user = AdminUser(
        username="admin_a",
        role="clinic_admin",
        clinic_id="clinic_aaa",
        user_id="user_a_001",
    )

    fake_report_id = str(uuid.uuid4())

    app.dependency_overrides[verify_credentials] = lambda: clinic_a_user

    try:
        # Mock LabReportService to raise ValueError("Report not found") when scoped query returns empty
        with patch("app.routers.admin.LabReportService.resend_report", new_callable=AsyncMock) as mock_resend:
            mock_resend.side_effect = ValueError("Report not found")

            response = client.post(f"/admin/lab-reports/{fake_report_id}/resend")
            assert response.status_code == 404
            assert "Report not found" in response.json()["detail"]

            # Verify resend_report was called with clinic_id="clinic_aaa"
            mock_resend.assert_called_once_with(fake_report_id, clinic_id="clinic_aaa")
    finally:
        app.dependency_overrides.pop(verify_credentials, None)


def test_resend_lab_report_own_tenant_succeeds():
    """P0-2: Clinic A admin can resend a lab report belonging to Clinic A."""
    client = TestClient(app)

    clinic_a_user = AdminUser(
        username="admin_a",
        role="clinic_admin",
        clinic_id="clinic_aaa",
        user_id="user_a_001",
    )

    fake_report_id = str(uuid.uuid4())

    app.dependency_overrides[verify_credentials] = lambda: clinic_a_user

    try:
        with patch("app.routers.admin.LabReportService.resend_report", new_callable=AsyncMock) as mock_resend:
            mock_resend.return_value = {"success": True}

            response = client.post(f"/admin/lab-reports/{fake_report_id}/resend")
            assert response.status_code == 200
            assert response.json()["success"] is True

            mock_resend.assert_called_once_with(fake_report_id, clinic_id="clinic_aaa")
    finally:
        app.dependency_overrides.pop(verify_credentials, None)


def test_resend_lab_report_super_admin_unrestricted():
    """P0-2: Super admin can resend any lab report without clinic filter."""
    client = TestClient(app)

    super_user = AdminUser(
        username="superadmin",
        role="super_admin",
        clinic_id=None,
        user_id="super_001",
    )

    fake_report_id = str(uuid.uuid4())

    app.dependency_overrides[verify_credentials] = lambda: super_user

    try:
        with patch("app.routers.admin.LabReportService.resend_report", new_callable=AsyncMock) as mock_resend:
            mock_resend.return_value = {"success": True}

            response = client.post(f"/admin/lab-reports/{fake_report_id}/resend")
            assert response.status_code == 200
            assert response.json()["success"] is True

            mock_resend.assert_called_once_with(fake_report_id, clinic_id=None)
    finally:
        app.dependency_overrides.pop(verify_credentials, None)


def test_admin_refund_booking_cross_tenant_rejected():
    """P0-3: Clinic A admin cannot refund a booking belonging to Clinic B (HTTP 403)."""
    client = TestClient(app)

    clinic_a_user = AdminUser(
        username="admin_a",
        role="clinic_admin",
        clinic_id="clinic_aaa",
        user_id="user_a_001",
    )

    fake_booking_id = str(uuid.uuid4())
    fake_booking_b = {
        "id": fake_booking_id,
        "clinic_id": "clinic_bbb",
        "status": "confirmed",
        "payment_id": "pay_test_b_001",
        "amount_paise": 50000,
    }

    app.dependency_overrides[require_admin] = lambda: clinic_a_user

    try:
        mock_select = MagicMock()
        mock_select.eq.return_value.execute.return_value.data = [fake_booking_b]

        with patch("app.routers.admin.supabase.table") as mock_table:
            mock_table.return_value.select.return_value = mock_select

            response = client.post(f"/admin/bookings/{fake_booking_id}/refund", json={"reason": "Test refund"})
            assert response.status_code == 403
            assert "Forbidden" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_admin_refund_booking_own_tenant_resolves_clinic_creds():
    """P0-3: Clinic A admin refunding Clinic A booking resolves per-clinic credentials."""
    client = TestClient(app)

    clinic_a_user = AdminUser(
        username="admin_a",
        role="clinic_admin",
        clinic_id="clinic_aaa",
        user_id="user_a_001",
    )

    fake_booking_id = str(uuid.uuid4())
    fake_booking_a = {
        "id": fake_booking_id,
        "clinic_id": "clinic_aaa",
        "status": "confirmed",
        "payment_id": "pay_test_a_001",
        "amount_paise": 50000,
    }
    clinic_a_dict = {
        "id": "clinic_aaa",
        "name": "Clinic Alpha",
        "config": {"razorpay_key_id": "rzp_custom_a", "razorpay_key_secret": "sec_custom_a"}
    }

    app.dependency_overrides[require_admin] = lambda: clinic_a_user

    try:
        mock_select = MagicMock()
        mock_select.eq.return_value.execute.return_value.data = [fake_booking_a]

        with patch("app.routers.admin.supabase.table") as mock_table, \
             patch("app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value=clinic_a_dict) as mock_get_clinic, \
             patch("app.services.payment.payment_service.initiate_refund", new_callable=AsyncMock) as mock_initiate_refund:

            mock_table.return_value.select.return_value = mock_select
            mock_initiate_refund.return_value = {"success": True, "refund_id": "rfnd_test_a_01"}

            response = client.post(f"/admin/bookings/{fake_booking_id}/refund", json={"reason": "Customer request"})
            assert response.status_code == 200
            assert response.json()["success"] is True

            mock_get_clinic.assert_called_once_with("clinic_aaa")
            mock_initiate_refund.assert_called_once_with(
                fake_booking_id,
                "Customer request",
                clinic=clinic_a_dict,
                idempotency_key=None,
            )
    finally:
        app.dependency_overrides.pop(require_admin, None)
