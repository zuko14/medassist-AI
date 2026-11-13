"""Kriya Diagnostic OS — catalogue service (migration 126, DX-15).

Departments, containers, signatories (+ signature image), the test master
(DX fields on lab_tests), parameter bands with DX fields and formulas,
panel/package components, and approval by content digest (dx_approve_test).
Design: docs/diagnostic-os/DATA-MODEL.md §3, REPORT-TEMPLATES.md §1–§3.

Zero-LLM (tests/test_dx_no_llm.py). Every query is scoped to the clinic.
"""

import asyncio
import hashlib
from typing import Optional

from fastapi import HTTPException

from app.database import restrict_to_branch, sb, scoped_query, supabase
from app.services import dx_formula
from app.services.dx import raise_for

SIGNATURE_BUCKET = "lab-reports"          # existing private bucket; DX assets live under <clinic>/dx-assets/
SIGNATURE_MAX_BYTES = 300 * 1024

TEST_FIELDS = ("test_code", "kind", "department_id", "container_id", "sample_volume_ml", "method", "tat_minutes",
               "stat_tat_minutes", "print_title", "interpretation", "report_notes", "gender_restriction",
               "loinc_code", "print_order", "dx_result_mode", "is_active")
LIST_COLUMNS = ("id, name, test_code, kind, branch_id, department_id, container_id, price_paise, is_active, "
                "approved_at, dx_result_mode, sample_type, tat_minutes")
PARAM_FIELDS = ("name", "unit", "data_type", "options", "sort_order", "min_age_days", "max_age_days", "gender",
                "normal_low", "normal_high", "critical_low", "critical_high", "normal_text_value", "is_active",
                "parameter_code", "method", "decimal_places", "rounding_mode", "formula", "reference_text",
                "machine_code", "delta_direction", "delta_limit_percent", "delta_limit_abs", "delta_lookback_days",
                "low_exclusive", "high_exclusive")
_NUMERIC = ("normal_low", "normal_high", "critical_low", "critical_high", "delta_limit_percent", "delta_limit_abs",
            "sample_volume_ml", "default_volume_ml")


async def _rows(query) -> list[dict]:
    try:
        res = await sb(query)
    except Exception as e:
        raise_for(e)
    return list(res.data or [])


async def _one(query, label: str) -> dict:
    rows = await _rows(query)
    if not rows:
        raise HTTPException(status_code=404, detail=f"{label} not found.")
    return rows[0]


def _clean(data: dict) -> dict:
    """Decimals travel as strings (NUMERIC columns, no float drift)."""
    return {k: (str(v) if k in _NUMERIC and v is not None else v) for k, v in data.items()}


async def _insert(table: str, clinic_id: str, data: dict) -> dict:
    # unscoped: insert_scoped_by_payload
    rows = await _rows(supabase.table(table).insert({**_clean(data), "clinic_id": clinic_id}))
    return rows[0]


async def _update(table: str, clinic_id: str, row_id: str, data: dict, label: str) -> dict:
    rows = await _rows(supabase.table(table).update(_clean(data)).eq("clinic_id", clinic_id).eq("id", row_id))
    if not rows:
        raise HTTPException(status_code=404, detail=f"{label} not found.")
    return rows[0]


# ── departments, containers ─────────────────────────────────────────────────

async def list_rows(table: str, clinic_id: str) -> list[dict]:
    order = {"dx_departments": ("sort_order", "code"), "dx_containers": ("code",),
             "dx_signatories": ("created_at",)}[table]
    q = scoped_query(table, clinic_id)
    for col in order:
        q = q.order(col)
    return await _rows(q)


async def create_row(table: str, clinic_id: str, data: dict) -> dict:
    return await _insert(table, clinic_id, data)


async def update_row(table: str, clinic_id: str, row_id: str, data: dict, label: str) -> dict:
    return await _update(table, clinic_id, row_id, data, label)


# ── signatories ─────────────────────────────────────────────────────────────

async def _registered_doctor(clinic_id: str, doctor_id: str) -> dict:
    doc = await _one(scoped_query("doctors", clinic_id, "id, name, registration_number").eq("id", doctor_id).limit(1),
                     "Doctor")
    if not (doc.get("registration_number") or "").strip():
        raise HTTPException(status_code=422, detail="A signatory must be a doctor with a registration number.")
    return doc


async def list_signatories(clinic_id: str) -> list[dict]:
    rows = await _rows(scoped_query("dx_signatories", clinic_id).order("created_at"))
    ids = sorted({r["doctor_id"] for r in rows})
    docs = {d["id"]: d for d in await _rows(scoped_query("doctors", clinic_id, "id, name, registration_number")
                                            .in_("id", ids))} if ids else {}
    return [{**r, "doctor_name": docs.get(r["doctor_id"], {}).get("name"),
             "registration_number": docs.get(r["doctor_id"], {}).get("registration_number")} for r in rows]


async def create_signatory(clinic_id: str, data: dict) -> dict:
    await _registered_doctor(clinic_id, data["doctor_id"])
    return await _insert("dx_signatories", clinic_id, data)


