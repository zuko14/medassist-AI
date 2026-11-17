"""Kriya Pharmacy OS — suppliers, purchase orders, goods receipt, supplier returns (migration 137, RX-23…RX-26).

Every number (taxable, GST, freight share, landed cost, PO / GRN / SRN numbers) is computed in SQL
(rx_grn_line_calc, rx_post_grn, rx_send_po, rx_supplier_return); this module validates input,
reads documents back and renders the PO and shelf-label PDFs. Stock moves only through those
functions (ADR-RX-01). Design: docs/pharmacy-os/DATA-MODEL.md §4, WORKFLOWS.md W1 / W6.
Zero-LLM.
"""

import re
from datetime import date, datetime, timezone
from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database import sb, scoped_query, supabase
from app.services.gst_billing import valid_gstin
from app.services.rx import _rows, raise_for
from app.services.rx_catalogue import TaxBps
from app.services.rx_stock import parse_expiry
from app.utils.helpers import today_ist

ReturnReason = Literal["expired", "near_expiry", "damaged", "recall", "excess", "other"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ── input models ────────────────────────────────────────────────────────────

class SupplierIn(_In):
    name: str = Field(..., min_length=2, max_length=120)
    gstin: Optional[str] = Field(None, max_length=15)
    state_code: Optional[str] = Field(None, pattern=r"^[0-9]{2}$")
    drug_licence_no: Optional[str] = Field(None, max_length=60)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=120)
    address: Optional[str] = Field(None, max_length=300)
    payment_terms_days: int = Field(30, ge=0, le=365)
    lead_time_days: int = Field(3, ge=0, le=90)
    return_window_days: Optional[int] = Field(None, ge=0, le=365)

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = v.upper()
        if not valid_gstin(v):
            raise ValueError("GSTIN is not valid (format or check digit)")
        return v


class SupplierPatch(SupplierIn):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    payment_terms_days: Optional[int] = Field(None, ge=0, le=365)
    lead_time_days: Optional[int] = Field(None, ge=0, le=90)
    is_active: Optional[bool] = None


class POLineIn(_In):
    item_id: str
    ordered_packs: int = Field(..., ge=1, le=1_000_000)
    free_packs: int = Field(0, ge=0, le=1_000_000)
    expected_rate_pack_paise: Optional[int] = Field(None, ge=0, le=100_000_000)


class POIn(_In):
    store_id: str
    supplier_id: str
    expected_on: Optional[date] = None
    notes: Optional[str] = Field(None, max_length=500)
    lines: list[POLineIn] = Field(..., min_length=1, max_length=500)


class POPatch(_In):
    expected_on: Optional[date] = None
    notes: Optional[str] = Field(None, max_length=500)
    lines: Optional[list[POLineIn]] = Field(None, min_length=1, max_length=500)


class GrnLineIn(_In):
    item_id: str
    po_line_id: Optional[str] = None
    batch_no: str = Field(..., pattern=r"^[A-Za-z0-9/._-]{1,30}$")
    expiry: str = Field(..., description="MM/YYYY (month end), DD/MM/YYYY or YYYY-MM-DD")
    mfg_date: Optional[date] = None
    gs1_serial: Optional[str] = Field(None, max_length=40)
    packs: int = Field(..., ge=1, le=1_000_000)
    free_packs: int = Field(0, ge=0, le=1_000_000)
    rate_pack_paise: int = Field(..., ge=0, le=100_000_000)
    scheme_discount_bps: int = Field(0, ge=0, le=10000)
    cash_discount_bps: int = Field(0, ge=0, le=10000)
    tax_rate_bps: Optional[TaxBps] = None          # default: the item master's rate
    mrp_pack_paise: int = Field(..., ge=1, le=100_000_000)


class GrnIn(_In):
    store_id: str
    supplier_id: str
    po_id: Optional[str] = None
    supplier_invoice_no: str = Field(..., min_length=1, max_length=40)
    supplier_invoice_date: date
    freight_paise: int = Field(0, ge=0, le=100_000_000)
    invoice_total_paise: int = Field(0, ge=0, le=10_000_000_000)
    is_interstate: Optional[bool] = None           # default: supplier state ≠ clinic GSTIN state
    lines: list[GrnLineIn] = Field(default_factory=list, max_length=500)


