"""Corporate employee-health insights — /admin/corporate-health (migration 095).

Two audiences on one single-tenant surface:

* the diagnostic centre (clinic_admin / super_admin, or staff holding
  CORPORATE_HEALTH_MANAGE): companies, PDF uploads, report list, deletes,
  company-viewer logins;
* a company-viewer login (staff_role CORPORATE_VIEWER): GET its OWN company and
  its aggregate insights. Nothing else — verify_credentials enforces that for
  every /admin route, and the two viewer-reachable routes below re-check the
  company binding against the database on every request.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field, field_validator

from app.database import is_uuid, sb, supabase
from app.routers.admin import (
    AdminUser,
    _reserved_usernames,
    enforce_clinic_access,
    hash_password,
    log_admin_action,
    revoke_sessions_for_user,
    verify_credentials,
)
from app.services import corporate_health as ch
from app.services.permissions import CORPORATE_VIEWER
from app.services.tenant import corporate_health_enabled, get_clinic_by_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/corporate-health", tags=["corporate-health"])


def _ip(request: Optional[Request]) -> str:
    return request.client.host if (request and request.client) else "unknown"


def _is_viewer(user: AdminUser) -> bool:
    return getattr(user, "staff_role", None) == CORPORATE_VIEWER


def _is_admin(user: AdminUser) -> bool:
    """clinic_admin / super_admin, or the platform owner acting through the
    /platform/corporate-health wrappers. The owner's AdminUser is built there,
    pinned to ONE host clinic (clinic_id), and its user_id is the env sentinel
    no clinic_admins row can carry (ids are UUIDs)."""
    if user.role == "platform_owner":
        return user.user_id == "platform_owner_env" and bool(user.clinic_id)
    return user.role in ("super_admin", "clinic_admin")


def _can_manage(user: AdminUser) -> bool:
    if _is_viewer(user):
        return False
    return _is_admin(user) or "CORPORATE_HEALTH_MANAGE" in (user.permissions or [])


def _unique_error(e: Exception) -> bool:
    s = str(e).lower()
    return "duplicate key" in s or "23505" in s


async def _scope(user: AdminUser, clinic_id: str) -> str:
    """Resolve the tenant and refuse if the owner has not enabled the feature."""
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not corporate_health_enabled(clinic):
        raise HTTPException(status_code=403, detail="Corporate health insights are not enabled for this clinic.")
    return scope


async def _manager_scope(user: AdminUser, clinic_id: str) -> str:
    if not _can_manage(user):
        raise HTTPException(status_code=403, detail="Missing permission: CORPORATE_HEALTH_MANAGE")
    return await _scope(user, clinic_id)


async def _viewer_company_id(user: AdminUser, scope: str) -> str:
    """The one company a viewer login may see, read fresh from the database so
    deactivation or a company delete takes effect on the very next request."""
    res = await sb(supabase.table("clinic_admins").select("corporate_client_id, is_active, staff_role")
                   .eq("clinic_id", scope).eq("id", str(user.user_id)).limit(1))
    row = (res.data or [None])[0]
    if (not row or not row.get("is_active") or row.get("staff_role") != CORPORATE_VIEWER
            or not row.get("corporate_client_id")):
        raise HTTPException(status_code=403, detail="This login is not linked to a company. Contact your lab.")
    return str(row["corporate_client_id"])


async def _company(scope: str, company_id: str) -> dict:
    if not is_uuid(company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    company = await ch.get_company(scope, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


# ═══════ companies ═══════


def _clean_name(v: str) -> str:
    v = " ".join((v or "").split())
    if not 2 <= len(v) <= 120:
        raise ValueError("Company name must be 2 to 120 characters.")
    return v


class CompanyIn(BaseModel):
    name: str = Field(..., max_length=200)

    @field_validator("name")
    @classmethod
    def _v(cls, v: str) -> str:
        return _clean_name(v)


class CompanyPatch(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    is_active: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def _v(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_name(v)


@router.get("/companies")
async def list_companies(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id)
    if _is_viewer(user):
        company_id = await _viewer_company_id(user, scope)
        companies = await ch.list_companies(scope, only_id=company_id)
        return {"companies": [{"id": c["id"], "name": c["name"]} for c in companies if c.get("is_active")],
                "can_manage": False}
    if not _can_manage(user):
        raise HTTPException(status_code=403, detail="Missing permission: CORPORATE_HEALTH_MANAGE")
    return {"companies": await ch.list_companies(scope), "can_manage": True}


@router.post("/companies")
async def create_company(body: CompanyIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope = await _manager_scope(user, clinic_id)
    try:
        res = await sb(
            # unscoped: insert_scoped_by_payload
            supabase.table("corporate_clients").insert(
                {"clinic_id": scope, "name": body.name, "created_by": user.username}))
    except Exception as e:
        if _unique_error(e):
            raise HTTPException(status_code=409, detail="A company with this name already exists.")
        logger.error(f"corporate_health: create company failed clinic={scope}: {e}")
        raise HTTPException(status_code=500, detail="Could not create the company.")
    company = (res.data or [{}])[0]
    await log_admin_action(user, "corporate_company_create", "corporate_client", company.get("id"),
                           {"name": body.name}, _ip(request))
    return {"success": True, "company": company}


@router.patch("/companies/{company_id}")
async def update_company(company_id: str, body: CompanyPatch, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope = await _manager_scope(user, clinic_id)
    await _company(scope, company_id)
    update = {k: v for k, v in (("name", body.name), ("is_active", body.is_active)) if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail="No changes provided")
    try:
        await sb(supabase.table("corporate_clients").update(update).eq("clinic_id", scope).eq("id", company_id))
    except Exception as e:
        if _unique_error(e):
            raise HTTPException(status_code=409, detail="A company with this name already exists.")
        logger.error(f"corporate_health: update company failed clinic={scope}: {e}")
        raise HTTPException(status_code=500, detail="Could not update the company.")
    await log_admin_action(user, "corporate_company_update", "corporate_client", company_id, update, _ip(request))
    return {"success": True}


@router.delete("/companies/{company_id}")
async def delete_company(company_id: str, request: Request, clinic_id: str = "default",
                         confirm: str = Query(..., description="Must equal the company name"),
                         user: AdminUser = Depends(verify_credentials)):
    """Deletes the company and every report under it (FK cascade). Its viewer
    logins lose access (FK SET NULL) and their live sessions are revoked."""
    scope = await _manager_scope(user, clinic_id)
    company = await _company(scope, company_id)
    if " ".join(confirm.split()).lower() != company["name"].lower():
        raise HTTPException(status_code=400, detail="Type the company name exactly to confirm.")
    viewers = await sb(supabase.table("clinic_admins").select("username")
                       .eq("clinic_id", scope).eq("corporate_client_id", company_id))
    await sb(supabase.table("corporate_clients").delete().eq("clinic_id", scope).eq("id", company_id))
    for v in viewers.data or []:
        await revoke_sessions_for_user(v["username"])
    await log_admin_action(user, "corporate_company_delete", "corporate_client", company_id,
                           {"name": company.get("name")}, _ip(request))
    return {"success": True}


# ═══════ reports ═══════


@router.post("/companies/{company_id}/reports")
async def upload_reports(company_id: str, request: Request, files: List[UploadFile] = File(...),
                         clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    """Parse up to MAX_FILES_PER_UPLOAD PDFs. The panel sends a big batch as
    many small requests so each one stays short. Every file gets its own
    outcome; one bad PDF never fails the others."""
    scope = await _manager_scope(user, clinic_id)
    await _company(scope, company_id)
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")
    if len(files) > ch.MAX_FILES_PER_UPLOAD:
        raise HTTPException(status_code=400, detail=f"Upload at most {ch.MAX_FILES_PER_UPLOAD} PDFs per request.")

    outcomes = []
    for f in files:
        name = (f.filename or "report.pdf")[:200]
        data = await f.read(ch.MAX_PDF_BYTES + 1)
        if len(data) > ch.MAX_PDF_BYTES:
            outcomes.append({"file": name, "status": "rejected", "reason": "PDF is larger than 15 MB."})
            continue
        try:
            out = await ch.ingest_pdf(scope, company_id, data, user.username)
        except Exception as e:  # database failure; the parse itself never raises
            logger.error(f"corporate_health: ingest failed clinic={scope} company={company_id}: {type(e).__name__}: {e}")
            out = {"status": "error", "reason": "Could not save this report. Try uploading it again."}
        outcomes.append({"file": name, **out})

    counts = {s: sum(1 for o in outcomes if o["status"] == s) for s in ("accepted", "duplicate", "rejected", "error")}
    await log_admin_action(user, "corporate_reports_upload", "corporate_client", company_id, counts, _ip(request))
    return {"success": True, "results": outcomes, "counts": counts}


@router.get("/companies/{company_id}/reports")
async def list_reports(company_id: str, clinic_id: str = "default",
                       limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                       user: AdminUser = Depends(verify_credentials)):
    scope = await _manager_scope(user, clinic_id)
    await _company(scope, company_id)
    return await ch.list_reports(scope, company_id, limit, offset)


@router.delete("/companies/{company_id}/reports/{report_id}")
async def delete_report(company_id: str, report_id: str, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope = await _manager_scope(user, clinic_id)
    await _company(scope, company_id)
    if not is_uuid(report_id) or not await ch.delete_reports(scope, company_id, report_id):
        raise HTTPException(status_code=404, detail="Report not found")
    await log_admin_action(user, "corporate_report_delete", "corporate_health_report", report_id,
                           {"company_id": company_id}, _ip(request))
    return {"success": True}


@router.delete("/companies/{company_id}/reports")
async def delete_all_reports(company_id: str, request: Request, clinic_id: str = "default",
                             confirm: str = Query(..., description="Must equal the company name"),
                             user: AdminUser = Depends(verify_credentials)):
    """Clear every uploaded report of a company. The company, its viewer logins
    and future uploads are unaffected. Typing the company name is the guard."""
    scope = await _manager_scope(user, clinic_id)
    company = await _company(scope, company_id)
    if " ".join(confirm.split()).lower() != company["name"].lower():
        raise HTTPException(status_code=400, detail="Type the company name exactly to confirm.")
    deleted = await ch.delete_reports(scope, company_id)
    await log_admin_action(user, "corporate_reports_clear", "corporate_client", company_id,
                           {"deleted": deleted}, _ip(request))
    return {"success": True, "deleted": deleted}


# ═══════ insights ═══════


@router.get("/companies/{company_id}/insights")
async def insights(company_id: str, clinic_id: str = "default",
                   sex: Optional[str] = Query(None, pattern="^[MF]$"),
                   age_band: Optional[str] = Query(None),
                   user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id)
    if _is_viewer(user):
        if company_id != await _viewer_company_id(user, scope):
            raise HTTPException(status_code=404, detail="Company not found")
    elif not _can_manage(user):
        raise HTTPException(status_code=403, detail="Missing permission: CORPORATE_HEALTH_MANAGE")
    company = await _company(scope, company_id)
    if _is_viewer(user) and not company.get("is_active"):
        raise HTTPException(status_code=403, detail="This company dashboard is currently disabled by the lab.")
    if age_band is not None and age_band not in ch.AGE_BAND_KEYS:
        raise HTTPException(status_code=422, detail=f"age_band must be one of {ch.AGE_BAND_KEYS}")
    rows = await ch.fetch_rows(scope, company_id)
    return {"company": {"id": company["id"], "name": company["name"]},
            **ch.build_insights(rows, sex=sex, band=age_band)}


# ═══════ company-viewer logins ═══════


class ViewerIn(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
    password: str = Field(..., min_length=8, max_length=200)


@router.get("/companies/{company_id}/viewers")
async def list_viewers(company_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _manager_scope(user, clinic_id)
    await _company(scope, company_id)
    res = await sb(supabase.table("clinic_admins").select("id, username, is_active, created_at")
                   .eq("clinic_id", scope).eq("corporate_client_id", company_id)
                   .eq("staff_role", CORPORATE_VIEWER).order("created_at"))
    return {"viewers": res.data or []}


@router.post("/companies/{company_id}/viewers")
async def create_viewer(company_id: str, body: ViewerIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    """A login for the company (e.g. its HR head). Creating a login is a staff
    action, so a delegated manager also needs STAFF_CREATE."""
    scope = await _manager_scope(user, clinic_id)
    if not _is_admin(user) and "STAFF_CREATE" not in (user.permissions or []):
        raise HTTPException(status_code=403, detail="Missing permission: STAFF_CREATE")
    company = await _company(scope, company_id)
    if body.username.lower() in _reserved_usernames():
        raise HTTPException(status_code=409, detail="That username is reserved.")
    clash = await sb(
        # unscoped: global_auth_lookup
        supabase.table("clinic_admins").select("id").eq("username", body.username).limit(1))
    if clash.data:
        raise HTTPException(status_code=409, detail="Username already exists")
    try:
        res = await sb(
            # unscoped: insert_scoped_by_payload
            supabase.table("clinic_admins").insert({
                "clinic_id": scope, "username": body.username, "password_hash": hash_password(body.password),
                "role": "staff", "staff_role": CORPORATE_VIEWER, "permissions": [], "branch_id": None,
                "corporate_client_id": company_id, "is_active": True,
            }))
    except Exception as e:
        if _unique_error(e):
            raise HTTPException(status_code=409, detail="Username already exists")
        logger.error(f"corporate_health: create viewer failed clinic={scope}: {e}")
        raise HTTPException(status_code=500, detail="Could not create the login.")
    row = (res.data or [{}])[0]
    await log_admin_action(user, "corporate_viewer_create", "clinic_admin", row.get("id"),
                           {"company_id": company_id, "company": company.get("name")}, _ip(request))
    return {"success": True, "viewer": {"id": row.get("id"), "username": body.username}}
