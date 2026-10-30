"""Operation Theatre OS (G8, migration 122).

Every state change is one SQL function, so each step happens completely or not at all:

  ot_schedule_surgery    book an admitted patient into a theatre (OT-YYYY-NNNNN); the database
                         refuses an overlapping booking of the theatre or the patient; creates
                         the empty WHO checklist
  ot_update_checklist    WHO Sign-In -> Time-Out -> Sign-Out, strictly in order; a phase
                         completes (and locks) only when every item is ticked
  ot_record_anaesthesia  the anaesthesia chart, editable until the anaesthetist signs it
  ot_set_status          wheel in (start), recovery, cancel / postpone with a reason
  ot_complete_surgery    refused until all three checklist phases are complete; saves the
                         surgeon's post-op note and posts exactly one IPD charge

Zero-LLM: imports nothing from the AI, voice or assistant packages.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.database import fetch_all, restrict_to_branch, sb, scoped_query, supabase
from app.services import ipd
from app.services.clinical_documents import doctor_snapshot
from app.utils.helpers import IST, actor_uuid

logger = logging.getLogger(__name__)

ACTIVE = ("scheduled", "in_theatre", "recovery")
PHASES = ("sign_in", "time_out", "sign_out")

_ERRORS: dict[str, tuple[int, str]] = {
    "ot_surgery_not_found": (404, "Surgery not found."),
    "ot_theatre_not_found": (404, "Theatre not found."),
    "ot_theatre_inactive": (409, "This theatre is switched off. Pick another theatre."),
    "ot_theatre_other_branch": (409, "This theatre belongs to another branch than the patient's admission."),
    "ot_theatre_busy": (409, "The theatre is already booked for part of that time. Pick another time or theatre."),
    "ot_patient_busy": (409, "This patient already has a surgery booked for part of that time."),
    "ot_admission_not_active": (409, "The patient is not currently admitted."),
    "ot_service_not_found": (404, "Service not found or inactive."),
    "ot_key_reused": (409, "This request key was used for another surgery. Reload and try again."),
    "ot_surgery_closed": (409, "This surgery is closed and can no longer change."),
    "ot_checklist_bad_phase": (422, "Unknown checklist phase."),
    "ot_checklist_bad_item": (422, "Unknown checklist item for this phase."),
    "ot_checklist_order": (409, "Complete the checklist phases in order: Sign-In, then Time-Out, then Sign-Out."),
    "ot_checklist_incomplete": (409, "Complete all three WHO checklist phases (Sign-In, Time-Out, Sign-Out) "
                                     "before finishing the surgery."),
    "ot_anaesthesia_signed": (409, "The anaesthesia record is signed and can no longer change."),
    "ot_anaesthesia_incomplete": (422, "Record the ASA status and the technique before signing."),
    "ot_doctor_required": (403, "Only a login linked to a doctor can sign the anaesthesia record."),
    "ot_bad_field": (422, "Unknown anaesthesia field."),
    "ot_bad_action": (422, "Unknown status change."),
    "ot_bad_transition": (409, "That step is not possible from the surgery's current status."),
    "ot_reason_required": (422, "Give a reason (at least 3 characters)."),
    "ot_note_required": (422, "Write the procedure notes before finishing the surgery."),
    "ot_final_bill_exists": (409, "The stay's final bill is already generated. Void it in OPD Billing first, "
                                  "so the surgery charge lands on the bill."),
    "ot_charge_amount_required": (422, "Enter the surgery charge, or link the surgery to a service with a price."),
    "ot_charge_amount_invalid": (422, "The surgery charge is out of range."),
    "ot_append_only": (409, "Operation theatre records cannot be deleted."),
    "ot_via_function_only": (409, "This change has to go through its own action."),
    "ot_identity_immutable": (409, "The surgery's identity cannot change."),
}
# constraint name in a database error -> what the person did wrong
_CONSTRAINTS: dict[str, tuple[int, str]] = {
    "ot_surgeries_surgeon_fk": (404, "Surgeon not found."),
    "ot_surgeries_anaesthetist_fk": (404, "Anaesthetist not found."),
    "ot_anaesthesia_records_doctor_fk": (404, "Anaesthetist not found."),
    "ot_surgeries_times": (422, "The surgery must end after it starts, within 24 hours."),
    "ot_theatres_name_key": (409, "A theatre with this name already exists in this branch."),
    "ot_theatres_branch_fk": (404, "Branch not found."),
    "uq_ot_surgeries_idempotency": (409, "This request key was used for another surgery. Reload and try again."),
}


def raise_for(exc: Exception) -> None:
    """ot_* codes and known constraints here; ipd_* and anything else through the IPD
    mapper, which never returns raw database text."""
    if isinstance(exc, HTTPException):
        raise exc
    msg = str(getattr(exc, "message", None) or exc)
    for code in sorted(_ERRORS, key=len, reverse=True):
        if code in msg:
            status, message = _ERRORS[code]
            raise HTTPException(status_code=status, detail={"code": code, "message": message})
    for name, (status, message) in _CONSTRAINTS.items():
        if name in msg:
            raise HTTPException(status_code=status, detail={"code": name, "message": message})
    ipd.raise_for(exc)


async def _rpc(name: str, params: dict) -> Any:
    try:
        res = await sb(supabase.rpc(name, params))
    except Exception as e:  # postgrest APIError carries the ot_* / ipd_* code
        raise_for(e)
    return res.data


def _actor(user: Any) -> tuple[Optional[str], str]:
    return actor_uuid(user), str(getattr(user, "username", None) or "system")[:100]


# ── theatres ────────────────────────────────────────────────────────────────

async def list_theatres(clinic_id: str, branch_id: Optional[str] = None, active_only: bool = False) -> list[dict]:
    def q():
        base = scoped_query("ot_theatres", clinic_id,
                            "id, branch_id, name, theatre_type, is_active, created_at, updated_at")
        if active_only:
            base = base.eq("is_active", True)
        return restrict_to_branch(base, branch_id).order("name").order("id")
    return await fetch_all(q, label="ot:theatres")


async def get_theatre(clinic_id: str, theatre_id: str) -> dict:
    res = await sb(scoped_query("ot_theatres", clinic_id).eq("id", theatre_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Theatre not found.")
    return res.data[0]


async def save_theatre(clinic_id: str, data: dict, theatre_id: Optional[str] = None) -> dict:
    try:
        if theatre_id:
            res = await sb(supabase.table("ot_theatres").update(data).eq("clinic_id", clinic_id).eq("id", theatre_id))
        else:
            # unscoped: insert_scoped_by_payload
            res = await sb(supabase.table("ot_theatres").insert({**data, "clinic_id": clinic_id}))
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=404, detail="Theatre not found.")
    return res.data[0]


# ── schedule ────────────────────────────────────────────────────────────────

async def get_surgery(clinic_id: str, surgery_id: str) -> dict:
    res = await sb(scoped_query("ot_surgeries", clinic_id).eq("id", surgery_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Surgery not found.")
    return res.data[0]


async def schedule_surgery(clinic_id: str, data: dict, actor: Any) -> dict:
    aid, name = _actor(actor)
    return await _rpc("ot_schedule_surgery", {"p_clinic_id": clinic_id, "p_payload": data,
                                              "p_actor_id": aid, "p_actor_name": name})


async def _enrich(clinic_id: str, rows: list[dict]) -> list[dict]:
    """Patient (name, MRN), admission number, theatre, surgeon / anaesthetist names and
    checklist progress for each surgery. No contact data."""
    if not rows:
        return []
    adm_ids = sorted({r["admission_id"] for r in rows})
    admissions = await ipd._rows_in(lambda: scoped_query(
        "ipd_admissions", clinic_id, "id, admission_number, patient_id, family_member_id, current_bed_id"),
        "id", adm_ids)
    people = await ipd._people(clinic_id, admissions)
    adm = {a["id"]: a for a in admissions}
    doctors = await ipd._doctor_names(clinic_id, {r.get("primary_surgeon_id") for r in rows}
                                      | {r.get("anaesthetist_id") for r in rows})
    theatres = {t["id"]: t for t in await ipd._rows_in(
        lambda: scoped_query("ot_theatres", clinic_id, "id, name, theatre_type"), "id",
        sorted({r["theatre_id"] for r in rows}))}
    checklists = {c["surgery_id"]: c for c in await ipd._rows_in(lambda: scoped_query(
        "ot_who_checklists", clinic_id, "surgery_id, sign_in_completed, time_out_completed, sign_out_completed"),
        "surgery_id", [r["id"] for r in rows])}
    out = []
    for r in rows:
        c = checklists.get(r["id"], {})
        out.append({**r, **people.get(r["admission_id"], {}),
                    "admission_number": adm.get(r["admission_id"], {}).get("admission_number"),
                    "theatre_name": theatres.get(r["theatre_id"], {}).get("name"),
                    "surgeon_name": doctors.get(r.get("primary_surgeon_id")),
                    "anaesthetist_name": doctors.get(r.get("anaesthetist_id")),
                    "checklist": {p: bool(c.get(f"{p}_completed")) for p in PHASES}})
    return out


async def day_schedule(clinic_id: str, day: date, branch_id: Optional[str] = None) -> dict:
    """The day's theatres and the surgeries starting that day (IST), by theatre and time."""
    start = datetime.combine(day, time.min, tzinfo=IST)
    lo, hi = start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()

    def q():
        base = scoped_query("ot_surgeries", clinic_id).gte("scheduled_start", lo).lt("scheduled_start", hi)
        return restrict_to_branch(base, branch_id).order("scheduled_start").order("id")

    rows = await _enrich(clinic_id, await fetch_all(q, label="ot:schedule"))
    theatres = await list_theatres(clinic_id, branch_id)
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in
              ("scheduled", "in_theatre", "recovery", "completed", "cancelled", "postponed")}
    return {"date": day.isoformat(), "theatres": theatres, "surgeries": rows, "counts": counts,
            "active": sum(1 for r in rows if r["status"] in ACTIVE)}


