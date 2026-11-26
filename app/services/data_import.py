"""Kriya Data Import — "Switch to Kriya" (migration 144; lane MI, docs/data-import/SPEC.md).

One engine for every kind: read CSV/XLSX (size / row caps, values only), map headers through a preset (a column-name
map only), validate each row into a typed dict with row-level errors, store the dry run, apply it idempotently (the
same file twice changes nothing), undo it atomically (mi_undo). Principles (SPEC §2): consent is never set; legacy
history is labelled legacy and read-only; future appointments are validated through get_available_slots first
(holidays, doctor leave, sessions — VERIFICATION D16) and booked through book_appointment, never forced, with no
confirmation message. Zero-LLM.
"""

import csv
import hashlib
import io
import logging
import re
from datetime import date, datetime, time
from typing import Any, Callable, Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services import client_data
from app.utils.helpers import today_ist
from app.utils.validators import normalize_phone, validate_phone

logger = logging.getLogger(__name__)

MAX_BYTES = client_data.IMPORT_MAX_FILE_BYTES          # 5 MB, the existing patient-import cap
MAX_ROWS = client_data.IMPORT_MAX_ROWS                 # 25,000
MAX_ERRORS = client_data.MAX_ISSUES_REPORTED           # 100 shown per batch
PRESETS = ("generic", "mocdoc", "practo_insta")
#: Wizard order (UI spec §7.8): each step depends on the ones before it.
STEPS = ("doctors", "services", "packages", "payers", "patients", "history", "lab_values", "dues",
         "appointments_future", "inpatients_cutover", "chronic_list")
LABELS = {"doctors": "Doctors & schedules", "services": "Services & tariffs", "packages": "IPD packages",
          "payers": "Insurance payers", "patients": "Patient list", "history": "Visit history",
          "lab_values": "Lab history", "dues": "Outstanding dues", "appointments_future": "Future appointments",
          "inpatients_cutover": "Current inpatients (cutover list)", "chronic_list": "Chronic patients"}
_ICD = re.compile(r"^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$")
_CODE = re.compile(r"^[A-Z0-9_-]{2,40}$")


class RowError(ValueError):
    pass


# ── cell parsers (pure) ──────────────────────────────────────────────────────

def _text(max_len: int, min_len: int = 1) -> Callable[[str], str]:
    def p(v: str) -> str:
        v = " ".join(str(v).split())
        if len(v) < min_len:
            raise RowError("is empty")
        if len(v) > max_len:
            raise RowError(f"is longer than {max_len} characters")
        return v
    return p


def _phone(v: str) -> str:
    raw = re.sub(r"[^\d+]", "", str(v))
    if re.fullmatch(r"0\d{10}", raw):                     # 098480 22338: the trunk 0 before a mobile number
        raw = raw[1:]
    p = normalize_phone(raw)
    if not (validate_phone(p) and re.fullmatch(r"\+?\d{10,15}", p)):
        raise RowError(f"'{str(v)[:30]}' is not a valid phone number")
    return p


def _date(v: str) -> str:
    try:
        return client_data.parse_date(str(v)).isoformat()
    except ValueError:
        raise RowError(f"'{str(v)[:30]}' is not a date (use DD/MM/YYYY)")


def _time(v: str) -> str:
    s = str(v).strip().lower().replace(".", ":")
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p"):
        try:
            return datetime.strptime(s, fmt).strftime("%H:%M")
        except ValueError:
            continue
    raise RowError(f"'{str(v)[:20]}' is not a time (use 10:30 or 10:30 AM)")


def _paise(v: str) -> int:
    s = re.sub(r"[₹,\s]|rs\.?|inr", "", str(v).lower())
    if not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", s):
        raise RowError(f"'{str(v)[:20]}' is not an amount in rupees")
    return int(round(float(s) * 100))


def _int(lo: int, hi: int) -> Callable[[str], int]:
    def p(v: str) -> int:
        s = str(v).strip()
        if not re.fullmatch(r"\d{1,6}(\.0+)?", s) or not lo <= int(float(s)) <= hi:
            raise RowError(f"must be a whole number from {lo} to {hi}")
        return int(float(s))
    return p


def _choice(mapping: dict) -> Callable[[str], str]:
    def p(v: str) -> str:
        k = re.sub(r"[^a-z0-9]+", " ", str(v).lower()).strip()
        if k not in mapping:
            raise RowError(f"'{str(v)[:30]}' is not one of: {', '.join(sorted(set(mapping.values())))}")
        return mapping[k]
    return p


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _days(v: str) -> str:
    s = str(v).strip().title().replace(" ", "")
    m = re.fullmatch(r"([A-Z][a-z]{2})[a-z]*-([A-Z][a-z]{2})[a-z]*", s)
    if m and m.group(1) in _DAYS and m.group(2) in _DAYS:
        a, b = _DAYS.index(m.group(1)), _DAYS.index(m.group(2))
        if a <= b:
            return ",".join(_DAYS[a:b + 1])
    out = []
    for part in re.split(r"[,/;]", s):
        d = part[:3]
        if d not in _DAYS:
            raise RowError(f"'{str(v)[:30]}' is not a list of days (Mon,Tue or Mon-Sat)")
        if d not in out:
            out.append(d)
    return ",".join(d for d in _DAYS if d in out)


def _slots(v: str) -> list:
    out = sorted({_time(x) for x in re.split(r"[,;|]", str(v)) if x.strip()})
    if not out or len(out) > 48:
        raise RowError("needs 1–48 times like 09:00,09:30")
    return out


def _icd(v: str) -> str:
    s = str(v).strip().upper()
    if not _ICD.fullmatch(s):
        raise RowError(f"'{s[:12]}' is not an ICD-10 code (e.g. E11.9)")
    return s


def _gst(v: str) -> int:
    s = str(v).strip().rstrip("%")
    try:
        bps = int(round(float(s) * 100))
    except ValueError:
        raise RowError("is not a GST rate")
    if bps not in (0, 500, 1200, 1800, 2800, 4000):
        raise RowError("GST must be 0, 5, 12, 18, 28 or 40 %")
    return bps


def _code(v: str) -> str:
    s = re.sub(r"\s+", "_", str(v).strip().upper())
    if not _CODE.fullmatch(s):
        raise RowError("must be 2–40 letters, digits, _ or -")
    return s


def _cats(v: str) -> list:
    allowed = {"bed", "nursing", "doctor_visit", "procedure", "investigation", "pharmacy", "consumable", "other"}
    out = sorted({re.sub(r"[^a-z]+", "_", x.strip().lower()).strip("_") for x in re.split(r"[,;|]", str(v)) if x.strip()})
    if not out or len(out) > 8 or not set(out) <= allowed:
        raise RowError(f"categories must be from: {', '.join(sorted(allowed))}")
    return out


def _hsn(v: str) -> str:
    s = str(v).strip()
    if not re.fullmatch(r"[0-9]{4,8}", s):
        raise RowError("must be 4–8 digits")
    return s


