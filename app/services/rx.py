"""Kriya Pharmacy OS — foundation service (migration 134, RX-M1).

Settings (validated, CAS-versioned), stores, provisioning, the setup checklist
and the owner's deactivation preview.
Design: docs/pharmacy-os/DATA-MODEL.md §1, ADR-RX-03 / ADR-RX-09.

Zero-LLM: imports nothing from the AI, voice or assistant packages
(tests/test_rx_no_llm.py).
"""

import logging
import re
from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.database import sb, scoped_query, supabase

logger = logging.getLogger(__name__)

STORE_TYPES = ("central", "retail_counter", "ward_substore", "ot_tray", "emergency_tray")
STORE_CODE = r"^[A-Z0-9_-]{2,20}$"


class RxSettings(BaseModel):
    """clinics.rx_settings (DATA-MODEL §1). Defaults apply to any missing key."""

    model_config = ConfigDict(extra="forbid")

    max_discount_bps: int = Field(1000, ge=0, le=10000)          # counter discount ceiling without RX_ADMIN
    near_expiry_days: int = Field(90, ge=7, le=365)
    min_shelf_days_at_sale: int = Field(30, ge=0, le=365)
    ipd_autopost: bool = True
    ipd_price_basis: Literal["mrp", "cost_plus"] = "mrp"
    ipd_cost_plus_bps: int = Field(0, ge=0, le=10000)
    allow_loose_sale: bool = True
    round_off_to_rupee: bool = True
    refill_enabled: bool = False
    refill_lead_days: int = Field(5, ge=1, le=30)
    receipt_format: Literal["80mm", "a5"] = "80mm"
    fefo_override_requires_reason: bool = True
    rx_validity_days: int = Field(30, ge=1, le=365)
    return_days: int = Field(7, ge=0, le=90)
    audit_approval_paise: int = Field(500000, ge=0, le=100_000_000)
    version: int = Field(0, ge=0)


DEFAULT_SETTINGS = RxSettings().model_dump()

# rx_* codes raised by migration 134 and the constraint names behind user mistakes.
# Matched longest-first against the database message; the raw text is never returned.
_ERRORS = {
    "rx_settings_conflict": (409, "These settings were changed by someone else. Reload and try again."),
    "rx_settings_invalid": (422, "Settings must be an object."),
    "rx_clinic_not_found": (404, "Clinic not found."),
    "rx_store_delete_refused": (409, "Stores are never deleted. Deactivate the store instead."),
    "rx_store_field_locked": (409, "A store's code and type never change. Create a new store instead."),
}
_CONSTRAINTS = (
    ("rx_stores_code_key", 409, "Another store already uses this code."),
    ("uq_rx_stores_one_central", 409, "This branch already has an active central store."),
    ("rx_stores_branch_fk", 404, "Branch not found."),
    ("rx_stores_parent_fk", 404, "Parent store not found."),
    ("rx_stores_ward_shape", 422, "A ward sub-store needs its ward; other stores have none."),
    ("rx_stores_sell_shape", 422, "Only a retail counter can sell."),
    ("rx_stores_no_self_parent", 422, "A store cannot be its own parent."),
    ("rx_stores_code_check", 422, "A store code is 2–20 capital letters, digits, _ or -."),
    ("rx_stores_name_check", 422, "A store name is 2–100 characters."),
)


def raise_for(exc: Exception) -> None:
    """Map an rx_* database error; anything else is a fixed 500 (never raw DB text)."""
    if isinstance(exc, HTTPException):
        raise exc
    msg = str(getattr(exc, "message", None) or exc)
    for code in sorted(_ERRORS, key=len, reverse=True):
        if code in msg:
            status, message = _ERRORS[code]
            m = re.search(re.escape(code) + r":([A-Za-z_]+)", msg)
            raise HTTPException(status_code=status, detail={"code": code, "reason": m.group(1) if m else None,
                                                            "message": message})
    for constraint, status, message in _CONSTRAINTS:
        if constraint in msg:
            raise HTTPException(status_code=status, detail=message)
    logger.error(f"RX_DB_ERROR type={type(exc).__name__}")
    raise HTTPException(status_code=500, detail="The pharmacy operation failed. Nothing was changed; please retry.")


async def _rows(query) -> list[dict]:
    res = await sb(query)
    return list(res.data or [])


# ── settings ────────────────────────────────────────────────────────────────

def get_rx_settings(clinic: dict) -> dict:
    return {**DEFAULT_SETTINGS, **(clinic.get("rx_settings") or {})}


