"""Investigation & procedure orders (G1, migration 115).

One opd_orders row per ordered line is both the lab / radiology worklist entry
and the source of its bill line. OPD orders are placed on an encounter
(opd_place_orders) and reach the visit's draft bill in the same transaction
(opd_bill_orders); IPD investigation / procedure orders are mirrored into the
worklist by a trigger on ipd_orders. Every state change is one SQL function or
one compare-and-set UPDATE; the database refuses anything else
(opd_guard_order). This module validates, calls them, maps their opd_* error
codes to HTTP errors, and builds the read models.

Zero-LLM: imports nothing from the AI, voice or assistant packages
(tests/test_ipd_no_llm.py).
"""

import logging
from datetime import datetime, time, timezone
from typing import Any, Iterable, Optional

from fastapi import HTTPException

from app.database import fetch_all, restrict_to_branch, sb, scoped_query, supabase
from app.services.opd_billing import get_or_create_appointment_invoice
from app.utils.helpers import IST, actor_uuid, today_ist

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("ordered", "billed", "sample_collected", "in_progress", "resulted")
CLOSED_STATUSES = ("completed", "cancelled")
_URGENCY_RANK = {"stat": 0, "urgent": 1, "routine": 2}

# error code (prefix before ':') -> (HTTP status, message shown to staff)
_ERRORS: dict[str, tuple[int, str]] = {
    "opd_not_found": (404, "Consultation or visit not found."),
    "opd_order_not_found": (404, "Order not found."),
    "opd_order_bad_key": (422, "A request key of 8-72 letters, digits, '-' or '_' is required."),
    "opd_order_empty": (422, "Add between 1 and 30 orders."),
    "opd_order_bad_catalog": (422, "Unknown catalogue type."),
    "opd_order_catalog_not_found": (422, "A test or service is no longer in the active catalogue. Reload and pick again."),
    "opd_order_key_reused": (409, "This request was already used for another consultation. Reload and try again."),
    "opd_encounter_superseded": (409, "This consultation was amended. Reload it and order on the current version."),
    "opd_order_not_billable": (409, "This order cannot be billed (it is cancelled or belongs to an inpatient stay)."),
    "opd_order_wrong_invoice": (409, "This order belongs to another patient or visit."),
    "opd_order_already_billed": (409, "This order is already on a bill."),
    "opd_order_ipd_managed": (409, "Inpatient orders are stopped from the IPD chart: discontinue the ward order."),
    "opd_order_not_cancellable": (409, "A resulted or completed order cannot be cancelled."),
    "opd_order_bill_changed": (409, "The bill changed while cancelling. Reload and try again."),
    "opd_order_paid": (409, "Cannot cancel an order that has already been paid. Please process a refund or credit "
                            "note at the billing desk."),
    "opd_order_on_issued_invoice": (409, "This order is on an issued bill. Void the bill at the billing desk, then "
                                         "cancel the order."),
    "opd_order_discount_exceeds": (409, "Removing this line would leave the bill discount above the bill total. "
                                        "Reduce the discount first."),
    "opd_order_bad_transition": (409, "The order is not in a state that allows this step. Reload the worklist."),
    "opd_order_billing_is_derived": (409, "Billing status follows the bill and cannot be set directly."),
    "opd_order_sample_required": (409, "Collect the sample before completing this test."),
    "opd_record_locked": (409, "This order is final and cannot be changed."),
    "opd_append_only": (409, "Orders cannot be deleted; cancel them instead."),
    "opd_invoice_too_many_lines": (422, "The bill would exceed 100 lines. Bill the rest separately."),
    "opd_visit_invoice_missing": (409, "Open the visit's bill first."),
    "opd_not_draft": (409, "This bill is no longer a draft. Reload it."),
}


def _code_of(exc: Exception) -> str:
    msg = str(getattr(exc, "message", None) or exc)
    for base in sorted(_ERRORS, key=len, reverse=True):   # longest first: opd_not_found vs opd_order_not_found
        idx = msg.find(base)
        if idx >= 0:
            tail = msg[idx:]
            end = min((tail.find(ch) for ch in ("'", '"', "\n", " ", ",", "}") if tail.find(ch) >= 0),
                      default=len(tail))
            return tail[:end]
    return ""


