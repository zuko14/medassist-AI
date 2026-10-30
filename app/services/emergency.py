"""Emergency / Casualty OS (G7, migration 121).

Every state change is one SQL function, so a registration, an MLC, a
disposition or an identity merge either happens completely or not at all:

  emergency_register       30-second registration; an unidentified patient gets a
                           temporary record ("Unknown Male ~40y") and EMG-YYYY-NNNNN
  emergency_record_mlc     medico-legal case / police intimation, MLC-YYYY-NNNNN,
                           frozen once the doctor locks it
  emergency_disposition    admit to IPD (ipd_admit in the same transaction), transfer,
                           discharge, LAMA (needs the G9 refusal consent), death
  emergency_merge_patient  moves everything recorded against the temporary identity
                           to the real patient, with a provenance log

Zero-LLM: imports nothing from the AI, voice or assistant packages.
"""

import logging
from datetime import datetime, time, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.database import fetch_all, restrict_to_branch, sb, scoped_query, supabase
from app.services import ipd
from app.services.clinical_documents import doctor_snapshot
from app.services.lis import letterhead_snapshot
from app.utils.helpers import IST, actor_uuid, today_ist

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("triage", "under_treatment")
CLOSED_STATUSES = ("admitted_ipd", "transferred", "discharged", "lama", "deceased")
_TRIAGE_RANK = {"red": 0, "yellow": 1, "green": 2, "black": 3}

_ERRORS: dict[str, tuple[int, str]] = {
    "emergency_visit_not_found": (404, "Emergency visit not found."),
    "emergency_patient_not_found": (404, "Patient not found."),
    "emergency_patient_merged": (409, "This record was merged into another patient. Use that patient."),
    "emergency_identity_required": (422, "Give the patient's name and phone, pick an existing patient, or register "
                                         "as unknown."),
    "emergency_visit_open": (409, "This patient already has an open emergency visit."),
    "emergency_visit_closed": (409, "This emergency visit is already closed."),
    "emergency_via_function_only": (409, "This change has to go through its own action (triage, MLC, disposition)."),
    "emergency_identity_immutable": (409, "The visit's identity cannot change."),
    "emergency_append_only": (409, "Emergency records cannot be deleted."),
    "emergency_mlc_locked": (409, "This MLC is locked. It is a statutory record and cannot be changed."),
    "emergency_transfer_to_required": (422, "Name the hospital the patient is transferred to."),
    "emergency_lama_consent_missing": (409, "Record the signed refusal (LAMA) consent first."),
    "emergency_doctor_required": (403, "Only a login linked to a doctor can declare death."),
    "emergency_bad_time_of_death": (422, "The time of death is outside this visit."),
    "emergency_bad_disposition": (422, "Unknown disposition."),
    "emergency_merge_same": (422, "Pick a different patient to merge into."),
    "emergency_merge_not_temporary": (409, "Only a temporary (unknown) emergency record can be merged."),
    "emergency_merge_bad_target": (409, "Merge into a registered patient, not another temporary record."),
    "emergency_merge_conflict": (409, "Both records have an open admission or ER visit. Close one first."),
}


def raise_for(exc: Exception) -> None:
    """emergency_* codes here; ipd_* (from the IPD admission) and anything else
    through the IPD mapper, which never returns raw database text."""
    if isinstance(exc, HTTPException):
        raise exc
    msg = str(getattr(exc, "message", None) or exc)
    for code in sorted(_ERRORS, key=len, reverse=True):
        if code in msg:
            status, message = _ERRORS[code]
            raise HTTPException(status_code=status, detail={"code": code, "message": message})
    ipd.raise_for(exc)


async def _rpc(name: str, params: dict) -> Any:
    try:
        res = await sb(supabase.rpc(name, params))
    except Exception as e:  # postgrest APIError carries the emergency_* / ipd_* code
        raise_for(e)
    return res.data


def _actor(user: Any) -> tuple[Optional[str], str]:
    return actor_uuid(user), str(getattr(user, "username", None) or "system")[:100]


