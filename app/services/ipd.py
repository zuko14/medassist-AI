"""Kriya IPD OS service layer (migration 111).

Every state change of a bed or an admission is ONE call to an ipd_* SQL
function that row-locks what it touches (bed allocation, transfer, discharge,
billing). This module only validates input, calls those functions, maps their
'ipd_*' error codes to HTTP errors, and builds read models for the screens.

Zero-LLM: imports nothing from the AI or voice packages
(tests/test_ipd_no_llm.py). Kriya Ketu reaches IPD only through the HTTP
routes, like every other module.
"""

import logging
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase
from app.utils.helpers import actor_uuid, doctor_title, today_ist

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "admitted", "discharge_pending")
IN_BED_STATUSES = ("admitted", "discharge_pending")
BED_STATUSES = ("available", "reserved", "occupied", "cleaning", "maintenance", "blocked")
DEFAULT_SETTINGS = {"discharge_cutoff_hour": 12, "bed_tariffs": {}, "reservation_minutes": 120}

# error code (prefix before ':') -> (HTTP status, message shown to staff)
_ERRORS: dict[str, tuple[int, str]] = {
    "ipd_patient_not_found": (404, "Patient not found."),
    "ipd_family_member_not_found": (404, "Family member not found for this patient."),
    "ipd_doctor_not_found": (404, "Doctor not found or inactive."),
    "ipd_admission_not_found": (404, "Admission not found."),
    "ipd_bed_not_found": (404, "Bed not found."),
    "ipd_observation_not_found": (404, "Observation not found."),
    "ipd_note_not_found": (404, "Note not found."),
    "ipd_summary_not_found": (404, "Discharge summary not found."),
    "ipd_charge_not_found": (404, "Charge not found."),
    "ipd_order_not_found": (404, "Order not found for this admission."),
    "ipd_active_admission_exists": (409, "This patient already has an active admission."),
    "ipd_bed_unavailable": (409, "That bed is no longer available. Refresh the bed board and pick another."),
    "ipd_bed_in_use": (409, "The bed is occupied or reserved."),
    "ipd_bed_has_history": (409, "This bed has admission history; deactivate it instead of deleting."),
    "ipd_bed_not_reserved": (409, "The bed is not reserved."),
    "ipd_stale_bed": (409, "The patient was moved by someone else. Refresh and try again."),
    "ipd_same_bed": (422, "The patient is already in that bed."),
    "ipd_bad_transition": (409, "That step is not allowed in the admission's current state."),
    "ipd_admission_not_active": (409, "The patient is not currently admitted to a bed."),
    "ipd_record_locked": (409, "This record is final and cannot be changed. Use a correction or amendment."),
    "ipd_not_author": (403, "Only the author can amend this note. Add a new note instead."),
    "ipd_not_attending_doctor": (403, "Only the attending doctor can sign the discharge summary."),
    "ipd_doctor_registration_missing": (422, "Add the doctor's registration number before signing."),
    "ipd_summary_incomplete": (422, "The discharge summary needs a final diagnosis before signing."),
    "ipd_order_not_medication": (422, "Medication administration can be recorded only against a medication order."),
    "ipd_order_not_active": (409, "That order is no longer active."),
    "ipd_charge_billed": (409, "This charge is already on the final bill."),
    "ipd_charges_pending_review": (409, "Post or void every proposed charge before generating the bill."),
    "ipd_no_charges": (409, "There are no posted charges to bill."),
    "ipd_bill_needs_discharge_pending": (409, "Start the discharge before generating the final bill."),
    "ipd_bill_exists_void_first": (409, "A final bill exists. Void it in OPD Billing before reverting the discharge."),
    "ipd_bill_too_many_lines": (422, "The bill would exceed 100 lines."),
    "ipd_bill_line_quantity": (422, "A bill line would exceed 999 units."),
    "ipd_bill_lines_locked": (409, "Inpatient bill lines come from posted charges and cannot be edited here."),
    "ipd_discharge_blocked": (409, "Discharge checklist is not complete."),
    "ipd_settlement_waiver_required": (409, "The bill is not settled. An administrator must record a waiver reason."),
    "ipd_reason_required": (422, "A reason is required."),
    "ipd_reservation_minutes": (422, "Reservation must be between 5 and 1440 minutes."),
    "ipd_bad_bed_status": (422, "Unknown bed status."),
    "ipd_bed_ineligible": (422, "This bed is not eligible for this patient."),
}
_INELIGIBLE = {"gender": "This ward is restricted to the other gender.",
               "gender_unknown": "Record the patient's gender before using a gender-restricted ward.",
               "branch": "The bed is in a different branch.", "inactive": "The bed or ward is inactive."}


