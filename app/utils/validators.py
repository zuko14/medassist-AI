"""Validation utilities."""

import re
from datetime import datetime
from typing import Optional


def validate_phone(phone: str) -> bool:
    """Validate phone number format."""
    # Remove + prefix and country code for validation
    cleaned = phone.replace("+", "").replace(" ", "")
    # Should be 10-15 digits
    return bool(re.match(r"^\d{10,15}$", cleaned))


def normalize_phone(phone: str) -> str:
    """Normalize phone number to international format."""
    # Remove spaces and dashes
    cleaned = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    # If starts with +, keep as is
    if cleaned.startswith("+"):
        return cleaned

    # If starts with country code without +, add +
    if cleaned.startswith("91") and len(cleaned) == 12:
        return "+" + cleaned

    # Assume Indian number, add +91
    if len(cleaned) == 10:
        return "+91" + cleaned

    return cleaned


def validate_name(name: str) -> tuple[bool, str]:
    name = name.strip()
    
    INVALID_NAMES = [
        "no", "yes", "hi", "hello", "ok", "okay",
        "skip", "none", "test", "admin", "user",
        "లేదు", "అవును", "నహీ", "హాయ్",
        "नहीं", "हाँ", "ठीक है"
    ]
    
    if len(name) < 3:
        return False, "too_short"
    if len(name) > 60:
        return False, "too_long"
    if not re.match(
        r"^[a-zA-Z\u0900-\u097F\u0C00-\u0C7F\s]+$", 
        name
    ):
        return False, "invalid_chars"
    if name.lower().strip() in INVALID_NAMES:
        return False, "invalid_name"
    if len(name.strip().split()) < 2:
        return False, "need_full_name"
    
    return True, name.title()


def validate_date(date_str: str) -> tuple[bool, Optional[str]]:
    """Validate date string."""
    formats = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"]

    for fmt in formats:
        try:
            datetime.strptime(date_str, fmt)
            return True, None
        except ValueError:
            continue

    return False, "Invalid date format. Use YYYY-MM-DD or DD-MM-YYYY"


def validate_time(time_str: str) -> tuple[bool, Optional[str]]:
    """Validate time string."""
    formats = ["%H:%M", "%I:%M %p", "%I:%M%p"]

    for fmt in formats:
        try:
            datetime.strptime(time_str, fmt)
            return True, None
        except ValueError:
            continue

    return False, "Invalid time format. Use HH:MM (24-hour)"


def mask_phone(phone: str) -> str:
    """Mask phone number for logging."""
    if len(phone) <= 4:
        return "XXXX"

    # Show first 3 and last 4, mask the rest
    return phone[:3] + "X" * (len(phone) - 7) + phone[-4:]
