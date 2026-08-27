"""Helper utilities."""

from datetime import date, datetime, timedelta, time as time_type
from typing import Optional


from app.config import settings


def generate_booking_reference(prefix: Optional[str] = None) -> str:
    """Collision-resistant, per-tenant booking reference.

    Was MC-{year}-{4 random digits}: 9,000 values/year against a GLOBALLY unique
    column with no retry — ~50% collision probability at 112 platform-wide
    bookings, and the resulting 23505 was reported to the patient as
    "slot_taken" (KRIYA-001).

    32^8 = 1.1e12 values per prefix per year. Ambiguous glyphs (O/0, I/1) are
    excluded so the reference is safe to read aloud over the phone at reception.
    """
    import secrets
    from datetime import datetime

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 chars, no O I 0 1
    p = (prefix or settings.booking_ref_prefix or "MC").strip().upper()[:6]
    body = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{p}-{datetime.now().year}-{body}"


def _pg_error_text(exc: Exception) -> str:
    """Best-effort lowercase error text from a PostgREST or psycopg exception."""
    parts = [str(exc)]
    for attr in ("message", "details", "hint", "code", "pgcode", "pgerror"):
        val = getattr(exc, attr, None)
        if val:
            parts.append(str(val))
    raw = getattr(exc, "_raw_error", None)
    if isinstance(raw, dict):
        parts.extend(str(v) for v in raw.values() if v)
    return " ".join(parts).lower()


def is_booking_ref_conflict(exc: Exception) -> bool:
    """True only for a booking_ref uniqueness violation."""
    s = _pg_error_text(exc)
    return ("23505" in s or "unique" in s or "duplicate" in s) and (
        "booking_ref" in s or "uq_appointment_booking_ref" in s or "appointments_booking_ref_key" in s
    )


def is_slot_conflict(exc: Exception) -> bool:
    """True only for the partial slot unique indexes.

    Both index names are matched because migrations 008 and 043 define
    functionally identical indexes and either may fire until T4.2 drops one.

    Do NOT match on the bare word "violates". app/services/payment.py:100
    currently does, which swallows foreign-key, NOT NULL and CHECK failures and
    reports them to the patient as slot conflicts, hiding real data-integrity
    errors from operators.
    """
    s = _pg_error_text(exc)
    return ("23505" in s or "unique" in s or "duplicate" in s) and (
        "idx_unique_active_slot" in s or "uq_appointment_active_slot" in s
    )


def format_date(date_str: str, format: str = "%d %b %Y") -> str:
    """Format date string for display."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime(format)
    except ValueError:
        return date_str


def format_time(time_str: str, format: str = "%I:%M %p") -> str:
    """Format time string for display."""
    try:
        if ":" in time_str:
            parts = time_str.split(":")
            hour = int(parts[0])
            minute = int(parts[1])
            dt = datetime.now().replace(hour=hour, minute=minute)
            return dt.strftime(format)
        return time_str
    except (ValueError, IndexError):
        return time_str


def get_next_dates(days: int = 7, from_date: Optional[date] = None) -> list[date]:
    """Get list of next N dates."""
    if not from_date:
        from_date = date.today()

    return [from_date + timedelta(days=i) for i in range(days)]


def is_weekend(date_obj: date) -> bool:
    """Check if date is weekend."""
    return date_obj.weekday() >= 5  # Saturday = 5, Sunday = 6


def get_day_name(date_obj: date) -> str:
    """Get short day name (Mon, Tue, etc.)."""
    return date_obj.strftime("%a")


def truncate_text(text: str, max_length: int, suffix: str = "...") -> str:
    """Truncate text to max length."""
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def sanitize_input(text: str) -> str:
    """Sanitize user input."""
    if not text:
        return ""

    # Remove control characters
    text = "".join(char for char in text if ord(char) >= 32 or char in "\n\t")

    # Strip whitespace
    text = text.strip()

    return text


def parse_natural_date(text: str) -> Optional[date]:
    """Parse natural language date."""
    text_lower = text.lower().strip()

    today = date.today()

    if text_lower in ["today", "आज", "ఈరోజు"]:
        return today

    if text_lower in ["tomorrow", "कल", "రేపు"]:
        return today + timedelta(days=1)

    if text_lower in ["day after tomorrow", "परसों", "ఎల్లుండి"]:
        return today + timedelta(days=2)

    # Try to parse day names
    days = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    if text_lower in days:
        target_day = days.index(text_lower)
        current_day = today.weekday()
        days_ahead = (target_day - current_day) % 7
        if days_ahead == 0:
            days_ahead = 7  # Next week
        return today + timedelta(days=days_ahead)

    return None


def calculate_age(dob: date) -> int:
    """Calculate age from date of birth."""
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def format_slot_time(value) -> str:
    """Render an appointment time as '9:30 AM' for anything patient-facing.

    Accepts both shapes in circulation: 'HH:MM' from the slot pickers and
    'HH:MM:SS' as Postgres returns a TIME column. Anything unparseable is
    passed through unchanged so a bad row degrades to ugly, not to a crash
    in the middle of a reminder send.
    """
    text = str(value or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%I:%M %p").lstrip("0")
        except ValueError:
            continue
    return text


def generate_slots(start: time_type, end: time_type, duration_minutes: int) -> list[str]:
    """Generate a list of "HH:MM" slot strings from start to end in fixed steps.

    Returns an empty list for any invalid input (start >= end, duration <= 0)
    rather than raising — callers decide whether that's an error worth
    surfacing (e.g. the admin API returns 422 for a truly invalid form
    submission, distinct from "this shift is intentionally empty").
    """
    if duration_minutes <= 0 or start >= end:
        return []

    slots = []
    current = datetime.combine(date.today(), start)
    end_dt = datetime.combine(date.today(), end)
    step = timedelta(minutes=duration_minutes)

    while current < end_dt:
        slots.append(current.strftime("%H:%M"))
        current += step

    return slots

