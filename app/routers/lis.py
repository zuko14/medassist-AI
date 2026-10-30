"""In-house Lab Information System API (G2, migration 118).

/admin/lis/orders/{id}/worksheet   parameters + this patient's reference ranges
/admin/lis/orders/{id}/results     technician entry (or a signed report's amendment)
/admin/lis/results/...             pathologist queue, sign-off, signed PDF
/admin/lis/tests/{id}/parameters   parameter definitions (LAB_TESTS_MANAGE)

Permissions: LAB_ENTRY / LAB_VALIDATE (app.services.permissions.holds_lab_permission).
Signing additionally needs a login linked to a doctor with a registration number.
Zero-LLM: nothing here imports the AI, voice or assistant packages.
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.routers.opd import _read_branch
from app.services import lis, orders
from app.services.permissions import holds_lab_permission, holds_order_permission
from app.services.tenant import get_clinic_by_id, ipd_access, opd_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/lis", tags=["Lab Information System"])

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


# ── models ──────────────────────────────────────────────────────────────────

class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValueIn(_In):
    parameter_id: str = Field(..., pattern=_UUID)
    value: Optional[str | float | int] = None
    remarks: Optional[str] = Field(None, max_length=500)


class ResultsIn(_In):
    values: list[ValueIn] = Field(..., min_length=1, max_length=100)
    amendment_reason: Optional[str] = Field(None, max_length=500)


class ValidateIn(_In):
    expected_updated_at: str = Field(..., min_length=10, max_length=40)


class ParameterIn(_In):
    name: str = Field(..., min_length=1, max_length=120)
    unit: Optional[str] = Field(None, max_length=30)
    data_type: Literal["numeric", "text", "options"] = "numeric"
    options: Optional[list[str]] = Field(None, max_length=20)
    sort_order: int = Field(1, ge=0, le=999)
    min_age_days: int = Field(0, ge=0, le=43800)
    max_age_days: int = Field(43800, ge=0, le=43800)
    gender: Literal["all", "male", "female", "other"] = "all"
    normal_low: Optional[float] = Field(None, ge=-9_999_999, le=9_999_999)
    normal_high: Optional[float] = Field(None, ge=-9_999_999, le=9_999_999)
    critical_low: Optional[float] = Field(None, ge=-9_999_999, le=9_999_999)
    critical_high: Optional[float] = Field(None, ge=-9_999_999, le=9_999_999)
    normal_text_value: Optional[str] = Field(None, max_length=120)
    is_active: bool = True

    @model_validator(mode="after")
    def _shape(self):
        if not self.name.strip():
            raise ValueError("name is required.")
        if self.max_age_days < self.min_age_days:
            raise ValueError("max_age_days must not be below min_age_days.")
        lo, hi, clo, chi = self.normal_low, self.normal_high, self.critical_low, self.critical_high
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("normal_low must not exceed normal_high.")
        if clo is not None and lo is not None and clo > lo:
            raise ValueError("critical_low must not exceed normal_low.")
        if chi is not None and hi is not None and chi < hi:
            raise ValueError("critical_high must not be below normal_high.")
        if self.data_type == "options":
            self.options = [o.strip() for o in (self.options or []) if o and o.strip()]
            if not self.options:
                raise ValueError("An options parameter needs at least one option.")
        else:
            self.options = None
        return self


# ── guards ──────────────────────────────────────────────────────────────────

async def _lis_scope(user: AdminUser, clinic_id: str, *perms: str) -> str:
    """Any one of `perms` (LAB_* or ORDERS_READ), tenant, and OPD or IPD entitlement."""
    allowed = any(holds_order_permission(user, p) if p.startswith("ORDERS_") else holds_lab_permission(user, p)
                  for p in perms)
    if not allowed:
        raise HTTPException(status_code=403, detail=f"Missing permission: {' or '.join(perms)}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not clinic or not (opd_enabled(clinic) or ipd_access(clinic)):
        raise HTTPException(status_code=403, detail="The lab module needs the OPD or IPD module.")
    return scope


async def _catalogue_scope(user: AdminUser, clinic_id: str) -> str:
    if user.role not in ("super_admin", "clinic_admin") and "LAB_TESTS_MANAGE" not in (user.permissions or []):
        raise HTTPException(status_code=403, detail="Missing permission: LAB_TESTS_MANAGE")
    return enforce_clinic_access(user, clinic_id)


async def _audit(user: AdminUser, request: Request, action: str, rtype: str, rid: Optional[str], details=None):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


# ── worksheet & entry ───────────────────────────────────────────────────────

@router.get("/orders/{order_id}/worksheet")
async def get_worksheet(order_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _lis_scope(user, clinic_id, "LAB_ENTRY", "LAB_VALIDATE")
    order = await orders.get_order(scope, order_id)
    enforce_branch_scope(user, order.get("branch_id"))
    return await lis.get_worksheet(scope, order_id)


@router.post("/orders/{order_id}/results")
async def save_results(order_id: str, body: ResultsIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope = await _lis_scope(user, clinic_id, "LAB_ENTRY")
    order = await orders.get_order(scope, order_id)
    enforce_branch_scope(user, order.get("branch_id"))
    result = await lis.record_test_results(scope, order_id, [v.model_dump() for v in body.values], user,
                                           body.amendment_reason)
    await _audit(user, request, "LIS_RESULTS_ENTER" if result["version"] == 1 else "LIS_RESULTS_AMEND",
                 "lab_result", result["id"], {"order_id": order_id, "version": result["version"],
                                              "critical": result["has_critical_value"],
                                              "values": len(result["values"])})
    return result


# ── pathologist ─────────────────────────────────────────────────────────────

@router.get("/results")
async def review_queue(status_filter: Literal["entered", "validated"] = Query("entered", alias="status"),
                       branch_id: Optional[str] = None, limit: int = Query(100, ge=1, le=500),
                       offset: int = Query(0, ge=0, le=100_000), clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope = await _lis_scope(user, clinic_id, "LAB_VALIDATE", "LAB_ENTRY")
    return await lis.get_review_queue(scope, _read_branch(user, branch_id), status_filter, limit, offset)


@router.get("/results/{result_id}")
async def get_result(result_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _lis_scope(user, clinic_id, "LAB_VALIDATE", "LAB_ENTRY", "ORDERS_READ")
    result = await lis.get_result_detail(scope, result_id)
    enforce_branch_scope(user, result.get("branch_id"))
    return result


@router.post("/results/{result_id}/validate")
async def validate_result(result_id: str, body: ValidateIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope = await _lis_scope(user, clinic_id, "LAB_VALIDATE")
    result = await lis.get_result(scope, result_id)
    enforce_branch_scope(user, result.get("branch_id"))
    signed = await lis.validate_and_sign_report(scope, result_id, body.expected_updated_at, user)
    if not signed.get("replayed"):
        await _audit(user, request, "LIS_REPORT_SIGN", "lab_result", result_id,
                     {"order_id": signed["order_id"], "version": signed["version"],
                      "critical": signed["has_critical_value"], "supersedes_id": signed.get("supersedes_id")})
    return signed


@router.get("/results/{result_id}/pdf")
async def result_pdf(result_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _lis_scope(user, clinic_id, "LAB_VALIDATE", "LAB_ENTRY", "ORDERS_READ")
    result = await lis.get_result(scope, result_id)
    enforce_branch_scope(user, result.get("branch_id"))
    pdf, result = await lis.render_report(scope, result_id)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{lis.report_filename(result)}"',
                             "Cache-Control": "no-store"})


# ── parameter definitions ───────────────────────────────────────────────────

@router.get("/tests/{lab_test_id}/parameters")
async def list_parameters(lab_test_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    return await lis.list_parameters(await _catalogue_scope(user, clinic_id), lab_test_id)


@router.post("/tests/{lab_test_id}/parameters", status_code=201)
async def create_parameter(lab_test_id: str, body: ParameterIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope = await _catalogue_scope(user, clinic_id)
    row = await lis.create_parameter(scope, lab_test_id, body.model_dump())
    await _audit(user, request, "LIS_PARAMETER_CREATE", "lab_test_parameter", row["id"],
                 {"lab_test_id": lab_test_id, "name": row["name"]})
    return row


@router.put("/tests/{lab_test_id}/parameters/{parameter_id}")
async def update_parameter(lab_test_id: str, parameter_id: str, body: ParameterIn, request: Request,
                           clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _catalogue_scope(user, clinic_id)
    row = await lis.update_parameter(scope, lab_test_id, parameter_id, body.model_dump())
    await _audit(user, request, "LIS_PARAMETER_UPDATE", "lab_test_parameter", parameter_id,
                 {"lab_test_id": lab_test_id, "name": row["name"], "is_active": row["is_active"]})
    return row
