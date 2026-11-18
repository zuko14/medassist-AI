"""Kriya Pharmacy OS — stock answer (Ketu), dashboard KPIs, lost-sale call list (P9; RX-61, RX-62, RX-80).

Zero-LLM, read-only. The stock answer resolves deterministically (rx_resolve_items: barcode → alias / code → brand /
generic trigram) and lists same-generic items purely as information — the response never carries advice text.
Design: docs/pharmacy-os/KRIYA-KETU.md §2.1, UI.md §9, ADVANCED-FEATURES.md A12.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.database import scoped_query
from app.services import rx_catalogue, rx_pos
from app.services.rx import _rows
from app.services.rx_pos import mask_phone
from app.services.rx_stock import packs_loose
from app.utils.helpers import today_ist

IST = ZoneInfo("Asia/Kolkata")
WARD_STORES = ("ward_substore", "ot_tray", "emergency_tray")
RESOLVED_BY = {"barcode": "barcode", "alias": "alias", "code": "alias", "brand": "trgm", "generic": "trgm"}


async def _by_ids(table: str, clinic_id: str, ids, fields: str = "*") -> dict:
    ids = sorted({str(i) for i in ids if i})
    out: dict = {}
    for i in range(0, len(ids), 200):
        for r in await _rows(scoped_query(table, clinic_id, fields).in_("id", ids[i:i + 200])):
            out[str(r["id"])] = r
    return out


def _day_bounds(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time.min, IST)
    return start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()


# ── stock answer (KRIYA-KETU §2.1) ──────────────────────────────────────────

def _confident(cands: list[dict]) -> bool:
    top = cands[0]
    if top["source"] in ("barcode", "alias", "code") or len(cands) == 1:
        return True
    return top["score"] >= 85 and cands[1]["score"] < top["score"]


async def stock_answer(clinic_id: str, q: str, stores: list[dict], nurse: bool) -> dict:
    """`stores`: the stores this login may see. A nurse (IPD_NURSING, no RX_READ) gets exact quantities only for ward
    stores in scope, and for the pharmacy only `available_at_pharmacy` — never another store's count."""
    cands = await rx_catalogue.resolve(clinic_id, q, 5)
    if not cands:
        return {"status": "not_found", "query": q, "resolved_by": None, "matches": []}
    resolved_by = RESOLVED_BY.get(cands[0]["source"], "trgm")
    if not _confident(cands):
        return {"status": "ambiguous", "query": q, "resolved_by": resolved_by, "matches": [
            {"item": {k: c.get(k) for k in ("item_id", "item_code", "brand_name", "schedule")}} for c in cands[:3]]}
    top = cands[0]
    item = (await _rows(scoped_query("rx_items", clinic_id, "id, item_code, brand_name, generic_id, schedule, pack_size, "
                                     "pack_label, base_unit").eq("id", top["item_id"]).limit(1)))[0]
    generic = (await _rows(scoped_query("rx_generics", clinic_id, "id, name, strength, dosage_form")
                           .eq("id", item["generic_id"]).limit(1)) or [None])[0] if item.get("generic_id") else None
    by_id = {str(s["id"]): s for s in stores}
    own = {sid for sid, s in by_id.items() if s.get("store_type") in WARD_STORES} if nurse else set(by_id)
    rows = await _rows(scoped_query("rx_v_item_stock", clinic_id, "store_id, sellable_units, nearest_expiry")
                       .eq("item_id", item["id"]))
    pack = int(item.get("pack_size") or 1)
    per_store, pharmacy_units = [], 0
    for r in rows:
        sid, units = str(r["store_id"]), int(r["sellable_units"] or 0)
        if sid in own:
            per_store.append({"store_id": sid, "store_name": by_id[sid]["name"], "store_code": by_id[sid]["code"],
                              "qty_units": units, "packs_loose": packs_loose(units, pack),
                              "nearest_expiry": r.get("nearest_expiry")})
        elif nurse and by_id.get(sid, {}).get("store_type") not in WARD_STORES:
            pharmacy_units += units                  # a count the nurse never sees: only "available: yes / no"
    per_store.sort(key=lambda s: (-s["qty_units"], s["store_name"]))
    subs = []
    for s in await rx_catalogue.substitutes(clinic_id, item, sorted(own) if nurse else None):
        if int(s.get("stock_units") or 0) > 0:
            subs.append({"item_id": s["id"], "brand_name": s["brand_name"],
                         "qty_units": None if nurse else int(s["stock_units"])})
    total = sum(s["qty_units"] for s in per_store)
    match = {"item": {k: item.get(k) for k in ("id", "item_code", "brand_name", "schedule", "pack_size", "pack_label",
                                               "base_unit")},
             "generic": generic, "stores": per_store, "substitutes_in_stock": subs}
    if nurse:
        match["available_at_pharmacy"] = pharmacy_units > 0
    in_stock = total > 0 or (nurse and pharmacy_units > 0)
    return {"status": "match" if in_stock else "out_of_stock", "query": q, "resolved_by": resolved_by,
            "matches": [match]}


