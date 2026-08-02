"""PII Sanitization Middleware for MediAssist AI.

Scrubs personally identifiable information (PII) from lab report text
BEFORE sending it to external LLM APIs (Groq), preventing patient data
leakage to third-party services.

Compliant with India DPDP Act 2023 data minimization requirements.

Redacts:
  - Patient names (exact match from provided name)
  - Indian phone numbers (+91XXXXXXXXXX, 91XXXXXXXXXX, 0XXXXXXXXXX)
  - Aadhaar card numbers (XXXX XXXX XXXX, XXXXXXXXXXXX)
  - ABHA Health IDs (14-digit numeric)
  - Email addresses
  - Date of Birth patterns
  - Indian PIN codes (within address contexts)
  - Patient record identifiers (MRN/UHID/Patient ID labels)
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── PII Regex Patterns ────────────────────────────────────────────────────────

# Indian mobile numbers: +91-XXXXXXXXXX, 91XXXXXXXXXX, 0XXXXXXXXXX, XXXXXXXXXX (10-digit starting with 6-9)
_PHONE_PATTERN = re.compile(r"(\+91[-\s]?|91[-\s]?|0)?[6-9]\d{9}", re.IGNORECASE)

# Aadhaar: 12 digits, optionally space- or dash-separated in groups of 4
_AADHAAR_PATTERN = re.compile(r"\b(\d{4}[\s\-]?\d{4}[\s\-]?\d{4})\b")

# ABHA Health ID: exactly 14 digits (standalone)
_ABHA_PATTERN = re.compile(r"\b(\d{14})\b")

# Email addresses
_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", re.IGNORECASE
)

# Date of Birth patterns: DD/MM/YYYY, DD-MM-YYYY, YYYY-MM-DD, DD.MM.YYYY
_DOB_PATTERN = re.compile(
    r"\b(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2})\b"
)

# Age patterns: "Age: 35", "Age/Sex: 35/M", "35 years", "35Y"
_AGE_PATTERN = re.compile(
    r"\b(?:age|age/sex)\s*:?\s*\d{1,3}(?:/[MFmf])?\b|\b\d{1,3}\s*(?:years?|yrs?|Y)\b",
    re.IGNORECASE,
)

# Patient ID / MRN / UHID labels followed by value
_PATIENT_ID_PATTERN = re.compile(
    r"\b(?:patient\s*id|mrn|uhid|reg(?:istration)?\s*(?:no|number)|pid)\s*[:\-#]?\s*[\w\-/]+",
    re.IGNORECASE,
)

# Indian PIN code (6 digits, often preceded by keywords)
_PINCODE_PATTERN = re.compile(
    r"\b(?:pin(?:\s*code)?|zip)\s*[:\-]?\s*\d{6}\b", re.IGNORECASE
)


def _build_name_pattern(patient_name: Optional[str]) -> Optional[re.Pattern]:
    """Build a case-insensitive regex for the patient's full name and parts."""
    if not patient_name or not patient_name.strip():
        return None
    # Escape regex metacharacters, then split on whitespace to match name parts
    parts = [re.escape(part) for part in patient_name.strip().split() if len(part) > 2]
    if not parts:
        return None
    # Match full name OR individual name parts (first name, last name)
    # Full name first for specificity, then parts
    full_escaped = re.escape(patient_name.strip())
    pattern_str = (
        full_escaped + "|" + "|".join(parts) if len(parts) > 1 else full_escaped
    )
    try:
        return re.compile(r"\b(?:" + pattern_str + r")\b", re.IGNORECASE)
    except re.error:
        return None


