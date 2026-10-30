"""In-house Lab Information System (G2, migration 118).

A G1 lab order (opd_orders, catalog_type = 'lab_test') whose sample is
collected gets its parameter values entered by a technician and signed by a
pathologist. Entry and sign-off are each one SQL function
(lis_record_results / lis_validate_result), so the result, its values and the
order's status move together or not at all; the database keeps a signed
report immutable (lis_guard_result).

Zero-LLM clinical path: picking the reference range for the patient's age and
sex and flagging low / high / critical is the plain code below. This module
imports nothing from the AI, voice or assistant packages
(tests/test_ipd_no_llm.py). Delivery hands the signed PDF to the existing
lab-report WhatsApp pipeline (app/services/lab_reports.py) after the report is
final.
"""

import logging
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import HTTPException

from app.database import fetch_all, restrict_to_branch, sb, scoped_query, supabase
from app.services import orders
from app.services.opd_pdf import render_lab_report_pdf
from app.services.permissions import holds_lab_permission
from app.services.tenant import clinic_letterhead, get_clinic_by_id
from app.utils.async_tasks import spawn_background_task
from app.utils.helpers import IST, actor_uuid, today_ist
from app.utils.validators import mask_phone

logger = logging.getLogger(__name__)

MAX_AGE_DAYS = 43800
_NUMERIC_LIMIT = Decimal("9999999.999")   # NUMERIC(10,3)

# error code (prefix before ':') -> (HTTP status, message shown to staff)
_ERRORS: dict[str, tuple[int, str]] = {
    "lis_order_not_found": (404, "Order not found."),
    "lis_order_not_lab": (409, "Only catalogue lab tests have result worksheets."),
    "lis_order_not_ready": (409, "Collect the sample before entering results. Reload the worklist."),
    "lis_result_not_found": (404, "Result not found."),
    "lis_not_pending": (409, "This result is not waiting for validation. Reload the queue."),
    "lis_result_changed": (409, "The values were changed after you opened them. Reload and review again before "
                                "signing."),
    "lis_values_empty": (422, "Enter at least one parameter value."),
    "lis_amendment_reason_required": (422, "This report is already signed. Give an amendment reason (5+ characters) "
                                           "to issue a corrected version."),
    "lis_signer_registration_missing": (409, "Signing needs the doctor's registration number. Add it to the doctor "
                                             "profile first."),
    "lis_report_locked": (409, "A signed report cannot be changed. Issue an amendment instead."),
    "lis_append_only": (409, "Lab results cannot be deleted."),
    "lis_bad_transition": (409, "The result is not in a state that allows this step. Reload it."),
}


def raise_for(exc: Exception) -> None:
    """Map a lis_* database error to an HTTPException; opd_* codes and anything
    else go to the orders mapper (never the raw database text)."""
    if isinstance(exc, HTTPException):
        raise exc
    msg = str(getattr(exc, "message", None) or exc)
    for code in sorted(_ERRORS, key=len, reverse=True):
        if code in msg:
            status, message = _ERRORS[code]
            raise HTTPException(status_code=status, detail={"code": code, "message": message})
    orders.raise_for(exc)


async def _rpc(name: str, params: dict) -> Any:
    try:
        res = await sb(supabase.rpc(name, params))
    except Exception as e:  # postgrest APIError carries the lis_* code
        raise_for(e)
    return res.data


# ── deterministic reference ranges and flags ────────────────────────────────

def _dec(v: Any) -> Optional[Decimal]:
    return None if v is None else Decimal(str(v))


def _fmt(v: Any) -> str:
    d = _dec(v).quantize(Decimal("0.001")).normalize()
    return format(d, "f")


def normalize_gender(raw: Optional[str]) -> Optional[str]:
    g = (raw or "").strip().lower()
    return {"m": "male", "male": "male", "f": "female", "female": "female", "o": "other", "other": "other"}.get(g)


def age_in_days(age_years: Optional[int], recorded_on: Any = None, today: Optional[date] = None) -> Optional[int]:
    """Age now, from the age recorded on a date (patients store no birth date)."""
    if age_years is None:
        return None
    today = today or today_ist()
    days = int(age_years) * 365 + int(age_years) // 4
    if recorded_on:
        days += max(0, (today - date.fromisoformat(str(recorded_on)[:10])).days)
    return days


