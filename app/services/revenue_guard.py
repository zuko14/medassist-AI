"""Kriya Revenue Guard — nightly unbilled-charge check (migration 143; lane RI, docs/revenue-integrity/SPEC.md).

Zero-LLM and read-only over billing: the rules live in the ri_scan SQL function (one call per clinic and day), this
module runs it for every opted-in clinic, raises an admin alert for integrity findings, sends the 08:00 IST digest and
serves the page. Revenue Guard never writes a bill — the existing bill screens do the billing.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.utils.helpers import today_ist

logger = logging.getLogger(__name__)

KINDS = ("leakage", "review", "integrity", "opportunity")
RULES = {
    "opd_seen_not_billed": ("leakage", "Seen, not billed"),
    "prepaid_not_invoiced": ("review", "Prepaid, invoice missing"),
    "order_done_not_billed": ("leakage", "Test or procedure done, not billed"),
    "order_stale_unbilled": ("review", "Order waiting over 48 h, not billed"),
    "draft_bill_stale": ("review", "Draft bill left over 24 h"),
    "ipd_charge_unposted": ("leakage", "IPD charge not posted"),
    "ipd_discharged_unbilled": ("leakage", "Discharged, charge not on a bill"),
    "ipd_discharge_pending_open": ("review", "Discharge pending with open charges"),
    "ot_done_no_charge": ("integrity", "Surgery done, no OT charge"),
    "rx_dispense_no_charge": ("integrity", "Ward dispense without its charge"),
    "rx_signed_not_sold": ("opportunity", "Prescription not bought here"),
    "dx_b2b_unstatemented": ("review", "B2B charges not on a statement"),
}
DEFAULT_SETTINGS = {"min_opd_invoices_30d": 20, "review_free_days": 0, "exempt_reasons": ["staff", "vip", "charity"]}
_BOUNDS = {"min_opd_invoices_30d": (0, 1000), "review_free_days": (0, 30)}
MANUAL_RUNS_PER_DAY = 3


def get_settings(clinic: dict) -> dict:
    return {**DEFAULT_SETTINGS, **(clinic.get("ri_settings") or {})}


def validate_settings(changes: dict) -> dict:
    """Bounded settings (422 on error). exempt_reasons: up to 10 words, 3–30 characters each."""
    out = {}
    for k, v in changes.items():
        if v is None:
            continue
        if k in _BOUNDS:
            lo, hi = _BOUNDS[k]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise HTTPException(status_code=422, detail=f"{k} must be a whole number from {lo} to {hi}.")
            out[k] = v
        elif k == "exempt_reasons":
            words = [str(w).strip().lower() for w in v]
            if len(words) > 10 or any(not 3 <= len(w) <= 30 for w in words):
                raise HTTPException(status_code=422, detail="Exempt reasons: up to 10 words of 3–30 characters.")
            out[k] = list(dict.fromkeys(words))
        else:
            raise HTTPException(status_code=422, detail=f"Unknown setting {k}.")
    return out


def _rupees(paise: int) -> str:
    rupees = (paise or 0) // 100
    s = str(rupees)
    if len(s) > 3:                                   # Indian grouping: 1,23,456
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(i - 2, 0):i] for i in range(len(head), 0, -2)][::-1])
        s = f"{head},{tail}"
    return f"₹{s}"


async def _notify(clinic_id: str, title: str, message: str) -> None:
    # unscoped: insert_scoped_by_payload
    await sb(supabase.table("admin_notifications").insert({
        "clinic_id": clinic_id, "admin_id": None, "title": title[:255], "message": message, "is_read": False}))


# ── the scan ─────────────────────────────────────────────────────────────────

async def scan_clinic(clinic: dict, day: Optional[date] = None, now: Optional[datetime] = None) -> dict:
    """Run every rule for one clinic over `day` (default yesterday IST) and re-check every open finding."""
    from app.services.tenant import ri_modules
    now = now or datetime.now(timezone.utc)
    day = day or (today_ist(now) - timedelta(days=1))
    cid = clinic["id"]
    out = (await sb(supabase.rpc("ri_scan", {
        "p_clinic_id": cid, "p_day": day.isoformat(), "p_now": now.isoformat(),
        "p_settings": get_settings(clinic), "p_modules": ri_modules(clinic)}))).data or {}
    integrity = {r: n for r, n in (out.get("rules") or {}).items() if RULES.get(r, ("",))[0] == "integrity"}
    if integrity:
        await _notify(cid, "[Revenue Guard] Billing integrity issue",
                      "; ".join(f"{RULES[r][1]}: {n}" for r, n in integrity.items())
                      + ". Open Revenue Guard to see each item; nothing has been changed automatically.")
    return out


async def _enabled_clinics() -> list[dict]:
    from app.services.tenant import ri_access
    # unscoped: platform_sweep (Revenue Guard is an owner opt-in; the flag lives in clinics.features)
    res = await sb(supabase.table("clinics").select("*").eq("is_active", True))
    return [c for c in res.data or [] if c.get("status") != "DELETED" and ri_access(c)]


async def run_nightly(day: Optional[date] = None) -> dict:
    """01:30 IST (scheduler.py, CAS-locked). One clinic never stops the sweep."""
    stats = {"clinics": 0, "failed": 0}
    for clinic in await _enabled_clinics():
        try:
            await scan_clinic(clinic, day)
            stats["clinics"] += 1
        except Exception as e:                            # noqa: BLE001
            stats["failed"] += 1
            logger.error(f"RI scan failed for clinic={clinic.get('id')}: {type(e).__name__}")
    logger.info(f"RI nightly scan: {stats}")
    return stats


async def digest_text(clinic_id: str, today: Optional[date] = None) -> Optional[str]:
    """'Yesterday: ₹18,400 done but not billed (7 items). Open > 7 days: ₹42,000.' — None when nothing is open."""
    today = today or today_ist()
    rows = await fetch_all(lambda: scoped_query("ri_findings", clinic_id, "kind, amount_paise, first_seen_on")
                           .eq("status", "open"), label="ri:digest")
    fresh = [r for r in rows if r["kind"] == "leakage" and r["first_seen_on"] == today.isoformat()]
    old = [r for r in rows if r["kind"] == "leakage" and r["first_seen_on"] <= (today - timedelta(days=7)).isoformat()]
    if not fresh and not old:
        return None
    return (f"Revenue Guard — yesterday: {_rupees(sum(r['amount_paise'] for r in fresh))} done but not billed "
            f"({len(fresh)} item{'s' if len(fresh) != 1 else ''}). "
            f"Open > 7 days: {_rupees(sum(r['amount_paise'] for r in old))}. Open Revenue Guard in Kriya to bill them.")


async def run_digest(today: Optional[date] = None) -> dict:
    """08:00 IST: in-app notification + WhatsApp to the clinic's own admin phone (clinic credentials only)."""
    from app.services.tenant import get_clinic_contact
    from app.services.whatsapp import whatsapp_service
    stats = {"clinics": 0, "whatsapp": 0}
    for clinic in await _enabled_clinics():
        try:
            text = await digest_text(clinic["id"], today)
            if not text:
                continue
            await _notify(clinic["id"], "Revenue Guard: unbilled work", text)
            stats["clinics"] += 1
            phone = get_clinic_contact(clinic, "admin_phone", "")
            if phone and await whatsapp_service.send_text(clinic, phone, text, _source="revenue_guard"):
                stats["whatsapp"] += 1
        except Exception as e:                            # noqa: BLE001
            logger.error(f"RI digest failed for clinic={clinic.get('id')}: {type(e).__name__}")
    return stats


