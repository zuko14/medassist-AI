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
    # Create/edit/deactivate the branches themselves. Deliberately NOT in any
    # role preset: a clinic_admin has to grant it per account, because a branch
    # is what patients are offered in WhatsApp.
    "BRANCHES_MANAGE",
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
    # Create/edit/delete/publish treatments. Deliberately in no role preset: a
    # clinic_admin grants it per staff account.
    "TREATMENTS_MANAGE",
    # Dental clinics only (migration 089): create treatment plans, book/cancel
    # sittings, message patient/doctor. In no role preset: granted per account.
    "DENTAL_PLANS_MANAGE",
    # Admin "Leads" (migration 092): every WhatsApp contact's number plus staff
    # follow-up messages. In no role preset: granted per account.
    "LEADS_MANAGE",
    # Corporate employee-health insights (migration 095): add companies, upload
    # employee report PDFs, delete them. In no role preset: granted per account.
    "CORPORATE_HEALTH_MANAGE",
    # Home sample collection (migration 097): settings, the day's visits and
    # (re)assigning them to phlebotomists. In no role preset: granted per account.
    "HOME_COLLECTION_MANAGE",
    # AI voice receptionist (migration 098). VIEW = call list, transcripts and
    # analytics (patient data); MANAGE = settings, lexicon, takeover, outbound
    # calls, test console. In no role preset: granted per account.
    "VOICE_VIEW",
    "VOICE_MANAGE",
    # OPD OS (migration 103). Routes still require opd_enabled(clinic).
    "OPD_FRONT_DESK",
    "OPD_CLINICAL",
    "OPD_BILLING",
    "OPD_ADMIN",
    # IPD OS (migration 111). Routes still require ipd_access(clinic).
    # Clinical authoring (progress notes, orders, discharge decision and
    # summary signing) additionally needs a login linked to a doctor.
    "IPD_ADMIN",
    "IPD_ADMISSIONS",
    "IPD_NURSING",
    "IPD_CLINICAL",
    "IPD_BILLING",
    # Investigation & procedure orders (migration 115). READ = the diagnostic
    # worklist and a visit's orders; WRITE = place orders (the treating
    # doctor's own login only) and record results; COLLECT = sample
    # collection; CANCEL = cancel an unpaid order.
    "ORDERS_READ",
    "ORDERS_WRITE",
    "ORDERS_COLLECT",
    "ORDERS_CANCEL",
    # GST credit notes (migration 117): reduce an issued invoice, refunding any
    # excess already paid into the issuer's cashier shift.
    "BILLING_CREDIT_NOTE",
    # In-house LIS (migration 118). ENTRY = enter / correct parameter values on
    # a collected sample; VALIDATE = pathologist sign-off (also needs a login
    # linked to a doctor with a registration number).
    "LAB_ENTRY",
    "LAB_VALIDATE",
    # Clinical documents (migration 119). READ = print discharge summaries,
    # consents and certificates; WRITE = record consents, issue / void
    # certificates, send documents on WhatsApp. Issuing a certificate also
    # needs a login linked to a doctor with a registration number.
    "DOCUMENTS_READ",
    "DOCUMENTS_WRITE",
    # Service catalogue & tariff master (migration 120): add, re-price, change
    # GST / SAC, deactivate. In no role preset; OPD_ADMIN (which edited the
    # legacy catalogue) implies it.
    "SERVICES_MANAGE",
    # Emergency / casualty (migration 121). READ = the casualty board and an
    # MLC's PDF; WRITE = rapid registration, triage, notes, disposition,
    # identity merge; MLC_MANAGE = record / lock a medico-legal case (a login
    # linked to a doctor with a registration number).
    "EMERGENCY_READ",
    "EMERGENCY_WRITE",
    "MLC_MANAGE",
    # Operation theatre (migration 122). Routes still require ipd_access(clinic).
    # SCHEDULE = book, cancel, postpone surgeries; CHECKLIST = the WHO Surgical
    # Safety Checklist, wheel in / out; CLINICAL = anaesthesia record and the
    # surgeon's post-op note (signing / finishing also needs a doctor link).
    "OT_SCHEDULE",
    "OT_CHECKLIST",
    "OT_CLINICAL",
    # Radiology (migration 123). Routes still require opd_enabled or ipd_access.
    # READ = imaging worklist, reports, PDFs; WRITE = draft reports, record
    # PCPNDT Form F, finalize (Form F and finalizing also need a login linked
    # to a doctor with a registration number).
    "RADIOLOGY_READ",
    "RADIOLOGY_WRITE",
    # Insurance / TPA (migration 124). Routes still require ipd_access(clinic).
    # READ = pre-auth pipeline, claims, payers, ageing; MANAGE = payers,
    # policies, pre-auth decisions, filing claims, recording settlements.
    # Writing off a disallowance also needs BILLING_CREDIT_NOTE (a GST credit note).
    "INSURANCE_READ",
    "INSURANCE_MANAGE",
})

