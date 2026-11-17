"""Kriya Pharmacy OS — inpatient pharmacy (migration 139; RX-38, RX-39, RX-40).

Ward indents against the doctor's ACTIVE medication orders, the pharmacy queue, dispense
(stock out + IPD charge in one SQL transaction, rx_dispense_indent), nurse receipt, issue
slip, MAR batch links and ward returns (void + re-issue, or a credit note on an issued bill).
Every stock, price and bill rule runs in SQL; this module only validates input, picks FEFO
batches and shapes reads. Zero-LLM: drug ↔ item mapping is the deterministic resolver
(ADR-RX-06). No IPD table is written here except through the SQL functions (VERIFICATION D2).
Design: docs/pharmacy-os/WORKFLOWS.md W4 / W5b, API.md §6, DATA-MODEL.md §6.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.database import sb, scoped_query, supabase
from app.services.rx import _rows, raise_for
from app.services.rx_catalogue import resolve
from app.services.rx_stock import get_fefo_batches
from app.services.rx_tax import _FREQ, prescribed_units

logger = logging.getLogger(__name__)

PRIORITY_RANK = {"stat": 0, "urgent": 1, "routine": 2}
OPEN = ("raised", "partially_dispensed")
_RETURN_NS = uuid.UUID("6f1c2a52-3b2e-4c55-9a77-1d39b0a0f139")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IndentLineIn(_In):
    ipd_order_id: str
    requested_units: int = Field(..., ge=1, le=10000)
    item_id: Optional[str] = None


class IndentIn(_In):
    admission_id: str
    from_store_id: str
    priority: Literal["routine", "urgent", "stat"] = "routine"
    lines: list[IndentLineIn] = Field(..., min_length=1, max_length=100)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=80)


class DispenseLineIn(_In):
    indent_line_id: str
    item_id: Optional[str] = None                 # absent → the indent line's item
    batch_id: Optional[str] = None                # absent → FEFO (may split over batches)
    qty_units: Optional[int] = Field(None, ge=1, le=10000)
    substitution_reason: Optional[str] = Field(None, min_length=3, max_length=120)
    fefo_override_reason: Optional[str] = Field(None, min_length=3, max_length=120)
    not_available: bool = False


class DispenseIn(_In):
    store_id: str
    lines: list[DispenseLineIn] = Field(..., min_length=1, max_length=100)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=80)


class CancelIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)


class ReturnLineIn(_In):
    dispense_line_id: str
    qty_units: int = Field(..., ge=1, le=10000)
    restock: bool = True


class ReturnIn(_In):
    lines: list[ReturnLineIn] = Field(..., min_length=1, max_length=100)
    reason: str = Field(..., min_length=3, max_length=200)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=80)


async def _rpc(fn: str, args: dict):
    try:
        return (await sb(supabase.rpc(fn, args))).data
    except Exception as e:
        raise_for(e)


async def _by_ids(table: str, clinic_id: str, ids, fields: str = "*", col: str = "id") -> dict[str, dict]:
    ids = sorted({str(i) for i in ids if i})
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 200):
        for r in await _rows(scoped_query(table, clinic_id, fields).in_(col, ids[i:i + 200])):
            out[str(r[col])] = r
    return out


def default_units(order: dict) -> int:
    """24 h need from the CLOSED frequency map (ADR-RX-06): oral solid doses from rx_tax, else one unit per
    dose; PRN / unknown frequency → 1. The nurse always edits it."""
    if order.get("is_prn"):
        return 1
    if order.get("route") in ("oral", "sl"):
        units = prescribed_units(order.get("dose"), order.get("frequency"), 1, "tablet")
        if units:
            return units
    return _FREQ.get((order.get("frequency") or "").strip().upper()) or 1


# ── admissions ──────────────────────────────────────────────────────────────

async def get_admission(clinic_id: str, admission_id: str) -> dict:
    rows = await _rows(scoped_query("ipd_admissions", clinic_id, "id, branch_id, admission_number, patient_id, status, "
                                    "current_bed_id").eq("id", admission_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Admission not found.")
    return rows[0]


def require_active(admission: dict) -> None:
    if admission["status"] not in ("admitted", "discharge_pending"):
        raise HTTPException(status_code=409, detail={"code": "rx_admission_not_active",
                                                     "message": "This patient is no longer admitted."})


async def _ward_of(clinic_id: str, admission_id: str) -> Optional[str]:
    rows = await _rows(scoped_query("ipd_bed_assignments", clinic_id, "ward_id").eq("admission_id", admission_id)
                       .is_("ended_at", "null").limit(1))
    return str(rows[0]["ward_id"]) if rows else None


async def indentable(clinic_id: str, admission: dict) -> dict:
    """Active medication orders of the stay, each with what was already dispensed, what is still on an open
    indent, a 24 h default, deterministic item candidates and their ward sub-store stock (W4 step 2)."""
    orders = await _rows(scoped_query("ipd_orders", clinic_id, "id, description, drug_name, dose, route, frequency, "
                                      "is_prn, instructions, priority, ordered_by_name, ordered_at")
                         .eq("admission_id", admission["id"]).eq("order_type", "medication").eq("status", "active")
                         .order("ordered_at"))
    ward = await _ward_of(clinic_id, admission["id"])
    ward_stores = [s["id"] for s in await _rows(scoped_query("rx_stores", clinic_id, "id").eq("ward_id", ward)
                                                .eq("is_active", True))] if ward else []
    lines = await _rows(scoped_query("rx_indent_lines", clinic_id, "ipd_order_id, item_id, requested_units, "
                                     "dispensed_units, returned_units, status")
                        .in_("ipd_order_id", [o["id"] for o in orders])) if orders else []
    out = []
    for o in orders:
        mine = [ln for ln in lines if str(ln["ipd_order_id"]) == str(o["id"])]
        candidates = await resolve(clinic_id, o["drug_name"] or o["description"], 5)
        last_item = next((ln["item_id"] for ln in reversed(mine) if ln.get("item_id")), None)
        stock = {}
        ids = [c.get("id") or c.get("item_id") for c in candidates] + ([last_item] if last_item else [])
        if ward_stores and ids:
            for r in await _rows(scoped_query("rx_v_item_stock", clinic_id, "item_id, sellable_units")
                                 .in_("store_id", ward_stores).in_("item_id", [i for i in ids if i])):
                stock[str(r["item_id"])] = stock.get(str(r["item_id"]), 0) + int(r["sellable_units"] or 0)
        out.append({**o, "default_units": default_units(o), "last_item_id": last_item,
                    "dispensed_units": sum(int(ln["dispensed_units"]) - int(ln["returned_units"]) for ln in mine),
                    "pending_units": sum(int(ln["requested_units"]) - int(ln["dispensed_units"]) for ln in mine
                                         if ln["status"] in ("open", "partial")),
                    "candidates": [{"item_id": c.get("id") or c.get("item_id"), "brand_name": c.get("brand_name"),
                                    "ward_stock_units": stock.get(str(c.get("id") or c.get("item_id")), 0)}
                                   for c in candidates],
                    "ward_stock_units": stock.get(str(last_item), 0) if last_item and ward_stores else None})
    return {"admission": admission, "ward_id": ward, "ward_store_ids": ward_stores, "orders": out}


# ── indents ─────────────────────────────────────────────────────────────────

async def raise_indent(clinic_id: str, body: IndentIn, actor: tuple) -> dict:
    return await _rpc("rx_raise_indent", {
        "p_clinic_id": clinic_id, "p_admission_id": body.admission_id, "p_store_id": body.from_store_id,
        "p_priority": body.priority, "p_lines": [ln.model_dump() for ln in body.lines], "p_idem": body.idempotency_key,
        "p_actor_id": actor[0], "p_actor_name": actor[1]})


async def get_indent_row(clinic_id: str, indent_id: str) -> dict:
    rows = await _rows(scoped_query("rx_ipd_indents", clinic_id).eq("id", indent_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Indent not found.")
    return rows[0]


async def _context(clinic_id: str, indents: list[dict]) -> tuple[dict, dict, dict, dict]:
    adms = await _by_ids("ipd_admissions", clinic_id, [i["admission_id"] for i in indents],
                         "id, admission_number, patient_id, current_bed_id, status")
    pats = await _by_ids("patients", clinic_id, [a["patient_id"] for a in adms.values()], "id, name, mrn, gender, age_years")
    beds = await _by_ids("ipd_beds", clinic_id, [a["current_bed_id"] for a in adms.values()], "id, code")
    wards = await _by_ids("ipd_wards", clinic_id, [i["ward_id"] for i in indents], "id, name, code")
    return adms, pats, beds, wards


def _summary(i: dict, adms: dict, pats: dict, beds: dict, wards: dict, now: datetime) -> dict:
    a = adms.get(str(i["admission_id"])) or {}
    p = pats.get(str(a.get("patient_id"))) or {}
    raised = datetime.fromisoformat(str(i["raised_at"]).replace("Z", "+00:00"))
    return {**i, "admission_number": a.get("admission_number"), "patient_name": p.get("name"), "mrn": p.get("mrn"),
            "bed": (beds.get(str(a.get("current_bed_id"))) or {}).get("code"),
            "ward": (wards.get(str(i.get("ward_id"))) or {}).get("name"),
            "age_minutes": max(int((now - raised).total_seconds() // 60), 0)}


async def list_indents(clinic_id: str, store_ids: Optional[list[str]], status: Optional[str], ward_id: Optional[str],
                       priority: Optional[str], admission_id: Optional[str], limit: int) -> list[dict]:
    """The queue: STAT → URGENT → ROUTINE, then oldest first. Default = open indents."""
    q = scoped_query("rx_ipd_indents", clinic_id)
    q = q.eq("status", status) if status else q.in_("status", list(OPEN))
    for col, val in (("ward_id", ward_id), ("priority", priority), ("admission_id", admission_id)):
        if val:
            q = q.eq(col, val)
    if store_ids is not None:
        q = q.in_("from_store_id", store_ids or ["00000000-0000-0000-0000-000000000000"])
    rows = await _rows(q.order("raised_at").limit(limit))
    rows.sort(key=lambda r: (PRIORITY_RANK[r["priority"]], str(r["raised_at"])))
    counts: dict[str, int] = {}
    for ln in (await _rows(scoped_query("rx_indent_lines", clinic_id, "indent_id").in_("indent_id", [r["id"] for r in rows]))
               if rows else []):
        counts[str(ln["indent_id"])] = counts.get(str(ln["indent_id"]), 0) + 1
    ctx = await _context(clinic_id, rows)
    now = datetime.now(timezone.utc)
    return [{**_summary(r, *ctx, now), "lines_count": counts.get(str(r["id"]), 0)} for r in rows]


async def indent_detail(clinic_id: str, indent: dict) -> dict:
    """Indent with lines (order, item, FEFO suggestion from the supplying store), dispenses and their lines."""
    lines = await _rows(scoped_query("rx_indent_lines", clinic_id).eq("indent_id", indent["id"]).order("line_no"))
    orders = await _by_ids("ipd_orders", clinic_id, [ln["ipd_order_id"] for ln in lines],
                           "id, description, drug_name, dose, route, frequency, is_prn, status")
    dispenses = await _rows(scoped_query("rx_dispenses", clinic_id).eq("indent_id", indent["id"]).order("dispensed_at"))
    dls = await _rows(scoped_query("rx_dispense_lines", clinic_id).in_("dispense_id", [d["id"] for d in dispenses])
                      .order("created_at")) if dispenses else []
    items = await _by_ids("rx_items", clinic_id, [ln["item_id"] for ln in lines] + [d["item_id"] for d in dls],
                          "id, item_code, brand_name, generic_id, schedule, pack_size, base_unit")
    batches = await _by_ids("rx_batches", clinic_id, [d["batch_id"] for d in dls], "id, batch_no, expiry_date")
    out_lines = []
    for ln in lines:
        order = orders.get(str(ln["ipd_order_id"])) or {}
        left = int(ln["requested_units"]) - int(ln["dispensed_units"])
        fefo = None
        if ln.get("item_id") and ln["status"] in ("open", "partial") and left > 0:
            fefo = await get_fefo_batches(clinic_id, indent["from_store_id"], ln["item_id"], left)
        out_lines.append({**ln, "order": order, "order_stopped": order.get("status") != "active",
                          "item": items.get(str(ln.get("item_id"))), "remaining_units": left, "fefo": fefo,
                          "candidates": [] if ln.get("item_id") else
                          await resolve(clinic_id, order.get("drug_name") or order.get("description") or "", 5)})
    for d in dls:
        d["item"] = items.get(str(d["item_id"]))
        d["batch"] = batches.get(str(d["batch_id"]))
    ctx = await _context(clinic_id, [indent])
    return {**_summary(indent, *ctx, datetime.now(timezone.utc)), "lines": out_lines,
            "dispenses": [{**d, "lines": [x for x in dls if x["dispense_id"] == d["id"]]} for d in dispenses]}


async def _fefo_rows(clinic_id: str, store_id: str, indent: dict, body: DispenseIn, settings: dict) -> list[dict]:
    """Request lines → SQL lines: FEFO split where no batch is named; a later-expiring batch needs a reason."""
    lines = {str(ln["id"]): ln for ln in
             await _rows(scoped_query("rx_indent_lines", clinic_id).eq("indent_id", indent["id"]))}
    rows: list[dict] = []
    for ln in body.lines:
        cur = lines.get(ln.indent_line_id)
        if not cur:
            raise HTTPException(status_code=404, detail="Indent line not found.")
        if ln.not_available:
            rows.append({"indent_line_id": ln.indent_line_id, "not_available": True})
            continue
        item_id = ln.item_id or cur.get("item_id")
        if not item_id:
            raise HTTPException(status_code=422, detail={"code": "rx_indent_line_unmapped",
                                                         "message": "Pick the medicine for this line first."})
        qty = ln.qty_units or int(cur["requested_units"]) - int(cur["dispensed_units"])
        extra = {"substitution_reason": ln.substitution_reason, "fefo_override_reason": ln.fefo_override_reason}
        if ln.batch_id:
            fefo = await get_fefo_batches(clinic_id, store_id, item_id, 1)
            first = fefo["picks"][0]["batch_id"] if fefo["picks"] else None
            if first and str(first) != ln.batch_id and settings.get("fefo_override_requires_reason", True) \
                    and not ln.fefo_override_reason:
                raise HTTPException(status_code=422, detail={"code": "rx_fefo_reason_required", "message":
                                    "Picking a later-expiring batch needs a reason."})
            rows.append({"indent_line_id": ln.indent_line_id, "item_id": item_id, "batch_id": ln.batch_id,
                         "qty_units": qty, **extra})
            continue
        fefo = await get_fefo_batches(clinic_id, store_id, item_id, qty)
        if fefo["shortfall_units"]:
            raise HTTPException(status_code=409, detail={"code": "rx_insufficient_stock", "message":
                                f"Only {qty - fefo['shortfall_units']} units are in stock at this store."})
        rows += [{"indent_line_id": ln.indent_line_id, "item_id": item_id, "batch_id": p["batch_id"],
                  "qty_units": p["take_units"], **extra} for p in fefo["picks"] if p["take_units"] > 0]
    return rows


async def dispense(clinic_id: str, indent: dict, body: DispenseIn, actor: tuple, settings: dict) -> dict:
    rows = await _fefo_rows(clinic_id, body.store_id, indent, body, settings)
    return await _rpc("rx_dispense_indent", {
        "p_clinic_id": clinic_id, "p_indent_id": indent["id"], "p_store_id": body.store_id, "p_lines": rows,
        "p_actor_id": actor[0], "p_actor_name": actor[1], "p_idem": body.idempotency_key})


async def cancel(clinic_id: str, indent: dict, reason: str, actor: tuple) -> dict:
    return await _rpc("rx_cancel_indent", {"p_clinic_id": clinic_id, "p_indent_id": indent["id"], "p_reason": reason,
                                           "p_actor_id": actor[0]})


# ── dispenses ───────────────────────────────────────────────────────────────

async def get_dispense_row(clinic_id: str, dispense_id: str) -> dict:
    rows = await _rows(scoped_query("rx_dispenses", clinic_id).eq("id", dispense_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Dispense not found.")
    return rows[0]


async def receive(clinic_id: str, dispense: dict, actor: tuple) -> dict:
    return await _rpc("rx_receive_dispense", {"p_clinic_id": clinic_id, "p_dispense_id": dispense["id"],
                                              "p_actor_id": actor[0], "p_actor_name": actor[1]})


def return_id(clinic_id: str, key: Optional[str]) -> str:
    """A retry with the same idempotency key is the same return (rx_ipd_return replays on this id)."""
    return str(uuid.uuid5(_RETURN_NS, f"{clinic_id}:{key}")) if key else str(uuid.uuid4())


async def ipd_return(clinic_id: str, dispense: dict, body: ReturnIn, actor: tuple, is_admin: bool) -> dict:
    if len({ln.dispense_line_id for ln in body.lines}) != len(body.lines):
        raise HTTPException(status_code=422, detail="Each dispensed line appears once per return.")
    return await _rpc("rx_ipd_return", {
        "p_clinic_id": clinic_id, "p_dispense_id": dispense["id"], "p_lines": [ln.model_dump() for ln in body.lines],
        "p_reason": body.reason, "p_return_id": return_id(clinic_id, body.idempotency_key), "p_actor_id": actor[0],
        "p_actor_name": actor[1], "p_is_admin": is_admin})


async def link_mar(clinic_id: str, mar_id: str, dispense_line_id: str, actor_id: Optional[str]) -> dict:
    """Best-effort batch link after the unchanged MAR insert (W4 step 6): a refusal never fails the dose
    record; the caller shows "batch not linked" with the reason."""
    try:
        return {"rx_link": "linked", **((await sb(supabase.rpc("rx_link_mar", {
            "p_clinic_id": clinic_id, "p_mar_id": mar_id, "p_dispense_line_id": dispense_line_id,
            "p_actor_id": actor_id}))).data or {})}
    except Exception as e:
        msg = str(getattr(e, "message", None) or e)
        code = next((c for c in ("rx_mar_line_mismatch", "rx_mar_already_linked", "rx_dispense_line_not_found",
                                 "rx_mar_not_found") if c in msg), "error")
        logger.warning(f"RX_MAR_LINK_REFUSED code={code}")
        return {"rx_link": f"refused: {code}"}


async def slip_model(clinic_id: str, dispense: dict, clinic: dict, store: dict) -> dict:
    indent = await get_indent_row(clinic_id, dispense["indent_id"])
    detail = await indent_detail(clinic_id, indent)
    d = next(x for x in detail["dispenses"] if x["id"] == dispense["id"])
    final_bill = bool(await _rows(scoped_query("opd_invoices", clinic_id, "id").eq("admission_id", indent["admission_id"])
                                  .neq("status", "void").limit(1)))
    return {"clinic": clinic.get("name") or "", "store": store.get("name") or "", "indent": detail, "dispense": d,
            "final_bill": final_bill,
            "lines": [{"name": (x["item"] or {}).get("brand_name", ""), "batch": (x["batch"] or {}).get("batch_no", ""),
                       "exp": str((x["batch"] or {}).get("expiry_date") or "")[:7], "qty": x["qty_units"],
                       "returned": x["returned_units"], "amount": int(x["qty_units"]) * int(x["unit_price_paise"]),
                       "subst": x.get("substitution_reason")} for x in d["lines"]]}


def render_slip_pdf(m: dict) -> bytes:
    from app.services.opd_pdf import _a4, _footer, _kv_rows, _table, _title
    from app.services.rx_pos_docs import _ist, _rs
    ind, d = m["indent"], m["dispense"]
    pdf = _a4("Ward issue slip")
    _title(pdf, f"WARD ISSUE SLIP {ind.get('indent_number') or ''}")
    _kv_rows(pdf, [("Hospital", m["clinic"]), ("From store", m["store"]), ("Patient", ind.get("patient_name")),
                   ("Admission", ind.get("admission_number")), ("Ward / bed", f"{ind.get('ward') or ''} / {ind.get('bed') or ''}"),
                   ("Priority", ind["priority"].upper()), ("Dispensed", f"{_ist(d['dispensed_at'])} by {d['dispensed_by_name']}"),
                   ("Received", f"{_ist(d['received_at'])} by {d['received_by_name']}" if d.get("received_at") else "—")])
    _table(pdf, (78, 30, 20, 16, 30), ("Item", "Batch", "Exp", "Qty", "Amount"),
           [(ln["name"] + (" (substitute)" if ln["subst"] else ""), ln["batch"], ln["exp"], str(ln["qty"]),
             _rs(ln["amount"])) for ln in m["lines"]], emphasis=())
    pdf.set_font("NotoSans", "", 8)
    total = sum(ln["amount"] for ln in m["lines"])
    notes = [f"Charged to the IPD bill: {_rs(total)}.",
             "Final bill already generated — regenerate before discharge." if m["final_bill"] else "",
             "Received by (nurse): ____________________    Delivered by: ____________________"]
    for text in filter(None, notes):
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 4.2, text, new_x="LMARGIN", new_y="NEXT")
    return _footer(pdf, f"Ward issue slip {ind.get('indent_number') or ''}")


async def open_indent_count(clinic_id: str) -> int:
    return len(await _rows(scoped_query("rx_ipd_indents", clinic_id, "id").in_("status", list(OPEN)).limit(1000)))