async def manual_run(clinic: dict) -> dict:
    """POST /admin/ri/run: re-run today for one clinic, at most 3 times a day (counted in ri_runs.manual_runs)."""
    cid, today = clinic["id"], today_ist()
    run = (await sb(scoped_query("ri_runs", cid, "id, manual_runs").eq("run_date", today.isoformat()).limit(1))).data
    used = run[0]["manual_runs"] if run else 0
    if used >= MANUAL_RUNS_PER_DAY:
        raise HTTPException(status_code=429, detail="Revenue Guard can be re-run 3 times a day. Try again tomorrow.")
    out = await scan_clinic(clinic, day=today)
    await sb(supabase.table("ri_runs").update({"manual_runs": used + 1}).eq("clinic_id", cid)
             .eq("run_date", today.isoformat()))
    return {**out, "manual_runs_left": MANUAL_RUNS_PER_DAY - used - 1}


# ── reads ────────────────────────────────────────────────────────────────────

def _branch(q, branch_id: Optional[str]):
    return q.eq("branch_id", branch_id) if branch_id else q


async def summary(clinic_id: str, branch_id: Optional[str], since: date, until: date) -> dict:
    """Totals by kind and rule (open), recovered ₹ and dismissed ₹ in [since, until], open > 7 days, yesterday."""
    today = today_ist()
    open_rows = await fetch_all(lambda: _branch(scoped_query(
        "ri_findings", clinic_id, "rule_code, kind, amount_paise, first_seen_on"), branch_id).eq("status", "open"),
        label="ri:open")
    closed = await fetch_all(lambda: _branch(scoped_query(
        "ri_findings", clinic_id, "rule_code, kind, resolution, amount_paise, recovered_paise, resolved_on"), branch_id)
        .gte("resolved_on", since.isoformat()).lte("resolved_on", until.isoformat()), label="ri:closed")
    by_kind = {k: {"count": 0, "amount_paise": 0} for k in KINDS}
    by_rule: dict[str, dict] = {}
    for r in open_rows:
        by_kind[r["kind"]]["count"] += 1
        by_kind[r["kind"]]["amount_paise"] += r["amount_paise"]
        x = by_rule.setdefault(r["rule_code"], {"rule_code": r["rule_code"], "kind": r["kind"],
                                                "label": RULES[r["rule_code"]][1], "count": 0, "amount_paise": 0})
        x["count"] += 1
        x["amount_paise"] += r["amount_paise"]
    week_ago, yday = (today - timedelta(days=7)).isoformat(), today.isoformat()
    leak = [r for r in open_rows if r["kind"] == "leakage"]
    last = (await sb(scoped_query("ri_runs", clinic_id, "run_date, finished_at, rule_counts")
                     .order("run_date", desc=True).limit(1))).data
    return {
        "by_kind": by_kind,
        "by_rule": sorted(by_rule.values(), key=lambda x: -x["amount_paise"]),
        "yesterday_leakage_paise": sum(r["amount_paise"] for r in leak if r["first_seen_on"] == yday),
        "yesterday_leakage_count": sum(1 for r in leak if r["first_seen_on"] == yday),
        "open_over_7d_paise": sum(r["amount_paise"] for r in leak if r["first_seen_on"] <= week_ago),
        "recovered_paise": sum(r.get("recovered_paise") or 0 for r in closed if r["resolution"] == "billed"),
        "recovered_count": sum(1 for r in closed if r["resolution"] == "billed"),
        # X14: dismissals are shown in rupees so they cannot quietly hide leakage
        "dismissed_paise": sum(r["amount_paise"] for r in closed if r["resolution"] == "dismissed"),
        "dismissed_count": sum(1 for r in closed if r["resolution"] == "dismissed"),
        "integrity_open": by_kind["integrity"]["count"],
        "last_run": last[0] if last else None,
        "from": since.isoformat(), "to": until.isoformat(),
    }


