"""Subscription lifecycle + daily report limits.

ONE module owns both facts, because both gate the same thing: whether an
automated outbound message is allowed to leave right now.

Subscription model (30-day prepaid, 5-day grace)
------------------------------------------------
    day 0 .. 30   active        outbound flows
    day 31 .. 35  grace_period  outbound STILL flows, banners warn
    day 36 ..     suspended     automated outbound is paused

The effective status is COMPUTED from the dates on every read, never trusted
from the stored column alone. A cron that flips a status column is a cron that
can miss a run and leave a suspended clinic messaging for a day; a computed
status cannot drift. `clinics.subscription_status` is kept as a sticky FLOOR:
an owner can suspend a clinic early, and only a renewal lifts that.

Renewal accounting (backdated grace deduction)
----------------------------------------------
"Renew 30 days" starts the new window at the PREVIOUS end date, not at now, so
the grace days a clinic already consumed come out of the period they are paying
for. Renew on day 33 and the new window ends on day 61, i.e. 28 usable days
remain. This is deliberate: the platform is paid for 30 days each time.

One clamp: if backdating would produce a window that has ALREADY expired (a
clinic renewing months late), the window starts at now instead. Handing someone
a renewal that re-suspends them the same second is not an accounting rule, it
is a bug.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

#: The only daily report limits a clinic may be assigned. 0 = unlimited.
#: Mirrors the CHECK constraint in migrations/068 — the DB is the real guard,
#: this is the one the API validates against so callers get a 400, not a 500.
DAILY_REPORT_LIMIT_TIERS = (0, 50, 100, 200, 300, 500)

SUBSCRIPTION_PERIOD_DAYS = 30
DEFAULT_GRACE_PERIOD_DAYS = 5

#: Fraction of the daily limit at which the panel turns yellow.
WARN_THRESHOLD = 0.8

#: Every clinic on this platform bills and reports on the Indian calendar day.
IST = timezone(timedelta(hours=5, minutes=30))

STATUS_ACTIVE = "active"
STATUS_GRACE = "grace_period"
STATUS_SUSPENDED = "suspended"
STATUS_TRIAL = "trial"

VALID_STATUSES = (STATUS_ACTIVE, STATUS_GRACE, STATUS_SUSPENDED, STATUS_TRIAL)


def ist_today(now: Optional[datetime] = None) -> date:
    """Today's date in Asia/Kolkata — the day daily counters are keyed on."""
    return (now or datetime.now(timezone.utc)).astimezone(IST).date()


def next_ist_midnight(now: Optional[datetime] = None) -> datetime:
    """UTC instant of the next Asia/Kolkata midnight, i.e. when limits reset."""
    now = now or datetime.now(timezone.utc)
    tomorrow = ist_today(now) + timedelta(days=1)
    return datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=IST
    ).astimezone(timezone.utc)


