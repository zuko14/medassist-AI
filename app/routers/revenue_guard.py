"""Kriya Revenue Guard API (migration 143; docs/revenue-integrity/SPEC.md §6–§7).

Every route: permission -> enforce_clinic_access -> ri_access(clinic) -> branch pin -> service. Read-only over
billing: dismiss writes only ri_findings, run writes only ri_* rows. Zero-LLM.
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.database import sb, supabase
from app.routers.admin import AdminUser, enforce_clinic_access, log_admin_action, verify_credentials
from app.services import revenue_guard as ri
from app.services.permissions import holds_ri_permission
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache, ri_access
from app.utils.helpers import today_ist

router = APIRouter(prefix="/admin/ri", tags=["Revenue Guard"])


class DismissIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=5, max_length=300)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_opd_invoices_30d: Optional[int] = None
    review_free_days: Optional[int] = None
    exempt_reasons: Optional[list[str]] = Field(None, max_length=10)


async def _scope(user: AdminUser, clinic_id: Optional[str], perm: str) -> tuple[str, dict]:
    if not holds_ri_permission(user, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not ri_access(clinic):
        raise HTTPException(status_code=403, detail="Revenue Guard is not enabled for this clinic.")
    return scope, clinic


def _branch(user: AdminUser, branch_id: Optional[str]) -> Optional[str]:
    """A branch-pinned login sees its branch only, whatever it asks for."""
    pinned = getattr(user, "branch_id", None)
    return str(pinned) if pinned else branch_id


@router.get("/summary")
async def get_summary(clinic_id: Optional[str] = None, branch_id: Optional[str] = None,
                      from_: Optional[date] = Query(None, alias="from"), to: Optional[date] = None,
                      user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "RI_VIEW")
    today = today_ist()
    until = to or today
    since = from_ or today.replace(day=1)
    if since > until or (until - since).days > 366:
        raise HTTPException(status_code=422, detail="Pick a date range of at most one year.")
    out = await ri.summary(scope, _branch(user, branch_id), since, until)
    return {**out, "settings": ri.get_settings(clinic), "can_resolve": holds_ri_permission(user, "RI_RESOLVE"),
            "rules": {k: {"kind": v[0], "label": v[1]} for k, v in ri.RULES.items()}}


@router.get("/findings")
async def get_findings(clinic_id: Optional[str] = None, branch_id: Optional[str] = None,
                       status: Optional[Literal["open", "resolved", "dismissed"]] = "open",
                       rule: Optional[str] = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "RI_VIEW")
    if rule and rule not in ri.RULES:
        raise HTTPException(status_code=422, detail="Unknown rule.")
    return await ri.list_findings(scope, _branch(user, branch_id), status, rule, limit, offset)


@router.post("/findings/{finding_id}/dismiss")
async def dismiss_finding(finding_id: str, body: DismissIn, request: Request, clinic_id: Optional[str] = None,
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "RI_RESOLVE")
    if not body.reason.strip() or len(body.reason.strip()) < 5:
        raise HTTPException(status_code=422, detail="Say why in at least 5 characters.")
    row = await ri.dismiss(scope, finding_id, _branch(user, None), body.reason, user)
    await log_admin_action(user=user, action="ri_dismiss", resource_type="ri_finding", resource_id=finding_id,
                           details={"rule": row.get("rule_code"), "amount_paise": row.get("amount_paise"),
                                    "reason": body.reason.strip()},
                           ip_address=request.client.host if request.client else None)
    return row


@router.post("/run")
async def run_now(request: Request, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "RI_RESOLVE")
    out = await ri.manual_run(clinic)
    await log_admin_action(user=user, action="ri_run", resource_type="clinic", resource_id=clinic["id"],
                           details={"new": out.get("new"), "resolved": out.get("resolved")},
                           ip_address=request.client.host if request.client else None)
    return out


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, request: Request, clinic_id: Optional[str] = None,
                         user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "RI_RESOLVE")
    if user.role not in ("super_admin", "clinic_admin") or getattr(user, "branch_id", None):
        raise HTTPException(status_code=403, detail="Only a clinic-wide administrator can change Revenue Guard settings.")
    changes = ri.validate_settings(body.model_dump(exclude_none=True))
    merged = {**(clinic.get("ri_settings") or {}), **changes}
    # unscoped: platform_admin
    await sb(supabase.table("clinics").update({"ri_settings": merged}).eq("id", scope))
    invalidate_tenant_cache()
    await log_admin_action(user=user, action="ri_settings", resource_type="clinic", resource_id=scope, details=changes,
                           ip_address=request.client.host if request.client else None)
    return {"settings": {**ri.DEFAULT_SETTINGS, **merged}}

