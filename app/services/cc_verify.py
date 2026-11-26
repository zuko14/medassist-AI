"""Continuous Care — verification and outcomes (migration 149; ADR-CC-08, ADR-CC-15, docs/continuous-care/OUTCOMES.md).

"Counts only when recorded": an action is verified only by a check-in or a signed encounter (visit), a validated LIS
result (lab) or a completed pharmacy sale (refill). appointments.status='completed' is NEVER evidence — the 00:30 job
marks no-shows completed too (AUDIT A10). Revenue counts only paid invoice lines / completed sale lines of what CC
booked. Outcomes are cohort-based against the same patients' 90 days before enrollment; the report says "associated
with", never "caused by". Zero-LLM.
"""

import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.database import fetch_all, sb, scoped_query, supabase
from app.services.cc import _now, _rows
from app.utils.helpers import today_ist

logger = logging.getLogger(__name__)
METHOD = "outcomes-1"


async def evidence_for_visit(clinic_id: str, appointment_id: str) -> Optional[dict]:
    a = _rows(await sb(scoped_query("appointments", clinic_id, "id, checked_in_at, status").eq("id", appointment_id).limit(1)))
    if not a:
        return None
    enc = _rows(await sb(scoped_query("opd_encounters", clinic_id, "id, signed_at").eq("appointment_id", appointment_id)
                         .eq("status", "signed").limit(1)))
    if enc:
        return {"kind": "signed_encounter", "ref": {"table": "opd_encounters", "id": enc[0]["id"]}}
    if a[0].get("checked_in_at"):
        return {"kind": "check_in", "ref": {"table": "appointments", "id": appointment_id}}
    return None                                            # status='completed' alone is not evidence


async def visit_revenue(clinic_id: str, appointment_id: str) -> int:
    """The paid consultation invoice of that visit (issued bills only count once paid in full or in part)."""
    inv = _rows(await sb(scoped_query("opd_invoices", clinic_id, "id, status, paid_paise").eq("appointment_id", appointment_id)
                         .in_("status", ["paid", "partially_paid"])))
    return sum(int(i.get("paid_paise") or 0) for i in inv)


async def evidence_for_lab(clinic_id: str, patient_id: str, after: str) -> Optional[dict]:
    r = _rows(await sb(scoped_query("lab_results", clinic_id, "id, order_id, validated_at").eq("patient_id", patient_id)
                       .eq("status", "validated").gte("validated_at", after).order("validated_at").limit(1)))
    return {"kind": "validated_result", "ref": {"table": "lab_results", "id": r[0]["id"]}, "order_id": r[0]["order_id"]} if r else None


async def lab_revenue(clinic_id: str, order_id: Optional[str]) -> int:
    if not order_id:
        return 0
    lines = _rows(await sb(scoped_query("opd_invoice_items", clinic_id, "line_total_paise, invoice_id").eq("order_id", order_id)))
    if not lines:
        return 0
    inv = {i["id"]: i for i in _rows(await sb(scoped_query("opd_invoices", clinic_id, "id, status")
                                              .in_("id", [ln["invoice_id"] for ln in lines])))}
    return sum(int(ln["line_total_paise"] or 0) for ln in lines
               if (inv.get(ln["invoice_id"]) or {}).get("status") in ("paid", "partially_paid", "issued"))


