"""Tests for diagnostics-only routing into the lab-test booking flow and full conversation handlers."""

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

from app.services.conversation import ConversationManager, ConversationState


class TestDiagnosticsOnlyRouting:
    def test_new_conversation_states_exist(self):
        assert ConversationState.BROWSING_LAB_TESTS == "browsing_lab_tests"
        assert ConversationState.CONFIRMING_COLLECTION_DATE == "confirming_collection_date"

    @pytest.mark.asyncio
    async def test_diagnostics_only_clinic_enters_lab_test_flow_not_department_list(self):
        """Regression: a diagnostics-only clinic (lab_test_booking feature, zero
        doctors) must never reach _show_department_list — this is the second,
        independent layer of protection against the department/doctor-list
        recursion crash."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"language": "en", "name": "Test Patient"}

        with patch(
            "app.services.tenant.has_feature", return_value=True
        ), patch(
            "app.services.conversation.get_doctors", new_callable=AsyncMock, return_value=[]
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_lab_tests, patch.object(
            manager, "_show_department_list", new_callable=AsyncMock
        ) as mock_show_dept_list, patch(
            "app.services.tenant.get_clinic_branches", new_callable=AsyncMock, return_value=[]
        ), patch(
            "app.services.tenant.has_branches", return_value=False
        ):
            await manager._start_booking(clinic, "+919876543210", patient, "en")

        mock_show_lab_tests.assert_called_once()
        mock_show_dept_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_clinic_with_doctors_never_enters_lab_test_flow(self):
        """A clinic with doctors (even if lab_test_booking is somehow enabled)
        keeps using the normal doctor-booking flow untouched."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"language": "en", "name": "Test Patient"}

        with patch(
            "app.services.tenant.has_feature", return_value=True
        ), patch(
            "app.services.conversation.get_doctors",
            new_callable=AsyncMock,
            return_value=[{"id": "doc-1", "name": "Dr. Test"}],
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_lab_tests, patch.object(
            manager, "_continue_booking_after_branch", new_callable=AsyncMock
        ) as mock_continue, patch(
            "app.services.tenant.get_clinic_branches", new_callable=AsyncMock, return_value=[]
        ), patch(
            "app.services.tenant.has_branches", return_value=False
        ):
            await manager._start_booking(clinic, "+919876543210", patient, "en")

        mock_show_lab_tests.assert_not_called()
        mock_continue.assert_called_once()


class TestShowLabTestList:
    @pytest.mark.asyncio
    async def test_sends_list_of_active_tests(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {}

        fake_tests = [
            {"id": "t1", "name": "CBC", "price_paise": 50000, "sample_type": "Blood"},
            {"id": "t2", "name": "Lipid Profile", "price_paise": 40000, "sample_type": "Blood"},
        ]

        with patch(
            "app.database.get_lab_tests", new_callable=AsyncMock, return_value=fake_tests
        ), patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ) as mock_update_state:
            await manager._show_lab_test_list(clinic, "+919876543210", context, "en")

        mock_send_list.assert_called_once()
        sections = mock_send_list.call_args.kwargs["sections"]
        row_ids = [r["id"] for r in sections[0]["rows"]]
        assert row_ids == ["labtest_t1", "labtest_t2"]
        mock_update_state.assert_called_once_with(clinic, "+919876543210", "browsing_lab_tests", context)

    @pytest.mark.asyncio
    async def test_no_active_tests_falls_back_to_main_menu(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {}

        with patch(
            "app.database.get_lab_tests", new_callable=AsyncMock, return_value=[]
        ), patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ) as mock_send_text, patch.object(
            manager, "_send_main_menu", new_callable=AsyncMock
        ) as mock_main_menu:
            await manager._show_lab_test_list(clinic, "+919876543210", context, "en")

        mock_send_text.assert_called_once()
        mock_main_menu.assert_called_once()


class TestHandleBrowsingLabTests:
    @pytest.mark.asyncio
    async def test_selecting_test_offers_collection_dates(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {}
        fake_test = {
            "id": "t1",
            "name": "CBC",
            "price_paise": 50000,
            "sample_type": "Blood",
            "is_active": True,
            "fasting_required": False,
            "prep_instructions": None,
            "turnaround_hours": 24,
        }
        fake_window = {"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"}

        with patch(
            "app.database.get_lab_test_by_id", new_callable=AsyncMock, return_value=fake_test
        ), patch(
            "app.database.get_lab_collection_window", new_callable=AsyncMock, return_value=fake_window
        ), patch.object(
            manager.whatsapp, "send_interactive_buttons", new_callable=AsyncMock
        ) as mock_send_buttons, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ) as mock_update_state:
            await manager._handle_browsing_lab_tests(
                clinic, "+919876543210", "", "select_lab_test", context, "en",
                interactive_data={"id": "labtest_t1"},
            )

        mock_send_buttons.assert_called_once()
        buttons = mock_send_buttons.call_args.kwargs["buttons"]
        assert len(buttons) == 3
        assert all(b["id"].startswith("labdate_") for b in buttons)
        assert context["lab_test_id"] == "t1"
        assert context["lab_test_name"] == "CBC"
        mock_update_state.assert_called_once_with(
            clinic, "+919876543210", "confirming_collection_date", context
        )

    @pytest.mark.asyncio
    async def test_deactivated_test_reprompts_list(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {}

        with patch(
            "app.database.get_lab_test_by_id", new_callable=AsyncMock, return_value=None
        ), patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_list:
            await manager._handle_browsing_lab_tests(
                clinic, "+919876543210", "", "select_lab_test", context, "en",
                interactive_data={"id": "labtest_deleted-id"},
            )

        mock_show_list.assert_called_once()


class TestHandleConfirmingCollectionDate:
    @pytest.mark.asyncio
    async def test_selecting_date_creates_payment_gated_booking(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"id": "patient-1", "name": "Test Patient"}
        context = {"lab_test_id": "t1", "lab_test_name": "CBC", "branch_id": None, "branch_name": None}

        fake_result = {
            "success": True,
            "booking_id": "booking-1",
            "booking_ref": "MC-2026-1000",
            "payment_link": "https://rzp.io/i/xyz",
            "amount_paise": 50000,
            "hold_expires_at": "2026-08-21T13:00:00Z",
        }

        with patch(
            "app.services.payment.payment_service.create_booking_with_payment",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as mock_create_booking, patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ) as mock_send_text, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ) as mock_update_state:
            await manager._handle_confirming_collection_date(
                clinic, "+919876543210", "", "select_date", context, patient, "en",
                interactive_data={"id": "labdate_2026-08-24"},
            )

        mock_create_booking.assert_called_once()
        call_kwargs = mock_create_booking.call_args.kwargs
        assert call_kwargs["booking_type"] == "lab_test"
        assert call_kwargs["lab_test_id"] == "t1"
        assert call_kwargs["lab_test_name"] == "CBC"
        assert call_kwargs["doctor_name"] is None
        assert call_kwargs["appointment_time"] is None
        assert call_kwargs["appointment_date"] == "2026-08-24"

        sent_text = mock_send_text.call_args[0][2]
        assert fake_result["payment_link"] in sent_text
        mock_update_state.assert_called_once_with(
            clinic, "+919876543210", "awaiting_payment", context
        )
        assert context["booking_id"] == "booking-1"
        assert context["booking_ref"] == "MC-2026-1000"

    @pytest.mark.asyncio
    async def test_no_date_selected_reprompts(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"id": "patient-1", "name": "Test Patient"}
        context = {"lab_test_id": "t1", "lab_test_name": "CBC"}

        with patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ) as mock_send_text:
            await manager._handle_confirming_collection_date(
                clinic, "+919876543210", "not a date", "unknown", context, patient, "en",
                interactive_data=None,
            )

        mock_send_text.assert_called_once()
