"""Client data portability: legacy patient import, CSV export, record quota.

Why imported patients live in `patient_records` and NOT in `patients`
-------------------------------------------------------------------
A `patients` row is live WhatsApp state, not a directory entry:

  * consent.accepts_engagement() suppresses follow-ups when opted_in is False;
  * the STOP/START keyword handling keys on the same flag;
  * patient_match (lab-report routing) auto-delivers PDFs to a phone whose
    `patients` row name-matches the report.

A list exported from a clinic's old software carries no WhatsApp opt-in, so
writing it into `patients` would either fabricate consent or mute reminders
for those numbers, and would change report routing for diagnostic clients.
`patient_records` is read by nothing except the Data & Support admin page and
its export — that is the whole isolation guarantee, keep it that way.

Called from: app/routers/admin.py (/admin/data/*), app/routers/platform.py
(/platform/data-storage), app/services/data_retention.py (DPDP erasure).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from app.database import sb, supabase
from app.tenancy import is_valid_clinic_scope
from app.utils.validators import normalize_phone, validate_phone

logger = logging.getLogger(__name__)

# ── Quota ───────────────────────────────────────────────────────────────────
#: Records a clinic may import before the owner raises its limit. ~1 KB/row,
#: so 5,000 rows is ~5 MB of the Supabase plan — generous for a first import,
#: small enough that the owner stays in charge of anything bigger.
DEFAULT_PATIENT_RECORDS_LIMIT = 5_000
MAX_PATIENT_RECORDS_LIMIT = 1_000_000
#: The admin panel shows the "storage filling up" banner at this percentage.
STORAGE_WARN_PERCENT = 90

# ── Import limits (same envelope as the lab-test CSV import) ────────────────
IMPORT_MAX_FILE_BYTES = 5 * 1024 * 1024
IMPORT_MAX_ROWS = 25_000
MAX_ISSUES_REPORTED = 100
MAX_EXTRA_COLUMNS = 30
INSERT_CHUNK = 500

# ── Export ──────────────────────────────────────────────────────────────────
EXPORT_MAX_ROWS = 50_000
EXPORT_MAX_RANGE_DAYS = 366
EXPORT_PAGE = 1000  # PostgREST's default max-rows

SUPPORT_CATEGORIES = ("concern", "feature_request", "storage_request", "billing", "other")
SUPPORT_STATUSES = ("open", "in_progress", "resolved")
SUPPORT_DAILY_LIMIT = 20


class ImportFileError(ValueError):
    """The file as a whole is unusable (size, encoding, header). Nothing written."""


# ═══════════════════════════════════════════════════════════════════════════
# Quota
# ═══════════════════════════════════════════════════════════════════════════


def records_limit(clinic_config: Optional[dict]) -> int:
    """The clinic's imported-record quota from clinics.config (owner-set)."""
    raw = (clinic_config or {}).get("patient_records_limit")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return DEFAULT_PATIENT_RECORDS_LIMIT
    return raw


