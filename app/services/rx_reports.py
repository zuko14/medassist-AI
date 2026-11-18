"""Kriya Pharmacy OS — GSTR-1 pharmacy section and the management / statutory CSV reports (P10; RX-52, RX-53).

Zero-LLM, read-only, tenant-scoped (scoped_query). No report selects a phone column; patients appear by name only.
Every CSV cell goes through `_csv_safe` (spreadsheet formula injection: brand, supplier and patient names are
user-typed). Money columns are rupees as Decimal, written as plain numbers.
Design: docs/pharmacy-os/API.md §9, AUDIT.md A13 (GAP G14).
"""

import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException

from app.database import fetch_all, scoped_query
from app.services.rx_dashboard import IST, _by_ids, _day_bounds, _in
from app.utils.helpers import today_ist

KINDS = ("sales_register", "purchase_register", "gst_summary", "hsn_summary", "stock_valuation", "movement", "expiry",
         "day_book", "margin")
SLABS = (0, 500, 1200, 1800, 2800, 4000)
MAX_RANGE_DAYS = 366
SALE_COLS = ("id, store_id, sale_kind, patient_id, walkin_name, prescriber_snapshot, status, sale_year, sale_seq, "
             "sale_number, clinic_gstin, buyer_gstin, gross_paise, discount_paise, round_off_paise, net_paise, "
             "taxable_paise, cgst_paise, sgst_paise, paid_paise, completed_at")
LINE_COLS = "sale_id, item_id, batch_id, qty_units, hsn_code, tax_rate_bps, taxable_paise, cgst_paise, sgst_paise"
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


# ── CSV ─────────────────────────────────────────────────────────────────────

def _csv_safe(val):
    """A cell a spreadsheet will not evaluate: text starting with = + - @ TAB CR gets a leading quote.
    Numbers (int / Decimal) stay numbers, so a negative round-off is still -0.05."""
    if val is None:
        return ""
    if isinstance(val, (int, Decimal)) and not isinstance(val, bool):
        return val
    s = str(val)
    return "'" + s if s.startswith(_FORMULA_START) else s