def select_range(rows: list[dict], age_days: Optional[int], gender: Optional[str]) -> Optional[dict]:
    """The definition row whose age / sex band covers this patient: a sex-specific
    band beats 'all', then the narrowest age band. Unknown age matches only
    age-independent bands, unknown sex only 'all' bands; None if nothing covers."""
    def covers(r: dict) -> bool:
        if r["gender"] not in ("all", gender):
            return False
        if age_days is None:
            return r["min_age_days"] == 0 and r["max_age_days"] >= MAX_AGE_DAYS
        return r["min_age_days"] <= age_days <= r["max_age_days"]

    fits = [r for r in rows if covers(r)]
    fits.sort(key=lambda r: (r["gender"] == "all", r["max_age_days"] - r["min_age_days"], r["id"]))
    return fits[0] if fits else None


def range_display(p: dict) -> Optional[str]:
    unit = f" {p['unit']}" if p.get("unit") else ""
    if p["data_type"] != "numeric":
        return p.get("normal_text_value") or None
    lo, hi = p.get("normal_low"), p.get("normal_high")
    if lo is not None and hi is not None:
        return f"{_fmt(lo)} - {_fmt(hi)}{unit}"
    if hi is not None:
        return f"< {_fmt(hi)}{unit}"
    if lo is not None:
        return f"> {_fmt(lo)}{unit}"
    return None


def flag_value(p: Optional[dict], numeric_value: Optional[Decimal], text_value: Optional[str]) -> str:
    """normal / low / high / critical_low / critical_high / abnormal, or
    not_evaluated when no range applies. `p` None = no band covers the patient.
    Critical limits are checked first: a critical value is never reported as
    merely low or high."""
    if p is None:
        return "not_evaluated"
    if p["data_type"] == "numeric":
        bounds = [_dec(p.get(k)) for k in ("critical_low", "normal_low", "normal_high", "critical_high")]
        clo, lo, hi, chi = bounds
        if numeric_value is None or all(b is None for b in bounds):
            return "not_evaluated"
        if clo is not None and numeric_value < clo:
            return "critical_low"
        if chi is not None and numeric_value > chi:
            return "critical_high"
        if lo is not None and numeric_value < lo:
            return "low"
        if hi is not None and numeric_value > hi:
            return "high"
        return "normal"
    expected = (p.get("normal_text_value") or "").strip().casefold()
    if not expected or text_value is None:
        return "not_evaluated"
    return "normal" if text_value.strip().casefold() == expected else "abnormal"


# ── reads ───────────────────────────────────────────────────────────────────

async def _one(table: str, clinic_id: str, row_id: Optional[str], cols: str = "*") -> dict:
    if not row_id:
        return {}
    res = await sb(scoped_query(table, clinic_id, cols).eq("id", row_id).limit(1))
    return res.data[0] if res.data else {}


async def subject_of(clinic_id: str, order: dict) -> dict:
    """The person tested (a family member when the order names one) and the
    account holder's phone, which is where the report is delivered."""
    patient = await _one("patients", clinic_id, order["patient_id"],
                         "id, name, mrn, phone, gender, age_years, age_recorded_on")
    who = patient
    if order.get("family_member_id"):
        member = await _one("family_members", clinic_id, order["family_member_id"],
                            "id, full_name, mrn, gender, age_years, age_recorded_on")
        who = {**member, "name": member.get("full_name")}
    gender = normalize_gender(who.get("gender"))
    return {"name": who.get("name") or "Patient", "mrn": who.get("mrn"), "gender": gender,
            "age_years": who.get("age_years"), "age_days": age_in_days(who.get("age_years"), who.get("age_recorded_on")),
            "phone": patient.get("phone")}


async def get_lab_order(clinic_id: str, order_id: str) -> dict:
    order = await orders.get_order(clinic_id, order_id)
    if order["catalog_type"] != "lab_test" or not order.get("catalog_reference_id"):
        raise HTTPException(status_code=409, detail={"code": "lis_order_not_lab",
                                                     "message": _ERRORS["lis_order_not_lab"][1]})
    return order


