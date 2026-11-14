"""Diagnostic OS result rules (REPORT-TEMPLATES.md §2) and the bench workflow (P6).

Pure rules: delta checks, auto-verification eligibility. Workflow (migration 130):
worksheet entry with calculated parameters, flags and deltas computed here (never
taken from the request), verification, approval (single / batch, human only),
critical calls and result attachments. State changes happen in SQL
(dx_record_results, dx_verify_result, dx_approve_results, dx_log_critical_call).
Zero-LLM (tests/test_dx_no_llm.py).
"""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase


def _dec(v) -> Optional[Decimal]:
    return None if v is None else (v if isinstance(v, Decimal) else Decimal(str(v)))


def delta_check(current_val, prev_val, delta_dir: Optional[str], delta_pct=None, delta_abs=None) -> Optional[dict]:
    """Compare a new value with the person's previous validated value of the same parameter.

    Returns None when no check applies (no previous value, no limit configured, no
    direction); otherwise {"delta_failed", "change_abs", "change_pct", "direction"}.
    A change counts only in the configured direction (increase | decrease | both);
    it fails when it exceeds the absolute limit or the percent limit (percent needs a
    non-zero previous value). Never blocks entry; it blocks batch approval and
    auto-verification only."""
    cur, prev = _dec(current_val), _dec(prev_val)
    pct_limit, abs_limit = _dec(delta_pct), _dec(delta_abs)
    if cur is None or prev is None or delta_dir not in ("increase", "decrease", "both") \
            or (pct_limit is None and abs_limit is None):
        return None
    change = cur - prev
    moved = (change > 0 if delta_dir == "increase" else change < 0 if delta_dir == "decrease" else change != 0)
    change_pct = None if prev == 0 else (abs(change) / abs(prev) * 100).quantize(Decimal("0.01"))
    failed = moved and ((abs_limit is not None and abs(change) > abs_limit)
                        or (pct_limit is not None and change_pct is not None and change_pct > pct_limit))
    return {"delta_failed": bool(failed), "change_abs": change, "change_pct": change_pct, "direction": delta_dir}


def auto_verify_eligible(result_values: list[dict]) -> bool:
    """True iff every value is 'normal' (none not_evaluated, low/high, critical or abnormal),
    none failed a delta check, none carries a free-text remark and none is an amendment.
    An empty result is never auto-verified."""
    if not result_values:
        return False
    return all(v.get("flag") == "normal" and not v.get("delta_failed") and not (v.get("remark") or "").strip()
               and not v.get("is_amendment") for v in result_values)


# ── workflow (migration 130, DX-38) ─────────────────────────────────────────

