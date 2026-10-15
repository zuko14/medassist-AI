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


def test_resend_lab_report_super_admin_must_name_a_clinic():
    """A super_admin with no clinic scope cannot resend ANY report.

    This assertion is inverted from what it used to be. It previously read
    "Super admin can resend any lab report without clinic filter" and asserted
    resend_report(..., clinic_id=None) — an unscoped call that reaches every
    tenant's lab_reports by row id. That is the same "super_admin means no
    tenant predicate" assumption that let a super_admin delete a live clinic's
    doctors from a test clinic's panel (2026-09-01). /admin is single-tenant:
    the super_admin must name the clinic.
    """
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

            # No clinic named -> refused, and the service is never reached.
            response = client.post(f"/admin/lab-reports/{fake_report_id}/resend")
            assert response.status_code == 400
            mock_resend.assert_not_called()

            # Naming a clinic scopes the call to exactly that clinic.
            response = client.post(
                f"/admin/lab-reports/{fake_report_id}/resend?clinic_id=clinic_bbb"
            )
            assert response.status_code == 200
            mock_resend.assert_called_once_with(fake_report_id, clinic_id="clinic_bbb")
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
        # D-37 (b): the lookup is scoped to the caller's clinic, where Clinic B's booking does not exist.
        mock_select.eq.return_value.eq.return_value.execute.return_value.data = []

        with patch("app.routers.admin.supabase.table") as mock_table:
            mock_table.return_value.select.return_value = mock_select

            response = client.post(f"/admin/bookings/{fake_booking_id}/refund", json={"reason": "Test refund"})
            assert response.status_code == 404  # was 403; 404 also hides that the booking exists
            assert response.json()["detail"] == "Booking not found"
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
        mock_select.eq.return_value.eq.return_value.execute.return_value.data = [fake_booking_a]  # D-37 (b) scoped

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
                enforce_window=False,  # staff refunds ignore the patient cutoff
            )
    finally:
        app.dependency_overrides.pop(require_admin, None)


@pytest.mark.parametrize("reason, shown", [
    ("razorpay_error: HTTPSConnectionPool(host='api.razorpay.com') key=rzp_secret_123", None),
    ("phonepe_error: Traceback (most recent call last)", None),
    ("no_payment_to_refund", "no_payment_to_refund"),
    ("cannot_refund_status_cancelled", "cannot_refund_status_cancelled"),
])
def test_admin_refund_never_returns_gateway_exception_text(reason, shown):
    """D-37: a gateway failure's raw exception text never reaches the client; the fixed reason codes still do."""
    client = TestClient(app)
    user = AdminUser(username="admin_a", role="clinic_admin", clinic_id="clinic_aaa", user_id="user_a_001")
    booking = {"id": str(uuid.uuid4()), "clinic_id": "clinic_aaa", "status": "confirmed",
               "payment_id": "pay_a", "amount_paise": 50000}
    app.dependency_overrides[require_admin] = lambda: user
    try:
        mock_select = MagicMock()
        mock_select.eq.return_value.eq.return_value.execute.return_value.data = [booking]
        with patch("app.routers.admin.supabase.table") as mock_table, \
             patch("app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value={"id": "clinic_aaa"}), \
             patch("app.services.payment.payment_service.initiate_refund", new_callable=AsyncMock,
                   return_value={"success": False, "reason": reason}):
            mock_table.return_value.select.return_value = mock_select
            response = client.post(f"/admin/bookings/{booking['id']}/refund", json={"reason": "x"})
        detail = response.json()["detail"]
        assert response.status_code == 400
        assert detail == shown if shown else ("rzp_secret" not in detail and "Traceback" not in detail
                                              and "gateway" in detail.lower())
    finally:
        app.dependency_overrides.pop(require_admin, None)


def _refund_as(user, clinic_scope, booking, *, filtered_rows, unfiltered_rows):
    """The booking query: filtered by the session clinic -> filtered_rows; by id alone -> unfiltered_rows."""
    client = TestClient(app)
    app.dependency_overrides[require_admin] = lambda: user
    try:
        mock_select = MagicMock()
        mock_select.eq.return_value.execute.return_value.data = unfiltered_rows
        mock_select.eq.return_value.eq.return_value.execute.return_value.data = filtered_rows
        with patch("app.routers.admin.supabase.table") as mock_table, \
             patch("app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value={"id": clinic_scope}), \
             patch("app.services.payment.payment_service.initiate_refund", new_callable=AsyncMock,
                   return_value={"success": True, "refund_id": "rfnd_x"}) as refund:
            mock_table.return_value.select.return_value = mock_select
            r = client.post(f"/admin/bookings/{booking['id']}/refund?clinic_id={clinic_scope}", json={"reason": "x"})
        return r, refund
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_super_admin_cannot_refund_another_clinics_booking_from_this_panel():
    """D-37 (b): the booking is looked up inside the session's clinic, so a super_admin working in
    clinic A gets 404 for clinic B's booking id (it used to be refunded)."""
    clinic_a, clinic_b = str(uuid.uuid4()), str(uuid.uuid4())
    owner = AdminUser(username="root", role="super_admin", clinic_id=None, user_id="super_admin_env")
    booking_b = {"id": str(uuid.uuid4()), "clinic_id": clinic_b, "status": "confirmed", "payment_id": "p", "amount_paise": 100}
    r, refund = _refund_as(owner, clinic_a, booking_b, filtered_rows=[], unfiltered_rows=[booking_b])
    assert r.status_code == 404
    refund.assert_not_awaited()


def test_super_admin_refunds_a_booking_of_the_clinic_they_are_working_in():
    clinic_a = str(uuid.uuid4())
    owner = AdminUser(username="root", role="super_admin", clinic_id=None, user_id="super_admin_env")
    booking_a = {"id": str(uuid.uuid4()), "clinic_id": clinic_a, "status": "confirmed", "payment_id": "p", "amount_paise": 100}
    r, refund = _refund_as(owner, clinic_a, booking_a, filtered_rows=[booking_a], unfiltered_rows=[booking_a])
    assert r.status_code == 200 and refund.await_count == 1
