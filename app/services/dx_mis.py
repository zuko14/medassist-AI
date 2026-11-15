"""Diagnostic OS dashboard and MIS (P9, DX-54). Read-only, tenant-scoped, zero-LLM.

Stages and TAT come from `dx.worklist` (the Lab Worklist's own derivation), so a dashboard
count always equals what the bench shows. Money comes from the OPD ledger (`opd_receipts`
on DX invoices) and the B2B ledger. Referral numbers are counts only (ADR-DX-11).
Every function takes `site_ids`: None = clinic-wide, else the caller's sites (SECURITY §4).
"""

import csv
import io
import math
import statistics
from datetime import date, datetime, time, timedelta, timezone
from typing import AsyncIterator, Optional

from fastapi import HTTPException

from app.database import scoped_query
from app.services import dx, dx_reports
from app.services.dx_b2b import _in, _rows
from app.utils.helpers import IST, today_ist

FUNNEL = ("registered", "collected", "in_transit", "received", "entered", "verified", "approved", "released")
OPEN_STAGES = ("awaiting_payment", "pending_collection", "pending_receipt", "pending_entry", "pending_verification",
               "pending_approval")
MAX_DAYS = 366
# shortcut: MIS reads at most this many visits per range (flagged `truncated`); move to SQL views past it
MAX_VISITS = 5000
REPORTS = ("visits", "tat", "revenue", "rejections", "referrers")
PAY_MODES = {"cash": "cash", "upi": "upi", "card": "card", "payment_link": "online", "prepaid_online": "online"}


# ── shared helpers ──────────────────────────────────────────────────────────

def check_range(start: date, end: date) -> None:
    if end < start:
        raise HTTPException(status_code=422, detail="The end date is before the start date.")
    if (end - start).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"Choose a range of at most {MAX_DAYS} days.")


def _utc(d: date) -> str:
    return datetime.combine(d, time.min, tzinfo=IST).astimezone(timezone.utc).isoformat()


def _ist_day(v) -> str:
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(IST).date().isoformat()