def _code_of(exc: Exception) -> str:
    msg = str(getattr(exc, "message", None) or exc)
    idx = msg.find("ipd_")
    if idx < 0:
        return ""
    tail = msg[idx:]
    end = min((tail.find(ch) for ch in ("'", '"', "\n", " ", ",", "}") if tail.find(ch) >= 0), default=len(tail))
    return tail[:end]


def raise_for(exc: Exception) -> None:
    """Map an ipd_* database error to an HTTPException; anything else is a 500
    with a fixed message (never the raw database text)."""
    code = _code_of(exc)
    base, _, sub = code.partition(":")
    if base in _ERRORS:
        status, message = _ERRORS[base]
        if base == "ipd_bed_ineligible" and sub in _INELIGIBLE:
            message = _INELIGIBLE[sub]
        raise HTTPException(status_code=status, detail={"code": base, "message": message, "reason": sub or None})
    logger.error(f"IPD_DB_ERROR type={type(exc).__name__}")
    raise HTTPException(status_code=500, detail="The inpatient operation failed. Nothing was changed; please retry.")


def _actor(user: Any) -> tuple[Optional[str], str]:
    return actor_uuid(user), str(getattr(user, "username", None) or "system")[:100]


async def rpc(name: str, params: dict) -> Any:
    try:
        res = await sb(supabase.rpc(name, params))
    except HTTPException:
        raise
    except Exception as e:  # postgrest APIError carries the ipd_* code
        raise_for(e)
    data = res.data
    return data[0] if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else data


async def _rows(query) -> list[dict]:
    res = await sb(query)
    return list(res.data or [])


# ── configuration: wards, beds, settings ────────────────────────────────────

async def provision_defaults(clinic_id: str) -> None:
    """Fill missing ipd_settings keys; never overwrite existing ones."""
    # unscoped: platform owner provisioning one clinic's IPD settings by its primary key
    res = await sb(supabase.table("clinics").select("ipd_settings").eq("id", clinic_id).limit(1))
    if not res.data:
        return
    existing = res.data[0].get("ipd_settings") or {}
    merged = {**DEFAULT_SETTINGS, **existing}
    if merged != existing:
        # unscoped: platform owner provisioning one clinic's IPD settings by its primary key
        await sb(supabase.table("clinics").update({"ipd_settings": merged}).eq("id", clinic_id))


async def list_wards(clinic_id: str) -> list[dict]:
    return await _rows(scoped_query("ipd_wards", clinic_id).order("code"))


async def create_ward(clinic_id: str, body: dict) -> dict:
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("ipd_wards").insert({**body, "clinic_id": clinic_id}))
    except Exception as e:
        if "ipd_wards_code_key" in str(e):
            raise HTTPException(status_code=409, detail="A ward with this code already exists.")
        if "ipd_wards_branch_fk" in str(e):
            raise HTTPException(status_code=404, detail="Branch not found.")
        raise_for(e)
    return res.data[0]


async def update_ward(clinic_id: str, ward_id: str, patch: dict) -> dict:
    res = await sb(supabase.table("ipd_wards").update(patch).eq("clinic_id", clinic_id).eq("id", ward_id))
    if not res.data:
        raise HTTPException(status_code=404, detail="Ward not found.")
    return res.data[0]


async def create_beds(clinic_id: str, ward_id: str, beds: list[dict]) -> list[dict]:
    if not await _rows(scoped_query("ipd_wards", clinic_id).eq("id", ward_id).limit(1)):
        raise HTTPException(status_code=404, detail="Ward not found.")
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("ipd_beds").insert(
            [{**b, "clinic_id": clinic_id, "ward_id": ward_id} for b in beds]))
    except Exception as e:
        if "ipd_beds_code_key" in str(e):
            raise HTTPException(status_code=409, detail="A bed with one of these codes already exists in this ward.")
        raise_for(e)
    return res.data or []