class GrnPatch(_In):
    supplier_invoice_no: Optional[str] = Field(None, min_length=1, max_length=40)
    supplier_invoice_date: Optional[date] = None
    freight_paise: Optional[int] = Field(None, ge=0, le=100_000_000)
    invoice_total_paise: Optional[int] = Field(None, ge=0, le=10_000_000_000)
    is_interstate: Optional[bool] = None
    lines: Optional[list[GrnLineIn]] = Field(None, max_length=500)


class ReturnLineIn(_In):
    batch_id: str
    qty_units: int = Field(..., ge=1, le=1_000_000)


class ReturnIn(_In):
    store_id: str
    supplier_id: str
    reason: ReturnReason
    notes: Optional[str] = Field(None, max_length=300)
    lines: list[ReturnLineIn] = Field(..., min_length=1, max_length=500)


class CreditIn(_In):
    credit_received_paise: int = Field(..., ge=0, le=10_000_000_000)
    credit_ref: str = Field(..., min_length=1, max_length=60)


# ── helpers ─────────────────────────────────────────────────────────────────

async def _one(table: str, clinic_id: str, row_id: str, what: str) -> dict:
    rows = await _rows(scoped_query(table, clinic_id).eq("id", row_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail=f"{what} not found.")
    return rows[0]


async def _exec(query) -> list[dict]:
    try:
        return list((await sb(query)).data or [])
    except Exception as e:
        raise_for(e)


async def _rpc(fn: str, args: dict) -> dict:
    try:
        return (await sb(supabase.rpc(fn, args))).data
    except Exception as e:
        raise_for(e)


async def _by_ids(table: str, clinic_id: str, ids, fields: str = "*") -> dict[str, dict]:
    ids = sorted({str(i) for i in ids if i})
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 200):
        for r in await _rows(scoped_query(table, clinic_id, fields).in_("id", ids[i:i + 200])):
            out[str(r["id"])] = r
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_any_expiry(v: str) -> date:
    """MM/YYYY → month end (printed expiry, DATA-MODEL §0); DD/MM/YYYY or ISO → that day."""
    v = (v or "").strip()
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", v)
    try:
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return date.fromisoformat(v)
    except ValueError:
        pass
    d = parse_expiry(v)
    if not d:
        raise HTTPException(status_code=422, detail=f"Expiry '{v}': use MM/YYYY, DD/MM/YYYY or YYYY-MM-DD.")
    return d


# ── suppliers ───────────────────────────────────────────────────────────────

async def list_suppliers(clinic_id: str, active: Optional[bool] = None) -> list[dict]:
    q = scoped_query("rx_suppliers", clinic_id).order("name").limit(1000)
    return await _rows(q.eq("is_active", active) if active is not None else q)


def _supplier_row(body: SupplierIn, partial: bool = False) -> dict:
    row = body.model_dump(exclude_unset=partial)
    if row.get("gstin"):
        row["state_code"] = row["gstin"][:2]      # the GSTIN decides the state (interstate GST)
    return row


async def create_supplier(clinic_id: str, body: SupplierIn) -> dict:
    # unscoped: insert_scoped_by_payload
    return (await _exec(supabase.table("rx_suppliers").insert({**_supplier_row(body), "clinic_id": clinic_id})))[0]


async def update_supplier(clinic_id: str, supplier_id: str, body: SupplierPatch) -> dict:
    row = _supplier_row(body, partial=True)
    if not row:
        return await _one("rx_suppliers", clinic_id, supplier_id, "Supplier")
    rows = await _exec(supabase.table("rx_suppliers").update({**row, "updated_at": _now()})
                       .eq("clinic_id", clinic_id).eq("id", supplier_id))
    if not rows:
        raise HTTPException(status_code=404, detail="Supplier not found.")
    return rows[0]


# ── purchase orders ─────────────────────────────────────────────────────────

async def _assert_items(clinic_id: str, item_ids) -> dict[str, dict]:
    items = await _by_ids("rx_items", clinic_id, item_ids)
    missing = {str(i) for i in item_ids} - set(items)
    if missing:
        raise HTTPException(status_code=404, detail="Item not found.")
    return items


async def _insert_lines(table: str, clinic_id: str, rows: list[dict]) -> None:
    if rows:
        # unscoped: insert_scoped_by_payload
        await _exec(supabase.table(table).insert([{**r, "clinic_id": clinic_id} for r in rows]))


