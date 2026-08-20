"""Tests for GET /admin/me endpoint returning identity, plan, features,
delegated permissions, branch scope, and staff role."""

import pytest
from unittest.mock import AsyncMock, patch

from app.routers.admin import AdminUser, get_current_admin


@pytest.mark.asyncio
async def test_get_current_admin_for_super_admin():
    user = AdminUser("super", role="super_admin", clinic_id=None, user_id="super_admin_env")
    result = await get_current_admin(user=user)
    assert result["username"] == "super"
    assert result["role"] == "super_admin"
    assert result["clinic_id"] is None
    assert result["permissions"] == []
    assert result["branch_id"] is None
    assert result["staff_role"] is None
    assert result["plan"] is None
    assert result["features"] is None


@pytest.mark.asyncio
async def test_get_current_admin_for_clinic_admin():
    user = AdminUser("admin1", role="clinic_admin", clinic_id="clinic-1", user_id="u1")
    with patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock) as mock_clinic:
        mock_clinic.return_value = {"id": "clinic-1", "plan": "enterprise"}
        result = await get_current_admin(user=user)

    assert result["username"] == "admin1"
    assert result["role"] == "clinic_admin"
    assert result["clinic_id"] == "clinic-1"
    assert result["plan"] == "enterprise"
    assert result["permissions"] == []
    assert result["branch_id"] is None


@pytest.mark.asyncio
async def test_get_current_admin_for_delegated_staff():
    user = AdminUser(
        "staff1",
        role="staff",
        clinic_id="clinic-1",
        user_id="s1",
        permissions=["DOCTORS_UPDATE", "HOLIDAYS_CREATE"],
        branch_id="branch-1",
        staff_role="DOCTOR_SCHEDULE_MANAGER",
    )
    with patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock) as mock_clinic:
        mock_clinic.return_value = {"id": "clinic-1", "plan": "pro"}
        result = await get_current_admin(user=user)

    assert result["username"] == "staff1"
    assert result["role"] == "staff"
    assert result["clinic_id"] == "clinic-1"
    assert result["permissions"] == ["DOCTORS_UPDATE", "HOLIDAYS_CREATE"]
    assert result["branch_id"] == "branch-1"
    assert result["staff_role"] == "DOCTOR_SCHEDULE_MANAGER"