async def update_bed(clinic_id: str, bed_id: str, patch: dict) -> dict:
    try:
        res = await sb(supabase.table("ipd_beds").update(patch).eq("clinic_id", clinic_id).eq("id", bed_id))
    except Exception as e:
        if "ipd_beds_inactive_not_in_use" in str(e):
            raise HTTPException(status_code=409, detail="An occupied or reserved bed cannot be deactivated.")
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=404, detail="Bed not found.")
    return res.data[0]


# ── read models ─────────────────────────────────────────────────────────────

async def _people(clinic_id: str, admissions: list[dict]) -> dict[str, dict]:
    """Display identity per admission id: name, MRN, gender, age (no contact data)."""
    pids = sorted({a["patient_id"] for a in admissions})
    fids = sorted({a["family_member_id"] for a in admissions if a.get("family_member_id")})
    patients = {p["id"]: p for p in await _rows(
        scoped_query("patients", clinic_id, "id, name, mrn, gender, age_years").in_("id", pids))} if pids else {}
    family = {f["id"]: f for f in await _rows(
        scoped_query("family_members", clinic_id, "id, full_name, mrn, gender, age_years").in_("id", fids))} if fids else {}
    out = {}
    for a in admissions:
        f = family.get(a.get("family_member_id") or "")
        p = patients.get(a["patient_id"], {})
        src = f or p
        out[a["id"]] = {"patient_name": (f or {}).get("full_name") or p.get("name"), "mrn": src.get("mrn"),
                        "gender": src.get("gender"), "age_years": src.get("age_years")}
    return out


async def _doctor_names(clinic_id: str, ids: set) -> dict[str, str]:
    ids = sorted(i for i in ids if i)
    if not ids:
        return {}
    return {d["id"]: doctor_title(d.get("name")) for d in await _rows(
        scoped_query("doctors", clinic_id, "id, name").in_("id", ids))}


def _parse_ts(value: Any) -> Optional[datetime]:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None


def _days(admitted_at: Any) -> Optional[int]:
    start = _parse_ts(admitted_at)
    return max(0, (datetime.now(timezone.utc) - start).days) if start else None


async def bed_board(clinic_id: str, branch_id: Optional[str] = None, clinical: bool = False) -> dict:
    """Wards -> beds with occupant summary. Diagnosis only for clinical roles.

    shortcut: whole-clinic read (wards x beds), fine to a few hundred beds; add
    paging per ward when a tenant passes ~1,000 beds.
    """
    wq = scoped_query("ipd_wards", clinic_id).order("code")
    if branch_id:
        wq = wq.eq("branch_id", branch_id)
    wards = await _rows(wq)
    ward_ids = [w["id"] for w in wards]
    beds = await _rows(scoped_query("ipd_beds", clinic_id).in_("ward_id", ward_ids).order("code")) if ward_ids else []
    adm_ids = sorted({b[k] for b in beds for k in ("current_admission_id", "reserved_admission_id") if b.get(k)})
    adms = {a["id"]: a for a in await _rows(scoped_query("ipd_admissions", clinic_id).in_("id", adm_ids))} if adm_ids else {}
    people = await _people(clinic_id, list(adms.values()))
    doctors = await _doctor_names(clinic_id, {a.get("attending_doctor_id") for a in adms.values()})
    now = datetime.now(timezone.utc)
    by_ward: dict[str, list] = {w["id"]: [] for w in wards}
    for b in beds:
        occ = adms.get(b.get("current_admission_id") or b.get("reserved_admission_id") or "")
        row = {k: b.get(k) for k in ("id", "code", "room_label", "bed_category", "features", "status",
                                     "status_reason", "reserved_until", "is_active", "version")}
        # an expired hold is shown as such, not as free: only the next assignment frees it
        until = _parse_ts(b.get("reserved_until"))
        row["reservation_expired"] = bool(b.get("status") == "reserved" and until and until < now)
        if occ:
            row["occupant"] = {
                "admission_id": occ["id"], "admission_number": occ.get("admission_number"),
                "status": occ.get("status"), **people.get(occ["id"], {}),
                "attending_doctor": doctors.get(occ.get("attending_doctor_id")),
                "days": _days(occ.get("admitted_at")), "expected_discharge_on": occ.get("expected_discharge_on"),
                **({"provisional_diagnosis": occ.get("provisional_diagnosis")} if clinical else {}),
            }
        by_ward.setdefault(b["ward_id"], []).append(row)
    counts = Counter(b["status"] for b in beds if b.get("is_active"))
    return {"wards": [{**w, "beds": by_ward.get(w["id"], [])} for w in wards],
            "summary": {**{s: counts.get(s, 0) for s in BED_STATUSES}, "total": sum(counts.values())}}