#: A login for a lab's CORPORATE client: sees that one company's aggregate
#: dashboard and nothing else. Enforced for every /admin route in
#: app.routers.admin.verify_credentials, not by hiding tabs.
CORPORATE_VIEWER = "CORPORATE_VIEWER"

#: A diagnostic centre's field phlebotomist (migration 097): sees only the home
#: sample collections assigned to them. Confined the same way as
#: CORPORATE_VIEWER, in app.routers.admin.verify_credentials.
PHLEBOTOMIST = "PHLEBOTOMIST"

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
    "DOCTOR",
    "CASHIER",
    "NURSE",
    "PATHOLOGIST",
    "RADIOLOGIST",
    "TPA_DESK",
    CORPORATE_VIEWER,
    PHLEBOTOMIST,
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
    "ORDERS_READ",
    "ORDERS_COLLECT",
    "ORDERS_WRITE",
    "LAB_ENTRY",
]

ROLE_PRESETS: dict[str, list[str]] = {
    "STAFF": [],
    "RECEPTIONIST": ["OPD_FRONT_DESK"],
    "FRONT_DESK": ["OPD_FRONT_DESK"],
    "APPOINTMENT_MANAGER": [],
    "LAB_OPERATOR": ["REPORTS_VIEW", "REPORTS_RESOLVE", "LAB_TESTS_MANAGE", "ORDERS_READ", "ORDERS_COLLECT",
                     "ORDERS_WRITE", "LAB_ENTRY"],
    "PHARMACY_OPERATOR": [],
    "DOCTOR_SCHEDULE_MANAGER": list(_DOCTOR_SCHEDULE_GRANTS),
    "BRANCH_MANAGER": _DOCTOR_SCHEDULE_GRANTS + ["DOCTORS_CREATE", "DOCTORS_DELETE", "STAFF_VIEW", "REPORTS_VIEW"],
    "DIAGNOSTIC_OPERATOR": list(_DIAGNOSTIC_OPERATOR_GRANTS),
    "CUSTOM_ROLE": [],
    "DOCTOR": ["OPD_CLINICAL", "ORDERS_READ", "ORDERS_WRITE", "ORDERS_CANCEL", "LAB_VALIDATE",
               "DOCUMENTS_READ", "DOCUMENTS_WRITE", "EMERGENCY_READ", "EMERGENCY_WRITE", "MLC_MANAGE",
               "OT_SCHEDULE", "OT_CHECKLIST", "OT_CLINICAL", "RADIOLOGY_READ", "RADIOLOGY_WRITE"],
    "CASHIER": ["OPD_BILLING", "ORDERS_READ", "BILLING_CREDIT_NOTE", "EMERGENCY_READ", "INSURANCE_READ"],
    "NURSE": ["IPD_NURSING", "ORDERS_READ", "ORDERS_COLLECT", "DOCUMENTS_READ", "EMERGENCY_READ", "EMERGENCY_WRITE",
              "OT_CHECKLIST", "RADIOLOGY_READ"],
    "PATHOLOGIST": ["ORDERS_READ", "LAB_ENTRY", "LAB_VALIDATE", "REPORTS_VIEW"],
    "RADIOLOGIST": ["ORDERS_READ", "RADIOLOGY_READ", "RADIOLOGY_WRITE"],
    # the spec's IPD_READ does not exist; the insurance routes carry the stay data the desk needs
    "TPA_DESK": ["INSURANCE_READ", "INSURANCE_MANAGE", "ORDERS_READ"],
    CORPORATE_VIEWER: [],
    PHLEBOTOMIST: [],
}

