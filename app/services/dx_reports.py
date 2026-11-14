"""Diagnostic OS releases and delivery (DX-45) — DELIVERY-POLICY.md is the rulebook. Zero-LLM.

  create_release   dx_create_release (SQL: snapshots, verify code, one delivery per target,
                   first decision) -> render + store in the background -> send what is queued
  send_delivery    THE ONLY caller of LabReportService.upload_and_send with source="dx"
                   (tests/test_dx_no_llm.py::test_dx_single_sender). It claims the row with
                   dx_claim_delivery (queued -> sending, decision re-run in SQL) first.
  overrides, desk print, staff view, share links, public verify / unlock, the outbox job.

"No payment, no report" and "payer-routed visits never go to the patient" are decided by
dx_delivery_decision in the database; nothing here re-implements them. Rule 9 (the plan
includes WhatsApp lab reports) is checked here, after the claim.
"""

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
from typing import Optional

from fastapi import HTTPException

from app.config import settings
from app.database import sb, scoped_query, supabase
from app.services import dx_templates
from app.services.dx_pdf import render_visit_report
from app.utils.async_tasks import spawn_background_task

logger = logging.getLogger(__name__)

BUCKET = "lab-reports"                 # the existing private lab-report bucket (SECURITY §7)
WHATSAPP_TARGETS = ("patient", "referrer", "account")
DELIVERY_COLUMNS = ("id, release_id, accession_id, target, recipient_phone, external_report_id, status, reason, "
                    "override_id, lab_report_id, attempts, decided_at, sent_at, created_at, updated_at")
RELEASE_COLUMNS = ("id, accession_id, release_no, kind, verify_code, status, superseded_by, pdf_sha256, page_count, "
                   "routing_snapshot, created_by_name, created_at")
HELD_TEXT = {"held_dues": "Unpaid dues", "held_shadow": "Shadow mode (branch not live on DX delivery)",
             "owner_connector": "Delivered by the connector for this branch", "payer_routed": "Payer-routed visit",
             "account_rule": "The account's delivery rule", "no_patient_phone": "No patient phone of their own",
             "no_referrer_consent": "No referrer consent / phone", "release_superseded": "Replaced by a newer report",
             "plan_no_delivery": "Plan has no WhatsApp report delivery"}


def verify_url(code: str) -> str:
    return f"{settings.medassist_url.rstrip('/')}/public/dx/verify/{code}"


def share_url(token: str) -> str:
    return f"{settings.medassist_url.rstrip('/')}/public/dx/r/{token}"


async def _rows(query) -> list[dict]:
    return list((await sb(query)).data or [])


async def _rpc(name: str, params: dict):
    from app.services.dx import rpc
    return await rpc(name, params)


def _actor(user) -> tuple[Optional[str], str]:
    from app.services.dx import _actor as actor
    return actor(user)


async def _in(table: str, clinic_id: str, cols: str, column: str, ids) -> list[dict]:
    ids = sorted({str(i) for i in ids if i})
    out: list[dict] = []
    for i in range(0, len(ids), 100):
        out += await _rows(scoped_query(table, clinic_id, cols).in_(column, ids[i:i + 100]))
    return out


async def _download(path: str) -> bytes:
    return await asyncio.to_thread(supabase.storage.from_(BUCKET).download, path)


