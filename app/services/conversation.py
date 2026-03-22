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

logger = logging.getLogger(__name__)


async def get_lang(phone: str) -> str:
    """Get language for a patient from database."""
    try:
        from app.database import supabase
        result = supabase.table("patients").select("language").eq("phone", phone).single().execute()
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
    MANAGING_APPOINTMENT = "managing_appointment"
    RESCHEDULING = "rescheduling"
    EMERGENCY = "emergency"
    ESCALATED_TO_HUMAN = "escalated_to_human"
    AWAITING_DATA_DELETION = "awaiting_data_deletion"


class ConversationManager:
    """Manages conversation state and flow."""

    async def update_state(self, phone: str, new_state: str, new_context: dict = None) -> None:
        if new_context is None:
            new_context = {}
        from app.database import get_conversation
        from app.database import supabase
        session = await get_conversation(phone)
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
        }).eq("phone", phone).execute()

    async def get_patient_language(self, phone: str) -> str:
        from app.database import supabase
        patient = supabase.table("patients").select("language").eq("phone", phone).execute()
        if patient.data and patient.data[0].get("language"):
            return patient.data[0]["language"]
        return "en"

    def __init__(self):
        self.whatsapp = whatsapp_service

    async def handle_message(
        self,
        phone: str,
        message: str,
        message_type: str = "text",
        message_id: Optional[str] = None,
        interactive_data: Optional[dict] = None
    ) -> None:
        """Handle incoming message with all guards."""

        # Guard 1: Duplicate webhook delivery
        session = await get_or_create_conversation(phone)
        if message_id and session.get("last_processed_message_id") == message_id:
            logger.info(f"Duplicate dropped: {message_id}")
            return

        if message_id:
            await update_conversation(phone, {"last_processed_message_id": message_id})
            await self.whatsapp.mark_as_read(message_id)

        # Get or create patient
        patient = await get_patient_by_phone(phone)
        if not patient:
            patient = await create_patient(phone)
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
                await update_conversation(phone, {
                    "state": "main_menu",
                    "context": {},
                    "booking_context_expires_at": None
                })
                await self.whatsapp.send_text(phone, get_message("session_timeout", lang))
                await self._send_main_menu(phone, lang)
                return

        # Reset booking timer on every message while mid-booking
        if session["state"] in mid_booking_states:
            expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            await update_conversation(phone, {"booking_context_expires_at": expires})

        # Update session expiry (24 hours from now)
        session_expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        await update_conversation(phone, {"session_expires_at": session_expires})

        # Detect intent
        intent = await detect_intent(message)

        # Handle interactive button responses FIRST (before guards)
        if message_type == "interactive" and interactive_data:
            button_id = interactive_data.get("id", "")
            if button_id in ["en", "hi", "te", "lang_en", "lang_hi", "lang_te"]:
                intent = "select_language"
            elif button_id == "self":
                intent = "booking_for_self"
            elif button_id == "family":
                intent = "booking_for_family"
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
                intent = "view_service"
                svc_map = {
                    "svc_general": "General Medicine",
                    "svc_cardiology": "Cardiology",
                    "svc_dental": "Dental",
                    "svc_ortho": "Orthopedics",
                    "svc_gynec": "Gynecology",
                    "svc_pediatrics": "Pediatrics",
                    "svc_ent": "ENT",
                    "svc_derma": "Dermatology"
                }
                message = svc_map.get(button_id, "General Medicine")
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
            elif button_id in ["menu_book", "menu_services", "menu_doctors", "menu_emergency", "menu_human"]:
                intent_map = {
                    "menu_book": "book_appointment",
                    "menu_services": "view_services",
                    "menu_doctors": "doctor_availability",
                    "menu_emergency": "emergency",
                    "menu_human": "human_escalation"
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
                phone,
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
            res = supabase.table("doctors").select("*").eq("id", message).execute()
            if res.data:
                doc = res.data[0]
                context = session.get("context", {})
                context["doctor"] = doc
                context["doctor_name"] = doc["name"]
                context["department"] = doc["department"]
                context["selected_doctor_id"] = message
                lang = await get_lang(phone)
                await self._show_date_picker(phone, context, lang)
                await self.update_state(phone, "selecting_date", context)
            return
        elif intent == "view_service":
            lang = await get_lang(phone)
            context = session.get("context", {})
            department = message
            from app.database import supabase
            response = supabase.table("doctors").select("*").eq("department", department).eq("is_active", True).order("rating", desc=True).execute()
            doctors = response.data
            
            if doctors:
                await self._show_doctor_list(phone, department, context, lang)
            else:
                await self.whatsapp.send_text(phone, f"No doctors available in {department} right now.")
                await self._send_main_menu(phone, lang)
            return

        # Process based on state and intent
        await self._process_state(phone, message, intent, session, patient, lang, interactive_data)

    async def _process_state(
        self,
        phone: str,
        message: str,
        intent: str,
        session: dict,
        patient: dict,
        lang_ignored: str,
        interactive_data: Optional[dict] = None
    ) -> None:
        """Process message based on current state."""
        lang = await get_lang(phone)
        
        state = session.get("state", "idle")
        context = session.get("context", {})

        # Global guard: Language must be set before any interaction (except selecting_language)
        if state != "selecting_language" and not patient.get("language"):
            await self._send_language_selection(phone)
            await self.update_state(phone, "selecting_language")
            return

        # Emergency can trigger from ANY state
        if intent == "emergency":
            await self._handle_emergency(phone, lang)
            return

        # Opt-out can trigger from ANY state
        if intent == "opt_out":
            await self._handle_opt_out(phone, patient, lang)
            return

        # Data deletion request
        if intent == "data_deletion_request":
            await self._handle_data_deletion(phone, patient, lang)
            return

        # Human escalation
        if intent == "human_escalation":
            await self._handle_human_escalation(phone, lang)
            return

        # Language change request (but NOT when already selecting language - let state machine handle it)
        if state != "selecting_language" and (
            intent in ["change_language", "select_language"] or message.lower() in [
                "change language", "भाषा बदलें", "భాష మార్చు"
            ]
        ):
            await self._send_language_selection(phone)
            await self.update_state(phone, "selecting_language")
            return

        # State machine
        if state == "idle":
            await self._handle_idle(phone, message, intent, patient, lang)
        elif state == "selecting_language":
            await self._handle_selecting_language(phone, message, patient, interactive_data)
        elif state == "awaiting_consent":
            await self._handle_awaiting_consent(phone, message, patient, lang, interactive_data)
        elif state == "main_menu":
            await self._handle_main_menu(phone, message, intent, patient, lang)
        elif state == "collecting_name":
            await self._handle_collecting_name(phone, message, context, patient, lang)
        elif state == "collecting_symptoms":
            await self._handle_collecting_symptoms(phone, message, context, patient, lang)
        elif state == "suggesting_department":
            await self._handle_suggesting_department(phone, message, intent, context, lang, interactive_data)
        elif state == "selecting_department":
            await self._handle_selecting_department(phone, message, intent, context, lang, interactive_data)
        elif state == "selecting_doctor":
            await self._handle_selecting_doctor(phone, message, intent, context, lang, interactive_data)
        elif state == "selecting_date":
            await self._handle_selecting_date(phone, message, context, lang)
        elif state == "selecting_slot":
            await self._handle_selecting_slot(phone, message, intent, context, lang)
        elif state == "confirming_booking":
            await self._handle_confirming_booking(phone, message, intent, context, patient, lang)
        elif state == "emergency":
            # Patient was in emergency state — process their new message normally
            # Reset to main_menu and handle as a main_menu interaction
            await self.update_state(phone, "main_menu")
            await self._handle_main_menu(phone, message, intent, patient, lang)
        else:
            # Unknown state, reset to main menu
            await self.update_state(phone, "main_menu")
            await self._send_main_menu(phone, lang)

    async def _handle_idle(self, phone: str, message: str, intent: str, patient: dict, lang: str) -> None:
        """Handle idle state - first interaction."""
        # Check if returning patient with language already set
        existing_lang = patient.get("language")
        has_visited = patient.get("visit_count", 0) > 0
        
        if existing_lang and existing_lang in ["en", "hi", "te"] and has_visited:
            # Returning patient — skip language picker
            if not patient.get("data_consent"):
                from app.database import get_session
                session = await get_session(phone)
                if session.get("state") == "awaiting_consent":
                    return  # already sent, don't send again

                await self.whatsapp.send_interactive_buttons(
                    phone,
                    body=get_message("consent_request", existing_lang),
                    buttons=[
                        {"id": "consent_yes", "title": "Yes" if existing_lang == "en" else ("हाँ" if existing_lang == "hi" else "అవును")},
                        {"id": "consent_no", "title": "No" if existing_lang == "en" else ("नहीं" if existing_lang == "hi" else "కాదు")}
                    ]
                )
                await self.update_state(phone, "awaiting_consent", {})
            else:
                patient_name = patient.get("name") or "there"
                first_name = patient_name.split()[0] if patient_name else "there"
                await self.whatsapp.send_text(phone, get_message("welcome_back", existing_lang, name=first_name))
                await self._send_main_menu(phone, existing_lang)
                await self.update_state(phone, "main_menu", {})
            return
        
        # New patient OR language not set → ALWAYS show language picker
        # Do NOT read the message content
        # Do NOT detect language from message
        # Do NOT set any language
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"IDLE: phone={phone}, existing_lang={patient.get('language')}, visits={patient.get('visit_count')}")

        await self._send_language_selection(phone)
        await self.update_state(phone, "selecting_language", {})
        return

    async def _send_language_selection(self, phone: str) -> None:
        """Send language selection buttons."""
        from app.config import settings
        body_text = f"Welcome to {settings.hospital_name} 🏥\nनमस्ते | నమస్కారం\n\nPlease select your language:\nअपनी भाषा चुनें | మీ భాష ఎంచుకోండి"
        await self.whatsapp.send_interactive_buttons(
            phone,
            body=body_text,
            buttons=[
                {"id": "lang_en", "title": "English"},
                {"id": "lang_hi", "title": "हिंदी"},
                {"id": "lang_te", "title": "తెలుగు"}
            ]
        )

    async def _handle_selecting_language(self, phone: str, message: str, patient: dict, interactive_data: Optional[dict] = None) -> None:
        """Handle language selection."""
        if interactive_data and interactive_data.get("id"):
            button_id = interactive_data.get("id", "")
            if button_id.startswith("lang_"):
                selected = button_id.replace("lang_", "")
            elif button_id in ["en", "hi", "te"]:
                selected = button_id
            else:
                # Invalid button fallback
                await self._send_language_selection(phone)
                return
        else:
            # Reject text inputs and force the picker usage
            await self._send_language_selection(phone)
            return

        # Validate selected language
        if selected not in ["en", "hi", "te"]:
            selected = "en"

        # Update patient language
        await update_patient(phone, {"language": selected})

        # Check data consent - proceed to consent, NOT language picker again
        consent = patient.get("data_consent")
        if consent is None or consent is False:
            from app.database import get_conversation
            session = await get_conversation(phone)
            state = session.get("state")
            if state == "awaiting_consent":
                return  # already sent consent, don't send again
                
            if state == "selecting_language":
                await self.whatsapp.send_interactive_buttons(
                    phone,
                    body=get_message("consent_request", selected),
                    buttons=[
                        {"id": "consent_yes", "title": "Yes" if selected == "en" else ("हाँ" if selected == "hi" else "అవును")},
                        {"id": "consent_no", "title": "No" if selected == "en" else ("नहीं" if selected == "hi" else "కాదు")}
                    ]
                )
                await self.update_state(phone, "awaiting_consent", {})
            return

        # Get welcome message in selected language
        await self.whatsapp.send_text(phone, get_message("welcome", selected))
        await self.whatsapp.send_text(phone, get_message("disclaimer", selected))
        await self._send_main_menu(phone, selected)
        await self.update_state(phone, "main_menu")

    async def _handle_awaiting_consent(self, phone: str, message: str, patient: dict, lang: str, interactive_data: Optional[dict] = None) -> None:
        """Handle data consent response."""
        button_id = interactive_data.get("id") if interactive_data else None
        msg_lower = message.lower().strip()

        if button_id == "consent_yes" or msg_lower in ["yes", "y", "ha", "हां", "అవును"]:
            await update_patient(phone, {"data_consent": True, "data_consent_at": "now()"})
            await self.whatsapp.send_text(phone, get_message("welcome", lang))
            await self.whatsapp.send_text(phone, get_message("disclaimer", lang))
            await self._send_main_menu(phone, lang)
            await self.update_state(phone, "main_menu")
        elif button_id == "consent_no" or msg_lower in ["no", "n", "nahin", "नहीं", "కాదు"]:
            await update_patient(phone, {"data_consent": False})
            await self.whatsapp.send_text(phone, get_message("welcome", lang))
            await self.whatsapp.send_text(phone, get_message("disclaimer", lang))
            await self._send_main_menu(phone, lang)
            await self.update_state(phone, "main_menu")
        else:
            await self.whatsapp.send_interactive_buttons(
                phone,
                body=get_message("consent_request", lang),
                buttons=[
                    {"id": "consent_yes", "title": "Yes" if lang == "en" else ("हाँ" if lang == "hi" else "అవును")},
                    {"id": "consent_no", "title": "No" if lang == "en" else ("नहीं" if lang == "hi" else "కాదు")}
                ]
            )

    async def _send_main_menu(self, phone: str, lang: str) -> None:
        """Send main menu with buttons."""
        titles = {
            "en": ["Book Appointment", "Our Services", "Our Doctors", "Emergency", "Talk to Staff"],
            "hi": ["Book Appointment", "Our Services", "Our Doctors", "Emergency", "Talk to Staff"],
            "te": ["Book Appointment", "Our Services", "Our Doctors", "Emergency", "Talk to Staff"]
        }

        t = titles.get(lang, titles["en"])

        await self.whatsapp.send_interactive_buttons(
            phone,
            body=get_message("main_menu", lang),
            buttons=[
                {"id": "menu_book", "title": t[0][:20]},
                {"id": "menu_services", "title": t[1][:20]},
                {"id": "menu_doctors", "title": t[2][:20]},
                {"id": "menu_emergency", "title": t[3][:20]},
                {"id": "menu_human", "title": t[4][:20]}
            ]
        )

    async def _handle_main_menu(
        self,
        phone: str,
        message: str,
        intent: str,
        patient: dict,
        lang: str
    ) -> None:
        """Handle main menu selections."""

        # Guard: Language must be set before proceeding
        if not patient.get("language"):
            await self._send_language_selection(phone)
            await self.update_state(phone, "selecting_language")
            return

        from app.database import get_conversation
        session = await get_conversation(phone) or {}
        context = session.get("context", {})
        if context.get("menu_shown"):
            pass
        else:
            await self._send_main_menu(phone, lang)
            context["menu_shown"] = True
            await self.update_state(phone, "main_menu", context)
            if intent in ["greeting", "unknown"]:
                # Menu shown via state entry, no need to process further
                return

        if intent == "book_appointment" or message.lower() in ["book", "appointment", "बुक", "బుక్"]:
            await self._start_booking(phone, patient, lang)
        elif intent == "view_services":
            await self._show_services(phone, lang)
        elif intent == "doctor_availability":
            await self._show_doctors(phone, lang)
        elif intent == "cancel_appointment":
            await self._handle_cancel_request(phone, patient, lang)
        elif intent == "reschedule_appointment":
            await self._handle_reschedule_request(phone, patient, lang)
        elif intent == "greeting":
            # Only show welcome_back for returning patients with language set
            if patient.get("visit_count", 0) > 0:
                patient_name = patient.get("name") or "there"
                first_name = patient_name.split()[0] if patient_name else "there"
                await self.whatsapp.send_text(phone, get_message("welcome_back", lang, name=first_name))
            if not session.get("context", {}).get("menu_shown"):
                await self._send_main_menu(phone, lang)
                context = session.get("context", {})
                context["menu_shown"] = True
                await self.update_state(phone, "main_menu", context)
        else:
            # Unknown intent, show menu again
            if not session.get("context", {}).get("menu_shown"):
                await self._send_main_menu(phone, lang)
                context = session.get("context", {})
                context["menu_shown"] = True
                await self.update_state(phone, "main_menu", context)

    async def _start_booking(self, phone: str, patient: dict, lang: str) -> None:
        """Start the booking flow."""

        # Guard: Language must be set before proceeding
        if not patient.get("language"):
            await self._send_language_selection(phone)
            await self.update_state(phone, "selecting_language")
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
                phone,
                body=msg_str,
                buttons=[
                    {"id": "for_self", "title": "For Me" if lang == "en" else ("मेरे लिए" if lang == "hi" else "నా కోసం")},
                    {"id": "for_family", "title": "For Family" if lang == "en" else ("परिवार के लिए" if lang == "hi" else "కుటుంబం కోసం")}
                ]
            )
            from app.database import update_conversation
            await update_conversation(phone, {
                "state": "collecting_name",
                "context": {"asked_for_whom": True}
            })
        else:
            # New patient, ask for name
            await self.whatsapp.send_text(phone, get_message("ask_name", lang))
            from app.database import update_conversation
            await update_conversation(phone, {
                "state": "collecting_name",
                "context": {"for_self": True}
            })

    async def _handle_collecting_name(
        self,
        phone: str,
        message: str,
        context: dict,
        patient: dict,
        lang: str
    ) -> None:
        """Handle name collection."""

        # Handle button responses
        if message.lower() in ["self", "for me", "मेरे लिए", "నా కోసం"]:
            context["for_self"] = True
            context["booking_name"] = patient.get("name")
            await self.whatsapp.send_text(phone, get_message("ask_symptoms", lang))
            await self.update_state(phone, "collecting_symptoms", context)
            return

        if message.lower() in ["family", "for family", "परिवार के लिए", "కుటుంబం కోసం"]:
            context["for_self"] = False
            await self.whatsapp.send_text(phone, get_message("ask_name", lang))
            await self.update_state(phone, "collecting_name", context)
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
                await self.whatsapp.send_text(phone, msg)
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
                await self.whatsapp.send_text(phone, error_msg)
            return

        name = result
        context["booking_name"] = name

        # Save to patient record if for self
        if context.get("for_self", True):
            await update_patient(phone, {"name": name})

        # Move to symptoms
        await self.whatsapp.send_text(phone, get_message("ask_symptoms", lang))
        await self.update_state(phone, "collecting_symptoms", context)

    async def _handle_collecting_symptoms(
        self,
        phone: str,
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
        await update_conversation(phone, {"context": context})

        # Allow skip
        if message.lower() in ["skip", "no symptoms", "don't know", "none", "नहीं", "తెలియదు"]:
            # Show department list directly
            await self._show_department_list(phone, context, lang)
            return

        # Check if emergency FIRST
        msg_lower = message.lower().strip()
        is_emergency = any(kw in msg_lower for kw in EMERGENCY_KEYWORDS)
        if is_emergency:
            await self._handle_emergency(phone, lang)
            return

        # Symptom follow-up questions
        if "chest pain" in msg_lower and context.get("symptom_followup") != "chest_pain":
            context["symptom_followup"] = "chest_pain"
            await self.whatsapp.send_interactive_buttons(
                phone,
                body="Is the chest pain sudden and severe, or mild and ongoing?",
                buttons=[
                    {"id": "chest_severe", "title": "Sudden & Severe"},
                    {"id": "chest_mild", "title": "Mild & Ongoing"}
                ]
            )
            await update_conversation(phone, {"context": context})
            return
            
        if "back pain" in msg_lower and context.get("symptom_followup") != "back_pain":
            context["symptom_followup"] = "back_pain"
            await self.whatsapp.send_interactive_buttons(
                phone,
                body="Is it lower back pain or upper back/neck pain?",
                buttons=[
                    {"id": "back_lower", "title": "Lower Back"},
                    {"id": "back_upper", "title": "Upper/Neck"}
                ]
            )
            await update_conversation(phone, {"context": context})
            return

        # Map symptoms to department
        symptom_result = await map_symptom_to_department(message)

        if symptom_result.get("suggested_department") is None:
            await self.whatsapp.send_text(
                phone, 
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
            phone,
            body=f"Based on your concern, our {symptom_result['suggested_department']} team may be able to help. Shall I book there?",
            buttons=[
                {"id": "suggest_yes", "title": "Yes" if lang == "en" else ("हां" if lang == "hi" else "అవును")},
                {"id": "suggest_no", "title": "No" if lang == "en" else ("नहीं" if lang == "hi" else "కాదు")}
            ]
        )

        await self.update_state(phone, "suggesting_department", context)

    async def _handle_suggesting_department(
        self,
        phone: str,
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
            response = supabase.table("doctors").select("*").eq("department", department).eq("is_active", True).order("rating", desc=True).execute()
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
                    phone=phone,
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
                await self.update_state(phone, "selecting_doctor", context_update)
            else:
                # Step 4: No doctors found
                await self.whatsapp.send_text(
                    phone,
                    f"No doctors available in {department} right now."
                )
                await self._show_department_list(phone, context, lang)
        else:
            # Show all departments
            await self._show_department_list(phone, context, lang)

    async def _show_department_list(self, phone: str, context: dict, lang: str) -> None:
        """Show list of departments."""
        sections = [{
            "title": "Departments",
            "rows": [
                {"id": "dept_general_medicine", "title": "General Medicine", "description": "Fever, cold, general health"},
                {"id": "dept_cardiology", "title": "Cardiology", "description": "Heart, chest pain, BP"},
                {"id": "dept_dental", "title": "Dental", "description": "Teeth, gums, oral health"},
                {"id": "dept_orthopedics", "title": "Orthopedics", "description": "Bones, joints, fractures"},
                {"id": "dept_gynecology", "title": "Gynecology", "description": "Women's health"},
                {"id": "dept_pediatrics", "title": "Pediatrics", "description": "Child healthcare"},
                {"id": "dept_ent", "title": "ENT", "description": "Ear, nose, throat"},
                {"id": "dept_dermatology", "title": "Dermatology", "description": "Skin, hair, nails"},
            ]
        }]

        msg = {
            "en": "No problem! Please choose a department:",
            "hi": "कोई बात नहीं! कृपया विभाग चुनें:",
            "te": "సరే! దయచేసి విభాగం ఎంచుకోండి:"
        }.get(lang, "Choose Department")

        await self.whatsapp.send_interactive_list(
            phone=phone,
            header="Choose Department",
            body=msg,
            button_text="Select",
            sections=sections
        )

        merged_context = {**context}
        await self.update_state(phone, "selecting_department", merged_context)

    async def _handle_selecting_department(
        self,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
        interactive_data: Optional[dict] = None
    ) -> None:
        """Handle manual department selection."""
        button_id = interactive_data.get("id", "") if interactive_data else ""
        
        # When patient selects from list
        if button_id.startswith("dept_"):
            DEPT_MAP = {
                "dept_general_medicine": "General Medicine",
                "dept_cardiology": "Cardiology",
                "dept_dental": "Dental",
                "dept_orthopedics": "Orthopedics",
                "dept_gynecology": "Gynecology",
                "dept_pediatrics": "Pediatrics",
                "dept_ent": "ENT",
                "dept_dermatology": "Dermatology",
            }
            department = DEPT_MAP.get(button_id, "General Medicine")
            
            # Fetch doctors for selected department
            from app.database import supabase
            response = supabase.table("doctors").select("*").eq("department", department).eq("is_active", True).order("rating", desc=True).execute()
            doctors = response.data
            
            if doctors:
                await self._show_doctor_list(phone, department, context, lang)
                # Note: _show_doctor_list automatically updates state to selecting_doctor
            else:
                await self.whatsapp.send_text(phone, f"No doctors available in {department} right now.")
                await self._show_department_list(phone, context, lang)
        else:
            # Re-show department list if they typed something invalid
            await self._show_department_list(phone, context, lang)

    async def _show_doctor_list(self, phone: str, department: str, context: dict, lang: str) -> None:
        """Show list of doctors in a department."""
        doctors = await get_doctors(department)

        if not doctors:
            await self.whatsapp.send_text(
                phone,
                f"Sorry, no doctors are currently available in {department}. Please try another department."
            )
            await self._show_department_list(phone, context, lang)
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
            phone=phone,
            header={"en": "Choose Your Doctor", "hi": "अपना डॉक्टर चुनें", "te": "మీ డాక్టర్‌ను ఎంచుకోండి"}.get(lang, "Choose Your Doctor"),
            body=get_message("available_doctors_in", lang, dept=department),
            button_text={"en": "Select Doctor", "hi": "डॉक्टर चुनें", "te": "డాక్టర్‌ ఎంచుకోండి"}.get(lang, "Select Doctor"),
            sections=sections
        )

        context["department"] = department
        merged_context = {**context}
        await self.update_state(phone, "selecting_doctor", merged_context)

    async def _handle_selecting_doctor(
        self,
        phone: str,
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
            res = supabase.table("doctors").select("*").eq("id", doctor_id).execute()
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
                response = supabase.table("doctors").select("*").eq("department", matched_dept).eq("is_active", True).order("rating", desc=True).execute()
                doctors = response.data
                if doctors:
                    await self._show_doctor_list(phone, matched_dept, context, lang)
                else:
                    await self.whatsapp.send_text(phone, f"No doctors available in {matched_dept} right now.")
                    await self._show_department_list(phone, context, lang)
                return
            
            # If no department match, try to match doctor name
            from app.database import supabase
            response = supabase.table("doctors").select("*").eq("is_active", True).execute()
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
            
            await self.whatsapp.send_text(phone, fallback_msg)
            if context.get("department"):
                await self._show_doctor_list(phone, context["department"], context, lang)
            else:
                await self._show_department_list(phone, context, lang)
            return

        context["doctor_name"] = doctor_name
        context["doctor"] = doctor

        # Ask for date — show interactive date picker
        context["doctor_name"] = doctor_name
        context["doctor"] = doctor
        merged_context = {**context}

        await self._show_date_picker(phone, merged_context, lang)
        await self.update_state(phone, "selecting_date", merged_context)

    async def _handle_selecting_date(
        self,
        phone: str,
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
            await self.whatsapp.send_text(phone, "Please provide a valid date (e.g., 'today', 'tomorrow', or '2026-03-20').")
            return

        # Validate date is not in past
        selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        if selected_date < datetime.now().date():
            await self.whatsapp.send_text(phone, "Please choose a future date.")
            return

        # Check if date is within 30 days
        if selected_date > datetime.now().date() + timedelta(days=30):
            await self.whatsapp.send_text(phone, "Please choose a date within the next 30 days.")
            return

        context["appointment_date"] = date_str

        # Get available slots
        slots, reason = await get_available_slots(context["doctor_name"], date_str)

        if not slots:
            date_display = selected_date.strftime('%d %b')
            
            # Inform the patient why the doctor is unavailable
            if reason == "doctor_on_leave":
                msg = {
                    "en": f"Dr. {context['doctor_name']} is on leave on {date_display}.",
                    "hi": f"डॉ. {context['doctor_name']} {date_display} को छुट्टी पर हैं।",
                    "te": f"డాక్టర్ {context['doctor_name']} {date_display} న సెలవులో ఉన్నారు."
                }.get(lang, f"Dr. {context['doctor_name']} is on leave on {date_display}.")
                await self.whatsapp.send_text(phone, msg)
            elif reason == "hospital_closed":
                msg = {
                    "en": f"The hospital is closed on {date_display} for a holiday.",
                    "hi": f"अस्पताल {date_display} को छुट्टी के कारण बंद है।",
                    "te": f"ఆసుపత్రి {date_display} న సెలవు కారణంగా మూసివేయబడింది."
                }.get(lang, f"The hospital is closed on {date_display} for a holiday.")
                await self.whatsapp.send_text(phone, msg)
            elif reason == "doctor_off_day":
                msg = {
                    "en": f"Dr. {context['doctor_name']} does not consult on this day of the week.",
                    "hi": f"डॉ. {context['doctor_name']} सप्ताह के इस दिन परामर्श नहीं देते हैं।",
                    "te": f"డా. {context['doctor_name']} వారంలో ఈ రోజున సంప్రదింపులు చేయరు."
                }.get(lang, f"Dr. {context['doctor_name']} does not work on this day of the week.")
                await self.whatsapp.send_text(phone, msg)

            # Find next available date
            next_date, next_slots, next_reason = await find_next_available_date(
                context["doctor_name"],
                (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            )

            if next_reason == "no_availability_14_days" or not next_date:
                # Doctor fully booked or unavailable for long time, suggest others
                await self._suggest_other_doctors(phone, context, lang)
                return

            next_date_display = datetime.strptime(next_date, "%Y-%m-%d").strftime("%d %b")
            msg = {
                "en": f"Next available date for {context['doctor_name']} is {next_date_display}.",
                "hi": f"{context['doctor_name']} के लिए अगली उपलब्ध तारीख {next_date_display} है।",
                "te": f"{context['doctor_name']} కోసం తదుపరి అందుబాటులో ఉన్న తేదీ {next_date_display}."
            }.get(lang, f"Next available date is {next_date_display}.")
            await self.whatsapp.send_text(phone, msg)
            
            context["appointment_date"] = next_date
            slots = next_slots

        # Show slots
        await self._show_slot_list(phone, slots, context, lang)

    async def _show_date_picker(self, phone: str, context: dict, lang: str) -> None:
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
            phone,
            body=get_message("select_date", lang),
            button_text="Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి"),
            sections=sections
        )

    async def _show_slot_list(
        self,
        phone: str,
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
            phone,
            body=get_message("select_slot", lang),
            button_text="Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి"),
            sections=sections
        )

        await self.update_state(phone, "selecting_slot", context)

    async def _handle_selecting_slot(
        self,
        phone: str,
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
        await self._show_booking_confirmation(phone, context, lang)

    async def _show_booking_confirmation(self, phone: str, context: dict, lang: str) -> None:
        """Show booking confirmation summary."""
        from datetime import datetime

        date_display = datetime.strptime(context["appointment_date"], "%Y-%m-%d").strftime("%d %b %Y")

        await self.whatsapp.send_interactive_buttons(
            phone,
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

        await self.update_state(phone, "confirming_booking", context)

    async def _handle_confirming_booking(
        self,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        patient: dict,
        lang: str
    ) -> None:
        """Handle booking confirmation."""

        if intent in ["confirm_booking", "yes"]:
            # Attempt to book
            from datetime import datetime

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

            result = await book_appointment(appointment_data)

            if result["success"]:
                appointment = result["appointment"]
                date_display = datetime.strptime(context["appointment_date"], "%Y-%m-%d").strftime("%d %b %Y")

                await self.whatsapp.send_text(
                    phone,
                    get_message(
                        "booking_confirmed",
                        lang,
                        ref=appointment["booking_ref"],
                        doctor=context["doctor_name"],
                        date=date_display,
                        time=context["appointment_time"]
                    )
                )

                await log_analytics_event(phone, "appointment_booked", department=context.get("department"))

                import asyncio
                await asyncio.sleep(2)
                
                # Pre-appointment instructions for dept
                dept_instruction = {
                    "en": f"Instructions for {context.get('department')}: Please arrive 15 minutes early and bring relevant medical records.",
                    "hi": f"{context.get('department')} के लिए निर्देश: कृपया 15 मिनट पहले पहुंचें और प्रासंगिक चिकित्सा रिकॉर्ड लाएं।",
                    "te": f"{context.get('department')} కోసం సూచనలు: దయచేసి సంబంధిత మెడికల్ రికార్డులను తీసుకుని 15 నిమిషాల ముందుగా రండి."
                }.get(lang, "Please arrive 15 mins early.")
                await self.whatsapp.send_text(phone, dept_instruction)

                # Follow-up
                follow_up_msg = {
                    "en": "What would you like to do?",
                    "hi": "आप आगे क्या करना चाहेंगे?",
                    "te": "మీరు ఇంకా ఏమి చేయాలనుకుంటున్నారు?"
                }.get(lang, "What would you like to do?")
                await self.whatsapp.send_interactive_buttons(
                    phone,
                    body=follow_up_msg,
                    buttons=[
                        {"id": "book_another", "title": "Book Appointment"},
                        {"id": "main_menu", "title": "Main Menu"}
                    ]
                )

                # Reset to main menu
                await self.update_state(phone, "main_menu")
            else:
                if result.get("reason") == "slot_taken":
                    # Slot was taken, show alternatives
                    await self.whatsapp.send_text(
                        phone,
                        get_message("slot_taken", lang, doctor=context["doctor_name"])
                    )
                    # Get next available slots
                    slots, _ = await get_available_slots(context["doctor_name"], context["appointment_date"])
                    if slots:
                        await self._show_slot_list(phone, slots[:3], context, lang)
                    else:
                        await self._suggest_other_doctors(phone, context, lang)
                else:
                    await self.whatsapp.send_text(
                        phone,
                        get_message("booking_failed", lang, phone=settings.hospital_phone)
                    )
                    await self.update_state(phone, "main_menu")
                    await self._send_main_menu(phone, lang)
        else:
            # Edit booking - go back to doctor selection
            await self._show_doctor_list(phone, context.get("department", "General Medicine"), context, lang)

    async def _suggest_other_doctors(self, phone: str, context: dict, lang: str) -> None:
        """Suggest other doctors when selected doctor is fully booked."""
        department = context.get("department", "General Medicine")
        exclude_doctor = context["doctor_name"]

        doctors = await get_doctors(department)
        available = []

        from datetime import datetime, timedelta

        for doc in doctors:
            if doc["name"] == exclude_doctor:
                continue
            for i in range(7):
                check_date = (datetime.now() + timedelta(days=i+1)).strftime("%Y-%m-%d")
                slots, _ = await get_available_slots(doc["name"], check_date)
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
                phone,
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
                phone,
                body="Select another doctor:",
                button_text="Select",
                sections=sections
            )
        else:
            await self.whatsapp.send_text(
                phone,
                get_message("no_doctors_available", lang, department=department, phone=settings.hospital_phone)
            )
            await self._send_main_menu(phone, lang)

    async def _handle_emergency(self, phone: str, lang: str) -> None:
        """Handle emergency situation."""
        await self.whatsapp.send_text(phone, get_message("emergency", lang))

        # Send location if available
        if settings.hospital_maps_link:
            await self.whatsapp.send_text(
                phone,
                f"Emergency location: {settings.hospital_maps_link}\nAddress: {settings.hospital_address}"
            )

        await self.update_state(phone, "main_menu")
        await log_analytics_event(phone, "emergency_detected")

    async def _handle_opt_out(self, phone: str, patient: dict, lang: str) -> None:
        """Handle opt-out request."""
        await update_patient(phone, {
            "opted_in": False,
            "opted_out_at": "now()"
        })

        await self.whatsapp.send_text(phone, get_message("opt_out_confirm", lang))
        await log_analytics_event(phone, "opt_out")

    async def _handle_data_deletion(self, phone: str, patient: dict, lang: str) -> None:
        """Handle data deletion request."""
        from app.database import delete_patient_data

        await delete_patient_data(phone)
        await self.whatsapp.send_text(phone, get_message("data_deleted", lang))
        await log_analytics_event(phone, "data_deleted")

    async def _handle_human_escalation(self, phone: str, lang: str) -> None:
        """Handle human escalation request."""
        await self.whatsapp.send_text(
            phone,
            get_message("human_escalation", lang, phone=settings.hospital_phone)
        )
        await self.update_state(phone, "escalated_to_human")
        await log_analytics_event(phone, "human_escalation")

    async def _show_services(self, phone: str, lang: str) -> None:
        """Show hospital services."""
        await self.whatsapp.send_interactive_list(
            phone=phone,
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

    async def _show_doctors(self, phone: str, lang: str) -> None:
        """Show available doctors."""
        from app.database import supabase
        response = supabase.table("doctors").select("*").eq("is_active", True).order("department").execute()
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
            phone=phone,
            header={"en": "Our Doctors", "hi": "हमारे डॉक्टर", "te": "మా డాక్టర్లు"}.get(lang, "Our Doctors"),
            body=get_message("our_doctors_body", lang),
            button_text={"en": "Select", "hi": "चुनें", "te": "ఎంచుకోండి"}.get(lang, "Select"),
            sections=sections[:10]
        )

    async def _handle_cancel_request(self, phone: str, patient: dict, lang: str) -> None:
        """Handle appointment cancellation request."""
        from app.database import get_patient_appointments, cancel_appointment

        appointments = await get_patient_appointments(phone, status="confirmed")

        if not appointments:
            await self.whatsapp.send_text(phone, "You don't have any confirmed appointments to cancel.")
            await self._send_main_menu(phone, lang)
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
            phone,
            body="Which appointment would you like to cancel?",
            button_text="Select",
            sections=sections
        )

    async def _handle_reschedule_request(self, phone: str, patient: dict, lang: str) -> None:
        """Handle reschedule request."""
        await self.whatsapp.send_text(
            phone,
            "To reschedule, please call us directly: " + settings.hospital_phone
        )
        await self._send_main_menu(phone, lang)


# Global instance
conversation_manager = ConversationManager()