async def surgery_detail(clinic_id: str, surgery_id: str) -> dict:
    s = await get_surgery(clinic_id, surgery_id)
    row = (await _enrich(clinic_id, [s]))[0]
    checklist = await sb(scoped_query("ot_who_checklists", clinic_id).eq("surgery_id", surgery_id).limit(1))
    anaes = await sb(scoped_query("ot_anaesthesia_records", clinic_id).eq("surgery_id", surgery_id).limit(1))
    charge = None
    if s.get("charge_id"):
        res = await sb(scoped_query("ipd_charges", clinic_id,
                                    "id, description, unit_price_paise, total_paise, status, posted_at, invoice_id")
                       .eq("id", s["charge_id"]).limit(1))
        charge = (res.data or [None])[0]
    service = None
    if s.get("service_id"):   # the price completion will post, unless the surgeon enters another
        res = await sb(scoped_query("billable_services", clinic_id, "id, service_code, name, standard_price_paise")
                       .eq("id", s["service_id"]).limit(1))
        service = (res.data or [None])[0]
    return {**row, "who_checklist": (checklist.data or [None])[0], "anaesthesia": (anaes.data or [None])[0],
            "charge": charge, "service": service}


# ── checklist, anaesthesia, status, completion ─────────────────────────────

async def update_who_checklist(clinic_id: str, surgery_id: str, phase: str, items: dict, actor: Any) -> dict:
    aid, name = _actor(actor)
    return await _rpc("ot_update_checklist", {"p_clinic_id": clinic_id, "p_surgery_id": surgery_id,
                                              "p_phase": phase, "p_items": items,
                                              "p_actor_id": aid, "p_actor_name": name})


