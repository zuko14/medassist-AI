"""Conversation state machine for MediAssist."""

import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from app.config import settings
from app.database import (
    get_or_create_conversation,
    update_conversation,
    get_patient_by_phone,
    create_patient,
    update_patient,
    get_doctors,
    get_available_slots,
    find_next_available_date,
    book_appointment,
    get_patient_queue_status,
    get_family_members,
    add_family_member,
    log_analytics_event,
)
from app.services.ai_engine import (
    detect_intent,
    is_greeting,
    language_change_request,
    map_symptom_to_department,
    EMERGENCY_KEYWORDS,
)
from app.services.whatsapp import whatsapp_service
from app.templates.whatsapp_templates import cancellation_policy_line, get_message
from app.utils.validators import mask_phone
from app.utils.helpers import format_slot_time

from app.services.tenant import cancellation_window_hours

# Clinical safety firewall — screens messages before LLM is called
from app.services.clinical_firewall import screen_message
from app.services import specialty_flow

# Per-phone asyncio lock with Meta timeout protection
from app.services.message_queue import (
    acquire_phone_lock_with_timeout,
    release_phone_lock_acquired,
)
from app.database import sb  # T5.1: off-loop query execution

logger = logging.getLogger(__name__)


async def get_lang(clinic: dict, phone: str) -> str:
    """Get language for a patient from database."""
    try:
        from app.database import supabase

        result = (
            await sb(supabase.table("patients")
            .select("language")
            .eq("clinic_id", clinic["id"])
            .eq("phone", phone)
            .single())
        )
        lang = result.data.get("language")
        return lang if lang in ["en", "hi", "te"] else "en"
    except Exception:
        return "en"


#: Row id for the "more doctors" pager in the "Our Doctors" browse list. The
#: next page number is appended (view_doc_more_1, _2, ...) so paging carries no
#: stored state. It deliberately shares the "view_doc_" namespace, which means
#: the button dispatch in handle_message MUST test this prefix FIRST.
_MORE_DOCTORS_ID = "view_doc_more"

#: Typed words that always mean "take me back to the main menu", from any
#: state. Kept at module level so state handlers that consume free text (lab
#: test search, for one) can exclude them instead of swallowing the patient's
#: only way out.
NAV_KEYWORDS = frozenset(
    {"menu", "main menu", "home", "start over", "reset", "मेनू", "మెనూ"}
)

#: Whole messages that mean "start booking a lab test". Matched on the exact
#: message, never as a substring, so "cancel my lab test" is not a booking.
#: The intent classifier has no lab-booking intent to return, so these words
#: have to be routed before any intent check or they arrive as
#: book_appointment (the doctor flow) or view_reports (the reports answer).
LAB_BOOKING_KEYWORDS = frozenset(
    {"book test", "book lab test", "booktest", "lab test", "lab tests"}
)

#: Whole messages asking how to use the bot. The "invalid input" reply has
#: always told patients to "type 'help'" -- nothing answered it until now.
HELP_KEYWORDS = frozenset({
    "help", "how to use", "how to use?", "guide", "commands", "instructions",
    "options", "?", "मदद", "सहायता", "సహాయం", "సహాయము",
})

#: Whole messages that turn follow-up messages back on after "stop". The
#: opt-out reply promises "Message us anytime to re-subscribe"; these words
#: are how that promise is kept. Only acted on for an opted-out patient.
RESUBSCRIBE_KEYWORDS = frozenset({
    "start", "subscribe", "resubscribe", "re-subscribe", "opt in", "optin",
    "resume", "शुरू", "ప్రారంభించు",
})

#: Ephemeral booking and branch context keys that must not bleed across distinct sessions or bookings
BOOKING_CONTEXT_KEYS = frozenset({
    "doctor",
    "doctor_id",
    "doctor_name",
    "selected_doctor_id",
    "department",
    "branch_id",
    "branch_name",
    "branch_address",
    "branch_landmark",
    "branch_maps_link",
    "branch_session",
    "appointment_date",
    "appointment_time",
    "booking_name",
    "booking_id",
    "booking_ref",
    "symptoms",
    "last_symptom",
    "for_self",
    "asked_for_whom",
    "razorpay_payment_link_id",
    "suggested_department",
    "suggestion_reasoning",
    "doctor_page",
    "department_page",
    "treatment_id",
    "treatment_name",
    "branch_page",
    "lab_test_id",
    "lab_test_name",
    "collection_date",
    "lab_flow",
})


class ConversationState(str, Enum):
    IDLE = "idle"
    SELECTING_LANGUAGE = "selecting_language"
    AWAITING_CONSENT = "awaiting_consent"
    MAIN_MENU = "main_menu"
    SELECTING_BRANCH = "selecting_branch"
    SELECTING_FAMILY_MEMBER = "selecting_family_member"
    COLLECTING_NAME = "collecting_name"
    CONFIRMING_SAVE_FAMILY_MEMBER = "confirming_save_family_member"
    COLLECTING_SYMPTOMS = "collecting_symptoms"
    SUGGESTING_DEPARTMENT = "suggesting_department"
    SELECTING_DEPARTMENT = "selecting_department"
    SELECTING_DOCTOR = "selecting_doctor"
    SELECTING_DATE = "selecting_date"
    SELECTING_SLOT = "selecting_slot"
    CONFIRMING_BOOKING = "confirming_booking"
    AWAITING_PAYMENT = "awaiting_payment"
    MANAGING_APPOINTMENT = "managing_appointment"
    RESCHEDULING = "rescheduling"
    EMERGENCY = "emergency"
    ESCALATED_TO_HUMAN = "escalated_to_human"
    AWAITING_DATA_DELETION = "awaiting_data_deletion"
    VIEWING_REPORTS = "viewing_reports"
    DOWNLOADING_REPORT = "downloading_report"
    BROWSING_LAB_TESTS = "browsing_lab_tests"
    CONFIRMING_COLLECTION_DATE = "confirming_collection_date"
    BROWSING_TREATMENTS = "browsing_treatments"
    SEARCHING_TREATMENTS = "searching_treatments"


# ── Inbound WhatsApp message types ───────────────────────────────────────────
# Meta delivers far more than text. Anything not listed as READABLE arrives with
# an empty body, and without this split it used to fall through the whole state
# machine as if the patient had sent a blank text — a voice note describing
# symptoms mid-booking came back as "Name is too short", and every photo burned
# a paid LLM intent call on an empty string.
READABLE_MESSAGE_TYPES = frozenset({"text", "interactive", "button"})

# Types that carry no patient request. Replying to these is noise: a thumbs-up
# reaction on a booking confirmation must not re-open a conversation.
IGNORED_MESSAGE_TYPES = frozenset({"reaction", "system", "order", "ephemeral"})


MID_BOOKING_STATES = {
    "selecting_branch",
    "selecting_family_member",
    "collecting_name",
    "confirming_save_family_member",
    "collecting_symptoms",
    "asking_symptoms",
    "suggesting_department",
    "selecting_department",
    "selecting_doctor",
    "selecting_date",
    "selecting_slot",
    "confirming_booking",
    "booking_lab_test",
    "selecting_lab_date",
    "confirming_lab_booking",
    # Lab date -> who -> name. Holding it to the same 30 minutes means a date
    # picked yesterday cannot be booked today by typing a name.
    "confirming_collection_date",
}

#: Merged into the context on leaving the lab booking steps. update_state
#: merges rather than replaces, so a key must be overwritten to be cleared.
_LAB_STEP_CLEARED = {
    "lab_step": None,
    "lab_for_self": None,
    "lab_collection_date": None,
    "lab_family": None,
    "lab_pending_name": None,
}


def extract_clean_message_content(message: str) -> str:
    """Extract user selection from multi-line messages that include WhatsApp quoted prompt headers."""
    if not message or "\n" not in message:
        return message

    lines = [line.strip() for line in message.strip().split("\n") if line.strip()]
    if len(lines) <= 1:
        return message

    HEADER_PATTERNS = [
        "what would you like to do",
        "our services",
        "please choose a department",
        "choose department",
        "choose your doctor",
        "available doctors in",
        "select branch",
        "select location",
        "who is this appointment for",
        "select date",
        "select time",
        "select a time slot",
        "please select from the list below",
        "how can we help",
        "हमारी सेवाएं",
        "మా సేవలు",
        "कृपया विभाग",
        "దయచేసి విభాగం",
    ]

    # Iteratively remove leading lines that match prompt header patterns,
    # as long as there is still remaining user selection content.
    while len(lines) > 1:
        first_line_lower = lines[0].lower()
        if any(h in first_line_lower for h in HEADER_PATTERNS):
            lines.pop(0)
        else:
            break

    return lines[0] if len(lines) == 1 else "\n".join(lines)


def resolve_booking_name(context: dict, patient: Optional[dict] = None) -> str:
    """The name of the person an appointment is for.

    "Who is this appointment for?" used to store the chosen person only as
    context["patient_name"], while the confirmation screen and both booking
    writers read context["booking_name"] — so every "For Me" and saved-family
    booking was confirmed and saved as "Patient". The writers now set
    booking_name, and every reader goes through here so a context written by
    either path (including sessions in flight during a deploy) resolves to the
    real name. "there" is the greeting placeholder, never a name.
    """
    candidates = (
        context.get("booking_name"),
        context.get("patient_name"),
        (patient or {}).get("name"),
    )
    for candidate in candidates:
        name = candidate.strip() if isinstance(candidate, str) else ""
        if name and name.lower() != "there" and name != "[REDACTED]":
            return name
    return "Patient"


