"""Unit & Integration tests for Safe Clinic Lifecycle Management & Soft-Deletion Engine."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.tenant import TenantNotFound, get_clinic_by_id, resolve_tenant

client = TestClient(app)


def get_owner_auth_header(username: str = None, password: str = None) -> dict:
    u = username or settings.owner_username or "test_owner"
    p = password or settings.owner_password or "test_owner_password_12345"
    creds = f"{u}:{p}"
    encoded = base64.b64encode(creds.encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {encoded}"}


# ─── 1. Authorization & RBAC ─────────────────────────────────────────────────


def test_delete_clinic_unauthorized_without_header():
    res = client.delete("/platform/clinics/some-clinic-uuid")
    assert res.status_code == 401


def test_delete_clinic_unauthorized_with_wrong_password():
    headers = get_owner_auth_header(password="wrong_password")
    res = client.delete("/platform/clinics/some-clinic-uuid", headers=headers)
    assert res.status_code == 401


# ─── 2. Deletion Impact Preview ──────────────────────────────────────────────


@patch("app.routers.platform.supabase")
def test_deletion_preview_success(mock_supabase):
    mock_clinic = {
        "id": "c-123",
        "name": "Apex Polyclinic",
        "whatsapp_number": "+919876543210",
        "plan": "polyclinic",
        "is_active": True,
        "status": "ACTIVE",
        "created_at": "2026-01-01T00:00:00Z",
    }

    mock_c_table = MagicMock()
    mock_c_table.select.return_value.eq.return_value.execute.return_value.data = [mock_clinic]

    mock_count_3 = MagicMock()
    mock_count_3.count = 3
    mock_count_3.data = []

    mock_count_150 = MagicMock()
    mock_count_150.count = 150
    mock_count_150.data = []

    mock_count_90 = MagicMock()
    mock_count_90.count = 90
    mock_count_90.data = []

    mock_count_2 = MagicMock()
    mock_count_2.count = 2
    mock_count_2.data = []

    def table_router(t):
        mock_obj = MagicMock()
        if t == "clinics":
            return mock_c_table
        if t == "doctors":
            mock_obj.select.return_value.eq.return_value.execute.return_value = mock_count_3
            return mock_obj
        if t == "appointments":
            mock_obj.select.return_value.eq.return_value.execute.return_value = mock_count_150
            return mock_obj
        if t == "patients":
            mock_obj.select.return_value.eq.return_value.execute.return_value = mock_count_90
            return mock_obj
        if t == "clinic_admins":
            mock_obj.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_count_2
            return mock_obj
        return mock_obj

    mock_supabase.table.side_effect = table_router

    headers = get_owner_auth_header()
    res = client.get("/platform/clinics/c-123/deletion-preview", headers=headers)

    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["clinic"]["id"] == "c-123"
    assert data["impact_preview"]["doctor_count"] == 3
    assert data["impact_preview"]["appointment_count"] == 150
    assert data["impact_preview"]["patient_count"] == 90
    assert data["impact_preview"]["active_admin_count"] == 2


@patch("app.routers.platform.supabase")
def test_deletion_preview_not_found(mock_supabase):
    mock_c_table = MagicMock()
    mock_c_table.select.return_value.eq.return_value.execute.return_value.data = []
    mock_supabase.table.return_value = mock_c_table

    headers = get_owner_auth_header()
    res = client.get("/platform/clinics/non-existent-uuid/deletion-preview", headers=headers)
    assert res.status_code == 404


@patch("app.routers.platform.supabase")
def test_deletion_preview_already_deleted(mock_supabase):
    mock_clinic = {
        "id": "c-123",
        "name": "Apex Polyclinic",
        "status": "DELETED",
        "is_active": False,
    }
    mock_c_table = MagicMock()
    mock_c_table.select.return_value.eq.return_value.execute.return_value.data = [mock_clinic]
    mock_supabase.table.return_value = mock_c_table

    headers = get_owner_auth_header()
    res = client.get("/platform/clinics/c-123/deletion-preview", headers=headers)
    assert res.status_code == 400
    assert "already deleted" in res.json()["detail"].lower()


# ─── 3. Soft-Deletion Execution & Cascading State ────────────────────────────


@patch("app.routers.platform.invalidate_branch_cache")
@patch("app.routers.platform.invalidate_tenant_cache")
@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_soft_delete_clinic_success(
    mock_supabase, mock_log_action, mock_inv_tenant, mock_inv_branch
):
    mock_clinic = {
        "id": "c-123",
        "name": "Apex Polyclinic",
        "whatsapp_number": "+919876543210",
        "status": "ACTIVE",
        "is_active": True,
    }

    mock_c_select = MagicMock()
    mock_c_select.select.return_value.eq.return_value.execute.return_value.data = [mock_clinic]
    mock_c_update = MagicMock()
    mock_c_update.update.return_value.eq.return_value.execute.return_value.data = [
        {"id": "c-123", "status": "DELETED"}
    ]

    mock_admins_update = MagicMock()
    mock_admins_update.update.return_value.eq.return_value.execute.return_value.data = []

    mock_conn_update = MagicMock()
    mock_conn_update.update.return_value.eq.return_value.execute.return_value.data = []

    def table_router(t):
        if t == "clinics":
            # Return select for first call, update for second
            m = MagicMock()
            m.select.return_value.eq.return_value.execute.return_value.data = [mock_clinic]
            m.update.return_value.eq.return_value.execute.return_value.data = [{"id": "c-123"}]
            return m
        if t == "clinic_admins":
            return mock_admins_update
        if t == "integration_connectors":
            return mock_conn_update
        return MagicMock()

    mock_supabase.table.side_effect = table_router

    headers = get_owner_auth_header()
    res = client.delete("/platform/clinics/c-123", headers=headers)

    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["clinic_id"] == "c-123"
    assert "Apex Polyclinic" in data["message"]

    # Verify cache invalidations
    mock_inv_tenant.assert_called_once_with("+919876543210")
    mock_inv_branch.assert_called_once_with("c-123")


# ─── 4. Tenant Resolution Rejection for Soft-Deleted Clinics ─────────────────


@pytest.mark.asyncio
async def test_soft_deleted_clinic_tenant_resolution_fails():
    with patch("app.services.tenant.supabase") as mock_supabase:
        mock_clinic = {
            "id": "c-123",
            "name": "Deleted Clinic",
            "whatsapp_number": "+919876543210",
            "is_active": False,
            "status": "DELETED",
            "deleted_at": "2026-08-17T10:00:00Z",
        }
        mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            mock_clinic
        ]

        with patch("app.services.tenant._tenant_cache", {}):
            with pytest.raises(TenantNotFound):
                await resolve_tenant("+919876543210")


@pytest.mark.asyncio
async def test_soft_deleted_clinic_get_by_id_fails():
    with patch("app.services.tenant.supabase") as mock_supabase:
        mock_clinic = {
            "id": "c-123",
            "name": "Deleted Clinic",
            "status": "DELETED",
            "deleted_at": "2026-08-17T10:00:00Z",
        }
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            mock_clinic
        ]

        with pytest.raises(TenantNotFound):
            await get_clinic_by_id("c-123")
