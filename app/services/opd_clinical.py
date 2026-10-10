"""Kriya OPD OS Clinical Service (Phase 1.3).

Encounters, vitals, clinical notes, deterministic allergy screening,
amendment chains, and e-prescription lifecycle.
Zero-LLM clinical safety compliant (NMC regulation mandate).
"""

import logging
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException, status

from app.database import sb, scoped_query, supabase
from app.services.tenant import get_clinic_by_id
from app.utils.helpers import IST, today_ist

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# DETERMINISTIC ALLERGY KNOWLEDGE BASE (Zero-LLM)
# ═══════════════════════════════════════════════════════════════════════════════

ALLERGY_CLASSES: dict[str, frozenset[str]] = {
    "penicillin": frozenset({
        "penicillin", "amoxicillin", "ampicillin", "piperacillin",
        "oxacillin", "cloxacillin", "methicillin", "augmentin",
        "amoxil", "ampiclox", "moxikind", "moxclav", "clavulanic",
        "amoxiclav"
    }),
    "sulfa": frozenset({
        "sulfa", "sulfamethoxazole", "trimethoprim", "bactrim",
        "septra", "sulfadiazine", "sulfasalazine", "sulfisoxazole",
        "cotrimoxazole"
    }),
    "nsaid": frozenset({
        "nsaid", "ibuprofen", "diclofenac", "aspirin", "naproxen",
        "aceclofenac", "indomethacin", "ketorolac", "mefenamic",
        "piroxicam", "meloxicam", "celecoxib", "etoricoxib"
    }),
    "cephalosporin": frozenset({
        "cephalosporin", "cefixime", "ceftriaxone", "cefuroxime",
        "cephalexin", "cefpodoxime", "cefazolin", "cefepime",
        "cefotaxime", "cefdinir", "cefadroxil", "taxim", "monocef"
    }),
    "macrolide": frozenset({
        "macrolide", "azithromycin", "clarithromycin", "erythromycin",
        "roxithromycin", "azithral", "zithromax"
    }),
    "fluoroquinolone": frozenset({
        "fluoroquinolone", "ciprofloxacin", "levofloxacin", "ofloxacin",
        "moxifloxacin", "norfloxacin", "cifran", "ciplox", "levoquine"
    }),
    "opioid": frozenset({
        "opioid", "tramadol", "codeine", "morphine", "fentanyl",
        "oxycodone", "hydrocodone", "buprenorphine", "pethidine",
        "pentazocine", "ultram"
    }),
}


def _clean_token(text: str) -> str:
    """Normalize text by stripping non-alphanumeric chars and lowering case."""
    return re.sub(r"[^a-z0-9\s]", " ", (text or "").lower()).strip()


def allergy_warnings(
    allergies: list[str],
    allergies_status: str,
    items: list[dict],
) -> list[dict]:
    """Deterministic, zero-LLM allergy cross-check between patient allergies and Rx items.

    Returns a list of warning dicts:
    [{"line_no": int, "allergen": str, "drug": str, "message": str}]
    """
    warnings: list[dict] = []

    if allergies_status == "unknown":
        warnings.append({
            "line_no": 0,
            "allergen": "unknown",
            "drug": "",
            "message": "Allergy history not recorded",
        })
        return warnings

    if allergies_status != "recorded" or not allergies or not items:
        return []

    # Map each allergen to its normalized form and matching class keys
    cleaned_allergens: list[tuple[str, str, set[str]]] = []
    for raw_allergen in allergies:
        norm_a = _clean_token(raw_allergen)
        if not norm_a:
            continue
        # Find which classes (if any) this allergen corresponds to
        classes: set[str] = set()
        for cls_name, cls_drugs in ALLERGY_CLASSES.items():
            if norm_a == cls_name or norm_a in cls_drugs or cls_name in norm_a:
                classes.add(cls_name)
        cleaned_allergens.append((raw_allergen, norm_a, classes))

    seen_warnings = set()

    for item in items:
        line_no = item.get("line_no", 1)
        drug_name = item.get("drug_name", "")
        norm_drug = _clean_token(drug_name)
        if not norm_drug:
            continue
        drug_tokens = set(norm_drug.split())

        for raw_allergen, norm_a, classes in cleaned_allergens:
            pair_key = (line_no, norm_a)
            if pair_key in seen_warnings:
                continue

            # 1. Direct substring match (either allergen is in drug name or drug token matches allergen)
            allergen_tokens = set(norm_a.split())
            if norm_a in norm_drug or any(tok in drug_tokens for tok in allergen_tokens):
                warnings.append({
                    "line_no": line_no,
                    "allergen": raw_allergen,
                    "drug": drug_name,
                    "message": f"Potential allergy match: patient has recorded allergy to '{raw_allergen}', prescribed '{drug_name}'",
                })
                seen_warnings.add(pair_key)
                continue

            # 2. Class match
            matched_cls = None
            for cls_name in classes:
                cls_drugs = ALLERGY_CLASSES[cls_name]
                if any(cd in norm_drug or cd in drug_tokens for cd in cls_drugs):
                    matched_cls = cls_name
                    break

            if matched_cls:
                warnings.append({
                    "line_no": line_no,
                    "allergen": raw_allergen,
                    "drug": drug_name,
                    "message": f"Class warning ({matched_cls}): patient allergic to '{raw_allergen}', drug '{drug_name}' belongs to class {matched_cls}",
                })
                seen_warnings.add(pair_key)

    return warnings