def _positive_paise(minimum: int) -> Callable[[str], int]:
    def p(v: str) -> int:
        n = _paise(v)
        if n < minimum:
            raise RowError("must be above 0")
        return n
    return p


SERVICE_CATS = {"consultation": "consultation", "consult": "consultation", "procedure": "procedure",
                "nursing": "nursing", "radiology": "radiology", "imaging": "radiology", "diagnostic": "diagnostic",
                "lab": "diagnostic", "laboratory": "diagnostic", "physiotherapy": "physiotherapy", "physio": "physiotherapy",
                "ambulance": "ambulance", "room service": "room_service", "room": "room_service", "other": "other",
                "misc": "other"}
PAYER_TYPES = {"tpa": "tpa", "insurance": "private_insurance", "private insurance": "private_insurance",
               "insurer": "private_insurance", "government": "government_scheme", "government scheme": "government_scheme",
               "govt": "government_scheme", "scheme": "government_scheme", "corporate": "corporate"}
FLAGS = {"h": "high", "high": "high", "l": "low", "low": "low", "n": "normal", "normal": "normal",
         "abnormal": "abnormal", "a": "abnormal", "critical": "critical", "c": "critical"}

# (field, parser, required, generic header aliases). Legacy patient identity columns are shared by the person kinds.
_WHO = (("legacy_id", _text(64), False, ("patient id", "uhid", "mrn", "mr no", "legacy id", "registration no", "reg no",
                                          "op no", "file no")),
        ("patient_name", _text(150), False, ("patient name", "name", "patient")),
        ("phone", _phone, False, ("phone", "mobile", "mobile no", "contact", "contact no", "phone number")))
SPECS: dict[str, tuple] = {
    "doctors": (("name", _text(100, 2), True, ("doctor name", "name", "doctor", "consultant")),
                ("department", _text(50, 2), True, ("department", "dept", "speciality department")),
                ("specialization", _text(50, 2), False, ("specialization", "speciality", "specialty", "designation")),
                ("registration_number", _text(40, 3), False, ("registration number", "registration no", "reg no",
                                                              "mci no", "nmc no", "medical council no")),
                ("registration_council", _text(60, 2), False, ("council", "registration council", "state council")),
                ("phone", _phone, False, ("phone", "mobile", "contact")),
                ("consultation_fee", _int(0, 100000), False, ("fee", "consultation fee", "consult fee", "op fee")),
                ("available_days", _days, False, ("days", "available days", "op days", "working days")),
                ("morning_slots", _slots, False, ("morning slots", "morning", "am slots")),
                ("evening_slots", _slots, False, ("evening slots", "evening", "pm slots"))),
    "services": (("service_code", _code, True, ("code", "service code", "item code", "tariff code")),
                 ("name", _text(200), True, ("name", "service name", "service", "item name", "description")),
                 ("category", _choice(SERVICE_CATS), True, ("category", "type", "service type", "group")),
                 ("standard_price_paise", _paise, True, ("price", "rate", "amount", "charge", "tariff", "mrp")),
                 ("hsn_sac", _hsn, False, ("hsn", "sac", "hsn sac", "hsn/sac")),
                 ("tax_rate_bps", _gst, False, ("gst", "gst rate", "tax", "tax rate", "gst %")),
                 ("department", _text(100), False, ("department", "dept"))),
    "packages": (("code", _code, True, ("code", "package code")),
                 ("name", _text(200, 2), True, ("name", "package name", "package")),
                 ("price_paise", _positive_paise(100), True, ("price", "package price", "amount", "rate")),
                 ("included_categories", _cats, False, ("includes", "included", "inclusions categories",
                                                        "included categories")),
                 ("description", _text(1000), False, ("inclusions", "description", "details")),
                 ("max_days", _int(1, 365), False, ("days", "max days", "length of stay", "los"))),
    "payers": (("name", _text(150), True, ("name", "payer", "payer name", "tpa", "insurance company", "company")),
               ("payer_type", _choice(PAYER_TYPES), True, ("type", "payer type", "category")),
               ("tpa_code", _text(50), False, ("code", "tpa code", "payer code")),
               ("contact_email", _text(120), False, ("email", "contact email")),
               ("contact_phone", _text(30, 6), False, ("phone", "contact phone", "contact"))),
    "patients": (),        # client_data.parse_patient_csv (the existing, tested 088 parser)
    "history": (*_WHO,
                ("visit_date", _date, True, ("visit date", "date", "consultation date", "op date")),
                ("doctor_name", _text(120), False, ("doctor", "consultant", "doctor name")),
                ("diagnosis_text", _text(1000), False, ("diagnosis", "provisional diagnosis", "impression")),
                ("icd10_code", _icd, False, ("icd", "icd10", "icd 10", "icd code")),
                ("medications_text", _text(4000), False, ("medicines", "medications", "prescription", "drugs", "rx")),
                ("vitals_text", _text(1000), False, ("vitals", "bp", "vital signs")),
                ("notes", _text(4000), False, ("notes", "remarks", "advice"))),
    "lab_values": (*_WHO,
                   ("test_name", _text(120), True, ("test", "test name", "parameter", "investigation")),
                   ("value_text", _text(60), True, ("value", "result", "result value")),
                   ("unit", _text(30), False, ("unit", "units")),
                   ("result_date", _date, True, ("date", "result date", "report date", "sample date")),
                   ("flag", _choice(FLAGS), False, ("flag", "h/l", "abnormal flag", "status"))),
    "dues": (*_WHO,
             ("amount_paise", _positive_paise(1), True, ("amount", "due", "balance", "outstanding", "due amount")),
             ("as_of", _date, True, ("as of", "date", "due date", "bill date")),
             ("reference", _text(80), False, ("reference", "bill no", "invoice no", "ref"))),
    "appointments_future": (*_WHO,
                            ("doctor", _text(100, 2), True, ("doctor", "doctor name", "consultant")),
                            ("appointment_date", _date, True, ("date", "appointment date", "visit date")),
                            ("appointment_time", _time, True, ("time", "slot", "appointment time"))),
    "inpatients_cutover": (*_WHO,
                           ("ward", _text(60), False, ("ward", "ward name")),
                           ("bed", _text(30), False, ("bed", "bed no")),
                           ("admitted_on", _date, False, ("admitted on", "admission date", "doa")),
                           ("doctor", _text(100), False, ("doctor", "consultant", "treating doctor"))),
    "chronic_list": (*_WHO,
                     ("condition_text", _text(200, 2), True, ("condition", "diagnosis", "disease", "chronic condition")),
                     ("icd10_code", _icd, False, ("icd", "icd10", "icd code")),
                     ("last_visit_date", _date, False, ("last visit", "last visit date"))),
}
PERSON_KINDS = ("history", "lab_values", "dues", "appointments_future", "inpatients_cutover", "chronic_list")
REPORT_ONLY = ("inpatients_cutover",)

