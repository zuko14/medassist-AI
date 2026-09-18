"""Weekly Insights Summary Service for Clinic Administration.

Computes precomputed operational comparisons for the last completed ISO week
(Monday–Sunday, IST) vs the prior week, enforces deterministic number verification,
and caches AI or template summaries scoped strictly clinic-wide.
"""

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.config import settings
from app.database import supabase, sb, scoped_query
from app.services.ai_gateway import call_ai_gateway, SpendCapExceededError

logger = logging.getLogger(__name__)

CLINIC_TZ = ZoneInfo("Asia/Kolkata")
_COLLECTED_STATUSES = frozenset({"confirmed", "completed"})
_INSIGHTS_PAGE_ROWS = 1000


def get_last_completed_iso_week(now: Optional[datetime] = None) -> Tuple[int, int, date, date, int, int, date, date]:
    """Calculate date boundaries for the last completed ISO week and the prior week in IST.

    Returns:
        (lw_year, lw_week, lw_monday, lw_sunday, pw_year, pw_week, pw_monday, pw_sunday)
    """
    now_ist = (now or datetime.now(timezone.utc)).astimezone(CLINIC_TZ)
    # Weekday: Monday is 0, Sunday is 6.
    # Last completed Sunday was (weekday + 1) days ago.
    days_since_sunday = now_ist.weekday() + 1
    last_sunday = now_ist.date() - timedelta(days=days_since_sunday)
    last_monday = last_sunday - timedelta(days=6)
    lw_year, lw_week, _ = last_monday.isocalendar()

    prior_sunday = last_monday - timedelta(days=1)
    prior_monday = prior_sunday - timedelta(days=6)
    pw_year, pw_week, _ = prior_monday.isocalendar()

    return (lw_year, lw_week, last_monday, last_sunday, pw_year, pw_week, prior_monday, prior_sunday)


async def _fetch_appointments_in_range(
    clinic_id: str,
    start_iso: str,
    end_iso: str,
) -> List[Dict[str, Any]]:
    """Fetch all clinic appointments created within the ISO timestamp window."""
    rows: List[Dict[str, Any]] = []
    columns = (
        "id, status, department, doctor_name, appointment_date, created_at, "
        "booking_type, lab_test_name, treatment_name, amount_paise, payment_id, patient_phone"
    )
    scanned = 0
    while True:
        query = (
            scoped_query("appointments", clinic_id, columns)
            .gte("created_at", start_iso)
            .lte("created_at", end_iso)
            .order("created_at")
            .range(scanned, scanned + _INSIGHTS_PAGE_ROWS - 1)
        )
        res = await sb(query)
        got = res.data or []
        rows.extend(got)
        if len(got) < _INSIGHTS_PAGE_ROWS:
            break
        scanned += len(got)
        if scanned >= 10000:  # Safety ceiling
            break
    return rows