async def record_anaesthesia(clinic_id: str, surgery_id: str, data: dict, actor: Any, sign: bool = False) -> dict:
    """Save the chart; `sign` locks it in the acting anaesthetist's name (a doctor login
    with a registration number)."""
    signer = await doctor_snapshot(clinic_id, getattr(actor, "doctor_id", None)) if sign else None
    return await _rpc("ot_record_anaesthesia", {
        "p_clinic_id": clinic_id, "p_surgery_id": surgery_id, "p_payload": data, "p_sign": sign,
        "p_doctor_id": signer["doctor_id"] if signer else None, "p_doctor_snapshot": signer,
        "p_actor_id": actor_uuid(actor)})


async def set_status(clinic_id: str, surgery_id: str, action: str, reason: Optional[str], actor: Any) -> dict:
    aid, name = _actor(actor)
    return await _rpc("ot_set_status", {"p_clinic_id": clinic_id, "p_surgery_id": surgery_id, "p_action": action,
                                        "p_reason": reason, "p_actor_id": aid, "p_actor_name": name})


async def complete_surgery(clinic_id: str, surgery_id: str, note: dict, amount_paise: Optional[int],
                           actor: Any) -> dict:
    aid, name = _actor(actor)
    return await _rpc("ot_complete_surgery", {"p_clinic_id": clinic_id, "p_surgery_id": surgery_id,
                                              "p_note": note, "p_amount_paise": amount_paise,
                                              "p_actor_id": aid, "p_actor_name": name})