#: Preset column maps (a column-name map only, SPEC §3): the vendor's export header → our field. Shapes taken from
#: the vendors' standard Excel exports; validated against real samples on the sandbox before a go-live (MI-O2).
PRESET_HEADERS: dict[str, dict[str, dict[str, str]]] = {
    "mocdoc": {
        "doctors": {"doctor name": "name", "speciality": "specialization", "department name": "department",
                    "reg no": "registration_number", "consultation charges": "consultation_fee",
                    "mobile no": "phone", "op days": "available_days"},
        "services": {"service code": "service_code", "service name": "name", "service group": "category",
                     "rate": "standard_price_paise", "sac code": "hsn_sac", "gst %": "tax_rate_bps"},
        "patients": {"patient name": "Name", "mr no": "Patient ID", "mobile no": "Mobile", "dob": "DOB",
                     "gender": "Gender", "age": "Age", "address": "Address", "last visit date": "Last Visit"},
        "history": {"mr no": "legacy_id", "patient name": "patient_name", "mobile no": "phone",
                    "visit date": "visit_date", "consultant": "doctor_name", "diagnosis": "diagnosis_text",
                    "prescription": "medications_text", "vitals": "vitals_text"},
        "lab_values": {"mr no": "legacy_id", "test name": "test_name", "result": "value_text", "units": "unit",
                       "report date": "result_date", "flag": "flag"},
        "dues": {"mr no": "legacy_id", "balance amount": "amount_paise", "bill date": "as_of", "bill no": "reference"},
        "appointments_future": {"mr no": "legacy_id", "patient name": "patient_name", "mobile no": "phone",
                                "consultant": "doctor", "appointment date": "appointment_date",
                                "appointment time": "appointment_time"},
    },
    "practo_insta": {
        "doctors": {"doctor": "name", "speciality": "specialization", "dept": "department",
                    "registration number": "registration_number", "op consultation fee": "consultation_fee"},
        "services": {"item code": "service_code", "item name": "name", "item type": "category",
                     "unit rate": "standard_price_paise", "hsn/sac": "hsn_sac", "tax %": "tax_rate_bps"},
        "patients": {"patient name": "Name", "uhid": "Patient ID", "mobile": "Mobile", "date of birth": "DOB",
                     "sex": "Gender", "age": "Age"},
        "history": {"uhid": "legacy_id", "patient name": "patient_name", "mobile": "phone",
                    "visit date": "visit_date", "doctor": "doctor_name", "final diagnosis": "diagnosis_text",
                    "icd code": "icd10_code", "medicines": "medications_text"},
        "lab_values": {"uhid": "legacy_id", "test": "test_name", "result value": "value_text", "unit": "unit",
                       "sample date": "result_date", "abnormal flag": "flag"},
        "dues": {"uhid": "legacy_id", "due amount": "amount_paise", "as on date": "as_of", "invoice no": "reference"},
        "appointments_future": {"uhid": "legacy_id", "patient name": "patient_name", "mobile": "phone",
                                "doctor": "doctor", "date": "appointment_date", "time": "appointment_time"},
    },
}


def _norm(h) -> str:
    return re.sub(r"[^a-z0-9/%]+", " ", str(h or "").lower()).strip()


# ── reading the file (pure) ──────────────────────────────────────────────────

def read_grid(raw: bytes, filename: str) -> list[list[str]]:
    """CSV (UTF-8 / cp1252, , ; or tab) or XLSX (first sheet, values only — no formulas, no macros)."""
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than 5 MB. Split it and import the parts one by one.")
    if not raw.strip():
        raise HTTPException(status_code=422, detail="The file is empty.")
    if (filename or "").lower().endswith((".xlsx", ".xlsm")) or raw[:2] == b"PK":
        import openpyxl
        from app.services.price_list_parser import validate_xlsx_zip_safety
        validate_xlsx_zip_safety(raw)
        try:
            ws = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True).worksheets[0]
            grid = []
            for r in ws.iter_rows(values_only=True):
                grid.append(["" if c is None else (c.strftime("%Y-%m-%d") if isinstance(c, (datetime, date))
                                                   else c.strftime("%H:%M") if isinstance(c, time) else str(c))
                             for c in r])
                if len(grid) > MAX_ROWS + 1:
                    break
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=422, detail="The spreadsheet could not be read. Save it as .xlsx or .csv.")
    else:
        try:
            text = client_data.decode_csv(raw)
        except client_data.ImportFileError as e:
            raise HTTPException(status_code=422, detail=str(e))
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        grid = list(csv.reader(io.StringIO(text), dialect))
    grid = [r for r in grid if any(str(c).strip() for c in r)]
    if len(grid) < 2:
        raise HTTPException(status_code=422, detail="The file needs a header row and at least one data row.")
    if len(grid) - 1 > MAX_ROWS:
        raise HTTPException(status_code=413, detail=f"At most {MAX_ROWS:,} rows per file. Split it into smaller files.")
    return grid


def map_header(kind: str, preset: str, header: list) -> list[Optional[str]]:
    """Column index → field (None = ignored). The preset's names win; then the generic aliases; then the field name."""
    pmap = {_norm(k): v for k, v in PRESET_HEADERS.get(preset, {}).get(kind, {}).items()}
    alias = {}
    for f, _p, _req, names in SPECS[kind]:
        for n in (f, f.replace("_", " "), *names):
            alias.setdefault(_norm(n), f)
    out, seen = [], set()
    for h in header:
        f = pmap.get(_norm(h)) or alias.get(_norm(h))
        out.append(f if f and f not in seen else None)
        if f:
            seen.add(f)
    return out


def parse_rows(kind: str, preset: str, grid: list[list[str]]) -> dict:
    """Validate every row: {'rows': [{_row, ...fields}], 'errors': [{row, column, problem}], 'duplicates': n}."""
    if kind == "patients":
        return _parse_patients(preset, grid)
    cols = map_header(kind, preset, grid[0])
    spec = {f: (p, req) for f, p, req, _ in SPECS[kind]}
    missing = [f for f, (_, req) in spec.items() if req and f not in cols]
    if kind in PERSON_KINDS and not ({"legacy_id", "phone"} & set(cols)):
        missing.append("legacy_id or phone")
    if missing:
        raise HTTPException(status_code=422, detail={"message": "These columns are missing: " + ", ".join(missing)
                                                     + ". Download the template to see the expected columns.",
                                                     "missing": missing})
    rows, errors, seen, dup = [], [], set(), 0
    for n, cells in enumerate(grid[1:], start=2):
        row, bad = {"_row": n}, False
        for idx, f in enumerate(cols):
            if not f or idx >= len(cells):
                continue
            v = str(cells[idx]).strip()
            if not v:
                continue
            try:
                row[f] = spec[f][0](v)
            except RowError as e:
                bad = True
                if len(errors) < MAX_ERRORS:
                    errors.append({"row": n, "column": f, "problem": f"{f.replace('_', ' ').capitalize()} {e}"[:300]})
        for f, (_, req) in spec.items():
            if req and f not in row and not any(e["row"] == n and e["column"] == f for e in errors):
                bad = True
                if len(errors) < MAX_ERRORS:
                    errors.append({"row": n, "column": f, "problem": f"{f.replace('_', ' ').capitalize()} is required"})
        if kind in PERSON_KINDS and not bad and not (row.get("legacy_id") or (row.get("phone") and row.get("patient_name"))):
            bad = True
            if len(errors) < MAX_ERRORS:
                errors.append({"row": n, "column": "legacy_id", "problem": "Give the old patient ID, or name and phone"})
        if bad:
            continue
        key = _row_key(kind, row)
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        rows.append(row)
    return {"rows": rows, "errors": errors, "error_rows": len({e["row"] for e in errors}), "duplicates": dup}


