"""Delegated staff permission model — extends the existing
super_admin/clinic_admin/staff tier in app.routers.admin with granular,
server-enforced grants for staff accounts.
"""

from typing import TYPE_CHECKING, Optional
from fastapi import Depends, HTTPException

if TYPE_CHECKING:
    from app.routers.admin import AdminUser


PERMISSIONS = frozenset({
    "DOCTORS_CREATE",
    "DOCTORS_UPDATE",
    "DOCTORS_DELETE",
    "DOCTOR_BRANCH_ASSIGN",
    "DOCTOR_LEAVES_CREATE",
    "DOCTOR_LEAVES_DELETE",
    "HOLIDAYS_CREATE",
    "HOLIDAYS_DELETE",
    "STAFF_VIEW",
    "STAFF_CREATE",
    "STAFF_UPDATE",
    "REPORTS_VIEW",
    "REPORTS_RESOLVE",
    "CONNECTOR_MANAGE",
    "LAB_TESTS_MANAGE",
})

STAFF_ROLES = frozenset({
    "STAFF",
    "RECEPTIONIST",
    "FRONT_DESK",
    "APPOINTMENT_MANAGER",
    "LAB_OPERATOR",
    "PHARMACY_OPERATOR",
    "DOCTOR_SCHEDULE_MANAGER",
    "BRANCH_MANAGER",
    "DIAGNOSTIC_OPERATOR",
    "CUSTOM_ROLE",
})

_DOCTOR_SCHEDULE_GRANTS = [
    "DOCTORS_UPDATE",
    "DOCTOR_BRANCH_ASSIGN",
    "DOCTOR_LEAVES_CREATE",
    "DOCTOR_LEAVES_DELETE",
    "HOLIDAYS_CREATE",
    "HOLIDAYS_DELETE",
]

_DIAGNOSTIC_OPERATOR_GRANTS = [
    "REPORTS_VIEW",
    "REPORTS_RESOLVE",
    "CONNECTOR_MANAGE",
    "LAB_TESTS_MANAGE",
]

ROLE_PRESETS: dict[str, list[str]] = {
    "STAFF": [],
    "RECEPTIONIST": [],
    "FRONT_DESK": [],
    "APPOINTMENT_MANAGER": [],
    "LAB_OPERATOR": ["REPORTS_VIEW", "REPORTS_RESOLVE", "LAB_TESTS_MANAGE"],
    "PHARMACY_OPERATOR": [],
    "DOCTOR_SCHEDULE_MANAGER": list(_DOCTOR_SCHEDULE_GRANTS),
    "BRANCH_MANAGER": _DOCTOR_SCHEDULE_GRANTS + ["DOCTORS_CREATE", "DOCTORS_DELETE", "STAFF_VIEW", "REPORTS_VIEW"],
    "DIAGNOSTIC_OPERATOR": list(_DIAGNOSTIC_OPERATOR_GRANTS),
    "CUSTOM_ROLE": [],
}


def validate_staff_role(staff_role: str) -> str:
    """Raise ValueError if staff_role isn't a recognized value."""
    if staff_role not in STAFF_ROLES:
        raise ValueError(f"Unknown staff_role: {staff_role!r}")
    return staff_role


def resolve_permissions(staff_role: str, extra_permissions: list[str]) -> list[str]:
    """Merge a role's preset grants with explicit extra grants.

    Raises ValueError if staff_role or any extra permission isn't recognized.
    """
    validate_staff_role(staff_role)
    for p in extra_permissions:
        if p not in PERMISSIONS:
            raise ValueError(f"Unknown permission: {p!r}")
    merged = list(dict.fromkeys(ROLE_PRESETS.get(staff_role, []) + list(extra_permissions)))
    return merged


def cap_permissions_to_authority(
    requested: list[str], granter_permissions: list[str], granter_role: str
) -> list[str]:
    """A granter can never hand out a permission they don't hold themselves.

    clinic_admin/super_admin hold full tenant authority — any valid permission
    passes through. A staff granter (has STAFF_CREATE/STAFF_UPDATE) is capped
    to the intersection of what they requested and what they themselves hold.
    """
    if granter_role in ("clinic_admin", "super_admin"):
        return [p for p in requested if p in PERMISSIONS]
    granter_set = set(granter_permissions or [])
    return [p for p in requested if p in granter_set]


def _verify_credentials_dep():
    """Lazy import to avoid circular dependency (app.routers.admin imports this module)."""
    from app.routers.admin import verify_credentials

    return verify_credentials


def require_permission(perm: str):
    """FastAPI dependency factory.
    
    NOTE: Depends on verify_credentials, NOT require_admin — require_admin
    unconditionally 403s every role="staff" account, which would make
    delegated permissions unreachable.
    """
    async def _dep(user: "AdminUser" = Depends(_verify_credentials_dep())) -> "AdminUser":
        if user.role in ("super_admin", "clinic_admin"):
            return user
        if perm not in (user.permissions or []):
            raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
        return user

    return _dep


def enforce_branch_scope(user: "AdminUser", resource_branch_id: Optional[str]) -> None:
    """A staff account scoped to one branch (user.branch_id is set) may only
    act on resources belonging to that branch. Tenant-wide staff
    (branch_id is None) and clinic_admin/super_admin are unrestricted.
    """
    if user.role in ("super_admin", "clinic_admin"):
        return
    if not getattr(user, "branch_id", None):
        return
    if resource_branch_id and str(resource_branch_id) != str(user.branch_id):
        raise HTTPException(
            status_code=403,
            detail="This action is outside your assigned branch.",
        )
