"""Phase B: Comprehensive Adversarial Multi-Tenant Boundary Enforcement Tests.

Executes adversarial HTTP tests across all admin and clinic-scoped endpoints:
- Tenant A Clinic Admin attempting to read / write / delete Tenant B resources
- Verification that all endpoints return HTTP 403 Forbidden or strict tenant-filtered responses
- Verification that Super Admin can access authorized cross-tenant endpoints
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import AdminUser, verify_credentials


@pytest.fixture
def client():
    return TestClient(app)


def mock_admin_user(username: str, role: str, clinic_id: str, permissions: list = None):
    return AdminUser(
        username=username,
        role=role,
        clinic_id=clinic_id,
        user_id=f"uid_{username}",
        permissions=permissions or ["all"],
        branch_id=None,
        staff_role=None,
    )


def test_adversarial_bookings_cross_tenant_access(client):
    """Phase B: Clinic Admin A querying bookings with clinic_id=clinic_b is rejected with 403."""
    user_a = mock_admin_user("admin_a", "clinic_admin", "clinic_a")
    app.dependency_overrides[verify_credentials] = lambda: user_a

    try:
        response = client.get(
            "/admin/bookings?clinic_id=clinic_b",
            headers={"Authorization": "Basic YWRtaW5fYTpwYXNzd29yZDEyMw=="},
        )
        assert response.status_code == 403
        assert "restricted" in response.json()["detail"].lower() or "forbidden" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_adversarial_refund_cross_tenant_rejected(client):
    """Phase B: Clinic Admin A attempting to refund Tenant B booking is rejected."""
    user_a = mock_admin_user("admin_a", "clinic_admin", "clinic_a")
    app.dependency_overrides[verify_credentials] = lambda: user_a

    fake_booking_b = {
        "id": "book_b_001",
        "clinic_id": "clinic_b",
        "status": "confirmed",
        "payment_id": "pay_b_001",
        "amount_paise": 50000,
    }

    mock_db = MagicMock()
    mock_db.select.return_value.eq.return_value.execute.return_value.data = [fake_booking_b]

    try:
        with patch("app.routers.admin.supabase.table", return_value=mock_db):
            response = client.post(
                "/admin/bookings/book_b_001/refund",
                headers={"Authorization": "Basic YWRtaW5fYTpwYXNzd29yZDEyMw=="},
                json={"reason": "Customer cancellation"},
            )
            assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_adversarial_doctor_creation_cross_tenant_rejected(client):
    """Phase B: Clinic Admin A creating a doctor with clinic_id=clinic_b is rejected."""
    user_a = mock_admin_user("admin_a", "clinic_admin", "clinic_a")
    app.dependency_overrides[verify_credentials] = lambda: user_a

    try:
        response = client.post(
            "/admin/doctors?clinic_id=clinic_b",
            headers={"Authorization": "Basic YWRtaW5fYTpwYXNzd29yZDEyMw=="},
            json={
                "name": "Dr. Hacker",
                "department": "Cardiology",
                "specialization": "Heart",
                "morning_start": "09:00",
                "morning_end": "12:00",
            },
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_adversarial_lab_tests_cross_tenant_rejected(client):
    """Phase B: Clinic Admin A cannot list lab tests in clinic_b."""
    user_a = mock_admin_user("admin_a", "clinic_admin", "clinic_a")
    app.dependency_overrides[verify_credentials] = lambda: user_a

    try:
        response = client.get(
            "/admin/lab-tests?clinic_id=clinic_b",
            headers={"Authorization": "Basic YWRtaW5fYTpwYXNzd29yZDEyMw=="},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_adversarial_audit_logs_cross_tenant_rejected(client):
    """Phase B: Clinic Admin A querying audit logs of clinic_b is rejected with 403."""
    user_a = mock_admin_user("admin_a", "clinic_admin", "clinic_a")
    app.dependency_overrides[verify_credentials] = lambda: user_a

    try:
        response = client.get(
            "/admin/audit-logs?clinic_id=clinic_b",
            headers={"Authorization": "Basic YWRtaW5fYTpwYXNzd29yZDEyMw=="},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_adversarial_patients_cross_tenant_rejected(client):
    """Phase B: Clinic Admin A searching patients in clinic_b is rejected with 403."""
    user_a = mock_admin_user("admin_a", "clinic_admin", "clinic_a")
    app.dependency_overrides[verify_credentials] = lambda: user_a

    try:
        response = client.get(
            "/admin/patients?clinic_id=clinic_b",
            headers={"Authorization": "Basic YWRtaW5fYTpwYXNzd29yZDEyMw=="},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_adversarial_payment_settings_cross_tenant_rejected(client):
    """Phase B: Clinic Admin A accessing payment settings of clinic_b is rejected with 403."""
    user_a = mock_admin_user("admin_a", "clinic_admin", "clinic_a")
    app.dependency_overrides[verify_credentials] = lambda: user_a

    try:
        response = client.get(
            "/admin/settings/payment?clinic_id=clinic_b",
            headers={"Authorization": "Basic YWRtaW5fYTpwYXNzd29yZDEyMw=="},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_super_admin_cross_tenant_allowed(client):
    """Phase B: Super Admin is authorized to access multi-clinic scopes."""
    super_user = mock_admin_user("super_admin", "super_admin", None)
    app.dependency_overrides[verify_credentials] = lambda: super_user

    mock_db = MagicMock()
    mock_db.select.return_value.eq.return_value.execute.return_value.data = []

    try:
        with patch("app.routers.admin.supabase.table", return_value=mock_db):
            response = client.get(
                "/admin/bookings?clinic_id=clinic_b",
                headers={"Authorization": "Basic c3VwZXJfYWRtaW46cGFzc3dvcmQxMjM="},
            )
            assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()
