"""Phone normalisation for telephony numbers.

Kriya stores patient phones as "+91XXXXXXXXXX" (app.utils.validators.normalize_phone,
the form the WhatsApp webhook writes). Exotel can deliver "09876543210",
"+919876543210" or "919876543210"; all must collapse to the same key or a
caller's WhatsApp history, bookings and reports are invisible to the voice agent.
"""

import re
from typing import Optional

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def to_e164(raw: Optional[str]) -> Optional[str]:
    """Indian-default E.164, or None when the input is not a phone number."""
    if not raw:
        return None
    s = re.sub(r"[^\d+]", "", str(raw))
    if s.startswith("+"):
        out = "+" + re.sub(r"\D", "", s[1:])
    else:
        digits = re.sub(r"\D", "", s)
        if digits.startswith("00"):
            out = "+" + digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            out = "+91" + digits[1:]
        elif len(digits) == 12 and digits.startswith("91"):
            out = "+" + digits
        elif len(digits) == 10:
            out = "+91" + digits
        else:
            return None
    return out if _E164.match(out) else None


def mask(phone: Optional[str]) -> str:
    """+91XXXXXX7890 — the CLAUDE.md log masking rule."""
    p = to_e164(phone) or (phone or "")
    if len(p) < 7:
        return "XXXX"
    return p[:3] + "X" * (len(p) - 7) + p[-4:]