async def get_release(clinic_id: str, release_id: str) -> dict:
    rows = await _rows(scoped_query("dx_report_releases", clinic_id).eq("id", release_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Report release not found.")
    return rows[0]


async def get_delivery(clinic_id: str, delivery_id: str) -> dict:
    rows = await _rows(scoped_query("dx_release_deliveries", clinic_id, DELIVERY_COLUMNS).eq("id", delivery_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Delivery not found.")
    return rows[0]


async def decision(clinic_id: str, release_id: str, target: str) -> tuple[bool, Optional[str]]:
    rows = await _rpc("dx_delivery_decision", {"p_clinic_id": clinic_id, "p_release_id": release_id, "p_target": target})
    row = (rows or [{}])[0] if isinstance(rows, list) else rows or {}
    return bool(row.get("allowed")), row.get("reason")


# ── render inputs (from the release's snapshots and its signed results only) ─

def _fmt(value, decimals) -> Optional[str]:
    from app.services import lis
    if value is None:
        return None
    try:
        from decimal import ROUND_HALF_UP, Decimal
        if decimals is not None:
            return str(Decimal(str(value)).quantize(Decimal(1).scaleb(-int(decimals)), rounding=ROUND_HALF_UP))
        return lis._fmt(value)
    except Exception:  # noqa: BLE001
        return str(value)


async def render_inputs(clinic_id: str, release: dict) -> tuple[dict, list[dict], dict, dict]:
    """(release_dict, results, visit_dict, layout) for dx_pdf.render_visit_report."""
    links = await _rows(scoped_query("dx_release_results", clinic_id, "result_id, order_id")
                        .eq("release_id", release["id"]))
    rids = [r["result_id"] for r in links]
    results = {r["id"]: r for r in await _in("lab_results", clinic_id, "id, order_id, test_name, version, lab_test_id, "
                                             "pathologist_snapshot, validated_at, amendment_reason", "id", rids)}
    values = await _in("lab_result_values", clinic_id, "result_id, parameter_id, parameter_name, unit, sort_order, "
                       "numeric_value, text_value, flag, reference_range_display, remarks", "result_id", rids)
    orders = {o["id"]: o for o in await _in("opd_orders", clinic_id, "id, accession_item_id, outsource_partner_id, "
                                            "dx_sample_id", "id", [r["order_id"] for r in links])}
    tests = {t["id"]: t for t in await _in("lab_tests", clinic_id, "id, name, print_title, interpretation, "
                                           "report_notes, department_id", "id",
                                           [r["lab_test_id"] for r in results.values()])}
    params = {p["id"]: p for p in await _in("lab_test_parameters", clinic_id, "id, method, reference_text, "
                                            "decimal_places", "id", [v["parameter_id"] for v in values])}
    depts = {d["id"]: d for d in await _in("dx_departments", clinic_id, "id, code, name, report_heading, sort_order",
                                           "id", [t.get("department_id") for t in tests.values()])}
    meta = {(m["result_id"], m["parameter_id"]): m for m in await _in(
        "dx_result_value_meta", clinic_id, "result_id, parameter_id, previous_value, previous_at", "result_id", rids)}
    items = {i["id"]: i for i in await _in("dx_accession_items", clinic_id, "id, kind, item_name", "id",
                                           [o.get("accession_item_id") for o in orders.values()])}
    atts = await _in("dx_result_attachments", clinic_id, "result_id, kind, storage_path, caption, created_at",
                     "result_id", rids)
    partners = (release.get("letterhead_snapshot") or {}).get("partners") or {}
    signatures: dict[str, Optional[bytes]] = {}

    async def signature(path: Optional[str]) -> Optional[bytes]:
        if not path or not path.startswith(f"{clinic_id}/"):
            return None
        if path not in signatures:
            try:
                signatures[path] = await _download(path)
            except Exception:  # noqa: BLE001 - a missing image prints the name only
                signatures[path] = None
        return signatures[path]

    out = []
    for link in links:
        r = results.get(link["result_id"]) or {}
        o = orders.get(link["order_id"]) or {}
        t = tests.get(r.get("lab_test_id")) or {}
        d = depts.get(t.get("department_id")) or {}
        item = items.get(o.get("accession_item_id")) or {}
        vals = sorted((v for v in values if v["result_id"] == r.get("id")), key=lambda v: (v["sort_order"], v["parameter_name"]))
        snap = r.get("pathologist_snapshot") or {}
        files = []
        for a in sorted((a for a in atts if a["result_id"] == r.get("id")), key=lambda a: a["created_at"]):
            try:
                files.append({"kind": a["kind"], "caption": a.get("caption"), "data": await _download(a["storage_path"])})
            except Exception:  # noqa: BLE001
                continue
        out.append({
            "department": {"code": d.get("code"), "heading": d.get("report_heading") or d.get("name"),
                           "sort_order": d.get("sort_order") or 0},
            "test_name": r.get("test_name"), "print_title": t.get("print_title") or r.get("test_name"),
            "package_name": item.get("item_name") if item.get("kind") in ("panel", "package") else None,
            "outsourced": partners.get(str(o.get("outsource_partner_id"))) if o.get("outsource_partner_id") else None,
            "interpretation": t.get("interpretation"), "notes": t.get("report_notes"), "attachments": files,
            "signer": {"name": snap.get("name"), "qualification": snap.get("qualification_text") or snap.get("qualifications"),
                       "registration_number": snap.get("registration_number"),
                       "registration_council": snap.get("registration_council"),
                       "signature_png": await signature(snap.get("signature_asset_path"))},
            "values": [{"name": v["parameter_name"], "unit": v.get("unit"), "flag": v.get("flag"),
                        "value": v.get("text_value") if v.get("numeric_value") is None else
                        _fmt(v["numeric_value"], (params.get(v["parameter_id"]) or {}).get("decimal_places")),
                        "method": (params.get(v["parameter_id"]) or {}).get("method"),
                        "reference_text": (params.get(v["parameter_id"]) or {}).get("reference_text"),
                        "reference_range_display": v.get("reference_range_display"),
                        "previous_value": (meta.get((r.get("id"), v["parameter_id"])) or {}).get("previous_value"),
                        "previous_at": (meta.get((r.get("id"), v["parameter_id"])) or {}).get("previous_at")}
                       for v in vals]})

    visit = (await _rows(scoped_query("dx_accessions", clinic_id).eq("id", release["accession_id"]).limit(1)))[0]
    samples = await _in("dx_samples", clinic_id, "id, barcode, collected_at, received_at", "id",
                        [o.get("dx_sample_id") for o in orders.values()])
    bills = await _rows(scoped_query("opd_invoices", clinic_id, "invoice_number, status")
                        .eq("accession_id", visit["id"]).neq("status", "void").order("created_at"))
    snap = visit.get("patient_snapshot") or {}
    from app.services.dx_barcode import age_sex
    ref = visit.get("referrer_snapshot") or {}
    route = release.get("routing_snapshot") or {}
    times = lambda k: sorted(s[k] for s in samples if s.get(k))  # noqa: E731
    visit_dict = {"accession_number": visit.get("accession_number"),
                  "patient": {"name": snap.get("name"), "age_sex": age_sex(snap.get("age_years"), snap.get("gender")),
                              "mrn": snap.get("mrn")},
                  "referrer": ", ".join(x for x in (ref.get("name"), ref.get("qualification")) if x) or None,
                  "external_ref": visit.get("external_ref"), "provider": (route.get("account") or {}).get("name"),
                  "bill_no": ", ".join(b["invoice_number"] for b in bills if b.get("invoice_number")) or None,
                  "collected_at": (times("collected_at") or [None])[0], "received_at": (times("received_at") or [None])[0],
                  "reported_at": release["created_at"],
                  "sample_ids": sorted({s["barcode"] for s in samples if s.get("barcode")})}
    replaces = None
    if release["kind"] == "amended":
        old = await _rows(scoped_query("dx_report_releases", clinic_id, "release_no, created_at")
                          .eq("superseded_by", release["id"]).order("release_no", desc=True).limit(1))
        replaces = old[0] if old else None
    layout = dx_templates.snapshot_layout(release.get("template_snapshot") or {})
    rel = {"id": release["id"], "release_no": release["release_no"], "kind": release["kind"],
           "verify_code": release["verify_code"], "verify_url": verify_url(release["verify_code"]),
           "created_at": release["created_at"], "letterhead_snapshot": release.get("letterhead_snapshot") or {},
           "routing_snapshot": route, "replaces": replaces, "watermark": None,
           "assets": await dx_templates.load_assets(clinic_id, layout)}
    return rel, out, visit_dict, layout


async def render_release(clinic_id: str, release: dict, watermark: Optional[str] = None) -> tuple[bytes, str, int]:
    rel, results, visit, layout = await render_inputs(clinic_id, release)
    rel["watermark"] = watermark
    return await asyncio.to_thread(render_visit_report, rel, results, visit, layout)


# ── releases ─────────────────────────────────────────────────────────────────

async def create_release(clinic_id: str, accession_id: str, result_ids: Optional[list[str]], kind: Optional[str],
                         actor, background: bool = True) -> dict:
    aid, name = _actor(actor)
    rel = await _rpc("dx_create_release", {"p_clinic_id": clinic_id, "p_accession_id": accession_id,
                                           "p_result_ids": result_ids, "p_kind": kind, "p_actor_id": aid,
                                           "p_actor_name": name})
    job = finish_release(clinic_id, rel["id"])
    if background:
        spawn_background_task(job, name=f"dx_release_{rel['id']}")
    else:
        await job
    return rel


async def finish_release(clinic_id: str, release_id: str) -> None:
    """Render from the snapshots, store content-addressed, then send what the policy allows.
    Never raises: the release row is committed; a failed render is retried from the panel."""
    try:
        release = await get_release(clinic_id, release_id)
        if release["status"] == "rendering" or release["status"] == "render_failed":
            try:
                pdf, sha, pages = await render_release(clinic_id, release,
                                                       "partial" if release["kind"] == "partial" else None)
                path = f"{clinic_id}/releases/{sha}.pdf"
                await asyncio.to_thread(supabase.storage.from_(BUCKET).upload, path, pdf,
                                        {"content-type": "application/pdf", "upsert": "true"})
                await _rpc("dx_set_release_render", {"p_clinic_id": clinic_id, "p_release_id": release_id, "p_ok": True,
                                                     "p_path": path, "p_sha256": sha, "p_pages": pages})
            except Exception as e:  # noqa: BLE001
                logger.error(f"DX_RENDER_FAILED release={release_id} type={type(e).__name__}")
                await _rpc("dx_set_release_render", {"p_clinic_id": clinic_id, "p_release_id": release_id,
                                                     "p_ok": False, "p_path": None, "p_sha256": None, "p_pages": None})
                return
        await reevaluate_visit(clinic_id, release["accession_id"])
    except Exception as e:  # noqa: BLE001
        logger.error(f"DX_RELEASE_FINISH_FAILED release={release_id} type={type(e).__name__}")


async def auto_release(clinic_id: str, clinic: dict, accession_ids: list[str], actor) -> list[dict]:
    """After approvals (W10 step 1): settings.auto_release 'when_complete' releases a visit whose every
    live test is signed; 'per_department' releases once a department's tests are all signed; 'off'
    never. Skipped while the branch is delivered by the connector (nothing leaves DX there)."""
    from app.services.dx import get_dx_settings
    st = get_dx_settings(clinic)
    mode = st.get("auto_release", "when_complete")
    if mode == "off":
        return []
    owners = st.get("delivery_owner") or {}
    out = []
    for vid in dict.fromkeys(accession_ids):
        visit = (await _rows(scoped_query("dx_accessions", clinic_id, "id, branch_id, status").eq("id", vid).limit(1)))
        if not visit or visit[0]["status"] == "cancelled" or owners.get(str(visit[0]["branch_id"]), "connector") == "connector":
            continue
        orders = await _rows(scoped_query("opd_orders", clinic_id, "id, catalog_reference_id, status")
                             .eq("accession_id", vid).neq("status", "cancelled"))
        signed = {r["order_id"] for r in await _in("lab_results", clinic_id, "order_id, status", "order_id",
                                                   [o["id"] for o in orders]) if r["status"] == "validated"}
        if mode == "when_complete":
            ready = bool(orders) and all(o["id"] in signed for o in orders)
        else:
            tests = {t["id"]: t.get("department_id") for t in await _in("lab_tests", clinic_id, "id, department_id",
                                                                         "id", [o["catalog_reference_id"] for o in orders])}
            by_dept: dict = {}
            for o in orders:
                by_dept.setdefault(tests.get(o["catalog_reference_id"]), []).append(o["id"] in signed)
            ready = any(all(v) for v in by_dept.values())
        if not ready:
            continue
        try:
            out.append(await create_release(clinic_id, vid, None, None, actor))
        except HTTPException as e:     # nothing new to release, visit closed: not an error here
            logger.info(f"DX_AUTO_RELEASE_SKIPPED visit={vid} code={(e.detail or {}).get('code') if isinstance(e.detail, dict) else e.detail}")
    return out


async def list_releases(clinic_id: str, accession_id: str) -> list[dict]:
    rels = await _rows(scoped_query("dx_report_releases", clinic_id, RELEASE_COLUMNS)
                       .eq("accession_id", accession_id).order("release_no"))
    dels = await _in("dx_release_deliveries", clinic_id, DELIVERY_COLUMNS, "release_id", [r["id"] for r in rels])
    for r in rels:
        r["deliveries"] = sorted((d for d in dels if d["release_id"] == r["id"]), key=lambda d: d["target"])
    return rels


async def visit_dues_paise(clinic_id: str, accession_id: str) -> int:
    """What the patient still owes on this visit (the Held tab), as the collection gate computes it."""
    return int(await _rpc("dx_visit_due", {"p_clinic_id": clinic_id, "p_accession_id": accession_id}) or 0)


# ── sending ──────────────────────────────────────────────────────────────────

async def reevaluate_visit(clinic_id: str, accession_id: str) -> list[dict]:
    ids = await _rpc("dx_reevaluate_deliveries", {"p_clinic_id": clinic_id, "p_accession_id": accession_id}) or []
    return [await send_delivery(clinic_id, d) for d in ids]


def _deferred(record: dict) -> Optional[str]:
    msg = str(record.get("error_message") or "").lower()
    if "daily report limit" in msg:
        return "deferred:daily_limit_reached"
    if "suspended" in msg:
        return "deferred:suspended"
    return None


async def send_delivery(clinic_id: str, delivery_id: str) -> dict:
    """Claim (CAS + policy re-check in SQL), then hand the stored PDF to the lab-report pipeline."""
    from app.services.lab_reports import LabReportService
    from app.services.tenant import get_clinic_by_id, has_feature
    claimed = await _rpc("dx_claim_delivery", {"p_clinic_id": clinic_id, "p_delivery_id": delivery_id})
    if not claimed:
        return await get_delivery(clinic_id, delivery_id)

    async def mark(ok: bool, report_id: Optional[str] = None, error: Optional[str] = None) -> dict:
        return await _rpc("dx_mark_delivery_result", {"p_clinic_id": clinic_id, "p_delivery_id": delivery_id,
                                                      "p_success": ok, "p_lab_report_id": report_id, "p_error": error})
    try:
        d = await get_delivery(clinic_id, delivery_id)
        clinic = await get_clinic_by_id(clinic_id)
        if not has_feature(clinic, "lab_reports"):                       # DELIVERY-POLICY rule 9
            return await mark(False, error="blocked:plan_no_delivery")
        if not d.get("recipient_phone"):
            return await mark(False, error="no_recipient_phone")
        release = await get_release(clinic_id, d["release_id"])
        visit = (await _rows(scoped_query("dx_accessions", clinic_id, "id, accession_number, patient_id, "
                                          "patient_snapshot, referrer_snapshot").eq("id", d["accession_id"]).limit(1)))[0]
        pdf = await _download(release["pdf_storage_path"])
        patient = (visit.get("patient_snapshot") or {}).get("name") or "Patient"
        title = f"Lab report {visit['accession_number']} (R{release['release_no']})"
        if d["target"] == "referrer":
            title = f"Copy for {(visit.get('referrer_snapshot') or {}).get('name') or 'Doctor'}: {patient}, {title}"
        record = await LabReportService().upload_and_send(
            clinic_id=clinic_id, file_bytes=pdf, filename=f"{visit['accession_number']}_R{release['release_no']}.pdf",
            content_type="application/pdf", patient_phone=d["recipient_phone"], patient_name=patient,
            report_name=title, report_type="Laboratory", external_report_id=d["external_report_id"], source="dx",
            match_confidence=1.0, match_source={"patient": "dx_visit", "referrer": "dx_referrer",
                                                "account": "dx_account"}[d["target"]],
            matched_patient_id=str(visit["patient_id"]) if d["target"] == "patient" else None,
            ai_summary=d["target"] == "patient")
        record = record or {}
        status = str(record.get("delivery_status") or record.get("status") or "")
        if status in ("sent", "delivered", "read") or record.get("already_processed"):
            return await mark(True, record.get("id"))
        return await mark(False, record.get("id"), _deferred(record) or (record.get("error_message") or status or
                                                                          "send_failed")[:300])
    except Exception as e:  # noqa: BLE001 - the row must leave 'sending'
        logger.error(f"DX_SEND_FAILED delivery={delivery_id} type={type(e).__name__}")
        return await mark(False, error=f"send_failed:{type(e).__name__}")


async def resend(clinic_id: str, delivery_id: str) -> dict:
    """Manual resend / "send now": the table is re-checked; a held row stays held."""
    out = await _rpc("dx_requeue_delivery", {"p_clinic_id": clinic_id, "p_delivery_id": delivery_id})
    if out.get("pipeline_owned"):
        raise HTTPException(status_code=409, detail={"code": "dx_delivery_in_pipeline",
                                                     "message": "This report is already in the WhatsApp retry queue."})
    if out["status"] == "queued":
        spawn_background_task(send_delivery(clinic_id, delivery_id), name=f"dx_send_{delivery_id}")
    return out


async def create_override(clinic_id: str, accession_id: str, reason: str, actor,
                          expires_at: Optional[str] = None) -> dict:
    aid, name = _actor(actor)
    out = await _rpc("dx_create_delivery_override", {"p_clinic_id": clinic_id, "p_accession_id": accession_id,
                                                     "p_reason": reason, "p_expires_at": expires_at,
                                                     "p_actor_id": aid, "p_actor_name": name})
    for did in out.get("sendable") or []:
        spawn_background_task(send_delivery(clinic_id, did), name=f"dx_send_{did}")
    return out


async def list_deliveries(clinic_id: str, status: Optional[str], target: Optional[str], since: Optional[str],
                          accession_ids: Optional[list[str]] = None, limit: int = 200) -> list[dict]:
    q = scoped_query("dx_release_deliveries", clinic_id, DELIVERY_COLUMNS)
    if status:
        q = q.in_("status", status.split(","))
    if target:
        q = q.eq("target", target)
    if since:
        q = q.gte("created_at", since)
    if accession_ids is not None:
        q = q.in_("accession_id", accession_ids or [""])
    return await _rows(q.order("created_at", desc=True).limit(limit))


# ── PDFs for staff, desk print, share links ──────────────────────────────────

async def _stored_or_render(clinic_id: str, release: dict, watermark: Optional[str]) -> bytes:
    if release["status"] not in ("rendered", "superseded") or (release["status"] == "superseded" and not
                                                                release.get("pdf_storage_path")):
        raise HTTPException(status_code=409, detail={"code": "dx_release_not_ready",
                                                     "message": "The report PDF is not ready yet."})
    if watermark or not release.get("pdf_storage_path"):
        return (await render_release(clinic_id, release, watermark))[0]
    try:
        return await _download(release["pdf_storage_path"])
    except Exception:  # noqa: BLE001 - deterministic: the same bytes come back from the snapshots
        return (await render_release(clinic_id, release, "partial" if release["kind"] == "partial" else None))[0]


def _owner(clinic: dict, branch_id: str) -> str:
    from app.services.dx import get_dx_settings
    return (get_dx_settings(clinic).get("delivery_owner") or {}).get(str(branch_id), "connector")


async def staff_pdf(clinic_id: str, clinic: dict, release: dict) -> tuple[bytes, Optional[str]]:
    """Clinical viewing inside the panel (never gated; watermarked when a patient copy would be refused)."""
    mark = None
    if release["status"] == "superseded":
        mark = "superseded"
    else:
        ok, reason = await decision(clinic_id, release["id"], "print_patient")
        visit = (await _rows(scoped_query("dx_accessions", clinic_id, "branch_id").eq("id", release["accession_id"])
                             .limit(1)))[0]
        if not ok or _owner(clinic, visit["branch_id"]) == "shadow":
            mark = "shadow" if reason in (None, "held_shadow") and _owner(clinic, visit["branch_id"]) == "shadow" \
                else "internal"
    return await _stored_or_render(clinic_id, release, mark), mark


async def print_release(clinic_id: str, release: dict, actor) -> tuple[bytes, dict]:
    """Desk print for the patient: decision 'print_patient' (rule 7 refuses with dues unless an override)."""
    if release["status"] not in ("rendered",):
        raise HTTPException(status_code=409, detail={"code": "dx_release_not_ready",
                                                     "message": "The report PDF is not ready yet."})
    aid, name = _actor(actor)
    out = await _rpc("dx_log_print", {"p_clinic_id": clinic_id, "p_release_id": release["id"], "p_actor_id": aid,
                                      "p_actor_name": name})
    if not out.get("allowed"):
        raise HTTPException(status_code=409, detail={"code": out.get("reason"),
                                                     "message": HELD_TEXT.get(out.get("reason"), "Not allowed") +
                                                     ": this report cannot be printed for the patient."})
    pdf = await _stored_or_render(clinic_id, release, "shadow" if out.get("watermark") else None)
    return pdf, out


async def create_share_link(clinic_id: str, clinic: dict, release: dict, actor) -> dict:
    from app.services.dx import get_dx_settings
    aid, _ = _actor(actor)
    token = secrets.token_urlsafe(16)                                  # 128 bits
    out = await _rpc("dx_create_share_link", {"p_clinic_id": clinic_id, "p_release_id": release["id"],
                                              "p_token_hash": hashlib.sha256(token.encode()).hexdigest(),
                                              "p_ttl_hours": int(get_dx_settings(clinic).get("share_link_ttl_hours", 72)),
                                              "p_actor_id": aid})
    if not out.get("allowed"):
        raise HTTPException(status_code=409, detail={"code": out.get("reason"),
                                                     "message": HELD_TEXT.get(out.get("reason"), "Not allowed") +
                                                     ": a share link cannot be created."})
    return {"id": out["id"], "url": share_url(token), "expires_at": out["expires_at"]}


# ── public (no session; dx_public.py) ────────────────────────────────────────

def _initials(name: Optional[str]) -> str:
    words = [w for w in re.split(r"[\s.]+", name or "") if w and w.lower() not in ("mr", "mrs", "ms", "dr", "master",
                                                                                     "baby", "smt", "shri")]
    return ".".join(w[0].upper() for w in words[:3]) + ("." if words else "")


async def public_verify(code: str) -> Optional[dict]:
    """W10.6: lab, date, release no., tests, current / superseded, SHA-256, initials + age/sex. No values."""
    rows = await _rows(
        # unscoped: global_auth_lookup - the 50-bit verify code is globally unique and IS the lookup key
        supabase.table("dx_report_releases").select("id, clinic_id, accession_id, release_no, status, pdf_sha256, "
                                                    "created_at, letterhead_snapshot, routing_snapshot")
        .eq("verify_code", code).limit(1))
    if not rows:
        return None
    r = rows[0]
    cid = str(r["clinic_id"])
    names = await _rows(scoped_query("dx_release_results", cid, "result_id").eq("release_id", r["id"]))
    tests = await _in("lab_results", cid, "test_name", "id", [n["result_id"] for n in names])
    visit = (await _rows(scoped_query("dx_accessions", cid, "patient_snapshot").eq("id", r["accession_id"]).limit(1)))
    snap = (visit[0].get("patient_snapshot") if visit else {}) or {}
    from app.services.dx_barcode import age_sex
    letter = r.get("letterhead_snapshot") or {}
    site = letter.get("site") or {}
    white = (r.get("routing_snapshot") or {}).get("white_label")
    return {"lab": site.get("display_name") if white else ((letter.get("clinic") or {}).get("name") or
                                                            site.get("display_name")),
            "tested_at": site.get("legal_name") or site.get("display_name"),
            "release_no": r["release_no"], "released_at": r["created_at"],
            "status": "superseded" if r["status"] == "superseded" else "current",
            "tests": sorted({t["test_name"] for t in tests}), "sha256": r.get("pdf_sha256"),
            "patient": {"initials": _initials(snap.get("name")), "age_sex": age_sex(snap.get("age_years"),
                                                                                    snap.get("gender"))}}


async def public_unlock(token: str, phone_last4: str) -> tuple[str, Optional[bytes]]:
    """('ok', pdf) or (state, None): not_found / expired / locked / wrong / held. One attempt is
    consumed before the comparison; the policy is re-checked at unlock (a void after the link holds it)."""
    taken = await _rpc("dx_share_take_attempt", {"p_token_hash": hashlib.sha256(token.encode()).hexdigest()})
    if taken.get("state") != "ok":
        return taken.get("state") or "not_found", None
    digits = re.sub(r"\D", "", taken.get("phone") or "")
    if len(digits) < 4 or not hmac.compare_digest(digits[-4:].encode(), (phone_last4 or "").encode()):
        return "wrong", None
    cid = str(taken["clinic_id"])
    ok, _ = await decision(cid, str(taken["release_id"]), "share_link")
    if not ok:
        return "held", None
    release = await get_release(cid, str(taken["release_id"]))
    return "ok", await _stored_or_render(cid, release, None)


# ── scheduler: dx_outbox_dispatch (every minute, scheduler_locks) ─────────────

async def outbox_dispatch(limit: int = 200) -> int:
    """Payments and bill status changes enqueue a visit (triggers, migration 131); re-decide its
    held deliveries and send what is now allowed. Returns the number of visits processed."""
    pairs = await _rpc("dx_outbox_claim", {"p_limit": limit}) or []
    for p in pairs:
        try:
            await reevaluate_visit(str(p["clinic_id"]), str(p["accession_id"]))
        except Exception as e:  # noqa: BLE001 - one visit must not stop the batch
            logger.error(f"DX_OUTBOX_FAILED clinic={p['clinic_id']} type={type(e).__name__}")
    return len(pairs)


