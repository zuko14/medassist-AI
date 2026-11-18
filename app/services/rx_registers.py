"""Kriya Pharmacy OS — statutory drug registers, batch recall trace, narcotic empties (migration 141;
RX-49, RX-50, RX-74, RX-79).

Register rows are written only by the ledger (trg_rx_sm_register → rx_register_append); this module reads them,
verifies the hash chain and renders the inspection exports (WORKFLOWS.md W9). Exports never carry a phone number:
the snapshots hold none, and _person() prints only name / age / gender / MRN / address.
Design: docs/pharmacy-os/DATA-MODEL.md §8, API.md §8, ADVANCED-FEATURES.md A6 / A11.
"""

import csv
import io
from datetime import date, datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.database import sb, scoped_query, supabase
from app.services.rx import _rows, raise_for
from app.services.rx_transfers import _by_ids

Register = Literal["H", "H1", "X", "narcotic", "psychotropic"]
REGISTER_NAMES = {"H": "Schedule H register", "H1": "Schedule H1 register", "X": "Schedule X register",
                  "narcotic": "Narcotic drugs register (NDPS)", "psychotropic": "Psychotropic substances register"}
ENTRY = {"in": "Receipt", "out": "Issue", "return": "Return", "writeoff": "Write-off", "adjust": "Adjustment"}
EXPORT_CAP = 20000          # rows per export; a wider range is refused with a hint to narrow it
PAGE = 1000


class EmptiesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    units: int = Field(..., ge=1, le=10000)
    returned_by: UUID                                               # the nurse's login id (clinic_admins.id)


def receipt(e: dict) -> bool:
    """Stock came INTO the store (the ledger sign); direction alone is coarse (a supplier return is a 'return')."""
    return e["movement_type"] == "opening" or e["movement_type"].endswith("_in")


async def _rpc(fn: str, args: dict):
    try:
        return (await sb(supabase.rpc(fn, args))).data
    except Exception as e:
        raise_for(e)


def _query(clinic_id: str, register: str, frm: Optional[date], to: Optional[date], store_ids: Optional[list[str]],
           item_id: Optional[str]):
    q = scoped_query("rx_register_entries", clinic_id).eq("register", register)
    if frm:
        q = q.gte("entry_date", frm.isoformat())
    if to:
        q = q.lte("entry_date", to.isoformat())
    if store_ids is not None:
        q = q.in_("store_id", store_ids or ["00000000-0000-0000-0000-000000000000"])
    if item_id:
        q = q.eq("item_id", item_id)
    return q.order("seq")


async def _enrich(clinic_id: str, rows: list[dict]) -> list[dict]:
    items = await _by_ids("rx_items", clinic_id, [r["item_id"] for r in rows],
                          "id, item_code, brand_name, schedule, base_unit")
    batches = await _by_ids("rx_batches", clinic_id, [r["batch_id"] for r in rows], "id, batch_no, expiry_date")
    stores = await _by_ids("rx_stores", clinic_id, [r["store_id"] for r in rows], "id, code, name, drug_licence_no")
    out = []
    for r in rows:
        r = {k: v for k, v in r.items() if k not in ("row_hash", "prev_hash")}
        out.append({**r, "item": items.get(str(r["item_id"])), "batch": batches.get(str(r["batch_id"])),
                    "store": stores.get(str(r["store_id"])), "receipt": receipt(r)})
    return out


async def list_entries(clinic_id: str, register: str, frm: Optional[date], to: Optional[date],
                       store_ids: Optional[list[str]], item_id: Optional[str], limit: int, offset: int) -> dict:
    rows = await _rows(_query(clinic_id, register, frm, to, store_ids, item_id).range(offset, offset + limit))
    more = len(rows) > limit
    return {"entries": await _enrich(clinic_id, rows[:limit]), "offset": offset, "limit": limit, "has_more": more}


async def all_entries(clinic_id: str, register: str, frm: Optional[date], to: Optional[date],
                      store_ids: Optional[list[str]], item_id: Optional[str]) -> list[dict]:
    rows: list[dict] = []
    while True:
        page = await _rows(_query(clinic_id, register, frm, to, store_ids, item_id).range(len(rows), len(rows) + PAGE - 1))
        rows.extend(page)
        if len(rows) > EXPORT_CAP:
            raise HTTPException(status_code=422, detail={"code": "rx_register_export_too_large", "message":
                                f"More than {EXPORT_CAP} entries: export a shorter date range."})
        if len(page) < PAGE:
            return await _enrich(clinic_id, rows)