def render_csv(headers: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([_csv_safe(h) for h in headers])
    w.writerows([[_csv_safe(v) for v in r] for r in rows])
    return ("﻿" + buf.getvalue()).encode("utf-8")        # BOM: Excel opens Indian names correctly


def _inr(paise) -> Decimal:
    return (Decimal(int(paise or 0)) / 100).quantize(Decimal("0.01"))


def _ts(ts) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(IST)


def _ist(ts, fmt: str = "%Y-%m-%d") -> str:
    return _ts(ts).strftime(fmt) if ts else ""


# ── shared reads ────────────────────────────────────────────────────────────

async def _fetch(table: str, clinic_id: str, cols: str, f=lambda q: q, order: tuple = ("id",)) -> list[dict]:
    def build():
        q = f(scoped_query(table, clinic_id, cols))
        for col in order:
            q = q.order(col)
        return q
    return await fetch_all(build, label=f"rx_reports:{table}")


async def _children(table: str, clinic_id: str, cols: str, col: str, ids) -> list[dict]:
    ids = sorted({str(i) for i in ids if i})
    out: list[dict] = []
    for k in range(0, len(ids), 200):                   # batches keep the request URL short
        out += await _fetch(table, clinic_id, cols, lambda q, chunk=ids[k:k + 200]: q.in_(col, chunk))
    return out


async def _sales(clinic_id: str, lo: str, hi: str, store_ids: Optional[list[str]],
                 statuses: tuple = ("completed",)) -> list[dict]:
    rows = await _fetch("rx_sales", clinic_id, SALE_COLS,
                        lambda q: _in(q.in_("status", list(statuses)).gte("completed_at", lo).lt("completed_at", hi),
                                      "store_id", store_ids))
    return sorted(rows, key=lambda s: (s["completed_at"] or "", s["sale_year"] or 0, s["sale_seq"] or 0))


async def _lines(clinic_id: str, sales: list[dict]) -> list[dict]:
    return await _children("rx_sale_items", clinic_id, LINE_COLS, "sale_id", [s["id"] for s in sales])


def hsn_rows(lines: list[dict]) -> list[dict]:
    """GSTR-1 Table 12: one row per (HSN, rate)."""
    agg: dict = {}
    for ln in lines:
        a = agg.setdefault((ln["hsn_code"] or "", int(ln["tax_rate_bps"])), [0, 0, 0, 0])
        a[0] += int(ln["qty_units"])
        a[1] += int(ln["taxable_paise"])
        a[2] += int(ln["cgst_paise"])
        a[3] += int(ln["sgst_paise"])
    return [{"hsn_code": h, "tax_rate_bps": r, "total_qty_units": u, "total_value_paise": t + c + s,
             "taxable_paise": t, "cgst_paise": c, "sgst_paise": s} for (h, r), (u, t, c, s) in sorted(agg.items())]


def _series(numbers: list[str], cancelled: int) -> dict:
    return {"from": numbers[0] if numbers else None, "to": numbers[-1] if numbers else None,
            "total": len(numbers), "cancelled": cancelled}


# ── GSTR-1 (RX-52) ──────────────────────────────────────────────────────────

async def pharmacy_gstr1(clinic_id: str, start: str, end: str) -> dict:
    """The pharmacy's share of GSTR-1 for [start, end): PHS- sales by completion time, PCN- credit notes by issue."""
    sales = await _sales(clinic_id, start, end, None, ("completed", "void"))
    sales.sort(key=lambda s: (s["sale_year"] or 0, s["sale_seq"] or 0))
    live = [s for s in sales if s["status"] == "completed"]
    lines = await _lines(clinic_id, live)
    by_sale: dict = {}
    for ln in lines:
        by_sale.setdefault(str(ln["sale_id"]), []).append(ln)
    names = await _by_ids("patients", clinic_id, [s["patient_id"] for s in live if s.get("buyer_gstin")], "id, name")

    b2b, b2cs, nil = [], {}, 0
    for s in live:
        buckets: dict = {}
        for ln in by_sale.get(str(s["id"]), []):
            b = buckets.setdefault(int(ln["tax_rate_bps"]), [0, 0, 0])
            b[0] += int(ln["taxable_paise"])
            b[1] += int(ln["cgst_paise"])
            b[2] += int(ln["sgst_paise"])
        nil += buckets.pop(0, [0, 0, 0])[0]
        if not buckets:
            continue
        if s.get("buyer_gstin"):
            b2b.append({"invoice_number": s["sale_number"], "issued_at": s["completed_at"],
                        "buyer_gstin": s["buyer_gstin"],
                        "buyer_name": (names.get(str(s.get("patient_id"))) or {}).get("name") or s.get("walkin_name"),
                        "invoice_value_paise": int(s["net_paise"]),
                        "rates": [{"rate_bps": r, "taxable_paise": v, "cgst_paise": c, "sgst_paise": g}
                                  for r, (v, c, g) in sorted(buckets.items())]})
        else:
            for r, vals in buckets.items():
                agg = b2cs.setdefault(r, [0, 0, 0])
                for i in range(3):
                    agg[i] += vals[i]

    cns = await _fetch("rx_credit_notes", clinic_id, "id, sale_id, cn_year, cn_seq, cn_number, created_at, amount_paise, "
                       "tax_paise, reason", lambda q: q.gte("created_at", start).lt("created_at", end))
    cns.sort(key=lambda c: (c["cn_year"], c["cn_seq"]))
    cn_sale = await _by_ids("rx_sales", clinic_id, [c["sale_id"] for c in cns],
                            "id, sale_number, completed_at, buyer_gstin")
    credit_notes = []
    for c in cns:
        tax, src = int(c["tax_paise"]), cn_sale.get(str(c["sale_id"])) or {}
        credit_notes.append({
            "credit_note_number": c["cn_number"], "created_at": c["created_at"], "reason": c["reason"],
            "invoice_number": src.get("sale_number"), "invoice_date": src.get("completed_at"),
            "buyer_gstin": src.get("buyer_gstin"), "taxable_paise": int(c["amount_paise"]) - tax,
            "cgst_paise": tax // 2, "sgst_paise": tax - tax // 2, "total_paise": int(c["amount_paise"])})

    return {
        "clinic_gstin": next((s["clinic_gstin"] for s in sales if s.get("clinic_gstin")), None),
        "b2b": b2b,
        "b2cs": [{"rate_bps": r, "taxable_paise": v, "cgst_paise": c, "sgst_paise": g}
                 for r, (v, c, g) in sorted(b2cs.items())],
        "nil_exempt_paise": nil,
        "credit_notes": credit_notes,
        "hsn_summary": hsn_rows(lines),
        "doc_summary": {
            "pharmacy_sales": _series([s["sale_number"] for s in sales], sum(1 for s in sales if s["status"] == "void")),
            "pharmacy_credit_notes": _series([c["cn_number"] for c in cns], 0),    # a PCN- is never cancelled
        },
        "totals": {
            "invoice_value_paise": sum(int(s["net_paise"]) for s in live),
            "tax_paise": sum(int(s["cgst_paise"]) + int(s["sgst_paise"]) for s in live),
            "credit_value_paise": sum(c["total_paise"] for c in credit_notes),
            "credit_tax_paise": sum(c["cgst_paise"] + c["sgst_paise"] for c in credit_notes),
        },
    }


def merge_gstr1(report: dict, ph: dict) -> dict:
    """Fold the pharmacy section into the OPD GSTR-1 summary (gst_billing.export_gstr1_report)."""
    b2cs = {r["rate_bps"]: dict(r) for r in report["b2cs"]}
    for r in ph["b2cs"]:
        row = b2cs.setdefault(r["rate_bps"], {"rate_bps": r["rate_bps"], "taxable_paise": 0, "cgst_paise": 0,
                                              "sgst_paise": 0})
        for k in ("taxable_paise", "cgst_paise", "sgst_paise"):
            row[k] += r[k]
    totals = dict(report["totals"])
    for k, v in ph["totals"].items():
        totals[k] = totals.get(k, 0) + v
    return {**report,
            "clinic_gstin": report.get("clinic_gstin") or ph["clinic_gstin"],
            "b2b": report["b2b"] + ph["b2b"],
            "b2cs": [b2cs[r] for r in sorted(b2cs)],
            "nil_exempt_paise": report["nil_exempt_paise"] + ph["nil_exempt_paise"],
            "credit_notes": report["credit_notes"] + ph["credit_notes"],
            "hsn_summary": ph["hsn_summary"],
            "doc_summary": {**report["doc_summary"], **ph["doc_summary"]},
            "totals": totals}


# ── CSV reports (RX-53) ─────────────────────────────────────────────────────

def date_range(frm: Optional[date], to: Optional[date]) -> tuple[date, date]:
    to = to or today_ist()
    frm = frm or to.replace(day=1)
    if frm > to:
        raise HTTPException(status_code=422, detail="'from' must be on or before 'to'.")
    if (to - frm).days >= MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"A report covers at most {MAX_RANGE_DAYS} days.")
    return frm, to


async def _stores(clinic_id: str, ids) -> dict:
    return await _by_ids("rx_stores", clinic_id, ids, "id, code, name")


def _name(m: dict, key) -> str:
    return (m.get(str(key)) or {}).get("name") or ""


async def _sales_register(cid, lo, hi, stores):
    sales = await _sales(cid, lo, hi, stores, ("completed", "void"))
    pats = await _by_ids("patients", cid, [s["patient_id"] for s in sales], "id, name")
    st = await _stores(cid, [s["store_id"] for s in sales])
    modes: dict = {}
    for p in await _children("rx_sale_payments", cid, "sale_id, mode, amount_paise", "sale_id", [s["id"] for s in sales]):
        if int(p["amount_paise"]) > 0:
            modes.setdefault(str(p["sale_id"]), set()).add(p["mode"])
    head = ["date", "sale_number", "sale_kind", "patient", "doctor", "store", "gross_inr", "discount_inr",
            "round_off_inr", "taxable_inr", "cgst_inr", "sgst_inr", "net_inr", "paid_inr", "payment_modes", "status"]
    return head, [[_ist(s["completed_at"]), s["sale_number"], s["sale_kind"],
                   _name(pats, s.get("patient_id")) or s.get("walkin_name") or "Walk-in",
                   (s.get("prescriber_snapshot") or {}).get("name") or "", _name(st, s["store_id"]),
                   _inr(s["gross_paise"]), _inr(s["discount_paise"]), _inr(s["round_off_paise"]),
                   _inr(s["taxable_paise"]), _inr(s["cgst_paise"]), _inr(s["sgst_paise"]), _inr(s["net_paise"]),
                   _inr(s["paid_paise"]), "+".join(sorted(modes.get(str(s["id"]), ()))), s["status"]] for s in sales]


async def _grns(cid, lo, hi, stores, statuses=("posted", "void")):
    rows = await _fetch("rx_grns", cid, "id, store_id, supplier_id, grn_number, grn_year, grn_seq, supplier_invoice_no, "
                        "supplier_invoice_date, is_interstate, status, taxable_paise, cgst_paise, sgst_paise, igst_paise, "
                        "freight_paise, round_off_paise, invoice_total_paise, posted_at",
                        lambda q: _in(q.in_("status", list(statuses)).gte("posted_at", lo).lt("posted_at", hi),
                                      "store_id", stores))
    return sorted(rows, key=lambda g: (g["posted_at"], g["grn_year"] or 0, g["grn_seq"] or 0))


async def _purchase_register(cid, lo, hi, stores):
    grns = await _grns(cid, lo, hi, stores)
    sup = await _by_ids("rx_suppliers", cid, [g["supplier_id"] for g in grns], "id, name, gstin")
    head = ["date", "grn_number", "supplier_name", "supplier_gstin", "invoice_no", "invoice_date", "is_interstate",
            "taxable_inr", "cgst_inr", "sgst_inr", "igst_inr", "freight_inr", "round_off_inr", "invoice_total_inr",
            "status"]
    return head, [[_ist(g["posted_at"]), g["grn_number"], _name(sup, g["supplier_id"]),
                   (sup.get(str(g["supplier_id"])) or {}).get("gstin") or "", g["supplier_invoice_no"],
                   g["supplier_invoice_date"], "yes" if g["is_interstate"] else "no", _inr(g["taxable_paise"]),
                   _inr(g["cgst_paise"]), _inr(g["sgst_paise"]), _inr(g["igst_paise"]), _inr(g["freight_paise"]),
                   _inr(g["round_off_paise"]), _inr(g["invoice_total_paise"]), g["status"]] for g in grns]


async def _gst_summary(cid, lo, hi, stores):
    out = {r: [0] * 7 for r in SLABS}                # sales taxable, cgst, sgst | purchase taxable, cgst, sgst, igst
    for ln in await _lines(cid, await _sales(cid, lo, hi, stores)):
        o = out.setdefault(int(ln["tax_rate_bps"]), [0] * 7)
        o[0] += int(ln["taxable_paise"])
        o[1] += int(ln["cgst_paise"])
        o[2] += int(ln["sgst_paise"])
    grns = {str(g["id"]): g for g in await _grns(cid, lo, hi, stores, ("posted",))}
    for ln in await _children("rx_grn_lines", cid, "grn_id, tax_rate_bps, taxable_paise, tax_paise", "grn_id", grns):
        o, tax = out.setdefault(int(ln["tax_rate_bps"]), [0] * 7), int(ln["tax_paise"])
        o[3] += int(ln["taxable_paise"])
        if grns[str(ln["grn_id"])]["is_interstate"]:
            o[6] += tax
        else:
            o[4] += tax // 2
            o[5] += tax - tax // 2
    head = ["gst_slab", "sales_taxable_inr", "sales_cgst_inr", "sales_sgst_inr", "purchase_taxable_inr",
            "purchase_cgst_inr", "purchase_sgst_inr", "purchase_igst_inr", "net_gst_payable_inr"]
    rows, total = [], [0] * 7
    for r in sorted(out):
        v = out[r]
        total = [a + b for a, b in zip(total, v)]
        rows.append([f"{r // 100}%", *map(_inr, v), _inr(v[1] + v[2] - v[4] - v[5] - v[6])])
    rows.append(["Total", *map(_inr, total), _inr(total[1] + total[2] - total[4] - total[5] - total[6])])
    return head, rows


async def _hsn_summary(cid, lo, hi, stores):
    head = ["hsn_code", "rate_bps", "total_units", "total_value_inr", "taxable_inr", "cgst_inr", "sgst_inr"]
    return head, [[h["hsn_code"], h["tax_rate_bps"], h["total_qty_units"], _inr(h["total_value_paise"]),
                   _inr(h["taxable_paise"]), _inr(h["cgst_paise"]), _inr(h["sgst_paise"])]
                  for h in hsn_rows(await _lines(cid, await _sales(cid, lo, hi, stores)))]


async def _on_hand(cid, stores) -> list[dict]:
    return await _fetch("rx_v_batch_stock", cid, "store_id, item_id, batch_id, qty_units, batch_no, expiry_date, "
                        "mrp_pack_paise, landed_unit_cost_paise_x100", lambda q: _in(q, "store_id", stores),
                        order=("store_id", "batch_id"))


async def _stock_context(cid, rows):
    items = await _by_ids("rx_items", cid, [r["item_id"] for r in rows], "id, item_code, brand_name, generic_id, pack_size")
    gens = await _by_ids("rx_generics", cid, [i.get("generic_id") for i in items.values()], "id, name, strength")
    return items, gens, await _stores(cid, [r["store_id"] for r in rows])


def _cost(r) -> int:
    return int(r["qty_units"]) * int(r["landed_unit_cost_paise_x100"] or 0) // 100


async def _stock_valuation(cid, lo, hi, stores):
    rows = await _on_hand(cid, stores)
    items, gens, st = await _stock_context(cid, rows)
    head = ["store", "item_code", "brand_name", "generic", "batch_no", "expiry_date", "qty_units", "landed_unit_cost_inr",
            "valuation_at_cost_inr", "mrp_pack_inr", "valuation_at_mrp_inr"]
    out = []
    for r in rows:
        it = items.get(str(r["item_id"])) or {}
        g = gens.get(str(it.get("generic_id"))) or {}
        qty, mrp = int(r["qty_units"]), int(r["mrp_pack_paise"])
        out.append([_name(st, r["store_id"]), it.get("item_code"), it.get("brand_name"),
                    " ".join(x for x in (g.get("name"), g.get("strength")) if x), r["batch_no"], r["expiry_date"], qty,
                    (Decimal(int(r["landed_unit_cost_paise_x100"] or 0)) / 10000).quantize(Decimal("0.0001")),
                    _inr(_cost(r)), _inr(mrp), _inr(qty * mrp // int(it.get("pack_size") or 1))])
    return head, sorted(out, key=lambda x: (x[0], x[1] or "", str(x[5]), x[4]))


async def _movement(cid, lo, hi, stores):
    rows = await _fetch("rx_stock_movements", cid, "store_id, batch_id, item_id, qty_units, movement_type, ref_table, "
                        "ref_id, actor_name, created_at",
                        lambda q: _in(q.gte("created_at", lo).lt("created_at", hi), "store_id", stores),
                        order=("created_at", "id"))
    items = await _by_ids("rx_items", cid, [r["item_id"] for r in rows], "id, item_code, brand_name")
    batches = await _by_ids("rx_batches", cid, [r["batch_id"] for r in rows], "id, batch_no")
    st = await _stores(cid, [r["store_id"] for r in rows])
    head = ["timestamp", "store", "item_code", "brand", "batch_no", "movement_type", "qty_units", "doc_ref", "actor"]
    return head, [[_ist(r["created_at"], "%Y-%m-%d %H:%M:%S"), _name(st, r["store_id"]),
                   (items.get(str(r["item_id"])) or {}).get("item_code"),
                   (items.get(str(r["item_id"])) or {}).get("brand_name"),
                   (batches.get(str(r["batch_id"])) or {}).get("batch_no"), r["movement_type"], int(r["qty_units"]),
                   f"{r['ref_table']}/{r['ref_id']}", r["actor_name"]] for r in rows]


def expiry_bucket(days: int) -> Optional[str]:
    return "Expired" if days < 0 else "≤30d" if days <= 30 else "≤60d" if days <= 60 else "≤90d" if days <= 90 else None


async def _expiry(cid, lo, hi, stores):
    today = today_ist()
    rows = [r for r in await _on_hand(cid, stores)
            if (date.fromisoformat(str(r["expiry_date"])) - today).days <= 90]
    items, _, st = await _stock_context(cid, rows)
    head = ["store", "item_code", "brand", "batch_no", "expiry_date", "days_to_expiry", "bucket", "qty_units",
            "value_at_cost_inr"]
    out = []
    for r in rows:
        days = (date.fromisoformat(str(r["expiry_date"])) - today).days
        it = items.get(str(r["item_id"])) or {}
        out.append([_name(st, r["store_id"]), it.get("item_code"), it.get("brand_name"), r["batch_no"], r["expiry_date"],
                    days, expiry_bucket(days), int(r["qty_units"]), _inr(_cost(r))])
    return head, sorted(out, key=lambda x: (x[5], x[0], x[1] or ""))


async def _day_book(cid, lo, hi, stores, frm: date, to: date):
    days = {(frm + timedelta(days=i)).isoformat(): {"n": 0, "gross": 0, "disc": 0, "net": 0, "ret": 0, "cash": 0,
                                                    "upi": 0, "card": 0, "wallet": 0, "grn_n": 0, "grn": 0}
            for i in range((to - frm).days + 1)}
    for s in await _sales(cid, lo, hi, stores):
        d = days[_ist(s["completed_at"])]
        d["n"] += 1
        d["gross"] += int(s["gross_paise"])
        d["disc"] += int(s["discount_paise"])
        d["net"] += int(s["net_paise"])
    for r in await _fetch("rx_returns", cid, "amount_paise, created_at",
                          lambda q: _in(q.gte("created_at", lo).lt("created_at", hi), "store_id", stores)):
        days[_ist(r["created_at"])]["ret"] += int(r["amount_paise"])
    pays = await _fetch("rx_sale_payments", cid, "sale_id, mode, amount_paise, created_at",
                        lambda q: q.in_("mode", ["cash", "upi", "card", "wallet"]).gte("created_at", lo).lt("created_at", hi))
    if stores is not None:          # a refund may be on an older sale: its store decides, not the payment's day
        owner = await _by_ids("rx_sales", cid, [p["sale_id"] for p in pays], "id, store_id")
        pays = [p for p in pays if str((owner.get(str(p["sale_id"])) or {}).get("store_id")) in stores]
    for p in pays:
        days[_ist(p["created_at"])][p["mode"]] += int(p["amount_paise"])
    for g in await _grns(cid, lo, hi, stores, ("posted",)):
        d = days[_ist(g["posted_at"])]
        d["grn_n"] += 1
        d["grn"] += int(g["invoice_total_paise"])
    head = ["date", "sales_count", "gross_inr", "discount_inr", "net_inr", "returns_inr", "net_cash_inr", "net_upi_inr",
            "net_card_inr", "net_wallet_inr", "grn_count", "grn_total_inr"]
    return head, [[day, d["n"], _inr(d["gross"]), _inr(d["disc"]), _inr(d["net"]), _inr(d["ret"]), _inr(d["cash"]),
                   _inr(d["upi"]), _inr(d["card"]), _inr(d["wallet"]), d["grn_n"], _inr(d["grn"])]
                  for day, d in days.items()]


async def _margin(cid, lo, hi, stores):
    sales = await _sales(cid, lo, hi, stores)
    day_of = {str(s["id"]): _ist(s["completed_at"]) for s in sales}
    lines = await _lines(cid, sales)
    cost = await _by_ids("rx_batches", cid, [ln["batch_id"] for ln in lines], "id, landed_unit_cost_paise_x100")
    items = await _by_ids("rx_items", cid, [ln["item_id"] for ln in lines], "id, item_code, brand_name")
    agg: dict = {}
    for ln in lines:
        a = agg.setdefault((day_of[str(ln["sale_id"])], str(ln["item_id"])), [0, 0, 0])
        a[0] += int(ln["qty_units"])
        a[1] += int(ln["taxable_paise"])
        a[2] += int(ln["qty_units"]) * int((cost.get(str(ln["batch_id"])) or {}).get("landed_unit_cost_paise_x100") or 0)
    head = ["date", "item_code", "brand", "units", "revenue_taxable_inr", "cogs_inr", "gross_margin_inr", "margin_pct"]
    out = []
    for (day, item), (units, rev, cost_x100) in sorted(agg.items(), key=lambda kv: (kv[0][0], (items.get(kv[0][1]) or {})
                                                                                      .get("item_code") or "")):
        cogs = cost_x100 // 100
        it = items.get(item) or {}
        pct = (Decimal(rev - cogs) * 100 / rev).quantize(Decimal("0.01")) if rev else None
        out.append([day, it.get("item_code"), it.get("brand_name"), units, _inr(rev), _inr(cogs), _inr(rev - cogs), pct])
    return head, out


_BUILDERS = {"sales_register": _sales_register, "purchase_register": _purchase_register, "gst_summary": _gst_summary,
             "hsn_summary": _hsn_summary, "stock_valuation": _stock_valuation, "movement": _movement,
             "expiry": _expiry, "margin": _margin}


async def build(clinic_id: str, kind: str, frm: date, to: date, store_ids: Optional[list[str]]) -> tuple[list, list]:
    """(headers, rows) for one report. stock_valuation and expiry are as of now; the rest cover [frm, to] IST."""
    lo, hi = _day_bounds(frm)[0], _day_bounds(to)[1]
    if kind == "day_book":
        return await _day_book(clinic_id, lo, hi, store_ids, frm, to)
    return await _BUILDERS[kind](clinic_id, lo, hi, store_ids)