async def run(clinic: dict, today: Optional[date] = None) -> dict:
    """01:45 IST: every booked action → verified (with revenue) / not_verified after its due date (case re-opens)."""
    from app.services import cc_cases
    cid = clinic["id"]
    today = today or today_ist()
    stats = {"verified": 0, "not_verified": 0}
    for a in await fetch_all(lambda: scoped_query("cc_actions", cid).eq("status", "booked"), label="cc:verify"):
        ev, revenue = None, None
        if a.get("appointment_id"):
            ev = await evidence_for_visit(cid, a["appointment_id"])
            revenue = await visit_revenue(cid, a["appointment_id"]) if ev else None
        elif a["action_code"] in ("order_lab", "book_home_sample"):
            case = _rows(await sb(scoped_query("cc_cases", cid, "patient_id").eq("id", a["case_id"]).limit(1)))
            if case:
                ev = await evidence_for_lab(cid, case[0]["patient_id"], str(a["created_at"]))
                revenue = await lab_revenue(cid, (ev or {}).get("order_id")) if ev else None
        if ev:
            try:
                # unscoped: insert_scoped_by_payload
                await sb(supabase.table("cc_verifications").insert({
                    "clinic_id": cid, "action_id": a["id"], "evidence_kind": ev["kind"], "evidence_ref": ev["ref"],
                    "revenue_paise": revenue}))
            except Exception as e:                         # noqa: BLE001 - a parallel run verified it first
                if "cc_verifications_action_key" not in str(e):
                    raise
            await sb(supabase.table("cc_actions").update({"status": "verified", "updated_at": _now()})
                     .eq("clinic_id", cid).eq("id", a["id"]).eq("status", "booked"))
            await sb(supabase.table("cc_cases").update({"status": "closed", "closed_reason": "verified", "closed_at": _now(),
                                                        "updated_at": _now()})
                     .eq("clinic_id", cid).eq("id", a["case_id"]).in_("status", ["actioned", "open", "acknowledged",
                                                                                  "awaiting_doctor"]))
            stats["verified"] += 1
        elif a.get("due_on") and date.fromisoformat(str(a["due_on"])[:10]) < today:
            await sb(supabase.table("cc_actions").update({"status": "not_verified", "updated_at": _now()})
                     .eq("clinic_id", cid).eq("id", a["id"]).eq("status", "booked"))
            case = _rows(await sb(scoped_query("cc_cases", cid).eq("id", a["case_id"]).limit(1)))
            if case:
                await sb(supabase.table("cc_cases").update({"status": "open", "updated_at": _now()})
                         .eq("clinic_id", cid).eq("id", a["case_id"]).eq("status", "actioned"))
                await cc_cases.event(cid, a["case_id"], "status_change", "Not recorded by the due date — reopened")
            stats["not_verified"] += 1
    return stats


# ── outcomes (OUTCOMES.md §1, method outcomes-1) ─────────────────────────────

def pct(n: int, d: int) -> Optional[int]:
    return round(100 * n / d) if d else None