async def _replace_lines(table: str, parent_col: str, clinic_id: str, parent_id: str, rows: list[dict]) -> None:
    await _exec(supabase.table(table).delete().eq("clinic_id", clinic_id).eq(parent_col, parent_id))
    await _insert_lines(table, clinic_id, [{**r, parent_col: parent_id} for r in rows])


def _po_lines(lines: list[POLineIn]) -> list[dict]:
    return [{**ln.model_dump(), "line_no": n} for n, ln in enumerate(lines, start=1)]


async def list_pos(clinic_id: str, store_ids: Optional[list[str]], status: Optional[str] = None,
                   supplier_id: Optional[str] = None) -> list[dict]:
    q = scoped_query("rx_purchase_orders", clinic_id).order("created_at", desc=True).limit(200)
    if status:
        q = q.eq("status", status)
    if supplier_id:
        q = q.eq("supplier_id", supplier_id)
    pos = await _rows(q.in_("store_id", store_ids) if store_ids is not None else q)
    sups = await _by_ids("rx_suppliers", clinic_id, [p["supplier_id"] for p in pos], "id, name")
    for p in pos:
        p["supplier_name"] = (sups.get(str(p["supplier_id"])) or {}).get("name")
    return pos


async def get_po(clinic_id: str, po_id: str) -> dict:
    po = await _one("rx_purchase_orders", clinic_id, po_id, "Purchase order")
    po["lines"] = await _rows(scoped_query("rx_po_lines", clinic_id).eq("po_id", po_id).order("line_no"))
    items = await _by_ids("rx_items", clinic_id, [ln["item_id"] for ln in po["lines"]],
                          "id, item_code, brand_name, pack_size, pack_label, base_unit, tax_rate_bps")
    for ln in po["lines"]:
        ln["item"] = items.get(str(ln["item_id"]))
    po["supplier"] = await _one("rx_suppliers", clinic_id, po["supplier_id"], "Supplier")
    return po


async def create_po(clinic_id: str, body: POIn, actor: tuple[Optional[str], str]) -> dict:
    await _one("rx_suppliers", clinic_id, body.supplier_id, "Supplier")
    await _assert_items(clinic_id, [ln.item_id for ln in body.lines])
    # unscoped: insert_scoped_by_payload
    po = (await _exec(supabase.table("rx_purchase_orders").insert({
        "clinic_id": clinic_id, "store_id": body.store_id, "supplier_id": body.supplier_id,
        "expected_on": str(body.expected_on) if body.expected_on else None, "notes": body.notes,
        "created_by": actor[0], "created_by_name": actor[1]})))[0]
    try:
        await _insert_lines("rx_po_lines", clinic_id, [{**r, "po_id": po["id"]} for r in _po_lines(body.lines)])
    except HTTPException:
        await _exec(supabase.table("rx_purchase_orders").delete().eq("clinic_id", clinic_id).eq("id", po["id"]))
        raise
    return await get_po(clinic_id, po["id"])


async def update_po(clinic_id: str, po_id: str, body: POPatch) -> dict:
    po = await get_po(clinic_id, po_id)
    if po["status"] != "draft":
        raise HTTPException(status_code=409, detail={"code": "rx_document_locked",
                                                     "message": "Only a draft purchase order can be edited."})
    head = body.model_dump(exclude_unset=True, exclude={"lines"})
    if "expected_on" in head and head["expected_on"]:
        head["expected_on"] = str(head["expected_on"])
    if head:
        await _exec(supabase.table("rx_purchase_orders").update(head).eq("clinic_id", clinic_id).eq("id", po_id))
    if body.lines is not None:
        await _assert_items(clinic_id, [ln.item_id for ln in body.lines])
        await _replace_lines("rx_po_lines", "po_id", clinic_id, po_id, _po_lines(body.lines))
    return await get_po(clinic_id, po_id)


async def send_po(clinic_id: str, po_id: str) -> dict:
    return await _rpc("rx_send_po", {"p_clinic_id": clinic_id, "p_po_id": po_id})


