"""Kriya Pharmacy OS — stock, FEFO, expiry, reorder, write-offs, opening stock (migration 136, RX-18…RX-20).

Stock is never written from here (ADR-RX-01): every movement goes through an rx_* SQL
function (rx_post_writeoff, rx_opening_stock; GRN / POS / IPD functions in later phases).
Reads come from the rx_v_* views over the balance projection.
Design: docs/pharmacy-os/DATA-MODEL.md §3, API.md §3, ROLLOUT.md §4.3.
"""

import calendar
import re
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase
from app.services.rx import _rows, raise_for
from app.services.rx_catalogue import sql_row_errors
from app.utils.helpers import today_ist


def _in(query, col: str, ids: Optional[list[str]]):
    return query if ids is None else query.in_(col, ids)


def expiry_status(row: dict, today: date, settings: dict) -> str:
    exp = date.fromisoformat(str(row["expiry_date"]))
    if row.get("is_blocked"):
        return "blocked"
    if exp < today:
        return "expired"
    if (exp - today).days < int(settings.get("min_shelf_days_at_sale", 30)):
        return "short_shelf"
    if (exp - today).days <= int(settings.get("near_expiry_days", 90)):
        return "near"
    return "ok"


def packs_loose(units: int, pack_size: int) -> dict:
    return {"packs": units // pack_size, "loose": units % pack_size} if pack_size > 1 else {"packs": units, "loose": 0}


# ── FEFO ────────────────────────────────────────────────────────────────────

async def get_fefo_batches(clinic_id: str, store_id: str, item_id: str, required_qty: int) -> dict:
    """First-expiry-first-out pick over sellable batches (not blocked, not expired), earliest
    expiry first. Advisory: the ledger re-checks stock and expiry when the movement is posted."""
    rows = await _rows(scoped_query("rx_v_fefo", clinic_id).eq("store_id", store_id).eq("item_id", item_id)
                       .order("fefo_rank").limit(200))
    picks, left = [], max(int(required_qty), 0)
    for r in rows:
        if left <= 0:
            break
        take = min(left, int(r["qty_units"]))
        picks.append({"batch_id": r["batch_id"], "batch_no": r["batch_no"], "expiry_date": r["expiry_date"],
                      "mrp_pack_paise": r["mrp_pack_paise"], "available_units": r["qty_units"], "take_units": take})
        left -= take
    return {"picks": picks, "available_units": sum(int(r["qty_units"]) for r in rows), "shortfall_units": left}


# ── reads ───────────────────────────────────────────────────────────────────

async def _items_by_id(clinic_id: str, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 200):
        for r in await _rows(scoped_query("rx_items", clinic_id, "id, item_code, brand_name, generic_id, schedule, "
                                          "pack_size, pack_label, base_unit, reorder_level_units, reorder_qty_units, "
                                          "is_active").in_("id", ids[i:i + 200])):
            out[r["id"]] = r
    return out


async def stock(clinic_id: str, store_ids: Optional[list[str]], q: Optional[str] = None,
                schedule: Optional[str] = None, below_reorder: bool = False, with_value: bool = False) -> list[dict]:
    # shortcut: one page of 1000 store×item rows; paginate when a clinic carries more SKUs than that
    rows = await _rows(_in(scoped_query("rx_v_item_stock", clinic_id), "store_id", store_ids).limit(1000))
    items = await _items_by_id(clinic_id, sorted({r["item_id"] for r in rows}))
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["item_id"], {"qty_units": 0, "sellable_units": 0, "value_paise": 0,
                                          "nearest_expiry": None, "by_store": []})
        a["qty_units"] += int(r["qty_units"])
        a["sellable_units"] += int(r["sellable_units"])
        a["value_paise"] += int(r["value_paise"] or 0)
        if r.get("nearest_expiry") and (a["nearest_expiry"] is None or r["nearest_expiry"] < a["nearest_expiry"]):
            a["nearest_expiry"] = r["nearest_expiry"]
        a["by_store"].append({"store_id": r["store_id"], "qty_units": int(r["qty_units"])})
    needle = (q or "").strip().lower()
    out = []
    for item_id, a in agg.items():
        item = items.get(item_id)
        if not item or (schedule and item["schedule"] != schedule):
            continue
        if needle and needle not in item["brand_name"].lower() and needle not in item["item_code"].lower():
            continue
        if below_reorder and not (item["reorder_level_units"] and a["qty_units"] < item["reorder_level_units"]):
            continue
        if not with_value:
            a.pop("value_paise")          # cost is RX_ADMIN / RX_PURCHASE information (SECURITY §2)
        out.append({"item": item, **a, "packs_loose": packs_loose(a["qty_units"], int(item["pack_size"]))})
    return sorted(out, key=lambda r: (r["item"]["brand_name"].lower(), r["item"]["id"]))


