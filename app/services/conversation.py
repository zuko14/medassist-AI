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
    map_symptom_to_department,
    EMERGENCY_KEYWORDS,
)
from app.services.whatsapp import whatsapp_service
from app.templates.whatsapp_templates import get_message
from app.utils.validators import mask_phone
from app.utils.helpers import format_slot_time

# Clinical safety firewall — screens messages before LLM is called
from app.services.clinical_firewall import screen_message

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


class ConversationManager:
    """Manages conversation state and flow."""

    async def update_state(
        self,
        clinic: dict,
        phone: str,
        new_state: str,
        new_context: Optional[dict] = None,
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

        merged = {**existing, **new_context}
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
                clinic, phone, get_message("unsupported_media", lang)
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

                # Cancel in database
                from app.database import cancel_appointment as db_cancel

                success = await db_cancel(clinic["id"], appointment_id)

                if success:
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
                    await self._show_lab_test_list(clinic, phone, {}, lang)
                else:
                    await self._show_doctors(clinic, phone, lang)
                return

            elif button_id == "menu_services":
                lang = await get_lang(clinic, phone)
                if await self._is_diagnostics_only(clinic):
                    await self._show_lab_test_list(clinic, phone, {}, lang)
                else:
                    await self._show_services(clinic, phone, lang)
                return

            elif button_id in ("book_lab_test", "menu_lab_tests"):
                # Quick-reply payload on the lab report delivery template, and
                # the standing "Book Lab Test" menu row. Tapping a template
                # button is an inbound message, so it legitimately opens the
                # 24h window and the full catalogue can be shown here.
                lang = await get_lang(clinic, phone)
                await self._show_lab_test_list(clinic, phone, {}, lang)
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
                .select("branch_id, session, branches(name, short_name, address)")
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
            context["doctor"] = doc
            context["doctor_name"] = doc["name"]
            context["department"] = doc["department"]
            context["selected_doctor_id"] = message

            # If multi-branch doctor and no branch pre-selected, ask patient to pick branch
            if len(branches) > 1 and not context.get("branch_id"):
                await self._send_doctor_branch_selection(clinic, phone, doc, branches, lang)
                await self.update_state(clinic, phone, "selecting_branch", context)
                return
            elif len(branches) == 1 and branches[0].get("branch_id"):
                # Single branch assigned — auto-attach
                binfo = branches[0].get("branches") or {}
                context["branch_id"] = branches[0]["branch_id"]
                context["branch_name"] = binfo.get("short_name") or binfo.get("name", "")
                context["branch_session"] = branches[0].get("session", "both")

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

        # Language change request (but NOT when already selecting language - let state machine handle it)
        if state != "selecting_language" and (
            intent in ["change_language", "select_language"]
            or message.lower() in ["change language", "भाषा बदलें", "భాష మార్చు"]
        ):
            await self._send_language_selection(clinic, phone)
            await self.update_state(clinic, phone, "selecting_language")
            return

        # Global handlers for top-level menu intents (escape hatches from selection states)
        if state not in ["selecting_language", "awaiting_consent"]:
            if intent == "doctor_availability":
                if await self._is_diagnostics_only(clinic):
                    await self._show_lab_test_list(clinic, phone, {}, lang)
                else:
                    await self._show_doctors(clinic, phone, lang)
                return

            if intent == "view_services":
                if await self._is_diagnostics_only(clinic):
                    await self._show_lab_test_list(clinic, phone, {}, lang)
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
            msg_lower = message.strip().lower()
            if (
                msg_lower in ["menu", "main menu", "home", "start over", "reset", "मेनू", "మెనూ"]
                or (intent == "greeting" and state in CHOICE_STATES and state != "main_menu")
            ):
                await self.update_state(clinic, phone, "main_menu", {"menu_shown": False})
                await self._send_main_menu(clinic, phone, lang)
                return

            # The lab report caption tells patients to reply "BOOK TEST", and the
            # report template's quick-reply button carries the same words. Handled
            # here, beside the other from-any-state navigation words, because a
            # patient who just received a report is rarely sitting in main_menu.
            if msg_lower in ("book test", "book lab test", "booktest"):
                from app.services.tenant import has_feature

                if has_feature(clinic, "lab_test_booking"):
                    await self._show_lab_test_list(clinic, phone, {}, lang)
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
        elif state == "collecting_symptoms":
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
        elif state == "viewing_reports":
            await self._handle_viewing_reports(clinic, phone, message, session, lang)
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

        rows = [{"id": "menu_book", "title": book_title[:24], "description": ""}]
        if not diagnostics_only:
            services_title = {"en": "Our Services", "hi": "Our Services", "te": "Our Services"}.get(lang, "Our Services")
            rows.append({"id": "menu_services", "title": services_title[:24], "description": ""})
            rows.append({"id": "menu_doctors", "title": t[0][:24], "description": ""})
            # A clinic that does consultations AND lab tests had no lab row at
            # all — "Book Appointment" reads as doctors-only, so patients never
            # discovered they could book a test here.
            if has_feature(clinic, "lab_test_booking"):
                rows.append({"id": "menu_lab_tests", "title": "🧪 Book Lab Test"[:24], "description": ""})
        rows.append({"id": "menu_reports", "title": "📋 My Reports"[:24], "description": ""})
        rows.append({"id": "menu_emergency", "title": t[1][:24], "description": ""})
        rows.append({"id": "menu_human", "title": t[2][:24], "description": ""})

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
                await self._show_lab_test_list(clinic, phone, {}, lang)
            else:
                await self._show_services(clinic, phone, lang)
        elif intent == "doctor_availability":
            if await self._is_diagnostics_only(clinic):
                await self._show_lab_test_list(clinic, phone, {}, lang)
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

    async def _start_booking(
        self, clinic: dict, phone: str, patient: Optional[dict], lang: str
    ) -> None:
        """Start the booking flow — with optional branch selection for multi-branch clinics."""
        patient = patient or {}

        # Guard: Language must be set before proceeding
        if not patient.get("language"):
            await self._send_language_selection(clinic, phone)
            await self.update_state(clinic, phone, "selecting_language")
            return

        # ── Diagnostics-Only Routing ─────────────────────────────────────────
        if await self._is_diagnostics_only(clinic):
            await self._show_lab_test_list(clinic, phone, {}, lang)
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
                await self.update_state(clinic, phone, "selecting_branch", {})
                return
            elif len(bookable_branches) == 1:
                # Only one bookable branch — auto-select it
                branch = bookable_branches[0]
                context = {
                    "branch_id": branch["id"],
                    "branch_name": branch.get("short_name") or branch["name"],
                    "branch_address": branch.get("address", ""),
                    "branch_landmark": branch.get("landmark", ""),
                    "branch_maps_link": branch.get("maps_link", ""),
                }
                await self._continue_booking_after_branch(
                    clinic, phone, patient, lang, context
                )
                return
        # ── End Multi-Branch Check ──────────────────────────────────────────

        await self._continue_booking_after_branch(clinic, phone, patient, lang, {})

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
            for b in branches[:10]:
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
        self, clinic: dict, phone: str, branches: list, lang: str
    ) -> None:
        """Send interactive list of branches for multi-branch clinics.
        Title = locality (short_name) so patients see 'Madhurwada', not 'City Polyclinic'.
        Description = short address + landmark for extra context.
        """
        rows = []
        for branch in branches[:10]:  # WhatsApp max 10 rows
            # Title: locality name (short_name preferred, fallback to name)
            title = (branch.get("short_name") or branch["name"])[:24]

            # Description: combine address snippet + landmark
            desc_parts = []
            if branch.get("address"):
                desc_parts.append(branch["address"][:50])
            if branch.get("landmark"):
                desc_parts.append(f"Near {branch['landmark']}")
            description = ", ".join(desc_parts) if desc_parts else ""

            rows.append(
                {
                    "id": f"branch_{branch['id']}",
                    "title": title,
                    "description": description[:72],
                }
            )

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

        if button_id.startswith("branch_"):
            branch_id = button_id.replace("branch_", "")
            from app.services.tenant import get_branch_by_id

            branch = await get_branch_by_id(branch_id)

            if branch:
                # Store branch context for the entire booking flow
                new_context = {
                    **context,
                    "branch_id": branch["id"],
                    "branch_name": branch.get("short_name") or branch["name"],
                    "branch_address": branch.get("address", ""),
                    "branch_landmark": branch.get("landmark", ""),
                    "branch_maps_link": branch.get("maps_link", ""),
                }

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
        bookable_branches = [b for b in branches if not b.get("is_diagnostic", False)]
        await self._send_branch_selection(clinic, phone, bookable_branches, lang)

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
            p_name = (patient or {}).get("name") or "there"
            new_ctx = {**context, "patient_name": p_name, "for_self": True}
            await self.update_state(clinic, phone, "asking_symptoms", new_ctx)
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
                "relationship": member.get("relationship"),
                "is_family": True,
            }
            await self.update_state(clinic, phone, "asking_symptoms", new_ctx)
            await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
            return

        # 4. Check if exact name was typed
        for m in family_members:
            if msg_clean == m["full_name"].lower():
                new_ctx = {
                    **context,
                    "patient_name": m["full_name"],
                    "relationship": m.get("relationship"),
                    "is_family": True,
                }
                await self.update_state(clinic, phone, "asking_symptoms", new_ctx)
                await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
                return

        # Fallback: Treat typed input as new name if 2+ words, or prompt again
        if len(msg_clean.split()) >= 2:
            new_ctx = {**context, "patient_name": message.strip(), "is_family": True}
            await self.update_state(clinic, phone, "asking_symptoms", new_ctx)
            await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
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
        """Save a new family member to the database if the patient confirmed YES."""
        msg = message.strip().lower()
        if msg in ["save_family_yes", "yes", "y", "हाँ", "అవును"]:
            name = context.get("patient_name")
            if name:
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
        await self.update_state(clinic, phone, "main_menu")

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
            return

        name = result
        context["booking_name"] = name

        # Save to patient record if for self
        if context.get("for_self", True):
            await update_patient(clinic["id"], phone, {"name": name})

        # Move to symptoms
        await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
        await self.update_state(clinic, phone, "collecting_symptoms", context)

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
        self, rows: list[dict], page: int, more_id: str, lang: str
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
        if page <= 0 and len(rows) <= 10:
            return rows, 0

        start = max(0, page) * self.LIST_PAGE_SIZE
        if start >= len(rows):  # ran past the end; restart from the beginning
            start, page = 0, 0

        page_rows = list(rows[start : start + self.LIST_PAGE_SIZE])
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
            if context.get("department"):
                await self._show_doctor_list(
                    clinic, phone, context["department"], context, lang
                )
            else:
                await self._show_department_list(clinic, phone, context, lang)
            return

        context["doctor_name"] = doctor_name
        context["doctor"] = doctor
        if isinstance(doctor, dict) and doctor.get("id"):
            context["doctor_id"] = doctor["id"]
            context["selected_doctor_id"] = doctor["id"]

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

        # Get available slots
        slots, reason = await get_available_slots(
            clinic["id"], context["doctor_name"], date_str
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
                clinic["id"], context["doctor_name"], d.strftime("%Y-%m-%d")
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
                clinic["id"], context["doctor_name"], date_str
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

        # Build confirmation body — include branch info when present
        branch_name = context.get("branch_name")  # Now stores locality (short_name)
        branch_landmark = context.get("branch_landmark", "")

        if branch_name:
            # Multi-branch: include locality in confirmation
            branch_line = f"\n🏥 Branch: {branch_name}"
            if branch_landmark:
                branch_line += f" ({branch_landmark})"

            confirm_body = (
                get_message(
                    "confirm_booking",
                    lang,
                    name=context.get("booking_name", "Patient"),
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
                name=context.get("booking_name", "Patient"),
                doctor=context["doctor_name"],
                department=context.get("department", ""),
                date=date_display,
                time=context["appointment_time"],
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

            # ── Resolve this clinic's payment mode: full / partial / none ──
            from app.services.payment import resolve_payment_mode

            payment_mode, deposit_percent = resolve_payment_mode(clinic)

            if payment_mode in ("full", "partial"):
                # ═══ PATH A: Payment-gated booking ═══
                from app.services.payment import payment_service

                result = await payment_service.create_booking_with_payment(
                    clinic_id=clinic["id"],
                    patient_phone=phone,
                    patient_name=context.get("booking_name", "Patient"),
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
                    )
                    if slots:
                        await self._show_slot_list(
                            clinic, phone, slots, context, lang
                        )
                    else:
                        await self._suggest_other_doctors(clinic, phone, context, lang)
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
                    "patient_name": context.get("booking_name", "Patient"),
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

                result = await book_appointment(clinic["id"], appointment_data)

                if result["success"]:
                    appointment = result["appointment"]
                    date_display = datetime.strptime(
                        context["appointment_date"], "%Y-%m-%d"
                    ).strftime("%d %b %Y")

                    await self.whatsapp.send_text(
                        clinic,
                        phone,
                        get_message(
                            "booking_confirmed",
                            lang,
                            ref=appointment["booking_ref"],
                            doctor=context["doctor_name"],
                            date=date_display,
                            time=context["appointment_time"],
                        ),
                    )

                    # Send location — use branch-specific info for multi-branch,
                    # or clinic-level info for single-branch bookings
                    if context.get("branch_id"):
                        # Multi-branch: send branch-specific address + Google Maps
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

                    await self.update_state(clinic, phone, "main_menu")
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
                        await self.update_state(clinic, phone, "main_menu")
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

                query = supabase.table("appointments").update({"status": "cancelled"}).eq("id", booking_id)
                if clinic and clinic.get("id"):
                    query = query.eq("clinic_id", clinic["id"])
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

                query = supabase.table("appointments").select("status, booking_ref").eq("id", booking_id)
                if clinic and clinic.get("id"):
                    query = query.eq("clinic_id", clinic["id"])
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

        doctors = [
            d
            for d in await get_doctors(clinic["id"], department)
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
                    get_available_slots(clinic["id"], d["name"], check_date)
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
                        for i, doc in enumerate(available[:10])
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

        if not status.get("checked_in"):
            await self.whatsapp.send_text(
                clinic,
                phone,
                get_message(
                    "queue_status_not_checked_in",
                    lang,
                    doctor=status.get("doctor_name", "your doctor"),
                ),
            )
            return

        await self.whatsapp.send_text(
            clinic,
            phone,
            get_message(
                "queue_status_waiting",
                lang,
                token=status["token_number"],
                doctor=status["doctor_name"],
                current=status["currently_serving"],
                ahead=status["patients_ahead"],
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

    async def _handle_data_deletion(
        self, clinic: dict, phone: str, patient: dict, lang: str
    ) -> None:
        """Handle data deletion request."""
        from app.database import delete_patient_data

        await delete_patient_data(clinic["id"], phone)
        await self.whatsapp.send_text(clinic, phone, get_message("data_deleted", lang))
        await log_analytics_event(clinic["id"], phone, "data_deleted")

    async def _handle_human_escalation(
        self, clinic: dict, phone: str, lang: str
    ) -> None:
        """Handle human escalation request."""
        contact_phone = (
            clinic.get("whatsapp_number")
            or clinic.get("phone")
            or settings.hospital_phone
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

    async def _show_doctors(self, clinic: dict, phone: str, lang: str) -> None:
        """Show available doctors grouped by canonical identity with branch annotations."""
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

        sections = []
        dept_groups = {}
        for doc in doctors:
            dept = doc.get("department", "General Medicine")
            if dept not in dept_groups:
                dept_groups[dept] = []
            dept_groups[dept].append(doc)

        # WhatsApp interactive lists allow max 10 rows TOTAL across all sections combined.
        remaining_rows = 10
        for dept, docs in dept_groups.items():
            if remaining_rows <= 0:
                break
            rows = []
            for doc in docs[:remaining_rows]:
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

                rows.append(
                    {
                        "id": f"view_doc_{doc['id']}",
                        "title": doc["name"][:24],
                        "description": " · ".join(desc_parts)[:72],
                    }
                )
                remaining_rows -= 1

            if rows:
                sections.append({"title": dept[:24], "rows": rows})

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
            sections=sections[:10],
        )

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
        for appt in appointments[:10]:
            appt_date = appt.get("appointment_date", "")
            date_label = "Today" if appt_date == today else appt_date
            status_label = (appt.get("status") or "").replace("_", " ").title()
            rows.append(
                {
                    "id": f"cancel_{appt['id']}",
                    "title": f"{appt.get('doctor_name', 'Doctor')[:20]}",
                    "description": f"{date_label} {format_slot_time(appt.get('appointment_time', ''))} · {status_label}"[
                        :72
                    ],
                }
            )

        sections = [{"title": "Select to Cancel", "rows": rows}]

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body="Which appointment would you like to cancel?",
            button_text="Select",
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
        """Handle 'My Reports' menu selection."""
        from app.services.tenant import has_feature

        if not has_feature(clinic, "lab_reports"):
            await self.whatsapp.send_text(
                clinic,
                phone,
                "Lab report delivery is not available at this facility via WhatsApp. "
                "Please visit the hospital reception to collect your reports.",
            )
            await self._send_main_menu(clinic, phone, lang)
            return

        from app.services.lab_reports import LabReportService

        reports = await LabReportService().get_reports_by_phone(phone, clinic["id"])

        if not reports:
            await self.whatsapp.send_text(
                clinic,
                phone,
                "📋 No reports found for your number. Please visit the hospital or contact reception.",
            )
            await self._send_main_menu(clinic, phone, lang)
            await self.update_state(clinic, phone, "main_menu")
            return

        # Show up to 5 most recent reports
        recent = reports[:5]
        lines = ["📋 *Your Lab Reports*\n\nHere are your available reports:\n"]
        for i, r in enumerate(recent, 1):
            date_str = ""
            if r.get("uploaded_at"):
                try:
                    from datetime import datetime

                    dt = datetime.fromisoformat(r["uploaded_at"].replace("Z", "+00:00"))
                    date_str = f" — {dt.strftime('%d %b %Y')}"
                except Exception:
                    pass
            lines.append(f"{i}. {r['report_name']}{date_str}")

        lines.append(
            "\nReply with the report number to download it. Reply 0 to go back to main menu."
        )
        await self.whatsapp.send_text(clinic, phone, "\n".join(lines))

        # Save reports list in context
        await self.update_state(
            clinic, phone, "viewing_reports", {"available_reports": recent}
        )

    async def _handle_viewing_reports(
        self, clinic: dict, phone: str, message: str, session: dict, lang: str
    ) -> None:
        """Handle report selection in VIEWING_REPORTS state."""
        context = session.get("context", {})
        available = context.get("available_reports", [])
        msg_stripped = message.strip()

        if msg_stripped == "0":
            await self.update_state(clinic, phone, "main_menu", {"menu_shown": False})
            await self._send_main_menu(clinic, phone, lang)
            return

        # Check for "menu" keyword
        if msg_stripped.lower() in ["menu", "main menu"]:
            await self.update_state(clinic, phone, "main_menu", {"menu_shown": False})
            await self._send_main_menu(clinic, phone, lang)
            return

        try:
            choice = int(msg_stripped)
            if 1 <= choice <= len(available):
                selected = available[choice - 1]
                await self.whatsapp.send_text(
                    clinic, phone, "📤 Sending your report now..."
                )

                from app.services.lab_reports import LabReportService

                try:
                    await LabReportService().resend_report(selected["id"])
                    await self.whatsapp.send_text(
                        clinic,
                        phone,
                        "✅ Report sent! You can save it directly from WhatsApp. Need anything else? Reply with *Menu* to return.",
                    )
                except Exception as e:
                    logger.error(f"Failed to resend report: {e}")
                    await self.whatsapp.send_text(
                        clinic,
                        phone,
                        "Sorry, we could not send the report right now. Please try again later or contact the hospital.",
                    )

                await self.update_state(
                    clinic, phone, "main_menu", {"menu_shown": False}
                )
                return
            else:
                await self.whatsapp.send_text(
                    clinic,
                    phone,
                    "Please reply with a number from the list, or reply 0 to go back.",
                )
                return
        except ValueError:
            await self.whatsapp.send_text(
                clinic,
                phone,
                "Please reply with a number from the list, or reply 0 to go back.",
            )
            return

    async def _show_lab_test_list(
        self, clinic: dict, phone: str, context: dict, lang: str, page: int = 0
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

        all_rows = []
        for t in tests:
            price_str = f"₹{t['price_paise'] // 100}"
            sample_str = f" • {t['sample_type']}" if t.get("sample_type") else ""
            desc = f"{price_str}{sample_str}"[:72]
            all_rows.append({
                "id": f"labtest_{t['id']}",
                "title": t["name"][:24],
                "description": desc,
            })

        rows, page = self._page_rows(all_rows, page, "labtest_more", lang)

        body = {
            "en": "Select a lab test to book your sample collection:",
            "hi": "सैंपल कलेक्शन बुक करने के लिए लैब टेस्ट चुनें:",
            "te": "శాంపిల్ కలెక్షన్ బుక్ చేసుకోవడానికి ల్యాబ్ పరీక్షను ఎంచుకోండి:",
        }.get(lang, "Select a lab test:")

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
            sections=[{"title": "Available Tests", "rows": rows}],
        )
        context["lab_test_page"] = page
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
        from app.database import get_lab_test_by_id, get_lab_collection_window, format_collection_window

        button_id = interactive_data.get("id", "") if interactive_data else ""

        # Before the labtest_ prefix match: "labtest_more" would otherwise be
        # read as a test id of "more".
        if button_id == "labtest_more":
            await self._show_lab_test_list(
                clinic, phone, context, lang,
                page=int(context.get("lab_test_page") or 0) + 1,
            )
            return

        selected_id = None
        if button_id.startswith("labtest_"):
            selected_id = button_id.removeprefix("labtest_")

        if not selected_id:
            # Fallback: patient typed something instead of tapping a list row
            # Re-present the list
            await self._show_lab_test_list(clinic, phone, context, lang)
            return

        test = await get_lab_test_by_id(clinic["id"], selected_id)
        if not test or not test.get("is_active"):
            msg = {
                "en": "That test is no longer available. Please pick another.",
                "hi": "वह टेस्ट अब उपलब्ध नहीं है। कृपया दूसरा चुनें।",
                "te": "ఆ పరీక్ష ఇప్పుడు అందుబాటులో లేదు. దయచేసి మరొకటి ఎంచుకోండి.",
            }.get(lang, "That test is no longer available.")
            await self.whatsapp.send_text(clinic, phone, msg)
            await self._show_lab_test_list(clinic, phone, context, lang)
            return

        # Stash test details in conversation context
        context["lab_test_id"] = test["id"]
        context["lab_test_name"] = test["name"]
        context["lab_test_price_paise"] = test["price_paise"]
        context["lab_test_fasting_required"] = test.get("fasting_required", False)
        context["lab_test_prep_instructions"] = test.get("prep_instructions")
        context["lab_test_turnaround_hours"] = test.get("turnaround_hours")

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
        if test.get("fasting_required"):
            instructions_line = "\n⚠️ *Fasting Required:* 10-12 hours fasting before collection."
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
        """Handle patient selecting a collection date and initialize payment-gated booking."""
        from app.services.payment import payment_service

        selected_date = None
        if interactive_data and interactive_data.get("id", "").startswith("labdate_"):
            selected_date = interactive_data["id"].removeprefix("labdate_")

        if not selected_date:
            msg = {
                "en": "Please tap one of the date buttons above to continue.",
                "hi": "आगे बढ़ने के लिए कृपया ऊपर दिए गए तारीख बटन में से एक पर टैप करें।",
                "te": "కొనసాగడానికి దయచేసి పైన ఉన్న తేదీ బటన్‌లలో ఒకదాన్ని నొక్కండి.",
            }.get(lang, "Please tap one of the date buttons above.")
            await self.whatsapp.send_text(clinic, phone, msg)
            return

        patient_name = (patient or {}).get("name") or context.get("patient_name") or "Patient"

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
        )

        if not result.get("success"):
            logger.error(f"Failed to create lab test booking: {result.get('error')}")
            err_msg = {
                "en": "We couldn't initialize your booking. Please try again or contact the center.",
                "hi": "हम आपकी बुकिंग शुरू नहीं कर सके। कृपया पुनः प्रयास करें।",
                "te": "మేము మీ బుకింగ్‌ను ప్రారంభించలేకపోయాము. దయచేసి మళ్లీ ప్రయత్నించండి.",
            }.get(lang, "Failed to initialize booking.")
            await self.whatsapp.send_text(clinic, phone, err_msg)
            await self._send_main_menu(clinic, phone, lang)
            return

        # Send payment link to patient
        amount_rupees = result["amount_paise"] // 100
        pay_msg = (
            f"🧪 *Lab Test Booking Reserved*\n\n"
            f"Test: *{context.get('lab_test_name')}*\n"
            f"Date: *{selected_date}*\n"
            f"Amount: *₹{amount_rupees}*\n\n"
            f"Please complete your payment within {settings.booking_hold_minutes} minutes to confirm:\n"
            f"{result['payment_link']}\n\n"
            f"Ref: `{result['booking_ref']}`"
        )
        await self.whatsapp.send_text(clinic, phone, pay_msg)

        context["booking_id"] = result["booking_id"]
        context["booking_ref"] = result["booking_ref"]
        context["payment_link"] = result["payment_link"]
        context["hold_expires_at"] = result["hold_expires_at"]

        await self.update_state(clinic, phone, "awaiting_payment", context)


# Global instance
conversation_manager = ConversationManager()