async def list_admissions(clinic_id: str, status: Optional[list[str]] = None, q: Optional[str] = None,
                          branch_id: Optional[str] = None, limit: int = 50, offset: int = 0) -> dict:
    query = scoped_query("ipd_admissions", clinic_id).order("created_at", desc=True)
    if status:
        query = query.in_("status", status)
    if branch_id:
        query = query.eq("branch_id", branch_id)
    if q:
        query = query.ilike("admission_number", f"%{q.strip()[:40]}%")
    rows = await _rows(query.range(offset, offset + limit - 1))
    people = await _people(clinic_id, rows)
    doctors = await _doctor_names(clinic_id, {r.get("attending_doctor_id") for r in rows})
    bed_ids = sorted({r["current_bed_id"] for r in rows if r.get("current_bed_id")})
    beds = {b["id"]: b["code"] for b in await _rows(
        scoped_query("ipd_beds", clinic_id, "id, code").in_("id", bed_ids))} if bed_ids else {}
    return {"admissions": [{
        **{k: r.get(k) for k in ("id", "admission_number", "status", "priority", "source", "admitted_at",
                                 "expected_discharge_on", "discharge_type", "discharged_at", "created_at",
                                 "payer_type", "branch_id")},
        **people.get(r["id"], {}), "bed": beds.get(r.get("current_bed_id")),
        "attending_doctor": doctors.get(r.get("attending_doctor_id"))} for r in rows],
        "limit": limit, "offset": offset}


