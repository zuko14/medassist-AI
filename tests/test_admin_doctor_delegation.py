"""Tests for delegated doctor management and doctor-branch assignments:
- DOCTORS_CREATE, DOCTORS_UPDATE, DOCTORS_DELETE
- Branch scope isolation (enforce_branch_scope)
- DOCTOR_BRANCH_ASSIGN
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException, Request

from app.routers.admin import (
    AdminUser,
    DoctorCreate,
    DoctorUpdate,
    DoctorBranchAssign,
    create_doctor,
    update_doctor,
    delete_doctor,
    get_branch_doctors,
    assign_doctor_to_branch,
    remove_doctor_from_branch,
    update_doctor_branch_session,
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
async def test_create_doctor_with_delegated_permission():
    doc = DoctorCreate(
        name="Dr. Smith",
        specialization="Cardiology",
        department="Cardiology",
        morning_start="09:00",
        morning_end="12:00",
    )
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1", "name": "Dr. Smith"}]
    )
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[]
    )

    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        res = await create_doctor(
            doctor=doc,
            request=_mock_request(),
            clinic_id="clinic-1",
            user=_staff_user(["DOCTORS_CREATE"]),
        )

    assert res["id"] == "doc-1"
    assert res["name"] == "Dr. Smith"


@pytest.mark.asyncio
async def test_create_doctor_branch_scoped_enforces_own_branch():
    doc = DoctorCreate(
        name="Dr. Smith",
        specialization="Cardiology",
        department="Cardiology",
        morning_start="09:00",
        morning_end="12:00",
        branch_id="branch-2",  # Different from user's branch-1
    )

    with pytest.raises(HTTPException) as exc:
        await create_doctor(
            doctor=doc,
            request=_mock_request(),
            clinic_id="clinic-1",
            user=_staff_user(["DOCTORS_CREATE"], branch_id="branch-1"),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_doctor_branch_scoped_checks_doctor_branch_assignment():
    mock_sb = MagicMock()
    # Doctor is assigned to branch-2
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"branch_id": "branch-2"}]
    )

    doc_update = DoctorUpdate(consultation_fee=500)

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await update_doctor(
                doctor_id="doc-1",
                doctor=doc_update,
                request=_mock_request(),
                clinic_id="clinic-1",
                user=_staff_user(["DOCTORS_UPDATE"], branch_id="branch-1"),
            )
    assert exc.value.status_code == 403
    assert "not assigned to your branch" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_doctor_branch_scoped_checks_doctor_branch_assignment():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"branch_id": "branch-2"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await delete_doctor(
                doctor_id="doc-1",
                request=_mock_request(),
                clinic_id="clinic-1",
                user=_staff_user(["DOCTORS_DELETE"], branch_id="branch-1"),
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_branch_doctors_enforces_branch_scope():
    with pytest.raises(HTTPException) as exc:
        await get_branch_doctors(
            branch_id="branch-2",
            user=_staff_user(["DOCTOR_BRANCH_ASSIGN"], branch_id="branch-1"),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_assign_doctor_to_branch_success():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "branch-1"}]
    )
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"doctor_id": "doc-1", "branch_id": "branch-1", "session": "morning"}]
    )

    body = DoctorBranchAssign(doctor_id="doc-1", session="morning")
    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        res = await assign_doctor_to_branch(
            branch_id="branch-1",
            body=body,
            request=_mock_request(),
            user=_staff_user(["DOCTOR_BRANCH_ASSIGN"], branch_id="branch-1"),
        )

    assert res["doctor_id"] == "doc-1"
    assert res["branch_id"] == "branch-1"
