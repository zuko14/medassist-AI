"""Conversation state machine for MediAssist."""

import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from app.config import settings
from app.database import (
    get_or_create_conversation, update_conversation,
    get_patient_by_phone, create_patient, update_patient,
    get_doctors, get_doctor_by_name, get_available_slots,
    find_next_available_date, book_appointment, log_analytics_event
)
from app.services.ai_engine import detect_intent, map_symptom_to_department, EMERGENCY_KEYWORDS
from app.services.whatsapp import whatsapp_service
from app.templates.whatsapp_templates import MESSAGES, get_message
# Clinical safety firewall — screens messages before LLM is called
from app.services.clinical_firewall import screen_message
# Per-phone asyncio lock with Meta timeout protection
from app.services.message_queue import (
    acquire_phone_lock_with_timeout,
    get_phone_lock,
    release_phone_lock,
)

logger = logging.getLogger(__name__)


async def get_lang(clinic: dict, phone: str) -> str:
    """Get language for a patient from database."""
    try:
        from app.database import supabase
        result = supabase.table("patients").select("language").eq("clinic_id", clinic["id"]).eq("phone", phone).single().execute()
        lang = result.data.get("language")
        return lang if lang in ["en", "hi", "te"] else "en"
    except Exception:
        return "en"


class ConversationState(str, Enum):
    IDLE = "idle"
    SELECTING_LANGUAGE = "selecting_language"
    AWAITING_CONSENT = "awaiting_consent"
    MAIN_MENU = "main_menu"
    COLLECTING_NAME = "collecting_name"
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