async def get_admission_row(clinic_id: str, admission_id: str) -> dict:
    rows = await _rows(scoped_query("ipd_admissions", clinic_id).eq("id", admission_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Admission not found.")
    return rows[0]


async def admission_detail(clinic_id: str, admission_id: str, clinical: bool, billing: bool) -> dict:
    a = await get_admission_row(clinic_id, admission_id)
    people = await _people(clinic_id, [a])
    doctors = await _doctor_names(clinic_id, {a.get("attending_doctor_id"), a.get("admitting_doctor_id")})
    assignments = await _rows(scoped_query("ipd_bed_assignments", clinic_id).eq("admission_id", admission_id)
                              .order("started_at"))
    bed_codes = {b["id"]: b["code"] for b in await _rows(scoped_query("ipd_beds", clinic_id, "id, code").in_(
        "id", sorted({x["bed_id"] for x in assignments})))} if assignments else {}
    ward_names = {w["id"]: w["name"] for w in await _rows(scoped_query("ipd_wards", clinic_id, "id, name"))}
    events = await _rows(scoped_query("ipd_events", clinic_id).eq("admission_id", admission_id)
                         .order("occurred_at").limit(500))
    out = {
        "admission": {k: v for k, v in a.items() if clinical or k != "provisional_diagnosis"},
        **people.get(a["id"], {}),
        "attending_doctor": doctors.get(a.get("attending_doctor_id")),
        "admitting_doctor": doctors.get(a.get("admitting_doctor_id")),
        "bed_history": [{**x, "bed_code": bed_codes.get(x["bed_id"]), "ward_name": ward_names.get(x["ward_id"])}
                        for x in assignments],
        "timeline": events,
    }
    if clinical:
        by_adm = lambda t: scoped_query(t, clinic_id).eq("admission_id", admission_id)
        out["observations"] = await _rows(by_adm("ipd_observations").order("observed_at", desc=True).limit(200))
        out["notes"] = await _rows(by_adm("ipd_notes").order("authored_at", desc=True).limit(200))
        out["orders"] = await _rows(by_adm("ipd_orders").order("ordered_at", desc=True))
        out["administrations"] = await _rows(by_adm("ipd_med_administrations").order("administered_at", desc=True)
                                             .limit(300))
        out["discharge_summaries"] = await _rows(by_adm("ipd_discharge_summaries").order("version"))
    if billing:
        out["charges"] = await _rows(scoped_query("ipd_charges", clinic_id).eq("admission_id", admission_id)
                                     .order("service_date"))
        out["bills"] = await _rows(scoped_query("opd_invoices", clinic_id,
                                                "id, invoice_number, status, total_paise, paid_paise")
                                   .eq("admission_id", admission_id).order("created_at"))
    return out


async def census(clinic_id: str, branch_id: Optional[str] = None) -> dict:
    """Operational numbers for the IPD dashboard (no clinical content)."""
    board = await bed_board(clinic_id, branch_id)
    q = scoped_query("ipd_admissions", clinic_id, "id, status, expected_discharge_on").in_(
        "status", list(ACTIVE_STATUSES))
    disc_q = scoped_query("ipd_admissions", clinic_id, "id")
    adm_q = scoped_query("ipd_admissions", clinic_id, "id")
    if branch_id:
        q, disc_q, adm_q = q.eq("branch_id", branch_id), disc_q.eq("branch_id", branch_id), adm_q.eq("branch_id", branch_id)
    active = await _rows(q)
    today = today_ist().isoformat()
    since = f"{today}T00:00:00+05:30"   # IST day boundary
    s = board["summary"]
    usable = s["total"] - s["maintenance"] - s["blocked"]
    return {
        "date": today, "beds": s,
        "occupancy_pct": round(100 * s["occupied"] / usable, 1) if usable else None,
        "inpatients": sum(1 for a in active if a["status"] in IN_BED_STATUSES),
        "awaiting_bed": sum(1 for a in active if a["status"] == "pending"),
        "discharge_pending": sum(1 for a in active if a["status"] == "discharge_pending"),
        "expected_discharges_today": sum(1 for a in active if a.get("expected_discharge_on") == today),
        "admitted_today": len(await _rows(adm_q.gte("admitted_at", since))),
        "discharged_today": len(await _rows(disc_q.eq("status", "discharged").gte("discharged_at", since))),
        "wards": [{"id": w["id"], "code": w["code"], "name": w["name"], "ward_type": w["ward_type"],
                   **Counter(b["status"] for b in w["beds"] if b.get("is_active"))} for w in board["wards"]],
    }


async def deactivation_preview(clinic_id: str) -> dict:
    """What an owner disable would leave running (platform toggle, ADR-IPD-07)."""
    active = await _rows(scoped_query("ipd_admissions", clinic_id, "id, status").in_("status", list(ACTIVE_STATUSES)))
    reserved = await _rows(scoped_query("ipd_beds", clinic_id, "id").eq("status", "reserved"))
    proposed = await _rows(scoped_query("ipd_charges", clinic_id, "id").eq("status", "proposed"))
    c = Counter(a["status"] for a in active)
    return {"in_bed": c.get("admitted", 0) + c.get("discharge_pending", 0), "pending": c.get("pending", 0),
            "discharge_pending": c.get("discharge_pending", 0), "reserved_beds": len(reserved),
            "proposed_charges": len(proposed), "blocking": bool(active)}


# ── admission lifecycle (SQL functions) ─────────────────────────────────────

def _p(clinic_id: str, user: Any, **params) -> dict:
    aid, name = _actor(user)
    return {"p_clinic_id": clinic_id, **{f"p_{k}": v for k, v in params.items()},
            "p_actor_id": aid, "p_actor_name": name}


async def admit(clinic_id: str, payload: dict, bed_id: Optional[str], user: Any) -> dict:
    return await rpc("ipd_admit", _p(clinic_id, user, payload=payload, bed_id=bed_id))


async def assign_bed(clinic_id: str, admission_id: str, bed_id: str, user: Any) -> dict:
    return await rpc("ipd_assign_bed", _p(clinic_id, user, admission_id=admission_id, bed_id=bed_id))


async def reserve_bed(clinic_id: str, admission_id: str, bed_id: str, minutes: int, user: Any) -> dict:
    return await rpc("ipd_reserve_bed", _p(clinic_id, user, admission_id=admission_id, bed_id=bed_id, minutes=minutes))


async def release_reservation(clinic_id: str, bed_id: str, user: Any) -> dict:
    return await rpc("ipd_release_reservation", _p(clinic_id, user, bed_id=bed_id))


async def transfer(clinic_id: str, admission_id: str, to_bed_id: str, expected_bed_id: str, reason: str,
                   user: Any) -> dict:
    return await rpc("ipd_transfer", _p(clinic_id, user, admission_id=admission_id, to_bed_id=to_bed_id,
                                        expected_bed_id=expected_bed_id, reason=reason))


async def set_bed_status(clinic_id: str, bed_id: str, status: str, reason: Optional[str], user: Any) -> dict:
    return await rpc("ipd_set_bed_status", _p(clinic_id, user, bed_id=bed_id, status=status, reason=reason))


async def cancel_admission(clinic_id: str, admission_id: str, reason: str, user: Any) -> dict:
    return await rpc("ipd_cancel_admission", _p(clinic_id, user, admission_id=admission_id, reason=reason))


async def initiate_discharge(clinic_id: str, admission_id: str, discharge_type: str, user: Any) -> dict:
    return await rpc("ipd_initiate_discharge", _p(clinic_id, user, admission_id=admission_id,
                                                  discharge_type=discharge_type))


async def revert_discharge(clinic_id: str, admission_id: str, reason: str, user: Any) -> dict:
    return await rpc("ipd_revert_discharge", _p(clinic_id, user, admission_id=admission_id, reason=reason))


async def discharge_checklist(clinic_id: str, admission_id: str) -> dict:
    return await rpc("ipd_discharge_checklist", {"p_clinic_id": clinic_id, "p_admission_id": admission_id})


async def complete_discharge(clinic_id: str, admission_id: str, waiver_reason: Optional[str], user: Any) -> dict:
    try:
        return await rpc("ipd_complete_discharge", _p(clinic_id, user, admission_id=admission_id,
                                                      waiver_reason=waiver_reason))
    except HTTPException as e:
        if isinstance(e.detail, dict) and e.detail.get("code") in ("ipd_discharge_blocked",
                                                                    "ipd_settlement_waiver_required"):
            e.detail["checklist"] = await discharge_checklist(clinic_id, admission_id)
        raise


async def update_admission(clinic_id: str, admission_id: str, patch: dict) -> dict:
    """Administrative fields only; the trigger refuses state fields and closed stays."""
    try:
        res = await sb(supabase.table("ipd_admissions").update(patch).eq("clinic_id", clinic_id).eq("id", admission_id))
    except Exception as e:
        if "ipd_admissions_attending_fk" in str(e):
            raise HTTPException(status_code=404, detail="Doctor not found.")
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=404, detail="Admission not found.")
    return res.data[0]


# ── clinical records ────────────────────────────────────────────────────────

async def _insert(table: str, row: dict, idempotent_on: Optional[str] = None) -> dict:
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table(table).insert(row))
    except Exception as e:
        if idempotent_on and row.get("idempotency_key") and idempotent_on in str(e):
            # a retried click: return what the first attempt wrote
            again = await _rows(scoped_query(table, row["clinic_id"]).eq("idempotency_key", row["idempotency_key"])
                                .limit(1))
            if again:
                return {**again[0], "replayed": True}
        if _code_of(e):
            raise_for(e)
        if "violates check constraint" in str(e):
            raise HTTPException(status_code=422, detail="Some values are outside the allowed range or incomplete.")
        raise_for(e)
    return res.data[0]


