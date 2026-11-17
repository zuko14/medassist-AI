"""Kriya Pharmacy OS — physical stock audits (migration 140; RX-45, RX-46).

Start (snapshot of every in-scope batch the store holds) → count (batched, idempotent per batch; blind:
counters never see the system quantity) → review (every line counted; variance by value) → post (reasons on
every non-zero variance; adjust_in / adjust_out movements; a second RX_ADMIN above the value threshold).
Movements during the count are tracked by the ledger trigger rx_sm_audit_delta; a frozen store sells nothing.
All of it runs in SQL; this module validates, shapes reads and hides what a blind counter must not see.
Design: docs/pharmacy-os/WORKFLOWS.md W8, API.md §7, DATA-MODEL.md §7.
"""

from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.database import sb, scoped_query, supabase
from app.services.rx import _rows, raise_for

Reason = Literal["breakage", "theft_suspected", "billing_error", "found", "expiry_missed", "other"]
BLIND_FIELDS = ("system_units_at_start", "movement_delta_since_start", "variance_units", "variance_value_paise")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StartIn(_In):
    store_id: str
    scope: Literal["full", "rack", "item_list", "controlled"] = "full"
    rack: Optional[str] = Field(None, min_length=1, max_length=40)
    item_ids: Optional[list[str]] = Field(None, min_length=1, max_length=500)
    freeze_sales: bool = False

    @model_validator(mode="after")
    def _shape(self):
        if (self.scope == "rack") != bool(self.rack) or (self.scope == "item_list") != bool(self.item_ids):
            raise ValueError("A rack audit names its rack; an item-list audit names its items; other scopes neither.")
        return self


class CountIn(_In):
    batch_id: str
    counted_units: int = Field(..., ge=0, le=1_000_000)


class ReasonRowIn(_In):
    line_id: str
    variance_reason: Reason


class PostIn(_In):
    reasons: list[ReasonRowIn] = Field(default_factory=list, max_length=5000)
    approver_username: Optional[str] = Field(None, min_length=1, max_length=80)
    approver_password: Optional[str] = Field(None, min_length=1, max_length=200)


class CancelIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)


def _value(units, cost_x100) -> Optional[int]:
    """Signed paise value of a variance, truncated like the SQL total (Σ |units| × cost / 100)."""
    if units is None:
        return None
    v = abs(int(units)) * int(cost_x100 or 0) // 100
    return -v if int(units) < 0 else v


async def _rpc(fn: str, args: dict):
    try:
        return (await sb(supabase.rpc(fn, args))).data
    except Exception as e:
        raise_for(e)


async def start(clinic_id: str, body: StartIn, actor: tuple) -> dict:
    return await _rpc("rx_start_audit", {
        "p_clinic_id": clinic_id, "p_store_id": body.store_id, "p_scope": body.scope, "p_rack": body.rack,
        "p_item_ids": body.item_ids, "p_freeze": body.freeze_sales, "p_actor_id": actor[0], "p_actor_name": actor[1]})


async def get_row(clinic_id: str, audit_id: str) -> dict:
    rows = await _rows(scoped_query("rx_stock_audits", clinic_id).eq("id", audit_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Audit not found.")
    return rows[0]


async def list_audits(clinic_id: str, store_ids: Optional[list[str]], status: Optional[str], limit: int) -> list[dict]:
    q = scoped_query("rx_stock_audits", clinic_id)
    if status:
        q = q.eq("status", status)
    if store_ids is not None:
        q = q.in_("store_id", store_ids or ["00000000-0000-0000-0000-000000000000"])
    return await _rows(q.order("started_at", desc=True).limit(limit))


async def detail(clinic_id: str, audit: dict, show_system: bool) -> dict:
    """Lines (rack-sorted) with item / batch. A blind count (status counting, no show_system) hides the system
    quantity, the delta and the variance; review onwards shows variance by value, largest first."""
    lines = await _rows(scoped_query("rx_stock_audit_lines", clinic_id).eq("audit_id", audit["id"]))
    ids = sorted({str(ln["item_id"]) for ln in lines})
    bids = sorted({str(ln["batch_id"]) for ln in lines})
    items, batches = {}, {}
    for i in range(0, len(ids), 200):
        for r in await _rows(scoped_query("rx_items", clinic_id, "id, item_code, brand_name, schedule, rack_location, "
                                          "pack_size, pack_label, base_unit").in_("id", ids[i:i + 200])):
            items[str(r["id"])] = r
    for i in range(0, len(bids), 200):
        for r in await _rows(scoped_query("rx_batches", clinic_id, "id, batch_no, expiry_date, landed_unit_cost_paise_x100")
                             .in_("id", bids[i:i + 200])):
            batches[str(r["id"])] = r
    blind = audit["status"] == "counting" and not show_system
    out = []
    for ln in lines:
        b = batches.get(str(ln["batch_id"])) or {}
        row = {**ln, "item": items.get(str(ln["item_id"])),
               "batch": {k: b.get(k) for k in ("id", "batch_no", "expiry_date")},
               "variance_value_paise": _value(ln.get("variance_units"), b.get("landed_unit_cost_paise_x100"))}
        if blind:
            for k in BLIND_FIELDS:
                row[k] = None
        out.append(row)
    if audit["status"] == "counting":
        out.sort(key=lambda r: ((r["item"] or {}).get("rack_location") or "~", ((r["item"] or {}).get("brand_name") or "").lower()))
    else:
        out.sort(key=lambda r: -abs(r["variance_value_paise"] or 0))
    counted = sum(1 for r in out if r["counted_units"] is not None)
    summary = {"lines": len(out), "counted": counted, "uncounted": len(out) - counted, "blind": blind}
    if not blind:
        summary.update({
            "deficit_value_paise": sum(r["variance_value_paise"] for r in out if (r["variance_value_paise"] or 0) < 0),
            "surplus_value_paise": sum(r["variance_value_paise"] for r in out if (r["variance_value_paise"] or 0) > 0),
            "abs_value_paise": sum(abs(r["variance_value_paise"] or 0) for r in out),
            "lines_with_variance": sum(1 for r in out if r["variance_units"])})
    return {**audit, "lines": out, "summary": summary}


async def record_counts(clinic_id: str, audit: dict, counts: list[CountIn]) -> dict:
    if len({c.batch_id for c in counts}) != len(counts):
        raise HTTPException(status_code=422, detail="Each batch appears once per submission.")
    return await _rpc("rx_record_counts", {"p_clinic_id": clinic_id, "p_audit_id": audit["id"],
                                           "p_counts": [c.model_dump() for c in counts]})


async def review(clinic_id: str, audit: dict) -> dict:
    return await _rpc("rx_review_audit", {"p_clinic_id": clinic_id, "p_audit_id": audit["id"]})


async def post(clinic_id: str, audit: dict, body: PostIn, actor: tuple, approver: Optional[tuple]) -> dict:
    return await _rpc("rx_post_audit", {
        "p_clinic_id": clinic_id, "p_audit_id": audit["id"], "p_reasons": [r.model_dump() for r in body.reasons],
        "p_actor_id": actor[0], "p_actor_name": actor[1], "p_approver_id": approver[0] if approver else None,
        "p_approver_name": approver[1] if approver else None})


async def cancel(clinic_id: str, audit: dict, reason: str) -> dict:
    return await _rpc("rx_cancel_audit", {"p_clinic_id": clinic_id, "p_audit_id": audit["id"], "p_reason": reason})


async def open_count(clinic_id: str) -> int:
    return len(await _rows(scoped_query("rx_stock_audits", clinic_id, "id").in_("status", ["counting", "review"]).limit(1000)))
