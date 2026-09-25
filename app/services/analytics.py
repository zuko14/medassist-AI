"""Analytics service for tracking events and metrics."""

import logging
import re
import statistics
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.database import (
    get_genuine_patients,
    log_analytics_event,
    restrict_to_branch,
    scoped_query,
    supabase,
)
from app.database import sb  # T5.1: off-loop query execution
from app.services.subscription import parse_timestamp

logger = logging.getLogger(__name__)


# ── Insights (admin panel → Insights page) ──────────────────────────────────
#
# ONE payload feeds every chart on that page, because every booking, service
# and revenue series below is counted from the SAME appointment rows. A
# chart-per-endpoint design would re-read the same window five more times on
# every page view, and the numbers could then disagree with each other.

#: The clinic's own calendar. Days are bucketed in IST, never UTC: at UTC
#: midnight it is already 05:30 in India, so a UTC-keyed series attributes every
#: booking taken between 00:00 and 05:30 IST to the previous day. That is the
#: same defect GET /admin/diagnostic/stats had to fix for its "today" tiles.
CLINIC_TZ = ZoneInfo("Asia/Kolkata")

#: PostgREST caps any single response at 1000 rows, and these series are counted
#: in Python, so a clinic past 1000 bookings in the window would silently plot a
#: truncated month. Page, exactly as get_lab_tests() does for the catalogue.
_INSIGHTS_PAGE_ROWS = 1000
#: Refuse to page forever if a filter ever matches unboundedly.
_INSIGHTS_MAX_ROWS = 60000
#: Longest window the page may ask for — bounds both the row scan and the
#: number of points a browser has to draw.
_INSIGHTS_MAX_DAYS = 365

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

#: Money counts as collected only when a gateway payment id exists AND the
#: booking still stands. "completed" is included deliberately: the nightly job
#: in app/services/scheduler.py auto-completes every past confirmed
#: appointment, so a confirmed-only rule would erase yesterday's takings from
#: the chart every morning.
_COLLECTED_STATUSES = frozenset({"confirmed", "completed"})
_PENDING_STATUSES = frozenset({"pending_payment", "pending_review"})


async def _fetch_window(
    table: str,
    clinic_id: str,
    columns: str,
    ts_column: str,
    since_iso: str,
    branch_id: Optional[str] = None,
) -> list:
    """Every row of `table` for one clinic since `since_iso`, paged and scoped."""
    rows: list = []
    while True:
        query = scoped_query(table, clinic_id, columns).gte(ts_column, since_iso)
        if branch_id:
            query = query.eq("branch_id", branch_id)
        page = await sb(
            query.order(ts_column).range(len(rows), len(rows) + _INSIGHTS_PAGE_ROWS - 1)
        )
        got = page.data or []
        rows.extend(got)
        # A short page means the window is exhausted, so a small clinic still
        # costs exactly one round trip. Keep _INSIGHTS_PAGE_ROWS equal to the
        # server's own cap, or a short first page becomes ambiguous.
        if len(got) < _INSIGHTS_PAGE_ROWS:
            break
        if len(rows) >= _INSIGHTS_MAX_ROWS:
            logger.error(
                f"Insights hit the {_INSIGHTS_MAX_ROWS}-row ceiling on '{table}' "
                f"for clinic {clinic_id} — series are truncated"
            )
            break
    return rows


