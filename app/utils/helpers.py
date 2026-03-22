"""Helper utilities."""

import random
import string
from datetime import date, datetime, timedelta
from typing import Optional

from app.config import settings


def generate_booking_reference() -> str:
    """Generate a unique booking reference."""
    import random
    from datetime import datetime
    year = datetime.now().year
    number = str(random.randint(1000, 9999)).zfill(4)
    return f"MC-{year}-{number}"


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
    return text[:max_length - len(suffix)] + suffix


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
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
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
