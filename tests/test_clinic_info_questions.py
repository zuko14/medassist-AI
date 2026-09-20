"""Questions a patient asks ABOUT the clinic, in their own words.

Production screenshots, 2026-09-20: a patient typed "Where are you located"
and got the "What would you like to book?" service picker; "What are yours
timings" got the same picker; asking the location again while the test
catalogue was open came back as 'No test matched "Where are you located"'.

Cause: every intent in the classifier was an ACTION, so an information
question was force-fit into the nearest one ("timing" sits inside
doctor_availability's keyword list) and at a diagnostics-only clinic that
lands on _start_lab_booking. These cover the `clinic_info` intent that now
catches them, and -- just as important -- that a real test name is still
searched and a doctor question still reaches the doctor list.
"""

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("HOSPITAL_NAME", "Accumax Diagnostics")
os.environ.setdefault("HOSPITAL_EMERGENCY_NUMBER", "108")
os.environ.setdefault("HOSPITAL_PHONE", "+919876543210")
os.environ.setdefault("BOOKING_REF_PREFIX", "AD")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

from app.services import faq_engine as fe  # noqa: E402
from app.services.ai_engine import keyword_intent_fallback  # noqa: E402
from app.services.conversation import ConversationManager  # noqa: E402


PHONE = "+919999999999"

#: A single-location diagnostic centre, configured the way provision_clinic
#: writes it -- contact and location under `config`, not at the top level.
CLINIC = {
    "id": "clinic-1",
    "name": "Accumax Diagnostics",
    "whatsapp_number": "+911111111111",
    # A real plan name: "diagstream" is what grants lab_test_booking, and the
    # hours answer only quotes a collection window for a clinic that has it.
    "plan": "diagstream",
    "config": {
        "address": "12 MG Road, Kukatpally, Hyderabad 500072",
        "landmark": "Opposite Metro Pillar 1234",
        "maps_link": "https://maps.example/accumax",
        "phone": "+919281235959",
        "emergency_number": "108",
    },
}

DOCTOR = {
    "id": "doc-1",
    "name": "Dr. Test",
    "morning_slots": ["09:00", "09:30", "10:00"],
    "evening_slots": ["17:00", "18:30"],
    "available_days": "Mon,Tue,Wed,Thu,Fri,Sat",
}


def _manager():
    m = ConversationManager()
    m.whatsapp = MagicMock()
    m.whatsapp.send_text = AsyncMock(return_value=True)
    m.whatsapp.send_interactive_list = AsyncMock(return_value=True)
    m.whatsapp.send_interactive_buttons = AsyncMock(return_value=True)
    m.update_state = AsyncMock()
    return m


# ─── The classifier ──────────────────────────────────────────────────────────


class TestIntentClassification:
    """Deterministic path only -- these must hold with OpenRouter unreachable."""

    @pytest.mark.parametrize(
        "message",
        [
            # The two from the production screenshots, verbatim.
            "Where are you located",
            "What are yours timings",
            # How patients actually type.
            "where r u located",
            "whats ur address",
            "send me location",
            "how to reach your hospital",
            "what time do you open",
            "are you open today",
            "do you work on sunday",
            "ur timings kya hai",
            "give me your contact number",
            "your phone number please",
            # Hindi / Telugu.
            "कहाँ है आपका अस्पताल",
            "आपका फोन नंबर",
            "మీరు ఎక్కడ ఉన్నారు",
            "చిరునామా చెప్పండి",
            "టైమింగ్స్ ఏమిటి",
        ],
    )
    def test_information_questions_classify_as_clinic_info(self, message):
        assert keyword_intent_fallback(message) == "clinic_info"

    @pytest.mark.parametrize(
        "message,expected",
        [
            # The action intents this must not have stolen.
            ("Our Doctors", "doctor_availability"),
            ("doctor list", "doctor_availability"),
            ("हमारे डॉक्टर", "doctor_availability"),
            ("మా డాక్టర్లు", "doctor_availability"),
            ("Our Services", "view_services"),
            ("departments", "view_services"),
            ("Book Appointment", "book_appointment"),
            ("book a slot", "book_appointment"),
            ("My Reports", "view_reports"),
            ("lab reports", "view_reports"),
            ("blood report", "view_reports"),
            ("delete my data", "data_deletion_request"),
            ("stop messaging me", "opt_out"),
            ("talk to human", "human_escalation"),
            ("heart attack help", "emergency"),
            ("severe bleeding", "emergency"),
            ("token status", "queue_status"),
        ],
    )
    def test_existing_intents_are_unchanged(self, message, expected):
        assert keyword_intent_fallback(message) == expected

    @pytest.mark.parametrize(
        "message", ["doctor timings", "Dr Sharma timings", "डॉक्टर का समय क्या है"]
    )
    def test_a_question_naming_a_doctor_is_availability_not_clinic_hours(self, message):
        assert keyword_intent_fallback(message) != "clinic_info"

    @pytest.mark.parametrize(
        "name",
        [
            "thyroid", "Blood glucose", "MRI brain", "HBA1C", "lipid profile",
            "urine sodium", "vitamin d", "COMPLETE BLOOD COUNT (CBC)",
            "24 Hrs URINE MICROALBUMIN", "hair fall",
        ],
    )
    def test_a_catalogue_item_is_never_an_info_question(self, name):
        """The search box is free text. A phrase list that matched any part of
        a test, scan or treatment name would take the patient's search away."""
        assert fe.detect_topic(name) is None


