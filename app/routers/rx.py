"""Kriya Pharmacy OS API — foundation (134, RX-05), drug master (135, RX-13…15), stock (136, RX-18…20),
purchase (137, RX-23…26, RX-71), counter sales (138, RX-29…35, RX-69/70/72/73/77/81/82), inpatient pharmacy (139, RX-38…40),
transfers / par levels / stock audits (140, RX-42…46, RX-78), statutory registers / batch recall / narcotic empties
(141, RX-49, RX-50, RX-74, RX-79), refills / alerts / expiry-return planner (142, RX-56…RX-59, RX-76), Ketu stock answer / dashboard / lost-sale list
(P9, RX-61, RX-62, RX-80).
Design: docs/pharmacy-os/API.md §0–§10, SECURITY.md §2–§3.

Every route: permission -> enforce_clinic_access -> rx_access(clinic) ->
(new work) wind-down rule -> store / branch scope -> service. Zero-LLM.
"""

import uuid
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.database import sb, scoped_query
from app.routers.admin import AdminUser, enforce_clinic_access, log_admin_action, verify_credentials
from app.services import (rx, rx_alerts, rx_audit, rx_catalogue, rx_dashboard, rx_gs1, rx_ipd, rx_pos, rx_pos_docs,
                          rx_purchase, rx_refills, rx_registers, rx_stock, rx_transfers)
from app.services.rx_catalogue import BarcodeIn, GenericIn, ItemIn, ItemPatch, ManufacturerIn
from app.services.rx_purchase import CreditIn, GrnIn, GrnPatch, POIn, POPatch, ReturnIn, SupplierIn, SupplierPatch
from app.services.permissions import allowed_store_ids, holds_rx_permission, resolve_owned_branch
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache, ipd_access, rx_access

router = APIRouter(prefix="/admin/rx", tags=["Pharmacy OS"])

StoreType = Literal["central", "retail_counter", "ward_substore", "ot_tray", "emergency_tray"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsPatch(_In):
    version: int = Field(..., ge=0)     # the version the editor loaded (CAS)
    max_discount_bps: Optional[int] = None
    near_expiry_days: Optional[int] = None
    min_shelf_days_at_sale: Optional[int] = None
    ipd_autopost: Optional[bool] = None
    ipd_price_basis: Optional[Literal["mrp", "cost_plus"]] = None
    ipd_cost_plus_bps: Optional[int] = None
    allow_loose_sale: Optional[bool] = None
    round_off_to_rupee: Optional[bool] = None
    refill_enabled: Optional[bool] = None
    refill_lead_days: Optional[int] = None
    receipt_format: Optional[Literal["80mm", "a5"]] = None
    fefo_override_requires_reason: Optional[bool] = None
    rx_validity_days: Optional[int] = None
    return_days: Optional[int] = None
    audit_approval_paise: Optional[int] = None
    pharmacist_reg_no: Optional[str] = None


class StoreIn(_In):
    code: str = Field(..., pattern=rx.STORE_CODE)
    name: str = Field(..., min_length=2, max_length=100)
    store_type: StoreType
    branch_id: Optional[str] = None
    parent_store_id: Optional[str] = None
    ward_id: Optional[str] = None
    can_sell: Optional[bool] = None
    can_receive_grn: Optional[bool] = None
    drug_licence_no: Optional[str] = Field(None, max_length=60)


class StorePatch(_In):
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    parent_store_id: Optional[str] = None
    can_sell: Optional[bool] = None
    can_receive_grn: Optional[bool] = None
    drug_licence_no: Optional[str] = Field(None, max_length=60)
    is_active: Optional[bool] = None


# ── scope helpers ───────────────────────────────────────────────────────────

async def _rx_scope(user: AdminUser, clinic_id: str, *perms: str, new_work: bool = False) -> tuple[str, dict]:
    """Any one of `perms`, tenant, entitlement. new_work=True also refuses in WIND_DOWN."""
    if perms and not any(holds_rx_permission(user, p) for p in perms):
        raise HTTPException(status_code=403, detail=f"Missing permission: {' or '.join(perms)}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    access = rx_access(clinic)
    if access is None:
        raise HTTPException(status_code=403, detail="Pharmacy OS is not enabled for this clinic.")
    if new_work and access == "WIND_DOWN":
        raise HTTPException(status_code=409, detail="Pharmacy OS is being switched off for this clinic: new sales, "
                                                    "purchases and configuration are closed.")
    return scope, clinic


def _store_for(user: AdminUser, store: dict) -> dict:
    """Store scope (SECURITY §3.3): outside the login's stores reads as not found."""
    allowed = allowed_store_ids(user, [store])
    if allowed is not None and str(store["id"]) not in allowed:
        raise HTTPException(status_code=404, detail="Store not found.")
    return store


def _clinic_wide(user: AdminUser) -> None:
    """Settings are clinic-wide: a branch-pinned login cannot change them."""
    if getattr(user, "branch_id", None):
        raise HTTPException(status_code=403, detail="Only a clinic-wide administrator can change pharmacy settings.")


def _pinned_branch(user: AdminUser) -> Optional[str]:
    return str(user.branch_id) if getattr(user, "branch_id", None) else None


async def _audit(user: AdminUser, request: Request, action: str, rtype: str, rid: Optional[str], details=None):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


async def _visible_stores(user: AdminUser, scope: str) -> list[dict]:
    stores = await rx.list_stores(scope)
    allowed = allowed_store_ids(user, stores)
    return stores if allowed is None else [s for s in stores if str(s["id"]) in allowed]


# ── setup and settings ──────────────────────────────────────────────────────

@router.get("/setup")
async def get_setup(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, "RX_ADMIN", "RX_READ")
    settings = rx.get_rx_settings(clinic)
    all_stores = await rx.list_stores(scope)
    allowed = allowed_store_ids(user, all_stores)
    stores = all_stores if allowed is None else [s for s in all_stores if str(s["id"]) in allowed]
    bq = scoped_query("branches", scope, "id, name, is_active").order("name")
    pinned = _pinned_branch(user)
    branches = (await sb(bq.eq("id", pinned) if pinned else bq)).data or []
    gstin = ((clinic.get("opd_settings") or {}).get("gstin") or "").strip() or None
    wards = []   # for ward sub-stores: names only, and only when IPD is on
    if ipd_access(clinic):
        wq = scoped_query("ipd_wards", scope, "id, name, branch_id, is_active").order("name")
        wards = [w for w in ((await sb(wq.eq("branch_id", pinned) if pinned else wq)).data or [])
                 if w.get("is_active") is not False]
    return {"access": rx_access(clinic), "state": clinic.get("rx_state"), "settings": settings, "stores": stores,
            # readiness is clinic-wide (a branch login still sees whether the clinic can sell); the list is scoped
            "branches": branches, "wards": wards,
            "checklist": rx.setup_checklist(settings, all_stores, gstin, {"items": await _active_item_count(scope)})}


async def _active_item_count(scope: str) -> int:
    # shortcut: counts up to 1000; the checklist only needs "any items yet"
    return len((await sb(scoped_query("rx_items", scope, "id").eq("is_active", True).limit(1000))).data or [])


@router.get("/settings")
async def get_settings(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _rx_scope(user, clinic_id, "RX_ADMIN", "RX_READ")
    return {"settings": rx.get_rx_settings(clinic)}


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, "RX_ADMIN")
    _clinic_wide(user)
    changes = body.model_dump(exclude_none=True, exclude={"version"})
    settings = await rx.update_rx_settings(scope, clinic, changes, body.version)
    invalidate_tenant_cache()
    await _audit(user, request, "rx_settings_update", "clinic", scope, {"keys": sorted(changes)})
    return {"settings": settings}


# ── stores ──────────────────────────────────────────────────────────────────

@router.get("/stores")
async def list_stores(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, "RX_ADMIN", "RX_READ")
    return {"stores": await _visible_stores(user, scope)}


async def _check_links(user: AdminUser, scope: str, branch_id: Optional[str], parent_id: Optional[str],
                       store_id: Optional[str] = None) -> None:
    if branch_id:
        await resolve_owned_branch(user, branch_id, scope)        # 404 other tenant, 403 other branch
    if parent_id:
        if parent_id == store_id:
            raise HTTPException(status_code=422, detail="A store cannot be its own parent.")
        _store_for(user, await rx.get_store(scope, parent_id))


@router.post("/stores", status_code=status.HTTP_201_CREATED)
async def create_store(body: StoreIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, "RX_STORE_MANAGE", "RX_ADMIN", new_work=True)
    pinned = _pinned_branch(user)
    if pinned and body.branch_id != pinned:
        raise HTTPException(status_code=403, detail="A branch login can only add stores of its own branch.")
    await _check_links(user, scope, body.branch_id, body.parent_store_id)
    if body.store_type == "ward_substore":
        if not body.ward_id:
            raise HTTPException(status_code=422, detail="A ward sub-store needs its ward.")
        await rx.assert_ward(scope, body.ward_id, body.branch_id)
    elif body.ward_id:
        raise HTTPException(status_code=422, detail="Only a ward sub-store has a ward.")
    row = body.model_dump(exclude_none=True)
    row.setdefault("can_sell", body.store_type == "retail_counter")
    row.setdefault("can_receive_grn", body.store_type in ("central", "retail_counter"))
    store = await rx.create_or_update_store(scope, row)
    await _audit(user, request, "rx_store_create", "rx_store", store["id"], {"code": body.code,
                                                                             "store_type": body.store_type})
    return store


@router.patch("/stores/{store_id}")
async def update_store(store_id: str, body: StorePatch, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, "RX_STORE_MANAGE", "RX_ADMIN")
    store = _store_for(user, await rx.get_store(scope, store_id))
    nullable = ("parent_store_id", "drug_licence_no")   # null clears these; never the rest
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None or k in nullable}
    if not patch:
        return store
    if patch.get("is_active") is False and store.get("is_active"):
        on_hand = (await sb(scoped_query("rx_v_item_stock", scope, "item_id").eq("store_id", store_id)
                            .gt("qty_units", 0).limit(1))).data
        if on_hand:
            raise HTTPException(status_code=409, detail="This store still holds stock. Transfer or write it off first.")
    if patch.get("is_active") is True and not store.get("is_active"):
        await _rx_scope(user, clinic_id, "RX_STORE_MANAGE", "RX_ADMIN", new_work=True)   # re-opening is new work
    await _check_links(user, scope, None, patch.get("parent_store_id"), store_id)
    store = await rx.create_or_update_store(scope, patch, store_id)
    await _audit(user, request, "rx_store_update", "rx_store", store_id, {"keys": sorted(patch)})
    return store


# ── shared helpers for the catalogue and stock routes ───────────────────────

CATALOGUE_READ = ("RX_READ", "RX_CATALOGUE", "RX_ADMIN")
CATALOGUE_WRITE = ("RX_CATALOGUE", "RX_ADMIN")
STOCK_WRITE = ("RX_ADJUST", "RX_ADMIN")


def _actor(user: AdminUser) -> tuple[Optional[str], str]:
    """(uuid or None, display name) for ledger / change-log rows; never a phone number."""
    try:
        uid = str(uuid.UUID(str(user.user_id))) if getattr(user, "user_id", None) else None
    except ValueError:
        uid = None
    return uid, str(getattr(user, "username", None) or "admin")[:100]


async def _store_ids(user: AdminUser, scope: str, store_id: Optional[uuid.UUID]) -> Optional[list[str]]:
    """The stores a read may cover: the one asked for (404 outside scope), else every visible
    store (None = all of the clinic's)."""
    if store_id:
        _store_for(user, await rx.get_store(scope, str(store_id)))
        return [str(store_id)]
    allowed = allowed_store_ids(user, await rx.list_stores(scope))
    return None if allowed is None else sorted(allowed)


async def _upload(file: UploadFile) -> list[dict]:
    raw = await file.read(rx_catalogue.MAX_IMPORT_BYTES + 1)
    return rx_catalogue.parse_table(raw, file.filename or "")


# ── catalogue: manufacturers, generics (RX-13) ──────────────────────────────

@router.get("/manufacturers")
async def list_manufacturers(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_READ)
    return {"manufacturers": await rx_catalogue.list_manufacturers(scope)}


@router.post("/manufacturers", status_code=status.HTTP_201_CREATED)
async def create_manufacturer(body: ManufacturerIn, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_WRITE, new_work=True)
    row = await rx_catalogue.create_manufacturer(scope, body)
    await _audit(user, request, "rx_manufacturer_create", "rx_manufacturer", row["id"], {"name": body.name})
    return row


@router.get("/generics")
async def list_generics(q: Optional[str] = Query(None, max_length=80), clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_READ)
    return {"generics": await rx_catalogue.list_generics(scope, q)}


@router.post("/generics", status_code=status.HTTP_201_CREATED)
async def create_generic(body: GenericIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_WRITE, new_work=True)
    row = await rx_catalogue.create_generic(scope, body)
    await _audit(user, request, "rx_generic_create", "rx_generic", row["id"], {"name": body.name})
    return row


# ── catalogue: items, barcodes (RX-13), resolver (RX-15), import (RX-14) ────

@router.get("/items")
async def list_items(q: Optional[str] = Query(None, max_length=80), generic_id: Optional[uuid.UUID] = None,
                     schedule: Optional[rx_catalogue.Schedule] = None, active: Optional[bool] = None,
                     limit: int = Query(50, ge=1, le=200), clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_READ)
    items = await rx_catalogue.list_items(scope, q, str(generic_id) if generic_id else None, schedule, active, limit)
    stock = {r["item"]["id"]: r for r in await rx_stock.stock(scope, await _store_ids(user, scope, None))}
    for i in items:
        i["stock_units"] = stock[i["id"]]["sellable_units"] if i["id"] in stock else 0
    return {"items": items}


@router.post("/items", status_code=status.HTTP_201_CREATED)
async def create_item(body: ItemIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_WRITE, new_work=True)
    item = await rx_catalogue.create_item(scope, body)
    await _audit(user, request, "rx_item_create", "rx_item", item["id"],
                 {"item_code": body.item_code, "schedule": body.schedule})
    return item


@router.get("/items/resolve")
async def resolve_items(q: str = Query(..., min_length=1, max_length=120), store_id: Optional[uuid.UUID] = None,
                        limit: int = Query(5, ge=1, le=20), clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    """Deterministic candidates (barcode > alias > code > brand > generic) and, for the best one,
    in-stock substitutes of the same generic. The pharmacist confirms; nothing is auto-picked."""
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_READ)
    store_ids = await _store_ids(user, scope, store_id)
    candidates = await rx_catalogue.resolve(scope, q, limit)
    subs = await rx_catalogue.substitutes(scope, candidates[0], store_ids) if candidates else []
    return {"candidates": candidates, "substitutes": subs}


@router.post("/items/import/dry-run")
async def import_items_dry_run(file: UploadFile = File(...), clinic_id: str = "default",
                               user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_WRITE, new_work=True)
    rows, errors = rx_catalogue.validate_item_rows(await _upload(file))
    res = await rx_catalogue.import_items(scope, rows, False, *_actor(user)) if rows else \
        {"would_create": 0, "would_update": 0, "unchanged": 0, "errors": [], "applied": False}
    errors = sorted(errors + res["errors"], key=lambda e: e["row"] or 0)
    return {**res, "errors": errors, "valid": not errors, "rows": len(rows) + len({e["row"] for e in errors})}


@router.post("/items/import/apply")
async def import_items_apply(request: Request, file: UploadFile = File(...), clinic_id: str = "default",
                             user: AdminUser = Depends(verify_credentials)):
    """All or nothing: any row error (file or database) means nothing is written."""
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_WRITE, new_work=True)
    rows, errors = rx_catalogue.validate_item_rows(await _upload(file))
    if errors or not rows:
        raise HTTPException(status_code=422, detail={"code": "rx_import_invalid", "errors": errors,
                                                     "message": "Fix the listed rows (run the dry run) and upload again."})
    res = await rx_catalogue.import_items(scope, rows, True, *_actor(user))
    if not res.get("applied"):
        raise HTTPException(status_code=422, detail={"code": "rx_import_invalid", "errors": res["errors"],
                                                     "message": "Nothing was imported. Fix the listed rows."})
    await _audit(user, request, "rx_item_import", "clinic", scope,
                 {k: res[k] for k in ("would_create", "would_update", "unchanged")})
    return {"created": res["would_create"], "updated": res["would_update"], "unchanged": res["unchanged"]}


