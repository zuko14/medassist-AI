"""Kriya Pharmacy OS — inter-store transfers and ward par levels (migration 140; RX-42, RX-78).

Draft (FEFO batches picked from the source when none is named) → dispatch (stock leaves the source,
STR- number) → receive (stock enters the target; short units are written off against the source with
the reason; controlled lines need two different people). Par levels drive a replenishment draft from the
ward store's parent store. Every stock rule runs in SQL (rx_save_transfer / rx_dispatch_transfer /
rx_receive_transfer); pick lists are rack-sorted (ADVANCED-FEATURES A10). Zero-LLM.
Design: docs/pharmacy-os/WORKFLOWS.md W7, API.md §7, DATA-MODEL.md §7.
"""

from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.database import sb, scoped_query, supabase
from app.services.rx import _rows, raise_for
from app.services.rx_stock import get_fefo_batches

WARD_STORE_TYPES = ("ward_substore", "ot_tray", "emergency_tray")
CONTROLLED = ("X", "narcotic", "psychotropic")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TransferLineIn(_In):
    item_id: str
    batch_id: Optional[str] = None        # absent → FEFO from the source (may split over batches)
    units: int = Field(..., ge=1, le=100_000)


class TransferIn(_In):
    from_store_id: str
    to_store_id: str
    lines: list[TransferLineIn] = Field(..., min_length=1, max_length=200)
    notes: Optional[str] = Field(None, max_length=300)


class ReceiveLineIn(_In):
    line_id: str
    received_units: int = Field(..., ge=0, le=100_000)
    discrepancy_reason: Optional[str] = Field(None, min_length=3, max_length=300)


class ReceiveIn(_In):
    lines: list[ReceiveLineIn] = Field(..., min_length=1, max_length=200)


class ReasonIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)


class ParRowIn(_In):
    item_id: str
    par_units: int = Field(..., ge=0, le=100_000)    # 0 removes the par


class ParIn(_In):
    store_id: str
    rows: list[ParRowIn] = Field(..., min_length=1, max_length=500)


class ReplenishIn(_In):
    store_id: str


async def _rpc(fn: str, args: dict):
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


def rack_key(item: Optional[dict]) -> tuple:
    """Pick-list order (A10): rack location first (unracked last), then item name."""
    item = item or {}
    rack = (item.get("rack_location") or "").strip()
    return (rack == "", rack.lower(), (item.get("brand_name") or "").lower())


# ── drafts ──────────────────────────────────────────────────────────────────

async def _sql_lines(clinic_id: str, from_store_id: str, lines: list[TransferLineIn]) -> list[dict]:
    rows: list[dict] = []
    for ln in lines:
        if ln.batch_id:
            rows.append({"item_id": ln.item_id, "batch_id": ln.batch_id, "sent_units": ln.units})
            continue
        fefo = await get_fefo_batches(clinic_id, from_store_id, ln.item_id, ln.units)
        if fefo["shortfall_units"]:
            raise HTTPException(status_code=409, detail={"code": "rx_insufficient_stock", "message":
                                f"Only {ln.units - fefo['shortfall_units']} units of this item are at the source store."})
        rows += [{"item_id": ln.item_id, "batch_id": p["batch_id"], "sent_units": p["take_units"]}
                 for p in fefo["picks"] if p["take_units"] > 0]
    merged: dict[str, dict] = {}                     # one line per batch (UNIQUE (transfer_id, batch_id))
    for r in rows:
        if r["batch_id"] in merged:
            merged[r["batch_id"]]["sent_units"] += r["sent_units"]
        else:
            merged[r["batch_id"]] = dict(r)
    return list(merged.values())


async def save(clinic_id: str, body: TransferIn, actor: tuple, transfer_id: Optional[str] = None,
               source: str = "manual") -> str:
    out = await _rpc("rx_save_transfer", {
        "p_clinic_id": clinic_id, "p_transfer_id": transfer_id, "p_from": body.from_store_id, "p_to": body.to_store_id,
        "p_lines": await _sql_lines(clinic_id, body.from_store_id, body.lines), "p_notes": body.notes,
        "p_source": source, "p_actor_id": actor[0], "p_actor_name": actor[1]})
    return out["transfer_id"]