# ─── Answers come from the clinic's own data ─────────────────────────────────


class TestAnswers:
    @pytest.mark.asyncio
    async def test_location_quotes_the_configured_address(self):
        with patch("app.services.tenant.get_clinic_branches", new=AsyncMock(return_value=[])):
            out = await fe.answer(CLINIC, "location", "en")
        assert "12 MG Road, Kukatpally, Hyderabad 500072" in out
        assert "Opposite Metro Pillar 1234" in out
        assert "https://maps.example/accumax" in out

    @pytest.mark.asyncio
    async def test_location_lists_every_branch_when_there_are_several(self):
        branches = [
            {"name": "Kukatpally", "address": "12 MG Road", "phone": "+910000000001"},
            {"name": "Madhapur", "address": "9 Hitech City Rd", "landmark": "Near IKEA"},
        ]
        with patch("app.services.tenant.get_clinic_branches", new=AsyncMock(return_value=branches)):
            out = await fe.answer(CLINIC, "location", "en")
        assert "Kukatpally" in out and "12 MG Road" in out
        assert "Madhapur" in out and "9 Hitech City Rd" in out and "Near IKEA" in out

    @pytest.mark.asyncio
    async def test_location_returns_none_rather_than_inventing_an_address(self):
        bare = {"id": "c9", "config": {}}
        with patch("app.services.tenant.get_clinic_branches", new=AsyncMock(return_value=[])), \
             patch("app.services.faq_engine.settings.hospital_address", ""):
            assert await fe.answer(bare, "location", "en") is None

    @pytest.mark.asyncio
    async def test_hours_are_derived_from_the_doctors_real_slots(self):
        with patch("app.database.get_doctors", new=AsyncMock(return_value=[DOCTOR])), \
             patch("app.services.tenant.has_feature", return_value=False):
            out = await fe.answer(CLINIC, "hours", "en")
        assert "9:00 AM" in out and "6:30 PM" in out
        assert "Mon-Sat" in out

    @pytest.mark.asyncio
    async def test_hours_quote_the_configured_collection_window_for_a_lab(self):
        window = {"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"}
        with patch("app.database.get_doctors", new=AsyncMock(return_value=[])), \
             patch("app.services.tenant.has_feature", return_value=True), \
             patch("app.database.get_lab_collection_window", new=AsyncMock(return_value=window)):
            out = await fe.answer(CLINIC, "hours", "en")
        assert "07:00 - 11:00" in out and "Mon-Sat" in out

    @pytest.mark.asyncio
    async def test_hours_returns_none_when_the_clinic_has_neither(self):
        with patch("app.database.get_doctors", new=AsyncMock(return_value=[])), \
             patch("app.services.tenant.has_feature", return_value=False):
            assert await fe.answer(CLINIC, "hours", "en") is None

    @pytest.mark.asyncio
    async def test_contact_quotes_the_configured_numbers(self):
        out = await fe.answer(CLINIC, "contact", "en")
        assert "+919281235959" in out and "108" in out

    @pytest.mark.asyncio
    async def test_an_uncategorised_question_gets_every_topic_with_data(self):
        with patch("app.services.tenant.get_clinic_branches", new=AsyncMock(return_value=[])), \
             patch("app.database.get_doctors", new=AsyncMock(return_value=[])), \
             patch("app.services.tenant.has_feature", return_value=False):
            out = await fe.answer(CLINIC, None, "en")
        assert "12 MG Road" in out and "+919281235959" in out

    @pytest.mark.asyncio
    async def test_a_clinic_can_add_its_own_topic(self):
        clinic = dict(CLINIC)
        clinic["config"] = dict(CLINIC["config"], **{
            "custom_faqs": {"en": {"parking": "Free parking in the basement."}},
            "custom_faq_keywords": {"en": {"parking": ["parking", "where to park"]}},
        })
        assert fe.detect_topic("is there parking", clinic) == "parking"
        assert await fe.answer(clinic, "parking", "en") == "Free parking in the basement."

    def test_day_formatting(self):
        assert fe.format_days("Mon,Tue,Wed,Thu,Fri,Sat") == "Mon-Sat"
        assert fe.format_days("Mon,Tue,Wed,Thu,Fri,Sat,Sun") == "All days"
        assert fe.format_days("Mon,Wed,Fri") == "Mon, Wed, Fri"
        assert fe.format_days("Sun") == "Sun"
        assert fe.format_days("") == ""


# ─── Routing through the state machine ───────────────────────────────────────


