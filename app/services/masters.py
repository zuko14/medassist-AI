"""Central masters (G12, migration 120): the service / tariff master and ICD-10 search.

billable_services replaces clinics.opd_settings.service_catalog (JSON). Every
reader, here and in SQL (order placement, GST line tax), goes through
opd_service_catalog / opd_catalog_item: a master row wins and a code that only
exists in the legacy JSON still resolves (fail-open), so clinics that were never
backfilled keep billing.

icd10_codes is shared WHO reference data (no clinic_id); icd10_search ranks
code prefix, description prefix, substring and (pg_trgm) fuzzy matches.
Zero-LLM: nothing here imports the AI, voice or assistant packages.
"""

import logging
from typing import Any, Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase

logger = logging.getLogger(__name__)

CATEGORIES = ("consultation", "procedure", "nursing", "radiology", "diagnostic", "physiotherapy", "ambulance",
              "room_service", "other")
TAX_RATES_BPS = (0, 500, 1200, 1800, 2800, 4000)
_EDITABLE = ("name", "category", "department", "standard_price_paise", "hsn_sac", "tax_rate_bps", "is_active",
             "notes", "branch_id")


# ── ICD-10 ──────────────────────────────────────────────────────────────────

async def search_icd10_diagnoses(query: str, limit: int = 20) -> list[dict]:
    """Deterministic autocomplete over the shared ICD-10 table (one indexed SQL call)."""
    q = " ".join((query or "").split())[:60]
    if not q:
        return []
    res = await sb(supabase.rpc("icd10_search", {"p_q": q, "p_limit": max(1, min(int(limit), 50))}))
    return [{"system": "ICD-10", "code": r["code"], "text": r["description"],
             "chapter": r.get("chapter_number"), "chapter_title": r.get("chapter_title"),
             "is_common": r.get("is_common")} for r in (res.data or [])]


# ── service catalogue ───────────────────────────────────────────────────────

async def effective_catalog(clinic_id: str) -> list[dict]:
    """The clinic's whole catalogue in the legacy item shape (code, name,
    item_type, price_paise, active, hsn_sac, tax_rate_bps ...): master rows plus
    legacy JSON entries not yet in the master (`source` says which)."""
    res = await sb(supabase.rpc("opd_service_catalog", {"p_clinic_id": clinic_id}))
    return list(res.data or [])


def _visible(item: dict, branch_id: Optional[str]) -> bool:
    return not branch_id or not item.get("branch_id") or str(item["branch_id"]) == str(branch_id)


async def get_services_catalog(clinic_id: str, category: Optional[str] = None, actor: Any = None,
                               include_inactive: bool = True) -> list[dict]:
    """The tariff master for the panel. A branch-pinned login sees its branch's
    services and the all-branch ones."""
    branch_id = getattr(actor, "branch_id", None) if getattr(actor, "role", None) not in (
        "super_admin", "clinic_admin") else None
    items = await effective_catalog(clinic_id)
    return [i for i in items
            if _visible(i, branch_id) and (include_inactive or i.get("active", True))
            and (not category or i.get("category", i.get("item_type")) == category)]


async def get_service(clinic_id: str, service_id: str) -> dict:
    res = await sb(scoped_query("billable_services", clinic_id).eq("id", service_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Service not found.")
    return res.data[0]


def _db_error(exc: Exception, code: Optional[str] = None) -> None:
    msg = str(getattr(exc, "message", None) or exc)
    if "billable_services_clinic_code_key" in msg or "duplicate key" in msg:
        raise HTTPException(status_code=409, detail=f"Service code {code} already exists. Edit that service instead.")
    if "opd_service_code_immutable" in msg:
        raise HTTPException(status_code=409, detail="A service code cannot change: bills and orders carry it.")
    if "billable_services_branch_fk" in msg:
        raise HTTPException(status_code=404, detail="Branch not found.")
    if "violates check constraint" in msg:
        raise HTTPException(status_code=422, detail="Some values are outside the allowed range.")
    logger.error(f"MASTERS_DB_ERROR type={type(exc).__name__}")
    raise HTTPException(status_code=500, detail="The service could not be saved. Nothing was changed.")


async def create_or_update_service(clinic_id: str, data: dict, actor: Any,
                                   service_id: Optional[str] = None,
                                   expected_updated_at: Optional[str] = None) -> dict:
    """Insert a service, or update one (compare-and-set on updated_at when
    given). The code is fixed once created; tax rate is a GST slab."""
    if "tax_rate_bps" in data and data["tax_rate_bps"] not in TAX_RATES_BPS:
        raise HTTPException(status_code=422, detail="GST rate must be one of 0, 5, 12, 18, 28 or 40%.")
    if "category" in data and data["category"] not in CATEGORIES:
        raise HTTPException(status_code=422, detail="Unknown service category.")
    row = {k: data[k] for k in _EDITABLE if k in data}
    try:
        if service_id is None:
            # unscoped: insert_scoped_by_payload
            res = await sb(supabase.table("billable_services").insert(
                {**row, "clinic_id": clinic_id, "service_code": data["service_code"]}))
        else:
            q = supabase.table("billable_services").update(row).eq("clinic_id", clinic_id).eq("id", service_id)
            if expected_updated_at:
                q = q.eq("updated_at", expected_updated_at)
            res = await sb(q)
    except Exception as e:
        _db_error(e, data.get("service_code"))
    if not res.data:
        raise HTTPException(status_code=409, detail="Someone else changed this service. Reload and try again.")
    return res.data[0]


async def service_in_use(clinic_id: str, code: str) -> bool:
    for table in ("opd_invoice_items", "opd_orders"):
        res = await sb(scoped_query(table, clinic_id, "id").eq("catalog_code", code).limit(1))
        if res.data:
            return True
    return False


async def remove_service(clinic_id: str, service: dict) -> dict:
    """Delete a never-used service; deactivate one that bills or orders carry."""
    if await service_in_use(clinic_id, service["service_code"]):
        row = await create_or_update_service(clinic_id, {"is_active": False}, None, service["id"])
        return {"deleted": False, "deactivated": True, "service": row}
    try:
        await sb(supabase.table("billable_services").delete().eq("clinic_id", clinic_id).eq("id", service["id"]))
    except Exception as e:
        _db_error(e)
    return {"deleted": True, "deactivated": False}


async def import_legacy_catalog(clinic_id: str, items: list[dict], overwrite: bool = True) -> int:
    """Write legacy-shaped items into the master (the OPD settings API writing
    through; also the idempotent backfill with overwrite=False)."""
    res = await sb(supabase.rpc("opd_import_service_catalog", {
        "p_clinic_id": clinic_id, "p_items": items, "p_overwrite": overwrite}))
    return int(res.data or 0)
