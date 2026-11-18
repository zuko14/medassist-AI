"""Kriya Pharmacy OS — consumption-based reorder and stock classification (P10; RX-75, ADVANCED-FEATURES A7).

Zero-LLM, deterministic. From the ledger (rx_stock_movements) per store × item:
  ADU      out-movements (sale, IPD dispense, ward issue) in the last 90 IST days ÷ 90, stored as adu_x1000 (half-up)
  reorder  ceil(ADU × (lead time of the item's last GRN supplier, default 3 + safety_days, default 7))
  max      reorder + ceil(ADU × review_days, default 15)
  ABC      by 90-day consumption value at landed cost, per store: A while the value before it is < 70 %, B < 90 %, C
  FSN      F > 1 unit/day, S > 0, N none
  dead     on hand and no out-movement in 180 IST days (computed when read, with its cost value)
materialised nightly into rx_item_metrics (replaced, not appended). Suggestions are never applied automatically:
apply_reorder writes rx_items only when an RX_ADMIN asks, and the item trigger logs it in rx_item_changes.
"""

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from app.database import sb, supabase
from app.services import rx_catalogue
from app.services.rx import get_rx_settings
from app.services.rx_dashboard import _by_ids, _day_bounds, _in
from app.services.rx_reports import _children, _fetch, _ts
from app.utils.helpers import today_ist

logger = logging.getLogger(__name__)

OUT_TYPES = ("sale_out", "ipd_dispense_out", "ward_issue_out")
ADU_DAYS, DEAD_DAYS, DEFAULT_LEAD_DAYS = 90, 180, 3


def adu_x1000(units: int) -> int:
    return (units * 1000 * 2 + ADU_DAYS) // (2 * ADU_DAYS)          # round(units * 1000 / 90), half-up


def _ceil(a: int, b: int) -> int:
    return -(-a // b)


def suggestions(adu: int, lead_days: int, safety_days: int, review_days: int) -> tuple[int, int]:
    reorder = _ceil(adu * (lead_days + safety_days), 1000)
    return reorder, reorder + _ceil(adu * review_days, 1000)


def fsn(adu: int) -> str:
    return "F" if adu > 1000 else "S" if adu > 0 else "N"


def abc(values: dict) -> dict:
    """{key: value} → {key: 'A'|'B'|'C'}; highest value first, by the share consumed before it."""
    total, before, out = sum(values.values()), 0, {}
    for k in sorted(values, key=lambda k: (-values[k], str(k))):
        out[k] = "C" if not total or not values[k] else "A" if before * 100 < total * 70 else \
            "B" if before * 100 < total * 90 else "C"
        before += values[k]
    return out


async def _out_moves(clinic_id: str, since: str, store_ids: Optional[list[str]] = None) -> list[dict]:
    return await _fetch("rx_stock_movements", clinic_id, "store_id, item_id, qty_units, unit_cost_paise_x100, created_at",
                        lambda q: _in(q.in_("movement_type", list(OUT_TYPES)).gte("created_at", since),
                                      "store_id", store_ids))


async def _lead_times(clinic_id: str, item_ids) -> dict:
    """item → lead_time_days of the supplier on its most recent posted GRN."""
    lines = await _children("rx_grn_lines", clinic_id, "grn_id, item_id", "item_id", item_ids)
    grns = {str(g["id"]): g for g in await _children("rx_grns", clinic_id, "id, supplier_id, status, posted_at", "id",
                                                     [ln["grn_id"] for ln in lines]) if g["status"] == "posted"}
    last: dict = {}
    for ln in lines:
        g = grns.get(str(ln["grn_id"]))
        if g and (str(ln["item_id"]) not in last or str(g["posted_at"]) > str(last[str(ln["item_id"])]["posted_at"])):
            last[str(ln["item_id"])] = g
    sup = await _by_ids("rx_suppliers", clinic_id, [g["supplier_id"] for g in last.values()], "id, lead_time_days")
    return {i: int((sup.get(str(g["supplier_id"])) or {}).get("lead_time_days") or DEFAULT_LEAD_DAYS)
            for i, g in last.items()}


async def compute_clinic(clinic: dict, today: Optional[date] = None) -> dict:
    cid, today = str(clinic["id"]), today or today_ist()
    s = get_rx_settings(clinic)
    safety, review = int(s.get("safety_days", 7)), int(s.get("review_days", 15))
    lo90 = _ts(_day_bounds(today - timedelta(days=ADU_DAYS - 1))[0])
    units, value = defaultdict(int), defaultdict(int)
    keys = set()
    for m in await _out_moves(cid, _day_bounds(today - timedelta(days=DEAD_DAYS - 1))[0]):
        k = (str(m["store_id"]), str(m["item_id"]))
        keys.add(k)
        if _ts(m["created_at"]) >= lo90:
            units[k] -= int(m["qty_units"])                       # out-movements are negative
            value[k] -= int(m["qty_units"]) * int(m["unit_cost_paise_x100"] or 0)
    keys |= {(str(b["store_id"]), str(b["item_id"])) for b in await _fetch(
        "rx_stock_balances", cid, "store_id, item_id", lambda q: q.gt("qty_units", 0), order=("store_id", "batch_id"))}
    lead = await _lead_times(cid, {i for _, i in keys})
    classes: dict = {}
    for store in {st for st, _ in keys}:
        classes.update(abc({k: value[k] for k in keys if k[0] == store}))
    rows = []
    for k in sorted(keys):
        adu = adu_x1000(units[k])
        reorder, mx = suggestions(adu, lead.get(k[1], DEFAULT_LEAD_DAYS), safety, review)
        rows.append({"clinic_id": cid, "store_id": k[0], "item_id": k[1], "adu_x1000": adu, "abc": classes[k],
                     "fsn": fsn(adu), "suggested_reorder_units": reorder, "suggested_max_units": mx,
                     "computed_on": today.isoformat()})
    await sb(supabase.table("rx_item_metrics").delete().eq("clinic_id", cid))       # replaced, not appended
    for i in range(0, len(rows), 500):
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("rx_item_metrics").insert(rows[i:i + 500]))
    return {"computed_on": today.isoformat(), "rows": len(rows)}