def _parse_patients(preset: str, grid: list[list[str]]) -> dict:
    """The existing 088 parser (aliases, day-first dates, Indian phones, dedupe), after the preset renames headers."""
    pmap = {_norm(k): v for k, v in PRESET_HEADERS.get(preset, {}).get("patients", {}).items()}
    header = [pmap.get(_norm(h), h) for h in grid[0]]
    buf = io.StringIO()
    csv.writer(buf).writerows([header, *grid[1:]])
    try:
        parsed = client_data.parse_patient_csv(buf.getvalue().encode("utf-8"))
    except client_data.ImportFileError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"rows": parsed.rows, "errors": parsed.errors, "warnings": parsed.warnings,
            "error_rows": len({e["row"] for e in parsed.errors}), "duplicates": parsed.duplicates_in_file,
            "total": parsed.total_rows}


def _row_key(kind: str, row: dict) -> str:
    """Identity of a row inside its kind: re-importing the same visit / value / due never duplicates it."""
    who = (row.get("legacy_id") or "").lower() or f"{row.get('phone')}|{client_data.name_key(row.get('patient_name', ''))}"
    parts = {
        "doctors": (row.get("registration_number") or "").upper() or client_data.name_key(row.get("name", "")),
        "services": row.get("service_code"), "packages": row.get("code"),
        "payers": client_data.name_key(row.get("name", "")),
        "history": f"{who}|{row.get('visit_date')}|{row.get('diagnosis_text')}|{row.get('medications_text')}",
        "lab_values": f"{who}|{row.get('test_name', '').lower()}|{row.get('result_date')}|{row.get('value_text')}",
        "dues": f"{who}|{row.get('as_of')}|{row.get('reference')}|{row.get('amount_paise')}",
        "appointments_future": f"{who}|{row.get('appointment_date')}|{row.get('appointment_time')}",
        "inpatients_cutover": who, "chronic_list": f"{who}|{row.get('condition_text', '').lower()}",
    }[kind]
    return f"{kind}:" + hashlib.sha256(str(parts).encode("utf-8")).hexdigest()[:40]


def template_csv(kind: str) -> str:
    if kind == "patients":
        return client_data.IMPORT_TEMPLATE_CSV
    return client_data.csv_from_columns([(f, (lambda r, f=f: r.get(f))) for f, *_ in SPECS[kind]], [])


# ── persistence ──────────────────────────────────────────────────────────────

def _rows(res) -> list:
    return list(getattr(res, "data", None) or [])


async def _records_by_identity(clinic_id: str, rows: list[dict]) -> dict[str, dict]:
    """legacy id / phone+name → the imported patient_records row (088 dedupe_key, so it is the same identity)."""
    keys = set()
    for r in rows:
        if r.get("legacy_id"):
            keys.add("id:" + r["legacy_id"].strip().lower())
        if r.get("phone") and r.get("patient_name"):
            keys.add("pn:" + r["phone"] + "|" + client_data.name_key(r["patient_name"]))
    out: dict[str, dict] = {}
    keys = sorted(keys)
    for i in range(0, len(keys), 200):
        for rec in _rows(await sb(scoped_query("patient_records", clinic_id, "id, full_name, phone, external_id, dedupe_key")
                                  .in_("dedupe_key", keys[i:i + 200]))):
            out[rec["dedupe_key"]] = rec
    return out


def _who(row: dict, recs: dict) -> Optional[dict]:
    if row.get("legacy_id"):
        r = recs.get("id:" + row["legacy_id"].strip().lower())
        if r:
            return r
    if row.get("phone") and row.get("patient_name"):
        return recs.get("pn:" + row["phone"] + "|" + client_data.name_key(row["patient_name"]))
    return None


async def dry_run(clinic: dict, kind: str, preset: str, filename: str, raw: bytes, user) -> dict:
    if kind not in SPECS:
        raise HTTPException(status_code=404, detail="Unknown import kind.")
    if preset not in PRESETS:
        raise HTTPException(status_code=422, detail="Unknown preset.")
    cid = clinic["id"]
    grid = read_grid(raw, filename)
    parsed = parse_rows(kind, preset, grid)
    rows, errors = parsed["rows"], list(parsed["errors"])
    info: dict[str, Any] = {}
    if kind in PERSON_KINDS:
        recs = await _records_by_identity(cid, rows)
        kept = []
        for r in rows:
            rec = _who(r, recs)
            if rec is None and kind != "appointments_future":
                if len(errors) < MAX_ERRORS:
                    errors.append({"row": r["_row"], "column": "legacy_id",
                                   "problem": "No imported patient with this ID / name and phone — import the patient list first"})
                continue
            if rec:
                r["patient_record_id"] = rec["id"]
            kept.append(r)
        parsed["error_rows"] += len(rows) - len(kept)
        rows = kept
    if kind == "history":
        mapped = await _diagnosis_map(cid)
        for r in rows:
            if not r.get("icd10_code") and r.get("diagnosis_text"):
                code = mapped.get(_diag_norm(r["diagnosis_text"]))
                if code:
                    r["icd10_code"] = code
    if kind in ("doctors", "services", "packages", "payers"):
        info["existing"] = len(await _existing(cid, kind, rows))
    sha = hashlib.sha256(raw).hexdigest()
    applied = _rows(await sb(scoped_query("mi_batches", cid, "id").eq("kind", kind).eq("sha256", sha)
                             .eq("status", "applied").limit(1)))
    total = parsed.get("total") or (len(grid) - 1)
    # unscoped: insert_scoped_by_payload
    batch = _rows(await sb(supabase.table("mi_batches").insert({
        "clinic_id": cid, "kind": kind, "preset": preset, "file_name": (filename or "")[:200] or None, "sha256": sha,
        "rows_total": total, "rows_ok": len(rows), "rows_error": parsed["error_rows"],
        "rows_duplicate": parsed["duplicates"], "payload": rows,
        "result": {**info, "warnings": parsed.get("warnings", [])[:MAX_ERRORS]},
        "created_by": _actor(user), "created_by_name": (getattr(user, "username", "") or "")[:100]})))[0]
    if errors:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("mi_row_errors").insert([
            {"clinic_id": cid, "batch_id": batch["id"], "row_no": e["row"], "column_name": str(e["column"])[:60],
             "problem": str(e["problem"])[:300]} for e in errors[:MAX_ERRORS]]))
    out = {k: batch[k] for k in ("id", "kind", "preset", "rows_total", "rows_ok", "rows_error", "rows_duplicate",
                                 "status")}
    return {**out, **info, "errors": errors[:MAX_ERRORS], "warnings": parsed.get("warnings", [])[:MAX_ERRORS],
            "already_applied": bool(applied), "report": rows if kind in REPORT_ONLY else None}


