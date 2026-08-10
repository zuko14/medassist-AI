# tests/test_platform_clinic_admins.py
"""Tests for owner-mediated clinic_admins account management
(GET/POST /platform/clinic-admins, PUT /platform/clinic-admins/{id}/toggle)
and the rate limit on POST /platform/reset-admin-password."""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException, Request

from app.routers.admin import AdminUser
from app.routers.platform import (
    ClinicAdminCreate,
    ResetAdminPasswordRequest,
    create_clinic_admin,
    list_clinic_admins,
    reset_clinic_admin_password,
    toggle_clinic_admin,
)


def _mock_request() -> Request:
    req = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _owner() -> AdminUser:
    return AdminUser("owner", role="platform_owner", clinic_id=None, user_id="platform_owner_env")


@pytest.mark.asyncio
async def test_list_clinic_admins_excludes_password_hash():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.order.return_value.execute.return_value = MagicMock(
        data=[{"id": "a1", "clinic_id": "c1", "username": "drpatel", "role": "clinic_admin", "is_active": True, "created_at": "2026-01-01"}]
    )

    with patch("app.routers.platform.supabase", mock_sb), patch("app.routers.platform.log_admin_action"):
        result = await list_clinic_admins(request=_mock_request(), owner=_owner())

    assert result["admins"][0]["username"] == "drpatel"
    assert "password_hash" not in result["admins"][0]


@pytest.mark.asyncio
async def test_create_clinic_admin_rejects_invalid_role():
    body = ClinicAdminCreate(clinic_id=None, username="newadmin", password="password1", role="superuser")

    with pytest.raises(HTTPException) as exc:
        await create_clinic_admin(body=body, request=_mock_request(), owner=_owner())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_create_clinic_admin_rejects_super_admin_role():
    """super_admin accounts are platform-wide and env-provisioned only —
    this self-service endpoint must not be able to mint one."""
    body = ClinicAdminCreate(clinic_id=None, username="newsuper", password="password1", role="super_admin")

    with pytest.raises(HTTPException) as exc:
        await create_clinic_admin(body=body, request=_mock_request(), owner=_owner())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_create_clinic_admin_rejects_duplicate_username():
    body = ClinicAdminCreate(clinic_id=None, username="drpatel", password="password1", role="clinic_admin")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "existing"}]
    )

    with patch("app.routers.platform.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await create_clinic_admin(body=body, request=_mock_request(), owner=_owner())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_clinic_admin_inserts_hashed_password():
    body = ClinicAdminCreate(clinic_id=None, username="newadmin", password="password1", role="clinic_admin")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "new-1", "username": "newadmin", "role": "clinic_admin"}]
    )

    with patch("app.routers.platform.supabase", mock_sb), patch("app.routers.platform.log_admin_action"):
        result = await create_clinic_admin(body=body, request=_mock_request(), owner=_owner())

    assert result["success"] is True
    insert_call = mock_sb.table.return_value.insert.call_args[0][0]
    assert insert_call["username"] == "newadmin"
    assert insert_call["password_hash"] != "password1"


@pytest.mark.asyncio
async def test_toggle_clinic_admin_flips_is_active():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "a1", "is_active": True}]
    )

    with patch("app.routers.platform.supabase", mock_sb), patch("app.routers.platform.log_admin_action"):
        result = await toggle_clinic_admin(admin_id="a1", request=_mock_request(), owner=_owner())

    assert result == {"success": True, "is_active": False}


@pytest.mark.asyncio
async def test_reset_admin_password_rate_limited():
    body = ResetAdminPasswordRequest(username="drpatel", new_password="newpassword1")

    with patch("app.routers.platform.login_rate_limiter") as mock_limiter:
        mock_limiter.check_and_record.return_value = True
        with pytest.raises(HTTPException) as exc:
            await reset_clinic_admin_password(body=body, request=_mock_request(), owner=_owner())
    assert exc.value.status_code == 429