@router.get("/items/{item_id}")
async def get_item(item_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_READ)
    item = await rx_catalogue.item_detail(scope, str(item_id))
    item["substitutes"] = await rx_catalogue.substitutes(scope, item, await _store_ids(user, scope, None))
    return item


@router.patch("/items/{item_id}")
async def update_item(item_id: uuid.UUID, body: ItemPatch, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_WRITE, new_work=True)
    nullable = ("ved_class", "rack_location", "manufacturer_id")       # null clears these; never the rest
    patch = {k: v for k, v in body.model_dump(exclude_unset=True, exclude={"reason"}).items()
             if v is not None or k in nullable}
    if not patch:
        return await rx_catalogue.get_item(scope, str(item_id))
    item = await rx_catalogue.update_item(scope, str(item_id), patch, *_actor(user), body.reason)
    await _audit(user, request, "rx_item_update", "rx_item", str(item_id), {"keys": sorted(patch)})
    return item


@router.post("/items/{item_id}/barcodes", status_code=status.HTTP_201_CREATED)
async def add_barcode(item_id: uuid.UUID, body: BarcodeIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *CATALOGUE_WRITE, new_work=True)
    row = await rx_catalogue.add_barcode(scope, str(item_id), body)
    await _audit(user, request, "rx_barcode_add", "rx_item", str(item_id), {"symbology": row["symbology"]})
    return row


# ── stock reads (RX-18) ─────────────────────────────────────────────────────

@router.get("/stock")
async def get_stock(store_id: Optional[uuid.UUID] = None, q: Optional[str] = Query(None, max_length=80),
                    schedule: Optional[rx_catalogue.Schedule] = None, below_reorder: bool = False,
                    clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, "RX_READ")
    with_value = holds_rx_permission(user, "RX_ADMIN") or holds_rx_permission(user, "RX_PURCHASE")
    return {"stock": await rx_stock.stock(scope, await _store_ids(user, scope, store_id), q, schedule, below_reorder,
                                          with_value)}


@router.get("/batches")
async def get_batches(item_id: uuid.UUID, store_id: Optional[uuid.UUID] = None,
                      required_qty: Optional[int] = Query(None, ge=1, le=1_000_000), clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    """Batches of one item, FEFO-ranked, with expiry status; with store_id + required_qty also the FEFO pick."""
    scope, clinic = await _rx_scope(user, clinic_id, "RX_READ")
    store_ids = await _store_ids(user, scope, store_id)
    out = {"batches": await rx_stock.batches(scope, str(item_id), store_ids, rx.get_rx_settings(clinic))}
    if store_id and required_qty:
        out["fefo"] = await rx_stock.get_fefo_batches(scope, str(store_id), str(item_id), required_qty)
    return out


@router.get("/expiry")
async def get_expiry(days: int = 90, store_id: Optional[uuid.UUID] = None,
                     clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, "RX_READ")
    if days not in (30, 60, 90):
        raise HTTPException(status_code=422, detail="days is 30, 60 or 90.")
    return await rx_stock.expiry(scope, await _store_ids(user, scope, store_id), days)


@router.get("/reorder")
async def get_reorder(store_id: Optional[uuid.UUID] = None, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, "RX_READ")
    return {"reorder": await rx_stock.reorder(scope, await _store_ids(user, scope, store_id))}


# ── quarantine, write-offs (RX-19), opening stock (RX-20) ───────────────────

class BlockIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)


class WriteoffLine(_In):
    batch_id: uuid.UUID
    qty_units: int = Field(..., ge=1, le=1_000_000)
    kind: Literal["expiry", "damage"]
    reason: str = Field(..., min_length=3, max_length=300)


class WriteoffIn(_In):
    store_id: uuid.UUID
    lines: list[WriteoffLine] = Field(..., min_length=1, max_length=200)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=80)


