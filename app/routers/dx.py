"""Kriya Diagnostic OS API — foundation (migration 125, DX-05).
Design: docs/diagnostic-os/API.md, SECURITY.md §2–§4.

Every route: permission -> enforce_clinic_access -> dx_access(clinic) ->
(new work) wind-down rule -> site/branch scope -> service. Zero-LLM.
"""

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.database import sb, scoped_query
from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.routers.lis import ParameterIn
from app.routers.opd import PatientRegisterIn
from app.services import dx, dx_b2b, dx_billing, dx_catalogue, dx_results, dx_seed
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
    counts = {key: sum(1 for r in await dx_catalogue.list_rows(table, scope) if r.get("is_active"))
              for key, table in (("departments", "dx_departments"), ("containers", "dx_containers"),
                                 ("signatories", "dx_signatories"))}
    return {"access": dx_access(clinic), "state": clinic.get("dx_state"), "settings": settings,
            "sites": sites, "branches": branches, "checklist": dx.setup_checklist(settings, sites, counts)}


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


# ═══ Catalogue (migration 126, DX-15) ════════════════════════════════════════
# Masters (departments, containers, signatories) are clinic-wide configuration;
# the test master follows lab_tests.branch_id like the existing lab-test admin.

CAT_READ = ("DX_READ", "DX_CATALOGUE", "DX_ADMIN", "DX_CATALOGUE_APPROVE",
            "DX_REGISTER", "DX_BILLING")   # the desk sells from the catalogue
MASTER_WRITE = ("DX_CATALOGUE", "DX_ADMIN")
TEST_CODE = r"^[A-Z0-9._-]{1,20}$"
PARAM_CODE = r"^[A-Z0-9._-]{1,30}$"
Kind = Literal["test", "panel", "package"]
ResultMode = Literal["parameters", "document", "none"]
SampleType = Literal["serum", "whole_blood", "plasma", "urine", "stool", "swab", "semen", "csf", "fluid", "other"]


class DepartmentIn(_In):
    code: str = Field(..., pattern=r"^[A-Z]{2,8}$")
    name: str = Field(..., min_length=2, max_length=80)
    report_heading: str = Field(..., min_length=2, max_length=120)
    sort_order: int = Field(100, ge=0, le=999)


class DepartmentPatch(_In):
    code: Optional[str] = Field(None, pattern=r"^[A-Z]{2,8}$")
    name: Optional[str] = Field(None, min_length=2, max_length=80)
    report_heading: Optional[str] = Field(None, min_length=2, max_length=120)
    sort_order: Optional[int] = Field(None, ge=0, le=999)
    is_active: Optional[bool] = None


class ContainerIn(_In):
    code: str = Field(..., pattern=r"^[0-9]{2}$")          # the barcode's container digits (YY cc SSSSSSSS)
    name: str = Field(..., min_length=2, max_length=80)
    colour_hex: str = Field(..., pattern=r"^#[0-9A-Fa-f]{6}$")
    sample_type: SampleType
    default_volume_ml: Optional[float] = Field(None, gt=0, le=9999)
    stability_room_hours: Optional[int] = Field(None, gt=0, le=32767)
    stability_cold_hours: Optional[int] = Field(None, gt=0, le=32767)


class ContainerPatch(_In):
    name: Optional[str] = Field(None, min_length=2, max_length=80)
    colour_hex: Optional[str] = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    sample_type: Optional[SampleType] = None
    default_volume_ml: Optional[float] = Field(None, gt=0, le=9999)
    stability_room_hours: Optional[int] = Field(None, gt=0, le=32767)
    stability_cold_hours: Optional[int] = Field(None, gt=0, le=32767)
    is_active: Optional[bool] = None


class SignatoryIn(_In):
    doctor_id: str
    department_id: Optional[str] = None
    designation: str = Field(..., min_length=2, max_length=120)
    qualification_text: str = Field(..., min_length=2, max_length=200)


class SignatoryPatch(_In):
    designation: Optional[str] = Field(None, min_length=2, max_length=120)
    qualification_text: Optional[str] = Field(None, min_length=2, max_length=200)
    is_active: Optional[bool] = None


class _TestFields(_In):
    test_code: Optional[str] = Field(None, pattern=TEST_CODE)
    department_id: Optional[str] = None
    container_id: Optional[str] = None
    sample_volume_ml: Optional[float] = Field(None, gt=0, le=9999)
    method: Optional[str] = Field(None, max_length=120)
    tat_minutes: Optional[int] = Field(None, ge=5, le=43200)
    stat_tat_minutes: Optional[int] = Field(None, ge=5, le=43200)
    print_title: Optional[str] = Field(None, max_length=200)
    interpretation: Optional[str] = Field(None, max_length=6000)
    report_notes: Optional[str] = Field(None, max_length=2000)
    loinc_code: Optional[str] = Field(None, pattern=r"^[0-9]{1,7}-[0-9]$")
    print_order: Optional[int] = Field(None, ge=0, le=32767)


class TestIn(_TestFields):
    name: str = Field(..., min_length=2, max_length=200)
    price_paise: int = Field(..., gt=0, le=100_000_000)
    kind: Kind = "test"
    gender_restriction: Literal["all", "male", "female"] = "all"
    dx_result_mode: ResultMode = "parameters"
    sample_type: Optional[str] = Field(None, max_length=60)
    branch_id: Optional[str] = None


class TestPatch(_TestFields):
    kind: Optional[Kind] = None
    gender_restriction: Optional[Literal["all", "male", "female"]] = None
    dx_result_mode: Optional[ResultMode] = None
    is_active: Optional[bool] = None


class DxParameterIn(ParameterIn):
    """A band (LIS 118 shape and checks) plus the DX fields of migration 126."""
    parameter_code: Optional[str] = Field(None, pattern=PARAM_CODE)
    method: Optional[str] = Field(None, max_length=120)
    decimal_places: int = Field(2, ge=0, le=4)
    rounding_mode: Literal["half_up", "half_even", "truncate"] = "half_up"
    formula: Optional[str] = Field(None, max_length=500)
    reference_text: Optional[str] = Field(None, max_length=2000)
    machine_code: Optional[str] = Field(None, max_length=40)
    delta_direction: Optional[Literal["increase", "decrease", "both"]] = None
    delta_limit_percent: Optional[float] = Field(None, gt=0, le=999.99)
    delta_limit_abs: Optional[float] = Field(None, gt=0, le=9_999_999)
    delta_lookback_days: int = Field(30, ge=1, le=3650)
    low_exclusive: bool = False
    high_exclusive: bool = False


class ComponentIn(_In):
    child_test_id: str
    sort_order: int = Field(10, ge=0, le=999)


class ApproveIn(_In):
    digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")     # the digest the approver reviewed


class PatientCtx(_In):
    age_years: Optional[float] = Field(None, ge=0, le=130)
    is_female: Optional[bool] = None


class FormulaCheckIn(_In):
    formula: str = Field(..., min_length=1, max_length=500)
    codes: list[str] = Field(default_factory=list, max_length=100)
    inputs: dict[str, float] = Field(default_factory=dict)
    patient: Optional[PatientCtx] = None
    decimal_places: int = Field(2, ge=0, le=4)
    rounding_mode: Literal["half_up", "half_even", "truncate"] = "half_up"


def _nullable_patch(body: BaseModel, not_null: tuple[str, ...] = ()) -> dict:
    """Fields the client sent; an explicit null clears a column, except NOT NULL ones."""
    return {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None or k not in not_null}


async def _test_for(user: AdminUser, scope: str, test_id: str) -> dict:
    test = await dx_catalogue.get_test_row(scope, test_id)
    enforce_branch_scope(user, test.get("branch_id"))
    return test


# ── departments / containers ────────────────────────────────────────────────

@router.get("/departments")
async def list_departments(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *CAT_READ)
    return {"departments": await dx_catalogue.list_rows("dx_departments", scope)}


@router.post("/departments", status_code=status.HTTP_201_CREATED)
async def create_department(body: DepartmentIn, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *MASTER_WRITE, new_work=True)
    _clinic_wide(user)
    row = await dx_catalogue.create_row("dx_departments", scope, body.model_dump())
    await _audit(user, request, "dx_department_create", "dx_department", row["id"], {"code": body.code})
    return row


@router.put("/departments/{department_id}")
async def update_department(department_id: str, body: DepartmentPatch, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *MASTER_WRITE)
    _clinic_wide(user)
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    row = await dx_catalogue.update_row("dx_departments", scope, department_id, patch, "Department")
    await _audit(user, request, "dx_department_update", "dx_department", department_id, {"keys": sorted(patch)})
    return row


@router.get("/containers")
async def list_containers(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *CAT_READ)
    return {"containers": await dx_catalogue.list_rows("dx_containers", scope)}


@router.post("/containers", status_code=status.HTTP_201_CREATED)
async def create_container(body: ContainerIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *MASTER_WRITE, new_work=True)
    _clinic_wide(user)
    row = await dx_catalogue.create_row("dx_containers", scope, body.model_dump(exclude_none=True))
    await _audit(user, request, "dx_container_create", "dx_container", row["id"], {"code": body.code})
    return row


@router.put("/containers/{container_id}")
async def update_container(container_id: str, body: ContainerPatch, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *MASTER_WRITE)
    _clinic_wide(user)
    patch = _nullable_patch(body, ("name", "colour_hex", "sample_type", "is_active"))
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    row = await dx_catalogue.update_row("dx_containers", scope, container_id, patch, "Container")
    await _audit(user, request, "dx_container_update", "dx_container", container_id, {"keys": sorted(patch)})
    return row


# ── signatories ─────────────────────────────────────────────────────────────

@router.get("/signatories")
async def list_signatories(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *CAT_READ)
    return {"signatories": await dx_catalogue.list_signatories(scope)}


@router.post("/signatories", status_code=status.HTTP_201_CREATED)
async def create_signatory(body: SignatoryIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *MASTER_WRITE, new_work=True)
    _clinic_wide(user)
    row = await dx_catalogue.create_signatory(scope, body.model_dump(exclude_none=True))
    await _audit(user, request, "dx_signatory_create", "dx_signatory", row["id"],
                 {"doctor_id": body.doctor_id, "department_id": body.department_id})
    return row


@router.put("/signatories/{signatory_id}")
async def update_signatory(signatory_id: str, body: SignatoryPatch, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *MASTER_WRITE)
    _clinic_wide(user)
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    row = await dx_catalogue.update_signatory(scope, signatory_id, patch)
    await _audit(user, request, "dx_signatory_update", "dx_signatory", signatory_id, {"keys": sorted(patch)})
    return row


@router.post("/signatories/{signatory_id}/signature")
async def upload_signature(signatory_id: str, request: Request, file: UploadFile = File(...),
                           clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *MASTER_WRITE)
    _clinic_wide(user)
    raw = await file.read(dx_catalogue.SIGNATURE_MAX_BYTES + 1)
    row = await dx_catalogue.store_signature(scope, signatory_id, raw)
    await _audit(user, request, "dx_signature_upload", "dx_signatory", signatory_id,
                 {"path": row.get("signature_asset_path")})
    return row


# ── test master ─────────────────────────────────────────────────────────────

@router.get("/tests")
async def list_tests(clinic_id: str = "default", kind: Optional[Kind] = None, department_id: Optional[str] = None,
                     q: Optional[str] = Query(None, max_length=80), approved: Optional[bool] = None,
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *CAT_READ)
    return {"tests": await dx_catalogue.list_tests(scope, _pinned_branch(user), kind, department_id, q, approved)}


