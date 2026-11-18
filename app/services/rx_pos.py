"""Kriya Pharmacy OS — counter sales (migration 138; RX-29, RX-31…RX-35, RX-69, RX-70, RX-73, RX-77, RX-81, RX-82).

Lookup a signed OPD prescription, draft a sale with deterministically mapped lines and FEFO
batches, edit lines, complete (rx_complete_sale), return / credit note (rx_record_return),
same-day void, park / recall, Rx Inbox, manager overrides, day summary (Z-report) and the
medicine-history panel. Prices, taxes, caps and schedule rules are enforced in SQL; nothing
here can sell stock on its own (ADR-RX-01). Zero-LLM: mapping is alias / barcode / code /
substring only and a substitute is only another item of the same generic (ADR-RX-06/07).
Design: docs/pharmacy-os/WORKFLOWS.md W2 / W3 / W5a, API.md §5, ADVANCED-FEATURES A1/A2/A5/A9/A13/A14.
"""

import re
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database import sb, scoped_query, supabase
from app.services.gst_billing import valid_gstin
from app.services.rx import _rows, raise_for
from app.services.rx_catalogue import resolve
from app.services.rx_stock import get_fefo_batches
from app.services.rx_tax import prescribed_units
from app.utils.helpers import IST, today_ist

SCHEDULED = ("H", "H1", "X", "narcotic", "psychotropic")
CONTROLLED = ("X", "narcotic", "psychotropic")
PayMode = Literal["cash", "upi", "card", "wallet"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExternalRx(_In):
    prescriber_name: str = Field(..., min_length=3, max_length=120)
    registration_number: str = Field(..., min_length=3, max_length=60)
    council: Optional[str] = Field(None, max_length=60)
    rx_date: date
    patient_name: Optional[str] = Field(None, min_length=2, max_length=120)


class SaleIn(_In):
    store_id: str
    sale_kind: Literal["opd_rx", "otc", "staff"] = "otc"
    opd_prescription_id: Optional[str] = None
    patient_id: Optional[str] = None
    walkin_name: Optional[str] = Field(None, min_length=2, max_length=120)
    external_rx: Optional[ExternalRx] = None
    repeat_of_sale_id: Optional[str] = None       # A5: same items, fresh FEFO batches, today's prices
    pick: bool = False                            # A1: picked from the Rx Inbox


class LineIn(_In):
    item_id: str
    batch_id: Optional[str] = None                # absent → FEFO (may split over batches)
    qty_units: int = Field(..., ge=1, le=100_000)
    opd_prescription_item_id: Optional[str] = None
    substituted_from_item_id: Optional[str] = None
    substitution_reason: Optional[str] = Field(None, min_length=3, max_length=120)
    fefo_override_reason: Optional[str] = Field(None, min_length=3, max_length=120)


class SalePatch(_In):
    discount_paise: Optional[int] = Field(None, ge=0, le=100_000_000)
    buyer_gstin: Optional[str] = Field(None, max_length=15)
    walkin_name: Optional[str] = Field(None, min_length=2, max_length=120)

    @field_validator("buyer_gstin")
    @classmethod
    def _gstin(cls, v):
        if v and not valid_gstin(v.upper()):
            raise ValueError("GSTIN is not valid (format or check digit)")
        return v.upper() if v else None


class PaymentIn(_In):
    mode: PayMode
    amount_paise: int = Field(..., ge=1, le=10_000_000_000)
    reference: Optional[str] = Field(None, max_length=60)


class CompleteIn(_In):
    payments: list[PaymentIn] = Field(default_factory=list, max_length=6)
    shift_id: Optional[str] = None
    idempotency_key: str = Field(..., min_length=8, max_length=80)
    override_id: Optional[str] = None
    send_whatsapp_bill: bool = False              # shortcut: e-bill template lands with the WhatsApp templates (P8)
    refill_opt_in: bool = False                   # pharmacist ticked "Patient wants refill reminders" (W10, X16)


class ReturnLineIn(_In):
    sale_item_id: str
    qty_units: int = Field(..., ge=1, le=100_000)
    restock: bool


class ReturnIn(_In):
    lines: list[ReturnLineIn] = Field(..., min_length=1, max_length=200)
    reason: Literal["unopened_unused", "wrong_item", "adverse_reaction_doctor_advised", "therapy_changed", "other"]
    refund_mode: Literal["cash", "upi", "card", "credit_note"]
    shift_id: Optional[str] = None
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=80)
    override_id: Optional[str] = None


class VoidIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)
    shift_id: Optional[str] = None


class ParkIn(_In):
    label: str = Field(..., min_length=1, max_length=60)


class OverrideIn(_In):
    action: Literal["discount_over_cap", "return_past_window", "restock_opened", "fefo_override", "rx_over_fulfil",
                    "staff_sale"]
    sale_id: Optional[str] = None
    return_id: Optional[str] = None
    amount_paise: Optional[int] = Field(None, ge=0, le=10_000_000_000)
    reason: str = Field(..., min_length=3, max_length=300)
    approver_username: str = Field(..., min_length=1, max_length=120)
    approver_password: str = Field(..., min_length=1, max_length=200)