# ── dashboard (UI §9) ───────────────────────────────────────────────────────

def _in(q, col: str, ids: Optional[list[str]]):
    return q if ids is None else q.in_(col, ids or ["00000000-0000-0000-0000-000000000000"])


async def dashboard(clinic_id: str, store_ids: Optional[list[str]], day: date, is_admin: bool, settings: dict) -> dict:
    lo, hi = _day_bounds(day)
    sales = await _rows(_in(scoped_query("rx_sales", clinic_id, "id, net_paise, taxable_paise"), "store_id", store_ids)
                        .eq("status", "completed").gte("completed_at", lo).lt("completed_at", hi).limit(5000))
    returns = await _rows(_in(scoped_query("rx_returns", clinic_id, "id, amount_paise"), "store_id", store_ids)
                          .gte("created_at", lo).lt("created_at", hi).limit(5000))
    indents = await _rows(_in(scoped_query("rx_ipd_indents", clinic_id, "id, priority"), "from_store_id", store_ids)
                          .in_("status", ["raised", "partially_dispensed"]).limit(5000))
    refills = await _rows(scoped_query("rx_refill_requests", clinic_id, "id").in_("status", ["new", "verified"]).limit(5000))
    alerts = await _rows(scoped_query("rx_alerts", clinic_id, "id, kind, store_id, payload, alert_date")
                         .is_("acknowledged_at", "null").limit(5000))
    alerts = [a for a in alerts if store_ids is None or not a.get("store_id") or str(a["store_id"]) in store_ids]
    today = today_ist()
    near_days = int(settings.get("near_expiry_days", 90))
    batches = await _rows(_in(scoped_query("rx_v_batch_stock", clinic_id, "item_id, qty_units, expiry_date, "
                                           "landed_unit_cost_paise_x100"), "store_id", store_ids)
                          .lte("expiry_date", (today + timedelta(days=90)).isoformat()).limit(5000))
    value = lambda b: int(b["qty_units"]) * int(b["landed_unit_cost_paise_x100"] or 0) // 100   # noqa: E731
    expired = [b for b in batches if str(b["expiry_date"]) < today.isoformat()]
    near = [b for b in batches if str(b["expiry_date"]) >= today.isoformat()]
    from app.services.rx_stock import stock as stock_rows
    low = await stock_rows(clinic_id, store_ids, below_reorder=True)
    breaks = [a for a in alerts if a["kind"] == "register_chain_break"]
    out = {
        "date": day.isoformat(), "near_expiry_days": near_days,
        "sales_today_paise": sum(int(s["net_paise"] or 0) for s in sales), "sales_count": len(sales),
        "returns_today_paise": sum(int(r["amount_paise"] or 0) for r in returns), "returns_count": len(returns),
        "pending_indents_count": len(indents), "stat_indents_count": sum(1 for i in indents if i["priority"] == "stat"),
        "pending_refill_requests_count": len(refills),
        "low_stock_count": len(low),
        "near_expiry_value_paise": sum(value(b) for b in near), "near_expiry_batches": len(near),
        "expired_on_hand_count": len(expired), "expired_on_hand_value_paise": sum(value(b) for b in expired),
        "unack_alerts_count": len(alerts),
        # the nightly scan verifies every register (rx_alerts_scan); a break stays red until acknowledged
        "register_chain_status": {"ok": not breaks, "checked": "nightly",
                                  "breaks": [{"register": (a.get("payload") or {}).get("register"),
                                              "first_bad_seq": (a.get("payload") or {}).get("first_bad_seq"),
                                              "alert_date": a.get("alert_date")} for a in breaks]},
        "gross_margin_paise": None, "gross_margin_bps": None,
    }
    if is_admin and sales:                         # cost and margin are RX_ADMIN information (SECURITY §2)
        lines = []
        ids = [s["id"] for s in sales]
        for i in range(0, len(ids), 200):
            lines += await _rows(scoped_query("rx_sale_items", clinic_id, "batch_id, qty_units, returned_units, taxable_paise")
                                 .in_("sale_id", ids[i:i + 200]))
        cost_of = await _by_ids("rx_batches", clinic_id, [ln["batch_id"] for ln in lines], "id, landed_unit_cost_paise_x100")
        taxable = sum(int(ln["taxable_paise"] or 0) for ln in lines)
        cost = sum(int(ln["qty_units"]) * int((cost_of.get(str(ln["batch_id"])) or {}).get("landed_unit_cost_paise_x100") or 0)
                   for ln in lines) // 100
        out["gross_margin_paise"] = taxable - cost
        out["gross_margin_bps"] = (taxable - cost) * 10000 // taxable if taxable else None
    elif is_admin:
        out["gross_margin_paise"], out["gross_margin_bps"] = 0, None
    return out