def storage_addon_paise(clinic_config: Optional[dict]) -> int:
    """Owner-set monthly add-on charge for extra storage. Owner-only value."""
    raw = (clinic_config or {}).get("data_storage_addon_paise")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def quota_state(used: int, limit: int) -> dict:
    """Traffic-light state for the imported-record quota.

    level: 'ok' | 'warning' (>= STORAGE_WARN_PERCENT) | 'full' (>= limit).
    A limit of 0 means importing is switched off for this clinic -> 'full'.
    """
    used = max(0, int(used or 0))
    limit = max(0, int(limit or 0))
    percent = 100 if limit == 0 else min(100, int(used * 100 // limit))
    if used >= limit:
        level = "full"
    elif percent >= STORAGE_WARN_PERCENT:
        level = "warning"
    else:
        level = "ok"
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "percent": percent,
        "level": level,
        "warn_percent": STORAGE_WARN_PERCENT,
    }


# ═══════════════════════════════════════════════════════════════════════════
# CSV import parsing (pure — no I/O, unit-tested directly)
# ═══════════════════════════════════════════════════════════════════════════


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


_HEADER_ALIASES = {
    "full_name": ("name", "full name", "patient name", "patientname", "patient",
                  "patient full name"),
    "phone": ("phone", "mobile", "mobile number", "mobile no", "phone number",
              "phone no", "contact", "contact number", "contact no", "whatsapp",
              "whatsapp number", "cell", "mobile phone"),
    "external_id": ("patient id", "patientid", "id", "uhid", "mr no", "mrn",
                    "mr number", "file no", "file number", "registration no",
                    "reg no", "registration number", "op no", "case no", "case number"),
    "gender": ("gender", "sex"),
    "date_of_birth": ("dob", "date of birth", "birth date", "birthdate"),
    "age_years": ("age", "age years", "age in years"),
    "email": ("email", "email id", "email address", "e mail"),
    "address": ("address", "full address", "residence", "location", "city"),
    "last_visit_date": ("last visit", "last visit date", "last visited",
                        "last consultation", "last appointment"),
    "notes": ("notes", "remarks", "medical history", "history", "treatment",
              "treatment notes", "comments", "diagnosis", "chief complaint"),
}
_ALIAS_TO_FIELD = {_norm_header(a): f for f, aliases in _HEADER_ALIASES.items() for a in aliases}

# Day-first: every clinic on the platform is in India, where 03/04/2024 is 3 April.
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d",
                 "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%d/%m/%y", "%d-%m-%y")
_OPTIONAL_LIMITS = {"gender": 20, "email": 254, "address": 500, "notes": 4000, "external_id": 64}
#: Every patient_records column the importer writes (besides tenant/batch/author).
_RECORD_FIELDS = ("full_name", "phone", "external_id", "gender", "date_of_birth", "age_years",
                  "email", "address", "last_visit_date", "notes", "extra", "dedupe_key")


def parse_date(value: str) -> date:
    """Day-first date from an old-software export. Raises ValueError."""
    v = (value or "").strip()
    candidates = [v]
    # Excel exports dates as "2023-05-01 00:00:00" or ISO with a T.
    head = re.split(r"[T ]", v, maxsplit=1)[0]
    if head != v:
        candidates.append(head)
    for c in candidates:
        for fmt in _DATE_FORMATS:
            try:
                d = datetime.strptime(c, fmt).date()
            except ValueError:
                continue
            if d.year < 1900:
                raise ValueError("year before 1900")
            return d
    raise ValueError("unrecognised date")


def name_key(name: str) -> str:
    return " ".join((name or "").lower().split())


def dedupe_key(row: dict) -> str:
    """Identity for "is this patient already imported?".

    The old software's own patient id wins when present (it is the only key
    that survives a name correction); otherwise phone+name, so one family phone
    shared by several patients still yields separate records.
    """
    if row.get("external_id"):
        return ("id:" + row["external_id"].strip().lower())[:300]
    if row.get("phone"):
        return ("pn:" + row["phone"] + "|" + name_key(row["full_name"]))[:300]
    return ("n:" + name_key(row["full_name"]) + "|" + (row.get("date_of_birth") or ""))[:300]


@dataclass
class ParsedImport:
    rows: list = field(default_factory=list)          # validated, insert-ready dicts
    errors: list = field(default_factory=list)        # blocking; nothing is written
    warnings: list = field(default_factory=list)      # value kept in `extra`, not blocking
    duplicates_in_file: int = 0
    total_rows: int = 0
    mapped_columns: dict = field(default_factory=dict)  # file header -> field
    extra_columns: list = field(default_factory=list)


def _issue(bucket: list, row: int, column: str, problem: str) -> None:
    if len(bucket) < MAX_ISSUES_REPORTED:
        bucket.append({"row": row, "column": column, "problem": problem})


def decode_csv(raw: bytes) -> str:
    if len(raw) > IMPORT_MAX_FILE_BYTES:
        raise ImportFileError(
            f"File is larger than {IMPORT_MAX_FILE_BYTES // (1024 * 1024)} MB. "
            "Split it into smaller files and import them one after another."
        )
    if not raw.strip():
        raise ImportFileError("The file is empty.")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    # Excel on Windows saves "CSV (Comma delimited)" as cp1252, not UTF-8.
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        raise ImportFileError(
            "Could not read the file's text encoding. In Excel use "
            "File > Save As > 'CSV UTF-8 (Comma delimited)'."
        )


