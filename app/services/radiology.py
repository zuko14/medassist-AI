"""Radiology Information System and PCPNDT Form F (G13, migration 123).

A G1 imaging order (opd_orders: an X-ray / USG / CT / MRI service or custom
line) gets a radiology report: a draft, then a radiologist's signature
(radiology_finalize_report), which completes the order in the same
transaction and freezes the report; a correction is a versioned amendment.
An obstetric ultrasound cannot be finalized without the statutory PCPNDT
Form F (radiology_record_form_f), recorded and locked in one step.

PCPNDT Act, 1994: nothing that determines or discloses the sex of the foetus
is ever saved. app.services.clinical_firewall.fetal_sex_disclosure refuses it
here (400) and the database refuses it again (radiology_fetal_sex_text).

Zero-LLM clinical path: modality and obstetric detection are the keyword rules
below; this module imports nothing from the AI, voice or assistant packages
(tests/test_ipd_no_llm.py). Delivery hands the signed PDF to the lab-report
WhatsApp pipeline after the report is final; an obstetric report goes without
the AI summary, so its text never reaches a model.
"""

import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.database import fetch_all, restrict_to_branch, sb, scoped_query, supabase
from app.services import lis, orders
from app.services.clinical_firewall import fetal_sex_disclosure
from app.services.masters import effective_catalog
from app.services.opd_pdf import render_pcpndt_form_f_pdf, render_radiology_report_pdf
from app.services.permissions import holds_radiology_permission
from app.utils.async_tasks import spawn_background_task
from app.utils.helpers import IST, actor_uuid, today_ist
from app.utils.validators import mask_phone

logger = logging.getLogger(__name__)

MODALITIES = ("x_ray", "ultrasound", "ct", "mri", "mammography", "dexa", "fluoroscopy", "other")
OPEN_STATUSES = ("ordered", "billed", "sample_collected", "in_progress", "resulted")
_URGENCY_RANK = {"stat": 0, "urgent": 1, "routine": 2}

# error code (prefix before ':') -> (HTTP status, message shown to staff)
_ERRORS: dict[str, tuple[int, str]] = {
    "radiology_order_not_found": (404, "Order not found."),
    "radiology_order_not_imaging": (409, "Lab tests are reported in the lab, not in radiology."),
    "radiology_order_cancelled": (409, "This order was cancelled."),
    "radiology_order_closed": (409, "This order was already closed with a text result. Nothing to report."),
    "radiology_report_not_found": (404, "Report not found."),
    "radiology_report_changed": (409, "The report was changed after you opened it. Reload and review again."),
    "radiology_report_incomplete": (422, "Findings and impression are both required before signing."),
    "radiology_not_draft": (409, "This report is already signed. Reload it."),
    "radiology_amendment_reason_required": (422, "This report is already signed. Give an amendment reason "
                                                 "(5+ characters) to issue a corrected version."),
    "radiology_signer_registration_missing": (409, "Signing needs the doctor's registration number. Add it to the "
                                                   "doctor profile first."),
    "radiology_form_f_required": (422, "Mandatory PCPNDT Form F is not recorded for this obstetric ultrasound. "
                                       "Record and lock Form F before finalizing."),
    "radiology_form_f_not_obstetric": (409, "Form F is recorded only for an obstetric ultrasound."),
    "radiology_form_f_bad_lmp": (422, "LMP must be a date within the last 45 weeks, not in the future."),
    "radiology_form_f_declarations": (422, "Both declarations (pregnant woman and doctor) are required."),
    "radiology_form_f_fixes_obstetric": (409, "Form F is recorded for this study: it stays an obstetric ultrasound."),
    "radiology_form_f_locked": (409, "Form F is a locked statutory record and cannot be changed."),
    "radiology_obstetric_needs_ultrasound": (422, "An obstetric study must be an ultrasound."),
    "radiology_pcpndt_violation": (400, "Text that determines or discloses the sex of the foetus is prohibited "
                                        "(PCPNDT Act, 1994). Remove it."),
    "radiology_report_locked": (409, "A signed report cannot be changed. Issue an amendment instead."),
    "radiology_append_only": (409, "Radiology reports cannot be deleted."),
    "radiology_bad_transition": (409, "The report is not in a state that allows this step. Reload it."),
    "radiology_via_function_only": (409, "The report cannot be changed this way."),
}