#: Logins created before migration 115 hold only their module grants. Each
#: implies the order grant its holder already needs, so no account has to be
#: re-saved for orders to work: a doctor with OPD_CLINICAL can order, a ward
#: nurse with IPD_NURSING can collect samples.
_ORDER_GRANTS_IMPLIED_BY: dict[str, tuple[str, ...]] = {
    "ORDERS_READ": ("OPD_CLINICAL", "OPD_BILLING", "IPD_CLINICAL", "IPD_NURSING", "IPD_BILLING"),
    "ORDERS_WRITE": ("OPD_CLINICAL", "IPD_CLINICAL"),
    "ORDERS_COLLECT": ("IPD_NURSING",),
    "ORDERS_CANCEL": ("OPD_CLINICAL", "IPD_CLINICAL"),
}

#: A phlebotomist login stores no grants (it is confined in
#: app.routers.admin.verify_credentials); its role alone opens the desk
#: worklist and sample collection, the two routes that allowlist admits.
_PHLEBOTOMIST_ORDER_GRANTS = frozenset({"ORDERS_READ", "ORDERS_COLLECT"})


def holds_order_permission(user: "AdminUser", perm: str) -> bool:
    """True if `user` may perform the order action `perm` (an ORDERS_* grant)."""
    if user.role in ("super_admin", "clinic_admin"):
        return True
    held = set(user.permissions or [])
    if perm in held or held.intersection(_ORDER_GRANTS_IMPLIED_BY.get(perm, ())):
        return True
    return getattr(user, "staff_role", None) == PHLEBOTOMIST and perm in _PHLEBOTOMIST_ORDER_GRANTS


#: Lab and doctor logins saved before migration 118 lack the LAB_* grants;
#: their role implies the one they need, so no account has to be re-saved.
_LAB_GRANTS_IMPLIED_BY_ROLE: dict[str, tuple[str, ...]] = {
    "LAB_ENTRY": ("LAB_OPERATOR", "DIAGNOSTIC_OPERATOR", "PATHOLOGIST"),
    "LAB_VALIDATE": ("PATHOLOGIST", "DOCTOR"),
}


def holds_lab_permission(user: "AdminUser", perm: str) -> bool:
    """True if `user` may perform the LIS action `perm` (LAB_ENTRY / LAB_VALIDATE)."""
    if user.role in ("super_admin", "clinic_admin"):
        return True
    if perm in (user.permissions or []):
        return True
    return getattr(user, "staff_role", None) in _LAB_GRANTS_IMPLIED_BY_ROLE.get(perm, ())


#: Logins saved before migration 119 hold only their module grants: clinical
#: authors may write documents, ward nurses may print them.
_DOCUMENT_GRANTS_IMPLIED_BY: dict[str, tuple[str, ...]] = {
    "DOCUMENTS_READ": ("OPD_CLINICAL", "IPD_CLINICAL", "IPD_NURSING", "DOCUMENTS_WRITE"),
    "DOCUMENTS_WRITE": ("OPD_CLINICAL", "IPD_CLINICAL"),
}


def holds_document_permission(user: "AdminUser", perm: str) -> bool:
    """True if `user` may perform the clinical-documents action `perm`."""
    if user.role in ("super_admin", "clinic_admin"):
        return True
    held = set(user.permissions or [])
    return perm in held or bool(held.intersection(_DOCUMENT_GRANTS_IMPLIED_BY.get(perm, ())))


#: Logins saved before migration 121: clinicians, ward nurses and the front
#: desk (who register arrivals) keep working; billing can see the board.
_EMERGENCY_GRANTS_IMPLIED_BY: dict[str, tuple[str, ...]] = {
    "EMERGENCY_READ": ("OPD_CLINICAL", "IPD_CLINICAL", "IPD_NURSING", "OPD_FRONT_DESK", "IPD_ADMISSIONS",
                       "OPD_BILLING", "IPD_BILLING", "EMERGENCY_WRITE", "MLC_MANAGE"),
    "EMERGENCY_WRITE": ("OPD_CLINICAL", "IPD_CLINICAL", "IPD_NURSING", "OPD_FRONT_DESK", "IPD_ADMISSIONS"),
    "MLC_MANAGE": ("OPD_CLINICAL", "IPD_CLINICAL"),
}


def holds_emergency_permission(user: "AdminUser", perm: str) -> bool:
    """True if `user` may perform the emergency action `perm`."""
    if user.role in ("super_admin", "clinic_admin"):
        return True
    held = set(user.permissions or [])
    return perm in held or bool(held.intersection(_EMERGENCY_GRANTS_IMPLIED_BY.get(perm, ())))


