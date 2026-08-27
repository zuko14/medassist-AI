"""Comprehensive Tenant Isolation Matrix Tests (T6.1 / KRIYA-001 audit).

Asserts negative and positive authorization outcomes across 5 distinct security principals:
1. Unauthenticated client (no credentials)
2. Super Admin (global scope)
3. Clinic Admin Alpha (clinic_alpha scope)
4. Clinic Admin Beta (clinic_beta scope)
5. Limited Staff (role=staff with constrained permissions)

Verifies:
- Negative authorization (cross-tenant access -> 403 Forbidden)
- Unauthenticated rejection (-> 401 Unauthorized)
- Zero cross-tenant data leakage in response payloads
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app
from app.routers.admin import AdminUser, verify_credentials


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    yield TestClient(app)
    app.dependency_overrides.clear()


def mock_admin_user(role="clinic_admin", clinic_id="clinic_alpha", permissions=None, staff_role=None):
    return AdminUser(
        username=f"user_{clinic_id or 'super'}",
        role=role,
        clinic_id=clinic_id,
        user_id="mock-user-uuid",
        permissions=permissions or ["APPOINTMENTS_VIEW", "APPOINTMENTS_MANAGE", "DOCTORS_VIEW", "DOCTORS_MANAGE"],
        staff_role=staff_role,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. UNAUTHENTICATED PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════

def test_unauthenticated_client_rejected(client):
    """Unauthenticated requests must be rejected with 401."""
    protected_routes = [
        "/admin/doctors",
        "/admin/staff",
        "/admin/bookings",
        "/admin/stats",
        "/admin/lab-tests",
        "/admin/lab-reports",
        "/admin/branches",
        "/admin/holidays",
        "/admin/leaves",
        "/admin/connectors",
    ]
    for route in protected_routes:
        resp = client.get(route)
        assert resp.status_code == 401, f"Route {route} failed to reject unauthenticated client"


# ═══════════════════════════════════════════════════════════════════════════
# 2. CROSS-TENANT REJECTION (Clinic Admin Alpha vs Clinic Beta)
# ═══════════════════════════════════════════════════════════════════════════

def test_clinic_admin_cross_tenant_access_forbidden(client):
    """Clinic Alpha admin requesting Clinic Beta data must receive 403 Forbidden."""
    alpha_admin = mock_admin_user(role="clinic_admin", clinic_id="clinic_alpha")
    app.dependency_overrides[verify_credentials] = lambda: alpha_admin

    target_routes = [
        "/admin/doctors?clinic_id=clinic_beta",
        "/admin/staff?clinic_id=clinic_beta",
        "/admin/branches?clinic_id=clinic_beta",
        "/admin/lab-tests?clinic_id=clinic_beta",
        "/admin/holidays?clinic_id=clinic_beta",
        "/admin/leaves?clinic_id=clinic_beta",
        "/admin/connectors?clinic_id=clinic_beta",
    ]

    for route in target_routes:
        resp = client.get(route)
        assert resp.status_code == 403, f"Cross-tenant access to {route} was not forbidden (status={resp.status_code})"


# ═══════════════════════════════════════════════════════════════════════════
# 3. SAME-TENANT SUCCESS & ZERO FOREIGN ROWS
# ═══════════════════════════════════════════════════════════════════════════

def test_same_tenant_data_isolation(client):
    """Clinic Alpha admin receives only Clinic Alpha data with zero Clinic Beta rows."""
    alpha_admin = mock_admin_user(role="clinic_admin", clinic_id="clinic_alpha")
    app.dependency_overrides[verify_credentials] = lambda: alpha_admin

    mock_alpha_doc = {"id": "doc-1", "name": "Dr. Alpha", "clinic_id": "clinic_alpha"}

    mock_supabase = MagicMock()
    mock_table = MagicMock()
    mock_supabase.table.return_value = mock_table

    # Scoped query returns only alpha doc
    mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[mock_alpha_doc]
    )
    mock_table.select.return_value.in_.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[]
    )

    with patch("app.routers.admin.supabase", mock_supabase):
        resp = client.get("/admin/doctors?clinic_id=clinic_alpha")
        assert resp.status_code == 200
        docs = resp.json()
        assert len(docs) == 1
        assert docs[0]["clinic_id"] == "clinic_alpha"
        assert "clinic_beta" not in [d.get("clinic_id") for d in docs]


# ═══════════════════════════════════════════════════════════════════════════
# 4. SUPER ADMIN GLOBAL ACCESS
# ═══════════════════════════════════════════════════════════════════════════

def test_super_admin_global_access(client):
    """Super Admin can access all clinics."""
    super_admin = mock_admin_user(role="super_admin", clinic_id=None)
    app.dependency_overrides[verify_credentials] = lambda: super_admin

    mock_supabase = MagicMock()
    mock_table = MagicMock()
    mock_supabase.table.return_value = mock_table
    mock_table.select.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
    mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
    mock_table.select.return_value.in_.return_value.limit.return_value.execute.return_value = MagicMock(data=[])

    with patch("app.routers.admin.supabase", mock_supabase):
        resp = client.get("/admin/doctors?clinic_id=clinic_beta")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# 5. LIMITED STAFF PERMISSION ENFORCEMENT
# ═══════════════════════════════════════════════════════════════════════════

def test_limited_staff_permissions(client):
    """Staff with only APPOINTMENTS_VIEW cannot access staff creation or doctor management."""
    limited_staff = mock_admin_user(
        role="staff",
        clinic_id="clinic_alpha",
        permissions=["APPOINTMENTS_VIEW"],
    )
    app.dependency_overrides[verify_credentials] = lambda: limited_staff

    # Staff management requires STAFF_VIEW
    resp = client.get("/admin/staff?clinic_id=clinic_alpha")
    assert resp.status_code == 403

    # Connector management requires CONNECTOR_MANAGE
    resp = client.get("/admin/connectors?clinic_id=clinic_alpha")
    assert resp.status_code == 403