def _actor(user) -> Optional[str]:
    import uuid
    try:
        return str(uuid.UUID(str(getattr(user, "user_id", None))))
    except (ValueError, TypeError):
        return None


async def get_batch(clinic_id: str, batch_id: str) -> dict:
    rows = _rows(await sb(scoped_query("mi_batches", clinic_id).eq("id", batch_id).limit(1)))
    if not rows:
        raise HTTPException(status_code=404, detail="Import batch not found.")
    return rows[0]


async def batch_errors(clinic_id: str, batch_id: str) -> list:
    return _rows(await sb(scoped_query("mi_row_errors", clinic_id, "row_no, column_name, problem, severity")
                          .eq("batch_id", batch_id).order("row_no").limit(MAX_ERRORS)))


async def list_batches(clinic_id: str, kind: Optional[str] = None) -> list:
    q = scoped_query("mi_batches", clinic_id, "id, kind, preset, file_name, rows_total, rows_ok, rows_error, "
                                              "rows_duplicate, status, result, created_by_name, created_at, applied_at, undone_at")
    if kind:
        q = q.eq("kind", kind)
    return _rows(await sb(q.order("created_at", desc=True).limit(200)))


async def progress(clinic_id: str) -> list:
    """The wizard stepper (and the owner console's read-only copy): per step, the last applied batch's counts."""
    batches = await fetch_all(lambda: scoped_query("mi_batches", clinic_id, "kind, status, rows_ok, rows_error, applied_at, result")
                              .eq("status", "applied").order("applied_at", desc=True), label="mi:progress")
    out = []
    for k in STEPS:
        mine = [b for b in batches if b["kind"] == k]
        out.append({"kind": k, "label": LABELS[k], "state": "done" if mine else "todo", "batches": len(mine),
                    "rows": sum((b.get("result") or {}).get("written", b["rows_ok"]) for b in mine),
                    "last_applied_at": mine[0]["applied_at"] if mine else None})
    return out


# ── apply ────────────────────────────────────────────────────────────────────

async def apply(clinic: dict, batch_id: str, user, send_confirmations: bool = False) -> dict:
    cid = clinic["id"]
    b = await get_batch(cid, batch_id)
    if b["kind"] in REPORT_ONLY:
        raise HTTPException(status_code=422, detail="The inpatient cutover list is a report: admit each patient in "
                                                    "IPD on day 1 (admission is a clinical act done by staff).")
    if b["status"] != "dry_run":
        raise HTTPException(status_code=409, detail="This import was already applied or undone. Run a new dry run.")
    other = _rows(await sb(scoped_query("mi_batches", cid, "id").eq("kind", b["kind"]).eq("sha256", b["sha256"])
                           .eq("status", "applied").limit(1)))
    if other:
        return {"already_applied": True, "batch_id": other[0]["id"], "written": 0}
    if b["kind"] == "chronic_list":
        from app.services.tenant import care_access
        if not care_access(clinic):
            raise HTTPException(status_code=409, detail="Chronic-patient lists go to Continuous Care: switch it on "
                                                        "for this clinic first. Nothing was imported.")
    # claim the batch first: the unique index on applied (kind, sha256) makes a parallel apply of the same file lose
    try:
        claimed = _rows(await sb(supabase.table("mi_batches").update({
            "status": "applied", "applied_at": datetime.now().astimezone().isoformat(),
            "applied_by_name": (getattr(user, "username", "") or "")[:100]})
            .eq("clinic_id", cid).eq("id", batch_id).eq("status", "dry_run")))
    except Exception:
        raise HTTPException(status_code=409, detail="The same file is being applied in another window.")
    if not claimed:
        raise HTTPException(status_code=409, detail="This import changed meanwhile. Reload and try again.")
    rows = b["payload"] or []
    try:
        result = await APPLIERS[b["kind"]](clinic, b, rows, user, send_confirmations)
    except Exception:
        # nothing half-written stays live: undo whatever landed, then put the batch back to dry run
        logger.error(f"MI apply failed clinic={cid} batch={batch_id} kind={b['kind']}")
        try:
            await sb(supabase.rpc("mi_undo", {"p_clinic_id": cid, "p_batch_id": batch_id}))
        except Exception:
            logger.error(f"MI_APPLY_ROLLBACK_FAILED clinic={cid} batch={batch_id}")
        raise HTTPException(status_code=500, detail="The import failed and was rolled back. Nothing was changed; "
                                                    "please retry or contact support.")
    await sb(supabase.table("mi_batches").update({"result": {**(b.get("result") or {}), **result}})
             .eq("clinic_id", cid).eq("id", batch_id))
    return {"already_applied": False, "batch_id": batch_id, **result}


async def _item(clinic_id: str, batch_id: str, table: str, target_id: str, previous: Optional[dict] = None):
    # unscoped: insert_scoped_by_payload
    await sb(supabase.table("mi_batch_items").insert({
        "clinic_id": clinic_id, "batch_id": batch_id, "target_table": table, "target_id": target_id,
        "action": "updated" if previous is not None else "created", "previous": previous}))


async def _existing(clinic_id: str, kind: str, rows: list) -> dict[str, dict]:
    """Rows already in the master: key → existing row."""
    if kind == "doctors":
        docs = await fetch_all(lambda: scoped_query("doctors", clinic_id, "id, name, registration_number"),
                               label="mi:doctors")
        by = {}
        for d in docs:
            by["n:" + client_data.name_key(d["name"])] = d
            if d.get("registration_number"):
                by["r:" + d["registration_number"].upper()] = d
        return {_row_key(kind, r): by[k] for r in rows for k in (_doc_key(r, by),) if k}
    table, col, key = {"services": ("billable_services", "service_code", "service_code"),
                       "packages": ("ipd_packages", "code", "code"),
                       "payers": ("insurance_payers", "name", "name")}[kind]
    found = await fetch_all(lambda: scoped_query(table, clinic_id, "*"), label=f"mi:{table}")
    norm = (lambda v: client_data.name_key(v)) if kind == "payers" else (lambda v: v)
    by = {norm(f[col]): f for f in found}
    return {_row_key(kind, r): by[norm(r[key])] for r in rows if norm(r[key]) in by}


def _doc_key(row: dict, by: dict) -> Optional[str]:
    if row.get("registration_number") and "r:" + row["registration_number"].upper() in by:
        return "r:" + row["registration_number"].upper()
    k = "n:" + client_data.name_key(row["name"])
    return k if k in by else None