def dashboard_period_start(days: int) -> str:
    """First day of the dashboard's rolling window, exactly as its tiles filter it.

    Shared with list_appointments() so a tile's number and the list it opens
    are cut on the same boundary. It is the server's calendar rather than IST
    on purpose: that is what the tiles have always counted, and moving it would
    shift every live dashboard's numbers.
    """
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _appointment_window(
    query,
    basis: str,
    date_from: Optional[date],
    date_to: Optional[date],
    period_days: Optional[int],
):
    """Apply the Appointments page's date filter to an appointments query.

    One function for both the status-count scan and the page query, so the
    chips, the pager total and the rows can never disagree about the window.
    """
    if period_days is not None:
        return query.gte("created_at", dashboard_period_start(period_days))
    if date_from is None or date_to is None:
        # Fail closed: an unbounded window would scan the clinic's whole history.
        raise ValueError("date_from and date_to are required unless period_days is given")
    if basis == "booked":
        # created_at is a UTC timestamptz; "booked on 5 Sep" means 5 Sep in
        # the clinic's calendar, i.e. from 4 Sep 18:30 UTC.
        start = datetime.combine(date_from, time.min, tzinfo=CLINIC_TZ)
        end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=CLINIC_TZ)
        return query.gte("created_at", start.astimezone(timezone.utc).isoformat()).lt(
            "created_at", end.astimezone(timezone.utc).isoformat()
        )
    return query.gte("appointment_date", date_from.isoformat()).lte(
        "appointment_date", date_to.isoformat()
    )


#: Anything PostgREST's filter grammar or LIKE treats specially, plus control
#: characters. User text is split on these, never passed through, so a search
#: can only ever become the one ilike pattern built below.
_SEARCH_SPLIT_RE = re.compile(r"[\s,()*%_\\\"'.:;!|&=<>\[\]{}\x00-\x1f\x7f]+")
_SEARCH_PHONE_RE = re.compile(r"\+?[\d\s-]+")
#: Searchable columns: who (patient name/phone), what (booking ref) and with
#: whom (doctor or lab test), so a front desk can find a visit from any of them.
_SEARCH_TEXT_COLUMNS = ("patient_name", "booking_ref", "doctor_name", "lab_test_name")


def appointment_search_filter(raw: Optional[str]) -> Optional[str]:
    """PostgREST or-filter for the Appointments search box, or None if too short.

    Words match in order with anything between them, so "priya sharma" finds
    "Dr. Priya K Sharma". A phone-like term matches on digits only, and on the
    last ten of them, so "+91 98765 43210" finds a number stored as 9876543210
    and one stored as 919876543210 alike.
    """
    raw = raw or ""
    tokens = [t for t in _SEARCH_SPLIT_RE.split(raw) if t]
    if sum(len(t) for t in tokens) < 2:
        return None
    pattern = "*" + "*".join(tokens) + "*"
    clauses = [f"{col}.ilike.{pattern}" for col in _SEARCH_TEXT_COLUMNS]
    if _SEARCH_PHONE_RE.fullmatch(raw.strip()):
        digits = re.sub(r"\D", "", raw)[-10:]
        if len(digits) >= 3:
            clauses.append(f"patient_phone.ilike.*{digits}*")
    return ",".join(clauses)


def _ist_day(value) -> Optional[date]:
    """Bucket a Supabase timestamptz into the clinic's own calendar day."""
    dt = parse_timestamp(value)
    return dt.astimezone(CLINIC_TZ).date() if dt else None


def _ranked(counts: dict, limit: int = 8, value_key: str = "count") -> list:
    """Top `limit` entries of a name -> number map, largest first."""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"name": name, value_key: value} for name, value in ordered[:limit]]


def _pct(part: int, whole: int) -> float:
    return round(part * 100.0 / whole, 1) if whole else 0.0


