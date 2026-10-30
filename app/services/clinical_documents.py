"""Clinical Documents Engine (G9, migration 119).

Signed, printable documents on the clinic's letterhead:

  * IPD discharge summary PDF. Signing (docs_sign_discharge_summary) freezes
    the letterhead and the clinical record the PDF shows, so a reprint is
    identical forever; the signed PDF then goes to the patient's WhatsApp.
  * informed consents, recorded as signed (paper / tablet) with signer,
    relationship and witness;
  * medical / fitness certificates with a gapless CERT-YYYY-NNNNN number.

The database keeps every signed / recorded / issued document immutable
(migration 119 guards). Delivery follows the OPD prescription path: the
WhatsApp opt-in is checked, the PDF goes as a document in the 24-hour window
or through the clinic's approved template outside it.

Zero-LLM: imports nothing from the AI, voice or assistant packages
(tests/test_ipd_no_llm.py); documents are rendered from stored rows only.
"""

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services import ipd
from app.services.lis import letterhead_snapshot, subject_of
from app.services.opd_pdf import render_certificate_pdf, render_discharge_summary_pdf
from app.services.tenant import get_clinic_by_id
from app.utils.async_tasks import spawn_background_task
from app.utils.helpers import actor_uuid
from app.utils.validators import mask_phone

logger = logging.getLogger(__name__)

STORAGE_BUCKET = "lab-reports"   # the existing private report bucket; documents live under <clinic>/documents/
MAX_SENDS = 5
MAX_LAB_RESULTS = 40
MAX_PROCEDURE_NOTES = 20

_ERRORS: dict[str, tuple[int, str]] = {
    "docs_record_locked": (409, "A signed or issued document cannot be changed."),
    "docs_not_signed": (409, "Sign the discharge summary first."),
}


def raise_for(exc: Exception) -> None:
    """docs_* codes here; ipd_* (and anything else) through the IPD mapper,
    which never returns raw database text."""
    if isinstance(exc, HTTPException):
        raise exc
    msg = str(getattr(exc, "message", None) or exc)
    for code, (status, message) in _ERRORS.items():
        if code in msg:
            raise HTTPException(status_code=status, detail={"code": code, "message": message})
    ipd.raise_for(exc)


async def _rpc(name: str, params: dict) -> Any:
    try:
        res = await sb(supabase.rpc(name, params))
    except Exception as e:  # postgrest APIError carries the docs_* / ipd_* code
        raise_for(e)
    return res.data


async def _one(table: str, clinic_id: str, row_id: Optional[str], cols: str = "*") -> dict:
    if not row_id:
        return {}
    res = await sb(scoped_query(table, clinic_id, cols).eq("id", row_id).limit(1))
    return res.data[0] if res.data else {}


def _actor(user: Any) -> tuple[Optional[str], str]:
    return actor_uuid(user), str(getattr(user, "username", None) or "system")[:100]


async def doctor_snapshot(clinic_id: str, doctor_id: Optional[str], required: bool = True) -> Optional[dict]:
    """The signer as printed: name, qualifications, registration. A login must be
    linked to a doctor with a registration number to sign."""
    doctor = await _one("doctors", clinic_id, doctor_id,
                        "id, name, qualifications, registration_number, registration_council")
    if not doctor:
        if not required:
            return None
        raise HTTPException(status_code=403, detail="Only a login linked to a doctor can sign this document.")
    if required and not (doctor.get("registration_number") or "").strip():
        raise HTTPException(status_code=409, detail="Add the doctor's registration number to the doctor profile "
                                                    "before signing.")
    return {"doctor_id": str(doctor["id"]), "name": doctor.get("name") or "",
            "qualifications": doctor.get("qualifications") or "",
            "registration_number": (doctor.get("registration_number") or "").strip(),
            "registration_council": doctor.get("registration_council") or ""}


async def _check_person(clinic_id: str, patient_id: Optional[str], family_member_id: Optional[str]) -> None:
    """The patient is this clinic's, and a family member is on that patient's
    account (family_members hang off the account holder's phone)."""
    patient = await _one("patients", clinic_id, patient_id, "id, phone")
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found.")
    if family_member_id:
        member = await _one("family_members", clinic_id, family_member_id, "id, primary_phone")
        if not member or member.get("primary_phone") != patient.get("phone"):
            raise HTTPException(status_code=404, detail="Family member not found for this patient.")