async def _apply_doctors(clinic, b, rows, user, _send) -> dict:
    cid = clinic["id"]
    have = await _existing(cid, "doctors", rows)
    written = skipped = 0
    for r in rows:
        if _row_key("doctors", r) in have:
            skipped += 1                                   # duplicates by registration number / name: never twice
            continue
        rec = {k: r[k] for k in ("name", "department", "registration_number", "registration_council",
                                 "consultation_fee", "available_days", "morning_slots", "evening_slots") if k in r}
        rec["specialization"] = r.get("specialization") or r["department"]
        if r.get("phone"):
            rec["whatsapp_phone"] = r["phone"]
        # unscoped: insert_scoped_by_payload
        d = _rows(await sb(supabase.table("doctors").insert({**rec, "clinic_id": cid, "is_active": True})))[0]
        await _item(cid, b["id"], "doctors", d["id"])
        written += 1
    return {"written": written, "skipped_existing": skipped}


async def _apply_master(clinic, b, rows, user, _send) -> dict:
    kind = b["kind"]
    cid = clinic["id"]
    table = {"services": "billable_services", "packages": "ipd_packages", "payers": "insurance_payers"}[kind]
    have = await _existing(cid, kind, rows)
    written = updated = skipped = 0
    for r in rows:
        rec = {k: v for k, v in r.items() if not k.startswith("_")}
        if kind == "packages":
            rec.setdefault("included_categories", ["bed", "nursing", "doctor_visit"])
        cur = have.get(_row_key(kind, r))
        if cur is None:
            # unscoped: insert_scoped_by_payload
            row = _rows(await sb(supabase.table(table).insert({**rec, "clinic_id": cid})))[0]
            await _item(cid, b["id"], table, row["id"])
            written += 1
        elif kind == "services":
            # price / name changes update the tariff in place (masters are not versioned) with the old values kept
            # for undo and the audit trail
            changes = {k: v for k, v in rec.items() if k != "service_code" and cur.get(k) != v}
            if not changes:
                skipped += 1
                continue
            await _item(cid, b["id"], table, cur["id"], {k: cur.get(k) for k in changes})
            await sb(supabase.table(table).update(changes).eq("clinic_id", cid).eq("id", cur["id"]))
            updated += 1
        else:
            skipped += 1                                   # packages are never re-priced; payers merge by name
    return {"written": written, "updated": updated, "skipped_existing": skipped}


async def _apply_patients(clinic, b, rows, user, _send) -> dict:
    cid = clinic["id"]
    existing = await client_data.fetch_existing_keys(cid)
    new = [r for r in rows if r["dedupe_key"] not in existing]
    quota = await client_data.clinic_quota(cid)
    if len(new) > quota["remaining"]:
        raise HTTPException(status_code=413, detail=f"This list has {len(new):,} new patients but the clinic's "
                                                    f"imported-records storage has {quota['remaining']:,} left.")

    class _P:                                              # the 088 importer takes its ParsedImport shape
        total_rows = b["rows_total"]
        duplicates_in_file = b["rows_duplicate"]
        warnings = (b.get("result") or {}).get("warnings", [])
    batch = await client_data.import_records(cid, getattr(user, "username", "import") or "import", b.get("file_name") or "",
                                             _P(), new, len(rows) - len(new))
    await _item(cid, b["id"], "patient_import_batches", batch["id"])
    return {"written": batch.get("inserted_count", 0), "skipped_existing": len(rows) - len(new),
            "patient_import_batch_id": batch["id"]}


def _source(b: dict) -> str:
    return {"mocdoc": "MocDoc", "practo_insta": "Practo Insta"}.get(b["preset"], "Imported file")


async def _insert_legacy(table: str, cid: str, b: dict, recs: list[dict]) -> int:
    written = 0
    for i in range(0, len(recs), 500):
        # unscoped: insert_scoped_by_payload (idempotent on row_key across batches)
        res = await sb(supabase.table(table).upsert(recs[i:i + 500], on_conflict="clinic_id,row_key",
                                                    ignore_duplicates=True))
        written += len(_rows(res))
    return written


async def _apply_person(clinic, b, rows, user, _send) -> dict:
    cid, kind = clinic["id"], b["kind"]
    fields = {"history": ("visit_date", "doctor_name", "diagnosis_text", "icd10_code", "medications_text",
                          "vitals_text", "notes"),
              "lab_values": ("test_name", "value_text", "unit", "result_date", "flag"),
              "dues": ("amount_paise", "as_of", "reference"),
              "chronic_list": ("condition_text", "icd10_code", "last_visit_date")}[kind]
    table = {"history": "mi_legacy_encounters", "lab_values": "mi_legacy_lab_values", "dues": "mi_opening_balances",
             "chronic_list": "mi_chronic_candidates"}[kind]
    recs = []
    for r in rows:
        rec = {"clinic_id": cid, "batch_id": b["id"], "patient_record_id": r["patient_record_id"],
               "row_key": _row_key(kind, r), **{f: r.get(f) for f in fields}}
        if kind != "dues":
            rec["source_system"] = _source(b)
        recs.append(rec)
    written = await _insert_legacy(table, cid, b, recs)
    return {"written": written, "skipped_existing": len(recs) - written}


async def _apply_appointments(clinic, b, rows, user, send_confirmations) -> dict:
    """Each row: the slot must be offered by get_available_slots (holiday, leave, session, already booked) and then
    book_appointment's conflict guard must accept it. Refusals are reported per row, never forced."""
    from app.database import book_appointment, doctor_session_slots, get_available_slots
    cid = clinic["id"]
    today = today_ist().isoformat()
    docs = await fetch_all(lambda: scoped_query("doctors", cid, "*"), label="mi:docs")
    by = {}
    for d in docs:
        by[client_data.name_key(d["name"])] = d
        if d.get("registration_number"):
            by[d["registration_number"].strip().lower()] = d
    links = {}
    rec_ids = [r["patient_record_id"] for r in rows if r.get("patient_record_id")]
    for i in range(0, len(rec_ids), 200):
        for ln in _rows(await sb(scoped_query("mi_patient_links", cid, "patient_record_id, patient_id")
                                 .in_("patient_record_id", rec_ids[i:i + 200]))):
            links[ln["patient_record_id"]] = ln["patient_id"]
    booked, refused = 0, []
    reasons = {"hospital_closed": "the hospital is closed that day", "doctor_on_leave": "the doctor is on leave",
               "doctor_off_day": "the doctor does not sit that day", "doctor_not_found": "doctor not found",
               "slot_taken": "the slot is already booked", "doctor_unavailable": "doctor not found",
               "not_offered": "that time is not one of the doctor's free slots", "past": "the date is in the past",
               "no_phone": "a phone number is needed to book"}
    for r in rows:
        doc = by.get(client_data.name_key(r["doctor"])) or by.get(r["doctor"].strip().lower())
        why = None
        if not doc or doc.get("is_active") is False:
            why = "doctor_not_found"
        elif r["appointment_date"] < today:
            why = "past"
        elif not r.get("phone"):
            why = "no_phone"
        else:
            slots, err = await get_available_slots(cid, doc["name"], r["appointment_date"])
            if err:
                why = err if err in reasons else "not_offered"
            elif r["appointment_time"] not in slots:
                sessions = doctor_session_slots(doc, "morning") + doctor_session_slots(doc, "evening")
                why = "slot_taken" if r["appointment_time"] in sessions else "not_offered"
        if why is None:
            res = await book_appointment(cid, {
                "patient_id": links.get(r.get("patient_record_id")), "patient_phone": r["phone"],
                "patient_name": r.get("patient_name") or "Patient", "doctor_id": doc["id"], "doctor_name": doc["name"],
                "department": doc.get("department"), "appointment_date": r["appointment_date"],
                "appointment_time": r["appointment_time"], "status": "confirmed", "booking_type": "consultation",
                "booking_channel": "front_desk", "is_walk_in": False,
                "notes": f"Imported from {_source(b)}" + ("" if send_confirmations else " (no confirmation sent)")})
            if not res.get("success"):
                why = res.get("reason") or "slot_taken"
            else:
                await _item(cid, b["id"], "appointments", res["appointment"]["id"])
                booked += 1
        if why:
            refused.append({"row": r["_row"], "column": "appointment_time",
                            "problem": f"Not booked: {reasons.get(why, why)}"})
    if refused:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("mi_row_errors").insert([
            {"clinic_id": cid, "batch_id": b["id"], "row_no": e["row"], "column_name": e["column"], "problem": e["problem"]}
            for e in refused[:MAX_ERRORS]]))
    # shortcut: imported bookings never send WhatsApp confirmations (SPEC §3 default); send_confirmations is recorded
    # for the audit only — upgrade when an owner asks for imported confirmations, through the normal template path.
    return {"written": booked, "refused": len(refused), "refusals": refused[:MAX_ERRORS],
            "confirmations_sent": 0}