async def cancel_po(clinic_id: str, po_id: str, reason: str) -> dict:
    po = await _one("rx_purchase_orders", clinic_id, po_id, "Purchase order")
    if po["status"] not in ("draft", "sent"):
        raise HTTPException(status_code=409, detail={"code": "rx_document_locked", "message":
                            "Goods have been received against this order; it can no longer be cancelled."})
    rows = await _exec(supabase.table("rx_purchase_orders")
                       .update({"status": "cancelled", "cancel_reason": reason, "cancelled_at": _now()})
                       .eq("clinic_id", clinic_id).eq("id", po_id).in_("status", ["draft", "sent"]))
    if not rows:
        raise HTTPException(status_code=409, detail={"code": "rx_document_locked",
                                                     "message": "This purchase order changed meanwhile. Reload."})
    return rows[0]


# ── GRN ─────────────────────────────────────────────────────────────────────

async def _grn_line_rows(clinic_id: str, lines: list[GrnLineIn]) -> list[dict]:
    items = await _assert_items(clinic_id, [ln.item_id for ln in lines])
    out = []
    for n, ln in enumerate(lines, start=1):
        row = ln.model_dump(exclude={"expiry", "mfg_date"})
        row.update(line_no=n, expiry_date=str(parse_any_expiry(ln.expiry)),
                   mfg_date=str(ln.mfg_date) if ln.mfg_date else None,
                   tax_rate_bps=ln.tax_rate_bps if ln.tax_rate_bps is not None else items[ln.item_id]["tax_rate_bps"])
        out.append(row)
    return out


def interstate(supplier: dict, clinic: dict) -> bool:
    mine = ((clinic.get("opd_settings") or {}).get("gstin") or "").strip()[:2]
    theirs = supplier.get("state_code") or (supplier.get("gstin") or "")[:2]
    return bool(mine and theirs and mine != theirs)


async def list_grns(clinic_id: str, store_ids: Optional[list[str]], status: Optional[str] = None,
                    supplier_id: Optional[str] = None) -> list[dict]:
    q = scoped_query("rx_grns", clinic_id).order("created_at", desc=True).limit(200)
    if status:
        q = q.eq("status", status)
    if supplier_id:
        q = q.eq("supplier_id", supplier_id)
    grns = await _rows(q.in_("store_id", store_ids) if store_ids is not None else q)
    sups = await _by_ids("rx_suppliers", clinic_id, [g["supplier_id"] for g in grns], "id, name")
    for g in grns:
        g["supplier_name"] = (sups.get(str(g["supplier_id"])) or {}).get("name")
        g["computed_total_paise"] = _computed(g)
    return grns


def _computed(g: dict) -> int:
    return int(g["taxable_paise"]) + int(g["cgst_paise"]) + int(g["sgst_paise"]) + int(g["igst_paise"]) \
        + int(g["freight_paise"])


async def get_grn(clinic_id: str, grn_id: str, settings: dict) -> dict:
    g = await _one("rx_grns", clinic_id, grn_id, "Goods receipt")
    g["lines"] = await _rows(scoped_query("rx_grn_lines", clinic_id).eq("grn_id", grn_id).order("line_no"))
    items = await _by_ids("rx_items", clinic_id, [ln["item_id"] for ln in g["lines"]],
                          "id, item_code, brand_name, pack_size, pack_label, base_unit, schedule, tax_rate_bps")
    last_mrp: dict[str, int] = {}
    if g["lines"] and g["status"] == "draft":
        for b in await _rows(scoped_query("rx_batches", clinic_id, "item_id, mrp_pack_paise, created_at")
                             .in_("item_id", sorted(items)).order("created_at", desc=True).limit(1000)):
            last_mrp.setdefault(str(b["item_id"]), int(b["mrp_pack_paise"]))
    today, near = today_ist(), int(settings.get("near_expiry_days", 90))
    for ln in g["lines"]:
        item = ln["item"] = items.get(str(ln["item_id"]))
        warn = []
        if g["status"] == "draft":
            left = (date.fromisoformat(str(ln["expiry_date"])) - today).days
            if left <= near:
                warn.append(f"Expires in {left} days")
            if ln["rate_pack_paise"] > ln["mrp_pack_paise"]:
                warn.append("Purchase rate is above MRP")
            prev = last_mrp.get(str(ln["item_id"]))
            if prev and prev != ln["mrp_pack_paise"]:
                warn.append(f"MRP differs from the last batch (₹{prev / 100:.2f})")
            if item and item["tax_rate_bps"] != ln["tax_rate_bps"]:
                warn.append(f"GST {ln['tax_rate_bps'] / 100:g}% differs from the drug master "
                            f"({item['tax_rate_bps'] / 100:g}%)")
        ln["warnings"] = warn
    g["supplier"] = await _one("rx_suppliers", clinic_id, g["supplier_id"], "Supplier")
    g["computed_total_paise"] = _computed(g)
    g["difference_paise"] = int(g["invoice_total_paise"]) - g["computed_total_paise"]
    g["can_post"] = g["status"] == "draft" and bool(g["lines"]) and abs(g["difference_paise"]) <= 99
    return g