# ── helpers ─────────────────────────────────────────────────────────────────

async def _rpc(fn: str, args: dict):
    try:
        return (await sb(supabase.rpc(fn, args))).data
    except Exception as e:
        raise_for(e)


async def _exec(query) -> list[dict]:
    try:
        return list((await sb(query)).data or [])
    except Exception as e:
        raise_for(e)


async def _by_ids(table: str, clinic_id: str, ids, fields: str = "*", col: str = "id") -> dict[str, dict]:
    ids = sorted({str(i) for i in ids if i})
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 200):
        for r in await _rows(scoped_query(table, clinic_id, fields).in_(col, ids[i:i + 200])):
            out[str(r[col])] = r
    return out


def _uuid(v: str) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except ValueError:
        return False


def mask_phone(phone: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", phone or "")
    return f"+91XXXXXX{digits[-4:]}" if len(digits) >= 4 else None


async def get_sale_row(clinic_id: str, sale_id: str) -> dict:
    rows = await _rows(scoped_query("rx_sales", clinic_id).eq("id", sale_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Sale not found.")
    return rows[0]


def _draft(sale: dict) -> None:
    if sale["status"] != "draft":
        raise HTTPException(status_code=409, detail={"code": "rx_document_locked",
                                                     "message": "This bill is no longer a draft."})


# ── prescriptions: lookup and fulfilment state (W2 steps 1–3, A14) ──────────

async def _fulfilled(clinic_id: str, rx_item_ids: list[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for i in range(0, len(rx_item_ids), 200):
        for f in await _rows(scoped_query("rx_rx_fulfilments", clinic_id, "opd_prescription_item_id, units")
                             .in_("opd_prescription_item_id", rx_item_ids[i:i + 200])):
            out[str(f["opd_prescription_item_id"])] += int(f["units"])
    return out


async def prescriptions_view(clinic_id: str, rxs: list[dict]) -> list[dict]:
    """Signed prescriptions with each line's prescribed / fulfilled / pending units."""
    if not rxs:
        return []
    items = await _rows(scoped_query("opd_prescription_items", clinic_id)
                        .in_("prescription_id", [r["id"] for r in rxs]).order("line_no"))
    done = await _fulfilled(clinic_id, [str(i["id"]) for i in items])
    by_rx: dict[str, list] = defaultdict(list)
    for i in items:
        want = prescribed_units(i.get("dosage"), i.get("frequency"), i.get("duration_days"), i.get("formulation"))
        got = done.get(str(i["id"]), 0)
        by_rx[str(i["prescription_id"])].append({
            "id": i["id"], "line_no": i["line_no"], "drug_name": i["drug_name"], "strength": i.get("strength"),
            "formulation": i["formulation"], "dosage": i["dosage"], "frequency": i["frequency"], "timing": i.get("timing"),
            "duration_days": i.get("duration_days"), "instructions": i.get("instructions"),
            "prescribed_units": want, "fulfilled_units": got,
            "pending": (want - got) if want is not None else (0 if got else None)})
    out = []
    for r in rxs:
        lines = by_rx.get(str(r["id"]), [])
        state = "billed" if lines and all(ln["fulfilled_units"] and (ln["pending"] or 0) <= 0 for ln in lines) \
            else "partial" if any(ln["fulfilled_units"] for ln in lines) else "new"
        out.append({"id": r["id"], "patient_id": r["patient_id"], "family_member_id": r.get("family_member_id"),
                    "signed_at": r["signed_at"], "doctor": (r.get("signer_snapshot") or {}).get("name"),
                    "registration_number": (r.get("signer_snapshot") or {}).get("registration_number"),
                    "patient": r.get("patient_snapshot") or {}, "allergy_review": r.get("allergy_review") or [],
                    "state": state, "lines": lines})
    return out


async def _signed_rx_for_patients(clinic_id: str, patient_ids: list[str], validity_days: int) -> list[dict]:
    if not patient_ids:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=validity_days)).isoformat()
    return await _rows(scoped_query("opd_prescriptions", clinic_id).in_("patient_id", patient_ids[:50])
                       .eq("status", "signed").gte("signed_at", since).order("signed_at", desc=True).limit(20))


async def patient_panel(clinic_id: str, patient_id: str) -> dict:
    """A14: last 10 pharmacy purchases + allergies exactly as clinicians recorded them. No checking."""
    pats = await _rows(scoped_query("patients", clinic_id, "id, name, mrn, gender, age_years, allergies, "
                                    "allergies_status, language").eq("id", patient_id).limit(1))
    if not pats:
        return {}
    p = pats[0]
    sales = await _rows(scoped_query("rx_sales", clinic_id, "id, sale_number, completed_at, net_paise")
                        .eq("patient_id", patient_id).eq("status", "completed").order("completed_at", desc=True).limit(10))
    lines = await _rows(scoped_query("rx_sale_items", clinic_id, "sale_id, item_id, qty_units, schedule")
                        .in_("sale_id", [s["id"] for s in sales])) if sales else []
    items = await _by_ids("rx_items", clinic_id, [ln["item_id"] for ln in lines], "id, brand_name, item_code")
    by_sale: dict[str, list] = defaultdict(list)
    for ln in lines:
        by_sale[str(ln["sale_id"])].append({"brand_name": (items.get(str(ln["item_id"])) or {}).get("brand_name"),
                                            "qty_units": ln["qty_units"], "schedule": ln["schedule"]})
    latest_rx = await _rows(scoped_query("opd_prescriptions", clinic_id, "signed_at, allergy_review")
                            .eq("patient_id", patient_id).eq("status", "signed").order("signed_at", desc=True).limit(1))
    allergies = [{"text": a, "source": "patient record"} for a in (p.get("allergies") or [])]
    for a in (latest_rx[0].get("allergy_review") or []) if latest_rx else []:
        allergies.append({"text": f"{a.get('allergen') or ''} — {a.get('drug') or ''}".strip(" —"),
                          "source": f"prescription review {str(latest_rx[0]['signed_at'])[:10]}"})
    return {"patient": {k: p.get(k) for k in ("id", "name", "mrn", "gender", "age_years", "language")},
            "allergies": allergies, "allergies_status": p.get("allergies_status"),
            "note": "Shown as recorded by clinicians. Kriya does not check interactions or allergies.",
            "history": [{**s, "items": by_sale.get(str(s["id"]), [])} for s in sales]}


async def lookup(clinic_id: str, q: str, settings: dict) -> dict:
    """q = token (today) / MRN / phone / printed-Rx QR (prescription id) / bill number."""
    q = q.strip()
    validity = int(settings.get("rx_validity_days", 30))
    if re.fullmatch(r"PHS-\d{4}-\d{1,8}", q.upper()):
        sales = await _rows(scoped_query("rx_sales", clinic_id).eq("sale_number", q.upper()).limit(1))
        return {"prescriptions": [], "patients": [], "sales": sales}
    rxs: list[dict] = []
    patient_ids: list[str] = []
    if _uuid(q):
        rxs = await _rows(scoped_query("opd_prescriptions", clinic_id).eq("id", q).eq("status", "signed").limit(1))
        if not rxs:
            sales = await _rows(scoped_query("rx_sales", clinic_id).eq("id", q).limit(1))
            return {"prescriptions": [], "patients": [], "sales": sales}
    elif re.fullmatch(r"\d{1,4}", q):
        appts = await _rows(scoped_query("appointments", clinic_id, "patient_id").eq("token_number", int(q))
                            .eq("appointment_date", str(today_ist())).limit(10))
        patient_ids = [str(a["patient_id"]) for a in appts if a.get("patient_id")]
    else:
        digits = re.sub(r"\D", "", q)
        pats = await _rows(scoped_query("patients", clinic_id, "id").eq("mrn", q).limit(5))
        if not pats and len(digits) >= 10:
            pats = await _rows(scoped_query("patients", clinic_id, "id").in_("phone", [f"+91{digits[-10:]}", digits[-10:]])
                               .limit(10))
        patient_ids = [str(p["id"]) for p in pats]
    if not rxs:
        rxs = await _signed_rx_for_patients(clinic_id, patient_ids, validity)
    view = await prescriptions_view(clinic_id, rxs)
    pids = sorted({str(r["patient_id"]) for r in view} | set(patient_ids))
    return {"prescriptions": view, "patients": [await patient_panel(clinic_id, p) for p in pids[:5]], "sales": []}


async def inbox(clinic_id: str, store: dict, since: Optional[str] = None) -> list[dict]:
    """A1: prescriptions signed today (IST) at the store's branch (all branches for a clinic-wide store)."""
    start = datetime.combine(today_ist(), datetime.min.time(), tzinfo=IST).isoformat()
    q = scoped_query("opd_prescriptions", clinic_id).eq("status", "signed").gte("signed_at", since or start)
    rxs = await _rows(q.order("signed_at", desc=True).limit(100))
    if store.get("branch_id"):
        appts = await _by_ids("appointments", clinic_id, [r["appointment_id"] for r in rxs], "id, branch_id")
        rxs = [r for r in rxs if str((appts.get(str(r["appointment_id"])) or {}).get("branch_id")) == str(store["branch_id"])]
    view = await prescriptions_view(clinic_id, rxs)
    drafts = await _rows(scoped_query("rx_sales", clinic_id, "id, opd_prescription_id, picked_at")
                         .eq("store_id", store["id"]).eq("status", "draft").in_("opd_prescription_id", [r["id"] for r in rxs])) \
        if rxs else []
    picked = {str(d["opd_prescription_id"]): d for d in drafts}
    for r in view:
        d = picked.get(str(r["id"]))
        if d and r["state"] == "new":
            r["state"] = "picked"
        r["draft_sale_id"] = d["id"] if d else None
    return view


# ── drafts (W2 step 2, W3, A1, A5) ──────────────────────────────────────────

async def _store_stock_lines(clinic_id: str, store_id: str, item: dict, units: int, rx_item_id: Optional[str] = None,
                             extra: Optional[dict] = None) -> tuple[list[dict], int]:
    """FEFO split of `units` of an item into line rows; returns (rows, shortfall)."""
    fefo = await get_fefo_batches(clinic_id, store_id, item["id"], units)
    rows = [{"item_id": item["id"], "batch_id": p["batch_id"], "qty_units": p["take_units"],
             "opd_prescription_item_id": rx_item_id, **(extra or {})} for p in fefo["picks"] if p["take_units"] > 0]
    return rows, fefo["shortfall_units"]


def _rx_units(rx_line: dict, item: dict) -> int:
    """W2 step 2: from the closed frequency map; whole packs only when the item cannot be sold loose;
    otherwise (SOS, liquids, free text) one pack, for the pharmacist to adjust."""
    pack = int(item["pack_size"])
    units = prescribed_units(rx_line.get("dosage"), rx_line.get("frequency"), rx_line.get("duration_days"),
                             rx_line.get("formulation")) or pack
    if not item.get("allow_loose", True) and units % pack:
        units += pack - units % pack
    return units


async def map_rx_line(clinic_id: str, rx_line: dict) -> dict:
    """ADR-RX-07: candidates for an Rx line (alias → barcode → code → brand → generic). Auto-mapped only when
    the best candidate is a confirmed alias or an exact brand / generic match and clearly ahead of the next."""
    text = " ".join(x for x in (rx_line["drug_name"], rx_line.get("strength") or "") if x).strip()
    cands = await resolve(clinic_id, text, 5)
    if not cands and rx_line.get("strength"):
        cands = await resolve(clinic_id, rx_line["drug_name"], 5)
    top = cands[0] if cands else None
    sure = bool(top) and (top["source"] == "alias" or top["score"] >= 85 or (top["score"] >= 70 and len(cands) == 1)) \
        and (len(cands) == 1 or cands[1]["score"] < top["score"])
    return {"rx_item_id": rx_line["id"], "text": text, "candidates": cands, "mapped_item_id": top["item_id"] if sure else None}


async def _insert_lines(clinic_id: str, sale_id: str, rows: list[dict]) -> None:
    if rows:
        # unscoped: insert_scoped_by_payload
        await _exec(supabase.table("rx_sale_items").insert(
            [{**r, "clinic_id": clinic_id, "sale_id": sale_id, "line_no": n} for n, r in enumerate(rows, start=1)]))


async def create_sale(clinic_id: str, store: dict, body: SaleIn, actor: tuple[Optional[str], str], settings: dict) -> dict:
    head = {"clinic_id": clinic_id, "store_id": store["id"], "branch_id": store.get("branch_id"),
            "sale_kind": body.sale_kind, "created_by": actor[0], "created_by_name": actor[1]}
    rx = None
    if body.opd_prescription_id:
        rows = await _rows(scoped_query("opd_prescriptions", clinic_id).eq("id", body.opd_prescription_id).limit(1))
        if not rows:
            raise HTTPException(status_code=404, detail="Prescription not found.")
        rx = rows[0]
        if rx["status"] != "signed":
            raise HTTPException(status_code=409, detail={"code": "rx_prescription_superseded",
                                                         "message": "This prescription is no longer the signed version."})
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(rx["signed_at"]).replace("Z", "+00:00"))).days
        if age > int(settings.get("rx_validity_days", 30)):
            raise HTTPException(status_code=409, detail={"code": "rx_prescription_expired",
                                                         "message": "This prescription is older than its validity."})
        existing = await _rows(scoped_query("rx_sales", clinic_id).eq("opd_prescription_id", rx["id"])
                               .eq("store_id", store["id"]).eq("status", "draft").limit(1))
        if existing:                                    # a second counter gets the same draft (W2 step 2)
            return {"sale_id": existing[0]["id"], "existing": True, "mapping": []}
        snap = dict(rx.get("signer_snapshot") or {})
        snap["patient_name"] = (rx.get("patient_snapshot") or {}).get("patient_name") or \
            (rx.get("patient_snapshot") or {}).get("name")
        head.update(sale_kind="opd_rx", opd_prescription_id=rx["id"], patient_id=rx["patient_id"],
                    family_member_id=rx.get("family_member_id"), prescriber_snapshot=snap)
    else:
        if body.sale_kind == "opd_rx":
            raise HTTPException(status_code=422, detail="An OPD prescription sale needs its prescription.")
        if body.patient_id:
            if not await _rows(scoped_query("patients", clinic_id, "id").eq("id", body.patient_id).limit(1)):
                raise HTTPException(status_code=404, detail="Patient not found.")
            head["patient_id"] = body.patient_id
        if body.walkin_name:
            head["walkin_name"] = body.walkin_name
        if body.external_rx:
            e = body.external_rx
            if (today_ist() - e.rx_date).days > int(settings.get("rx_validity_days", 30)) or e.rx_date > today_ist():
                raise HTTPException(status_code=422, detail="The outside prescription's date is not within its validity.")
            head["prescriber_snapshot"] = {"external": True, "name": e.prescriber_name,
                                           "registration_number": e.registration_number, "council": e.council,
                                           "rx_date": str(e.rx_date), "patient_name": e.patient_name or body.walkin_name}
            head["external_rx_ref"] = f"{e.prescriber_name} · {e.registration_number}"[:120]
    if body.pick:
        head.update(picked_at=datetime.now(timezone.utc).isoformat(), picked_by=actor[0])
    try:
        # unscoped: insert_scoped_by_payload
        sale = (await sb(supabase.table("rx_sales").insert(head))).data[0]
    except Exception as e:
        if "uq_rx_sales_one_live_per_rx" in str(e):      # lost the race to another counter: use its draft
            existing = await _rows(scoped_query("rx_sales", clinic_id).eq("opd_prescription_id", head["opd_prescription_id"])
                                   .eq("store_id", store["id"]).eq("status", "draft").limit(1))
            return {"sale_id": existing[0]["id"], "existing": True, "mapping": []}
        raise_for(e)
    rows, mapping = [], []
    if rx:
        view = (await prescriptions_view(clinic_id, [rx]))[0]
        items: dict[str, dict] = {}
        for ln in view["lines"]:
            if ln["pending"] is not None and ln["pending"] <= 0:
                continue
            m = await map_rx_line(clinic_id, ln)
            if m["mapped_item_id"]:
                items.update(await _by_ids("rx_items", clinic_id, [m["mapped_item_id"]]))
                item = items[str(m["mapped_item_id"])]
                want = ln["pending"] if ln["pending"] else _rx_units(ln, item)
                got, short = await _store_stock_lines(clinic_id, store["id"], item, want, ln["id"])
                rows += got
                m.update(units=want, shortfall=short)
            mapping.append(m)
    elif body.repeat_of_sale_id:
        rows, mapping = await _repeat_rows(clinic_id, store, body.repeat_of_sale_id, sale)
    try:
        await _insert_lines(clinic_id, sale["id"], rows)
    except HTTPException:
        await _exec(supabase.table("rx_sales").delete().eq("clinic_id", clinic_id).eq("id", sale["id"]))
        raise
    return {"sale_id": sale["id"], "existing": False, "mapping": mapping}


async def _repeat_rows(clinic_id: str, store: dict, prev_id: str, sale: dict) -> tuple[list[dict], list[dict]]:
    """A5: the previous bill's items at today's FEFO and prices; scheduled items only with a valid Rx."""
    prev = await get_sale_row(clinic_id, prev_id)
    if prev["status"] != "completed":
        raise HTTPException(status_code=409, detail="Only a completed bill can be repeated.")
    lines = await _rows(scoped_query("rx_sale_items", clinic_id).eq("sale_id", prev_id).order("line_no"))
    items = await _by_ids("rx_items", clinic_id, [ln["item_id"] for ln in lines])
    rows, notes = [], []
    qty: dict[str, int] = defaultdict(int)
    for ln in lines:
        qty[str(ln["item_id"])] += int(ln["qty_units"]) - int(ln["returned_units"])
    for item_id, units in qty.items():
        item = items[item_id]
        if units <= 0:
            continue
        if item["schedule"] in SCHEDULED:
            notes.append({"item_id": item_id, "brand_name": item["brand_name"], "status": "needs_rx"})
            continue
        got, short = await _store_stock_lines(clinic_id, store["id"], item, units)
        rows += got
        notes.append({"item_id": item_id, "brand_name": item["brand_name"], "status": "added", "shortfall": short})
    if prev.get("patient_id") and not sale.get("patient_id"):
        await _exec(supabase.table("rx_sales").update({"patient_id": prev["patient_id"]})
                    .eq("clinic_id", clinic_id).eq("id", sale["id"]))
    return rows, notes


async def replace_lines(clinic_id: str, sale: dict, lines: list[LineIn], settings: dict) -> None:
    """PUT /lines: FEFO when no batch is named; a non-FEFO batch needs a reason; a substitute must share the
    prescribed item's generic (ADR-RX-06)."""
    _draft(sale)
    item_ids = {ln.item_id for ln in lines} | {ln.substituted_from_item_id for ln in lines if ln.substituted_from_item_id}
    items = await _by_ids("rx_items", clinic_id, item_ids)
    if set(map(str, item_ids)) - set(items):
        raise HTTPException(status_code=404, detail="Item not found.")
    rows: list[dict] = []
    for ln in lines:
        item = items[ln.item_id]
        if ln.substituted_from_item_id:
            orig = items[ln.substituted_from_item_id]
            if not item.get("generic_id") or item["generic_id"] != orig.get("generic_id"):
                raise HTTPException(status_code=422, detail={"code": "rx_substitute_not_same_generic", "message":
                                    "A substitute must be the same generic (molecule, strength and form)."})
            if not ln.substitution_reason:
                raise HTTPException(status_code=422, detail="A substitution needs a reason.")
        extra = {k: v for k, v in ln.model_dump(include={"substituted_from_item_id", "substitution_reason",
                                                          "fefo_override_reason"}).items() if v}
        if ln.batch_id:
            fefo = await get_fefo_batches(clinic_id, sale["store_id"], ln.item_id, 1)
            first = fefo["picks"][0]["batch_id"] if fefo["picks"] else None
            if first and str(first) != ln.batch_id and settings.get("fefo_override_requires_reason", True) \
                    and not ln.fefo_override_reason:
                raise HTTPException(status_code=422, detail={"code": "rx_fefo_reason_required", "message":
                                    "Picking a later-expiring batch needs a reason."})
            rows.append({"item_id": ln.item_id, "batch_id": ln.batch_id, "qty_units": ln.qty_units,
                         "opd_prescription_item_id": ln.opd_prescription_item_id, **extra})
        else:
            got, short = await _store_stock_lines(clinic_id, sale["store_id"], item, ln.qty_units,
                                                  ln.opd_prescription_item_id, extra)
            if short:
                raise HTTPException(status_code=409, detail={"code": "rx_insufficient_stock", "message":
                                    f"Only {ln.qty_units - short} units of {item['brand_name']} are in stock here."})
            rows += got
    if len(rows) > 200:
        raise HTTPException(status_code=422, detail="A bill has at most 200 lines.")
    await _exec(supabase.table("rx_sale_items").delete().eq("clinic_id", clinic_id).eq("sale_id", sale["id"]))
    await _insert_lines(clinic_id, sale["id"], rows)


async def patch_sale(clinic_id: str, sale: dict, body: SalePatch, max_bps: int, is_admin: bool) -> None:
    _draft(sale)
    patch = body.model_dump(exclude_unset=True)
    if "discount_paise" in patch and not is_admin and \
            patch["discount_paise"] * 10000 > int(sale["gross_paise"]) * max_bps:
        # the counter may still enter it; completion then needs a manager override (A9)
        patch["_needs_override"] = True
    needs = patch.pop("_needs_override", False)
    if patch:
        await _exec(supabase.table("rx_sales").update(patch).eq("clinic_id", clinic_id).eq("id", sale["id"]))
    return needs


async def sale_detail(clinic_id: str, sale_id: str, settings: dict) -> dict:
    sale = await get_sale_row(clinic_id, sale_id)
    lines = await _rows(scoped_query("rx_sale_items", clinic_id).eq("sale_id", sale_id).order("line_no"))
    items = await _by_ids("rx_items", clinic_id, [ln["item_id"] for ln in lines] +
                          [ln["substituted_from_item_id"] for ln in lines],
                          "id, item_code, brand_name, generic_id, pack_size, pack_label, base_unit, schedule, rack_location, is_chronic")
    batches = await _by_ids("rx_batches", clinic_id, [ln["batch_id"] for ln in lines],
                            "id, batch_no, expiry_date, is_blocked")
    today, min_shelf = today_ist(), int(settings.get("min_shelf_days_at_sale", 30))
    warnings = []
    for ln in lines:
        ln["item"] = items.get(str(ln["item_id"]))
        ln["batch"] = batches.get(str(ln["batch_id"]))
        ln["substituted_from"] = items.get(str(ln.get("substituted_from_item_id")))
        exp = date.fromisoformat(str((ln["batch"] or {}).get("expiry_date") or "9999-12-31"))
        if (exp - today).days < min_shelf:
            warnings.append({"line_no": ln["line_no"], "code": "short_shelf",
                             "message": f"Expires {exp:%m/%Y}: less than {min_shelf} days of shelf life."})
        if ln["schedule"] in SCHEDULED and not (sale.get("prescriber_snapshot") or {}).get("registration_number"):
            warnings.append({"line_no": ln["line_no"], "code": "needs_rx",
                             "message": f"Schedule {ln['schedule']}: needs a prescription with the prescriber's "
                                        "registration number."})
        if ln["schedule"] in CONTROLLED:
            warnings.append({"line_no": ln["line_no"], "code": "controlled",
                             "message": "Controlled drug: needs RX_CONTROLLED and an in-house or captured prescription."})
    sale["lines"] = lines
    sale["warnings"] = warnings
    sale["payments"] = await _rows(scoped_query("rx_sale_payments", clinic_id).eq("sale_id", sale_id).order("created_at"))
    sale["returns"] = await _rows(scoped_query("rx_returns", clinic_id).eq("sale_id", sale_id).order("created_at"))
    sale["credit_notes"] = await _rows(scoped_query("rx_credit_notes", clinic_id).eq("sale_id", sale_id))
    if sale.get("opd_prescription_id"):
        rx = await _rows(scoped_query("opd_prescriptions", clinic_id).eq("id", sale["opd_prescription_id"]).limit(1))
        sale["prescription"] = (await prescriptions_view(clinic_id, rx))[0] if rx else None
    if sale.get("patient_id"):
        sale["patient_panel"] = await patient_panel(clinic_id, sale["patient_id"])
    sale.pop("external_rx_fingerprint", None)
    return sale


async def delete_draft(clinic_id: str, sale: dict) -> None:
    _draft(sale)
    await _exec(supabase.table("rx_sales").delete().eq("clinic_id", clinic_id).eq("id", sale["id"]).eq("status", "draft"))


async def park(clinic_id: str, sale: dict, label: Optional[str]) -> dict:
    _draft(sale)
    patch = {"parked_label": label, "parked_at": datetime.now(timezone.utc).isoformat() if label else None}
    return (await _exec(supabase.table("rx_sales").update(patch).eq("clinic_id", clinic_id).eq("id", sale["id"])))[0]


async def list_sales(clinic_id: str, store_ids: Optional[list[str]], status: Optional[str], day_from: Optional[date],
                     day_to: Optional[date], q: Optional[str], parked: Optional[bool], limit: int) -> list[dict]:
    query = scoped_query("rx_sales", clinic_id, "id, sale_number, status, sale_kind, store_id, patient_id, walkin_name, "
                         "net_paise, gross_paise, parked_label, parked_at, picked_at, created_at, completed_at, "
                         "refunded_paise, credited_paise, opd_prescription_id")
    if store_ids is not None:
        query = query.in_("store_id", store_ids)
    if status:
        query = query.eq("status", status)
    if parked is not None:
        query = query.not_.is_("parked_at", "null") if parked else query.is_("parked_at", "null")
    col = "created_at" if status == "draft" else "completed_at"
    if day_from:
        query = query.gte(col, datetime.combine(day_from, datetime.min.time(), tzinfo=IST).isoformat())
    if day_to:
        query = query.lt(col, datetime.combine(day_to + timedelta(days=1), datetime.min.time(), tzinfo=IST).isoformat())
    if q:
        query = query.eq("sale_number", q.strip().upper())
    rows = await _rows(query.order("created_at", desc=True).limit(limit))
    pats = await _by_ids("patients", clinic_id, [r["patient_id"] for r in rows], "id, name, mrn")
    now = datetime.now(timezone.utc)
    for r in rows:
        r["patient"] = pats.get(str(r.get("patient_id")))
        r["stale"] = r["status"] == "draft" and \
            (now - datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))) > timedelta(hours=24)
    return rows


