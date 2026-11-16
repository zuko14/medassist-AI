"""Kriya Pharmacy OS API — foundation (migration 134, RX-05).
Design: docs/pharmacy-os/API.md §0–§1, SECURITY.md §2–§3.

Every route: permission -> enforce_clinic_access -> rx_access(clinic) ->
(new work) wind-down rule -> store / branch scope -> service. Zero-LLM.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.database import sb, scoped_query
from app.routers.admin import AdminUser, enforce_clinic_access, log_admin_action, verify_credentials
from app.services import rx
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
            "branches": branches, "wards": wards, "checklist": rx.setup_checklist(settings, all_stores, gstin)}


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
    if patch.get("is_active") is True and not store.get("is_active"):
        await _rx_scope(user, clinic_id, "RX_STORE_MANAGE", "RX_ADMIN", new_work=True)   # re-opening is new work
    await _check_links(user, scope, None, patch.get("parent_store_id"), store_id)
    store = await rx.create_or_update_store(scope, patch, store_id)
    await _audit(user, request, "rx_store_update", "rx_store", store_id, {"keys": sorted(patch)})
    return store