def _booking_and_revenue(appts: list, day_index: dict, span: int) -> tuple:
    """Booking, service and revenue series from ONE pass over the appointments."""
    created = [0] * span
    confirmed_by_day = [0] * span
    cancelled_by_day = [0] * span
    completed_by_day = [0] * span
    collected_by_day = [0.0] * span
    refunded_by_day = [0.0] * span

    status_mix: dict = {}
    departments: dict = {}
    services: dict = {}
    doctors: dict = {}
    revenue_by_service: dict = {}
    booked_hours = [0] * 24
    visit_weekdays = [0] * 7
    patient_visits: dict = {}

    totals = {
        "total": 0, "confirmed": 0, "cancelled": 0, "completed": 0,
        "no_show": 0, "consultations": 0, "lab_tests": 0,
    }
    collected_paise = refunded_paise = pending_paise = 0
    paid_count = 0

    for a in appts:
        status = (a.get("status") or "unknown").strip() or "unknown"
        booking_type = a.get("booking_type") or "consultation"
        amount = a.get("amount_paise") or 0
        slot = day_index.get(_ist_day(a.get("created_at")))

        totals["total"] += 1
        if status in ("confirmed", "cancelled", "completed", "no_show"):
            totals[status] += 1
        totals["lab_tests" if booking_type == "lab_test" else "consultations"] += 1
        status_mix[status] = status_mix.get(status, 0) + 1

        phone = a.get("patient_phone")
        if phone:
            patient_visits[phone] = patient_visits.get(phone, 0) + 1

        department = (a.get("department") or "").strip()
        if booking_type == "consultation" and department:
            departments[department] = departments.get(department, 0) + 1

        # What the patient actually bought: the test for a lab booking, the
        # department for a consultation. One axis, so a centre running both can
        # read its whole service mix off a single chart.
        if booking_type == "lab_test":
            service = (a.get("lab_test_name") or "").strip() or "Lab Test"
        elif (a.get("treatment_name") or "").strip():
            # Specialty clinics: the treatment is what the patient came for.
            service = a["treatment_name"].strip()
        else:
            service = department or "Consultation"
        services[service] = services.get(service, 0) + 1

        doctor = (a.get("doctor_name") or "").strip()
        if doctor:
            doctors[doctor] = doctors.get(doctor, 0) + 1

        if slot is not None:
            created[slot] += 1
            if status == "confirmed":
                confirmed_by_day[slot] += 1
            elif status == "cancelled":
                cancelled_by_day[slot] += 1
            elif status == "completed":
                completed_by_day[slot] += 1

        booked_at = parse_timestamp(a.get("created_at"))
        if booked_at:
            booked_hours[booked_at.astimezone(CLINIC_TZ).hour] += 1

        try:
            visit_weekdays[date.fromisoformat(a["appointment_date"]).weekday()] += 1
        except (KeyError, TypeError, ValueError):
            pass

        if amount:
            if status == "refunded":
                refunded_paise += amount
                if slot is not None:
                    refunded_by_day[slot] += amount / 100
            elif a.get("payment_id") and status in _COLLECTED_STATUSES:
                collected_paise += amount
                paid_count += 1
                revenue_by_service[service] = (
                    revenue_by_service.get(service, 0) + amount
                )
                if slot is not None:
                    collected_by_day[slot] += amount / 100
            elif status in _PENDING_STATUSES:
                pending_paise += amount

    total = totals["total"]
    repeat_patients = sum(1 for n in patient_visits.values() if n > 1)

    bookings = {
        "series": {
            "created": created,
            "confirmed": confirmed_by_day,
            "cancelled": cancelled_by_day,
            "completed": completed_by_day,
        },
        "status_mix": [
            {"name": name, "count": count}
            for name, count in sorted(
                status_mix.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ],
        "by_department": _ranked(departments),
        "by_service": _ranked(services),
        "top_doctors": _ranked(doctors, limit=6),
        "peak_hours": [{"hour": h, "count": booked_hours[h]} for h in range(24)],
        "by_weekday": [
            {"name": _WEEKDAY_NAMES[i], "count": visit_weekdays[i]} for i in range(7)
        ],
        "totals": totals,
        "unique_patients": len(patient_visits),
        "repeat_patients": repeat_patients,
        "repeat_rate": _pct(repeat_patients, len(patient_visits)),
        "completion_rate": _pct(totals["completed"], total),
        "cancellation_rate": _pct(totals["cancelled"], total),
        "no_show_rate": _pct(totals["no_show"], total),
    }

    revenue = {
        "series": {"collected": collected_by_day, "refunded": refunded_by_day},
        "collected_inr": round(collected_paise / 100, 2),
        "refunded_inr": round(refunded_paise / 100, 2),
        "pending_inr": round(pending_paise / 100, 2),
        "paid_count": paid_count,
        "avg_ticket_inr": (
            round(collected_paise / 100 / paid_count, 2) if paid_count else 0.0
        ),
        "by_service": [
            {"name": name, "inr": round(paise / 100, 2)}
            for name, paise in sorted(
                revenue_by_service.items(), key=lambda kv: (-kv[1], kv[0])
            )[:8]
        ],
    }
    return bookings, revenue


#: Patient taps logged by the WhatsApp lab flow (conversation.py): a service
#: type opened, and a test card opened. Interest, as opposed to bookings.
_LAB_INTEREST_EVENTS = ("lab_category_viewed", "lab_test_viewed")
_UNFILED_LAB_LABEL = "Lab Tests"  # conversation.LAB_UNCATEGORISED_LABEL


def _diagnostics_insights(appts: list, catalogue: list, events: list) -> dict:
    """Service-type performance for a centre that sells tests: what patients
    booked, what they paid, and what they looked at without booking.

    A booking's service type is its test's CURRENT heading (by id, then by
    name), so filing the catalogue re-files history too -- the question a
    centre asks is "how are my packages doing", not "how were they filed".
    """
    by_id = {str(t.get("id")): t for t in catalogue if t.get("id")}
    by_name = {(t.get("name") or "").strip().lower(): t for t in catalogue}

    def heading(test: Optional[dict]) -> str:
        return ((test or {}).get("category") or "").strip() or _UNFILED_LAB_LABEL

    types: dict = {}

    def bucket(name: str) -> dict:
        return types.setdefault(name, {
            "name": name, "bookings": 0, "revenue_paise": 0, "views": 0,
            "_viewers": set(), "_bookers": set(),
        })

    tests: dict = {}
    lab_bookings = revenue_paise = 0
    for a in appts:
        if (a.get("booking_type") or "") != "lab_test":
            continue
        test = by_id.get(str(a.get("lab_test_id"))) or by_name.get((a.get("lab_test_name") or "").strip().lower())
        kind = heading(test)
        b = bucket(kind)
        b["bookings"] += 1
        lab_bookings += 1
        if a.get("patient_phone"):
            b["_bookers"].add(a["patient_phone"])
        paid = 0
        if a.get("payment_id") and (a.get("status") or "") in _COLLECTED_STATUSES:
            paid = a.get("amount_paise") or 0
        b["revenue_paise"] += paid
        revenue_paise += paid
        name = (a.get("lab_test_name") or (test or {}).get("name") or "Lab Test").strip()
        row = tests.setdefault(name, {"name": name, "category": kind, "bookings": 0, "revenue_paise": 0, "views": 0})
        row["bookings"] += 1
        row["revenue_paise"] += paid

    category_views = test_views = 0
    for e in events:
        meta = e.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        kind = (meta.get("category") or "").strip() or _UNFILED_LAB_LABEL
        b = bucket(kind)
        if e.get("phone"):
            b["_viewers"].add(e["phone"])
        if e.get("event_type") == "lab_category_viewed":
            b["views"] += 1
            category_views += 1
        elif e.get("event_type") == "lab_test_viewed":
            test_views += 1
            name = (meta.get("test_name") or "").strip()
            if name:
                row = tests.setdefault(name, {"name": name, "category": kind, "bookings": 0,
                                              "revenue_paise": 0, "views": 0})
                row["views"] += 1

    by_type = []
    for b in types.values():
        interested = b["_viewers"] | b["_bookers"]
        by_type.append({
            "name": b["name"],
            "bookings": b["bookings"],
            "revenue_inr": round(b["revenue_paise"] / 100, 2),
            "views": b["views"],
            "interested_patients": len(interested),
            # Of the patients who looked at this service type, how many booked
            # it. Bookers count as interested, so this can never exceed 100.
            "conversion_pct": _pct(len(b["_bookers"]), len(interested)),
        })
    by_type.sort(key=lambda r: (-r["bookings"], -r["interested_patients"], r["name"]))

    def public(r: dict) -> dict:
        return {"name": r["name"], "category": r["category"], "bookings": r["bookings"],
                "revenue_inr": round(r["revenue_paise"] / 100, 2), "views": r["views"]}

    rows = list(tests.values())
    return {
        "by_service_type": by_type,
        "top_tests": [public(r) for r in sorted(rows, key=lambda r: (-r["bookings"], -r["views"], r["name"]))
                      if r["bookings"]][:10],
        # Looked at, not (yet) booked: where a follow-up call earns money.
        "most_viewed": [public(r) for r in sorted(rows, key=lambda r: (-r["views"], -r["bookings"], r["name"]))
                        if r["views"]][:10],
        "totals": {
            "lab_bookings": lab_bookings,
            "revenue_inr": round(revenue_paise / 100, 2),
            "category_views": category_views,
            "test_views": test_views,
        },
    }


async def _fetch_lab_interest_events(clinic_id: str, since_iso: str) -> list:
    """Only the two lab-interest event types, paged like _fetch_window."""
    rows: list = []
    while True:
        query = (
            scoped_query("analytics_events", clinic_id, "event_type,phone,metadata,created_at")
            .in_("event_type", list(_LAB_INTEREST_EVENTS))
            .gte("created_at", since_iso)
        )
        page = await sb(query.order("created_at").range(len(rows), len(rows) + _INSIGHTS_PAGE_ROWS - 1))
        got = page.data or []
        rows.extend(got)
        if len(got) < _INSIGHTS_PAGE_ROWS or len(rows) >= _INSIGHTS_MAX_ROWS:
            break
    return rows


async def _fetch_lab_catalogue(clinic_id: str) -> list:
    rows: list = []
    while True:
        page = await sb(
            scoped_query("lab_tests", clinic_id, "id,name,category")
            .order("id").range(len(rows), len(rows) + _INSIGHTS_PAGE_ROWS - 1)
        )
        got = page.data or []
        rows.extend(got)
        if len(got) < _INSIGHTS_PAGE_ROWS or len(rows) >= _INSIGHTS_MAX_ROWS:
            break
    return rows


def _report_delivery(reports: list, day_index: dict, span: int) -> dict:
    """Report-delivery series, for plans whose clinics dispatch lab reports."""
    delivered = [0] * span
    failed = [0] * span
    needs_review = [0] * span

    outcome_mix: dict = {}
    by_type: dict = {}
    turnarounds: list = []
    abnormal = 0

    for r in reports:
        status = (r.get("status") or "pending").strip() or "pending"
        outcome_mix[status] = outcome_mix.get(status, 0) + 1

        slot = day_index.get(_ist_day(r.get("uploaded_at")))
        if slot is not None:
            if status == "sent":
                delivered[slot] += 1
            elif status == "failed":
                failed[slot] += 1
            elif status == "needs_review":
                needs_review[slot] += 1

        report_type = (r.get("report_type") or "").strip() or "General"
        by_type[report_type] = by_type.get(report_type, 0) + 1

        if r.get("has_abnormal_values"):
            abnormal += 1

        uploaded_at = parse_timestamp(r.get("uploaded_at"))
        sent_at = parse_timestamp(r.get("sent_at"))
        if uploaded_at and sent_at and sent_at >= uploaded_at:
            turnarounds.append((sent_at - uploaded_at).total_seconds() / 60)

    total = len(reports)
    sent_total = outcome_mix.get("sent", 0)
    return {
        "series": {
            "delivered": delivered,
            "failed": failed,
            "needs_review": needs_review,
        },
        "outcome_mix": [
            {"name": name, "count": count}
            for name, count in sorted(
                outcome_mix.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ],
        "by_type": _ranked(by_type),
        "total": total,
        "delivered_total": sent_total,
        "failed_total": outcome_mix.get("failed", 0),
        "needs_review_total": outcome_mix.get("needs_review", 0),
        "abnormal_total": abnormal,
        "delivery_rate": _pct(sent_total, total),
        # Median, not mean: one report stuck in a retry loop drags a mean into
        # nonsense while the typical turnaround is still minutes.
        "median_turnaround_minutes": (
            round(statistics.median(turnarounds), 1) if turnarounds else None
        ),
    }


class AnalyticsService:
    """Service for analytics and reporting."""

    async def track_event(
        self,
        phone: str,
        event_type: str,
        clinic_id: str = "default",
        department: Optional[str] = None,
        intent: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Track an analytics event."""
        return await log_analytics_event(
            clinic_id,
            phone,
            event_type,
            department=department,
            intent=intent,
            metadata=metadata or {},
        )

    async def get_dashboard_stats(self, clinic_id: str, days: int = 30) -> dict:
        """Get dashboard statistics."""
        try:
            from_date = dashboard_period_start(days)

            # Fetch all appointments in period and count in Python
            query = (
                supabase.table("appointments")
                .select("status,department,created_at")
                .gte("created_at", from_date)
            )
            query = query.eq("clinic_id", clinic_id)
            all_appts = await sb(query)
            appts = all_appts.data or []

            total_appointments = len(appts)
            confirmed = sum(1 for a in appts if a.get("status") == "confirmed")
            cancelled = sum(1 for a in appts if a.get("status") == "cancelled")
            completed = sum(1 for a in appts if a.get("status") == "completed")
            no_show = sum(1 for a in appts if a.get("status") == "no_show")

            # Department breakdown
            dept_counts = {}
            for a in appts:
                d = a.get("department", "Unknown")
                dept_counts[d] = dept_counts.get(d, 0) + 1
            by_department = sorted(
                [{"department": k, "count": v} for k, v in dept_counts.items()],
                key=lambda x: x["count"],
                reverse=True,
            )

            # Genuine Patients (only patients with confirmed clinical engagement)
            genuine_patients = await get_genuine_patients(clinic_id)
            total_patients = len(genuine_patients)
            new_patients = sum(
                1 for p in genuine_patients if p.get("created_at", "") >= from_date
            )

            return {
                "period_days": days,
                # The Patients page filters "new" on this exact string, so the
                # New Patients tile and the list it opens always agree.
                "period_start": from_date,
                "total_appointments": total_appointments,
                "confirmed": confirmed,
                "cancelled": cancelled,
                "completed": completed,
                "no_show": no_show,
                "new_patients": new_patients,
                "total_patients": total_patients,
                "by_department": by_department,
            }

        except Exception as e:
            logger.error(f"Error getting dashboard stats: {e}")
            return {
                "period_days": days,
                "total_appointments": 0,
                "confirmed": 0,
                "cancelled": 0,
                "completed": 0,
                "no_show": 0,
                "new_patients": 0,
                "total_patients": 0,
                "by_department": [],
                "error": str(e),
            }

    async def get_recent_appointments(
        self, clinic_id: str, limit: int = 20, branch_id: Optional[str] = None
    ) -> list:
        """Get recent appointments."""
        try:
            query = (
                supabase.table("appointments")
                .select("*")
                .order("created_at", desc=True)
                .limit(limit)
            )
            query = restrict_to_branch(query.eq("clinic_id", clinic_id), branch_id)
            result = await sb(query)
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting recent appointments: {e}")
            return []

    async def get_upcoming_appointments(
        self, clinic_id: str, days: int = 7, branch_id: Optional[str] = None
    ) -> list:
        """Get upcoming appointments."""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

            query = (
                supabase.table("appointments")
                .select("*")
                .gte("appointment_date", today)
                .lte("appointment_date", end_date)
                .order("appointment_date")
                .order("appointment_time")
            )
            query = restrict_to_branch(query.eq("clinic_id", clinic_id), branch_id)
            result = await sb(query)

            return result.data or []
        except Exception as e:
            logger.error(f"Error getting upcoming appointments: {e}")
            return []

    async def list_appointments(
        self,
        clinic_id: str,
        *,
        basis: str = "visit",
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        period_days: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        branch_id: Optional[str] = None,
        search: Optional[str] = None,
    ) -> dict:
        """One page of the admin Appointments list, with per-status counts.

        basis "visit" filters on appointment_date, "booked" on created_at in
        the clinic's calendar. period_days is the dashboard tiles' own window
        (booked basis only), so a tile and the list it opens show one number.

        search (an appointment_search_filter() term) narrows the window; with
        no dates at all it spans every date, newest first — the search itself
        is then what bounds the scan.

        Raises on a database failure instead of returning an empty page: "no
        appointments on 5 Sep" must never be what an outage looks like.
        """
        search_filter = appointment_search_filter(search) if search else None
        if search and not search_filter:
            raise ValueError("search term is too short")
        all_dates = bool(search_filter) and period_days is None and date_from is None and date_to is None

        def narrowed(query):
            if not all_dates:
                query = _appointment_window(query, basis, date_from, date_to, period_days)
            return query.or_(search_filter) if search_filter else query

        # Counts cover the whole window, ignoring the status filter, so every
        # chip keeps its number while one is selected. Only `status` is read,
        # and it is paged because PostgREST caps a response at 1000 rows.
        # ponytail: Python-side count, a GROUP BY RPC if windows reach _INSIGHTS_MAX_ROWS.
        summary: dict = {}
        scanned = 0
        truncated = False
        while True:
            page = await sb(
                narrowed(
                    restrict_to_branch(
                        scoped_query("appointments", clinic_id, "status"), branch_id
                    )
                )
                .order("id")
                .range(scanned, scanned + _INSIGHTS_PAGE_ROWS - 1)
            )
            got = page.data or []
            for row in got:
                if row.get("status"):
                    summary[row["status"]] = summary.get(row["status"], 0) + 1
            scanned += len(got)
            if len(got) < _INSIGHTS_PAGE_ROWS:
                break
            if scanned >= _INSIGHTS_MAX_ROWS:
                truncated = True
                logger.error(
                    f"Appointments list hit the {_INSIGHTS_MAX_ROWS}-row ceiling "
                    f"for clinic {clinic_id} — counts are truncated"
                )
                break

        total = summary.get(status, 0) if status else scanned
        rows: list = []
        if truncated or offset < total:
            query = narrowed(
                restrict_to_branch(scoped_query("appointments", clinic_id, "*"), branch_id)
            )
            if status:
                query = query.eq("status", status)
            if all_dates:
                # A search across all history: the latest visit is the likeliest one.
                query = (
                    query.order("appointment_date", desc=True)
                    .order("appointment_time", desc=True)
                    .order("id", desc=True)
                )
            elif period_days is not None or basis == "booked":
                query = query.order("created_at", desc=True).order("id", desc=True)
            else:
                query = (
                    query.order("appointment_date")
                    .order("appointment_time")
                    .order("id")
                )
            rows = (await sb(query.range(offset, offset + limit - 1))).data or []

        return {
            "appointments": rows,
            "total": total,
            "window_total": scanned,
            "summary": summary,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
        }

    async def get_popular_departments(self, clinic_id: str, days: int = 30) -> list:
        """Get most popular departments."""
        try:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

            query = (
                supabase.table("appointments")
                .select("department")
                .gte("created_at", from_date)
            )
            query = query.eq("clinic_id", clinic_id)
            result = await sb(query)

            dept_counts = {}
            for row in result.data:
                dept = row["department"]
                dept_counts[dept] = dept_counts.get(dept, 0) + 1

            # Sort by count
            sorted_depts = sorted(dept_counts.items(), key=lambda x: x[1], reverse=True)
            return [{"department": d[0], "count": d[1]} for d in sorted_depts]

        except Exception as e:
            logger.error(f"Error getting popular departments: {e}")
            return []

    async def get_insights(
        self,
        clinic_id: str,
        days: int = 30,
        branch_id: Optional[str] = None,
        include_reports: bool = False,
        include_diagnostics: bool = False,
    ) -> dict:
        """Every chart series behind the admin panel's Insights page.

        Read-only and side-effect free — it writes nothing and touches no
        booking, payment or delivery path. A section that fails is returned as
        null and named in `errors`, so one bad query renders a partial page
        rather than an empty one.

        `include_reports` is the caller's plan check: a clinic whose plan does
        not deliver lab reports never pays for that query.
        """
        span = max(1, min(int(days or 30), _INSIGHTS_MAX_DAYS))
        today = datetime.now(CLINIC_TZ).date()
        first_day = today - timedelta(days=span - 1)
        # Every day in the window gets a slot, so a quiet Sunday plots as zero
        # instead of collapsing the axis and making the next day look adjacent.
        day_index = {first_day + timedelta(days=i): i for i in range(span)}
        since = (
            datetime.combine(first_day, time.min, tzinfo=CLINIC_TZ)
            .astimezone(timezone.utc)
            .isoformat()
        )

        payload: dict = {
            "period_days": span,
            "start_date": first_day.isoformat(),
            "end_date": today.isoformat(),
            "dates": [(first_day + timedelta(days=i)).isoformat() for i in range(span)],
            "labels": [
                (first_day + timedelta(days=i)).strftime("%d %b") for i in range(span)
            ],
            "branch_id": branch_id,
            "bookings": None,
            "revenue": None,
            "reports": None,
            "diagnostics": None,
            "errors": [],
        }

        try:
            appts = await _fetch_window(
                "appointments",
                clinic_id,
                "status,department,doctor_name,appointment_date,created_at,"
                "booking_type,lab_test_id,lab_test_name,treatment_name,amount_paise,payment_id,patient_phone",
                "created_at",
                since,
                branch_id=branch_id,
            )
            payload["bookings"], payload["revenue"] = _booking_and_revenue(
                appts, day_index, span
            )
        except Exception as e:
            logger.error(f"Insights: booking series failed for {clinic_id}: {e}")
            payload["errors"].append("bookings")
            appts = None

        # Service-type performance (migration 080/083): only for a clinic that
        # sells tests. Its own section, so a failure here costs this card only.
        if include_diagnostics and appts is not None:
            try:
                payload["diagnostics"] = _diagnostics_insights(
                    appts,
                    await _fetch_lab_catalogue(clinic_id),
                    await _fetch_lab_interest_events(clinic_id, since),
                )
            except Exception as e:
                logger.error(f"Insights: diagnostics section failed for {clinic_id}: {e}")
                payload["errors"].append("diagnostics")

        if include_reports:
            try:
                reports = await _fetch_window(
                    "lab_reports",
                    clinic_id,
                    "status,uploaded_at,sent_at,report_type,has_abnormal_values",
                    "uploaded_at",
                    since,
                )
                payload["reports"] = _report_delivery(reports, day_index, span)
                if branch_id and payload["reports"]:
                    payload["reports"]["is_centralized"] = True
            except Exception as e:
                logger.error(f"Insights: report series failed for {clinic_id}: {e}")
                payload["errors"].append("reports")

        return payload


# Global instance
analytics_service = AnalyticsService()