# ── money: complete, return, void ───────────────────────────────────────────

async def complete(clinic_id: str, sale: dict, body: CompleteIn, shift_id: Optional[str], actor: tuple, is_admin: bool,
                   can_controlled: bool) -> dict:
    return await _rpc("rx_complete_sale", {
        "p_clinic_id": clinic_id, "p_sale_id": sale["id"], "p_payments": [p.model_dump() for p in body.payments],
        "p_shift_id": shift_id, "p_idem": body.idempotency_key, "p_actor_id": actor[0], "p_actor_name": actor[1],
        "p_override_id": body.override_id, "p_is_admin": is_admin, "p_can_controlled": can_controlled})


async def record_return(clinic_id: str, sale: dict, body: ReturnIn, shift_id: Optional[str], actor: tuple,
                        is_admin: bool) -> dict:
    return await _rpc("rx_record_return", {
        "p_clinic_id": clinic_id, "p_sale_id": sale["id"], "p_lines": [ln.model_dump() for ln in body.lines],
        "p_reason": body.reason, "p_refund_mode": body.refund_mode, "p_shift_id": shift_id,
        "p_idem": body.idempotency_key, "p_actor_id": actor[0], "p_actor_name": actor[1], "p_is_admin": is_admin,
        "p_override_id": body.override_id})