async def _check_po(clinic_id: str, po_id: Optional[str], supplier_id: str, store_id: str) -> None:
    if not po_id:
        return
    po = await _one("rx_purchase_orders", clinic_id, po_id, "Purchase order")
    if str(po["supplier_id"]) != supplier_id or str(po["store_id"]) != store_id:
        raise HTTPException(status_code=422, detail="The purchase order is for another supplier or store.")
    if po["status"] not in ("sent", "partially_received"):
        raise HTTPException(status_code=409, detail="Goods can be received only against a sent purchase order.")


async def create_grn(clinic_id: str, clinic: dict, store: dict, body: GrnIn, actor: tuple[Optional[str], str],
                     settings: dict) -> dict:
    if not store.get("can_receive_grn") or not store.get("is_active"):
        raise HTTPException(status_code=409, detail={"code": "rx_store_cannot_receive",
                                                     "message": "This store does not receive goods from suppliers."})
    supplier = await _one("rx_suppliers", clinic_id, body.supplier_id, "Supplier")
    await _check_po(clinic_id, body.po_id, body.supplier_id, body.store_id)
    lines = await _grn_line_rows(clinic_id, body.lines)
    # unscoped: insert_scoped_by_payload
    g = (await _exec(supabase.table("rx_grns").insert({
        "clinic_id": clinic_id, "store_id": body.store_id, "supplier_id": body.supplier_id, "po_id": body.po_id,
        "supplier_invoice_no": body.supplier_invoice_no, "supplier_invoice_date": str(body.supplier_invoice_date),
        "freight_paise": body.freight_paise, "invoice_total_paise": body.invoice_total_paise,
        "is_interstate": interstate(supplier, clinic) if body.is_interstate is None else body.is_interstate,
        "created_by": actor[0], "created_by_name": actor[1]})))[0]
    try:
        await _insert_lines("rx_grn_lines", clinic_id, [{**r, "grn_id": g["id"]} for r in lines])
    except HTTPException:
        await _exec(supabase.table("rx_grns").delete().eq("clinic_id", clinic_id).eq("id", g["id"]))
        raise
    return await get_grn(clinic_id, g["id"], settings)


async def update_grn(clinic_id: str, grn_id: str, body: GrnPatch, settings: dict) -> dict:
    g = await _one("rx_grns", clinic_id, grn_id, "Goods receipt")
    if g["status"] != "draft":
        raise HTTPException(status_code=409, detail={"code": "rx_document_locked",
                                                     "message": "Only a draft goods receipt can be edited."})
    head = body.model_dump(exclude_unset=True, exclude={"lines"})
    if head.get("supplier_invoice_date"):
        head["supplier_invoice_date"] = str(head["supplier_invoice_date"])
    head = {k: v for k, v in head.items() if v is not None}
    if body.lines is not None:
        rows = await _grn_line_rows(clinic_id, body.lines)
        await _replace_lines("rx_grn_lines", "grn_id", clinic_id, grn_id, rows)
    if head:
        await _exec(supabase.table("rx_grns").update(head).eq("clinic_id", clinic_id).eq("id", grn_id))
    return await get_grn(clinic_id, grn_id, settings)


async def post_grn(clinic_id: str, grn_id: str, actor: tuple[Optional[str], str]) -> dict:
    return await _rpc("rx_post_grn", {"p_clinic_id": clinic_id, "p_grn_id": grn_id, "p_actor_id": actor[0],
                                      "p_actor_name": actor[1]})


async def void_grn(clinic_id: str, grn_id: str, reason: str, actor: tuple[Optional[str], str]) -> dict:
    return await _rpc("rx_void_grn", {"p_clinic_id": clinic_id, "p_grn_id": grn_id, "p_reason": reason,
                                      "p_actor_id": actor[0], "p_actor_name": actor[1]})