async def report(clinic_id: str, protocol: Optional[str], since: date, until: date) -> dict:
    enr = await fetch_all(lambda: (scoped_query("cc_enrollments", clinic_id).eq("protocol_code", protocol) if protocol
                                   else scoped_query("cc_enrollments", clinic_id)), label="cc:outcomes")
    started = [e for e in enr if e.get("enrolled_at")]
    funnel = {"invited": sum(1 for e in enr if e.get("doctor_approved_at")), "consented": len(started),
              "active": sum(1 for e in enr if e["status"] == "active"),
              "ended": sum(1 for e in enr if e["status"] in ("graduated", "withdrawn", "lost", "ended_by_clinic")),
              "candidates": len(_rows(await sb(scoped_query("cc_radar_rows", clinic_id, "id").eq("reason", "candidate")
                                               .is_("closed_on", "null").limit(10000))))}
    cohort = [e for e in started if (until - date.fromisoformat(str(e["enrolled_at"])[:10])).days >= 90]
    returned = baseline_returned = 0
    for e in cohort:
        start = date.fromisoformat(str(e["enrolled_at"])[:10])
        if await _verified_visit_between(clinic_id, e["patient_id"], start, start + timedelta(days=90)):
            returned += 1
        if await _verified_visit_between(clinic_id, e["patient_id"], start - timedelta(days=90), start - timedelta(days=1)):
            baseline_returned += 1
    actions = await fetch_all(lambda: scoped_query("cc_actions", clinic_id, "id, status, action_code, created_at, due_on"),
                              label="cc:outcomes:act")
    in_range = [a for a in actions if since.isoformat() <= str(a["created_at"])[:10] <= until.isoformat()]
    labs = [a for a in in_range if a["action_code"] in ("order_lab", "book_home_sample")]
    ver = {v["action_id"]: v for v in await fetch_all(lambda: scoped_query("cc_verifications", clinic_id), label="cc:ver")}
    labs_on_time = sum(1 for a in labs if a["id"] in ver)
    revenue = sum(int(v.get("revenue_paise") or 0) for a in in_range if (v := ver.get(a["id"])))
    by_kind = {"visits": ("check_in", "signed_encounter"), "labs": ("validated_result",), "refills": ("completed_sale",)}
    revenue_split = {k: sum(int(v.get("revenue_paise") or 0) for a in in_range if (v := ver.get(a["id"]))
                            and v["evidence_kind"] in kinds) for k, kinds in by_kind.items()}
    at_target, with_value = await _at_target(clinic_id, started, since, until)
    baseline_revenue = 0
    for e in cohort:
        start = date.fromisoformat(str(e["enrolled_at"])[:10])
        baseline_revenue += await _paid_between(clinic_id, e["patient_id"], start - timedelta(days=90), start - timedelta(days=1))
    visits_verified = sum(1 for a in in_range if a["action_code"] == "book_review_visit" and a["id"] in ver)
    split = {k: sum(1 for v in ver.values() if v["evidence_kind"] == k)
             for k in ("check_in", "signed_encounter", "completed_sale")}
    prompts = await fetch_all(lambda: scoped_query("cc_prompts", clinic_id, "status, channel, sent_at")
                              .gte("created_at", since.isoformat()), label="cc:outcomes:prompts")
    sent = [p for p in prompts if p.get("sent_at")]
    answered = sum(1 for p in prompts if p["status"] == "answered")
    urgent = await fetch_all(lambda: scoped_query("cc_cases", clinic_id, "id, tier, due_by, escalated_at, created_at")
                             .eq("tier", "urgent").gte("created_at", since.isoformat()), label="cc:outcomes:urgent")
    return {
        "method_version": METHOD, "from": since.isoformat(), "to": until.isoformat(), "protocol": protocol,
        "funnel": funnel,
        "return_rate": pct(returned, len(cohort)), "baseline_return_rate": pct(baseline_returned, len(cohort)),
        "cohort_90d": len(cohort), "labs_on_time": pct(labs_on_time, len(labs)), "labs_due": len(labs),
        "visits_verified": visits_verified, "verified_by": split, "revenue_paise": revenue,
        "revenue_split_paise": revenue_split, "refill_revenue_paise": revenue_split["refills"],
        "baseline_revenue_paise": baseline_revenue, "revenue_delta_paise": revenue - baseline_revenue,
        "at_target": pct(at_target, with_value), "at_target_count": at_target, "with_value": with_value,
        "answered_rate": pct(answered, len(sent)), "prompts_sent": len(sent),
        "template_share": pct(sum(1 for p in sent if p["channel"] == "whatsapp_template"), len(sent)),
        "safety_response": pct(sum(1 for c in urgent if not c.get("escalated_at")), len(urgent)),
        "note": "Associated with, not caused by: the cohort is compared with the same patients' 90 days before enrollment. "
                "Only verified events count (check-in, signed encounter, validated result, completed sale).",
    }


async def _at_target(clinic_id: str, enrollments: list, since: date, until: date) -> tuple:
    """(at target, with a value) over the period's latest value (OUTCOMES §1): t2dm latest HbA1c < the signed target;
    htn mean of the 7 days ending at the latest reading < both signed targets. Other packs have no target measure."""
    from app.services import cc_protocols
    from app.services.cc_protocols.base import resolve_params
    acts = {a["id"]: a for a in await fetch_all(lambda: scoped_query("cc_protocol_activations", clinic_id, "id, params"),
                                                label="cc:outcomes:acts")}
    hit = n = 0
    for e in enrollments:
        code = e["protocol_code"]
        if code not in ("t2dm", "htn"):
            continue
        p = resolve_params(cc_protocols.get(code), (acts.get(e["activation_id"]) or {}).get("params") or {})
        measure = "hba1c" if code == "t2dm" else "bp"
        obs = _rows(await sb(scoped_query("cc_observations", clinic_id, "value_num, value_num2, observed_at")
                             .eq("enrollment_id", e["id"]).eq("measure_code", measure).eq("plausible", True)
                             .not_.is_("value_num", "null").gte("observed_at", since.isoformat())
                             .lt("observed_at", (until + timedelta(days=1)).isoformat()).order("observed_at", desc=True)
                             .limit(60)))
        if not obs:
            continue
        n += 1
        if code == "t2dm":
            hit += float(obs[0]["value_num"]) < p["a1c_target"]
            continue
        last = date.fromisoformat(str(obs[0]["observed_at"])[:10])
        week = [o for o in obs if (last - date.fromisoformat(str(o["observed_at"])[:10])).days < 7]
        sys_ = sum(float(o["value_num"]) for o in week) / len(week)
        dia = [float(o["value_num2"]) for o in week if o.get("value_num2") is not None]
        hit += sys_ < p["target_sys"] and (not dia or sum(dia) / len(dia) < p["target_dia"])
    return hit, n