def _parse_ts(value) -> Optional[datetime]:
    """Parse a Supabase timestamptz into an aware UTC datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        # Postgres renders UTC as '+00:00' but Supabase sometimes emits 'Z',
        # and fromisoformat only learned 'Z' in 3.11.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            logger.warning(f"Unparseable subscription timestamp: {value!r}")
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


#: Public alias — routers parse owner-supplied ISO timestamps with the same
#: leniency the lifecycle itself uses, so what validates is what is stored.
parse_timestamp = _parse_ts


# ── Lifecycle ───────────────────────────────────────────────────────────────


def compute_subscription_state(clinic: dict, now: Optional[datetime] = None) -> dict:
    """Effective subscription state for a clinic. Pure — no I/O.

    Returns a dict safe to hand to BOTH the clinic admin panel and the owner
    console: it carries no cost, price or rate field of any kind.

    `outbound_allowed` is the single answer every automated sender asks.
    """
    now = now or datetime.now(timezone.utc)

    start = _parse_ts(clinic.get("subscription_start_date"))
    end = _parse_ts(clinic.get("subscription_end_date"))
    stored = clinic.get("subscription_status") or STATUS_ACTIVE
    if stored not in VALID_STATUSES:
        stored = STATUS_ACTIVE

    grace_days = clinic.get("grace_period_days")
    grace_days = DEFAULT_GRACE_PERIOD_DAYS if grace_days is None else int(grace_days)
    grace_days = max(0, grace_days)

    # A clinic predating migration 068 (or a synthetic env-var fallback clinic)
    # has no dates. Fail OPEN: never silence a live hospital's reminders over a
    # column that has not been backfilled yet.
    if end is None:
        return {
            "status": STATUS_SUSPENDED if stored == STATUS_SUSPENDED else STATUS_ACTIVE,
            "subscription_start_date": start.isoformat() if start else None,
            "subscription_end_date": None,
            "grace_period_days": grace_days,
            "days_remaining": None,
            "grace_day": 0,
            "grace_days_left": 0,
            "outbound_allowed": stored != STATUS_SUSPENDED,
            "banner": None,
            "unconfigured": True,
        }

    grace_end = end + timedelta(days=grace_days)

    if now < end:
        computed = STATUS_TRIAL if stored == STATUS_TRIAL else STATUS_ACTIVE
    elif now < grace_end:
        computed = STATUS_GRACE
    else:
        computed = STATUS_SUSPENDED

    # Sticky floor: an owner-set suspension outranks the dates. Dates may only
    # ever move a clinic FORWARD through the lifecycle, never rehabilitate it.
    if stored == STATUS_SUSPENDED:
        computed = STATUS_SUSPENDED

    # "Day X of 5": the first day past expiry is Day 1, not Day 0.
    grace_day = 0
    grace_days_left = 0
    if computed == STATUS_GRACE:
        elapsed = int((now - end).total_seconds() // 86400)
        grace_day = min(grace_days, elapsed + 1)
        grace_days_left = max(0, grace_days - elapsed)

    days_remaining = int((end - now).total_seconds() // 86400) if now < end else 0

    banner = None
    if computed == STATUS_GRACE:
        banner = (
            "Your 30-day subscription has expired. You are currently in your "
            f"{grace_days}-day grace period (Day {grace_day} of {grace_days}). "
            "Please contact the administrator to renew and prevent service interruption."
        )
    elif computed == STATUS_SUSPENDED:
        banner = (
            "Your subscription has expired and the grace period has ended. "
            "Automated WhatsApp messaging is paused. Please contact the "
            "administrator to renew."
        )

    return {
        "status": computed,
        "subscription_start_date": start.isoformat() if start else None,
        "subscription_end_date": end.isoformat(),
        "grace_period_days": grace_days,
        "days_remaining": days_remaining,
        "grace_day": grace_day,
        "grace_days_left": grace_days_left,
        "outbound_allowed": computed != STATUS_SUSPENDED,
        "banner": banner,
        "unconfigured": False,
    }


def renewal_window(clinic: dict, now: Optional[datetime] = None) -> tuple:
    """(start, end) of the next 30-day window, per the backdating rule above."""
    now = now or datetime.now(timezone.utc)
    previous_end = _parse_ts(clinic.get("subscription_end_date"))

    start = previous_end or now
    end = start + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)
    if end <= now:
        # Backdating would hand back an already-dead window. Start fresh.
        start = now
        end = start + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)
    return start, end


# ── Daily limits ────────────────────────────────────────────────────────────


def limit_state(daily_limit: Optional[int], used: int) -> dict:
    """Traffic-light state for a clinic's daily report dispatch budget.

    level is one of 'unlimited' | 'ok' | 'warning' | 'blocked'.
    """
    used = max(0, int(used or 0))
    limit = int(daily_limit or 0)

    if limit <= 0:
        return {
            "limit": 0,
            "used": used,
            "remaining": None,
            "percent": 0,
            "level": "unlimited",
            "is_unlimited": True,
        }

    percent = int(round(used * 100 / limit))
    if used >= limit:
        level = "blocked"
    elif used >= limit * WARN_THRESHOLD:
        level = "warning"
    else:
        level = "ok"

    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "percent": percent,
        "level": level,
        "is_unlimited": False,
    }


#: Which clinic_daily_usage counter each outbound classification advances.
#: Keys are the classifications from message_accounting.classify_source().
_COUNTER_FOR_CLASS = {
    "LAB_REPORT": "p_reports",
    "PRESCRIPTION": "p_prescriptions",
    "APPOINTMENT_REMINDER": "p_reminders",
    "FOLLOW_UP": "p_followups",
}


async def record_outbound_usage(clinic_id: Optional[str], classification: str) -> None:
    """Advance today's counters for one delivered message. Never raises.

    Called from message_accounting.log_outbound(), which is already the single
    write path for every Meta API call, so there is exactly one place a send
    can be counted and no sender can forget to.
    """
    from app.tenancy import is_valid_clinic_scope

    if not is_valid_clinic_scope(clinic_id):
        return

    params = {
        "p_clinic_id": str(clinic_id),
        "p_usage_date": ist_today().isoformat(),
        "p_reports": 0,
        "p_prescriptions": 0,
        "p_reminders": 0,
        "p_followups": 0,
        "p_total": 1,
    }
    counter = _COUNTER_FOR_CLASS.get(classification)
    if counter:
        params[counter] = 1

    try:
        from app.database import sb, supabase

        await sb(supabase.rpc("increment_clinic_daily_usage", params))
    except Exception as e:
        # Fire-and-forget, exactly like the ledger write it sits beside. A
        # usage counter is not worth failing a healthcare message over.
        logger.error(f"Daily usage increment failed (non-fatal): {e}")


async def get_daily_usage(clinic_id: Optional[str], usage_date: Optional[date] = None) -> dict:
    """Today's counters for a clinic. Returns zeros on any failure."""
    from app.tenancy import is_valid_clinic_scope

    zeros = {
        "usage_date": (usage_date or ist_today()).isoformat(),
        "reports_delivered_count": 0,
        "prescriptions_sent_count": 0,
        "reminders_sent_count": 0,
        "followups_sent_count": 0,
        "total_outbound_count": 0,
    }
    if not is_valid_clinic_scope(clinic_id):
        return zeros

    try:
        from app.database import sb, supabase

        res = (
            await sb(supabase.table("clinic_daily_usage")
            .select("usage_date, reports_delivered_count, prescriptions_sent_count, "
                    "reminders_sent_count, followups_sent_count, total_outbound_count")
            .eq("clinic_id", str(clinic_id))
            .eq("usage_date", zeros["usage_date"]))
        )
        if res.data:
            return {**zeros, **res.data[0]}
    except Exception as e:
        logger.error(f"Daily usage read failed for clinic {clinic_id}: {e}")
    return zeros