async def _batch_in_scope(user: AdminUser, scope: str, batch_id: str) -> dict:
    """A branch login may quarantine only batches held in its own stores (404 otherwise)."""
    batch = await rx_stock.get_batch(scope, batch_id)
    allowed = allowed_store_ids(user, await rx.list_stores(scope))
    if allowed is not None and not (await rx_stock.batch_store_ids(scope, batch_id)) & allowed:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return batch


@router.post("/batches/{batch_id}/block")
async def block_batch(batch_id: uuid.UUID, body: BlockIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *STOCK_WRITE)          # recalls stay possible in wind-down
    await _batch_in_scope(user, scope, str(batch_id))
    batch = await rx_stock.set_blocked(scope, str(batch_id), True, body.reason)
    await _audit(user, request, "rx_batch_block", "rx_batch", str(batch_id), {"reason": body.reason})
    return batch


@router.post("/batches/{batch_id}/unblock")
async def unblock_batch(batch_id: uuid.UUID, body: BlockIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *STOCK_WRITE)
    await _batch_in_scope(user, scope, str(batch_id))
    batch = await rx_stock.set_blocked(scope, str(batch_id), False, None)
    await _audit(user, request, "rx_batch_unblock", "rx_batch", str(batch_id), {"reason": body.reason})
    return batch


@router.post("/write-offs", status_code=status.HTTP_201_CREATED)
async def post_writeoff(body: WriteoffIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *STOCK_WRITE)          # clearing expired stock is not new work
    _store_for(user, await rx.get_store(scope, str(body.store_id)))
    keys = [(str(line.batch_id), line.kind) for line in body.lines]
    if len(set(keys)) != len(keys):
        raise HTTPException(status_code=422, detail="Each batch appears once per write-off kind.")
    lines = [{**line.model_dump(), "batch_id": str(line.batch_id)} for line in body.lines]
    res = await rx_stock.post_writeoff(scope, str(body.store_id), rx_stock.writeoff_ref(scope, body.idempotency_key),
                                       lines, *_actor(user))
    if not res.get("replayed"):
        await _audit(user, request, "rx_writeoff", "rx_store", str(body.store_id),
                     {"ref_id": res["ref_id"], "lines": len(lines), "units": res["units"]})
    return res


@router.post("/stock/opening-import")
async def opening_import(request: Request, store_id: uuid.UUID, dry_run: bool = True, file: UploadFile = File(...),
                         clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    """Opening stock CSV/XLSX (ROLLOUT §4.3). dry_run=true (default) writes nothing; apply is all or
    nothing, idempotent per batch × store, and refused once the store has traded."""
    scope, clinic = await _rx_scope(user, clinic_id, *STOCK_WRITE, new_work=True)
    _store_for(user, await rx.get_store(scope, str(store_id)))
    gstin = ((clinic.get("opd_settings") or {}).get("gstin") or "").strip()
    rows, errors = await rx_stock.validate_opening_rows(scope, await _upload(file), bool(gstin))
    if errors and not dry_run:
        raise HTTPException(status_code=422, detail={"code": "rx_import_invalid", "errors": errors,
                                                     "message": "Fix the listed rows (run the dry run) and upload again."})
    res = await rx_stock.opening_stock(scope, str(store_id), rows, not dry_run, *_actor(user)) if rows else \
        {"new_batches": 0, "movements": 0, "units": 0, "already_loaded": 0, "errors": [], "applied": False}
    errors = sorted(errors + res["errors"], key=lambda e: e["row"] or 0)
    if not dry_run:
        if not res.get("applied"):
            raise HTTPException(status_code=422, detail={"code": "rx_import_invalid", "errors": errors,
                                                         "message": "Nothing was loaded. Fix the listed rows."})
        await _audit(user, request, "rx_opening_stock", "rx_store", str(store_id),
                     {"movements": res["movements"], "units": res["units"]})
    return {**res, "errors": errors, "valid": not errors}


# ── purchase: suppliers, purchase orders, GRN, supplier returns (137, RX-23…RX-26) ──

PURCHASE_READ = ("RX_READ", "RX_PURCHASE", "RX_ADMIN")
PURCHASE_WRITE = ("RX_PURCHASE", "RX_ADMIN")


class ReasonIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)


class PostIn(_In):
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=80)


async def _doc_in_scope(user: AdminUser, scope: str, doc: dict) -> dict:
    """A document is visible only through its store (SECURITY §3.3): outside scope reads as not found."""
    _store_for(user, await rx.get_store(scope, str(doc["store_id"])))
    return doc


def _pdf(content: bytes, name: str) -> Response:
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "no-store"})


@router.get("/suppliers")
async def list_suppliers(active: Optional[bool] = None, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_READ)
    return {"suppliers": await rx_purchase.list_suppliers(scope, active)}


@router.post("/suppliers", status_code=status.HTTP_201_CREATED)
async def create_supplier(body: SupplierIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_WRITE, new_work=True)
    row = await rx_purchase.create_supplier(scope, body)
    await _audit(user, request, "rx_supplier_create", "rx_supplier", row["id"], {"has_gstin": bool(body.gstin)})
    return row


@router.patch("/suppliers/{supplier_id}")
async def update_supplier(supplier_id: uuid.UUID, body: SupplierPatch, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_WRITE, new_work=True)
    row = await rx_purchase.update_supplier(scope, str(supplier_id), body)
    await _audit(user, request, "rx_supplier_update", "rx_supplier", str(supplier_id),
                 {"keys": sorted(body.model_dump(exclude_unset=True))})
    return row


@router.get("/purchase-orders")
async def list_pos(status_: Optional[Literal["draft", "sent", "partially_received", "received", "cancelled"]] = Query(
                       None, alias="status"), supplier_id: Optional[uuid.UUID] = None, store_id: Optional[uuid.UUID] = None,
                   clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_READ)
    return {"purchase_orders": await rx_purchase.list_pos(scope, await _store_ids(user, scope, store_id), status_,
                                                          str(supplier_id) if supplier_id else None)}


@router.post("/purchase-orders", status_code=status.HTTP_201_CREATED)
async def create_po(body: POIn, request: Request, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_WRITE, new_work=True)
    _store_for(user, await rx.get_store(scope, body.store_id))
    po = await rx_purchase.create_po(scope, body, _actor(user))
    await _audit(user, request, "rx_po_create", "rx_purchase_order", po["id"], {"lines": len(body.lines)})
    return po


@router.get("/purchase-orders/{po_id}.pdf")
async def po_pdf(po_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *PURCHASE_READ)
    po = await _doc_in_scope(user, scope, await rx_purchase.get_po(scope, str(po_id)))
    store = await rx.get_store(scope, str(po["store_id"]))
    return _pdf(rx_purchase.render_po_pdf(po, clinic, store), f"{po.get('po_number') or 'PO-draft'}.pdf")


@router.get("/purchase-orders/{po_id}")
async def get_po(po_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_READ)
    return await _doc_in_scope(user, scope, await rx_purchase.get_po(scope, str(po_id)))


@router.patch("/purchase-orders/{po_id}")
async def update_po(po_id: uuid.UUID, body: POPatch, request: Request, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_WRITE, new_work=True)
    await _doc_in_scope(user, scope, await rx_purchase.get_po(scope, str(po_id)))
    po = await rx_purchase.update_po(scope, str(po_id), body)
    await _audit(user, request, "rx_po_update", "rx_purchase_order", str(po_id),
                 {"keys": sorted(body.model_dump(exclude_unset=True))})
    return po


@router.post("/purchase-orders/{po_id}/send")
async def send_po(po_id: uuid.UUID, request: Request, clinic_id: str = "default",
                  user: AdminUser = Depends(verify_credentials)):
    """Approval step: RX_PURCHASE and RX_ADMIN (a Pharmacy Manager holds both)."""
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_WRITE, new_work=True)
    if not (holds_rx_permission(user, "RX_PURCHASE") and holds_rx_permission(user, "RX_ADMIN")):
        raise HTTPException(status_code=403, detail="Missing permission: RX_PURCHASE and RX_ADMIN (manager approval)")
    await _doc_in_scope(user, scope, await rx_purchase.get_po(scope, str(po_id)))
    out = await rx_purchase.send_po(scope, str(po_id))
    if not out.get("replayed"):
        await _audit(user, request, "rx_po_send", "rx_purchase_order", str(po_id), {"po_number": out["po_number"]})
    return out


@router.post("/purchase-orders/{po_id}/cancel")
async def cancel_po(po_id: uuid.UUID, body: ReasonIn, request: Request, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_WRITE)
    await _doc_in_scope(user, scope, await rx_purchase.get_po(scope, str(po_id)))
    po = await rx_purchase.cancel_po(scope, str(po_id), body.reason)
    await _audit(user, request, "rx_po_cancel", "rx_purchase_order", str(po_id), {"reason": body.reason})
    return po


@router.get("/grns")
async def list_grns(status_: Optional[Literal["draft", "posted", "void"]] = Query(None, alias="status"),
                    supplier_id: Optional[uuid.UUID] = None, store_id: Optional[uuid.UUID] = None,
                    clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_READ)
    return {"grns": await rx_purchase.list_grns(scope, await _store_ids(user, scope, store_id), status_,
                                                str(supplier_id) if supplier_id else None)}


@router.post("/grns", status_code=status.HTTP_201_CREATED)
async def create_grn(body: GrnIn, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *PURCHASE_WRITE, new_work=True)
    store = _store_for(user, await rx.get_store(scope, body.store_id))
    g = await rx_purchase.create_grn(scope, clinic, store, body, _actor(user), rx.get_rx_settings(clinic))
    await _audit(user, request, "rx_grn_create", "rx_grn", g["id"], {"lines": len(body.lines)})
    return g


@router.get("/grns/{grn_id}/labels.pdf")
async def grn_labels(grn_id: uuid.UUID, copies: Literal["1", "per_pack", "per_unit"] = "1", clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *PURCHASE_READ)
    g = await _doc_in_scope(user, scope, await rx_purchase.get_grn(scope, str(grn_id), rx.get_rx_settings(clinic)))
    return _pdf(rx_purchase.render_labels_pdf(g, copies), f"labels-{g.get('grn_number') or 'draft'}.pdf")


