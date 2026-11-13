"""Kriya Diagnostic OS API — foundation (migration 125, DX-05).
Design: docs/diagnostic-os/API.md, SECURITY.md §2–§4.

Every route: permission -> enforce_clinic_access -> dx_access(clinic) ->
(new work) wind-down rule -> site/branch scope -> service. Zero-LLM.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.database import scoped_query, sb
from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.services import dx
from app.services.permissions import holds_dx_permission, resolve_owned_branch
from app.services.tenant import dx_access, get_clinic_by_id, invalidate_tenant_cache

router = APIRouter(prefix="/admin/dx", tags=["Diagnostic OS"])

SITE_CODE = r"^[A-Z0-9]{2,6}$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsPatch(_In):
    version: int = Field(..., ge=0)     # the version the editor loaded (CAS)
    approval_levels: Optional[Literal[2, 3]] = None
    collect_before_payment: Optional[bool] = None
    auto_release: Optional[Literal["off", "per_department", "when_complete"]] = None
    delivery_owner: Optional[dict[str, Literal["connector", "shadow", "dx"]]] = None
    sample_seq_floor: Optional[int] = None
    label_mode: Optional[Literal["per_sample", "per_test"]] = None
    register_label: Optional[bool] = None
    extra_labels: Optional[int] = None
    tat_clock: Optional[Literal["registration", "receipt"]] = None
    accession_prefix: Optional[str] = None
    batch_prefix: Optional[str] = None
    b2b_prefix: Optional[str] = None
    referrer_copy_default: Optional[bool] = None
    critical_call_required: Optional[bool] = None
    share_link_ttl_hours: Optional[int] = None
    instrument_auto_enter: Optional[bool] = None


class SiteIn(_In):
    branch_id: str
    kind: Literal["collection", "processing", "collection_and_processing"] = "collection_and_processing"
    code: str = Field(..., pattern=SITE_CODE)
    display_name: str = Field(..., min_length=2, max_length=120)
    legal_name: Optional[str] = Field(None, max_length=200)
    registration_no: Optional[str] = Field(None, max_length=80)
    nabl_certificate_no: Optional[str] = Field(None, max_length=80)


class SitePatch(_In):
    kind: Optional[Literal["collection", "processing", "collection_and_processing"]] = None
    code: Optional[str] = Field(None, pattern=SITE_CODE)
    display_name: Optional[str] = Field(None, min_length=2, max_length=120)
    legal_name: Optional[str] = Field(None, max_length=200)
    registration_no: Optional[str] = Field(None, max_length=80)
    nabl_certificate_no: Optional[str] = Field(None, max_length=80)
    is_active: Optional[bool] = None


class GoLiveIn(_In):
    version: int = Field(..., ge=0)


# ── scope helpers ───────────────────────────────────────────────────────────

async def _dx_scope(user: AdminUser, clinic_id: str, *perms: str, new_work: bool = False) -> tuple[str, dict]:
    """Any one of `perms`, tenant, entitlement. new_work=True also refuses in WIND_DOWN."""
    if perms and not any(holds_dx_permission(user, p) for p in perms):
        raise HTTPException(status_code=403, detail=f"Missing permission: {' or '.join(perms)}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    access = dx_access(clinic)
    if access is None:
        raise HTTPException(status_code=403, detail="The Diagnostic OS is not enabled for this clinic.")
    if new_work and access == "WIND_DOWN":
        raise HTTPException(status_code=409, detail="The Diagnostic OS is being switched off for this clinic: new "
                                                    "visits and configuration are closed. Existing visits continue.")
    return scope, clinic


def _visit_for(user: AdminUser, visit: dict) -> dict:
    """Site scope (SECURITY §4): a branch-pinned user reaches a visit of their own
    branch, or one with a sample collected / processed / received at their site.
    The caller loads `visit` with "site_branch_ids" (branches of those sites)."""
    pinned = getattr(user, "branch_id", None)
    if not pinned or str(pinned) in {str(visit.get("branch_id"))} | {str(b) for b in visit.get("site_branch_ids", [])}:
        return visit
    raise HTTPException(status_code=404, detail="Visit not found.")


def _clinic_wide(user: AdminUser) -> None:
    """Settings and go-live are clinic-wide: a branch-pinned admin is site-limited."""
    if getattr(user, "branch_id", None):
        raise HTTPException(status_code=403, detail="Only a clinic-wide administrator can change lab settings.")


async def _audit(user: AdminUser, request: Request, action: str, rtype: str, rid: Optional[str], details=None):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


def _pinned_branch(user: AdminUser) -> Optional[str]:
    return str(user.branch_id) if getattr(user, "branch_id", None) else None


# ── setup ───────────────────────────────────────────────────────────────────

@router.get("/setup")
async def get_setup(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _dx_scope(user, clinic_id, "DX_ADMIN", "DX_READ")
    settings = dx.get_dx_settings(clinic)
    sites = await dx.list_sites(scope, _pinned_branch(user))
    bq = scoped_query("branches", scope, "id, name, is_active").order("name")
    branches = (await sb(bq.eq("id", _pinned_branch(user)) if _pinned_branch(user) else bq)).data or []
    return {"access": dx_access(clinic), "state": clinic.get("dx_state"), "settings": settings,
            "sites": sites, "branches": branches, "checklist": dx.setup_checklist(settings, sites)}


@router.get("/settings")
async def get_settings(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _dx_scope(user, clinic_id, "DX_ADMIN", "DX_READ")
    return {"settings": dx.get_dx_settings(clinic)}


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _dx_scope(user, clinic_id, "DX_ADMIN")
    _clinic_wide(user)
    changes = body.model_dump(exclude_none=True, exclude={"version"})
    settings = await dx.update_dx_settings(scope, clinic, changes, body.version)
    invalidate_tenant_cache()
    await _audit(user, request, "dx_settings_update", "clinic", scope, {"keys": sorted(changes)})
    return {"settings": settings}


@router.get("/sites")
async def list_sites(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", "DX_READ")
    return {"sites": await dx.list_sites(scope, _pinned_branch(user))}


@router.post("/sites", status_code=status.HTTP_201_CREATED)
async def create_site(body: SiteIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", new_work=True)
    await resolve_owned_branch(user, body.branch_id, scope)   # 404 other tenant, 403 other branch
    site = await dx.create_or_update_site(scope, body.model_dump(exclude_none=True))
    await _audit(user, request, "dx_site_create", "dx_site", site["id"], {"code": body.code})
    return site


@router.put("/sites/{site_id}")
async def update_site(site_id: str, body: SitePatch, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    site = await dx.get_site(scope, site_id)
    enforce_branch_scope(user, site.get("branch_id"))
    nullable = ("legal_name", "registration_no", "nabl_certificate_no")   # null clears these; never the rest
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None or k in nullable}
    if not patch:
        return site
    site = await dx.create_or_update_site(scope, patch, site_id)
    await _audit(user, request, "dx_site_update", "dx_site", site_id, {"keys": sorted(patch)})
    return site


@router.post("/go-live")
async def go_live(body: GoLiveIn, request: Request, clinic_id: str = "default",
                  user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _dx_scope(user, clinic_id, "DX_ADMIN", new_work=True)
    _clinic_wide(user)
    settings = await dx.go_live(scope, clinic, body.version)
    invalidate_tenant_cache()
    await _audit(user, request, "dx_go_live", "clinic", scope, {"went_live_at": settings.get("went_live_at")})
    return {"settings": settings}