@router.post("/tests", status_code=status.HTTP_201_CREATED)
async def create_test(body: TestIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_CATALOGUE", new_work=True)
    data = body.model_dump(exclude_none=True)
    if body.branch_id:
        await resolve_owned_branch(user, body.branch_id, scope)
    elif _pinned_branch(user):
        data["branch_id"] = _pinned_branch(user)
    # a new test starts as an inactive draft: WhatsApp booking never offers it until someone activates it
    row = await dx_catalogue.create_test(scope, {**data, "is_active": False})
    await _audit(user, request, "dx_test_create", "lab_test", row["id"], {"name": body.name, "kind": body.kind})
    return row


@router.get("/tests/{test_id}")
async def get_test(test_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *CAT_READ)
    await _test_for(user, scope, test_id)
    return await dx_catalogue.get_test(scope, test_id)


@router.put("/tests/{test_id}")
async def update_test(test_id: str, body: TestPatch, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_CATALOGUE")
    await _test_for(user, scope, test_id)
    patch = _nullable_patch(body, ("kind", "gender_restriction", "dx_result_mode", "is_active"))
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    row = await dx_catalogue.update_test(scope, test_id, patch)
    await _audit(user, request, "dx_test_update", "lab_test", test_id, {"keys": sorted(patch)})
    return row


@router.post("/tests/{test_id}/parameters", status_code=status.HTTP_201_CREATED)
async def create_parameter(test_id: str, body: DxParameterIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_CATALOGUE")
    await _test_for(user, scope, test_id)
    row = await dx_catalogue.create_parameter(scope, test_id, body.model_dump())
    await _audit(user, request, "dx_parameter_create", "lab_test_parameter", row["id"],
                 {"lab_test_id": test_id, "name": row["name"]})
    return row


@router.put("/tests/{test_id}/parameters/{parameter_id}")
async def update_parameter(test_id: str, parameter_id: str, body: DxParameterIn, request: Request,
                           clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_CATALOGUE")
    await _test_for(user, scope, test_id)
    row = await dx_catalogue.update_parameter(scope, test_id, parameter_id, body.model_dump())
    await _audit(user, request, "dx_parameter_update", "lab_test_parameter", parameter_id,
                 {"lab_test_id": test_id, "name": row["name"], "is_active": row["is_active"]})
    return row


@router.get("/tests/{test_id}/components")
async def list_components(test_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *CAT_READ)
    await _test_for(user, scope, test_id)
    return {"components": await dx_catalogue.components_of(scope, test_id)}


@router.post("/tests/{test_id}/components", status_code=status.HTTP_201_CREATED)
async def add_component(test_id: str, body: ComponentIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_CATALOGUE")
    await _test_for(user, scope, test_id)
    await _test_for(user, scope, body.child_test_id)
    row = await dx_catalogue.add_component(scope, test_id, body.child_test_id, body.sort_order)
    await _audit(user, request, "dx_component_add", "lab_test", test_id, {"child_test_id": body.child_test_id})
    return row


@router.delete("/tests/{test_id}/components/{child_test_id}")
async def remove_component(test_id: str, child_test_id: str, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_CATALOGUE")
    await _test_for(user, scope, test_id)
    await dx_catalogue.remove_component(scope, test_id, child_test_id)
    await _audit(user, request, "dx_component_remove", "lab_test", test_id, {"child_test_id": child_test_id})
    return {"success": True}


@router.post("/tests/{test_id}/approve")
async def approve_test(test_id: str, body: ApproveIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_CATALOGUE_APPROVE")
    await _test_for(user, scope, test_id)
    out = await dx_catalogue.approve_test(scope, test_id, body.digest, getattr(user, "doctor_id", None),
                                          user.username)
    await _audit(user, request, "dx_test_approve", "lab_test", test_id,
                 {"digest": body.digest, "approved_by": out.get("approved_by"), "approver": user.username})
    return out


@router.post("/formula/check")
async def check_formula(body: FormulaCheckIn, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    await _dx_scope(user, clinic_id, "DX_CATALOGUE", "DX_CATALOGUE_APPROVE")
    p = body.patient
    patient = {} if p is None else {k: v for k, v in (
        ("AGE_YEARS", p.age_years), ("IS_FEMALE", None if p.is_female is None else int(p.is_female)),
        ("IS_MALE", None if p.is_female is None else int(not p.is_female))) if v is not None}
    return dx_catalogue.check_formula(body.formula, body.codes, body.inputs, patient, body.decimal_places,
                                      body.rounding_mode)


# ── seed library and parameter import (DX-16) ───────────────────────────────

class SeedApplyIn(_In):
    test_keys: Optional[list[str]] = Field(None, max_length=500)    # None = the whole library


@router.get("/seed/preview")
async def seed_preview(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_CATALOGUE")
    return await dx_seed.preview_seed(scope)


@router.post("/seed/apply")
async def seed_apply(body: SeedApplyIn, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_CATALOGUE", new_work=True)
    _clinic_wide(user)
    out = await dx_seed.apply_seed(scope, body.test_keys)
    await _audit(user, request, "dx_seed_apply", "clinic", scope,
                 {k: v for k, v in out.items() if k != "conflicts"} | {"conflicts": len(out["conflicts"])})
    return out


async def _import(request: Request, file: UploadFile, clinic_id: str, user: AdminUser, dry_run: bool) -> dict:
    scope, _ = await _dx_scope(user, clinic_id, "DX_CATALOGUE", new_work=not dry_run)
    _clinic_wide(user)
    raw = await file.read(dx_seed.IMPORT_MAX_BYTES + 1)
    out = await dx_seed.import_parameters(scope, raw, dry_run)
    if not dry_run:
        await _audit(user, request, "dx_parameter_import", "clinic", scope, {"rows": out["rows"],
                                                                             "written": out["written"]})
    return out


@router.post("/import/dry-run")
async def import_dry_run(request: Request, file: UploadFile = File(...), clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    return await _import(request, file, clinic_id, user, dry_run=True)


@router.post("/import/apply")
async def import_apply(request: Request, file: UploadFile = File(...), clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    return await _import(request, file, clinic_id, user, dry_run=False)


# ═══ Registration, visits, billing (migration 127, DX-20…DX-23) ══════════════

VISIT_READ = ("DX_READ", "DX_REGISTER", "DX_BILLING", "DX_COLLECT", "DX_DISPATCH", "DX_ADMIN")
MONEY = ("DX_BILLING", "DX_REGISTER", "DX_B2B_MANAGE", "DX_REPORTS_MIS")
PHONE = r"^\+?[0-9]{10,15}$"
IDEM = Field(..., min_length=8, max_length=80)


class ReferrerIn(_In):
    kind: Literal["doctor", "hospital", "clinic", "corporate", "other"] = "doctor"
    name: str = Field(..., min_length=2, max_length=120)
    qualification: Optional[str] = Field(None, max_length=120)
    registration_no: Optional[str] = Field(None, max_length=60)
    phone: Optional[str] = Field(None, pattern=PHONE)
    email: Optional[str] = Field(None, max_length=120)
    organisation: Optional[str] = Field(None, max_length=120)
    b2b_account_id: Optional[str] = None
    report_copy: Literal["none", "whatsapp", "email"] = "none"
    force: bool = False          # create even though a similar name exists


class ReferrerPatch(_In):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    qualification: Optional[str] = Field(None, max_length=120)
    registration_no: Optional[str] = Field(None, max_length=60)
    phone: Optional[str] = Field(None, pattern=PHONE)
    email: Optional[str] = Field(None, max_length=120)
    organisation: Optional[str] = Field(None, max_length=120)
    report_copy: Optional[Literal["none", "whatsapp", "email"]] = None
    is_active: Optional[bool] = None


AccountKind = Literal["hospital", "clinic", "franchise", "corporate", "insurance_tpa", "camp", "other"]
Delivery = Literal["patient", "account", "both", "none"]


class AccountIn(_In):
    code: Optional[str] = Field(None, pattern=r"^[A-Z0-9]{2,12}$")
    name: str = Field(..., min_length=2, max_length=120)
    kind: AccountKind
    contact_name: Optional[str] = Field(None, max_length=100)
    contact_phone: Optional[str] = Field(None, pattern=PHONE)
    desk_whatsapp: Optional[str] = Field(None, pattern=PHONE)
    report_delivery: Optional[Delivery] = None        # default: patient; payer-routed kinds: account
    patient_copy_reason: Optional[str] = Field(None, min_length=10, max_length=300)
    default_billing_mode: Literal["patient", "b2b_credit", "b2b_prepaid", "b2b_split"] = "b2b_credit"
    white_label: bool = False
    # migration 132: money terms (all optional; DB defaults otherwise)
    rate_card_id: Optional[UUID] = None
    credit_limit_paise: Optional[int] = Field(None, ge=0, le=10_000_000_000)
    credit_days: Optional[int] = Field(None, ge=0, le=365)
    gstin: Optional[str] = Field(None, pattern=r"^[0-9A-Z]{15}$")
    billing_address: Optional[str] = Field(None, max_length=500)
    statement_cycle: Optional[Literal["weekly", "fortnightly", "monthly"]] = None
    auto_hold: Optional[bool] = None
    copay_mode: Optional[Literal["none", "percent", "fixed"]] = None
    copay_value: Optional[int] = Field(None, ge=0, le=100_000_000)   # basis points (percent) or paise (fixed)


class AccountPatch(_In):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    contact_name: Optional[str] = Field(None, max_length=100)
    contact_phone: Optional[str] = Field(None, pattern=PHONE)
    desk_whatsapp: Optional[str] = Field(None, pattern=PHONE)
    report_delivery: Optional[Delivery] = None
    patient_copy_reason: Optional[str] = Field(None, min_length=10, max_length=300)
    white_label: Optional[bool] = None
    status: Optional[Literal["active", "on_hold", "closed"]] = None
    hold_reason: Optional[str] = Field(None, max_length=300)
    default_billing_mode: Optional[Literal["patient", "b2b_credit", "b2b_prepaid", "b2b_split"]] = None
    rate_card_id: Optional[UUID] = None
    credit_limit_paise: Optional[int] = Field(None, ge=0, le=10_000_000_000)
    credit_days: Optional[int] = Field(None, ge=0, le=365)
    gstin: Optional[str] = Field(None, pattern=r"^[0-9A-Z]{15}$")
    billing_address: Optional[str] = Field(None, max_length=500)
    statement_cycle: Optional[Literal["weekly", "fortnightly", "monthly"]] = None
    auto_hold: Optional[bool] = None
    copay_mode: Optional[Literal["none", "percent", "fixed"]] = None
    copay_value: Optional[int] = Field(None, ge=0, le=100_000_000)


class SubjectIn(_In):
    name: str = Field(..., min_length=1, max_length=100)
    phone: Optional[str] = Field(None, pattern=PHONE)
    gender: Optional[Literal["male", "female", "other", "undisclosed"]] = None
    age_years: Optional[int] = Field(None, ge=0, le=130)


class LineIn(_In):
    lab_test_id: str
    discount_paise: int = Field(0, ge=0, le=100_000_000)


class FeeIn(_In):
    fee_code: Literal["home_collection", "stat_surcharge", "report_courier", "other"]
    amount_paise: int = Field(..., ge=0, le=100_000_000)
    item_name: Optional[str] = Field(None, max_length=100)


class _CartIn(_In):
    items: list[LineIn] = Field(default_factory=list, max_length=100)
    fees: list[FeeIn] = Field(default_factory=list, max_length=10)


class QuoteIn(_CartIn):
    patient_id: Optional[str] = None
    family_member_id: Optional[str] = None
    branch_id: Optional[str] = None
    billing_mode: Literal["patient", "internal", "b2b_credit", "b2b_prepaid", "b2b_split"] = "patient"
    b2b_account_id: Optional[str] = None


class _VisitFields(_CartIn):
    branch_id: Optional[str] = None
    referrer_id: Optional[str] = None
    b2b_account_id: Optional[str] = None
    external_ref: Optional[str] = Field(None, max_length=40)
    patient_type: Literal["op", "ip", "home", "b2b"] = "op"
    billing_mode: Literal["patient", "internal", "b2b_credit", "b2b_prepaid", "b2b_split"] = "patient"
    priority: Literal["routine", "urgent", "stat"] = "routine"
    fasting_status: Literal["fasting", "non_fasting", "unknown"] = "unknown"
    last_meal_at: Optional[datetime] = None
    clinical_notes: Optional[str] = Field(None, max_length=1000)
    delivery_prefs: dict = Field(default_factory=dict)
    idempotency_key: str = IDEM


class VisitIn(_VisitFields):
    patient_id: str
    family_member_id: Optional[str] = None
    source: Literal["walk_in", "b2b", "camp", "home_collection"] = "walk_in"


class ConvertIn(_VisitFields):
    appointment_ids: list[str] = Field(..., min_length=1, max_length=20)


class AddItemsIn(_CartIn):
    idempotency_key: str = IDEM


class ReasonIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)


class BillIn(_In):
    issue: bool = True


class ReceiptIn(_In):
    mode: Literal["cash", "upi", "card", "bank_transfer"]
    amount_paise: int = Field(..., gt=0, le=100_000_000)
    reference: Optional[str] = Field(None, max_length=80)
    idempotency_key: str = IDEM
    invoice_id: Optional[str] = None
    shift_id: Optional[str] = None


def _cart(body: _CartIn) -> dict:
    return {"items": [i.model_dump() for i in body.items], "fees": [f.model_dump(exclude_none=True) for f in body.fees]}


def _can_money(user: AdminUser) -> bool:
    return any(holds_dx_permission(user, p) for p in MONEY)


CONTACT = ("DX_REGISTER", "DX_BILLING", "DX_DISPATCH", "DX_COLLECT", "DX_B2B_MANAGE")


def _filter_visit(user: AdminUser, v: dict) -> dict:
    """SECURITY §4 data minimisation: no prices, bills or dues without a money grant; no
    contact details (external ref, delivery address / phone / e-mail) without a desk grant.
    A pinned user reaching another branch's visit through a sample site (processing
    site, runner) gets neither, whatever their grants."""
    pinned = _pinned_branch(user)
    other_site = bool(pinned) and str((v.get("visit") or {}).get("branch_id")) != pinned
    money_ok = _can_money(user) and not other_site
    contact_ok = any(holds_dx_permission(user, p) for p in CONTACT) and not other_site
    if money_ok and contact_ok:
        return v
    out = dict(v)
    if not money_ok:
        money = ("mrp_paise", "price_paise", "discount_paise", "patient_share_paise", "payer_share_paise",
                 "price_source")
        out["items"] = [{k: x for k, x in i.items() if k not in money} for i in v.get("items", [])]
        out["invoices"] = []
        if "timeline" in v:
            out["timeline"] = [{**e, "details": {k: x for k, x in (e.get("details") or {}).items()
                                                 if not k.endswith("_paise")}} for e in v["timeline"]]
    if not contact_ok and out.get("visit"):
        out["visit"] = {k: x for k, x in out["visit"].items() if k not in ("external_ref", "delivery_prefs")}
    return out


async def _visit(user: AdminUser, scope: str, accession_id: str) -> dict:
    v = await dx.get_visit(scope, accession_id)
    reach = await dx.site_branch_ids(scope, [accession_id])
    _visit_for(user, {**v["visit"], "site_branch_ids": reach.get(accession_id, set())})
    return v


async def _reg_branch(user: AdminUser, scope: str, requested: Optional[str]) -> str:
    """Where the visit is registered: the named branch (own tenant / own pin), the pinned
    branch, or the clinic's only site."""
    if requested:
        await resolve_owned_branch(user, requested, scope)
        return requested
    if _pinned_branch(user):
        return _pinned_branch(user)
    sites = [s for s in await dx.list_sites(scope) if s.get("is_active")]
    if len(sites) == 1:
        return sites[0]["branch_id"]
    raise HTTPException(status_code=422, detail="Choose the branch this visit is registered at.")


async def _reg_payload(user: AdminUser, scope: str, body: _VisitFields, **extra) -> dict:
    payload = {**body.model_dump(exclude={"items", "fees", "branch_id"}, mode="json", exclude_none=True),
               **_cart(body), **extra}
    payload["branch_id"] = await _reg_branch(user, scope, body.branch_id)
    return payload


# ── patients (OPD registry, under the DX scope) ─────────────────────────────

@router.get("/patients/search")
async def search_patients(q: str = Query(..., min_length=2, max_length=80), clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_REGISTER", "DX_BILLING")
    from app.services import opd
    return {"patients": await opd.search_patients(scope, q=q, limit=20)}


@router.post("/patients", status_code=status.HTTP_201_CREATED)
async def register_patient(body: PatientRegisterIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_REGISTER", new_work=True)
    from app.services import opd
    out = await opd.register_patient(scope, body.model_dump(), user)
    await _audit(user, request, "dx_patient_register", "patient", out.get("patient_id"),
                 {"family_member_id": out.get("family_member_id")})
    return out


# ── referrers ───────────────────────────────────────────────────────────────

@router.get("/referrers")
async def list_referrers(q: Optional[str] = Query(None, max_length=80), clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *VISIT_READ)
    rows = await dx_catalogue.list_rows("dx_referrers", scope)
    if q:
        key = "".join(ch for ch in q.upper() if ch.isalnum())
        rows = [r for r in rows if key in (r.get("name_key") or "") or (r.get("phone") or "").endswith(key)]
    return {"referrers": rows[:200]}


@router.get("/referrers/volume")
async def referrer_volume(months: int = Query(6, ge=1, le=24), clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    """Referral volume (visits and tests per referrer per IST month). ADR-DX-11: counts only —
    no payout, incentive or commission is computed or returned."""
    scope, _ = await _dx_scope(user, clinic_id, *VISIT_READ, "DX_B2B_MANAGE", "DX_REPORTS_MIS")
    from app.utils.helpers import today_ist
    start = today_ist().replace(day=1)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    visits = await dx_b2b._rows(scoped_query("dx_accessions", scope, "id, referrer_id, created_at")
                                .not_.is_("referrer_id", "null").neq("status", "cancelled")
                                .gte("created_at", f"{start.isoformat()}T00:00:00+05:30"))
    items = await dx_b2b._in("dx_accession_items", scope, "accession_id, kind, status", "accession_id",
                             [v["id"] for v in visits])
    tests: dict[str, int] = {}
    for i in items:
        if i["status"] == "active" and i["kind"] != "fee":
            tests[i["accession_id"]] = tests.get(i["accession_id"], 0) + 1
    ist = timezone(timedelta(hours=5, minutes=30))
    out: dict[tuple, dict] = {}
    for v in visits:
        month = datetime.fromisoformat(str(v["created_at"]).replace("Z", "+00:00")).astimezone(ist).strftime("%Y-%m")
        row = out.setdefault((v["referrer_id"], month), {"referrer_id": v["referrer_id"], "month": month,
                                                         "visits": 0, "tests": 0})
        row["visits"] += 1
        row["tests"] += tests.get(v["id"], 0)
    return {"from": start.isoformat(), "rows": sorted(out.values(), key=lambda r: (r["month"], r["referrer_id"]))}


@router.post("/referrers", status_code=status.HTTP_201_CREATED)
async def create_referrer(body: ReferrerIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_REGISTER", "DX_ADMIN", new_work=True)
    if not body.force:
        key = "".join(ch for ch in body.name.upper() if ch.isalnum())
        same = [r for r in await dx_catalogue.list_rows("dx_referrers", scope)
                if r.get("name_key") == key or (body.phone and r.get("phone") == body.phone)]
        if same:
            raise HTTPException(status_code=409, detail={"code": "dx_referrer_duplicate", "candidates": same[:5],
                                                         "message": "A referrer with this name or phone exists."})
    row = await dx_catalogue.create_row("dx_referrers", scope, body.model_dump(exclude={"force"}, exclude_none=True))
    await _audit(user, request, "dx_referrer_create", "dx_referrer", row["id"], {"name": body.name})
    return row


@router.put("/referrers/{referrer_id}")
async def update_referrer(referrer_id: str, body: ReferrerPatch, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    patch = _nullable_patch(body, ("name", "report_copy", "is_active"))
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    row = await dx_catalogue.update_row("dx_referrers", scope, referrer_id, patch, "Referrer")
    await _audit(user, request, "dx_referrer_update", "dx_referrer", referrer_id, {"keys": sorted(patch)})
    return row


# ── B2B accounts (identity, routing; money arrives in DX-M8) ────────────────

def _authorisation(user: AdminUser, reason: Optional[str]) -> dict:
    if not reason:
        return {}
    return {"patient_copy_reason": reason, "patient_copy_authorised_at": datetime.now(timezone.utc).isoformat(),
            "patient_copy_authorised_by": dx._actor(user)[0]}


@router.get("/b2b-accounts")
async def list_accounts(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_B2B_MANAGE", *VISIT_READ)
    accounts = await dx_catalogue.list_rows("dx_b2b_accounts", scope)
    if _can_money(user):        # balances are money (SECURITY §4)
        bal = await dx_b2b.balances(scope)
        accounts = [{**a, "balance_paise": (bal.get(a["id"]) or {}).get("balance_paise", 0)} for a in accounts]
    return {"accounts": accounts}


@router.post("/b2b-accounts", status_code=status.HTTP_201_CREATED)
async def create_account(body: AccountIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_B2B_MANAGE", new_work=True)
    data = body.model_dump(exclude={"patient_copy_reason"}, exclude_none=True, mode="json")
    # ADR-DX-20: a payer (insurance / TPA / corporate) gets the report; the patient only with an authorisation
    data.setdefault("report_delivery", "account" if body.kind in ("insurance_tpa", "corporate") else "patient")
    row = await dx_catalogue.create_row("dx_b2b_accounts", scope, {**data, **_authorisation(user, body.patient_copy_reason)})
    await _audit(user, request, "dx_b2b_account_create", "dx_b2b_account", row["id"],
                 {"name": body.name, "kind": body.kind, "report_delivery": row["report_delivery"]})
    return row


@router.put("/b2b-accounts/{account_id}")
async def update_account(account_id: str, body: AccountPatch, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_B2B_MANAGE")
    patch = {k: v for k, v in _nullable_patch(body, ("name", "report_delivery", "white_label", "status",
                                                     "default_billing_mode", "statement_cycle", "auto_hold",
                                                     "copay_mode")).items()
             if k != "patient_copy_reason"}
    patch = {k: (str(v) if isinstance(v, UUID) else v) for k, v in patch.items()}
    if "status" in patch:       # a hold set here is a manual hold; leaving it clears the kind
        patch["hold_kind"] = "manual" if patch["status"] == "on_hold" else None
        if patch["status"] != "on_hold":
            patch["hold_reason"] = None
    if "patient_copy_reason" in body.model_fields_set:
        # a reason records a new patient-copy authorisation; an explicit null revokes it
        patch.update(_authorisation(user, body.patient_copy_reason) or
                     {"patient_copy_reason": None, "patient_copy_authorised_at": None, "patient_copy_authorised_by": None})
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    row = await dx_catalogue.update_row("dx_b2b_accounts", scope, account_id, patch, "B2B account")
    await _audit(user, request, "dx_b2b_account_update", "dx_b2b_account", account_id, {"keys": sorted(patch)})
    return row


@router.post("/b2b-accounts/{account_id}/subjects", status_code=status.HTTP_201_CREATED)
async def add_subject(account_id: str, body: SubjectIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_REGISTER", new_work=True)
    out = await dx.ensure_b2b_subject(scope, account_id, body.model_dump())
    await _audit(user, request, "dx_b2b_subject_create", "family_member", out["family_member_id"],
                 {"b2b_account_id": account_id})
    return out


# ── visits ──────────────────────────────────────────────────────────────────

@router.post("/visits/quote")
async def quote_visit(body: QuoteIn, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _dx_scope(user, clinic_id, "DX_REGISTER", "DX_BILLING")
    payload = {**body.model_dump(exclude={"items", "fees"}, exclude_none=True), **_cart(body)}
    payload["branch_id"] = await _reg_branch(user, scope, body.branch_id)
    lines = await dx.quote(scope, clinic, payload, user)
    return {"lines": lines, "total_paise": sum(line["patient_share_paise"] for line in lines)}


@router.post("/visits", status_code=status.HTTP_201_CREATED)
async def create_visit(body: VisitIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _dx_scope(user, clinic_id, "DX_REGISTER", new_work=True)
    out = await dx.register_visit(scope, clinic, await _reg_payload(user, scope, body), user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_visit_register", "dx_accession", out["visit"]["id"],
                     {"number": out["visit"]["accession_number"], "items": len(out["items"])})
    return _filter_visit(user, out)


@router.get("/visits")
async def list_visits(clinic_id: str = "default", date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                      status_: Optional[str] = Query(None, alias="status"), q: Optional[str] = Query(None, max_length=80),
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *VISIT_READ)
    return {"visits": await dx.list_visits(scope, _pinned_branch(user), date, status_, q)}


@router.get("/visits/{accession_id}")
async def get_visit(accession_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *VISIT_READ)
    return _filter_visit(user, await _visit(user, scope, accession_id))


@router.post("/visits/{accession_id}/items")
async def add_visit_items(accession_id: str, body: AddItemsIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _dx_scope(user, clinic_id, "DX_REGISTER")
    await _visit(user, scope, accession_id)
    c = _cart(body)
    out = await dx.add_items(scope, clinic, accession_id, c["items"] + c["fees"], body.idempotency_key, user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_visit_items_add", "dx_accession", accession_id,
                     {"items": len(c["items"]) + len(c["fees"])})
    return _filter_visit(user, out)


@router.post("/visits/{accession_id}/items/{item_id}/cancel")
async def cancel_visit_item(accession_id: str, item_id: str, body: ReasonIn, request: Request,
                            clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_REGISTER")
    await _visit(user, scope, accession_id)
    await dx.item_of(scope, accession_id, item_id)
    out = await dx.cancel_item(scope, item_id, body.reason, user)
    await _audit(user, request, "dx_visit_item_cancel", "dx_accession", accession_id,
                 {"item_id": item_id, "reason": body.reason})
    return _filter_visit(user, out)


@router.post("/visits/{accession_id}/cancel")
async def cancel_visit(accession_id: str, body: ReasonIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_REGISTER")
    await _visit(user, scope, accession_id)
    out = await dx.cancel_visit(scope, accession_id, body.reason, user)
    await _audit(user, request, "dx_visit_cancel", "dx_accession", accession_id, {"reason": body.reason})
    return _filter_visit(user, out)


# ── billing (OPD ledger) ────────────────────────────────────────────────────

@router.post("/visits/{accession_id}/bill")
async def bill_visit(accession_id: str, body: BillIn, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_BILLING")
    await _visit(user, scope, accession_id)
    out = await dx_billing.bill_visit(scope, accession_id, user, body.issue)
    await _audit(user, request, "dx_visit_bill", "dx_accession", accession_id,
                 {"invoice_id": out.get("invoice_id"), "issued": body.issue, "lines": out.get("lines_added")})
    return out


@router.post("/visits/{accession_id}/receipts", status_code=status.HTTP_201_CREATED)
async def take_receipt(accession_id: str, body: ReceiptIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_BILLING")
    await _visit(user, scope, accession_id)
    out = await dx_billing.take_receipt(scope, accession_id, body.model_dump(), user)
    await _audit(user, request, "dx_visit_receipt", "dx_accession", accession_id,
                 {"mode": body.mode, "amount_paise": body.amount_paise})
    return out


@router.post("/visits/{accession_id}/payment-link")
async def visit_payment_link(accession_id: str, request: Request, clinic_id: str = "default",
                             user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _dx_scope(user, clinic_id, "DX_BILLING")
    await _visit(user, scope, accession_id)
    out = await dx_billing.payment_link(scope, clinic, accession_id)
    await _audit(user, request, "dx_visit_payment_link", "dx_accession", accession_id,
                 {"amount_paise": out.get("amount_paise")})
    return out


# ── today's bookings (W3) ───────────────────────────────────────────────────

@router.get("/bookings/today")
async def bookings_today(clinic_id: str = "default", branch_id: Optional[str] = None,
                         date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_REGISTER")
    if branch_id:
        enforce_branch_scope(user, branch_id)
    return {"bookings": await dx.bookings_today(scope, branch_id or _pinned_branch(user), date)}


@router.post("/bookings/convert", status_code=status.HTTP_201_CREATED)
async def convert_bookings(body: ConvertIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _dx_scope(user, clinic_id, "DX_REGISTER", new_work=True)
    appts = (await sb(scoped_query("appointments", scope, "id, patient_id, family_member_id, patient_phone, branch_id, "
                                   "booking_channel, collection_mode").in_("id", body.appointment_ids))).data or []
    if len(appts) != len(set(body.appointment_ids)):
        raise HTTPException(status_code=404, detail="Booking not found.")
    for a in appts:
        enforce_branch_scope(user, a.get("branch_id"))
    patient_id, member_id = await dx.booking_patient(scope, appts)
    source = ("voice_booking" if any(a.get("booking_channel") == "voice" for a in appts) else
              "home_collection" if any(a.get("collection_mode") == "home" for a in appts) else "whatsapp_booking")
    if not body.branch_id and len({a.get("branch_id") for a in appts}) == 1 and appts[0].get("branch_id"):
        body.branch_id = str(appts[0]["branch_id"])
    payload = await _reg_payload(user, scope, body, patient_id=patient_id, source=source,
                                 booking_ids=body.appointment_ids,
                                 **({"family_member_id": member_id} if member_id else {}))
    payload.pop("appointment_ids", None)
    out = await dx.register_visit(scope, clinic, payload, user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_bookings_convert", "dx_accession", out["visit"]["id"],
                     {"bookings": body.appointment_ids, "number": out["visit"]["accession_number"]})
    return _filter_visit(user, out)


# ═══ Collection, labels, rejection, receipt (migration 128, DX-28) ═══════════

COLLECT = ("DX_COLLECT",)
LABELS = ("DX_COLLECT", "DX_REGISTER")
REJECT = ("DX_RECEIVE", "DX_COLLECT")
BARCODE = r"^[A-Za-z0-9._/-]{3,64}$"


class CollectSampleIn(_In):
    id: str
    barcode: Optional[str] = Field(None, pattern=BARCODE)
    volume_ml: Optional[float] = Field(None, gt=0, le=999)


class CollectIn(_In):
    samples: list[CollectSampleIn] = Field(..., min_length=1, max_length=30)
    collection_mode: Literal["centre", "home", "ward", "partner"] = "centre"
    idempotency_key: str = IDEM


class BindIn(_In):
    barcode: str = Field(..., pattern=BARCODE)


class RejectIn(_In):
    reason_code: str = Field(..., pattern=r"^[A-Za-z_]{3,30}$")
    notes: Optional[str] = Field(None, max_length=500)
    recollect: bool = True


class ReceiveIn(_In):
    barcode: str = Field(..., pattern=BARCODE)
    site_id: str


class LabelPrintIn(_In):
    sample_ids: list[str] = Field(default_factory=list, max_length=30)    # empty = every labellable tube
    mode: Literal["per_sample", "per_test", "register"] = "per_sample"
    format: Literal["pdf", "zpl"] = "pdf"
    copies: Optional[int] = Field(None, ge=1, le=10)                     # default 1 + extra_labels setting
    register_label: bool = False                                         # also the register-book label


async def _sample_visit(user: AdminUser, scope: str, sample_id: Optional[str] = None,
                        barcode: Optional[str] = None) -> tuple[dict, dict]:
    """The tube and its visit, inside the caller's site scope (404 otherwise)."""
    s = await dx.sample_of(scope, sample_id=sample_id, barcode=barcode)
    return s, await _visit(user, scope, s["accession_id"])


async def _user_site_ids(user: AdminUser, scope: str, site_id: Optional[str]) -> Optional[list[str]]:
    sites = await dx.list_sites(scope, _pinned_branch(user))
    if site_id:
        if site_id not in {s["id"] for s in sites}:
            raise HTTPException(status_code=404, detail="Site not found.")
        return [site_id]
    return [s["id"] for s in sites] if _pinned_branch(user) else None


@router.get("/collection/queue")
async def collection_queue(clinic_id: str = "default", site_id: Optional[str] = None,
                           priority: Optional[Literal["routine", "urgent", "stat"]] = None,
                           q: Optional[str] = Query(None, max_length=80),
                           date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *COLLECT)
    site_ids = await _user_site_ids(user, scope, site_id)
    mine = [{k: s.get(k) for k in ("id", "code", "display_name", "kind")}
            for s in await dx.list_sites(scope, _pinned_branch(user)) if s.get("is_active")]
    return {"visits": await dx.collection_queue(scope, site_ids, date, priority, q), "sites": mine}


@router.post("/visits/{accession_id}/labels/print")
async def print_labels(accession_id: str, body: LabelPrintIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    from fastapi.responses import Response
    from app.services import dx_barcode
    scope, clinic = await _dx_scope(user, clinic_id, *LABELS)
    v = await _visit(user, scope, accession_id)
    labels, register = await dx.label_data(scope, v, body.sample_ids, body.mode)
    copies = body.copies or 1 + int(dx.get_dx_settings(clinic).get("extra_labels") or 0)
    with_register = body.mode == "register" or body.register_label
    printed = {x["barcode"] for x in labels}
    sample_ids = sorted(s["id"] for s in v["samples"] if s.get("barcode") in printed)
    log = await dx.log_label_print(scope, accession_id, sample_ids, body.mode, copies, user)
    await _audit(user, request, "dx_labels_print", "dx_accession", accession_id,
                 {"mode": body.mode, "format": body.format, "labels": len(labels), "copies": copies,
                  "reprint": log["is_reprint"]})
    reg = register if with_register else None
    headers = {"X-Label-Print-Id": str(log["id"]), "X-Reprint": "true" if log["is_reprint"] else "false"}
    name = v["visit"]["accession_number"]
    if body.format == "zpl":
        return Response(dx_barcode.render_labels_zpl(labels, reg, copies), media_type="text/plain",
                        headers={**headers, "Content-Disposition": f'attachment; filename="{name}.zpl"'})
    return Response(dx_barcode.render_labels_pdf(labels, reg, copies), media_type="application/pdf",
                    headers={**headers, "Content-Disposition": f'inline; filename="{name}-labels.pdf"'})


@router.post("/samples/receive-direct")
async def receive_direct(body: ReceiveIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_RECEIVE")
    site = await dx.get_site(scope, body.site_id)
    enforce_branch_scope(user, site.get("branch_id"))
    await _sample_visit(user, scope, barcode=body.barcode)
    out = await dx.receive_direct(scope, body.barcode, body.site_id, user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_sample_receive", "dx_sample", out["id"], {"site_id": body.site_id})
    return out


@router.post("/samples/{sample_id}/bind")
async def bind_barcode(sample_id: str, body: BindIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *COLLECT)
    await _sample_visit(user, scope, sample_id=sample_id)
    out = await dx.bind_barcode(scope, sample_id, body.barcode, user)
    await _audit(user, request, "dx_sample_bind", "dx_sample", sample_id, {"barcode": body.barcode})
    return out


@router.post("/visits/{accession_id}/collect")
async def collect_samples(accession_id: str, body: CollectIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *COLLECT)
    await _visit(user, scope, accession_id)
    out = await dx.collect_samples(scope, accession_id, [s.model_dump(exclude_none=True) for s in body.samples],
                                   body.collection_mode, body.idempotency_key, user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_samples_collect", "dx_accession", accession_id,
                     {"samples": [s.id for s in body.samples], "mode": body.collection_mode})
    return _filter_visit(user, out)


@router.post("/samples/{sample_id}/reject")
async def reject_sample(sample_id: str, body: RejectIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    from app.utils.async_tasks import spawn_background_task
    scope, clinic = await _dx_scope(user, clinic_id, *REJECT)
    await _sample_visit(user, scope, sample_id=sample_id)
    out = await dx.reject_sample(scope, sample_id, body.reason_code, body.notes, body.recollect, user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_sample_reject", "dx_sample", sample_id,
                     {"reason_code": body.reason_code.upper(), "recollect": body.recollect,
                      "replacement_sample_id": out.get("replacement_sample_id")})
        if out.get("replacement_sample_id"):     # W7 step 3, off the request path
            spawn_background_task(dx.notify_recollection(clinic, out, out.get("reason_label") or body.reason_code),
                                  name="dx_recollection_notice")
    return _filter_visit(user, out)


@router.get("/samples/{barcode}/track")
async def track_sample(barcode: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *VISIT_READ, "DX_RECEIVE")
    await _sample_visit(user, scope, barcode=barcode)
    return await dx.track_sample(scope, barcode)


@router.get("/rejection-reasons")
async def list_rejection_reasons(clinic_id: str = "default", include_inactive: bool = False,
                                 user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *REJECT, "DX_ADMIN", "DX_READ")
    return {"reasons": await dx.rejection_reasons(scope, not include_inactive)}


# ═══ Home collection: doorstep money and the cashier handover (DX-29, AUDIT A14) ═

class FieldCollectionIn(_In):
    amount_paise: int = Field(..., gt=0, le=100_000_000)
    mode: Literal["cash", "upi_to_clinic"]
    reference: Optional[str] = Field(None, max_length=80)
    idempotency_key: str = IDEM


class HomeSamplesIn(_In):
    samples: list[CollectSampleIn] = Field(..., min_length=1, max_length=30)
    idempotency_key: str = IDEM


class HandoverIn(_In):
    shift_id: Optional[str] = None


async def _home_visit(user: AdminUser, clinic_id: str, appointment_id: str) -> tuple[str, str]:
    """A phlebotomist reaches only the home visits assigned to them (someone else's = 404);
    other staff need DX_COLLECT and the booking's branch."""
    from app.services import home_collection as hc
    from app.services.permissions import PHLEBOTOMIST
    from app.database import is_uuid
    if getattr(user, "staff_role", None) == PHLEBOTOMIST:
        scope, _ = await _dx_scope(user, clinic_id)
    else:
        scope, _ = await _dx_scope(user, clinic_id, *COLLECT)
    visit = await hc.get_visit(scope, appointment_id) if is_uuid(appointment_id) else None
    if not visit:
        raise HTTPException(status_code=404, detail="Visit not found.")
    if getattr(user, "staff_role", None) == PHLEBOTOMIST:
        if str(visit.get("phlebotomist_id") or "") != str(user.user_id):
            raise HTTPException(status_code=404, detail="Visit not found.")
    else:
        enforce_branch_scope(user, visit.get("branch_id"))
    return scope, await dx.home_visit_accession(scope, appointment_id)


@router.get("/home-collection/visits/{appointment_id}/samples")
async def home_visit_samples(appointment_id: str, clinic_id: str = "default",
                             user: AdminUser = Depends(verify_credentials)):
    scope, accession_id = await _home_visit(user, clinic_id, appointment_id)
    v = await dx.get_visit(scope, accession_id)
    return {"visit": {k: v["visit"].get(k) for k in ("id", "accession_number", "status", "priority", "fasting_status")},
            "samples": [{k: s.get(k) for k in ("id", "barcode", "barcode_source", "status", "container_code",
                                               "container_name", "colour_hex", "sample_type")} for s in v["samples"]],
            "due_paise": await dx.rpc("dx_visit_due", {"p_clinic_id": scope, "p_accession_id": accession_id}),
            "field_collections": await dx.field_collections(scope, None, [accession_id])}


@router.post("/home-collection/visits/{appointment_id}/samples")
async def home_visit_collect(appointment_id: str, body: HomeSamplesIn, request: Request, clinic_id: str = "default",
                             user: AdminUser = Depends(verify_credentials)):
    scope, accession_id = await _home_visit(user, clinic_id, appointment_id)
    out = await dx.collect_samples(scope, accession_id, [s.model_dump(exclude_none=True) for s in body.samples],
                                   "home", body.idempotency_key, user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_samples_collect", "dx_accession", accession_id,
                     {"samples": [s.id for s in body.samples], "mode": "home", "appointment_id": appointment_id})
    return {"visit": {k: out["visit"].get(k) for k in ("id", "accession_number", "status")},
            "samples": [{k: s.get(k) for k in ("id", "barcode", "status", "container_name")} for s in out["samples"]],
            "replayed": out.get("replayed", False)}


@router.post("/home-collection/visits/{appointment_id}/doorstep-cash", status_code=status.HTTP_201_CREATED)
async def doorstep_cash(appointment_id: str, body: FieldCollectionIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    if body.mode == "upi_to_clinic" and not (body.reference or "").strip():
        raise HTTPException(status_code=422, detail="A UPI payment needs its reference (UTR).")
    scope, accession_id = await _home_visit(user, clinic_id, appointment_id)
    out = await dx.record_field_collection(scope, accession_id, body.model_dump(), user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_field_collection", "dx_accession", accession_id,
                     {"amount_paise": body.amount_paise, "mode": body.mode, "appointment_id": appointment_id})
    return out


@router.get("/home-collection/field-collections")
async def list_field_collections(clinic_id: str = "default",
                                 status_: Optional[Literal["pending_handover", "handed_over", "disputed"]] =
                                 Query("pending_handover", alias="status"),
                                 user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_BILLING")
    rows = await dx.field_collections(scope, status_)
    if _pinned_branch(user) and rows:
        visits = {v["id"]: v for v in await dx._rows(scoped_query("dx_accessions", scope, "id, branch_id")
                                                     .in_("id", list({r["accession_id"] for r in rows})))}
        rows = [r for r in rows if str(visits.get(r["accession_id"], {}).get("branch_id")) == _pinned_branch(user)]
    return {"field_collections": rows}


@router.post("/home-collection/field-collections/{fc_id}/handover")
async def handover_field_collection(fc_id: str, body: HandoverIn, request: Request, clinic_id: str = "default",
                                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_BILLING")
    rows = await dx._rows(scoped_query("dx_field_collections", scope, "accession_id").eq("id", fc_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Field collection not found.")
    await _visit(user, scope, rows[0]["accession_id"])
    out = await dx_billing.handover_field_collection(scope, fc_id, user, body.shift_id)
    if not out.get("replayed"):
        await _audit(user, request, "dx_field_collection_handover", "dx_field_collection", fc_id,
                     {"receipt_id": out.get("receipt_id"), "amount_paise": out.get("amount_paise")})
    return out


# ═══ Logistics and outsourcing (migration 129, DX-32…34) ═════════════════════

UUID_RX = r"^[0-9a-fA-F-]{36}$"
LOGISTICS = ("DX_LOGISTICS",)
BATCH_READ = ("DX_LOGISTICS", "DX_RECEIVE")
BatchStatus = Literal["open", "dispatched", "received", "received_with_discrepancy", "cancelled"]


class PartnerIn(_In):
    name: str = Field(..., min_length=2, max_length=120)
    legal_name: Optional[str] = Field(None, max_length=200)
    address: Optional[str] = Field(None, max_length=500)
    registration_no: Optional[str] = Field(None, max_length=80)
    nabl_certificate_no: Optional[str] = Field(None, max_length=80)
    contact_phone: Optional[str] = Field(None, pattern=PHONE)


class PartnerPatch(PartnerIn):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    is_active: Optional[bool] = None


class RuleIn(_In):
    from_site_id: Optional[str] = Field(None, pattern=UUID_RX)
    lab_test_id: Optional[str] = Field(None, pattern=UUID_RX)
    department_id: Optional[str] = Field(None, pattern=UUID_RX)
    to_site_id: Optional[str] = Field(None, pattern=UUID_RX)
    outsource_partner_id: Optional[str] = Field(None, pattern=UUID_RX)
    priority: int = Field(100, ge=1, le=999)
    is_active: bool = True


class RulePreviewIn(_In):
    from_site_id: str = Field(..., pattern=UUID_RX)
    lab_test_ids: list[str] = Field(..., min_length=1, max_length=50)


class BatchIn(_In):
    from_site_id: Optional[str] = Field(None, pattern=UUID_RX)
    to_site_id: Optional[str] = Field(None, pattern=UUID_RX)
    outsource_partner_id: Optional[str] = Field(None, pattern=UUID_RX)
    carrier_name: Optional[str] = Field(None, max_length=100)
    temp_c: Optional[float] = Field(None, ge=-90, le=60)


class BatchScanIn(_In):
    barcodes: list[str] = Field(..., min_length=1, max_length=200)


class DispatchIn(_In):
    temp_c: Optional[float] = Field(None, ge=-90, le=60)
    notes: Optional[str] = Field(None, max_length=500)


class ReceiveBatchIn(_In):
    barcodes: list[str] = Field(default_factory=list, max_length=500)
    temp_c: Optional[float] = Field(None, ge=-90, le=60)
    discrepancy_notes: Optional[str] = Field(None, max_length=500)


def _one_target(body: BaseModel) -> None:
    if (body.to_site_id is None) == (body.outsource_partner_id is None):
        raise HTTPException(status_code=422, detail="Choose one processing site or one outsource partner.")


# ── outsource partners (DX-34) ──────────────────────────────────────────────

@router.get("/outsource-partners")
async def list_outsource_partners(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", *LOGISTICS)
    return {"partners": await dx.list_partners(scope)}


@router.post("/outsource-partners", status_code=status.HTTP_201_CREATED)
async def create_outsource_partner(body: PartnerIn, request: Request, clinic_id: str = "default",
                                   user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", new_work=True)
    _clinic_wide(user)
    out = await dx.save_config_row("dx_outsource_partners", scope, body.model_dump(exclude_none=True))
    await _audit(user, request, "dx_partner_create", "dx_outsource_partner", out["id"], {"name": out["name"]})
    return out


@router.put("/outsource-partners/{partner_id}")
async def update_outsource_partner(partner_id: str, body: PartnerPatch, request: Request, clinic_id: str = "default",
                                   user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    _clinic_wide(user)
    changes = body.model_dump(exclude_unset=True)
    if not changes or changes.get("name", "") is None:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    out = await dx.save_config_row("dx_outsource_partners", scope, changes, partner_id)
    await _audit(user, request, "dx_partner_update", "dx_outsource_partner", partner_id, {"fields": sorted(changes)})
    return out


# ── routing rules (DX-32) ───────────────────────────────────────────────────

@router.get("/routing-rules")
async def list_routing_rules(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    return {"rules": await dx.list_routing_rules(scope)}


@router.post("/routing-rules", status_code=status.HTTP_201_CREATED)
async def create_routing_rule(body: RuleIn, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", new_work=True)
    _clinic_wide(user)
    _one_target(body)
    data = body.model_dump()
    await dx.check_rule_target(scope, data)
    out = await dx.save_config_row("dx_routing_rules", scope, data)
    await _audit(user, request, "dx_routing_rule_create", "dx_routing_rule", out["id"], data)
    return out


@router.put("/routing-rules/{rule_id}")
async def update_routing_rule(rule_id: str, body: RuleIn, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    _clinic_wide(user)
    _one_target(body)
    data = body.model_dump()          # a rule is replaced whole: the match and the target belong together
    await dx.check_rule_target(scope, data)
    out = await dx.save_config_row("dx_routing_rules", scope, data, rule_id)
    await _audit(user, request, "dx_routing_rule_update", "dx_routing_rule", rule_id, data)
    return out


@router.delete("/routing-rules/{rule_id}")
async def delete_routing_rule(rule_id: str, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    _clinic_wide(user)
    await dx.delete_routing_rule(scope, rule_id)
    await _audit(user, request, "dx_routing_rule_delete", "dx_routing_rule", rule_id)
    return {"deleted": True}


@router.post("/routing-rules/preview")
async def preview_routing(body: RulePreviewIn, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    await dx.get_site(scope, body.from_site_id)
    return {"routes": await dx.preview_routing(scope, body.from_site_id, body.lab_test_ids)}


# ── batches (DX-33, W6) ─────────────────────────────────────────────────────

async def _batch_for(user: AdminUser, scope: str, batch_id: str) -> dict:
    """Site scope: a pinned user reaches batches leaving or arriving at their site (404 otherwise)."""
    b = await dx.get_batch(scope, batch_id)
    mine = await _user_site_ids(user, scope, None)
    if mine is not None and b["from_site_id"] not in mine and b.get("to_site_id") not in mine:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return b


@router.get("/batches")
async def list_batches(clinic_id: str = "default", from_site: Optional[str] = Query(None, pattern=UUID_RX),
                       to_site: Optional[str] = Query(None, pattern=UUID_RX), status_: Optional[BatchStatus] =
                       Query(None, alias="status"), user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *BATCH_READ)
    mine = await _user_site_ids(user, scope, None)
    sites = [{k: s.get(k) for k in ("id", "code", "display_name", "kind", "branch_id")}
             for s in await dx.list_sites(scope) if s.get("is_active")]
    return {"batches": await dx.list_batches(scope, mine, from_site, to_site, status_),
            "my_site_ids": mine, "sites": sites,
            "partners": [{"id": p["id"], "name": p["name"]} for p in await dx.list_partners(scope, active_only=True)]}


@router.post("/batches", status_code=status.HTTP_201_CREATED)
async def create_batch(body: BatchIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *LOGISTICS, new_work=True)
    _one_target(body)
    mine = await _user_site_ids(user, scope, None)
    from_site = body.from_site_id or (mine[0] if mine and len(mine) == 1 else None)
    if not from_site:
        raise HTTPException(status_code=422, detail="Choose the site this batch leaves from.")
    if mine is not None and from_site not in mine:
        raise HTTPException(status_code=404, detail="Site not found.")
    out = await dx.create_batch(scope, {**body.model_dump(), "from_site_id": from_site}, user)
    await _audit(user, request, "dx_batch_create", "dx_batch", out["id"],
                 {"number": out["batch_number"], "to_site_id": body.to_site_id,
                  "outsource_partner_id": body.outsource_partner_id})
    return out


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *BATCH_READ)
    return await _batch_for(user, scope, batch_id)


@router.post("/batches/{batch_id}/samples")
async def add_batch_samples(batch_id: str, body: BatchScanIn, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *LOGISTICS)
    await _batch_for(user, scope, batch_id)
    out = await dx.batch_add(scope, batch_id, body.barcodes, user)
    if out.get("added"):
        await _audit(user, request, "dx_batch_add", "dx_batch", batch_id, {"barcodes": body.barcodes,
                                                                           "added": out["added"]})
    return out


@router.post("/batches/{batch_id}/dispatch")
async def dispatch_batch(batch_id: str, body: DispatchIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *LOGISTICS)
    await _batch_for(user, scope, batch_id)
    out = await dx.dispatch_batch(scope, batch_id, body.temp_c, body.notes, user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_batch_dispatch", "dx_batch", batch_id,
                     {"temp_c": body.temp_c, "samples": len(out.get("samples", []))})
    return out


@router.post("/batches/{batch_id}/receive")
async def receive_batch(batch_id: str, body: ReceiveBatchIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    """At the destination site (DX_RECEIVE); a partner batch is handed over by its sending site."""
    scope, _ = await _dx_scope(user, clinic_id, "DX_RECEIVE", *LOGISTICS)
    b = await _batch_for(user, scope, batch_id)
    at = b.get("to_site_id") or b["from_site_id"]
    mine = await _user_site_ids(user, scope, None)
    if mine is not None and at not in mine:
        raise HTTPException(status_code=403, detail="Only the receiving site can receive this batch.")
    if b.get("to_site_id") and not holds_dx_permission(user, "DX_RECEIVE"):
        raise HTTPException(status_code=403, detail="Missing permission: DX_RECEIVE")
    out = await dx.receive_batch(scope, batch_id, body.barcodes, body.temp_c, body.discrepancy_notes, user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_batch_receive", "dx_batch", batch_id,
                     {"temp_c": body.temp_c, "status": out["status"], **(out.get("discrepancy") or {})})
    return out


# ═══ Bench: worklist, entry, verification, approval (migration 130, DX-38/39) ═

BENCH_READ = (*VISIT_READ, "DX_RECEIVE", "DX_VERIFY")
Stage = Literal["awaiting_payment", "pending_collection", "pending_receipt", "pending_entry", "pending_verification",
                "pending_approval", "pending_release", "released", "cancelled"]


def _lab_perm(user: AdminUser, *perms: str) -> None:
    from app.services.permissions import holds_lab_permission
    if not any(holds_lab_permission(user, p) if p.startswith("LAB_") else holds_dx_permission(user, p) for p in perms):
        raise HTTPException(status_code=403, detail=f"Missing permission: {' or '.join(perms)}")


async def _dx_order(user: AdminUser, scope: str, order_id: str) -> dict:
    """A DX order inside the caller's site scope (404 / 409 otherwise)."""
    from app.services import orders as orders_svc
    order = await orders_svc.get_order(scope, order_id)
    if not order.get("accession_id"):            # source 'dx', or an adopted OPD / IPD order (P6b)
        raise HTTPException(status_code=409, detail={"code": "dx_not_dx_order",
                                                     "message": "This is not a Diagnostic OS order."})
    await _visit(user, scope, order["accession_id"])
    return order


async def orders_svc_get(scope: str, order_id: str) -> dict:
    from app.services import orders as orders_svc
    return await orders_svc.get_order(scope, order_id)


async def _dx_result(user: AdminUser, scope: str, result_id: str) -> dict:
    from app.services import lis
    result = await lis.get_result(scope, result_id)
    await _dx_order(user, scope, result["order_id"])
    return result


class ValueIn(_In):
    parameter_id: str = Field(..., pattern=UUID_RX)
    value: Optional[str] = Field(None, max_length=500)
    remarks: Optional[str] = Field(None, max_length=500)


class WorksheetIn(_In):
    values: list[ValueIn] = Field(..., min_length=1, max_length=100)
    amendment_reason: Optional[str] = Field(None, max_length=500)
    preview: bool = False


class VerifyIn(_In):
    expected_updated_at: str = Field(..., min_length=10, max_length=40)
    decision: Literal["verified", "returned"]
    comment: Optional[str] = Field(None, max_length=500)


class ApproveItemIn(_In):
    result_id: str = Field(..., pattern=UUID_RX)
    expected_updated_at: str = Field(..., min_length=10, max_length=40)


class ApproveIn(_In):
    items: list[ApproveItemIn] = Field(..., min_length=1, max_length=50)
    batch: bool = False


class CriticalCallIn(_In):
    notified_to: str = Field(..., min_length=2, max_length=120)
    notified_role: Literal["referrer", "patient", "ward", "account", "other"]
    channel: Literal["phone", "whatsapp", "in_person"]
    read_back_confirmed: bool
    notes: Optional[str] = Field(None, max_length=500)


@router.get("/worklist")
async def bench_worklist(clinic_id: str = "default", stage: Optional[Stage] = None,
                         site_id: Optional[str] = Query(None, pattern=UUID_RX),
                         department_id: Optional[str] = Query(None, pattern=UUID_RX),
                         priority: Optional[Literal["stat", "urgent", "routine"]] = None,
                         date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                         open_only: bool = False, user: AdminUser = Depends(verify_credentials)):
    """Tests by stage. `open_only` = every open visit of the last 7 days (else the day's visits)."""
    scope, clinic = await _dx_scope(user, clinic_id)
    _lab_perm(user, *BENCH_READ, "LAB_ENTRY", "LAB_VALIDATE")
    sites = await _user_site_ids(user, scope, site_id)
    visits = await dx.open_visits(scope) if open_only else await dx.list_visits(scope, None, date, None, None, 500)
    out = await dx.worklist(scope, visits, sites, department_id, priority, stage)
    out["sites"] = [{k: s.get(k) for k in ("id", "code", "display_name", "kind")}
                    for s in await dx.list_sites(scope, _pinned_branch(user)) if s.get("is_active")]
    out["departments"] = [{"id": d["id"], "name": d["name"]}
                          for d in await dx_catalogue.list_rows("dx_departments", scope)]
    out["approval_levels"] = int(dx.get_dx_settings(clinic).get("approval_levels", 2))
    return out


@router.get("/orders/{order_id}/worksheet")
async def bench_worksheet(order_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id)
    _lab_perm(user, "LAB_ENTRY", "LAB_VALIDATE", "DX_VERIFY")
    await _dx_order(user, scope, order_id)
    return await dx_results.worksheet(scope, order_id)


@router.post("/orders/{order_id}/results")
async def bench_save_results(order_id: str, body: WorksheetIn, request: Request, clinic_id: str = "default",
                             user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id)
    _lab_perm(user, "LAB_ENTRY")
    await _dx_order(user, scope, order_id)
    out = await dx_results.enter_worksheet_results(scope, order_id, [v.model_dump() for v in body.values], user,
                                                   body.amendment_reason, body.preview)
    if out.get("saved"):
        await _audit(user, request, "dx_results_enter", "lab_result", out["id"],
                     {"order_id": order_id, "version": out["version"], "critical": out["has_critical_value"],
                      "values": len(out["values"])})
    return out


@router.get("/results/{result_id}")
async def bench_result(result_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id)
    _lab_perm(user, "LAB_ENTRY", "LAB_VALIDATE", "DX_VERIFY")
    await _dx_result(user, scope, result_id)
    return await dx_results.result_detail(scope, result_id)


@router.post("/results/approve")
async def bench_approve(body: ApproveIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    """Approve & sign: one result or a batch of normal ones (W9). Human, doctor-linked only."""
    scope, clinic = await _dx_scope(user, clinic_id)
    _lab_perm(user, "LAB_VALIDATE")
    for it in body.items:
        await _dx_result(user, scope, it.result_id)
    out = await dx_results.approve_results(scope, [i.model_dump() for i in body.items], user, body.batch)
    await _audit(user, request, "dx_results_approve", "lab_result", None,
                 {"batch": body.batch, "signed": out["signed"], "refused": out["refused"],
                  "items": [{"result_id": i["result_id"], "ok": i["ok"], "code": i.get("code")} for i in out["items"]]})
    signed = [i["result_id"] for i in out["items"] if i["ok"] and not i.get("replayed")]
    if signed:      # W10 step 1: auto release per settings.auto_release (never while the connector delivers)
        from app.services import dx_reports, lis
        visits = [(await orders_svc_get(scope, (await lis.get_result(scope, rid))["order_id"])).get("accession_id")
                  for rid in signed]
        rels = await dx_reports.auto_release(scope, clinic, [v for v in visits if v], user)
        out["releases"] = [{"id": r["id"], "accession_id": r["accession_id"], "release_no": r["release_no"],
                            "kind": r["kind"]} for r in rels]
    return out


@router.post("/results/{result_id}/verify")
async def bench_verify(result_id: str, body: VerifyIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_VERIFY")
    await _dx_result(user, scope, result_id)
    out = await dx_results.verify_result(scope, result_id, body.expected_updated_at, body.decision, body.comment, user)
    await _audit(user, request, f"dx_result_{body.decision}", "lab_result", result_id, {"comment": body.comment})
    return out


@router.post("/critical-events/{event_id}/call", status_code=status.HTTP_201_CREATED)
async def bench_critical_call(event_id: str, body: CriticalCallIn, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    """W8 step 6 / AUDIT A26: who was told about a critical value, how, and the read-back."""
    scope, _ = await _dx_scope(user, clinic_id)
    _lab_perm(user, "DX_VERIFY", "LAB_VALIDATE", "LAB_ENTRY")
    rows = await dx._rows(scoped_query("dx_critical_events", scope, "result_id").eq("id", event_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Critical value record not found.")
    await _dx_result(user, scope, rows[0]["result_id"])
    out = await dx_results.log_critical_call(scope, event_id, body.notified_to, body.notified_role, body.channel,
                                             body.read_back_confirmed, body.notes, user)
    await _audit(user, request, "dx_critical_call", "dx_critical_event", out["id"],
                 {"result_id": rows[0]["result_id"], "role": body.notified_role, "channel": body.channel,
                  "read_back": body.read_back_confirmed})
    return out


@router.post("/results/{result_id}/attachments", status_code=status.HTTP_201_CREATED)
async def bench_attach(result_id: str, request: Request, file: UploadFile = File(...),
                       kind: Literal["graph", "image", "pdf"] = Query("pdf"),
                       caption: Optional[str] = Query(None, max_length=200), clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id)
    _lab_perm(user, "LAB_ENTRY")
    await _dx_result(user, scope, result_id)
    raw = await file.read(dx_results.ATTACHMENT_MAX_BYTES + 1)
    out = await dx_results.store_attachment(scope, result_id, raw, file.filename or "attachment", kind, caption, user)
    await _audit(user, request, "dx_result_attach", "lab_result", result_id, {"attachment_id": out["id"], "kind": kind})
    return out


@router.get("/results/{result_id}/attachments/{attachment_id}")
async def bench_attachment(result_id: str, attachment_id: str, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    from fastapi.responses import Response
    scope, _ = await _dx_scope(user, clinic_id)
    _lab_perm(user, "LAB_ENTRY", "LAB_VALIDATE", "DX_VERIFY")
    await _dx_result(user, scope, result_id)
    data, row = await dx_results.attachment_file(scope, result_id, attachment_id)
    media = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg"}.get(row["storage_path"].rsplit(".", 1)[-1],
                                                                                 "application/octet-stream")
    return Response(data, media_type=media, headers={"Content-Disposition": f'inline; filename="{row["sha256"][:12]}"',
                                                     "Cache-Control": "no-store"})


# ── P6b: OPD / IPD lab orders on the DX bench (W15, DX-41) ─────────────────

class AdoptIn(_In):
    order_ids: list[UUID] = Field(..., min_length=1, max_length=100)


@router.post("/orders/adopt")
async def adopt_orders(body: AdoptIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    """Move uncollected OPD / IPD lab orders onto the DX bench as internal visits (bills untouched)."""
    from app.services import orders as orders_svc
    scope, _ = await _dx_scope(user, clinic_id, "DX_REGISTER", "DX_COLLECT", new_work=True)
    ids = list(dict.fromkeys(str(x) for x in body.order_ids))
    for oid in ids:
        enforce_branch_scope(user, (await orders_svc.get_order(scope, oid)).get("branch_id"))
    out = await dx.adopt_orders(scope, ids, user)
    await _audit(user, request, "dx_orders_adopt", "dx_accession", None,
                 {"orders": ids, "visits": [v["visit"]["id"] for v in out["visits"]],
                  "adopted": out["adopted"]})
    return out



# ── P7: report templates (DX-44) ────────────────────────────────────────────

TEMPLATE_READ = ("DX_ADMIN", "DX_DISPATCH")
OwnerKind = Literal["clinic", "branch", "b2b_account"]


class TemplateIn(_In):
    owner_kind: OwnerKind = "clinic"
    owner_id: Optional[UUID] = None
    name: str = Field(..., min_length=2, max_length=80)
    starter: Literal["classic_band", "minimal", "hospital_white_label"] = "classic_band"
    layout: dict = Field(default_factory=dict)


class TemplatePatch(_In):
    layout: dict


class PreviewIn(_In):
    layout: Optional[dict] = None


def _template_reach(user: AdminUser, owner_kind: str, owner_id: Optional[str], write: bool) -> None:
    """A branch-pinned admin manages its own branch's templates; reads the clinic default too."""
    pinned = _pinned_branch(user)
    if not pinned:
        return
    if owner_kind == "branch" and str(owner_id) == pinned:
        return
    if not write and owner_kind == "clinic":
        return
    raise HTTPException(status_code=404 if not write else 403, detail="Template not found." if not write else
                        "A branch administrator manages only their own branch's templates.")


async def _letterhead(scope: str, clinic: dict, user: AdminUser, white_label: bool) -> dict:
    sites = await dx.list_sites(scope, _pinned_branch(user))
    site = sites[0] if sites else {}
    return {"clinic": {"name": clinic.get("name"), "address": clinic.get("address"), "phone": clinic.get("phone")},
            "site": {k: site.get(k) for k in ("display_name", "legal_name", "registration_no", "nabl_certificate_no")},
            "white_label": white_label}


@router.get("/templates")
async def list_templates(clinic_id: str = "default", owner_kind: Optional[OwnerKind] = None,
                         user: AdminUser = Depends(verify_credentials)):
    from app.services import dx_templates
    scope, _ = await _dx_scope(user, clinic_id, *TEMPLATE_READ)
    return {"templates": await dx_templates.list_templates(scope, owner_kind, _pinned_branch(user)),
            "starters": list(dx_templates.STARTERS)}


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_template(body: TemplateIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    from app.services import dx_templates
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", new_work=True)
    owner = str(body.owner_id) if body.owner_id else None
    _template_reach(user, body.owner_kind, owner, write=True)
    out = await dx_templates.create_template(scope, {**body.model_dump(), "owner_id": owner}, dx._actor(user)[0])
    await _audit(user, request, "dx_template_create", "dx_report_template", out["id"],
                 {"owner_kind": body.owner_kind, "owner_id": owner, "name": out["name"], "version": out["version"]})
    return out


async def _template(user: AdminUser, scope: str, template_id: str, write: bool) -> dict:
    from app.services import dx_templates
    t = await dx_templates.get_template(scope, template_id)
    _template_reach(user, t["owner_kind"], t.get("owner_id"), write)
    return t


@router.get("/templates/{template_id}")
async def get_template(template_id: UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *TEMPLATE_READ)
    return await _template(user, scope, str(template_id), write=False)


@router.put("/templates/{template_id}")
async def update_template(template_id: UUID, body: TemplatePatch, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    from app.services import dx_templates
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", new_work=True)
    await _template(user, scope, str(template_id), write=True)
    out = await dx_templates.update_draft(scope, str(template_id), body.layout)
    await _audit(user, request, "dx_template_update", "dx_report_template", str(template_id), {})
    return out


@router.post("/templates/{template_id}/publish")
async def publish_template(template_id: UUID, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    from app.services import dx_templates
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", new_work=True)
    await _template(user, scope, str(template_id), write=True)
    out = await dx_templates.publish(scope, str(template_id), dx._actor(user)[0])
    await _audit(user, request, "dx_template_publish", "dx_report_template", str(template_id),
                 {"name": out["name"], "version": out["version"]})
    return out


@router.post("/templates/{template_id}/retire")
async def retire_template(template_id: UUID, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    from app.services import dx_templates
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    await _template(user, scope, str(template_id), write=True)
    out = await dx_templates.retire(scope, str(template_id))
    await _audit(user, request, "dx_template_retire", "dx_report_template", str(template_id), {})
    return out


@router.post("/templates/{template_id}/preview")
async def preview_template(template_id: UUID, body: Optional[PreviewIn] = None, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    """"Test with Sample Visit": the sample HbA1c + lipid visit rendered with this (or the edited) layout."""
    from fastapi.responses import Response
    from app.services import dx_templates
    scope, clinic = await _dx_scope(user, clinic_id, *TEMPLATE_READ)
    t = await _template(user, scope, str(template_id), write=False)
    layout = (body.layout if body and body.layout is not None else t["layout"])
    pdf = await dx_templates.preview(scope, layout, await _letterhead(scope, clinic, user,
                                                                      t["owner_kind"] == "b2b_account"))
    return Response(pdf, media_type="application/pdf", headers={"Cache-Control": "no-store",
                                                                "Content-Disposition": 'inline; filename="preview.pdf"'})


@router.post("/templates/assets", status_code=status.HTTP_201_CREATED)
async def upload_template_asset(request: Request, file: UploadFile = File(...), clinic_id: str = "default",
                                user: AdminUser = Depends(verify_credentials)):
    from app.services import dx_templates
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", new_work=True)
    out = await dx_templates.store_asset(scope, await file.read(dx_templates.ASSET_MAX_BYTES + 1))
    await _audit(user, request, "dx_template_asset", "dx_report_template", None, {"path": out["path"]})
    return out


# ── P7: releases, deliveries, overrides, print, share links (DX-45) ─────────

DISPATCH = ("DX_DISPATCH",)
DISPATCH_READ = ("DX_DISPATCH", "DX_READ", "DX_ADMIN")
DeliveryStatus = r"^(pending|held_dues|held_shadow|blocked|queued|sending|sent|failed|deferred)(,(pending|held_dues|" \
                 r"held_shadow|blocked|queued|sending|sent|failed|deferred))*$"


class ReleaseIn(_In):
    result_ids: Optional[list[UUID]] = Field(None, min_length=1, max_length=100)
    kind: Optional[Literal["partial", "final", "amended"]] = None


class OverrideIn(_In):
    reason: str = Field(..., min_length=10, max_length=300)
    expires_at: Optional[datetime] = None


async def _release(user: AdminUser, scope: str, release_id: str) -> dict:
    from app.services import dx_reports
    rel = await dx_reports.get_release(scope, release_id)
    await _visit(user, scope, rel["accession_id"])          # site scope
    return rel


@router.get("/visits/{accession_id}/releases")
async def visit_releases(accession_id: UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    from app.services import dx_reports
    scope, _ = await _dx_scope(user, clinic_id, *DISPATCH_READ)
    await _visit(user, scope, str(accession_id))
    return {"releases": await dx_reports.list_releases(scope, str(accession_id)),
            "dues_paise": await dx_reports.visit_dues_paise(scope, str(accession_id))}


@router.post("/visits/{accession_id}/releases", status_code=status.HTTP_201_CREATED)
async def create_release(accession_id: UUID, request: Request, body: Optional[ReleaseIn] = None,
                         clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    """Release now (W10): signed results into a visit report; render, store and send per policy."""
    from app.services import dx_reports
    scope, _ = await _dx_scope(user, clinic_id, *DISPATCH)
    await _visit(user, scope, str(accession_id))
    body = body or ReleaseIn()
    out = await dx_reports.create_release(scope, str(accession_id),
                                          [str(x) for x in body.result_ids] if body.result_ids else None, body.kind, user)
    await _audit(user, request, "dx_release_create", "dx_report_release", out["id"],
                 {"accession_id": str(accession_id), "release_no": out["release_no"], "kind": out["kind"],
                  "deliveries": [{"target": d["target"], "status": d["status"], "reason": d["reason"]}
                                 for d in out["deliveries"]]})
    return out


@router.post("/releases/{release_id}/render")
async def rerender_release(release_id: UUID, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    """Retry a failed render (the release content is fixed; same input, same PDF)."""
    from app.services import dx_reports
    from app.utils.async_tasks import spawn_background_task
    scope, _ = await _dx_scope(user, clinic_id, *DISPATCH)
    rel = await _release(user, scope, str(release_id))
    if rel["status"] != "render_failed":
        raise HTTPException(status_code=409, detail="Only a failed render can be retried.")
    spawn_background_task(dx_reports.finish_release(scope, rel["id"]), name=f"dx_release_{rel['id']}")
    await _audit(user, request, "dx_release_rerender", "dx_report_release", rel["id"], {})
    return {"status": "rendering"}


@router.get("/releases/{release_id}/pdf")
async def release_pdf(release_id: UUID, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    """Staff clinical viewing (audit-logged; "INTERNAL VIEW" when a patient copy would be refused)."""
    from fastapi.responses import Response
    from app.services import dx_reports
    scope, clinic = await _dx_scope(user, clinic_id, *DISPATCH_READ, "DX_VERIFY")
    rel = await _release(user, scope, str(release_id))
    pdf, mark = await dx_reports.staff_pdf(scope, clinic, rel)
    await _audit(user, request, "dx_release_view", "dx_report_release", rel["id"], {"watermark": mark})
    return Response(pdf, media_type="application/pdf", headers={
        "Cache-Control": "no-store", "Content-Disposition": f'inline; filename="R{rel["release_no"]}.pdf"'})


@router.post("/releases/{release_id}/print")
async def print_release(release_id: UUID, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    """Desk print for the patient: refused (409 held_dues) while dues exist unless an override."""
    from fastapi.responses import Response
    from app.services import dx_reports
    scope, _ = await _dx_scope(user, clinic_id, *DISPATCH)
    rel = await _release(user, scope, str(release_id))
    pdf, out = await dx_reports.print_release(scope, rel, user)
    await _audit(user, request, "dx_release_print", "dx_report_release", rel["id"],
                 {"print_id": out.get("print_id"), "override_id": out.get("override_id"),
                  "watermark": out.get("watermark")})
    return Response(pdf, media_type="application/pdf", headers={
        "Cache-Control": "no-store", "Content-Disposition": f'inline; filename="R{rel["release_no"]}.pdf"'})


@router.post("/releases/{release_id}/share-link", status_code=status.HTTP_201_CREATED)
async def share_release(release_id: UUID, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    from app.services import dx_reports
    scope, clinic = await _dx_scope(user, clinic_id, *DISPATCH)
    rel = await _release(user, scope, str(release_id))
    out = await dx_reports.create_share_link(scope, clinic, rel, user)
    await _audit(user, request, "dx_share_link_create", "dx_report_release", rel["id"],
                 {"link_id": out["id"], "expires_at": out["expires_at"]})
    return out


@router.get("/deliveries")
async def list_deliveries(clinic_id: str = "default", status_: Optional[str] = Query(None, alias="status",
                                                                                     pattern=DeliveryStatus),
                          target: Optional[Literal["patient", "referrer", "account"]] = None,
                          date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                          user: AdminUser = Depends(verify_credentials)):
    """Reports & Dispatch tabs. Rows carry the visit number, patient and, when held for dues, the amount."""
    from app.services import dx_reports
    from app.utils.helpers import IST
    scope, _ = await _dx_scope(user, clinic_id, *DISPATCH_READ)
    since = None
    if date:
        since = datetime.fromisoformat(date).replace(tzinfo=IST).astimezone(timezone.utc).isoformat()
    visits = None
    if _pinned_branch(user):
        visits = [v["id"] for v in await dx.list_visits(scope, _pinned_branch(user), None, None, None, 500)]
    rows = await dx_reports.list_deliveries(scope, status_, target, since, visits)
    vids = list({r["accession_id"] for r in rows})
    info = {v["id"]: v for v in await dx_reports._in("dx_accessions", scope, "id, accession_number, patient_snapshot, "
                                                     "billing_mode", "id", vids)}
    rels = {r["id"]: r for r in await dx_reports._in("dx_report_releases", scope, "id, release_no, kind, status",
                                                     "id", [r["release_id"] for r in rows])}
    dues = {vid: await dx_reports.visit_dues_paise(scope, vid)
            for vid in {r["accession_id"] for r in rows if r["status"] == "held_dues"}}
    for r in rows:
        v = info.get(r["accession_id"], {})
        r["accession_number"] = v.get("accession_number")
        r["patient_name"] = (v.get("patient_snapshot") or {}).get("name")
        r["release_no"] = (rels.get(r["release_id"]) or {}).get("release_no")
        r["release_status"] = (rels.get(r["release_id"]) or {}).get("status")
        r["reason_text"] = dx_reports.HELD_TEXT.get(r.get("reason") or "", r.get("reason"))
        if r["status"] == "held_dues":
            r["dues_paise"] = dues.get(r["accession_id"], 0)
    return {"deliveries": rows}


@router.get("/prints")
async def list_prints(clinic_id: str = "default", date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                      user: AdminUser = Depends(verify_credentials)):
    """The desk print log (Reports & Dispatch, Printed tab)."""
    from app.services import dx_reports
    from app.utils.helpers import IST
    scope, _ = await _dx_scope(user, clinic_id, *DISPATCH_READ)
    q = scoped_query("dx_print_log", scope, "id, release_id, accession_id, printed_by_name, watermark, override_id, "
                                            "created_at")
    if date:
        q = q.gte("created_at", datetime.fromisoformat(date).replace(tzinfo=IST).astimezone(timezone.utc).isoformat())
    if _pinned_branch(user):
        q = q.in_("accession_id", [v["id"] for v in await dx.list_visits(scope, _pinned_branch(user), None, None, None,
                                                                         500)] or [str(UUID(int=0))])
    rows = (await sb(q.order("created_at", desc=True).limit(200))).data or []
    info = {v["id"]: v for v in await dx_reports._in("dx_accessions", scope, "id, accession_number, patient_snapshot",
                                                     "id", [r["accession_id"] for r in rows])}
    rels = {r["id"]: r for r in await dx_reports._in("dx_report_releases", scope, "id, release_no", "id",
                                                     [r["release_id"] for r in rows])}
    for r in rows:
        r["accession_number"] = info.get(r["accession_id"], {}).get("accession_number")
        r["patient_name"] = (info.get(r["accession_id"], {}).get("patient_snapshot") or {}).get("name")
        r["release_no"] = rels.get(r["release_id"], {}).get("release_no")
    return {"prints": rows}


@router.post("/deliveries/{delivery_id}/resend")
async def resend_delivery(delivery_id: UUID, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    from app.services import dx_reports
    scope, _ = await _dx_scope(user, clinic_id, *DISPATCH)
    d = await dx_reports.get_delivery(scope, str(delivery_id))
    await _visit(user, scope, d["accession_id"])
    out = await dx_reports.resend(scope, str(delivery_id))
    await _audit(user, request, "dx_delivery_resend", "dx_release_delivery", str(delivery_id),
                 {"target": d["target"], "status": out["status"], "reason": out.get("reason")})
    return out


@router.post("/visits/{accession_id}/delivery-overrides", status_code=status.HTTP_201_CREATED)
async def create_delivery_override(accession_id: UUID, body: OverrideIn, request: Request, clinic_id: str = "default",
                                   user: AdminUser = Depends(verify_credentials)):
    """DELIVERY-POLICY §6: DX_ADMIN / clinic admin only, reason >= 10 characters, rule 7 only."""
    from app.services import dx_reports
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    await _visit(user, scope, str(accession_id))
    out = await dx_reports.create_override(scope, str(accession_id), body.reason.strip(), user,
                                           body.expires_at.isoformat() if body.expires_at else None)
    await _audit(user, request, "dx_delivery_override", "dx_accession", str(accession_id),
                 {"override_id": out["id"], "reason": out["reason"], "expires_at": out["expires_at"]})
    return out


# ── B2B money (migration 132, DX-49…DX-52) ──────────────────────────────────

B2B = ("DX_B2B_MANAGE",)
B2B_READ = ("DX_B2B_MANAGE", "DX_REPORTS_MIS")
CSV_MAX_BYTES = 1024 * 1024


class RateCardIn(_In):
    name: str = Field(..., min_length=2, max_length=80)
    kind: Literal["b2b", "corporate", "camp", "insurance"] = "b2b"
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    is_active: bool = True


class RateCardPatch(_In):
    name: Optional[str] = Field(None, min_length=2, max_length=80)
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    is_active: Optional[bool] = None


class RateItemIn(_In):
    lab_test_id: str = Field(..., pattern=UUID_RX)
    price_paise: Optional[int] = Field(None, ge=0, le=100_000_000)     # null removes the test from the card


class RateItemsIn(_In):
    items: list[RateItemIn] = Field(..., min_length=1, max_length=2000)


class PaymentIn(_In):
    amount_paise: int = Field(..., gt=0, le=10_000_000_000)
    mode: Literal["neft", "upi", "cheque", "cash", "card", "gateway"]
    reference: Optional[str] = Field(None, max_length=80)
    idempotency_key: str = IDEM


class StatementIn(_In):
    period_from: date
    period_to: date


class HoldIn(_In):
    status: Literal["active", "on_hold"]
    reason: Optional[str] = Field(None, max_length=300)


class StockIn(_In):
    account_id: Optional[str] = Field(None, pattern=UUID_RX)
    site_id: Optional[str] = Field(None, pattern=UUID_RX)
    prefix: str = Field("", pattern=r"^[A-Z0-9]{0,12}$")
    range_from: int = Field(..., ge=0, le=999_999_999_999_999_999)
    range_to: int = Field(..., ge=0, le=999_999_999_999_999_999)


class PartnerUserIn(_In):
    username: str = Field(..., min_length=3, max_length=60, pattern=r"^[A-Za-z0-9._@-]+$")
    password: str = Field(..., min_length=8, max_length=200)
    full_name: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = Field(None, pattern=PHONE)


# rate cards

@router.get("/rate-cards")
async def list_rate_cards(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B_READ, "DX_BILLING")
    return {"rate_cards": await dx_b2b.list_rate_cards(scope)}


@router.post("/rate-cards", status_code=status.HTTP_201_CREATED)
async def create_rate_card(body: RateCardIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B, new_work=True)
    row = await dx_b2b.save_rate_card(scope, body.model_dump(exclude_none=True, mode="json"))
    await _audit(user, request, "dx_rate_card_create", "dx_rate_card", row["id"], {"name": body.name})
    return row


@router.get("/rate-cards/{card_id}")
async def get_rate_card(card_id: UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B_READ, "DX_BILLING")
    return await dx_b2b.get_rate_card(scope, str(card_id))


@router.put("/rate-cards/{card_id}")
async def update_rate_card(card_id: UUID, body: RateCardPatch, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B)
    patch = {k: (v.isoformat() if isinstance(v, date) else v)
             for k, v in _nullable_patch(body, ("name", "valid_from", "is_active")).items()}
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    row = await dx_b2b.save_rate_card(scope, patch, str(card_id))
    await _audit(user, request, "dx_rate_card_update", "dx_rate_card", str(card_id), {"keys": sorted(patch)})
    return row


@router.post("/rate-cards/{card_id}/items")
async def set_rate_card_items(card_id: UUID, body: RateItemsIn, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B)
    out = await dx_b2b.set_rate_card_items(scope, str(card_id), [i.model_dump() for i in body.items])
    await _audit(user, request, "dx_rate_card_items", "dx_rate_card", str(card_id), out)
    return out


@router.post("/rate-cards/{card_id}/import")
async def import_rate_card(card_id: UUID, request: Request, file: UploadFile = File(...), dry_run: bool = False,
                           clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B)
    data = await file.read(CSV_MAX_BYTES + 1)
    if len(data) > CSV_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The file is larger than 1 MB.")
    await dx_b2b.get_rate_card(scope, str(card_id))
    out = await dx_b2b.import_rate_csv(scope, str(card_id), data, dry_run)
    if out["applied"]:
        await _audit(user, request, "dx_rate_card_import", "dx_rate_card", str(card_id), out)
    return out


# accounts: money

@router.get("/b2b-accounts/ageing")
async def receivables_ageing(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B_READ)
    rows = await dx_b2b.ageing(scope)
    keys = ("current_paise", "d1_30_paise", "d31_60_paise", "d61_90_paise", "d90_plus_paise", "balance_paise")
    return {"accounts": rows, "totals": {k: sum(int(r.get(k) or 0) for r in rows) for k in keys}}


@router.get("/b2b-accounts/{account_id}")
async def get_account(account_id: UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B_READ)
    out = await dx_b2b.account_summary(scope, str(account_id))
    out["partner_users"] = await dx_b2b._rows(scoped_query("clinic_admins", scope, "id, username, full_name, is_active, "
                                                           "created_at").eq("dx_b2b_account_id", str(account_id)))
    return out


@router.get("/b2b-accounts/{account_id}/ledger")
async def account_ledger(account_id: UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B_READ)
    await dx_b2b.get_account(scope, str(account_id), "id")
    return {"entries": await dx_b2b.ledger(scope, str(account_id)),
            "balance": await dx_b2b.balance(scope, str(account_id))}


@router.post("/b2b-accounts/{account_id}/payments", status_code=status.HTTP_201_CREATED)
async def record_payment(account_id: UUID, body: PaymentIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B)
    out = await dx_b2b.record_payment(scope, str(account_id), body.model_dump(), user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_b2b_payment", "dx_b2b_account", str(account_id),
                     {"entry_id": out["id"], "entry_type": out["entry_type"], "amount_paise": body.amount_paise,
                      "mode": body.mode, "reference": body.reference})
    return out


@router.post("/b2b-accounts/{account_id}/hold")
async def set_account_hold(account_id: UUID, body: HoldIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B)
    if body.status == "on_hold" and not (body.reason or "").strip():
        raise HTTPException(status_code=422, detail="Say why the account is put on hold.")
    out = await dx_b2b.set_hold(scope, str(account_id), body.status, body.reason, user)
    await _audit(user, request, "dx_b2b_hold", "dx_b2b_account", str(account_id),
                 {"status": body.status, "reason": body.reason, "changed": out.get("changed")})
    return out


@router.get("/b2b-accounts/{account_id}/statements")
async def list_statements(account_id: UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B_READ)
    await dx_b2b.get_account(scope, str(account_id), "id")
    return {"statements": await dx_b2b.list_statements(scope, str(account_id))}


@router.post("/b2b-accounts/{account_id}/statements", status_code=status.HTTP_201_CREATED)
async def issue_statement(account_id: UUID, body: StatementIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B)
    st = await dx_b2b.issue_statement(scope, str(account_id), body.period_from.isoformat(), body.period_to.isoformat(),
                                      user)
    await _audit(user, request, "dx_b2b_statement_issue", "dx_b2b_statement", st["id"],
                 {"number": st["statement_number"], "closing_paise": st["closing_paise"]})
    return st


@router.get("/b2b-accounts/statements/{statement_id}/pdf")
async def statement_pdf(statement_id: UUID, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    from fastapi.responses import Response
    scope, _ = await _dx_scope(user, clinic_id, *B2B_READ)
    st = await dx_b2b.get_statement(scope, str(statement_id))
    pdf = await dx_b2b.statement_pdf(scope, st)
    await _audit(user, request, "dx_b2b_statement_view", "dx_b2b_statement", st["id"], {})
    return Response(pdf, media_type="application/pdf", headers={
        "Cache-Control": "no-store", "Content-Disposition": f'inline; filename="{st["statement_number"]}.pdf"'})


@router.post("/b2b-accounts/statements/{statement_id}/send")
async def send_statement(statement_id: UUID, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _dx_scope(user, clinic_id, *B2B)
    st = await dx_b2b.get_statement(scope, str(statement_id))
    out = await dx_b2b.send_statement(clinic, st)
    await _audit(user, request, "dx_b2b_statement_send", "dx_b2b_statement", st["id"], out)
    return out


@router.post("/b2b-accounts/statements/{statement_id}/void")
async def void_statement(statement_id: UUID, body: ReasonIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B)
    out = await dx_b2b.void_statement(scope, str(statement_id), body.reason, user)
    await _audit(user, request, "dx_b2b_statement_void", "dx_b2b_statement", str(statement_id), {"reason": body.reason})
    return out


@router.post("/b2b-accounts/{account_id}/users", status_code=status.HTTP_201_CREATED)
async def create_partner_user(account_id: UUID, body: PartnerUserIn, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    """ADR-DX-09: a B2B client's login (staff_role B2B_PARTNER, no permissions), confined to
    /admin/dx/partner/* and to this account in verify_credentials."""
    from app.database import supabase
    from app.routers.admin import hash_password
    scope, _ = await _dx_scope(user, clinic_id, *B2B, new_work=True)
    _clinic_wide(user)
    await dx_b2b.get_account(scope, str(account_id), "id")
    # unscoped: checking global username uniqueness across all clinic admins
    if (await sb(supabase.table("clinic_admins").select("id").eq("username", body.username))).data:
        raise HTTPException(status_code=409, detail="Username already exists")
    # unscoped: insert_scoped_by_payload
    res = await sb(supabase.table("clinic_admins").insert({
        "clinic_id": scope, "username": body.username, "password_hash": hash_password(body.password), "role": "staff",
        "staff_role": "B2B_PARTNER", "permissions": [], "dx_b2b_account_id": str(account_id), "is_active": True,
        **({"full_name": body.full_name} if body.full_name else {}), **({"phone": body.phone} if body.phone else {})}))
    row = res.data[0]
    await _audit(user, request, "dx_partner_user_create", "clinic_admin", row["id"], {"b2b_account_id": str(account_id)})
    return {k: row.get(k) for k in ("id", "username", "full_name", "is_active", "dx_b2b_account_id", "created_at")}


# barcode stock

@router.get("/barcode-stock")
async def list_barcode_stock(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B, "DX_ADMIN", "DX_COLLECT")
    return {"stock": await dx_b2b.list_stock(scope)}


@router.post("/barcode-stock", status_code=status.HTTP_201_CREATED)
async def issue_barcode_stock(body: StockIn, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, *B2B, "DX_ADMIN", new_work=True)
    if (body.account_id is None) == (body.site_id is None):
        raise HTTPException(status_code=422, detail="Issue a barcode range to one account or one site.")
    if body.range_to < body.range_from:
        raise HTTPException(status_code=422, detail="The range must run from a lower to a higher number.")
    row = await dx_b2b.issue_stock(scope, body.model_dump(), user)
    await _audit(user, request, "dx_barcode_stock_issue", "dx_barcode_stock", row["id"],
                 {"prefix": body.prefix, "from": body.range_from, "to": body.range_to,
                  "account_id": body.account_id, "site_id": body.site_id})
    return row


# ── partner portal (B2B_PARTNER; SECURITY §5) ───────────────────────────────

class PartnerLineIn(_In):
    lab_test_id: str = Field(..., pattern=UUID_RX)


class PartnerVisitIn(_In):
    name: str = Field(..., min_length=1, max_length=100)
    gender: Optional[Literal["male", "female", "other", "undisclosed"]] = None
    age_years: Optional[int] = Field(None, ge=0, le=130)
    external_ref: Optional[str] = Field(None, max_length=40)       # the hospital's OP / IP number
    branch_id: Optional[str] = Field(None, pattern=UUID_RX)         # only when the lab has several sites
    priority: Literal["routine", "urgent", "stat"] = "routine"
    clinical_notes: Optional[str] = Field(None, max_length=1000)
    items: list[PartnerLineIn] = Field(..., min_length=1, max_length=50)
    idempotency_key: str = IDEM


async def _partner(user: AdminUser, clinic_id: str) -> tuple[str, dict, dict]:
    """The login's own account, never one from the request (verify_credentials already confined the path)."""
    if getattr(user, "staff_role", None) != "B2B_PARTNER" or not getattr(user, "dx_b2b_account_id", None):
        raise HTTPException(status_code=403, detail="Partner portal only.")
    scope, clinic = await _dx_scope(user, clinic_id)
    acc = await dx_b2b.get_account(scope, str(user.dx_b2b_account_id), dx_b2b.ACCOUNT_PUBLIC)
    if acc["status"] == "closed":
        raise HTTPException(status_code=403, detail="This B2B account is closed.")
    return scope, clinic, acc


@router.get("/partner/me")
async def partner_me(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _, acc = await _partner(user, clinic_id)
    summary = await dx_b2b.account_summary(scope, acc["id"], dx_b2b.ACCOUNT_PUBLIC)
    tests = await dx_b2b._rows(scoped_query("lab_tests", scope, "id, name, test_code, kind, price_paise")
                               .eq("is_active", True).is_("branch_id", "null").not_.is_("approved_at", "null")
                               .order("name"))
    card = {}
    if summary["rate_card"] and summary["rate_card"].get("is_active"):
        card = {i["lab_test_id"]: i["price_paise"] for i in
                (await dx_b2b.get_rate_card(scope, summary["rate_card"]["id"]))["items"]}
    sites = [{"branch_id": s["branch_id"], "name": s["display_name"]} for s in await dx.list_sites(scope)
             if s.get("is_active")]
    return {**summary, "sites": sites, "tests": [{**t, "price_paise": card.get(t["id"], t["price_paise"]),
                                                  "contract_price": t["id"] in card} for t in tests]}


@router.get("/partner/visits")
async def partner_visits(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _, acc = await _partner(user, clinic_id)
    visits = await dx_b2b.partner_visits(scope, acc["id"])
    releases = await dx_b2b.partner_releases(scope, acc["id"], [v["id"] for v in visits])
    by_visit: dict[str, list] = {}
    for r in releases:
        if r["status"] != "superseded":
            by_visit.setdefault(r["accession_id"], []).append(r)
    return {"visits": [{**v, "releases": sorted(by_visit.get(v["id"], []), key=lambda r: r["release_no"])}
                       for v in visits]}


@router.post("/partner/visits", status_code=status.HTTP_201_CREATED)
async def partner_register_visit(body: PartnerVisitIn, request: Request, clinic_id: str = "default",
                                 user: AdminUser = Depends(verify_credentials)):
    """W5-A: the hospital registers its own sample at the account's contract price, billed per the
    account (subjects are the account's dependants: ADR-DX-21)."""
    scope, clinic, acc = await _partner(user, clinic_id)
    if dx_access(clinic) != "ACTIVE":
        raise HTTPException(status_code=409, detail="The lab is not taking new samples through the portal.")
    if acc["status"] != "active":
        raise HTTPException(status_code=409, detail={"code": "dx_b2b_on_hold",
                                                     "message": "This account is on hold. Contact the lab."})
    sites = [s for s in await dx.list_sites(scope) if s.get("is_active")]
    branch = body.branch_id if body.branch_id in {s["branch_id"] for s in sites} else \
        (sites[0]["branch_id"] if len(sites) == 1 else None)
    if not branch:
        raise HTTPException(status_code=422, detail="Choose the lab branch the sample goes to.")
    subject = await dx.ensure_b2b_subject(scope, acc["id"], {"name": body.name, "gender": body.gender,
                                                             "age_years": body.age_years})
    payload = {"branch_id": branch, "patient_id": subject["patient_id"], "family_member_id": subject["family_member_id"],
               "source": "partner_portal", "b2b_account_id": acc["id"], "billing_mode": acc["default_billing_mode"],
               "patient_type": "b2b", "priority": body.priority, "external_ref": body.external_ref,
               "clinical_notes": body.clinical_notes,
               # per-account key space, fixed length (the visit key column allows 80 characters)
               "idempotency_key": "partner:" + hashlib.sha256(f"{acc['id']}:{body.idempotency_key}".encode()).hexdigest()[:48],
               "items": [{"lab_test_id": i.lab_test_id} for i in body.items], "fees": []}
    out = await dx.register_visit(scope, clinic, {k: v for k, v in payload.items() if v is not None}, user)
    if not out.get("replayed"):
        await _audit(user, request, "dx_partner_visit_register", "dx_accession", out["visit"]["id"],
                     {"number": out["visit"]["accession_number"], "b2b_account_id": acc["id"]})
    v = out["visit"]
    return {"visit": {k: v.get(k) for k in ("id", "accession_number", "status", "priority", "external_ref",
                                            "patient_snapshot", "created_at")},
            "items": [{k: i.get(k) for k in ("id", "item_name", "price_paise", "payer_share_paise", "status")}
                      for i in out["items"]],
            "samples": [{k: s.get(k) for k in ("id", "barcode", "container_code", "sample_type")}
                        for s in out.get("samples", [])],
            "replayed": out.get("replayed", False)}


@router.get("/partner/statements")
async def partner_statements(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _, acc = await _partner(user, clinic_id)
    rows = [s for s in await dx_b2b.list_statements(scope, acc["id"]) if s["status"] == "issued"]
    return {"statements": rows, "balance": await dx_b2b.balance(scope, acc["id"])}


@router.get("/partner/statements/{statement_id}/pdf")
async def partner_statement_pdf(statement_id: UUID, request: Request, clinic_id: str = "default",
                                user: AdminUser = Depends(verify_credentials)):
    from fastapi.responses import Response
    scope, _, acc = await _partner(user, clinic_id)
    st = await dx_b2b._one(scoped_query("dx_b2b_statements", scope).eq("account_id", acc["id"])
                           .eq("id", str(statement_id)).eq("status", "issued"), "Statement")
    pdf = await dx_b2b.statement_pdf(scope, st)
    await _audit(user, request, "dx_partner_statement_download", "dx_b2b_statement", st["id"], {})
    return Response(pdf, media_type="application/pdf", headers={
        "Cache-Control": "no-store", "Content-Disposition": f'inline; filename="{st["statement_number"]}.pdf"'})


@router.get("/partner/releases/{release_id}/pdf")
async def partner_release_pdf(release_id: UUID, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    """Only the account's own visits, only when it receives reports (report_delivery account / both), and
    only when the delivery rulebook allows the partner target (dues, owner, payer rules: DELIVERY-POLICY)."""
    from fastapi.responses import Response
    from app.services import dx_reports
    scope, _, acc = await _partner(user, clinic_id)
    if acc["report_delivery"] not in ("account", "both"):
        raise HTTPException(status_code=403, detail="Reports for this account are not delivered to the portal.")
    rel = await dx_reports.get_release(scope, str(release_id))
    await dx_b2b.partner_visit(scope, acc["id"], rel["accession_id"])          # 404 for another account's visit
    ok, reason = await dx_reports.decision(scope, rel["id"], "partner_portal")
    if not ok:
        raise HTTPException(status_code=409, detail={"code": reason, "message": dx_reports.HELD_TEXT.get(
            reason, "This report cannot be downloaded yet.")})
    pdf = await dx_reports._stored_or_render(scope, rel, None)
    await _audit(user, request, "dx_partner_release_download", "dx_report_release", rel["id"], {})
    return Response(pdf, media_type="application/pdf", headers={
        "Cache-Control": "no-store", "Content-Disposition": f'inline; filename="R{rel["release_no"]}.pdf"'})