async def update_signatory(clinic_id: str, signatory_id: str, data: dict) -> dict:
    return await _update("dx_signatories", clinic_id, signatory_id, data, "Signatory")


async def store_signature(clinic_id: str, signatory_id: str, raw: bytes) -> dict:
    """PNG only, <= 300 KB, content-addressed under the clinic's prefix."""
    await _one(scoped_query("dx_signatories", clinic_id, "id").eq("id", signatory_id).limit(1), "Signatory")
    if not raw or len(raw) > SIGNATURE_MAX_BYTES:
        raise HTTPException(status_code=413 if raw else 400, detail="The signature must be a PNG of at most 300 KB.")
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=415, detail="The signature must be a PNG image.")
    path = f"{clinic_id}/dx-assets/signatures/{hashlib.sha256(raw).hexdigest()}.png"
    try:
        await asyncio.to_thread(supabase.storage.from_(SIGNATURE_BUCKET).upload, path, raw,
                                {"content-type": "image/png", "upsert": "true"})
    except Exception:
        raise HTTPException(status_code=502, detail="The signature could not be stored. Please retry.")
    return await _update("dx_signatories", clinic_id, signatory_id, {"signature_asset_path": path}, "Signatory")


# ── test master ─────────────────────────────────────────────────────────────

async def list_tests(clinic_id: str, branch_id: Optional[str], kind: Optional[str] = None,
                     department_id: Optional[str] = None, q: Optional[str] = None,
                     approved: Optional[bool] = None) -> list[dict]:
    query = restrict_to_branch(scoped_query("lab_tests", clinic_id, LIST_COLUMNS), branch_id)
    if kind:
        query = query.eq("kind", kind)
    if department_id:
        query = query.eq("department_id", department_id)
    if approved is not None:
        query = query.not_.is_("approved_at", "null") if approved else query.is_("approved_at", "null")
    rows = await _rows(query.order("name").limit(2000))
    if q:
        needle = q.strip().casefold()
        rows = [r for r in rows if needle in (r.get("name") or "").casefold()
                or needle in (r.get("test_code") or "").casefold()]
    return rows


async def get_test_row(clinic_id: str, test_id: str) -> dict:
    return await _one(scoped_query("lab_tests", clinic_id).eq("id", test_id).limit(1), "Test")


async def parameters_of(clinic_id: str, test_id: str) -> list[dict]:
    return await _rows(scoped_query("lab_test_parameters", clinic_id).eq("lab_test_id", test_id)
                       .order("sort_order").order("name").order("min_age_days").order("id"))


async def components_of(clinic_id: str, test_id: str) -> list[dict]:
    comps = await _rows(scoped_query("dx_test_components", clinic_id).eq("parent_test_id", test_id)
                        .order("sort_order"))
    ids = [c["child_test_id"] for c in comps]
    names = {t["id"]: t for t in await _rows(scoped_query("lab_tests", clinic_id, "id, name, test_code, kind, approved_at")
                                             .in_("id", ids))} if ids else {}
    return [{**c, "child": names.get(c["child_test_id"])} for c in comps]


async def digest_of(clinic_id: str, test_id: str) -> str:
    try:
        res = await sb(supabase.rpc("dx_test_digest", {"p_clinic_id": clinic_id, "p_test_id": test_id}))
    except Exception as e:
        raise_for(e)
    return res.data


def readiness(test: dict, params: list[dict], components: list[dict]) -> list[str]:
    """What blocks approval (REPORT-TEMPLATES §1 rules), in words a lab director acts on."""
    issues: list[str] = []
    active = [p for p in params if p.get("is_active")]
    if test.get("kind") == "test":
        if not test.get("department_id"):
            issues.append("Choose a department.")
        if not test.get("container_id") and test.get("dx_result_mode") == "parameters":
            issues.append("Choose a container.")
        if test.get("dx_result_mode") == "parameters" and not active:
            issues.append("Add at least one parameter.")
        formulas = {p["parameter_code"]: p["formula"] for p in active if p.get("formula") and p.get("parameter_code")}
        codes = {p["parameter_code"] for p in active if p.get("parameter_code")}
        for p in active:
            if p.get("formula") and not p.get("parameter_code"):
                issues.append(f"Give '{p['name']}' a code: it is calculated.")
        try:
            for code, formula in formulas.items():
                dx_formula.validate_formula(formula, codes)
            dx_formula.evaluation_order(formulas)
        except dx_formula.FormulaError as e:
            issues.append(f"Formula problem: {e} {e.token}".strip())
    elif not components:
        issues.append("Add at least one member test.")
    return issues


async def get_test(clinic_id: str, test_id: str) -> dict:
    test = await get_test_row(clinic_id, test_id)
    params = await parameters_of(clinic_id, test_id)
    comps = await components_of(clinic_id, test_id)
    return {"test": test, "parameters": params, "components": comps,
            "digest": await digest_of(clinic_id, test_id), "issues": readiness(test, params, comps)}


async def create_test(clinic_id: str, data: dict) -> dict:
    return await _insert("lab_tests", clinic_id, data)