async def add_observation(clinic_id: str, admission_id: str, body: dict, user: Any) -> dict:
    aid, name = _actor(user)
    return await _insert("ipd_observations", {**body, "clinic_id": clinic_id, "admission_id": admission_id,
                                              "recorded_by": aid, "recorded_by_name": name})


async def correct_observation(clinic_id: str, observation_id: str, reason: str, replacement: Optional[dict],
                              user: Any) -> dict:
    return await rpc("ipd_correct_observation", _p(clinic_id, user, observation_id=observation_id, reason=reason,
                                                   replacement=replacement))


async def add_note(clinic_id: str, admission_id: str, note_type: str, body: str, user: Any) -> dict:
    aid, name = _actor(user)
    return await _insert("ipd_notes", {"clinic_id": clinic_id, "admission_id": admission_id, "note_type": note_type,
                                       "body": body, "author_id": aid, "author_name": name,
                                       "author_doctor_id": getattr(user, "doctor_id", None)})


async def amend_note(clinic_id: str, note_id: str, body: str, reason: str, user: Any) -> dict:
    return await rpc("ipd_amend_note", _p(clinic_id, user, note_id=note_id, body=body, reason=reason,
                                          author_doctor_id=getattr(user, "doctor_id", None)))


async def add_order(clinic_id: str, admission_id: str, body: dict, user: Any) -> dict:
    aid, name = _actor(user)
    return await _insert("ipd_orders", {**body, "clinic_id": clinic_id, "admission_id": admission_id,
                                        "ordered_by_doctor_id": user.doctor_id, "ordered_by": aid,
                                        "ordered_by_name": name}, idempotent_on="uq_ipd_orders_idempotency")


