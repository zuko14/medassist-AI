"""Tests for delegated holiday and leave management:
- HOLIDAYS_CREATE, HOLIDAYS_DELETE
- DOCTOR_LEAVES_CREATE, DOCTOR_LEAVES_DELETE
- Branch scope isolation for doctor leaves
"""

import pytest
from datetime import date
from unittest.mock import MagicMock, patch
from fastapi import HTTPException, Request

from app.routers.admin import (
    AdminUser,
    LeaveCreate,
    create_holiday,
    delete_holiday,
    get_holidays,
    create_leave,
    delete_leave,
    get_leaves,
)


def _mock_request() -> Request:
    req = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _clinic_admin(clinic_id="clinic-1") -> AdminUser:
    return AdminUser("drpatel", role="clinic_admin", clinic_id=clinic_id, user_id="user-1")


def _staff_user(permissions, branch_id=None, clinic_id="clinic-1") -> AdminUser:
    return AdminUser(
        "staff1",
        role="staff",
        clinic_id=clinic_id,
        user_id="s1",
        permissions=permissions,
        branch_id=branch_id,
    )


@pytest.mark.asyncio
async def test_create_holiday_with_delegated_permission():
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"holiday_date": "2026-10-02", "name": "Gandhi Jayanti", "clinic_id": "clinic-1"}]
    )

    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        res = await create_holiday(
            holiday_date=date(2026, 10, 2),
            name="Gandhi Jayanti",
            request=_mock_request(),
            clinic_id="clinic-1",
            user=_staff_user(["HOLIDAYS_CREATE"]),
        )

    assert res["name"] == "Gandhi Jayanti"
    assert res["holiday_date"] == "2026-10-02"


@pytest.mark.asyncio
async def test_delete_holiday_with_delegated_permission():
    mock_sb = MagicMock()
    mock_sb.table.return_value.delete.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock()

    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        res = await delete_holiday(
            holiday_date="2026-10-02",
            request=_mock_request(),
            clinic_id="clinic-1",
            user=_staff_user(["HOLIDAYS_DELETE"]),
        )

    assert res == {"status": "deleted"}


@pytest.mark.asyncio
async def test_create_leave_with_delegated_permission():
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"doctor_name": "Dr. Smith", "leave_date": "2026-09-15", "clinic_id": "clinic-1"}]
    )

    leave = LeaveCreate(doctor_name="Dr. Smith", leave_date=date(2026, 9, 15), leave_type="full_day")
    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        res = await create_leave(
            leave=leave,
            request=_mock_request(),
            clinic_id="clinic-1",
            user=_staff_user(["DOCTOR_LEAVES_CREATE"]),
        )

    assert res["doctor_name"] == "Dr. Smith"
    assert res["leave_date"] == "2026-09-15"


@pytest.mark.asyncio
async def test_create_leave_branch_scoped_checks_doctor_branch():
    mock_sb = MagicMock()
    # Doctor is in clinic-1 with doctor_id="doc-1"
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1"}]
    )
    # Doctor's branches are branch-2 (not branch-1)
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"branch_id": "branch-2"}]
    )

    leave = LeaveCreate(doctor_name="Dr. Smith", leave_date=date(2026, 9, 15), leave_type="full_day")
    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await create_leave(
                leave=leave,
                request=_mock_request(),
                clinic_id="clinic-1",
                user=_staff_user(["DOCTOR_LEAVES_CREATE"], branch_id="branch-1"),
            )
    assert exc.value.status_code == 403
    assert "not assigned to your branch" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_leave_with_delegated_permission():
    mock_sb = MagicMock()
    mock_sb.table.return_value.delete.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock()

    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        res = await delete_leave(
            leave_id="leave-1",
            request=_mock_request(),
            clinic_id="clinic-1",
            user=_staff_user(["DOCTOR_LEAVES_DELETE"]),
        )

    assert res == {"status": "deleted"}