async def _paid_between(clinic_id: str, patient_id: str, lo: date, hi: date) -> int:
    """Baseline revenue: the patient's paid OPD invoices (visits + labs) and completed pharmacy sales in [lo, hi]."""
    hi_x = (hi + timedelta(days=1)).isoformat()
    inv = _rows(await sb(scoped_query("opd_invoices", clinic_id, "paid_paise").eq("patient_id", patient_id)
                         .in_("status", ["paid", "partially_paid"]).gte("issued_at", lo.isoformat()).lt("issued_at", hi_x)))
    total = sum(int(i.get("paid_paise") or 0) for i in inv)
    try:
        sales = _rows(await sb(scoped_query("rx_sales", clinic_id, "paid_paise, refunded_paise").eq("patient_id", patient_id)
                               .eq("status", "completed").gte("completed_at", lo.isoformat()).lt("completed_at", hi_x)))
    except Exception:                                     # noqa: BLE001 - no Pharmacy OS tables
        sales = []
    return total + sum(max(0, int(x.get("paid_paise") or 0) - int(x.get("refunded_paise") or 0)) for x in sales)


async def _verified_visit_between(clinic_id: str, patient_id: str, lo: date, hi: date) -> bool:
    a = _rows(await sb(scoped_query("appointments", clinic_id, "id").eq("patient_id", patient_id)
                       .gte("checked_in_at", lo.isoformat()).lte("checked_in_at", (hi + timedelta(days=1)).isoformat()).limit(1)))
    if a:
        return True
    e = _rows(await sb(scoped_query("opd_encounters", clinic_id, "id").eq("patient_id", patient_id).eq("status", "signed")
                       .gte("signed_at", lo.isoformat()).lte("signed_at", (hi + timedelta(days=1)).isoformat()).limit(1)))
    return bool(e)


async def snapshot_month(clinic_id: str, month_start: date) -> int:
    """Monthly snapshot per pack (idempotent on (clinic, pack, month))."""
    nxt = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    codes = {e["protocol_code"] for e in await fetch_all(lambda: scoped_query("cc_enrollments", clinic_id, "protocol_code"),
                                                          label="cc:snap")}
    n = 0
    for code in codes:
        r = await report(clinic_id, code, month_start, nxt - timedelta(days=1))
        row = {"clinic_id": clinic_id, "protocol_code": code, "period_month": month_start.isoformat(),
               "enrolled": r["funnel"]["consented"], "active": r["funnel"]["active"],
               "returned_90d": round((r["return_rate"] or 0) * r["cohort_90d"] / 100),
               "labs_verified": round((r["labs_on_time"] or 0) * r["labs_due"] / 100), "visits_verified": r["visits_verified"],
               "at_target": r["at_target_count"], "refills_verified": r["verified_by"].get("completed_sale", 0),
               "baseline": {"revenue_paise": r["baseline_revenue_paise"], "return_rate": r["baseline_return_rate"]},
               "revenue_paise": r["revenue_paise"], "report": r, "method_version": METHOD, "computed_at": _now()}
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("cc_outcome_snapshots").upsert(row, on_conflict="clinic_id,protocol_code,period_month"))
        n += 1
    return n


def report_csv(r: dict) -> str:
    from app.services.client_data import csv_cell
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Measure", "Value"])
    for k, v in (("Method", r["method_version"]), ("From", r["from"]), ("To", r["to"]), ("Programme", r["protocol"] or "All"),
                 ("Return rate %", r["return_rate"]), ("Baseline return rate %", r["baseline_return_rate"]),
                 ("Labs on time %", r["labs_on_time"]), ("Visits verified", r["visits_verified"]),
                 ("At target % of tested", r.get("at_target")),
                 ("Verified revenue (Rs)", (r["revenue_paise"] or 0) / 100),
                 ("Verified refill revenue (Rs)", (r.get("refill_revenue_paise") or 0) / 100),
                 ("Baseline revenue (Rs)", (r.get("baseline_revenue_paise") or 0) / 100),
                 ("Answered %", r["answered_rate"]),
                 ("Note", r["note"])):
        w.writerow([csv_cell(k), csv_cell(v)])
    return buf.getvalue()