async def get_order(clinic_id: str, order_id: str) -> dict:
    rows = await _rows(scoped_query("ipd_orders", clinic_id).eq("id", order_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Order not found.")
    return rows[0]


async def end_order(clinic_id: str, order_id: str, status: str, reason: Optional[str], user: Any) -> dict:
    try:
        res = await sb(supabase.table("ipd_orders").update({
            "status": status, "ended_at": datetime.now(timezone.utc).isoformat(), "ended_by": actor_uuid(user),
            "end_reason": reason}).eq("clinic_id", clinic_id).eq("id", order_id).eq("status", "active"))
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=409, detail="Order not found or no longer active.")
    return res.data[0]


async def record_administration(clinic_id: str, admission_id: str, body: dict, user: Any) -> dict:
    aid, name = _actor(user)
    return await _insert("ipd_med_administrations", {**body, "clinic_id": clinic_id, "admission_id": admission_id,
                                                     "recorded_by": aid, "recorded_by_name": name},
                         idempotent_on="uq_ipd_mar_idempotency")


async def mark_administration_error(clinic_id: str, mar_id: str, reason: str, user: Any) -> dict:
    try:
        res = await sb(supabase.table("ipd_med_administrations").update({
            "status": "entered_in_error", "error_reason": reason, "errored_at": datetime.now(timezone.utc).isoformat(),
            "errored_by": actor_uuid(user)}).eq("clinic_id", clinic_id).eq("id", mar_id).eq("status", "recorded"))
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=409, detail="Entry not found or already marked.")
    return res.data[0]


async def save_summary_draft(clinic_id: str, admission_id: str, fields: dict, user: Any,
                             expected_updated_at: Optional[str] = None) -> dict:
    """Create or update THE draft. A draft after a signed version becomes the
    next version (amendment); signing it supersedes the old one."""
    aid, name = _actor(user)
    drafts = await _rows(scoped_query("ipd_discharge_summaries", clinic_id).eq("admission_id", admission_id)
                         .eq("status", "draft").limit(1))
    if drafts:
        q = supabase.table("ipd_discharge_summaries").update({**fields, "prepared_by": aid, "prepared_by_name": name}) \
            .eq("clinic_id", clinic_id).eq("id", drafts[0]["id"]).eq("status", "draft")
        if expected_updated_at:
            q = q.eq("updated_at", expected_updated_at)
        res = await sb(q)
        if not res.data:
            raise HTTPException(status_code=409, detail="Someone else saved this summary. Reload to see their changes.")
        return res.data[0]
    signed = await _rows(scoped_query("ipd_discharge_summaries", clinic_id).eq("admission_id", admission_id)
                         .neq("status", "draft").order("version", desc=True).limit(1))
    row = {**fields, "clinic_id": clinic_id, "admission_id": admission_id, "prepared_by": aid, "prepared_by_name": name}
    if signed:
        if not fields.get("amend_reason"):
            raise HTTPException(status_code=422, detail="An amendment reason is required to change a signed summary.")
        row.update({"version": signed[0]["version"] + 1, "supersedes_id": signed[0]["id"]})
    else:
        row.pop("amend_reason", None)
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("ipd_discharge_summaries").insert(row))
    except Exception as e:
        if "uq_ipd_ds_one_draft" in str(e):
            raise HTTPException(status_code=409, detail="A draft was just created by someone else. Reload.")
        raise_for(e)
    return res.data[0]