def _patient_snapshot(subject: dict, **extra) -> dict:
    return {"name": subject["name"], "mrn": subject["mrn"], "gender": subject["gender"],
            "age_years": subject["age_years"], **extra}


# ── discharge summary ───────────────────────────────────────────────────────

def _vital(row: Optional[dict]) -> Optional[dict]:
    keys = ("observed_at", "bp_systolic", "bp_diastolic", "pulse_bpm", "resp_rate", "temperature_c", "spo2_pct",
            "blood_glucose_mg_dl", "consciousness")
    return {k: row.get(k) for k in keys} if row else None


async def build_clinical_snapshot(clinic_id: str, admission: dict) -> dict:
    """The record the discharge summary PDF shows, read once at signing:
    patient, stay, first and last vitals, signed lab results of the stay's
    orders, other completed investigations, and final procedure notes."""
    adm_id = admission["id"]
    subject = await subject_of(clinic_id, admission)
    doctor = await _one("doctors", clinic_id, admission.get("attending_doctor_id"), "id, name")
    bed = await _one("ipd_beds", clinic_id, admission.get("current_bed_id"), "id, code")
    if not bed:   # discharged: the last bed held
        last = await sb(scoped_query("ipd_bed_assignments", clinic_id, "bed_id").eq("admission_id", adm_id)
                        .order("started_at", desc=True).limit(1))
        bed = await _one("ipd_beds", clinic_id, (last.data or [{}])[0].get("bed_id"), "id, code")

    vitals = await fetch_all(lambda: scoped_query("ipd_observations", clinic_id).eq("admission_id", adm_id)
                             .eq("kind", "vitals").eq("status", "recorded").order("observed_at").order("id"),
                             label="docs:vitals")
    orders = await fetch_all(lambda: scoped_query("opd_orders", clinic_id,
                                                  "id, item_name, catalog_type, status, result_summary, resulted_at")
                             .eq("admission_id", adm_id).order("created_at").order("id"), label="docs:orders")
    results = []
    for i in range(0, len(orders), 100):
        ids = [o["id"] for o in orders[i:i + 100]]
        results += await fetch_all(lambda c=ids: scoped_query("lab_results", clinic_id).in_("order_id", c)
                                   .eq("status", "validated").order("validated_at").order("id"), label="docs:labs")
    results = results[-MAX_LAB_RESULTS:]
    labs = []
    for r in results:
        values = await fetch_all(lambda rid=r["id"]: scoped_query("lab_result_values", clinic_id)
                                 .eq("result_id", rid).order("sort_order").order("id"), label="docs:values")
        labs.append({"test_name": r["test_name"], "version": r["version"], "validated_at": r["validated_at"],
                     "has_critical_value": r["has_critical_value"],
                     "values": [{"parameter_name": v["parameter_name"], "unit": v.get("unit"), "flag": v["flag"],
                                 "reference_range_display": v.get("reference_range_display"),
                                 "value": (format(float(v["numeric_value"]), "g") if v.get("numeric_value") is not None
                                           else v.get("text_value"))} for v in values]})
    with_lis = {r["order_id"] for r in results}
    other = [{"item_name": o["item_name"], "status": o["status"], "result_summary": o.get("result_summary")}
             for o in orders if o["status"] == "completed" and o["id"] not in with_lis]
    notes = await fetch_all(lambda: scoped_query("ipd_notes", clinic_id, "body, author_name, authored_at")
                            .eq("admission_id", adm_id).eq("note_type", "procedure").eq("status", "final")
                            .order("authored_at").order("id"), label="docs:notes")
    return {
        "patient": _patient_snapshot(subject),
        "admission": {"admission_number": admission.get("admission_number"), "admitted_at": admission.get("admitted_at"),
                      "discharged_at": admission.get("discharged_at") or admission.get("discharge_initiated_at"),
                      "discharge_type": admission.get("discharge_type"), "reason": admission.get("reason"),
                      "attending_doctor": doctor.get("name"), "bed": bed.get("code"),
                      "payer_type": admission.get("payer_type")},
        "vitals": {"admission": _vital(vitals[0] if vitals else None),
                   "discharge": _vital(vitals[-1] if len(vitals) > 1 else None)},
        "lab_results": labs,
        "other_investigations": other,
        "procedure_notes": [{**n, "body": (n.get("body") or "")[:2000]} for n in notes[-MAX_PROCEDURE_NOTES:]],
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


async def _snapshots(clinic_id: str, admission: dict) -> tuple[dict, dict]:
    return (await letterhead_snapshot(clinic_id, admission.get("branch_id")),
            await build_clinical_snapshot(clinic_id, admission))


async def sign_discharge_summary(clinic_id: str, summary_id: str, user: Any) -> dict:
    """Sign as the attending doctor (111 rules) and freeze the PDF's snapshots in
    the same transaction; then send the PDF to the patient in the background."""
    summary = await _one("ipd_discharge_summaries", clinic_id, summary_id, "id, admission_id")
    if not summary:
        raise HTTPException(status_code=404, detail="Discharge summary not found.")
    admission = await ipd.get_admission_row(clinic_id, summary["admission_id"])
    letterhead, clinical = await _snapshots(clinic_id, admission)
    aid, name = _actor(user)
    signed = await _rpc("docs_sign_discharge_summary", {
        "p_clinic_id": clinic_id, "p_summary_id": summary_id, "p_doctor_id": getattr(user, "doctor_id", None),
        "p_actor_id": aid, "p_actor_name": name, "p_letterhead_snapshot": letterhead, "p_clinical_snapshot": clinical,
    })
    spawn_background_task(_deliver_quietly(clinic_id, summary_id), name=f"docs_ds_{summary_id}")
    return signed


async def printable_summary(clinic_id: str, admission_id: str, version: Optional[int] = None) -> dict:
    """The current signed summary (or a given signed / superseded version), with
    its snapshots; a summary signed before 119 has them frozen on first print."""
    q = scoped_query("ipd_discharge_summaries", clinic_id).eq("admission_id", admission_id)
    q = q.eq("version", version).in_("status", ["signed", "superseded"]) if version else q.eq("status", "signed")
    res = await sb(q.limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="No signed discharge summary for this admission yet.")
    row = res.data[0]
    if row.get("letterhead_snapshot") is None or row.get("clinical_snapshot") is None:
        letterhead, clinical = await _snapshots(clinic_id, await ipd.get_admission_row(clinic_id, admission_id))
        row = await _rpc("docs_freeze_discharge_summary", {
            "p_clinic_id": clinic_id, "p_summary_id": row["id"], "p_letterhead_snapshot": letterhead,
            "p_clinical_snapshot": clinical})
    return row


async def render_summary(clinic_id: str, admission_id: str, version: Optional[int] = None) -> tuple[bytes, dict]:
    row = await printable_summary(clinic_id, admission_id, version)
    return render_discharge_summary_pdf(row), row


def summary_filename(row: dict) -> str:
    number = ((row.get("clinical_snapshot") or {}).get("admission") or {}).get("admission_number") or row["id"][:8]
    return f"Discharge_Summary_{number}_v{row['version']}.pdf"


async def send_discharge_summary(clinic_id: str, admission_id: str) -> dict:
    """Store (once) and send the current signed summary to the patient's WhatsApp."""
    pdf, row = await render_summary(clinic_id, admission_id)
    clinic = await get_clinic_by_id(clinic_id) or {"id": clinic_id}
    return await _deliver(clinic, "ipd_discharge_summaries", row, pdf, summary_filename(row),
                          f"Your discharge summary from {clinic.get('name') or 'the hospital'}",
                          "discharge_summary_ready", "discharge-summaries")


async def _deliver_quietly(clinic_id: str, summary_id: str) -> None:
    """Background send after signing. Never raises: the signature is committed."""
    try:
        row = await _one("ipd_discharge_summaries", clinic_id, summary_id, "id, admission_id")
        out = await send_discharge_summary(clinic_id, row["admission_id"])
        logger.info(f"DOCS_DS_DELIVERY summary={summary_id} sent={out.get('sent')} reason={out.get('reason')}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"DOCS_DS_DELIVERY_FAILED summary={summary_id} type={type(e).__name__}")


# ── delivery (OPD prescription pattern) ─────────────────────────────────────

async def _store(clinic_id: str, folder: str, row: dict, pdf: bytes, filename: str) -> Optional[str]:
    if row.get("pdf_storage_path"):
        return row["pdf_storage_path"]
    path = f"{clinic_id}/documents/{folder}/{row['id']}/{filename}"
    try:
        await asyncio.to_thread(supabase.storage.from_(STORAGE_BUCKET).upload, path, pdf,
                                {"content-type": "application/pdf", "upsert": "true"})
    except Exception as e:  # noqa: BLE001 - the PDF re-renders from the row; storage is a copy
        logger.warning(f"DOCS_STORAGE_FAILED doc={row['id']} type={type(e).__name__}")
        return None
    return path


async def _deliver(clinic: dict, table: str, row: dict, pdf: bytes, filename: str, caption: str,
                   template_key: str, folder: str) -> dict:
    """Send a signed document to the account holder's WhatsApp and record the
    outcome on the row. The order / admission fixes the patient, so the
    recipient is exact; the patient must have opted in."""
    clinic_id = str(clinic["id"])
    if (row.get("send_count") or 0) >= MAX_SENDS:
        raise HTTPException(status_code=429, detail=f"This document was already sent {MAX_SENDS} times.")
    patient_id = row.get("patient_id") or ((await _one("ipd_admissions", clinic_id, row.get("admission_id"),
                                                       "id, patient_id")).get("patient_id"))
    patient = await _one("patients", clinic_id, patient_id, "id, phone, opted_in")
    path = await _store(clinic_id, folder, row, pdf, filename)
    if path and not row.get("pdf_storage_path"):
        await sb(supabase.table(table).update({"pdf_storage_path": path}).eq("clinic_id", clinic_id)
                 .eq("id", row["id"]).is_("pdf_storage_path", "null"))
    phone = (patient.get("phone") or "").strip()
    if not phone or not patient.get("opted_in"):
        await _record_delivery(table, clinic_id, row, False, None)
        return {"sent": False, "channel": None, "reason": "no_whatsapp_consent" if phone else "no_phone"}

    from app.services.whatsapp import whatsapp_service   # Meta client, loaded only when sending

    capture: dict = {}
    sent, channel, reason = False, "session", None
    try:
        media_id = await whatsapp_service.upload_media(clinic, pdf, filename=filename, content_type="application/pdf")
        if await whatsapp_service._can_send_freeform(clinic, phone):
            sent = await whatsapp_service.send_document(clinic, phone, media_id=media_id, filename=filename,
                                                        caption=caption, _source="documents", _capture=capture,
                                                        _fallback_file_bytes=pdf)
        else:
            channel = "template"
            settings = clinic.get("opd_settings") if isinstance(clinic.get("opd_settings"), dict) else {}
            template = (settings.get("templates") or {}).get(template_key)
            if not template:
                reason = "outside_24h_no_template"
            else:
                sent = await whatsapp_service.send_template(
                    clinic, phone, template_name=template, _source="documents", _capture=capture,
                    components=[{"type": "header", "parameters": [
                        {"type": "document", "document": {"id": media_id, "filename": filename}}]}])
    except Exception as e:  # noqa: BLE001 - recorded as failed; staff can resend
        logger.error(f"DOCS_SEND_FAILED doc={row['id']} to={mask_phone(phone)} type={type(e).__name__}")
        sent = False
    await _record_delivery(table, clinic_id, row, sent, capture.get("meta_message_id"), attempted=reason is None)
    return {"sent": bool(sent), "channel": channel, "reason": reason or (None if sent else "send_failed")}


async def _record_delivery(table: str, clinic_id: str, row: dict, sent: bool, message_id: Optional[str],
                           attempted: bool = False) -> None:
    update = {"delivery_status": "sent" if sent else ("failed" if attempted else "not_sent"),
              "last_sent_at": datetime.now(timezone.utc).isoformat() if sent else row.get("last_sent_at"),
              "send_count": (row.get("send_count") or 0) + (1 if sent else 0)}
    if message_id:
        update["whatsapp_message_id"] = message_id
    await sb(supabase.table(table).update(update).eq("clinic_id", clinic_id).eq("id", row["id"]))


# ── consents ────────────────────────────────────────────────────────────────

async def create_consent(clinic_id: str, data: dict, actor: Any) -> dict:
    """Record a consent the patient or their representative has signed. For an
    inpatient the admission fixes patient and branch; otherwise patient_id must
    be this clinic's patient. A retried request with the same key returns the
    first record (`replayed`)."""
    key = data.get("idempotency_key")
    if key:
        existing = await sb(scoped_query("clinical_consents", clinic_id).eq("idempotency_key", key).limit(1))
        if existing.data:
            return {**existing.data[0], "replayed": True}
    extra: dict = {}
    if data.get("admission_id"):
        adm = await ipd.get_admission_row(clinic_id, data["admission_id"])
        if adm["status"] == "cancelled":
            raise HTTPException(status_code=409, detail="This admission was cancelled.")
        person = {"patient_id": adm["patient_id"], "family_member_id": adm.get("family_member_id"),
                  "branch_id": adm.get("branch_id")}
        extra["admission_number"] = adm.get("admission_number")
    else:
        await _check_person(clinic_id, data.get("patient_id"), data.get("family_member_id"))
        person = {"patient_id": data["patient_id"], "family_member_id": data.get("family_member_id"),
                  "branch_id": data.get("branch_id")}
    subject = await subject_of(clinic_id, person)
    doctor = await doctor_snapshot(clinic_id, data.get("doctor_id"), required=False) if data.get("doctor_id") else None
    if data.get("doctor_id") and not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    aid, name = _actor(actor)
    row = {
        **person, "clinic_id": clinic_id, "admission_id": data.get("admission_id"),
        **{k: data.get(k) for k in ("consent_type", "language", "title", "procedure_name", "body_text",
                                    "risks_explained", "witness_name", "witness_phone", "signer_relationship",
                                    "signer_name", "idempotency_key")},
        "doctor_id": data.get("doctor_id"), "doctor_snapshot": doctor,
        "is_signed": True, "signed_at": datetime.now(timezone.utc).isoformat(), "signed_by_admin_id": aid,
        "signed_by_name": name, "letterhead_snapshot": await letterhead_snapshot(clinic_id, person.get("branch_id")),
        "patient_snapshot": _patient_snapshot(subject, **extra),
    }
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("clinical_consents").insert(row))
    except Exception as e:
        if key and "uq_clinical_consents_idempotency" in str(e):
            again = await sb(scoped_query("clinical_consents", clinic_id).eq("idempotency_key", key).limit(1))
            if again.data:
                return {**again.data[0], "replayed": True}
        raise_for(e)
    return {**res.data[0], "replayed": False}


async def list_consents(clinic_id: str, admission_id: Optional[str] = None, patient_id: Optional[str] = None) -> list:
    def build():
        q = scoped_query("clinical_consents", clinic_id,
                         "id, admission_id, patient_id, consent_type, language, title, procedure_name, signer_name, "
                         "signer_relationship, witness_name, signed_at, signed_by_name, branch_id, created_at")
        q = q.eq("admission_id", admission_id) if admission_id else q.eq("patient_id", patient_id)
        return q.order("created_at", desc=True).order("id")
    return await fetch_all(build, label="docs:consents")


async def get_consent(clinic_id: str, consent_id: str) -> dict:
    row = await _one("clinical_consents", clinic_id, consent_id)
    if not row:
        raise HTTPException(status_code=404, detail="Consent not found.")
    return row


# ── certificates ────────────────────────────────────────────────────────────

async def issue_medical_certificate(clinic_id: str, patient_id: str, data: dict, actor: Any) -> dict:
    """Issue a certificate signed by the acting doctor, numbered CERT-YYYY-NNNNN
    without gaps (counter row lock). Linked encounter / admission must belong to
    the same patient."""
    signer = await doctor_snapshot(clinic_id, getattr(actor, "doctor_id", None))
    branch_id = data.get("branch_id")
    family_member_id = data.get("family_member_id")
    if data.get("encounter_id"):
        enc = await _one("opd_encounters", clinic_id, data["encounter_id"], "id, patient_id, family_member_id, branch_id")
        if not enc or str(enc["patient_id"]) != str(patient_id):
            raise HTTPException(status_code=404, detail="Consultation not found for this patient.")
        branch_id, family_member_id = enc.get("branch_id") or branch_id, enc.get("family_member_id") or family_member_id
    if data.get("admission_id"):
        adm = await _one("ipd_admissions", clinic_id, data["admission_id"], "id, patient_id, family_member_id, branch_id")
        if not adm or str(adm["patient_id"]) != str(patient_id):
            raise HTTPException(status_code=404, detail="Admission not found for this patient.")
        branch_id, family_member_id = adm.get("branch_id") or branch_id, adm.get("family_member_id") or family_member_id
    await _check_person(clinic_id, patient_id, family_member_id)
    start, end = data.get("start_date"), data.get("end_date")
    rest = data.get("rest_days_advised")
    if data["certificate_type"] == "medical_leave" and rest is None and start and end:
        rest = (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days + 1
    subject = await subject_of(clinic_id, {"patient_id": patient_id, "family_member_id": family_member_id})
    aid, name = _actor(actor)
    row = {
        "branch_id": branch_id, "patient_id": patient_id, "family_member_id": family_member_id,
        "encounter_id": data.get("encounter_id"), "admission_id": data.get("admission_id"),
        "doctor_id": signer["doctor_id"], "certificate_type": data["certificate_type"],
        "diagnosis_text": data["diagnosis_text"].strip(), "rest_days_advised": rest,
        "start_date": str(start) if start else None, "end_date": str(end) if end else None,
        "remarks": (data.get("remarks") or "").strip() or None, "doctor_snapshot": signer,
        "letterhead_snapshot": await letterhead_snapshot(clinic_id, branch_id),
        "patient_snapshot": _patient_snapshot(subject), "issued_by": aid, "issued_by_name": name,
    }
    return await _rpc("docs_issue_certificate", {"p_clinic_id": clinic_id, "p_row": row,
                                                 "p_idempotency_key": data.get("idempotency_key")})


async def get_certificate(clinic_id: str, cert_id: str) -> dict:
    row = await _one("clinical_certificates", clinic_id, cert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Certificate not found.")
    return row


async def list_certificates(clinic_id: str, patient_id: Optional[str] = None,
                            encounter_id: Optional[str] = None) -> list:
    def build():
        q = scoped_query("clinical_certificates", clinic_id,
                         "id, certificate_number, certificate_type, patient_id, encounter_id, admission_id, status, "
                         "diagnosis_text, start_date, end_date, rest_days_advised, doctor_snapshot, created_at, "
                         "void_reason, delivery_status, branch_id")
        q = q.eq("encounter_id", encounter_id) if encounter_id else q.eq("patient_id", patient_id)
        return q.order("created_at", desc=True).order("id")
    return await fetch_all(build, label="docs:certificates")


async def void_certificate(clinic_id: str, cert_id: str, reason: str, actor: Any) -> dict:
    try:
        res = await sb(supabase.table("clinical_certificates").update({
            "status": "void", "void_reason": reason.strip(), "voided_at": datetime.now(timezone.utc).isoformat(),
            "voided_by": actor_uuid(actor)}).eq("clinic_id", clinic_id).eq("id", cert_id).eq("status", "issued"))
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=409, detail="This certificate is already void.")
    return res.data[0]


def certificate_filename(cert: dict) -> str:
    return f"{cert.get('certificate_number') or 'Certificate'}.pdf"


async def send_certificate(clinic_id: str, cert_id: str) -> dict:
    cert = await get_certificate(clinic_id, cert_id)
    if cert["status"] != "issued":
        raise HTTPException(status_code=409, detail="A void certificate cannot be sent.")
    clinic = await get_clinic_by_id(clinic_id) or {"id": clinic_id}
    return await _deliver(clinic, "clinical_certificates", cert, render_certificate_pdf(cert),
                          certificate_filename(cert),
                          f"Your medical certificate from {clinic.get('name') or 'the clinic'}",
                          "certificate_ready", "certificates")