class ConversationManager:
    """Manages conversation state and flow."""

    def _set_branch_context(
        self,
        context: dict,
        branch: Optional[dict] = None,
        session_val: Optional[str] = None,
    ) -> dict:
        """Atomically set or clear branch fields in conversation context.
        
        Ensures branch_id, branch_name, branch_address, branch_landmark,
        branch_maps_link, and branch_session are always updated together,
        preventing mixed-branch fields from leaking across sessions.
        """
        if branch:
            context["branch_id"] = branch.get("id") or branch.get("branch_id")
            context["branch_name"] = branch.get("short_name") or branch.get("name", "")
            context["branch_address"] = branch.get("address", "")
            context["branch_landmark"] = branch.get("landmark", "")
            context["branch_maps_link"] = branch.get("maps_link", "")
            if session_val is not None:
                context["branch_session"] = session_val
            elif "branch_session" not in context:
                context["branch_session"] = "both"
        else:
            context.pop("branch_id", None)
            context.pop("branch_name", None)
            context.pop("branch_address", None)
            context.pop("branch_landmark", None)
            context.pop("branch_maps_link", None)
            context.pop("branch_session", None)
        return context

    def _clear_booking_context(self, context: dict) -> dict:
        """Clear all appointment/booking-specific fields from context to prevent state leakage."""
        for key in BOOKING_CONTEXT_KEYS:
            context.pop(key, None)
        return context

    async def update_state(
        self,
        clinic: dict,
        phone: str,
        new_state: str,
        new_context: Optional[dict] = None,
        reset_context: bool = False,
    ) -> None:
        if new_context is None:
            new_context = {}
        from app.database import supabase

        existing = {}
        session_state = None
        try:
            conv_res = (
                await sb(supabase.table("conversations")
                .select("context, state")
                .eq("clinic_id", clinic["id"])
                .eq("phone", phone))
            )
            if conv_res and conv_res.data and isinstance(conv_res.data, list) and len(conv_res.data) > 0 and isinstance(conv_res.data[0], dict):
                existing = conv_res.data[0].get("context", {}) or {}
                session_state = conv_res.data[0].get("state")
        except Exception:
            pass

        # Reset menu_shown to False if transitioning BACK to main_menu from another state
        if new_state == "main_menu" and session_state != "main_menu":
            new_context["menu_shown"] = False

        if reset_context:
            merged = new_context
        else:
            merged = {**existing, **new_context}

        # A treatment tag must never outlive the treatment flow that set it. A
        # patient who abandons it and books through departments or Our Doctors
        # would otherwise get that booking labelled with the old treatment.
        # No-op for every clinic that never sets these keys.
        if new_state in specialty_flow.TREATMENT_RESET_STATES:
            specialty_flow.clear_treatment_context(merged)

        update_payload = {
            "state": new_state,
            "context": merged,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # If entering/advancing in a booking state, refresh the 30-minute booking expiry
        if new_state in MID_BOOKING_STATES:
            update_payload["booking_context_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat()
        elif new_state in ["main_menu", "idle", "selecting_language", "awaiting_consent"]:
            # Any return to main menu or idle clears stale mid-booking expiry timestamp —
            # otherwise a leftover value from an old abandoned booking falsely times out
            # the next booking attempt on its very first message.
            update_payload["booking_context_expires_at"] = None

        try:
            await sb(supabase.table("conversations").update(update_payload).eq(
                "clinic_id", clinic["id"]
            ).eq("phone", phone))
        except Exception as e:
            logger.warning(f"Error updating conversation state: {e}")

    async def get_patient_language(self, clinic: dict, phone: str) -> str:
        from app.database import supabase

        patient = (
            await sb(supabase.table("patients")
            .select("language")
            .eq("clinic_id", clinic["id"])
            .eq("phone", phone))
        )
        if patient.data and patient.data[0].get("language"):
            return patient.data[0]["language"]
        return "en"

    def __init__(self):
        self.whatsapp = whatsapp_service

    async def handle_message(
        self,
        clinic: dict,
        phone: str,
        message: str,
        message_type: str = "text",
        message_id: Optional[str] = None,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Handle incoming message with all guards.

        Meta Webhook Timeout Protection:
          Meta requires 200 OK within 20 seconds. Our webhook already returns
          200 immediately via BackgroundTasks, but the per-phone asyncio.Lock
          can cause cascading delays if Groq has a latency spike.

          Solution: acquire_phone_lock_with_timeout() waits at most 15 seconds
          for the lock. If it times out, the message is deferred to the
          Supabase dead-letter queue for retry rather than blocking indefinitely.
        """

        clinic_id = clinic["id"]

        # ── Per-phone asyncio lock with timeout ────────────────────────────────
        # If two messages from the same patient arrive simultaneously, the second
        # waits up to 15s for the first to finish. If it can't acquire in time
        # (e.g., Groq latency spike), it defers gracefully instead of blocking.
        acquired = await acquire_phone_lock_with_timeout(phone, timeout=15)
        if not acquired:
            # Lock timed out — the previous message is still processing.
            # Parked for replay by SchedulerService.drain_pending_retry_messages
            # (every 5 min, gives up after 30 min) rather than dropped.
            logger.warning(
                f"Phone lock timeout for {phone[:6]}*** — deferring message "
                f"{message_id} to dead-letter queue"
            )
            try:
                from app.database import supabase
                import json

                # unscoped: insert_scoped_by_payload
                await sb(supabase.table("failed_messages").insert(
                    {
                        "phone": phone,
                        "display_phone": clinic.get("phone", ""),
                        "clinic_id": clinic_id,  # KA-20: Promote to column for per-tenant DLQ triage
                        "payload": json.dumps(
                            {
                                "message": message[:500],
                                "message_type": message_type,
                                "message_id": message_id,
                                "clinic_id": clinic_id,
                                # Without this the replay loses the button/list
                                # reply ID and only keeps its title. A doctor
                                # pick whose ID is a UUID then replays as the
                                # doctor's display name and resolves to nothing.
                                "interactive_data": interactive_data,
                            }
                        ),
                        "error": "Phone lock timeout (15s) — previous message still processing",
                        "status": "pending_retry",
                    }
                ))
            except Exception as dlq_err:
                logger.error(f"Failed to save timed-out message to DLQ: {dlq_err}")
            return

        # Lock acquired — process the message with guaranteed cleanup
        try:
            # We already hold the lock from acquire_phone_lock_with_timeout(),
            # so _handle_message_locked runs exclusively for this phone.
            await self._handle_message_locked(
                clinic=clinic,
                phone=phone,
                message=message,
                message_type=message_type,
                message_id=message_id,
                interactive_data=interactive_data,
            )
            # Record last_processed_message_id only upon successful completion
            if message_id:
                try:
                    await update_conversation(
                        clinic_id, phone, {"last_processed_message_id": message_id}
                    )
                except Exception as update_err:
                    logger.warning(
                        f"Failed to record last_processed_message_id for {mask_phone(phone)}: {update_err}"
                    )
        finally:
            # Releases the local lock, its refcount, and the distributed lease.
            await release_phone_lock_acquired(phone)

    async def _handle_message_locked(
        self,
        clinic: dict,
        phone: str,
        message: str,
        message_type: str = "text",
        message_id: Optional[str] = None,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Inner handler called while holding the per-phone asyncio lock."""
        clinic_id = clinic["id"]

        # Guard 1: Duplicate webhook delivery (secondary check at conversation layer)
        session = await get_or_create_conversation(clinic_id, phone)
        if message_id and session.get("last_processed_message_id") == message_id:
            logger.info(f"Duplicate dropped at conversation layer: {message_id}")
            return

        # No mark_as_read here: app/routers/webhook.py already fires it as a
        # background task the moment the message is dispatched. Awaiting a
        # second one put an extra Meta API round-trip on the critical path of
        # every single patient message, before any reply could be composed.

        # Get or create patient
        patient = await get_patient_by_phone(clinic["id"], phone)
        if not patient:
            patient = await create_patient(clinic["id"], phone)
            logger.info(f"Created new patient for {mask_phone(phone)}")

        # Extract clean text from multi-line messages that include WhatsApp quoted prompt headers
        if message_type == "text":
            message = extract_clean_message_content(message)

        # Determine language - use None if not set (don't default here)
        lang = patient.get("language") or "en"

        # Guard 2: Session timeout mid-booking
        booking_expires = session.get("booking_context_expires_at")

        if booking_expires and session.get("state") in MID_BOOKING_STATES:
            expires_dt = datetime.fromisoformat(booking_expires.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expires_dt:
                await update_conversation(
                    clinic["id"],
                    phone,
                    {
                        "state": "main_menu",
                        "context": {},
                        "booking_context_expires_at": None,
                    },
                )
                await self.whatsapp.send_text(
                    clinic, phone, get_message("session_timeout", lang)
                )
                await self._send_main_menu(clinic, phone, lang)
                return

        # Reset booking timer on every message while mid-booking
        if session.get("state") in MID_BOOKING_STATES:
            expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            await update_conversation(
                clinic["id"], phone, {"booking_context_expires_at": expires}
            )

        # Update session expiry (24 hours from now)
        session_expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        await update_conversation(
            clinic["id"], phone, {"session_expires_at": session_expires}
        )

        # ── Unreadable message types (voice notes, photos, PDFs, location) ────
        # Placed after the session/booking timers above — a patient who sends a
        # voice note IS engaged, so their booking window should still refresh —
        # but before intent detection, so we never pay for an LLM call on an
        # empty body or answer a media message with a state-machine error.
        # State is deliberately left untouched: they can retype and carry on
        # exactly where they left off.
        if message_type not in READABLE_MESSAGE_TYPES:
            if message_type in IGNORED_MESSAGE_TYPES:
                logger.info(
                    f"Ignoring non-request message type '{message_type}' from {mask_phone(phone)}"
                )
                return
            await self.whatsapp.send_text(
                clinic,
                phone,
                get_message(
                    "unsupported_media",
                    lang,
                    phone=clinic.get("whatsapp_number") or settings.hospital_phone,
                ),
            )
            logger.info(
                f"Unreadable message type '{message_type}' from {mask_phone(phone)} "
                f"in state '{session.get('state')}' — asked patient to type instead"
            )
            return
        # ── End unreadable message types ──────────────────────────────────────

        # ── Clinical Firewall: Screen for medical advice requests ──────────────
        # This runs BEFORE the LLM is called. If a patient asks for medication
        # names, dosages, or diagnoses, we return a safe static response and
        # never reach the Groq API — protecting against NMC liability.
        # Skip firewall for interactive button responses (they are controlled inputs)
        if message_type == "text" and message.strip():
            lang_for_firewall = patient.get("language") or "en"
            firewall_blocked, firewall_response = screen_message(
                message, lang_for_firewall
            )
            if firewall_blocked and firewall_response:
                await self.whatsapp.send_text(clinic, phone, firewall_response)
                # Specialty clinics only: the firewall stays exactly as strict,
                # but the patient also gets a way into the clinic's own catalogue.
                await specialty_flow.offer_treatment_browse(self, clinic, phone, lang_for_firewall)
                logger.info(
                    f"Clinical firewall blocked message from {phone[:6]}*** "
                    f"(type: medication/diagnosis request)"
                )
                return
        # ── End Clinical Firewall ──────────────────────────────────────────────

        # Detect intent (skip LLM inference for controlled interactive button clicks)
        if message_type == "interactive" and interactive_data:
            intent = "button_click"
        else:
            intent = await detect_intent(message, clinic)

        # Handle interactive button responses FIRST (before guards)
        if message_type == "interactive" and interactive_data:
            button_id = interactive_data.get("id", "")
            if button_id in ["en", "hi", "te", "lang_en", "lang_hi", "lang_te"]:
                intent = "select_language"
            elif button_id in ["self", "for_self"]:
                lang = await get_lang(clinic, phone)
                patient_local = await get_patient_by_phone(clinic["id"], phone)
                patient_name = (patient_local or {}).get("name", "")
                ctx = session.get("context", {}) or {}
                ctx["for_self"] = True
                ctx["booking_name"] = patient_name
                if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, ctx, lang):
                    return
                await update_conversation(
                    clinic["id"],
                    phone,
                    {"context": ctx, "state": "collecting_symptoms"},
                )
                await self.whatsapp.send_text(
                    clinic, phone, get_message("ask_symptoms", lang)
                )
                return

            elif button_id in ["family", "for_family"]:
                lang = await get_lang(clinic, phone)
                ctx = session.get("context", {}) or {}
                ctx["for_self"] = False
                await update_conversation(clinic["id"], phone, {"context": ctx})
                await self.whatsapp.send_text(
                    clinic, phone, get_message("ask_name", lang)
                )
                return

            elif button_id in specialty_flow.TREATMENT_BUTTON_IDS or button_id.startswith(
                specialty_flow.TREATMENT_BUTTON_PREFIXES
            ):
                # Specialty treatments: menu rows, category/treatment lists and
                # card buttons. Returns to the main menu when the clinic no
                # longer has the feature (a stale list tapped after a plan change).
                lang = await get_lang(clinic, phone)
                await specialty_flow.handle_treatment_button(self, clinic, phone, button_id, session, lang)
                return
            elif button_id == "continue_booking":
                intent = "continue_booking"
            elif button_id == "restart_booking":
                intent = "restart_booking"
            elif button_id.startswith("dept_"):
                intent = "select_department"
                message = button_id.replace("dept_", "")
            elif button_id.startswith("doc_"):
                intent = "select_doctor"
                # IDs are formatted as doc_{index}_{name}, extract just the name
                parts = button_id.split("_", 2)
                message = parts[2] if len(parts) > 2 else button_id.replace("doc_", "")
            elif button_id.startswith(_MORE_DOCTORS_ID + "_"):
                # MUST precede the "view_doc_" branch below: "view_doc_more_2"
                # also starts with "view_doc_", and falling through would look
                # up a doctor whose id is "more_2", find nothing, and re-show
                # page 0 - an endless loop on the first ten doctors.
                intent = "view_doctors_page"
                message = button_id[len(_MORE_DOCTORS_ID) + 1:]
            elif button_id.startswith("view_doc_"):
                intent = "view_doctor"
                message = button_id.replace("view_doc_", "")
            elif button_id.startswith("svc_"):
                intent = "select_service"
                message = button_id
            elif button_id.startswith("slot_"):
                intent = "select_slot"
                message = button_id.replace("slot_", "")
            elif button_id.startswith("dtslot_"):
                intent = "select_datetime"
                message = button_id.replace("dtslot_", "")
            elif button_id.startswith("date_"):
                intent = "select_date"
                message = button_id.replace("date_", "")
            elif button_id == "confirm_yes":
                intent = "confirm_booking"
            elif button_id == "confirm_no":
                intent = "edit_booking"
            elif button_id in ["main_menu", "go_main_menu"]:
                lang = await get_lang(clinic, phone)
                await self.update_state(
                    clinic, phone, "main_menu", {"menu_shown": False}
                )
                await self._send_main_menu(clinic, phone, lang)
                return

            elif button_id == "book_another":
                lang = await get_lang(clinic, phone)
                patient = await get_patient_by_phone(clinic["id"], phone)
                await self._start_booking(clinic, phone, patient, lang)
                return

            elif button_id == "suggest_yes":
                intent = "accept_suggestion"
            elif button_id == "suggest_no":
                intent = "reject_suggestion"
            elif button_id == "checkin_concern":
                intent = "health_checkin_concern"
            elif button_id == "checkin_ok":
                intent = "health_checkin_ok"
            elif button_id.startswith("fam_") or button_id.startswith("save_family_"):
                message = button_id
            elif button_id == "edit_doctor":
                intent = "edit_doctor"
            elif button_id == "edit_date":
                intent = "edit_date"
            elif button_id == "edit_time":
                intent = "edit_time"
            elif button_id in [
                "chest_severe",
                "chest_mild",
                "back_lower",
                "back_upper",
            ]:
                intent_map = {
                    "chest_severe": "severe chest pain",
                    "chest_mild": "mild chest pain",
                    "back_lower": "lower back pain",
                    "back_upper": "upper back pain",
                }
                message = intent_map.get(button_id, message)
            elif button_id.startswith("cancel_"):
                appointment_id = button_id.replace("cancel_", "")
                lang = await get_lang(clinic, phone)
                cancelled, refund = await self._cancel_with_refund(
                    clinic, phone, appointment_id
                )

                if cancelled:
                    if refund is None:
                        # Nothing was paid — plain confirmation, no money talk.
                        cancel_msg = {
                            "en": "Your appointment has been cancelled successfully.",
                            "hi": "आपका अपॉइंटमेंट सफलतापूर्वक रद्द कर दिया गया है।",
                            "te": "మీ అపాయింట్మెంట్ విజయవంతంగా రద్దు చేయబడింది.",
                        }.get(lang, "Appointment cancelled.")
                        await self.whatsapp.send_text(clinic, phone, cancel_msg)
                else:
                    await self.whatsapp.send_text(
                        clinic,
                        phone,
                        "Could not cancel. Please call us: "
                        + clinic["whatsapp_number"],
                    )

                await self.update_state(clinic, phone, "main_menu", {})
                await self._send_main_menu(clinic, phone, lang)
                return

            elif button_id == "menu_doctors":
                lang = await get_lang(clinic, phone)
                if await self._is_diagnostics_only(clinic):
                    await self._start_lab_booking(clinic, phone, lang)
                else:
                    await self._show_doctors(clinic, phone, lang)
                return

            elif button_id == "menu_services":
                lang = await get_lang(clinic, phone)
                if await self._is_diagnostics_only(clinic):
                    await self._start_lab_booking(clinic, phone, lang)
                else:
                    await self._show_services(clinic, phone, lang)
                return

            elif button_id in ("book_lab_test", "menu_lab_tests"):
                # Quick-reply payload on the lab report delivery template, and
                # the standing "Book Lab Test" menu row. Tapping a template
                # button is an inbound message, so it legitimately opens the
                # 24h window and the full catalogue can be shown here.
                lang = await get_lang(clinic, phone)
                await self._start_lab_booking(clinic, phone, lang)
                return

            elif button_id.startswith("labsvc_"):
                # A service type (Health Packages, Radiology...) on the main menu.
                lang = await get_lang(clinic, phone)
                await self._start_lab_booking_for_heading(
                    clinic, phone, button_id.removeprefix("labsvc_"), lang
                )
                return

            elif button_id == "menu_help":
                lang = await get_lang(clinic, phone)
                await self._send_help_guide(clinic, phone, lang)
                return

            elif button_id == "menu_reports":
                lang = await get_lang(clinic, phone)
                await self._handle_view_reports(clinic, phone, lang)
                return

            elif button_id == "menu_book":
                lang = await get_lang(clinic, phone)
                patient_obj = await get_patient_by_phone(clinic["id"], phone)
                await self._start_booking(clinic, phone, patient_obj, lang)
                return

            elif button_id == "menu_emergency":
                lang = await get_lang(clinic, phone)
                await self._handle_emergency(clinic, phone, lang)
                return

            elif button_id == "menu_human":
                lang = await get_lang(clinic, phone)
                await self._handle_human_escalation(clinic, phone, lang)
                return

            elif button_id.startswith("branch_"):
                intent = "select_branch"
                message = button_id  # Pass full button_id for branch resolution

        # Guard 5: Concurrent booking protection
        # Only trigger when user says "book appointment" via text while deep in booking
        # Skip states where user text input is expected (names, symptoms, dates, slots, etc.)
        SAFE_STATES = [
            "collecting_name",
            "collecting_symptoms",
            "suggesting_department",
            # Lab flow: the "already booking with <doctor>" prompt has no
            # doctor to name, and "book test" restarts the lab flow anyway.
            "confirming_collection_date",
        ]
        if (
            intent == "book_appointment"
            and session.get("state") in MID_BOOKING_STATES
            and session.get("state") not in SAFE_STATES
            and message_type != "interactive"
        ):
            context = session.get("context", {})
            doctor = context.get("doctor_name", "this doctor")
            await self.whatsapp.send_interactive_buttons(
                clinic,
                phone,
                body=get_message("already_booking", lang, doctor=doctor),
                buttons=[
                    {
                        "id": "continue_booking",
                        "title": (
                            "Continue"
                            if lang == "en"
                            else ("जारी रखें" if lang == "hi" else "కొనసాగించు")
                        ),
                    },
                    {
                        "id": "restart_booking",
                        "title": (
                            "Start Over"
                            if lang == "en"
                            else (
                                "फिर से शुरू" if lang == "hi" else "మళ్లీ ప్రారంభించు"
                            )
                        ),
                    },
                ],
            )
            return

        # Handle global views (interactive buttons from _show_doctors and _show_services)
        if intent == "view_doctors_page":
            lang = await get_lang(clinic, phone)
            try:
                next_page = int(message)
            except (TypeError, ValueError):
                next_page = 0
            await self._show_doctors(clinic, phone, lang, page=next_page)
            return

        if intent == "view_doctor":
            from app.database import supabase

            res = (
                await sb(supabase.table("doctors")
                .select("*")
                .eq("clinic_id", clinic["id"])
                .eq("id", message)
                .eq("is_active", True))
            )
            if not res.data:
                lang = await get_lang(clinic, phone)
                no_doc_msg = {
                    "en": "This doctor is no longer available for online booking. Please choose another doctor.",
                    "hi": "यह डॉक्टर अब ऑनलाइन बुकिंग के लिए उपलब्ध नहीं हैं। कृपया अन्य डॉक्टर चुनें।",
                    "te": "ఈ డాక్టర్ ఇప్పుడు ఆన్‌లైన్ బుకింగ్ కోసం అందుబాటులో లేరు. దయచేసి మరొక డాక్టర్‌ను ఎంచుకోండి.",
                }.get(lang, "This doctor is no longer available. Please choose another doctor.")
                await self.whatsapp.send_text(clinic, phone, no_doc_msg)
                await self._show_doctors(clinic, phone, lang)
                return

            doc = res.data[0]
            lang = await get_lang(clinic, phone)

            # Fetch branch assignments for hierarchical display
            branch_res = (
                await sb(supabase.table("doctor_branches")
                .select("branch_id, session, branches(id, name, short_name, address, landmark, maps_link)")
                .eq("doctor_id", doc["id"]))
            )
            branches = branch_res.data or []

            session_label = {
                "morning": "🌅 Morning",
                "evening": "🌆 Evening",
                "both": "🌅 Morning & 🌆 Evening",
            }
            spec = doc.get("specialization")
            dept = doc.get("department", "")
            sub_title = f"🩺 {spec} | {dept}" if spec else (f"🩺 {dept}" if dept else "")
            detail_lines = [f"👨‍⚕️ *{doc.get('name', 'Doctor')}*"]
            if sub_title:
                detail_lines.append(sub_title)
            if doc.get("consultation_fee") is not None:
                detail_lines.append(f"💰 Consultation Fee: ₹{doc['consultation_fee']}")
            if doc.get("rating"):
                detail_lines.append(f"⭐ Rating: {doc['rating']}")
            detail_lines.append("")

            if branches:
                detail_lines.append("📍 *Available Locations & Sessions:*")
                for b in branches:
                    binfo = b.get("branches") or {}
                    bname = binfo.get("short_name") or binfo.get("name", "Branch")
                    sess = session_label.get(b.get("session", "both"), "All sessions")
                    detail_lines.append(f"• *{bname}*: {sess}")
                detail_lines.append("")

            await self.whatsapp.send_text(clinic, phone, "\n".join(detail_lines))

            context = session.get("context", {})
            # Our Doctors is not the treatment flow; drop any abandoned tag.
            specialty_flow.clear_treatment_context(context)
            context["doctor"] = doc
            context["doctor_name"] = doc["name"]
            context["department"] = doc["department"]
            context["selected_doctor_id"] = message

            # If multi-branch doctor, verify whether pre-selected branch belongs to this doctor
            doctor_branch_ids = {b["branch_id"] for b in branches if b.get("branch_id")}
            current_branch_id = context.get("branch_id")

            if len(branches) > 1 and (not current_branch_id or current_branch_id not in doctor_branch_ids):
                await self._send_doctor_branch_selection(clinic, phone, doc, branches, lang)
                await self.update_state(clinic, phone, "selecting_branch", context)
                return
            elif len(branches) == 1 and branches[0].get("branch_id"):
                # Single branch assigned — auto-attach all branch fields atomically
                binfo = dict(branches[0].get("branches") or {})
                binfo["id"] = binfo.get("id") or branches[0]["branch_id"]
                self._set_branch_context(
                    context, binfo, session_val=branches[0].get("session", "both")
                )
            elif len(branches) > 1 and current_branch_id in doctor_branch_ids:
                # Valid branch already picked — refresh all fields from this doctor's branch row to ensure consistency
                match = next((b for b in branches if b["branch_id"] == current_branch_id), None)
                if match:
                    binfo = dict(match.get("branches") or {})
                    binfo["id"] = binfo.get("id") or match["branch_id"]
                    self._set_branch_context(
                        context, binfo, session_val=match.get("session", "both")
                    )

            await self._show_date_picker(clinic, phone, context, lang)
            await self.update_state(clinic, phone, "selecting_date", context)
            return

        # Process based on state and intent
        await self._process_state(
            clinic, phone, message, intent, session, patient, lang, interactive_data
        )

    async def _process_state(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        session: dict,
        patient: dict,
        lang_ignored: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Process message based on current state."""
        lang = await get_lang(clinic, phone)

        state = session.get("state", "idle")
        context = session.get("context", {})

        # Global guard: Language must be set before any interaction (except selecting_language)
        if state != "selecting_language" and not patient.get("language"):
            await self._send_language_selection(clinic, phone)
            await self.update_state(clinic, phone, "selecting_language")
            return

        # Emergency can trigger from ANY state
        if intent == "emergency":
            await self._handle_emergency(clinic, phone, lang)
            return

        if intent == "health_checkin_concern":
            await self._handle_health_checkin_concern(clinic, phone, lang)
            return

        if intent == "health_checkin_ok":
            await self._handle_health_checkin_ok(clinic, phone, lang)
            return

        if intent == "queue_status":
            await self._handle_queue_status(clinic, phone, lang)
            return

        # Opt-out can trigger from ANY state
        if intent == "opt_out":
            await self._handle_opt_out(clinic, phone, patient, lang)
            return

        # Data deletion request
        if intent == "data_deletion_request":
            await self._handle_data_deletion(clinic, phone, patient, lang)
            return

        # Human escalation
        if intent == "human_escalation":
            await self._handle_human_escalation(clinic, phone, lang)
            return

        # "help" / "how to use" -- from any state, typed only. The state is left
        # alone so a patient mid-booking can read the guide and carry on.
        typed_norm = "" if interactive_data else (message or "").strip().lower()
        if typed_norm in HELP_KEYWORDS:
            await self._send_help_guide(clinic, phone, lang)
            return

        # "start" after "stop": turn follow-up messages back on. Only for a
        # patient who actually opted out -- for everyone else the word means
        # whatever it meant before.
        if typed_norm in RESUBSCRIBE_KEYWORDS and patient.get("opted_in") is False:
            await self._handle_opt_in(clinic, phone, lang)
            return

        # A question ABOUT the clinic -- "where are you located", "what are
        # your timings", "your contact number". Answered from any state and
        # WITHOUT touching it, exactly like the help guide above: a patient
        # halfway through a booking who asks where to come keeps their place.
        # Placed ahead of the free-text capture branches below (lab search,
        # treatment search, typed patient name) because those swallow any text
        # handed to them -- which is how "Where are you located" came back as
        # 'No test matched "Where are you located"'.
        # Deliberately NOT before consent: DPDP consent has to be answered
        # before this bot holds a conversation, and a patient sitting in
        # `idle` without it is one the state machine is about to send the
        # consent prompt to. Anyone mid-booking has consented by definition,
        # so this only gates the first conversation.
        if (
            intent == "clinic_info"
            and patient.get("data_consent")
            and state not in ("selecting_language", "awaiting_consent")
        ):
            if await self._answer_clinic_info(clinic, phone, message, state, lang):
                return
            # False means the LLM alone reached clinic_info while the patient
            # was mid-flow. Fall through to the routing that ran before this
            # block existed, so a misread test name is still searched.

        # Language change request (but NOT when already selecting language - let state machine handle it)
        if state != "selecting_language" and (
            intent in ["change_language", "select_language"]
            or message.lower() in ["change language", "भाषा बदलें", "భాష మార్చు"]
        ):
            # A language button tapped on an earlier picker already says which
            # language: apply it rather than asking again. Only for a patient
            # who has consented -- everyone else goes through the picker so the
            # consent step in _handle_selecting_language still runs.
            if intent == "select_language" and interactive_data and patient.get("data_consent"):
                await self._handle_selecting_language(
                    clinic, phone, message, patient, interactive_data
                )
                return
            # Typed with the language named -- "switch to Telugu", "hindi
            # please" -- is the same answer as tapping that button, so it is
            # applied the same way, under the same consent rule.
            named = None if interactive_data else language_change_request(message)
            if named in ("en", "hi", "te") and patient.get("data_consent"):
                await self._handle_selecting_language(
                    clinic, phone, message, patient, {"id": f"lang_{named}"}
                )
                return
            await self._send_language_selection(clinic, phone)
            await self.update_state(clinic, phone, "selecting_language")
            return

        # A patient typing while browsing the test catalogue is searching it.
        # Placed ahead of the global menu intents because a bare test name
        # ("thyroid", "urine sodium") classifies as view_services /
        # doctor_availability, and at a diagnostics-only clinic both call
        # _start_lab_booking -- which would reset the search to page 1 of the
        # unfiltered catalogue and make search impossible to use.
        # Emergency, opt-out, escalation and language change are all handled
        # above this point, so they still win.
        if (
            state == "browsing_lab_tests"
            and not interactive_data
            and (message or "").strip()
            # "book test" and friends are handled a few lines below; the exit
            # words are handled by _handle_browsing_lab_tests itself.
            and message.strip().lower() not in LAB_BOOKING_KEYWORDS
            # Intents no test name is plausibly confused with; leaving them to
            # the handlers below keeps every escape hatch reachable.
            and intent
            not in {
                "greeting",
                "book_appointment",
                "cancel_appointment",
                "reschedule_appointment",
                "view_reports",
            }
        ):
            await self._handle_browsing_lab_tests(
                clinic, phone, message, intent, context, lang, interactive_data
            )
            return
        # The lab flow's typed patient name, for the same reason: a name is
        # free text the classifier may call anything (doctor_availability at a
        # diagnostics-only clinic restarts the whole lab flow). Navigation,
        # cancel and the always-first handlers above still win.
        if (
            state == "confirming_collection_date"
            and context.get("lab_step") == "name"
            and not interactive_data
            and (message or "").strip()
            and message.strip().lower() not in NAV_KEYWORDS | LAB_BOOKING_KEYWORDS
            and intent not in {"greeting", "cancel_appointment", "reschedule_appointment"}
        ):
            await self._handle_confirming_collection_date(
                clinic, phone, message, intent, context, patient, lang, interactive_data
            )
            return
        # A patient typing while browsing or searching treatments is describing a
        # concern. Placed ahead of the global menu intents for the same reason as
        # the lab-test search above: a free-text concern ("hair fall") can be
        # classified as view_services / doctor_availability and would otherwise
        # be hijacked. Emergency, opt-out, escalation and language change are
        # handled above this point, so they still win.
        if (
            state in ("browsing_treatments", "searching_treatments")
            and not interactive_data
            and (message or "").strip()
            and message.strip().lower() not in NAV_KEYWORDS
            and intent not in {"greeting", "book_appointment", "cancel_appointment", "reschedule_appointment"}
        ):
            await specialty_flow.handle_treatment_search_text(self, clinic, phone, message, lang)
            return

        # Global handlers for top-level menu intents (escape hatches from selection states)
        if state not in ["selecting_language", "awaiting_consent"]:
            msg_lower = message.strip().lower()

            # Ahead of every intent check: see LAB_BOOKING_KEYWORDS for why the
            # classifier cannot be trusted with these words. The lab report
            # caption tells patients to reply "BOOK TEST" and the report
            # template's quick-reply button carries the same words, so a patient
            # arriving here is rarely sitting in main_menu. Clinics without lab
            # booking fall through to the intent handlers below.
            if msg_lower in LAB_BOOKING_KEYWORDS:
                from app.services.tenant import has_feature

                if has_feature(clinic, "lab_test_booking"):
                    await self._start_lab_booking(clinic, phone, lang)
                    return

            # "I have sugar, what test can I do", "cost of CBC": the question
            # itself is the search. The catalogue answers it, never the model
            # -- only tests this centre actually sells are listed. Skipped
            # where the patient is answering OUR question in free text (a
            # name, their symptoms): "I have sugar" there is the answer.
            if (
                intent == "find_tests"
                and not interactive_data
                and state not in self._FREE_TEXT_ANSWER_STATES
                # Consent answered (either way) first, as for clinic_info.
                and patient.get("data_consent") is not None
            ):
                from app.services.tenant import has_feature

                if has_feature(clinic, "lab_test_booking"):
                    await self._start_lab_booking(clinic, phone, lang, query=message)
                    return
                # The plan changed under an open conversation: the same
                # answer "services" questions got before find_tests existed.
                intent = "view_services"

            if intent == "doctor_availability":
                if await self._is_diagnostics_only(clinic):
                    await self._start_lab_booking(clinic, phone, lang)
                else:
                    await self._show_doctors(clinic, phone, lang)
                return

            if intent == "view_services":
                if await self._is_diagnostics_only(clinic):
                    await self._start_lab_booking(clinic, phone, lang)
                else:
                    await self._show_services(clinic, phone, lang)
                return

            if intent == "view_reports":
                await self._handle_view_reports(clinic, phone, lang)
                return

            # Explicit navigation / reset to main menu
            CHOICE_STATES = {
                "main_menu",
                "selecting_department",
                "selecting_doctor",
                "selecting_branch",
                "selecting_date",
                "selecting_slot",
            }
            if (
                msg_lower in NAV_KEYWORDS
                or (intent == "greeting" and state in CHOICE_STATES and state != "main_menu")
            ):
                await self.update_state(clinic, phone, "main_menu", {"menu_shown": False})
                await self._send_main_menu(clinic, phone, lang)
                return

            if (
                intent == "book_appointment"
                and state in {"selecting_department", "selecting_doctor", "selecting_branch"}
            ):
                await self._start_booking(clinic, phone, patient, lang)
                return

            if intent == "cancel_appointment" and state not in ["cancelling_select_appointment"]:
                await self._handle_cancel_request(clinic, phone, patient, lang)
                return

            if intent == "reschedule_appointment" and not state.startswith("rescheduling_"):
                await self._handle_reschedule_request(clinic, phone, patient, lang)
                return

        # State machine
        if state == "idle":
            await self._handle_idle(clinic, phone, message, intent, patient, lang)
        elif state == "selecting_language":
            await self._handle_selecting_language(
                clinic, phone, message, patient, interactive_data
            )
        elif state == "awaiting_consent":
            await self._handle_awaiting_consent(
                clinic, phone, message, patient, lang, interactive_data
            )
        elif state == "main_menu":
            await self._handle_main_menu(clinic, phone, message, intent, patient, lang)
        elif state == "selecting_branch":
            await self._handle_selecting_branch(
                clinic, phone, message, intent, context, patient, lang, interactive_data
            )
        elif state == "selecting_family_member":
            await self._handle_selecting_family_member(
                clinic, phone, message, context, lang, patient
            )
        elif state == "collecting_name":
            await self._handle_collecting_name(
                clinic, phone, message, context, patient, lang
            )
        elif state == "confirming_save_family_member":
            await self._handle_confirming_save_family_member(
                clinic, phone, message, context, lang
            )
        elif state in ("collecting_symptoms", "asking_symptoms"):
            await self._handle_collecting_symptoms(
                clinic, phone, message, context, patient, lang
            )
        elif state == "suggesting_department":
            await self._handle_suggesting_department(
                clinic, phone, message, intent, context, lang, interactive_data
            )
        elif state == "selecting_department":
            await self._handle_selecting_department(
                clinic, phone, message, intent, context, lang, interactive_data
            )
        elif state == "selecting_doctor":
            await self._handle_selecting_doctor(
                clinic, phone, message, intent, context, lang, interactive_data
            )
        elif state == "selecting_date":
            await self._handle_selecting_date(clinic, phone, message, context, lang)
        elif state == "selecting_slot":
            await self._handle_selecting_slot(
                clinic, phone, message, intent, context, lang
            )
        elif state == "confirming_booking":
            await self._handle_confirming_booking(
                clinic, phone, message, intent, context, patient, lang
            )
        elif state == "awaiting_payment":
            await self._handle_awaiting_payment(
                clinic, phone, message, context, patient, lang
            )
        elif state == "browsing_lab_tests":
            await self._handle_browsing_lab_tests(
                clinic, phone, message, intent, context, lang, interactive_data
            )
        elif state == "confirming_collection_date":
            await self._handle_confirming_collection_date(
                clinic, phone, message, intent, context, patient, lang, interactive_data
            )
        elif state in ("browsing_treatments", "searching_treatments"):
            # message is forwarded so a concern the classifier mislabelled is
            # still searched rather than answered with the main menu; a tapped
            # row carries its own title as `message` and must not be, so an
            # interactive turn passes nothing.
            await specialty_flow.handle_treatment_state(
                self, clinic, phone, lang,
                message="" if interactive_data else message,
            )
        # "viewing_reports" is no longer entered — the report archive is gone.
        # Sessions still parked in it from before this change fall to the
        # unknown-state branch below, which resets them to the main menu.
        elif state == "emergency":
            # Patient was in emergency state — process their new message normally
            # Reset to main_menu and handle as a main_menu interaction
            await self.update_state(clinic, phone, "main_menu")
            await self._handle_main_menu(clinic, phone, message, intent, patient, lang)
        else:
            # Unknown state, reset to main menu
            await self.update_state(clinic, phone, "main_menu")
            await self._send_main_menu(clinic, phone, lang)

    async def _handle_idle(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        patient: dict,
        lang: str,
    ) -> None:
        """Handle idle state - first interaction."""
        # Check if returning patient with language already set
        existing_lang = patient.get("language")
        has_visited = patient.get("visit_count", 0) > 0

        if existing_lang and existing_lang in ["en", "hi", "te"] and has_visited:
            # Returning patient — skip language picker
            if not patient.get("data_consent"):
                from app.database import get_conversation

                session = await get_conversation(clinic["id"], phone)
                if session and session.get("state") == "awaiting_consent":
                    return  # already sent, don't send again

                await self.whatsapp.send_interactive_buttons(
                    clinic,
                    phone,
                    body=get_message("consent_request", existing_lang),
                    buttons=[
                        {
                            "id": "consent_yes",
                            "title": (
                                "Yes"
                                if existing_lang == "en"
                                else ("हाँ" if existing_lang == "hi" else "అవును")
                            ),
                        },
                        {
                            "id": "consent_no",
                            "title": (
                                "No"
                                if existing_lang == "en"
                                else ("नहीं" if existing_lang == "hi" else "కాదు")
                            ),
                        },
                    ],
                )
                await self.update_state(clinic, phone, "awaiting_consent", {})
            else:
                patient_name = patient.get("name") or "there"
                first_name = patient_name.split()[0] if patient_name else "there"
                await self.whatsapp.send_text(
                    clinic,
                    phone,
                    get_message("welcome_back", existing_lang, name=first_name),
                )
                await self._send_main_menu(clinic, phone, existing_lang)
                await self.update_state(clinic, phone, "main_menu", {})
            return

        # New patient OR language not set → ALWAYS show language picker
        # Do NOT read the message content
        # Do NOT detect language from message
        # Do NOT set any language
        import logging

        logger = logging.getLogger(__name__)
        logger.info(
            f"IDLE: phone={mask_phone(phone)}, existing_lang={patient.get('language')}, visits={patient.get('visit_count')}"
        )

        await self._send_language_selection(clinic, phone)
        await self.update_state(clinic, phone, "selecting_language", {})
        return

    async def _send_language_selection(self, clinic: dict, phone: str) -> None:
        """Send language selection buttons."""
        body_text = f"Welcome to {clinic['name']} 🏥\nनमस्ते | నమస్కారం\n\nPlease select your language:\nअपनी भाषा चुनें | మీ భాష ఎంచుకోండి"
        await self.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=body_text,
            buttons=[
                {"id": "lang_en", "title": "English"},
                {"id": "lang_hi", "title": "हिंदी"},
                {"id": "lang_te", "title": "తెలుగు"},
            ],
        )

    async def _handle_selecting_language(
        self,
        clinic: dict,
        phone: str,
        message: str,
        patient: dict,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Handle language selection."""
        if interactive_data and interactive_data.get("id"):
            button_id = interactive_data.get("id", "")
            if button_id.startswith("lang_"):
                selected = button_id.replace("lang_", "")
            elif button_id in ["en", "hi", "te"]:
                selected = button_id
            else:
                # Invalid button fallback
                await self._send_language_selection(clinic, phone)
                return
        else:
            # Reject text inputs and force the picker usage
            await self._send_language_selection(clinic, phone)
            return

        # Validate selected language
        if selected not in ["en", "hi", "te"]:
            selected = "en"

        # Update patient language
        await update_patient(clinic["id"], phone, {"language": selected})

        # Check data consent - proceed to consent, NOT language picker again
        consent = patient.get("data_consent")
        if consent is None or consent is False:
            from app.database import get_conversation

            session = await get_conversation(clinic["id"], phone)
            state = session.get("state") if session else None
            if state == "awaiting_consent":
                return  # already sent consent, don't send again

            if state == "selecting_language":
                await self.whatsapp.send_interactive_buttons(
                    clinic,
                    phone,
                    body=get_message("consent_request", selected),
                    buttons=[
                        {
                            "id": "consent_yes",
                            "title": (
                                "Yes"
                                if selected == "en"
                                else ("हाँ" if selected == "hi" else "అవును")
                            ),
                        },
                        {
                            "id": "consent_no",
                            "title": (
                                "No"
                                if selected == "en"
                                else ("नहीं" if selected == "hi" else "కాదు")
                            ),
                        },
                    ],
                )
                await self.update_state(clinic, phone, "awaiting_consent", {})
            return

        # Get welcome message in selected language
        from app.services.tenant import get_clinic_contact

        emergency_number = get_clinic_contact(
            clinic, "emergency_number", settings.hospital_emergency_number
        )
        await self.whatsapp.send_text(
            clinic,
            phone,
            get_message(
                "welcome", selected, hospital_name=clinic.get("name", settings.hospital_name)
            ),
        )
        await self.whatsapp.send_text(
            clinic,
            phone,
            get_message("disclaimer", selected, emergency=emergency_number),
        )
        await self._send_main_menu(clinic, phone, selected)
        await self.update_state(clinic, phone, "main_menu")

    async def _handle_awaiting_consent(
        self,
        clinic: dict,
        phone: str,
        message: str,
        patient: dict,
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Handle data consent response."""
        from app.services.tenant import get_clinic_contact

        button_id = interactive_data.get("id") if interactive_data else None
        msg_lower = message.lower().strip()
        emergency_number = get_clinic_contact(
            clinic, "emergency_number", settings.hospital_emergency_number
        )

        if button_id == "consent_yes" or msg_lower in [
            "yes",
            "y",
            "ha",
            "हां",
            "అవును",
        ]:
            await update_patient(
                clinic["id"], phone, {"data_consent": True, "data_consent_at": "now()"}
            )
            await self.whatsapp.send_text(
                clinic,
                phone,
                get_message(
                    "welcome", lang, hospital_name=clinic.get("name", settings.hospital_name)
                ),
            )
            await self.whatsapp.send_text(
                clinic,
                phone,
                get_message("disclaimer", lang, emergency=emergency_number),
            )
            await self._send_main_menu(clinic, phone, lang)
            await self.update_state(clinic, phone, "main_menu")
        elif button_id == "consent_no" or msg_lower in [
            "no",
            "n",
            "nahin",
            "नहीं",
            "కాదు",
        ]:
            await update_patient(clinic["id"], phone, {"data_consent": False})
            await self.whatsapp.send_text(
                clinic,
                phone,
                get_message(
                    "welcome", lang, hospital_name=clinic.get("name", settings.hospital_name)
                ),
            )
            await self.whatsapp.send_text(
                clinic,
                phone,
                get_message("disclaimer", lang, emergency=emergency_number),
            )
            await self._send_main_menu(clinic, phone, lang)
            await self.update_state(clinic, phone, "main_menu")
        else:
            await self.whatsapp.send_interactive_buttons(
                clinic,
                phone,
                body=get_message("consent_request", lang),
                buttons=[
                    {
                        "id": "consent_yes",
                        "title": (
                            "Yes"
                            if lang == "en"
                            else ("हाँ" if lang == "hi" else "అవును")
                        ),
                    },
                    {
                        "id": "consent_no",
                        "title": (
                            "No"
                            if lang == "en"
                            else ("नहीं" if lang == "hi" else "కాదు")
                        ),
                    },
                ],
            )

    async def _send_main_menu(self, clinic: dict, phone: str, lang: str) -> None:
        """Send main menu with buttons."""
        from app.services.tenant import has_feature

        diagnostics_only = await self._is_diagnostics_only(clinic)

        book_title = {
            "en": "Book Lab Test" if diagnostics_only else "Book Appointment",
            "hi": "Book Lab Test" if diagnostics_only else "Book Appointment",
            "te": "Book Lab Test" if diagnostics_only else "Book Appointment",
        }.get(lang, "Book Lab Test" if diagnostics_only else "Book Appointment")

        titles = {
            "en": ["Our Doctors", "Emergency", "Talk to Staff"],
            "hi": ["Our Doctors", "Emergency", "Talk to Staff"],
            "te": ["Our Doctors", "Emergency", "Talk to Staff"],
        }
        t = titles.get(lang, titles["en"])

        # Specialty clinics with at least one published treatment lead with
        # their treatments. treatment_menu_active() is False — without a
        # database call — for every plan that existed before migration 077.
        treatment_menu = (not diagnostics_only) and await specialty_flow.treatment_menu_active(clinic)

        # A clinic that has published a first-visit consultation leads with
        # booking it: its patients arrive describing a symptom, not naming a
        # procedure. Driven by the catalogue, never by the plan -- see
        # specialty_flow.treatment_menu_rows.
        rows = (
            specialty_flow.treatment_menu_rows(
                lang, has_entry=await specialty_flow.has_entry_treatment(clinic["id"])
            )
            if treatment_menu
            else []
        )
        # A diagnostic centre whose catalogue is filed under two or more service
        # types (migration 080) shows them right here -- Health Packages,
        # Radiology & Imaging, Scans -- instead of one "Book Lab Test" row the
        # patient has to open to discover what the centre sells. One heading
        # (every unfiled catalogue) keeps the single row it always had.
        heading_rows = await self._lab_heading_menu_rows(clinic, lang) if diagnostics_only else []
        if heading_rows:
            rows.extend(heading_rows)
        else:
            rows.append({"id": "menu_book", "title": book_title[:24], "description": ""})
        if not diagnostics_only:
            # On a single-specialty plan "Our Services" would list one department;
            # Our Treatments replaces it. Override-enabled general clinics keep both.
            if not (treatment_menu and specialty_flow.is_specialty_plan(clinic)):
                services_title = {"en": "Our Services", "hi": "Our Services", "te": "Our Services"}.get(lang, "Our Services")
                rows.append({"id": "menu_services", "title": services_title[:24], "description": ""})
            rows.append({"id": "menu_doctors", "title": t[0][:24], "description": ""})
            # A clinic that does consultations AND lab tests had no lab row at
            # all — "Book Appointment" reads as doctors-only, so patients never
            # discovered they could book a test here.
            if has_feature(clinic, "lab_test_booking"):
                rows.append({"id": "menu_lab_tests", "title": "🧪 Book Lab Test"[:24], "description": ""})
        # No "My Reports" row. Kriya delivers each report the moment the lab
        # releases it; it is not an archive patients browse. A self-service list
        # would force us to hold every PDF for as long as any patient might ask
        # for it — storage we deliberately do not own. Older reports come from
        # the facility's own system, via reception.
        rows.append({"id": "menu_emergency", "title": t[1][:24], "description": ""})
        rows.append({"id": "menu_human", "title": t[2][:24], "description": ""})
        # Last, and only while there is room: Meta drops an eleventh row
        # silently, and Emergency must never be the row that goes.
        if len(rows) < 10:
            rows.append({
                "id": "menu_help",
                "title": {"en": "❓ How to use", "hi": "❓ उपयोग कैसे करें", "te": "❓ ఎలా ఉపయోగించాలి"}.get(
                    lang, "❓ How to use")[:24],
                "description": "",
            })

        sections = [{"title": "Menu", "rows": rows}]

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body=get_message("main_menu", lang),
            button_text=(
                "Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి")
            ),
            sections=sections,
        )

    async def _handle_main_menu(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        patient: dict,
        lang: str,
    ) -> None:
        """Handle main menu selections."""

        # Guard: Language must be set before proceeding
        if not patient.get("language"):
            await self._send_language_selection(clinic, phone)
            await self.update_state(clinic, phone, "selecting_language")
            return

        from app.database import get_conversation

        session = await get_conversation(clinic["id"], phone) or {}
        context = session.get("context", {})

        # Only show menu if not triggered by a specific button action
        # and menu hasn't been shown yet
        is_button_action = intent not in ["greeting", "unknown", None]

        if not context.get("menu_shown") and not is_button_action:
            await self._send_main_menu(clinic, phone, lang)
            context["menu_shown"] = True
            await self.update_state(clinic, phone, "main_menu", context)
            return

        if intent == "book_appointment" or message.lower() in [
            "book",
            "appointment",
            "बुक",
            "బుక్",
        ]:
            await self._start_booking(clinic, phone, patient, lang)
        elif intent == "view_services":
            if await self._is_diagnostics_only(clinic):
                await self._start_lab_booking(clinic, phone, lang)
            else:
                await self._show_services(clinic, phone, lang)
        elif intent == "doctor_availability":
            if await self._is_diagnostics_only(clinic):
                await self._start_lab_booking(clinic, phone, lang)
            else:
                await self._show_doctors(clinic, phone, lang)
        elif intent == "view_reports":
            await self._handle_view_reports(clinic, phone, lang)
        elif intent == "cancel_appointment":
            await self._handle_cancel_request(clinic, phone, patient, lang)
        elif intent == "reschedule_appointment":
            await self._handle_reschedule_request(clinic, phone, patient, lang)
        elif intent == "greeting":
            # Only show welcome_back for returning patients with language set
            if patient.get("visit_count", 0) > 0:
                patient_name = patient.get("name") or "there"
                first_name = patient_name.split()[0] if patient_name else "there"
                await self.whatsapp.send_text(
                    clinic, phone, get_message("welcome_back", lang, name=first_name)
                )

            # ALWAYS resend the menu if they say hi again
            await self._send_main_menu(clinic, phone, lang)
            context = session.get("context", {})
            context["menu_shown"] = True
            await self.update_state(clinic, phone, "main_menu", context)
        else:
            # Unknown intent: Let the LLM generate a conversational response
            try:
                from app.services.ai_engine import generate_response

                ai_reply = await generate_response(message, clinic, context, lang)
                await self.whatsapp.send_text(clinic, phone, ai_reply)
            except Exception:
                await self.whatsapp.send_text(
                    clinic, phone, get_message("invalid_input", lang)
                )

            # Resend menu to help them navigate back to structured flows
            await self._send_main_menu(clinic, phone, lang)
            context = session.get("context", {})
            context["menu_shown"] = True
            await self.update_state(clinic, phone, "main_menu", context)

    async def _is_diagnostics_only(self, clinic: dict) -> bool:
        """True if this clinic offers lab-test booking and has zero active
        doctors — i.e. it should never see the doctor/department flow."""
        from app.services.tenant import has_feature

        if not has_feature(clinic, "lab_test_booking"):
            return False
        doctors = await get_doctors(clinic["id"])
        return not doctors

    #: How many service types the diagnostics main menu shows as their own
    #: rows. With Emergency, Talk to Staff and How to use that is 10 -- Meta's
    #: cap. More headings than this collapse the tail into "All services".
    LAB_MENU_MAX_HEADINGS = 7

    #: Cache of each clinic's heading counts for the main menu, so "Hi" does
    #: not re-read a 1,392-row catalogue every time. 60s: an admin who files a
    #: new service type sees it on WhatsApp within a minute.
    _LAB_HEADING_TTL_SECONDS = 60
    _lab_heading_cache: dict = {}

    @staticmethod
    def _lab_heading_emoji(label: str) -> str:
        """An icon for a centre's own heading text, read off its wording."""
        text = (label or "").lower()
        rules = (
            (("package", "checkup", "check-up", "check up", "health", "wellness", "master"), "📦"),
            (("mri", "ct ", "ct)", "ct/", "(ct", "scan", "pet"), "🧲"),
            (("radiolog", "imaging", "x-ray", "xray", "x ray", "ultrasound", "usg", "sonograph",
              "mammo", "doppler"), "📷"),
            (("cardiac", "heart", "ecg", "echo", "tmt", "special"), "❤️"),
            (("patholog", "lab", "blood", "urine", "test"), "🧪"),
        )
        for words, emoji in rules:
            if any(w in text for w in words):
                return emoji
        return "🔬"

    @classmethod
    def _lab_heading_title(cls, label: str) -> str:
        """Icon + heading, within Meta's 24-character row title. A heading too
        long for both keeps its words and drops the icon: "Cardiac & Special
        Tests" beats "Cardiac & Special Tes"."""
        titled = f"{cls._lab_heading_emoji(label)} {label}"
        return titled if len(titled) <= 24 else (label or "")[:24]

    @staticmethod
    def _lab_heading_key(label: str) -> str:
        """Stable row-id suffix for a heading: its own text, lowercased.
        A tap on a stale menu is matched against the catalogue as it is NOW."""
        return (label or "").strip().lower()[:150]

    async def _lab_heading_groups(self, clinic: dict) -> list[tuple[str, int]]:
        """(heading, test count) for the whole active catalogue, largest first."""
        import time as _time
        from app.database import get_lab_tests

        cid = clinic["id"]
        hit = self._lab_heading_cache.get(cid)
        if hit and _time.monotonic() - hit[0] < self._LAB_HEADING_TTL_SECONDS:
            return hit[1]
        tests = await get_lab_tests(cid, active_only=True)
        groups = [(label, len(rows)) for label, rows in self._group_lab_tests_by_category(tests)]
        self._lab_heading_cache[cid] = (_time.monotonic(), groups)
        return groups

    async def _lab_heading_menu_rows(self, clinic: dict, lang: str) -> list[dict]:
        """One main-menu row per service type, or [] to keep "Book Lab Test".

        Never raises: the main menu is the one message every patient must get.
        """
        try:
            groups = await self._lab_heading_groups(clinic)
        except Exception as e:
            logger.warning(f"Lab heading menu rows unavailable for clinic {clinic.get('id')}: {e}")
            return []
        if len(groups) < 2:
            return []
        shown = groups if len(groups) <= self.LAB_MENU_MAX_HEADINGS else groups[: self.LAB_MENU_MAX_HEADINGS - 1]
        rows = [
            {
                "id": f"labsvc_{self._lab_heading_key(label)}",
                "title": self._lab_heading_title(label),
                "description": {
                    "en": f"{count} available", "hi": f"{count} उपलब्ध", "te": f"{count} అందుబాటులో",
                }.get(lang, f"{count} available")[:72],
            }
            for label, count in shown
        ]
        if len(shown) < len(groups):
            rows.append({
                "id": "menu_lab_tests",
                "title": {"en": "🔬 All services", "hi": "🔬 सभी सेवाएं", "te": "🔬 అన్ని సేవలు"}.get(
                    lang, "🔬 All services")[:24],
                "description": {"en": "Every test and service", "hi": "सभी टेस्ट और सेवाएं",
                                "te": "అన్ని పరీక్షలు, సేవలు"}.get(lang, "Every test and service")[:72],
            })
        return rows

    async def _start_lab_booking_for_heading(self, clinic: dict, phone: str, key: str, lang: str) -> None:
        """A service-type row tapped on the main menu. Resolved against the
        catalogue as it is now; a heading renamed since the menu was sent
        simply opens the full list of headings."""
        try:
            groups = await self._lab_heading_groups(clinic)
        except Exception:
            groups = []
        label = next((g for g, _ in groups if self._lab_heading_key(g) == key), None)
        await log_analytics_event(
            clinic["id"], phone, "lab_category_viewed", metadata={"category": label or key, "source": "menu"}
        )
        await self._start_lab_booking(clinic, phone, lang, category=label)

    async def _start_lab_booking(
        self,
        clinic: dict,
        phone: str,
        lang: str,
        category: Optional[str] = None,
        query: Optional[str] = None,
    ) -> None:
        """Entry point for the diagnostics-only lab-test flow.

        A diagnostic chain with several collection centres (Vijaya, Lucid,
        Apollo Diagnostics and the like) must ask WHERE before it asks WHAT:
        catalogues and prices differ per centre, and the booking has to carry a
        branch_id for the sample collection to be routed anywhere.

        Single-location centres -- the common case -- are auto-selected and
        never see the extra step, so nothing changes for them until the clinic
        actually adds a second branch in the admin panel.
        """
        from app.services.tenant import get_clinic_branches

        branches = await get_clinic_branches(clinic["id"])
        # Unlike the consultation flow, is_diagnostic branches are NOT filtered
        # out here -- for a diagnostics-only clinic they are the whole business.
        active = [b for b in (branches or []) if b.get("is_active", True)]

        # A service type chosen on the main menu rides along; _show_lab_test_list
        # falls back to the headings if that centre does not offer it.
        seed = {"lab_category": category} if category else {}

        if len(active) >= 2:
            await self._send_branch_selection(clinic, phone, active, lang)
            # A question asked before the centre was picked is answered
            # right after it, from that centre's catalogue.
            pending = {"lab_pending_query": query} if query else {}
            await self.update_state(
                clinic, phone, "selecting_branch", {"lab_flow": True, **seed, **pending},
                reset_context=True,
            )
            return

        context = dict(seed)
        if len(active) == 1:
            branch = active[0]
            context = self._set_branch_context(dict(seed), branch)

        await self.update_state(clinic, phone, "browsing_lab_tests", context, reset_context=True)
        await self._show_lab_test_list(clinic, phone, context, lang, query=query)

    async def _start_booking(
        self,
        clinic: dict,
        phone: str,
        patient: Optional[dict],
        lang: str,
        seed_context: Optional[dict] = None,
    ) -> None:
        """Start the booking flow — with optional branch selection for multi-branch clinics.

        seed_context: keys carried into the fresh booking context. Only the
        specialty treatment flow passes it ({"treatment_id", "treatment_name"});
        every existing caller passes nothing and gets the empty context it
        always had.
        """
        patient = patient or {}
        seed = dict(seed_context or {})

        # Guard: Language must be set before proceeding
        if not patient.get("language"):
            await self._send_language_selection(clinic, phone)
            await self.update_state(clinic, phone, "selecting_language")
            return

        # ── Diagnostics-Only Routing ─────────────────────────────────────────
        if await self._is_diagnostics_only(clinic):
            await self._start_lab_booking(clinic, phone, lang)
            return
        # ── End Diagnostics-Only Routing ─────────────────────────────────────

        # ── Multi-Branch Check ──────────────────────────────────────────────
        # If this clinic has 2+ branches with multi_branch feature,
        # show branch selection BEFORE proceeding to the booking flow.
        from app.services.tenant import get_clinic_branches, has_branches

        branches = await get_clinic_branches(clinic["id"])
        if has_branches(clinic, branches):
            # Filter out diagnostic-only branches for booking flow
            bookable_branches = [
                b for b in branches if not b.get("is_diagnostic", False)
            ]
            if len(bookable_branches) >= 2:
                await self._send_branch_selection(
                    clinic, phone, bookable_branches, lang
                )
                await self.update_state(clinic, phone, "selecting_branch", dict(seed), reset_context=True)
                return
            elif len(bookable_branches) == 1:
                # Only one bookable branch — auto-select it
                branch = bookable_branches[0]
                context = self._set_branch_context(dict(seed), branch)
                await self.update_state(clinic, phone, "selecting_family_member", context, reset_context=True)
                await self._continue_booking_after_branch(
                    clinic, phone, patient, lang, context
                )
                return
        # ── End Multi-Branch Check ──────────────────────────────────────────

        await self.update_state(clinic, phone, "selecting_family_member", dict(seed), reset_context=True)
        await self._continue_booking_after_branch(clinic, phone, patient, lang, dict(seed))

    async def _continue_booking_after_branch(
        self, clinic: dict, phone: str, patient: dict, lang: str, context: dict
    ) -> None:
        """Continue booking flow after branch is selected (or skipped for single-branch clinics)."""
        patient = patient or {}

        patient_name = patient.get("name") or "there"
        first_name = patient_name.split()[0] if patient_name != "there" else "there"

        msg_str = {
            "en": f"Who is this appointment for, {first_name}?",
            "hi": f"यह अपॉइंटमेंट किसके लिए है, {first_name}?",
            "te": f"ఈ అపాయింట్‌మెంట్ ఎవరి కోసం, {first_name}?",
        }.get(lang, f"Who is this appointment for, {first_name}?")

        saved_family = await get_family_members(clinic["id"], phone)
        if saved_family:
            rows = [
                {
                    "id": "fam_self",
                    "title": ("For Me" if lang == "en" else ("मेरे लिए" if lang == "hi" else "నా కోసం"))[:24],
                }
            ]
            for i, m in enumerate(saved_family):
                rows.append({"id": f"fam_{i}", "title": m["full_name"][:24]})
            rows.append(
                {
                    "id": "fam_new",
                    "title": ("+ Someone Else" if lang == "en" else ("+ अन्य व्यक्ति" if lang == "hi" else "+ వేరొకరు"))[:24],
                }
            )
            await self.whatsapp.send_interactive_list(
                clinic,
                phone,
                body=msg_str,
                button_text=(
                    "Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి")
                ),
                sections=[{"rows": rows}],
            )
            await self.update_state(
                clinic,
                phone,
                "selecting_family_member",
                {**context, "family_members": saved_family},
            )
            return

        # Check if returning patient with name and language is set
        if patient.get("name") and patient.get("language"):
            await self.whatsapp.send_interactive_buttons(
                clinic,
                phone,
                body=msg_str,
                buttons=[
                    {
                        "id": "for_self",
                        "title": (
                            "For Me"
                            if lang == "en"
                            else ("मेरे लिए" if lang == "hi" else "నా కోసం")
                        ),
                    },
                    {
                        "id": "for_family",
                        "title": (
                            "For Family"
                            if lang == "en"
                            else ("परिवार के लिए" if lang == "hi" else "కుటుంబం కోసం")
                        ),
                    },
                ],
            )
            await self.update_state(
                clinic,
                phone,
                "collecting_name",
                {**context, "asked_for_whom": True},
            )
        else:
            # New patient, ask for name
            await self.whatsapp.send_text(clinic, phone, get_message("ask_name", lang))
            await self.update_state(
                clinic,
                phone,
                "collecting_name",
                {**context, "for_self": True},
            )

    async def _send_doctor_branch_selection(
        self, clinic: dict, phone: str, doctor: dict, branches: list, lang: str
    ) -> None:
        """Send branch selection for a doctor who works at multiple branches."""
        msg = {
            "en": f"Dr. {doctor['name']} is available at multiple locations. Please select your preferred branch:",
            "hi": f"डॉ. {doctor['name']} कई स्थानों पर उपलब्ध हैं। कृपया अपनी पसंदीदा शाखा चुनें:",
            "te": f"డాక్టర్ {doctor['name']} అనేక స్థానాల్లో అందుబాటులో ఉన్నారు. దయచేసి మీ శాఖను ఎంచుకోండి:",
        }.get(lang, f"Dr. {doctor['name']} is available at multiple branches. Select one:")

        if len(branches) <= 3:
            buttons = []
            for b in branches[:3]:  # WhatsApp buttons max 3
                binfo = b.get("branches") or {}
                bname = binfo.get("short_name") or binfo.get("name", "Branch")
                sess = b.get("session", "both")
                sess_tag = " (AM)" if sess == "morning" else (" (PM)" if sess == "evening" else "")
                buttons.append(
                    {
                        "id": f"branch_{b['branch_id']}",
                        "title": f"{bname}{sess_tag}"[:20],
                    }
                )
            await self.whatsapp.send_interactive_buttons(
                clinic, phone, body=msg, buttons=buttons
            )
        else:
            rows = []
            # Full list: send_interactive_list caps at 10 and reports the
            # overflow, rather than us dropping branches 11+ in silence.
            for b in branches:
                binfo = b.get("branches") or {}
                bname = binfo.get("short_name") or binfo.get("name", "Branch")
                sess = b.get("session", "both")
                sess_label = "Morning" if sess == "morning" else ("Evening" if sess == "evening" else "Both Sessions")
                rows.append(
                    {
                        "id": f"branch_{b['branch_id']}",
                        "title": bname[:24],
                        "description": f"Hours: {sess_label}"[:72],
                    }
                )
            await self.whatsapp.send_interactive_list(
                clinic,
                phone=phone,
                header="Select Branch",
                body=msg,
                button_text="Select",
                sections=[{"title": "Branches", "rows": rows}],
            )

    async def _send_branch_selection(
        self, clinic: dict, phone: str, branches: list, lang: str, page: int = 0
    ) -> None:
        """Send interactive list of branches for multi-branch clinics.
        Title = locality (short_name) so patients see 'Madhurwada', not 'City Polyclinic'.
        Description = short address + landmark for extra context.
        """
        all_rows = []
        for branch in branches:
            # Title: locality name (short_name preferred, fallback to name)
            title = (branch.get("short_name") or branch["name"])[:24]

            # Description: combine address snippet + landmark
            desc_parts = []
            if branch.get("address"):
                desc_parts.append(branch["address"][:50])
            if branch.get("landmark"):
                desc_parts.append(f"Near {branch['landmark']}")
            description = ", ".join(desc_parts) if desc_parts else ""

            all_rows.append(
                {
                    "id": f"branch_{branch['id']}",
                    "title": title,
                    "description": description[:72],
                }
            )

        rows, page = self._page_rows(all_rows, page, "branch_more", lang)
        sections = [{"title": "Locations", "rows": rows}]

        header_text = {
            "en": "Select Location",
            "hi": "स्थान चुनें",
            "te": "స్థానాన్ని ఎంచుకోండి",
        }.get(lang, "Select Location")

        body_text = {
            "en": "Please choose your preferred clinic location:",
            "hi": "कृपया अपना पसंदीदा क्लिनिक स्थान चुनें:",
            "te": "దయచేసి మీ ప్రాధాన్యత గల క్లినిక్ స్థానాన్ని ఎంచుకోండి:",
        }.get(lang, "Please choose your preferred clinic location:")

        button_text = {
            "en": "Choose Location",
            "hi": "स्थान चुनें",
            "te": "స్థానాన్ని ఎంచుకోండి",
        }.get(lang, "Choose Location")

        await self.whatsapp.send_interactive_list(
            clinic,
            phone=phone,
            header=header_text,
            body=body_text,
            button_text=button_text,
            sections=sections,
        )

    async def _handle_selecting_branch(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        patient: dict,
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Handle branch selection from interactive list."""
        button_id = interactive_data.get("id", "") if interactive_data else ""

        # Before the branch_ prefix match: "branch_more" would be read as a
        # branch id of "more".
        if button_id == "branch_more":
            from app.services.tenant import get_clinic_branches

            branches = await get_clinic_branches(clinic["id"]) or []
            if context.get("lab_flow") or await self._is_diagnostics_only(clinic):
                offer = [b for b in branches if b.get("is_active", True)]
            else:
                offer = [b for b in branches if not b.get("is_diagnostic", False)]
            page = int(context.get("branch_page") or 0) + 1
            await self._send_branch_selection(clinic, phone, offer, lang, page=page)
            await self.update_state(
                clinic, phone, "selecting_branch", {**context, "branch_page": page}
            )
            return

        if button_id.startswith("branch_"):
            branch_id = button_id.replace("branch_", "")
            from app.services.tenant import get_branch_by_id

            branch = await get_branch_by_id(branch_id)

            if branch:
                # Store branch context for the entire booking flow atomically
                new_context = self._set_branch_context({**context}, branch)

                # Lab flow: the patient is choosing a collection centre, so show
                # that centre's catalogue. Checked BEFORE the is_diagnostic
                # redirect below -- for a diagnostics-only chain every branch is
                # diagnostic, and that redirect would bounce the patient to the
                # main menu instead of letting them book. _is_diagnostics_only
                # is re-checked rather than trusting the flag alone, so a
                # session that lost its context still lands correctly.
                if context.get("lab_flow") or await self._is_diagnostics_only(clinic):
                    new_context.pop("lab_flow", None)
                    await self._show_lab_test_list(
                        clinic, phone, new_context, lang,
                        query=new_context.pop("lab_pending_query", None),
                    )
                    return

                # If diagnostic-only branch, redirect to reports
                if branch.get("is_diagnostic", False):
                    await self.whatsapp.send_text(
                        clinic,
                        phone,
                        {
                            "en": f"📋 {branch['name']} is a diagnostics center. You can view your lab reports from the menu.",
                            "hi": f"📋 {branch['name']} एक डायग्नोस्टिक सेंटर है। आप मेनू से अपनी लैब रिपोर्ट देख सकते हैं।",
                            "te": f"📋 {branch['name']} డయాగ్నస్టిక్ సెంటర్. మీరు మెనూ నుండి మీ ల్యాబ్ రిపోర్ట్‌లు చూడవచ్చు.",
                        }.get(lang, f"📋 {branch['name']} is a diagnostics center."),
                    )
                    await self.update_state(clinic, phone, "main_menu", new_context)
                    await self._send_main_menu(clinic, phone, lang)
                    return

                # If doctor was pre-selected from Our Doctors, jump straight to date selection
                if new_context.get("doctor") or new_context.get("selected_doctor_id"):
                    await self._show_date_picker(clinic, phone, new_context, lang)
                    await self.update_state(clinic, phone, "selecting_date", new_context)
                    return

                # Continue with booking flow
                await self._continue_booking_after_branch(
                    clinic, phone, patient, lang, new_context
                )
                return

        # Invalid selection — resend the branch list
        from app.services.tenant import get_clinic_branches

        branches = await get_clinic_branches(clinic["id"])
        if context.get("lab_flow") or await self._is_diagnostics_only(clinic):
            # Filtering out diagnostic branches here would resend an EMPTY list
            # to a diagnostics-only chain and strand the patient.
            offer = [b for b in (branches or []) if b.get("is_active", True)]
        else:
            offer = [b for b in branches if not b.get("is_diagnostic", False)]
        await self._send_branch_selection(clinic, phone, offer, lang)

    async def _handle_selecting_family_member(
        self,
        clinic: dict,
        phone: str,
        message: str,
        context: dict,
        lang: str,
        patient: Optional[dict] = None,
    ) -> None:
        """Handle patient selection of which family member / self to book for."""
        msg_clean = message.strip().lower()
        family_members = context.get("family_members", [])

        # 1. Selected "For Self"
        if msg_clean in ["fam_self", "self", "for me", "me", "for myself", "myself"]:
            p_name = ((patient or {}).get("name") or "").strip()
            if not p_name:
                # No name on file: ask for it rather than booking as "there".
                await self.update_state(
                    clinic, phone, "collecting_name", {**context, "for_self": True, "is_family": False}
                )
                await self.whatsapp.send_text(clinic, phone, get_message("ask_name", lang))
                return
            new_ctx = {
                **context,
                "patient_name": p_name,
                "booking_name": p_name,
                "for_self": True,
                "is_family": False,
            }
            if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, new_ctx, lang):
                return
            await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
            await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
            return

        # 2. Selected "+ Someone Else / New"
        if msg_clean in ["fam_new", "new", "+ someone else", "+ new person", "someone else", "new person"]:
            await self.update_state(
                clinic, phone, "collecting_name", {**context, "is_family": True}
            )
            await self.whatsapp.send_text(clinic, phone, get_message("ask_name", lang))
            return

        # 3. Selected a numbered choice or fam_X button
        selected_idx = None
        if msg_clean.startswith("fam_") and msg_clean[4:].isdigit():
            selected_idx = int(msg_clean[4:])
        elif msg_clean.isdigit():
            # 1-indexed choice
            idx = int(msg_clean) - 1
            if 0 <= idx < len(family_members):
                selected_idx = idx

        if selected_idx is not None and 0 <= selected_idx < len(family_members):
            member = family_members[selected_idx]
            new_ctx = {
                **context,
                "patient_name": member["full_name"],
                "booking_name": member["full_name"],
                "relationship": member.get("relationship"),
                "is_family": True,
                "for_self": False,
            }
            if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, new_ctx, lang):
                return
            await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
            await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
            return

        # 4. Check if exact name was typed
        for m in family_members:
            if msg_clean == m["full_name"].lower():
                new_ctx = {
                    **context,
                    "patient_name": m["full_name"],
                    "booking_name": m["full_name"],
                    "relationship": m.get("relationship"),
                    "is_family": True,
                    "for_self": False,
                }
                if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, new_ctx, lang):
                    return
                await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
                await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
                return

        # Fallback: Treat typed input as new name if 2+ words, or prompt again.
        # Validated like the name prompt, now that the name may be saved.
        from app.utils.validators import validate_name

        name_ok, typed_name = validate_name(message) if len(msg_clean.split()) >= 2 else (False, "")
        if name_ok:
            new_ctx = {
                **context,
                "patient_name": typed_name,
                "booking_name": typed_name,
                "is_family": True,
                "for_self": False,
            }
            if await self._offer_save_family_member(clinic, phone, new_ctx, lang, typed_name):
                return
            await self._continue_after_patient_name(clinic, phone, new_ctx, lang)
        else:
            await self.whatsapp.send_text(
                clinic, phone, "Please select who this appointment is for or type their full name."
            )

    async def _handle_confirming_save_family_member(
        self,
        clinic: dict,
        phone: str,
        message: str,
        context: dict,
        lang: str,
    ) -> None:
        """Save (or not) a newly typed family member, then carry on booking.

        This state was never entered before: nothing offered the save, so the
        family list the booking flow reads was always empty. Either answer now
        continues to the next booking step instead of dropping the patient at
        the main menu mid-booking.
        """
        msg = message.strip().lower()
        name = context.get("pending_family_name")
        if not name:
            # A session parked here by nothing we know of: start clean.
            await self.update_state(clinic, phone, "main_menu", {"menu_shown": False})
            await self._send_main_menu(clinic, phone, lang)
            return

        if msg in ["save_family_yes", "yes", "y", "save", "हाँ", "हां", "అవును"]:
            await add_family_member(
                clinic["id"],
                phone,
                full_name=name,
                relationship=context.get("relationship"),
            )
            save_ack = {
                "en": f"Saved {name} to your family profiles for quick booking next time! 👍",
                "hi": f"{name} को अगली बार त्वरित बुकिंग के लिए आपकी प्रोफ़ाइल में सहेज लिया गया है! 👍",
                "te": f"{name} ను తదుపరి శీఘ్ర బుకింగ్ కోసం మీ ప్రొఫైల్‌లో సేవ్ చేసాము! 👍",
            }.get(lang, f"Saved {name} to your family profiles!")
            await self.whatsapp.send_text(clinic, phone, save_ack)
        elif msg not in ["save_family_no", "no", "n", "not now", "नहीं", "కాదు"]:
            await self._send_save_family_prompt(clinic, phone, name, lang)
            return

        context["pending_family_name"] = None
        await self._continue_after_patient_name(clinic, phone, context, lang)

    async def _send_save_family_prompt(self, clinic: dict, phone: str, name: str, lang: str) -> None:
        await self.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body={
                "en": f"Save *{name}* to your family list, so you can pick them next time?",
                "hi": f"क्या *{name}* को अपनी फैमिली सूची में सेव करें, ताकि अगली बार सीधे चुन सकें?",
                "te": f"*{name}* ను మీ కుటుంబ జాబితాలో సేవ్ చేయాలా? తదుపరి సారి నేరుగా ఎంచుకోవచ్చు.",
            }.get(lang, f"Save {name} to your family list?"),
            buttons=[
                {"id": "save_family_yes", "title": {
                    "en": "Save", "hi": "सेव करें", "te": "సేవ్ చేయండి",
                }.get(lang, "Save")},
                {"id": "save_family_no", "title": {
                    "en": "Not now", "hi": "अभी नहीं", "te": "ఇప్పుడు వద్దు",
                }.get(lang, "Not now")},
            ],
        )

    async def _offer_save_family_member(
        self, clinic: dict, phone: str, context: dict, lang: str, name: str
    ) -> bool:
        """Ask to keep a newly typed name. False when it is already saved."""
        saved = context.get("family_members")
        if saved is None:
            saved = await get_family_members(clinic["id"], phone)
        if name.casefold() in {(m.get("full_name") or "").strip().casefold() for m in saved}:
            return False
        context["pending_family_name"] = name
        await self._send_save_family_prompt(clinic, phone, name, lang)
        await self.update_state(clinic, phone, "confirming_save_family_member", context)
        return True

    async def _continue_after_patient_name(
        self, clinic: dict, phone: str, context: dict, lang: str
    ) -> None:
        """The step after "who is this for": the treatment's doctors, or symptoms."""
        # Treatment bookings skip symptoms: the patient already chose the treatment.
        if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, context, lang):
            return
        await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
        await self.update_state(clinic, phone, "collecting_symptoms", context)

    async def _handle_collecting_name(
        self,
        clinic: dict,
        phone: str,
        message: str,
        context: dict,
        patient: dict,
        lang: str,
    ) -> None:
        """Handle name collection."""

        # Skip validation for button responses
        if message.lower() in [
            "self",
            "for me",
            "family",
            "for family",
            "for_self",
            "for_family",
            "మెరే లిఏ",
            "నా కోసం",
            "కుటుంబం కోసం",
            "मेरे लिए",
            "परिवार के लिए",
        ]:
            # These are handled by button handlers above, ignore here
            return

        # Handle button responses
        if message.lower() in ["self", "for me", "मेरे लिए", "నా కోసం"]:
            context["for_self"] = True
            context["booking_name"] = patient.get("name")
            if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, context, lang):
                return
            await self.whatsapp.send_text(
                clinic, phone, get_message("ask_symptoms", lang)
            )
            await self.update_state(clinic, phone, "collecting_symptoms", context)
            return

        if message.lower() in ["family", "for family", "परिवार के लिए", "కుటుంబం కోసం"]:
            context["for_self"] = False
            await self.whatsapp.send_text(clinic, phone, get_message("ask_name", lang))
            await self.update_state(clinic, phone, "collecting_name", context)
            return

        # Validate name
        from app.utils.validators import validate_name

        is_valid, result = validate_name(message)
        if not is_valid:
            await self._send_name_error(clinic, phone, result, lang)
            return

        name = result
        context["booking_name"] = name

        # Save to patient record if for self. "+ Someone Else" sets is_family
        # without for_self, and the old default-True check overwrote the
        # account holder's own name with the family member's.
        if context.get("for_self", True) and not context.get("is_family"):
            await update_patient(clinic["id"], phone, {"name": name})
        else:
            context["patient_name"] = name
            if await self._offer_save_family_member(clinic, phone, context, lang, name):
                return

        await self._continue_after_patient_name(clinic, phone, context, lang)

    async def _send_name_error(self, clinic: dict, phone: str, result: str, lang: str) -> None:
        """Why a typed name was refused (validate_name's reason code)."""
        if result == "need_full_name":
            msg = {
                "en": "Please share both first and last name. \nExample: Chaitanya Kumar",
                "hi": "कृपया अपना पूरा नाम बताएं। \nउदाहरण: चैतन्य कुमार",
                "te": "దయచేసి మీ పూర్తి పేరు చెప్పండి. \nఉదా: చైతన్య కుమార్",
            }.get(
                lang,
                "Please share both first and last name. \nExample: Chaitanya Kumar",
            )
            await self.whatsapp.send_text(clinic, phone, msg)
        else:
            errors = {
                "en": {
                    "too_short": "Name is too short. Please share your full name.",
                    "invalid_chars": "Name should contain only letters.",
                    "invalid_name": "That doesn't look like a name. \nPlease share the patient's full name.",
                },
                "hi": {
                    "too_short": "नाम बहुत छोटा है। कृपया अपना पूरा नाम बताएं।",
                    "invalid_chars": "नाम में केवल अक्षर होने चाहिए।",
                    "invalid_name": "यह नाम जैसा नहीं लगता। \nकृपया मरीज़ का पूरा नाम बताएं।",
                },
                "te": {
                    "too_short": "పేరు చాలా చిన్నది. దయచేసి మీ పూర్తి పేరు చెప్పండి.",
                    "invalid_chars": "పేరులో అక్షరాలు మాత్రమే ఉండాలి.",
                    "invalid_name": "ఇది పేరులా అనిపించడం లేదు. \nదయచేసి రోగి పూర్తి పేరును పంచుకోండి.",
                },
            }
            lang_errors = errors.get(lang, errors["en"])
            error_msg = lang_errors.get(
                result, errors["en"].get(result, "Please enter a valid full name.")
            )
            await self.whatsapp.send_text(clinic, phone, error_msg)

    async def _handle_collecting_symptoms(
        self,
        clinic: dict,
        phone: str,
        message: str,
        context: dict,
        patient: dict,
        lang: str,
    ) -> None:
        """Handle symptom collection."""

        last_symptom = context.get("last_symptom")
        if last_symptom == message.lower().strip():
            return  # same message, ignore
        context["last_symptom"] = message.lower().strip()
        await update_conversation(clinic["id"], phone, {"context": context})

        # Allow skip
        if message.lower() in [
            "skip",
            "no symptoms",
            "don't know",
            "none",
            "नहीं",
            "తెలియదు",
        ]:
            # Show department list directly
            await self._show_department_list(clinic, phone, context, lang)
            return

        # Check if emergency FIRST
        msg_lower = message.lower().strip()
        is_emergency = any(kw in msg_lower for kw in EMERGENCY_KEYWORDS)
        if is_emergency:
            await self._handle_emergency(clinic, phone, lang)
            return

        # Symptom follow-up questions
        if (
            "chest pain" in msg_lower
            and context.get("symptom_followup") != "chest_pain"
        ):
            context["symptom_followup"] = "chest_pain"
            await self.whatsapp.send_interactive_buttons(
                clinic,
                phone,
                body="Is the chest pain sudden and severe, or mild and ongoing?",
                buttons=[
                    {"id": "chest_severe", "title": "Sudden & Severe"},
                    {"id": "chest_mild", "title": "Mild & Ongoing"},
                ],
            )
            await update_conversation(clinic["id"], phone, {"context": context})
            return

        if "back pain" in msg_lower and context.get("symptom_followup") != "back_pain":
            context["symptom_followup"] = "back_pain"
            await self.whatsapp.send_interactive_buttons(
                clinic,
                phone,
                body="Is it lower back pain or upper back/neck pain?",
                buttons=[
                    {"id": "back_lower", "title": "Lower Back"},
                    {"id": "back_upper", "title": "Upper/Neck"},
                ],
            )
            await update_conversation(clinic["id"], phone, {"context": context})
            return

        # Map symptoms to department
        symptom_result = await map_symptom_to_department(message, clinic)

        if symptom_result.get("suggested_department") is None:
            await self.whatsapp.send_text(
                clinic,
                phone,
                {
                    "en": "I didn't understand that. Please describe your symptoms.\nExample: fever, chest pain, tooth pain",
                    "hi": "मुझे समझ नहीं आया। अपने लक्षण बताएं।\nउदाहरण: बुखार, सीने में दर्द, दांत दर्द",
                    "te": "అర్థం కాలేదు. మీ లక్షణాలు వివరించండి.\nఉదా: జ్వరం, గుండె నొప్పి, పళ్ళు నొప్పి",
                }.get(lang, "Please describe your symptoms."),
            )
            return

        # Store suggestion in context
        context["suggested_department"] = symptom_result["suggested_department"]
        context["symptoms"] = message
        context["suggestion_reasoning"] = symptom_result["reasoning"]

        # Show suggestion
        dept_name = symptom_result["suggested_department"]
        suggestion_body = {
            "en": f"Based on your concern, our *{dept_name}* team may be able to help. Shall I book an appointment there?",
            "hi": f"आपकी चिंता के आधार पर, हमारी *{dept_name}* टीम मदद कर सकती है। क्या मैं वहां अपॉइंटमेंट बुक करूं?",
            "te": f"మీ ఆందోళన ఆధారంగా, మా *{dept_name}* బృందం సహాయం చేయగలదు. అక్కడ అపాయింట్‌మెంట్ బుక్ చేయమంటారా?",
        }.get(lang, f"Based on your concern, our *{dept_name}* team may be able to help. Shall I book an appointment there?")

        await self.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=suggestion_body,
            buttons=[
                {
                    "id": "suggest_yes",
                    "title": (
                        "Yes" if lang == "en" else ("हाँ" if lang == "hi" else "అవును")
                    ),
                },
                {
                    "id": "suggest_no",
                    "title": (
                        "No" if lang == "en" else ("नहीं" if lang == "hi" else "కాదు")
                    ),
                },
            ],
        )

        await self.update_state(clinic, phone, "suggesting_department", context)

    async def _handle_suggesting_department(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Handle department suggestion response."""
        button_id = interactive_data.get("id") if interactive_data else None
        msg_lower = message.lower().strip()

        is_yes = (
            button_id in ["yes", "suggest_yes"]
            or intent in ["accept_suggestion", "yes"]
            or msg_lower in ["yes", "అవును", "हाँ", "ha", "y", "हां"]
        )

        if is_yes:
            department = context.get("suggested_department")
            # Step 2: Query database directly
            from app.database import supabase

            response = (
                await sb(supabase.table("doctors")
                .select("*")
                .eq("clinic_id", clinic["id"])
                .eq("department", department)
                .eq("is_active", True)
                .order("rating", desc=True))
            )
            doctors = response.data

            if doctors:
                logger.info(f"Doctors found: {len(doctors)}")

                # Step 3: Build WhatsApp LIST message
                sections = [
                    {
                        "title": department,
                        "rows": [
                            {
                                "id": f"doc_{doc['id']}",
                                "title": doc["name"][:24],
                                "description": f"{doc['specialization']} · ⭐{doc.get('rating', '4.5')} · ₹{doc['consultation_fee']}"[
                                    :72
                                ],
                            }
                            for doc in doctors
                        ],
                    }
                ]

                await self.whatsapp.send_interactive_list(
                    clinic,
                    phone=phone,
                    header={
                        "en": "Choose Your Doctor",
                        "hi": "अपना डॉक्टर चुनें",
                        "te": "మీ డాక్టర్‌ను ఎంచుకోండి",
                    }.get(lang, "Choose Your Doctor"),
                    body=get_message("available_doctors_in", lang, dept=department),
                    button_text={
                        "en": "Select Doctor",
                        "hi": "डॉक्टर चुनें",
                        "te": "డాక్టర్‌ ఎంచుకోండి",
                    }.get(lang, "Select Doctor"),
                    sections=sections,
                )

                context_update = {
                    "suggested_department": department,
                    "symptoms": context.get("symptoms"),
                    "department": department,
                }
                await self.update_state(
                    clinic, phone, "selecting_doctor", context_update
                )
            else:
                # Step 4: No doctors found
                await self.whatsapp.send_text(
                    clinic, phone, f"No doctors available in {department} right now."
                )
                await self._show_department_list(clinic, phone, context, lang)
        else:
            # Show all departments
            await self._show_department_list(clinic, phone, context, lang)

    #: WhatsApp caps an interactive list at 10 rows. Nine leaves room for the
    #: "More options" row that makes item 11 onward reachable at all.
    LIST_PAGE_SIZE = 9

    def _page_rows(
        self,
        rows: list[dict],
        page: int,
        more_id: str,
        lang: str,
        page_size: Optional[int] = None,
    ) -> tuple[list[dict], int]:
        """Return one page of interactive-list rows, plus the page actually used.

        Every list builder used to hand its full result set to
        send_interactive_list and let it truncate at 10. The clinic could add an
        11th doctor, or import a 200-test catalogue from the admin panel, and
        no patient could ever select any of it -- send_interactive_list logs
        "ALERT list_truncated ... need pagination" and drops the rest silently.

        A list that already fits is returned untouched, so short catalogues keep
        showing all 10 rows and gain no extra tap.
        """
        # `page_size` exists for callers that append their own navigation rows
        # (Main Menu, All categories) after paging: they shrink the content
        # page by exactly that many rows so the total still lands inside Meta's
        # 10-row cap instead of being silently truncated there.
        size = page_size or self.LIST_PAGE_SIZE
        if page <= 0 and len(rows) <= size + 1:
            return rows, 0

        start = max(0, page) * size
        if start >= len(rows):  # ran past the end; restart from the beginning
            start, page = 0, 0

        page_rows = list(rows[start : start + size])
        remaining = len(rows) - (start + len(page_rows))
        if remaining > 0:
            page_rows.append(
                {
                    "id": more_id,
                    "title": {
                        "en": "More options",
                        "hi": "और विकल्प",
                        "te": "మరిన్ని ఎంపికలు",
                    }.get(lang, "More options"),
                    "description": {
                        "en": f"{remaining} more to choose from",
                        "hi": f"{remaining} और विकल्प",
                        "te": f"{remaining} మరిన్ని",
                    }.get(lang, f"{remaining} more"),
                }
            )
        return page_rows, max(0, page)

    async def _show_department_list(
        self, clinic: dict, phone: str, context: dict, lang: str, page: int = 0
    ) -> None:
        """Show list of departments dynamically derived from active doctors."""
        from app.services.tenant import has_feature

        branch_id = context.get("branch_id")
        from app.database import supabase

        if branch_id:
            # Get departments from active doctors assigned to this branch
            from app.database import get_doctors_at_branch

            branch_doctors = await get_doctors_at_branch(clinic["id"], branch_id)
            dept_names = sorted(list(set(d["department"] for d in branch_doctors if d.get("is_active", True) and d.get("department"))))
        else:
            result = (
                await sb(supabase.table("doctors")
                .select("department")
                .eq("clinic_id", clinic["id"])
                .eq("is_active", True))
            )
            dept_names = sorted(list(set(r["department"] for r in (result.data or []) if r.get("department"))))

        # Q1: If no active doctors exist, do NOT fall back to General Medicine. Show clear message.
        if not dept_names:
            no_svc_msg = {
                "en": "No medical services or doctors are currently available for booking at this clinic. Please call us directly.",
                "hi": "इस क्लिनिक में अभी बुकिंग के लिए कोई सेवा या डॉक्टर उपलब्ध नहीं है। कृपया सीधे हमें कॉल करें।",
                "te": "ఈ క్లినిక్‌లో ప్రస్తుతం బుకింగ్ కోసం సేవలు లేదా డాక్టర్లు అందుబాటులో లేరు. దయచేసి నేరుగా కాల్ చేయండి.",
            }.get(lang, "No medical services or doctors are currently available for booking.")
            await self.whatsapp.send_text(clinic, phone, no_svc_msg)
            await self._send_main_menu(clinic, phone, lang)
            return

        all_rows = []
        dept_options = {}
        for d in dept_names:
            dept_id = f"dept_{d.lower().replace(' ', '_')}"
            all_rows.append({"id": dept_id, "title": d[:24], "description": ""})
            # The map holds EVERY department, not just this page, so a pick
            # from page 2 still resolves.
            dept_options[dept_id] = d

        rows, page = self._page_rows(all_rows, page, "dept_more", lang)
        sections = [{"title": "Departments", "rows": rows}]

        msg = {
            "en": "Please choose a department / service:",
            "hi": "कृपया विभाग / सेवा चुनें:",
            "te": "దయచేసి విభాగం / సేవను ఎంచుకోండి:",
        }.get(lang, "Choose Department")

        await self.whatsapp.send_interactive_list(
            clinic,
            phone=phone,
            header={"en": "Our Services", "hi": "हमारी सेवाएँ", "te": "మా సేవలు"}.get(lang, "Our Services"),
            body=msg,
            button_text={"en": "Select", "hi": "चुनें", "te": "ఎంచుకోండి"}.get(lang, "Select"),
            sections=sections,
        )

        merged_context = {**context, "dept_options": dept_options, "dept_page": page}
        await self.update_state(clinic, phone, "selecting_department", merged_context)

    async def _handle_selecting_department(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Handle department selection with support for dynamic options and legacy svc_* fallback."""
        button_id = interactive_data.get("id", "") if interactive_data else ""

        # Must be checked before the dept_ prefix match below: "dept_more" is
        # not a department, and falling through would re-show the same page
        # forever.
        if button_id == "dept_more":
            await self._show_department_list(
                clinic, phone, context, lang, page=int(context.get("dept_page") or 0) + 1
            )
            return

        # Legacy mapping retained for backward compatibility (OQ-2)
        LEGACY_SVC_MAP = {
            "svc_general": "General Medicine",
            "svc_cardiology": "Cardiology",
            "svc_dental": "Dental",
            "svc_ortho": "Orthopedics",
            "svc_gynec": "Gynecology",
            "svc_pediatrics": "Pediatrics",
            "svc_ent": "ENT",
            "svc_derma": "Dermatology",
        }

        department = None
        if button_id.startswith("dept_"):
            dept_options = context.get("dept_options") or {}
            department = dept_options.get(button_id)
        elif button_id.startswith("svc_"):
            department = LEGACY_SVC_MAP.get(button_id)

        if not department:
            # Check text match against active clinic departments
            from app.database import supabase

            result = (
                await sb(supabase.table("doctors")
                .select("department")
                .eq("clinic_id", clinic["id"])
                .eq("is_active", True))
            )
            clinic_depts = list(set(r["department"] for r in (result.data or []) if r.get("department")))
            msg_clean = message.strip().lower()
            for dept in clinic_depts:
                if dept.lower() in msg_clean or msg_clean in dept.lower():
                    department = dept
                    break

        if department:
            # Fetch active doctors for selected department
            from app.database import supabase

            branch_id = context.get("branch_id")
            if branch_id:
                from app.database import get_doctors_at_branch

                doctors = await get_doctors_at_branch(clinic["id"], branch_id, department=department, active_only=True)
            else:
                response = (
                    await sb(supabase.table("doctors")
                    .select("*")
                    .eq("clinic_id", clinic["id"])
                    .eq("department", department)
                    .eq("is_active", True)
                    .order("rating", desc=True))
                )
                doctors = response.data or []

            if doctors:
                await self._show_doctor_list(clinic, phone, department, context, lang)
            else:
                await self.whatsapp.send_text(
                    clinic, phone, f"No doctors available in {department} right now."
                )
                await self._show_department_list(clinic, phone, context, lang)
        else:
            # Re-show department list if invalid
            await self._show_department_list(clinic, phone, context, lang)

    async def _show_doctor_list(
        self,
        clinic: dict,
        phone: str,
        department: str,
        context: dict,
        lang: str,
        page: int = 0,
    ) -> None:
        """Show list of doctors in a department (branch-filtered when branch_id in context)."""
        branch_id = context.get("branch_id")
        doctors = await get_doctors(clinic["id"], department, branch_id=branch_id)

        if not doctors:
            branch_name = context.get("branch_name", "")
            if branch_name:
                no_doc_msg = {
                    "en": f"Sorry, no doctors are available in {department} at {branch_name}. Please try another department.",
                    "hi": f"क्षमा करें, {branch_name} में {department} में कोई डॉक्टर उपलब्ध नहीं है। कृपया अन्य विभाग आज़माएं।",
                    "te": f"క్షమించండి, {branch_name} లో {department} లో డాక్టర్లు అందుబాటులో లేరు. దయచేసి మరొక విభాగం ప్రయత్నించండి.",
                }.get(lang, f"Sorry, no doctors are available in {department} at {branch_name}.")
            else:
                no_doc_msg = f"Sorry, no doctors are currently available in {department}. Please try another department."
            await self.whatsapp.send_text(clinic, phone, no_doc_msg)

            from app.services.tenant import has_feature

            if has_feature(clinic, "multi_department"):
                await self._show_department_list(clinic, phone, context, lang)
            else:
                await self._send_main_menu(clinic, phone, lang)
            return

        all_rows = [
            {
                "id": f"doc_{doc['id']}",
                "title": doc["name"][:24],
                "description": f"{doc['specialization']} · ⭐{doc.get('rating', '4.5')} · ₹{doc['consultation_fee']}"[
                    :72
                ],
            }
            for doc in doctors
        ]
        rows, page = self._page_rows(all_rows, page, "doc_more", lang)
        sections = [{"title": department[:24], "rows": rows}]

        await self.whatsapp.send_interactive_list(
            clinic,
            phone=phone,
            header={
                "en": "Choose Your Doctor",
                "hi": "अपना डॉक्टर चुनें",
                "te": "మీ డాక్టర్‌ను ఎంచుకోండి",
            }.get(lang, "Choose Your Doctor"),
            body=get_message("available_doctors_in", lang, dept=department),
            button_text={
                "en": "Select Doctor",
                "hi": "डॉक्टर चुनें",
                "te": "డాక్టర్‌ ఎంచుకోండి",
            }.get(lang, "Select Doctor"),
            sections=sections,
        )

        context["department"] = department
        merged_context = {**context, "doctor_page": page}
        await self.update_state(clinic, phone, "selecting_doctor", merged_context)

    async def _handle_selecting_doctor(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Handle doctor selection."""

        button_id = interactive_data.get("id", "") if interactive_data else ""

        # Before the doc_ prefix match: "doc_more" would otherwise be parsed as
        # a doctor id of "more" and sent to the database as a UUID.
        if button_id == "doc_more":
            if context.get("treatment_id"):
                await specialty_flow.show_treatment_doctors(
                    self, clinic, phone, context, lang,
                    page=int(context.get("doctor_page") or 0) + 1,
                )
                return
            await self._show_doctor_list(
                clinic,
                phone,
                context.get("department") or "",
                context,
                lang,
                page=int(context.get("doctor_page") or 0) + 1,
            )
            return

        doctor_id = None
        if button_id.startswith("doc_"):
            doctor_id = button_id.replace("doc_", "")

        if doctor_id:
            from app.database import supabase

            res = (
                await sb(supabase.table("doctors")
                .select("*")
                .eq("clinic_id", clinic["id"])
                .eq("id", doctor_id))
            )
            doctor = res.data[0] if res.data else None
            doctor_name = doctor["name"] if doctor else message.strip()
        else:
            msg = message.lower().strip()

            # Dynamic check if input matches an active clinic department
            from app.database import supabase

            dept_res = (
                await sb(supabase.table("doctors")
                .select("department")
                .eq("clinic_id", clinic["id"])
                .eq("is_active", True))
            )
            active_depts = list(set(r["department"] for r in (dept_res.data or []) if r.get("department")))

            matched_dept = None
            for dept in active_depts:
                if dept.lower() in msg or msg in dept.lower():
                    matched_dept = dept
                    break

            if matched_dept:
                # Patient is telling us which department they want
                response = (
                    await sb(supabase.table("doctors")
                    .select("*")
                    .eq("clinic_id", clinic["id"])
                    .eq("department", matched_dept)
                    .eq("is_active", True)
                    .order("rating", desc=True))
                )
                doctors = response.data
                if doctors:
                    await self._show_doctor_list(
                        clinic, phone, matched_dept, context, lang
                    )
                else:
                    await self.whatsapp.send_text(
                        clinic,
                        phone,
                        f"No doctors available in {matched_dept} right now.",
                    )
                    await self._show_department_list(clinic, phone, context, lang)
                return

            # If no department match, try to match doctor name
            response = (
                await sb(supabase.table("doctors")
                .select("*")
                .eq("clinic_id", clinic["id"])
                .eq("is_active", True))
            )
            all_doctors = response.data or []
            matched_doc = None
            for doc in all_doctors:
                if doc["name"].lower() in msg or msg in doc["name"].lower():
                    matched_doc = doc
                    break

            if matched_doc:
                doctor = matched_doc
                doctor_name = doctor["name"]
            else:
                doctor = None
                doctor_name = ""

        if not doctor:
            # Implement Fallback: resend the list instead of just an error text
            fallback_msg = {
                "en": "Please select from the list below:",
                "hi": "कृपया नीचे दी गई सूची से चुनें:",
                "te": "దయచేసి దిగువ జాబితా నుండి ఎంచుకోండి:",
            }.get(lang, "Please select from the list below:")

            await self.whatsapp.send_text(clinic, phone, fallback_msg)
            if context.get("treatment_id"):
                await specialty_flow.show_treatment_doctors(self, clinic, phone, context, lang)
                return
            if context.get("department"):
                await self._show_doctor_list(
                    clinic, phone, context["department"], context, lang
                )
            else:
                await self._show_department_list(clinic, phone, context, lang)
            return

        context["doctor_name"] = doctor_name
        context["doctor"] = doctor
        if context.get("treatment_id") and isinstance(doctor, dict) and doctor.get("department"):
            # The treatment flow skipped department selection; the booking's
            # department (analytics, confirmations) is the specialist's own.
            context["department"] = doctor["department"]
        if isinstance(doctor, dict) and doctor.get("id"):
            context["doctor_id"] = doctor["id"]
            context["selected_doctor_id"] = doctor["id"]

            # If doctor has assigned branch(es), validate or auto-attach
            try:
                from app.database import supabase
                d_branch_res = (
                    await sb(supabase.table("doctor_branches")
                    .select("branch_id, session, branches(id, name, short_name, address, landmark, maps_link)")
                    .eq("doctor_id", doctor["id"]))
                )
                d_branches = d_branch_res.data or []
                d_branch_ids = {b["branch_id"] for b in d_branches if b.get("branch_id")}
                current_bid = context.get("branch_id")

                if len(d_branches) == 1 and d_branches[0].get("branch_id"):
                    # Auto-attach the single branch for this doctor
                    binfo = dict(d_branches[0].get("branches") or {})
                    binfo["id"] = binfo.get("id") or d_branches[0]["branch_id"]
                    self._set_branch_context(
                        context, binfo, session_val=d_branches[0].get("session", "both")
                    )
                elif len(d_branches) > 1 and (not current_bid or current_bid not in d_branch_ids):
                    await self._send_doctor_branch_selection(clinic, phone, doctor, d_branches, lang)
                    await self.update_state(clinic, phone, "selecting_branch", context)
                    return
                elif len(d_branches) > 1 and current_bid in d_branch_ids:
                    match = next((b for b in d_branches if b["branch_id"] == current_bid), None)
                    if match:
                        binfo = dict(match.get("branches") or {})
                        binfo["id"] = binfo.get("id") or match["branch_id"]
                        self._set_branch_context(
                            context, binfo, session_val=match.get("session", "both")
                        )
            except Exception as e:
                logger.warning(f"Failed to check doctor branch assignment: {e}")

        # Ask for date — two-step flow: date picker → slot list
        merged_context = {**context}

        await self._show_date_picker(clinic, phone, merged_context, lang)
        await self.update_state(clinic, phone, "selecting_date", merged_context)

    async def _handle_selecting_date(
        self, clinic: dict, phone: str, message: str, context: dict, lang: str
    ) -> None:
        """Handle date selection."""
        from datetime import datetime, timedelta, timezone as tz

        ist = tz(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(ist)

        # Parse date from message
        date_str = None
        msg_lower = message.lower().strip()

        if msg_lower in ["today", "आज", "ఈరోజు"]:
            date_str = now_ist.strftime("%Y-%m-%d")
        elif msg_lower in ["tomorrow", "कल", "రేపు"]:
            date_str = (now_ist + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            # Try to parse date formats
            for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%B %d", "%d %B"]:
                try:
                    parsed = datetime.strptime(message.strip(), fmt)
                    if parsed.year == 1900:
                        parsed = parsed.replace(year=datetime.now().year)
                    date_str = parsed.strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue

        if not date_str:
            await self.whatsapp.send_text(
                clinic,
                phone,
                "Please provide a valid date (e.g., 'today', 'tomorrow', or '2026-03-20').",
            )
            return

        # Validate date is not in past
        selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        if selected_date < now_ist.date():
            await self.whatsapp.send_text(clinic, phone, "Please choose a future date.")
            return

        # Check if date is within 30 days
        if selected_date > now_ist.date() + timedelta(days=30):
            await self.whatsapp.send_text(
                clinic, phone, "Please choose a date within the next 30 days."
            )
            return

        context["appointment_date"] = date_str

        # Get available slots respecting branch and session
        slots, reason = await get_available_slots(
            clinic["id"],
            context["doctor_name"],
            date_str,
            branch_id=context.get("branch_id"),
            branch_session=context.get("branch_session"),
        )

        if not slots:
            date_display = selected_date.strftime("%d %b")

            # Inform the patient why the doctor is unavailable
            if reason == "doctor_on_leave":
                msg = {
                    "en": f"Dr. {context['doctor_name']} is on leave on {date_display}.",
                    "hi": f"डॉ. {context['doctor_name']} {date_display} को छुट्टी पर हैं।",
                    "te": f"డాక్టర్ {context['doctor_name']} {date_display} న సెలవులో ఉన్నారు.",
                }.get(
                    lang, f"Dr. {context['doctor_name']} is on leave on {date_display}."
                )
                await self.whatsapp.send_text(clinic, phone, msg)
            elif reason == "hospital_closed":
                msg = {
                    "en": f"The hospital is closed on {date_display} for a holiday.",
                    "hi": f"अस्पताल {date_display} को छुट्टी के कारण बंद है।",
                    "te": f"ఆసుపత్రి {date_display} న సెలవు కారణంగా మూసివేయబడింది.",
                }.get(lang, f"The hospital is closed on {date_display} for a holiday.")
                await self.whatsapp.send_text(clinic, phone, msg)
            elif reason == "doctor_off_day":
                msg = {
                    "en": f"Dr. {context['doctor_name']} does not consult on this day of the week.",
                    "hi": f"डॉ. {context['doctor_name']} सप्ताह के इस दिन परामर्श नहीं देते हैं।",
                    "te": f"డా. {context['doctor_name']} వారంలో ఈ రోజున సంప్రదింపులు చేయరు.",
                }.get(
                    lang,
                    f"Dr. {context['doctor_name']} does not work on this day of the week.",
                )
                await self.whatsapp.send_text(clinic, phone, msg)

            # Find next available date
            next_date, next_slots, next_reason = await find_next_available_date(
                clinic["id"],
                context["doctor_name"],
                (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime(
                    "%Y-%m-%d"
                ),
                branch_id=context.get("branch_id"),
                branch_session=context.get("branch_session"),
            )

            if next_reason == "no_availability_14_days" or not next_date:
                # Doctor fully booked or unavailable for long time, suggest others
                await self._suggest_other_doctors(clinic, phone, context, lang)
                return

            next_date_display = datetime.strptime(next_date, "%Y-%m-%d").strftime(
                "%d %b"
            )
            msg = {
                "en": f"Next available date for {context['doctor_name']} is {next_date_display}.",
                "hi": f"{context['doctor_name']} के लिए अगली उपलब्ध तारीख {next_date_display} है।",
                "te": f"{context['doctor_name']} కోసం తదుపరి అందుబాటులో ఉన్న తేదీ {next_date_display}.",
            }.get(lang, f"Next available date is {next_date_display}.")
            await self.whatsapp.send_text(clinic, phone, msg)

            context["appointment_date"] = next_date
            slots = next_slots

        # Show slots
        await self._show_slot_list(clinic, phone, slots, context, lang)

    async def _show_date_picker(
        self, clinic: dict, phone: str, context: dict, lang: str
    ) -> None:
        """Show a date picker with up to 7 available dates.

        Uses parallel availability scanning (asyncio.gather) across 7 days
        for ~300-500ms wall-clock latency instead of 2-3s serial.
        Falls back to find_next_available_date() if zero availability in 7 days.
        """
        import asyncio
        from datetime import datetime, timedelta, timezone as tz

        ist = tz(timedelta(hours=5, minutes=30))
        today = datetime.now(ist).date()

        day_labels = {
            "en": ["Today", "Tomorrow"],
            "hi": ["आज", "कल"],
            "te": ["ఈరోజు", "రేపు"],
        }
        labels = day_labels.get(lang, day_labels["en"])

        # Parallel availability scan for 7 days
        candidates = [today + timedelta(days=i) for i in range(7)]
        results = await asyncio.gather(*[
            get_available_slots(
                clinic["id"],
                context["doctor_name"],
                d.strftime("%Y-%m-%d"),
                branch_id=context.get("branch_id"),
                branch_session=context.get("branch_session"),
            )
            for d in candidates
        ])

        date_rows = []
        for i, (slots, _reason) in enumerate(results):
            if not slots:
                continue
            d = candidates[i]
            date_str = d.strftime("%Y-%m-%d")
            slot_count = len(slots)

            if i == 0:
                title = f"{labels[0]} ({d.strftime('%d %b')})"
            elif i == 1:
                title = f"{labels[1]} ({d.strftime('%d %b')})"
            else:
                title = d.strftime("%A, %d %b")

            desc = f"{slot_count} {'slot' if slot_count == 1 else 'slots'} available"
            date_rows.append(
                {"id": f"date_{date_str}", "title": title[:24], "description": desc[:72]}
            )

        # If no availability in 7 days, try extended search
        if not date_rows:
            next_date, next_slots, next_reason = await find_next_available_date(
                clinic["id"],
                context["doctor_name"],
                (today + timedelta(days=7)).strftime("%Y-%m-%d"),
                branch_id=context.get("branch_id"),
                branch_session=context.get("branch_session"),
            )
            if next_date and next_slots:
                d = datetime.strptime(next_date, "%Y-%m-%d").date()
                slot_count = len(next_slots)
                title = d.strftime("%A, %d %b")
                desc = f"{slot_count} {'slot' if slot_count == 1 else 'slots'} available"
                date_rows.append(
                    {"id": f"date_{next_date}", "title": title[:24], "description": desc[:72]}
                )
            else:
                # No availability at all — suggest other doctors
                await self._suggest_other_doctors(clinic, phone, context, lang)
                return

        sections = [
            {
                "title": (
                    "Select Date"
                    if lang == "en"
                    else ("तारीख चुनें" if lang == "hi" else "తేదీ ఎంచుకోండి")
                ),
                "rows": date_rows[:7],  # WhatsApp max 10 rows; 7 dates is safe
            }
        ]

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body=get_message("select_date", lang),
            button_text=(
                "Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి")
            ),
            sections=sections,
        )

    async def _show_combined_slot_picker(
        self, clinic: dict, phone: str, context: dict, lang: str
    ) -> None:
        """Show date+time as ONE interactive list instead of two separate
        messages — merges what used to be _show_date_picker followed by
        _show_slot_list into a single patient tap."""
        today = datetime.now().date()

        day_labels = {
            "en": ["Today", "Tomorrow"],
            "hi": ["आज", "कल"],
            "te": ["ఈరోజు", "రేపు"],
        }
        labels = day_labels.get(lang, day_labels["en"])

        sections = []
        rows_used = 0
        days_with_slots = 0
        MAX_ROWS = 10
        MAX_DAYS = 4

        for i in range(14):
            if rows_used >= MAX_ROWS or days_with_slots >= MAX_DAYS:
                break
            d = today + timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")

            slots, _reason = await get_available_slots(
                clinic["id"],
                context["doctor_name"],
                date_str,
                branch_id=context.get("branch_id"),
                branch_session=context.get("branch_session"),
            )
            if not slots:
                continue

            remaining = MAX_ROWS - rows_used
            day_slots = slots[: min(3, remaining)]
            if not day_slots:
                break

            if i == 0:
                title = f"{labels[0]} ({d.strftime('%d %b')})"
            elif i == 1:
                title = f"{labels[1]} ({d.strftime('%d %b')})"
            else:
                title = d.strftime("%A, %d %b")

            sections.append(
                {
                    "title": title[:24],
                    "rows": [
                        {
                            "id": f"dtslot_{date_str}_{slot}",
                            "title": self._to_ampm(slot),
                            "description": "",
                        }
                        for slot in day_slots
                    ],
                }
            )
            rows_used += len(day_slots)
            days_with_slots += 1

        if not sections:
            await self._suggest_other_doctors(clinic, phone, context, lang)
            return

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body=get_message("select_datetime", lang),
            button_text=(
                "Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి")
            ),
            sections=sections,
        )

        await self.update_state(clinic, phone, "selecting_slot", context)

    def _to_ampm(self, time_24: str) -> str:
        """Convert a 24h 'HH:MM' time string to 12h AM/PM display format."""
        return format_slot_time(time_24)

    # A 14:00 slot filed under "Evening" reads as a mistake to the patient.
    # (name, hour_start, hour_end, labels) — kept in clock order.
    SLOT_SESSIONS = (
        ("morning", 0, 12, {"en": "🌅 Morning", "hi": "🌅 सुबह", "te": "🌅 ఉదయం"}),
        ("afternoon", 12, 17, {"en": "☀️ Afternoon", "hi": "☀️ दोपहर", "te": "☀️ మధ్యాహ్నం"}),
        ("evening", 17, 24, {"en": "🌆 Evening", "hi": "🌆 शाम", "te": "🌆 సాయంత్రం"}),
    )

    async def _show_slot_list(
        self, clinic: dict, phone: str, slots: list, context: dict, lang: str
    ) -> None:
        """Show available time slots in 12-hour AM/PM format, grouped by session."""
        grouped: dict[str, list] = {name: [] for name, *_ in self.SLOT_SESSIONS}
        for slot in slots:
            try:
                hour = int(str(slot).split(":")[0])
            except ValueError:
                hour = 0  # unparseable: still offer it rather than silently drop it
            for name, hour_from, hour_to, _labels in self.SLOT_SESSIONS:
                if hour_from <= hour < hour_to:
                    grouped[name].append(slot)
                    break
            else:
                grouped[self.SLOT_SESSIONS[0][0]].append(slot)

        filled = [
            (labels.get(lang, labels["en"]), grouped[name])
            for name, _from, _to, labels in self.SLOT_SESSIONS
            if grouped[name]
        ]

        # WhatsApp hard-caps a list at 10 rows, so share the budget across the
        # sessions that actually have slots — otherwise a busy morning buries
        # the evening entirely. Counts are of everything free that day, so "(8)"
        # beside 4 rows still tells the patient more exist.
        sections = []
        budget = 10
        for i, (title, group) in enumerate(filled):
            take = min(len(group), -(-budget // (len(filled) - i)))
            sections.append(
                {
                    "title": f"{title} ({len(group)})",
                    "rows": [
                        {"id": f"slot_{slot}", "title": self._to_ampm(slot), "description": ""}
                        for slot in group[:take]
                    ],
                }
            )
            budget -= take

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body=get_message("select_slot", lang),
            button_text=(
                "Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి")
            ),
            sections=sections,
        )

        await self.update_state(clinic, phone, "selecting_slot", context)

    async def _handle_selecting_slot(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
    ) -> None:
        """Handle slot selection — combined date+time tap, legacy single-day
        slot tap, or free-text date input (delegates to the date parser)."""

        if intent == "select_datetime":
            date_str, _, time_str = message.partition("_")
            context["appointment_date"] = date_str
            context["appointment_time"] = time_str
        elif intent == "select_slot":
            context["appointment_time"] = message.strip()
        else:
            await self._handle_selecting_date(clinic, phone, message, context, lang)
            return

        # Show confirmation
        await self._show_booking_confirmation(clinic, phone, context, lang)

    async def _show_booking_confirmation(
        self, clinic: dict, phone: str, context: dict, lang: str
    ) -> None:
        """Show booking confirmation summary (includes branch when applicable)."""
        from datetime import datetime

        date_display = datetime.strptime(
            context["appointment_date"], "%Y-%m-%d"
        ).strftime("%d %b %Y")

        # Build confirmation body — resolve branch authoritatively when branch_id is present
        branch_id = context.get("branch_id")
        branch_name = context.get("branch_name")
        branch_landmark = context.get("branch_landmark", "")

        if branch_id:
            from app.services.tenant import get_branch_by_id
            try:
                auth_branch = await get_branch_by_id(branch_id)
                if auth_branch:
                    branch_name = auth_branch.get("short_name") or auth_branch.get("name", "")
                    branch_landmark = auth_branch.get("landmark", "")
                    # Sync back into context to guarantee downstream steps have exact values
                    self._set_branch_context(context, auth_branch, session_val=context.get("branch_session"))
            except Exception as e:
                logger.warning(f"Error resolving branch for confirmation: {e}")

        if branch_name:
            # Multi-branch: include locality in confirmation
            branch_line = f"\n🏥 Branch: {branch_name}"
            if branch_landmark:
                branch_line += f" ({branch_landmark})"

            confirm_body = (
                get_message(
                    "confirm_booking",
                    lang,
                    name=resolve_booking_name(context),
                    doctor=context["doctor_name"],
                    department=context.get("department", ""),
                    date=date_display,
                    time=context["appointment_time"],
                )
                + branch_line
            )
        else:
            confirm_body = get_message(
                "confirm_booking",
                lang,
                name=resolve_booking_name(context),
                doctor=context["doctor_name"],
                department=context.get("department", ""),
                date=date_display,
                time=context["appointment_time"],
            )

        if context.get("treatment_name"):
            confirm_body += (
                "\n🩺 " + {"en": "Treatment", "hi": "उपचार", "te": "చికిత్స"}.get(lang, "Treatment")
                + f": {context['treatment_name']}"
            )

        await self.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=confirm_body,
            buttons=[
                {
                    "id": "confirm_yes",
                    "title": (
                        "Confirm"
                        if lang == "en"
                        else ("पुष्टि" if lang == "hi" else "నిర్ధారించు")
                    ),
                },
                {
                    "id": "confirm_no",
                    "title": (
                        "Edit"
                        if lang == "en"
                        else ("संपादन" if lang == "hi" else "మార్చు")
                    ),
                },
            ],
        )

        await self.update_state(clinic, phone, "confirming_booking", context)

    async def _handle_confirming_booking(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        patient: dict,
        lang: str,
    ) -> None:
        """Handle booking confirmation — payment-gated when Razorpay is configured.

        Two modes:
          A) Razorpay configured → payment-gated flow (pending_payment → webhook → confirmed)
          B) Razorpay NOT configured → direct booking (original flow, confirmed immediately)
        """

        if intent in ["confirm_booking", "yes"]:
            from datetime import datetime
            from app.database import get_doctor_by_name

            # Pre-booking server-side re-validation: verify doctor is still active
            doc_name = context.get("doctor_name")
            if doc_name:
                try:
                    doc_check = await get_doctor_by_name(clinic["id"], doc_name)
                    if doc_check is not None and not doc_check.get("is_active"):
                        no_doc_err = {
                            "en": f"Sorry, Dr. {doc_name} is no longer available for online bookings. Please select another doctor.",
                            "hi": f"क्षमा करें, डॉ. {doc_name} अब ऑनलाइन बुकिंग के लिए उपलब्ध नहीं हैं। कृपया अन्य डॉक्टर चुनें।",
                            "te": f"క్షమించండి, డాక్టర్ {doc_name} ఇకపై ఆన్‌లైన్ బుకింగ్‌ల కోసం అందుబాటులో లేరు. దయచేసి మరొక డాక్టర్‌ను ఎంచుకోండి.",
                        }.get(lang, f"Sorry, Dr. {doc_name} is no longer available. Please select another doctor.")
                        await self.whatsapp.send_text(clinic, phone, no_doc_err)
                        await self.update_state(clinic, phone, "main_menu")
                        await self._send_main_menu(clinic, phone, lang)
                        return
                except Exception as doc_err:
                    logger.warning(f"Failed to check doctor active status: {doc_err}")

            # A treatment deleted while the patient was booking would fail the
            # foreign key and lose the booking: drop the tag, keep the booking.
            await specialty_flow.revalidate_treatment(clinic, context)

            # ── Resolve this clinic's payment mode: full / partial / none ──
            from app.services.payment import resolve_payment_mode

            payment_mode, deposit_percent = resolve_payment_mode(clinic)

            if payment_mode in ("full", "partial"):
                # ═══ PATH A: Payment-gated booking ═══
                from app.services.payment import payment_service

                result = await payment_service.create_booking_with_payment(
                    clinic_id=clinic["id"],
                    patient_phone=phone,
                    patient_name=resolve_booking_name(context, patient),
                    department=context.get("department", "General Medicine"),
                    doctor_name=context["doctor_name"],
                    appointment_date=context["appointment_date"],
                    appointment_time=context["appointment_time"],
                    symptoms=context.get("symptoms", ""),
                    patient_id=patient.get("id"),
                    clinic=clinic,
                    branch_id=context.get("branch_id"),
                    branch_name=context.get("branch_name"),
                    deposit_percent=deposit_percent,
                    doctor_id=context.get("doctor_id") or context.get("selected_doctor_id"),
                    treatment_id=context.get("treatment_id"),
                    treatment_name=context.get("treatment_name"),
                )

                if result["success"]:
                    amount_rupees = result["amount_paise"] / 100
                    date_display = datetime.strptime(
                        context["appointment_date"], "%Y-%m-%d"
                    ).strftime("%d %b %Y")

                    deposit_note_en = (
                        f"_This is a {deposit_percent}% deposit — the remaining "
                        f"{100 - deposit_percent}% is payable at the clinic._\n\n"
                        if payment_mode == "partial"
                        else ""
                    )
                    deposit_note_hi = (
                        f"_यह {deposit_percent}% जमा राशि है — शेष {100 - deposit_percent}% "
                        f"क्लिनिक में देय है।_\n\n"
                        if payment_mode == "partial"
                        else ""
                    )
                    deposit_note_te = (
                        f"_ఇది {deposit_percent}% డిపాజిట్ — మిగిలిన {100 - deposit_percent}% "
                        f"క్లినిక్‌లో చెల్లించాలి._\n\n"
                        if payment_mode == "partial"
                        else ""
                    )

                    hold_mins = getattr(settings, "booking_hold_minutes", 10)

                    payment_msg = {
                        "en": (
                            f"💳 *Payment Required to Confirm Booking*\n\n"
                            f"👨‍⚕️ Doctor: {context['doctor_name']}\n"
                            f"📅 Date: {date_display}\n"
                            f"🕐 Time: {context['appointment_time']}\n"
                            f"💰 Amount: ₹{amount_rupees:.0f}\n\n"
                            f"{deposit_note_en}"
                            f"⏱️ *This slot is held for {hold_mins} minutes.* Pay before it expires.\n\n"
                            f"👉 Click below to pay securely via Razorpay:\n"
                            f"{result['payment_link']}\n\n"
                            f"_Amount is refundable if cancelled {settings.refund_window_hours}+ hours before appointment. "
                            f"No-show bookings are non-refundable._"
                        ),
                        "hi": (
                            f"💳 *बुकिंग की पुष्टि के लिए भुगतान करें*\n\n"
                            f"👨‍⚕️ डॉक्टर: {context['doctor_name']}\n"
                            f"📅 तारीख: {date_display}\n"
                            f"🕐 समय: {context['appointment_time']}\n"
                            f"💰 राशि: ₹{amount_rupees:.0f}\n\n"
                            f"{deposit_note_hi}"
                            f"⏱️ *यह स्लॉट {hold_mins} मिनट के लिए होल्ड है।* समय से पहले भुगतान करें।\n\n"
                            f"👉 Razorpay से सुरक्षित भुगतान करें:\n"
                            f"{result['payment_link']}\n\n"
                            f"_अपॉइंटमेंट से {settings.refund_window_hours}+ घंटे पहले रद्द करने पर राशि वापस की जाएगी। "
                            f"नो-शो बुकिंग पर रिफंड नहीं होगा।_"
                        ),
                        "te": (
                            f"💳 *బుకింగ్ నిర్ధారించడానికి చెల్లింపు అవసరం*\n\n"
                            f"👨‍⚕️ డాక్టర్: {context['doctor_name']}\n"
                            f"📅 తేదీ: {date_display}\n"
                            f"🕐 సమయం: {context['appointment_time']}\n"
                            f"💰 మొత్తం: ₹{amount_rupees:.0f}\n\n"
                            f"{deposit_note_te}"
                            f"⏱️ *ఈ స్లాట్ {hold_mins} నిమిషాలు హోల్డ్ చేయబడింది.* గడువులోపు చెల్లించండి.\n\n"
                            f"👉 Razorpay ద్వారా సురక్షితంగా చెల్లించండి:\n"
                            f"{result['payment_link']}\n\n"
                            f"_అపాయింట్‌మెంట్‌కు {settings.refund_window_hours}+ గంటల ముందు రద్దు చేస్తే మొత్తం రీఫండ్ అవుతుంది. "
                            f"నో-షో బుకింగ్‌లు రీఫండ్ కావు._"
                        ),
                    }.get(lang, None)

                    if not payment_msg:
                        payment_msg = (
                            f"💳 *Payment Required to Confirm Booking*\n\n"
                            f"👨‍⚕️ Doctor: {context['doctor_name']}\n"
                            f"📅 Date: {date_display}\n"
                            f"🕐 Time: {context['appointment_time']}\n"
                            f"💰 Amount: ₹{amount_rupees:.0f}\n\n"
                            f"{deposit_note_en}"
                            f"⏱️ *This slot is held for {hold_mins} minutes.* Pay before it expires.\n\n"
                            f"👉 Click below to pay securely via Razorpay:\n"
                            f"{result['payment_link']}\n\n"
                            f"_Refundable if cancelled {settings.refund_window_hours}+ hours before appointment. "
                            f"No-show bookings are non-refundable._"
                        )

                    await self.whatsapp.send_text(clinic, phone, payment_msg)

                    await log_analytics_event(
                        clinic["id"],
                        phone,
                        "payment_link_sent",
                        department=context.get("department"),
                    )

                    # Save booking context and transition to awaiting_payment
                    context["booking_id"] = result["booking_id"]
                    context["razorpay_payment_link_id"] = result["razorpay_payment_link_id"]
                    context["booking_ref"] = result["booking_ref"]
                    await self.update_state(clinic, phone, "awaiting_payment", context)

                elif result.get("reason") == "slot_taken":
                    await self.whatsapp.send_text(
                        clinic,
                        phone,
                        get_message("slot_taken", lang, doctor=context["doctor_name"]),
                    )
                    slots, _ = await get_available_slots(
                        clinic["id"],
                        context["doctor_name"],
                        context["appointment_date"],
                        branch_id=context.get("branch_id"),
                        branch_session=context.get("branch_session"),
                    )
                    if slots:
                        await self._show_slot_list(
                            clinic, phone, slots, context, lang
                        )
                    else:
                        await self._suggest_other_doctors(clinic, phone, context, lang)
                elif result.get("reason") == "razorpay_error":
                    error_msg = {
                        "en": "We're having trouble connecting to the payment gateway right now. Please try again in a few minutes or contact the clinic.",
                        "hi": "भुगतान गेटवे से जुड़ने में समस्या आ रही है। कृपया कुछ समय बाद पुनः प्रयास करें या क्लिनिक से संपर्क करें।",
                        "te": "పేమెంట్ గేట్‌వే కనెక్ట్ చేయడంలో సమస్య ఉంది. దయచేసి కొద్దిసేపటి తర్వాత మళ్లీ ప్రయత్నించండి లేదా క్లినిక్‌ని సంప్రదించండి.",
                    }.get(
                        lang,
                        "Payment gateway is temporarily unavailable. Please try again later.",
                    )
                    await self.whatsapp.send_text(clinic, phone, error_msg)
                    await self.update_state(clinic, phone, "main_menu")
                    await self._send_main_menu(clinic, phone, lang)
                else:
                    error_msg = {
                        "en": "Sorry, we couldn't process your booking right now. Please try again.",
                        "hi": "क्षमा करें, अभी बुकिंग प्रक्रिया नहीं हो सकी। कृपया पुनः प्रयास करें।",
                        "te": "క్షమించండి, మీ బుకింగ్ ప్రాసెస్ కాలేదు. దయచేసి మళ్ళీ ప్రయత్నించండి.",
                    }.get(
                        lang,
                        "Sorry, we couldn't process your booking right now. Please try again.",
                    )
                    await self.whatsapp.send_text(clinic, phone, error_msg)
                    await self.update_state(clinic, phone, "main_menu")
                    await self._send_main_menu(clinic, phone, lang)
            else:
                # ═══ PATH B: Direct booking (payment_mode == "none") ═══
                appointment_data = {
                    "patient_id": patient.get("id"),
                    "patient_phone": phone,
                    "patient_name": resolve_booking_name(context, patient),
                    "department": context.get("department", "General Medicine"),
                    "doctor_name": context["doctor_name"],
                    "appointment_date": context["appointment_date"],
                    "appointment_time": context["appointment_time"],
                    "symptoms": context.get("symptoms", ""),
                    "status": "confirmed",
                }

                doctor_id_val = context.get("doctor_id") or context.get("selected_doctor_id")
                if doctor_id_val:
                    appointment_data["doctor_id"] = doctor_id_val

                # Include branch info when booking at a specific branch
                if context.get("branch_id"):
                    appointment_data["branch_id"] = context["branch_id"]
                    appointment_data["branch_name"] = context.get("branch_name", "")

                if context.get("treatment_id"):
                    appointment_data["treatment_id"] = context["treatment_id"]
                    appointment_data["treatment_name"] = context.get("treatment_name")

                result = await book_appointment(clinic["id"], appointment_data)

                if result["success"]:
                    appointment = result["appointment"]
                    date_display = datetime.strptime(
                        context["appointment_date"], "%Y-%m-%d"
                    ).strftime("%d %b %Y")

                    confirm_text = get_message(
                        "booking_confirmed",
                        lang,
                        ref=appointment["booking_ref"],
                        doctor=context["doctor_name"],
                        date=date_display,
                        time=context["appointment_time"],
                    )
                    # State the cancellation deadline at the moment of booking,
                    # computed from this clinic's own window. `refundable` keys
                    # off payment_id: this branch is the no-Razorpay direct
                    # booking, so promising a "full refund" would promise money
                    # the patient never paid.
                    policy_line = cancellation_policy_line(
                        lang,
                        cancellation_window_hours(clinic),
                        context["appointment_date"],
                        context["appointment_time"],
                        refundable=bool(appointment.get("payment_id")),
                    )
                    if policy_line:
                        confirm_text += "\n\n" + policy_line

                    await self.whatsapp.send_text(
                        clinic,
                        phone,
                        confirm_text,
                        _source="booking_confirmation",
                    )

                    # Send location — use branch-specific info for multi-branch,
                    # or clinic-level info for single-branch bookings
                    if context.get("branch_id"):
                        # Multi-branch: send branch-specific address + Google Maps
                        from app.services.tenant import get_branch_by_id
                        branch = None
                        try:
                            branch = await get_branch_by_id(context["branch_id"])
                        except Exception as e:
                            logger.warning(f"Error fetching branch for confirmation location: {e}")

                        if branch:
                            branch_name = branch.get("short_name") or branch.get("name", "")
                            branch_address = branch.get("address", "")
                            branch_landmark = branch.get("landmark", "")
                            branch_maps = branch.get("maps_link", "")
                        else:
                            branch_name = context.get("branch_name", "")
                            branch_address = context.get("branch_address", "")
                            branch_landmark = context.get("branch_landmark", "")
                            branch_maps = context.get("branch_maps_link", "")

                        if branch_address or branch_maps or branch_landmark:
                            location_lines = [
                                f"📍 Location: {branch_name}"
                            ]
                            if branch_address:
                                location_lines[0] += f", {branch_address}"
                            if branch_landmark:
                                location_lines.append(
                                    f"Near {branch_landmark}"
                                )
                            if branch_maps:
                                location_lines.append(
                                    f"🗺️ Google Maps: {branch_maps}"
                                )
                            await self.whatsapp.send_text(
                                clinic, phone, "\n".join(location_lines)
                            )
                    else:
                        from app.services.tenant import get_clinic_contact

                        clinic_address = get_clinic_contact(
                            clinic, "address", settings.hospital_address
                        )
                        clinic_maps_link = get_clinic_contact(
                            clinic, "maps_link", settings.hospital_maps_link
                        )
                        if clinic_address or clinic_maps_link:
                            location_lines = [
                                f"📍 Location: {clinic.get('name', settings.hospital_name)}"
                            ]
                            if clinic_address:
                                location_lines[0] += f", {clinic_address}"
                            if clinic_maps_link:
                                location_lines.append(
                                    f"Google Maps: {clinic_maps_link}"
                                )
                            await self.whatsapp.send_text(
                                clinic, phone, "\n".join(location_lines)
                            )

                    await log_analytics_event(
                        clinic["id"],
                        phone,
                        "appointment_booked",
                        department=context.get("department"),
                    )

                    import asyncio

                    await asyncio.sleep(2)

                    # Pre-appointment instructions
                    dept_instruction = {
                        "en": f"Instructions for {context.get('department')}: Please arrive 15 minutes early and bring relevant medical records.",
                        "hi": f"{context.get('department')} के लिए निर्देश: कृपया 15 मिनट पहले पहुंचें और प्रासंगिक चिकित्सा रिकॉर्ड लाएं।",
                        "te": f"{context.get('department')} కోసం సూచనలు: దయచేసి సంబంధిత మెడికల్ రికార్డులను తీసుకుని 15 నిమిషాల ముందుగా రండి.",
                    }.get(lang, "Please arrive 15 mins early.")
                    await self.whatsapp.send_text(clinic, phone, dept_instruction)
                    await specialty_flow.send_prep_note(
                        self.whatsapp, clinic, phone, context.get("treatment_id"), lang
                    )

                    follow_up_msg = {
                        "en": "What would you like to do?",
                        "hi": "आप आगे क्या करना चाहेंगे?",
                        "te": "మీరు ఇంకా ఏమి చేయాలనుకుంటున్నారు?",
                    }.get(lang, "What would you like to do?")
                    await self.whatsapp.send_interactive_buttons(
                        clinic,
                        phone,
                        body=follow_up_msg,
                        buttons=[
                            {"id": "main_menu", "title": "Main Menu"},
                        ],
                    )

                    await self.update_state(clinic, phone, "main_menu", reset_context=True)
                else:
                    if result.get("reason") == "slot_taken":
                        await self.whatsapp.send_text(
                            clinic,
                            phone,
                            get_message(
                                "slot_taken", lang, doctor=context["doctor_name"]
                            ),
                        )
                        slots, _ = await get_available_slots(
                            clinic["id"],
                            context["doctor_name"],
                            context["appointment_date"],
                            branch_id=context.get("branch_id"),
                            branch_session=context.get("branch_session"),
                        )
                        if slots:
                            await self._show_slot_list(
                                clinic, phone, slots, context, lang
                            )
                        else:
                            await self._suggest_other_doctors(
                                clinic, phone, context, lang
                            )
                    else:
                        await self.whatsapp.send_text(
                            clinic,
                            phone,
                            get_message(
                                "booking_failed", lang, phone=clinic["whatsapp_number"]
                            ),
                        )
                        await self.update_state(clinic, phone, "main_menu", reset_context=True)
                        await self._send_main_menu(clinic, phone, lang)
        else:
            # Edit booking - go back to doctor selection
            await self._show_doctor_list(
                clinic,
                phone,
                context.get("department", "General Medicine"),
                context,
                lang,
            )

    async def _handle_awaiting_payment(
        self,
        clinic: dict,
        phone: str,
        message: str,
        context: dict,
        patient: dict,
        lang: str,
    ) -> None:
        """Handle messages while patient is in the awaiting_payment state.

        The patient may ask about payment status or want to cancel.
        Actual confirmation only happens via Razorpay webhook, never here.
        """
        msg_lower = message.lower().strip()

        if msg_lower in ["cancel", "रद्द", "రద్దు", "cancel booking"]:
            # Cancel the pending booking
            booking_id = context.get("booking_id")
            if booking_id:
                from app.database import supabase

                query = (
                    supabase.table("appointments")
                    .update({"status": "cancelled"})
                    .eq("id", booking_id)
                    .eq("clinic_id", (clinic or {}).get("id") or "")
                )
                await sb(query.eq("status", "pending_payment"))

            cancel_msg = {
                "en": "Booking cancelled. The slot has been released.",
                "hi": "बुकिंग रद्द कर दी गई। स्लॉट खाली हो गया है।",
                "te": "బుకింగ్ రద్దు చేయబడింది. స్లాట్ విడుదల చేయబడింది.",
            }.get(lang, "Booking cancelled. The slot has been released.")
            await self.whatsapp.send_text(clinic, phone, cancel_msg)
            await self.update_state(clinic, phone, "main_menu")
            await self._send_main_menu(clinic, phone, lang)
            return

        if msg_lower in ["status", "payment status", "स्थिति", "స్థితి"]:
            # Check if booking was confirmed by webhook in the meantime
            booking_id = context.get("booking_id")
            if booking_id:
                from app.database import supabase

                query = (
                    supabase.table("appointments")
                    .select("status, booking_ref")
                    .eq("id", booking_id)
                    .eq("clinic_id", (clinic or {}).get("id") or "")
                )
                result = await sb(query)
                if result.data:
                    status = result.data[0]["status"]
                    if status == "confirmed":
                        confirmed_msg = {
                            "en": f"✅ Your payment has been received and booking *{result.data[0].get('booking_ref', '')}* is confirmed!",
                            "hi": f"✅ आपका भुगतान प्राप्त हो गया है और बुकिंग *{result.data[0].get('booking_ref', '')}* पुष्ट है!",
                            "te": f"✅ మీ చెల్లింపు అందింది మరియు బుకింగ్ *{result.data[0].get('booking_ref', '')}* నిర్ధారించబడింది!",
                        }.get(
                            lang,
                            f"✅ Payment received — booking {result.data[0].get('booking_ref', '')} confirmed!",
                        )
                        await self.whatsapp.send_text(clinic, phone, confirmed_msg)
                        await self.update_state(clinic, phone, "main_menu")
                        await self._send_main_menu(clinic, phone, lang)
                        return
                    elif status == "expired":
                        expired_msg = {
                            "en": "⏰ Your payment window has expired. The slot has been released. Would you like to book again?",
                            "hi": "⏰ भुगतान का समय समाप्त हो गया। स्लॉट खाली हो गया है। क्या आप फिर से बुक करना चाहेंगे?",
                            "te": "⏰ చెల్లింపు సమయం ముగిసింది. స్లాట్ విడుదల చేయబడింది. మళ్ళీ బుక్ చేయాలనుకుంటున్నారా?",
                        }.get(
                            lang,
                            "⏰ Payment window expired. Slot released. Book again?",
                        )
                        await self.whatsapp.send_text(clinic, phone, expired_msg)
                        await self.update_state(clinic, phone, "main_menu")
                        await self._send_main_menu(clinic, phone, lang)
                        return

            # Still pending
            pending_msg = {
                "en": "⏳ Waiting for your payment. Please complete the payment using the link above, or type *cancel* to cancel.",
                "hi": "⏳ आपके भुगतान की प्रतीक्षा है। ऊपर दिए गए लिंक से भुगतान करें, या *cancel* टाइप करें।",
                "te": "⏳ మీ చెల్లింపు కోసం ఎదురుచూస్తున్నాము. పైన ఉన్న లింక్ ద్వారా చెల్లించండి, లేదా *cancel* టైప్ చేయండి.",
            }.get(lang, "⏳ Waiting for payment. Use the link or type *cancel*.")
            await self.whatsapp.send_text(clinic, phone, pending_msg)
            return

        # Default: remind them to pay
        reminder_msg = {
            "en": "💳 Your slot is being held. Please complete the payment using the link sent above, or type *cancel* to release the slot.",
            "hi": "💳 आपका स्लॉट होल्ड है। ऊपर दिए लिंक से भुगतान करें, या *cancel* टाइप करें।",
            "te": "💳 మీ స్లాట్ హోల్డ్ చేయబడింది. పైన పంపిన లింక్ ద్వారా చెల్లించండి, లేదా *cancel* టైప్ చేయండి.",
        }.get(lang, "💳 Slot held. Pay via the link or type *cancel*.")
        await self.whatsapp.send_text(clinic, phone, reminder_msg)

    async def _suggest_other_doctors(
        self, clinic: dict, phone: str, context: dict, lang: str
    ) -> None:
        """Suggest other doctors when selected doctor is fully booked."""
        department = context.get("department", "General Medicine")
        exclude_doctor = context["doctor_name"]

        import asyncio
        from datetime import datetime, timedelta

        branch_id = context.get("branch_id")
        doctors = [
            d
            for d in await get_doctors(clinic["id"], department, branch_id=branch_id)
            if d["name"] != exclude_doctor
        ]

        # One round per day across every doctor still needing one, instead of
        # doctors x 7 serial round-trips — this runs inside the patient's turn,
        # and the old scan cost whole seconds on a busy department.
        found: dict[str, dict] = {}
        for offset in range(7):
            pending = [d for d in doctors if d["name"] not in found]
            if not pending:
                break
            check_date = (datetime.now() + timedelta(days=offset + 1)).strftime(
                "%Y-%m-%d"
            )
            results = await asyncio.gather(
                *[
                    get_available_slots(
                        clinic["id"],
                        d["name"],
                        check_date,
                        branch_id=branch_id,
                        branch_session=d.get("session") or d.get("branch_session"),
                    )
                    for d in pending
                ]
            )
            for doc, (slots, _reason) in zip(pending, results):
                if slots:
                    found[doc["name"]] = {
                        "name": doc["name"],
                        "specialization": doc.get("specialization", ""),
                        "next_date": datetime.strptime(
                            check_date, "%Y-%m-%d"
                        ).strftime("%d %b"),
                        "next_slot": slots[0],
                    }

        # Keep the department's own doctor ordering, not first-found order.
        available = [found[d["name"]] for d in doctors if d["name"] in found]

        if available:
            await self.whatsapp.send_text(
                clinic,
                phone,
                get_message(
                    "doctor_fully_booked",
                    lang,
                    doctor=exclude_doctor,
                    department=department,
                ),
            )

            sections = [
                {
                    "title": (
                        "Available Doctors"
                        if lang == "en"
                        else (
                            "उपलब्ध डॉक्टर"
                            if lang == "hi"
                            else "అందుబాటులో ఉన్న డాక్టర్లు"
                        )
                    ),
                    "rows": [
                        {
                            "id": f"doc_{i}_{doc['name']}"[:200],
                            "title": doc["name"][:24],
                            "description": f"Available {doc['next_date']}"[:72],
                        }
                        for i, doc in enumerate(available)
                    ],
                }
            ]

            await self.whatsapp.send_interactive_list(
                clinic,
                phone,
                body="Select another doctor:",
                button_text="Select",
                sections=sections,
            )
        else:
            await self.whatsapp.send_text(
                clinic,
                phone,
                get_message(
                    "no_doctors_available",
                    lang,
                    department=department,
                    phone=clinic["whatsapp_number"],
                ),
            )
            await self._send_main_menu(clinic, phone, lang)

    async def _handle_emergency(self, clinic: dict, phone: str, lang: str) -> None:
        """Handle emergency situation."""
        from app.services.tenant import get_clinic_contact

        emergency_number = get_clinic_contact(
            clinic, "emergency_number", settings.hospital_emergency_number
        )
        await self.whatsapp.send_text(
            clinic, phone, get_message("emergency", lang, emergency=emergency_number)
        )

        # Send location if the clinic (or the platform default) has one configured
        maps_link = get_clinic_contact(clinic, "maps_link", settings.hospital_maps_link)
        address = get_clinic_contact(clinic, "address", settings.hospital_address)
        if maps_link or address:
            location_lines = []
            if address:
                location_lines.append(f"Address: {address}")
            if maps_link:
                location_lines.append(f"Google Maps: {maps_link}")
            await self.whatsapp.send_text(
                clinic, phone, "📍 Emergency location\n" + "\n".join(location_lines)
            )

        # Alert hospital staff, if a staff alert number is configured for this clinic/platform
        staff_alert_number = get_clinic_contact(
            clinic, "staff_alert_number", settings.hospital_staff_alert_number
        )
        if staff_alert_number:
            staff_msg = (
                f"🚨 Emergency keyword detected\n\n"
                f"Patient: {mask_phone(phone)}\n"
                f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"Please follow up if not already in contact."
            )
            await self.whatsapp.send_text(clinic, staff_alert_number, staff_msg)

        await self.update_state(clinic, phone, "main_menu")
        await log_analytics_event(clinic["id"], phone, "emergency_detected")

    async def _handle_health_checkin_concern(
        self, clinic: dict, phone: str, lang: str
    ) -> None:
        """Patient reported ongoing symptoms in a post-discharge check-in."""
        await self.whatsapp.send_text(
            clinic, phone, get_message("health_checkin_concern", lang, phone=clinic["whatsapp_number"])
        )
        await log_analytics_event(clinic["id"], phone, "discharge_checkin_concern")

    async def _handle_health_checkin_ok(self, clinic: dict, phone: str, lang: str) -> None:
        """Patient confirmed they're feeling fine in a post-discharge check-in."""
        await self.whatsapp.send_text(clinic, phone, get_message("health_checkin_ok", lang))

    async def _handle_queue_status(
        self, clinic: dict, phone: str, lang: str
    ) -> None:
        """Handle patient query about their live OPD token / queue position."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        status = await get_patient_queue_status(clinic["id"], phone, today_str)

        if not status:
            await self.whatsapp.send_text(
                clinic, phone, get_message("queue_status_none", lang)
            )
            return

        # A sample collection has no doctor, so it gets its own wording. The
        # .get() default below never fired for a lab booking either: the key
        # was present with a None value, so the patient was told their
        # appointment was with "None".
        is_lab_test = bool(status.get("is_lab_test"))
        label = status.get("doctor_name") or (
            "your test" if is_lab_test else "your doctor"
        )

        if not status.get("checked_in"):
            await self.whatsapp.send_text(
                clinic,
                phone,
                get_message(
                    "queue_status_not_checked_in_lab"
                    if is_lab_test
                    else "queue_status_not_checked_in",
                    lang,
                    **({"test": label} if is_lab_test else {"doctor": label}),
                ),
            )
            return

        await self.whatsapp.send_text(
            clinic,
            phone,
            get_message(
                "queue_status_waiting_lab" if is_lab_test else "queue_status_waiting",
                lang,
                token=status["token_number"],
                current=status["currently_serving"],
                ahead=status["patients_ahead"],
                **({"test": label} if is_lab_test else {"doctor": label}),
            ),
        )
        await log_analytics_event(clinic["id"], phone, "queue_status_checked")

    async def _handle_opt_out(
        self, clinic: dict, phone: str, patient: dict, lang: str
    ) -> None:
        """Handle opt-out request."""
        await update_patient(
            clinic["id"], phone, {"opted_in": False, "opted_out_at": "now()"}
        )

        await self.whatsapp.send_text(
            clinic, phone, get_message("opt_out_confirm", lang)
        )
        await log_analytics_event(clinic["id"], phone, "opt_out")

    async def _handle_opt_in(self, clinic: dict, phone: str, lang: str) -> None:
        """Turn follow-up and check-in messages back on after "stop"."""
        await update_patient(
            clinic["id"], phone,
            {"opted_in": True, "opted_in_at": datetime.now(timezone.utc).isoformat()},
        )
        await self.whatsapp.send_text(clinic, phone, {
            "en": "✅ Welcome back — follow-up and health check-in messages are on again. "
                  "Send *stop* anytime to turn them off, or *menu* to see all options.",
            "hi": "✅ फिर से स्वागत है — फॉलो-अप और हेल्थ चेक-इन संदेश फिर से चालू हैं। "
                  "बंद करने के लिए कभी भी *stop* भेजें, या सभी विकल्पों के लिए *menu*।",
            "te": "✅ తిరిగి స్వాగతం — ఫాలో-అప్, ఆరోగ్య చెక్-ఇన్ సందేశాలు మళ్లీ ప్రారంభమయ్యాయి. "
                  "ఆపడానికి ఎప్పుడైనా *stop* పంపండి, అన్ని ఎంపికలకు *menu* పంపండి.",
        }.get(lang, "✅ Follow-up messages are on again. Send *stop* anytime to turn them off."))
        await log_analytics_event(clinic["id"], phone, "opt_in")

    async def _send_help_guide(self, clinic: dict, phone: str, lang: str) -> None:
        """How to use the bot, listing only what THIS clinic's plan can do.

        Every command below is one this codebase actually answers: a guide
        that names a command the bot then misreads is worse than no guide.
        """
        from app.services.tenant import has_feature

        diagnostics_only = await self._is_diagnostics_only(clinic)
        labs = has_feature(clinic, "lab_test_booking")
        doctors = has_feature(clinic, "booking") and not diagnostics_only
        name = (clinic.get("name") or "").strip()

        def t(en: str, hi: str, te: str) -> str:
            return {"en": en, "hi": hi, "te": te}.get(lang, en)

        lines = [t(
            f"❓ *How to use{' ' + name if name else ''} on WhatsApp*",
            "❓ *WhatsApp पर उपयोग कैसे करें*",
            "❓ *WhatsAppలో ఎలా ఉపయోగించాలి*",
        ), "", t("Just type any of these words:", "बस इनमें से कोई शब्द लिखें:",
                 "ఈ పదాల్లో ఏదైనా టైప్ చేయండి:"), ""]
        lines.append(t("📋 *menu* — see all options", "📋 *menu* — सभी विकल्प देखें",
                       "📋 *menu* — అన్ని ఎంపికలు చూడండి"))
        if doctors:
            lines.append(t("📅 *book* — book a doctor appointment", "📅 *book* — डॉक्टर अपॉइंटमेंट बुक करें",
                           "📅 *book* — డాక్టర్ అపాయింట్‌మెంట్ బుక్ చేయండి"))
        if labs:
            lines.append(t("🧪 *book test* — book a lab test, scan or health package",
                           "🧪 *book test* — लैब टेस्ट, स्कैन या हेल्थ पैकेज बुक करें",
                           "🧪 *book test* — ల్యాబ్ పరీక్ష, స్కాన్ లేదా హెల్త్ ప్యాకేజీ బుక్ చేయండి"))
            lines.append(t('🔍 Type a test name to search — e.g. "thyroid", "MRI brain"',
                           '🔍 खोजने के लिए टेस्ट का नाम लिखें — जैसे "thyroid", "MRI brain"',
                           '🔍 వెతకడానికి పరీక్ష పేరు టైప్ చేయండి — ఉదా. "thyroid", "MRI brain"'))
        lines.append(t("❌ *cancel booking* — cancel an upcoming booking",
                       "❌ *cancel booking* — आने वाली बुकिंग रद्द करें",
                       "❌ *cancel booking* — రాబోయే బుకింగ్ రద్దు చేయండి"))
        if doctors:
            lines.append(t("🔁 *reschedule* — change your appointment date or time",
                           "🔁 *reschedule* — अपॉइंटमेंट की तारीख या समय बदलें",
                           "🔁 *reschedule* — అపాయింట్‌మెంట్ తేదీ లేదా సమయం మార్చండి"))
        lines += [
            t("🌐 *change language* — English, हिंदी, తెలుగు", "🌐 *change language* — भाषा बदलें",
              "🌐 *change language* — భాష మార్చండి"),
            t("👩‍⚕️ *talk to staff* — reach our team", "👩‍⚕️ *talk to staff* — हमारी टीम से बात करें",
              "👩‍⚕️ *talk to staff* — మా బృందంతో మాట్లాడండి"),
            t("🚨 *emergency* — urgent help and our emergency number",
              "🚨 *emergency* — तुरंत मदद और आपातकालीन नंबर",
              "🚨 *emergency* — అత్యవసర సహాయం, నంబర్"),
            "",
            t("🔕 *stop* — stop follow-up and health check-in messages. Booking "
              "confirmations, reminders, reports and payment updates still arrive.",
              "🔕 *stop* — फॉलो-अप और हेल्थ चेक-इन संदेश बंद करें। बुकिंग, रिमाइंडर, "
              "रिपोर्ट और भुगतान सूचनाएं आती रहेंगी।",
              "🔕 *stop* — ఫాలో-అప్, ఆరోగ్య చెక్-ఇన్ సందేశాలు ఆపండి. బుకింగ్, రిమైండర్లు, "
              "రిపోర్ట్లు, చెల్లింపు సమాచారం వస్తూనే ఉంటాయి."),
            t("🔔 *start* — turn them back on", "🔔 *start* — इन्हें फिर से चालू करें",
              "🔔 *start* — మళ్లీ ప్రారంభించండి"),
            t("🗑️ *delete my data* — erase your details from our system",
              "🗑️ *delete my data* — अपना डेटा हटाएं",
              "🗑️ *delete my data* — మీ వివరాలు తొలగించండి"),
            "",
            t("You can also simply type what you need in your own words.",
              "आप अपनी बात अपने शब्दों में भी लिख सकते हैं।",
              "మీకు కావలసింది మీ మాటల్లో కూడా టైప్ చేయవచ్చు."),
        ]
        body = "\n".join(lines)
        await self.whatsapp.send_interactive_buttons(
            clinic, phone,
            body=body[:1024],
            buttons=[{"id": "main_menu", "title": t("Main Menu", "मुख्य मेनू", "ప్రధాన మెనూ")[:20]}],
        )
        await log_analytics_event(clinic["id"], phone, "help_viewed")

    #: States a patient is not in the middle of anything. Everywhere else, an
    #: answered question is an interruption they need help getting back from.
    _RESTING_STATES = frozenset({"idle", "main_menu", "emergency", "escalated_to_human"})

    #: States where free text is the patient's answer to our question.
    _FREE_TEXT_ANSWER_STATES = frozenset({"collecting_name", "collecting_symptoms", "asking_symptoms"})

    async def _answer_clinic_info(
        self, clinic: dict, phone: str, message: str, state: str, lang: str
    ) -> bool:
        """Answer a question about the clinic from the clinic's own data.

        Returns True when an answer was sent, False when the caller should
        carry on routing as if this block did not exist.

        The conversation state is left exactly as it was, so this can be asked
        at any point in a booking without costing the patient their progress.
        """
        from app.services.tenant import get_clinic_contact
        from app.services.faq_engine import answer as clinic_info_answer, detect_topic

        topic = detect_topic(message, clinic, lang)

        # A patient mid-search or mid-booking is only interrupted on a
        # deterministic phrase match. The LLM may route a novel phrasing
        # ("ur place kahan hai") from a resting state, but never while a
        # catalogue search is open: one misread test name there would take the
        # patient's search away, which costs more than the answer is worth.
        if topic is None and state not in self._RESTING_STATES:
            return False

        # topic None from a resting state = an info question we could not
        # categorise. answer() then returns every topic this clinic has data
        # for, which beats guessing one and guessing wrong.
        body = await clinic_info_answer(clinic, topic, lang)

        def t(en: str, hi: str, te: str) -> str:
            return {"en": en, "hi": hi, "te": te}.get(lang, en)

        if state in self._RESTING_STATES:
            hint = t(
                "Type *menu* for all options.",
                "सभी विकल्पों के लिए *menu* लिखें।",
                "అన్ని ఎంపికల కోసం *menu* అని టైప్ చేయండి.",
            )
        else:
            hint = t(
                "↩️ You can carry on from where you left off — or type *menu* to start again.",
                "↩️ आप जहाँ थे वहीं से जारी रख सकते हैं — या फिर से शुरू करने के लिए *menu* लिखें।",
                "↩️ మీరు ఆపిన చోటి నుండే కొనసాగవచ్చు — లేదా మళ్లీ మొదలుపెట్టడానికి *menu* అని టైప్ చేయండి.",
            )

        if not body:
            # Nothing configured for what they asked. Say so and hand them a
            # human -- the one thing this must never do is fill the gap with a
            # plausible-sounding address or opening time.
            reception = get_clinic_contact(
                clinic,
                "staff_phone",
                get_clinic_contact(
                    clinic,
                    "phone",
                    clinic.get("whatsapp_number") or settings.hospital_phone,
                ),
            )
            body = t(
                f"I don't have that detail on hand. Our team can help — please call {reception}.",
                f"मेरे पास यह जानकारी नहीं है। कृपया हमारी टीम को {reception} पर कॉल करें।",
                f"ఆ వివరం నా దగ్గర లేదు. దయచేసి మా బృందానికి {reception} కు కాల్ చేయండి.",
            )

        await self.whatsapp.send_text(clinic, phone, f"{body}\n\n{hint}")
        # analytics_events.intent is VARCHAR(50) and a clinic names its own
        # custom topics, so the width is not ours to assume.
        await log_analytics_event(
            clinic["id"], phone, "clinic_info_answered", intent=(topic or "general")[:50]
        )
        return True

    async def _handle_data_deletion(
        self, clinic: dict, phone: str, patient: dict, lang: str
    ) -> None:
        """Handle data deletion request."""
        from app.database import delete_patient_data

        deleted = await delete_patient_data(clinic["id"], phone)
        if deleted:
            reply = get_message("data_deleted", lang)
        else:
            reply = {
                "en": "We couldn't delete your data just now. Please try again in a few minutes, or contact the centre.",
                "hi": "हम अभी आपका डेटा नहीं हटा सके। कृपया कुछ मिनट बाद फिर कोशिश करें या केंद्र से संपर्क करें।",
                "te": "ప్రస్తుతం మీ డేటాను తొలగించలేకపోయాము. దయచేసి కొన్ని నిమిషాల తర్వాత మళ్లీ ప్రయత్నించండి లేదా కేంద్రాన్ని సంప్రదించండి.",
            }.get(lang, "We couldn't delete your data just now. Please try again.")
        # The purge above deletes the conversation row, and send_text reads
        # that row to decide whether the 24h window is open -- so the
        # confirmation was always dropped as "session expired". The patient
        # messaged us seconds ago, so the window is open by definition.
        await self.whatsapp.send_text(clinic, phone, reply, _window_open=True)
        if deleted:
            await log_analytics_event(clinic["id"], phone, "data_deleted")

    async def _handle_human_escalation(
        self, clinic: dict, phone: str, lang: str
    ) -> None:
        """Handle human escalation request."""
        from app.services.tenant import get_clinic_contact

        # The clinic's own reception line, set in Hospital Profile -> Staff /
        # Reception Phone. Falls back to the WhatsApp/contact number so clinics
        # that never set one keep the previous behaviour.
        contact_phone = get_clinic_contact(
            clinic,
            "staff_phone",
            clinic.get("whatsapp_number")
            or clinic.get("phone")
            or settings.hospital_phone,
        )
        await self.whatsapp.send_text(
            clinic,
            phone,
            get_message("human_escalation", lang, phone=contact_phone),
        )
        await self.update_state(clinic, phone, "escalated_to_human")
        await log_analytics_event(clinic["id"], phone, "human_escalation")

    async def _show_services(self, clinic: dict, phone: str, lang: str) -> None:
        """Show bookable services by delegating to dynamic department list (OQ-3 Unification)."""
        await self._show_department_list(clinic, phone, context={}, lang=lang)

    async def _show_doctors(
        self, clinic: dict, phone: str, lang: str, page: int = 0
    ) -> None:
        """Show available doctors grouped by department, one page at a time."""
        from app.database import supabase

        response = (
            await sb(supabase.table("doctors")
            .select("*")
            .eq("clinic_id", clinic["id"])
            .eq("is_active", True)
            .order("department"))
        )
        doctors = response.data or []

        if not doctors:
            no_doctors_msg = {
                "en": "We don't have any doctors listed for online booking right now. Please call us directly.",
                "hi": "अभी ऑनलाइन बुकिंग के लिए कोई डॉक्टर सूचीबद्ध नहीं है। कृपया सीधे हमें कॉल करें।",
                "te": "ప్రస్తుతం ఆన్‌లైన్ బుకింగ్ కోసం డాక్టర్లు జాబితా చేయబడలేదు. దయచేసి నేరుగా మాకు కాల్ చేయండి.",
            }.get(
                lang,
                "We don't have any doctors listed for online booking right now. Please call us directly.",
            )
            await self.whatsapp.send_text(clinic, phone, no_doctors_msg)
            return

        # Fetch branch assignments for all doctors
        doctor_ids = [d["id"] for d in doctors]
        branch_result = (
            await sb(supabase.table("doctor_branches")
            .select("doctor_id, branch_id, session, branches(name, short_name)")
            .in_("doctor_id", doctor_ids))
        )

        doc_branches = {}
        for row in (branch_result.data or []):
            did = row["doctor_id"]
            if did not in doc_branches:
                doc_branches[did] = []
            b_info = row.get("branches") or {}
            doc_branches[did].append(
                {
                    "name": b_info.get("short_name") or b_info.get("name", ""),
                    "session": row.get("session", "both"),
                }
            )

        dept_groups = {}
        for doc in doctors:
            dept = doc.get("department", "General Medicine")
            dept_groups.setdefault(dept, []).append(doc)

        # Build a row for EVERY doctor first, then page. This used to cap at
        # `remaining_rows = 10` and simply stop: a clinic with 14 doctors showed
        # 10 and the other 4 were unreachable from WhatsApp entirely, with no
        # "more" row and nothing in the message to say anything had been left
        # out. The doctors existed in the admin panel, so it read as the bot
        # losing them.
        all_rows = []
        row_dept = {}
        for dept, docs in dept_groups.items():
            for doc in docs:
                branches = doc_branches.get(doc["id"], [])
                if branches:
                    branch_label = ", ".join(
                        f"{b['name']}({b['session'][:3]})" if b["session"] != "both" else b["name"]
                        for b in branches if b["name"]
                    )
                else:
                    branch_label = ""

                desc_parts = [doc["specialization"]]
                if branch_label:
                    desc_parts.append(branch_label)
                desc_parts.append(f"₹{doc['consultation_fee']}")

                row_id = f"view_doc_{doc['id']}"
                all_rows.append(
                    {
                        "id": row_id,
                        "title": doc["name"][:24],
                        "description": " · ".join(desc_parts)[:72],
                    }
                )
                row_dept[row_id] = dept

        rows, page = self._page_rows(all_rows, page, _MORE_DOCTORS_ID, lang)

        # Stateless paging: the next page number rides on the button id rather
        # than the conversation context. "Our Doctors" is reachable from several
        # states, and writing a page counter into whichever one the patient
        # happens to be in risks clobbering a booking in progress.
        for row in rows:
            if row["id"] == _MORE_DOCTORS_ID:
                row["id"] = f"{_MORE_DOCTORS_ID}_{page + 1}"

        # Re-group the paged rows back under their department headings.
        sections = []
        for row in rows:
            title = (
                {"en": "More", "hi": "और", "te": "మరిన్ని"}.get(lang, "More")
                if row["id"].startswith(_MORE_DOCTORS_ID)
                else row_dept.get(row["id"], "Doctors")
            )[:24]
            if sections and sections[-1]["title"] == title:
                sections[-1]["rows"].append(row)
            else:
                sections.append({"title": title, "rows": [row]})

        await self.whatsapp.send_interactive_list(
            clinic,
            phone=phone,
            header={
                "en": "Our Doctors",
                "hi": "हमारे डॉक्टर",
                "te": "మా డాక్టర్లు",
            }.get(lang, "Our Doctors"),
            body=get_message("our_doctors_body", lang),
            button_text={"en": "Select", "hi": "चुनें", "te": "ఎంచుకోండి"}.get(
                lang, "Select"
            ),
            sections=sections,
        )

    async def _cancel_with_refund(self, clinic: dict, phone: str, appointment_id: str):
        """Cancel one appointment and settle the money in the same step.

        Returns (cancelled, refund) where `refund` is None when the booking was
        never paid for, and otherwise the initiate_refund() result — the caller
        stays silent about money in the first case and lets
        notify_cancellation_outcome() do the talking in the second.

        ORDER MATTERS: the refund runs BEFORE the cancel. initiate_refund()
        only accepts a booking in 'confirmed'/'pending_review', so flipping the
        status first would make every refund fail with
        cannot_refund_status_cancelled and silently keep the patient's money.

        The refund itself already moves the row to 'refunded', so db_cancel()
        is called only on the paths where no refund landed.
        """
        from app.database import cancel_appointment as db_cancel, supabase
        from app.services.payment import payment_service

        booking = None
        try:
            res = (
                await sb(supabase.table("appointments")
                .select("*")
                .eq("clinic_id", clinic["id"])
                .eq("id", appointment_id))
            )
            if res.data:
                booking = res.data[0]
        except Exception as e:
            logger.warning(f"Could not load appointment {appointment_id} for cancel: {e}")

        # Unpaid (or unreadable) booking: the original behaviour, unchanged.
        if not booking or not booking.get("payment_id"):
            return await db_cancel(clinic["id"], appointment_id), None

        refund = await payment_service.initiate_refund(
            appointment_id, reason="patient_cancelled", clinic=clinic
        )

        if refund.get("success"):
            # initiate_refund() already set status='refunded'.
            cancelled = True
        else:
            # Too late for a refund, or the gateway refused: the patient still
            # asked to cancel, so cancel. Never leave a slot blocked because
            # the money could not be moved.
            cancelled = await db_cancel(clinic["id"], appointment_id)

        if cancelled:
            await payment_service.notify_cancellation_outcome(
                booking, refund, clinic=clinic
            )
        return cancelled, refund

    async def _handle_cancel_request(
        self, clinic: dict, phone: str, patient: dict, lang: str
    ) -> None:
        """Handle appointment cancellation request.

        Only shows today's and future appointments — past-date bookings are
        excluded even if they still carry 'confirmed' status in the DB.
        Includes both confirmed and pending_payment bookings.
        """
        from app.database import get_patient_appointments
        from datetime import date as date_mod

        today = date_mod.today().isoformat()  # YYYY-MM-DD

        confirmed = await get_patient_appointments(
            clinic["id"], phone, status="confirmed", from_date=today
        )
        pending = await get_patient_appointments(
            clinic["id"], phone, status="pending_payment", from_date=today
        )
        appointments = confirmed + pending

        if not appointments:
            no_appt_msg = {
                "en": "You don't have any upcoming appointments to cancel.",
                "hi": "रद्द करने के लिए कोई आगामी अपॉइंटमेंट नहीं है।",
                "te": "రద్దు చేయడానికి రాబోయే అపాయింట్‌మెంట్‌లు లేవు.",
            }.get(lang, "You don't have any upcoming appointments to cancel.")
            await self.whatsapp.send_text(clinic, phone, no_appt_msg)
            await self._send_main_menu(clinic, phone, lang)
            return

        # Build interactive list with improved date labels
        rows = []
        # Full list; the send path caps and reports any overflow.
        for appt in appointments:
            appt_date = appt.get("appointment_date", "")
            date_label = "Today" if appt_date == today else appt_date
            status_label = (appt.get("status") or "").replace("_", " ").title()
            # A lab-test booking stores doctor_name as NULL (migration 039), and
            # .get(key, default) returns that None rather than the default, so
            # slicing it raised TypeError and "cancel" went unanswered for
            # every patient holding a lab booking.
            title = appt.get("doctor_name") or appt.get("lab_test_name") or "Booking"
            rows.append(
                {
                    "id": f"cancel_{appt['id']}",
                    "title": title[:24],
                    "description": f"{date_label} {format_slot_time(appt.get('appointment_time', ''))} · {status_label}"[
                        :72
                    ],
                }
            )

        sections = [{"title": "Select to Cancel", "rows": rows}]

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body={
                "en": "Which booking would you like to cancel?",
                "hi": "आप कौन सी बुकिंग रद्द करना चाहते हैं?",
                "te": "మీరు ఏ బుకింగ్ రద్దు చేయాలనుకుంటున్నారు?",
            }.get(lang, "Which booking would you like to cancel?"),
            button_text={"en": "Select", "hi": "चुनें", "te": "ఎంచుకోండి"}.get(lang, "Select"),
            sections=sections,
        )

    async def _handle_reschedule_request(
        self, clinic: dict, phone: str, patient: dict, lang: str
    ) -> None:
        """Handle reschedule request."""
        await self.whatsapp.send_text(
            clinic,
            phone,
            "To reschedule, please call us directly: " + clinic["whatsapp_number"],
        )
        await self._send_main_menu(clinic, phone, lang)

    async def _handle_view_reports(self, clinic: dict, phone: str, lang: str) -> None:
        """Answer a report request without offering a report archive.

        Kriya pushes each report the moment the lab releases it; patients have
        no self-service list to browse. The handler stays because the removed
        "My Reports" row remains tappable in every patient's chat history, and
        because "my reports" / "lab report" still classify as view_reports —
        both must land somewhere honest instead of falling through to the LLM.
        """
        from app.services.tenant import has_feature

        if has_feature(clinic, "lab_reports"):
            en = (
                "📋 Your lab reports are sent to you here on WhatsApp "
                "automatically, as soon as the lab releases them — there is "
                "nothing to request.\n\nFor an older report, please contact "
                "the reception."
            )
            body = {
                "en": en,
                "hi": (
                    "📋 आपकी लैब रिपोर्ट लैब से जारी होते ही अपने आप यहीं WhatsApp "
                    "पर भेज दी जाती है — आपको कुछ मांगने की ज़रूरत नहीं।\n\n"
                    "पुरानी रिपोर्ट के लिए कृपया रिसेप्शन से संपर्क करें।"
                ),
                "te": (
                    "📋 మీ ల్యాబ్ రిపోర్టులు ల్యాబ్ విడుదల చేసిన వెంటనే ఇక్కడే "
                    "WhatsAppలో మీకు ఆటోమేటిక్‌గా పంపబడతాయి — మీరు అడగాల్సిన అవసరం "
                    "లేదు.\n\nపాత రిపోర్ట్ కోసం దయచేసి రిసెప్షన్‌ను సంప్రదించండి."
                ),
            }.get(lang, en)
        else:
            body = (
                "Lab report delivery is not available at this facility via WhatsApp. "
                "Please visit the hospital reception to collect your reports."
            )

        await self.whatsapp.send_text(clinic, phone, body)
        # Reached from the global escape hatch, so the patient may have been
        # mid-booking. Reset before showing the menu, or their next tap is
        # routed by a state that no longer matches what is on their screen.
        await self.update_state(clinic, phone, "main_menu", {"menu_shown": False})
        await self._send_main_menu(clinic, phone, lang)

    #: Above this many tests, paging 9-at-a-time is unusable (a 1000-test
    #: catalogue is 110 taps deep), so the list is introduced as searchable.
    LAB_SEARCH_HINT_THRESHOLD = 20

    #: Typed while browsing the catalogue, these mean "show me everything
    #: again", not "search for this". Everything else typed is a search term.
    LAB_SEARCH_RESET_WORDS = frozenset(
        {"all", "all tests", "list", "show all", "सभी", "पूरी सूची", "అన్నీ"}
    )

    #: ...and these mean "get me out of here". Without them a patient typing
    #: "cancel" would be searched for a test named "cancel" and land back on
    #: the very list they were trying to leave.
    LAB_SEARCH_EXIT_WORDS = NAV_KEYWORDS | {"cancel", "back", "exit", "stop"}

    #: Heading shown for catalogue rows carrying no category (migration 080) --
    #: which is every row of every catalogue imported before it. A centre that
    #: never files its tests therefore has exactly ONE heading, and the
    #: category step below never appears for it.
    LAB_UNCATEGORISED_LABEL = "Lab Tests"

    @staticmethod
    def _group_lab_tests_by_category(
        tests: list[dict],
    ) -> list[tuple[str, list[dict]]]:
        """Tests grouped under their category heading, largest group first.

        Grouped case-insensitively on the stored text: "Radiology" typed once
        as "radiology" in one CSV must not split a centre's imaging menu into
        two headings. The first spelling seen is the one displayed.

        Largest group first because a centre's pathology list outranks its
        three-item packages menu; ties fall back to the heading name so the
        order never reshuffles between two taps of the same list.
        """
        groups: dict[str, tuple[str, list[dict]]] = {}
        for t in tests:
            label = (t.get("category") or "").strip() or ConversationManager.LAB_UNCATEGORISED_LABEL
            key = label.lower()
            if key not in groups:
                groups[key] = (label, [])
            groups[key][1].append(t)
        return sorted(groups.values(), key=lambda g: (-len(g[1]), g[0].lower()))

    @staticmethod
    def _match_lab_tests(tests: list[dict], query: str) -> list[dict]:
        """Tests whose name contains every whitespace-separated term in `query`.

        All-terms-contained rather than the raw phrase, so "thyroid profile"
        finds "PROFILE - THYROID T3 T4 TSH" and "urine sodium" still finds
        "24 Hrs URINE SODIUM". Best matches are ordered first so the answer
        lands on page 1 instead of behind a "More options" tap.
        """
        # Tier 1: All-terms-contained exact substring matching (unchanged)
        terms = [t for t in query.strip().lower().split() if t]
        if not terms:
            return list(tests)
        joined = " ".join(terms)
        matched = [
            t
            for t in tests
            if all(term in (t.get("name") or "").lower() for term in terms)
        ]
        if matched:
            matched.sort(
                key=lambda t: (
                    not (t.get("name") or "").lower().startswith(joined),
                    len(t.get("name") or ""),
                    (t.get("name") or "").lower(),
                )
            )
            return matched

        # Tier 2 & 3: Multilingual synonyms and difflib typo tolerance (runs only when Tier 1 is empty)
        try:
            from app.services.hybrid_search import multilingual_synonym_search
            hits = multilingual_synonym_search(tests, query)
        except Exception:
            hits = []
        if hits or "," not in query:
            return hits

        # "sugar, thyroid" -- two things asked about at once (the interpreted
        # query _show_lab_test_list stores). Each part is searched on its own,
        # in order, rather than hunting for one test that is both.
        seen: set = set()
        union: list[dict] = []
        for part in query.split(","):
            if not part.strip():
                continue
            for t in ConversationManager._match_lab_tests(tests, part):
                if id(t) not in seen:
                    seen.add(id(t))
                    union.append(t)
        return union

    async def _interpret_lab_query(
        self, clinic: dict, tests: list[dict], query: str
    ) -> tuple[list[dict], str]:
        """Read a sentence typed into the search when the words themselves
        matched nothing. Returns (tests, the query they matched), or
        ([], query) to fall back exactly as before.

        1. Drop the conversational words: "I have sugar, what test can I do"
           is a search for "sugar". Free, and works with the LLM down.
        2. Still nothing and it reads like a sentence: ask the model which
           test, organ or condition was NAMED (never inferred from a
           symptom), in any language, and look those up.

        Either way every row shown is a test this centre actually sells.
        """
        from app.services.ai_engine import extract_catalogue_terms
        from app.services.hybrid_search import strip_query_filler

        cleaned = strip_query_filler(query)
        if cleaned and cleaned != query.strip().lower():
            hits = self._match_lab_tests(tests, cleaned)
            if hits:
                return hits, cleaned

        # Single words and typos are the fuzzy tier's job; the model is only
        # worth a call for a sentence.
        if len(query.split()) >= self.LAB_INTERPRET_MIN_WORDS:
            terms = await extract_catalogue_terms(query, clinic)
            if terms:
                joined = ", ".join(terms)
                hits = self._match_lab_tests(tests, joined)
                if hits:
                    return hits, joined
        return [], query

    #: Words a no-match search needs before the model is asked to read it.
    LAB_INTERPRET_MIN_WORDS = 3

    async def _show_lab_test_list(
        self,
        clinic: dict,
        phone: str,
        context: dict,
        lang: str,
        page: int = 0,
        query: Optional[str] = None,
    ) -> None:
        """Fetch active lab tests for this clinic/branch and display as interactive list."""
        from app.database import get_lab_tests

        branch_id = context.get("branch_id")
        tests = await get_lab_tests(clinic["id"], branch_id=branch_id, active_only=True)

        if not tests:
            msg = {
                "en": "No lab tests are currently available for online booking. Please call our center directly.",
                "hi": "वर्तमान में ऑनलाइन बुकिंग के लिए कोई लैब टेस्ट उपलब्ध नहीं है। कृपया सीधे हमारे केंद्र पर कॉल करें।",
                "te": "ఆన్‌లైన్ బుకింగ్ కోసం ప్రస్తుతం ల్యాబ్ పరీక్షలు అందుబాటులో లేవు. దయచేసి మా కేంద్రాన్ని నేరుగా సంప్రదించండి.",
            }.get(lang, "No lab tests are currently available for online booking.")
            await self.whatsapp.send_text(clinic, phone, msg)
            await self._send_main_menu(clinic, phone, lang)
            return

        # A diagnostic centre does not sell one flat list of blood tests: it
        # sells pathology, health packages, radiology and scans, at wildly
        # different prices. Asking WHICH KIND first is the difference between a
        # 1,392-row list and a four-row one. The headings are whatever the
        # catalogue's own category column carries, so a centre that does no
        # imaging has no imaging heading and there is nothing to switch off.
        groups = self._group_lab_tests_by_category(tests)
        category = (context.get("lab_category") or "").strip() or None
        if category:
            picked = next(
                (rows for label, rows in groups if label.lower() == category.lower()),
                None,
            )
            if picked is None:
                # The heading was renamed, or its last test deleted, while the
                # patient held the list open. Fall back to the headings rather
                # than showing them an empty catalogue.
                context.pop("lab_category", None)
                category = None
            else:
                tests = picked

        # One heading -- every catalogue that existed before migration 080 --
        # skips this step entirely and behaves exactly as it always did.
        if category is None and not (query or "").strip() and len(groups) > 1:
            await self._show_lab_category_list(clinic, phone, context, lang, groups)
            return

        # Capped because the query is echoed back into the list body, and
        # Meta rejects the whole message over 1024 characters -- a pasted
        # paragraph would otherwise take the patient's list away entirely.
        # The whole question is read when it has to be interpreted; only the
        # echo is capped.
        full_query = (query or "").strip()[:300]
        query = full_query[:60].strip() or None
        shown = tests
        # True when the list answers what the patient MEANT rather than the
        # words they typed -- the body then says these are options, not advice.
        interpreted = False
        if query:
            shown = self._match_lab_tests(tests, query)
            if not shown:
                shown, matched_query = await self._interpret_lab_query(clinic, tests, full_query)
                if shown:
                    query, interpreted = matched_query[:60].strip(), True
            if not shown:
                # Falling back to the unfiltered catalogue is the only exit
                # that does not dead-end a patient who mistyped a test name.
                if len(query.split()) >= self.LAB_INTERPRET_MIN_WORDS:
                    # A sentence nothing in the catalogue answers -- usually
                    # a symptom. Which test suits it is a doctor's call.
                    no_match = {
                        "en": "I couldn't find a test by that name. I can't advise which test you need — a doctor can. Type a test name (e.g. \"thyroid\", \"CBC\"), pick from the list below, or type *talk to staff*.",
                        "hi": "इस नाम से कोई टेस्ट नहीं मिला। कौन सा टेस्ट चाहिए, यह डॉक्टर बता सकते हैं — मैं सलाह नहीं दे सकता। टेस्ट का नाम लिखें (जैसे \"thyroid\"), नीचे सूची से चुनें, या *talk to staff* लिखें।",
                        "te": "ఆ పేరుతో పరీక్ష దొరకలేదు. మీకు ఏ పరీక్ష అవసరమో డాక్టర్ చెప్పగలరు — నేను సలహా ఇవ్వలేను. పరీక్ష పేరు టైప్ చేయండి (ఉదా. \"thyroid\"), క్రింది జాబితా నుండి ఎంచుకోండి, లేదా *talk to staff* అని టైప్ చేయండి.",
                    }
                else:
                    no_match = {
                        "en": f'No test matched "{query}". Showing the full list — try a shorter word, like "thyroid" or "urine".',
                        "hi": f'"{query}" से कोई टेस्ट नहीं मिला। पूरी सूची दिखा रहे हैं — छोटा शब्द आज़माएँ, जैसे "thyroid"।',
                        "te": f'"{query}" కి పరీక్ష దొరకలేదు. పూర్తి జాబితా చూపిస్తున్నాం — చిన్న పదం ప్రయత్నించండి, ఉదా. "thyroid".',
                    }
                await self.whatsapp.send_text(clinic, phone, no_match.get(lang, no_match["en"]))
                shown, query, page = tests, None, 0

        all_rows = []
        for t in shown:
            name = t["name"]
            price_str = f"₹{t['price_paise'] // 100}"
            # Meta truncates a row title at 24 characters. 57 groups in a real
            # 1,392-test catalogue share their first 24, and 21 of those match
            # on price and sample type too -- so the patient saw the same row
            # twice ("17-HYDROXYPROGESTERONE (") with nothing to choose
            # between them. The cut-off tail is exactly what tells them apart,
            # so it leads the description, which Meta allows 72 characters for.
            if len(name) > 24:
                detail = f"…{name[24:]}"
            elif t.get("sample_type"):
                detail = t["sample_type"]
            else:
                detail = ""
            desc = f"{price_str} • {detail}" if detail else price_str
            all_rows.append({
                "id": f"labtest_{t['id']}",
                "title": name[:24],
                "description": desc[:72],
            })

        # A patient who has just been shown 73 search hits has no way back: the
        # list is the whole screen and typing again only searches again. These
        # ride along on every page, and the content page shrinks by exactly as
        # many rows so the total stays inside Meta's 10-row cap.
        nav_rows = []
        if len(groups) > 1:
            nav_rows.append({
                "id": "labcat_all",
                "title": {
                    "en": "⬅️ All services",
                    "hi": "⬅️ सभी सेवाएं",
                    "te": "⬅️ అన్ని సేవలు",
                }.get(lang, "⬅️ All services")[:24],
                "description": {
                    "en": "Back to service types",
                    "hi": "सेवा प्रकार पर वापस",
                    "te": "సేవల రకాలకు తిరిగి",
                }.get(lang, "Back to service types")[:72],
            })
        nav_rows.append({
            "id": "lab_menu",
            "title": {
                "en": "🏠 Main Menu",
                "hi": "🏠 मुख्य मेनू",
                "te": "🏠 మెనూ",
            }.get(lang, "🏠 Main Menu")[:24],
            "description": {
                "en": "Emergency, staff and more",
                "hi": "आपात, स्टाफ और अधिक",
                "te": "అత్యవసరం, సిబ్బంది",
            }.get(lang, "Emergency, staff and more")[:72],
        })

        rows, page = self._page_rows(
            all_rows, page, "labtest_more", lang,
            page_size=self.LIST_PAGE_SIZE - len(nav_rows),
        )
        rows = rows + nav_rows

        if query:
            body = {
                "en": f'🔍 {len(shown)} test(s) matching "{query}".\n\nTap below to pick one, type another name to search again, or send "all" for the full list.',
                "hi": f'🔍 "{query}" से मिलते {len(shown)} टेस्ट।\n\nनीचे टैप करके चुनें, दूसरा नाम टाइप करके फिर खोजें, या पूरी सूची के लिए "all" भेजें।',
                "te": f'🔍 "{query}" కి సరిపోయే {len(shown)} పరీక్షలు.\n\nక్రింద ట్యాప్ చేసి ఎంచుకోండి, మళ్లీ వెతకడానికి మరో పేరు టైప్ చేయండి, లేదా పూర్తి జాబితాకు "all" పంపండి.',
            }.get(lang, f'{len(shown)} test(s) matching "{query}".')
            if interpreted:
                body += {
                    "en": "\n\nℹ️ These are the tests we offer for what you mentioned. Only your doctor can advise which one you need.",
                    "hi": "\n\nℹ️ आपने जो बताया, उससे जुड़े हमारे टेस्ट ये हैं। कौन सा टेस्ट आपके लिए सही है, यह केवल आपके डॉक्टर बता सकते हैं।",
                    "te": "\n\nℹ️ మీరు చెప్పినదానికి సంబంధించి మా వద్ద ఉన్న పరీక్షలు ఇవి. మీకు ఏది అవసరమో మీ డాక్టర్ మాత్రమే చెప్పగలరు.",
                }.get(lang, "")
        elif len(tests) > self.LAB_SEARCH_HINT_THRESHOLD:
            # Lead with the question, not the catalogue size. A 1,392-test
            # menu is 155 taps deep at 9 rows a page, so browsing is the
            # fallback here and typing is the path: the button below says so
            # too, rather than inviting a tap that cannot finish the job.
            body = {
                "en": f'🧪 *Which test would you like to book?*\n\nType the test name — e.g. "thyroid", "widal" or "vitamin d".\nWe offer {len(tests)} tests, so searching is quicker than browsing.',
                "hi": f'🧪 *आप कौन सा टेस्ट बुक करना चाहते हैं?*\n\nटेस्ट का नाम टाइप करें — जैसे "thyroid", "widal" या "vitamin d"।\nहमारे पास {len(tests)} टेस्ट हैं, इसलिए खोजना ज़्यादा आसान है।',
                "te": f'🧪 *మీరు ఏ పరీక్ష బుక్ చేయాలనుకుంటున్నారు?*\n\nపరీక్ష పేరు టైప్ చేయండి — ఉదా. "thyroid", "widal" లేదా "vitamin d".\nమా వద్ద {len(tests)} పరీక్షలు ఉన్నాయి, కాబట్టి వెతకడం సులభం.',
            }.get(
                lang,
                f"Which test would you like to book? Type the test name. "
                f"We offer {len(tests)} tests.",
            )
        else:
            body = {
                "en": "Select a lab test to book your sample collection:",
                "hi": "सैंपल कलेक्शन बुक करने के लिए लैब टेस्ट चुनें:",
                "te": "శాంపిల్ కలెక్షన్ బుక్ చేసుకోవడానికి ల్యాబ్ పరీక్షను ఎంచుకోండి:",
            }.get(lang, "Select a lab test:")

        # Meta caps the button label at 20 characters. "Browse all tests" tells
        # a patient facing a four-figure catalogue that the tap is the long way
        # round, where the old "View Tests" implied it was the only way.
        if query:
            button_text = {
                "en": "View results",
                "hi": "परिणाम देखें",
                "te": "ఫలితాలు చూడండి",
            }.get(lang, "View results")
        elif len(tests) > self.LAB_SEARCH_HINT_THRESHOLD:
            button_text = {
                "en": "Browse all tests",
                "hi": "सभी टेस्ट देखें",
                "te": "అన్ని పరీక్షలు",
            }.get(lang, "Browse all tests")
        else:
            button_text = {
                "en": "View Tests",
                "hi": "टेस्ट देखें",
                "te": "పరీక్షలు చూడండి",
            }.get(lang, "View Tests")

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body=body,
            button_text=button_text,
            sections=[
                {
                    "title": (
                        "Search Results" if query
                        else (category or "Available Tests")
                    )[:24],
                    "rows": rows,
                }
            ],
        )
        context["lab_test_page"] = page
        # Persisted so "More options" pages within the search result set
        # instead of silently jumping back to the full catalogue.
        context["lab_test_query"] = query
        # Persisted for the same reason: without it "More options" on page 2 of
        # Radiology would page through the whole catalogue instead.
        if category:
            context["lab_category"] = category
        else:
            context.pop("lab_category", None)
        await self.update_state(clinic, phone, "browsing_lab_tests", context)

    async def _show_lab_category_list(
        self,
        clinic: dict,
        phone: str,
        context: dict,
        lang: str,
        groups: list[tuple[str, list[dict]]],
        page: int = 0,
    ) -> None:
        """Ask which KIND of service before asking which test.

        Only reached when the centre's catalogue carries more than one heading
        (see _group_lab_tests_by_category). Headings are read off the rows
        themselves rather than configured anywhere, so an admin who imports a
        radiology CSV gets a Radiology heading in the bot on the next tap, and
        one who offers no health packages never shows that heading at all.
        """
        all_rows = [
            {
                "id": f"labcat_{i}",
                "title": self._lab_heading_title(label),
                "description": {
                    "en": f"{len(rows)} available",
                    "hi": f"{len(rows)} उपलब्ध",
                    "te": f"{len(rows)} అందుబాటులో",
                }.get(lang, f"{len(rows)} available")[:72],
            }
            # Indexed over the FULL heading list, not the page, so a row id
            # stays valid after the patient taps "More options".
            for i, (label, rows) in enumerate(groups)
        ]

        rows, page = self._page_rows(
            all_rows, page, "labcat_more", lang,
            page_size=self.LIST_PAGE_SIZE - 1,
        )
        rows = rows + [{
            "id": "lab_menu",
            "title": {
                "en": "🏠 Main Menu",
                "hi": "🏠 मुख्य मेनू",
                "te": "🏠 మెనూ",
            }.get(lang, "🏠 Main Menu")[:24],
            "description": {
                "en": "Emergency, staff and more",
                "hi": "आपात, स्टाफ और अधिक",
                "te": "అత్యవసరం, సిబ్బంది",
            }.get(lang, "Emergency, staff and more")[:72],
        }]

        body = {
            "en": "🏥 *What would you like to book?*\n\nChoose the kind of service below — or just type what you need, e.g. \"thyroid\", \"MRI brain\" or \"full body checkup\".",
            "hi": "🏥 *आप क्या बुक करना चाहते हैं?*\n\nनीचे सेवा का प्रकार चुनें — या सीधे नाम टाइप करें, जैसे \"thyroid\" या \"MRI brain\"।",
            "te": "🏥 *మీరు ఏది బుక్ చేయాలనుకుంటున్నారు?*\n\nక్రింద సేవ రకం ఎంచుకోండి — లేదా పేరు టైప్ చేయండి, ఉదా. \"thyroid\" లేదా \"MRI brain\".",
        }.get(lang, "What would you like to book? Choose a service type below, or type what you need.")

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body=body,
            button_text={
                "en": "Choose service",
                "hi": "सेवा चुनें",
                "te": "సేవ ఎంచుకోండి",
            }.get(lang, "Choose service")[:20],
            sections=[{"title": "Services"[:24], "rows": rows}],
        )

        context["lab_categories"] = [label for label, _ in groups]
        context["lab_cat_page"] = page
        # Standing on the headings means no test list is open; leaving the old
        # page/query behind would make the next "More options" page a list the
        # patient can no longer see.
        context.pop("lab_category", None)
        context.pop("lab_test_page", None)
        context.pop("lab_test_query", None)
        await self.update_state(clinic, phone, "browsing_lab_tests", context)

    def _next_collection_dates(self, allowed_days_str: str, count: int = 3) -> list[str]:
        """Compute the next `count` calendar dates (YYYY-MM-DD) whose weekday is in allowed_days_str."""
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        allowed = {d.strip() for d in allowed_days_str.split(",") if d.strip()}
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        out = []
        cur = datetime.now(IST).date() + timedelta(days=1)  # start tomorrow in IST
        while len(out) < count:
            if day_names[cur.weekday()] in allowed:
                out.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
        return out

    async def _handle_browsing_lab_tests(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Handle patient picking a lab test from the interactive list."""
        from app.database import (
            get_lab_test_by_id,
            get_lab_collection_window,
            format_collection_window,
            get_lab_tests,
        )

        button_id = interactive_data.get("id", "") if interactive_data else ""

        # Deliberately not "labtest_menu": that prefix is parsed as a test id
        # a few lines down, and a test called "menu" is not the point.
        if button_id == "lab_menu":
            await self.update_state(clinic, phone, "main_menu", {"menu_shown": False})
            await self._send_main_menu(clinic, phone, lang)
            return

        if button_id.startswith("labcat_"):
            suffix = button_id.removeprefix("labcat_")
            # A stale heading list stays tappable forever, so every branch here
            # re-reads the catalogue rather than trusting what the row says.
            if suffix == "more":
                catalogue = await get_lab_tests(
                    clinic["id"], branch_id=context.get("branch_id"), active_only=True
                )
                await self._show_lab_category_list(
                    clinic, phone, context, lang,
                    self._group_lab_tests_by_category(catalogue),
                    page=int(context.get("lab_cat_page") or 0) + 1,
                )
                return
            categories = context.get("lab_categories") or []
            context.pop("lab_test_page", None)
            context.pop("lab_test_query", None)
            if suffix.isdigit() and int(suffix) < len(categories):
                context["lab_category"] = categories[int(suffix)]
                # "How many patients looked at Health Packages" -- the interest
                # half of the Insights conversion figure.
                await log_analytics_event(
                    clinic["id"], phone, "lab_category_viewed",
                    metadata={"category": context["lab_category"], "source": "list"},
                )
            else:
                # "All services", or a row from a list built before the
                # catalogue was re-filed: show the headings again.
                context.pop("lab_category", None)
            await self._show_lab_test_list(clinic, phone, context, lang)
            return

        # Before the labtest_ prefix match: "labtest_more" would otherwise be
        # read as a test id of "more".
        if button_id == "labtest_more":
            await self._show_lab_test_list(
                clinic, phone, context, lang,
                page=int(context.get("lab_test_page") or 0) + 1,
                query=context.get("lab_test_query"),
            )
            return

        selected_id = None
        if button_id.startswith("labtest_"):
            selected_id = button_id.removeprefix("labtest_")

        if not selected_id:
            # Patient typed instead of tapping a row. Re-presenting the same
            # first page was a dead end for a catalogue of hundreds of tests
            # ("More options" 100+ times), so the text is a search query.
            typed = (message or "").strip()
            typed_lower = typed.lower()
            # A greeting is a patient starting over, never a test name: "Hi"
            # used to come back as "73 test(s) matching 'Hi'" (THIAMINE,
            # CHIKUNGUNYA...), because this state is not one the global
            # greeting-to-menu rule covers.
            if typed_lower in self.LAB_SEARCH_EXIT_WORDS or is_greeting(typed) or intent == "greeting":
                await self.update_state(clinic, phone, "main_menu", {"menu_shown": False})
                await self._send_main_menu(clinic, phone, lang)
            elif typed and typed_lower not in self.LAB_SEARCH_RESET_WORDS:
                await self._show_lab_test_list(
                    clinic, phone, context, lang, query=typed
                )
            else:
                await self._show_lab_test_list(clinic, phone, context, lang)
            return

        test = await get_lab_test_by_id(
            clinic["id"], selected_id, branch_id=context.get("branch_id")
        )
        if not test or not test.get("is_active"):
            msg = {
                "en": "That test is no longer available. Please pick another.",
                "hi": "वह टेस्ट अब उपलब्ध नहीं है। कृपया दूसरा चुनें।",
                "te": "ఆ పరీక్ష ఇప్పుడు అందుబాటులో లేదు. దయచేసి మరొకటి ఎంచుకోండి.",
            }.get(lang, "That test is no longer available.")
            await self.whatsapp.send_text(clinic, phone, msg)
            await self._show_lab_test_list(clinic, phone, context, lang)
            return

        # Stash test details in conversation context. A new test starts the
        # date/who/name steps afresh.
        context.update(_LAB_STEP_CLEARED)
        context["lab_test_id"] = test["id"]
        context["lab_test_name"] = test["name"]
        context["lab_test_price_paise"] = test["price_paise"]
        context["lab_test_fasting_required"] = test.get("fasting_required", False)
        context["lab_test_prep_instructions"] = test.get("prep_instructions")
        context["lab_test_turnaround_hours"] = test.get("turnaround_hours")
        # Interest that did not become a booking is exactly what Insights
        # shows a centre ("opened 40 times, booked 6").
        await log_analytics_event(
            clinic["id"], phone, "lab_test_viewed",
            metadata={
                "test_id": str(test["id"]),
                "test_name": test["name"],
                "category": (test.get("category") or "").strip() or self.LAB_UNCATEGORISED_LABEL,
            },
        )

        # Fetch collection window for branch or clinic
        window = await get_lab_collection_window(clinic, branch_id=context.get("branch_id"))
        dates = self._next_collection_dates(window.get("days", "Mon,Tue,Wed,Thu,Fri,Sat,Sun"), count=3)

        # Build date selection buttons
        buttons = []
        for d in dates:
            dt = datetime.strptime(d, "%Y-%m-%d")
            buttons.append({
                "id": f"labdate_{d}",
                "title": dt.strftime("%a, %d %b")[:20],
            })

        # Format test summary + instructions
        price_rupees = test["price_paise"] // 100
        instructions_line = ""
        # migration 083: what a health package includes, what a scan covers.
        # .get() -- rows read before the column exists simply have none.
        details = (test.get("description") or "").strip()
        if details:
            instructions_line += f"\n📝 *Details:* {details[:400]}"
        if test.get("fasting_required"):
            # += : plain = here threw away the package's Details line above.
            instructions_line += "\n⚠️ *Fasting Required:* 10-12 hours fasting before collection."
        if test.get("prep_instructions"):
            instructions_line += f"\n📋 *Prep:* {test['prep_instructions']}"

        body = (
            f"*{test['name']}*\n"
            f"💰 Price: ₹{price_rupees}\n"
            f"⏱️ Turnaround: {test.get('turnaround_hours') or 24} hours\n"
            f"🏠 Collection window: {format_collection_window(window)}"
            f"{instructions_line}\n\n"
            f"Please choose your preferred sample collection date:"
        )

        await self.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=body,
            buttons=buttons,
        )
        await self.update_state(clinic, phone, "confirming_collection_date", context)

    async def _handle_confirming_collection_date(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        patient: Optional[dict],
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Collection date, then who the test is for, then the booking.

        Three steps inside one state, told apart by context["lab_step"]:
          (no date yet) -> date buttons
          "who"         -> For Me / Someone Else buttons
          "name"        -> the patient's full name, typed
        The booking used to be written the moment a date was tapped, under
        the account holder's saved name or the literal "Patient" -- so the
        centre never learned who was coming, and a test booked for a parent
        carried nobody's name at all.
        """
        from app.utils.validators import validate_name

        button_id = interactive_data.get("id", "") if interactive_data else ""

        # A date tap is honoured at any step, so re-picking the date works.
        if button_id.startswith("labdate_"):
            context["lab_collection_date"] = button_id.removeprefix("labdate_")
            await self._ask_lab_test_patient(clinic, phone, context, patient, lang)
            return

        selected_date = context.get("lab_collection_date")
        if not selected_date:
            msg = {
                "en": "Please tap one of the date buttons above to continue.",
                "hi": "आगे बढ़ने के लिए कृपया ऊपर दिए गए तारीख बटन में से एक पर टैप करें।",
                "te": "కొనసాగడానికి దయచేసి పైన ఉన్న తేదీ బటన్‌లలో ఒకదాన్ని నొక్కండి.",
            }.get(lang, "Please tap one of the date buttons above.")
            await self.whatsapp.send_text(clinic, phone, msg)
            return

        if button_id in ("labfor_self", "labfor_other"):
            for_self = button_id == "labfor_self"
            saved_name = ((patient or {}).get("name") or "").strip()
            if for_self and saved_name:
                await self._finalize_lab_booking(
                    clinic, phone, context, patient, lang, selected_date, saved_name
                )
                return
            context["lab_step"] = "name"
            context["lab_for_self"] = for_self
            await self.whatsapp.send_text(clinic, phone, get_message("ask_name", lang))
            await self.update_state(clinic, phone, "confirming_collection_date", context)
            return

        # A saved family member, indexed into the list the patient was shown.
        if button_id.startswith("labfor_fam_"):
            family = context.get("lab_family") or []
            idx = button_id.removeprefix("labfor_fam_")
            if idx.isdigit() and int(idx) < len(family):
                await self._finalize_lab_booking(
                    clinic, phone, context, patient, lang, selected_date, family[int(idx)]
                )
                return
            # A list from before the family list changed: ask again.
            await self._ask_lab_test_patient(clinic, phone, context, patient, lang)
            return

        if button_id in ("labsave_yes", "labsave_no") and context.get("lab_pending_name"):
            name = context["lab_pending_name"]
            if button_id == "labsave_yes":
                await add_family_member(clinic["id"], phone, full_name=name)
            await self._finalize_lab_booking(
                clinic, phone, context, patient, lang, selected_date, name
            )
            return

        if context.get("lab_step") == "name" and not interactive_data:
            is_valid, result = validate_name(message or "")
            if not is_valid:
                await self._send_name_error(clinic, phone, result, lang)
                return
            if context.get("lab_for_self"):
                await update_patient(clinic["id"], phone, {"name": result})
            elif result.casefold() not in {n.casefold() for n in context.get("lab_family") or []}:
                # Someone new: one tap to keep them for next time. Both
                # buttons book, so paid and counter centres alike continue.
                context["lab_step"] = "save"
                context["lab_pending_name"] = result
                await self._ask_save_lab_family_member(clinic, phone, context, lang)
                return
            await self._finalize_lab_booking(
                clinic, phone, context, patient, lang, selected_date, result
            )
            return

        if context.get("lab_step") == "save" and context.get("lab_pending_name"):
            await self._ask_save_lab_family_member(clinic, phone, context, lang)
            return

        # Anything else at the "who" step: ask again.
        await self._ask_lab_test_patient(clinic, phone, context, patient, lang)

    async def _ask_save_lab_family_member(
        self, clinic: dict, phone: str, context: dict, lang: str
    ) -> None:
        """Offer to keep a newly typed name in the family list."""
        name = context["lab_pending_name"]
        body = {
            "en": f"Save *{name}* to your family list, so you can pick them next time?",
            "hi": f"क्या *{name}* को अपनी फैमिली सूची में सेव करें, ताकि अगली बार सीधे चुन सकें?",
            "te": f"*{name}* ను మీ కుటుంబ జాబితాలో సేవ్ చేయాలా? తదుపరి సారి నేరుగా ఎంచుకోవచ్చు.",
        }.get(lang, f"Save {name} to your family list?")
        await self.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=body,
            buttons=[
                {"id": "labsave_yes", "title": {
                    "en": "Save & Book", "hi": "सेव करके बुक करें", "te": "సేవ్ చేసి బుక్",
                }.get(lang, "Save & Book")},
                {"id": "labsave_no", "title": {
                    "en": "Just Book", "hi": "सिर्फ बुक करें", "te": "బుక్ మాత్రమే",
                }.get(lang, "Just Book")},
            ],
        )
        await self.update_state(clinic, phone, "confirming_collection_date", context)

    @staticmethod
    def _collection_hours_on(window: dict, date_str: str, lang: str) -> str:
        """Collection hours for the ONE date booked. The confirmation used to
        quote the whole week ("07:00 - 21:00 (Sun: 07:00 - 14:00)") even for a
        Sunday booking, leaving the patient to work out which applied."""
        start, end = window.get("start", "07:00"), window.get("end", "11:00")
        try:
            is_sunday = datetime.strptime(date_str, "%Y-%m-%d").weekday() == 6
        except (TypeError, ValueError):
            is_sunday = False
        if is_sunday and window.get("sunday_start") and window.get("sunday_end"):
            start, end = window["sunday_start"], window["sunday_end"]
        label = {
            "en": "Sample collection", "hi": "सैंपल कलेक्शन", "te": "శాంపిల్ కలెక్షన్",
        }.get(lang, "Sample collection")
        return f"{label}: {start} - {end}"

    async def _ask_lab_test_patient(
        self, clinic: dict, phone: str, context: dict, patient: Optional[dict], lang: str
    ) -> None:
        """Ask who the lab test is for, once the collection date is chosen."""
        saved_name = ((patient or {}).get("name") or "").strip()
        first = f", {saved_name.split()[0]}" if saved_name else ""
        body = {
            "en": f"Who is this test for{first}?",
            "hi": f"यह टेस्ट किसके लिए है{first}?",
            "te": f"ఈ పరీక్ష ఎవరి కోసం{first}?",
        }.get(lang, f"Who is this test for{first}?")
        self_row = {"id": "labfor_self", "title": {
            "en": "For Me", "hi": "मेरे लिए", "te": "నా కోసం",
        }.get(lang, "For Me")}
        other_row = {"id": "labfor_other", "title": {
            "en": "Someone Else", "hi": "किसी और के लिए", "te": "వేరొకరి కోసం",
        }.get(lang, "Someone Else")}

        # Meta allows 10 list rows: For Me + 8 family + Someone Else.
        family = [
            (m.get("full_name") or "").strip()
            for m in await get_family_members(clinic["id"], phone)
        ]
        family = [n for n in family if n][:8]
        context["lab_family"] = family

        if family:
            rows = [self_row]
            rows += [
                {"id": f"labfor_fam_{i}", "title": n[:24]} for i, n in enumerate(family)
            ]
            rows.append({**other_row, "title": "+ " + other_row["title"]})
            await self.whatsapp.send_interactive_list(
                clinic,
                phone,
                body=body,
                button_text={"en": "Choose", "hi": "चुनें", "te": "ఎంచుకోండి"}.get(lang, "Choose"),
                sections=[{"title": {
                    "en": "Patient", "hi": "मरीज़", "te": "రోగి",
                }.get(lang, "Patient"), "rows": rows}],
            )
        else:
            await self.whatsapp.send_interactive_buttons(
                clinic, phone, body=body, buttons=[self_row, other_row],
            )
        context["lab_step"] = "who"
        await self.update_state(clinic, phone, "confirming_collection_date", context)

    async def _finalize_lab_booking(
        self,
        clinic: dict,
        phone: str,
        context: dict,
        patient: Optional[dict],
        lang: str,
        selected_date: str,
        patient_name: str,
    ) -> None:
        """Write the lab booking once date and patient name are both known."""
        from app.services.payment import payment_service, resolve_payment_mode

        # A diagnostic centre with no Razorpay keys used to reach
        # create_booking_with_payment anyway, which asked Razorpay for a payment
        # link with empty credentials, took a 401, cancelled the row it had just
        # written and told the patient "We couldn't initialize your booking."
        # Every lab booking at such a centre died there. Consultations have
        # always consulted resolve_payment_mode() and booked directly when a
        # clinic collects at the counter; the lab flow simply never did.
        payment_mode, deposit_percent = resolve_payment_mode(clinic)

        if payment_mode == "none":
            await self._book_lab_test_without_payment(
                clinic, phone, context, patient, lang, selected_date, patient_name
            )
            return

        result = await payment_service.create_booking_with_payment(
            # clinic_id and department are required positionally. Omitting them
            # raised TypeError on every lab-test booking before it could reach
            # the payment service at all, so the whole flow was dead: the
            # patient picked a test and a date, then got the generic failure
            # message. The consultation call site two thousand lines up passes
            # both; this one did not, and no test exercised this call site.
            clinic_id=clinic["id"],
            clinic=clinic,
            patient_phone=phone,
            patient_name=patient_name,
            department="Lab Test",
            doctor_name=None,
            appointment_date=selected_date,
            appointment_time=None,
            booking_type="lab_test",
            lab_test_id=context.get("lab_test_id"),
            lab_test_name=context.get("lab_test_name"),
            branch_id=context.get("branch_id"),
            branch_name=context.get("branch_name"),
            # Both were dropped here: a centre on "partial" was charged the
            # full price, and the booking never linked to the patient record.
            patient_id=(patient or {}).get("id"),
            deposit_percent=deposit_percent,
        )

        if not result.get("success"):
            # create_booking_with_payment reports its cause in "reason"; it has
            # no "error" key, so this line logged a bare None for every failure
            # and the Razorpay 401 behind them was only visible in the
            # payment_events table.
            logger.error(
                f"Failed to create lab test booking for clinic {clinic['id']}: "
                f"{result.get('reason')}"
            )
            err_msg = {
                "en": "We couldn't initialize your booking. Please try again or contact the center.",
                "hi": "हम आपकी बुकिंग शुरू नहीं कर सके। कृपया पुनः प्रयास करें।",
                "te": "మేము మీ బుకింగ్‌ను ప్రారంభించలేకపోయాము. దయచేసి మళ్లీ ప్రయత్నించండి.",
            }.get(lang, "Failed to initialize booking.")
            await self.whatsapp.send_text(clinic, phone, err_msg)
            # Leave the name step, or the next thing typed at the menu would
            # be read as a patient name and retry the booking unasked.
            await self.update_state(
                clinic, phone, "main_menu", {"menu_shown": False, **_LAB_STEP_CLEARED}
            )
            await self._send_main_menu(clinic, phone, lang)
            return

        # Send payment link to patient
        amount_rupees = result["amount_paise"] // 100
        deposit_note = (
            f"_This is a {deposit_percent}% deposit — the remaining "
            f"{100 - deposit_percent}% is payable at the centre._\n\n"
            if payment_mode == "partial" and deposit_percent < 100
            else ""
        )
        pay_msg = (
            f"🧪 *Lab Test Booking Reserved*\n\n"
            f"Test: *{context.get('lab_test_name')}*\n"
            f"Patient: *{patient_name}*\n"
            f"Date: *{selected_date}*\n"
            f"Amount: *₹{amount_rupees}*\n\n"
            f"{deposit_note}"
            f"Please complete your payment within {settings.booking_hold_minutes} minutes to confirm:\n"
            f"{result['payment_link']}\n\n"
            f"Ref: `{result['booking_ref']}`"
        )
        await self.whatsapp.send_text(clinic, phone, pay_msg)

        context["booking_id"] = result["booking_id"]
        context["booking_ref"] = result["booking_ref"]
        context["payment_link"] = result["payment_link"]
        context["hold_expires_at"] = result["hold_expires_at"]
        context.update(_LAB_STEP_CLEARED)

        await self.update_state(clinic, phone, "awaiting_payment", context)

    async def _book_lab_test_without_payment(
        self,
        clinic: dict,
        phone: str,
        context: dict,
        patient: Optional[dict],
        lang: str,
        selected_date: str,
        patient_name: str,
    ) -> None:
        """Confirm a lab test at a centre that collects payment at the counter.

        The counterpart of the consultation flow's direct-booking path, for a
        centre whose payment mode resolves to "none". The test is confirmed
        immediately and the price is quoted as payable on arrival.
        """
        from app.database import get_lab_collection_window

        appointment_data = {
            "patient_id": (patient or {}).get("id"),
            "patient_phone": phone,
            "patient_name": patient_name,
            "department": "Lab Test",
            "doctor_name": None,
            "appointment_date": selected_date,
            "appointment_time": None,
            "status": "confirmed",
            "booking_type": "lab_test",
            "lab_test_id": context.get("lab_test_id"),
            "lab_test_name": context.get("lab_test_name"),
            "amount_paise": context.get("lab_test_price_paise"),
        }
        if context.get("branch_id"):
            appointment_data["branch_id"] = context["branch_id"]
            appointment_data["branch_name"] = context.get("branch_name") or ""

        result = await book_appointment(clinic["id"], appointment_data)

        if not result.get("success"):
            logger.error(
                f"Direct lab test booking failed for clinic {clinic['id']}: "
                f"{result.get('reason')}"
            )
            err_msg = {
                "en": "We couldn't complete your booking. Please try again or contact the center.",
                "hi": "हम आपकी बुकिंग पूरी नहीं कर सके। कृपया पुनः प्रयास करें या केंद्र से संपर्क करें।",
                "te": "మేము మీ బుకింగ్‌ను పూర్తి చేయలేకపోయాము. దయచేసి మళ్లీ ప్రయత్నించండి లేదా కేంద్రాన్ని సంప్రదించండి.",
            }.get(lang, "We couldn't complete your booking. Please try again.")
            await self.whatsapp.send_text(clinic, phone, err_msg)
            await self.update_state(
                clinic, phone, "main_menu", {"menu_shown": False, **_LAB_STEP_CLEARED}
            )
            await self._send_main_menu(clinic, phone, lang)
            return

        appointment = result["appointment"]
        date_display = datetime.strptime(selected_date, "%Y-%m-%d").strftime("%a, %d %b %Y")

        price_line = ""
        if context.get("lab_test_price_paise"):
            rupees = context["lab_test_price_paise"] // 100
            price_line = {
                "en": f"\n💰 ₹{rupees} — payable at the centre",
                "hi": f"\n💰 ₹{rupees} — केंद्र पर देय",
                "te": f"\n💰 ₹{rupees} — కేంద్రంలో చెల్లించాలి",
            }.get(lang, f"\n💰 ₹{rupees} — payable at the centre")

        window_line = ""
        try:
            window = await get_lab_collection_window(
                clinic, branch_id=context.get("branch_id")
            )
            window_line = f"\n🏠 {self._collection_hours_on(window, selected_date, lang)}"
        except Exception as e:
            # The booking is already written; a missing window must not turn a
            # confirmed test into an error message.
            logger.warning(f"Could not render collection window for confirmation: {e}")

        prep_line = ""
        if context.get("lab_test_fasting_required"):
            prep_line = {
                "en": "\n\n⚠️ *Fasting required:* 10-12 hours before collection.",
                "hi": "\n\n⚠️ *उपवास आवश्यक:* सैंपल से 10-12 घंटे पहले।",
                "te": "\n\n⚠️ *ఉపవాసం అవసరం:* శాంపిల్‌కు 10-12 గంటల ముందు.",
            }.get(lang, "\n\n⚠️ *Fasting required:* 10-12 hours before collection.")

        header = {
            "en": "✅ *Lab Test Booked*",
            "hi": "✅ *लैब टेस्ट बुक हो गया*",
            "te": "✅ *ల్యాబ్ పరీక్ష బుక్ అయింది*",
        }.get(lang, "✅ *Lab Test Booked*")

        ref_label = {"en": "Ref", "hi": "संदर्भ", "te": "రెఫ్"}.get(lang, "Ref")

        confirm_text = (
            f"{header}\n\n"
            f"🧪 {context.get('lab_test_name')}\n"
            f"👤 {patient_name}\n"
            f"📅 {date_display}"
            f"{price_line}"
            f"{window_line}"
            f"{prep_line}\n\n"
            f"{ref_label}: `{appointment['booking_ref']}`"
        )

        await self.whatsapp.send_text(
            clinic, phone, confirm_text, _source="booking_confirmation"
        )

        await self.update_state(
            clinic, phone, "main_menu", {"menu_shown": False, **_LAB_STEP_CLEARED}
        )
        await self._send_main_menu(clinic, phone, lang)


# Global instance
conversation_manager = ConversationManager()