async def sign_summary(clinic_id: str, summary_id: str, user: Any) -> dict:
    return await rpc("ipd_sign_discharge_summary", _p(clinic_id, user, summary_id=summary_id,
                                                      doctor_id=user.doctor_id))


# ── charges + final bill ────────────────────────────────────────────────────

async def add_charge(clinic_id: str, admission_id: str, body: dict, user: Any) -> dict:
    aid, name = _actor(user)
    row = {**body, "clinic_id": clinic_id, "admission_id": admission_id, "source": "manual",
           "created_by": aid, "created_by_name": name}
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("ipd_charges").insert(row))
    except Exception as e:
        if "uq_ipd_charges_source_ref" in str(e):   # retried submit with the same client reference
            again = await _rows(scoped_query("ipd_charges", clinic_id).eq("admission_id", admission_id)
                                .eq("source_ref", body.get("source_ref")).neq("status", "void").limit(1))
            if again:
                return {**again[0], "replayed": True}
        raise_for(e)
    return res.data[0]


async def accrue_bed_charges(clinic_id: str, admission_id: str, user: Any) -> dict:
    return await rpc("ipd_accrue_bed_charges", _p(clinic_id, user, admission_id=admission_id))


async def post_charges(clinic_id: str, admission_id: str, charge_ids: list[str], user: Any) -> dict:
    return await rpc("ipd_post_charges", _p(clinic_id, user, admission_id=admission_id, charge_ids=charge_ids))


async def void_charge(clinic_id: str, charge_id: str, reason: str, user: Any) -> dict:
    return await rpc("ipd_void_charge", _p(clinic_id, user, charge_id=charge_id, reason=reason))


async def generate_final_bill(clinic_id: str, admission_id: str, user: Any) -> dict:
    return await rpc("ipd_generate_final_bill", _p(clinic_id, user, admission_id=admission_id))


# ── worklist (derived; no separate table) ───────────────────────────────────

async def worklist(clinic_id: str, branch_id: Optional[str] = None) -> dict:
    """What needs a human now: placements, discharges in progress, beds to
    clean, active orders. Derived from the source tables, so it can never
    disagree with them."""
    q = scoped_query("ipd_admissions", clinic_id).in_("status", list(ACTIVE_STATUSES))
    if branch_id:
        q = q.eq("branch_id", branch_id)
    active = await _rows(q)
    by_id = {a["id"]: a for a in active}
    people = await _people(clinic_id, active)
    in_bed = [a["id"] for a in active if a["status"] in IN_BED_STATUSES]
    orders = await _rows(scoped_query("ipd_orders", clinic_id, "id, admission_id, order_type, description, frequency, "
                                      "priority, is_prn, ordered_at").in_("admission_id", in_bed)
                         .eq("status", "active")) if in_bed else []
    cq = scoped_query("ipd_beds", clinic_id, "id, code, ward_id, status_reason, updated_at").eq("status", "cleaning")
    if branch_id:   # a branch-pinned user sees only their branch's beds
        wards = [w["id"] for w in await _rows(scoped_query("ipd_wards", clinic_id, "id").eq("branch_id", branch_id))]
        cq = cq.in_("ward_id", wards) if wards else None
    cleaning = await _rows(cq) if cq is not None else []

    def label(a: dict) -> dict:
        who = people.get(a["id"], {})
        return {"admission_id": a["id"], "admission_number": a.get("admission_number"),
                "patient_name": who.get("patient_name"), "mrn": who.get("mrn")}

    return {
        "awaiting_bed": [{**label(a), "priority": a.get("priority"), "since": a.get("created_at")}
                         for a in active if a["status"] == "pending"],
        "discharge_pending": [{**label(a), "discharge_type": a.get("discharge_type"),
                               "since": a.get("discharge_initiated_at")}
                              for a in active if a["status"] == "discharge_pending"],
        "beds_to_clean": cleaning,
        "active_orders": [{**o, **label(by_id[o["admission_id"]])} for o in orders if o["admission_id"] in by_id],
    }


def admission_payload(body: dict, branch_id: Optional[str]) -> dict:
    """Map the API model to the ipd_admit payload (dates as ISO strings)."""
    out = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in body.items() if v is not None}
    out.pop("bed_id", None)
    if branch_id:
        out["branch_id"] = branch_id
    return out
