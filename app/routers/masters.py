"""Central masters API (G12, migration 120).

GET    /admin/masters/icd10/search?q=   ICD-10 diagnosis autocomplete (shared reference data)
GET    /admin/masters/services          the clinic's service / tariff master
POST   /admin/masters/services          add a service                       (SERVICES_MANAGE)
PATCH  /admin/masters/services/{id}     re-price, GST / SAC, activate ...   (SERVICES_MANAGE)
DELETE /admin/masters/services/{id}     delete if never billed, else deactivate

Zero-LLM: nothing here imports the AI, voice or assistant packages.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.services import masters
from app.services.permissions import holds_services_permission, resolve_owned_branch

router = APIRouter(prefix="/admin/masters", tags=["Masters"])

Category = Literal["consultation", "procedure", "nursing", "radiology", "diagnostic", "physiotherapy", "ambulance",
                   "room_service", "other"]
TaxRate = Literal[0, 500, 1200, 1800, 2800, 4000]
_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

#: Who reads the tariff master: anyone who bills, orders or runs OPD / IPD.
_READ_GRANTS = {"SERVICES_MANAGE", "OPD_ADMIN", "OPD_BILLING", "OPD_FRONT_DESK", "OPD_CLINICAL", "IPD_BILLING",
                "IPD_ADMIN", "ORDERS_WRITE"}


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceIn(_In):
    service_code: str = Field(..., pattern=r"^[A-Z0-9_-]{2,40}$")
    name: str = Field(..., min_length=1, max_length=200)
    category: Category = "procedure"
    department: Optional[str] = Field(None, max_length=100)
    standard_price_paise: int = Field(0, ge=0, le=100_000_000)
    hsn_sac: str = Field("999312", pattern=r"^[0-9]{4,8}$")
    tax_rate_bps: TaxRate = 0
    is_active: bool = True
    notes: Optional[str] = Field(None, max_length=300)
    branch_id: Optional[str] = Field(None, pattern=_UUID)


class ServicePatch(_In):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    category: Optional[Category] = None
    department: Optional[str] = Field(None, max_length=100)
    standard_price_paise: Optional[int] = Field(None, ge=0, le=100_000_000)
    hsn_sac: Optional[str] = Field(None, pattern=r"^[0-9]{4,8}$")
    tax_rate_bps: Optional[TaxRate] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=300)
    branch_id: Optional[str] = Field(None, pattern=_UUID)
    expected_updated_at: Optional[str] = Field(None, max_length=40)


def _can_read(user: AdminUser) -> bool:
    return user.role in ("super_admin", "clinic_admin") or bool(set(user.permissions or []) & _READ_GRANTS)


def _manage(user: AdminUser) -> None:
    if not holds_services_permission(user):
        raise HTTPException(status_code=403, detail="Missing permission: SERVICES_MANAGE")


async def _audit(user: AdminUser, request: Request, action: str, rid: Optional[str], details=None):
    await log_admin_action(user=user, action=action, resource_type="billable_service", resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


@router.get("/icd10/search")
async def icd10_search(q: str = Query(..., min_length=1, max_length=60), limit: int = Query(20, ge=1, le=50),
                       clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    # shared reference data, but still an /admin route: a clinic must be in scope
    enforce_clinic_access(user, clinic_id)
    return {"results": await masters.search_icd10_diagnoses(q, limit)}


@router.get("/services")
async def list_services(category: Optional[Category] = None, active_only: bool = False, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    if not _can_read(user):
        raise HTTPException(status_code=403, detail="Missing permission: SERVICES_MANAGE")
    scope = enforce_clinic_access(user, clinic_id)
    return {"services": await masters.get_services_catalog(scope, category, user, include_inactive=not active_only),
            "can_manage": holds_services_permission(user)}


@router.post("/services", status_code=201)
async def create_service(body: ServiceIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    _manage(user)
    scope = enforce_clinic_access(user, clinic_id)
    data = body.model_dump()
    if data.get("branch_id"):
        await resolve_owned_branch(user, data["branch_id"], scope)
    elif getattr(user, "branch_id", None) and user.role not in ("super_admin", "clinic_admin"):
        data["branch_id"] = str(user.branch_id)      # a branch-pinned manager prices their own branch
    row = await masters.create_or_update_service(scope, data, user)
    await _audit(user, request, "MASTERS_SERVICE_CREATE", row["id"],
                 {"code": row["service_code"], "price_paise": row["standard_price_paise"]})
    return row


@router.patch("/services/{service_id}")
async def update_service(service_id: str, body: ServicePatch, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    _manage(user)
    scope = enforce_clinic_access(user, clinic_id)
    current = await masters.get_service(scope, service_id)
    enforce_branch_scope(user, current.get("branch_id"))
    data = body.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if data.get("branch_id"):
        await resolve_owned_branch(user, data["branch_id"], scope)
    if not data:
        return current
    row = await masters.create_or_update_service(scope, data, user, service_id, body.expected_updated_at)
    changed = {k: [current.get(k), row.get(k)] for k in data if current.get(k) != row.get(k)}
    await _audit(user, request, "MASTERS_SERVICE_UPDATE", service_id, {"code": row["service_code"], "changed": changed})
    return row


@router.delete("/services/{service_id}")
async def delete_service(service_id: str, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    _manage(user)
    scope = enforce_clinic_access(user, clinic_id)
    current = await masters.get_service(scope, service_id)
    enforce_branch_scope(user, current.get("branch_id"))
    out = await masters.remove_service(scope, current)
    await _audit(user, request, "MASTERS_SERVICE_DELETE" if out["deleted"] else "MASTERS_SERVICE_DEACTIVATE",
                 service_id, {"code": current["service_code"]})
    return out
