"""OPD OS data export (Data & Support → Export data).

Six CSV datasets for an OPD clinic: visits, the patient registry, invoices,
invoice lines, payments/refunds and cashier shifts. Every query carries the
clinic_id predicate; the caller has already resolved and checked the clinic.

Same caps as the general export (client_data.EXPORT_MAX_ROWS rows, one year
per download). Times are IST. Money is rupees with two decimals.

Erased (DPDP) patients are left out of visits and the registry, as in the
general export. Invoices and receipts are a money ledger, so their rows stay
and simply carry whatever the erasure left in the patient snapshot.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Optional

from app.database import sb, supabase
from app.services.client_data import (
    EXPORT_MAX_ROWS,
    EXPORT_PAGE,
    _IST,
    _is_erased,
    _require_scope,
    _rupees,
    ist_day_bounds_utc,
)

_IN_CHUNK = 150  # ids per IN (...) lookup; keeps the URL well under limits


def _ist(ts) -> str:
    """'YYYY-MM-DD HH:MM' in IST, or '' for a missing timestamp."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return str(ts)
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M")
    return dt.astimezone(_IST).strftime("%Y-%m-%d %H:%M")


def _words(v) -> str:
    return str(v).replace("_", " ") if v else ""


def _yes_no(v) -> str:
    return "Yes" if v else "No"


async def _paged(make_query: Callable, keep: Callable[[dict], bool] = lambda r: True) -> list:
    """Page a query to the end. Raises OverflowError past EXPORT_MAX_ROWS."""
    rows: list = []
    start = 0
    while True:
        page = (await sb(make_query().range(start, start + EXPORT_PAGE - 1))).data or []
        rows.extend(r for r in page if keep(r))
        if len(rows) > EXPORT_MAX_ROWS:
            raise OverflowError
        if len(page) < EXPORT_PAGE:
            return rows
        start += EXPORT_PAGE


async def _by_ids(table: str, clinic_id: str, column: str, ids: Iterable, select: str) -> list:
    """Rows of `table` in this clinic whose `column` is one of `ids`."""
    uniq = sorted({str(i) for i in ids if i})
    out: list = []
    for k in range(0, len(uniq), _IN_CHUNK):
        q = supabase.table(table).select(select).eq("clinic_id", clinic_id).in_(column, uniq[k:k + _IN_CHUNK])
        out.extend((await sb(q)).data or [])
    return out


async def _branch_names(clinic_id: str) -> dict:
    res = await sb(supabase.table("branches").select("id, name").eq("clinic_id", clinic_id))
    return {str(b["id"]): b.get("name") or "" for b in (res.data or [])}


async def _doctor_names(clinic_id: str) -> dict:
    res = await sb(supabase.table("doctors").select("id, name").eq("clinic_id", clinic_id))
    return {str(d["id"]): d.get("name") or "" for d in (res.data or [])}


def _snap(r: dict, key: str) -> str:
    return (r.get("patient_snapshot") or {}).get(key) or ""


# ─── fetchers: (clinic_id, date_from, date_to) -> flat rows ──────────────────


async def _visits(clinic_id: str, date_from: date, date_to: date) -> list:
    """Every OPD visit (a token was issued: walk-in or arrived booking) by visit date."""
    rows = await _paged(
        lambda: supabase.table("appointments").select("*").eq("clinic_id", clinic_id)
        .gte("appointment_date", date_from.isoformat())
        .lt("appointment_date", (date_to + timedelta(days=1)).isoformat())
        .not_.is_("token_number", "null")
        .order("appointment_date").order("token_number").order("id"),
        keep=lambda r: not _is_erased(r),
    )
    pats = {str(p["id"]): p for p in await _by_ids(
        "patients", clinic_id, "id", (r.get("patient_id") for r in rows), "id, mrn")}
    fams = {str(f["id"]): f for f in await _by_ids(
        "family_members", clinic_id, "id", (r.get("family_member_id") for r in rows), "id, mrn")}
    invs: dict = {}
    for inv in await _by_ids("opd_invoices", clinic_id, "appointment_id", (r["id"] for r in rows),
                             "appointment_id, invoice_number, status, total_paise, paid_paise"):
        if inv.get("status") != "void":  # at most one live invoice per visit (unique index)
            invs[str(inv["appointment_id"])] = inv
    branches = await _branch_names(clinic_id)
    for r in rows:
        holder = (fams.get(str(r["family_member_id"])) if r.get("family_member_id")
                  else pats.get(str(r.get("patient_id"))))
        r["_mrn"] = (holder or {}).get("mrn") or ""
        r["_branch"] = r.get("branch_name") or branches.get(str(r.get("branch_id")), "")
        r["_inv"] = invs.get(str(r["id"])) or {}
    return rows


