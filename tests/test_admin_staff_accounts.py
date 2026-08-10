# tests/test_admin_staff_accounts.py
"""Tests for clinic-admin self-service staff account management
(GET/POST /admin/staff, PUT /admin/staff/{id}/toggle) — lets a clinic_admin
create front-desk login credentials for their own clinic without going
through the platform owner, while keeping tenant isolation intact."""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException, Request

from app.routers.admin import AdminUser, StaffCreate, create_staff, list_staff, toggle_staff


def _mock_request() -> Request:
    req = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _clinic_admin(clinic_id="clinic-1") -> AdminUser:
    return AdminUser("drpatel", role="clinic_admin", clinic_id=clinic_id, user_id="user-1")


@pytest.mark.asyncio
async def test_create_staff_rejects_duplicate_username():
    body = StaffCreate(username="frontdesk1", password="password1")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "existing"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await create_staff(body=body, request=_mock_request(), user=_clinic_admin())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_staff_inserts_hashed_password_scoped_to_own_clinic():
    body = StaffCreate(username="frontdesk1", password="password1")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "new-1", "username": "frontdesk1", "role": "staff"}]
    )

    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        result = await create_staff(body=body, request=_mock_request(), user=_clinic_admin("clinic-1"))

    assert result["success"] is True
    insert_call = mock_sb.table.return_value.insert.call_args[0][0]
    assert insert_call["username"] == "frontdesk1"
    assert insert_call["role"] == "staff"
    assert insert_call["clinic_id"] == "clinic-1"
    assert insert_call["password_hash"] != "password1"


@pytest.mark.asyncio
async def test_list_staff_scoped_to_own_clinic():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
        data=[{"id": "s1", "username": "frontdesk1", "role": "staff", "is_active": True, "created_at": "2026-01-01"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        result = await list_staff(clinic_id="default", user=_clinic_admin("clinic-1"))

    assert result["staff"][0]["username"] == "frontdesk1"


@pytest.mark.asyncio
async def test_toggle_staff_flips_is_active():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "s1", "clinic_id": "clinic-1", "is_active": True, "role": "staff"}]
    )

    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        result = await toggle_staff(staff_id="s1", request=_mock_request(), user=_clinic_admin("clinic-1"))

    assert result == {"success": True, "is_active": False}


@pytest.mark.asyncio
async def test_toggle_staff_rejects_cross_clinic_access():
    """A clinic_admin from clinic-1 must not be able to toggle a staff account
    belonging to clinic-2 — regression test for the removed `or "default"`
    fallback that would have bypassed tenant isolation."""
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "s2", "clinic_id": "clinic-2", "is_active": True, "role": "staff"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await toggle_staff(staff_id="s2", request=_mock_request(), user=_clinic_admin("clinic-1"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_toggle_staff_rejects_non_staff_target():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "a1", "clinic_id": "clinic-1", "is_active": True, "role": "clinic_admin"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await toggle_staff(staff_id="a1", request=_mock_request(), user=_clinic_admin("clinic-1"))
    assert exc.value.status_code == 404