class ConversationManager:
    """Manages conversation state and flow."""

    async def update_state(self, clinic: dict, phone: str, new_state: str, new_context: dict = None) -> None:
        if new_context is None:
            new_context = {}
        from app.database import get_conversation
        from app.database import supabase
        session = await get_conversation(clinic["id"], phone)
        if not session:
            return
        existing = session.get("context", {}) or {}
        
        # Reset menu_shown to False if transitioning BACK to main_menu from another state
        if new_state == "main_menu" and session.get("state") != "main_menu":
            new_context["menu_shown"] = False

        merged = {**existing, **new_context}
        supabase.table("conversations").update({
            "state": new_state,
            "context": merged,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("clinic_id", clinic["id"]).eq("phone", phone).execute()

    async def get_patient_language(self, clinic: dict, phone: str) -> str:
        from app.database import supabase
        patient = supabase.table("patients").select("language").eq("clinic_id", clinic["id"]).eq("phone", phone).execute()
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
        interactive_data: Optional[dict] = None
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
            # Save to dead-letter queue for automatic retry rather than dropping.
            logger.warning(
                f"Phone lock timeout for {phone[:6]}*** — deferring message "
                f"{message_id} to dead-letter queue"
            )
            try:
                from app.database import supabase
                import json
                supabase.table("failed_messages").insert({
                    "phone": phone,
                    "display_phone": clinic.get("phone", ""),
                    "payload": json.dumps({
                        "message": message[:500],
                        "message_type": message_type,
                        "message_id": message_id,
                        "clinic_id": clinic_id,
                    }),
                    "error": "Phone lock timeout (15s) — previous message still processing",
                    "status": "pending_retry"
                }).execute()
            except Exception as dlq_err:
                logger.error(f"Failed to save timed-out message to DLQ: {dlq_err}")
            return

        # Lock acquired — process the message with guaranteed cleanup
        phone_lock = await get_phone_lock(phone)
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
        finally:
            # Always release the lock and decrement refcount
            phone_lock.release()
            await release_phone_lock(phone)

    async def _handle_message_locked(
        self,
        clinic: dict,
        phone: str,
        message: str,
        message_type: str = "text",
        message_id: Optional[str] = None,
        interactive_data: Optional[dict] = None
    ) -> None:
        """Inner handler called while holding the per-phone asyncio lock."""
        clinic_id = clinic["id"]

        # Guard 1: Duplicate webhook delivery (secondary check at conversation layer)
        session = await get_or_create_conversation(clinic_id, phone)
        if message_id and session.get("last_processed_message_id") == message_id:
            logger.info(f"Duplicate dropped at conversation layer: {message_id}")
            return

        if message_id:
            await update_conversation(clinic["id"], phone, {"last_processed_message_id": message_id})
            await self.whatsapp.mark_as_read(clinic, message_id)

        # Get or create patient
        patient = await get_patient_by_phone(clinic["id"], phone)
        if not patient:
            patient = await create_patient(clinic["id"], phone)
            logger.info(f"Created new patient for {phone}")

        # Determine language - use None if not set (don't default here)
        lang = patient.get("language") or "en"

        # Guard 2: Session timeout mid-booking
        mid_booking_states = [
            "collecting_name", "collecting_symptoms", "suggesting_department",
            "selecting_doctor", "selecting_date", "selecting_slot", "confirming_booking"
        ]
        booking_expires = session.get("booking_context_expires_at")

        if (booking_expires and
            session["state"] in mid_booking_states):
            expires_dt = datetime.fromisoformat(booking_expires.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expires_dt:
                await update_conversation(clinic["id"], phone, {
                    "state": "main_menu",
                    "context": {},
                    "booking_context_expires_at": None
                })
                await self.whatsapp.send_text(clinic, phone, get_message("session_timeout", lang))
                await self._send_main_menu(clinic, phone, lang)
                return

        # Reset booking timer on every message while mid-booking
        if session["state"] in mid_booking_states:
            expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            await update_conversation(clinic["id"], phone, {"booking_context_expires_at": expires})

        # Update session expiry (24 hours from now)
        session_expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        await update_conversation(clinic["id"], phone, {"session_expires_at": session_expires})

        # ── Clinical Firewall: Screen for medical advice requests ──────────────
        # This runs BEFORE the LLM is called. If a patient asks for medication
        # names, dosages, or diagnoses, we return a safe static response and
        # never reach the Groq API — protecting against NMC liability.
        # Skip firewall for interactive button responses (they are controlled inputs)
        if message_type == "text" and message.strip():
            lang_for_firewall = patient.get("language") or "en"
            firewall_blocked, firewall_response = screen_message(message, lang_for_firewall)
            if firewall_blocked and firewall_response:
                await self.whatsapp.send_text(clinic, phone, firewall_response)
                logger.info(
                    f"Clinical firewall blocked message from {phone[:6]}*** "
                    f"(type: medication/diagnosis request)"
                )
                return
        # ── End Clinical Firewall ──────────────────────────────────────────────

        # Detect intent
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
                await update_conversation(clinic["id"], phone, {
                    "context": ctx,
                    "state": "collecting_symptoms"
                })
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
            elif button_id.startswith("date_"):
                intent = "select_date"
                message = button_id.replace("date_", "")
            elif button_id == "confirm_yes":
                intent = "confirm_booking"
            elif button_id == "confirm_no":
                intent = "edit_booking"
            elif button_id == "go_main_menu":
                lang = await get_lang(clinic, phone)
                await self.update_state(clinic, phone, "main_menu", {
                    "menu_shown": False
                })
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
            elif button_id == "edit_doctor":
                intent = "edit_doctor"
            elif button_id == "edit_date":
                intent = "edit_date"
            elif button_id == "edit_time":
                intent = "edit_time"
            elif button_id in ["chest_severe", "chest_mild", "back_lower", "back_upper"]:
                intent_map = {
                    "chest_severe": "severe chest pain",
                    "chest_mild": "mild chest pain",
                    "back_lower": "lower back pain",
                    "back_upper": "upper back pain"
                }
                message = intent_map.get(button_id, message)
            elif button_id.startswith("cancel_"):
                appointment_id = button_id.replace("cancel_", "")
                lang = await get_lang(clinic, phone)
                
                # Cancel in database
                from app.database import cancel_appointment as db_cancel
                success = await db_cancel(clinic_id, appointment_id)
                
                if success:
                    cancel_msg = {
                        "en": "Your appointment has been cancelled successfully.",
                        "hi": "आपका अपॉइंटमेंट सफलतापूर्वक रद्द कर दिया गया है।",
                        "te": "మీ అపాయింట్మెంట్ విజయవంతంగా రద్దు చేయబడింది."
                    }.get(lang, "Appointment cancelled.")
                    await self.whatsapp.send_text(clinic, phone, cancel_msg)
                else:
                    await self.whatsapp.send_text(
                        clinic, phone, "Could not cancel. Please call us: " + clinic["whatsapp_number"]
                    )
                
                await self.update_state(clinic, phone, "main_menu", {})
                await self._send_main_menu(clinic, phone, lang)
                return

            elif button_id in ["menu_book", "menu_services", "menu_doctors", "menu_emergency", "menu_human", "menu_reports"]:
                intent_map = {
                    "menu_book": "book_appointment",
                    "menu_services": "view_services",
                    "menu_doctors": "doctor_availability",
                    "menu_emergency": "emergency",
                    "menu_human": "human_escalation",
                    "menu_reports": "view_reports"
                }
                intent = intent_map.get(button_id, intent)

        # Guard 5: Concurrent booking protection
        # Only trigger when user says "book appointment" via text while deep in booking
        # Skip states where user text input is expected (names, symptoms, dates, slots, etc.)
        SAFE_STATES = ["collecting_name", "collecting_symptoms", "suggesting_department"]
        if (intent == "book_appointment" and
            session["state"] in mid_booking_states and
            session["state"] not in SAFE_STATES and
            message_type != "interactive"):
            context = session.get("context", {})
            doctor = context.get("doctor_name", "this doctor")
            await self.whatsapp.send_interactive_buttons(
                clinic, phone,
                body=get_message("already_booking", lang, doctor=doctor),
                buttons=[
                    {"id": "continue_booking", "title": "Continue" if lang == "en" else ("जारी रखें" if lang == "hi" else "కొనసాగించు")},
                    {"id": "restart_booking", "title": "Start Over" if lang == "en" else ("फिर से शुरू" if lang == "hi" else "మళ్లీ ప్రారంభించు")}
                ]
            )
            return

        # Handle global views (interactive buttons from _show_doctors and _show_services)
        if intent == "view_doctor":
            from app.database import supabase
            res = supabase.table("doctors").select("*").eq("clinic_id", clinic["id"]).eq("id", message).execute()
            if res.data:
                doc = res.data[0]
                context = session.get("context", {})
                context["doctor"] = doc
                context["doctor_name"] = doc["name"]
                context["department"] = doc["department"]
                context["selected_doctor_id"] = message
                lang = await get_lang(clinic, phone)
                await self._show_date_picker(clinic, phone, context, lang)
                await self.update_state(clinic, phone, "selecting_date", context)
            return


        # Process based on state and intent
        await self._process_state(clinic, phone, message, intent, session, patient, lang, interactive_data)

    async def _process_state(self, clinic: dict, phone: str,
        message: str,
        intent: str,
        session: dict,
        patient: dict,
        lang_ignored: str,
        interactive_data: Optional[dict] = None
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
            intent in ["change_language", "select_language"] or message.lower() in [
                "change language", "भाषा बदलें", "భాష మార్చు"
            ]
        ):
            await self._send_language_selection(clinic, phone)
            await self.update_state(clinic, phone, "selecting_language")
            return

        # State machine
        if state == "idle":
            await self._handle_idle(clinic, phone, message, intent, patient, lang)
        elif state == "selecting_language":
            await self._handle_selecting_language(clinic, phone, message, patient, interactive_data)
        elif state == "awaiting_consent":
            await self._handle_awaiting_consent(clinic, phone, message, patient, lang, interactive_data)
        elif state == "main_menu":
            await self._handle_main_menu(clinic, phone, message, intent, patient, lang)
        elif state == "collecting_name":
            await self._handle_collecting_name(clinic, phone, message, context, patient, lang)
        elif state == "collecting_symptoms":
            await self._handle_collecting_symptoms(clinic, phone, message, context, patient, lang)
        elif state == "suggesting_department":
            await self._handle_suggesting_department(clinic, phone, message, intent, context, lang, interactive_data)
        elif state == "selecting_department":
            await self._handle_selecting_department(clinic, phone, message, intent, context, lang, interactive_data)
        elif state == "selecting_doctor":
            await self._handle_selecting_doctor(clinic, phone, message, intent, context, lang, interactive_data)
        elif state == "selecting_date":
            await self._handle_selecting_date(clinic, phone, message, context, lang)
        elif state == "selecting_slot":
            await self._handle_selecting_slot(clinic, phone, message, intent, context, lang)
        elif state == "confirming_booking":
            await self._handle_confirming_booking(clinic, phone, message, intent, context, patient, lang)
        elif state == "awaiting_payment":
            await self._handle_awaiting_payment(clinic, phone, message, context, patient, lang)
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

    async def _handle_idle(self, clinic: dict, phone: str, message: str, intent: str, patient: dict, lang: str) -> None:
        """Handle idle state - first interaction."""
        # Check if returning patient with language already set
        existing_lang = patient.get("language")
        has_visited = patient.get("visit_count", 0) > 0
        
        if existing_lang and existing_lang in ["en", "hi", "te"] and has_visited:
            # Returning patient — skip language picker
            if not patient.get("data_consent"):
                from app.database import get_conversation
                session = await get_session(phone)
                if session.get("state") == "awaiting_consent":
                    return  # already sent, don't send again

                await self.whatsapp.send_interactive_buttons(
                    clinic, phone,
                    body=get_message("consent_request", existing_lang),
                    buttons=[
                        {"id": "consent_yes", "title": "Yes" if existing_lang == "en" else ("हाँ" if existing_lang == "hi" else "అవును")},
                        {"id": "consent_no", "title": "No" if existing_lang == "en" else ("नहीं" if existing_lang == "hi" else "కాదు")}
                    ]
                )
                await self.update_state(clinic, phone, "awaiting_consent", {})
            else:
                patient_name = patient.get("name") or "there"
                first_name = patient_name.split()[0] if patient_name else "there"
                await self.whatsapp.send_text(clinic, phone, get_message("welcome_back", existing_lang, name=first_name))
                await self._send_main_menu(clinic, phone, existing_lang)
                await self.update_state(clinic, phone, "main_menu", {})
            return
        
        # New patient OR language not set → ALWAYS show language picker
        # Do NOT read the message content
        # Do NOT detect language from message
        # Do NOT set any language
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"IDLE: phone={phone}, existing_lang={patient.get('language')}, visits={patient.get('visit_count')}")

        await self._send_language_selection(clinic, phone)
        await self.update_state(clinic, phone, "selecting_language", {})
        return

    async def _send_language_selection(self, clinic: dict, phone: str) -> None:
        """Send language selection buttons."""
        from app.config import settings
        body_text = f"Welcome to {clinic['name']} 🏥\nनमस्ते | నమస్కారం\n\nPlease select your language:\nअपनी भाषा चुनें | మీ భాష ఎంచుకోండి"
        await self.whatsapp.send_interactive_buttons(
            clinic, phone,
            body=body_text,
            buttons=[
                {"id": "lang_en", "title": "English"},
                {"id": "lang_hi", "title": "हिंदी"},
                {"id": "lang_te", "title": "తెలుగు"}
            ]
        )

    async def _handle_selecting_language(self, clinic: dict, phone: str, message: str, patient: dict, interactive_data: Optional[dict] = None) -> None:
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
            state = session.get("state")
            if state == "awaiting_consent":
                return  # already sent consent, don't send again
                
            if state == "selecting_language":
                await self.whatsapp.send_interactive_buttons(
                    clinic, phone,
                    body=get_message("consent_request", selected),
                    buttons=[
                        {"id": "consent_yes", "title": "Yes" if selected == "en" else ("हाँ" if selected == "hi" else "అవును")},
                        {"id": "consent_no", "title": "No" if selected == "en" else ("नहीं" if selected == "hi" else "కాదు")}
                    ]
                )
                await self.update_state(clinic, phone, "awaiting_consent", {})
            return

        # Get welcome message in selected language
        await self.whatsapp.send_text(clinic, phone, get_message("welcome", selected))
        await self.whatsapp.send_text(clinic, phone, get_message("disclaimer", selected))
        await self._send_main_menu(clinic, phone, selected)
        await self.update_state(clinic, phone, "main_menu")

    async def _handle_awaiting_consent(self, clinic: dict, phone: str, message: str, patient: dict, lang: str, interactive_data: Optional[dict] = None) -> None:
        """Handle data consent response."""
        button_id = interactive_data.get("id") if interactive_data else None
        msg_lower = message.lower().strip()

        if button_id == "consent_yes" or msg_lower in ["yes", "y", "ha", "हां", "అవును"]:
            await update_patient(clinic["id"], phone, {"data_consent": True, "data_consent_at": "now()"})
            await self.whatsapp.send_text(clinic, phone, get_message("welcome", lang))
            await self.whatsapp.send_text(clinic, phone, get_message("disclaimer", lang))
            await self._send_main_menu(clinic, phone, lang)
            await self.update_state(clinic, phone, "main_menu")
        elif button_id == "consent_no" or msg_lower in ["no", "n", "nahin", "नहीं", "కాదు"]:
            await update_patient(clinic["id"], phone, {"data_consent": False})
            await self.whatsapp.send_text(clinic, phone, get_message("welcome", lang))
            await self.whatsapp.send_text(clinic, phone, get_message("disclaimer", lang))
            await self._send_main_menu(clinic, phone, lang)
            await self.update_state(clinic, phone, "main_menu")
        else:
            await self.whatsapp.send_interactive_buttons(
                clinic, phone,
                body=get_message("consent_request", lang),
                buttons=[
                    {"id": "consent_yes", "title": "Yes" if lang == "en" else ("हाँ" if lang == "hi" else "అవును")},
                    {"id": "consent_no", "title": "No" if lang == "en" else ("नहीं" if lang == "hi" else "కాదు")}
                ]
            )

    async def _send_main_menu(self, clinic: dict, phone: str, lang: str) -> None:
        """Send main menu with buttons."""
        titles = {
            "en": ["Book Appointment", "Our Services", "Our Doctors", "Emergency", "Talk to Staff"],
            "hi": ["Book Appointment", "Our Services", "Our Doctors", "Emergency", "Talk to Staff"],
            "te": ["Book Appointment", "Our Services", "Our Doctors", "Emergency", "Talk to Staff"]
        }

        t = titles.get(lang, titles["en"])

        sections = [{
            "title": "Menu",
            "rows": [
                {"id": "menu_book", "title": t[0][:24], "description": ""},
                {"id": "menu_services", "title": t[1][:24], "description": ""},
                {"id": "menu_doctors", "title": t[2][:24], "description": ""},
                {"id": "menu_reports", "title": "📋 My Reports"[:24], "description": ""},
                {"id": "menu_emergency", "title": t[3][:24], "description": ""},
                {"id": "menu_human", "title": t[4][:24], "description": ""},
            ]
        }]

        await self.whatsapp.send_interactive_list(
            clinic, phone,
            body=get_message("main_menu", lang),
            button_text="Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి"),
            sections=sections
        )

    async def _handle_main_menu(self, clinic: dict, phone: str,
        message: str,
        intent: str,
        patient: dict,
        lang: str
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

        if intent == "book_appointment" or message.lower() in ["book", "appointment", "बुक", "బుక్"]:
            await self._start_booking(clinic, phone, patient, lang)
        elif intent == "view_services":
            await self._show_services(clinic, phone, lang)
        elif intent == "doctor_availability":
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
                await self.whatsapp.send_text(clinic, phone, get_message("welcome_back", lang, name=first_name))
            
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
                await self.whatsapp.send_text(clinic, phone, get_message("invalid_input", lang))
                
            # Resend menu to help them navigate back to structured flows
            await self._send_main_menu(clinic, phone, lang)
            context = session.get("context", {})
            context["menu_shown"] = True
            await self.update_state(clinic, phone, "main_menu", context)

    async def _start_booking(self, clinic: dict, phone: str, patient: dict, lang: str) -> None:
        """Start the booking flow."""

        # Guard: Language must be set before proceeding
        if not patient.get("language"):
            await self._send_language_selection(clinic, phone)
            await self.update_state(clinic, phone, "selecting_language")
            return

        # Check if returning patient with name and language is set
        if patient.get("name") and patient.get("visit_count", 0) > 0 and patient.get("language"):
            patient_name = patient.get("name") or "there"
            first_name = patient_name.split()[0] if patient_name else "there"
            
            msg_str = {
                "en": f"Who is this appointment for, {first_name}?",
                "hi": f"यह अपॉइंटमेंट किसके लिए है, {first_name}?",
                "te": f"ఈ అపాయింట్‌మెంట్ ఎవరి కోసం, {first_name}?"
            }.get(lang, f"Who is this appointment for, {first_name}?")
            
            await self.whatsapp.send_interactive_buttons(
                clinic, phone,
                body=msg_str,
                buttons=[
                    {"id": "for_self", "title": "For Me" if lang == "en" else ("मेरे लिए" if lang == "hi" else "నా కోసం")},
                    {"id": "for_family", "title": "For Family" if lang == "en" else ("परिवार के लिए" if lang == "hi" else "కుటుంబం కోసం")}
                ]
            )
            from app.database import update_conversation
            await update_conversation(clinic["id"], phone, {
                "state": "collecting_name",
                "context": {"asked_for_whom": True}
            })
        else:
            # New patient, ask for name
            await self.whatsapp.send_text(clinic, phone, get_message("ask_name", lang))
            from app.database import update_conversation
            await update_conversation(clinic["id"], phone, {
                "state": "collecting_name",
                "context": {"for_self": True}
            })

    async def _handle_collecting_name(self, clinic: dict, phone: str,
        message: str,
        context: dict,
        patient: dict,
        lang: str
    ) -> None:
        """Handle name collection."""

        # Skip validation for button responses
        if message.lower() in [
            "self", "for me", "family", "for family",
            "for_self", "for_family",
            "మెరే లిఏ", "నా కోసం", "కుటుంబం కోసం",
            "मेरे लिए", "परिवार के लिए"
        ]:
            # These are handled by button handlers above, ignore here
            return

        # Handle button responses
        if message.lower() in ["self", "for me", "मेरे लिए", "నా కోసం"]:
            context["for_self"] = True
            context["booking_name"] = patient.get("name")
            await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
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
                    "te": "దయచేసి మీ పూర్తి పేరు చెప్పండి. \nఉదా: చైతన్య కుమార్"
                }.get(lang, "Please share both first and last name. \nExample: Chaitanya Kumar")
                await self.whatsapp.send_text(clinic, phone, msg)
            else:
                errors = {
                    "en": {
                        "too_short": "Name is too short. Please share your full name.",
                        "invalid_chars": "Name should contain only letters.",
                        "invalid_name": "That doesn't look like a name. \nPlease share the patient's full name."
                    },
                    "hi": {
                        "too_short": "नाम बहुत छोटा है। कृपया अपना पूरा नाम बताएं।",
                        "invalid_chars": "नाम में केवल अक्षर होने चाहिए।",
                        "invalid_name": "यह नाम जैसा नहीं लगता। \nकृपया मरीज़ का पूरा नाम बताएं।"
                    },
                    "te": {
                        "too_short": "పేరు చాలా చిన్నది. దయచేసి మీ పూర్తి పేరు చెప్పండి.",
                        "invalid_chars": "పేరులో అక్షరాలు మాత్రమే ఉండాలి.",
                        "invalid_name": "ఇది పేరులా అనిపించడం లేదు. \nదయచేసి రోగి పూర్తి పేరును పంచుకోండి."
                    }
                }
                lang_errors = errors.get(lang, errors["en"])
                error_msg = lang_errors.get(result, errors["en"].get(result, "Please enter a valid full name."))
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

    async def _handle_collecting_symptoms(self, clinic: dict, phone: str,
        message: str,
        context: dict,
        patient: dict,
        lang: str
    ) -> None:
        """Handle symptom collection."""
        
        last_symptom = context.get("last_symptom")
        if last_symptom == message.lower().strip():
            return  # same message, ignore
        context["last_symptom"] = message.lower().strip()
        await update_conversation(clinic["id"], phone, {"context": context})

        # Allow skip
        if message.lower() in ["skip", "no symptoms", "don't know", "none", "नहीं", "తెలియదు"]:
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
        if "chest pain" in msg_lower and context.get("symptom_followup") != "chest_pain":
            context["symptom_followup"] = "chest_pain"
            await self.whatsapp.send_interactive_buttons(
                clinic, phone,
                body="Is the chest pain sudden and severe, or mild and ongoing?",
                buttons=[
                    {"id": "chest_severe", "title": "Sudden & Severe"},
                    {"id": "chest_mild", "title": "Mild & Ongoing"}
                ]
            )
            await update_conversation(clinic["id"], phone, {"context": context})
            return
            
        if "back pain" in msg_lower and context.get("symptom_followup") != "back_pain":
            context["symptom_followup"] = "back_pain"
            await self.whatsapp.send_interactive_buttons(
                clinic, phone,
                body="Is it lower back pain or upper back/neck pain?",
                buttons=[
                    {"id": "back_lower", "title": "Lower Back"},
                    {"id": "back_upper", "title": "Upper/Neck"}
                ]
            )
            await update_conversation(clinic["id"], phone, {"context": context})
            return

        # Map symptoms to department
        symptom_result = await map_symptom_to_department(message, clinic)

        if symptom_result.get("suggested_department") is None:
            await self.whatsapp.send_text(
                clinic, phone, 
                {"en": "I didn't understand that. Please describe your symptoms.\nExample: fever, chest pain, tooth pain",
                 "hi": "मुझे समझ नहीं आया। अपने लक्षण बताएं।\nउदाहरण: बुखार, सीने में दर्द, दांत दर्द",
                 "te": "అర్థం కాలేదు. మీ లక్షణాలు వివరించండి.\nఉదా: జ్వరం, గుండె నొప్పి, పళ్ళు నొప్పి"}.get(lang, "Please describe your symptoms.")
            )
            return

        # Store suggestion in context
        context["suggested_department"] = symptom_result["suggested_department"]
        context["symptoms"] = message
        context["suggestion_reasoning"] = symptom_result["reasoning"]

        # Show suggestion (removed suggestion_reasoning from message template)
        await self.whatsapp.send_interactive_buttons(
            clinic, phone,
            body=f"Based on your concern, our {symptom_result['suggested_department']} team may be able to help. Shall I book there?",
            buttons=[
                {"id": "suggest_yes", "title": "Yes" if lang == "en" else ("हां" if lang == "hi" else "అవును")},
                {"id": "suggest_no", "title": "No" if lang == "en" else ("नहीं" if lang == "hi" else "కాదు")}
            ]
        )

        await self.update_state(clinic, phone, "suggesting_department", context)

    async def _handle_suggesting_department(self, clinic: dict, phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
        interactive_data: Optional[dict] = None
    ) -> None:
        """Handle department suggestion response."""
        button_id = interactive_data.get("id") if interactive_data else None
        msg_lower = message.lower().strip()
        
        is_yes = (
            button_id in ["yes", "suggest_yes"] or
            intent in ["accept_suggestion", "yes"] or
            msg_lower in ["yes", "అవును", "हाँ", "ha", "y", "हां"]
        )

        if is_yes:
            department = context.get("suggested_department")
            # Step 2: Query database directly
            from app.database import supabase
            response = supabase.table("doctors").select("*").eq("clinic_id", clinic["id"]).eq("department", department).eq("is_active", True).order("rating", desc=True).execute()
            doctors = response.data

            if doctors:
                logger.info(f"Doctors found: {len(doctors)}")
                
                # Step 3: Build WhatsApp LIST message
                sections = [{
                    "title": department,
                    "rows": [
                        {
                            "id": f"doc_{doc['id']}", 
                            "title": doc['name'][:24],
                            "description": f"{doc['specialization']} · ⭐{doc.get('rating', '4.5')} · ₹{doc['consultation_fee']}"[:72]
                        }
                        for doc in doctors
                    ]
                }]
                
                await self.whatsapp.send_interactive_list(
                    clinic, phone=phone,
                    header={"en": "Choose Your Doctor", "hi": "अपना डॉक्टर चुनें", "te": "మీ డాక్టర్‌ను ఎంచుకోండి"}.get(lang, "Choose Your Doctor"),
                    body=get_message("available_doctors_in", lang, dept=department),
                    button_text={"en": "Select Doctor", "hi": "डॉक्टर चुनें", "te": "డాక్టర్‌ ఎంచుకోండి"}.get(lang, "Select Doctor"),
                    sections=sections
                )
                
                context_update = {
                    "suggested_department": department,
                    "symptoms": context.get("symptoms"),
                    "department": department
                }
                await self.update_state(clinic, phone, "selecting_doctor", context_update)
            else:
                # Step 4: No doctors found
                await self.whatsapp.send_text(
                    clinic, phone,
                    f"No doctors available in {department} right now."
                )
                await self._show_department_list(clinic, phone, context, lang)
        else:
            # Show all departments
            await self._show_department_list(clinic, phone, context, lang)

    async def _show_department_list(self, clinic: dict, phone: str, context: dict, lang: str) -> None:
        """Show list of departments."""
        from app.services.tenant import has_feature
        
        if not has_feature(clinic, "multi_department"):
            dept = clinic.get("config", {}).get("default_department", "General Medicine")
            await self._show_doctor_list(clinic, phone, dept, context, lang)
            return

        from app.database import supabase
        result = supabase.table("doctors").select("department").eq("clinic_id", clinic["id"]).eq("is_active", True).execute()
        dept_names = list(set([r["department"] for r in (result.data or [])]))
        
        if not dept_names:
            dept_names = ["General Medicine"]

        rows = []
        for d in dept_names[:10]:  # limit to 10 for interactive list
            dept_id = f"dept_{d.lower().replace(' ', '_')}"
            rows.append({"id": dept_id, "title": d[:24], "description": ""})

        sections = [{
            "title": "Departments",
            "rows": rows
        }]

        msg = {
            "en": "No problem! Please choose a department:",
            "hi": "कोई बात नहीं! कृपया विभाग चुनें:",
            "te": "సరే! దయచేసి విభాగం ఎంచుకోండి:"
        }.get(lang, "Choose Department")

        await self.whatsapp.send_interactive_list(
            clinic, phone=phone,
            header="Choose Department",
            body=msg,
            button_text="Select",
            sections=sections
        )

        merged_context = {**context}
        await self.update_state(clinic, phone, "selecting_department", merged_context)

    async def _handle_selecting_department(self, clinic: dict, phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
        interactive_data: Optional[dict] = None
    ) -> None:
        """Handle manual department selection."""
        button_id = interactive_data.get("id", "") if interactive_data else ""
        
        # When patient selects from list
        if button_id.startswith("dept_") or button_id.startswith("svc_"):
            DEPT_MAP = {
                "dept_general_medicine": "General Medicine",
                "dept_cardiology": "Cardiology",
                "dept_dental": "Dental",
                "dept_orthopedics": "Orthopedics",
                "dept_gynecology": "Gynecology",
                "dept_pediatrics": "Pediatrics",
                "dept_ent": "ENT",
                "dept_dermatology": "Dermatology",
                "svc_general": "General Medicine",
                "svc_cardiology": "Cardiology",
                "svc_dental": "Dental",
                "svc_ortho": "Orthopedics",
                "svc_gynec": "Gynecology",
                "svc_pediatrics": "Pediatrics",
                "svc_ent": "ENT",
                "svc_derma": "Dermatology"
            }
            department = DEPT_MAP.get(button_id, "General Medicine")
            
            # Fetch doctors for selected department
            from app.database import supabase
            response = supabase.table("doctors").select("*").eq("clinic_id", clinic["id"]).eq("department", department).eq("is_active", True).order("rating", desc=True).execute()
            doctors = response.data
            
            if doctors:
                await self._show_doctor_list(clinic, phone, department, context, lang)
                # Note: _show_doctor_list automatically updates state to selecting_doctor
            else:
                await self.whatsapp.send_text(clinic, phone, f"No doctors available in {department} right now.")
                await self._show_department_list(clinic, phone, context, lang)
        else:
            # Re-show department list if they typed something invalid
            await self._show_department_list(clinic, phone, context, lang)

    async def _show_doctor_list(self, clinic: dict, phone: str, department: str, context: dict, lang: str) -> None:
        """Show list of doctors in a department."""
        doctors = await get_doctors(clinic["id"], department)

        if not doctors:
            await self.whatsapp.send_text(
                clinic, phone,
                f"Sorry, no doctors are currently available in {department}. Please try another department."
            )
            await self._show_department_list(clinic, phone, context, lang)
            return

        sections = [{
            "title": department[:24],
            "rows": [
                {
                    "id": f"doc_{doc['id']}",
                    "title": doc['name'][:24],
                    "description": f"{doc['specialization']} · ⭐{doc.get('rating', '4.5')} · ₹{doc['consultation_fee']}"[:72]
                }
                for doc in doctors
            ]
        }]

        await self.whatsapp.send_interactive_list(
            clinic, phone=phone,
            header={"en": "Choose Your Doctor", "hi": "अपना डॉक्टर चुनें", "te": "మీ డాక్టర్‌ను ఎంచుకోండి"}.get(lang, "Choose Your Doctor"),
            body=get_message("available_doctors_in", lang, dept=department),
            button_text={"en": "Select Doctor", "hi": "डॉक्टर चुनें", "te": "డాక్టర్‌ ఎంచుకోండి"}.get(lang, "Select Doctor"),
            sections=sections
        )

        context["department"] = department
        merged_context = {**context}
        await self.update_state(clinic, phone, "selecting_doctor", merged_context)

    async def _handle_selecting_doctor(self, clinic: dict, phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
        interactive_data: Optional[dict] = None
    ) -> None:
        """Handle doctor selection."""

        doctor_id = None
        if interactive_data and interactive_data.get("id", "").startswith("doc_"):
            doctor_id = interactive_data["id"].replace("doc_", "")
            
        if doctor_id:
            from app.database import supabase
            res = supabase.table("doctors").select("*").eq("clinic_id", clinic["id"]).eq("id", doctor_id).execute()
            doctor = res.data[0] if res.data else None
            doctor_name = doctor["name"] if doctor else message.strip()
        else:
            msg = message.lower().strip()
            
            # Check if it matches a department name
            DEPT_KEYWORDS = {
                "dental": "Dental", "teeth": "Dental", "tooth": "Dental",
                "cardiology": "Cardiology", "heart": "Cardiology",
                "general": "General Medicine", "medicine": "General Medicine",
                "ortho": "Orthopedics", "bone": "Orthopedics",
                "gynec": "Gynecology", "women": "Gynecology",
                "pediatric": "Pediatrics", "child": "Pediatrics",
                "ent": "ENT", "ear": "ENT",
                "skin": "Dermatology", "derma": "Dermatology",
            }
            
            matched_dept = None
            for keyword, dept in DEPT_KEYWORDS.items():
                if keyword in msg:
                    matched_dept = dept
                    break
            
            if matched_dept:
                # Patient is telling us which department they want
                from app.database import supabase
                response = supabase.table("doctors").select("*").eq("clinic_id", clinic["id"]).eq("department", matched_dept).eq("is_active", True).order("rating", desc=True).execute()
                doctors = response.data
                if doctors:
                    await self._show_doctor_list(clinic, phone, matched_dept, context, lang)
                else:
                    await self.whatsapp.send_text(clinic, phone, f"No doctors available in {matched_dept} right now.")
                    await self._show_department_list(clinic, phone, context, lang)
                return
            
            # If no department match, try to match doctor name
            from app.database import supabase
            response = supabase.table("doctors").select("*").eq("clinic_id", clinic["id"]).eq("is_active", True).execute()
            all_doctors = response.data
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

        if not doctor:
            # Implement Fallback: resend the list instead of just an error text
            fallback_msg = {
                "en": "Please select from the list below:",
                "hi": "कृपया नीचे दी गई सूची से चुनें:",
                "te": "దయచేసి దిగువ జాబితా నుండి ఎంచుకోండి:"
            }.get(lang, "Please select from the list below:")
            
            await self.whatsapp.send_text(clinic, phone, fallback_msg)
            if context.get("department"):
                await self._show_doctor_list(clinic, phone, context["department"], context, lang)
            else:
                await self._show_department_list(clinic, phone, context, lang)
            return

        context["doctor_name"] = doctor_name
        context["doctor"] = doctor

        # Ask for date — show interactive date picker
        context["doctor_name"] = doctor_name
        context["doctor"] = doctor
        merged_context = {**context}

        await self._show_date_picker(clinic, phone, merged_context, lang)
        await self.update_state(clinic, phone, "selecting_date", merged_context)

    async def _handle_selecting_date(self, clinic: dict, phone: str,
        message: str,
        context: dict,
        lang: str
    ) -> None:
        """Handle date selection."""
        from datetime import datetime, timedelta

        # Parse date from message
        date_str = None
        msg_lower = message.lower().strip()

        if msg_lower in ["today", "आज", "ఈరోజు"]:
            date_str = datetime.now().strftime("%Y-%m-%d")
        elif msg_lower in ["tomorrow", "कल", "రేపు"]:
            date_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
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
            await self.whatsapp.send_text(clinic, phone, "Please provide a valid date (e.g., 'today', 'tomorrow', or '2026-03-20').")
            return

        # Validate date is not in past
        selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        if selected_date < datetime.now().date():
            await self.whatsapp.send_text(clinic, phone, "Please choose a future date.")
            return

        # Check if date is within 30 days
        if selected_date > datetime.now().date() + timedelta(days=30):
            await self.whatsapp.send_text(clinic, phone, "Please choose a date within the next 30 days.")
            return

        context["appointment_date"] = date_str

        # Get available slots
        slots, reason = await get_available_slots(clinic["id"], context["doctor_name"], date_str)

        if not slots:
            date_display = selected_date.strftime('%d %b')
            
            # Inform the patient why the doctor is unavailable
            if reason == "doctor_on_leave":
                msg = {
                    "en": f"Dr. {context['doctor_name']} is on leave on {date_display}.",
                    "hi": f"डॉ. {context['doctor_name']} {date_display} को छुट्टी पर हैं।",
                    "te": f"డాక్టర్ {context['doctor_name']} {date_display} న సెలవులో ఉన్నారు."
                }.get(lang, f"Dr. {context['doctor_name']} is on leave on {date_display}.")
                await self.whatsapp.send_text(clinic, phone, msg)
            elif reason == "hospital_closed":
                msg = {
                    "en": f"The hospital is closed on {date_display} for a holiday.",
                    "hi": f"अस्पताल {date_display} को छुट्टी के कारण बंद है।",
                    "te": f"ఆసుపత్రి {date_display} న సెలవు కారణంగా మూసివేయబడింది."
                }.get(lang, f"The hospital is closed on {date_display} for a holiday.")
                await self.whatsapp.send_text(clinic, phone, msg)
            elif reason == "doctor_off_day":
                msg = {
                    "en": f"Dr. {context['doctor_name']} does not consult on this day of the week.",
                    "hi": f"डॉ. {context['doctor_name']} सप्ताह के इस दिन परामर्श नहीं देते हैं।",
                    "te": f"డా. {context['doctor_name']} వారంలో ఈ రోజున సంప్రదింపులు చేయరు."
                }.get(lang, f"Dr. {context['doctor_name']} does not work on this day of the week.")
                await self.whatsapp.send_text(clinic, phone, msg)

            # Find next available date
            next_date, next_slots, next_reason = await find_next_available_date(clinic["id"],
                context["doctor_name"],
                (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            )

            if next_reason == "no_availability_14_days" or not next_date:
                # Doctor fully booked or unavailable for long time, suggest others
                await self._suggest_other_doctors(clinic, phone, context, lang)
                return

            next_date_display = datetime.strptime(next_date, "%Y-%m-%d").strftime("%d %b")
            msg = {
                "en": f"Next available date for {context['doctor_name']} is {next_date_display}.",
                "hi": f"{context['doctor_name']} के लिए अगली उपलब्ध तारीख {next_date_display} है।",
                "te": f"{context['doctor_name']} కోసం తదుపరి అందుబాటులో ఉన్న తేదీ {next_date_display}."
            }.get(lang, f"Next available date is {next_date_display}.")
            await self.whatsapp.send_text(clinic, phone, msg)
            
            context["appointment_date"] = next_date
            slots = next_slots

        # Show slots
        await self._show_slot_list(clinic, phone, slots, context, lang)

    async def _show_date_picker(self, clinic: dict, phone: str, context: dict, lang: str) -> None:
        """Show a date picker with the next 7 available days."""
        from datetime import datetime, timedelta

        today = datetime.now().date()
        date_rows = []

        day_labels = {
            "en": ["Today", "Tomorrow"],
            "hi": ["आज", "कल"],
            "te": ["ఈరోజు", "రేపు"]
        }
        labels = day_labels.get(lang, day_labels["en"])

        for i in range(7):
            d = today + timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")
            if i == 0:
                title = f"{labels[0]} ({d.strftime('%d %b')})"
            elif i == 1:
                title = f"{labels[1]} ({d.strftime('%d %b')})"
            else:
                title = f"{d.strftime('%A, %d %b')}"
            date_rows.append({
                "id": f"date_{date_str}",
                "title": title[:24],
                "description": ""
            })

        sections = [{
            "title": "Select Date" if lang == "en" else ("तारीख चुनें" if lang == "hi" else "తేదీ ఎంచుకోండి"),
            "rows": date_rows
        }]

        await self.whatsapp.send_interactive_list(
            clinic, phone,
            body=get_message("select_date", lang),
            button_text="Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి"),
            sections=sections
        )

    async def _show_slot_list(self, clinic: dict, phone: str,
        slots: list,
        context: dict,
        lang: str
    ) -> None:
        """Show available time slots in 12-hour AM/PM format."""
        from datetime import datetime

        def to_ampm(time_24: str) -> str:
            """Convert 24h time string to 12h AM/PM."""
            try:
                t = datetime.strptime(time_24.strip(), "%H:%M")
                return t.strftime("%I:%M %p").lstrip("0")
            except ValueError:
                return time_24

        sections = [{
            "title": "Select Time" if lang == "en" else ("समय चुनें" if lang == "hi" else "సమయం ఎంచుకోండి"),
            "rows": [
                {"id": f"slot_{slot}", "title": to_ampm(slot), "description": ""}
                for slot in slots[:10]  # Max 10 slots
            ]
        }]

        await self.whatsapp.send_interactive_list(
            clinic, phone,
            body=get_message("select_slot", lang),
            button_text="Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి"),
            sections=sections
        )

        await self.update_state(clinic, phone, "selecting_slot", context)

    async def _handle_selecting_slot(self, clinic: dict, phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str
    ) -> None:
        """Handle slot selection."""

        if intent == "select_slot":
            time_str = message
        else:
            time_str = message.strip()

        context["appointment_time"] = time_str

        # Show confirmation
        await self._show_booking_confirmation(clinic, phone, context, lang)

    async def _show_booking_confirmation(self, clinic: dict, phone: str, context: dict, lang: str) -> None:
        """Show booking confirmation summary."""
        from datetime import datetime

        date_display = datetime.strptime(context["appointment_date"], "%Y-%m-%d").strftime("%d %b %Y")

        await self.whatsapp.send_interactive_buttons(
            clinic, phone,
            body=get_message(
                "confirm_booking",
                lang,
                name=context.get("booking_name", "Patient"),
                doctor=context["doctor_name"],
                department=context.get("department", ""),
                date=date_display,
                time=context["appointment_time"]
            ),
            buttons=[
                {"id": "confirm_yes", "title": "Confirm" if lang == "en" else ("पुष्टि" if lang == "hi" else "నిర్ధారించు")},
                {"id": "confirm_no", "title": "Edit" if lang == "en" else ("संपादन" if lang == "hi" else "మార్చు")}
            ]
        )

        await self.update_state(clinic, phone, "confirming_booking", context)

    async def _handle_confirming_booking(self, clinic: dict, phone: str,
        message: str,
        intent: str,
        context: dict,
        patient: dict,
        lang: str
    ) -> None:
        """Handle booking confirmation — payment-gated when Razorpay is configured.

        Two modes:
          A) Razorpay configured → payment-gated flow (pending_payment → webhook → confirmed)
          B) Razorpay NOT configured → direct booking (original flow, confirmed immediately)
        """

        if intent in ["confirm_booking", "yes"]:
            from datetime import datetime

            # ── Check if Razorpay is configured (per-clinic first, global fallback) ──
            from app.services.payment import get_razorpay_creds
            _rz_key_id, _rz_key_secret, _ = get_razorpay_creds(clinic)
            razorpay_configured = bool(_rz_key_id and _rz_key_secret)

            if razorpay_configured:
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
                )


                if result["success"]:
                    amount_rupees = result["amount_paise"] / 100
                    date_display = datetime.strptime(context["appointment_date"], "%Y-%m-%d").strftime("%d %b %Y")

                    payment_msg = {
                        "en": (
                            f"💳 *Payment Required to Confirm Booking*\n\n"
                            f"👨‍⚕️ Doctor: {context['doctor_name']}\n"
                            f"📅 Date: {date_display}\n"
                            f"🕐 Time: {context['appointment_time']}\n"
                            f"💰 Amount: ₹{amount_rupees:.0f}\n\n"
                            f"⏱️ *This slot is held for 10 minutes.* Pay before it expires.\n\n"
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
                            f"⏱️ *यह स्लॉट 10 मिनट के लिए होल्ड है।* समय से पहले भुगतान करें।\n\n"
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
                            f"⏱️ *ఈ స్లాట్ 10 నిమిషాలు హోల్డ్ చేయబడింది.* గడువులోపు చెల్లించండి.\n\n"
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
                            f"⏱️ *This slot is held for 10 minutes.* Pay before it expires.\n\n"
                            f"👉 Click below to pay securely via Razorpay:\n"
                            f"{result['payment_link']}\n\n"
                            f"_Refundable if cancelled {settings.refund_window_hours}+ hours before appointment. "
                            f"No-show bookings are non-refundable._"
                        )

                    await self.whatsapp.send_text(clinic, phone, payment_msg)

                    await log_analytics_event(clinic["id"], phone, "payment_link_sent", department=context.get("department"))

                    # Save booking context and transition to awaiting_payment
                    context["booking_id"] = result["booking_id"]
                    context["razorpay_order_id"] = result["razorpay_order_id"]
                    context["booking_ref"] = result["booking_ref"]
                    await self.update_state(clinic, phone, "awaiting_payment", context)

                elif result.get("reason") == "slot_taken":
                    await self.whatsapp.send_text(
                        clinic, phone,
                        get_message("slot_taken", lang, doctor=context["doctor_name"])
                    )
                    slots, _ = await get_available_slots(clinic["id"], context["doctor_name"], context["appointment_date"])
                    if slots:
                        await self._show_slot_list(clinic, phone, slots[:3], context, lang)
                    else:
                        await self._suggest_other_doctors(clinic, phone, context, lang)
                else:
                    error_msg = {
                        "en": "Sorry, we couldn't process your booking right now. Please try again.",
                        "hi": "क्षमा करें, अभी बुकिंग प्रक्रिया नहीं हो सकी। कृपया पुनः प्रयास करें।",
                        "te": "క్షమించండి, మీ బుకింగ్ ప్రాసెస్ కాలేదు. దయచేసి మళ్ళీ ప్రయత్నించండి.",
                    }.get(lang, "Sorry, we couldn't process your booking right now. Please try again.")
                    await self.whatsapp.send_text(clinic, phone, error_msg)
                    await self.update_state(clinic, phone, "main_menu")
                    await self._send_main_menu(clinic, phone, lang)
            else:
                # ═══ PATH B: Direct booking (Razorpay NOT configured) ═══
                appointment_data = {
                    "patient_id": patient.get("id"),
                    "patient_phone": phone,
                    "patient_name": context.get("booking_name", "Patient"),
                    "department": context.get("department", "General Medicine"),
                    "doctor_name": context["doctor_name"],
                    "appointment_date": context["appointment_date"],
                    "appointment_time": context["appointment_time"],
                    "symptoms": context.get("symptoms", ""),
                    "status": "confirmed"
                }

                result = await book_appointment(clinic["id"], appointment_data)

                if result["success"]:
                    appointment = result["appointment"]
                    date_display = datetime.strptime(context["appointment_date"], "%Y-%m-%d").strftime("%d %b %Y")

                    await self.whatsapp.send_text(
                        clinic, phone,
                        get_message(
                            "booking_confirmed",
                            lang,
                            ref=appointment["booking_ref"],
                            doctor=context["doctor_name"],
                            date=date_display,
                            time=context["appointment_time"]
                        )
                    )

                    await log_analytics_event(clinic["id"], phone, "appointment_booked", department=context.get("department"))

                    import asyncio
                    await asyncio.sleep(2)

                    # Pre-appointment instructions
                    dept_instruction = {
                        "en": f"Instructions for {context.get('department')}: Please arrive 15 minutes early and bring relevant medical records.",
                        "hi": f"{context.get('department')} के लिए निर्देश: कृपया 15 मिनट पहले पहुंचें और प्रासंगिक चिकित्सा रिकॉर्ड लाएं।",
                        "te": f"{context.get('department')} కోసం సూచనలు: దయచేసి సంబంధిత మెడికల్ రికార్డులను తీసుకుని 15 నిమిషాల ముందుగా రండి."
                    }.get(lang, "Please arrive 15 mins early.")
                    await self.whatsapp.send_text(clinic, phone, dept_instruction)

                    follow_up_msg = {
                        "en": "What would you like to do?",
                        "hi": "आप आगे क्या करना चाहेंगे?",
                        "te": "మీరు ఇంకా ఏమి చేయాలనుకుంటున్నారు?"
                    }.get(lang, "What would you like to do?")
                    await self.whatsapp.send_interactive_buttons(
                        clinic, phone,
                        body=follow_up_msg,
                        buttons=[
                            {"id": "book_another", "title": "Book Appointment"},
                            {"id": "main_menu", "title": "Main Menu"}
                        ]
                    )

                    await self.update_state(clinic, phone, "main_menu")
                else:
                    if result.get("reason") == "slot_taken":
                        await self.whatsapp.send_text(
                            clinic, phone,
                            get_message("slot_taken", lang, doctor=context["doctor_name"])
                        )
                        slots, _ = await get_available_slots(clinic["id"], context["doctor_name"], context["appointment_date"])
                        if slots:
                            await self._show_slot_list(clinic, phone, slots[:3], context, lang)
                        else:
                            await self._suggest_other_doctors(clinic, phone, context, lang)
                    else:
                        await self.whatsapp.send_text(
                            clinic, phone,
                            get_message("booking_failed", lang, phone=clinic["whatsapp_number"])
                        )
                        await self.update_state(clinic, phone, "main_menu")
                        await self._send_main_menu(clinic, phone, lang)
        else:
            # Edit booking - go back to doctor selection
            await self._show_doctor_list(clinic, phone, context.get("department", "General Medicine"), context, lang)

    async def _handle_awaiting_payment(self, clinic: dict, phone: str,
        message: str,
        context: dict,
        patient: dict,
        lang: str
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
                supabase.table("appointments").update({
                    "status": "cancelled"
                }).eq("id", booking_id).eq("status", "pending_payment").execute()

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
                result = supabase.table("appointments").select("status, booking_ref").eq("id", booking_id).execute()
                if result.data:
                    status = result.data[0]["status"]
                    if status == "confirmed":
                        confirmed_msg = {
                            "en": f"✅ Your payment has been received and booking *{result.data[0].get('booking_ref', '')}* is confirmed!",
                            "hi": f"✅ आपका भुगतान प्राप्त हो गया है और बुकिंग *{result.data[0].get('booking_ref', '')}* पुष्ट है!",
                            "te": f"✅ మీ చెల్లింపు అందింది మరియు బుకింగ్ *{result.data[0].get('booking_ref', '')}* నిర్ధారించబడింది!",
                        }.get(lang, f"✅ Payment received — booking {result.data[0].get('booking_ref', '')} confirmed!")
                        await self.whatsapp.send_text(clinic, phone, confirmed_msg)
                        await self.update_state(clinic, phone, "main_menu")
                        await self._send_main_menu(clinic, phone, lang)
                        return
                    elif status == "expired":
                        expired_msg = {
                            "en": "⏰ Your payment window has expired. The slot has been released. Would you like to book again?",
                            "hi": "⏰ भुगतान का समय समाप्त हो गया। स्लॉट खाली हो गया है। क्या आप फिर से बुक करना चाहेंगे?",
                            "te": "⏰ చెల్లింపు సమయం ముగిసింది. స్లాట్ విడుదల చేయబడింది. మళ్ళీ బుక్ చేయాలనుకుంటున్నారా?",
                        }.get(lang, "⏰ Payment window expired. Slot released. Book again?")
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

    async def _suggest_other_doctors(self, clinic: dict, phone: str, context: dict, lang: str) -> None:
        """Suggest other doctors when selected doctor is fully booked."""
        department = context.get("department", "General Medicine")
        exclude_doctor = context["doctor_name"]

        doctors = await get_doctors(clinic["id"], department)
        available = []

        from datetime import datetime, timedelta

        for doc in doctors:
            if doc["name"] == exclude_doctor:
                continue
            for i in range(7):
                check_date = (datetime.now() + timedelta(days=i+1)).strftime("%Y-%m-%d")
                slots, _ = await get_available_slots(clinic["id"], doc["name"], check_date)
                if slots:
                    date_display = datetime.strptime(check_date, "%Y-%m-%d").strftime("%d %b")
                    available.append({
                        "name": doc["name"],
                        "specialization": doc["specialization"],
                        "next_date": date_display,
                        "next_slot": slots[0]
                    })
                    break

        if available:
            await self.whatsapp.send_text(
                clinic, phone,
                get_message("doctor_fully_booked", lang, doctor=exclude_doctor, department=department)
            )

            sections = [{
                "title": "Available Doctors" if lang == "en" else ("उपलब्ध डॉक्टर" if lang == "hi" else "అందుబాటులో ఉన్న డాక్టర్లు"),
                "rows": [
                    {
                        "id": f"doc_{i}_{doc['name']}"[:200],
                        "title": doc['name'][:24],
                        "description": f"Available {doc['next_date']}"[:72]
                    }
                    for i, doc in enumerate(available[:10])
                ]
            }]

            await self.whatsapp.send_interactive_list(
                clinic, phone,
                body="Select another doctor:",
                button_text="Select",
                sections=sections
            )
        else:
            await self.whatsapp.send_text(
                clinic, phone,
                get_message("no_doctors_available", lang, department=department, phone=clinic["whatsapp_number"])
            )
            await self._send_main_menu(clinic, phone, lang)

    async def _handle_emergency(self, clinic: dict, phone: str, lang: str) -> None:
        """Handle emergency situation."""
        await self.whatsapp.send_text(clinic, phone, get_message("emergency", lang))

        # Send location if available
        if settings.hospital_maps_link:
            await self.whatsapp.send_text(
                clinic, phone,
                f"Emergency location: {settings.hospital_maps_link}\nAddress: {settings.hospital_address}"
            )

        await self.update_state(clinic, phone, "main_menu")
        await log_analytics_event(clinic["id"], phone, "emergency_detected")

    async def _handle_opt_out(self, clinic: dict, phone: str, patient: dict, lang: str) -> None:
        """Handle opt-out request."""
        await update_patient(clinic["id"], phone, {
            "opted_in": False,
            "opted_out_at": "now()"
        })

        await self.whatsapp.send_text(clinic, phone, get_message("opt_out_confirm", lang))
        await log_analytics_event(clinic["id"], phone, "opt_out")

    async def _handle_data_deletion(self, clinic: dict, phone: str, patient: dict, lang: str) -> None:
        """Handle data deletion request."""
        from app.database import delete_patient_data

        await delete_patient_data(clinic["id"], phone)
        await self.whatsapp.send_text(clinic, phone, get_message("data_deleted", lang))
        await log_analytics_event(clinic["id"], phone, "data_deleted")

    async def _handle_human_escalation(self, clinic: dict, phone: str, lang: str) -> None:
        """Handle human escalation request."""
        await self.whatsapp.send_text(
            clinic, phone,
            get_message("human_escalation", lang, phone=clinic["whatsapp_number"])
        )
        await self.update_state(clinic, phone, "escalated_to_human")
        await log_analytics_event(clinic["id"], phone, "human_escalation")

    async def _show_services(self, clinic: dict, phone: str, lang: str) -> None:
        """Show hospital services."""
        await self.whatsapp.send_interactive_list(
            clinic, phone=phone,
            header={"en": "Our Services", "hi": "हमारी सेवाएँ", "te": "మా సేవలు"}.get(lang, "Our Services"),
            body=get_message("our_services_body", lang),
            button_text={"en": "Select", "hi": "चुनें", "te": "ఎంచుకోండి"}.get(lang, "Select"),
            sections=[{
                "title": "Available Services"[:24],
                "rows": [
                    {"id": "svc_general",    "title": "General Medicine"[:24],
                     "description": "Fever, cold, general checkups"[:72]},
                    {"id": "svc_cardiology", "title": "Cardiology"[:24],
                     "description": "Heart-related concerns"[:72]},
                    {"id": "svc_dental",     "title": "Dental"[:24],
                     "description": "Teeth and oral care"[:72]},
                    {"id": "svc_ortho",      "title": "Orthopedics"[:24],
                     "description": "Bones, joints, fractures"[:72]},
                    {"id": "svc_gynec",      "title": "Gynecology"[:24],
                     "description": "Women's health"[:72]},
                    {"id": "svc_pediatrics", "title": "Pediatrics"[:24],
                     "description": "Child healthcare"[:72]},
                    {"id": "svc_ent",        "title": "ENT"[:24],
                     "description": "Ear, nose, throat"[:72]},
                    {"id": "svc_derma",      "title": "Dermatology"[:24],
                     "description": "Skin concerns"[:72]},
                ]
            }]
        )

    async def _show_doctors(self, clinic: dict, phone: str, lang: str) -> None:
        """Show available doctors."""
        from app.database import supabase
        response = supabase.table("doctors").select("*").eq("clinic_id", clinic["id"]).eq("is_active", True).order("department").execute()
        doctors = response.data

        sections = []
        dept_groups = {}
        for doc in doctors:
            dept = doc.get("department", "General Medicine")
            if dept not in dept_groups:
                dept_groups[dept] = []
            dept_groups[dept].append(doc)

        import collections
        # Sort dept_groups alphabetically or logically if desired. Here we just take up to 10 sections max.
        for dept, docs in list(dept_groups.items())[:10]:
            sections.append({
                "title": dept[:24],
                "rows": [
                    {
                        "id": f"view_doc_{doc['id']}",
                        "title": doc["name"][:24],
                        "description": f"{doc['specialization']} | Rs.{doc['consultation_fee']}"[:72]
                    }
                    for doc in docs[:10]  # whatsapp limit max 10 rows per section
                ]
            })

        await self.whatsapp.send_interactive_list(
            clinic, phone=phone,
            header={"en": "Our Doctors", "hi": "हमारे डॉक्टर", "te": "మా డాక్టర్లు"}.get(lang, "Our Doctors"),
            body=get_message("our_doctors_body", lang),
            button_text={"en": "Select", "hi": "चुनें", "te": "ఎంచుకోండి"}.get(lang, "Select"),
            sections=sections[:10]
        )

    async def _handle_cancel_request(self, clinic: dict, phone: str, patient: dict, lang: str) -> None:
        """Handle appointment cancellation request."""
        from app.database import get_patient_appointments, cancel_appointment

        appointments = await get_patient_appointments(clinic["id"], phone, status="confirmed")

        if not appointments:
            await self.whatsapp.send_text(clinic, phone, "You don't have any confirmed appointments to cancel.")
            await self._send_main_menu(clinic, phone, lang)
            return

        # Show appointments to cancel
        sections = [{
            "title": "Select to Cancel",
            "rows": [
                {
                    "id": f"cancel_{appt['id']}",
                    "title": f"{appt['doctor_name'][:20]}",
                    "description": f"{appt['appointment_date']} {appt['appointment_time']}"[:72]
                }
                for appt in appointments[:10]
            ]
        }]

        await self.whatsapp.send_interactive_list(
            clinic, phone,
            body="Which appointment would you like to cancel?",
            button_text="Select",
            sections=sections
        )

    async def _handle_reschedule_request(self, clinic: dict, phone: str, patient: dict, lang: str) -> None:
        """Handle reschedule request."""
        await self.whatsapp.send_text(
            clinic, phone,
            "To reschedule, please call us directly: " + clinic["whatsapp_number"]
        )
        await self._send_main_menu(clinic, phone, lang)


    async def _handle_view_reports(self, clinic: dict, phone: str, lang: str) -> None:
        """Handle 'My Reports' menu selection."""
        from app.services.tenant import has_feature
        if not has_feature(clinic, "lab_reports"):
            await self.whatsapp.send_text(
                clinic, phone,
                "Lab report delivery is not available at this facility via WhatsApp. "
                "Please visit the hospital reception to collect your reports."
            )
            await self._send_main_menu(clinic, phone, lang)
            return

        from app.services.lab_reports import LabReportService
        reports = await LabReportService().get_reports_by_phone(phone, clinic["id"])

        if not reports:
            await self.whatsapp.send_text(
                clinic, phone,
                "📋 No reports found for your number. Please visit the hospital or contact reception."
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

        lines.append("\nReply with the report number to download it. Reply 0 to go back to main menu.")
        await self.whatsapp.send_text(clinic, phone, "\n".join(lines))

        # Save reports list in context
        await self.update_state(clinic, phone, "viewing_reports", {"available_reports": recent})

    async def _handle_viewing_reports(self, clinic: dict, phone: str, message: str, session: dict, lang: str) -> None:
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
                await self.whatsapp.send_text(clinic, phone, "📤 Sending your report now...")

                from app.services.lab_reports import LabReportService
                try:
                    await LabReportService().resend_report(selected["id"])
                    await self.whatsapp.send_text(
                        clinic, phone,
                        "✅ Report sent! You can save it directly from WhatsApp. Need anything else? Reply with *Menu* to return."
                    )
                except Exception as e:
                    logger.error(f"Failed to resend report: {e}")
                    await self.whatsapp.send_text(
                        clinic, phone,
                        "Sorry, we could not send the report right now. Please try again later or contact the hospital."
                    )

                await self.update_state(clinic, phone, "main_menu", {"menu_shown": False})
                return
            else:
                await self.whatsapp.send_text(clinic, phone, "Please reply with a number from the list, or reply 0 to go back.")
                return
        except ValueError:
            await self.whatsapp.send_text(clinic, phone, "Please reply with a number from the list, or reply 0 to go back.")
            return


# Global instance
conversation_manager = ConversationManager()