async def _registry(clinic_id: str, date_from=None, date_to=None) -> list:
    """Every patient and family member who holds an MRN (registered at the OPD)."""
    pats = await _paged(
        lambda: supabase.table("patients").select("*").eq("clinic_id", clinic_id)
        .not_.is_("mrn", "null").order("mrn").order("id"),
        keep=lambda r: not _is_erased(r),
    )
    fams = await _paged(
        lambda: supabase.table("family_members").select("*").eq("clinic_id", clinic_id)
        .not_.is_("mrn", "null").order("mrn").order("id"),
        keep=lambda r: "[REDACTED]" not in (r.get("full_name"), r.get("primary_phone")),
    )
    if len(pats) + len(fams) > EXPORT_MAX_ROWS:
        raise OverflowError
    out = [{**p, "_kind": "Account holder", "_name": p.get("name"), "_phone": p.get("phone")} for p in pats]
    out += [{**f, "_kind": "Family member", "_name": f.get("full_name"), "_phone": f.get("primary_phone")}
            for f in fams]
    out.sort(key=lambda r: (str(r.get("mrn") or ""), str(r.get("id"))))
    return out


async def _invoices_in_range(clinic_id: str, date_from: date, date_to: date, select: str = "*") -> list:
    """Invoices ISSUED in the IST range. Drafts have no issued_at, so never appear."""
    lo, hi = ist_day_bounds_utc(date_from, date_to)
    return await _paged(
        lambda: supabase.table("opd_invoices").select(select).eq("clinic_id", clinic_id)
        .gte("issued_at", lo).lt("issued_at", hi).order("issued_at").order("id")
    )


async def _invoices(clinic_id: str, date_from: date, date_to: date) -> list:
    rows = await _invoices_in_range(clinic_id, date_from, date_to)
    branches = await _branch_names(clinic_id)
    for r in rows:
        r["_branch"] = branches.get(str(r.get("branch_id")), "")
        total = int(r.get("total_paise") or 0)
        r["_balance"] = 0 if r.get("status") == "void" else max(0, total - int(r.get("paid_paise") or 0))
    return rows


async def _invoice_items(clinic_id: str, date_from: date, date_to: date) -> list:
    invs = await _invoices_in_range(
        clinic_id, date_from, date_to, "id, invoice_number, issued_at, status, patient_snapshot")
    by_id = {str(i["id"]): i for i in invs}
    items = await _by_ids(
        "opd_invoice_items", clinic_id, "invoice_id", by_id.keys(),
        "invoice_id, line_no, item_type, catalog_code, doctor_id, description, quantity, "
        "unit_price_paise, discount_paise, line_total_paise")
    if len(items) > EXPORT_MAX_ROWS:
        raise OverflowError
    doctors = await _doctor_names(clinic_id)
    order = {iid: n for n, iid in enumerate(by_id)}  # invoices are already in issue order
    for it in items:
        it["_inv"] = by_id.get(str(it["invoice_id"])) or {}
        it["_doctor"] = doctors.get(str(it["doctor_id"]), "") if it.get("doctor_id") else ""
    items.sort(key=lambda it: (order.get(str(it["invoice_id"]), 0), int(it.get("line_no") or 0)))
    return items


async def _receipts(clinic_id: str, date_from: date, date_to: date) -> list:
    lo, hi = ist_day_bounds_utc(date_from, date_to)
    rows = await _paged(
        lambda: supabase.table("opd_receipts").select("*").eq("clinic_id", clinic_id)
        .gte("created_at", lo).lt("created_at", hi).order("created_at").order("id")
    )
    invs = {str(i["id"]): i for i in await _by_ids(
        "opd_invoices", clinic_id, "id", (r.get("invoice_id") for r in rows),
        "id, invoice_number, patient_snapshot, branch_id")}
    branches = await _branch_names(clinic_id)
    for r in rows:
        inv = invs.get(str(r.get("invoice_id"))) or {}
        r["_inv"] = inv
        r["_branch"] = branches.get(str(inv.get("branch_id")), "")
        amt = int(r.get("amount_paise") or 0)
        r["_net"] = -amt if r.get("kind") == "refund" else amt
    return rows


