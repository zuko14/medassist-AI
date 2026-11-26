"""Pharmacy OS without OPD — the bridges a standalone pharmacy (plan `pharmacy`) needs (lane RXR; RETAIL-PLAN.md).

Routes live under /admin/pharmacy (not /admin/rx) so the Pharmacy OS lane's route and table pins stay exactly as built.
RXR-ADR-3  cashier shifts: thin routes calling the SAME opd_billing shift service (same table, same blind close);
           hospitals keep their OPD routes and both see the same shifts.
RXR-ADR-10 customers: create / search through the SAME register_patient / search_patients services (MRN, DPDP
           consent, duplicate check) — Pharmacy OS had no patient front door (VERIFICATION D13).
RXR-ADR-5/6 order requests: the pharmacist's queue of WhatsApp / voice / counter requests (migration 150).
Every route: RX permission → tenant → rx_access (pharmacy ACTIVE; new work refused in WIND_DOWN). Zero-LLM.
"""

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.routers.admin import AdminUser, log_admin_action, verify_credentials
from app.routers.opd import PatientRegisterIn, ShiftCloseIn, ShiftOpenIn, _read_branch, _write_branch
from app.routers.rx import _rx_scope
from app.services import rx_retail

router = APIRouter(prefix="/admin/pharmacy", tags=["Pharmacy OS — retail"])
SHIFT_WORK = ("RX_DISPENSE", "RX_ADMIN")


# ── cashier shifts (RXR-ADR-3) ───────────────────────────────────────────────