# ═══════════════════════════════════════════════════════════════════════════════
# PATIENT ALLERGIES RESOLUTION
# ═══════════════════════════════════════════════════════════════════════════════

async def get_patient_allergies(
    clinic_id: str,
    patient_id: str,
    family_member_id: Optional[str] = None,
) -> tuple[list[str], str]:
    """Retrieve patient or family member allergies and allergies_status."""
    if family_member_id:
        res = await sb(
            supabase.table("family_members")
            .select("allergies, allergies_status")
            .eq("clinic_id", clinic_id)
            .eq("id", family_member_id)
            .single()
        )
        if res.data:
            return res.data.get("allergies") or [], res.data.get("allergies_status") or "unknown"

    res = await sb(
        supabase.table("patients")
        .select("allergies, allergies_status")
        .eq("clinic_id", clinic_id)
        .eq("id", patient_id)
        .single()
    )
    if res.data:
        return res.data.get("allergies") or [], res.data.get("allergies_status") or "unknown"
    return [], "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# ENCOUNTER MANAGEMENT (§3.5)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_or_create_draft(
    clinic_id: str,
    appointment_id: str,
    actor_id: Optional[str] = None,
) -> dict:
    """Return the active draft encounter for an appointment, or create version 1."""
    # Look for existing draft
    draft_res = await sb(
        supabase.table("opd_encounters")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("appointment_id", appointment_id)
        .eq("status", "draft")
        .limit(1)
    )
    if draft_res.data:
        return draft_res.data[0]

    # Look for existing signed encounter if no draft
    signed_res = await sb(
        supabase.table("opd_encounters")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("appointment_id", appointment_id)
        .eq("status", "signed")
        .limit(1)
    )
    if signed_res.data:
        return signed_res.data[0]

    # Fetch appointment details to initialize encounter
    apt_res = await sb(
        supabase.table("appointments")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", appointment_id)
        .single()
    )
    if not apt_res.data:
        raise HTTPException(status_code=404, detail="Appointment not found")

    apt = apt_res.data
    now_iso = datetime.now(timezone.utc).isoformat()

    new_enc = {
        "clinic_id": clinic_id,
        "branch_id": apt.get("branch_id"),
        "appointment_id": appointment_id,
        "patient_id": apt.get("patient_id"),
        "family_member_id": apt.get("family_member_id"),
        "doctor_id": apt.get("doctor_id"),
        "version": 1,
        "status": "draft",
        "created_by": actor_id,
        "started_at": now_iso,
    }

    # unscoped: insert_scoped_by_payload
    insert_res = await sb(supabase.table("opd_encounters").insert(new_enc))
    if not insert_res.data:
        raise HTTPException(status_code=500, detail="Failed to initialize encounter")
    return insert_res.data[0]