async def _shifts(clinic_id: str, date_from: date, date_to: date) -> list:
    lo, hi = ist_day_bounds_utc(date_from, date_to)
    rows = await _paged(
        lambda: supabase.table("opd_cashier_shifts").select("*").eq("clinic_id", clinic_id)
        .gte("opened_at", lo).lt("opened_at", hi).order("opened_at").order("id")
    )
    branches = await _branch_names(clinic_id)
    for r in rows:
        r["_branch"] = branches.get(str(r.get("branch_id")), "")
    return rows


_MODES = {"cash": "Cash", "upi": "UPI", "card": "Card",
          "payment_link": "Payment link", "prepaid_online": "Prepaid online"}

#: dataset -> (needs a date range, fetcher, [(CSV header, row -> value)])
OPD_EXPORT_DATASETS: dict = {
    "opd_visits": (True, _visits, [
        ("Visit Date", lambda r: r.get("appointment_date")),
        ("Token", lambda r: r.get("token_number")),
        ("Booking Ref", lambda r: r.get("booking_ref")),
        ("Patient Name", lambda r: r.get("patient_name")),
        ("MRN", lambda r: r.get("_mrn")),
        ("Patient Phone", lambda r: r.get("patient_phone")),
        ("Doctor", lambda r: r.get("doctor_name")),
        ("Department", lambda r: r.get("department")),
        ("Branch", lambda r: r.get("_branch")),
        ("Visit Type", lambda r: _words(r.get("visit_type"))),
        ("Walk-in", lambda r: _yes_no(r.get("is_walk_in"))),
        ("Booked Via", lambda r: _words(r.get("booking_channel"))),
        ("Queue Stage", lambda r: _words(r.get("queue_status"))),
        ("Status", lambda r: _words(r.get("status"))),
        ("Checked In (IST)", lambda r: _ist(r.get("checked_in_at"))),
        ("Completed (IST)", lambda r: _ist(r.get("completed_at"))),
        ("Invoice No", lambda r: r["_inv"].get("invoice_number")),
        ("Invoice Status", lambda r: _words(r["_inv"].get("status"))),
        ("Billed (Rs)", lambda r: _rupees(r["_inv"].get("total_paise"))),
        ("Paid (Rs)", lambda r: _rupees(r["_inv"].get("paid_paise"))),
    ]),
    "opd_patients": (False, _registry, [
        ("MRN", lambda r: r.get("mrn")),
        ("Name", lambda r: r.get("_name")),
        ("Record Type", lambda r: r.get("_kind")),
        ("Relationship", lambda r: r.get("relationship")),
        ("Phone", lambda r: r.get("_phone")),
        ("Gender", lambda r: r.get("gender")),
        ("Date of Birth", lambda r: r.get("date_of_birth")),
        ("Age (as recorded)", lambda r: r.get("age_years")),
        ("Age Recorded On", lambda r: r.get("age_recorded_on")),
        ("Address", lambda r: r.get("address_line")),
        ("City", lambda r: r.get("city")),
        ("Pincode", lambda r: r.get("pincode")),
        ("Emergency Contact", lambda r: r.get("emergency_contact_name")),
        ("Emergency Phone", lambda r: r.get("emergency_contact_phone")),
        ("Emergency Relation", lambda r: r.get("emergency_contact_relation")),
        ("Allergies", lambda r: "; ".join(r.get("allergies") or [])),
        ("Allergy Status", lambda r: _words(r.get("allergies_status"))),
        ("Registered (IST)", lambda r: _ist(r.get("created_at"))),
    ]),
    "opd_invoices": (True, _invoices, [
        ("Invoice No", lambda r: r.get("invoice_number")),
        ("Issued (IST)", lambda r: _ist(r.get("issued_at"))),
        ("Status", lambda r: _words(r.get("status"))),
        ("Patient Name", lambda r: _snap(r, "name")),
        ("MRN", lambda r: _snap(r, "mrn")),
        ("Patient Phone", lambda r: _snap(r, "phone")),
        ("Branch", lambda r: r.get("_branch")),
        ("Subtotal (Rs)", lambda r: _rupees(r.get("subtotal_paise"))),
        ("Discount (Rs)", lambda r: _rupees(r.get("discount_paise"))),
        ("Discount Reason", lambda r: r.get("discount_reason")),
        ("Total (Rs)", lambda r: _rupees(r.get("total_paise"))),
        ("Paid (Rs)", lambda r: _rupees(r.get("paid_paise"))),
        ("Balance Due (Rs)", lambda r: _rupees(r.get("_balance"))),
        ("Voided (IST)", lambda r: _ist(r.get("voided_at"))),
        ("Void Reason", lambda r: r.get("void_reason")),
        ("Notes", lambda r: r.get("notes")),
    ]),
    "opd_invoice_items": (True, _invoice_items, [
        ("Invoice No", lambda r: r["_inv"].get("invoice_number")),
        ("Issued (IST)", lambda r: _ist(r["_inv"].get("issued_at"))),
        ("Invoice Status", lambda r: _words(r["_inv"].get("status"))),
        ("Patient Name", lambda r: _snap(r["_inv"], "name")),
        ("Line", lambda r: r.get("line_no")),
        ("Type", lambda r: _words(r.get("item_type"))),
        ("Code", lambda r: r.get("catalog_code")),
        ("Description", lambda r: r.get("description")),
        ("Doctor", lambda r: r.get("_doctor")),
        ("Qty", lambda r: r.get("quantity")),
        ("Unit Price (Rs)", lambda r: _rupees(r.get("unit_price_paise"))),
        ("Line Discount (Rs)", lambda r: _rupees(r.get("discount_paise"))),
        ("Line Total (Rs)", lambda r: _rupees(r.get("line_total_paise"))),
    ]),
    "opd_receipts": (True, _receipts, [
        ("Receipt No", lambda r: r.get("receipt_number")),
        ("Date (IST)", lambda r: _ist(r.get("created_at"))),
        ("Type", lambda r: "Refund" if r.get("kind") == "refund" else "Payment"),
        ("Mode", lambda r: _MODES.get(r.get("mode"), _words(r.get("mode")))),
        ("Amount (Rs)", lambda r: _rupees(r.get("amount_paise"))),
        ("Net Effect (Rs)", lambda r: _rupees(r.get("_net"))),
        ("Reference", lambda r: r.get("reference")),
        ("Invoice No", lambda r: r["_inv"].get("invoice_number")),
        ("Patient Name", lambda r: _snap(r["_inv"], "name")),
        ("Branch", lambda r: r.get("_branch")),
        ("Received By", lambda r: r.get("received_by_name") or ("Online" if not r.get("shift_id") else "")),
        ("Reason", lambda r: r.get("reason")),
        ("Gateway", lambda r: r.get("gateway")),
        ("Gateway Payment ID", lambda r: r.get("gateway_payment_id")),
    ]),
    "opd_shifts": (True, _shifts, [
        ("Cashier", lambda r: r.get("cashier_name")),
        ("Branch", lambda r: r.get("_branch")),
        ("Status", lambda r: _words(r.get("status"))),
        ("Opened (IST)", lambda r: _ist(r.get("opened_at"))),
        ("Closed (IST)", lambda r: _ist(r.get("closed_at"))),
        ("Opening Float (Rs)", lambda r: _rupees(r.get("opening_float_paise"))),
        ("Expected Cash (Rs)", lambda r: _rupees(r.get("expected_cash_paise"))),
        ("Declared Cash (Rs)", lambda r: _rupees(r.get("declared_cash_paise"))),
        ("Variance (Rs)", lambda r: _rupees(r.get("variance_paise"))),
        ("Close Notes", lambda r: r.get("close_notes")),
    ]),
}


async def fetch_opd_export_rows(clinic_id: str, dataset: str,
                                date_from: Optional[date], date_to: Optional[date]) -> list:
    _require_scope(clinic_id)
    needs_range, fetch, _ = OPD_EXPORT_DATASETS[dataset]
    if needs_range and (date_from is None or date_to is None):
        raise ValueError("date range required")  # check_date_range() runs first
    return await fetch(clinic_id, date_from, date_to)
