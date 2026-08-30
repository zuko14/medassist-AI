# tests/test_conversation_payment_mode.py
"""Test that _handle_confirming_booking branches correctly on payment_mode."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("WABA_DISPLAY_NAME", "Test Hospital")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("HOSPITAL_NAME", "City Care Hospital")
os.environ.setdefault("HOSPITAL_EMERGENCY_NUMBER", "108")
os.environ.setdefault("HOSPITAL_PHONE", "+919876543210")
os.environ.setdefault("HOSPITAL_MAPS_LINK", "https://maps.google.com")
os.environ.setdefault("HOSPITAL_WEBSITE", "https://test.hospital.com")
os.environ.setdefault("HOSPITAL_PRIVACY_POLICY_URL", "https://test.hospital.com/privacy")
os.environ.setdefault("HOSPITAL_ADDRESS", "Test Address")
os.environ.setdefault("HOSPITAL_LANDMARK", "Test Landmark")
os.environ.setdefault("BOOKING_REF_PREFIX", "MC")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

mock_supabase = MagicMock()
mock_db_module = MagicMock()
mock_db_module.supabase = mock_supabase
mock_db_module.log_analytics_event = AsyncMock()


async def _mock_sb(builder):
    """Stand-in for app.database.sb (T5.1 off-loop query execution).

    Every attribute of a MagicMock module is itself a MagicMock, so without an
    explicit entry `sb` resolves to one — and `await sb(...)` then raises
    "object MagicMock can't be used in 'await' expression" in any module that
    imported it while this fake was installed in sys.modules.

    Runs the builder inline rather than on a thread: these are mocks, there is
    nothing to block on, and staying on the loop keeps call ordering
    deterministic for assertions.
    """
    return builder.execute()


mock_db_module.sb = _mock_sb
sys.modules["app.database"] = mock_db_module

from app.services.conversation import ConversationManager  # noqa: E402


def _clinic(config: dict) -> dict:
    return {"id": "clinic-1", "name": "Test Clinic", "config": config}


@pytest.fixture(scope="module", autouse=True)
def cleanup_mock_db():
    yield
    if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
        del sys.modules["app.database"]


def _context() -> dict:
    return {
        "doctor_name": "Dr. Test",
        "appointment_date": "2026-07-05",
        "appointment_time": "10:00",
        "department": "General Medicine",
        "booking_name": "Patient",
    }


@pytest.mark.asyncio
async def test_partial_mode_sends_deposit_note_and_scaled_amount():
    manager = ConversationManager()
    manager.whatsapp.send_text = AsyncMock()
    manager.update_state = AsyncMock()

    clinic = _clinic(
        {
            "razorpay_key_id": "rzp_1",
            "razorpay_key_secret": "secret1",
            "payment_mode": "partial",
            "payment_deposit_percent": 20,
        }
    )

    with patch(
        "app.services.payment.payment_service.create_booking_with_payment",
        new_callable=AsyncMock,
        return_value={
            "success": True,
            "booking_id": "booking-1",
            "booking_ref": "MC-1",
            "razorpay_payment_link_id": "link-1",
            "payment_link": "https://razorpay.example/pay",
            "amount_paise": 10000,
            "hold_expires_at": "2026-07-05T10:00:00Z",
        },
    ) as mock_create:
        await manager._handle_confirming_booking(
            clinic, "+919876543210", "yes", "confirm_booking", _context(), {"id": "patient-1"}, "en"
        )

    # deposit_percent scaled correctly reaches the payment service
    assert mock_create.call_args.kwargs["deposit_percent"] == 20

    sent_message = manager.whatsapp.send_text.call_args[0][2]
    assert "20%" in sent_message
    assert "80%" in sent_message

    # Regression guard: create_booking_with_payment's real return dict has no
    # "razorpay_order_id" key (only "razorpay_payment_link_id") — asserting
    # the state transition ran confirms the handler didn't KeyError before
    # reaching update_state.
    manager.update_state.assert_awaited_once()
    saved_context = manager.update_state.call_args[0][3]
    assert saved_context["razorpay_payment_link_id"] == "link-1"
    assert saved_context["booking_id"] == "booking-1"


@pytest.mark.asyncio
async def test_none_mode_skips_payment_and_books_directly():
    manager = ConversationManager()
    manager.whatsapp.send_text = AsyncMock()
    manager.update_state = AsyncMock()

    clinic = _clinic({"payment_mode": "none"})

    with patch(
        "app.services.conversation.book_appointment",
        new_callable=AsyncMock,
        return_value={
            "success": True,
            "appointment": {"booking_ref": "MC-2"},
        },
    ) as mock_book, patch(
        "app.services.payment.payment_service.create_booking_with_payment",
        new_callable=AsyncMock,
    ) as mock_create:
        await manager._handle_confirming_booking(
            clinic, "+919876543210", "yes", "confirm_booking", _context(), {"id": "patient-1"}, "en"
        )

    mock_book.assert_called_once()
    mock_create.assert_not_called()