ATTACHMENT_BUCKET = "lab-reports"     # private; DX assets live under <clinic>/dx-assets/
ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024
_SNIFF = {b"%PDF": ("pdf", "application/pdf"), b"\x89PNG": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg")}


async def _call(name: str, params: dict):
    """rpc with the DX error map first (dx_* codes), then the LIS one (lis_* codes)."""
    from app.services import dx, lis
    try:
        return (await sb(supabase.rpc(name, params))).data
    except Exception as e:  # noqa: BLE001 - mapped, never shown raw
        msg = str(getattr(e, "message", None) or e)
        if any(code in msg for code in dx._ERRORS):
            dx.raise_for(e)
        lis.raise_for(e)


def item_error(text: str) -> dict:
    """{code, message} for one refused approval item (dx_* or lis_* code in the text)."""
    from app.services import dx, lis
    for table in (dx._ERRORS, lis._ERRORS):
        for code in sorted(table, key=len, reverse=True):
            if code in (text or ""):
                return {"code": code, "message": table[code][1]}
    return {"code": "dx_approve_failed", "message": "This result could not be signed. Reload and try again."}


async def _rows(query) -> list[dict]:
    return list((await sb(query)).data or [])


async def _context(clinic_id: str, order_id: str) -> tuple[dict, dict, list[dict]]:
    """The DX order, the person tested, and the worksheet lines (band chosen for this patient)."""
    from app.services import lis
    order = await lis.get_lab_order(clinic_id, order_id)
    if not order.get("accession_id"):            # source 'dx', or an OPD / IPD order adopted onto the bench (P6b)
        raise HTTPException(status_code=409, detail={"code": "dx_not_dx_order",
                                                     "message": "This is not a Diagnostic OS order."})
    subject = await lis.subject_of(clinic_id, order)
    lines = lis._worksheet_lines(await lis._parameter_rows(clinic_id, order["catalog_reference_id"]), subject)
    if not lines:
        raise HTTPException(status_code=409, detail="This test has no parameters defined. Set them up first.")
    return order, subject, lines


async def previous_values(clinic_id: str, order: dict, lines: list[dict]) -> dict[str, dict]:
    """The person's latest validated value per parameter name (other orders of the same test),
    within each parameter's delta lookback: {name_key: {"value", "at"}}."""
    q = scoped_query("lab_results", clinic_id, "id, validated_at").eq("patient_id", order["patient_id"]) \
        .eq("lab_test_id", order["catalog_reference_id"]).eq("status", "validated").neq("order_id", order["id"])
    q = q.eq("family_member_id", order["family_member_id"]) if order.get("family_member_id") \
        else q.is_("family_member_id", "null")
    results = await _rows(q.order("validated_at", desc=True).limit(10))
    if not results:
        return {}
    at = {r["id"]: r["validated_at"] for r in results}
    vals = await _rows(scoped_query("lab_result_values", clinic_id, "result_id, parameter_name, numeric_value, text_value")
                       .in_("result_id", list(at)))
    lookback = {ln["name"].strip().casefold(): int(ln.get("delta_lookback_days") or 30) for ln in lines}
    now = datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for v in sorted(vals, key=lambda v: at[v["result_id"]] or "", reverse=True):
        key = v["parameter_name"].strip().casefold()
        when = datetime.fromisoformat(str(at[v["result_id"]]).replace("Z", "+00:00"))
        if key in out or key not in lookback or when < now - timedelta(days=lookback[key]):
            continue
        out[key] = {"value": v["numeric_value"] if v.get("numeric_value") is not None else v.get("text_value"),
                    "at": at[v["result_id"]]}
    return out


def _show(v) -> str:
    """A previous value for display: numbers without trailing zeros, text as stored."""
    from app.services import lis
    try:
        return lis._fmt(v)
    except (ArithmeticError, ValueError, TypeError):
        return str(v)


def evaluate_lines(lines: list[dict], values: list[dict], subject: dict, previous: dict[str, dict]) -> list[dict]:
    """Entered values parsed, calculated parameters recomputed (unrounded values feed later
    formulas), flags from the patient's band, delta against the previous validated value.
    Returns one row per line that has a value, in print order."""
    from app.services import lis
    from app.services.dx_formula import compute_all
    by_id = {ln["parameter_id"]: ln for ln in lines}
    entered: dict[str, Any] = {}
    seen: set[str] = set()
    for item in values:
        pid = str(item.get("parameter_id") or "")
        line = by_id.get(pid)
        if not line:
            raise HTTPException(status_code=422, detail="A value names a parameter that is not on this worksheet. "
                                                        "Reload it.")
        if pid in seen:
            raise HTTPException(status_code=422, detail=f"{line['name']} is entered twice.")
        seen.add(pid)
        if line.get("formula") or item.get("value") is None or str(item.get("value")).strip() == "":
            continue      # calculated lines are computed below; blanks are left off the report
        entered[pid] = (lis._parse_value(line, item["value"]), str(item.get("remarks") or "").strip()[:500] or None)
    inputs = {by_id[pid]["parameter_code"]: num for pid, ((num, _), _) in entered.items()
              if by_id[pid].get("parameter_code") and num is not None}
    ctx = {}
    if subject.get("age_years") is not None:
        ctx["AGE_YEARS"] = Decimal(int(subject["age_years"]))
    if subject.get("age_days") is not None:
        ctx["AGE_DAYS"] = Decimal(int(subject["age_days"]))
    if subject.get("gender") in ("male", "female"):
        ctx["IS_FEMALE"], ctx["IS_MALE"] = (Decimal(1), Decimal(0)) if subject["gender"] == "female" else (Decimal(0), Decimal(1))
    calc = compute_all([{"code": ln.get("parameter_code"), "formula": ln.get("formula"),
                         "decimal_places": ln.get("decimal_places", 2), "rounding_mode": ln.get("rounding_mode")}
                        for ln in lines], inputs, ctx)
    out = []
    for ln in lines:
        pid = ln["parameter_id"]
        if ln.get("formula"):
            value = (calc.get(ln.get("parameter_code")) or {}).get("value")
            if value is None:
                continue          # not computable yet (an input is missing)
            num, text, remarks = value.quantize(Decimal("0.001")), None, None
        elif pid in entered:
            (num, text), remarks = entered[pid]
        else:
            continue
        flag = lis.flag_value(ln if ln["range_applies"] else None, num, text)
        prev = previous.get(ln["name"].strip().casefold())
        delta = delta_check(num, prev and prev["value"], ln.get("delta_direction"), ln.get("delta_limit_percent"),
                            ln.get("delta_limit_abs")) if num is not None and prev else None
        out.append({"parameter_id": pid, "parameter_name": ln["name"], "unit": ln.get("unit"),
                    "sort_order": ln["sort_order"], "numeric_value": str(num) if num is not None else None,
                    "text_value": text, "flag": flag, "reference_range_display": ln["reference_range_display"],
                    "remarks": remarks, "is_calculated": bool(ln.get("formula")),
                    "formula_snapshot": ln.get("formula"),
                    "previous_value": None if not prev else _show(prev["value"]),
                    "previous_at": None if not prev else prev["at"],
                    "delta_pct": None if not delta or delta["change_pct"] is None else str(delta["change_pct"]),
                    "delta_failed": bool(delta and delta["delta_failed"])})
    return out


async def enter_worksheet_results(clinic_id: str, order_id: str, values: list[dict], actor,
                                  amendment_reason: Optional[str] = None, preview: bool = False) -> dict:
    """W8 steps 2–3. preview=True evaluates without saving (live calculated values and flags)."""
    from app.services import lis
    from app.services.dx import _actor
    order, subject, lines = await _context(clinic_id, order_id)
    rows = evaluate_lines(lines, values, subject, await previous_values(clinic_id, order, lines))
    critical = any(r["flag"].startswith("critical") for r in rows)
    if preview:
        return {"values": rows, "has_critical": critical, "saved": False}
    if not rows:
        raise HTTPException(status_code=422, detail="Enter at least one parameter value.")
    aid, name = _actor(actor)
    lis_keys = ("parameter_id", "parameter_name", "unit", "sort_order", "numeric_value", "text_value", "flag",
                "reference_range_display", "remarks")
    meta_keys = ("parameter_id", "is_calculated", "formula_snapshot", "previous_value", "previous_at", "delta_pct",
                 "delta_failed")
    result = await _call("dx_record_results", {
        "p_clinic_id": clinic_id, "p_order_id": order_id, "p_values": [{k: r[k] for k in lis_keys} for r in rows],
        "p_meta": [{**{k: r[k] for k in meta_keys}, "source": "calculated" if r["is_calculated"] else "manual"}
                   for r in rows],
        "p_has_critical": critical, "p_amendment_reason": (amendment_reason or "").strip() or None,
        "p_actor_id": aid, "p_actor_name": name})
    return {**result, "values": rows, "saved": True, "stored_values": await lis._values_of(clinic_id, result["id"])}


async def result_detail(clinic_id: str, result_id: str) -> dict:
    """A result with its values, value meta, verifications, critical events and attachments."""
    from app.services import lis
    r = await lis.get_result_detail(clinic_id, result_id)

    async def of(table: str, cols: str = "*", order: str = "created_at") -> list[dict]:
        return await _rows(scoped_query(table, clinic_id, cols).eq("result_id", result_id).order(order))
    r["meta"] = await of("dx_result_value_meta")
    r["verifications"] = await of("dx_result_verifications")
    r["critical_events"] = await of("dx_critical_events")
    r["attachments"] = await of("dx_result_attachments", "id, kind, file_name, caption, sha256, print_on_report, "
                                                         "created_at")
    events = r["critical_events"]
    last_detect = max((e["created_at"] for e in events if not e.get("notified_at")), default=None)
    r["critical_pending"] = bool(r.get("has_critical_value") and last_detect and not any(
        e.get("notified_at") and e.get("read_back_confirmed") and e["created_at"] > last_detect for e in events))
    return r


async def worksheet(clinic_id: str, order_id: str) -> dict:
    """The LIS worksheet plus the DX layer: calculated lines, previous values, open result detail."""
    from app.services import lis
    order, _, lines = await _context(clinic_id, order_id)
    ws = await lis.get_worksheet(clinic_id, order_id)
    prev = await previous_values(clinic_id, order, lines)
    for ln in ws["parameters"]:
        ln["is_calculated"] = bool(ln.get("formula"))
        ln["previous"] = prev.get(ln["name"].strip().casefold())
    if ws.get("result"):
        ws["result"] = await result_detail(clinic_id, ws["result"]["id"])
    return ws


async def verify_result(clinic_id: str, result_id: str, expected_updated_at: str, decision: str,
                        comment: Optional[str], actor) -> dict:
    from app.services.dx import _actor
    aid, name = _actor(actor)
    return await _call("dx_verify_result", {"p_clinic_id": clinic_id, "p_result_id": result_id,
                                            "p_expected_updated_at": expected_updated_at, "p_decision": decision,
                                            "p_comment": comment, "p_actor_id": aid, "p_actor_name": name})


async def _signer(clinic_id: str, actor) -> dict:
    """Doctor-linked login with a registration number (lis rule), plus the DX signatory's
    designation / qualification / signature image when the doctor is one."""
    from app.services import lis
    snap = await lis._signer_snapshot(clinic_id, actor)
    sig = await _rows(scoped_query("dx_signatories", clinic_id, "designation, qualification_text, signature_asset_path")
                      .eq("doctor_id", snap["doctor_id"]).eq("is_active", True).limit(1))
    return {**snap, **(sig[0] if sig else {})}


async def approve_results(clinic_id: str, items: list[dict], actor, is_batch: bool = False) -> dict:
    """W8 step 5 / W9. Each item is signed or refused on its own; never automatic."""
    from app.services import lis, orders
    from app.services.dx import _actor
    if not 1 <= len(items) <= 50:
        raise HTTPException(status_code=422, detail="Approve between 1 and 50 results at a time.")
    signer = await _signer(clinic_id, actor)
    letterheads: dict = {}
    payload = []
    for it in items:
        try:
            r = await lis.get_result(clinic_id, it["result_id"])
            o = await orders.get_order(clinic_id, r["order_id"])
        except HTTPException:
            payload.append({"result_id": it["result_id"], "expected_updated_at": it["expected_updated_at"]})
            continue          # the SQL answers lis_result_not_found for this item
        subject = await lis.subject_of(clinic_id, o)
        visit = (await _rows(scoped_query("dx_accessions", clinic_id, "accession_number, referrer_snapshot")
                             .eq("id", o.get("accession_id")).limit(1))) or [{}]
        if r.get("branch_id") not in letterheads:
            letterheads[r.get("branch_id")] = await lis.letterhead_snapshot(clinic_id, r.get("branch_id"))
        payload.append({"result_id": r["id"], "expected_updated_at": it["expected_updated_at"],
                        "pathologist_snapshot": signer, "letterhead_snapshot": letterheads[r.get("branch_id")],
                        "patient_snapshot": {"name": subject["name"], "mrn": subject["mrn"], "gender": subject["gender"],
                                             "age_years": subject["age_years"], "source": "dx",
                                             "accession_number": visit[0].get("accession_number"),
                                             "referred_by": (visit[0].get("referrer_snapshot") or {}).get("name"),
                                             "sample_type": o.get("sample_type"),
                                             "specimen_barcode": o.get("specimen_barcode"),
                                             "sample_collected_at": o.get("sample_collected_at")}})
    aid, name = _actor(actor)
    out = await _call("dx_approve_results", {"p_clinic_id": clinic_id, "p_items": payload, "p_actor_id": aid,
                                             "p_actor_name": signer.get("name") or name, "p_batch": is_batch})
    for i in out["items"]:
        if not i["ok"]:
            i.update(item_error(i.pop("error", "")))
    return out


async def log_critical_call(clinic_id: str, event_id: str, notified_to: str, notified_role: str, channel: str,
                            read_back_confirmed: bool, notes: Optional[str], actor) -> dict:
    from app.services.dx import _actor
    aid, name = _actor(actor)
    return await _call("dx_log_critical_call", {
        "p_clinic_id": clinic_id, "p_event_id": event_id, "p_notified_to": notified_to,
        "p_notified_role": notified_role, "p_channel": channel, "p_read_back": read_back_confirmed,
        "p_notes": notes, "p_actor_id": aid, "p_actor_name": name})


async def store_attachment(clinic_id: str, result_id: str, raw: bytes, file_name: str, kind: str,
                           caption: Optional[str], actor) -> dict:
    """A graph / image / partner PDF on an open result (the trigger refuses a signed one)."""
    from app.services.dx import _actor, raise_for
    if not raw or len(raw) > ATTACHMENT_MAX_BYTES:
        raise HTTPException(status_code=413 if raw else 400, detail="Attach a file of at most 5 MB.")
    sniffed = next((v for magic, v in _SNIFF.items() if raw.startswith(magic)), None)
    if not sniffed:
        raise HTTPException(status_code=415, detail="Attach a PDF, PNG or JPEG file.")
    if (kind == "pdf") != (sniffed[0] == "pdf"):
        raise HTTPException(status_code=415, detail="A 'pdf' attachment must be a PDF; a graph or image a PNG or JPEG.")
    sha = hashlib.sha256(raw).hexdigest()
    path = f"{clinic_id}/dx-assets/results/{result_id}/{sha}.{sniffed[0]}"
    rows = await _rows(scoped_query("lab_results", clinic_id, "id, status").eq("id", result_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Result not found.")
    if rows[0]["status"] not in ("draft", "entered"):
        raise_for(Exception("dx_attachment_locked"))
    try:
        await asyncio.to_thread(supabase.storage.from_(ATTACHMENT_BUCKET).upload, path, raw,
                                {"content-type": sniffed[1], "upsert": "true"})
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="The file could not be stored. Please retry.")
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("dx_result_attachments").insert({
            "clinic_id": clinic_id, "result_id": result_id, "kind": kind, "storage_path": path,
            "file_name": (file_name or "attachment")[:200], "caption": (caption or "").strip()[:200] or None,
            "sha256": sha, "uploaded_by": _actor(actor)[0]}))
    except Exception as e:  # noqa: BLE001
        raise_for(e)
    return res.data[0]


async def attachment_file(clinic_id: str, result_id: str, attachment_id: str) -> tuple[bytes, dict]:
    rows = await _rows(scoped_query("dx_result_attachments", clinic_id).eq("id", attachment_id)
                       .eq("result_id", result_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    try:
        data = await asyncio.to_thread(supabase.storage.from_(ATTACHMENT_BUCKET).download, rows[0]["storage_path"])
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="The file could not be read. Please retry.")
    return data, rows[0]