async def update_test(clinic_id: str, test_id: str, data: dict) -> dict:
    return await _update("lab_tests", clinic_id, test_id, data, "Test")


# ── parameters ──────────────────────────────────────────────────────────────

async def _check_formulas(clinic_id: str, test_id: str, row: dict, param_id: Optional[str]) -> None:
    """A formula must parse, name only codes of this test, and not close a loop (refused at save)."""
    others = [p for p in await parameters_of(clinic_id, test_id) if p.get("is_active") and p["id"] != param_id]
    me = row if row.get("is_active", True) else {}
    codes = {p["parameter_code"] for p in others if p.get("parameter_code")} | (
        {me["parameter_code"]} if me.get("parameter_code") else set())
    formulas = {p["parameter_code"]: p["formula"] for p in others if p.get("formula") and p.get("parameter_code")}
    if me.get("formula"):
        if not me.get("parameter_code"):
            raise HTTPException(status_code=422, detail="A calculated parameter needs a code.")
        if me.get("data_type", "numeric") != "numeric":
            raise HTTPException(status_code=422, detail="Only numeric parameters can be calculated.")
        formulas[me["parameter_code"]] = me["formula"]
    try:
        for formula in formulas.values():
            dx_formula.validate_formula(formula, codes)
        dx_formula.evaluation_order(formulas)
    except dx_formula.FormulaError as e:
        raise HTTPException(status_code=422, detail={"code": e.code, "message": str(e), "token": e.token})


async def create_parameter(clinic_id: str, test_id: str, data: dict) -> dict:
    await get_test_row(clinic_id, test_id)
    await _check_formulas(clinic_id, test_id, data, None)
    return await _insert("lab_test_parameters", clinic_id, {**data, "lab_test_id": test_id})


async def update_parameter(clinic_id: str, test_id: str, param_id: str, data: dict) -> dict:
    current = await _one(scoped_query("lab_test_parameters", clinic_id).eq("lab_test_id", test_id)
                         .eq("id", param_id).limit(1), "Parameter")
    await _check_formulas(clinic_id, test_id, {**current, **data}, param_id)
    rows = await _rows(supabase.table("lab_test_parameters").update(_clean(data)).eq("clinic_id", clinic_id)
                       .eq("lab_test_id", test_id).eq("id", param_id))
    return rows[0]


# ── components ──────────────────────────────────────────────────────────────

async def add_component(clinic_id: str, test_id: str, child_test_id: str, sort_order: int) -> dict:
    return await _insert("dx_test_components", clinic_id, {"parent_test_id": test_id, "child_test_id": child_test_id,
                                                           "sort_order": sort_order})


async def remove_component(clinic_id: str, test_id: str, child_test_id: str) -> None:
    rows = await _rows(supabase.table("dx_test_components").delete().eq("clinic_id", clinic_id)
                       .eq("parent_test_id", test_id).eq("child_test_id", child_test_id))
    if not rows:
        raise HTTPException(status_code=404, detail="This test is not in the panel or package.")


# ── approval ────────────────────────────────────────────────────────────────

async def approve_test(clinic_id: str, test_id: str, digest: str, doctor_id: Optional[str],
                       actor_name: str) -> dict:
    """The lab director approves exactly the content they reviewed (digest CAS in SQL)."""
    if not doctor_id:
        raise HTTPException(status_code=403, detail="Approval needs a login linked to a doctor with a registration "
                                                    "number.")
    await _registered_doctor(clinic_id, doctor_id)
    view = await get_test(clinic_id, test_id)
    if view["digest"] != digest:
        raise HTTPException(status_code=409, detail={"code": "dx_test_changed",
                                                     "message": "The test changed since you reviewed it. Reload, "
                                                                "review again and approve."})
    if view["issues"]:
        raise HTTPException(status_code=409, detail={"code": "dx_test_not_ready", "message": view["issues"][0],
                                                     "issues": view["issues"]})
    try:
        res = await sb(supabase.rpc("dx_approve_test", {"p_clinic_id": clinic_id, "p_test_id": test_id,
                                                        "p_digest": digest, "p_doctor_id": doctor_id,
                                                        "p_actor_name": actor_name}))
    except Exception as e:
        raise_for(e)
    return res.data


# ── formula check (no write) ────────────────────────────────────────────────

def check_formula(formula: str, codes: list[str], inputs: dict, patient: dict, decimal_places: int,
                  rounding_mode: str) -> dict:
    try:
        deps = dx_formula.validate_formula(formula, codes) if codes else dx_formula.extract_dependencies(formula)
    except dx_formula.FormulaError as e:
        return {"valid": False, "error": str(e), "token": e.token, "dependencies": []}
    raw = dx_formula.evaluate_raw(formula, {**patient, **inputs}) if inputs or patient else None
    value = None if raw is None else dx_formula.round_value(raw, decimal_places, rounding_mode)
    return {"valid": True, "dependencies": deps, "raw": None if raw is None else str(raw),
            "value": None if value is None else str(value),
            "missing": [d for d in deps if d not in inputs]}
