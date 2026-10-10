"""Kriya OPD OS core service (Phase 1).

Registry, walk-ins, live queue, TV display, and setup wizard orchestration.
Zero-LLM clinical safety compliant.
"""

import asyncio
import hashlib
import json
import logging
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException
from postgrest.types import CountMethod

from app.database import (
    check_in_appointment,
    get_doctor_by_name,
    sb,
    scoped_query,
    supabase,
)
from app.services.tenant import clinic_letterhead, get_clinic_by_id, has_feature
from app.utils.helpers import IST, doctor_title, generate_booking_reference, today_ist

logger = logging.getLogger(__name__)

STAGES = (
    "registered",
    "vitals_pending",
    "waiting",
    "in_consultation",
    "billing",
    "completed",
    "cancelled",
    "done",  # legacy support
)

ACTIVE_QUEUE_STAGES = (
    "registered",
    "vitals_pending",
    "waiting",
    "in_consultation",
    "billing",
)

ALLOWED_TRANSITIONS = {
    "registered": {"vitals_pending", "waiting", "cancelled"},
    "vitals_pending": {"waiting", "cancelled"},
    "waiting": {"in_consultation", "vitals_pending", "cancelled"},
    "in_consultation": {"billing", "completed", "waiting"},
    "billing": {"completed"},
    "completed": set(),
    "cancelled": set(),
    "done": set(),
}

DEFAULT_SETTINGS: dict = {
    "vitals_required": True,
    "after_checkin_stage": "vitals_pending",
    "billing_after_consult": True,
    "token_rule": "per_doctor_daily",
    "payment_modes": ["cash", "upi"],
    "auto_open_shift": True,
    "rooms": {},
    "templates": {},
    "service_catalog": [],
    "confirmed_steps": [],
}


def _coerce_opd_count(res: Any) -> int:
    """Extract integer count safely handling mock or PostgREST responses."""
    cnt = getattr(res, "count", None)
    if isinstance(cnt, int):
        return cnt
    data = getattr(res, "data", None)
    if isinstance(data, list):
        return len(data)
    return 0


def _normalize_phone(phone: str) -> str:
    """Normalize phone to standard format (digits or +E.164)."""
    p = re.sub(r"[^\d+]", "", phone.strip())
    if not p.startswith("+") and len(p) == 10:
        p = f"+91{p}"
    return p