def raise_for(exc: Exception) -> None:
    """Map a radiology_* database error to an HTTPException; opd_* codes and
    anything else go to the orders mapper (never the raw database text)."""
    if isinstance(exc, HTTPException):
        raise exc
    msg = str(getattr(exc, "message", None) or exc)
    for code in sorted(_ERRORS, key=len, reverse=True):
        if code in msg:
            status, message = _ERRORS[code]
            raise HTTPException(status_code=status, detail={"code": code, "message": message})
    orders.raise_for(exc)


def _refuse(code: str) -> None:
    status, message = _ERRORS[code]
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


async def _rpc(name: str, params: dict) -> Any:
    try:
        res = await sb(supabase.rpc(name, params))
    except Exception as e:  # postgrest APIError carries the radiology_* code
        raise_for(e)
    return res.data


def _require(actor: Any, perm: str) -> None:
    if not holds_radiology_permission(actor, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")


# ── deterministic text rules ────────────────────────────────────────────────

# Mirrors the SQL radiology_is_obstetric_text (migration 123); parity is tested.
_OBSTETRIC = re.compile(
    r"\b(?:obstetric\w*|antenatal|ante-natal|pregnan\w*|gravid\w*|fetal|foetal|fetus\w*|foetus\w*|anomaly scan"
    r"|nt scan|nt-nb scan|nuchal|growth scan|biophysical|gestation\w*|dating scan|viability scan|tiffa|level ii scan"
    r"|trimester|placenta\w*|umbilical artery|amniotic|liquor|ob usg|ob scan|obs usg|intrauterine pregnancy|iup)\b",
    re.IGNORECASE,
)
# first match wins: the specific modality before the generic ones
_MODALITY_WORDS: tuple[tuple[str, re.Pattern], ...] = (
    ("mammography", re.compile(r"\bmammo\w*", re.I)),
    ("dexa", re.compile(r"\b(?:dexa|dxa|bone densitometry|bmd scan)\b", re.I)),
    ("mri", re.compile(r"\b(?:mri|mra|mrcp|magnetic resonance)\b", re.I)),
    ("ct", re.compile(r"\b(?:ct|hrct|cect|ncct|cta|ct angio\w*|computed tomography)\b", re.I)),
    ("fluoroscopy", re.compile(r"\b(?:fluoro\w*|barium|hsg|ivp|ivu|mcu|rgu)\b", re.I)),
    ("ultrasound", re.compile(r"\b(?:usg|ultrasound|ultrasonograph\w*|sonograph\w*|sonomammo\w*|doppler|tvs|"
                              r"nt scan|anomaly scan|growth scan|tiffa|obstetric\w* scan)\b", re.I)),
    ("x_ray", re.compile(r"\b(?:x-?ray\w*|x ray\w*|xray\w*|radiograph\w*|cxr|skiagram\w*|opg)\b", re.I)),
)


def is_obstetric_text(text: Optional[str]) -> bool:
    return bool(text and _OBSTETRIC.search(text))


def detect_modality(*texts: Optional[str]) -> Optional[str]:
    joined = " ".join(t for t in texts if t)
    for modality, pattern in _MODALITY_WORDS:
        if pattern.search(joined):
            return modality
    return None


def classify_order(order: dict, catalog: dict[str, dict]) -> Optional[str]:
    """The imaging modality of an order, or None when it is not imaging. A
    service the master files under 'radiology' always is (modality 'other' when
    its name says nothing); any other line is imaging only if its name says so."""
    if order.get("catalog_type") == "lab_test":
        return None
    item = catalog.get(order.get("catalog_code") or "") or {}
    modality = detect_modality(order.get("item_name"), item.get("name"))
    if item.get("category") == "radiology" or "radiolog" in (item.get("department") or "").lower():
        return modality or "other"
    return modality


def _check_text(text: str, obstetric: bool) -> None:
    hit = fetal_sex_disclosure(text, obstetric)
    if hit:
        logger.warning("RADIOLOGY_PCPNDT_BLOCKED obstetric=%s", obstetric)   # never log the text itself
        status, message = _ERRORS["radiology_pcpndt_violation"]
        raise HTTPException(status_code=status, detail={"code": "radiology_pcpndt_violation", "message": message,
                                                        "term": hit})


# ── reads ───────────────────────────────────────────────────────────────────

async def _catalog_by_code(clinic_id: str) -> dict[str, dict]:
    return {str(i.get("code")): i for i in await effective_catalog(clinic_id) if i.get("code")}


async def get_imaging_order(clinic_id: str, order_id: str) -> tuple[dict, str]:
    """The order and its detected modality; 409 for a lab test or a non-imaging line."""
    order = await orders.get_order(clinic_id, order_id)
    if order["catalog_type"] == "lab_test":
        _refuse("radiology_order_not_imaging")
    modality = classify_order(order, await _catalog_by_code(clinic_id))
    if modality is None:
        raise HTTPException(status_code=409, detail={"code": "radiology_order_not_imaging",
                                                     "message": "This order is not an imaging study."})
    return order, modality


async def _reports_of_order(clinic_id: str, order_id: str) -> list[dict]:
    return await fetch_all(lambda: scoped_query("radiology_reports", clinic_id).eq("order_id", order_id)
                           .order("version"), label="radiology:versions")


async def _form_f_of_order(clinic_id: str, order_id: str) -> Optional[dict]:
    res = await sb(scoped_query("pcpndt_form_f_records", clinic_id).eq("order_id", order_id).limit(1))
    return res.data[0] if res.data else None


async def get_report(clinic_id: str, report_id: str) -> dict:
    row = await lis._one("radiology_reports", clinic_id, report_id)
    if not row:
        _refuse("radiology_report_not_found")
    return row


async def get_report_detail(clinic_id: str, report_id: str) -> dict:
    """A report with its order, patient (no phone), Form F and version list."""
    report = await get_report(clinic_id, report_id)
    return await _detail(clinic_id, report)


async def _detail(clinic_id: str, report: dict) -> dict:
    order = await orders.get_order(clinic_id, report["order_id"])
    subject = await lis.subject_of(clinic_id, order)
    subject.pop("phone", None)
    versions = await _reports_of_order(clinic_id, report["order_id"])
    return {**report, "patient": subject,
            "order": {k: order.get(k) for k in ("id", "status", "item_name", "urgency", "clinical_notes", "source",
                                                "branch_id", "admission_id")},
            "form_f": await _form_f_of_order(clinic_id, report["order_id"]),
            "versions": [{k: r[k] for k in ("id", "version", "status", "finalized_at")} for r in versions],
            "is_latest": bool(versions) and versions[-1]["id"] == report["id"],
            # the console's print preview: frozen once signed, the current letterhead before
            "letterhead": report.get("letterhead_snapshot") or await lis.letterhead_snapshot(clinic_id,
                                                                                            report.get("branch_id"))}


async def get_order_workspace(clinic_id: str, order_id: str) -> dict:
    """What the reporting console opens for an order: its latest report (if
    any) and the suggested modality / obstetric flag for a new draft."""
    order, modality = await get_imaging_order(clinic_id, order_id)
    versions = await _reports_of_order(clinic_id, order_id)
    if versions:
        return {"report": await _detail(clinic_id, versions[-1]), "suggested": None}
    subject = await lis.subject_of(clinic_id, order)
    subject.pop("phone", None)
    return {"report": None, "patient": subject,
            "letterhead": await lis.letterhead_snapshot(clinic_id, order.get("branch_id")),
            "order": {k: order.get(k) for k in ("id", "status", "item_name", "urgency", "clinical_notes", "source",
                                                "branch_id")},
            "suggested": {"modality": modality, "procedure_name": order["item_name"],
                          "is_obstetric": modality == "ultrasound" and is_obstetric_text(
                              f"{order['item_name']} {order.get('clinical_notes') or ''}"),
                          "clinical_history": order.get("clinical_notes")}}


def _modality_match(modality: Optional[str], wanted: Optional[str]) -> bool:
    if not wanted:
        return True
    if wanted == "ct_mri":
        return modality in ("ct", "mri")
    return modality == wanted


async def get_radiology_worklist(clinic_id: str, branch_id: Optional[str] = None, modality: Optional[str] = None,
                                 status: str = "pending", limit: int = 100, offset: int = 0) -> dict:
    """pending: open imaging orders, STAT first, each with its draft (if any).
    completed: reports signed in the last 7 days (IST), newest first."""
    catalog = await _catalog_by_code(clinic_id)
    if status == "completed":
        since = datetime.combine(today_ist() - timedelta(days=6), time.min, tzinfo=IST).astimezone(timezone.utc)

        def build_done():
            q = (scoped_query("radiology_reports", clinic_id).eq("status", "finalized")
                 .gte("finalized_at", since.isoformat()))
            return restrict_to_branch(q, branch_id).order("finalized_at", desc=True).order("id")

        reports = [r for r in await fetch_all(build_done, label="radiology:done")
                   if _modality_match(r["modality"], modality)]
        page = reports[offset:offset + limit]
        order_rows = {o["id"]: o for o in await orders._rows_in("opd_orders", clinic_id, "*", "id",
                                                                [r["order_id"] for r in page])}
        pairs = [(order_rows.get(r["order_id"], {}), r) for r in page]
        total = len(reports)
    else:
        def build_open():
            q = (scoped_query("opd_orders", clinic_id).in_("status", list(OPEN_STATUSES))
                 .neq("catalog_type", "lab_test"))
            return restrict_to_branch(q, branch_id).order("created_at").order("id")

        rows = []
        for o in await fetch_all(build_open, label="radiology:open"):
            m = classify_order(o, catalog)
            if m and _modality_match(m, modality):
                rows.append({**o, "_modality": m})
        rows.sort(key=lambda r: (_URGENCY_RANK.get(r.get("urgency"), 9), r.get("created_at") or "", r["id"]))
        page_orders = rows[offset:offset + limit]
        latest: dict[str, dict] = {}
        for r in await orders._rows_in("radiology_reports", clinic_id,
                                       "id, order_id, version, status, modality, report_number, is_obstetric, "
                                       "updated_at, finalized_at", "order_id", [o["id"] for o in page_orders]):
            if r["order_id"] not in latest or r["version"] > latest[r["order_id"]]["version"]:
                latest[r["order_id"]] = r
        pairs = [(o, latest.get(o["id"])) for o in page_orders]
        total = len(rows)

    page_o = [o for o, _ in pairs]
    patients = {p["id"]: p for p in await orders._rows_in("patients", clinic_id, "id, name, mrn, gender, age_years",
                                                          "id", [o.get("patient_id") for o in page_o])}
    members = {m["id"]: m for m in await orders._rows_in("family_members", clinic_id, "id, full_name, mrn", "id",
                                                         [o.get("family_member_id") for o in page_o])}
    visits = {a["id"]: a for a in await orders._rows_in("appointments", clinic_id, "id, token_number", "id",
                                                        [o.get("appointment_id") for o in page_o])}
    stays = {a["id"]: a for a in await orders._rows_in("ipd_admissions", clinic_id, "id, admission_number", "id",
                                                       [o.get("admission_id") for o in page_o])}
    forms = {f["order_id"]: f for f in await orders._rows_in("pcpndt_form_f_records", clinic_id,
                                                             "id, order_id, form_f_number", "order_id",
                                                             [o.get("id") for o in page_o])}
    out = []
    for o, r in pairs:
        member = members.get(o.get("family_member_id")) if o.get("family_member_id") else None
        p = patients.get(o.get("patient_id"), {})
        modality = (r or {}).get("modality") or o.get("_modality") or classify_order(o, catalog)
        out.append({
            "order_id": o.get("id"), "order_status": o.get("status"), "urgency": o.get("urgency"),
            "source": o.get("source"), "item_name": o.get("item_name"), "clinical_notes": o.get("clinical_notes"),
            "created_at": o.get("created_at"), "branch_id": o.get("branch_id"), "modality": modality,
            "patient_name": (member or {}).get("full_name") or p.get("name"),
            "mrn": (member or {}).get("mrn") or p.get("mrn"),
            "gender": p.get("gender") if not member else None, "age_years": p.get("age_years") if not member else None,
            "token_number": visits.get(o.get("appointment_id"), {}).get("token_number"),
            "admission_number": stays.get(o.get("admission_id"), {}).get("admission_number"),
            "report": None if not r else {k: r.get(k) for k in ("id", "version", "status", "report_number",
                                                               "is_obstetric", "updated_at", "finalized_at")},
            "needs_form_f": bool(r and r.get("is_obstetric") and o.get("id") not in forms),
            "form_f_number": forms.get(o.get("id"), {}).get("form_f_number"),
        })
    return {"items": out, "total": total, "limit": limit, "offset": offset}


# ── write: draft / amendment ────────────────────────────────────────────────

_REPORT_FIELDS = ("modality", "procedure_name", "is_obstetric", "clinical_history", "findings", "impression",
                  "recommendations", "pacs_study_uid", "pacs_viewer_url")


async def create_or_update_report(clinic_id: str, order_id: str, data: dict, actor: Any,
                                  amendment_reason: Optional[str] = None,
                                  expected_updated_at: Optional[str] = None) -> dict:
    """Save the draft for an order (created on first save). On a signed report
    this opens a versioned amendment, which needs a reason. Text that
    determines the sex of the foetus is refused before anything is written."""
    _require(actor, "RADIOLOGY_WRITE")
    order, detected = await get_imaging_order(clinic_id, order_id)
    row = {k: data.get(k) for k in _REPORT_FIELDS}
    row["modality"] = row.get("modality") or detected
    row["procedure_name"] = (row.get("procedure_name") or order["item_name"]).strip()
    obstetric = bool(row.get("is_obstetric")) or (row["modality"] == "ultrasound" and is_obstetric_text(
        f"{row['procedure_name']} {row.get('clinical_history') or ''}"))
    if obstetric and row["modality"] != "ultrasound":
        _refuse("radiology_obstetric_needs_ultrasound")
    row["is_obstetric"] = obstetric
    if obstetric:
        subject = await lis.subject_of(clinic_id, order)
        if subject["gender"] == "male":
            raise HTTPException(status_code=422, detail="An obstetric ultrasound needs a female patient. Check the "
                                                        "patient record or the procedure.")
    _check_text(" ".join(str(row.get(k) or "") for k in ("procedure_name", "clinical_history", "findings",
                                                          "impression", "recommendations"))
                + " " + (amendment_reason or ""), obstetric)
    saved = await _rpc("radiology_save_report", {
        "p_clinic_id": clinic_id, "p_order_id": order_id, "p_row": row,
        "p_amendment_reason": (amendment_reason or "").strip() or None,
        "p_expected_updated_at": expected_updated_at or None,
        "p_actor_id": actor_uuid(actor), "p_actor_name": str(getattr(actor, "username", None) or "system")[:100],
    })
    return saved


# ── write: Form F ───────────────────────────────────────────────────────────

async def _signer_snapshot(clinic_id: str, actor: Any, what: str) -> dict:
    doctor_id = getattr(actor, "doctor_id", None)
    doctor = await lis._one("doctors", clinic_id, str(doctor_id),
                            "id, name, qualifications, registration_number, registration_council") if doctor_id else {}
    if not doctor:
        raise HTTPException(status_code=403, detail=f"Only a login linked to a doctor can {what}.")
    if not (doctor.get("registration_number") or "").strip():
        _refuse("radiology_signer_registration_missing")
    return {"doctor_id": str(doctor["id"]), "name": doctor.get("name") or "",
            "qualifications": doctor.get("qualifications") or "",
            "registration_number": doctor["registration_number"].strip(),
            "registration_council": doctor.get("registration_council") or ""}


_FORM_F_FIELDS = ("patient_age", "relative_name", "referred_by", "num_living_children", "num_female_children",
                  "num_male_children", "last_menstrual_period", "gestational_age_weeks", "clinical_indication",
                  "indication_notes", "invasive_procedure_done", "pregnant_woman_declaration_signed",
                  "doctor_declaration_signed")


async def save_pcpndt_form_f(clinic_id: str, report_id: str, data: dict, actor: Any) -> dict:
    """Record Form F for an obstetric draft and lock it in the same step. The
    doctor performing the scan signs it (a login linked to a doctor with a
    registration number). A repeat returns the recorded form (`replayed`)."""
    _require(actor, "RADIOLOGY_WRITE")
    report = await get_report(clinic_id, report_id)
    signer = await _signer_snapshot(clinic_id, actor, "sign Form F")
    order = await orders.get_order(clinic_id, report["order_id"])
    subject = await lis.subject_of(clinic_id, order)
    if subject["gender"] == "male":
        raise HTTPException(status_code=422, detail="Form F is for a pregnant woman. Check the patient record.")
    row = {k: data.get(k) for k in _FORM_F_FIELDS}
    if row.get("last_menstrual_period") is not None:
        row["last_menstrual_period"] = str(row["last_menstrual_period"])
    if row.get("gestational_age_weeks") is not None:
        row["gestational_age_weeks"] = str(row["gestational_age_weeks"])
    _check_text(f"{row.get('indication_notes') or ''} {row.get('referred_by') or ''} {row.get('relative_name') or ''}",
                True)
    row["patient_snapshot"] = {"name": subject["name"], "mrn": subject["mrn"], "age_years": subject["age_years"],
                               "report_number": report.get("report_number")}
    row["letterhead_snapshot"] = await lis.letterhead_snapshot(clinic_id, report.get("branch_id"))
    return await _rpc("radiology_record_form_f", {
        "p_clinic_id": clinic_id, "p_report_id": report_id, "p_row": row, "p_doctor_id": signer["doctor_id"],
        "p_doctor_snapshot": signer, "p_actor_id": actor_uuid(actor),
    })


# ── write: finalize & sign ──────────────────────────────────────────────────

async def finalize_report(clinic_id: str, report_id: str, expected_updated_at: str, actor: Any) -> dict:
    """Sign the draft exactly as reviewed (expected_updated_at) and complete the
    order in the same transaction; then deliver the PDF in the background. An
    obstetric ultrasound is refused (422) until Form F is recorded. A double
    click returns the first signature (`replayed`)."""
    _require(actor, "RADIOLOGY_WRITE")
    report = await get_report(clinic_id, report_id)
    signer = await _signer_snapshot(clinic_id, actor, "sign a radiology report")
    order = await orders.get_order(clinic_id, report["order_id"])
    subject = await lis.subject_of(clinic_id, order)
    doctor = await lis._one("doctors", clinic_id, order.get("doctor_id"), "id, name")
    form = await _form_f_of_order(clinic_id, report["order_id"]) if report.get("is_obstetric") else None
    patient_snapshot = {"name": subject["name"], "mrn": subject["mrn"], "gender": subject["gender"],
                        "age_years": subject["age_years"], "referred_by": doctor.get("name"),
                        "source": order.get("source"), "form_f_number": (form or {}).get("form_f_number")}
    signed = await _rpc("radiology_finalize_report", {
        "p_clinic_id": clinic_id, "p_report_id": report_id, "p_expected_updated_at": expected_updated_at,
        "p_actor_id": actor_uuid(actor), "p_doctor_id": signer["doctor_id"], "p_signer_snapshot": signer,
        "p_letterhead_snapshot": await lis.letterhead_snapshot(clinic_id, report.get("branch_id")),
        "p_patient_snapshot": patient_snapshot,
    })
    if not signed.get("replayed"):
        spawn_background_task(deliver_report(clinic_id, report_id), name=f"radiology_deliver_{report_id}")
    return signed


# ── PDFs and delivery ───────────────────────────────────────────────────────

async def render_report(clinic_id: str, report_id: str) -> tuple[bytes, dict]:
    report = await get_report(clinic_id, report_id)
    if report["status"] not in ("finalized", "amended"):
        raise HTTPException(status_code=409, detail="Only a signed report has a PDF.")
    return render_radiology_report_pdf(report), report


async def render_form_f(clinic_id: str, report_id: str) -> tuple[bytes, dict]:
    report = await get_report(clinic_id, report_id)
    form = await _form_f_of_order(clinic_id, report["order_id"])
    if not form:
        raise HTTPException(status_code=404, detail="No Form F is recorded for this study.")
    return render_pcpndt_form_f_pdf(form), form


def _safe_name(text: Optional[str], fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or fallback).strip("_")[:40] or fallback


def report_filename(report: dict) -> str:
    return f"{report.get('report_number') or 'RAD'}_v{report['version']}_{_safe_name(report.get('procedure_name'), 'Radiology')}.pdf"


def form_f_filename(form: dict) -> str:
    return f"{form.get('form_f_number') or 'Form_F'}.pdf"


async def deliver_report(clinic_id: str, report_id: str) -> None:
    """Send the signed PDF to the patient's WhatsApp through the lab-report
    pipeline (storage, dispatch gates, retry queue). external_report_id makes
    a re-run a no-op. An obstetric report skips the AI summary. Never raises:
    the signature is already committed. Form F is a facility record and is
    never sent."""
    try:
        pdf, report = await render_report(clinic_id, report_id)
        patient = await lis._one("patients", clinic_id, report["patient_id"], "id, phone")
        phone = (patient.get("phone") or "").strip()
        if not phone:
            logger.warning(f"RADIOLOGY_DELIVERY_SKIPPED report={report_id} reason=no_phone")
            return
        from app.services.lab_reports import LabReportService   # the delivery pipeline, loaded only when sending

        record = await LabReportService().upload_and_send(
            clinic_id=clinic_id, file_bytes=pdf, filename=report_filename(report), content_type="application/pdf",
            patient_phone=phone, patient_name=(report.get("patient_snapshot") or {}).get("name") or "Patient",
            report_name=report["procedure_name"], report_type="Radiology",
            external_report_id=f"rad-{report_id}", source="radiology", match_confidence=1.0,
            match_source="radiology_order", matched_patient_id=str(report["patient_id"]),
            ai_summary=not report.get("is_obstetric"),
        )
        path = (record or {}).get("file_path")
        if path and "Storage upload failed" not in str((record or {}).get("error_message") or ""):
            await sb(supabase.table("radiology_reports").update({"pdf_storage_path": path})
                     .eq("clinic_id", clinic_id).eq("id", report_id).is_("pdf_storage_path", "null"))
        logger.info(f"RADIOLOGY_DELIVERY report={report_id} to={mask_phone(phone)} status={(record or {}).get('status')}")
    except Exception as e:  # noqa: BLE001 - delivery failure must not undo a signed report
        logger.error(f"RADIOLOGY_DELIVERY_FAILED report={report_id} type={type(e).__name__}")
