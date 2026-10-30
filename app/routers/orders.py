"""Investigation & procedure orders API (G1, migration 115).

/admin/opd/...        a consultation's orders: place, list, bill, cancel
/admin/diagnostic/... the sample-collection / lab desk worklist: OPD + IPD orders

Permissions are the ORDERS_* grants (app.services.permissions.holds_order_permission).
Placing an order is a clinical decision: only the treating doctor's own login.
Zero-LLM: nothing here imports the AI, voice or assistant packages.
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.routers.opd import _doctor_only, _opd_scope, _read_branch, _visit_in_branch
from app.services import orders
from app.services.opd_billing import get_catalog
from app.services.permissions import holds_order_permission
from app.services.tenant import get_clinic_by_id, ipd_access, opd_enabled

logger = logging.getLogger(__name__)

opd_router = APIRouter(prefix="/admin/opd", tags=["OPD Orders"])
diagnostic_router = APIRouter(prefix="/admin/diagnostic", tags=["Diagnostic Worklist"])

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


# ── models ──────────────────────────────────────────────────────────────────

class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderItemIn(_In):
    order_type: Literal["investigation", "procedure"]
    catalog_type: Literal["lab_test", "service_catalog", "custom"]
    catalog_reference_id: Optional[str] = Field(None, pattern=_UUID)
    catalog_code: Optional[str] = Field(None, pattern=r"^[A-Z0-9_\-]{2,40}$")
    item_name: Optional[str] = Field(None, min_length=1, max_length=200)
    sample_type: Optional[str] = Field(None, max_length=60)
    urgency: Literal["routine", "urgent", "stat"] = "routine"
    clinical_notes: Optional[str] = Field(None, max_length=1000)
    quantity: int = Field(1, ge=1, le=99)
    unit_price_paise: int = Field(0, ge=0, le=100_000_000)   # custom lines only; catalogue lines are priced server-side

    @model_validator(mode="after")
    def _shape(self):
        if self.catalog_type == "lab_test" and not self.catalog_reference_id:
            raise ValueError("A lab test order needs catalog_reference_id.")
        if self.catalog_type == "service_catalog" and not self.catalog_code:
            raise ValueError("A service order needs catalog_code.")
        if self.catalog_type == "custom" and not (self.item_name and self.item_name.strip()):
            raise ValueError("A custom order needs item_name.")
        return self


class OrdersIn(_In):
    orders: list[OrderItemIn] = Field(..., min_length=1, max_length=30)
    idempotency_key: str = Field(..., pattern=r"^[A-Za-z0-9_-]{8,72}$")


class CancelIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)


class CollectIn(_In):
    specimen_barcode: str = Field(..., pattern=r"^[A-Za-z0-9._/-]{3,64}$")


class CompleteIn(_In):
    result_summary: str = Field(..., min_length=1, max_length=4000)


# ── guards ──────────────────────────────────────────────────────────────────

def _require(user: AdminUser, perm: str) -> None:
    if not holds_order_permission(user, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")


async def _dx_scope(user: AdminUser, clinic_id: str, perm: str) -> str:
    """Worklist routes: permission, tenant, and OPD or IPD entitlement."""
    _require(user, perm)
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not clinic or not (opd_enabled(clinic) or ipd_access(clinic)):
        raise HTTPException(status_code=403, detail="Diagnostic orders need the OPD or IPD module.")
    return scope


async def _audit(user: AdminUser, request: Request, action: str, rtype: str, rid: Optional[str], details=None):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


# ── OPD: a consultation's orders ────────────────────────────────────────────

@opd_router.post("/encounters/{encounter_id}/orders", status_code=status.HTTP_201_CREATED)
async def place_orders(encounter_id: str, body: OrdersIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    _require(user, "ORDERS_WRITE")
    scope, _ = await _opd_scope(user, clinic_id)
    encounter = await orders.get_encounter(scope, encounter_id)
    if not user.doctor_id or str(user.doctor_id) != str(encounter["doctor_id"]):
        raise HTTPException(status_code=403, detail="Only the treating doctor can order tests for this consultation.")
    result = await orders.create_orders(scope, encounter_id, [o.model_dump(mode="json") for o in body.orders],
                                        body.idempotency_key, user)
    if not result.get("replayed"):
        await _audit(user, request, "OPD_ORDERS_CREATE", "opd_encounter", encounter_id,
                     {"count": len(result.get("orders") or []), "billing": result.get("billing")})
    return result


@opd_router.get("/orders/catalog")
async def get_order_catalog(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    """What a doctor can order: active lab tests and the clinic's diagnostic / procedure services."""
    _require(user, "ORDERS_WRITE")
    scope, _ = await _opd_scope(user, clinic_id)
    catalog = await get_catalog(scope)
    return {"lab_tests": catalog["lab_tests"],
            "services": [s for s in catalog["service_catalog"]
                         if s.get("active", True) and s.get("item_type") in ("diagnostic", "procedure")]}


