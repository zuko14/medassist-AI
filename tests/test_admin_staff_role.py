# tests/test_admin_staff_role.py
"""Tests for require_admin — the RBAC gate that limits 'staff' accounts to
front-desk operations (appointments, check-in, bookings, patients) and blocks
them from admin-only actions (payments, settings, connectors, branches,
doctor/leave roster management, audit logs)."""

import pytest
from fastapi import HTTPException

from app.routers.admin import AdminUser, require_admin


def _staff() -> AdminUser:
    return AdminUser("frontdesk", role="staff", clinic_id="clinic-1", user_id="user-1")


def _clinic_admin() -> AdminUser:
    return AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-2")


def _super_admin() -> AdminUser:
    return AdminUser("owner", role="super_admin", clinic_id=None, user_id="super_admin_env")


@pytest.mark.asyncio
async def test_require_admin_rejects_staff():
    with pytest.raises(HTTPException) as exc:
        await require_admin(user=_staff())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_allows_clinic_admin():
    result = await require_admin(user=_clinic_admin())
    assert result.role == "clinic_admin"


@pytest.mark.asyncio
async def test_require_admin_allows_super_admin():
    result = await require_admin(user=_super_admin())
    assert result.role == "super_admin"