def parse_patient_csv(raw: bytes) -> ParsedImport:
    """Validate an old-software patient export. Pure: never touches the DB.

    Only a missing/overlong name blocks the import. A malformed optional value
    (bad date, bad phone, age out of range) is NOT fatal: legacy data is dirty,
    and rejecting a 5,000-row file for 40 bad birthdays is worse than keeping
    the raw text in `extra` and telling the admin which rows were affected.
    """
    text = decode_csv(raw)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise ImportFileError("The file has no header row.")

    result = ParsedImport()
    col_field: list = []
    seen_fields: set = set()
    for h in header:
        f = _ALIAS_TO_FIELD.get(_norm_header(h))
        if f and f not in seen_fields:
            seen_fields.add(f)
            col_field.append(f)
            result.mapped_columns[h.strip()] = f
        else:
            col_field.append(None)
            if (h or "").strip() and len(result.extra_columns) < MAX_EXTRA_COLUMNS:
                result.extra_columns.append(h.strip()[:60])

    if "full_name" not in seen_fields:
        raise ImportFileError(
            "The file needs a patient name column (for example 'Name' or 'Patient Name'). "
            "Download the template to see the expected columns."
        )

    seen_keys: set = set()
    for line_no, cells in enumerate(reader, start=2):
        if not any((c or "").strip() for c in cells):
            continue
        result.total_rows += 1
        if result.total_rows > IMPORT_MAX_ROWS:
            raise ImportFileError(
                f"The file has more than {IMPORT_MAX_ROWS:,} patients. Split it into smaller files."
            )

        values: dict = {}
        extra: dict = {}
        for idx, cell in enumerate(cells):
            v = (cell or "").strip()
            if not v or idx >= len(header):
                continue
            f = col_field[idx]
            if f:
                values[f] = v
            elif header[idx].strip()[:60] in result.extra_columns:
                extra[header[idx].strip()[:60]] = v[:1000]

        name = " ".join(values.get("full_name", "").split())
        if not name:
            _issue(result.errors, line_no, "name", "Patient name is empty.")
            continue
        if len(name) > 150:
            _issue(result.errors, line_no, "name", "Patient name is longer than 150 characters.")
            continue

        row: dict = {"full_name": name}

        if "phone" in values:
            p = normalize_phone(re.sub(r"[^\d+]", "", values["phone"]))
            if validate_phone(p) and re.fullmatch(r"\+?\d{10,15}", p):
                row["phone"] = p
            else:
                extra["phone (unparsed)"] = values["phone"][:60]
                _issue(result.warnings, line_no, "phone",
                       f"'{values['phone'][:30]}' is not a valid phone number; kept as a note.")

        for f in ("date_of_birth", "last_visit_date"):
            if f in values:
                try:
                    d = parse_date(values[f])
                    if d > date.today():
                        raise ValueError("date is in the future")
                    row[f] = d.isoformat()
                except ValueError:
                    extra[f"{f} (unparsed)"] = values[f][:60]
                    _issue(result.warnings, line_no, f,
                           f"'{values[f][:30]}' is not a valid date (use DD/MM/YYYY); kept as a note.")

        if "age_years" in values:
            m = re.fullmatch(r"(\d{1,3})(\.\d+)?\s*(y|yr|yrs|years?)?", values["age_years"].lower())
            if m and int(m.group(1)) <= 130:
                row["age_years"] = int(m.group(1))
            else:
                extra["age (unparsed)"] = values["age_years"][:60]
                _issue(result.warnings, line_no, "age",
                       f"'{values['age_years'][:30]}' is not an age; kept as a note.")

        for f, max_len in _OPTIONAL_LIMITS.items():
            if f in values:
                v = values[f]
                if len(v) > max_len:
                    _issue(result.warnings, line_no, f, f"Longer than {max_len} characters; shortened.")
                    v = v[:max_len]
                row[f] = v

        row["extra"] = extra
        key = dedupe_key(row)
        if key in seen_keys:
            result.duplicates_in_file += 1
            continue
        seen_keys.add(key)
        row["dedupe_key"] = key
        result.rows.append(row)

    if result.total_rows == 0:
        raise ImportFileError("The file has a header row but no patients.")
    return result