@router.get("/grns/{grn_id}")
async def get_grn(grn_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *PURCHASE_READ)
    return await _doc_in_scope(user, scope, await rx_purchase.get_grn(scope, str(grn_id), rx.get_rx_settings(clinic)))


@router.patch("/grns/{grn_id}")
async def update_grn(grn_id: uuid.UUID, body: GrnPatch, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *PURCHASE_WRITE, new_work=True)
    settings = rx.get_rx_settings(clinic)
    await _doc_in_scope(user, scope, await rx_purchase.get_grn(scope, str(grn_id), settings))
    g = await rx_purchase.update_grn(scope, str(grn_id), body, settings)
    await _audit(user, request, "rx_grn_update", "rx_grn", str(grn_id),
                 {"keys": sorted(body.model_dump(exclude_unset=True))})
    return g


@router.post("/grns/{grn_id}/post")
async def post_grn(grn_id: uuid.UUID, request: Request, body: Optional[PostIn] = None, clinic_id: str = "default",
                   user: AdminUser = Depends(verify_credentials)):
    """rx_post_grn is idempotent per GRN: a retry (same or no key) returns the posted number, replayed."""
    scope, clinic = await _rx_scope(user, clinic_id, *PURCHASE_WRITE, new_work=True)
    await _doc_in_scope(user, scope, await rx_purchase.get_grn(scope, str(grn_id), rx.get_rx_settings(clinic)))
    out = await rx_purchase.post_grn(scope, str(grn_id), _actor(user))
    if not out.get("replayed"):
        await _audit(user, request, "rx_grn_post", "rx_grn", str(grn_id),
                     {"grn_number": out["grn_number"], "units": out["units"]})
    return out


@router.post("/grns/{grn_id}/void")
async def void_grn(grn_id: uuid.UUID, body: ReasonIn, request: Request, clinic_id: str = "default",
                   user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, "RX_ADMIN")
    await _doc_in_scope(user, scope, await rx_purchase.get_grn(scope, str(grn_id), rx.get_rx_settings(clinic)))
    out = await rx_purchase.void_grn(scope, str(grn_id), body.reason, _actor(user))
    if not out.get("replayed"):
        await _audit(user, request, "rx_grn_void", "rx_grn", str(grn_id), {"reason": body.reason})
    return out


@router.get("/supplier-returns")
async def list_returns(status_: Optional[Literal["draft", "dispatched", "cancelled"]] = Query(None, alias="status"),
                       store_id: Optional[uuid.UUID] = None, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_READ)
    return {"returns": await rx_purchase.list_returns(scope, await _store_ids(user, scope, store_id), status_)}


@router.post("/supplier-returns", status_code=status.HTTP_201_CREATED)
async def create_return(body: ReturnIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_WRITE)      # clearing expired stock continues in wind-down
    _store_for(user, await rx.get_store(scope, body.store_id))
    r = await rx_purchase.create_return(scope, body, _actor(user))
    await _audit(user, request, "rx_supplier_return_create", "rx_supplier_return", r["id"],
                 {"reason": body.reason, "lines": len(body.lines)})
    return r


@router.get("/supplier-returns/{return_id}")
async def get_return(return_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_READ)
    return await _doc_in_scope(user, scope, await rx_purchase.get_return(scope, str(return_id)))


@router.post("/supplier-returns/{return_id}/dispatch")
async def dispatch_return(return_id: uuid.UUID, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_WRITE)
    await _doc_in_scope(user, scope, await rx_purchase.get_return(scope, str(return_id)))
    out = await rx_purchase.dispatch_return(scope, str(return_id), _actor(user))
    if not out.get("replayed"):
        await _audit(user, request, "rx_supplier_return_dispatch", "rx_supplier_return", str(return_id),
                     {"srn_number": out["srn_number"], "units": out["units"]})
    return out


@router.patch("/supplier-returns/{return_id}/credit")
async def record_credit(return_id: uuid.UUID, body: CreditIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_WRITE)
    await _doc_in_scope(user, scope, await rx_purchase.get_return(scope, str(return_id)))
    r = await rx_purchase.record_credit(scope, str(return_id), body)
    await _audit(user, request, "rx_supplier_credit", "rx_supplier_return", str(return_id),
                 {"credit_received_paise": body.credit_received_paise})
    return r


@router.get("/gs1")
async def parse_gs1(code: str = Query(..., min_length=4, max_length=200), clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    """A3: one scan of a GS1 DataMatrix / pharma QR → GTIN, batch, expiry, serial and the matching item."""
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_READ, "RX_DISPENSE")
    try:
        parsed = rx_gs1.parse(code)
    except rx_gs1.GS1Error as e:
        raise HTTPException(status_code=422, detail={"code": "rx_gs1_invalid", "message": str(e)})
    rows = (await sb(scoped_query("rx_item_barcodes", scope, "item_id, barcode")
                     .in_("barcode", rx_gs1.barcode_candidates(parsed["gtin"])).limit(1))).data or []
    item = await rx_catalogue.get_item(scope, rows[0]["item_id"]) if rows else None
    return {**parsed, "expiry_date": str(parsed["expiry_date"]) if parsed["expiry_date"] else None,
            "mfg_date": str(parsed["mfg_date"]) if parsed["mfg_date"] else None, "item": item}


# ── counter sales (138): lookup, drafts, completion, returns, void, park, overrides, Z-report ──

POS_WORK = ("RX_DISPENSE", "RX_ADMIN")
POS_READ = ("RX_READ", "RX_DISPENSE", "RX_ADMIN")


async def _sale_in_scope(user: AdminUser, scope: str, sale_id: uuid.UUID) -> dict:
    sale = await rx_pos.get_sale_row(scope, str(sale_id))
    _store_for(user, await rx.get_store(scope, str(sale["store_id"])))
    return sale


async def _shift(scope: str, user: AdminUser, shift_id: Optional[str], cash: bool) -> Optional[str]:
    """Cash goes into the actor's open OPD cashier shift (D5, resolve_counter_shift); UPI / card need none."""
    if not cash:
        return None
    from app.services.opd_billing import resolve_counter_shift
    return await resolve_counter_shift(scope, user, shift_id, require_drawer=True)


@router.get("/pos/inbox")
async def pos_inbox(store_id: uuid.UUID, since: Optional[str] = Query(None, max_length=40), clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *POS_WORK)
    store = _store_for(user, await rx.get_store(scope, str(store_id)))
    return {"prescriptions": await rx_pos.inbox(scope, store, since)}


@router.get("/pos/lookup")
async def pos_lookup(q: str = Query(..., min_length=1, max_length=80), clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *POS_WORK)
    return await rx_pos.lookup(scope, q, rx.get_rx_settings(clinic))


@router.get("/pos/day-summary")
async def pos_day_summary(day: Optional[date] = Query(None, alias="date"), store_id: Optional[uuid.UUID] = None,
                          shift_id: Optional[uuid.UUID] = None, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *POS_READ)
    from app.utils.helpers import today_ist
    return await rx_pos.day_summary(scope, await _store_ids(user, scope, store_id), day or today_ist(),
                                    str(shift_id) if shift_id else None)


@router.get("/sales")
async def list_sales(status_: Optional[Literal["draft", "completed", "void"]] = Query(None, alias="status"),
                     day_from: Optional[date] = Query(None, alias="from"), day_to: Optional[date] = Query(None, alias="to"),
                     store_id: Optional[uuid.UUID] = None, q: Optional[str] = Query(None, max_length=40),
                     parked: Optional[bool] = None, limit: int = Query(50, ge=1, le=200), clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *POS_READ)
    return {"sales": await rx_pos.list_sales(scope, await _store_ids(user, scope, store_id), status_, day_from, day_to, q,
                                             parked, limit)}


@router.post("/sales", status_code=status.HTTP_201_CREATED)
async def create_sale(body: rx_pos.SaleIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *POS_WORK, new_work=True)
    store = _store_for(user, await rx.get_store(scope, body.store_id))
    if not (store.get("can_sell") and store.get("is_active")):
        raise HTTPException(status_code=409, detail={"code": "rx_store_cannot_sell", "message": "This store does not sell."})
    if body.sale_kind == "staff" and not holds_rx_permission(user, "RX_ADMIN"):
        raise HTTPException(status_code=403, detail="Missing permission: RX_ADMIN (staff sale)")
    settings = rx.get_rx_settings(clinic)
    out = await rx_pos.create_sale(scope, store, body, _actor(user), settings)
    if not out["existing"]:
        await _audit(user, request, "rx_sale_draft", "rx_sale", out["sale_id"],
                     {"kind": body.sale_kind, "rx": bool(body.opd_prescription_id), "repeat": bool(body.repeat_of_sale_id)})
    return {**await rx_pos.sale_detail(scope, out["sale_id"], settings), "mapping": out["mapping"],
            "existing_draft": out["existing"]}


@router.get("/sales/{sale_id}")
async def get_sale(sale_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *POS_READ)
    await _sale_in_scope(user, scope, sale_id)
    return await rx_pos.sale_detail(scope, str(sale_id), rx.get_rx_settings(clinic))


@router.put("/sales/{sale_id}/lines")
async def put_sale_lines(sale_id: uuid.UUID, lines: list[rx_pos.LineIn], clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *POS_WORK, new_work=True)
    sale = await _sale_in_scope(user, scope, sale_id)
    settings = rx.get_rx_settings(clinic)
    await rx_pos.replace_lines(scope, sale, lines, settings)
    return await rx_pos.sale_detail(scope, str(sale_id), settings)


@router.patch("/sales/{sale_id}")
async def patch_sale(sale_id: uuid.UUID, body: rx_pos.SalePatch, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *POS_WORK, new_work=True)
    sale = await _sale_in_scope(user, scope, sale_id)
    settings = rx.get_rx_settings(clinic)
    needs = await rx_pos.patch_sale(scope, sale, body, int(settings["max_discount_bps"]),
                                    holds_rx_permission(user, "RX_ADMIN"))
    return {**await rx_pos.sale_detail(scope, str(sale_id), settings), "discount_needs_override": needs}


