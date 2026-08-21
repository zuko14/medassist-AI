"""Tests for diagnostics-only clinic menu behavior — 'Our Services'/'Our
Doctors' must never leak the doctor-department flow, and the post-booking
follow-up must offer only Main Menu."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.conversation import ConversationManager


class TestIsDiagnosticsOnly:
    @pytest.mark.asyncio
    async def test_true_when_feature_on_and_zero_doctors(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1"}

        with patch(
            "app.services.tenant.has_feature", return_value=True
        ), patch(
            "app.services.conversation.get_doctors", new_callable=AsyncMock, return_value=[]
        ):
            result = await manager._is_diagnostics_only(clinic)

        assert result is True

    @pytest.mark.asyncio
    async def test_false_when_doctors_present_even_with_feature_on(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1"}

        with patch(
            "app.services.tenant.has_feature", return_value=True
        ), patch(
            "app.services.conversation.get_doctors",
            new_callable=AsyncMock,
            return_value=[{"id": "doc-1"}],
        ):
            result = await manager._is_diagnostics_only(clinic)

        assert result is False

    @pytest.mark.asyncio
    async def test_false_when_feature_off(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1"}

        with patch(
            "app.services.tenant.has_feature", return_value=False
        ), patch(
            "app.services.conversation.get_doctors", new_callable=AsyncMock, return_value=[]
        ):
            result = await manager._is_diagnostics_only(clinic)

        assert result is False


class TestStartBookingUsesSharedHelper:
    @pytest.mark.asyncio
    async def test_diagnostics_only_still_routes_to_lab_tests(self):
        """Regression: Task 1's refactor must not change _start_booking's
        existing diagnostics-only routing behavior."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"language": "en", "name": "Test Patient"}

        with patch.object(
            manager, "_is_diagnostics_only", new_callable=AsyncMock, return_value=True
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


class TestDiagnosticsAwareMainMenu:
    @pytest.mark.asyncio
    async def test_diagnostics_only_menu_omits_services_and_doctors(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

        with patch.object(
            manager, "_is_diagnostics_only", new_callable=AsyncMock, return_value=True
        ), patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list:
            await manager._send_main_menu(clinic, "+919876543210", "en")

        row_ids = [
            r["id"]
            for section in mock_send_list.call_args.kwargs["sections"]
            for r in section["rows"]
        ]
        assert "menu_services" not in row_ids
        assert "menu_doctors" not in row_ids
        assert "menu_book" in row_ids

    @pytest.mark.asyncio
    async def test_regular_clinic_menu_unchanged(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

        with patch.object(
            manager, "_is_diagnostics_only", new_callable=AsyncMock, return_value=False
        ), patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list:
            await manager._send_main_menu(clinic, "+919876543210", "en")

        row_ids = [
            r["id"]
            for section in mock_send_list.call_args.kwargs["sections"]
            for r in section["rows"]
        ]
        assert "menu_services" in row_ids
        assert "menu_doctors" in row_ids


class TestStaleMenuTapGuards:
    @pytest.mark.asyncio
    async def test_view_services_redirects_to_lab_tests_for_diagnostics_only(self):
        """A patient with an old WhatsApp thread (message sent before this
        fix shipped) tapping a stale 'Our Services' button must not see the
        doctor-department list."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"language": "en"}

        with patch(
            "app.database.get_conversation", new_callable=AsyncMock,
            return_value={"context": {"menu_shown": True}},
        ), patch.object(
            manager, "_is_diagnostics_only", new_callable=AsyncMock, return_value=True
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_lab_tests, patch.object(
            manager, "_show_services", new_callable=AsyncMock
        ) as mock_show_services:
            await manager._handle_main_menu(
                clinic, "+919876543210", "", "view_services", patient, "en"
            )

        mock_show_lab_tests.assert_called_once()
        mock_show_services.assert_not_called()

    @pytest.mark.asyncio
    async def test_doctor_availability_redirects_to_lab_tests_for_diagnostics_only(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"language": "en"}

        with patch(
            "app.database.get_conversation", new_callable=AsyncMock,
            return_value={"context": {"menu_shown": True}},
        ), patch.object(
            manager, "_is_diagnostics_only", new_callable=AsyncMock, return_value=True
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_lab_tests, patch.object(
            manager, "_show_doctors", new_callable=AsyncMock
        ) as mock_show_doctors:
            await manager._handle_main_menu(
                clinic, "+919876543210", "", "doctor_availability", patient, "en"
            )

        mock_show_lab_tests.assert_called_once()
        mock_show_doctors.assert_not_called()


class TestShowDoctorsEmptyGuard:
    @pytest.mark.asyncio
    async def test_zero_doctors_sends_friendly_text_not_empty_list(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

        mock_result = MagicMock()
        mock_result.data = []

        with patch("app.database.supabase") as mock_sb, patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list, patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ) as mock_send_text:
            mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value = mock_result
            await manager._show_doctors(clinic, "+919876543210", "en")

        mock_send_list.assert_not_called()
        mock_send_text.assert_called_once()