async def void(clinic_id: str, sale: dict, reason: str, shift_id: Optional[str], actor: tuple) -> dict:
    return await _rpc("rx_void_sale", {"p_clinic_id": clinic_id, "p_sale_id": sale["id"], "p_reason": reason,
                                       "p_shift_id": shift_id, "p_actor_id": actor[0], "p_actor_name": actor[1]})


async def has_cash(clinic_id: str, sale_id: str) -> bool:
    return any(p["mode"] == "cash" for p in await _rows(scoped_query("rx_sale_payments", clinic_id, "mode")
                                                         .eq("sale_id", sale_id)))


# ── overrides (A9) ──────────────────────────────────────────────────────────

_OVERRIDE_CALLS: dict[str, deque] = defaultdict(deque)


def override_rate_ok(requester_id: str, limit: int = 5, window_s: int = 60) -> bool:
    """5 requests a minute per requester.
    shortcut: per-process memory; move to the rate_limits table if the API runs several workers."""
    now, calls = time.monotonic(), _OVERRIDE_CALLS[requester_id]
    while calls and now - calls[0] > window_s:
        calls.popleft()
    if len(calls) >= limit:
        return False
    calls.append(now)
    return True


async def grant_override(clinic_id: str, body: OverrideIn, requester_id: str, approver) -> dict:
    row = {"clinic_id": clinic_id, "action": body.action, "sale_id": body.sale_id, "return_id": body.return_id,
           "amount_paise": body.amount_paise, "reason": body.reason, "approver_admin_id": str(approver.user_id),
           "approver_name": approver.username, "requester_admin_id": requester_id}
    # unscoped: insert_scoped_by_payload
    out = (await _exec(supabase.table("rx_overrides").insert(row)))[0]
    return {"override_id": out["id"], "expires_at": out["expires_at"], "action": out["action"],
            "approver_name": out["approver_name"]}