@router.delete("/sales/{sale_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sale(sale_id: uuid.UUID, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *POS_WORK)
    sale = await _sale_in_scope(user, scope, sale_id)
    await rx_pos.delete_draft(scope, sale)
    await _audit(user, request, "rx_sale_draft_delete", "rx_sale", str(sale_id), {})
    return Response(status_code=204)


@router.post("/sales/{sale_id}/complete")
async def complete_sale(sale_id: uuid.UUID, body: rx_pos.CompleteIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *POS_WORK, new_work=True)
    sale = await _sale_in_scope(user, scope, sale_id)
    shift = await _shift(scope, user, body.shift_id, any(p.mode == "cash" for p in body.payments))
    out = await rx_pos.complete(scope, sale, body, shift, _actor(user), holds_rx_permission(user, "RX_ADMIN"),
                                holds_rx_permission(user, "RX_CONTROLLED"))
    if not out.get("replayed"):
        await _audit(user, request, "rx_sale_complete", "rx_sale", str(sale_id),
                     {"sale_number": out["sale_number"], "net_paise": out["net_paise"],
                      "modes": sorted({p.mode for p in body.payments}), "override": out.get("override_used")})
    refill_plans = []
    if not out.get("replayed"):
        await rx_refills.mark_fulfilled(scope, str(sale_id))          # a packed refill completed at the counter
        if body.refill_opt_in:                                         # W10 step 1, pharmacist's tick (X16)
            refill_plans = await rx_refills.create_plans(scope, str(sale_id), _actor(user)[0], rx.get_rx_settings(clinic))
    detail = await rx_pos.sale_detail(scope, str(sale_id), rx.get_rx_settings(clinic))
    return {**detail, "replayed": out.get("replayed", False), "refill_plans": len(refill_plans),
            "receipt_url": f"/admin/rx/sales/{sale_id}/receipt?format={rx.get_rx_settings(clinic)['receipt_format']}"}


@router.post("/sales/{sale_id}/void")
async def void_sale(sale_id: uuid.UUID, body: rx_pos.VoidIn, request: Request, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, "RX_ADMIN")
    sale = await _sale_in_scope(user, scope, sale_id)
    shift = await _shift(scope, user, body.shift_id, await rx_pos.has_cash(scope, str(sale_id)))
    out = await rx_pos.void(scope, sale, body.reason, shift, _actor(user))
    if not out.get("replayed"):
        await _audit(user, request, "rx_sale_void", "rx_sale", str(sale_id), {"reason": body.reason})
    return out


@router.post("/sales/{sale_id}/park")
async def park_sale(sale_id: uuid.UUID, body: rx_pos.ParkIn, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *POS_WORK)
    return await rx_pos.park(scope, await _sale_in_scope(user, scope, sale_id), body.label)


@router.post("/sales/{sale_id}/recall")
async def recall_sale(sale_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *POS_WORK)
    await rx_pos.park(scope, await _sale_in_scope(user, scope, sale_id), None)
    return await rx_pos.sale_detail(scope, str(sale_id), rx.get_rx_settings(clinic))


@router.post("/sales/{sale_id}/returns", status_code=status.HTTP_201_CREATED)
async def sale_return(sale_id: uuid.UUID, body: rx_pos.ReturnIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *POS_WORK)        # returns continue in wind-down
    sale = await _sale_in_scope(user, scope, sale_id)
    shift = await _shift(scope, user, body.shift_id, body.refund_mode == "cash")
    out = await rx_pos.record_return(scope, sale, body, shift, _actor(user), holds_rx_permission(user, "RX_ADMIN"))
    if not out.get("replayed"):
        await _audit(user, request, "rx_sale_return", "rx_sale", str(sale_id),
                     {"return_number": out["return_number"], "amount_paise": out["amount_paise"],
                      "refund_mode": body.refund_mode})
    return out


@router.get("/sales/{sale_id}/receipt")
async def sale_receipt(sale_id: uuid.UUID, fmt: Literal["80mm", "a5"] = Query("80mm", alias="format"),
                       clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *POS_READ)
    sale = await _sale_in_scope(user, scope, sale_id)
    settings = rx.get_rx_settings(clinic)
    model = rx_pos_docs.receipt_model(await rx_pos.sale_detail(scope, str(sale_id), settings), clinic,
                                      await rx.get_store(scope, str(sale["store_id"])), settings)
    if fmt == "a5":
        return _pdf(rx_pos_docs.render_receipt_pdf(model), f"{model['number']}.pdf")
    return Response(content=rx_pos_docs.render_receipt_html(model, fmt), media_type="text/html",
                    headers={"Cache-Control": "no-store"})


@router.get("/sales/{sale_id}/labels.pdf")
async def sale_labels(sale_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *POS_READ)
    await _sale_in_scope(user, scope, sale_id)
    detail = await rx_pos.sale_detail(scope, str(sale_id), rx.get_rx_settings(clinic))
    lang = ((detail.get("patient_panel") or {}).get("patient") or {}).get("language") or "en"
    return _pdf(rx_pos_docs.render_labels_pdf(detail, clinic, lang), f"labels-{detail.get('sale_number') or 'draft'}.pdf")


@router.get("/credit-notes/{cn_id}.pdf")
async def credit_note_pdf(cn_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *POS_READ)
    rows = (await sb(scoped_query("rx_credit_notes", scope).eq("id", str(cn_id)).limit(1))).data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Credit note not found.")
    sale = await _sale_in_scope(user, scope, uuid.UUID(str(rows[0]["sale_id"])))
    return _pdf(rx_pos_docs.render_credit_note_pdf(rows[0], sale, clinic), f"{rows[0]['cn_number']}.pdf")


@router.post("/overrides", status_code=status.HTTP_201_CREATED)
async def grant_override(body: rx_pos.OverrideIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    """A9: a second person holding RX_ADMIN re-authenticates here; the token lives 60 s and is single-use."""
    scope, _ = await _rx_scope(user, clinic_id, *POS_WORK)
    requester, _name = _actor(user)
    if not requester:
        raise HTTPException(status_code=403, detail="Overrides are requested from a staff login.")
    if not rx_pos.override_rate_ok(requester):
        raise HTTPException(status_code=429, detail="Too many override requests. Wait a minute.")
    if body.sale_id:
        await _sale_in_scope(user, scope, uuid.UUID(body.sale_id))
    from app.routers.admin import _authenticate_password
    approver = await _authenticate_password(body.approver_username, body.approver_password,
                                            request.client.host if request.client else "unknown")
    approver_id = _actor(approver)[0]
    if str(getattr(approver, "clinic_id", None) or "") != scope or not approver_id \
            or not holds_rx_permission(approver, "RX_ADMIN"):
        raise HTTPException(status_code=403, detail={"code": "rx_override_not_authorised",
                                                     "message": "The approver must be a pharmacy manager of this clinic."})
    if approver_id == requester:
        raise HTTPException(status_code=403, detail={"code": "rx_override_same_person",
                                                     "message": "The approver must be a different person."})
    out = await rx_pos.grant_override(scope, body, requester, approver)
    await _audit(user, request, "rx_override_grant", "rx_override", out["override_id"],
                 {"action": body.action, "sale_id": body.sale_id, "approver": approver.username,
                  "amount_paise": body.amount_paise})
    return out


# ── inpatient pharmacy: ward indents, dispense, receipt, slip, returns (139, RX-38…40) ──
# API.md §6: also needs ipd_access(clinic) (ACTIVE or WIND_DOWN, VERIFICATION D6); SQL re-checks the stay.

IPD_WARD = ("IPD_NURSING", "RX_DISPENSE", "RX_ADMIN")
IPD_PHARMACY = ("RX_DISPENSE", "RX_ADMIN")
IPD_NURSE = ("IPD_NURSING", "RX_ADMIN")


async def _ipd_scope(user: AdminUser, clinic_id: str, *perms: str, new_work: bool = False) -> tuple[str, dict]:
    scope, clinic = await _rx_scope(user, clinic_id, *perms, new_work=new_work)
    if ipd_access(clinic) is None:
        raise HTTPException(status_code=403, detail="The IPD module is not enabled for this clinic.")
    return scope, clinic


async def _admission_in_scope(user: AdminUser, scope: str, admission_id: str) -> dict:
    adm = await rx_ipd.get_admission(scope, admission_id)
    pinned = _pinned_branch(user)
    if pinned and adm.get("branch_id") and str(adm["branch_id"]) != pinned:
        raise HTTPException(status_code=404, detail="Admission not found.")
    return adm


async def _indent_in_scope(user: AdminUser, scope: str, indent_id: uuid.UUID) -> dict:
    indent = await rx_ipd.get_indent_row(scope, str(indent_id))
    _store_for(user, await rx.get_store(scope, str(indent["from_store_id"])))
    return indent


async def _dispense_in_scope(user: AdminUser, scope: str, dispense_id: uuid.UUID) -> dict:
    d = await rx_ipd.get_dispense_row(scope, str(dispense_id))
    _store_for(user, await rx.get_store(scope, str(d["store_id"])))
    return d


@router.get("/ipd/admissions/{admission_id}/indentable")
async def ipd_indentable(admission_id: uuid.UUID, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *IPD_WARD)
    adm = await _admission_in_scope(user, scope, str(admission_id))
    rx_ipd.require_active(adm)
    # the stores a nurse may ask: a nurse holds no RX_READ, so /stores is closed to them
    stores = [{k: s[k] for k in ("id", "code", "name", "store_type")} for s in await _visible_stores(user, scope)
              if s.get("is_active") and s["store_type"] != "ward_substore"]
    return {**await rx_ipd.indentable(scope, adm), "stores": stores}


@router.post("/ipd/indents", status_code=status.HTTP_201_CREATED)
async def ipd_raise_indent(body: rx_ipd.IndentIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *IPD_NURSE, new_work=True)
    rx_ipd.require_active(await _admission_in_scope(user, scope, body.admission_id))
    _store_for(user, await rx.get_store(scope, body.from_store_id))
    out = await rx_ipd.raise_indent(scope, body, _actor(user))
    if not out.get("replayed"):
        await _audit(user, request, "rx_indent_raise", "rx_ipd_indent", out["indent_id"],
                     {"indent_number": out["indent_number"], "priority": body.priority, "lines": len(body.lines)})
    detail = await rx_ipd.indent_detail(scope, await rx_ipd.get_indent_row(scope, out["indent_id"]))
    if body.priority == "stat" and not out.get("replayed"):
        await rx_alerts.stat_indent(scope, detail)
    return {**detail, "replayed": out.get("replayed", False)}


@router.get("/ipd/indents")
async def ipd_list_indents(status_: Optional[Literal["raised", "partially_dispensed", "dispensed", "cancelled", "closed"]]
                           = Query(None, alias="status"), ward_id: Optional[uuid.UUID] = None,
                           priority: Optional[Literal["routine", "urgent", "stat"]] = None,
                           admission_id: Optional[uuid.UUID] = None, limit: int = Query(50, ge=1, le=200),
                           clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *IPD_WARD)
    return {"indents": await rx_ipd.list_indents(scope, await _store_ids(user, scope, None), status_,
                                                 str(ward_id) if ward_id else None, priority,
                                                 str(admission_id) if admission_id else None, limit)}


@router.get("/ipd/indents/{indent_id}")
async def ipd_get_indent(indent_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *IPD_WARD)
    return await rx_ipd.indent_detail(scope, await _indent_in_scope(user, scope, indent_id))


@router.post("/ipd/indents/{indent_id}/dispense")
async def ipd_dispense(indent_id: uuid.UUID, body: rx_ipd.DispenseIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _ipd_scope(user, clinic_id, *IPD_PHARMACY)    # open indents finish in wind-down
    indent = await _indent_in_scope(user, scope, indent_id)
    _store_for(user, await rx.get_store(scope, body.store_id))
    out = await rx_ipd.dispense(scope, indent, body, _actor(user), rx.get_rx_settings(clinic))
    if not out.get("replayed"):
        await _audit(user, request, "rx_dispense", "rx_ipd_indent", str(indent_id),
                     {"dispense_id": out["dispense_id"], "lines": out.get("lines"), "total_paise": out.get("total_paise")})
    return {**await rx_ipd.indent_detail(scope, await rx_ipd.get_indent_row(scope, str(indent_id))),
            "dispense_id": out["dispense_id"], "replayed": out.get("replayed", False),
            "final_bill_exists": out.get("final_bill_exists", False),
            "slip_url": f"/admin/rx/ipd/dispenses/{out['dispense_id']}/slip.pdf"}


@router.post("/ipd/indents/{indent_id}/cancel")
async def ipd_cancel_indent(indent_id: uuid.UUID, body: rx_ipd.CancelIn, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *IPD_WARD)
    out = await rx_ipd.cancel(scope, await _indent_in_scope(user, scope, indent_id), body.reason, _actor(user))
    await _audit(user, request, "rx_indent_cancel", "rx_ipd_indent", str(indent_id),
                 {"reason": body.reason, "lines_cancelled": out.get("lines_cancelled")})
    return out


@router.post("/ipd/dispenses/{dispense_id}/receive")
async def ipd_receive(dispense_id: uuid.UUID, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *IPD_NURSE)
    out = await rx_ipd.receive(scope, await _dispense_in_scope(user, scope, dispense_id), _actor(user))
    if not out.get("replayed"):
        await _audit(user, request, "rx_dispense_receive", "rx_dispense", str(dispense_id), {})
    return out


@router.get("/ipd/dispenses/{dispense_id}/slip.pdf")
async def ipd_slip(dispense_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _ipd_scope(user, clinic_id, *POS_READ)
    d = await _dispense_in_scope(user, scope, dispense_id)
    model = await rx_ipd.slip_model(scope, d, clinic, await rx.get_store(scope, str(d["store_id"])))
    return _pdf(rx_ipd.render_slip_pdf(model), f"slip-{model['indent'].get('indent_number') or dispense_id}.pdf")


@router.post("/ipd/dispenses/{dispense_id}/returns", status_code=status.HTTP_201_CREATED)
async def ipd_return(dispense_id: uuid.UUID, body: rx_ipd.ReturnIn, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *IPD_WARD)
    d = await _dispense_in_scope(user, scope, dispense_id)
    out = await rx_ipd.ipd_return(scope, d, body, _actor(user), holds_rx_permission(user, "RX_ADMIN"))
    if not out.get("replayed"):
        await _audit(user, request, "rx_ipd_return", "rx_dispense", str(dispense_id),
                     {"return_id": out["return_id"], "value_paise": out.get("value_paise"),
                      "credit_notes": out.get("credit_notes")})
    return out


# ── transfers, par levels, stock audits (140, RX-42…46, RX-78) ──────────────
# API.md §7. RX_STOCK of the v1 spec is RX_TRANSFER / RX_AUDIT (SECURITY §2). A nurse (IPD_NURSING) reaches only
# transfers INTO a ward store in scope (receive) and that store's par levels; anything else reads as not found.

TRANSFER_WORK = ("RX_TRANSFER", "RX_ADMIN")
TRANSFER_READ = ("RX_READ", "RX_TRANSFER", "RX_ADMIN", "IPD_NURSING")
AUDIT_WORK = ("RX_AUDIT", "RX_ADMIN")
AUDIT_READ = ("RX_READ", "RX_AUDIT", "RX_ADMIN")


def _ward_only(user: AdminUser) -> bool:
    """A nurse with no pharmacy grant: ward stores only."""
    return not any(holds_rx_permission(user, p) for p in ("RX_READ", "RX_TRANSFER", "RX_ADMIN"))


def _ward_store_for(user: AdminUser, store: dict) -> dict:
    _store_for(user, store)
    if _ward_only(user) and store["store_type"] not in rx_transfers.WARD_STORE_TYPES:
        raise HTTPException(status_code=404, detail="Store not found.")
    return store


async def _transfer_in_scope(user: AdminUser, scope: str, transfer_id: uuid.UUID) -> dict:
    t = await rx_transfers.get_row(scope, str(transfer_id))
    if _ward_only(user):            # the receiving ward's view
        _ward_store_for(user, await rx.get_store(scope, str(t["to_store_id"])))
        return t
    stores = await rx.list_stores(scope)
    allowed = allowed_store_ids(user, stores)
    if allowed is not None and not {str(t["from_store_id"]), str(t["to_store_id"])} & allowed:
        raise HTTPException(status_code=404, detail="Transfer not found.")
    return t


@router.get("/transfers")
async def list_transfers(status_: Optional[Literal["draft", "dispatched", "received", "partially_received", "cancelled"]]
                         = Query(None, alias="status"), store_id: Optional[uuid.UUID] = None,
                         limit: int = Query(50, ge=1, le=200), clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *TRANSFER_READ)
    ids = await _store_ids(user, scope, None)                        # None = every store of the clinic
    if _ward_only(user):
        ids = [str(s["id"]) for s in await _visible_stores(user, scope)
               if s["store_type"] in rx_transfers.WARD_STORE_TYPES]
    if store_id:
        _ward_store_for(user, await rx.get_store(scope, str(store_id)))
        ids = [str(store_id)]
    rows = await rx_transfers.list_transfers(scope, ids, status_, limit)
    visible = await _visible_stores(user, scope)
    if _ward_only(user):            # a nurse sees what comes in, not what the ward sends
        rows = [r for r in rows if str(r["to_store_id"]) in set(ids or [])]
        visible = [x for x in visible if x["store_type"] in rx_transfers.WARD_STORE_TYPES]
    return {"transfers": rows,
            "stores": [{k: x.get(k) for k in ("id", "code", "name", "store_type", "parent_store_id", "is_active")}
                       for x in visible]}


@router.post("/transfers", status_code=status.HTTP_201_CREATED)
async def create_transfer(body: rx_transfers.TransferIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *TRANSFER_WORK, new_work=True)
    _store_for(user, await rx.get_store(scope, body.from_store_id))
    await rx.get_store(scope, body.to_store_id)
    tid = await rx_transfers.save(scope, body, _actor(user))
    await _audit(user, request, "rx_transfer_draft", "rx_transfer", tid, {"lines": len(body.lines)})
    return await rx_transfers.detail(scope, await rx_transfers.get_row(scope, tid))


@router.get("/transfers/{transfer_id}/challan.pdf")
async def transfer_challan(transfer_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *TRANSFER_READ)
    d = await rx_transfers.detail(scope, await _transfer_in_scope(user, scope, transfer_id))
    return _pdf(rx_transfers.render_challan_pdf(d, clinic), f"{d.get('transfer_number') or 'transfer-draft'}.pdf")


@router.get("/transfers/{transfer_id}")
async def get_transfer(transfer_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *TRANSFER_READ)
    return await rx_transfers.detail(scope, await _transfer_in_scope(user, scope, transfer_id))


@router.patch("/transfers/{transfer_id}")
async def update_transfer(transfer_id: uuid.UUID, body: rx_transfers.TransferIn, request: Request,
                          clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *TRANSFER_WORK, new_work=True)
    await _transfer_in_scope(user, scope, transfer_id)
    _store_for(user, await rx.get_store(scope, body.from_store_id))
    await rx.get_store(scope, body.to_store_id)
    await rx_transfers.save(scope, body, _actor(user), str(transfer_id))
    await _audit(user, request, "rx_transfer_draft_edit", "rx_transfer", str(transfer_id), {"lines": len(body.lines)})
    return await rx_transfers.detail(scope, await rx_transfers.get_row(scope, str(transfer_id)))


@router.post("/transfers/{transfer_id}/dispatch")
async def dispatch_transfer(transfer_id: uuid.UUID, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *TRANSFER_WORK)      # a drafted transfer may still leave in wind-down
    t = await _transfer_in_scope(user, scope, transfer_id)
    _store_for(user, await rx.get_store(scope, str(t["from_store_id"])))
    out = await rx_transfers.dispatch(scope, t, _actor(user), holds_rx_permission(user, "RX_CONTROLLED"))
    if not out.get("replayed"):
        await _audit(user, request, "rx_transfer_dispatch", "rx_transfer", str(transfer_id),
                     {"transfer_number": out["transfer_number"], "controlled": out.get("controlled")})
    return {**await rx_transfers.detail(scope, await rx_transfers.get_row(scope, str(transfer_id))),
            "replayed": out.get("replayed", False), "challan_url": f"/admin/rx/transfers/{transfer_id}/challan.pdf"}


@router.post("/transfers/{transfer_id}/receive")
async def receive_transfer(transfer_id: uuid.UUID, body: rx_transfers.ReceiveIn, request: Request,
                           clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, "RX_TRANSFER", "RX_ADMIN", "IPD_NURSING")
    t = await _transfer_in_scope(user, scope, transfer_id)
    _ward_store_for(user, await rx.get_store(scope, str(t["to_store_id"])))   # receiving needs the target's scope
    out = await rx_transfers.receive(scope, t, body, _actor(user), holds_rx_permission(user, "RX_CONTROLLED"))
    if not out.get("replayed"):
        await _audit(user, request, "rx_transfer_receive", "rx_transfer", str(transfer_id),
                     {"status": out["status"], "short_units": out.get("short_units")})
    return {**await rx_transfers.detail(scope, await rx_transfers.get_row(scope, str(transfer_id))),
            "replayed": out.get("replayed", False)}


@router.post("/transfers/{transfer_id}/cancel")
async def cancel_transfer(transfer_id: uuid.UUID, body: rx_transfers.ReasonIn, request: Request,
                          clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *TRANSFER_WORK)
    out = await rx_transfers.cancel(scope, await _transfer_in_scope(user, scope, transfer_id), body.reason)
    await _audit(user, request, "rx_transfer_cancel", "rx_transfer", str(transfer_id), {"reason": body.reason})
    return out


@router.get("/par-levels")
async def get_par_levels(store_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *TRANSFER_READ)
    store = _ward_store_for(user, await rx.get_store(scope, str(store_id)))
    return {"store": store, "par_levels": await rx_transfers.par_levels(scope, str(store_id))}


@router.post("/par-levels")
async def set_par_levels(body: rx_transfers.ParIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, "RX_TRANSFER", "RX_ADMIN", "IPD_NURSING")
    _ward_store_for(user, await rx.get_store(scope, body.store_id))
    out = await rx_transfers.set_par_levels(scope, body, _actor(user)[0])
    await _audit(user, request, "rx_par_levels_set", "rx_store", body.store_id, out)
    return {**out, "par_levels": await rx_transfers.par_levels(scope, body.store_id)}


@router.post("/par-levels/replenish")
async def replenish_par(body: rx_transfers.ReplenishIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *TRANSFER_WORK, new_work=True)
    store = _store_for(user, await rx.get_store(scope, body.store_id))
    out = await rx_transfers.replenish(scope, store, _actor(user))
    if out["transfer"]:
        await _audit(user, request, "rx_par_replenish", "rx_transfer", out["transfer"]["id"],
                     {"lines": len(out["transfer"]["lines"]), "short_items": len(out["short"])})
    return out


async def _audit_in_scope(user: AdminUser, scope: str, audit_id: uuid.UUID) -> dict:
    a = await rx_audit.get_row(scope, str(audit_id))
    _store_for(user, await rx.get_store(scope, str(a["store_id"])))
    return a


@router.get("/audits")
async def list_audits(status_: Optional[Literal["counting", "review", "posted", "cancelled"]] = Query(None, alias="status"),
                      store_id: Optional[uuid.UUID] = None, limit: int = Query(50, ge=1, le=200),
                      clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *AUDIT_READ)
    return {"audits": await rx_audit.list_audits(scope, await _store_ids(user, scope, store_id), status_, limit)}


@router.post("/audits", status_code=status.HTTP_201_CREATED)
async def start_audit(body: rx_audit.StartIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *AUDIT_WORK)          # audits continue in wind-down (ADR-RX-09)
    _store_for(user, await rx.get_store(scope, body.store_id))
    out = await rx_audit.start(scope, body, _actor(user))
    await _audit(user, request, "rx_audit_start", "rx_stock_audit", out["audit_id"],
                 {"audit_number": out["audit_number"], "scope": body.scope, "freeze_sales": body.freeze_sales})
    return await rx_audit.detail(scope, await rx_audit.get_row(scope, out["audit_id"]), False)


@router.get("/audits/{audit_id}")
async def get_audit(audit_id: uuid.UUID, show_system: bool = False, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *AUDIT_READ)
    a = await _audit_in_scope(user, scope, audit_id)
    # blind count: only a manager may look at the system quantity while counting is open
    return await rx_audit.detail(scope, a, show_system and holds_rx_permission(user, "RX_ADMIN"))


@router.put("/audits/{audit_id}/counts")
async def put_audit_counts(audit_id: uuid.UUID, counts: list[rx_audit.CountIn], clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *AUDIT_WORK)
    if not 1 <= len(counts) <= 500:
        raise HTTPException(status_code=422, detail="Send 1–500 counts at a time.")
    return await rx_audit.record_counts(scope, await _audit_in_scope(user, scope, audit_id), counts)


@router.post("/audits/{audit_id}/review")
async def review_audit(audit_id: uuid.UUID, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *AUDIT_WORK)
    a = await _audit_in_scope(user, scope, audit_id)
    out = await rx_audit.review(scope, a)
    if not out.get("replayed"):
        await _audit(user, request, "rx_audit_review", "rx_stock_audit", str(audit_id), {})
    return await rx_audit.detail(scope, await rx_audit.get_row(scope, str(audit_id)), True)


@router.post("/audits/{audit_id}/post")
async def post_audit(audit_id: uuid.UUID, body: rx_audit.PostIn, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    """Over rx_settings.audit_approval_paise a second RX_ADMIN re-authenticates in the body (UI.md §6); the SQL
    function checks the threshold and that the approver is neither the starter nor the poster."""
    scope, _ = await _rx_scope(user, clinic_id, *AUDIT_WORK)
    a = await _audit_in_scope(user, scope, audit_id)
    approver = None
    if body.approver_username or body.approver_password:
        actor_id = _actor(user)[0]
        if not rx_pos.override_rate_ok(actor_id or user.username):
            raise HTTPException(status_code=429, detail="Too many approval attempts. Wait a minute.")
        from app.routers.admin import _authenticate_password
        who = await _authenticate_password(body.approver_username or "", body.approver_password or "",
                                           request.client.host if request.client else "unknown")
        approver = _actor(who)
        if str(getattr(who, "clinic_id", None) or "") != scope or not approver[0] or not holds_rx_permission(who, "RX_ADMIN"):
            raise HTTPException(status_code=403, detail={"code": "rx_approver_not_authorised",
                                                         "message": "The approver must be a pharmacy manager of this clinic."})
        if approver[0] == actor_id:
            raise HTTPException(status_code=403, detail={"code": "rx_approver_same_person",
                                                         "message": "The approver must be a different person."})
    out = await rx_audit.post(scope, a, body, _actor(user), approver)
    if not out.get("replayed"):
        await _audit(user, request, "rx_audit_post", "rx_stock_audit", str(audit_id),
                     {"audit_number": out["audit_number"], "variance_value_paise": out["variance_value_paise"],
                      "approver": approver[1] if approver else None})
    return {**await rx_audit.detail(scope, await rx_audit.get_row(scope, str(audit_id)), True),
            "replayed": out.get("replayed", False)}


@router.post("/audits/{audit_id}/cancel")
async def cancel_audit(audit_id: uuid.UUID, body: rx_audit.CancelIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *AUDIT_WORK)
    out = await rx_audit.cancel(scope, await _audit_in_scope(user, scope, audit_id), body.reason)
    await _audit(user, request, "rx_audit_cancel", "rx_stock_audit", str(audit_id), {"reason": body.reason})
    return out


# ── statutory registers (141, RX-49 / RX-50), recall trace (A6, RX-74), narcotic empties (A11, RX-79) ──
# RX_REGISTER_EXPORT of the v1 spec is RX_REGISTERS (P1, SECURITY §2). The list is PHI (patient names and
# addresses): RX_READ alone does not open it.
REGISTER_READ = ("RX_REGISTERS", "RX_ADMIN")


async def _register_args(user: AdminUser, scope: str, store_id: Optional[uuid.UUID], frm: Optional[date],
                         to: Optional[date]) -> Optional[list[str]]:
    if frm and to and frm > to:
        raise HTTPException(status_code=422, detail="The start date must be on or before the end date.")
    return await _store_ids(user, scope, store_id)


@router.get("/registers/{register}")
async def list_register(register: rx_registers.Register, from_: Optional[date] = Query(None, alias="from"),
                        to: Optional[date] = None, store_id: Optional[uuid.UUID] = None,
                        item_id: Optional[uuid.UUID] = None, limit: int = Query(100, ge=1, le=500),
                        offset: int = Query(0, ge=0, le=1_000_000), clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *REGISTER_READ)
    stores = await _register_args(user, scope, store_id, from_, to)
    return {"register": register, **await rx_registers.list_entries(
        scope, register, from_, to, stores, str(item_id) if item_id else None, limit, offset)}


@router.get("/registers/{register}/verify")
async def verify_register(register: rx_registers.Register, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *REGISTER_READ)
    return {"register": register, **await rx_registers.verify(scope, register)}


async def _export(register: str, fmt: str, request: Request, from_: Optional[date], to: Optional[date],
                  store_id: Optional[uuid.UUID], item_id: Optional[uuid.UUID], clinic_id: str, user: AdminUser) -> Response:
    scope, clinic = await _rx_scope(user, clinic_id, *REGISTER_READ)
    stores = await _register_args(user, scope, store_id, from_, to)
    rows = await rx_registers.all_entries(scope, register, from_, to, stores, str(item_id) if item_id else None)
    period = f"{from_.isoformat() if from_ else 'start'} to {to.isoformat() if to else date.today().isoformat()}"
    await _audit(user, request, "rx_register_export", "rx_register", register,
                 {"format": fmt, "from": from_.isoformat() if from_ else None, "to": to.isoformat() if to else None,
                  "store_id": str(store_id) if store_id else None, "item_id": str(item_id) if item_id else None,
                  "rows": len(rows)})
    name = f"register-{register}-{period.replace(' ', '_')}.{fmt}"
    if fmt == "csv":
        return Response(content=rx_registers.render_csv(register, rows), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
    store = await rx.get_store(scope, str(store_id)) if store_id else None
    chain = await rx_registers.verify(scope, register)
    return _pdf(rx_registers.render_pdf(register, rows, clinic, period, store, chain), name)


@router.get("/registers/{register}/export.pdf")
async def export_register_pdf(register: rx_registers.Register, request: Request,
                              from_: Optional[date] = Query(None, alias="from"), to: Optional[date] = None,
                              store_id: Optional[uuid.UUID] = None, item_id: Optional[uuid.UUID] = None,
                              clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    return await _export(register, "pdf", request, from_, to, store_id, item_id, clinic_id, user)


@router.get("/registers/{register}/export.csv")
async def export_register_csv(register: rx_registers.Register, request: Request,
                              from_: Optional[date] = Query(None, alias="from"), to: Optional[date] = None,
                              store_id: Optional[uuid.UUID] = None, item_id: Optional[uuid.UUID] = None,
                              clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    return await _export(register, "csv", request, from_, to, store_id, item_id, clinic_id, user)


@router.get("/batches/{batch_id}/trace")
async def batch_trace(batch_id: uuid.UUID, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    """A6: everyone who got this batch. Patient names and masked phones: managers only, and every look is logged."""
    scope, _ = await _rx_scope(user, clinic_id, "RX_ADMIN")
    out = await rx_registers.trace(scope, str(batch_id))
    await _audit(user, request, "rx_batch_trace", "rx_batch", str(batch_id), out["summary"])
    return out


@router.post("/dispenses/{dispense_line_id}/empties", status_code=status.HTTP_201_CREATED)
async def record_empties(dispense_line_id: uuid.UUID, body: rx_registers.EmptiesIn, request: Request,
                         clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    """A11: the custodian (this login, RX_CONTROLLED) receives empties from the nurse named in the body."""
    scope, _ = await _ipd_scope(user, clinic_id, "RX_CONTROLLED")
    lines = await rx._rows(scoped_query("rx_dispense_lines", scope, "id, dispense_id").eq("id", str(dispense_line_id)).limit(1))
    if not lines:
        raise HTTPException(status_code=404, detail="Dispensed line not found.")
    await _dispense_in_scope(user, scope, lines[0]["dispense_id"])
    out = await rx_registers.record_empties(scope, str(dispense_line_id), body, _actor(user)[0])
    await _audit(user, request, "rx_empties_received", "rx_dispense_line", str(dispense_line_id),
                 {"units": body.units, "returned_by": str(body.returned_by), "outstanding_units": out["outstanding_units"]})
    return out


# ── refills (142, W10, RX-57…RX-59), alerts (RX-56), expiry-return planner (A8, RX-76) ──
REFILL_READ = ("RX_READ", "RX_DISPENSE", "RX_ADMIN")
REFILL_WORK = ("RX_DISPENSE", "RX_ADMIN")
ALERT_READ = ("RX_READ", "RX_ADMIN")


@router.get("/refills/requests")
async def list_refill_requests(status_: Optional[rx_refills.RequestStatus] = Query(None, alias="status"),
                               limit: int = Query(100, ge=1, le=500), clinic_id: str = "default",
                               user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *REFILL_READ)
    return {"requests": await rx_refills.list_requests(scope, status_, limit)}


@router.post("/refills/requests/{request_id}/verify")
async def verify_refill(request_id: uuid.UUID, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *REFILL_WORK, new_work=True)
    out = await rx_refills.verify(scope, await rx_refills.get_request(scope, str(request_id)), rx.get_rx_settings(clinic),
                                  _actor(user)[0])
    await _audit(user, request, "rx_refill_verify", "rx_refill_request", str(request_id), {})
    return out


@router.post("/refills/requests/{request_id}/pack")
async def pack_refill(request_id: uuid.UUID, body: rx_refills.PackIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    """A DRAFT counter sale (FEFO) for the pharmacist to complete at the counter; nothing leaves the shelf here."""
    scope, clinic = await _rx_scope(user, clinic_id, *REFILL_WORK, new_work=True)
    store = _store_for(user, await rx.get_store(scope, body.store_id))
    out = await rx_refills.pack(scope, await rx_refills.get_request(scope, str(request_id)), store, _actor(user),
                                rx.get_rx_settings(clinic))
    await _audit(user, request, "rx_refill_pack", "rx_refill_request", str(request_id), {"sale_id": out["sale_id"]})
    return out


@router.post("/refills/requests/{request_id}/reject")
async def reject_refill(request_id: uuid.UUID, body: rx_refills.RejectIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, *REFILL_WORK)
    out = await rx_refills.reject(scope, clinic, await rx_refills.get_request(scope, str(request_id)), body)
    await _audit(user, request, "rx_refill_reject", "rx_refill_request", str(request_id),
                 {"reason": body.reason, "notified": body.notify_patient})
    return out


@router.get("/refills/plans")
async def list_refill_plans(patient_id: Optional[uuid.UUID] = None,
                            status_: Optional[rx_refills.PlanStatus] = Query(None, alias="status"),
                            limit: int = Query(200, ge=1, le=500), clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *REFILL_READ)
    return {"plans": await rx_refills.list_plans(scope, str(patient_id) if patient_id else None, status_, limit)}


@router.post("/refills/plans/{plan_id}/cancel")
async def cancel_refill_plan(plan_id: uuid.UUID, request: Request, clinic_id: str = "default",
                             user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *REFILL_WORK)
    out = await rx_refills.cancel_plan(scope, await rx_refills.get_plan(scope, str(plan_id)))
    await _audit(user, request, "rx_refill_plan_cancel", "rx_refill_plan", str(plan_id), {})
    return out


@router.get("/alerts")
async def list_alerts(kind: Optional[Literal["low_stock", "near_expiry", "expired_on_hand", "ledger_mismatch",
                                             "register_chain_break", "cold_chain", "stat_indent"]] = None,
                      store_id: Optional[uuid.UUID] = None, ack: Optional[bool] = False,
                      limit: int = Query(200, ge=1, le=500), clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *ALERT_READ)
    return {"alerts": await rx_alerts.list_alerts(scope, await _store_ids(user, scope, store_id), kind, ack, limit)}


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: uuid.UUID, request: Request, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _rx_scope(user, clinic_id, *ALERT_READ)
    alert = await rx_alerts.get_alert(scope, str(alert_id))
    if alert.get("store_id"):
        _store_for(user, await rx.get_store(scope, str(alert["store_id"])))
    out = await rx_alerts.acknowledge(scope, alert, _actor(user)[0])
    await _audit(user, request, "rx_alert_ack", "rx_alert", str(alert_id), {"kind": alert["kind"]})
    return out


@router.get("/expiry/return-plan")
async def expiry_return_plan(store_id: uuid.UUID, clinic_id: str = "default",
                             user: AdminUser = Depends(verify_credentials)):
    """A8: a suggestion only. Drafting goes through POST /supplier-returns; nothing is dispatched from here."""
    scope, _ = await _rx_scope(user, clinic_id, *PURCHASE_READ)
    store = _store_for(user, await rx.get_store(scope, str(store_id)))
    return {"store": store, "suppliers": await rx_purchase.return_plan(scope, str(store_id))}


# ── P9: Ketu stock answer (RX-61), dashboard (RX-62), lost-sale call list (A12, RX-80) ──

@router.get("/stock/answer")
async def stock_answer(q: str = Query("", max_length=120), clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    """KRIYA-KETU §2.1. A nurse without RX_READ sees exact counts for ward stores only, plus available_at_pharmacy."""
    scope, _ = await _rx_scope(user, clinic_id, "RX_READ", "RX_ADMIN", "IPD_NURSING")
    nurse = _ward_only(user)
    stores = [s for s in await _visible_stores(user, scope) if s.get("is_active") is not False]
    if nurse:                       # the pharmacy's stores stay in the list only to answer "available: yes / no"
        all_stores = [s for s in await rx.list_stores(scope) if s.get("is_active") is not False]
        ids = {str(s["id"]) for s in stores if s["store_type"] in rx_dashboard.WARD_STORES}
        stores = [s for s in all_stores if str(s["id"]) in ids or s["store_type"] not in rx_dashboard.WARD_STORES]
    if not q.strip():
        return {"status": "not_found", "query": "", "resolved_by": None, "matches": []}
    return await rx_dashboard.stock_answer(scope, q.strip(), stores, nurse)


@router.get("/dashboard")
async def pharmacy_dashboard(store_id: Optional[uuid.UUID] = None, date_: Optional[date] = Query(None, alias="date"),
                             clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _rx_scope(user, clinic_id, "RX_READ", "RX_ADMIN")
    from app.utils.helpers import today_ist
    return await rx_dashboard.dashboard(scope, await _store_ids(user, scope, store_id), date_ or today_ist(),
                                        holds_rx_permission(user, "RX_ADMIN"), rx.get_rx_settings(clinic))


@router.get("/lost-sales")
async def lost_sales(date_: Optional[date] = Query(None, alias="date"), clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    """A12: yesterday's (or `date`'s) signed prescriptions the pharmacy did not bill, and part-filled ones."""
    scope, _ = await _rx_scope(user, clinic_id, "RX_READ", "RX_DISPENSE", "RX_ADMIN")
    from datetime import timedelta
    from app.utils.helpers import today_ist
    day = date_ or (today_ist() - timedelta(days=1))
    return {"date": day.isoformat(), "rows": await rx_dashboard.lost_sales(scope, day)}


@router.post("/lost-sales/{prescription_id}/reveal-phone")
async def reveal_lost_sale_phone(prescription_id: uuid.UUID, request: Request, clinic_id: str = "default",
                                 user: AdminUser = Depends(verify_credentials)):
    """The full number for a manual call. Audited; nothing is ever sent automatically from the call list."""
    scope, _ = await _rx_scope(user, clinic_id, "RX_DISPENSE", "RX_ADMIN")
    out = await rx_dashboard.reveal_phone(scope, str(prescription_id))
    await _audit(user, request, "rx_lost_sale_phone_reveal", "opd_prescription", str(prescription_id), {})
    return out