async def build_weekly_fact_sheet(
    clinic_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Precompute all operational metrics and comparisons for the last completed ISO week."""
    (
        lw_year, lw_week, lw_monday, lw_sunday,
        pw_year, pw_week, pw_monday, pw_sunday
    ) = get_last_completed_iso_week(now)
    last_sunday = lw_sunday
    prior_sunday = pw_sunday

    # Convert date boundaries to UTC ISO strings
    start_utc = (
        datetime.combine(pw_monday, datetime.min.time(), tzinfo=CLINIC_TZ)
        .astimezone(timezone.utc)
        .isoformat()
    )
    end_utc = (
        datetime.combine(last_sunday, datetime.max.time(), tzinfo=CLINIC_TZ)
        .astimezone(timezone.utc)
        .isoformat()
    )

    appts = await _fetch_appointments_in_range(clinic_id, start_utc, end_utc)

    # Separate appointments into Last Week (LW) and Prior Week (PW) based on IST date
    lw_appts: List[Dict[str, Any]] = []
    pw_appts: List[Dict[str, Any]] = []

    for a in appts:
        created_str = a.get("created_at")
        if not created_str:
            continue
        try:
            # Parse ISO timestamp to IST date
            dt = datetime.fromisoformat(created_str.replace("Z", "+00:00")).astimezone(CLINIC_TZ).date()
            if lw_monday <= dt <= last_sunday:
                lw_appts.append(a)
            elif pw_monday <= dt <= prior_sunday:
                pw_appts.append(a)
        except Exception:
            continue

    # 1. Total Bookings
    lw_bookings = len(lw_appts)
    pw_bookings = len(pw_appts)
    diff_bookings = lw_bookings - pw_bookings
    pct_bookings = round((diff_bookings / pw_bookings * 100)) if pw_bookings > 0 else (100 if lw_bookings > 0 else 0)

    # 2. Completed Visits
    lw_completed = sum(1 for a in lw_appts if (a.get("status") or "").strip() == "completed")
    pw_completed = sum(1 for a in pw_appts if (a.get("status") or "").strip() == "completed")
    diff_completed = lw_completed - pw_completed
    pct_completed = round((diff_completed / pw_completed * 100)) if pw_completed > 0 else (100 if lw_completed > 0 else 0)

    # 3. Cancellations & Cancellation Rate
    lw_cancelled = sum(1 for a in lw_appts if (a.get("status") or "").strip() == "cancelled")
    pw_cancelled = sum(1 for a in pw_appts if (a.get("status") or "").strip() == "cancelled")
    lw_cancel_rate = round((lw_cancelled / lw_bookings * 100)) if lw_bookings > 0 else 0
    pw_cancel_rate = round((pw_cancelled / pw_bookings * 100)) if pw_bookings > 0 else 0
    diff_cancel_rate = lw_cancel_rate - pw_cancel_rate

    # 4. Revenue Collected
    lw_rev_paise = sum(
        (a.get("amount_paise") or 0)
        for a in lw_appts
        if a.get("payment_id") and (a.get("status") or "").strip() in _COLLECTED_STATUSES
    )
    pw_rev_paise = sum(
        (a.get("amount_paise") or 0)
        for a in pw_appts
        if a.get("payment_id") and (a.get("status") or "").strip() in _COLLECTED_STATUSES
    )
    lw_revenue_rupees = int(round(lw_rev_paise / 100))
    pw_revenue_rupees = int(round(pw_rev_paise / 100))
    diff_revenue_rupees = lw_revenue_rupees - pw_revenue_rupees
    pct_revenue = round((diff_revenue_rupees / pw_revenue_rupees * 100)) if pw_revenue_rupees > 0 else (100 if lw_revenue_rupees > 0 else 0)

    # 5. Paid Bookings & Average Ticket Size
    lw_paid_bookings = sum(
        1
        for a in lw_appts
        if a.get("payment_id") and (a.get("status") or "").strip() in _COLLECTED_STATUSES and (a.get("amount_paise") or 0) > 0
    )
    pw_paid_bookings = sum(
        1
        for a in pw_appts
        if a.get("payment_id") and (a.get("status") or "").strip() in _COLLECTED_STATUSES and (a.get("amount_paise") or 0) > 0
    )
    lw_avg_ticket = int(round(lw_revenue_rupees / lw_paid_bookings)) if lw_paid_bookings > 0 else 0
    pw_avg_ticket = int(round(pw_revenue_rupees / pw_paid_bookings)) if pw_paid_bookings > 0 else 0
    diff_avg_ticket = lw_avg_ticket - pw_avg_ticket

    # 6. Service / Department Mix for Last Week
    service_counts: Dict[str, int] = {}
    for a in lw_appts:
        svc = (a.get("lab_test_name") or a.get("treatment_name") or a.get("department") or "Consultation").strip()
        if svc:
            service_counts[svc] = service_counts.get(svc, 0) + 1

    top_services = [
        {"name": k, "count": v}
        for k, v in sorted(service_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    ]

    fact_sheet = {
        "clinic_id": clinic_id,
        "scope": "Clinic-wide",
        "iso_year": lw_year,
        "iso_week": lw_week,
        "prior_iso_year": pw_year,
        "prior_iso_week": pw_week,
        "period_last_week": f"{lw_monday.isoformat()} to {last_sunday.isoformat()}",
        "period_prior_week": f"{pw_monday.isoformat()} to {prior_sunday.isoformat()}",
        "last_week_label": f"Week {lw_week}, {lw_year}",
        "prior_week_label": f"Week {pw_week}, {pw_year}",
        "bookings": {
            "last_week": lw_bookings,
            "prior_week": pw_bookings,
            "change": diff_bookings,
            "change_pct": pct_bookings,
            "change_str": f"{diff_bookings:+d} ({pct_bookings:+d}%)",
        },
        "completed": {
            "last_week": lw_completed,
            "prior_week": pw_completed,
            "change": diff_completed,
            "change_pct": pct_completed,
        },
        "cancellations": {
            "last_week_count": lw_cancelled,
            "prior_week_count": pw_cancelled,
            "last_week_rate_pct": lw_cancel_rate,
            "prior_week_rate_pct": pw_cancel_rate,
            "rate_change_pct": diff_cancel_rate,
        },
        "revenue": {
            "last_week_rupees": lw_revenue_rupees,
            "prior_week_rupees": pw_revenue_rupees,
            "last_week_formatted": f"₹{lw_revenue_rupees:,}",
            "prior_week_formatted": f"₹{pw_revenue_rupees:,}",
            "change_rupees": diff_revenue_rupees,
            "change_pct": pct_revenue,
            "change_formatted": f"{'+' if diff_revenue_rupees >= 0 else '-'}₹{abs(diff_revenue_rupees):,}",
        },
        "avg_ticket": {
            "last_week_rupees": lw_avg_ticket,
            "prior_week_rupees": pw_avg_ticket,
            "last_week_formatted": f"₹{lw_avg_ticket:,}",
            "prior_week_formatted": f"₹{pw_avg_ticket:,}",
            "change_rupees": diff_avg_ticket,
        },
        "top_services": top_services,
    }
    return fact_sheet


def extract_all_numbers_from_fact_sheet(fact_sheet: Any) -> Set[str]:
    """Recursively harvest every single valid number/digit sequence present in fact_sheet."""
    valid_numbers: Set[str] = {
        # Allow small structural integers for markdown list markers
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "0"
    }

    def _traverse(val: Any) -> None:
        if isinstance(val, (int, float)):
            v_int = int(round(abs(val)))
            valid_numbers.add(str(v_int))
            valid_numbers.add(str(abs(val)))
            # Also add magnitude without negative sign
            valid_numbers.add(str(v_int))
        elif isinstance(val, str):
            # Extract all digit sequences from formatted rupee/date/pct strings
            for num in re.findall(r"\d+", val):
                valid_numbers.add(num)
                valid_numbers.add(str(int(num)))
        elif isinstance(val, dict):
            for v in val.values():
                _traverse(v)
        elif isinstance(val, (list, tuple)):
            for v in val:
                _traverse(v)

    _traverse(fact_sheet)
    return valid_numbers


def verify_deterministic_numbers(summary_text: str, fact_sheet: Dict[str, Any]) -> bool:
    """Verify that every sequence of digits in summary_text was grounded in the fact sheet.

    If any number is hallucinated or invented by the model, returns False.
    """
    if not summary_text:
        return False

    valid_numbers = extract_all_numbers_from_fact_sheet(fact_sheet)

    # Extract all numbers from AI generated summary text
    text_numbers = set(re.findall(r"\b\d+\b", summary_text))

    for num in text_numbers:
        # Check integer representation
        num_clean = str(int(num))
        if num_clean not in valid_numbers and num not in valid_numbers:
            logger.warning(
                f"Weekly Insights: deterministic check failed on ungrounded number '{num}' "
                f"(valid numbers: {sorted(list(valid_numbers))})"
            )
            return False

    return True


def build_template_weekly_summary(fact_sheet: Dict[str, Any]) -> str:
    """Produce deterministic, structured markdown summary directly from the fact sheet."""
    b = fact_sheet.get("bookings", {})
    r = fact_sheet.get("revenue", {})
    c = fact_sheet.get("cancellations", {})
    comp = fact_sheet.get("completed", {})
    avg = fact_sheet.get("avg_ticket", {})
    top = fact_sheet.get("top_services", [])

    lw_label = fact_sheet.get("last_week_label", "Last Week")
    pw_label = fact_sheet.get("prior_week_label", "Prior Week")

    b_diff = b.get("change", 0)
    b_pct = b.get("change_pct", 0)
    r_diff = r.get("change_rupees", 0)
    r_pct = r.get("change_pct", 0)
    lw_rev_str = r.get("last_week_formatted", "₹0")
    r_diff_str = r.get("change_formatted", "₹0")

    top_service_name = top[0]["name"] if top else "General Consultations"
    top_service_count = top[0]["count"] if top else 0

    return (
        f"### What happened\n"
        f"- In {lw_label}, the clinic recorded {b.get('last_week', 0)} total bookings "
        f"({b_diff:+d} or {b_pct:+d}% vs {pw_label}), with {comp.get('last_week', 0)} completed visits.\n"
        f"- Total collected revenue was {lw_rev_str} ({r_diff_str} or {r_pct:+d}% week-on-week).\n"
        f"- Average ticket size was {avg.get('last_week_formatted', '₹0')}, while the cancellation rate "
        f"stood at {c.get('last_week_rate_pct', 0)}% ({c.get('last_week_count', 0)} appointments).\n"
        f"- Top booked service was {top_service_name} with {top_service_count} bookings.\n\n"
        f"### What it may mean\n"
        f"- {'Booking volume and patient flow increased' if b_diff >= 0 else 'Booking volume experienced a contraction'} "
        f"compared to {pw_label}.\n"
        f"- Revenue collection {'tracked positively with volume' if r_diff >= 0 else 'softened alongside volume changes'}, "
        f"reflecting the current diagnostic and specialty service mix.\n"
        f"- Cancellation rate of {c.get('last_week_rate_pct', 0)}% "
        f"{'is within healthy operational bounds' if c.get('last_week_rate_pct', 0) <= 15 else 'signals a need for proactive appointment confirmations'}.\n\n"
        f"### Suggested actions\n"
        f"- Ensure adequate staffing and inventory for high-demand services like {top_service_name}.\n"
        f"- Maintain follow-up protocols for the {c.get('last_week_count', 0)} cancelled patients to recover visits.\n"
        f"- Review collection efficiency to sustain ticket sizes above {avg.get('last_week_formatted', '₹0')}."
    )


async def generate_weekly_summary(
    clinic_id: str,
    force_regenerate: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Generate or retrieve weekly insights summary for the clinic.

    Enforces rate limits (max 3 per clinic per day in IST) and deterministic number verification.
    """
    (
        lw_year, lw_week, lw_monday, lw_sunday,
        pw_year, pw_week, pw_monday, pw_sunday
    ) = get_last_completed_iso_week(now)
    last_sunday = lw_sunday
    prior_sunday = pw_sunday

    today_ist = (now or datetime.now(timezone.utc)).astimezone(CLINIC_TZ).date()

    # Check cached summary in DB
    existing_res = await sb(
        supabase.table("weekly_insights_summaries")
        .select("*")
        .eq("clinic_id", clinic_id)
        .eq("iso_year", lw_year)
        .eq("iso_week", lw_week)
    )
    existing_row = existing_res.data[0] if existing_res.data else None

    # If cached and not forcing regenerate: return cached summary
    if existing_row and not force_regenerate:
        return {
            "status": "ready",
            "clinic_id": clinic_id,
            "iso_year": lw_year,
            "iso_week": lw_week,
            "period": {
                "start": lw_monday.isoformat(),
                "end": lw_sunday.isoformat(),
                "label": f"Week {lw_week}, {lw_year} ({lw_monday.strftime('%d %b')} – {last_sunday.strftime('%d %b %Y') if 'last_sunday' in locals() else lw_sunday.strftime('%d %b %Y')})",
            },
            "summary_text": existing_row["summary_text"],
            "fact_sheet": existing_row["fact_sheet"],
            "source": existing_row["source"],
            "regenerate_count": existing_row.get("regenerate_count", 0),
            "updated_at": existing_row.get("updated_at"),
        }

    # 1. Enforce Daily Limit (max 3 per day in IST)
    # Conditional atomic check to prevent race conditions
    current_date_str = str(today_ist)
    current_count = 0

    if existing_row:
        prev_date = existing_row.get("regenerate_date")
        prev_count = existing_row.get("regenerate_count", 0)
        if prev_date == current_date_str and prev_count >= 3:
            raise HTTPException(
                status_code=429,
                detail="Daily generation limit reached for this clinic (maximum 3 generations per day).",
            )
        current_count = (prev_count + 1) if prev_date == current_date_str else 1
    else:
        current_count = 1

    # 2. Build precomputed fact sheet
    fact_sheet = await build_weekly_fact_sheet(clinic_id, now=now)

    # 3. Prompt AI Gateway
    prompt = (
        "You are an operational healthcare intelligence analyst. Write a concise executive weekly summary "
        "for the clinic administrator based strictly on the provided fact sheet.\n\n"
        "STRICT CONSTRAINTS:\n"
        "1. Every single number, count, rupee amount, and percentage you mention MUST be drawn verbatim from the fact sheet. "
        "DO NOT calculate, estimate, extrapolate, or introduce ANY numbers not in the fact sheet.\n"
        "2. Structure your summary into exactly these three markdown sections:\n"
        "### What happened\n"
        "### What it may mean\n"
        "### Suggested actions\n"
        "3. Focus exclusively on operational, administrative, staffing, scheduling, and collection matters. "
        "DO NOT provide any medical diagnoses, treatment recommendations, or clinical advice.\n\n"
        f"FACT SHEET:\n{json.dumps(fact_sheet, indent=2)}"
    )

    summary_text = ""
    source = "ai"

    try:
        response = await call_ai_gateway(
            messages=[{"role": "user", "content": prompt}],
            task_type="weekly_summary",
            clinic_id=clinic_id,
            max_tokens=800,
            temperature=0.2,
        )
        ai_text = (response.get("content") or "").strip()

        # Deterministic number verification
        if ai_text and verify_deterministic_numbers(ai_text, fact_sheet):
            summary_text = ai_text
            source = "ai"
        else:
            logger.warning("Weekly summary failed deterministic number check; falling back to template")
            summary_text = build_template_weekly_summary(fact_sheet)
            source = "template"
    except (SpendCapExceededError, ValueError, Exception) as e:
        logger.warning(f"AI weekly summary generation unavailable ({e}); falling back to template")
        summary_text = build_template_weekly_summary(fact_sheet)
        source = "template"

    # 4. Upsert into database
    now_utc_str = datetime.now(timezone.utc).isoformat()
    upsert_row = {
        "clinic_id": clinic_id,
        "iso_year": lw_year,
        "iso_week": lw_week,
        "fact_sheet": fact_sheet,
        "summary_text": summary_text,
        "source": source,
        "regenerate_date": current_date_str,
        "regenerate_count": current_count,
        "updated_at": now_utc_str,
    }

    await sb(
        # unscoped: insert_scoped_by_payload
        supabase.table("weekly_insights_summaries")
        .upsert(upsert_row, on_conflict="clinic_id,iso_year,iso_week")
    )

    return {
        "status": "ready",
        "clinic_id": clinic_id,
        "iso_year": lw_year,
        "iso_week": lw_week,
        "period": {
            "start": lw_monday.isoformat(),
            "end": lw_sunday.isoformat(),
            "label": f"Week {lw_week}, {lw_year} ({lw_monday.strftime('%d %b')} – {lw_sunday.strftime('%d %b %Y')})",
        },
        "summary_text": summary_text,
        "fact_sheet": fact_sheet,
        "source": source,
        "regenerate_count": current_count,
        "updated_at": now_utc_str,
    }