async def batches(clinic_id: str, item_id: str, store_ids: Optional[list[str]], settings: dict) -> list[dict]:
    rows = await _rows(_in(scoped_query("rx_v_batch_stock", clinic_id).eq("item_id", item_id), "store_id", store_ids)
                       .order("expiry_date").order("batch_created_at").limit(500))
    today = today_ist()
    for r in rows:
        r["expiry_status"] = expiry_status(r, today, settings)
        r["days_left"] = (date.fromisoformat(str(r["expiry_date"])) - today).days
        r.pop("landed_unit_cost_paise_x100", None)
    rank: dict[str, int] = {}
    for r in rows:                                     # FEFO rank among sellable batches, per store
        if r["expiry_status"] not in ("expired", "blocked"):
            rank[r["store_id"]] = rank.get(r["store_id"], 0) + 1
            r["fefo_rank"] = rank[r["store_id"]]
        else:
            r["fefo_rank"] = None
    return rows


async def expiry(clinic_id: str, store_ids: Optional[list[str]], days: int) -> dict:
    today = today_ist()
    rows = await _rows(_in(scoped_query("rx_v_batch_stock", clinic_id), "store_id", store_ids)
                       .lte("expiry_date", str(today + timedelta(days=days))).order("expiry_date").limit(1000))
    items = await _items_by_id(clinic_id, sorted({r["item_id"] for r in rows}))
    buckets = {"expired": [], "le_30": [], "le_60": [], "le_90": [], "later": []}
    for r in rows:
        left = (date.fromisoformat(str(r["expiry_date"])) - today).days
        key = "expired" if left < 0 else "le_30" if left <= 30 else "le_60" if left <= 60 else "le_90" if left <= 90 \
            else "later"
        value = int(r["qty_units"]) * int(r.pop("landed_unit_cost_paise_x100") or 0) // 100
        buckets[key].append({**r, "days_left": left, "value_paise": value, "item": items.get(r["item_id"])})
    if not buckets["later"]:
        buckets.pop("later")
    return {"today": str(today), "within_days": days, "buckets": buckets,
            "totals": {k: {"batches": len(v), "units": sum(int(x["qty_units"]) for x in v),
                           "value_paise": sum(x["value_paise"] for x in v)} for k, v in buckets.items()}}