async def get_clinic_status(clinic: dict, now: Optional[datetime] = None) -> dict:
    """Subscription state + today's report-limit state for one clinic.

    CUSTOMER-SAFE: contains no cost, price, rate or Meta-economics field, so it
    can back both the clinic admin banner and the owner console.
    """
    state = compute_subscription_state(clinic, now=now)
    usage = await get_daily_usage(clinic.get("id"))
    limits = limit_state(
        clinic.get("daily_report_limit"), usage.get("reports_delivered_count", 0)
    )
    return {
        "subscription": state,
        "daily_reports": limits,
        "usage_today": usage,
        "resets_at": next_ist_midnight(now).isoformat(),
    }


# ── The gates every automated sender asks ───────────────────────────────────


def automated_outbound_allowed(clinic: Optional[dict], now: Optional[datetime] = None) -> bool:
    """False once a clinic is suspended. Grace period still sends.

    Deliberately synchronous and pure so it can sit inside a scheduler's inner
    loop without adding a round-trip per appointment.
    """
    if not isinstance(clinic, dict):
        return True
    return compute_subscription_state(clinic, now=now)["outbound_allowed"]


async def report_dispatch_allowed(clinic: Optional[dict], now: Optional[datetime] = None) -> tuple:
    """Whether one more report may be dispatched for this clinic today.

    Returns (allowed, reason). `reason` is '' when allowed, else a short
    machine-readable token: 'suspended' | 'daily_limit_reached'.
    """
    if not isinstance(clinic, dict):
        return True, ""

    if not automated_outbound_allowed(clinic, now=now):
        return False, "suspended"

    limit = int(clinic.get("daily_report_limit") or 0)
    if limit <= 0:
        return True, ""

    usage = await get_daily_usage(clinic.get("id"))
    if usage.get("reports_delivered_count", 0) >= limit:
        return False, "daily_limit_reached"
    return True, ""