async def verify(clinic_id: str, register: str) -> dict:
    out = await _rpc("rx_register_verify", {"p_clinic_id": clinic_id, "p_register": register})
    row = (out or [{}])[0] if isinstance(out, list) else (out or {})
    return {"ok": bool(row.get("ok")), "rows": int(row.get("verified_rows") or 0), "first_bad_seq": row.get("first_bad_seq"),
            "verified_at": datetime.now(timezone.utc).isoformat()}


# ── statutory formats (W9) ──────────────────────────────────────────────────

def _person(p: Optional[dict]) -> str:
    """Name, age / gender, MRN, address — never a phone (the snapshot has none; any stray key is ignored)."""
    if not p:
        return ""
    bits = [p.get("name") or "", "/".join(str(x) for x in (p.get("age"), (p.get("gender") or "")[:1].upper()) if x),
            f"MRN {p['mrn']}" if p.get("mrn") else "", f"Adm {p['admission']}" if p.get("admission") else "",
            p.get("address") or ""]
    return ", ".join(b for b in bits if b)


def _prescriber(p: Optional[dict]) -> str:
    if not p:
        return ""
    reg = " ".join(x for x in (p.get("registration_number"), f"({p['council']})" if p.get("council") else "") if x)
    return " — ".join(x for x in (p.get("name"), f"Reg. {reg}" if reg else "") if x)


def _supplier(s: Optional[dict]) -> str:
    if not s:
        return ""
    return ", ".join(x for x in (s.get("name"), f"DL {s['licence_no']}" if s.get("licence_no") else "",
                                 f"Inv {s['invoice_no']}" if s.get("invoice_no") else "",
                                 str(s.get("invoice_date") or "")) if x)


def _pharmacist(e: dict) -> str:
    return e["dispensed_by_name"] + (f" (Reg. {e['pharmacist_reg_no']})" if e.get("pharmacist_reg_no") else "")


def table(register: str, rows: list[dict]) -> tuple[tuple, list[tuple]]:
    """(headers, rows) shared by the CSV and the PDF so the two never disagree."""
    drug = lambda e: (e.get("item") or {}).get("brand_name") or ""          # noqa: E731
    batch = lambda e: (e.get("batch") or {}).get("batch_no") or ""         # noqa: E731
    if register == "narcotic":
        headers = ("Seq", "Date", "Doc No.", "Drug Name", "Batch", "Store", "Opening Balance", "Quantity Received",
                   "Quantity Issued", "Balance", "Patient / Supplier", "Prescriber Name & Reg. No.", "Signature")
        out = []
        for e in rows:
            signed = e["qty_units"] if e["receipt"] else -e["qty_units"]
            out.append((e["seq"], str(e["entry_date"]), e.get("doc_ref") or "", drug(e), batch(e),
                        (e.get("store") or {}).get("code") or "", e["balance_after_units"] - signed,
                        e["qty_units"] if e["receipt"] else "", "" if e["receipt"] else e["qty_units"],
                        e["balance_after_units"], _person(e.get("patient_snapshot")) or _supplier(e.get("supplier_snapshot")),
                        _prescriber(e.get("prescriber_snapshot")), _pharmacist(e)))
        return headers, out
    headers = ("Seq", "Date", "Bill / Doc No.", "Entry", "Patient Name & Address", "Prescriber Name & Reg. No.",
               "Drug Name", "Batch", "Qty", "Balance", "Supplier", "Pharmacist")
    return headers, [(e["seq"], str(e["entry_date"]), e.get("doc_ref") or "", ENTRY.get(e["direction"], e["direction"]),
                      _person(e.get("patient_snapshot")), _prescriber(e.get("prescriber_snapshot")), drug(e), batch(e),
                      e["qty_units"], e["balance_after_units"], _supplier(e.get("supplier_snapshot")), _pharmacist(e))
                     for e in rows]