IMPORT_TEMPLATE_CSV = (
    "Patient ID,Name,Phone,Gender,Date of Birth,Age,Email,Address,Last Visit,Notes\n"
    "P-1001,Ravi Kumar,9876543210,Male,15/08/1985,,ravi@example.com,\"12 MG Road, Vizag\",02/03/2024,Root canal 36; review in 6 months\n"
    "P-1002,Lakshmi Devi,+91 98480 22338,Female,,42,,Gajuwaka,11/01/2024,Scaling done\n"
)


# ═══════════════════════════════════════════════════════════════════════════
# CSV export (pure)
# ═══════════════════════════════════════════════════════════════════════════

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"\+?\d+(\.\d+)?")


def csv_cell(value) -> str:
    """Spreadsheet-safe cell. A phone like +919876543210 is a plain number and
    cannot be a formula, so it is left alone instead of gaining a stray quote."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    s = str(value)
    if _PLAIN_NUMBER.fullmatch(s):
        return s
    if s.startswith(_FORMULA_PREFIXES) or s.lstrip().startswith(_FORMULA_PREFIXES):
        return "'" + s
    return s


def _rupees(paise) -> str:
    return f"{paise / 100:.2f}" if isinstance(paise, int) and not isinstance(paise, bool) else ""


#: dataset -> (table, date column filtered by the range or None, ordering,
#:            [(CSV header, row -> value)])
EXPORT_DATASETS = {
    "appointments": (
        "appointments", "appointment_date", ("appointment_date", "appointment_time", "id"),
        [
            ("Booking Ref", lambda r: r.get("booking_ref")),
            ("Booking Type", lambda r: r.get("booking_type") or "appointment"),
            ("Patient Name", lambda r: r.get("patient_name")),
            ("Patient Phone", lambda r: r.get("patient_phone")),
            ("Date", lambda r: r.get("appointment_date")),
            ("Time", lambda r: (r.get("appointment_time") or "")[:5]),
            ("Status", lambda r: r.get("status")),
            ("Department", lambda r: r.get("department")),
            ("Doctor", lambda r: r.get("doctor_name")),
            ("Lab Test", lambda r: r.get("lab_test_name")),
            ("Treatment", lambda r: r.get("treatment_name")),
            ("Branch", lambda r: r.get("branch_name")),
            ("Symptoms / Reason", lambda r: r.get("symptoms")),
            ("Amount (Rs)", lambda r: _rupees(r.get("amount_paise"))),
            ("Booked At", lambda r: r.get("created_at")),
        ],
    ),
    "patients": (
        "patients", "created_at", ("created_at", "id"),
        [
            ("Name", lambda r: r.get("name")),
            ("Phone", lambda r: r.get("phone")),
            ("Language", lambda r: r.get("language")),
            ("WhatsApp Opted In", lambda r: r.get("opted_in")),
            ("Visits", lambda r: r.get("visit_count")),
            ("First Contact", lambda r: r.get("created_at")),
            ("Last Seen", lambda r: r.get("last_seen_at")),
        ],
    ),
    "patient_records": (
        "patient_records", None, ("created_at", "id"),
        [
            ("Patient ID", lambda r: r.get("external_id")),
            ("Name", lambda r: r.get("full_name")),
            ("Phone", lambda r: r.get("phone")),
            ("Gender", lambda r: r.get("gender")),
            ("Date of Birth", lambda r: r.get("date_of_birth")),
            ("Age", lambda r: r.get("age_years")),
            ("Email", lambda r: r.get("email")),
            ("Address", lambda r: r.get("address")),
            ("Last Visit", lambda r: r.get("last_visit_date")),
            ("Notes", lambda r: r.get("notes")),
            ("Other Fields", lambda r: "; ".join(f"{k}: {v}" for k, v in (r.get("extra") or {}).items())),
            ("Imported At", lambda r: r.get("created_at")),
        ],
    ),
}


def build_csv(dataset: str, rows: Iterable[dict]) -> str:
    _, _, _, columns = EXPORT_DATASETS[dataset]
    buf = io.StringIO()
    buf.write("﻿")  # Excel needs the BOM to show Telugu/Hindi names correctly
    w = csv.writer(buf)
    w.writerow([h for h, _ in columns])
    for r in rows:
        w.writerow([csv_cell(get(r)) for _, get in columns])
    return buf.getvalue()


def validate_export_range(dataset: str, date_from: Optional[date], date_to: Optional[date]) -> None:
    """Raises ValueError with a user-facing message."""
    if dataset not in EXPORT_DATASETS:
        raise ValueError(f"Unknown dataset. Choose one of: {', '.join(EXPORT_DATASETS)}.")
    if EXPORT_DATASETS[dataset][1] is None:
        return
    if not date_from or not date_to:
        raise ValueError("Choose a From and To date.")
    if date_from > date_to:
        raise ValueError("'From' date must be on or before 'To' date.")
    if (date_to - date_from).days + 1 > EXPORT_MAX_RANGE_DAYS:
        raise ValueError(f"Export at most {EXPORT_MAX_RANGE_DAYS} days at a time (one year).")


_IST = timezone(timedelta(hours=5, minutes=30))


def ist_day_bounds_utc(date_from: date, date_to: date) -> tuple:
    """[start, end) of the IST calendar days as UTC 'Z' strings.

    'Z', not '+05:30': a literal '+' in a PostgREST query string can arrive as
    a space, silently turning the filter into garbage."""
    lo = datetime.combine(date_from, datetime.min.time(), _IST).astimezone(timezone.utc)
    hi = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), _IST).astimezone(timezone.utc)
    return lo.strftime("%Y-%m-%dT%H:%M:%SZ"), hi.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_erased(r: dict) -> bool:
    """DPDP-erased shells carry "[REDACTED]"; they are not patient data."""
    return "[REDACTED]" in (r.get("patient_phone"), r.get("phone"), r.get("name"), r.get("patient_name"))


# ═══════════════════════════════════════════════════════════════════════════
# Database helpers — every query carries the clinic_id predicate
# ═══════════════════════════════════════════════════════════════════════════


def _require_scope(clinic_id: str) -> None:
    if not is_valid_clinic_scope(clinic_id):
        raise ValueError(f"Refusing client-data operation on invalid clinic_id: {clinic_id!r}")


async def fetch_clinic_config(clinic_id: str) -> Optional[dict]:
    """Fresh (uncached) clinics.config — the owner may have just changed the limit."""
    _require_scope(clinic_id)
    res = await sb(supabase.table("clinics").select("id, config").eq("id", clinic_id).limit(1))
    if not res.data:
        return None
    return res.data[0].get("config") or {}


async def count_records(clinic_id: str) -> int:
    _require_scope(clinic_id)
    res = await sb(
        supabase.table("patient_records").select("id", count="exact")
        .eq("clinic_id", clinic_id).limit(1)
    )
    return int(res.count or 0)


async def clinic_quota(clinic_id: str, config: Optional[dict] = None) -> dict:
    if config is None:
        config = await fetch_clinic_config(clinic_id) or {}
    return quota_state(await count_records(clinic_id), records_limit(config))


async def fetch_existing_keys(clinic_id: str) -> set:
    """Every dedupe_key already stored for the clinic. Bounded by its quota."""
    _require_scope(clinic_id)
    keys: set = set()
    start = 0
    while True:
        res = await sb(
            supabase.table("patient_records").select("dedupe_key")
            .eq("clinic_id", clinic_id).order("id")
            .range(start, start + EXPORT_PAGE - 1)
        )
        page = res.data or []
        keys.update(r["dedupe_key"] for r in page)
        if len(page) < EXPORT_PAGE:
            return keys
        start += EXPORT_PAGE


async def delete_batch(clinic_id: str, batch_id: str) -> bool:
    """Undo one import. The FK cascade removes its records in the same statement."""
    _require_scope(clinic_id)
    res = await sb(
        supabase.table("patient_import_batches").delete()
        .eq("clinic_id", clinic_id).eq("id", batch_id)
    )
    return bool(res.data)


async def import_records(clinic_id: str, username: str, file_name: str,
                         parsed: "ParsedImport", new_rows: list, skipped_existing: int) -> dict:
    """Write one import as a batch. All-or-nothing: on ANY failure the batch row
    is deleted, which cascades away whatever chunks had already landed."""
    _require_scope(clinic_id)
    batch_res = await sb(
        # unscoped: insert_scoped_by_payload
        supabase.table("patient_import_batches").insert({
            "clinic_id": clinic_id,
            "file_name": (file_name or "")[:200] or None,
            "rows_in_file": parsed.total_rows,
            "duplicates_in_file": parsed.duplicates_in_file,
            "warning_count": len(parsed.warnings),
            "skipped_existing": skipped_existing,
            "created_by": username,
        })
    )
    batch = batch_res.data[0]
    batch_id = str(batch["id"])
    inserted = 0
    try:
        for i in range(0, len(new_rows), INSERT_CHUNK):
            # Identical keys on every row: PostgREST builds one column list for
            # a bulk insert, so a key present on some rows and absent on others
            # must mean NULL explicitly, never "whatever the first row had".
            chunk = [
                {**{f: r.get(f) for f in _RECORD_FIELDS},
                 "clinic_id": clinic_id, "import_batch_id": batch_id, "created_by": username}
                for r in new_rows[i:i + INSERT_CHUNK]
            ]
            res = await sb(
                # unscoped: insert_scoped_by_payload
                supabase.table("patient_records")
                .upsert(chunk, on_conflict="clinic_id,dedupe_key", ignore_duplicates=True)
            )
            inserted += len(res.data or [])
        done = await sb(
            supabase.table("patient_import_batches")
            .update({"status": "completed", "inserted_count": inserted,
                     "skipped_existing": skipped_existing + (len(new_rows) - inserted)})
            .eq("clinic_id", clinic_id).eq("id", batch_id)
        )
    except Exception:
        try:
            await delete_batch(clinic_id, batch_id)
        except Exception as cleanup_err:
            logger.error(
                f"CLIENT_DATA_IMPORT_ROLLBACK_FAILED clinic={clinic_id} batch={batch_id}: {cleanup_err}"
            )
        raise
    return (done.data or [batch])[0] | {"inserted_count": inserted}


async def fetch_export_rows(clinic_id: str, dataset: str,
                            date_from: Optional[date], date_to: Optional[date]) -> list:
    """Page through the dataset for ONE clinic. Raises OverflowError past EXPORT_MAX_ROWS."""
    _require_scope(clinic_id)
    table, date_col, ordering, _ = EXPORT_DATASETS[dataset]
    bounds = None
    if date_col:
        if date_from is None or date_to is None:
            raise ValueError("date range required")  # validate_export_range() runs first
        if date_col == "appointment_date":  # a DATE column: inclusive calendar days
            bounds = (date_from.isoformat(), (date_to + timedelta(days=1)).isoformat())
        else:  # timestamptz: IST calendar days
            bounds = ist_day_bounds_utc(date_from, date_to)
    rows: list = []
    start = 0
    while True:
        q = supabase.table(table).select("*").eq("clinic_id", clinic_id)
        if date_col and bounds:
            q = q.gte(date_col, bounds[0]).lt(date_col, bounds[1])
        for col in ordering:
            q = q.order(col)
        res = await sb(q.range(start, start + EXPORT_PAGE - 1))
        page = res.data or []
        rows.extend(r for r in page if not _is_erased(r))
        if len(rows) > EXPORT_MAX_ROWS:
            raise OverflowError
        if len(page) < EXPORT_PAGE:
            return rows
        start += EXPORT_PAGE