def _compute_age(
    dob: Optional[Any],
    age_years: Optional[int],
    age_recorded_on: Optional[Any],
) -> Optional[int]:
    """Compute current age from date_of_birth or baseline recorded age."""
    today = today_ist()
    if dob:
        if isinstance(dob, str):
            try:
                dob = datetime.strptime(dob[:10], "%Y-%m-%d").date()
            except ValueError:
                dob = None
        if isinstance(dob, (date, datetime)):
            dob_date = dob if isinstance(dob, date) else dob.date()
            years = today.year - dob_date.year - (
                (today.month, today.day) < (dob_date.month, dob_date.day)
            )
            return max(0, years)
    if age_years is not None:
        if age_recorded_on:
            if isinstance(age_recorded_on, str):
                try:
                    age_recorded_on = datetime.strptime(
                        age_recorded_on[:10], "%Y-%m-%d"
                    ).date()
                except ValueError:
                    age_recorded_on = None
            if isinstance(age_recorded_on, (date, datetime)):
                rec_date = (
                    age_recorded_on
                    if isinstance(age_recorded_on, date)
                    else age_recorded_on.date()
                )
                diff = max(0, (today - rec_date).days // 365)
                return age_years + diff
        return age_years
    return None


async def provision_defaults(clinic_id: str) -> dict:
    """Provision default settings for OPD OS if not already set.

    Existing keys win — never overwrite existing configurations.
    Uses CAS on opd_settings with up to 3 retries.
    """
    merged = dict(DEFAULT_SETTINGS)
    for _ in range(3):
        res = await sb(
            supabase.table("clinics")
            .select("opd_settings")
            .eq("id", clinic_id)
            .limit(1)
        )
        if not res.data:
            return DEFAULT_SETTINGS
        existing = res.data[0].get("opd_settings") or {}
        merged = {**DEFAULT_SETTINGS, **existing}
        update_res = await sb(
            supabase.table("clinics")
            .update({"opd_settings": merged})
            .eq("id", clinic_id)
            # postgrest-py str()s filter values; a dict would go out as a
            # Python repr ({'k': True}) that Postgres rejects as invalid JSON.
            .eq("opd_settings", json.dumps(existing))
        )
        if update_res.data:
            return merged
    return merged


async def deactivation_preview(clinic_id: str) -> dict:
    """Inspect clinical and operational state to preview OPD deactivation impact."""
    today_str = today_ist().isoformat()

    active_q_res = await sb(
        supabase.table("appointments")
        .select("id", count=CountMethod.exact)
        .eq("clinic_id", clinic_id)
        .eq("appointment_date", today_str)
        .in_("queue_status", list(ACTIVE_QUEUE_STAGES))
    )
    active_queue = _coerce_opd_count(active_q_res)

    draft_enc_res = await sb(
        supabase.table("opd_encounters")
        .select("id", count=CountMethod.exact)
        .eq("clinic_id", clinic_id)
        .eq("status", "draft")
    )
    draft_encounters = _coerce_opd_count(draft_enc_res)

    draft_rx_res = await sb(
        supabase.table("opd_prescriptions")
        .select("id", count=CountMethod.exact)
        .eq("clinic_id", clinic_id)
        .eq("status", "draft")
    )
    draft_prescriptions = _coerce_opd_count(draft_rx_res)

    open_shifts_res = await sb(
        supabase.table("opd_cashier_shifts")
        .select("id", count=CountMethod.exact)
        .eq("clinic_id", clinic_id)
        .is_("closed_at", "null")
    )
    open_shifts = _coerce_opd_count(open_shifts_res)

    unpaid_inv_res = await sb(
        scoped_query("opd_invoices", clinic_id)
        .select("id, total_paise, paid_paise")
        .in_("status", ["issued", "partially_paid"])
    )
    unpaid_rows = unpaid_inv_res.data or []
    unpaid_count = len(unpaid_rows)
    unpaid_amount = sum(
        max(0, (r.get("total_paise") or 0) - (r.get("paid_paise") or 0))
        for r in unpaid_rows
    )

    enc_ret_res = await sb(
        supabase.table("opd_encounters")
        .select("id", count=CountMethod.exact)
        .eq("clinic_id", clinic_id)
        .in_("status", ["signed", "superseded"])
    )
    rx_ret_res = await sb(
        supabase.table("opd_prescriptions")
        .select("id", count=CountMethod.exact)
        .eq("clinic_id", clinic_id)
        .in_("status", ["signed", "superseded"])
    )
    inv_ret_res = await sb(
        supabase.table("opd_invoices")
        .select("id", count=CountMethod.exact)
        .eq("clinic_id", clinic_id)
        .in_("status", ["paid", "cancelled", "void"])
    )
    rcpt_ret_res = await sb(
        supabase.table("opd_receipts")
        .select("id", count=CountMethod.exact)
        .eq("clinic_id", clinic_id)
    )

    retained = {
        "encounters": _coerce_opd_count(enc_ret_res),
        "prescriptions": _coerce_opd_count(rx_ret_res),
        "invoices": _coerce_opd_count(inv_ret_res),
        "receipts": _coerce_opd_count(rcpt_ret_res),
    }

    blocking = bool(
        active_queue > 0
        or draft_encounters > 0
        or draft_prescriptions > 0
        or open_shifts > 0
        or unpaid_count > 0
    )

    return {
        "active_queue": active_queue,
        "todays_open_visits": active_queue,
        "unpaid_invoices": {
            "count": unpaid_count,
            "amount_paise": unpaid_amount,
        },
        "draft_encounters": draft_encounters,
        "draft_prescriptions": draft_prescriptions,
        "open_shifts": open_shifts,
        "retained": retained,
        "blocking": blocking,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 1. REGISTRY & PATIENTS
# ═══════════════════════════════════════════════════════════════════════════════


async def search_patients(
    clinic_id: str,
    q: str,
    limit: int = 20,
) -> list[dict]:
    """Search patients across account holders, family members, and legacy records."""
    cleaned = q.strip()
    if len(cleaned) < 2:
        return []

    hits: list[dict] = []
    seen: set[tuple[str, Optional[str]]] = set()

    # 1. Search account holders in `patients`
    pat_query = scoped_query("patients", clinic_id)
    digits_only = re.sub(r"\D", "", cleaned)
    if digits_only and len(digits_only) >= 4:
        pat_res = await sb(pat_query.ilike("phone", f"%{digits_only}%").limit(limit))
    elif cleaned.upper().startswith("MRN-"):
        pat_res = await sb(pat_query.ilike("mrn", f"{cleaned}%").limit(limit))
    else:
        tokens = cleaned.split()
        pat_res = await sb(
            pat_query.ilike("name", "%" + "%".join(tokens) + "%").limit(limit)
        )

    for p in pat_res.data or []:
        key = (p["id"], None)
        if key not in seen:
            seen.add(key)
            hits.append(
                {
                    "patient_id": p["id"],
                    "family_member_id": None,
                    "mrn": p.get("mrn"),
                    "name": p.get("name") or "Unknown",
                    "phone": p.get("phone") or "",
                    "relationship": None,
                    "age_years": _compute_age(
                        p.get("date_of_birth"),
                        p.get("age_years"),
                        p.get("age_recorded_on"),
                    ),
                    "gender": p.get("gender"),
                    "last_visit_date": None,
                    "match_reason": None,
                }
            )

    # 2. Search family members
    fam_query = scoped_query("family_members", clinic_id)
    if digits_only and len(digits_only) >= 4:
        fam_res = await sb(fam_query.ilike("primary_phone", f"%{digits_only}%").limit(limit))
    elif cleaned.upper().startswith("MRN-"):
        fam_res = await sb(fam_query.ilike("mrn", f"{cleaned}%").limit(limit))
    else:
        tokens = cleaned.split()
        fam_res = await sb(
            fam_query.ilike("full_name", "%" + "%".join(tokens) + "%").limit(limit)
        )

    for fm in fam_res.data or []:
        # Resolve parent patient_id
        parent_res = await sb(
            scoped_query("patients", clinic_id)
            .eq("phone", fm.get("primary_phone"))
            .limit(1)
        )
        parent_id = parent_res.data[0]["id"] if parent_res.data else fm["id"]
        key = (parent_id, fm["id"])
        if key not in seen:
            seen.add(key)
            hits.append(
                {
                    "patient_id": parent_id,
                    "family_member_id": fm["id"],
                    "mrn": fm.get("mrn"),
                    "name": fm.get("full_name") or "Unknown",
                    "phone": fm.get("primary_phone") or "",
                    "relationship": fm.get("relationship"),
                    "age_years": _compute_age(
                        fm.get("date_of_birth"),
                        fm.get("age_years"),
                        fm.get("age_recorded_on"),
                    ),
                    "gender": fm.get("gender"),
                    "last_visit_date": None,
                    "match_reason": None,
                }
            )

    # 3. Search legacy records (prefill only)
    if len(hits) < limit:
        pr_query = scoped_query("patient_records", clinic_id)
        if digits_only and len(digits_only) >= 4:
            pr_res = await sb(pr_query.ilike("phone", f"%{digits_only}%").limit(limit))
        else:
            tokens = cleaned.split()
            pr_res = await sb(
                pr_query.ilike("name", "%" + "%".join(tokens) + "%").limit(limit)
            )

        for pr in pr_res.data or []:
            pr_id = pr.get("id")
            key = (pr_id, None)
            if key not in seen:
                seen.add(key)
                hits.append(
                    {
                        "patient_id": pr_id,
                        "family_member_id": None,
                        "mrn": None,
                        "name": pr.get("name") or "Unknown",
                        "phone": pr.get("phone") or "",
                        "relationship": None,
                        "age_years": pr.get("age"),
                        "gender": pr.get("gender"),
                        "last_visit_date": None,
                        "match_reason": "legacy_record",
                    }
                )

    return hits[:limit]


async def duplicate_candidates(
    clinic_id: str,
    name: str,
    phone: str,
    dob: Optional[date] = None,
    age: Optional[int] = None,
) -> list[dict]:
    """Find potential duplicate patients within this clinic."""
    candidates: list[dict] = []
    norm_phone = _normalize_phone(phone)
    norm_name = name.strip().lower()

    # 1. Check same phone in patients
    phone_res = await sb(
        scoped_query("patients", clinic_id).eq("phone", norm_phone)
    )
    for p in phone_res.data or []:
        candidates.append(
            {
                "patient_id": p["id"],
                "family_member_id": None,
                "mrn": p.get("mrn"),
                "name": p.get("name") or "",
                "phone": p.get("phone") or "",
                "relationship": None,
                "age_years": p.get("age_years"),
                "gender": p.get("gender"),
                "last_visit_date": None,
                "match_reason": "same_phone",
            }
        )

    # 2. Check same phone in family members
    fam_phone_res = await sb(
        scoped_query("family_members", clinic_id).eq("primary_phone", norm_phone)
    )
    for fm in fam_phone_res.data or []:
        parent_res = await sb(
            scoped_query("patients", clinic_id).eq("phone", norm_phone).limit(1)
        )
        parent_id = parent_res.data[0]["id"] if parent_res.data else fm["id"]
        candidates.append(
            {
                "patient_id": parent_id,
                "family_member_id": fm["id"],
                "mrn": fm.get("mrn"),
                "name": fm.get("full_name") or "",
                "phone": fm.get("primary_phone") or "",
                "relationship": fm.get("relationship"),
                "age_years": fm.get("age_years"),
                "gender": fm.get("gender"),
                "last_visit_date": None,
                "match_reason": "same_phone",
            }
        )

    # 3. Check same name + dob or name + age in patients
    name_res = await sb(
        scoped_query("patients", clinic_id).ilike("name", norm_name)
    )
    for p in name_res.data or []:
        if p["id"] in [c["patient_id"] for c in candidates if not c["family_member_id"]]:
            continue
        p_dob = p.get("date_of_birth")
        p_age = p.get("age_years")
        if dob and p_dob and str(dob)[:10] == str(p_dob)[:10]:
            candidates.append(
                {
                    "patient_id": p["id"],
                    "family_member_id": None,
                    "mrn": p.get("mrn"),
                    "name": p.get("name") or "",
                    "phone": p.get("phone") or "",
                    "relationship": None,
                    "age_years": p_age,
                    "gender": p.get("gender"),
                    "last_visit_date": None,
                    "match_reason": "same_name_dob",
                }
            )
        elif age is not None and p_age is not None and abs(age - p_age) <= 1:
            candidates.append(
                {
                    "patient_id": p["id"],
                    "family_member_id": None,
                    "mrn": p.get("mrn"),
                    "name": p.get("name") or "",
                    "phone": p.get("phone") or "",
                    "relationship": None,
                    "age_years": p_age,
                    "gender": p.get("gender"),
                    "last_visit_date": None,
                    "match_reason": "same_name_age",
                }
            )

    return candidates


async def assign_mrn(
    clinic_id: str,
    patient_id: str,
    family_member_id: Optional[str] = None,
) -> str:
    """Invoke opd_assign_mrn RPC to assign gapless MRN."""
    res = await sb(
        supabase.rpc(
            "opd_assign_mrn",
            {
                "p_clinic_id": clinic_id,
                "p_patient_id": patient_id,
                "p_family_member_id": family_member_id,
            },
        )
    )
    return res.data or ""


async def register_patient(
    clinic_id: str,
    body: dict,
    actor: Any,
) -> dict:
    """Register a new patient or family member with DPDP consent and duplicate verification."""
    if body.get("data_consent") is not True:
        raise HTTPException(
            status_code=422,
            detail="Data consent is required for patient registration.",
        )

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Patient name is required.")

    phone = _normalize_phone(body.get("phone") or "")
    if not phone or len(phone) < 10:
        raise HTTPException(status_code=422, detail="Valid phone number is required.")

    is_account_holder = body.get("is_account_holder", True)
    relationship = body.get("relationship")
    if not is_account_holder and not relationship:
        raise HTTPException(
            status_code=422,
            detail="Relationship is required for dependent family members.",
        )

    dob = body.get("date_of_birth")
    if dob:
        if isinstance(dob, str):
            dob = datetime.strptime(dob[:10], "%Y-%m-%d").date()
        if dob > today_ist():
            raise HTTPException(
                status_code=422, detail="Date of birth cannot be in the future."
            )

    allergies_status = body.get("allergies_status", "unknown")
    allergies = body.get("allergies") or []
    if allergies and allergies_status != "recorded":
        allergies_status = "recorded"
    if allergies_status == "recorded" and not allergies:
        raise HTTPException(
            status_code=422,
            detail="Allergies must be specified when status is 'recorded'.",
        )

    # Check duplicates
    acknowledged = set(body.get("acknowledged_duplicates") or [])
    dupes = await duplicate_candidates(
        clinic_id,
        name,
        phone,
        dob=dob,
        age=body.get("age_years"),
    )
    unack = [
        d
        for d in dupes
        if d["patient_id"] not in acknowledged
        and f"{d['patient_id']}:{d.get('family_member_id') or ''}" not in acknowledged
    ]
    if unack:
        raise HTTPException(
            status_code=409,
            detail={"error": "duplicates_found", "candidates": unack},
        )

    today_str = today_ist().isoformat()

    if is_account_holder:
        # Check if patient exists
        existing_res = await sb(
            scoped_query("patients", clinic_id).eq("phone", phone).limit(1)
        )
        if existing_res.data:
            existing = existing_res.data[0]
            patient_id = existing["id"]
            # Update only null/empty demographic fields
            updates = {}
            for k in (
                "date_of_birth",
                "age_years",
                "gender",
                "address_line",
                "city",
                "pincode",
                "emergency_contact_name",
                "emergency_contact_phone",
                "emergency_contact_relation",
            ):
                if not existing.get(k) and body.get(k) is not None:
                    updates[k] = body[k]
            if not existing.get("age_recorded_on") and body.get("age_years"):
                updates["age_recorded_on"] = today_str
            if not existing.get("allergies") and allergies:
                updates["allergies"] = allergies
                updates["allergies_status"] = allergies_status

            if updates:
                await sb(
                    supabase.table("patients")
                    .update(updates)
                    .eq("clinic_id", clinic_id)
                    .eq("id", patient_id)
                )
        else:
            insert_data = {
                "clinic_id": clinic_id,
                "phone": phone,
                "name": name,
                "date_of_birth": dob.isoformat() if dob else None,
                "age_years": body.get("age_years"),
                "age_recorded_on": today_str if body.get("age_years") else None,
                "gender": body.get("gender"),
                "address_line": body.get("address_line"),
                "city": body.get("city"),
                "pincode": body.get("pincode"),
                "emergency_contact_name": body.get("emergency_contact_name"),
                "emergency_contact_phone": body.get("emergency_contact_phone"),
                "emergency_contact_relation": body.get("emergency_contact_relation"),
                "allergies": allergies,
                "allergies_status": allergies_status,
                "opted_in": bool(body.get("whatsapp_opt_in")),
                "data_consent": True,
            }
            # unscoped: insert_scoped_by_payload
            ins_res = await sb(supabase.table("patients").insert(insert_data))
            if not ins_res.data:
                raise HTTPException(500, "Failed to create patient account.")
            patient_id = ins_res.data[0]["id"]

        mrn = await assign_mrn(clinic_id, patient_id, None)

        pat_row = (
            await sb(scoped_query("patients", clinic_id).eq("id", patient_id))
        ).data[0]

        return {
            "patient_id": patient_id,
            "family_member_id": None,
            "mrn": pat_row.get("mrn") or mrn,
            "name": pat_row.get("name"),
            "phone": pat_row.get("phone"),
            "is_account_holder": True,
            "relationship": None,
            "date_of_birth": pat_row.get("date_of_birth"),
            "age_years": _compute_age(
                pat_row.get("date_of_birth"),
                pat_row.get("age_years"),
                pat_row.get("age_recorded_on"),
            ),
            "gender": pat_row.get("gender"),
            "address_line": pat_row.get("address_line"),
            "city": pat_row.get("city"),
            "pincode": pat_row.get("pincode"),
            "emergency_contact_name": pat_row.get("emergency_contact_name"),
            "emergency_contact_phone": pat_row.get("emergency_contact_phone"),
            "emergency_contact_relation": pat_row.get("emergency_contact_relation"),
            "allergies": pat_row.get("allergies") or [],
            "allergies_status": pat_row.get("allergies_status") or "unknown",
            "opted_in": pat_row.get("opted_in", False),
        }

    else:
        # Dependant / family member
        parent_res = await sb(
            scoped_query("patients", clinic_id).eq("phone", phone).limit(1)
        )
        if not parent_res.data:
            # Create shell patient row
            p_data = {
                "clinic_id": clinic_id,
                "phone": phone,
                "name": body.get("guardian_name") or name,
                "data_consent": True,
                "opted_in": bool(body.get("whatsapp_opt_in")),
            }
            # unscoped: insert_scoped_by_payload
            parent_ins = await sb(supabase.table("patients").insert(p_data))
            parent_id = parent_ins.data[0]["id"]
        else:
            parent_id = parent_res.data[0]["id"]

        fm_data = {
            "clinic_id": clinic_id,
            "primary_phone": phone,
            "full_name": name,
            "relationship": relationship,
            "date_of_birth": dob.isoformat() if dob else None,
            "age_years": body.get("age_years"),
            "age_recorded_on": today_str if body.get("age_years") else None,
            "gender": body.get("gender"),
            "allergies": allergies,
            "allergies_status": allergies_status,
        }
        # unscoped: insert_scoped_by_payload
        fm_res = await sb(supabase.table("family_members").insert(fm_data))
        if not fm_res.data:
            raise HTTPException(500, "Failed to create family member.")
        fm_row = fm_res.data[0]
        fm_id = fm_row["id"]

        mrn = await assign_mrn(clinic_id, parent_id, fm_id)

        fm_fresh = (
            await sb(scoped_query("family_members", clinic_id).eq("id", fm_id))
        ).data[0]

        return {
            "patient_id": parent_id,
            "family_member_id": fm_id,
            "mrn": fm_fresh.get("mrn") or mrn,
            "name": fm_fresh.get("full_name"),
            "phone": fm_fresh.get("primary_phone"),
            "is_account_holder": False,
            "relationship": fm_fresh.get("relationship"),
            "date_of_birth": fm_fresh.get("date_of_birth"),
            "age_years": _compute_age(
                fm_fresh.get("date_of_birth"),
                fm_fresh.get("age_years"),
                fm_fresh.get("age_recorded_on"),
            ),
            "gender": fm_fresh.get("gender"),
            "address_line": None,
            "city": None,
            "pincode": None,
            "emergency_contact_name": None,
            "emergency_contact_phone": None,
            "emergency_contact_relation": None,
            "allergies": fm_fresh.get("allergies") or [],
            "allergies_status": fm_fresh.get("allergies_status") or "unknown",
            "opted_in": False,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 2. WALK-INS & QUEUE ACTIONS
# ═══════════════════════════════════════════════════════════════════════════════


async def create_walk_in(
    clinic: dict,
    body: dict,
    actor: Any,
) -> dict:
    """Create a walk-in consultation appointment and check in to the queue."""
    clinic_id = str(clinic["id"])
    doctor_id = str(body["doctor_id"])

    # 1. Doctor verification
    doc_res = await sb(
        scoped_query("doctors", clinic_id).eq("id", doctor_id).limit(1)
    )
    if not doc_res.data:
        raise HTTPException(status_code=404, detail="Doctor not found")
    doc = doc_res.data[0]
    if not doc.get("is_active", True):
        raise HTTPException(status_code=422, detail="Doctor is inactive")

    today = today_ist()
    today_str = today.isoformat()

    # 2. Holiday check
    holidays = await sb(
        scoped_query("hospital_holidays", clinic_id).eq("holiday_date", today_str)
    )
    if holidays.data:
        raise HTTPException(status_code=409, detail="clinic_closed")

    # 3. Doctor leave check
    leaves = await sb(
        scoped_query("doctor_leaves", clinic_id)
        .eq("doctor_name", doc["name"])
        .eq("leave_date", today_str)
    )
    if leaves.data:
        leave_type = leaves.data[0].get("leave_type") or "full"
        if leave_type == "full":
            raise HTTPException(status_code=409, detail="doctor_on_leave")
        elif leave_type in ("half_morning", "half_evening"):
            is_morning = datetime.now(IST).hour < 14
            if (is_morning and leave_type == "half_morning") or (
                not is_morning and leave_type == "half_evening"
            ):
                raise HTTPException(status_code=409, detail="doctor_on_leave")

    # 4. Doctor branch & availability
    branch_id = await resolve_visit_branch(doc["id"], actor, body.get("branch_id"))

    # 5. Patient & family member verification
    patient_id = str(body["patient_id"])
    pat_res = await sb(
        scoped_query("patients", clinic_id).eq("id", patient_id).limit(1)
    )
    if not pat_res.data:
        raise HTTPException(status_code=404, detail="Patient not found")
    patient = pat_res.data[0]

    family_member_id = body.get("family_member_id")
    fam = None
    if family_member_id:
        fam_res = await sb(
            scoped_query("family_members", clinic_id)
            .eq("id", str(family_member_id))
            .limit(1)
        )
        if not fam_res.data:
            raise HTTPException(status_code=404, detail="Family member not found")
        fam = fam_res.data[0]

    patient_name = fam.get("full_name") if fam else patient.get("name") or "Patient"

    # Ensure MRN exists
    if not (fam.get("mrn") if fam else patient.get("mrn")):
        await assign_mrn(clinic_id, patient_id, family_member_id)

    # 6. Insert appointment
    now_time = datetime.now(IST).strftime("%H:%M")
    booking_ref = generate_booking_reference(clinic.get("booking_ref_prefix"))
    appt_data = {
        "clinic_id": clinic_id,
        "patient_id": patient_id,
        "family_member_id": family_member_id,
        "doctor_id": doc["id"],
        "doctor_name": doc["name"],
        "department": doc.get("department") or "General Medicine",
        "branch_id": branch_id,
        "patient_phone": patient.get("phone"),
        "patient_name": patient_name,
        "appointment_date": today_str,
        "appointment_time": now_time,
        "booking_type": "consultation",
        "booking_channel": "front_desk",
        "is_walk_in": True,
        "visit_type": body.get("visit_type", "new"),
        "symptoms": body.get("symptoms"),
        "status": "confirmed",
        "booking_ref": booking_ref,
    }
    # unscoped: insert_scoped_by_payload
    ins_res = await sb(supabase.table("appointments").insert(appt_data))
    if not ins_res.data:
        raise HTTPException(500, "Failed to create appointment record.")
    appt = ins_res.data[0]

    # 7. Check-in appointment
    skip_vitals = bool(body.get("skip_vitals"))
    if skip_vitals:
        initial_stage = "waiting"
    else:
        opd_settings = clinic.get("opd_settings") or {}
        initial_stage = opd_settings.get("after_checkin_stage", "vitals_pending")

    checked_in = await check_in_appointment(
        clinic_id, appt["id"], initial_queue_status=initial_stage
    )
    if not checked_in:
        raise HTTPException(500, "Failed to check in walk-in patient.")

    # 8. Background notification
    if patient.get("opted_in"):
        try:
            from app.services.whatsapp import whatsapp_service
            opd_settings = (clinic or {}).get("opd_settings") or {}
            templates = opd_settings.get("templates") or {}
            tmpl_name = templates.get("token_issued") or templates.get("opd_token_issued")
            tok_num = str(checked_in.get("token_number"))
            doc_name = doctor_title(doc['name'])
            clinic_name = str(clinic.get("name", "Clinic"))
            if tmpl_name:
                asyncio.create_task(
                    whatsapp_service.send_template(
                        clinic,
                        patient["phone"],
                        template_name=tmpl_name,
                        components=[
                            {
                                "type": "body",
                                "parameters": [
                                    {"type": "text", "text": tok_num},
                                    {"type": "text", "text": doc_name},
                                    {"type": "text", "text": clinic_name},
                                ],
                            }
                        ],
                        _source="opd_walk_in",
                    )
                )
            else:
                asyncio.create_task(
                    whatsapp_service.send_text(
                        clinic,
                        patient["phone"],
                        f"Your OPD Token number is {tok_num} for {doc_name}.",
                        _source="opd_walk_in",
                    )
                )
        except Exception as e:
            logger.warning(f"Failed to queue WhatsApp token notification: {e}")

    return _make_queue_row(checked_in)


async def arrive(
    clinic: dict,
    appointment_id: str,
    actor: Any,
    visit_type: Optional[str] = None,
) -> dict:
    """Check in a scheduled patient on arrival at front desk."""
    clinic_id = str(clinic["id"])
    appt_res = await sb(
        scoped_query("appointments", clinic_id).eq("id", appointment_id).limit(1)
    )
    if not appt_res.data:
        raise HTTPException(status_code=404, detail="Appointment not found")
    appt = appt_res.data[0]

    today_str = today_ist().isoformat()
    if appt.get("appointment_date") != today_str:
        raise HTTPException(
            status_code=409,
            detail="Cannot arrive appointment scheduled for a different date.",
        )

    if appt.get("token_number"):
        raise HTTPException(status_code=409, detail="Appointment already arrived.")

    # Same rule as check_in_appointment, checked before visit_type is written: a
    # cancelled or unpaid booking used to surface as a 500 after that update.
    from app.database import _NOT_CHECKABLE_IN
    if appt.get("status") in _NOT_CHECKABLE_IN:
        raise HTTPException(
            status_code=409,
            detail=f"This booking is {str(appt.get('status')).replace('_', ' ')} and cannot be marked arrived.",
        )

    from app.services.permissions import enforce_branch_scope
    enforce_branch_scope(actor, appt.get("branch_id"))

    arrive_update: dict = {}
    if visit_type:
        arrive_update["visit_type"] = visit_type
    if not appt.get("branch_id") and appt.get("doctor_id"):
        branch = await resolve_visit_branch(appt["doctor_id"], actor)
        if branch:
            arrive_update["branch_id"] = branch
    if arrive_update:
        await sb(
            supabase.table("appointments")
            .update(arrive_update)
            .eq("clinic_id", clinic_id)
            .eq("id", appointment_id)
        )

    opd_settings = clinic.get("opd_settings") or {}
    initial_stage = opd_settings.get("after_checkin_stage", "vitals_pending")

    checked_in = await check_in_appointment(
        clinic_id, appointment_id, initial_queue_status=initial_stage
    )
    if not checked_in:
        raise HTTPException(status_code=500, detail="Failed to check in appointment.")

    if appt.get("patient_phone"):
        try:
            from app.services.whatsapp import whatsapp_service
            templates = opd_settings.get("templates") or {}
            tmpl_name = templates.get("token_issued") or templates.get("opd_token_issued")
            tok_num = str(checked_in.get("token_number"))
            doc_name = doctor_title(appt.get('doctor_name'))
            clinic_name = str(clinic.get("name", "Clinic"))
            if tmpl_name:
                asyncio.create_task(
                    whatsapp_service.send_template(
                        clinic,
                        appt["patient_phone"],
                        template_name=tmpl_name,
                        components=[
                            {
                                "type": "body",
                                "parameters": [
                                    {"type": "text", "text": tok_num},
                                    {"type": "text", "text": doc_name},
                                    {"type": "text", "text": clinic_name},
                                ],
                            }
                        ],
                        _source="opd_arrive",
                    )
                )
            else:
                asyncio.create_task(
                    whatsapp_service.send_text(
                        clinic,
                        appt["patient_phone"],
                        f"Your OPD Token number is {tok_num} for {doc_name}.",
                        _source="opd_arrive",
                    )
                )
        except Exception as e:
            logger.warning(f"Failed to queue WhatsApp token notification on arrive: {e}")

    return _make_queue_row(checked_in)


async def resolve_visit_branch(
    doctor_id: str, actor: Any, explicit: Optional[str] = None
) -> Optional[str]:
    """The branch an OPD visit belongs to.

    Branch-pinned logins filter the queue on appointments.branch_id, so a visit
    saved with no branch is invisible to every one of them. Order: the branch
    the desk picked, the desk's own branch, then the doctor's branch when the
    doctor practises at exactly one (doctor_branches). doctors has no
    branch_id column; reading one is why walk-ins used to land branchless.
    """
    if explicit:
        return str(explicit)
    if getattr(actor, "branch_id", None):
        return str(actor.branch_id)
    res = await sb(
        # unscoped: doctor branch association; doctor_id was verified against the clinic by the caller
        supabase.table("doctor_branches").select("branch_id").eq("doctor_id", str(doctor_id))
    )
    ids = {str(r["branch_id"]) for r in (res.data or []) if r.get("branch_id")}
    return ids.pop() if len(ids) == 1 else None


def _make_queue_row(appt: dict) -> dict:
    """Format appointment row as QueueRow."""
    return {
        "appointment_id": str(appt["id"]),
        "token_number": appt.get("token_number") or 0,
        "stage": appt.get("queue_status") or "registered",
        "doctor_id": str(appt["doctor_id"]) if appt.get("doctor_id") else None,
        "doctor_name": appt.get("doctor_name") or "Doctor",
        "department": appt.get("department"),
        "branch_id": str(appt["branch_id"]) if appt.get("branch_id") else None,
        "patient_name": appt.get("patient_name") or "Patient",
        "mrn": appt.get("mrn"),
        "visit_type": appt.get("visit_type") or "new",
        "booking_channel": appt.get("booking_channel") or "front_desk",
        "is_walk_in": bool(appt.get("is_walk_in")),
        "checked_in_at": appt.get("checked_in_at"),
        "waiting_minutes": _calc_waiting_minutes(appt.get("checked_in_at")),
        "has_vitals": bool(appt.get("has_vitals")),
        "invoice_status": None,
        "payment_status": appt.get("payment_status"),
    }


def _calc_waiting_minutes(checked_in_at: Optional[str]) -> Optional[int]:
    if not checked_in_at:
        return None
    try:
        dt = datetime.fromisoformat(checked_in_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max(0, int((now - dt).total_seconds() // 60))
    except Exception:
        return None


async def get_queue_board(
    clinic_id: str,
    date_str: Optional[str] = None,
    doctor_id: Optional[str] = None,
    department: Optional[str] = None,
    branch_id: Optional[str] = None,
    *,
    date: Optional[Any] = None,
) -> dict:
    """Load the full queue board for live operations, including etag."""
    actual_date = date_str or (date.isoformat() if (date is not None and hasattr(date, "isoformat")) else str(date) if date is not None else None)
    if not actual_date:
        actual_date = today_ist().isoformat()

    query = (
        scoped_query("appointments", clinic_id)
        .eq("appointment_date", actual_date)
        .in_("status", ["confirmed", "in_consultation", "completed"])
        .not_.is_("token_number", "null")
    )
    if doctor_id:
        query = query.eq("doctor_id", doctor_id)
    if department:
        query = query.eq("department", department)
    if branch_id:
        query = query.eq("branch_id", branch_id)

    res = await sb(query.order("token_number", desc=False))
    rows = res.data or []

    # Group by doctor
    doctors_map: dict[str, dict] = {}
    etag_tokens: list[str] = []

    for r in rows:
        d_id = str(r.get("doctor_id") or r.get("doctor_name") or "unassigned")
        if d_id not in doctors_map:
            doctors_map[d_id] = {
                "doctor_id": str(r["doctor_id"]) if r.get("doctor_id") else None,
                "doctor_name": r.get("doctor_name") or "Doctor",
                "department": r.get("department"),
                "room": "",
                "now_serving": None,
                "rows": [],
                "counts": {s: 0 for s in STAGES},
            }

        qrow = _make_queue_row(r)
        doctors_map[d_id]["rows"].append(qrow)
        stage = qrow["stage"]
        if stage in doctors_map[d_id]["counts"]:
            doctors_map[d_id]["counts"][stage] += 1

        if stage == "in_consultation" and not doctors_map[d_id]["now_serving"]:
            doctors_map[d_id]["now_serving"] = qrow

        etag_tokens.append(
            f"{r['id']}:{r.get('queue_status')}:{r.get('token_number')}:{r.get('updated_at')}"
        )

    etag = hashlib.sha1("|".join(etag_tokens).encode("utf-8")).hexdigest()

    return {
        "date": actual_date,
        "doctors": list(doctors_map.values()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "etag": etag,
    }


async def transition_stage(
    clinic: dict,
    appointment_id: str,
    to_stage: str,
    expected_from: str,
    actor: Any,
    reason: Optional[str] = None,
) -> dict:
    """Transition an appointment's queue stage atomically using CAS."""
    clinic_id = str(clinic["id"])

    allowed = ALLOWED_TRANSITIONS.get(expected_from, set())
    if to_stage not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Transition from '{expected_from}' to '{to_stage}' is not permitted.",
        )

    if to_stage == "cancelled" and not reason:
        raise HTTPException(
            status_code=422,
            detail="Cancellation reason is required when moving to cancelled.",
        )

    # Fetch appointment with lock/check
    appt_res = await sb(
        scoped_query("appointments", clinic_id).eq("id", appointment_id).limit(1)
    )
    if not appt_res.data:
        raise HTTPException(status_code=404, detail="Appointment not found")
    appt = appt_res.data[0]

    current_stage = appt.get("queue_status")
    if current_stage != expected_from:
        raise HTTPException(
            status_code=409,
            detail="Queue stage changed concurrently. Please refresh.",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    raw_timeline = appt.get("queue_timeline")
    timeline = dict(raw_timeline) if isinstance(raw_timeline, dict) else {}
    timeline[to_stage] = now_iso

    update_payload = {
        "queue_status": to_stage,
        "queue_timeline": timeline,
    }
    if to_stage == "completed":
        update_payload["status"] = "completed"
    elif to_stage == "cancelled":
        update_payload["status"] = "cancelled"
        # No cancellation_reason column: the router audit-logs `reason`.

    upd_res = await sb(
        supabase.table("appointments")
        .update(update_payload)
        .eq("clinic_id", clinic_id)
        .eq("id", appointment_id)
        .eq("queue_status", expected_from)
    )
    if not upd_res.data:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification detected on queue stage transition.",
        )

    updated_appt = upd_res.data[0]

    # Side effect: in_consultation creates draft encounter
    if to_stage == "in_consultation":
        enc_check = await sb(
            scoped_query("opd_encounters", clinic_id)
            .eq("appointment_id", appointment_id)
            .eq("status", "draft")
            .limit(1)
        )
        if not enc_check.data:
            enc_data = {
                "clinic_id": clinic_id,
                "appointment_id": appointment_id,
                "doctor_id": updated_appt.get("doctor_id"),
                "patient_id": updated_appt["patient_id"],
                "family_member_id": updated_appt.get("family_member_id"),
                "status": "draft",
                "started_at": now_iso,
            }
            try:
                # unscoped: insert_scoped_by_payload
                await sb(supabase.table("opd_encounters").insert(enc_data))
            except Exception as e:
                logger.warning(f"Could not auto-create draft encounter: {e}")

    return _make_queue_row(updated_appt)


async def opd_call_next(
    clinic: dict,
    doctor_id: str,
    actor: Any,
) -> dict:
    """Advance current in_consultation patient and claim next waiting patient."""
    clinic_id = str(clinic["id"])
    today_str = today_ist().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    finished_row = None
    called_row = None

    # Step 1: Finish current in_consultation patient if any
    current_res = await sb(
        scoped_query("appointments", clinic_id)
        .eq("doctor_id", doctor_id)
        .eq("appointment_date", today_str)
        .eq("queue_status", "in_consultation")
        .limit(1)
    )
    if current_res.data:
        curr = current_res.data[0]
        opd_settings = clinic.get("opd_settings") or {}
        next_stage = "billing" if opd_settings.get("billing_after_consult", True) else "completed"

        timeline = dict(curr.get("queue_timeline") or {})
        timeline[next_stage] = now_iso

        upd = await sb(
            supabase.table("appointments")
            .update({
                "queue_status": next_stage,
                "queue_timeline": timeline,
                "status": "completed" if next_stage == "completed" else curr.get("status"),
            })
            .eq("clinic_id", clinic_id)
            .eq("id", curr["id"])
            .eq("queue_status", "in_consultation")
        )
        if upd.data:
            finished_row = _make_queue_row(upd.data[0])

    # Step 2: Claim next waiting patient
    for _ in range(5):
        next_cand = await sb(
            scoped_query("appointments", clinic_id)
            .eq("doctor_id", doctor_id)
            .eq("appointment_date", today_str)
            .eq("queue_status", "waiting")
            .order("token_number", desc=False)
            .limit(1)
        )
        if not next_cand.data:
            break

        cand = next_cand.data[0]
        timeline = dict(cand.get("queue_timeline") or {})
        timeline["in_consultation"] = now_iso

        claim = await sb(
            supabase.table("appointments")
            .update({
                "queue_status": "in_consultation",
                "queue_timeline": timeline,
            })
            .eq("clinic_id", clinic_id)
            .eq("id", cand["id"])
            .eq("queue_status", "waiting")
        )
        if claim.data:
            claimed_appt = claim.data[0]
            called_row = _make_queue_row(claimed_appt)

            # Idempotently ensure draft encounter
            enc_check = await sb(
                scoped_query("opd_encounters", clinic_id)
                .eq("appointment_id", claimed_appt["id"])
                .eq("status", "draft")
                .limit(1)
            )
            if not enc_check.data:
                try:
                    # unscoped: insert_scoped_by_payload
                    await sb(supabase.table("opd_encounters").insert({
                        "clinic_id": clinic_id,
                        "appointment_id": claimed_appt["id"],
                        "doctor_id": doctor_id,
                        "patient_id": claimed_appt["patient_id"],
                        "family_member_id": claimed_appt.get("family_member_id"),
                        "status": "draft",
                        "started_at": now_iso,
                    }))
                except Exception as e:
                    logger.warning(f"Draft encounter insert failed: {e}")

            # Notify patient (WhatsApp)
            if claimed_appt.get("patient_phone"):
                try:
                    from app.services.whatsapp import whatsapp_service
                    opd_settings = (clinic or {}).get("opd_settings") or {}
                    templates = opd_settings.get("templates") or {}
                    tmpl_name = templates.get("token_called") or templates.get("opd_token_called")
                    tok_num = str(claimed_appt.get("token_number"))
                    doc_name = doctor_title(claimed_appt.get('doctor_name'))
                    room_name = (opd_settings.get("rooms") or {}).get(str(claimed_appt.get("doctor_id"))) or "Consultation Room"
                    if tmpl_name:
                        asyncio.create_task(
                            whatsapp_service.send_template(
                                clinic,
                                claimed_appt["patient_phone"],
                                template_name=tmpl_name,
                                components=[
                                    {
                                        "type": "body",
                                        "parameters": [
                                            {"type": "text", "text": tok_num},
                                            {"type": "text", "text": doc_name},
                                            {"type": "text", "text": room_name},
                                        ],
                                    }
                                ],
                                _source="opd_call_next",
                            )
                        )
                    else:
                        asyncio.create_task(
                            whatsapp_service.send_text(
                                clinic,
                                claimed_appt["patient_phone"],
                                f"It is your turn! Token {tok_num} for {doc_name}.",
                                _source="opd_call_next",
                            )
                        )
                except Exception:
                    pass

            break

    return {"called": called_row, "finished": finished_row}


async def queue_position(clinic_id: str, appointment: dict) -> dict:
    """Calculate patient queue position matching get_patient_queue_status shape."""
    token = appointment.get("token_number")
    doc_name = appointment.get("doctor_name") or "Doctor"
    if token is None:
        return {
            "checked_in": False,
            "is_lab_test": False,
            "doctor_name": doc_name,
        }

    date_str = appointment.get("appointment_date") or today_ist().isoformat()
    doc_id = appointment.get("doctor_id")

    query = (
        scoped_query("appointments", clinic_id, "token_number, queue_status")
        .eq("appointment_date", date_str)
        .in_("status", ["confirmed", "in_consultation", "completed"])
    )
    if doc_id:
        query = query.eq("doctor_id", doc_id)
    elif doc_name:
        query = query.eq("doctor_name", doc_name)

    res = await sb(query)
    rows = res.data or []

    # Currently serving = token in consultation, or lowest token waiting
    in_consult = [r["token_number"] for r in rows if r.get("queue_status") == "in_consultation" and r.get("token_number")]
    if in_consult:
        currently_serving = min(in_consult)
    else:
        active_tokens = [
            r["token_number"]
            for r in rows
            if r.get("queue_status") in ("waiting", "vitals_pending", "registered")
            and r.get("token_number")
        ]
        currently_serving = min(active_tokens) if active_tokens else token

    ahead = len([
        r
        for r in rows
        if r.get("queue_status") in ("registered", "vitals_pending", "waiting")
        and r.get("token_number")
        and r["token_number"] < token
    ])

    return {
        "checked_in": True,
        "is_lab_test": False,
        "token_number": token,
        "currently_serving": currently_serving,
        "patients_ahead": max(0, ahead),
        "doctor_name": doc_name,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 3. SETUP WIZARD & GO LIVE
# ═══════════════════════════════════════════════════════════════════════════════


async def setup_checklist(clinic: dict) -> list[dict]:
    """Compute 14 live setup checklist items dynamically."""
    clinic_id = str(clinic["id"])
    settings = clinic.get("opd_settings") or {}
    confirmed = set(settings.get("confirmed_steps") or [])

    # 1. clinic_profile
    lh = clinic_letterhead(clinic)
    profile_done = bool(lh["name"] and lh["address"] and lh["phone"])

    # Active doctors
    doc_res = await sb(
        scoped_query("doctors", clinic_id).eq("is_active", True)
    )
    active_docs = doc_res.data or []
    has_active_docs = len(active_docs) > 0

    # 2. operating_hours
    hours_done = has_active_docs and all(
        (d.get("morning_start") or d.get("evening_start") or d.get("morning_slots") or d.get("evening_slots") or d.get("slot_duration_minutes"))
        for d in active_docs
    )

    # 3. branches. The setting is the multi_branch plan feature (clinics has no
    # multi_branch column, so this used to always pass). Entitlement alone is
    # not "runs branches": enterprise grants every feature, and a single-site
    # clinic with no branch rows is fine. It fails only when branches exist and
    # none is active, so there is nowhere to send a patient.
    if has_feature(clinic, "multi_branch"):
        branch_res = await sb(scoped_query("branches", clinic_id).select("id, is_active"))
        branch_rows = branch_res.data or []
        branches_done = (
            not branch_rows
            or any(b.get("is_active") is not False for b in branch_rows)
            or "branches" in confirmed
        )
    else:
        branches_done = True

    # 4. departments
    depts = [d.get("department") for d in active_docs if d.get("department")]
    depts_done = len(depts) >= 1 or "departments" in confirmed

    # 5. doctor_roster
    roster_done = has_active_docs

    # 6. doctor_registration
    reg_done = has_active_docs and all(
        bool(d.get("registration_number") and d.get("registration_council"))
        for d in active_docs
    )

    # 7. consult_durations
    durations_done = True  # slot_duration_minutes defaults to 30

    # 8. consult_fees
    fees_done = has_active_docs and all(
        d.get("consultation_fee") is not None for d in active_docs
    )

    # 9. front_desk_permissions
    admins_res = await sb(
        supabase.table("clinic_admins")
        .select("id, role, permissions, doctor_id, is_active")
        .eq("clinic_id", clinic_id)
    )
    # A deactivated account cannot sign in, so it staffs nothing.
    admins = [a for a in (admins_res.data or []) if a.get("is_active") is not False]
    desk_done = any(
        a.get("role") in ("clinic_admin", "super_admin")
        or "OPD_FRONT_DESK" in (a.get("permissions") or [])
        for a in admins
    )

    # 10. doctor_logins
    linked_doctor_ids = {str(a["doctor_id"]) for a in admins if a.get("doctor_id")}
    doc_logins_done = has_active_docs and all(
        str(d["id"]) in linked_doctor_ids for d in active_docs
    )

    # 11. token_rules
    rules_done = bool(
        settings.get("token_rule")
        and settings.get("after_checkin_stage")
        or "token_rules" in confirmed
    )

    # 12. billing_setup
    modes = settings.get("payment_modes") or []
    has_modes = len(modes) >= 1
    upi_ok = ("upi" not in modes) or bool(settings.get("upi_vpa"))
    billing_done = bool(has_modes and upi_ok)

    # 13. whatsapp_templates (non-blocking)
    tmpl = settings.get("templates") or {}
    tmpl_done = bool(tmpl.get("token_issued") or tmpl.get("prescription_ready"))

    # 14. test_run_go_live
    dry_at = settings.get("dry_run_passed_at")
    test_run_done = False
    if dry_at:
        try:
            dt = datetime.fromisoformat(dry_at.replace("Z", "+00:00"))
            test_run_done = (datetime.now(timezone.utc) - dt) < timedelta(hours=24)
        except Exception:
            test_run_done = False

    return [
        {
            "key": "clinic_profile",
            "label": "Clinic Profile",
            "done": profile_done,
            "blocking": True,
            "detail": None if profile_done else "Clinic name, address, and phone must be set",
            "fix_page": "profile",
        },
        {
            "key": "operating_hours",
            "label": "Operating Hours & Sessions",
            "done": hours_done,
            "blocking": True,
            "detail": None if hours_done else "Active doctors must have consultation session timings",
            "fix_page": "doctors",
        },
        {
            "key": "branches",
            "label": "Branches",
            "done": branches_done,
            "blocking": True,
            "detail": None if branches_done else "At least one active branch required for multi-branch clinic",
            "fix_page": "branches",
        },
        {
            "key": "departments",
            "label": "Departments",
            "done": depts_done,
            "blocking": True,
            "detail": None if depts_done else "At least one clinical department required",
            "fix_page": "doctors",
        },
        {
            "key": "doctor_roster",
            "label": "Doctor Roster",
            "done": roster_done,
            "blocking": True,
            "detail": None if roster_done else "At least one active doctor required",
            "fix_page": "doctors",
        },
        {
            "key": "doctor_registration",
            "label": "NMC Doctor Registrations",
            "done": reg_done,
            "blocking": True,
            "detail": None if reg_done else "Every active doctor must have registration number & council",
            "fix_page": "doctors",
        },
        {
            "key": "consult_durations",
            "label": "Consultation Slot Durations",
            "done": durations_done,
            "blocking": True,
            "detail": None,
            "fix_page": "doctors",
        },
        {
            "key": "consult_fees",
            "label": "Doctor Consultation Fees",
            "done": fees_done,
            "blocking": True,
            "detail": None if fees_done else "Consultation fee must be configured for all active doctors",
            "fix_page": "doctors",
        },
        {
            "key": "front_desk_permissions",
            "label": "Front Desk Staff Permissions",
            "done": desk_done,
            "blocking": True,
            "detail": None if desk_done else "At least one staff member must hold OPD_FRONT_DESK permission",
            "fix_page": "staff",
        },
        {
            "key": "doctor_logins",
            "label": "Doctor Login Accounts",
            "done": doc_logins_done,
            "blocking": True,
            "detail": None if doc_logins_done else "Every active doctor must be linked to a login account",
            "fix_page": "staff",
        },
        {
            "key": "token_rules",
            "label": "Queue & Token Rules",
            "done": rules_done,
            "blocking": True,
            "detail": None if rules_done else "Token rule and after-check-in stage must be confirmed",
            "fix_page": "opdsetup#opdPrefsCard",
        },
        {
            "key": "billing_setup",
            "label": "Billing & Payment Methods",
            "done": billing_done,
            "blocking": True,
            "detail": None if billing_done else "Payment modes and UPI VPA must be configured",
            "fix_page": "opdsetup#opdBillingCard",
        },
        {
            "key": "whatsapp_templates",
            "label": "WhatsApp Templates",
            "done": tmpl_done,
            "blocking": False,
            "detail": None if tmpl_done else "Meta template names configured (optional)",
            "fix_page": "opdsetup#opdTemplatesCard",
        },
        {
            "key": "test_run_go_live",
            "label": "Pre-flight Dry Run",
            "done": test_run_done,
            "blocking": True,
            "detail": None if test_run_done else "Test visit dry run must have passed within 24 hours",
            "fix_page": "opdsetup#opdDryRunAnchor",
        },
    ]


async def effective_state(
    clinic: dict,
    checklist: Optional[list[dict]] = None,
) -> str:
    """Compute effective OPD state: DEGRADED if READY but blocking check fails."""
    raw = clinic.get("opd_state") or "NOT_CONFIGURED"
    if raw == "READY":
        items = checklist if checklist is not None else await setup_checklist(clinic)
        if any(i["blocking"] and not i["done"] for i in items):
            return "DEGRADED"
    return raw


async def dry_run(clinic: dict) -> dict:
    """Execute pre-flight verification without mutating operational records."""
    clinic_id = str(clinic["id"])
    checks = []

    # 1. Doctors & sessions check
    doc_res = await sb(scoped_query("doctors", clinic_id).eq("is_active", True))
    active_docs = doc_res.data or []
    checks.append({
        "name": "active_doctors",
        "ok": len(active_docs) > 0,
        "detail": f"{len(active_docs)} active doctors found",
    })

    # 2. Registration check
    regs_ok = all(
        bool(d.get("registration_number") and d.get("registration_council"))
        for d in active_docs
    )
    checks.append({
        "name": "doctor_registrations",
        "ok": regs_ok,
        "detail": "All active doctors registered" if regs_ok else "Missing doctor registrations",
    })

    # 3. Next token preview (read-only)
    today_str = today_ist().isoformat()
    tok_res = await sb(
        scoped_query("appointments", clinic_id)
        .eq("appointment_date", today_str)
        .order("token_number", desc=True, nullsfirst=False)  # DESC puts NULLs first in Postgres
        .limit(1)
    )
    max_tok = tok_res.data[0]["token_number"] if tok_res.data and tok_res.data[0].get("token_number") else 0
    checks.append({
        "name": "token_generation_preview",
        "ok": True,
        "detail": f"Next token would be {max_tok + 1}",
    })

    # 4. WhatsApp credentials check
    has_wa = bool(clinic.get("phone_number_id") and clinic.get("whatsapp_access_token"))
    checks.append({
        "name": "whatsapp_credentials",
        "ok": has_wa,
        "detail": "Clinic WhatsApp credentials present" if has_wa else "WhatsApp credentials not configured",
    })

    all_ok = all(c["ok"] for c in checks if c["name"] != "whatsapp_credentials")

    if all_ok:
        now_iso = datetime.now(timezone.utc).isoformat()
        settings = dict(clinic.get("opd_settings") or {})
        settings["dry_run_passed_at"] = now_iso
        await sb(
            supabase.table("clinics")
            .update({"opd_settings": settings})
            .eq("id", clinic_id)
        )

    return {"ok": all_ok, "checks": checks}


async def go_live(clinic: dict, actor: Any) -> dict:
    """Transition clinic to live OPD READY status."""
    clinic_id = str(clinic["id"])
    items = await setup_checklist(clinic)

    failing = [i for i in items if i["blocking"] and not i["done"]]
    if failing:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "setup_incomplete",
                "message": "All blocking setup checklist items must be completed.",
                "failing_items": failing,
            },
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    settings = dict(clinic.get("opd_settings") or {})
    settings["went_live_at"] = now_iso

    res = await sb(
        supabase.table("clinics")
        .update({
            "opd_state": "READY",
            "opd_settings": settings,
        })
        .eq("id", clinic_id)
    )
    if not res.data:
        raise HTTPException(500, "Failed to update clinic status to live.")

    return {
        "state": "READY",
        "checklist": items,
        "settings": settings,
        "went_live_at": now_iso,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 4. TV DISPLAY & TOKENS
# ═══════════════════════════════════════════════════════════════════════════════


async def rotate_display_token(clinic_id: str) -> str:
    """Generate and store a new hallway TV display token hash."""
    raw_token = secrets.token_urlsafe(32)
    tok_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    await sb(
        supabase.table("clinics")
        .update({"opd_display_token_hash": tok_hash})
        .eq("id", clinic_id)
    )
    return raw_token


async def public_display_payload(
    clinic_id: str,
    branch_id: Optional[str] = None,
) -> dict:
    """Produce public hallway TV display data stripped of all patient PII."""
    clinic = await get_clinic_by_id(clinic_id)
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")

    if branch_id:
        br_res = await sb(
            scoped_query("branches", clinic_id).eq("id", branch_id).limit(1)
        )
        if not br_res.data:
            raise HTTPException(status_code=404, detail="Branch not found")

    today_str = today_ist().isoformat()
    q = (
        scoped_query("appointments", clinic_id)
        .eq("appointment_date", today_str)
        .in_("status", ["confirmed", "in_consultation"])
        .not_.is_("token_number", "null")
    )
    if branch_id:
        q = q.eq("branch_id", branch_id)

    res = await sb(q.order("token_number", desc=False))
    rows = res.data or []

    settings = clinic.get("opd_settings") or {}
    rooms = settings.get("rooms") or {}

    doctors_map: dict[str, dict] = {}
    for r in rows:
        d_id = str(r.get("doctor_id") or r.get("doctor_name") or "doc")
        if d_id not in doctors_map:
            doctors_map[d_id] = {
                "doctor_id": str(r["doctor_id"]) if r.get("doctor_id") else None,
                "doctor_name": r.get("doctor_name") or "Doctor",
                "department": r.get("department") or "",
                "room": rooms.get(str(r.get("doctor_id") or ""), ""),
                "now_serving": None,
                "next_tokens": [],
            }

        stage = r.get("queue_status")
        tok = r.get("token_number")
        if stage == "in_consultation" and not doctors_map[d_id]["now_serving"]:
            doctors_map[d_id]["now_serving"] = tok
        elif stage == "waiting" and tok:
            if len(doctors_map[d_id]["next_tokens"]) < 5:
                doctors_map[d_id]["next_tokens"].append(tok)

    payload = {
        "clinic_name": clinic.get("name") or "Clinic",
        "doctors": list(doctors_map.values()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Recursive PII leak guard
    def _assert_no_pii(obj: Any):
        forbidden_keys = {"patient_name", "patient_phone", "mrn", "phone", "symptoms"}
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in forbidden_keys:
                    raise AssertionError(f"PII leak detected in public display payload: {k}")
                _assert_no_pii(v)
        elif isinstance(obj, list):
            for item in obj:
                _assert_no_pii(item)

    _assert_no_pii(payload)
    return payload


# ═══════════════════════════════════════════════════════════════════════════════
# 4. OPERATIONAL ANALYTICS (§3.8)
# ═══════════════════════════════════════════════════════════════════════════════


def _percentile(values: list[float], p: float) -> Optional[float]:
    """Calculate the p-th percentile with linear interpolation. None when there
    is no data: 0.0 read as "nobody waited" on the dashboard."""
    if not values:
        return None
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c < len(sorted_v):
        d0 = sorted_v[f] * (c - k)
        d1 = sorted_v[c] * (k - f)
        return round(float(d0 + d1), 1)
    else:
        return round(float(sorted_v[f]), 1)


async def get_opd_analytics(
    clinic_id: str,
    from_date_str: str,
    to_date_str: str,
    branch_id: Optional[str] = None,
) -> dict:
    """Compute aggregated operational metrics over scoped rows with pagination and 50,000 cap."""
    try:
        from_dt = date.fromisoformat(from_date_str)
        to_dt = date.fromisoformat(to_date_str)
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Invalid date format. Expected YYYY-MM-DD for both 'from' and 'to'.",
        )

    if from_dt > to_dt:
        raise HTTPException(
            status_code=422,
            detail="Invalid date range: 'from' date must be before or equal to 'to' date.",
        )

    day_span = (to_dt - from_dt).days
    if day_span > 92:
        raise HTTPException(
            status_code=422,
            detail=f"Date range exceeds maximum allowed window of 92 days (requested {day_span} days). Please narrow the range.",
        )

    MAX_ROWS = 50000
    PAGE_SIZE = 1000
    scanned_count = 0

    # 1. Fetch appointments in range
    appointments: list[dict] = []
    offset = 0
    while True:
        q = (
            scoped_query("appointments", clinic_id)
            .gte("appointment_date", from_date_str)
            .lte("appointment_date", to_date_str)
        )
        if branch_id:
            q = q.eq("branch_id", branch_id)
        batch_res = await sb(q.range(offset, offset + PAGE_SIZE - 1))
        batch = batch_res.data or []
        appointments.extend(batch)
        scanned_count += len(batch)
        if scanned_count > MAX_ROWS:
            raise HTTPException(
                status_code=422,
                detail=f"Data volume exceeds 50,000 row cap ({scanned_count} rows). Please narrow the date range.",
            )
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    # 2. Fetch opd_invoices in range
    invoices: list[dict] = []
    offset = 0
    from_iso = f"{from_date_str}T00:00:00+05:30"
    to_iso = f"{to_date_str}T23:59:59+05:30"
    while True:
        q = (
            scoped_query("opd_invoices", clinic_id)
            .gte("created_at", from_iso)
            .lte("created_at", to_iso)
        )
        if branch_id:
            q = q.eq("branch_id", branch_id)
        batch_res = await sb(q.range(offset, offset + PAGE_SIZE - 1))
        batch = batch_res.data or []
        invoices.extend(batch)
        scanned_count += len(batch)
        if scanned_count > MAX_ROWS:
            raise HTTPException(
                status_code=422,
                detail=f"Data volume exceeds 50,000 row cap ({scanned_count} rows). Please narrow the date range.",
            )
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    # 3. Fetch opd_receipts in range
    receipts: list[dict] = []
    offset = 0
    while True:
        q = (
            scoped_query("opd_receipts", clinic_id)
            .gte("created_at", from_iso)
            .lte("created_at", to_iso)
        )
        batch_res = await sb(q.range(offset, offset + PAGE_SIZE - 1))
        batch = batch_res.data or []
        receipts.extend(batch)
        scanned_count += len(batch)
        if scanned_count > MAX_ROWS:
            raise HTTPException(
                status_code=422,
                detail=f"Data volume exceeds 50,000 row cap ({scanned_count} rows). Please narrow the date range.",
            )
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if branch_id:
        branch_inv_ids = {str(inv["id"]) for inv in invoices}
        receipts = [r for r in receipts if str(r.get("invoice_id")) in branch_inv_ids]

    # 4. Fetch opd_prescriptions in range
    prescriptions: list[dict] = []
    offset = 0
    while True:
        q = (
            scoped_query("opd_prescriptions", clinic_id)
            .gte("created_at", from_iso)
            .lte("created_at", to_iso)
        )
        batch_res = await sb(q.range(offset, offset + PAGE_SIZE - 1))
        batch = batch_res.data or []
        prescriptions.extend(batch)
        scanned_count += len(batch)
        if scanned_count > MAX_ROWS:
            raise HTTPException(
                status_code=422,
                detail=f"Data volume exceeds 50,000 row cap ({scanned_count} rows). Please narrow the date range.",
            )
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if branch_id:
        branch_appt_ids = {str(a["id"]) for a in appointments}
        prescriptions = [p for p in prescriptions if str(p.get("appointment_id")) in branch_appt_ids]

    # Footfall aggregation
    footfall: dict = {
        "total": len(appointments),
        "walk_in": sum(1 for a in appointments if a.get("is_walk_in")),
        "booked": sum(1 for a in appointments if not a.get("is_walk_in")),
        "by_channel": {
            "whatsapp": sum(1 for a in appointments if a.get("booking_channel") == "whatsapp"),
            "voice": sum(1 for a in appointments if a.get("booking_channel") == "voice"),
            "front_desk": sum(1 for a in appointments if a.get("booking_channel") == "front_desk"),
            "web": sum(1 for a in appointments if a.get("booking_channel") == "web"),
            "unknown": sum(
                1 for a in appointments
                if not a.get("booking_channel") or a.get("booking_channel") not in ("whatsapp", "voice", "front_desk", "web")
            ),
        },
        "by_visit_type": {
            "new": sum(1 for a in appointments if a.get("visit_type") == "new"),
            "followup": sum(1 for a in appointments if a.get("visit_type") == "followup"),
            "review": sum(1 for a in appointments if a.get("visit_type") == "review"),
            "unknown": sum(
                1 for a in appointments
                if not a.get("visit_type") or a.get("visit_type") not in ("new", "followup", "review")
            ),
        },
        "by_doctor": {},
        "by_department": {},
        "by_day": {},
    }

    for a in appointments:
        doc = a.get("doctor_name") or "Unassigned"
        footfall["by_doctor"][doc] = footfall["by_doctor"].get(doc, 0) + 1

        dept = a.get("department") or "General"
        footfall["by_department"][dept] = footfall["by_department"].get(dept, 0) + 1

        day = str(a.get("appointment_date") or "")
        if day:
            footfall["by_day"][day] = footfall["by_day"].get(day, 0) + 1

    # Wait & consultation durations
    wait_times: list[float] = []
    consult_times: list[float] = []

    for a in appointments:
        raw_timeline = a.get("queue_timeline")
        timeline = raw_timeline if isinstance(raw_timeline, dict) else {}
        checked_in = a.get("checked_in_at") or timeline.get("registered") or timeline.get("waiting")
        in_consult = timeline.get("in_consultation")
        finished = timeline.get("billing") or timeline.get("completed")

        if checked_in and in_consult:
            try:
                t0 = datetime.fromisoformat(str(checked_in).replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(str(in_consult).replace("Z", "+00:00"))
                diff = (t1 - t0).total_seconds() / 60.0
                if diff >= 0:
                    wait_times.append(diff)
            except Exception:
                pass

        if in_consult and finished:
            try:
                t1 = datetime.fromisoformat(str(in_consult).replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
                diff = (t2 - t1).total_seconds() / 60.0
                if diff >= 0:
                    consult_times.append(diff)
            except Exception:
                pass

    waits = {
        "wait_minutes_p50": _percentile(wait_times, 50),
        "wait_minutes_p90": _percentile(wait_times, 90),
        "consult_minutes_p50": _percentile(consult_times, 50),
        "consult_minutes_p90": _percentile(consult_times, 90),
    }

    # No-shows & Cancellations
    today_str = today_ist().isoformat()
    no_shows = sum(
        1 for a in appointments
        if not a.get("is_walk_in")
        and a.get("status") == "confirmed"
        and str(a.get("appointment_date") or "") < today_str
        and not a.get("checked_in_at")
        and a.get("token_number") is None
    )

    cancellations_in_queue = sum(
        1 for a in appointments
        if (a.get("status") == "cancelled" or a.get("queue_status") == "cancelled")
        and (a.get("token_number") is not None or a.get("checked_in_at") is not None)
    )

    # Collections & Invoices
    invoices_by_status: dict[str, int] = {}
    outstanding_paise = 0
    for inv in invoices:
        st = inv.get("status") or "draft"
        invoices_by_status[st] = invoices_by_status.get(st, 0) + 1
        if st in ("issued", "partially_paid"):
            tot = inv.get("total_paise", 0)
            pd = inv.get("paid_paise", 0)
            outstanding_paise += max(0, tot - pd)

    gross_by_mode: dict[str, int] = {}
    refunds_paise = 0
    online_paise = 0
    counter_paise = 0

    for r in receipts:
        amt = r.get("amount_paise") or 0
        kind = r.get("kind") or "payment"
        mode = r.get("mode") or "cash"

        if kind == "refund":
            refunds_paise += amt
        else:
            gross_by_mode[mode] = gross_by_mode.get(mode, 0) + amt
            if mode in ("upi", "payment_link", "prepaid_online", "razorpay", "phonepe") or r.get("gateway"):
                online_paise += amt
            else:
                counter_paise += amt

    total_gross = sum(gross_by_mode.values())
    collections = {
        "gross_by_mode": gross_by_mode,
        "refunds": refunds_paise,
        "net": total_gross - refunds_paise,
        "online_vs_counter": {
            "online": online_paise,
            "counter": counter_paise,
        },
        "outstanding_paise": outstanding_paise,
        "invoices_by_status": invoices_by_status,
    }

    # Prescriptions
    prescriptions_summary = {
        "signed": sum(1 for rx in prescriptions if rx.get("signed_at")),
        "sent_whatsapp": sum(1 for rx in prescriptions if rx.get("delivery_status") == "sent"),
        "send_failed": sum(1 for rx in prescriptions if rx.get("delivery_status") == "failed"),
        "amended": sum(1 for rx in prescriptions if rx.get("is_amended") or rx.get("superseded_by")),
    }

    # Peak hours (IST hour 0..23)
    hour_counts = {h: 0 for h in range(24)}
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    for a in appointments:
        chk = a.get("checked_in_at")
        if chk:
            try:
                dt = datetime.fromisoformat(str(chk).replace("Z", "+00:00"))
                dt_ist = dt.astimezone(ist_tz)
                hour_counts[dt_ist.hour] += 1
            except Exception:
                pass

    peak_hours = [{"hour": h, "check_ins": hour_counts[h]} for h in range(24)]

    return {
        "footfall": footfall,
        "waits": waits,
        "no_shows": no_shows,
        "cancellations_in_queue": cancellations_in_queue,
        "collections": collections,
        "prescriptions": prescriptions_summary,
        "peak_hours": peak_hours,
    }