async def save_vitals(
    clinic: dict,
    appointment_id: str,
    vitals: dict,
    actor: dict,
) -> dict:
    """Save vitals on the draft encounter and advance vitals_pending -> waiting."""
    clinic_id = str(clinic["id"])
    encounter = await get_or_create_draft(clinic_id, appointment_id, actor.get("user_id"))

    if encounter.get("status") != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Encounter is already signed or superseded",
        )

    # Validate vitals ranges
    bp_sys = vitals.get("bp_systolic")
    bp_dia = vitals.get("bp_diastolic")
    pulse = vitals.get("pulse_bpm")
    temp = vitals.get("temperature_c")
    spo2 = vitals.get("spo2_pct")
    wt = vitals.get("weight_kg")
    ht = vitals.get("height_cm")

    if bp_sys is not None and not (50 <= bp_sys <= 300):
        raise HTTPException(status_code=422, detail="Systolic BP must be between 50 and 300")
    if bp_dia is not None and not (20 <= bp_dia <= 200):
        raise HTTPException(status_code=422, detail="Diastolic BP must be between 20 and 200")
    if bp_sys is not None and bp_dia is not None and bp_sys <= bp_dia:
        raise HTTPException(status_code=422, detail="Systolic BP must be greater than diastolic BP")
    if pulse is not None and not (20 <= pulse <= 250):
        raise HTTPException(status_code=422, detail="Pulse must be between 20 and 250")
    if temp is not None and not (30.0 <= float(temp) <= 45.0):
        raise HTTPException(status_code=422, detail="Temperature must be between 30 and 45 °C")
    if spo2 is not None and not (50 <= spo2 <= 100):
        raise HTTPException(status_code=422, detail="SpO2 must be between 50 and 100%")
    if wt is not None and not (0.01 <= float(wt) <= 400.0):
        raise HTTPException(status_code=422, detail="Weight must be between 0.01 and 400 kg")
    if ht is not None and not (30.0 <= float(ht) <= 250.0):
        raise HTTPException(status_code=422, detail="Height must be between 30 and 250 cm")

    now_iso = datetime.now(timezone.utc).isoformat()
    update_data = {
        "bp_systolic": bp_sys,
        "bp_diastolic": bp_dia,
        "pulse_bpm": pulse,
        "temperature_c": float(temp) if temp is not None else None,
        "spo2_pct": spo2,
        "weight_kg": float(wt) if wt is not None else None,
        "height_cm": float(ht) if ht is not None else None,
        "vitals_recorded_at": now_iso,
        "vitals_recorded_by": actor.get("user_id"),
        "vitals_recorded_by_name": actor.get("name") or actor.get("email"),
        "updated_at": now_iso,
    }

    upd_res = await sb(
        supabase.table("opd_encounters")
        .update(update_data)
        .eq("clinic_id", clinic_id)
        .eq("id", encounter["id"])
    )
    if not upd_res.data:
        raise HTTPException(status_code=500, detail="Failed to save vitals")
    saved_enc = upd_res.data[0]

    # Advance queue stage if currently vitals_pending
    apt_res = await sb(
        supabase.table("appointments")
        .select("queue_status")
        .eq("clinic_id", clinic_id)
        .eq("id", appointment_id)
        .single()
    )
    if apt_res.data and apt_res.data.get("queue_status") == "vitals_pending":
        from app.services.opd import transition_stage
        try:
            await transition_stage(clinic, appointment_id, "waiting", "vitals_pending", actor)
        except Exception as e:
            logger.warning(f"Could not advance queue from vitals_pending to waiting: {e}")

    return saved_enc


async def save_notes(
    clinic_id: str,
    encounter_id: str,
    notes: dict,
    actor: dict,
) -> dict:
    """Save clinical notes and diagnoses with CAS optimistic locking."""
    enc_res = await sb(
        supabase.table("opd_encounters")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", encounter_id)
        .single()
    )
    if not enc_res.data:
        raise HTTPException(status_code=404, detail="Encounter not found")
    encounter = enc_res.data

    if encounter.get("status") != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Encounter is already signed or superseded",
        )

    # CLINICAL: treating doctor only
    user_doc_id = actor.get("doctor_id")
    if not user_doc_id or str(user_doc_id) != str(encounter.get("doctor_id")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the treating doctor can edit encounter notes",
        )

    # CAS check on expected_updated_at
    expected_updated_at = notes.get("expected_updated_at")
    if expected_updated_at:
        exp_iso = expected_updated_at.isoformat() if hasattr(expected_updated_at, "isoformat") else str(expected_updated_at)
        curr_iso = str(encounter.get("updated_at") or "")
        # Compare ISO prefixes to avoid microsecond formatting drift
        if exp_iso[:19] != curr_iso[:19]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "stale_update", "message": "Encounter was modified elsewhere"},
            )

    follow_up = notes.get("follow_up_date")
    if follow_up:
        fu_date = date.fromisoformat(str(follow_up)) if isinstance(follow_up, str) else follow_up
        if fu_date < today_ist():
            raise HTTPException(status_code=422, detail="Follow up date must be today or in the future")

    now_iso = datetime.now(timezone.utc).isoformat()
    update_data = {
        "chief_complaints": notes.get("chief_complaints"),
        "clinical_findings": notes.get("clinical_findings"),
        "examination_notes": notes.get("examination_notes"),
        "diagnoses": notes.get("diagnoses") or [],
        "advice": notes.get("advice"),
        "follow_up_date": str(follow_up) if follow_up else None,
        "updated_at": now_iso,
    }

    # Conditional write: the check above alone let two concurrent saves both pass
    # and the later silently overwrite the earlier (lost update).
    upd_q = (
        supabase.table("opd_encounters")
        .update(update_data)
        .eq("clinic_id", clinic_id)
        .eq("id", encounter_id)
        .eq("status", "draft")
    )
    if encounter.get("updated_at"):
        upd_q = upd_q.eq("updated_at", encounter["updated_at"])
    upd_res = await sb(upd_q)
    if not upd_res.data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "stale_update", "message": "Encounter was modified elsewhere"},
        )
    return upd_res.data[0]


