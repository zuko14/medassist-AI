# tests/test_admin_password.py
"""Tests for self-service password change (PUT /admin/change-password) and the
owner-mediated forgot-password path (PUT /platform/reset-admin-password)."""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException, Request

from app.routers.admin import AdminUser, ChangePasswordRequest, change_password, hash_password
from app.routers.platform import ResetAdminPasswordRequest, reset_clinic_admin_password


def _mock_request() -> Request:
    req = MagicMock()
    req.client.host = "127.0.0.1"
    return req


@pytest.mark.asyncio
async def test_change_password_rejects_env_super_admin():
    admin = AdminUser("admin", role="super_admin", clinic_id=None, user_id="super_admin_env")
    body = ChangePasswordRequest(current_password="old", new_password="newpassword1")

    with pytest.raises(HTTPException) as exc:
        await change_password(body=body, request=_mock_request(), user=admin)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_change_password_rejects_wrong_current_password():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    body = ChangePasswordRequest(current_password="wrong", new_password="newpassword1")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "user-1", "password_hash": hash_password("correct")}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await change_password(body=body, request=_mock_request(), user=admin)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_change_password_updates_hash_on_success():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    body = ChangePasswordRequest(current_password="correct", new_password="newpassword1")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "user-1", "password_hash": hash_password("correct")}]
    )

    with patch("app.routers.admin.supabase", mock_sb), patch(
        "app.routers.admin.log_admin_action"
    ):
        result = await change_password(body=body, request=_mock_request(), user=admin)

    assert result == {"success": True, "message": "Password updated successfully"}
    mock_sb.table.return_value.update.assert_called_once()


@pytest.mark.asyncio
async def test_reset_admin_password_requires_existing_username():
    owner = AdminUser("owner", role="platform_owner", clinic_id=None, user_id="platform_owner_env")
    body = ResetAdminPasswordRequest(username="ghost", new_password="newpassword1")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

    with patch("app.routers.platform.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await reset_clinic_admin_password(body=body, request=_mock_request(), owner=owner)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_reset_admin_password_updates_hash_on_success():
    owner = AdminUser("owner", role="platform_owner", clinic_id=None, user_id="platform_owner_env")
    body = ResetAdminPasswordRequest(username="drpatel", new_password="newpassword1")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "user-1"}]
    )

    with patch("app.routers.platform.supabase", mock_sb), patch(
        "app.routers.platform.log_admin_action"
    ):
        result = await reset_clinic_admin_password(body=body, request=_mock_request(), owner=owner)

    assert result["success"] is True
    mock_sb.table.return_value.update.assert_called_once()
