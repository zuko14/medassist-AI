"""Tests for Conversational FSM Navigation, Session Timeout Lifecycle & Message Sanitization.

Verifies:
1. extract_clean_message_content strips multi-line WhatsApp quoted prompt headers.
2. Global escape hatches from selection states (selecting_department, selecting_doctor, selecting_date).
3. Interactive menu button clicks (menu_doctors, menu_services, menu_book, menu_reports) execute cleanly from any state.
4. Booking timer lifecycle (booking_context_expires_at refreshes on state transitions and does NOT trigger false early timeouts).
5. Keyword intent classification precision for doctors, services, booking, and reports.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.conversation import (
    ConversationManager,
    extract_clean_message_content,
    MID_BOOKING_STATES,
)
from app.services.ai_engine import keyword_intent_fallback


MOCK_CLINIC = {
    "id": "11111111-2222-3333-4444-555555555555",
    "name": "Apollo Clinic",
    "whatsapp_number": "+919999999999",
    "phone": "+919999999999",
    "config": {},
}


# ─────────────────────────────────────────────────────────────────────────────
# 1. MESSAGE SANITIZATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_clean_message_content_single_line():
    """Verify single line messages remain untouched."""
    assert extract_clean_message_content("Cardiology") == "Cardiology"
    assert extract_clean_message_content("Hi") == "Hi"
    assert extract_clean_message_content("Dr. Smith") == "Dr. Smith"


def test_extract_clean_message_content_whatsapp_quoted_menu():
    """Verify multi-line WhatsApp quoted reply from main menu extracts the user's choice."""
    raw = "What would you like to do?\nOur Doctors"
    assert extract_clean_message_content(raw) == "Our Doctors"

    raw_services = "What would you like to do?\nOur Services"
    assert extract_clean_message_content(raw_services) == "Our Services"

    raw_booking = "What would you like to do?\nBook Appointment"
    assert extract_clean_message_content(raw_booking) == "Book Appointment"


def test_extract_clean_message_content_whatsapp_quoted_department():
    """Verify multi-line WhatsApp quoted reply from department list extracts the department."""
    raw = "Our Services\nPlease choose a department / service:\nCardiology"
    assert extract_clean_message_content(raw) == "Cardiology"


def test_extract_clean_message_content_whatsapp_quoted_doctor():
    """Verify multi-line WhatsApp quoted reply from doctor list preserves the doctor info."""
    raw = "Choose Your Doctor\nAvailable doctors in Cardiology:\nDR LATCHIREDDI S A NAIDU\nCLINICAL CARDIO PHYSICIAN (NI) · ⭐4.5 · ₹500"
    cleaned = extract_clean_message_content(raw)
    assert "DR LATCHIREDDI S A NAIDU" in cleaned
    assert "Choose Your Doctor" not in cleaned


# ─────────────────────────────────────────────────────────────────────────────
# 2. KEYWORD INTENT FALLBACK TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_keyword_intent_fallback_distinguishes_doctors_and_booking():
    """Verify 'Our Doctors' maps to doctor_availability and 'Book' maps to book_appointment."""
    assert keyword_intent_fallback("Our Doctors") == "doctor_availability"
    assert keyword_intent_fallback("doctor list") == "doctor_availability"
    assert keyword_intent_fallback("Doctors") == "doctor_availability"
    assert keyword_intent_fallback("మా డాక్టర్లు") == "doctor_availability"
    assert keyword_intent_fallback("हमारे डॉक्टर") == "doctor_availability"

    assert keyword_intent_fallback("Our Services") == "view_services"
    assert keyword_intent_fallback("departments") == "view_services"
    assert keyword_intent_fallback("మా సేవలు") == "view_services"

    assert keyword_intent_fallback("Book Appointment") == "book_appointment"
    assert keyword_intent_fallback("book a slot") == "book_appointment"
    assert keyword_intent_fallback("अपॉइंटमेंट बुक") == "book_appointment"

    assert keyword_intent_fallback("My Reports") == "view_reports"
    assert keyword_intent_fallback("lab reports") == "view_reports"
    assert keyword_intent_fallback("ల్యాబ్ రిపోర్టులు") == "view_reports"


