"""Kriya Pharmacy OS API — foundation (134, RX-05), drug master (135, RX-13…15), stock (136, RX-18…20),
purchase (137, RX-23…26, RX-71), counter sales (138, RX-29…35, RX-69/70/72/73/77/81/82).
Design: docs/pharmacy-os/API.md §0–§5, SECURITY.md §2–§3.

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
from app.services import rx, rx_catalogue, rx_gs1, rx_pos, rx_pos_docs, rx_purchase, rx_stock
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
    detail = await rx_pos.sale_detail(scope, str(sale_id), rx.get_rx_settings(clinic))
    return {**detail, "replayed": out.get("replayed", False),
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