def sanitize_report_text(
    text: str,
    patient_name: Optional[str] = None,
) -> tuple[str, dict]:
    """Scrub PII from lab report text before sending to external LLM API.

    Args:
        text: Raw extracted lab report text.
        patient_name: Patient's name to redact (exact match).

    Returns:
        Tuple of (sanitized_text, redaction_map):
            - sanitized_text: Text with PII replaced by placeholders.
            - redaction_map: Dict mapping placeholder → original value
              (used to restore context in final WhatsApp message).

    Example:
        sanitized, rmap = sanitize_report_text(text, "Rahul Sharma")
        summary = call_llm(sanitized)
        final_msg = restore_pii(summary, rmap)
    """
    redaction_map: dict = {}
    counter = [0]  # mutable for nested closure

    def make_placeholder(label: str, original: str) -> str:
        counter[0] += 1
        key = f"[{label}_{counter[0]}]"
        redaction_map[key] = original
        return key

    redacted = text

    # 1. Redact patient name first (highest priority — appears throughout report)
    name_pattern = _build_name_pattern(patient_name)
    if name_pattern:

        def _replace_name(m: re.Match) -> str:
            return make_placeholder("PATIENT", m.group(0))

        redacted = name_pattern.sub(_replace_name, redacted)

    # 2. Redact ABHA IDs (14-digit) — before generic phone/Aadhaar to avoid overlap
    def _replace_abha(m: re.Match) -> str:
        return make_placeholder("ABHA_ID", m.group(0))

    redacted = _ABHA_PATTERN.sub(_replace_abha, redacted)

    # 3. Redact Aadhaar numbers
    def _replace_aadhaar(m: re.Match) -> str:
        return make_placeholder("AADHAAR", m.group(0))

    redacted = _AADHAAR_PATTERN.sub(_replace_aadhaar, redacted)

    # 4. Redact phone numbers
    def _replace_phone(m: re.Match) -> str:
        return make_placeholder("PHONE", m.group(0))

    redacted = _PHONE_PATTERN.sub(_replace_phone, redacted)

    # 5. Redact email addresses
    def _replace_email(m: re.Match) -> str:
        return make_placeholder("EMAIL", m.group(0))

    redacted = _EMAIL_PATTERN.sub(_replace_email, redacted)

    # 6. Redact patient IDs / MRNs
    def _replace_pid(m: re.Match) -> str:
        return make_placeholder("PID", m.group(0))

    redacted = _PATIENT_ID_PATTERN.sub(_replace_pid, redacted)

    # 7. Redact DOB (keep date context but strip the value)
    def _replace_dob(m: re.Match) -> str:
        return make_placeholder("DOB", m.group(0))

    redacted = _DOB_PATTERN.sub(_replace_dob, redacted)

    # 8. Redact age patterns (but preserve in medical context — only if labeled)
    def _replace_age(m: re.Match) -> str:
        return make_placeholder("AGE", m.group(0))

    redacted = _AGE_PATTERN.sub(_replace_age, redacted)

    # 9. Redact PIN codes
    def _replace_pin(m: re.Match) -> str:
        return make_placeholder("PINCODE", m.group(0))

    redacted = _PINCODE_PATTERN.sub(_replace_pin, redacted)

    redaction_count = len(redaction_map)
    if redaction_count > 0:
        logger.info(
            f"PII sanitizer: redacted {redaction_count} item(s) before LLM call "
            f"[types: {set(k.rsplit('_', 1)[0].lstrip('[') for k in redaction_map)}]"
        )

    return redacted, redaction_map


def restore_pii(text: str, redaction_map: dict) -> str:
    """Restore patient name into the LLM summary for final WhatsApp delivery.

    NOTE: We only restore the PATIENT name placeholder — all other PII
    (phone, Aadhaar, email) is intentionally left redacted in the
    outbound summary, as patients already know their own details.

    Args:
        text: LLM-generated summary (may reference placeholders).
        redaction_map: Map from sanitize_report_text().

    Returns:
        Text with patient name restored.
    """
    result = text
    for placeholder, original in redaction_map.items():
        if "[PATIENT_" in placeholder:
            result = result.replace(placeholder, original)
    return result


def get_redaction_summary(redaction_map: dict) -> str:
    """Return a human-readable audit log of what was redacted."""
    if not redaction_map:
        return "No PII redacted."
    types: dict[str, int] = {}
    for key in redaction_map:
        label = key.lstrip("[").rsplit("_", 1)[0]
        types[label] = types.get(label, 0) + 1
    parts = [f"{count}x {label}" for label, count in types.items()]
    return "Redacted: " + ", ".join(parts)