async def list_findings(clinic_id: str, branch_id: Optional[str], status: Optional[str], rule: Optional[str],
                        limit: int, offset: int) -> dict:
    q = _branch(supabase.table("ri_findings").select("*", count="exact").eq("clinic_id", clinic_id), branch_id)
    if status:
        q = q.eq("status", status)
    if rule:
        q = q.eq("rule_code", rule)
    res = await sb(q.order("amount_paise", desc=True).order("service_date", desc=True).order("id")
                   .range(offset, offset + limit - 1))
    rows = res.data or []
    ids = list({r["patient_id"] for r in rows if r.get("patient_id")})
    pats = {}
    if ids:
        pats = {p["id"]: p for p in (await sb(scoped_query("patients", clinic_id, "id, name, mrn").in_("id", ids))).data
                or []}
    for r in rows:
        r["label"] = RULES[r["rule_code"]][1]
        p = pats.get(r.get("patient_id")) or {}
        r["patient_name"] = p.get("name") or (r.get("detail") or {}).get("patient_name")
        r["mrn"] = p.get("mrn")
    return {"findings": rows, "total": res.count if res.count is not None else len(rows)}


async def dismiss(clinic_id: str, finding_id: str, branch_id: Optional[str], reason: str, user) -> dict:
    rows = (await sb(_branch(scoped_query("ri_findings", clinic_id), branch_id).eq("id", finding_id).limit(1))).data
    if not rows:
        raise HTTPException(status_code=404, detail="Finding not found.")
    if rows[0]["status"] != "open":
        raise HTTPException(status_code=409, detail="This finding is already closed.")
    actor = getattr(user, "user_id", None)
    try:
        import uuid
        uuid.UUID(str(actor))
    except (ValueError, TypeError):
        raise HTTPException(status_code=403, detail="Dismissing a finding needs a named staff login.")
    res = await sb(supabase.table("ri_findings").update({
        "status": "dismissed", "resolution": "dismissed", "resolved_on": today_ist().isoformat(),
        "dismissed_by": str(actor), "dismissed_by_name": (getattr(user, "username", None) or "")[:100],
        "dismiss_reason": reason.strip(), "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("clinic_id", clinic_id).eq("id", finding_id).eq("status", "open"))
    if not res.data:
        raise HTTPException(status_code=409, detail="This finding changed meanwhile. Reload and try again.")
    return res.data[0]


async def month_value(clinic_id: str, today: Optional[date] = None) -> dict:
    """Owner console sales proof: ₹ flagged (first seen this month) and ₹ recovered / dismissed this month."""
    today = today or today_ist()
    start = today.replace(day=1).isoformat()
    cols = "id, kind, amount_paise, recovered_paise, resolution, first_seen_on, resolved_on"
    seen = await fetch_all(lambda: scoped_query("ri_findings", clinic_id, cols).gte("first_seen_on", start),
                           label="ri:month:seen")
    shut = await fetch_all(lambda: scoped_query("ri_findings", clinic_id, cols).gte("resolved_on", start),
                           label="ri:month:closed")
    rows = list({r["id"]: r for r in seen + shut}.values())
    return {
        "flagged_paise": sum(r["amount_paise"] for r in rows if r["kind"] == "leakage" and r["first_seen_on"] >= start),
        "recovered_paise": sum(r.get("recovered_paise") or 0 for r in rows
                               if r["resolution"] == "billed" and (r.get("resolved_on") or "") >= start),
        "dismissed_paise": sum(r["amount_paise"] for r in rows
                               if r["resolution"] == "dismissed" and (r.get("resolved_on") or "") >= start),
    }