# ── day summary / Z-report (A13) and the OPD collections keys (RX-34) ───────

async def day_summary(clinic_id: str, store_ids: Optional[list[str]], day: date, shift_id: Optional[str]) -> dict:
    start = datetime.combine(day, datetime.min.time(), tzinfo=IST).isoformat()
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=IST).isoformat()

    def scoped(table, fields="*"):
        q = scoped_query(table, clinic_id, fields)
        return q.in_("store_id", store_ids) if store_ids is not None else q
    sales = await _rows(scoped("rx_sales").in_("status", ["completed", "void"]).gte("completed_at", start)
                        .lt("completed_at", end).limit(5000))
    done = [s for s in sales if s["status"] == "completed"]
    done_ids = {str(s["id"]) for s in done}
    lines = []
    ids = sorted(done_ids)
    for i in range(0, len(ids), 200):
        lines += await _rows(scoped_query("rx_sale_items", clinic_id).in_("sale_id", ids[i:i + 200]))
    # money that moved today at these stores, refunds included (negative rows)
    pays = await _rows(scoped_query("rx_sale_payments", clinic_id).gte("created_at", start).lt("created_at", end).limit(10000))
    if store_ids is not None and pays:
        owner = await _by_ids("rx_sales", clinic_id, [p["sale_id"] for p in pays], "id, store_id")
        pays = [p for p in pays if str((owner.get(str(p["sale_id"])) or {}).get("store_id")) in set(map(str, store_ids))]
    returns = await _rows(scoped("rx_returns").gte("created_at", start).lt("created_at", end).limit(5000))
    by_mode: dict[str, int] = defaultdict(int)
    for p in pays:
        by_mode[p["mode"]] += int(p["amount_paise"])
    slabs: dict[str, dict] = {}
    sched: dict[str, dict] = {}
    for ln in lines:
        k = f"{int(ln['tax_rate_bps']) / 100:g}%"
        sl = slabs.setdefault(k, {"taxable_paise": 0, "cgst_paise": 0, "sgst_paise": 0})
        sl["taxable_paise"] += int(ln["taxable_paise"])
        sl["cgst_paise"] += int(ln["cgst_paise"])
        sl["sgst_paise"] += int(ln["sgst_paise"])
        if ln["schedule"] != "none":
            sc = sched.setdefault(ln["schedule"], {"lines": 0, "units": 0})
            sc["lines"] += 1
            sc["units"] += int(ln["qty_units"])
    overrides = await _rows(scoped_query("rx_overrides", clinic_id, "action, approver_name, amount_paise, consumed_at")
                            .gte("consumed_at", start).lt("consumed_at", end))
    out = {"date": str(day), "sales_count": len(done), "gross_paise": sum(int(s["gross_paise"]) for s in done),
           "discount_paise": sum(int(s["discount_paise"]) for s in done),
           "net_paise": sum(int(s["net_paise"]) for s in done), "voids": sum(1 for s in sales if s["status"] == "void"),
           "returns_count": len(returns), "returns_paise": sum(int(r["amount_paise"]) for r in returns),
           "by_mode": dict(by_mode), "by_gst_slab": slabs, "by_schedule": sched, "overrides": overrides}
    if shift_id:
        out["cash_expected_paise"] = await _rpc("rx_shift_expected_cash", {"p_clinic_id": clinic_id,
                                                                            "p_shift_id": shift_id})
    return out