APPLIERS: dict[str, Callable] = {
    "doctors": _apply_doctors, "services": _apply_master, "packages": _apply_master, "payers": _apply_master,
    "patients": _apply_patients, "history": _apply_person, "lab_values": _apply_person, "dues": _apply_person,
    "chronic_list": _apply_person, "appointments_future": _apply_appointments,
}


async def undo(clinic_id: str, batch_id: str) -> dict:
    try:
        res = await sb(supabase.rpc("mi_undo", {"p_clinic_id": clinic_id, "p_batch_id": batch_id}))
    except Exception as e:
        msg = str(getattr(e, "message", None) or e)
        if "mi_not_found" in msg:
            raise HTTPException(status_code=404, detail="Import batch not found.")
        if "mi_not_applied" in msg:
            raise HTTPException(status_code=409, detail="Only an applied import can be undone.")
        if "mi_batch_referenced" in msg:
            raise HTTPException(status_code=409, detail="This import is already in use (a booking, bill, collected "
                                                        "due or linked patient refers to it), so it cannot be undone.")
        logger.error(f"MI undo failed clinic={clinic_id} batch={batch_id}: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="The undo failed. Nothing was changed; please retry.")
    return res.data or {}


# ── legacy patients at the front desk ────────────────────────────────────────

def _diag_norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())[:200]


async def _diagnosis_map(clinic_id: str) -> dict[str, str]:
    return {m["legacy_text_norm"]: m["icd10_code"] for m in await fetch_all(
        lambda: scoped_query("mi_diagnosis_map", clinic_id, "legacy_text_norm, icd10_code"), label="mi:dxmap")}


async def legacy_search(clinic_id: str, q: str) -> list:
    """Front-desk search over the imported list: name, phone or legacy id. Each hit says whether it is linked."""
    q = q.strip()
    if len(q) < 3:
        raise HTTPException(status_code=422, detail="Type at least 3 characters.")
    cols = "id, full_name, phone, external_id, gender, date_of_birth, age_years, last_visit_date"
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 5 and len(digits) >= len(q.replace(" ", "")) - 1:
        hits = _rows(await sb(scoped_query("patient_records", clinic_id, cols).like("phone", f"%{digits[-10:]}%").limit(20)))
    else:
        safe = re.sub(r"[%_,()]", " ", q)
        hits = _rows(await sb(scoped_query("patient_records", clinic_id, cols).ilike("full_name", f"%{safe}%").limit(20)))
        hits += _rows(await sb(scoped_query("patient_records", clinic_id, cols).eq("external_id", q).limit(5)))
    hits = list({h["id"]: h for h in hits}.values())
    links = {}
    if hits:
        links = {ln["patient_record_id"]: ln["patient_id"] for ln in _rows(await sb(
            scoped_query("mi_patient_links", clinic_id, "patient_record_id, patient_id")
            .in_("patient_record_id", [h["id"] for h in hits])))}
    return [{**h, "linked_patient_id": links.get(h["id"]), "source": "Imported record"} for h in hits]


async def _record(clinic_id: str, record_id: str) -> dict:
    rows = _rows(await sb(scoped_query("patient_records", clinic_id, "id, full_name, phone, external_id")
                          .eq("id", record_id).limit(1)))
    if not rows:
        raise HTTPException(status_code=404, detail="Imported record not found.")
    return rows[0]


async def _patient(clinic_id: str, patient_id: str) -> dict:
    rows = _rows(await sb(scoped_query("patients", clinic_id, "id, name, phone").eq("id", patient_id).limit(1)))
    if not rows:
        raise HTTPException(status_code=404, detail="Patient not found.")
    return rows[0]


async def link(clinic_id: str, record_id: str, patient_id: str, user, source: str = "front_desk") -> dict:
    """'Use this record': a staff decision, never automatic. Consent is not touched — the live patient keeps what
    they themselves agreed to."""
    rec = await _record(clinic_id, record_id)
    await _patient(clinic_id, patient_id)
    try:
        # unscoped: insert_scoped_by_payload
        row = _rows(await sb(supabase.table("mi_patient_links").insert({
            "clinic_id": clinic_id, "patient_record_id": record_id, "patient_id": patient_id,
            "legacy_mrn": rec.get("external_id"), "source": source, "linked_by": _actor(user),
            "linked_by_name": (getattr(user, "username", "") or "")[:100]})))[0]
    except Exception as e:
        if "mi_patient_links_record_key" in str(e) or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail="This imported record is already linked to a patient.")
        raise
    # any other pending phone match for this record is now settled
    await sb(supabase.table("mi_match_queue").update({"status": "rejected", "decided_at": datetime.now().astimezone()
                                                      .isoformat(), "decided_by_name": "linked elsewhere"})
             .eq("clinic_id", clinic_id).eq("patient_record_id", record_id).eq("status", "pending"))
    return row


async def refresh_matches(clinic_id: str) -> int:
    return (await sb(supabase.rpc("mi_refresh_matches", {"p_clinic_id": clinic_id}))).data or 0


