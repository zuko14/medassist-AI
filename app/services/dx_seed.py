"""Kriya Diagnostic OS — seed library and parameter import (DX-16, REPORT-TEMPLATES.md §8).

Seed: app/assets/dx_seed/tests_v1.json, ~150 common Indian tests, panels and
packages with INDICATIVE bands. Apply is idempotent by test_code; an existing
clinic-level test with the same name (case-insensitive, AUDIT A23) only gets its
empty DX fields filled. New tests arrive inactive and unapproved: nothing reaches
WhatsApp booking or a report until someone activates and a lab director approves.

Import: CSV / XLSX, one row per band, into existing tests; a dry run reports every
problem and nothing is written unless the whole file is clean.

Deterministic, zero-LLM (tests/test_dx_no_llm.py).
"""

import csv
import io
import json
import zipfile
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase
from app.services import dx_formula
from app.services.dx import raise_for

SEED_PATH = Path(__file__).resolve().parents[1] / "assets" / "dx_seed" / "tests_v1.json"
IMPORT_MAX_BYTES = 2 * 1024 * 1024
IMPORT_MAX_ROWS = 5000
_XLSX_MAX_ENTRIES, _XLSX_MAX_UNPACKED = 200, 20 * 1024 * 1024

_TEST_KEYS = ("name", "price_paise", "test_code", "kind", "department_id", "container_id", "method", "tat_minutes",
              "loinc_code", "dx_result_mode", "gender_restriction", "sample_type", "is_active")
_PARAM_KEYS = ("lab_test_id", "name", "unit", "data_type", "options", "sort_order", "min_age_days", "max_age_days",
               "gender", "normal_low", "normal_high", "critical_low", "critical_high", "normal_text_value",
               "is_active", "parameter_code", "method", "decimal_places", "rounding_mode", "formula",
               "reference_text", "machine_code", "delta_direction", "delta_limit_percent", "delta_limit_abs",
               "delta_lookback_days", "low_exclusive", "high_exclusive")


@lru_cache(maxsize=1)
def load_seed() -> dict:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


async def _rows(query) -> list[dict]:
    try:
        res = await sb(query)
    except Exception as e:
        raise_for(e)
    return list(res.data or [])


async def _insert_many(table: str, clinic_id: str, rows: list[dict], keys: tuple) -> list[dict]:
    """Bulk insert with the same full key set on every row: a missing key in a bulk
    insert becomes NULL, not the column default."""
    if not rows:
        return []
    payload = [{**{k: r.get(k) for k in keys}, "clinic_id": clinic_id} for r in rows]
    # unscoped: insert_scoped_by_payload
    return await _rows(supabase.table(table).insert(payload))


def _s(v) -> Optional[str]:
    return None if v is None else str(v)


# ── seed ────────────────────────────────────────────────────────────────────

async def _clinic_tests(clinic_id: str) -> list[dict]:
    return await _rows(scoped_query("lab_tests", clinic_id, "id, name, test_code, kind, branch_id, department_id, "
                                    "container_id, method, tat_minutes, loinc_code").is_("branch_id", "null"))


def _expand(keys: Optional[list[str]]) -> list[dict]:
    """The selected seed entries plus every member they need, in seed order."""
    tests = load_seed()["tests"]
    if keys is None:
        return tests
    by_key = {t["key"]: t for t in tests}
    unknown = [k for k in keys if k not in by_key]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown seed test: {unknown[0]}")
    want: set = set()
    stack = list(keys)
    while stack:
        k = stack.pop()
        if k not in want:
            want.add(k)
            stack.extend(by_key[k]["components"])
    return [t for t in tests if t["key"] in want]


def _classify(entry: dict, by_code: dict, by_name: dict) -> tuple[str, Optional[dict]]:
    if entry["code"].upper() in by_code:
        return "exists", by_code[entry["code"].upper()]
    match = by_name.get(entry["name"].strip().lower())
    if match is None:
        return "new", None
    return ("name_match", match) if not match.get("test_code") else ("conflict", match)