# ── supplier returns ────────────────────────────────────────────────────────

async def list_returns(clinic_id: str, store_ids: Optional[list[str]], status: Optional[str] = None) -> list[dict]:
    q = scoped_query("rx_supplier_returns", clinic_id).order("created_at", desc=True).limit(200)
    if status:
        q = q.eq("status", status)
    rets = await _rows(q.in_("store_id", store_ids) if store_ids is not None else q)
    sups = await _by_ids("rx_suppliers", clinic_id, [r["supplier_id"] for r in rets], "id, name")
    for r in rets:
        r["supplier_name"] = (sups.get(str(r["supplier_id"])) or {}).get("name")
    return rets


async def get_return(clinic_id: str, return_id: str) -> dict:
    r = await _one("rx_supplier_returns", clinic_id, return_id, "Supplier return")
    r["lines"] = await _rows(scoped_query("rx_supplier_return_lines", clinic_id).eq("return_id", return_id))
    batches = await _by_ids("rx_batches", clinic_id, [ln["batch_id"] for ln in r["lines"]],
                            "id, batch_no, expiry_date, mrp_pack_paise, is_blocked")
    items = await _by_ids("rx_items", clinic_id, [ln["item_id"] for ln in r["lines"]],
                          "id, item_code, brand_name, pack_size, pack_label, base_unit")
    taxable = tax = 0
    for ln in r["lines"]:
        ln["batch"], ln["item"] = batches.get(str(ln["batch_id"])), items.get(str(ln["item_id"]))
        pack = int((ln["item"] or {}).get("pack_size") or 1)
        # debit note value at the GRN rate (W6): units × rate per pack / pack size, half-up
        ln["taxable_paise"] = (int(ln["qty_units"]) * int(ln["rate_pack_paise"]) * 2 + pack) // (2 * pack)
        ln["tax_paise"] = (ln["taxable_paise"] * int(ln["tax_rate_bps"]) + 5000) // 10000
        taxable += ln["taxable_paise"]
        tax += ln["tax_paise"]
    r["debit_note"] = {"taxable_paise": taxable, "tax_paise": tax, "total_paise": taxable + tax}
    r["supplier"] = await _one("rx_suppliers", clinic_id, r["supplier_id"], "Supplier")
    return r


async def create_return(clinic_id: str, body: ReturnIn, actor: tuple[Optional[str], str]) -> dict:
    await _one("rx_suppliers", clinic_id, body.supplier_id, "Supplier")
    if len({ln.batch_id for ln in body.lines}) != len(body.lines):
        raise HTTPException(status_code=422, detail="Each batch appears once per return.")
    # unscoped: insert_scoped_by_payload
    r = (await _exec(supabase.table("rx_supplier_returns").insert({
        "clinic_id": clinic_id, "store_id": body.store_id, "supplier_id": body.supplier_id, "reason": body.reason,
        "notes": body.notes, "created_by": actor[0], "created_by_name": actor[1]})))[0]
    try:
        # item / rate / GST are filled from the batch by rx_srn_line_fill; placeholders satisfy NOT NULL
        await _insert_lines("rx_supplier_return_lines", clinic_id, [
            {"return_id": r["id"], "batch_id": ln.batch_id, "qty_units": ln.qty_units, "item_id": None,
             "rate_pack_paise": 0, "tax_rate_bps": 0} for ln in body.lines])
    except HTTPException:
        await _exec(supabase.table("rx_supplier_returns").delete().eq("clinic_id", clinic_id).eq("id", r["id"]))
        raise
    return await get_return(clinic_id, r["id"])


async def dispatch_return(clinic_id: str, return_id: str, actor: tuple[Optional[str], str]) -> dict:
    return await _rpc("rx_supplier_return", {"p_clinic_id": clinic_id, "p_return_id": return_id,
                                             "p_actor_id": actor[0], "p_actor_name": actor[1]})


async def record_credit(clinic_id: str, return_id: str, body: CreditIn) -> dict:
    rows = await _exec(supabase.table("rx_supplier_returns").update(body.model_dump())
                       .eq("clinic_id", clinic_id).eq("id", return_id))
    if not rows:
        raise HTTPException(status_code=404, detail="Supplier return not found.")
    return rows[0]


# ── PDFs ────────────────────────────────────────────────────────────────────