async def get_visit(clinic_id: str, visit_id: str) -> dict:
    res = await sb(scoped_query("emergency_visits", clinic_id).eq("id", visit_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Emergency visit not found.")
    return res.data[0]


# ── registration & board ────────────────────────────────────────────────────

async def rapid_register_emergency(clinic_id: str, data: dict, actor: Any) -> dict:
    aid, name = _actor(actor)
    return await _rpc("emergency_register", {"p_clinic_id": clinic_id, "p_payload": data,
                                             "p_actor_id": aid, "p_actor_name": name})


async def update_visit(clinic_id: str, visit_id: str, data: dict) -> dict:
    """Triage, doctor, notes, vitals while the patient is on the floor. A doctor
    taking the patient moves triage -> under_treatment."""
    row = dict(data)
    if "triage_category" in row:
        row["triaged_at"] = datetime.now(timezone.utc).isoformat()
    if row.get("attending_doctor_id"):
        row["status"] = "under_treatment"
    q = supabase.table("emergency_visits").update(row).eq("clinic_id", clinic_id).eq("id", visit_id) \
        .in_("status", list(OPEN_STATUSES))
    try:
        res = await sb(q)
    except Exception as e:
        if "emergency_visits_doctor_fk" in str(e):
            raise HTTPException(status_code=404, detail="Doctor not found.")
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=409, detail={"code": "emergency_visit_closed",
                                                     "message": _ERRORS["emergency_visit_closed"][1]})
    return res.data[0]


async def casualty_board(clinic_id: str, branch_id: Optional[str] = None, triage: Optional[str] = None,
                         include_closed_today: bool = False) -> dict:
    """Open ER visits, RED first then oldest; optionally today's (IST) closed
    ones. Counts per triage category cover the open visits."""
    since = datetime.combine(today_ist(), time.min, tzinfo=IST).astimezone(timezone.utc).isoformat()

    def build(open_only: bool):
        def q():
            base = scoped_query("emergency_visits", clinic_id)
            base = base.in_("status", list(OPEN_STATUSES)) if open_only else \
                base.in_("status", list(CLOSED_STATUSES)).gte("disposition_at", since)
            return restrict_to_branch(base, branch_id).order("created_at").order("id")
        return q

    rows = await fetch_all(build(True), label="emergency:open")
    closed = await fetch_all(build(False), label="emergency:closed") if include_closed_today else []
    counts = {k: sum(1 for r in rows if r["triage_category"] == k) for k in _TRIAGE_RANK}
    visible = [r for r in rows + closed if not triage or r["triage_category"] == triage]
    visible.sort(key=lambda r: (r["status"] not in OPEN_STATUSES, _TRIAGE_RANK[r["triage_category"]],
                                r.get("created_at") or "", r["id"]))
    ids = lambda col: sorted({str(r[col]) for r in visible if r.get(col)})  # noqa: E731
    patients = {p["id"]: p for p in await _in("patients", clinic_id, "id, name, mrn, gender, age_years, is_temporary",
                                               ids("patient_id"))}
    doctors = {d["id"]: d for d in await _in("doctors", clinic_id, "id, name", ids("attending_doctor_id"))}
    mlcs = {m["emergency_visit_id"]: m for m in await _in("emergency_mlc_records", clinic_id,
                                                          "id, emergency_visit_id, mlc_number, is_locked",
                                                          [r["id"] for r in visible], column="emergency_visit_id")}
    out = []
    for r in visible:
        p = patients.get(r["patient_id"], {})
        m = mlcs.get(r["id"])
        out.append({**r, "patient_name": p.get("name"), "mrn": p.get("mrn"), "gender": p.get("gender"),
                    "age_years": p.get("age_years"), "is_temporary": bool(p.get("is_temporary")),
                    "doctor_name": doctors.get(r.get("attending_doctor_id"), {}).get("name"),
                    "mlc": {"id": m["id"], "mlc_number": m["mlc_number"], "is_locked": m["is_locked"]} if m else None})
    return {"visits": out, "counts": counts, "open": len(rows)}


async def _in(table: str, clinic_id: str, cols: str, ids: list, column: str = "id") -> list[dict]:
    out: list[dict] = []
    ids = list(ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        out += await fetch_all(lambda c=chunk: scoped_query(table, clinic_id, cols).in_(column, c).order("id"),
                               label=f"emergency:{table}")
    return out


# ── MLC ─────────────────────────────────────────────────────────────────────

async def record_mlc(clinic_id: str, visit_id: str, data: dict, actor: Any, lock: bool = False) -> dict:
    """Record (or correct, until locked) the medico-legal case in the acting
    doctor's name; `lock` freezes it for good."""
    signer = await doctor_snapshot(clinic_id, getattr(actor, "doctor_id", None))
    visit = await get_visit(clinic_id, visit_id)
    return await _rpc("emergency_record_mlc", {
        "p_clinic_id": clinic_id, "p_visit_id": visit_id, "p_payload": data, "p_doctor_id": signer["doctor_id"],
        "p_doctor_snapshot": signer, "p_letterhead_snapshot": await letterhead_snapshot(clinic_id, visit.get("branch_id")),
        "p_lock": lock, "p_actor_id": actor_uuid(actor),
    })


async def get_mlc(clinic_id: str, visit_id: str) -> dict:
    res = await sb(scoped_query("emergency_mlc_records", clinic_id).eq("emergency_visit_id", visit_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="No MLC recorded for this visit.")
    return res.data[0]


# ── disposition & merge ─────────────────────────────────────────────────────

async def disposition_emergency(clinic_id: str, visit_id: str, action: str, details: dict, actor: Any) -> dict:
    aid, name = _actor(actor)
    return await _rpc("emergency_disposition", {
        "p_clinic_id": clinic_id, "p_visit_id": visit_id, "p_action": action, "p_payload": details,
        "p_doctor_id": getattr(actor, "doctor_id", None), "p_actor_id": aid, "p_actor_name": name})


async def merge_unknown_patient(clinic_id: str, temp_patient_id: str, target_patient_id: str, actor: Any,
                                reason: Optional[str] = None) -> dict:
    aid, name = _actor(actor)
    return await _rpc("emergency_merge_patient", {
        "p_clinic_id": clinic_id, "p_temp_patient_id": temp_patient_id, "p_target_patient_id": target_patient_id,
        "p_actor_id": aid, "p_actor_name": name, "p_reason": reason})


async def search_patients(clinic_id: str, q: str, limit: int = 15) -> list[dict]:
    """Registered (not temporary, not merged) patients by name, phone or MRN,
    for the identify-and-merge search."""
    q = " ".join(q.split())[:60]
    if len(q) < 2:
        return []
    pattern = "*" + q.replace("*", "").replace(",", " ").replace("(", " ").replace(")", " ") + "*"
    res = await sb(scoped_query("patients", clinic_id, "id, name, phone, mrn, gender, age_years")
                   .eq("is_temporary", False).is_("merged_into_patient_id", "null")
                   .or_(f"name.ilike.{pattern},phone.ilike.{pattern},mrn.ilike.{pattern}")
                   .order("name").limit(limit))
    return list(res.data or [])