async def preview_seed(clinic_id: str) -> dict:
    existing = await _clinic_tests(clinic_id)
    by_code = {t["test_code"].upper(): t for t in existing if t.get("test_code")}
    by_name = {t["name"].strip().lower(): t for t in existing}
    seed = load_seed()
    items = []
    for t in seed["tests"]:
        status, _ = _classify(t, by_code, by_name)
        items.append({"key": t["key"], "code": t["code"], "name": t["name"], "kind": t["kind"],
                      "department": t["department"], "parameters": len(t["parameters"]),
                      "components": t["components"], "status": status})
    count = {s: sum(1 for i in items if i["status"] == s) for s in ("new", "exists", "name_match", "conflict")}
    return {"version": seed["version"], "source": seed["source"], "total": len(items), **count, "items": items}


def _param_rows(test_id: str, params: list[dict]) -> list[dict]:
    rows = []
    for i, p in enumerate(params):
        for b in p["bands"] or [{}]:      # a calculated value without a band still needs its definition row
            rows.append({
                "lab_test_id": test_id, "name": p["name"], "unit": p.get("unit"), "data_type": p["data_type"],
                "options": p.get("options"), "sort_order": (i + 1) * 10,
                "min_age_days": b.get("min_age_days", 0), "max_age_days": b.get("max_age_days", 43800),
                "gender": b.get("gender", "all"), "normal_low": _s(b.get("normal_low")),
                "normal_high": _s(b.get("normal_high")), "critical_low": _s(b.get("critical_low")),
                "critical_high": _s(b.get("critical_high")), "normal_text_value": b.get("normal_text_value"),
                "is_active": True, "parameter_code": p["code"], "method": p.get("method"),
                "decimal_places": p.get("decimal_places", 2), "rounding_mode": p.get("rounding_mode", "half_up"),
                "formula": p.get("formula"), "reference_text": p.get("reference_text"), "machine_code": None,
                "delta_direction": None, "delta_limit_percent": None, "delta_limit_abs": None,
                "delta_lookback_days": 30, "low_exclusive": bool(b.get("low_exclusive")),
                "high_exclusive": bool(b.get("high_exclusive"))})
    return rows