#: Logins saved before migration 122: the admissions desk and doctors book
#: surgeries, doctors write the clinical record, ward nurses run the checklist.
_OT_GRANTS_IMPLIED_BY: dict[str, tuple[str, ...]] = {
    "OT_SCHEDULE": ("IPD_ADMIN", "IPD_ADMISSIONS", "IPD_CLINICAL"),
    "OT_CHECKLIST": ("IPD_NURSING", "IPD_CLINICAL", "OT_CLINICAL"),
    "OT_CLINICAL": ("IPD_CLINICAL",),
}
_OT_GRANTS_IMPLIED_BY_ROLE: dict[str, tuple[str, ...]] = {
    "OT_SCHEDULE": ("DOCTOR",),
    "OT_CHECKLIST": ("DOCTOR", "NURSE"),
    "OT_CLINICAL": ("DOCTOR",),
}
OT_PERMISSIONS = ("OT_SCHEDULE", "OT_CHECKLIST", "OT_CLINICAL")


def holds_ot_permission(user: "AdminUser", perm: str) -> bool:
    """True if `user` may perform the operation-theatre action `perm`; "OT_ANY"
    = any OT grant (reading the schedule)."""
    if user.role in ("super_admin", "clinic_admin"):
        return True
    if perm == "OT_ANY":
        return any(holds_ot_permission(user, p) for p in OT_PERMISSIONS)
    held = set(user.permissions or [])
    if perm in held or held.intersection(_OT_GRANTS_IMPLIED_BY.get(perm, ())):
        return True
    return getattr(user, "staff_role", None) in _OT_GRANTS_IMPLIED_BY_ROLE.get(perm, ())


#: Logins saved before migration 123: clinicians and ward nurses read imaging
#: reports; doctor and radiologist roles write them.
_RADIOLOGY_GRANTS_IMPLIED_BY: dict[str, tuple[str, ...]] = {
    "RADIOLOGY_READ": ("RADIOLOGY_WRITE", "OPD_CLINICAL", "IPD_CLINICAL", "IPD_NURSING"),
    "RADIOLOGY_WRITE": (),
}
_RADIOLOGY_GRANTS_IMPLIED_BY_ROLE: dict[str, tuple[str, ...]] = {
    "RADIOLOGY_READ": ("RADIOLOGIST", "DOCTOR", "NURSE"),
    "RADIOLOGY_WRITE": ("RADIOLOGIST", "DOCTOR"),
}


def holds_radiology_permission(user: "AdminUser", perm: str) -> bool:
    """True if `user` may perform the radiology action `perm` (RADIOLOGY_READ / RADIOLOGY_WRITE)."""
    if user.role in ("super_admin", "clinic_admin"):
        return True
    held = set(user.permissions or [])
    if perm in held or held.intersection(_RADIOLOGY_GRANTS_IMPLIED_BY.get(perm, ())):
        return True
    return getattr(user, "staff_role", None) in _RADIOLOGY_GRANTS_IMPLIED_BY_ROLE.get(perm, ())


#: Logins saved before migration 124: IPD billing / admissions staff read the
#: insurance desk; IPD_ADMIN manages it; the cashier and TPA desk roles imply theirs.
_INSURANCE_GRANTS_IMPLIED_BY: dict[str, tuple[str, ...]] = {
    "INSURANCE_READ": ("INSURANCE_MANAGE", "IPD_BILLING", "IPD_ADMISSIONS", "IPD_ADMIN", "OPD_BILLING"),
    "INSURANCE_MANAGE": ("IPD_ADMIN",),
}
_INSURANCE_GRANTS_IMPLIED_BY_ROLE: dict[str, tuple[str, ...]] = {
    "INSURANCE_READ": ("TPA_DESK", "CASHIER"),
    "INSURANCE_MANAGE": ("TPA_DESK",),
}


def holds_insurance_permission(user: "AdminUser", perm: str) -> bool:
    """True if `user` may perform the insurance action `perm` (INSURANCE_READ / INSURANCE_MANAGE)."""
    if user.role in ("super_admin", "clinic_admin"):
        return True
    held = set(user.permissions or [])
    if perm in held or held.intersection(_INSURANCE_GRANTS_IMPLIED_BY.get(perm, ())):
        return True
    return getattr(user, "staff_role", None) in _INSURANCE_GRANTS_IMPLIED_BY_ROLE.get(perm, ())