@opd_router.get("/encounters/{encounter_id}/orders")
async def list_encounter_orders(encounter_id: str, clinic_id: str = "default",
                                user: AdminUser = Depends(verify_credentials)):
    _require(user, "ORDERS_READ")
    scope, _ = await _opd_scope(user, clinic_id)
    await _visit_in_branch(user, scope, encounter_id=encounter_id)
    encounter = await orders.get_encounter(scope, encounter_id)
    return {"orders": await orders.list_visit_orders(scope, encounter["appointment_id"])}


@opd_router.get("/appointments/{appointment_id}/orders")
async def list_visit_orders(appointment_id: str, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    _require(user, "ORDERS_READ")
    scope, _ = await _opd_scope(user, clinic_id)
    await _visit_in_branch(user, scope, appointment_id=appointment_id)
    return {"orders": await orders.list_visit_orders(scope, appointment_id)}


@opd_router.post("/appointments/{appointment_id}/orders/bill")
async def bill_visit_orders(appointment_id: str, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    """'Add all to invoice': every unbilled order of the visit onto a draft bill."""
    scope, _ = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_FRONT_DESK", "OPD_ADMIN")
    await _visit_in_branch(user, scope, appointment_id=appointment_id)
    result = await orders.bill_pending_orders(scope, appointment_id, user)
    if result.get("added"):
        await _audit(user, request, "OPD_ORDERS_BILL", "opd_invoice", result.get("invoice_id"),
                     {"appointment_id": appointment_id, "added": result["added"], "created": result.get("created")})
    return result


@opd_router.delete("/orders/{order_id}")
async def cancel_order(order_id: str, body: CancelIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    _require(user, "ORDERS_CANCEL")
    scope, _ = await _opd_scope(user, clinic_id)
    order = await orders.get_order(scope, order_id)
    if _doctor_only(user):
        # a doctor login works its own patients wherever they were registered
        if str(user.doctor_id) != str(order["doctor_id"]):
            raise HTTPException(status_code=403, detail="Only the ordering doctor can cancel this order.")
    else:
        enforce_branch_scope(user, order.get("branch_id"))
    result = await orders.cancel_order(scope, order_id, body.reason, user)
    if not result.get("replayed"):
        await _audit(user, request, "OPD_ORDER_CANCEL", "opd_order", order_id,
                     {"removed_line": result.get("removed_line"), "invoice_id": result.get("invoice_id")})
    return result


# ── diagnostic worklist ─────────────────────────────────────────────────────

@diagnostic_router.get("/worklist")
async def get_worklist(branch_id: Optional[str] = None,
                       status_filter: Optional[Literal["to_collect", "ordered", "billed", "sample_collected",
                                                       "in_progress", "resulted", "completed",
                                                       "cancelled"]] = Query(None, alias="status"),
                       limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0, le=100_000),
                       clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _dx_scope(user, clinic_id, "ORDERS_READ")
    return await orders.get_diagnostic_worklist(scope, _read_branch(user, branch_id), status_filter, limit, offset)


@diagnostic_router.post("/orders/{order_id}/collect")
async def collect_sample(order_id: str, body: CollectIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope = await _dx_scope(user, clinic_id, "ORDERS_COLLECT")
    order = await orders.get_order(scope, order_id)
    enforce_branch_scope(user, order.get("branch_id"))
    row = await orders.collect_sample(scope, order_id, body.specimen_barcode, user)
    await _audit(user, request, "DIAGNOSTIC_SAMPLE_COLLECT", "opd_order", order_id,
                 {"barcode": body.specimen_barcode, "source": order["source"]})
    return row


@diagnostic_router.post("/orders/{order_id}/complete")
async def complete_order(order_id: str, body: CompleteIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    # results are recorded by doctors and lab staff (ORDERS_WRITE), never by a sample collector
    scope = await _dx_scope(user, clinic_id, "ORDERS_WRITE")
    order = await orders.get_order(scope, order_id)
    enforce_branch_scope(user, order.get("branch_id"))
    row = await orders.complete_order(scope, order_id, body.result_summary, user)
    await _audit(user, request, "DIAGNOSTIC_ORDER_COMPLETE", "opd_order", order_id, {"source": order["source"]})
    return row
