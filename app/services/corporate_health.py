"""Corporate employee-health insights (migration 095).

A diagnostic centre runs a health package for a company's employees, uploads
each employee's report PDF, and the company sees AGGREGATE insights only.

Design decisions
----------------
* Deterministic parsing, no LLM. The reports come out of the lab's LIS with a
  real text layer; a regex parser over that text is free, instant and gives the
  same answer every time. An LLM would cost money per page and could not
  promise that every number is exactly what the report says. A PDF the parser
  cannot read is REJECTED with a reason, never guessed at.
* Normal / low / high comes from the reference range PRINTED ON THE REPORT —
  the lab's own sex-specific range (Hb 13-17 male vs 11-15 female) — never a
  table of ours. A range we cannot read confidently makes that one result
  "unclassified": counted as tested, excluded from normal/abnormal percentages.
* Data minimisation. The PDF, the employee's name and the file name are never
  stored. A report row keeps only what the charts need: sex, age, collection
  date, the lab bill id (duplicate guard) and the package values with their
  status — roughly 0.6 KB per employee.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import statistics
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

logger = logging.getLogger(__name__)

PARSER_VERSION = "lis-text-1"

MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 60
MAX_FILES_PER_UPLOAD = 5

_NUM = r"\d+(?:\.\d+)?"

# key, label, panel, line-start name pattern, accepted units (lower-case).
# The pattern must be followed by whitespace and a number, so section headers
# such as "Calcium (Ca)" or "ESR (Erythrocyte Sedimentation Rate)" never match.
PARAMS: list[dict] = [
    {"key": "hemoglobin", "label": "Hemoglobin (Hb%)", "panel": "Blood Count",
     "pattern": r"H(?:a)?emoglobin(?:\s*\(Hb\))?", "units": {"gm%", "g/dl", "gm/dl", "g%"}},
    {"key": "esr", "label": "ESR", "panel": "Blood Count",
     "pattern": r"(?:ESR\s*[-–]\s*)?Erythrocyte\s+Sedimentation\s+Rate(?:\s*\(ESR\))?|ESR",
     "units": {"mm/hr", "mm/h"}},
    {"key": "fbs", "label": "Fasting Blood Sugar (FBS)", "panel": "Diabetes",
     "pattern": r"Glucose\s*[-–]?\s*Fasting(?:\s*\(FBS\))?|Fasting\s+Blood\s+(?:Glucose|Sugar)(?:\s*\(FBS\))?|FBS",
     "units": {"mg/dl"}},
    {"key": "hba1c", "label": "HbA1c", "panel": "Diabetes",
     "pattern": r"Glyc(?:osyl|)ated\s+H(?:b|a?emoglobin)(?:\s*\(HbA1c\))?|HbA1c",
     "units": {"%"}},
    {"key": "total_cholesterol", "label": "Total Cholesterol", "panel": "Lipid Profile",
     "pattern": r"Total\s+Cholesterol|Cholesterol,?\s*Total", "units": {"mg/dl"}},
    {"key": "triglycerides", "label": "Triglycerides", "panel": "Lipid Profile",
     "pattern": r"Triglycerides?", "units": {"mg/dl"}},
    {"key": "total_bilirubin", "label": "Total Bilirubin", "panel": "Liver Function",
     "pattern": r"Total\s+Bilirubin|Bilirubin,?\s*Total", "units": {"mg/dl"}},
    {"key": "sgot", "label": "SGOT (AST)", "panel": "Liver Function",
     "pattern": r"AST\s*\(SGOT\)|SGOT(?:\s*\(AST\))?", "units": {"u/l", "iu/l"}},
    {"key": "sgpt", "label": "SGPT (ALT)", "panel": "Liver Function",
     "pattern": r"ALT\s*\(SGPT\)|SGPT(?:\s*\(ALT\))?", "units": {"u/l", "iu/l"}},
    {"key": "total_protein", "label": "Total Protein", "panel": "Liver Function",
     "pattern": r"Total\s+Proteins?|Proteins?,?\s*Total", "units": {"g/dl", "gm/dl"}},
    {"key": "creatinine", "label": "Creatinine", "panel": "Kidney Function",
     "pattern": r"(?:Serum\s+)?Creatinine", "units": {"mg/dl"}},
    {"key": "blood_urea", "label": "Blood Urea", "panel": "Kidney Function",
     "pattern": r"Blood\s+Urea|(?:Serum\s+)?Urea", "units": {"mg/dl"}},
    {"key": "vitamin_d", "label": "Vitamin D", "panel": "Vitamins & Minerals",
     "pattern": r"Vitamin\s*D\s*\(\s*25\s*[-–]?\s*(?:Hydroxy|OH)\s*\)|25\s*[-–]?\s*(?:OH|Hydroxy)\s*Vitamin\s*D3?",
     "units": {"ng/ml"}},
    {"key": "vitamin_b12", "label": "Vitamin B12", "panel": "Vitamins & Minerals",
     "pattern": r"Vitamin\s*B\s*12(?:\s*\((?:Serum|Cobalamin)\))?", "units": {"pg/ml"}},
    {"key": "calcium", "label": "Calcium", "panel": "Vitamins & Minerals",
     "pattern": r"(?:Serum\s+)?Calcium(?:\s*\(Ca\))?", "units": {"mg/dl"}},
    {"key": "t3", "label": "T3 (Total)", "panel": "Thyroid",
     "pattern": r"(?:Total\s+)?Triiodothyronine\s*\(T3\)|Total\s+T3|T3,?\s*Total", "units": {"ng/ml"}},
    {"key": "t4", "label": "T4 (Total)", "panel": "Thyroid",
     "pattern": r"(?:Total\s+)?Thyroxine\s*\(T4\)|Total\s+T4|T4,?\s*Total", "units": {"ug/dl", "µg/dl", "mcg/dl"}},
    {"key": "tsh", "label": "TSH", "panel": "Thyroid",
     "pattern": r"Thyroid\s+Stimulating\s+Hormone(?:\s*\(TSH\))?|TSH", "units": {"uiu/ml", "µiu/ml", "miu/l"}},
]
PARAM_KEYS = [p["key"] for p in PARAMS]
_PARAM_BY_KEY = {p["key"]: p for p in PARAMS}

_VALUE_LINE = {
    p["key"]: re.compile(rf"^(?:{p['pattern']})\s+(?P<val>{_NUM})(?![\d.])\s*(?P<rest>.*)$", re.I)
    for p in PARAMS
}

# Band labels that name the NORMAL band. Any other label on the value line
# ("Deficiency: < 20" on Vitamin D) sends us looking for the normal band below.
_NORMAL_LABELS = {
    "normal", "desirable", "desirable level", "sufficiency", "sufficient",
    "non-diabetic", "non diabetic", "adult", "adults", "optimal", "reference",
}
_NORMAL_LABEL_RE = "|".join(sorted((re.escape(x) for x in _NORMAL_LABELS), key=len, reverse=True))
# Requires ":" or "|" after the label, so "Near / Above Optimal 100-129" and
# "Insufficiency: 20 - <30" (no word boundary before "sufficiency") never match.
_LABELLED_NORMAL_IN_LINE = re.compile(rf"(?:^|\s)(?:{_NORMAL_LABEL_RE})\s*[:|]\s*(?P<range>.+)$", re.I)
_AGE_QUALIFIED = re.compile(r"^(?P<age>\d{1,3})\s*(?:years?|yrs?)\s*(?:and\s+)?above\s*[:|]?\s*(?P<range>.+)$", re.I)
_LABELLED = re.compile(r"^(?P<label>[A-Za-z][A-Za-z \-/]*?)\s*[:|]?\s*(?P<range>(?:[<>≤≥]|\d).*)$")
_RANGE_BETWEEN = re.compile(rf"^(?P<a>{_NUM})\s*(?:-|–|to)\s*(?P<op><=?|≤)?\s*(?P<b>{_NUM})(?![\d.])")
_RANGE_UPPER = re.compile(rf"^(?P<op><\s*or\s*=|<=|≤|<)\s*(?P<b>{_NUM})(?![\d.])", re.I)
_RANGE_LOWER = re.compile(rf"^(?P<op>>\s*or\s*=|>=|≥|>)\s*(?P<a>{_NUM})(?![\d.])", re.I)

_HEADER_NAME = re.compile(
    r"Patient\s*Name\s*:\s*(?:(?:MR|MRS|MS|MISS|DR|MASTER|BABY|SMT|SRI|SHRI|KUM)\.?\s+)?"
    r"(?P<name>.+?)\s+(?:Collected|Registered|Received|Reported|Sample)\b", re.I)
_HEADER_AGE = re.compile(
    r"\bAge\s*(?:/\s*(?:Sex|Gender))?\s*:\s*(?P<age>\d{1,3})\s*(?P<unit>years?|yrs?|y|months?|m|days?|d)\b\.?"
    r"\s*[(/,]?\s*(?P<sex>male|female|m|f)\b", re.I)
_HEADER_BILL = re.compile(r"\bBill\s*(?:ID|No\.?)\s*:\s*(?P<bill>[A-Za-z0-9][A-Za-z0-9\-/]{0,39})", re.I)
_HEADER_COLLECTED = re.compile(r"\bCollected\s*:\s*(?P<d>[A-Za-z]{3}\s+\d{1,2},\s*\d{4})", re.I)
_END_OF_REPORT = re.compile(r"\*\*\s*END OF REPORT\s*\*\*", re.I)


class ReportRejected(ValueError):
    """The PDF cannot be used. The message is safe to show the uploader."""


# ═══════ reference ranges ═══════


def _parse_range(text: str) -> Optional[dict]:
    """'13 - 17' | '< 35' | '>or = 240' | '20 - <30' -> bounds, or None."""
    t = (text or "").strip()
    m = _RANGE_BETWEEN.match(t)
    if m:
        lo, hi = float(m.group("a")), float(m.group("b"))
        if lo > hi:
            return None
        return {"lo": lo, "lo_incl": True, "hi": hi, "hi_incl": not m.group("op") or "=" in m.group("op")}
    m = _RANGE_UPPER.match(t)
    if m:
        op = re.sub(r"\s+", "", m.group("op"))
        return {"lo": None, "lo_incl": True, "hi": float(m.group("b")), "hi_incl": op in ("<=", "≤", "<or=")}
    m = _RANGE_LOWER.match(t)
    if m:
        op = re.sub(r"\s+", "", m.group("op"))
        return {"lo": float(m.group("a")), "lo_incl": op in (">=", "≥", ">or="), "hi": None, "hi_incl": True}
    return None


def normal_band(ref_text: str, following: list[str], age_years: int) -> tuple[Optional[dict], Optional[str]]:
    """Find the NORMAL band for one result.

    Returns (band, None) or (None, reason). ref_text is what follows the unit
    on the value line; `following` are the remaining lines of the same test.
    """
    ref = (ref_text or "").strip()
    if not ref:
        return None, "no reference range printed"

    m = _AGE_QUALIFIED.match(ref)
    if m:
        if age_years < int(m.group("age")):
            return None, f"printed range applies from age {m.group('age')}"
        band = _parse_range(m.group("range"))
        return (band, None) if band else (None, "reference range not readable")

    band = _parse_range(ref)
    if band:
        return band, None

    m = _LABELLED.match(ref)
    if not m:
        return None, "reference range not readable"
    label = re.sub(r"\s+", " ", m.group("label").strip().lower())
    if label in _NORMAL_LABELS:
        band = _parse_range(m.group("range"))
        return (band, None) if band else (None, "reference range not readable")

    # The value line shows an abnormal band first (Vitamin D: "Deficiency: < 20").
    for line in following:
        mm = _LABELLED_NORMAL_IN_LINE.search(line)
        if mm:
            band = _parse_range(mm.group("range"))
            if band:
                band["text"] = mm.group(0).strip()
                return band, None
    return None, "normal band not found in printed range"


def classify(value: float, band: dict) -> str:
    lo, hi = band.get("lo"), band.get("hi")
    if lo is not None and (value < lo or (value == lo and not band.get("lo_incl", True))):
        return "low"
    if hi is not None and (value > hi or (value == hi and not band.get("hi_incl", True))):
        return "high"
    return "normal"


# ═══════ PDF → report ═══════


def extract_pdf_text(data: bytes) -> str:
    """Text layer of a PDF. Raises ReportRejected with a user-safe reason."""
    import pdfplumber

    if not data or not data.startswith(b"%PDF"):
        raise ReportRejected("Not a PDF file.")
    if len(data) > MAX_PDF_BYTES:
        raise ReportRejected("PDF is larger than 15 MB.")
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise ReportRejected(f"PDF has more than {MAX_PDF_PAGES} pages.")
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except ReportRejected:
        raise
    except Exception as e:  # corrupt, encrypted, truncated
        logger.info(f"corporate_health: unreadable PDF ({type(e).__name__})")
        raise ReportRejected("PDF could not be opened (corrupt or password-protected).") from e
    if len(text.strip()) < 20:
        raise ReportRejected("PDF has no text layer (scanned image). Upload the lab-generated PDF.")
    return text


def _one(values: set, what: str):
    if len(values) > 1:
        raise ReportRejected(f"PDF contains more than one {what} — upload one employee per PDF.")
    return next(iter(values)) if values else None


def parse_report_text(text: str) -> dict:
    """Parse one employee's report. Pure: same text in, same dict out."""
    lines = [ln.strip() for ln in text.splitlines()]

    names = {re.sub(r"\s+", " ", m.group("name")).strip().upper() for m in _HEADER_NAME.finditer(text)}
    ages = set()
    for m in _HEADER_AGE.finditer(text):
        age = int(m.group("age")) if m.group("unit").lower().startswith("y") else 0
        ages.add((age, "M" if m.group("sex").lower().startswith("m") else "F"))
    bills = {m.group("bill").strip().upper() for m in _HEADER_BILL.finditer(text)}

    # Every page repeats the header. More than one distinct patient means two
    # people's reports were merged into one file — refuse rather than mix them.
    name = _one(names, "patient name")
    age_sex = _one(ages, "patient age/sex")
    bill_id = _one(bills, "bill id")
    if not name:
        raise ReportRejected("Patient name not found — is this a lab report?")
    if not age_sex:
        raise ReportRejected("Patient age and sex not found on the report.")
    age, sex = age_sex
    if age > 120:
        raise ReportRejected("Patient age on the report is not plausible.")

    collected = []
    for m in _HEADER_COLLECTED.finditer(text):
        try:
            collected.append(datetime.strptime(re.sub(r"\s+", " ", m.group("d")), "%b %d, %Y").date())
        except ValueError:
            pass

    # Locate every value line first, so each test's block ends where the next begins.
    hits = []
    for i, line in enumerate(lines):
        for key, rx in _VALUE_LINE.items():
            m = rx.match(line)
            if m:
                hits.append((i, key, m))
                break
    starts = [i for i, _, _ in hits]

    results: dict = {}
    warnings: list[str] = []
    for i, key, m in hits:
        label = _PARAM_BY_KEY[key]["label"]
        nxt = next((s for s in starts if s > i), len(lines))
        block = []
        for ln in lines[i + 1:min(nxt, i + 13)]:
            if _END_OF_REPORT.search(ln):
                break
            block.append(ln)
        value = float(m.group("val"))
        unit, _, ref = m.group("rest").strip().partition(" ")
        entry = {"v": value, "u": unit, "ref": ref.strip()[:80]}
        if unit.lower() not in _PARAM_BY_KEY[key]["units"]:
            entry["s"] = "unclassified"
            warnings.append(f"{label}: unexpected unit '{unit}'")
        else:
            band, why = normal_band(ref, block, age)
            if band:
                entry.update({"lo": band["lo"], "hi": band["hi"], "s": classify(value, band)})
                if band.get("text"):  # the normal band came from a later line (Vitamin D)
                    entry["ref"] = band["text"][:80]
            else:
                entry["s"] = "unclassified"
                warnings.append(f"{label}: {why}")
        prev = results.get(key)
        if prev is None:
            results[key] = entry
        elif prev["v"] != value:
            results[key] = {**prev, "s": "unclassified"}
            warnings.append(f"{label}: reported twice with different values")

    if not results:
        raise ReportRejected("None of the package tests were found in this PDF.")

    return {
        "employee_name": name,  # shown to the uploader once, never stored
        "sex": sex,
        "age_years": age,
        "bill_id": bill_id,
        "collected_on": min(collected).isoformat() if collected else None,
        "results": results,
        "missing": [k for k in PARAM_KEYS if k not in results],
        "warnings": warnings,
    }


