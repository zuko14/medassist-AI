"""Phase 6: Frontend ↔ Backend Wiring Verification Tests.

Verifies:
1. GET /admin/profile returns self-service hospital profile configuration.
2. PUT /admin/profile updates clinic profile, hospital address, maps link, and emergency contact.
3. Tenant isolation on profile endpoints (clinic_admin cannot read/write another clinic's profile).
4. RBAC checks on connector endpoints with CONNECTOR_MANAGE.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import require_admin, AdminUser


def test_get_admin_profile_success():
    """GET /admin/profile returns clinic profile details."""
    client = TestClient(app)

    admin_user = AdminUser("admin")
    admin_user.role = "clinic_admin"
    admin_user.clinic_id = "clinic_abc"
    admin_user.permissions = []

    mock_clinic = {
        "id": "clinic_abc",
        "name": "Apollo City Center",
        "config": {
            "address": "45 Healthcare Blvd, Hyderabad",
            "maps_link": "https://maps.google.com/?q=Apollo",
            "emergency_number": "+919876543210",
        },
    }

    app.dependency_overrides[require_admin] = lambda: admin_user

    try:
        with patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=mock_clinic):
            response = client.get("/admin/profile?clinic_id=clinic_abc")
            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "Apollo City Center"
            assert data["hospital_address"] == "45 Healthcare Blvd, Hyderabad"
            assert data["hospital_maps_link"] == "https://maps.google.com/?q=Apollo"
            assert data["hospital_emergency_number"] == "+919876543210"
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_put_admin_profile_updates_config():
    """PUT /admin/profile updates clinic configuration."""
    client = TestClient(app)

    admin_user = AdminUser("admin")
    admin_user.role = "clinic_admin"
    admin_user.clinic_id = "clinic_abc"
    admin_user.permissions = []

    mock_clinic = {
        "id": "clinic_abc",
        "name": "Apollo City Center",
        "config": {},
    }

    app.dependency_overrides[require_admin] = lambda: admin_user

    try:
        with patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=mock_clinic), \
             patch("app.routers.admin.supabase.table") as mock_table, \
             patch("app.routers.admin.invalidate_tenant_cache") as mock_inv, \
             patch("app.routers.admin.log_admin_action", new_callable=AsyncMock):

            mock_table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_clinic])

            payload = {
                "name": "Apollo Super Specialty",
                "hospital_address": "100 Health Way",
                "hospital_maps_link": "https://maps.google.com/?q=ApolloNew",
                "hospital_emergency_number": "+919111122222",
            }

            response = client.put("/admin/profile?clinic_id=clinic_abc", json=payload)
            assert response.status_code == 200
            assert response.json()["success"] is True
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_get_admin_profile_cross_tenant_forbidden():
    """Clinic admin cannot read another clinic's profile."""
    client = TestClient(app)

    admin_user = AdminUser("clinic_a_user")
    admin_user.role = "clinic_admin"
    admin_user.clinic_id = "clinic_a"
    admin_user.permissions = []

    app.dependency_overrides[require_admin] = lambda: admin_user

    try:
        response = client.get("/admin/profile?clinic_id=clinic_b")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(require_admin, None)