async def sign_encounter(
    clinic: dict,
    encounter_id: str,
    user: dict,
) -> dict:
    """Sign and lock encounter via opd_sign_encounter RPC."""
    clinic_id = str(clinic["id"])
    enc_res = await sb(
        supabase.table("opd_encounters")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", encounter_id)
        .single()
    )
    if not enc_res.data:
        raise HTTPException(status_code=404, detail="Encounter not found")
    encounter = enc_res.data

    if encounter.get("status") != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Encounter is already signed or superseded",
        )

    # Strictly require user.doctor_id == encounter.doctor_id
    signer_doc_id = user.get("doctor_id")
    if not signer_doc_id or str(signer_doc_id) != str(encounter.get("doctor_id")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the linked treating doctor can sign this encounter",
        )

    # Doctor must have registration_number and registration_council
    doc_res = await sb(
        supabase.table("doctors")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", signer_doc_id)
        .single()
    )
    if not doc_res.data:
        raise HTTPException(status_code=404, detail="Doctor not found")
    doctor = doc_res.data

    reg_no = doctor.get("registration_number")
    council = doctor.get("registration_council")
    if not reg_no or not council:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "registration_missing", "message": "Doctor registration number and council are required to sign encounters"},
        )

    # Clinical content check: chief_complaints or >=1 diagnosis
    cc = encounter.get("chief_complaints") or ""
    diagnoses = encounter.get("diagnoses") or []
    if not cc.strip() and len(diagnoses) == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "clinical_content_required", "message": "Chief complaints or at least one diagnosis is required to sign encounter"},
        )

    signer_snapshot = {
        "doctor_id": str(doctor["id"]),
        "name": doctor.get("name", ""),
        "qualifications": doctor.get("qualifications", ""),
        "registration_number": reg_no,
        "registration_council": council,
    }

    try:
        rpc_res = await sb(
            supabase.rpc(
                "opd_sign_encounter",
                {
                    "p_clinic_id": clinic_id,
                    "p_encounter_id": encounter_id,
                    "p_signer_admin_id": user.get("user_id"),
                    "p_signer_doctor_id": signer_doc_id,
                    "p_signer_snapshot": signer_snapshot,
                },
            )
        )
    except Exception as e:
        err_msg = str(e).lower()
        if "opd_record_locked" in err_msg or "opd_not_draft" in err_msg:
            raise HTTPException(status_code=409, detail="Encounter is locked or not a draft")
        if "opd_not_treating_doctor" in err_msg:
            raise HTTPException(status_code=403, detail="Not treating doctor")
        raise HTTPException(status_code=500, detail=f"Failed to sign encounter: {e}")

    signed_row = dict(rpc_res.data[0] if isinstance(rpc_res.data, list) and rpc_res.data else (rpc_res.data or {}))

    # If appointment was in_consultation, advance to billing stage
    apt_id = encounter.get("appointment_id")
    if apt_id:
        apt_res = await sb(
            supabase.table("appointments")
            .select("queue_status")
            .eq("clinic_id", clinic_id)
            .eq("id", apt_id)
            .single()
        )
        if apt_res.data and apt_res.data.get("queue_status") == "in_consultation":
            from app.services.opd import transition_stage
            try:
                await transition_stage(clinic, apt_id, "billing", "in_consultation", user)
            except Exception as e:
                logger.warning(f"Could not advance queue from in_consultation to billing: {e}")

    return signed_row


async def amend_encounter(
    clinic: dict,
    encounter_id: str,
    reason: str,
    user: dict,
) -> dict:
    """Create a new draft encounter (v = current.v + 1) superseding the signed encounter."""
    clinic_id = str(clinic["id"])
    enc_res = await sb(
        supabase.table("opd_encounters")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", encounter_id)
        .single()
    )
    if not enc_res.data:
        raise HTTPException(status_code=404, detail="Encounter not found")
    encounter = enc_res.data

    if encounter.get("status") != "signed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only signed encounters can be amended",
        )

    user_doc_id = user.get("doctor_id")
    if not user_doc_id or str(user_doc_id) != str(encounter.get("doctor_id")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the treating doctor can amend this encounter",
        )

    # Check if a draft already exists for this appointment
    draft_check = await sb(
        supabase.table("opd_encounters")
        .select("id")
        .eq("clinic_id", clinic_id)
        .eq("appointment_id", encounter["appointment_id"])
        .eq("status", "draft")
    )
    if draft_check.data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An amendment draft already exists for this appointment",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    new_version = encounter.get("version", 1) + 1

    new_enc = {
        "clinic_id": clinic_id,
        "branch_id": encounter.get("branch_id"),
        "appointment_id": encounter.get("appointment_id"),
        "patient_id": encounter.get("patient_id"),
        "family_member_id": encounter.get("family_member_id"),
        "doctor_id": encounter.get("doctor_id"),
        "version": new_version,
        "supersedes_id": encounter_id,
        "status": "draft",
        "bp_systolic": encounter.get("bp_systolic"),
        "bp_diastolic": encounter.get("bp_diastolic"),
        "pulse_bpm": encounter.get("pulse_bpm"),
        "temperature_c": encounter.get("temperature_c"),
        "spo2_pct": encounter.get("spo2_pct"),
        "weight_kg": encounter.get("weight_kg"),
        "height_cm": encounter.get("height_cm"),
        "vitals_recorded_at": encounter.get("vitals_recorded_at"),
        "vitals_recorded_by": encounter.get("vitals_recorded_by"),
        "vitals_recorded_by_name": encounter.get("vitals_recorded_by_name"),
        "chief_complaints": encounter.get("chief_complaints"),
        "clinical_findings": encounter.get("clinical_findings"),
        "examination_notes": encounter.get("examination_notes"),
        "diagnoses": encounter.get("diagnoses") or [],
        "advice": encounter.get("advice"),
        "follow_up_date": encounter.get("follow_up_date"),
        "created_by": user.get("user_id"),
        "started_at": now_iso,
    }

    # unscoped: insert_scoped_by_payload
    insert_res = await sb(supabase.table("opd_encounters").insert(new_enc))
    if not insert_res.data:
        raise HTTPException(status_code=500, detail="Failed to create amendment draft")
    return insert_res.data[0]