def parse_pdf(data: bytes) -> dict:
    return parse_report_text(extract_pdf_text(data))


# ═══════ aggregation ═══════

AGE_BANDS = [("<30", 0, 29), ("30-39", 30, 39), ("40-49", 40, 49), ("50-59", 50, 59), ("60+", 60, 200)]
AGE_BAND_KEYS = [b[0] for b in AGE_BANDS]
STATUSES = ("normal", "low", "high", "unclassified")


def age_band(age: int) -> str:
    for key, lo, hi in AGE_BANDS:
        if lo <= age <= hi:
            return key
    return AGE_BANDS[-1][0]


def pct(n: int, d: int) -> float:
    """Half-up to one decimal. round() is banker's rounding: 12.25 -> 12.2."""
    if not d:
        return 0.0
    return float((Decimal(n) * 100 / Decimal(d)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _counts() -> dict:
    return {s: 0 for s in STATUSES}


def build_insights(rows: list[dict], sex: Optional[str] = None, band: Optional[str] = None) -> dict:
    """Aggregate report rows for the dashboard. Pure and deterministic.

    Normal/abnormal percentages are over CLASSIFIED results only; the
    unclassified count is returned alongside, so nothing is silently dropped.
    """
    rows = [r for r in rows
            if (not sex or r["sex"] == sex) and (not band or age_band(int(r["age_years"])) == band)]

    ages = [int(r["age_years"]) for r in rows]
    dates = sorted(r["collected_on"] for r in rows if r.get("collected_on"))
    with_abnormal = sum(
        1 for r in rows if any(v.get("s") in ("low", "high") for v in (r.get("results") or {}).values()))
    males = sum(1 for r in rows if r["sex"] == "M")

    params = []
    for p in PARAMS:
        c = _counts()
        by_sex = {"M": _counts(), "F": _counts()}
        by_age = {b: _counts() for b in AGE_BAND_KEYS}
        values, refs, units = [], {}, {}
        for r in rows:
            res = (r.get("results") or {}).get(p["key"])
            if not res:
                continue
            s = res.get("s") if res.get("s") in STATUSES else "unclassified"
            c[s] += 1
            by_sex[r["sex"]][s] += 1
            by_age[age_band(int(r["age_years"]))][s] += 1
            if s != "unclassified":
                values.append(float(res["v"]))
                units[res.get("u") or ""] = units.get(res.get("u") or "", 0) + 1
                ref_key = (r["sex"], res.get("ref") or "")
                refs[ref_key] = refs.get(ref_key, 0) + 1
        tested = sum(c.values())
        classified = tested - c["unclassified"]
        abnormal = c["low"] + c["high"]
        params.append({
            "key": p["key"], "label": p["label"], "panel": p["panel"],
            "unit": max(sorted(units), key=lambda u: units[u]) if units else None,
            "tested": tested, **c, "abnormal": abnormal,
            "abnormal_pct": pct(abnormal, classified), "normal_pct": pct(c["normal"], classified),
            "by_sex": by_sex, "by_age": by_age,
            "stats": {
                "min": min(values), "max": max(values),
                "mean": round(statistics.fmean(values), 2), "median": round(statistics.median(values), 2),
            } if values else None,
            "ranges": [{"sex": k[0], "text": k[1], "count": n}
                       for k, n in sorted(refs.items(), key=lambda kv: (-kv[1], kv[0]))[:4]],
        })

    return {
        "summary": {
            "employees": len(rows),
            "male": males, "female": len(rows) - males,
            "male_pct": pct(males, len(rows)), "female_pct": pct(len(rows) - males, len(rows)),
            "avg_age": round(statistics.fmean(ages), 1) if ages else None,
            "with_abnormal": with_abnormal, "with_abnormal_pct": pct(with_abnormal, len(rows)),
            "collected_from": dates[0] if dates else None, "collected_to": dates[-1] if dates else None,
            "age_distribution": {b: sum(1 for a in ages if age_band(a) == b) for b in AGE_BAND_KEYS},
        },
        "params": params,
        "age_bands": AGE_BAND_KEYS,
        "filters": {"sex": sex, "age_band": band},
    }


# ═══════ persistence ═══════
# Every query carries .eq("clinic_id", ...) — see tests/test_lint_unscoped_queries.py.

_PARSE_LOCK = asyncio.Semaphore(1)


def _db():
    from app.database import is_valid_clinic_scope, sb, supabase

    return sb, supabase, is_valid_clinic_scope


def _scope(clinic_id: str) -> str:
    if not _db()[2](clinic_id):
        raise ValueError("clinic scope required")
    return str(clinic_id)


async def list_companies(clinic_id: str, only_id: Optional[str] = None) -> list[dict]:
    sb, supabase, _ = _db()
    scope = _scope(clinic_id)
    q = supabase.table("corporate_clients").select("id, name, is_active, created_at").eq("clinic_id", scope)
    if only_id:
        q = q.eq("id", only_id)
    companies = (await sb(q.order("name").limit(500))).data or []
    for c in companies:  # a lab has a handful of corporate clients; one count each
        res = await sb(supabase.table("corporate_health_reports").select("id", count="exact")
                       .eq("clinic_id", scope).eq("corporate_client_id", c["id"]).limit(1))
        c["reports"] = res.count or 0
    return companies


async def get_company(clinic_id: str, company_id: str) -> Optional[dict]:
    sb, supabase, _ = _db()
    res = await sb(supabase.table("corporate_clients").select("id, name, is_active")
                   .eq("clinic_id", _scope(clinic_id)).eq("id", company_id).limit(1))
    return (res.data or [None])[0]


def _is_unique_violation(e: Exception) -> bool:
    s = str(e).lower()
    return "duplicate key" in s or "23505" in s


async def ingest_pdf(clinic_id: str, company_id: str, data: bytes, uploaded_by: str) -> dict:
    """Parse and store one report. Never raises for a bad PDF; returns
    {'status': 'accepted'|'duplicate'|'rejected', ...} per file."""
    sb, supabase, _ = _db()
    scope = _scope(clinic_id)
    digest = hashlib.sha256(data).hexdigest()
    try:
        async with _PARSE_LOCK:
            # ponytail: one parse at a time per process, off the event loop. pdfminer is
            # pure Python (~1s/PDF) and this process also ACKs Meta webhooks; move parsing
            # to a worker process if upload volume ever makes webhook latency measurable.
            parsed = await asyncio.to_thread(parse_pdf, data)
    except ReportRejected as e:
        return {"status": "rejected", "reason": str(e)}

    view = {k: parsed[k] for k in ("employee_name", "sex", "age_years", "bill_id", "collected_on", "missing", "warnings")}
    view["found"] = len(parsed["results"])
    view["abnormal"] = sum(1 for v in parsed["results"].values() if v["s"] in ("low", "high"))
    duplicate = {"status": "duplicate", "reason": "This report is already uploaded for this company.", **view}

    q = (supabase.table("corporate_health_reports").select("id")
         .eq("clinic_id", scope).eq("corporate_client_id", company_id))
    q = q.eq("bill_id", parsed["bill_id"]) if parsed["bill_id"] else q.eq("file_sha256", digest)
    if (await sb(q.limit(1))).data:
        return duplicate

    row = {
        "clinic_id": scope, "corporate_client_id": company_id,
        "sex": parsed["sex"], "age_years": parsed["age_years"], "bill_id": parsed["bill_id"],
        "collected_on": parsed["collected_on"], "results": parsed["results"],
        "file_sha256": digest, "parser_version": PARSER_VERSION, "uploaded_by": uploaded_by,
    }
    try:
        res = await sb(
            # unscoped: insert_scoped_by_payload
            supabase.table("corporate_health_reports").insert(row))
    except Exception as e:
        if _is_unique_violation(e):  # lost a race with a concurrent upload of the same report
            return duplicate
        raise
    return {"status": "accepted", "id": (res.data or [{}])[0].get("id"), **view}


async def fetch_rows(clinic_id: str, company_id: str) -> list[dict]:
    """Every report row for a company, paged past PostgREST's 1000-row cap."""
    sb, supabase, _ = _db()
    scope = _scope(clinic_id)
    rows, page = [], 1000
    while True:
        res = await sb(supabase.table("corporate_health_reports")
                       .select("id, sex, age_years, collected_on, results")
                       .eq("clinic_id", scope).eq("corporate_client_id", company_id)
                       .order("id").range(len(rows), len(rows) + page - 1))
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < page:
            return rows


async def list_reports(clinic_id: str, company_id: str, limit: int, offset: int) -> dict:
    sb, supabase, _ = _db()
    res = await sb(supabase.table("corporate_health_reports")
                   .select("id, bill_id, sex, age_years, collected_on, results, created_at", count="exact")
                   .eq("clinic_id", _scope(clinic_id)).eq("corporate_client_id", company_id)
                   .order("created_at", desc=True).range(offset, offset + limit - 1))
    reports = []
    for r in res.data or []:
        results = r.pop("results") or {}
        r["found"] = len(results)
        r["abnormal"] = sum(1 for v in results.values() if v.get("s") in ("low", "high"))
        r["unclassified"] = sum(1 for v in results.values() if v.get("s") == "unclassified")
        reports.append(r)
    return {"reports": reports, "total": res.count or 0}


async def delete_reports(clinic_id: str, company_id: str, report_id: Optional[str] = None) -> int:
    """Delete one report, or (report_id=None) every report of the company."""
    sb, supabase, _ = _db()
    q = (supabase.table("corporate_health_reports").delete()
         .eq("clinic_id", _scope(clinic_id)).eq("corporate_client_id", company_id))
    if report_id:
        q = q.eq("id", report_id)
    return len((await sb(q)).data or [])
