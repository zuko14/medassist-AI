"""Editing a staff account's authority ends that user's sessions (bug register D-09).

resolve_admin_session() builds the user from the admin_sessions row written at
sign-in, so role, permissions, branch and doctor link are a snapshot. PUT
/admin/staff/{id} changed clinic_admins but left those sessions alive: a removed
permission kept working until the user signed in again (up to 12 hours), and
is_active=false through this route did not sign the user out at all.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers.admin import AdminUser, StaffUpdate, update_staff


def _target(**over):
    row = {"id": "s1", "clinic_id": "clinic-1", "role": "staff", "staff_role": "RECEPTIONIST",
           "permissions": ["OPD_FRONT_DESK"], "branch_id": None, "doctor_id": None,
           "is_active": True, "username": "frontdesk1", "full_name": "Asha", "phone": None}
    row.update(over)
    return row


async def _edit(body, target):
    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[target])
    db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[target])
    admin = AdminUser("owner", role="clinic_admin", clinic_id="clinic-1", user_id="u-1")
    with patch("app.routers.admin.supabase", db), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock) as audit, \
         patch("app.routers.admin.revoke_sessions_for_user", new_callable=AsyncMock) as revoke:
        await update_staff(staff_id="s1", body=body, request=None, user=admin)
    return revoke, audit


@pytest.mark.asyncio
async def test_role_change_signs_the_user_out():
    revoke, audit = await _edit(StaffUpdate(staff_role="DOCTOR_SCHEDULE_MANAGER"), _target())
    revoke.assert_awaited_once_with("frontdesk1")
    assert audit.await_args.kwargs["details"]["sessions_revoked"] is True


@pytest.mark.asyncio
async def test_removing_a_permission_signs_the_user_out():
    revoke, _ = await _edit(StaffUpdate(extra_permissions=[]),
                            _target(staff_role="STAFF", permissions=["LEADS_MANAGE"]))
    revoke.assert_awaited_once_with("frontdesk1")


@pytest.mark.asyncio
async def test_deactivating_through_edit_signs_the_user_out():
    revoke, _ = await _edit(StaffUpdate(is_active=False), _target())
    revoke.assert_awaited_once_with("frontdesk1")


@pytest.mark.asyncio
async def test_resaving_the_same_role_keeps_the_session():
    revoke, audit = await _edit(StaffUpdate(staff_role="RECEPTIONIST"), _target())
    revoke.assert_not_called()
    assert audit.await_args.kwargs["details"]["sessions_revoked"] is False


@pytest.mark.asyncio
async def test_contact_details_change_keeps_the_session():
    revoke, _ = await _edit(StaffUpdate(full_name="Asha Rani"), _target())
    revoke.assert_not_called()