async def apply_seed(clinic_id: str, test_keys: Optional[list[str]] = None) -> dict:
    seed = load_seed()
    entries = _expand(test_keys)
    out = {"departments": 0, "containers": 0, "created": 0, "matched_by_name": 0, "already_present": 0,
           "conflicts": [], "parameters": 0, "components": 0}

    # 1. departments and containers, by code (a clinic's own rows are kept as they are)
    masters = {}
    for table, key, fields in (("dx_departments", "departments", ("code", "name", "report_heading", "sort_order")),
                               ("dx_containers", "containers", ("code", "name", "colour_hex", "sample_type",
                                                                "default_volume_ml", "stability_room_hours",
                                                                "stability_cold_hours"))):
        cols = "id, code, name" + (", sample_type" if table == "dx_containers" else "")
        have = {r["code"]: r for r in await _rows(scoped_query(table, clinic_id, cols))}
        missing = [{k: _s(m[k]) if k == "default_volume_ml" else m[k] for k in fields}
                   for m in seed[key] if m["code"] not in have]
        for r in await _insert_many(table, clinic_id, missing, fields):
            have[r["code"]] = r
        out[key] = len(missing)
        masters[key] = have

    # 2. tests: new rows, name matches (fill empty DX fields), already present
    existing = await _clinic_tests(clinic_id)
    by_code = {t["test_code"].upper(): t for t in existing if t.get("test_code")}
    by_name = {t["name"].strip().lower(): t for t in existing}
    ids: dict[str, str] = {}
    new_rows, needs_params = [], []
    for e in entries:
        status, match = _classify(e, by_code, by_name)
        dept = masters["departments"].get(e["department"], {}).get("id")
        cont = masters["containers"].get(e["container"]) if e["container"] else None
        if status == "exists":
            ids[e["key"]] = match["id"]
            out["already_present"] += 1
        elif status == "conflict":
            out["conflicts"].append({"key": e["key"], "name": e["name"], "existing_code": match["test_code"]})
        elif status == "name_match":
            fill = {"test_code": e["code"]}
            for col, val in (("department_id", dept), ("container_id", cont and cont["id"]), ("method", e["method"]),
                             ("tat_minutes", e["tat_minutes"]), ("loinc_code", e["loinc"])):
                if not match.get(col) and val is not None:
                    fill[col] = val
            if e["kind"] != "test" and match.get("kind") == "test":
                fill["kind"] = e["kind"]
            try:
                await _rows(supabase.table("lab_tests").update(fill).eq("clinic_id", clinic_id).eq("id", match["id"]))
            except HTTPException:
                # e.g. the clinic's test already has parameters: keep it as it is, link it by code only
                fill.pop("kind", None)
                await _rows(supabase.table("lab_tests").update(fill).eq("clinic_id", clinic_id).eq("id", match["id"]))
            ids[e["key"]] = match["id"]
            out["matched_by_name"] += 1
            if e["kind"] == "test" and e["parameters"]:
                needs_params.append(e)
        else:
            new_rows.append({"name": e["name"], "price_paise": e["price_paise"], "test_code": e["code"],
                             "kind": e["kind"], "department_id": dept, "container_id": cont and cont["id"],
                             "method": e["method"], "tat_minutes": e["tat_minutes"], "loinc_code": e["loinc"],
                             "dx_result_mode": e["dx_result_mode"], "gender_restriction": e["gender_restriction"],
                             "sample_type": cont and cont["name"], "is_active": False})
    for r in await _insert_many("lab_tests", clinic_id, new_rows, _TEST_KEYS):
        key = next(e["key"] for e in entries if e["code"] == r["test_code"])
        ids[key] = r["id"]
        out["created"] += 1
        entry = next(e for e in entries if e["key"] == key)
        if entry["parameters"]:
            needs_params.append(entry)

    # 3. parameters: only for tests that have none yet (a clinic's own bands are never touched)
    if needs_params:
        have_params = {r["lab_test_id"] for r in await _rows(
            scoped_query("lab_test_parameters", clinic_id, "lab_test_id").in_("lab_test_id",
                                                                             [ids[e["key"]] for e in needs_params]))}
        rows = [row for e in needs_params if ids[e["key"]] not in have_params
                for row in _param_rows(ids[e["key"]], e["parameters"])]
        for i in range(0, len(rows), 500):
            out["parameters"] += len(await _insert_many("lab_test_parameters", clinic_id, rows[i:i + 500],
                                                        _PARAM_KEYS))

    # 4. components of panels and packages
    groups = [e for e in entries if e["components"] and e["key"] in ids]
    if groups:
        linked = {(c["parent_test_id"], c["child_test_id"]) for c in await _rows(
            scoped_query("dx_test_components", clinic_id, "parent_test_id, child_test_id")
            .in_("parent_test_id", [ids[g["key"]] for g in groups]))}
        links = [{"parent_test_id": ids[g["key"]], "child_test_id": ids[c], "sort_order": (i + 1) * 10}
                 for g in groups for i, c in enumerate(g["components"])
                 if c in ids and (ids[g["key"]], ids[c]) not in linked]
        out["components"] = len(await _insert_many("dx_test_components", clinic_id, links,
                                                   ("parent_test_id", "child_test_id", "sort_order")))
    return out


# ── CSV / XLSX parameter import ─────────────────────────────────────────────

COLUMNS = ("test_code", "parameter_code", "name", "unit", "data_type", "options", "sort_order", "gender",
           "min_age_days", "max_age_days", "normal_low", "normal_high", "critical_low", "critical_high",
           "low_exclusive", "high_exclusive", "normal_text_value", "decimal_places", "rounding_mode", "formula",
           "reference_text", "method")


def _read_table(raw: bytes) -> list[dict]:
    """CSV (UTF-8) or XLSX bytes -> rows keyed by lower-case header."""
    if not raw:
        raise HTTPException(status_code=400, detail="The file is empty.")
    if len(raw) > IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The file is larger than 2 MB.")
    if raw.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                infos = zf.infolist()
                if len(infos) > _XLSX_MAX_ENTRIES or sum(i.file_size for i in infos) > _XLSX_MAX_UNPACKED:
                    raise HTTPException(status_code=400, detail="The spreadsheet is too large to read safely.")
            import openpyxl
            ws = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
            grid = [[("" if c is None else str(c)).strip() for c in row] for row in ws.iter_rows(values_only=True)]
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="The spreadsheet could not be read. Save it as .xlsx or CSV.")
    elif raw.startswith((b"MZ", b"\x7fELF", b"%PDF", b"\xd0\xcf\x11\xe0")) or b"\x00" in raw[:4096]:
        raise HTTPException(status_code=400, detail="Upload a CSV or .xlsx file.")
    else:
        try:
            grid = [[c.strip() for c in row] for row in csv.reader(io.StringIO(raw.decode("utf-8-sig")))]
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Save the CSV as UTF-8.")
    grid = [r for r in grid if any(r)]
    if not grid:
        raise HTTPException(status_code=400, detail="The file has no rows.")
    header = [h.strip().lower().replace(" ", "_") for h in grid[0]]
    for need in ("test_code", "name"):
        if need not in header:
            raise HTTPException(status_code=422, detail=f"Missing column: {need}")
    if len(grid) - 1 > IMPORT_MAX_ROWS:
        raise HTTPException(status_code=413, detail=f"At most {IMPORT_MAX_ROWS} rows per file.")
    return [{h: (r[i] if i < len(r) else "") for i, h in enumerate(header) if h in COLUMNS} for r in grid[1:]]


