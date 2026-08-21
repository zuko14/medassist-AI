"""Tests for the post-booking-confirmation follow-up prompt — must offer
only 'Main Menu', not a redundant 'Book Appointment' button, per production
screenshot evidence (diagnostics_plan_issues/...2.29.52 PM.jpeg)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.conversation import ConversationManager


class TestConfirmingBookingFollowUp:
    @pytest.mark.asyncio
    async def test_follow_up_offers_only_main_menu(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"id": "patient-1", "name": "Test Patient"}
        context = {
            "doctor_name": "Dr. Test",
            "department": "Cardiology",
            "appointment_date": "2026-08-24",
            "appointment_time": "10:00",
            "booking_name": "Test Patient",
        }

        fake_result = {
            "success": True,
            "appointment": {
                "id": "apt-1",
                "booking_ref": "MC-2026-1001",
                "patient_name": "Test Patient",
                "doctor_name": "Dr. Test",
                "department": "Cardiology",
                "appointment_date": "2026-08-24",
                "appointment_time": "10:00",
            },
        }

        with patch(
            "app.services.conversation.book_appointment", new_callable=AsyncMock, return_value=fake_result
        ), patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ), patch.object(
            manager.whatsapp, "send_interactive_buttons", new_callable=AsyncMock
        ) as mock_send_buttons, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ), patch(
            "app.services.conversation.log_analytics_event", new_callable=AsyncMock
        ), patch("asyncio.sleep", new_callable=AsyncMock):
            await manager._handle_confirming_booking(
                clinic, "+919876543210", "", "confirm_booking", context, patient, "en"
            )

        buttons = mock_send_buttons.call_args.kwargs["buttons"]
        assert len(buttons) == 1
        assert buttons[0]["id"] == "main_menu"


class TestNotifyPaymentConfirmedFollowUp:
    @pytest.mark.asyncio
    async def test_lab_test_confirmation_sends_single_main_menu_button(self):
        from app.services.payment import PaymentService

        service = PaymentService()
        booking = {
            "clinic_id": "test-clinic",
            "patient_phone": "+919876543210",
            "booking_ref": "MC-2026-9001",
            "booking_type": "lab_test",
            "lab_test_name": "Complete Blood Count",
            "appointment_date": "2026-08-24",
            "amount_paise": 50000,
            "branch_id": None,
        }

        with patch(
            "app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock
        ), patch(
            "app.services.whatsapp.whatsapp_service.send_interactive_buttons", new_callable=AsyncMock
        ) as mock_send_buttons, patch(
            "app.services.tenant.get_clinic_by_id", new_callable=AsyncMock,
            return_value={"id": "test-clinic", "name": "Accumax Diagnostics", "config": {}},
        ), patch(
            "app.services.conversation.conversation_manager.update_state", new_callable=AsyncMock
        ) as mock_update_state:
            await service._notify_payment_confirmed(booking)

        buttons = mock_send_buttons.call_args.kwargs["buttons"]
        assert len(buttons) == 1
        assert buttons[0]["id"] == "main_menu"
        mock_update_state.assert_called_once()
        assert mock_update_state.call_args[0][2] == "main_menu"

    @pytest.mark.asyncio
    async def test_doctor_razorpay_confirmation_also_sends_main_menu_button(self):
        from app.services.payment import PaymentService

        service = PaymentService()
        booking = {
            "clinic_id": "test-clinic",
            "patient_phone": "+919876543210",
            "booking_ref": "MC-2026-9002",
            "booking_type": "consultation",
            "doctor_name": "Dr. Test",
            "department": "Cardiology",
            "appointment_date": "2026-08-24",
            "appointment_time": "10:00",
            "amount_paise": 50000,
            "branch_id": None,
        }

        with patch(
            "app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock
        ), patch(
            "app.services.whatsapp.whatsapp_service.send_interactive_buttons", new_callable=AsyncMock
        ) as mock_send_buttons, patch(
            "app.services.tenant.get_clinic_by_id", new_callable=AsyncMock,
            return_value={"id": "test-clinic", "name": "Test Clinic", "config": {}},
        ), patch(
            "app.services.conversation.conversation_manager.update_state", new_callable=AsyncMock
        ):
            await service._notify_payment_confirmed(booking)

        buttons = mock_send_buttons.call_args.kwargs["buttons"]
        assert len(buttons) == 1
        assert buttons[0]["id"] == "main_menu"


class TestMainMenuButtonTap:
    @pytest.mark.asyncio
    async def test_main_menu_button_tap_resets_state_and_sends_main_menu(self):
        manager = ConversationManager()
        clinic = {"id": "11111111-1111-1111-1111-111111111111", "whatsapp_number": "+911111111111"}
        phone = "+919876543210"

        with patch.object(
            manager, "_send_main_menu", new_callable=AsyncMock
        ) as mock_send_menu, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ) as mock_update_state, patch(
            "app.services.conversation.get_patient_by_phone",
            new_callable=AsyncMock,
            return_value={"language": "en"},
        ), patch(
            "app.services.conversation.get_or_create_conversation",
            new_callable=AsyncMock,
            return_value={"id": "conv-1", "state": "main_menu", "context": {}},
        ), patch(
            "app.services.conversation.screen_message",
            return_value=(False, ""),
        ):
            await manager.handle_message(
                clinic, phone, "Main Menu", message_type="interactive", interactive_data={"id": "main_menu"}
            )

        mock_update_state.assert_called_once_with(
            clinic, phone, "main_menu", {"menu_shown": False}
        )
        mock_send_menu.assert_called_once_with(clinic, phone, "en")