async def _parameter_rows(clinic_id: str, lab_test_id: str, active_only: bool = True) -> list[dict]:
    def build():
        q = scoped_query("lab_test_parameters", clinic_id).eq("lab_test_id", lab_test_id)
        if active_only:
            q = q.eq("is_active", True)
        return q.order("sort_order").order("name").order("id")
    return await fetch_all(build, label="lis:parameters")


def _worksheet_lines(rows: list[dict], subject: dict) -> list[dict]:
    """One line per parameter name, with the band that applies to this patient.
    A parameter with no covering band is still enterable, never auto-flagged."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["name"].strip().casefold(), []).append(r)
    lines = []
    for rows_for_name in groups.values():
        chosen = select_range(rows_for_name, subject["age_days"], subject["gender"])
        base = chosen or rows_for_name[0]
        lines.append({**base, "parameter_id": base["id"], "range_applies": chosen is not None,
                      "reference_range_display": range_display(base) if chosen else None})
    lines.sort(key=lambda ln: (ln["sort_order"], ln["name"].casefold()))
    return lines


async def _results_of_order(clinic_id: str, order_id: str) -> list[dict]:
    return await fetch_all(lambda: scoped_query("lab_results", clinic_id).eq("order_id", order_id)
                           .order("version"), label="lis:results")


async def _values_of(clinic_id: str, result_id: str) -> list[dict]:
    return await fetch_all(lambda: scoped_query("lab_result_values", clinic_id).eq("result_id", result_id)
                           .order("sort_order").order("parameter_name").order("id"), label="lis:values")


async def get_worksheet(clinic_id: str, order_id: str) -> dict:
    order = await get_lab_order(clinic_id, order_id)
    subject = await subject_of(clinic_id, order)
    lines = _worksheet_lines(await _parameter_rows(clinic_id, order["catalog_reference_id"]), subject)
    versions = await _results_of_order(clinic_id, order_id)
    latest = versions[-1] if versions else None
    prefill = {v["parameter_id"]: v for v in await _values_of(clinic_id, latest["id"])} if latest else {}
    for ln in lines:
        v = prefill.get(ln["parameter_id"])
        ln["entered"] = None if not v else {
            "value": _fmt(v["numeric_value"]) if v.get("numeric_value") is not None else v.get("text_value"),
            "flag": v["flag"], "remarks": v.get("remarks")}
    subject.pop("phone", None)
    return {
        "order": {k: order.get(k) for k in ("id", "status", "item_name", "catalog_reference_id", "sample_type",
                                            "specimen_barcode", "sample_collected_at", "urgency", "branch_id",
                                            "source")},
        "patient": subject, "parameters": lines,
        "result": latest, "versions": [{k: r[k] for k in ("id", "version", "status", "validated_at")}
                                       for r in versions],
        "needs_amendment_reason": bool(latest and latest["status"] == "validated"),
    }


# ── technician: enter results ───────────────────────────────────────────────

def _parse_value(line: dict, raw: Any) -> tuple[Optional[Decimal], Optional[str]]:
    if line["data_type"] == "numeric":
        try:
            num = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError):
            raise HTTPException(status_code=422, detail=f"{line['name']}: enter a number.")
        if not num.is_finite() or abs(num) > _NUMERIC_LIMIT:
            raise HTTPException(status_code=422, detail=f"{line['name']}: value out of range.")
        return num.quantize(Decimal("0.001")), None
    text = str(raw).strip()
    if not text or len(text) > 500:
        raise HTTPException(status_code=422, detail=f"{line['name']}: enter a value (up to 500 characters).")
    if line["data_type"] == "options":
        allowed = {str(o).strip().casefold(): str(o) for o in (line.get("options") or [])}
        if text.casefold() not in allowed:
            raise HTTPException(status_code=422, detail=f"{line['name']}: choose one of {', '.join(allowed.values())}.")
        text = allowed[text.casefold()]
    return None, text


async def record_test_results(clinic_id: str, order_id: str, values_list: list[dict], actor: Any,
                              amendment_reason: Optional[str] = None) -> dict:
    """Save the technician's values. Flags are computed here from the
    parameter bands, never taken from the request. The first save moves the
    order to in_progress; a save on a signed report is a new amendment version."""
    if not holds_lab_permission(actor, "LAB_ENTRY"):
        raise HTTPException(status_code=403, detail="Missing permission: LAB_ENTRY")
    order = await get_lab_order(clinic_id, order_id)
    subject = await subject_of(clinic_id, order)
    lines = {ln["parameter_id"]: ln
             for ln in _worksheet_lines(await _parameter_rows(clinic_id, order["catalog_reference_id"]), subject)}
    if not lines:
        raise HTTPException(status_code=409, detail="This test has no parameters defined. Set them up first.")

    payload, seen = [], set()
    for item in values_list:
        pid = str(item.get("parameter_id") or "")
        line = lines.get(pid)
        if not line:
            raise HTTPException(status_code=422, detail="A value names a parameter that is not on this worksheet. "
                                                        "Reload it.")
        if pid in seen:
            raise HTTPException(status_code=422, detail=f"{line['name']} is entered twice.")
        seen.add(pid)
        if item.get("value") is None or str(item.get("value")).strip() == "":
            continue   # not done: left off the report
        num, text = _parse_value(line, item["value"])
        flag = flag_value(line if line["range_applies"] else None, num, text)
        payload.append({
            "parameter_id": pid, "parameter_name": line["name"], "unit": line.get("unit"),
            "sort_order": line["sort_order"], "numeric_value": str(num) if num is not None else None,
            "text_value": text, "flag": flag, "reference_range_display": line["reference_range_display"],
            "remarks": (str(item.get("remarks") or "").strip()[:500]) or None,
        })
    if not payload:
        raise HTTPException(status_code=422, detail=_ERRORS["lis_values_empty"][1])

    result = await _rpc("lis_record_results", {
        "p_clinic_id": clinic_id, "p_order_id": order_id, "p_values": payload,
        "p_has_critical": any(v["flag"].startswith("critical") for v in payload),
        "p_amendment_reason": (amendment_reason or "").strip() or None,
        "p_actor_id": actor_uuid(actor), "p_actor_name": str(getattr(actor, "username", None) or "system")[:100],
    })
    return {**result, "values": await _values_of(clinic_id, result["id"])}


# ── pathologist: validate & sign ────────────────────────────────────────────

async def get_result(clinic_id: str, result_id: str) -> dict:
    row = await _one("lab_results", clinic_id, result_id)
    if not row:
        raise HTTPException(status_code=404, detail="Result not found.")
    return row


async def get_result_detail(clinic_id: str, result_id: str) -> dict:
    result = await get_result(clinic_id, result_id)
    return {**result, "values": await _values_of(clinic_id, result_id)}


async def _signer_snapshot(clinic_id: str, actor: Any) -> dict:
    doctor_id = getattr(actor, "doctor_id", None)
    if not doctor_id:
        raise HTTPException(status_code=403, detail="Only a login linked to a doctor can sign a lab report.")
    doctor = await _one("doctors", clinic_id, str(doctor_id),
                        "id, name, qualifications, registration_number, registration_council")
    if not doctor:
        raise HTTPException(status_code=403, detail="Only a login linked to a doctor can sign a lab report.")
    if not (doctor.get("registration_number") or "").strip():
        raise HTTPException(status_code=409, detail={"code": "lis_signer_registration_missing",
                                                     "message": _ERRORS["lis_signer_registration_missing"][1]})
    return {"doctor_id": str(doctor["id"]), "name": doctor.get("name") or "",
            "qualifications": doctor.get("qualifications") or "",
            "registration_number": doctor["registration_number"].strip(),
            "registration_council": doctor.get("registration_council") or ""}


async def letterhead_snapshot(clinic_id: str, branch_id: Optional[str]) -> dict:
    clinic = await get_clinic_by_id(clinic_id) or {}
    lh = clinic_letterhead(clinic)
    branch = await _one("branches", clinic_id, branch_id, "id, name, address") if branch_id else {}
    return {"clinic_name": lh["name"], "address": lh["address"], "phone": lh["phone"],
            "email": clinic.get("email") or "", "branch_name": branch.get("name"),
            "branch_address": branch.get("address")}


async def validate_and_sign_report(clinic_id: str, result_id: str, expected_updated_at: str, actor: Any) -> dict:
    """Sign the entered values exactly as reviewed (expected_updated_at) and
    complete the order in the same transaction; then deliver the PDF in the
    background. A double click returns the first signature (`replayed`)."""
    if not holds_lab_permission(actor, "LAB_VALIDATE"):
        raise HTTPException(status_code=403, detail="Missing permission: LAB_VALIDATE")
    result = await get_result(clinic_id, result_id)
    signer = await _signer_snapshot(clinic_id, actor)
    order = await orders.get_order(clinic_id, result["order_id"])
    subject = await subject_of(clinic_id, order)
    doctor = await _one("doctors", clinic_id, order.get("doctor_id"), "id, name")
    patient_snapshot = {
        "name": subject["name"], "mrn": subject["mrn"], "gender": subject["gender"], "age_years": subject["age_years"],
        "referred_by": doctor.get("name"), "sample_type": order.get("sample_type"),
        "specimen_barcode": order.get("specimen_barcode"), "sample_collected_at": order.get("sample_collected_at"),
        "source": order.get("source"),
    }
    signed = await _rpc("lis_validate_result", {
        "p_clinic_id": clinic_id, "p_result_id": result_id, "p_expected_updated_at": expected_updated_at,
        "p_actor_id": actor_uuid(actor), "p_pathologist_snapshot": signer,
        "p_letterhead_snapshot": await letterhead_snapshot(clinic_id, result.get("branch_id")),
        "p_patient_snapshot": patient_snapshot,
    })
    if not signed.get("replayed"):
        spawn_background_task(deliver_report(clinic_id, result_id), name=f"lis_deliver_{result_id}")
    return signed


async def render_report(clinic_id: str, result_id: str) -> tuple[bytes, dict]:
    result = await get_result(clinic_id, result_id)
    if result["status"] not in ("validated", "amended"):
        raise HTTPException(status_code=409, detail="Only a signed report has a PDF.")
    return render_lab_report_pdf(result, await _values_of(clinic_id, result_id)), result


def report_filename(result: dict) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", result.get("test_name") or "Lab_Report").strip("_")[:40] or "Lab_Report"
    return f"{name}_v{result['version']}_{str(result['id'])[:8]}.pdf"


async def deliver_report(clinic_id: str, result_id: str) -> None:
    """Send the signed PDF to the patient's WhatsApp through the lab-report
    pipeline (storage, dispatch gates, retry queue). The order fixes the
    patient, so the match is exact. external_report_id makes a re-run a no-op.
    Never raises: the signature is already committed."""
    try:
        pdf, result = await render_report(clinic_id, result_id)
        patient = await _one("patients", clinic_id, result["patient_id"], "id, phone")
        phone = (patient.get("phone") or "").strip()
        if not phone:
            logger.warning(f"LIS_DELIVERY_SKIPPED result={result_id} reason=no_phone")
            return
        from app.services.lab_reports import LabReportService   # the delivery pipeline, loaded only when sending

        record = await LabReportService().upload_and_send(
            clinic_id=clinic_id, file_bytes=pdf, filename=report_filename(result), content_type="application/pdf",
            patient_phone=phone, patient_name=(result.get("patient_snapshot") or {}).get("name") or "Patient",
            report_name=result["test_name"], report_type="Laboratory",
            external_report_id=f"lis-{result_id}", source="lis", match_confidence=1.0, match_source="lis_order",
            matched_patient_id=str(result["patient_id"]),
            sample_barcode=(result.get("patient_snapshot") or {}).get("specimen_barcode"),
        )
        path = (record or {}).get("file_path")
        if path and "Storage upload failed" not in str((record or {}).get("error_message") or ""):
            await sb(supabase.table("lab_results").update({"pdf_storage_path": path})
                     .eq("clinic_id", clinic_id).eq("id", result_id).is_("pdf_storage_path", "null"))
        logger.info(f"LIS_DELIVERY result={result_id} to={mask_phone(phone)} status={(record or {}).get('status')}")
    except Exception as e:  # noqa: BLE001 - delivery failure must not undo a signed report
        logger.error(f"LIS_DELIVERY_FAILED result={result_id} type={type(e).__name__}")


# ── queue ───────────────────────────────────────────────────────────────────

async def get_review_queue(clinic_id: str, branch_id: Optional[str] = None, status: str = "entered",
                           limit: int = 100, offset: int = 0) -> dict:
    """Pathologist queue: 'entered' = waiting for sign-off (critical first,
    then oldest); 'validated' = signed today (IST)."""
    since = None
    if status == "validated":
        since = datetime.combine(today_ist(), time.min, tzinfo=IST).astimezone(timezone.utc).isoformat()

    def build():
        q = scoped_query("lab_results", clinic_id).eq("status", status)
        if since:
            q = q.gte("validated_at", since)
        return restrict_to_branch(q, branch_id).order("entered_at").order("id")

    rows = await fetch_all(build, label="lis:queue")
    if status == "entered":
        rows.sort(key=lambda r: (not r["has_critical_value"], r.get("entered_at") or "", r["id"]))
    else:
        rows.sort(key=lambda r: r.get("validated_at") or "", reverse=True)
    page = rows[offset:offset + limit]
    patients = {p["id"]: p for p in await orders._rows_in("patients", clinic_id, "id, name, mrn", "id",
                                                          [r["patient_id"] for r in page])}
    members = {m["id"]: m for m in await orders._rows_in("family_members", clinic_id, "id, full_name, mrn", "id",
                                                         [r.get("family_member_id") for r in page])}
    order_rows = {o["id"]: o for o in await orders._rows_in("opd_orders", clinic_id,
                                                            "id, urgency, specimen_barcode, source", "id",
                                                            [r["order_id"] for r in page])}
    out = []
    for r in page:
        who = members.get(r.get("family_member_id")) or {}
        p = patients.get(r["patient_id"], {})
        o = order_rows.get(r["order_id"], {})
        out.append({k: r.get(k) for k in ("id", "order_id", "test_name", "version", "status", "has_critical_value",
                                          "technician_name", "entered_at", "validated_at", "updated_at",
                                          "amendment_reason", "branch_id")}
                   | {"patient_name": who.get("full_name") or p.get("name"), "mrn": who.get("mrn") or p.get("mrn"),
                      "urgency": o.get("urgency"), "specimen_barcode": o.get("specimen_barcode"),
                      "source": o.get("source"),
                      "signer_name": (r.get("pathologist_snapshot") or {}).get("name")})
    return {"results": out, "total": len(rows), "limit": limit, "offset": offset}


# ── parameter definitions ───────────────────────────────────────────────────

_PARAM_FIELDS = ("name", "unit", "data_type", "options", "sort_order", "min_age_days", "max_age_days", "gender",
                 "normal_low", "normal_high", "critical_low", "critical_high", "normal_text_value", "is_active")


async def _lab_test(clinic_id: str, lab_test_id: str) -> dict:
    test = await _one("lab_tests", clinic_id, lab_test_id, "id, name, sample_type, is_active")
    if not test:
        raise HTTPException(status_code=404, detail="Lab test not found.")
    return test


async def list_parameters(clinic_id: str, lab_test_id: str) -> dict:
    test = await _lab_test(clinic_id, lab_test_id)
    return {"lab_test": test, "parameters": await _parameter_rows(clinic_id, lab_test_id, active_only=False)}


def _param_row(data: dict) -> dict:
    row = {k: data[k] for k in _PARAM_FIELDS if k in data}
    for k in ("normal_low", "normal_high", "critical_low", "critical_high"):
        if row.get(k) is not None:
            row[k] = str(row[k])
    return row


async def _write_parameter(query) -> dict:
    try:
        res = await sb(query)
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=404, detail="Parameter not found.")
    return res.data[0]


async def create_parameter(clinic_id: str, lab_test_id: str, data: dict) -> dict:
    await _lab_test(clinic_id, lab_test_id)
    # unscoped: insert_scoped_by_payload
    return await _write_parameter(supabase.table("lab_test_parameters").insert(
        {**_param_row(data), "clinic_id": clinic_id, "lab_test_id": lab_test_id}))


async def update_parameter(clinic_id: str, lab_test_id: str, parameter_id: str, data: dict) -> dict:
    """Edits apply to future results only: every saved value carries its own
    name, unit and range text."""
    return await _write_parameter(supabase.table("lab_test_parameters").update(_param_row(data))
                                  .eq("clinic_id", clinic_id).eq("lab_test_id", lab_test_id).eq("id", parameter_id))