def _num(v: str, field: str, errors: list) -> Optional[Decimal]:
    if v in ("", None):
        return None
    try:
        d = Decimal(str(v).replace(",", ""))
        if not d.is_finite() or abs(d) > 9_999_999:
            raise InvalidOperation
        return d
    except InvalidOperation:
        errors.append(f"{field}: '{v}' is not a number")
        return None


def _int(v: str, field: str, default: int, lo: int, hi: int, errors: list) -> int:
    if v in ("", None):
        return default
    try:
        n = int(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        errors.append(f"{field}: '{v}' is not a whole number")
        return default
    if not lo <= n <= hi:
        errors.append(f"{field}: {n} is outside {lo}–{hi}")
    return n


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _band_row(r: dict, test_id: str, errors: list) -> dict:
    name = r.get("name", "").strip()
    if not name or len(name) > 120:
        errors.append("name: required, up to 120 characters")
    data_type = (r.get("data_type") or "numeric").strip().lower()
    if data_type not in ("numeric", "text", "options"):
        errors.append(f"data_type: '{data_type}' (numeric, text or options)")
    gender = (r.get("gender") or "all").strip().lower()
    if gender not in ("all", "male", "female", "other"):
        errors.append(f"gender: '{gender}' (all, male, female, other)")
    rounding = (r.get("rounding_mode") or "half_up").strip().lower()
    if rounding not in dx_formula.ROUNDING:
        errors.append(f"rounding_mode: '{rounding}'")
    code = (r.get("parameter_code") or "").strip().upper() or None
    if code and not (len(code) <= 30 and all(ch.isalnum() or ch in "._-" for ch in code)):
        errors.append(f"parameter_code: '{code}'")
    options = [o.strip() for o in (r.get("options") or "").split("|") if o.strip()] or None
    if data_type == "options" and not options:
        errors.append("options: list them separated by |")
    bounds = {k: _num(r.get(k, ""), k, errors) for k in ("normal_low", "normal_high", "critical_low", "critical_high")}
    lo, hi, clo, chi = (bounds[k] for k in ("normal_low", "normal_high", "critical_low", "critical_high"))
    if (lo is not None and hi is not None and lo > hi) or (clo is not None and lo is not None and clo > lo) \
            or (chi is not None and hi is not None and chi < hi):
        errors.append("ranges must be ordered: critical low <= normal low <= normal high <= critical high")
    min_age = _int(r.get("min_age_days", ""), "min_age_days", 0, 0, 43800, errors)
    max_age = _int(r.get("max_age_days", ""), "max_age_days", 43800, 0, 43800, errors)
    if max_age < min_age:
        errors.append("max_age_days is below min_age_days")
    formula = (r.get("formula") or "").strip() or None
    if formula and not code:
        errors.append("formula: a calculated parameter needs a parameter_code")
    return {"lab_test_id": test_id, "name": name, "unit": (r.get("unit") or "").strip()[:30] or None,
            "data_type": data_type, "options": options,
            "sort_order": _int(r.get("sort_order", ""), "sort_order", 1, 0, 999, errors),
            "min_age_days": min_age, "max_age_days": max_age, "gender": gender,
            **{k: _s(v) for k, v in bounds.items()},
            "normal_text_value": (r.get("normal_text_value") or "").strip()[:120] or None, "is_active": True,
            "parameter_code": code, "method": (r.get("method") or "").strip()[:120] or None,
            "decimal_places": _int(r.get("decimal_places", ""), "decimal_places", 2, 0, 4, errors),
            "rounding_mode": rounding, "formula": formula,
            "reference_text": (r.get("reference_text") or "").replace("\\n", "\n").strip()[:2000] or None,
            "machine_code": None, "delta_direction": None, "delta_limit_percent": None, "delta_limit_abs": None,
            "delta_lookback_days": 30, "low_exclusive": _bool(r.get("low_exclusive", "")),
            "high_exclusive": _bool(r.get("high_exclusive", ""))}


def _overlaps(a: dict, b: dict) -> bool:
    return (a["name"].lower() == b["name"].lower() and a["gender"] == b["gender"]
            and a["min_age_days"] <= b["max_age_days"] and b["min_age_days"] <= a["max_age_days"])


async def import_parameters(clinic_id: str, raw: bytes, dry_run: bool) -> dict:
    """Validate every row; write only if dry_run is False and nothing is wrong."""
    rows = _read_table(raw)
    tests = {t["test_code"].upper(): t for t in await _rows(
        scoped_query("lab_tests", clinic_id, "id, name, test_code, kind").not_.is_("test_code", "null"))}
    report, staged = [], []
    for n, r in enumerate(rows, start=2):                    # row 1 is the header
        errors: list[str] = []
        test = tests.get((r.get("test_code") or "").strip().upper())
        if test is None:
            errors.append(f"test_code: '{r.get('test_code', '')}' is not a test of this clinic")
        elif test["kind"] != "test":
            errors.append(f"test_code: {test['test_code']} is a {test['kind']}; bands belong to its member tests")
        band = _band_row(r, test["id"] if test else "", errors)
        report.append({"row": n, "test_code": (r.get("test_code") or "").upper(), "name": band["name"],
                       "errors": errors})
        staged.append(band)

    by_test: dict[str, list[int]] = {}
    for i, b in enumerate(staged):
        if b["lab_test_id"]:
            by_test.setdefault(b["lab_test_id"], []).append(i)
    current = {}
    if by_test:
        for p in await _rows(scoped_query("lab_test_parameters", clinic_id).in_("lab_test_id", list(by_test))
                             .eq("is_active", True)):
            current.setdefault(p["lab_test_id"], []).append(p)
    for test_id, idxs in by_test.items():
        mine = [staged[i] for i in idxs]
        for k, i in enumerate(idxs):
            b = staged[i]
            if any(_overlaps(b, other) for other in mine[:k]):
                report[i]["errors"].append("overlaps another band in this file (same parameter, sex and age)")
            if any(_overlaps(b, p) for p in current.get(test_id, [])):
                report[i]["errors"].append("overlaps an existing band of this test; deactivate it first")
        codes = {p["parameter_code"] for p in current.get(test_id, []) + mine if p.get("parameter_code")}
        formulas = {p["parameter_code"]: p["formula"] for p in current.get(test_id, [])
                    if p.get("formula") and p.get("parameter_code")}
        for i in idxs:                           # each row's own formula, reported on that row
            b = staged[i]
            if b.get("formula") and b.get("parameter_code"):
                try:
                    dx_formula.validate_formula(b["formula"], codes)
                    formulas[b["parameter_code"]] = b["formula"]
                except dx_formula.FormulaError as e:
                    report[i]["errors"].append(f"formula: {e} {e.token}".strip())
        cycle = dx_formula.find_cycle(formulas)
        for i in idxs:
            if cycle and staged[i].get("parameter_code") in cycle and staged[i].get("formula"):
                report[i]["errors"].append("formula: calculated parameters depend on each other in a loop "
                                           f"({' -> '.join(cycle)})")

    bad = [r for r in report if r["errors"]]
    result = {"rows": len(report), "valid": len(report) - len(bad), "invalid": len(bad),
              "report": report if dry_run or bad else [], "written": 0}
    if dry_run:
        return result
    if bad:
        raise HTTPException(status_code=422, detail={"message": f"{len(bad)} row(s) need fixing; nothing was imported.",
                                                     **result})
    for i in range(0, len(staged), 500):
        result["written"] += len(await _insert_many("lab_test_parameters", clinic_id, staged[i:i + 500], _PARAM_KEYS))
    return result
