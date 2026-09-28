"""WhatsApp Leads — everyone who messaged the clinic, what they asked for, and
staff follow-up (migration 092, admin panel "Leads").

Capture: the contact is the `patients` row created on the first message. This
module adds only the *interest* signal: one analytics_events row
(event_type='lead_interest') per meaningful request, as a bounded label. The
message text is never stored.

Follow-up: Meta allows a free-form business message only inside the 24-hour
customer-service window opened by the patient's own last message. Outside it
a pre-approved template is required, and none exists for lead outreach, so the
panel offers a phone call instead. Opted-out (STOP) and consent-declined
patients are never messaged.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.database import (
    get_conversation,
    get_patient_by_phone,
    is_valid_clinic_scope,
    log_analytics_event,
    sb,
    supabase,
)
from app.utils.async_tasks import spawn_background_task

logger = logging.getLogger(__name__)

EVENT_TYPE = "lead_interest"

# detect_intent() labels that say what a patient wants. The others (greeting,
# unknown, change_language, opt_out, data_deletion_request, queue_status) are
# housekeeping, not interest.
_TEXT_INTERESTS = {
    "book_appointment": "book_appointment",
    "followup_booking": "followup",
    "doctor_availability": "doctors",
    "view_services": "services",
    "find_tests": "lab_tests",
    "clinic_info": "clinic_info",
    "view_reports": "reports",
    "cancel_appointment": "cancel",
    "reschedule_appointment": "reschedule",
    "human_escalation": "talk_to_staff",
    "emergency": "emergency",
}

# Menu entry points only. Taps inside a flow (slot, date, confirm) would just
# re-count the interest already recorded when the flow started.
_BUTTON_INTERESTS = {
    "menu_book": "book_appointment",
    "book_another": "book_appointment",
    "menu_doctors": "doctors",
    "menu_services": "services",
    "menu_lab_tests": "lab_tests",
    "book_lab_test": "lab_tests",
    "menu_reports": "reports",
    "menu_emergency": "emergency",
    "menu_human": "talk_to_staff",
    "menu_treatments": "treatments",
    "menu_concern": "treatments",
    "menu_entry_consult": "treatments",
}
_BUTTON_PREFIX_INTERESTS = (
    ("trtcall_", "callback_request"),
    ("trtbook_", "treatments"),
    ("labsvc_", "lab_tests"),
)

INTEREST_LABELS = frozenset(
    set(_TEXT_INTERESTS.values())
    | set(_BUTTON_INTERESTS.values())
    | {label for _, label in _BUTTON_PREFIX_INTERESTS}
)
SEGMENTS = frozenset({"all", "hot", "open", "booked", "dnc"})
MAX_MESSAGE_CHARS = 1000


class LeadError(Exception):
    """A refusal the panel should show as-is, with its HTTP status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def classify_interest(
    message_type: str, intent: Optional[str], interactive_data: Optional[dict]
) -> Optional[tuple[Optional[str], Optional[str]]]:
    """(interest label, department) for one inbound message, or None."""
    if interactive_data and message_type in ("interactive", "button"):
        button_id = str(interactive_data.get("id") or "")
        if button_id in _BUTTON_INTERESTS:
            return _BUTTON_INTERESTS[button_id], None
        for prefix, label in _BUTTON_PREFIX_INTERESTS:
            if button_id.startswith(prefix):
                return label, None
        if button_id.startswith("dept_"):
            # Built as f"dept_{name.lower().replace(' ', '_')}" by the department list.
            dept = button_id[len("dept_"):].replace("_", " ").strip().title()[:50]
            return (None, dept) if dept else None
        if message_type == "interactive":
            return None
    label = _TEXT_INTERESTS.get(intent or "")
    return (label, None) if label else None


def record_interest(
    clinic_id: str,
    phone: str,
    message_type: str,
    intent: Optional[str],
    interactive_data: Optional[dict] = None,
) -> None:
    """Record what the patient asked for. Never raises and never delays the
    reply: the write runs as a background task, and log_analytics_event
    swallows its own database errors."""
    try:
        found = classify_interest(message_type, intent, interactive_data)
        if not found:
            return
        label, department = found
        spawn_background_task(
            log_analytics_event(clinic_id, phone, EVENT_TYPE, intent=label, department=department),
            name="lead_interest",
        )
    except Exception as e:
        logger.warning(f"LEAD_INTEREST_NOT_RECORDED clinic={clinic_id}: {e}")


async def list_leads(
    clinic_id: str,
    segment: str = "all",
    interest: Optional[str] = None,
    search: Optional[str] = None,
    days: int = 30,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """One page of this clinic's WhatsApp contacts plus totals and interest mix."""
    if not is_valid_clinic_scope(clinic_id):
        raise LeadError(400, "A clinic must be selected.")
    if segment not in SEGMENTS:
        raise LeadError(400, f"Unknown segment '{segment}'.")
    if interest and interest not in INTEREST_LABELS:
        raise LeadError(400, f"Unknown interest '{interest}'.")
    search = (search or "").strip()[:60] or None

    res = await sb(supabase.rpc("admin_whatsapp_leads", {
        "p_clinic_id": clinic_id,
        "p_segment": segment,
        "p_interest": interest or None,
        "p_search": search,
        "p_days": max(0, min(int(days), 3650)),
        "p_limit": max(1, min(int(limit), 100)),
        "p_offset": max(0, min(int(offset), 100_000)),
    }))
    data = res.data
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise RuntimeError(f"admin_whatsapp_leads returned an unexpected shape: {type(data).__name__}")

    # One definition of "mid-booking": the state machine's own. Lazy import:
    # conversation.py imports this module.
    from app.services.conversation import MID_BOOKING_STATES

    for row in data["rows"]:
        row["mid_booking"] = row.pop("state", None) in MID_BOOKING_STATES
    return data


def _window_open(conversation: Optional[dict]) -> bool:
    expires = (conversation or {}).get("session_expires_at")
    if not expires:
        return False
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) < expires


async def send_lead_message(clinic: dict, phone: str, text: str) -> None:
    """Send one staff-written WhatsApp message to a contact of THIS clinic.

    Raises LeadError for every refusal; returns only when Meta accepted it.
    """
    from app.services.whatsapp import whatsapp_service

    clinic_id = clinic["id"]
    text = (text or "").strip()
    if not text:
        raise LeadError(400, "Message is empty.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise LeadError(400, f"Message is longer than {MAX_MESSAGE_CHARS} characters.")

    # Only a number that messaged THIS clinic: the endpoint must never become
    # a way to WhatsApp arbitrary numbers from the clinic's business account.
    patient = await get_patient_by_phone(clinic_id, phone)
    if not patient:
        raise LeadError(404, "This number has not messaged your clinic on WhatsApp.")
    if patient.get("opted_in") is False or patient.get("data_consent") is False:
        raise LeadError(409, "This patient opted out or declined consent. Do not contact them.")
    if not _window_open(await get_conversation(clinic_id, phone)):
        raise LeadError(
            409,
            "WhatsApp only allows a clinic to message a patient within 24 hours of the "
            "patient's last message. Please call them instead.",
        )

    if not await whatsapp_service.send_text(clinic, phone, text, _source="admin_lead"):
        raise LeadError(502, "WhatsApp did not accept the message. Please try again or call the patient.")