def _days(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


async def _site_branches(clinic_id: str, site_ids: Optional[list[str]]) -> Optional[set]:
    if site_ids is None:
        return None
    return {str(s["branch_id"]) for s in await dx.list_sites(clinic_id) if s["id"] in site_ids}


async def _visits_between(clinic_id: str, start: date, end: date, branches: Optional[set] = None) -> tuple[list, bool]:
    rows = await _rows(scoped_query("dx_accessions", clinic_id, dx.VISIT_LIST_COLUMNS)
                       .gte("created_at", _utc(start)).lt("created_at", _utc(end + timedelta(days=1)))
                       .neq("status", "cancelled").order("created_at").limit(MAX_VISITS + 1))
    truncated = len(rows) > MAX_VISITS
    rows = rows[:MAX_VISITS]
    if branches is not None:
        rows = [v for v in rows if str(v["branch_id"]) in branches]
    return rows, truncated


async def _worklist(clinic_id: str, visits: list, site_ids=None, department_id=None) -> list[dict]:
    """Every DX test of `visits` with its stage and TAT (the bench's worklist, 100 visits per read)."""
    out = []
    for i in range(0, len(visits), 100):
        wl = await dx.worklist(clinic_id, visits[i:i + 100], site_ids, department_id)
        out += [{**o, "visit": v} for v in wl["visits"] for o in v["orders"]]
    return out


def funnel_key(o: dict) -> Optional[str]:
    """Bench stage -> dashboard funnel column (None = cancelled)."""
    st = o["stage"]
    if st in ("awaiting_payment", "pending_collection"):
        return "registered"
    if st == "pending_receipt":
        return "in_transit" if (o.get("sample") or {}).get("status") == "in_transit" else "collected"
    if st == "pending_entry":
        return "received"
    if st == "pending_verification":
        return "entered"
    if st == "pending_approval":
        return "verified" if (o.get("result") or {}).get("verified") else "entered"
    return {"pending_release": "approved", "released": "released"}.get(st)


def _breached(o: dict) -> bool:
    return o["stage"] in OPEN_STAGES and o["tat"]["status"] == "red"


def percentile(values: list[int], pct: float) -> Optional[int]:
    """Nearest-rank percentile (no interpolation: every value is a real test's minutes)."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(pct / 100 * len(s)) - 1)]


# ── dashboard ───────────────────────────────────────────────────────────────

async def get_dx_dashboard(clinic_id: str, site_ids: Optional[list[str]] = None, on_date: Optional[date] = None,
                           with_money: bool = False) -> dict:
    """Funnel + alerts for one IST day (default: today plus every open visit of the last 7 days)."""
    day = on_date or today_ist()
    visits = await dx.list_visits(clinic_id, None, day.isoformat(), None, None, 1000)
    if on_date is None:
        seen = {v["id"] for v in visits}
        visits += [v for v in await dx.open_visits(clinic_id) if v["id"] not in seen]
    orders = await _worklist(clinic_id, visits, site_ids)
    funnel = {k: 0 for k in FUNNEL}
    for o in orders:
        k = funnel_key(o)
        if k:
            funnel[k] += 1
    visible = {o["visit"]["id"] for o in orders}
    held = [] if site_ids is not None and not visible else await dx_reports.list_deliveries(
        clinic_id, "held_dues", None, None, None if site_ids is None else sorted(visible), limit=1000)
    held_visits = sorted({d["accession_id"] for d in held})
    # shortcut: dues are read per held visit (one RPC each, first 200); a SQL aggregate if a lab holds more
    dues = sum([await dx_reports.visit_dues_paise(clinic_id, v) for v in held_visits[:200]])
    rq = scoped_query("dx_samples", clinic_id, "id, collection_site_id").eq("status", "rejected") \
        .gte("rejected_at", _utc(day)).lt("rejected_at", _utc(day + timedelta(days=1)))
    rejected = [s for s in await _rows(rq) if site_ids is None or s["collection_site_id"] in site_ids]
    out = {
        "date": day.isoformat(), "open_visits_included": on_date is None, "funnel": funnel,
        "alerts": {
            "critical_open": sum(1 for o in orders if (o.get("result") or {}).get("critical_pending")),
            "tat_breaches": sum(1 for o in orders if _breached(o)),
            "held_dues": {"visits": len(held_visits), "dues_paise": dues},
            "rejections_today": len(rejected),
        },
    }
    if with_money:
        out["revenue"] = (await get_mis_revenue(clinic_id, day, day, site_ids))["totals"]
    return out


# ── MIS ─────────────────────────────────────────────────────────────────────

async def get_mis_tat(clinic_id: str, start: date, end: date, site_ids: Optional[list[str]] = None,
                      department_id: Optional[str] = None) -> dict:
    """Median / 90th percentile TAT of finished tests, and breaches (finished late + open past target)."""
    check_range(start, end)
    visits, truncated = await _visits_between(clinic_id, start, end)
    orders = await _worklist(clinic_id, visits, site_ids, department_id)
    depts = {d["id"]: d["name"] for d in await _rows(scoped_query("dx_departments", clinic_id, "id, name"))}

    def group(key, name) -> list[dict]:
        acc: dict = {}
        for o in orders:
            if o["stage"] == "cancelled":
                continue
            g = acc.setdefault(key(o), {"name": name(o), "tests": 0, "finished": [], "breached": 0,
                                        "open_breached": 0, "target_minutes": o["tat"]["target_minutes"]})
            g["tests"] += 1
            if o["stage"] in ("pending_release", "released") and o["tat"]["elapsed_minutes"] is not None:
                g["finished"].append(o["tat"]["elapsed_minutes"])
                g["breached"] += o["tat"]["status"] == "red"
            g["open_breached"] += _breached(o)
        rows = []
        for k, g in acc.items():
            f = g.pop("finished")
            late = g["breached"] + g["open_breached"]
            rows.append({"id": k, **g, "finished": len(f), "median_minutes": int(statistics.median(f)) if f else None,
                         "p90_minutes": percentile(f, 90), "breach_percent": round(100 * late / g["tests"], 1)})
        return sorted(rows, key=lambda r: (-r["breach_percent"], r["name"] or ""))

    return {"from": start.isoformat(), "to": end.isoformat(), "truncated": truncated,
            "tests": group(lambda o: o["lab_test_id"], lambda o: o["item_name"]),
            "departments": group(lambda o: o.get("department_id"), lambda o: depts.get(o.get("department_id"), "—"))}


async def _test_departments(clinic_id: str, items: list) -> dict:
    return {t["id"]: t.get("department_id") for t in await _in("lab_tests", clinic_id, "id, department_id", "id",
                                                               [i["lab_test_id"] for i in items])}


async def get_mis_volumes(clinic_id: str, start: date, end: date, site_ids: Optional[list[str]] = None,
                          department_id: Optional[str] = None) -> dict:
    """Visits, tests and tubes per IST day (cancelled visits and items excluded)."""
    check_range(start, end)
    visits, truncated = await _visits_between(clinic_id, start, end, await _site_branches(clinic_id, site_ids))
    vids = [v["id"] for v in visits]
    items = [i for i in await _in("dx_accession_items", clinic_id, "accession_id, lab_test_id, kind, status",
                                  "accession_id", vids) if i["status"] == "active" and i["kind"] != "fee"]
    if department_id:
        dept = await _test_departments(clinic_id, items)
        items = [i for i in items if dept.get(i["lab_test_id"]) == department_id]
        keep = {i["accession_id"] for i in items}
        visits = [v for v in visits if v["id"] in keep]
    samples = await _in("dx_samples", clinic_id, "accession_id, status", "accession_id", [v["id"] for v in visits])
    day_of = {v["id"]: _ist_day(v["created_at"]) for v in visits}
    rows = {d: {"date": d, "visits": 0, "tests": 0, "samples": 0, "rejected": 0} for d in _days(start, end)}
    for v in visits:
        rows[day_of[v["id"]]]["visits"] += 1
    for i in items:
        rows[day_of[i["accession_id"]]]["tests"] += 1
    for s in samples:
        rows[day_of[s["accession_id"]]]["samples"] += 1
        rows[day_of[s["accession_id"]]]["rejected"] += s["status"] == "rejected"
    days = list(rows.values())
    return {"from": start.isoformat(), "to": end.isoformat(), "truncated": truncated, "days": days,
            "totals": {k: sum(r[k] for r in days) for k in ("visits", "tests", "samples", "rejected")}}


async def get_mis_revenue(clinic_id: str, start: date, end: date, site_ids: Optional[list[str]] = None,
                          department_id: Optional[str] = None) -> dict:
    """Money collected on DX bills per day and mode (refunds subtract), B2B credit charged (charges minus
    reversals), billed and still due on DX bills issued in the range, and test billing by department."""
    check_range(start, end)
    lo, hi = _utc(start), _utc(end + timedelta(days=1))
    branches = await _site_branches(clinic_id, site_ids)
    receipts = await _rows(scoped_query("opd_receipts", clinic_id, "invoice_id, kind, mode, amount_paise, created_at")
                           .gte("created_at", lo).lt("created_at", hi))
    issued = await _rows(scoped_query("opd_invoices", clinic_id, "id, accession_id, status, total_paise, paid_paise, "
                                      "credited_paise").not_.is_("accession_id", "null")
                         .in_("status", ["issued", "partially_paid", "paid"])
                         .gte("issued_at", lo).lt("issued_at", hi))
    invoices = {i["id"]: i for i in await _in("opd_invoices", clinic_id, "id, accession_id", "id",
                                              [r["invoice_id"] for r in receipts])}
    ledger = await _rows(scoped_query("dx_b2b_ledger", clinic_id, "entry_type, amount_paise, accession_id, created_at")
                         .in_("entry_type", ["charge", "charge_reversal"]).gte("created_at", lo).lt("created_at", hi))
    branch_of = {v["id"]: str(v["branch_id"]) for v in await _in(
        "dx_accessions", clinic_id, "id, branch_id", "id",
        [i.get("accession_id") for i in invoices.values()] + [i["accession_id"] for i in issued]
        + [r["accession_id"] for r in ledger])}

    def mine(accession_id) -> bool:
        return accession_id in branch_of and (branches is None or branch_of[accession_id] in branches)

    names = {str(b["id"]): b["name"] for b in await _rows(scoped_query("branches", clinic_id, "id, name"))}
    zero = {"cash": 0, "upi": 0, "card": 0, "online": 0, "b2b_credit": 0, "collected": 0}
    days = {d: {"date": d, **zero} for d in _days(start, end)}
    by_branch: dict = {}
    for r in receipts:
        acc = (invoices.get(r["invoice_id"]) or {}).get("accession_id")
        if not mine(acc):
            continue
        amt = r["amount_paise"] * (-1 if r["kind"] == "refund" else 1)
        for row in (days[_ist_day(r["created_at"])],
                    by_branch.setdefault(branch_of[acc], {"branch_id": branch_of[acc], **zero})):
            row[PAY_MODES[r["mode"]]] += amt
            row["collected"] += amt
    for r in ledger:
        if not mine(r["accession_id"]):
            continue
        amt = r["amount_paise"] * (-1 if r["entry_type"] == "charge_reversal" else 1)
        days[_ist_day(r["created_at"])]["b2b_credit"] += amt
        by_branch.setdefault(branch_of[r["accession_id"]], {"branch_id": branch_of[r["accession_id"]], **zero})[
            "b2b_credit"] += amt
    issued = [i for i in issued if mine(i["accession_id"])]
    for b in by_branch.values():
        b["name"] = names.get(b["branch_id"], "—")
    # tests billed by department: visits registered in the range, active test lines, net of discount
    visits, truncated = await _visits_between(clinic_id, start, end, branches)
    items = [i for i in await _in("dx_accession_items", clinic_id, "accession_id, lab_test_id, kind, status, "
                                  "price_paise, discount_paise", "accession_id", [v["id"] for v in visits])
             if i["status"] == "active" and i["kind"] != "fee"]
    dept_of = await _test_departments(clinic_id, items)
    dnames = {d["id"]: d["name"] for d in await _rows(scoped_query("dx_departments", clinic_id, "id, name"))}
    by_dept: dict = {}
    for i in items:
        d = dept_of.get(i["lab_test_id"])
        if department_id and d != department_id:
            continue
        row = by_dept.setdefault(d, {"department_id": d, "name": dnames.get(d, "—"), "tests": 0, "billed_paise": 0})
        row["tests"] += 1
        row["billed_paise"] += i["price_paise"] - i["discount_paise"]
    dl = list(days.values())
    totals = {k: sum(r[k] for r in dl) for k in zero}
    totals["billed_paise"] = sum(i["total_paise"] for i in issued)
    totals["paid_paise"] = sum(i["paid_paise"] for i in issued)
    totals["dues_paise"] = sum(max(0, i["total_paise"] - (i.get("credited_paise") or 0) - i["paid_paise"])
                               for i in issued)
    return {"from": start.isoformat(), "to": end.isoformat(), "truncated": truncated, "days": dl, "totals": totals,
            "by_branch": sorted(by_branch.values(), key=lambda b: b["name"]),
            "by_department": sorted(by_dept.values(), key=lambda d: -d["billed_paise"])}


async def get_mis_rejections(clinic_id: str, start: date, end: date, site_ids: Optional[list[str]] = None) -> dict:
    """Rejected tubes by reason, collection site and collector; rate = rejected / tubes collected in the range."""
    check_range(start, end)
    lo, hi = _utc(start), _utc(end + timedelta(days=1))
    cols = "id, collection_site_id, collected_by, collected_by_name, rejection_reason_code"
    rejected = await _rows(scoped_query("dx_samples", clinic_id, cols).eq("status", "rejected")
                           .gte("rejected_at", lo).lt("rejected_at", hi))
    collected = await _rows(scoped_query("dx_samples", clinic_id, "id, collection_site_id")
                            .gte("collected_at", lo).lt("collected_at", hi))
    if site_ids is not None:
        rejected = [s for s in rejected if s["collection_site_id"] in site_ids]
        collected = [s for s in collected if s["collection_site_id"] in site_ids]
    reasons = {r["code"]: r["label"] for r in await dx.rejection_reasons(clinic_id, active_only=False)}
    sites = {s["id"]: s["display_name"] for s in await dx.list_sites(clinic_id)}

    def group(key, name) -> list[dict]:
        acc: dict = {}
        for s in rejected:
            acc.setdefault(key(s), {"id": key(s), "name": name(s), "rejected": 0})["rejected"] += 1
        return sorted(acc.values(), key=lambda r: (-r["rejected"], r["name"] or ""))

    n = len(collected)
    return {"from": start.isoformat(), "to": end.isoformat(), "rejected": len(rejected), "collected": n,
            "rate_percent": round(100 * len(rejected) / n, 2) if n else None,
            "by_reason": group(lambda s: s["rejection_reason_code"],
                               lambda s: reasons.get(s["rejection_reason_code"], s["rejection_reason_code"])),
            "by_site": group(lambda s: s["collection_site_id"], lambda s: sites.get(s["collection_site_id"], "—")),
            "by_collector": group(lambda s: s.get("collected_by"), lambda s: s.get("collected_by_name") or "—")}


async def get_mis_referrers(clinic_id: str, start: date, end: date, site_ids: Optional[list[str]] = None) -> dict:
    """Visits and tests per referrer. ADR-DX-11: counts only — no payout, incentive or commission."""
    check_range(start, end)
    visits, truncated = await _visits_between(clinic_id, start, end, await _site_branches(clinic_id, site_ids))
    visits = [v for v in visits if v.get("referrer_id")]
    tests: dict = {}
    for i in await _in("dx_accession_items", clinic_id, "accession_id, kind, status", "accession_id",
                       [v["id"] for v in visits]):
        if i["status"] == "active" and i["kind"] != "fee":
            tests[i["accession_id"]] = tests.get(i["accession_id"], 0) + 1
    names = {r["id"]: r["name"] for r in await _in("dx_referrers", clinic_id, "id, name", "id",
                                                   [v["referrer_id"] for v in visits])}
    out: dict = {}
    for v in visits:
        row = out.setdefault(v["referrer_id"], {"referrer_id": v["referrer_id"], "name": names.get(v["referrer_id"]),
                                                "visits": 0, "tests": 0})
        row["visits"] += 1
        row["tests"] += tests.get(v["id"], 0)
    return {"from": start.isoformat(), "to": end.isoformat(), "truncated": truncated,
            "referrers": sorted(out.values(), key=lambda r: (-r["tests"], r["name"] or ""))}


# ── CSV export ──────────────────────────────────────────────────────────────

def _cell(v) -> str:
    """A spreadsheet must never run a cell as a formula (CSV injection)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _rupees(p) -> str:
    return f"{(p or 0) / 100:.2f}"


async def export_rows(clinic_id: str, report: str, start: date, end: date,
                      site_ids: Optional[list[str]] = None) -> tuple[list[str], list[list]]:
    if report not in REPORTS:
        raise HTTPException(status_code=422, detail=f"Unknown report; choose one of {', '.join(REPORTS)}.")
    check_range(start, end)
    if report == "tat":
        d = await get_mis_tat(clinic_id, start, end, site_ids)
        return (["test", "tests", "finished", "median_minutes", "p90_minutes", "target_minutes", "breached_finished",
                 "breached_open", "breach_percent"],
                [[r["name"], r["tests"], r["finished"], r["median_minutes"], r["p90_minutes"], r["target_minutes"],
                  r["breached"], r["open_breached"], r["breach_percent"]] for r in d["tests"]])
    if report == "revenue":
        d = await get_mis_revenue(clinic_id, start, end, site_ids)
        return (["date", "cash", "upi", "card", "online", "collected", "b2b_credit"],
                [[r["date"], *(_rupees(r[k]) for k in ("cash", "upi", "card", "online", "collected", "b2b_credit"))]
                 for r in d["days"]])
    if report == "rejections":
        d = await get_mis_rejections(clinic_id, start, end, site_ids)
        return (["group", "name", "rejected"],
                [[g, r["name"], r["rejected"]] for g in ("by_reason", "by_site", "by_collector") for r in d[g]])
    if report == "referrers":
        d = await get_mis_referrers(clinic_id, start, end, site_ids)
        return ["referrer", "visits", "tests"], [[r["name"], r["visits"], r["tests"]] for r in d["referrers"]]
    # visits: operational columns only — no patient name or phone leaves in a spreadsheet
    visits, _ = await _visits_between(clinic_id, start, end, await _site_branches(clinic_id, site_ids))
    tests: dict = {}
    for i in await _in("dx_accession_items", clinic_id, "accession_id, kind, status", "accession_id",
                       [v["id"] for v in visits]):
        tests[i["accession_id"]] = tests.get(i["accession_id"], 0) + (i["status"] == "active" and i["kind"] != "fee")
    return (["visit_no", "date", "source", "billing_mode", "priority", "status", "tests"],
            [[v["accession_number"], _ist_day(v["created_at"]), v["source"], v["billing_mode"], v["priority"],
              v["status"], tests.get(v["id"], 0)] for v in visits])


async def export_mis_csv(clinic_id: str, report: str, start: date, end: date,
                         site_ids: Optional[list[str]] = None) -> AsyncIterator[str]:
    """The report as CSV text, one line per chunk (the route streams it)."""
    header, rows = await export_rows(clinic_id, report, start, end, site_ids)

    async def gen():
        for row in [header, *rows]:
            buf = io.StringIO()
            csv.writer(buf).writerow([_cell(c) for c in row])
            yield buf.getvalue()
    return gen()