def raise_for(exc: Exception) -> None:
    """Map an opd_* database error to an HTTPException; anything else is a 422
    (constraint) or a 500 with a fixed message, never the raw database text."""
    if isinstance(exc, HTTPException):
        raise exc
    code = _code_of(exc)
    base, _, sub = code.partition(":")
    if base in _ERRORS:
        status, message = _ERRORS[base]
        raise HTTPException(status_code=status, detail={"code": base, "message": message, "reason": sub or None})
    if "violates check constraint" in str(exc):
        raise HTTPException(status_code=422, detail="Some values are outside the allowed range or incomplete.")
    logger.error(f"ORDERS_DB_ERROR type={type(exc).__name__}")
    raise HTTPException(status_code=500, detail="The order operation failed. Nothing was changed; please retry.")


def _actor(user: Any) -> tuple[Optional[str], str]:
    return actor_uuid(user), str(getattr(user, "username", None) or "system")[:100]


async def _rpc(name: str, params: dict) -> Any:
    try:
        res = await sb(supabase.rpc(name, params))
    except Exception as e:  # postgrest APIError carries the opd_* code
        raise_for(e)
    return res.data


async def _rows_in(table: str, clinic_id: str, cols: str, column: str, ids: Iterable) -> list[dict]:
    """IN lookup in batches of 100 (a long UUID list overflows the gateway's
    request line), each batch paged past the 1000-row response cap."""
    ids = sorted({str(i) for i in ids if i})
    out: list[dict] = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        out += await fetch_all(lambda c=chunk: scoped_query(table, clinic_id, cols).in_(column, c).order("id"),
                               label=f"orders:{table}")
    return out