class TestRouting:
    async def _route(self, message, intent, state, lab_step=None, consent=True):
        m = _manager()
        m._start_lab_booking = AsyncMock()
        m._show_doctors = AsyncMock()
        m._show_services = AsyncMock()
        m._send_main_menu = AsyncMock()
        m._handle_browsing_lab_tests = AsyncMock()
        m._handle_confirming_collection_date = AsyncMock()
        m._is_diagnostics_only = AsyncMock(return_value=True)
        context = {"lab_step": lab_step} if lab_step else {}
        session = {"state": state, "context": context}
        with patch("app.services.conversation.get_lang", new_callable=AsyncMock, return_value="en"), \
             patch("app.services.conversation.log_analytics_event", new=AsyncMock()), \
             patch("app.services.tenant.get_clinic_branches", new=AsyncMock(return_value=[])), \
             patch("app.database.get_doctors", new=AsyncMock(return_value=[])), \
             patch("app.database.get_lab_collection_window",
                   new=AsyncMock(return_value={"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"})):
            await m._process_state(
                CLINIC, PHONE, message, intent, session,
                {"id": "p1", "language": "en", "data_consent": consent}, "en", None,
            )
        return m

    def _reply(self, m):
        return m.whatsapp.send_text.await_args.args[2]

    @pytest.mark.asyncio
    async def test_location_question_answers_the_address_not_the_booking_picker(self):
        """The exact production failure from the screenshot."""
        m = await self._route("Where are you located", "clinic_info", "main_menu")
        m._start_lab_booking.assert_not_awaited()
        assert "12 MG Road, Kukatpally, Hyderabad 500072" in self._reply(m)

    @pytest.mark.asyncio
    async def test_hours_question_answers_hours_not_the_booking_picker(self):
        m = await self._route("What are yours timings", "clinic_info", "main_menu")
        m._start_lab_booking.assert_not_awaited()
        assert "07:00 - 11:00" in self._reply(m)

    @pytest.mark.asyncio
    async def test_the_question_is_answered_mid_search_without_losing_the_search(self):
        """Screenshot 2: asked while the catalogue was open, this came back as
        'No test matched "Where are you located"'."""
        m = await self._route("Where are you located", "clinic_info", "browsing_lab_tests")
        m._handle_browsing_lab_tests.assert_not_awaited()
        assert "12 MG Road" in self._reply(m)
        # The state is untouched: the catalogue is still open behind the answer.
        m.update_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_answer_tells_a_mid_flow_patient_how_to_carry_on(self):
        m = await self._route("Where are you located", "clinic_info", "selecting_slot")
        assert "carry on" in self._reply(m)
        m.update_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_llm_only_match_never_interrupts_an_open_search(self):
        """No phrase matches "Blood glucose", so if the model called it
        clinic_info anyway the search must still win."""
        m = await self._route("Blood glucose", "clinic_info", "browsing_lab_tests")
        m._handle_browsing_lab_tests.assert_awaited_once()
        m.whatsapp.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_llm_only_match_never_interrupts_the_typed_patient_name(self):
        m = await self._route(
            "Ravi Kumar", "clinic_info", "confirming_collection_date", lab_step="name"
        )
        m._handle_confirming_collection_date.assert_awaited_once()
        m.whatsapp.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["selecting_language", "awaiting_consent"])
    async def test_consent_still_comes_first(self, state):
        """DPDP: nothing is answered before the patient has consented."""
        m = await self._route("Where are you located", "clinic_info", state)
        m.whatsapp.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_consent_is_asked_before_anything_is_answered(self):
        """A returning patient who has not consented sits in `idle` waiting for
        the consent prompt. Answering here would skip it."""
        m = await self._route(
            "Where are you located", "clinic_info", "idle", consent=False
        )
        m.whatsapp.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_emergency_still_wins(self):
        m = _manager()
        m._handle_emergency = AsyncMock()
        m._answer_clinic_info = AsyncMock()
        session = {"state": "main_menu", "context": {}}
        with patch("app.services.conversation.get_lang", new_callable=AsyncMock, return_value="en"):
            await m._process_state(
                CLINIC, PHONE, "chest pain", "emergency", session,
                {"id": "p1", "language": "en"}, "en", None,
            )
        m._handle_emergency.assert_awaited_once()
        m._answer_clinic_info.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_clinic_with_no_data_offers_a_human_not_an_invented_answer(self):
        bare = {"id": "c9", "name": "Bare Clinic", "config": {"phone": "+910000000000"}}
        m = _manager()
        with patch("app.services.conversation.log_analytics_event", new=AsyncMock()), \
             patch("app.services.tenant.get_clinic_branches", new=AsyncMock(return_value=[])), \
             patch("app.services.faq_engine.settings.hospital_address", ""):
            sent = await m._answer_clinic_info(
                bare, PHONE, "where are you located", "main_menu", "en"
            )
        assert sent is True
        reply = m.whatsapp.send_text.await_args.args[2]
        assert "+910000000000" in reply
        assert "don't have that detail" in reply