async def reorder(clinic_id: str, store_ids: Optional[list[str]]) -> list[dict]:
    items = await _rows(scoped_query("rx_items", clinic_id, "id, item_code, brand_name, schedule, pack_size, pack_label, "
                                     "base_unit, reorder_level_units, reorder_qty_units")
                        .eq("is_active", True).gt("reorder_level_units", 0).order("brand_name").limit(1000))
    on_hand: dict[str, int] = {}
    if items:
        q = scoped_query("rx_v_item_stock", clinic_id, "item_id, sellable_units").in_("item_id", [i["id"] for i in items])
        for r in await _rows(_in(q, "store_id", store_ids)):
            on_hand[r["item_id"]] = on_hand.get(r["item_id"], 0) + int(r["sellable_units"] or 0)
    out = []
    for i in items:
        have = on_hand.get(i["id"], 0)
        if have < i["reorder_level_units"]:
            want = max(int(i["reorder_qty_units"]) or int(i["reorder_level_units"]) - have, 1)
            out.append({"item": i, "sellable_units": have, "reorder_level_units": i["reorder_level_units"],
                        "suggested_packs": -(-want // int(i["pack_size"]))})
    # W1 step 1: group by the supplier of the latest posted GRN line (migration 137)
    last = {}
    ids = [r["item"]["id"] for r in out]
    for i in range(0, len(ids), 200):
        for r in await _rows(scoped_query("rx_v_item_last_supplier", clinic_id).in_("item_id", ids[i:i + 200])):
            last[r["item_id"]] = r
    for r in out:
        r["last_supplier"] = last.get(r["item"]["id"])
    return out


# ── writes (all through rx_* SQL functions) ─────────────────────────────────

async def get_batch(clinic_id: str, batch_id: str) -> dict:
    rows = await _rows(scoped_query("rx_batches", clinic_id).eq("id", batch_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return rows[0]


async def batch_store_ids(clinic_id: str, batch_id: str) -> set[str]:
    return {r["store_id"] for r in await _rows(scoped_query("rx_stock_balances", clinic_id, "store_id")
                                               .eq("batch_id", batch_id).gt("qty_units", 0))}


async def set_blocked(clinic_id: str, batch_id: str, blocked: bool, reason: Optional[str]) -> dict:
    """Quarantine flag only: the batch guard refuses any other field."""
    try:
        res = await sb(supabase.table("rx_batches").update({"is_blocked": blocked, "block_reason": reason if blocked
                                                            else None})
                       .eq("clinic_id", clinic_id).eq("id", batch_id))
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return res.data[0]


def writeoff_ref(clinic_id: str, idempotency_key: Optional[str]) -> str:
    """A replayed key maps to the same ledger reference, so a retry never writes off twice."""
    if idempotency_key:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kriya:rx_writeoff:{clinic_id}:{idempotency_key}"))
    return str(uuid.uuid4())


async def post_writeoff(clinic_id: str, store_id: str, ref_id: str, lines: list[dict], actor_id: Optional[str],
                        actor_name: str) -> dict:
    try:
        res = await sb(supabase.rpc("rx_post_writeoff", {"p_clinic_id": clinic_id, "p_store_id": store_id,
                                                         "p_ref_id": ref_id, "p_lines": lines,
                                                         "p_actor_id": actor_id, "p_actor_name": actor_name}))
    except Exception as e:
        raise_for(e)
    return res.data


# ── opening stock (ROLLOUT §4.3) ────────────────────────────────────────────

def _paise(v: Optional[str]) -> Optional[int]:
    """Rupees text ('35.50', '₹1,200') → integer paise, half-up."""
    if v is None:
        return None
    try:
        return int((Decimal(re.sub(r"[₹,\s]", "", v)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


def parse_expiry(v: Optional[str]) -> Optional[date]:
    """'MM/YYYY' (also MM-YYYY, MM/YY) → last day of that month (DATA-MODEL §0)."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[/-]\s*(\d{2}|\d{4})\s*", v or "")
    if not m or not 1 <= int(m.group(1)) <= 12:
        return None
    year = int(m.group(2)) + (2000 if len(m.group(2)) == 2 else 0)
    return date(year, int(m.group(1)), calendar.monthrange(year, int(m.group(1)))[1])


def _int(v: Optional[str]) -> Optional[int]:
    return int(v) if v is not None and re.fullmatch(r"\d{1,9}", v) else None


async def validate_opening_rows(clinic_id: str, rows: list[dict], has_gstin: bool) -> tuple[list[dict], list[dict]]:
    """→ (rows for rx_opening_stock, [{row, field, msg}]). Landed cost per unit = purchase rate per pack
    (excl. GST with a GSTIN — input credit — incl. GST without one) / pack size, ×100 for sub-paise precision."""
    codes = sorted({str(r.get("item_code", "")).strip().upper() for r in rows if r.get("item_code")})
    items: dict[str, dict] = {}
    for i in range(0, len(codes), 200):
        for it in await _rows(scoped_query("rx_items", clinic_id, "item_code, pack_size").in_("item_code",
                                                                                            codes[i:i + 200])):
            items[it["item_code"]] = it
    clean, errors, seen, today = [], [], {}, today_ist()
    for r in rows:
        n, errs = r["_row"], []
        code = str(r.get("item_code", "")).strip().upper()
        item = items.get(code)
        if not item:
            errs.append(("item_code", "unknown item code (import the drug master first)"))
        batch_no = (r.get("batch_no") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9/._-]{1,30}", batch_no):
            errs.append(("batch_no", "1–30 letters, digits or / . _ -"))
        exp = parse_expiry(r.get("expiry_mm_yyyy"))
        if not exp:
            errs.append(("expiry_mm_yyyy", "expected MM/YYYY"))
        elif exp < today:
            errs.append(("expiry_mm_yyyy", "expired stock cannot be opening stock; write it off in the old system"))
        mrp, rate = _paise(r.get("mrp_per_pack")), _paise(r.get("purchase_rate_per_pack"))
        if not mrp or mrp <= 0:
            errs.append(("mrp_per_pack", "MRP per pack in rupees, more than 0"))
        if rate is None or rate < 0:
            errs.append(("purchase_rate_per_pack", "purchase rate per pack in rupees"))
        gst = (r.get("gst_rate_percent") or "0").strip().rstrip("%").removesuffix(".0")
        if gst not in ("0", "5", "12", "18", "28", "40"):
            errs.append(("gst_rate_percent", "one of 0, 5, 12, 18, 28, 40"))
        qty = _int(r.get("qty_units"))
        if qty is None and (r.get("packs") or r.get("loose")) and item:
            packs, loose = _int(r.get("packs") or "0"), _int(r.get("loose") or "0")
            qty = None if packs is None or loose is None else packs * int(item["pack_size"]) + loose
        if not qty:
            errs.append(("qty_units", "a positive whole number of units (or packs + loose)"))
        key = (code, batch_no, exp, mrp)
        if not errs and key in seen:
            errs.append(("batch_no", f"duplicate of row {seen[key]}"))
        if errs:
            errors.extend({"row": n, "field": f, "msg": m} for f, m in errs)
            continue
        seen[key] = n
        pack = int(item["pack_size"])
        gross = 10000 if has_gstin else 10000 + int(gst) * 100            # GST in bps over 10 000
        landed_x100 = (rate * 100 * gross + pack * 10000 // 2) // (pack * 10000)
        clean.append({"item_code": code, "batch_no": batch_no, "expiry_date": str(exp), "mrp_pack_paise": mrp,
                      "ptr_pack_paise": rate, "landed_unit_cost_paise_x100": landed_x100, "qty_units": qty, "_row": n})
    return clean, errors


async def opening_stock(clinic_id: str, store_id: str, rows: list[dict], apply: bool, actor_id: Optional[str],
                        actor_name: str) -> dict:
    try:
        res = await sb(supabase.rpc("rx_opening_stock", {"p_clinic_id": clinic_id, "p_store_id": store_id,
                                                         "p_rows": rows, "p_apply": apply, "p_actor_id": actor_id,
                                                         "p_actor_name": actor_name}))
    except Exception as e:
        raise_for(e)
    out = dict(res.data or {})
    out["errors"] = sql_row_errors(out.get("errors") or [])
    return out