async def collections_keys(clinic_id: str, start_dt: str, end_dt: str, branch_id: Optional[str]) -> dict:
    """Additive pharmacy keys for get_collections_summary (RX-34): money taken at pharmacy counters today."""
    q = scoped_query("rx_sale_payments", clinic_id, "sale_id, mode, amount_paise").gte("created_at", start_dt) \
        .lte("created_at", end_dt)
    pays = await _rows(q.limit(10000))
    if branch_id and pays:
        own = await _by_ids("rx_sales", clinic_id, [p["sale_id"] for p in pays], "id, branch_id")
        pays = [p for p in pays if str((own.get(str(p["sale_id"])) or {}).get("branch_id")) == str(branch_id)]
    by_mode: dict[str, int] = defaultdict(int)
    for p in pays:
        by_mode[p["mode"]] += int(p["amount_paise"])
    gross = sum(int(p["amount_paise"]) for p in pays if int(p["amount_paise"]) > 0)
    refunds = -sum(int(p["amount_paise"]) for p in pays if int(p["amount_paise"]) < 0)
    return {"pharmacy_gross_paise": gross, "pharmacy_refunds_paise": refunds, "pharmacy_net_paise": gross - refunds,
            "pharmacy_cash_paise": by_mode.get("cash", 0), "pharmacy_by_mode": dict(by_mode)}
