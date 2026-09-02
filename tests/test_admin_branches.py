# tests/test_admin_branches.py
"""Tests for DELETE /admin/branches/{id}: hard-delete when the branch has
no dependent appointments/doctors/connectors/staff, otherwise deactivate
with a message explaining why (regression guard for duplicate branches
that could never actually be removed via the admin panel)."""

import pytest
from unittest.mock import MagicMock, patch

from app.routers.admin import AdminUser, delete_branch


def _mock_supabase(dependents_by_table=None):
    """Build a mock supabase client. dependents_by_table maps table name
    to whether a dependent-check query on it should return a row."""
    dependents_by_table = dependents_by_table or {}
    mock_sb = MagicMock()

    def table_side_effect(name):
        mock_table = MagicMock()
        has_dependent = dependents_by_table.get(name, False)
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "dep-1"}] if has_dependent else []
        )
        # resolve_owned_branch() does select("*").eq("id", ...).execute() with no
        # .limit() — it is the ownership/branch-pin check the write endpoints run
        # before mutating anything.
        mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "branch-1", "clinic_id": "clinic-1"}] if name == "branches" else []
        )
        mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"id": "branch-1"}])
        mock_table.delete.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"id": "branch-1"}])
        return mock_table

    mock_sb.table.side_effect = table_side_effect
    return mock_sb


@pytest.mark.asyncio
async def test_delete_branch_hard_deletes_when_unused():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    mock_sb = _mock_supabase(dependents_by_table={})

    with patch("app.routers.admin.supabase", mock_sb),          patch("app.database.supabase", mock_sb):
        result = await delete_branch(branch_id="branch-1", clinic_id="clinic-1", user=admin)

    assert result["deleted"] is True
    delete_calls = [c for c in mock_sb.table.call_args_list if c.args == ("branches",)]
    assert len(delete_calls) >= 1


@pytest.mark.asyncio
async def test_delete_branch_deactivates_when_appointments_reference_it():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    mock_sb = _mock_supabase(dependents_by_table={"appointments": True})

    with patch("app.routers.admin.supabase", mock_sb),          patch("app.database.supabase", mock_sb):
        result = await delete_branch(branch_id="branch-1", clinic_id="clinic-1", user=admin)

    assert result["deleted"] is False
    assert "appointment" in result["message"].lower()


@pytest.mark.asyncio
async def test_delete_branch_refuses_a_branch_from_another_clinic():
    """The ownership check must run BEFORE any mutation: a branch id alone is
    not authorization now that delegated BRANCHES_MANAGE staff can reach this."""
    from fastapi import HTTPException

    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    mock_sb = _mock_supabase(dependents_by_table={})

    with patch("app.routers.admin.supabase", mock_sb),          patch("app.database.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await delete_branch(branch_id="branch-1", clinic_id="clinic-2", user=admin)

    assert exc.value.status_code == 404
    assert not mock_sb.table.return_value.delete.called