async def get_order(clinic_id: str, order_id: str) -> dict:
    res = await sb(scoped_query("opd_orders", clinic_id).eq("id", order_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Order not found.")
    return res.data[0]


async def get_encounter(clinic_id: str, encounter_id: str) -> dict:
    res = await sb(scoped_query("opd_encounters", clinic_id, "id, appointment_id, doctor_id, branch_id, status")
                   .eq("id", encounter_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Encounter not found")
    return res.data[0]


# ── OPD: place, list, bill, cancel ──────────────────────────────────────────

async def create_orders(clinic_id: str, encounter_id: str, items: list[dict], idempotency_key: str,
                        user: Any) -> dict:
    """Place a consultation's orders in one transaction; they join the visit's
    draft bill, if one is open, in that same transaction. A retried click with
    the same key returns the first attempt's orders (`replayed`)."""
    aid, name = _actor(user)
    return await _rpc("opd_place_orders", {
        "p_clinic_id": clinic_id, "p_encounter_id": encounter_id, "p_items": items,
        "p_idempotency_key": idempotency_key, "p_actor_id": aid, "p_actor_name": name,
    })


async def _billing_by_order(clinic_id: str, order_ids: list[str]) -> dict[str, dict]:
    """order id -> the live (non-void) bill carrying it."""
    lines = await _rows_in("opd_invoice_items", clinic_id, "id, invoice_id, order_id, line_total_paise",
                           "order_id", order_ids)
    invoices = {i["id"]: i for i in await _rows_in("opd_invoices", clinic_id, "id, status, invoice_number",
                                                   "id", [ln["invoice_id"] for ln in lines])}
    out: dict[str, dict] = {}
    for ln in lines:
        inv = invoices.get(ln["invoice_id"])
        if inv and inv["status"] != "void":
            out[ln["order_id"]] = {"invoice_id": inv["id"], "invoice_status": inv["status"],
                                   "invoice_number": inv.get("invoice_number"),
                                   "line_total_paise": ln.get("line_total_paise")}
    return out


async def list_visit_orders(clinic_id: str, appointment_id: str) -> list[dict]:
    """Every order of one OPD visit (all encounter versions), with its bill."""
    rows = await fetch_all(lambda: scoped_query("opd_orders", clinic_id).eq("appointment_id", appointment_id)
                           .order("created_at").order("id"), label="orders:visit")
    billing = await _billing_by_order(clinic_id, [r["id"] for r in rows])
    return [{**r, "billing": billing.get(r["id"])} for r in rows]


async def bill_pending_orders(clinic_id: str, appointment_id: str, user: Any) -> dict:
    """Put every unbilled, uncancelled order of the visit on a draft bill: the
    visit's own draft (opened now if the visit has no bill yet), or a
    supplementary draft once the visit's bill has been issued."""
    live = await sb(scoped_query("opd_invoices", clinic_id, "id").eq("appointment_id", appointment_id)
                    .neq("status", "void").limit(1))
    if not live.data:
        # creates the draft with the consultation line and adds pending orders to it
        await get_or_create_appointment_invoice(clinic_id, appointment_id, user)
    return await _rpc("opd_bill_orders", {
        "p_clinic_id": clinic_id, "p_appointment_id": appointment_id, "p_order_ids": None,
        "p_create": True, "p_actor_id": actor_uuid(user),
    })


async def cancel_order(clinic_id: str, order_id: str, reason: str, user: Any) -> dict:
    """Cancel an OPD order. A draft bill loses its line in the same transaction;
    an issued or paid bill refuses (refund and void at the counter first)."""
    return await _rpc("opd_cancel_order", {
        "p_clinic_id": clinic_id, "p_order_id": order_id, "p_reason": reason, "p_actor_id": actor_uuid(user),
    })


# ── worklist: collect, complete ─────────────────────────────────────────────

async def get_diagnostic_worklist(clinic_id: str, branch_id: Optional[str] = None,
                                  status_filter: Optional[str] = None, limit: int = 100,
                                  offset: int = 0) -> dict:
    """The sample-collection / lab desk: open OPD and IPD orders, STAT first.

    Every open row is read (paged past the response cap) and sorted before the
    page is cut, so STAT orders are never pushed onto a later page. A closed
    status (completed / cancelled) shows today's (IST) rows only."""
    # to_collect: everything still waiting at the collection desk (paid or not)
    statuses = (["ordered", "billed"] if status_filter == "to_collect"
                else [status_filter] if status_filter else list(OPEN_STATUSES))
    since = None
    if status_filter in CLOSED_STATUSES:
        since = datetime.combine(today_ist(), time.min, tzinfo=IST).astimezone(timezone.utc).isoformat()

    def build():
        q = scoped_query("opd_orders", clinic_id).in_("status", statuses)
        if since:
            q = q.gte("updated_at", since)
        return restrict_to_branch(q, branch_id).order("created_at").order("id")

    rows = await fetch_all(build, label="orders:worklist")
    rows.sort(key=lambda r: (_URGENCY_RANK.get(r.get("urgency"), 9), r.get("created_at") or "", r["id"]))
    page = rows[offset:offset + limit]

    patients = {p["id"]: p for p in await _rows_in("patients", clinic_id, "id, name, mrn, gender, age_years", "id",
                                                   [r["patient_id"] for r in page])}
    members = {m["id"]: m for m in await _rows_in("family_members", clinic_id, "id, full_name, mrn", "id",
                                                  [r.get("family_member_id") for r in page])}
    doctors = {d["id"]: d for d in await _rows_in("doctors", clinic_id, "id, name", "id",
                                                  [r["doctor_id"] for r in page])}
    visits = {a["id"]: a for a in await _rows_in("appointments", clinic_id, "id, token_number", "id",
                                                 [r.get("appointment_id") for r in page])}
    stays = {a["id"]: a for a in await _rows_in("ipd_admissions", clinic_id, "id, admission_number, current_bed_id",
                                                "id", [r.get("admission_id") for r in page])}
    beds = {b["id"]: b for b in await _rows_in("ipd_beds", clinic_id, "id, code", "id",
                                               [s.get("current_bed_id") for s in stays.values()])}
    billing = await _billing_by_order(clinic_id, [r["id"] for r in page if r["source"] == "opd"])

    out = []
    for r in page:
        member = members.get(r.get("family_member_id")) if r.get("family_member_id") else None
        patient = patients.get(r["patient_id"], {})
        stay = stays.get(r.get("admission_id")) or {}
        out.append({
            "id": r["id"], "source": r["source"], "status": r["status"], "urgency": r["urgency"],
            "order_type": r["order_type"], "catalog_type": r["catalog_type"], "item_name": r["item_name"],
            "sample_type": r.get("sample_type"),
            "quantity": r.get("quantity"), "clinical_notes": r.get("clinical_notes"),
            "specimen_barcode": r.get("specimen_barcode"), "sample_collected_at": r.get("sample_collected_at"),
            "sample_collected_by_name": r.get("sample_collected_by_name"), "resulted_at": r.get("resulted_at"),
            "result_summary": r.get("result_summary"), "created_at": r.get("created_at"),
            "branch_id": r.get("branch_id"), "appointment_id": r.get("appointment_id"),
            "admission_id": r.get("admission_id"),
            "patient_name": (member or {}).get("full_name") or patient.get("name"),
            "mrn": (member or {}).get("mrn") or patient.get("mrn"),
            "gender": patient.get("gender") if not member else None,
            "age_years": patient.get("age_years") if not member else None,
            "doctor_name": doctors.get(r["doctor_id"], {}).get("name"),
            "token_number": visits.get(r.get("appointment_id"), {}).get("token_number"),
            "admission_number": stay.get("admission_number"),
            "bed_code": beds.get(stay.get("current_bed_id"), {}).get("code"),
            "billing": billing.get(r["id"]),
        })
    return {"orders": out, "total": len(rows), "limit": limit, "offset": offset}


async def collect_sample(clinic_id: str, order_id: str, barcode: str, user: Any) -> dict:
    """ordered|billed -> sample_collected, once (compare-and-set)."""
    order = await get_order(clinic_id, order_id)
    if not order.get("sample_type"):
        raise HTTPException(status_code=409, detail="This order needs no specimen. Mark it complete instead.")
    aid, name = _actor(user)
    try:
        res = await sb(supabase.table("opd_orders").update({
            "status": "sample_collected", "specimen_barcode": barcode,
            "sample_collected_at": datetime.now(timezone.utc).isoformat(),
            "sample_collected_by": aid, "sample_collected_by_name": name,
        }).eq("clinic_id", clinic_id).eq("id", order_id).in_("status", ["ordered", "billed"]))
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=409, detail="This order is not waiting for a sample (already collected, "
                                                    "completed or cancelled). Reload the worklist.")
    return res.data[0]


async def complete_order(clinic_id: str, order_id: str, result_summary: str, user: Any) -> dict:
    """Close an order with its result note. A specimen-bound test must have its
    sample collected first (the database refuses otherwise). 'resulted' rows
    belong to the LIS (G2), which completes them itself."""
    order = await get_order(clinic_id, order_id)
    if order["catalog_type"] == "lab_test":
        # entered LIS values are released only by a pathologist's signature, never by a text note
        lis = await sb(scoped_query("lab_results", clinic_id, "id").eq("order_id", order_id).limit(1))
        if lis.data:
            raise HTTPException(status_code=409, detail="This test has lab results awaiting sign-off. Release it from "
                                                        "Lab Sign-off.")
    else:
        # a drafted radiology report (and an obstetric scan's Form F) is released only by the radiologist's signature
        rad = await sb(scoped_query("radiology_reports", clinic_id, "id").eq("order_id", order_id).limit(1))
        if rad.data:
            raise HTTPException(status_code=409, detail="This study has a radiology report in progress. Finalize it "
                                                        "from Radiology.")
    aid, name = _actor(user)
    try:
        res = await sb(supabase.table("opd_orders").update({
            "status": "completed", "resulted_at": datetime.now(timezone.utc).isoformat(),
            "resulted_by": aid, "resulted_by_name": name, "result_summary": result_summary,
        }).eq("clinic_id", clinic_id).eq("id", order_id)
          .in_("status", ["ordered", "billed", "sample_collected", "in_progress"]))
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=409, detail="This order is already completed or cancelled. Reload the worklist.")
    return res.data[0]
