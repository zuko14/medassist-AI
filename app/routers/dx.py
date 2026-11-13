"""Kriya Diagnostic OS API — foundation (migration 125, DX-05).
Design: docs/diagnostic-os/API.md, SECURITY.md §2–§4.

Every route: permission -> enforce_clinic_access -> dx_access(clinic) ->
(new work) wind-down rule -> site/branch scope -> service. Zero-LLM.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.database import scoped_query, sb
from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.routers.lis import ParameterIn
from app.services import dx, dx_catalogue, dx_seed
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

CAT_READ = ("DX_READ", "DX_CATALOGUE", "DX_ADMIN", "DX_CATALOGUE_APPROVE")
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