def _rs(paise) -> str:
    return "₹" + f"{int(paise or 0) / 100:,.2f}"


def render_po_pdf(po: dict, clinic: dict, store: dict) -> bytes:
    from app.services.opd_pdf import _a4, _footer, _kv_rows, _table, _title
    pdf = _a4("Purchase order")
    _title(pdf, f"PURCHASE ORDER {po.get('po_number') or '(DRAFT)'}")
    s = po["supplier"]
    _kv_rows(pdf, [("Buyer", clinic.get("name")), ("Store", f"{store.get('code')} · {store.get('name')}"),
                   ("Buyer GSTIN", (clinic.get("opd_settings") or {}).get("gstin")),
                   ("Drug licence", store.get("drug_licence_no")),
                   ("Supplier", s.get("name")), ("Supplier GSTIN", s.get("gstin")),
                   ("Supplier DL", s.get("drug_licence_no")), ("Phone", s.get("phone")),
                   ("Status", po["status"]), ("Expected", po.get("expected_on"))])
    rows, total = [], 0
    for ln in po["lines"]:
        it = ln.get("item") or {}
        value = int(ln.get("expected_rate_pack_paise") or 0) * int(ln["ordered_packs"])
        total += value
        rows.append((ln["line_no"], f"{it.get('brand_name', '')} ({it.get('item_code', '')})",
                     f"{it.get('pack_size', '')} {it.get('base_unit', '')}/{it.get('pack_label', '')}",
                     ln["ordered_packs"], ln["free_packs"],
                     _rs(ln["expected_rate_pack_paise"]) if ln.get("expected_rate_pack_paise") is not None else "—",
                     _rs(value) if value else "—"))
    _table(pdf, (10, 70, 30, 18, 14, 22, 22), ("#", "Item", "Pack", "Packs", "Free", "Rate", "Value"), rows, emphasis=())
    pdf.set_font("NotoSans", "B", 9)
    pdf.cell(0, 6, f"Expected value (excl. GST): {_rs(total)}", align="R")
    pdf.ln(8)
    pdf.set_font("NotoSans", "", 8)
    if po.get("notes"):
        pdf.multi_cell(0, 4.2, f"Notes: {po['notes']}")
    return _footer(pdf, f"Purchase order {po.get('po_number') or 'draft'}")


def render_labels_pdf(grn: dict, copies: str) -> bytes:
    """38 × 25 mm shelf / strip labels: item, batch, expiry, MRP, and a Code 128 of the batch id
    (RX + 10 hex chars) for packs without their own GS1 code. Printing changes nothing."""
    from app.services.dx_barcode import _bars
    from app.services.opd_pdf import OPDDocument
    pdf = OPDDocument(doc_label="Labels", orientation="L", unit="mm", format=(25, 38))
    pdf.set_auto_page_break(False)
    pdf.set_margins(1.5, 1.5, 1.5)
    count = 0
    for ln in grn["lines"]:
        it = ln.get("item") or {}
        units = (int(ln["packs"]) + int(ln["free_packs"])) * int(it.get("pack_size") or 1)
        n = {"1": 1, "per_pack": int(ln["packs"]) + int(ln["free_packs"]), "per_unit": units}[copies]
        for _ in range(min(n, 500 - count)):
            pdf.add_page()
            pdf.set_font("NotoSans", "B", 6.5)
            pdf.set_xy(1.5, 1.5)
            pdf.cell(35, 3, str(it.get("brand_name", ""))[:28])
            pdf.set_font("NotoSans", "", 5.5)
            pdf.set_xy(1.5, 5)
            exp = str(ln["expiry_date"])
            pdf.cell(35, 2.6, f"B: {ln['batch_no']}  EXP: {exp[5:7]}/{exp[2:4]}")
            pdf.set_xy(1.5, 8)
            pdf.cell(35, 2.6, f"MRP {_rs(ln['mrp_pack_paise'])} / {it.get('pack_label', 'pack')}")
            if ln.get("batch_id"):
                code = "RX" + str(ln["batch_id"]).replace("-", "")[:10].upper()
                _bars(pdf, code, 1.5, 11.5, 9, 35)
                pdf.set_xy(1.5, 21)
                pdf.cell(35, 2.4, code, align="C")
            count += 1
        if count >= 500:
            break
    if not count:
        pdf.add_page()
    return bytes(pdf.output())
