"""Unit tests for staff permissions, role presets, and authority capping."""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from app.services.permissions import (
    PERMISSIONS,
    ROLE_PRESETS,
    STAFF_ROLES,
    cap_permissions_to_authority,
    enforce_branch_scope,
    require_permission,
    resolve_permissions,
    validate_staff_role,
)
from app.routers.admin import AdminUser


def test_permissions_constants_are_nonempty_and_unique():
    assert len(PERMISSIONS) > 0
    assert len(PERMISSIONS) == len(set(PERMISSIONS))
    assert "DOCTORS_CREATE" in PERMISSIONS
    assert "DOCTORS_UPDATE" in PERMISSIONS
    assert "DOCTORS_DELETE" in PERMISSIONS
    assert "DOCTOR_BRANCH_ASSIGN" in PERMISSIONS
    assert "DOCTOR_LEAVES_CREATE" in PERMISSIONS
    assert "DOCTOR_LEAVES_DELETE" in PERMISSIONS
    assert "HOLIDAYS_CREATE" in PERMISSIONS
    assert "HOLIDAYS_DELETE" in PERMISSIONS
    assert "STAFF_VIEW" in PERMISSIONS
    assert "STAFF_CREATE" in PERMISSIONS
    assert "STAFF_UPDATE" in PERMISSIONS


def test_role_presets_only_reference_known_permissions():
    for role, perms in ROLE_PRESETS.items():
        assert role in STAFF_ROLES
        for p in perms:
            assert p in PERMISSIONS


def test_resolve_permissions_merges_preset_and_extras():
    result = resolve_permissions("DOCTOR_SCHEDULE_MANAGER", ["HOLIDAYS_CREATE"])
    assert "DOCTORS_UPDATE" in result
    assert "HOLIDAYS_CREATE" in result
    assert len(result) == len(set(result))  # deduplicated


def test_resolve_permissions_rejects_unknown_extra_permission():
    with pytest.raises(ValueError, match="Unknown permission"):
        resolve_permissions("STAFF", ["INVALID_PERMISSION_NAME"])


def test_resolve_permissions_default_staff_role_has_no_grants():
    assert resolve_permissions("STAFF", []) == []


def test_validate_staff_role_rejects_unknown_role():
    with pytest.raises(ValueError, match="Unknown staff_role"):
        validate_staff_role("SUPER_ADMIN")


def test_validate_staff_role_accepts_known_role():
    assert validate_staff_role("RECEPTIONIST") == "RECEPTIONIST"
    assert validate_staff_role("DOCTOR_SCHEDULE_MANAGER") == "DOCTOR_SCHEDULE_MANAGER"
    assert validate_staff_role("BRANCH_MANAGER") == "BRANCH_MANAGER"


def test_cap_permissions_to_authority_for_clinic_admin_allows_anything_valid():
    capped = cap_permissions_to_authority(
        requested=["DOCTORS_CREATE", "HOLIDAYS_CREATE"],
        granter_permissions=[],
        granter_role="clinic_admin",
    )
    assert set(capped) == {"DOCTORS_CREATE", "HOLIDAYS_CREATE"}


def test_cap_permissions_to_authority_for_super_admin_allows_anything_valid():
    capped = cap_permissions_to_authority(
        requested=["DOCTORS_CREATE", "HOLIDAYS_CREATE"],
        granter_permissions=[],
        granter_role="super_admin",
    )
    assert set(capped) == {"DOCTORS_CREATE", "HOLIDAYS_CREATE"}


def test_cap_permissions_to_authority_for_staff_strips_ungranted_permissions():
    capped = cap_permissions_to_authority(
        requested=["DOCTORS_CREATE", "HOLIDAYS_CREATE"],
        granter_permissions=["DOCTORS_CREATE"],
        granter_role="staff",
    )
    assert capped == ["DOCTORS_CREATE"]


def test_admin_user_defaults_permissions_and_branch_to_empty():
    user = AdminUser("frontdesk", role="staff", clinic_id="clinic-1")
    assert user.permissions == []
    assert user.branch_id is None


def test_admin_user_carries_explicit_permissions_and_branch():
    user = AdminUser(
        "frontdesk",
        role="staff",
        clinic_id="clinic-1",
        permissions=["DOCTORS_UPDATE"],
        branch_id="branch-1",
    )
    assert user.permissions == ["DOCTORS_UPDATE"]
    assert user.branch_id == "branch-1"


def _staff(permissions=None, branch_id=None):
    return AdminUser(
        "s1",
        role="staff",
        clinic_id="clinic-1",
        permissions=permissions or [],
        branch_id=branch_id,
    )


def _clinic_admin():
    return AdminUser("a1", role="clinic_admin", clinic_id="clinic-1")


@pytest.mark.asyncio
async def test_require_permission_allows_clinic_admin_regardless_of_grants():
    dep = require_permission("DOCTORS_CREATE")
    result = await dep(user=_clinic_admin())
    assert result.role == "clinic_admin"


@pytest.mark.asyncio
async def test_require_permission_allows_staff_with_grant():
    dep = require_permission("DOCTORS_CREATE")
    result = await dep(user=_staff(permissions=["DOCTORS_CREATE"]))
    assert result.role == "staff"


@pytest.mark.asyncio
async def test_require_permission_rejects_staff_without_grant():
    dep = require_permission("DOCTORS_CREATE")
    with pytest.raises(HTTPException) as exc:
        await dep(user=_staff(permissions=["HOLIDAYS_CREATE"]))
    assert exc.value.status_code == 403
    assert "DOCTORS_CREATE" in exc.value.detail


def test_enforce_branch_scope_passes_for_tenant_wide_staff():
    enforce_branch_scope(_staff(branch_id=None), resource_branch_id="branch-1")


def test_enforce_branch_scope_passes_when_branches_match():
    enforce_branch_scope(_staff(branch_id="branch-1"), resource_branch_id="branch-1")


def test_enforce_branch_scope_rejects_mismatched_branch():
    with pytest.raises(HTTPException) as exc:
        enforce_branch_scope(_staff(branch_id="branch-1"), resource_branch_id="branch-2")
    assert exc.value.status_code == 403
    assert "outside your assigned branch" in exc.value.detail


def test_enforce_branch_scope_never_restricts_clinic_admin():
    enforce_branch_scope(_clinic_admin(), resource_branch_id="branch-2")