# ── lost-sale and pending-balance call list (A12 / RX-80) ───────────────────

async def lost_sales(clinic_id: str, day: date) -> list[dict]:
    """OPD prescriptions signed on `day` that the pharmacy never billed, and part-filled ones with units pending.
    Phones are masked here; the full number only through the audited reveal. Nothing is ever sent from this list."""
    lo, hi = _day_bounds(day)
    rxs = await _rows(scoped_query("opd_prescriptions", clinic_id).eq("status", "signed")
                      .gte("signed_at", lo).lt("signed_at", hi).order("signed_at").limit(1000))
    view = await rx_pos.prescriptions_view(clinic_id, rxs)
    pats = await _by_ids("patients", clinic_id, [v["patient_id"] for v in view], "id, name, phone, mrn")
    sold = {str(s["opd_prescription_id"]) for s in await _rows(
        scoped_query("rx_sales", clinic_id, "opd_prescription_id").in_("opd_prescription_id", [r["id"] for r in rxs] or
                                                                       ["00000000-0000-0000-0000-000000000000"])
        .eq("status", "completed"))}
    out = []
    for v in view:
        pending = [{"drug_name": ln["drug_name"], "strength": ln.get("strength"), "pending_units": ln["pending"],
                    "prescribed_units": ln["prescribed_units"]}
                   for ln in v["lines"] if ln["pending"] is None or ln["pending"] > 0]
        if str(v["id"]) not in sold:
            kind = "unbilled_rx"
            pending = [{"drug_name": ln["drug_name"], "strength": ln.get("strength"),
                        "pending_units": ln["prescribed_units"], "prescribed_units": ln["prescribed_units"]}
                       for ln in v["lines"]]
        elif any(p["pending_units"] for p in pending):
            kind = "partial_fulfilment"
            pending = [p for p in pending if p["pending_units"]]
        else:
            continue
        p = pats.get(str(v["patient_id"])) or {}
        out.append({"prescription_id": v["id"], "type": kind, "signed_at": v["signed_at"], "doctor_name": v["doctor"],
                    "patient_id": v["patient_id"], "patient_name": p.get("name") or (v["patient"] or {}).get("patient_name"),
                    "mrn": p.get("mrn"), "phone_masked": mask_phone(p.get("phone")), "items": pending})
    return out


async def reveal_phone(clinic_id: str, prescription_id: str) -> dict:
    rx = (await _rows(scoped_query("opd_prescriptions", clinic_id, "id, patient_id").eq("id", prescription_id).limit(1))
          or [None])[0]
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found.")
    p = (await _rows(scoped_query("patients", clinic_id, "name, phone").eq("id", rx["patient_id"]).limit(1)) or [{}])[0]
    if not p.get("phone"):
        raise HTTPException(status_code=404, detail="No phone number on record for this patient.")
    return {"prescription_id": prescription_id, "patient_name": p.get("name"), "phone": p["phone"]}