@router.post("/shifts/open")
async def rx_open_shift(body: ShiftOpenIn, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    from app.services.opd_billing import open_shift
    scope, _ = await _rx_scope(user, clinic_id, *SHIFT_WORK, new_work=True)
    return await open_shift(scope, opening_float_paise=body.opening_float_paise,
                            branch_id=await _write_branch(user, scope, body.branch_id), actor=user)


@router.get("/shifts/current")
async def rx_current_shift(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    from app.services.opd_billing import get_current_shift
    scope, _ = await _rx_scope(user, clinic_id, *SHIFT_WORK)
    return await get_current_shift(scope, actor=user)


@router.post("/shifts/{shift_id}/close")
async def rx_close_shift(shift_id: uuid.UUID, body: ShiftCloseIn, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    from app.services.opd_billing import close_shift
    scope, _ = await _rx_scope(user, clinic_id, *SHIFT_WORK)
    return await close_shift(scope, str(shift_id), declared_cash_paise=body.declared_cash_paise, notes=body.notes,
                             actor=user)


@router.get("/shifts")
async def rx_list_shifts(date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"), clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    from app.services.opd_billing import list_shifts
    scope, _ = await _rx_scope(user, clinic_id, "RX_ADMIN")
    return await list_shifts(scope, date_str=date, actor=user, branch_id=_read_branch(user, None))


# ── customers (RXR-ADR-10) ───────────────────────────────────────────────────

@router.get("/customers")
async def rx_search_customers(q: str = Query(..., min_length=2, max_length=80), limit: int = Query(20, ge=1, le=50),
                              clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    from app.services.opd import search_patients
    scope, _ = await _rx_scope(user, clinic_id, "RX_READ", "RX_DISPENSE", "RX_ADMIN")
    return await search_patients(scope, q=q, limit=limit)


@router.post("/customers", status_code=201)
async def rx_register_customer(body: PatientRegisterIn, clinic_id: str = "default",
                               user: AdminUser = Depends(verify_credentials)):
    from app.services.opd import register_patient
    scope, _ = await _rx_scope(user, clinic_id, *SHIFT_WORK, new_work=True)
    res = await register_patient(scope, body.model_dump(), user)
    await log_admin_action(user=user, action="RX_CUSTOMER_REGISTER", resource_type="patient",
                           resource_id=res["patient_id"], details={"mrn": res.get("mrn")})
    return res


# ── Business & GST card (RXR-04, RXR-ADR-4) ──────────────────────────────────

class LicenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_id: uuid.UUID
    drug_licence_no: str = Field(..., min_length=3, max_length=60)


class BusinessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gstin: Optional[str] = Field(None, max_length=15)                 # "" clears it
    pharmacist_name: Optional[str] = Field(None, min_length=2, max_length=100)
    pharmacist_registration: Optional[str] = Field(None, min_length=3, max_length=60)
    licences: Optional[list[LicenceIn]] = Field(None, max_length=20)
    po_template: Optional[str] = Field(None, pattern=r"^[a-z0-9_]{0,512}$")   # Meta template name; "" clears

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, v):
        from app.routers.opd import _gstin_or_blank
        return _gstin_or_blank(v) if v is not None else None


@router.get("/business")
async def rx_business(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _rx_scope(user, clinic_id, "RX_READ", "RX_ADMIN")
    return await rx_retail.business_profile(clinic)


@router.put("/business")
async def rx_business_update(body: BusinessIn, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    from app.routers.rx import _clinic_wide
    _, clinic = await _rx_scope(user, clinic_id, "RX_ADMIN", new_work=True)
    _clinic_wide(user)
    out = await rx_retail.update_business_profile(clinic, body.model_dump(exclude_none=True, mode="json"))
    await log_admin_action(user=user, action="RX_BUSINESS_UPDATE", resource_type="clinic", resource_id=clinic["id"],
                           details={"fields": sorted(body.model_dump(exclude_none=True))})
    return out


# ── PO to distributor (RXR-10): CSV export + WhatsApp document ──────────────

async def _po(user: AdminUser, clinic_id: str, po_id: uuid.UUID, *perms: str, new_work: bool = False):
    from app.routers.rx import _doc_in_scope
    from app.services import rx_purchase
    scope, clinic = await _rx_scope(user, clinic_id, *perms, new_work=new_work)
    return clinic, await _doc_in_scope(user, scope, await rx_purchase.get_po(scope, str(po_id)))


@router.get("/purchase-orders/{po_id}.csv")
async def rx_po_csv(po_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    _, po = await _po(user, clinic_id, po_id, "RX_READ", "RX_PURCHASE", "RX_ADMIN")
    name = po.get("po_number") or "PO-draft"
    return Response(rx_retail.po_csv(po), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"', "Cache-Control": "no-store"})


@router.post("/purchase-orders/{po_id}/whatsapp")
async def rx_po_whatsapp(po_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    from app.services import rx, rx_purchase
    clinic, po = await _po(user, clinic_id, po_id, "RX_PURCHASE", "RX_ADMIN", new_work=True)
    store = await rx.get_store(clinic["id"], str(po["store_id"]))
    out = await rx_retail.send_po_whatsapp(clinic, po, rx_purchase.render_po_pdf(po, clinic, store))
    await log_admin_action(user=user, action="RX_PO_WHATSAPP", resource_type="rx_purchase_order", resource_id=str(po_id),
                           details={"po_number": po["po_number"]})
    return out


# ── order requests (the pharmacist's queue, migration 150) ───────────────────

class OrderMoveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Literal["reviewing", "ready", "collected", "rejected", "cancelled"]
    reason: Optional[str] = Field(None, min_length=3, max_length=300)
    sale_id: Optional[uuid.UUID] = None
    pickup_by: Optional[str] = Field(None, max_length=40)


class CounterRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(..., pattern=r"^\+?[0-9]{10,15}$")
    kind: Literal["prescription", "refill", "callback"] = "prescription"
    note: Optional[str] = Field(None, max_length=500)


@router.get("/order-requests")
async def pharmacy_order_requests(status: Optional[Literal["new", "reviewing", "ready", "collected", "rejected", "cancelled"]] = None,
                            clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, "RX_READ", "RX_DISPENSE", "RX_ADMIN")
    return {"requests": await rx_retail.list_requests(scope, status)}


@router.post("/order-requests", status_code=201)
async def rx_counter_request(body: CounterRequestIn, clinic_id: str = "default",
                             user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _rx_scope(user, clinic_id, *SHIFT_WORK, new_work=True)
    row, created = await rx_retail.create_request(clinic, body.phone, body.kind, "counter", note=body.note)
    if not created:
        raise HTTPException(status_code=409, detail="An open request of this kind already exists for this number.")
    return row


@router.post("/order-requests/{request_id}/move")
async def rx_move_request(request_id: uuid.UUID, body: OrderMoveIn, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _rx_scope(user, clinic_id, *SHIFT_WORK)
    out = await rx_retail.move(clinic, str(request_id), body.to, user, reason=body.reason,
                               sale_id=str(body.sale_id) if body.sale_id else None, pickup_by=body.pickup_by)
    await log_admin_action(user=user, action="RX_ORDER_REQUEST_" + body.to.upper(), resource_type="rx_order_request",
                           resource_id=str(request_id), details={})
    return out


@router.get("/order-requests/{request_id}/photo")
async def rx_request_photo(request_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    """The prescription photo, fetched from Meta with the clinic's own token at the moment the pharmacist opens it
    (never stored by Kriya, never read by a model)."""
    import httpx

    from app.config import settings
    scope, clinic = await _rx_scope(user, clinic_id, *SHIFT_WORK)
    media_id = await rx_retail.media_ref(scope, str(request_id))
    token = (clinic.get("config") or {}).get("meta_access_token")
    if not token:
        raise HTTPException(status_code=409, detail="This clinic's WhatsApp credentials are not configured.")
    base = f"https://graph.facebook.com/{settings.whatsapp_api_version}"
    async with httpx.AsyncClient(timeout=20) as client:
        meta = await client.get(f"{base}/{media_id}", headers={"Authorization": f"Bearer {token}"})
        url = (meta.json() or {}).get("url") if meta.status_code < 300 else None
        if not url:
            raise HTTPException(status_code=410, detail="WhatsApp no longer has this photo (media expire after 30 days).")
        img = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    await log_admin_action(user=user, action="RX_ORDER_PHOTO_VIEW", resource_type="rx_order_request",
                           resource_id=str(request_id), details={})
    return Response(img.content, media_type=img.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "no-store"})