async def list_matches(clinic_id: str, status: str = "pending") -> list:
    rows = _rows(await sb(scoped_query("mi_match_queue", clinic_id).eq("status", status)
                          .order("created_at", desc=True).limit(200)))
    if not rows:
        return []
    recs = {r["id"]: r for r in _rows(await sb(scoped_query(
        "patient_records", clinic_id, "id, full_name, phone, external_id, date_of_birth, age_years, last_visit_date")
        .in_("id", list({r["patient_record_id"] for r in rows}))))}
    pats = {p["id"]: p for p in _rows(await sb(scoped_query("patients", clinic_id, "id, name, phone, mrn")
                                               .in_("id", list({r["patient_id"] for r in rows}))))}
    from app.utils.validators import mask_phone
    out = []
    for r in rows:
        rec, p = recs.get(r["patient_record_id"]) or {}, pats.get(r["patient_id"]) or {}
        out.append({**r, "record": {**rec, "phone": mask_phone(rec.get("phone") or "")},
                    "patient": {**p, "phone": mask_phone(p.get("phone") or "")}})
    return out


async def decide_match(clinic_id: str, match_id: str, confirm: bool, user) -> dict:
    rows = _rows(await sb(scoped_query("mi_match_queue", clinic_id).eq("id", match_id).limit(1)))
    if not rows:
        raise HTTPException(status_code=404, detail="Match not found.")
    m = rows[0]
    if m["status"] != "pending":
        raise HTTPException(status_code=409, detail="This match was already decided.")
    done = _rows(await sb(supabase.table("mi_match_queue").update({
        "status": "confirmed" if confirm else "rejected", "decided_at": datetime.now().astimezone().isoformat(),
        "decided_by": _actor(user), "decided_by_name": (getattr(user, "username", "") or "")[:100]})
        .eq("clinic_id", clinic_id).eq("id", match_id).eq("status", "pending")))
    if not done:
        raise HTTPException(status_code=409, detail="This match was already decided.")
    if confirm:
        await link(clinic_id, m["patient_record_id"], m["patient_id"], user, source="match_queue")
    return done[0]


async def patient_legacy(clinic_id: str, patient_id: str) -> dict:
    """Read-only cards for the patient profile, always labelled 'Imported record (from <system>, <date>)'."""
    await _patient(clinic_id, patient_id)
    recs = [ln["patient_record_id"] for ln in _rows(await sb(scoped_query("mi_patient_links", clinic_id,
                                                                          "patient_record_id, legacy_mrn")
                                                             .eq("patient_id", patient_id)))]
    if not recs:
        return {"linked": False, "encounters": [], "lab_values": [], "dues": []}
    enc = _rows(await sb(scoped_query("mi_legacy_encounters", clinic_id).in_("patient_record_id", recs)
                         .order("visit_date", desc=True).limit(200)))
    labs = _rows(await sb(scoped_query("mi_legacy_lab_values", clinic_id).in_("patient_record_id", recs)
                          .order("result_date", desc=True).limit(500)))
    dues = _rows(await sb(scoped_query("mi_opening_balances", clinic_id).in_("patient_record_id", recs)
                          .eq("status", "open")))
    for row in enc + labs:
        row["label"] = f"Imported record (from {row['source_system']}, {row.get('visit_date') or row.get('result_date')})"
        row["legacy"] = True
    return {"linked": True, "encounters": enc, "lab_values": labs, "dues": dues,
            "open_dues_paise": sum(d["amount_paise"] for d in dues)}


async def close_due(clinic_id: str, due_id: str, status: str, invoice_id: Optional[str], user) -> dict:
    rows = _rows(await sb(scoped_query("mi_opening_balances", clinic_id).eq("id", due_id).limit(1)))
    if not rows:
        raise HTTPException(status_code=404, detail="Opening balance not found.")
    if rows[0]["status"] != "open":
        raise HTTPException(status_code=409, detail="This balance is already closed.")
    if status == "collected":
        if not invoice_id:
            raise HTTPException(status_code=422, detail="Collect an opening balance through an OPD bill and give its id.")
        inv = _rows(await sb(scoped_query("opd_invoices", clinic_id, "id, status").eq("id", invoice_id).limit(1)))
        if not inv or inv[0]["status"] in ("draft", "void"):
            raise HTTPException(status_code=422, detail="That bill is not an issued OPD bill of this clinic.")
    done = _rows(await sb(supabase.table("mi_opening_balances").update({
        "status": status, "collected_invoice_id": invoice_id if status == "collected" else None,
        "closed_at": datetime.now().astimezone().isoformat(),
        "closed_by_name": (getattr(user, "username", "") or "")[:100]})
        .eq("clinic_id", clinic_id).eq("id", due_id).eq("status", "open")))
    if not done:
        raise HTTPException(status_code=409, detail="This balance changed meanwhile. Reload and try again.")
    return done[0]


async def set_diagnosis_map(clinic_id: str, legacy_text: str, icd10_code: str, user) -> dict:
    norm = _diag_norm(legacy_text)
    if len(norm) < 2:
        raise HTTPException(status_code=422, detail="Give the legacy diagnosis text.")
    try:
        code = _icd(icd10_code)
    except RowError as e:
        raise HTTPException(status_code=422, detail=f"ICD-10 code {e}")
    # unscoped: insert_scoped_by_payload
    row = _rows(await sb(supabase.table("mi_diagnosis_map").upsert({
        "clinic_id": clinic_id, "legacy_text_norm": norm, "icd10_code": code,
        "updated_by_name": (getattr(user, "username", "") or "")[:100],
        "updated_at": datetime.now().astimezone().isoformat()}, on_conflict="clinic_id,legacy_text_norm")))
    # deterministic back-fill: imported visits with exactly this text and no code get the mapped code
    for e in await fetch_all(lambda: scoped_query("mi_legacy_encounters", clinic_id, "id, diagnosis_text")
                             .is_("icd10_code", "null").not_.is_("diagnosis_text", "null"), label="mi:dxfill"):
        if _diag_norm(e["diagnosis_text"]) == norm:
            await sb(supabase.table("mi_legacy_encounters").update({"icd10_code": code})
                     .eq("clinic_id", clinic_id).eq("id", e["id"]))
    return row[0] if row else {"legacy_text_norm": norm, "icd10_code": code}


async def list_diagnosis_map(clinic_id: str) -> list:
    return _rows(await sb(scoped_query("mi_diagnosis_map", clinic_id).order("legacy_text_norm").limit(1000)))


async def chronic_candidates(clinic_id: str, status: Optional[str] = "new", limit: int = 500) -> list:
    """The CC radar's import source (reason 'candidate', source 'import'): never an enrolment."""
    q = scoped_query("mi_chronic_candidates", clinic_id)
    if status:
        q = q.eq("status", status)
    return _rows(await sb(q.order("created_at").limit(limit)))


def errors_csv(errors: list) -> str:
    """Error rows for the admin to fix in their sheet; every cell through the formula-injection guard."""
    return client_data.csv_from_columns([("Row", lambda r: r.get("row_no")), ("Column", lambda r: r.get("column_name")),
                                         ("Problem", lambda r: r.get("problem"))], errors)