def render_csv(register: str, rows: list[dict]) -> bytes:
    headers, body = table(register, rows)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(body)
    return ("﻿" + buf.getvalue()).encode("utf-8")        # BOM: Excel opens Indian names correctly


def render_pdf(register: str, rows: list[dict], clinic: dict, period: str, store: Optional[dict], chain: dict) -> bytes:
    from app.services.opd_pdf import OPDDocument, _kv_rows, _title

    class _Doc(OPDDocument):
        def footer(self):
            self.set_y(-10)
            self.set_font("NotoSans", "", 7)
            self.set_text_color(110, 110, 110)
            self.cell(0, 4, f"{REGISTER_NAMES[register]} · {period} · page {self.page_no()} of {{nb}}", align="C")
            self.set_text_color(0, 0, 0)

    pdf = _Doc(doc_label=REGISTER_NAMES[register], orientation="L", unit="mm", format="A4")
    pdf.set_margins(10, 10, 10)
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    _title(pdf, REGISTER_NAMES[register].upper())
    _kv_rows(pdf, [("Hospital", clinic.get("name")), ("Period", period),
                   ("Store", f"{store['code']} · {store['name']}" if store else "All stores"),
                   ("Drug licence", (store or {}).get("drug_licence_no") or "—")])
    headers, body = table(register, rows)
    widths = ((10, 18, 24, 34, 18, 16, 16, 16, 16, 14, 46, 32, 17) if register == "narcotic"
              else (10, 18, 24, 18, 52, 40, 34, 18, 12, 14, 0, 37))
    if register != "narcotic":       # the supplier column only matters for receipts: fold it into the patient column
        headers = headers[:10] + headers[11:]
        widths = widths[:10] + widths[11:]
        body = [r[:4] + ((r[4] or r[10]),) + r[5:10] + r[11:] for r in body]
    pdf.set_font("NotoSans", "", 7)
    # wrapping table (never truncates a statutory field); headings repeat on every page
    with pdf.table(width=sum(widths), col_widths=widths, text_align="LEFT", line_height=3.6,
                   repeat_headings=1) as t:
        for r in [headers] + (body or [("—",) + ("",) * (len(headers) - 1)]):
            row = t.row()
            for x in r:
                row.cell(str(x))
    pdf.ln(3)
    pdf.set_font("NotoSans", "B" if not chain["ok"] else "", 8)
    pdf.multi_cell(0, 4.2, (f"Hash chain verified: entries 1–{chain['rows']} intact." if chain["ok"]
                            else f"HASH CHAIN BROKEN at entry {chain['first_bad_seq']} — report to the pharmacy manager.")
                   + f"  Generated {datetime.now(timezone.utc).astimezone().strftime('%d %b %Y %H:%M')}."
                   + "  Pharmacist signature: ____________________", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# ── A6 recall trace, A11 empties ────────────────────────────────────────────

async def trace(clinic_id: str, batch_id: str) -> dict:
    out = await _rpc("rx_batch_trace", {"p_clinic_id": clinic_id, "p_batch_id": batch_id})
    if not out:
        raise HTTPException(status_code=404, detail="Batch not found.")
    out["summary"] = {"patients": len({s.get("patient_id") or s.get("patient_name") for s in out["sales"]}
                                      | {i["admission_id"] for i in out["ipd"]}),
                      "sold_units": sum(s["qty_units"] - s["returned_units"] for s in out["sales"]),
                      "ipd_units": sum(i["qty_units"] - i["returned_units"] for i in out["ipd"]),
                      "on_hand_units": sum(h["qty_units"] for h in out["on_hand"])}
    return out


async def record_empties(clinic_id: str, dispense_line_id: str, body: EmptiesIn, received_by: Optional[str]) -> dict:
    if not received_by:
        raise HTTPException(status_code=403, detail="Receiving empties needs a named staff login.")
    if not await _rows(scoped_query("clinic_admins", clinic_id, "id").eq("id", str(body.returned_by)).eq("is_active", True).limit(1)):
        raise HTTPException(status_code=404, detail="The returning staff member was not found in this clinic.")
    return await _rpc("rx_record_empties", {"p_clinic_id": clinic_id, "p_dispense_line_id": dispense_line_id,
                                            "p_units": body.units, "p_returned_by": str(body.returned_by),
                                            "p_received_by": received_by})