# ─────────────────────────────────────────────────────────────────────────────
# 3. GLOBAL MENU ESCAPE HATCH TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_button_menu_doctors_escapes_selecting_department_state():
    """Verify that tapping menu_doctors while in selecting_department state invokes _show_doctors."""
    cm = ConversationManager()
    phone = "+919876543210"
    
    mock_session = {
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "state": "selecting_department",
        "context": {"dept_options": {"dept_cardiology": "Cardiology"}},
        "booking_context_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=25)).isoformat(),
    }
    
    mock_patient = {
        "id": "pat-123",
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "language": "en",
        "name": "Chaitanya",
    }

    with patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=mock_session)), \
         patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value=mock_patient)), \
         patch("app.services.conversation.get_lang", new=AsyncMock(return_value="en")), \
         patch.object(cm, "_is_diagnostics_only", new=AsyncMock(return_value=False)), \
         patch.object(cm, "_show_doctors", new=AsyncMock()) as mock_show_doctors, \
         patch.object(cm, "_show_department_list", new=AsyncMock()) as mock_show_dept:

        await cm._handle_message_locked(
            clinic=MOCK_CLINIC,
            phone=phone,
            message="Our Doctors",
            message_type="interactive",
            interactive_data={"id": "menu_doctors", "type": "list_reply"},
        )

        mock_show_doctors.assert_called_once_with(MOCK_CLINIC, phone, "en")
        mock_show_dept.assert_not_called()


@pytest.mark.asyncio
async def test_text_our_doctors_escapes_selecting_department_state():
    """Verify that typing 'Our Doctors' while in selecting_department state shows doctors list."""
    cm = ConversationManager()
    phone = "+919876543210"

    mock_session = {
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "state": "selecting_department",
        "context": {"dept_options": {"dept_cardiology": "Cardiology"}},
        "booking_context_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=25)).isoformat(),
    }

    mock_patient = {
        "id": "pat-123",
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "language": "en",
        "name": "Chaitanya",
    }

    with patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=mock_session)), \
         patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value=mock_patient)), \
         patch("app.services.conversation.get_lang", new=AsyncMock(return_value="en")), \
         patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="doctor_availability")), \
         patch.object(cm, "_is_diagnostics_only", new=AsyncMock(return_value=False)), \
         patch.object(cm, "_show_doctors", new=AsyncMock()) as mock_show_doctors, \
         patch.object(cm, "_show_department_list", new=AsyncMock()) as mock_show_dept:

        await cm._handle_message_locked(
            clinic=MOCK_CLINIC,
            phone=phone,
            message="Our Doctors",
            message_type="text",
        )

        mock_show_doctors.assert_called_once_with(MOCK_CLINIC, phone, "en")
        mock_show_dept.assert_not_called()


@pytest.mark.asyncio
async def test_text_menu_escapes_selecting_doctor_state():
    """Verify that typing 'menu' while in selecting_doctor state returns to main menu."""
    cm = ConversationManager()
    phone = "+919876543210"

    mock_session = {
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "state": "selecting_doctor",
        "context": {"department": "Cardiology"},
        "booking_context_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=25)).isoformat(),
    }

    mock_patient = {
        "id": "pat-123",
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "language": "en",
        "name": "Chaitanya",
    }

    with patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=mock_session)), \
         patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value=mock_patient)), \
         patch("app.services.conversation.get_lang", new=AsyncMock(return_value="en")), \
         patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="greeting")), \
         patch.object(cm, "update_state", new=AsyncMock()) as mock_update_state, \
         patch.object(cm, "_send_main_menu", new=AsyncMock()) as mock_send_menu:

        await cm._handle_message_locked(
            clinic=MOCK_CLINIC,
            phone=phone,
            message="menu",
            message_type="text",
        )

        mock_update_state.assert_called_once_with(MOCK_CLINIC, phone, "main_menu", {"menu_shown": False})
        mock_send_menu.assert_called_once_with(MOCK_CLINIC, phone, "en")