async def patient_history(
    clinic_id: str,
    patient_id: str,
    family_member_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Retrieve signed/superseded encounters history ordered newest first."""
    query = (
        supabase.table("opd_encounters")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("patient_id", patient_id)
        .in_("status", ["signed", "superseded"])
        .order("created_at", desc=True)
        .limit(limit)
    )
    if family_member_id:
        query = query.eq("family_member_id", family_member_id)
    else:
        query = query.is_("family_member_id", "null")

    res = await sb(query)
    encounters = res.data or []

    # Attach prescription summary to each encounter
    for enc in encounters:
        rx_res = await sb(
            supabase.table("opd_prescriptions")
            .select("id, version, status, signed_at")
            .eq("clinic_id", clinic_id)
            .eq("encounter_id", enc["id"])
            .in_("status", ["signed", "superseded"])
            .limit(1)
        )
        enc["prescription"] = rx_res.data[0] if rx_res.data else None

    return encounters


# ═══════════════════════════════════════════════════════════════════════════════
# e-PRESCRIPTION MANAGEMENT (§3.6)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_encounter_prescription(
    clinic_id: str,
    encounter_id: str,
) -> Optional[dict]:
    """Fetch prescription and its items for an encounter (draft or signed)."""
    rx_res = await sb(
        supabase.table("opd_prescriptions")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("encounter_id", encounter_id)
        .order("version", desc=True)
        .limit(1)
    )
    if not rx_res.data:
        return None

    rx = rx_res.data[0]
    items_res = await sb(
        supabase.table("opd_prescription_items")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("prescription_id", rx["id"])
        .order("line_no")
    )
    rx["items"] = items_res.data or []

    # Compute allergy warnings
    allergies, status_code = await get_patient_allergies(
        clinic_id, rx["patient_id"], rx.get("family_member_id")
    )
    rx["warnings"] = allergy_warnings(allergies, status_code, rx["items"])
    return rx


async def save_prescription_draft(
    clinic_id: str,
    encounter_id: str,
    draft: dict,
    actor: dict,
) -> dict:
    """Save prescription draft and replace items atomically."""
    enc_res = await sb(
        supabase.table("opd_encounters")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", encounter_id)
        .single()
    )
    if not enc_res.data:
        raise HTTPException(status_code=404, detail="Encounter not found")
    encounter = enc_res.data

    user_doc_id = actor.get("doctor_id")
    if not user_doc_id or str(user_doc_id) != str(encounter.get("doctor_id")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the treating doctor can edit the prescription",
        )

    # Look for existing draft prescription
    rx_res = await sb(
        supabase.table("opd_prescriptions")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("encounter_id", encounter_id)
        .eq("status", "draft")
        .limit(1)
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    if rx_res.data:
        rx = rx_res.data[0]
        # CAS check
        expected_updated_at = draft.get("expected_updated_at")
        if expected_updated_at:
            exp_iso = expected_updated_at.isoformat() if hasattr(expected_updated_at, "isoformat") else str(expected_updated_at)
            curr_iso = str(rx.get("updated_at") or "")
            if exp_iso[:19] != curr_iso[:19]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"error": "stale_update", "message": "Prescription was modified elsewhere"},
                )

        rx_id = rx["id"]
        # Update header
        upd_res = await sb(
            supabase.table("opd_prescriptions")
            .update({
                "general_instructions": draft.get("general_instructions"),
                "updated_at": now_iso,
            })
            .eq("clinic_id", clinic_id)
            .eq("id", rx_id)
        )
        saved_rx = upd_res.data[0]
    else:
        # Create draft header
        new_rx = {
            "clinic_id": clinic_id,
            "encounter_id": encounter_id,
            "appointment_id": encounter["appointment_id"],
            "patient_id": encounter["patient_id"],
            "family_member_id": encounter.get("family_member_id"),
            "doctor_id": encounter["doctor_id"],
            "version": 1,
            "status": "draft",
            "general_instructions": draft.get("general_instructions"),
            "created_by": actor.get("user_id"),
        }
        # unscoped: insert_scoped_by_payload
        ins_res = await sb(supabase.table("opd_prescriptions").insert(new_rx))
        if not ins_res.data:
            raise HTTPException(status_code=500, detail="Failed to initialize prescription draft")
        saved_rx = ins_res.data[0]
        rx_id = saved_rx["id"]

    # Delete existing items for draft
    await sb(
        supabase.table("opd_prescription_items")
        .delete()
        .eq("clinic_id", clinic_id)
        .eq("prescription_id", rx_id)
    )

    # Insert new items with line numbers
    raw_items = draft.get("items") or []
    item_rows = []
    for idx, itm in enumerate(raw_items, start=1):
        item_rows.append({
            "clinic_id": clinic_id,
            "prescription_id": rx_id,
            "line_no": idx,
            "drug_name": itm.get("drug_name", "").strip(),
            "formulation": itm.get("formulation"),
            "strength": itm.get("strength"),
            "dosage": itm.get("dosage", "").strip(),
            "route": itm.get("route", "oral"),
            "frequency": itm.get("frequency", "").strip(),
            "timing": itm.get("timing", "any"),
            "duration_days": itm.get("duration_days"),
            "instructions": itm.get("instructions"),
        })

    if item_rows:
        # unscoped: insert_scoped_by_payload
        items_ins = await sb(supabase.table("opd_prescription_items").insert(item_rows))
        saved_rx["items"] = items_ins.data or []
    else:
        saved_rx["items"] = []

    # Compute allergy warnings
    allergies, status_code = await get_patient_allergies(
        clinic_id, saved_rx["patient_id"], saved_rx.get("family_member_id")
    )
    saved_rx["warnings"] = allergy_warnings(allergies, status_code, saved_rx["items"])
    return saved_rx


async def sign_prescription(
    clinic: dict,
    rx_id: str,
    user: dict,
    acknowledgements: list[dict],
) -> dict:
    """Sign and lock prescription via opd_sign_prescription RPC."""
    clinic_id = str(clinic["id"])
    rx_res = await sb(
        supabase.table("opd_prescriptions")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", rx_id)
        .single()
    )
    if not rx_res.data:
        raise HTTPException(status_code=404, detail="Prescription not found")
    rx = rx_res.data

    if rx.get("status") != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Prescription is already signed or superseded",
        )

    signer_doc_id = user.get("doctor_id")
    if not signer_doc_id or str(signer_doc_id) != str(rx.get("doctor_id")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the treating doctor can sign this prescription",
        )

    # Encounter must be signed
    enc_res = await sb(
        supabase.table("opd_encounters")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", rx["encounter_id"])
        .single()
    )
    if not enc_res.data or enc_res.data.get("status") not in ("signed", "superseded"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "opd_encounter_not_signed", "message": "Encounter must be signed before signing prescription"},
        )
    encounter = enc_res.data

    # Doctor registration check
    doc_res = await sb(
        supabase.table("doctors")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", signer_doc_id)
        .single()
    )
    if not doc_res.data:
        raise HTTPException(status_code=404, detail="Doctor not found")
    doctor = doc_res.data

    reg_no = doctor.get("registration_number")
    council = doctor.get("registration_council")
    if not reg_no or not council:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "registration_missing", "message": "Doctor registration number and council are required to sign"},
        )

    # Must have items
    items_res = await sb(
        supabase.table("opd_prescription_items")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("prescription_id", rx_id)
        .order("line_no")
    )
    items = items_res.data or []
    if not items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "prescription_empty", "message": "Prescription has no items to sign"},
        )

    # Server recomputes warnings
    allergies, status_code = await get_patient_allergies(
        clinic_id, rx["patient_id"], rx.get("family_member_id")
    )
    computed_warnings = allergy_warnings(allergies, status_code, items)

    # Verify every warning is acknowledged with override_reason >= 5 chars
    allergy_review = []
    unacked = []

    for w in computed_warnings:
        w_line = w["line_no"]
        w_allergen = _clean_token(w["allergen"])

        # Match ack by line_no and allergen (or allergen only)
        match_ack = None
        for ack in acknowledgements:
            ack_line = ack.get("line_no")
            ack_allergen = _clean_token(ack.get("allergen", ""))
            reason = (ack.get("override_reason") or "").strip()
            if (ack_line == w_line or ack_allergen == w_allergen) and len(reason) >= 5:
                match_ack = ack
                break

        if not match_ack:
            unacked.append(w)
        else:
            allergy_review.append({
                "line_no": w_line,
                "allergen": w["allergen"],
                "drug": w.get("drug", ""),
                "override_reason": match_ack.get("override_reason", "").strip(),
            })

    if unacked:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "unacknowledged_warnings",
                "message": "Every allergy warning requires an override reason (min 5 characters)",
                "warnings": computed_warnings,
            },
        )

    # Build snapshots
    signer_snapshot = {
        "doctor_id": str(doctor["id"]),
        "name": doctor.get("name", ""),
        "qualifications": doctor.get("qualifications", ""),
        "registration_number": reg_no,
        "registration_council": council,
    }

    # Fetch branch if present
    branch_name, branch_addr = None, None
    if encounter.get("branch_id"):
        br_res = await sb(
            supabase.table("branches")
            .select("name, address")
            .eq("clinic_id", clinic_id)
            .eq("id", encounter["branch_id"])
            .single()
        )
        if br_res.data:
            branch_name = br_res.data.get("name")
            branch_addr = br_res.data.get("address")

    from app.services.tenant import clinic_letterhead
    lh = clinic_letterhead(clinic)
    letterhead_snapshot = {
        "clinic_name": clinic.get("name", ""),
        "address": lh["address"],
        "phone": lh["phone"],
        "email": clinic.get("email", ""),
        "branch_name": branch_name,
        "branch_address": branch_addr,
    }

    # Fetch patient row
    pat_res = await sb(
        supabase.table("patients")
        .select("id, name, mrn, age:age_years, gender, phone")
        .eq("clinic_id", clinic_id)
        .eq("id", rx["patient_id"])
        .single()
    )
    patient_row = pat_res.data or {}

    # If family member, override demographics
    if rx.get("family_member_id"):
        fam_res = await sb(
            supabase.table("family_members")
            .select("id, name:full_name, mrn, age:age_years, gender")
            .eq("clinic_id", clinic_id)
            .eq("id", rx["family_member_id"])
            .single()
        )
        if fam_res.data:
            fam = fam_res.data
            patient_row["name"] = fam.get("name")
            patient_row["mrn"] = fam.get("mrn")
            patient_row["age"] = fam.get("age")
            patient_row["gender"] = fam.get("gender")

    patient_snapshot = {
        "patient_id": str(rx["patient_id"]),
        "family_member_id": str(rx["family_member_id"]) if rx.get("family_member_id") else None,
        "name": patient_row.get("name", ""),
        "mrn": patient_row.get("mrn", ""),
        "age": patient_row.get("age"),
        "gender": patient_row.get("gender"),
        "phone": patient_row.get("phone", ""),
        "allergies": allergies,
        "allergies_status": status_code,
    }

    try:
        rpc_res = await sb(
            supabase.rpc(
                "opd_sign_prescription",
                {
                    "p_clinic_id": clinic_id,
                    "p_rx_id": rx_id,
                    "p_signer_admin_id": user.get("user_id"),
                    "p_signer_doctor_id": signer_doc_id,
                    "p_signer_snapshot": signer_snapshot,
                    "p_letterhead_snapshot": letterhead_snapshot,
                    "p_patient_snapshot": patient_snapshot,
                    "p_allergy_review": allergy_review,
                },
            )
        )
    except Exception as e:
        err_msg = str(e).lower()
        if "opd_record_locked" in err_msg or "opd_not_draft" in err_msg:
            raise HTTPException(status_code=409, detail="Prescription is locked or not a draft")
        if "opd_encounter_not_signed" in err_msg:
            raise HTTPException(status_code=422, detail="Encounter must be signed before signing prescription")
        if "opd_prescription_empty" in err_msg:
            raise HTTPException(status_code=422, detail="Prescription has no items")
        raise HTTPException(status_code=500, detail=f"Failed to sign prescription: {e}")

    signed_rx = dict(rpc_res.data[0] if isinstance(rpc_res.data, list) and rpc_res.data else (rpc_res.data or {}))
    signed_rx["items"] = items
    signed_rx["warnings"] = []
    return signed_rx


async def amend_prescription(
    clinic: dict,
    rx_id: str,
    reason: str,
    user: dict,
) -> dict:
    """Create a new prescription draft (v = current.v + 1) superseding the signed prescription."""
    clinic_id = str(clinic["id"])
    rx_res = await sb(
        supabase.table("opd_prescriptions")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", rx_id)
        .single()
    )
    if not rx_res.data:
        raise HTTPException(status_code=404, detail="Prescription not found")
    rx = rx_res.data

    if rx.get("status") != "signed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only signed prescriptions can be amended",
        )

    user_doc_id = user.get("doctor_id")
    if not user_doc_id or str(user_doc_id) != str(rx.get("doctor_id")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the treating doctor can amend this prescription",
        )

    # Check if a draft already exists for this appointment
    draft_check = await sb(
        supabase.table("opd_prescriptions")
        .select("id")
        .eq("clinic_id", clinic_id)
        .eq("appointment_id", rx["appointment_id"])
        .eq("status", "draft")
    )
    if draft_check.data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An amendment draft already exists for this appointment",
        )

    new_version = rx.get("version", 1) + 1
    new_rx = {
        "clinic_id": clinic_id,
        "encounter_id": rx["encounter_id"],
        "appointment_id": rx["appointment_id"],
        "patient_id": rx["patient_id"],
        "family_member_id": rx.get("family_member_id"),
        "doctor_id": rx["doctor_id"],
        "version": new_version,
        "supersedes_id": rx_id,
        "status": "draft",
        "general_instructions": rx.get("general_instructions"),
        "created_by": user.get("user_id"),
    }

    # unscoped: insert_scoped_by_payload
    ins_res = await sb(supabase.table("opd_prescriptions").insert(new_rx))
    if not ins_res.data:
        raise HTTPException(status_code=500, detail="Failed to create amendment draft")
    draft_rx = ins_res.data[0]
    new_rx_id = draft_rx["id"]

    # Copy items
    old_items_res = await sb(
        supabase.table("opd_prescription_items")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("prescription_id", rx_id)
        .order("line_no")
    )
    old_items = old_items_res.data or []
    item_rows = []
    for itm in old_items:
        item_rows.append({
            "clinic_id": clinic_id,
            "prescription_id": new_rx_id,
            "line_no": itm["line_no"],
            "drug_name": itm["drug_name"],
            "formulation": itm["formulation"],
            "strength": itm.get("strength"),
            "dosage": itm["dosage"],
            "route": itm.get("route", "oral"),
            "frequency": itm["frequency"],
            "timing": itm.get("timing", "any"),
            "duration_days": itm.get("duration_days"),
            "instructions": itm.get("instructions"),
        })

    if item_rows:
        # unscoped: insert_scoped_by_payload
        items_ins = await sb(supabase.table("opd_prescription_items").insert(item_rows))
        draft_rx["items"] = items_ins.data or []
    else:
        draft_rx["items"] = []

    allergies, status_code = await get_patient_allergies(
        clinic_id, draft_rx["patient_id"], draft_rx.get("family_member_id")
    )
    draft_rx["warnings"] = allergy_warnings(allergies, status_code, draft_rx["items"])
    return draft_rx


async def send_prescription(
    clinic: dict,
    rx_id: str,
    actor: dict,
) -> dict:
    """Send signed prescription via WhatsApp (in-session or template)."""
    clinic_id = str(clinic["id"])
    rx_res = await sb(
        supabase.table("opd_prescriptions")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("id", rx_id)
        .single()
    )
    if not rx_res.data:
        raise HTTPException(status_code=404, detail="Prescription not found")
    rx = rx_res.data

    if rx.get("status") not in ("signed", "superseded"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only signed prescriptions can be dispatched",
        )

    # Daily send limit
    if rx.get("send_count", 0) >= 5:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily send limit (5 sends per Rx) reached",
        )

    # Consent check: account holder must have opted in
    pat_res = await sb(
        supabase.table("patients")
        .select("phone, opted_in")
        .eq("clinic_id", clinic_id)
        .eq("id", rx["patient_id"])
        .single()
    )
    if not pat_res.data or not pat_res.data.get("opted_in"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "no_whatsapp_consent", "message": "Patient has not opted in to WhatsApp communication"},
        )
    phone = pat_res.data.get("phone")

    # Render PDF
    items_res = await sb(
        supabase.table("opd_prescription_items")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("prescription_id", rx_id)
        .order("line_no")
    )
    items = items_res.data or []

    from app.services.opd_pdf import render_prescription_pdf
    pdf_bytes = render_prescription_pdf(rx, items)

    from app.services.whatsapp import whatsapp_service
    filename = f"Rx_{rx['id'][:8]}.pdf"
    media_id = await whatsapp_service.upload_media(
        clinic,
        pdf_bytes,
        filename=filename,
        content_type="application/pdf",
    )

    capture: dict = {}
    channel: str = "session"
    sent_ok: bool = False

    can_freeform = await whatsapp_service._can_send_freeform(clinic, phone)
    if can_freeform:
        channel = "session"
        sent_ok = await whatsapp_service.send_document(
            clinic,
            phone,
            media_id=media_id,
            filename=filename,
            caption=f"Your prescription from {clinic.get('name', 'Kriya Clinic')}",
            _source="opd",
            _capture=capture,
            _fallback_file_bytes=pdf_bytes,
        )
    else:
        # Check template configuration
        templates = clinic.get("opd_settings", {}).get("templates", {}) if isinstance(clinic.get("opd_settings"), dict) else {}
        template_name = templates.get("prescription_ready")
        if not template_name:
            return {"sent": False, "channel": "template", "reason": "outside_24h_no_template"}

        channel = "template"
        try:
            sent_ok = await whatsapp_service.send_template(
                clinic,
                phone,
                template_name=template_name,
                components=[
                    {
                        "type": "header",
                        "parameters": [
                            {
                                "type": "document",
                                "document": {"id": media_id, "filename": filename},
                            }
                        ],
                    }
                ],
                _source="opd",
                _capture=capture,
            )
        except Exception as e:
            logger.error(f"WhatsApp template dispatch failed: {e}")
            sent_ok = False

    now_iso = datetime.now(timezone.utc).isoformat()
    await sb(
        supabase.table("opd_prescriptions")
        .update({
            "delivery_status": "sent" if sent_ok else "failed",
            "whatsapp_message_id": capture.get("meta_message_id"),
            "last_sent_at": now_iso,
            "send_count": rx.get("send_count", 0) + (1 if sent_ok else 0),
        })
        .eq("clinic_id", clinic_id)
        .eq("id", rx_id)
    )

    return {"sent": sent_ok, "channel": channel}