def holds_services_permission(user: "AdminUser") -> bool:
    """True if `user` may change the service / tariff master."""
    if user.role in ("super_admin", "clinic_admin"):
        return True
    return bool(set(user.permissions or []) & {"SERVICES_MANAGE", "OPD_ADMIN"})


def holds_credit_note_permission(user: "AdminUser") -> bool:
    """True if `user` may issue a credit note. A CASHIER login saved before
    migration 117 has no stored BILLING_CREDIT_NOTE grant; its role implies it."""
    if user.role in ("super_admin", "clinic_admin"):
        return True
    held = set(user.permissions or [])
    return bool(held & {"BILLING_CREDIT_NOTE", "OPD_ADMIN"}) or getattr(user, "staff_role", None) == "CASHIER"


def role_implied_grants(staff_role: Optional[str]) -> set[str]:
    """What `staff_role` confers by itself (the *_BY_ROLE tables above), whatever grants are stored.
    A phlebotomist's worklist access is left out: that login is confined to two routes."""
    out = {perm for table in (_LAB_GRANTS_IMPLIED_BY_ROLE, _OT_GRANTS_IMPLIED_BY_ROLE,
                              _RADIOLOGY_GRANTS_IMPLIED_BY_ROLE, _INSURANCE_GRANTS_IMPLIED_BY_ROLE)
           for perm, roles in table.items() if staff_role in roles}
    return out | ({"BILLING_CREDIT_NOTE"} if staff_role == "CASHIER" else set())


def role_grants_beyond_authority(staff_role: Optional[str], granter: "AdminUser") -> list[str]:
    """The grants `staff_role` would confer that `granter` does not hold. A role implies
    grants on its own, so cap_permissions_to_authority alone cannot stop a staff granter
    (or a staff editor on their own account) minting authority by picking the role."""
    if granter.role in ("super_admin", "clinic_admin"):
        return []
    holders = (("LAB_", holds_lab_permission), ("OT_", holds_ot_permission),
               ("RADIOLOGY_", holds_radiology_permission), ("INSURANCE_", holds_insurance_permission))

    def held(perm: str) -> bool:
        if perm == "BILLING_CREDIT_NOTE":
            return holds_credit_note_permission(granter)
        fn = next((f for prefix, f in holders if perm.startswith(prefix)), None)
        return fn(granter, perm) if fn else perm in (granter.permissions or [])

    return sorted(p for p in role_implied_grants(staff_role) if not held(p))


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


assert_staff_not_pinned_elsewhere = enforce_branch_scope


async def resolve_owned_branch(
    user: "AdminUser", branch_id: str, clinic_id: Optional[str] = None
) -> dict:
    """Verify branch_id exists inside the caller's ACTIVE clinic scope, then
    enforce branch pinning. Returns the branch row.

    404 if the branch does not exist or belongs to another clinic (cross-tenant
    IDOR), 403 if the caller is staff pinned to a different branch.

    `clinic_id` is the scope already resolved by
    admin.enforce_clinic_access() for this request. Pass it. It used to be
    absent, and super_admin was exempted from the ownership check entirely —
    so a super_admin acting in what looked like one clinic's panel could
    assign, unassign and re-session doctors on ANY tenant's branches. There is
    no legitimate /admin caller that needs to reach across tenants; that is
    what /platform is for.
    """
    # 1. Branch-pinning check for branch-scoped staff (fail fast before DB call)
    enforce_branch_scope(user, branch_id)

    from app.database import sb, supabase, is_valid_clinic_scope

    scope = clinic_id if is_valid_clinic_scope(clinic_id) else getattr(user, "clinic_id", None)
    if not is_valid_clinic_scope(scope):
        raise HTTPException(
            status_code=400,
            detail="No clinic selected. Pass ?clinic_id=<clinic id> for this action.",
        )

    # Off the event loop: this runs on six admin endpoints, and a blocking
    # PostgREST round-trip here froze every other request in the worker.
    # unscoped: unique_row_key
    result = await sb(supabase.table("branches").select("*").eq("id", branch_id))
    if not result.data:
        raise HTTPException(status_code=404, detail="Branch not found")

    branch = result.data[0]
    # 2. Cross-tenant IDOR check — applies to EVERY role, super_admin included.
    if str(branch.get("clinic_id") or "") != str(scope):
        raise HTTPException(status_code=404, detail="Branch not found")

    return branch