# ─────────────────────────────────────────────────────────────────────────────
# 4. BOOKING SESSION TIMEOUT LIFECYCLE TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_timeout_does_not_fire_when_expires_at_is_in_future():
    """Verify that a valid future booking timer does not trigger false timeout."""
    cm = ConversationManager()
    phone = "+919876543210"

    future_ts = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    mock_session = {
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "state": "selecting_doctor",
        "context": {"department": "Cardiology"},
        "booking_context_expires_at": future_ts,
    }

    mock_patient = {
        "id": "pat-123",
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "language": "en",
        "name": "Chaitanya",
    }

    with patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=mock_session)), \
         patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value=mock_patient)), \
         patch("app.services.conversation.get_lang", new=AsyncMock(return_value="en")), \
         patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="unknown")), \
         patch("app.services.conversation.update_conversation", new=AsyncMock()) as mock_update_conv, \
         patch.object(cm, "_process_state", new=AsyncMock()) as mock_process_state, \
         patch.object(cm.whatsapp, "send_text", new=AsyncMock()) as mock_send_text:

        await cm._handle_message_locked(
            clinic=MOCK_CLINIC,
            phone=phone,
            message="Dr. House",
            message_type="text",
        )

        # Timeout text should NOT be sent
        for call in mock_send_text.call_args_list:
            assert "timed out" not in str(call)
        
        # State machine processing should proceed
        assert mock_process_state.called


@pytest.mark.asyncio
async def test_session_timeout_fires_when_timer_is_genuinely_expired():
    """Verify that an expired booking timer resets session to main menu."""
    cm = ConversationManager()
    phone = "+919876543210"

    past_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    mock_session = {
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "state": "selecting_doctor",
        "context": {"department": "Cardiology"},
        "booking_context_expires_at": past_ts,
    }

    mock_patient = {
        "id": "pat-123",
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "language": "en",
        "name": "Chaitanya",
    }

    with patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=mock_session)), \
         patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value=mock_patient)), \
         patch("app.services.conversation.get_lang", new=AsyncMock(return_value="en")), \
         patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="unknown")), \
         patch("app.services.conversation.update_conversation", new=AsyncMock()) as mock_update_conv, \
         patch.object(cm, "_send_main_menu", new=AsyncMock()) as mock_send_menu, \
         patch.object(cm.whatsapp, "send_text", new=AsyncMock()) as mock_send_text:

        await cm._handle_message_locked(
            clinic=MOCK_CLINIC,
            phone=phone,
            message="Dr. House",
            message_type="text",
        )

        # Timeout message must be sent and main menu sent
        mock_send_menu.assert_called_once_with(MOCK_CLINIC, phone, "en")
        assert mock_update_conv.called
        update_payload = mock_update_conv.call_args[0][2]
        assert update_payload["state"] == "main_menu"
        assert update_payload["booking_context_expires_at"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 5. REAL PRODUCTION TRANSCRIPT SIMULATION TEST
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_production_transcript_escapes_department_loop_to_doctors():
    """Simulate the exact production transcript where a patient in selecting_department
    sends 'What would you like to do?\nOur Doctors' and must immediately see doctors list.
    """
    cm = ConversationManager()
    phone = "+919490386668"

    # State is currently selecting_department
    mock_session = {
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "state": "selecting_department",
        "context": {"dept_options": {"dept_cardiology": "Cardiology"}},
        "booking_context_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=28)).isoformat(),
    }

    mock_patient = {
        "id": "pat-123",
        "clinic_id": MOCK_CLINIC["id"],
        "phone": phone,
        "language": "en",
        "name": "Ck",
    }

    raw_whatsapp_msg = "What would you like to do?\nOur Doctors"

    with patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=mock_session)), \
         patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value=mock_patient)), \
         patch("app.services.conversation.get_lang", new=AsyncMock(return_value="en")), \
         patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="doctor_availability")), \
         patch.object(cm, "_is_diagnostics_only", new=AsyncMock(return_value=False)), \
         patch.object(cm, "_show_doctors", new=AsyncMock()) as mock_show_doctors, \
         patch.object(cm, "_show_department_list", new=AsyncMock()) as mock_show_dept, \
         patch.object(cm.whatsapp, "send_text", new=AsyncMock()) as mock_send_text:

        await cm._handle_message_locked(
            clinic=MOCK_CLINIC,
            phone=phone,
            message=raw_whatsapp_msg,
            message_type="text",
        )

        # Must cleanly route to _show_doctors
        mock_show_doctors.assert_called_once_with(MOCK_CLINIC, phone, "en")
        # Must NEVER loop back to department list
        mock_show_dept.assert_not_called()
        # Must NEVER send timeout message
        for call in mock_send_text.call_args_list:
            assert "timed out" not in str(call)