async def get_row(clinic_id: str, transfer_id: str) -> dict:
    rows = await _rows(scoped_query("rx_transfers", clinic_id).eq("id", transfer_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Transfer not found.")
    return rows[0]


async def detail(clinic_id: str, t: dict) -> dict:
    lines = await _rows(scoped_query("rx_transfer_lines", clinic_id).eq("transfer_id", t["id"]))
    items = await _by_ids("rx_items", clinic_id, [ln["item_id"] for ln in lines],
                          "id, item_code, brand_name, schedule, pack_size, pack_label, base_unit, rack_location")
    batches = await _by_ids("rx_batches", clinic_id, [ln["batch_id"] for ln in lines], "id, batch_no, expiry_date")
    stores = await _by_ids("rx_stores", clinic_id, [t["from_store_id"], t["to_store_id"]], "id, code, name, store_type")
    out = [{**ln, "item": items.get(str(ln["item_id"])), "batch": batches.get(str(ln["batch_id"]))} for ln in lines]
    out.sort(key=lambda ln: rack_key(ln["item"]))
    return {**t, "from_store": stores.get(str(t["from_store_id"])), "to_store": stores.get(str(t["to_store_id"])),
            "lines": out, "controlled": any((ln["item"] or {}).get("schedule") in CONTROLLED for ln in out)}


async def list_transfers(clinic_id: str, store_ids: Optional[list[str]], status: Optional[str], limit: int) -> list[dict]:
    """Transfers touching any of `store_ids` (None = every store), newest first."""
    def q():
        base = scoped_query("rx_transfers", clinic_id)
        return base.eq("status", status) if status else base
    if store_ids is None:
        rows = await _rows(q().order("created_at", desc=True).limit(limit))
    elif not store_ids:
        rows = []
    else:   # from OR to, as two scoped reads
        seen: dict[str, dict] = {}
        for col in ("from_store_id", "to_store_id"):
            for r in await _rows(q().in_(col, store_ids).order("created_at", desc=True).limit(limit)):
                seen[str(r["id"])] = r
        rows = sorted(seen.values(), key=lambda r: str(r["created_at"]), reverse=True)[:limit]
    stores = await _by_ids("rx_stores", clinic_id, [r["from_store_id"] for r in rows] + [r["to_store_id"] for r in rows],
                           "id, code, name, store_type")
    return [{**r, "from_store": stores.get(str(r["from_store_id"])), "to_store": stores.get(str(r["to_store_id"]))}
            for r in rows]


async def dispatch(clinic_id: str, t: dict, actor: tuple, can_controlled: bool) -> dict:
    return await _rpc("rx_dispatch_transfer", {"p_clinic_id": clinic_id, "p_transfer_id": t["id"], "p_actor_id": actor[0],
                                               "p_actor_name": actor[1], "p_can_controlled": can_controlled})


async def receive(clinic_id: str, t: dict, body: ReceiveIn, actor: tuple, can_controlled: bool) -> dict:
    return await _rpc("rx_receive_transfer", {
        "p_clinic_id": clinic_id, "p_transfer_id": t["id"], "p_lines": [ln.model_dump() for ln in body.lines],
        "p_actor_id": actor[0], "p_actor_name": actor[1], "p_can_controlled": can_controlled})


async def cancel(clinic_id: str, t: dict, reason: str) -> dict:
    return await _rpc("rx_cancel_transfer", {"p_clinic_id": clinic_id, "p_transfer_id": t["id"], "p_reason": reason})


def render_challan_pdf(d: dict, clinic: dict) -> bytes:
    from app.services.opd_pdf import _a4, _footer, _kv_rows, _table, _title
    from app.services.rx_pos_docs import _ist
    pdf = _a4("Stock transfer challan")
    _title(pdf, f"STOCK TRANSFER {d.get('transfer_number') or '(draft)'}")
    fs, ts = d.get("from_store") or {}, d.get("to_store") or {}
    _kv_rows(pdf, [("Hospital", clinic.get("name")), ("Status", d["status"]),
                   ("From", f"{fs.get('code', '')} · {fs.get('name', '')}"), ("To", f"{ts.get('code', '')} · {ts.get('name', '')}"),
                   ("Dispatched", f"{_ist(d.get('dispatched_at'))} {d.get('dispatched_by_name') or ''}".strip() or "—"),
                   ("Received", f"{_ist(d.get('received_at'))} {d.get('received_by_name') or ''}".strip() or "—")])
    _table(pdf, (22, 64, 28, 18, 20, 22), ("Rack", "Item", "Batch", "Exp", "Sent", "Received"),
           [((ln["item"] or {}).get("rack_location") or "—",
             (ln["item"] or {}).get("brand_name", "") + (" [CTRL]" if (ln["item"] or {}).get("schedule") in CONTROLLED else ""),
             (ln["batch"] or {}).get("batch_no", ""), str((ln["batch"] or {}).get("expiry_date") or "")[:7],
             str(ln["sent_units"]), "" if ln.get("received_units") is None else str(ln["received_units"]))
            for ln in d["lines"]], emphasis=())
    pdf.set_font("NotoSans", "", 8)
    for text in filter(None, [d.get("notes"),
                              "Two-person rule: a controlled line is received by someone other than the dispatcher."
                              if d.get("controlled") else "",
                              "Dispatched by: ____________________    Received by: ____________________"]):
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 4.2, text, new_x="LMARGIN", new_y="NEXT")
    return _footer(pdf, f"Stock transfer {d.get('transfer_number') or ''}")


# ── par levels (A10 / RX-78) ────────────────────────────────────────────────

async def _on_hand(clinic_id: str, store_id: str, item_ids: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    if item_ids:
        for r in await _rows(scoped_query("rx_v_item_stock", clinic_id, "item_id, sellable_units")
                             .eq("store_id", store_id).in_("item_id", item_ids)):
            out[str(r["item_id"])] = int(r["sellable_units"] or 0)
    return out


async def par_levels(clinic_id: str, store_id: str) -> list[dict]:
    rows = await _rows(scoped_query("rx_par_levels", clinic_id).eq("store_id", store_id))
    items = await _by_ids("rx_items", clinic_id, [r["item_id"] for r in rows],
                          "id, item_code, brand_name, rack_location, pack_size, pack_label, base_unit")
    stock = await _on_hand(clinic_id, store_id, [str(r["item_id"]) for r in rows])
    out = [{**r, "item": items.get(str(r["item_id"])), "on_hand_units": stock.get(str(r["item_id"]), 0),
            "deficit_units": max(int(r["par_units"]) - stock.get(str(r["item_id"]), 0), 0)} for r in rows]
    out.sort(key=lambda r: rack_key(r["item"]))
    return out


async def set_par_levels(clinic_id: str, body: ParIn, actor_id: Optional[str]) -> dict:
    return await _rpc("rx_set_par_levels", {"p_clinic_id": clinic_id, "p_store_id": body.store_id,
                                            "p_rows": [r.model_dump() for r in body.rows], "p_actor_id": actor_id})


async def parent_store(clinic_id: str, store: dict) -> dict:
    """Where a ward store is replenished from: its parent, else its branch's (or the clinic's) central store."""
    if store.get("parent_store_id"):
        rows = await _rows(scoped_query("rx_stores", clinic_id).eq("id", store["parent_store_id"]).eq("is_active", True))
    else:
        q = scoped_query("rx_stores", clinic_id).eq("store_type", "central").eq("is_active", True)
        rows = await _rows(q.eq("branch_id", store["branch_id"]) if store.get("branch_id") else q.is_("branch_id", "null"))
    if not rows or str(rows[0]["id"]) == str(store["id"]):
        raise HTTPException(status_code=409, detail={"code": "rx_no_parent_store", "message":
                            "This store has no parent or central store to be replenished from."})
    return rows[0]


async def replenish(clinic_id: str, store: dict, actor: tuple) -> dict:
    """Draft (never dispatch) a transfer of par − on hand, positive deficits only, FEFO from the parent. A par draft
    for the same pair is refreshed in place, so pressing it twice never doubles the order."""
    parent = await parent_store(clinic_id, store)
    lines, short = [], []
    for p in await par_levels(clinic_id, store["id"]):
        need = int(p["deficit_units"])
        if need <= 0:
            continue
        fefo = await get_fefo_batches(clinic_id, parent["id"], p["item_id"], need)
        got = need - int(fefo["shortfall_units"])
        if got > 0:
            lines.append(TransferLineIn(item_id=str(p["item_id"]), units=got))
        if fefo["shortfall_units"]:
            short.append({"item_id": p["item_id"], "brand_name": (p["item"] or {}).get("brand_name"),
                          "short_units": fefo["shortfall_units"]})
    if not lines:
        return {"transfer": None, "short": short}
    existing = await _rows(scoped_query("rx_transfers", clinic_id, "id").eq("from_store_id", parent["id"])
                           .eq("to_store_id", store["id"]).eq("status", "draft").eq("source", "par").limit(1))
    tid = await save(clinic_id, TransferIn(from_store_id=str(parent["id"]), to_store_id=str(store["id"]), lines=lines,
                                           notes="Par replenishment"), actor,
                     str(existing[0]["id"]) if existing else None, source="par")
    return {"transfer": await detail(clinic_id, await get_row(clinic_id, tid)), "short": short}


async def open_counts(clinic_id: str) -> int:
    return len(await _rows(scoped_query("rx_transfers", clinic_id, "id").eq("status", "dispatched").limit(1000)))