async def run_nightly(today: Optional[date] = None) -> dict:
    """03:00 IST (scheduler.py, CAS-locked). Every clinic whose pharmacy is ACTIVE or WIND_DOWN; OFF is skipped."""
    from app.services.tenant import rx_access
    # unscoped: platform_sweep
    res = await sb(supabase.table("clinics").select("*").neq("rx_state", "OFF").eq("is_active", True))
    stats = {"clinics": 0, "rows": 0}
    for clinic in res.data or []:
        if clinic.get("status") == "DELETED" or rx_access(clinic) is None:
            continue
        try:
            stats["rows"] += (await compute_clinic(clinic, today))["rows"]
            stats["clinics"] += 1
        except Exception as e:                            # noqa: BLE001 - one clinic never stops the sweep
            logger.error(f"RX metrics failed for clinic={clinic.get('id')}: {type(e).__name__}")
    logger.info(f"RX metrics: {stats}")
    return stats


async def list_metrics(clinic_id: str, store_ids: Optional[list[str]], abc_class: Optional[str] = None,
                       fsn_class: Optional[str] = None, dead_stock: Optional[bool] = None) -> dict:
    rows = await _fetch("rx_item_metrics", clinic_id, "*", lambda q: _in(q, "store_id", store_ids),
                        order=("store_id", "item_id"))
    items = await _by_ids("rx_items", clinic_id, [r["item_id"] for r in rows], "id, item_code, brand_name, ved_class, "
                          "reorder_level_units, reorder_qty_units")
    stores = await _by_ids("rx_stores", clinic_id, [r["store_id"] for r in rows], "id, code, name")
    on_hand, cost = defaultdict(int), defaultdict(int)
    for b in await _fetch("rx_v_batch_stock", clinic_id, "store_id, item_id, qty_units, landed_unit_cost_paise_x100",
                          lambda q: _in(q, "store_id", store_ids), order=("store_id", "batch_id")):
        k = (str(b["store_id"]), str(b["item_id"]))
        on_hand[k] += int(b["qty_units"])
        cost[k] += int(b["qty_units"]) * int(b["landed_unit_cost_paise_x100"] or 0)
    moved = {(str(m["store_id"]), str(m["item_id"])) for m in await _out_moves(
        clinic_id, _day_bounds(today_ist() - timedelta(days=DEAD_DAYS - 1))[0], store_ids)}
    out = []
    for r in rows:
        k = (str(r["store_id"]), str(r["item_id"]))
        it, dead = items.get(k[1]) or {}, on_hand[k] > 0 and k not in moved
        if (abc_class and r["abc"] != abc_class) or (fsn_class and r["fsn"] != fsn_class) or \
                (dead_stock is not None and dead != dead_stock):
            continue
        out.append({"store_id": k[0], "store_name": (stores.get(k[0]) or {}).get("name"), "item_id": k[1],
                    "item_code": it.get("item_code"), "brand_name": it.get("brand_name"), "ved": it.get("ved_class"),
                    "abc": r["abc"], "fsn": r["fsn"], "adu_x1000": r["adu_x1000"], "on_hand_units": on_hand[k],
                    "stock_value_paise": cost[k] // 100,
                    "current_reorder_level_units": it.get("reorder_level_units"),
                    "current_reorder_qty_units": it.get("reorder_qty_units"),
                    "suggested_reorder_units": r["suggested_reorder_units"],
                    "suggested_max_units": r["suggested_max_units"],
                    "suggested_reorder_qty_units": (r["suggested_max_units"] or 0) - (r["suggested_reorder_units"] or 0),
                    "dead_stock": dead, "dead_stock_value_paise": cost[k] // 100 if dead else 0,
                    "computed_on": r["computed_on"]})
    out.sort(key=lambda x: (x["store_name"] or "", x["abc"] or "C", -x["adu_x1000"], x["item_code"] or ""))
    return {"computed_on": max((r["computed_on"] for r in rows), default=None), "rows": out,
            "dead_stock_value_paise": sum(r["dead_stock_value_paise"] for r in out)}


async def apply_reorder(clinic_id: str, lines: list[dict], actor_id: Optional[str], actor_name: str) -> list[dict]:
    """Write the levels an RX_ADMIN picked. Each item is its own transaction (rx_update_item), logged by the trigger."""
    out = []
    for ln in lines:
        item = await rx_catalogue.update_item(clinic_id, ln["item_id"], {
            "reorder_level_units": ln["reorder_level_units"], "reorder_qty_units": ln["reorder_qty_units"]},
            actor_id, actor_name, "A7 reorder suggestion applied")
        out.append({k: item.get(k) for k in ("id", "item_code", "reorder_level_units", "reorder_qty_units")})
    return out