def validate_settings(current: dict, changes: dict) -> dict:
    """Merge `changes` over the stored settings and validate the result (422 on error)."""
    merged = {**get_rx_settings({"rx_settings": current}), **changes}
    try:
        return RxSettings(**merged).model_dump()
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=[{"loc": err["loc"], "msg": err["msg"]} for err in e.errors()])


async def update_rx_settings(clinic_id: str, clinic: dict, changes: dict, expected_version: int) -> dict:
    """Validated write with compare-and-swap on settings.version (rx_update_settings)."""
    merged = validate_settings(clinic.get("rx_settings") or {}, changes)
    merged.pop("version", None)
    try:
        res = await sb(supabase.rpc("rx_update_settings", {"p_clinic_id": clinic_id, "p_settings": merged,
                                                           "p_expected_version": expected_version}))
    except Exception as e:
        raise_for(e)
    return {**DEFAULT_SETTINGS, **(res.data or {})}


# ── stores ──────────────────────────────────────────────────────────────────

async def list_stores(clinic_id: str, branch_id: Optional[str] = None) -> list[dict]:
    q = scoped_query("rx_stores", clinic_id).order("code")
    return await _rows(q.eq("branch_id", branch_id) if branch_id else q)


async def get_store(clinic_id: str, store_id: str) -> dict:
    rows = await _rows(scoped_query("rx_stores", clinic_id).eq("id", store_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Store not found.")
    return rows[0]


async def create_or_update_store(clinic_id: str, body: dict, store_id: Optional[str] = None) -> dict:
    try:
        if store_id:
            res = await sb(supabase.table("rx_stores").update(body).eq("clinic_id", clinic_id).eq("id", store_id))
        else:
            # unscoped: insert_scoped_by_payload
            res = await sb(supabase.table("rx_stores").insert({**body, "clinic_id": clinic_id}))
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=404, detail="Store not found.")
    return res.data[0]


async def assert_ward(clinic_id: str, ward_id: str, branch_id: Optional[str]) -> None:
    """A ward sub-store sits on a ward of this clinic (and of its branch, when it has one).
    The database FK to ipd_wards arrives with migration 139; until then this is the check."""
    q = scoped_query("ipd_wards", clinic_id, "id, branch_id").eq("id", ward_id).limit(1)
    rows = await _rows(q)
    if not rows or (branch_id and rows[0].get("branch_id") and str(rows[0]["branch_id"]) != str(branch_id)):
        raise HTTPException(status_code=404, detail="Ward not found.")


# ── provisioning, checklist, deactivation ───────────────────────────────────

async def provision_defaults(clinic_id: str) -> None:
    """Owner enable: a clinic with no store gets one clinic-wide retail counter
    that can also receive goods ("Main Pharmacy"). Existing stores are never touched."""
    if await list_stores(clinic_id):
        return
    await create_or_update_store(clinic_id, {"code": "MAIN", "name": "Main Pharmacy", "store_type": "retail_counter",
                                             "can_sell": True, "can_receive_grn": True})


def setup_checklist(settings: dict, stores: list[dict], gstin: Optional[str], counts: Optional[dict] = None) -> list[dict]:
    """What the pharmacy still has to configure. `counts` carries later-phase masters
    (items: migration 135) and is empty until those tables exist."""
    active = [s for s in stores if s.get("is_active")]
    selling = [s for s in active if s.get("can_sell")]
    items = int((counts or {}).get("items") or 0)
    saved = int(settings.get("version") or 0) >= 1
    return [
        {"key": "stores", "label": "Stores", "done": bool(selling),
         "detail": f"{len(active)} active, {len(selling)} selling" if active else "Add a retail counter that can sell"},
        {"key": "items", "label": "Drug master", "done": items > 0,
         "detail": f"{items} items" if items else "Import or add medicines (Drug Master)"},
        {"key": "gstin", "label": "GST registration", "done": bool(gstin),
         "detail": "GSTIN on file: bills are tax invoices" if gstin else
                   "No GSTIN in OPD settings: bills will be bills of supply"},
        {"key": "settings", "label": "Settings", "done": saved,
         "detail": "Saved" if saved else "Review and save the pharmacy settings"},
    ]


async def deactivation_preview(clinic_id: str) -> dict:
    """What an owner disable would leave running (ADR-RX-09).
    shortcut: always zero until drafts / indents / transfers / audits exist (RX-M5…M7);
    each of those migrations must count its open rows here and set blocking."""
    return {"open_sales": 0, "open_indents": 0, "transfers_in_transit": 0, "open_audits": 0, "blocking": False}
