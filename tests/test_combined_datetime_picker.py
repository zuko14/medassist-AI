"""Tests for the combined date+time picker that merges what used to be two
separate WhatsApp interactive messages (pick a date, then pick a time) into
one, per user-reported production feedback."""

from app.services.conversation import ConversationManager


class TestToAmPm:
    def test_converts_morning_time(self):
        manager = ConversationManager()
        assert manager._to_ampm("09:30") == "9:30 AM"

    def test_converts_afternoon_time(self):
        manager = ConversationManager()
        assert manager._to_ampm("17:00") == "5:00 PM"

    def test_returns_input_unchanged_on_parse_failure(self):
        manager = ConversationManager()
        assert manager._to_ampm("not-a-time") == "not-a-time"


from app.templates.whatsapp_templates import get_message


class TestSelectDatetimeMessage:
    def test_english_message_exists(self):
        msg = get_message("select_datetime", "en")
        assert msg and msg != "select_datetime"

    def test_falls_back_to_english_for_unknown_language(self):
        msg = get_message("select_datetime", "fr")
        assert msg == get_message("select_datetime", "en")


import pytest
from unittest.mock import AsyncMock, patch


class TestShowCombinedSlotPicker:
    @pytest.mark.asyncio
    async def test_builds_one_list_spanning_multiple_days(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {"doctor_name": "Dr. Test"}

        async def fake_get_available_slots(clinic_id, doctor_name, date_str, **kwargs):
            # First two checked days have slots; the rest are empty.
            if date_str in ("2026-08-21", "2026-08-22"):
                return ["09:00", "09:30", "10:00"], None
            return [], None

        with patch(
            "app.services.conversation.get_available_slots", side_effect=fake_get_available_slots
        ), patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ) as mock_update_state, patch(
            "app.services.conversation.datetime"
        ) as mock_dt:
            from datetime import datetime as real_datetime

            mock_dt.now.return_value = real_datetime(2026, 8, 21)
            mock_dt.strptime = real_datetime.strptime

            await manager._show_combined_slot_picker(clinic, "+919876543210", context, "en")

        sections = mock_send_list.call_args.kwargs["sections"]
        assert len(sections) == 2  # two days with availability
        total_rows = sum(len(s["rows"]) for s in sections)
        assert total_rows <= 10
        all_ids = [r["id"] for s in sections for r in s["rows"]]
        assert all(rid.startswith("dtslot_") for rid in all_ids)
        mock_update_state.assert_called_once_with(
            clinic, "+919876543210", "selecting_slot", context
        )

    @pytest.mark.asyncio
    async def test_no_availability_in_14_days_suggests_other_doctors(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {"doctor_name": "Dr. Test"}

        with patch(
            "app.services.conversation.get_available_slots",
            new_callable=AsyncMock,
            return_value=([], None),
        ), patch.object(
            manager, "_suggest_other_doctors", new_callable=AsyncMock
        ) as mock_suggest, patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list:
            await manager._show_combined_slot_picker(clinic, "+919876543210", context, "en")

        mock_suggest.assert_called_once()
        mock_send_list.assert_not_called()


from unittest.mock import AsyncMock, MagicMock, patch


class TestDoctorSelectionCallSites:
    @pytest.mark.asyncio
    async def test_view_doctor_intent_calls_combined_picker(self):
        manager = ConversationManager()
        clinic = {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "City Clinic",
            "whatsapp_number": "+911111111111",
        }
        phone = "+919876543210"

        mock_doc = {
            "id": "doc-1",
            "name": "Dr. Test",
            "department": "General",
        }
        mock_result = MagicMock()
        mock_result.data = [mock_doc]

        with patch(
            "app.database.supabase"
        ) as mock_sb, patch.object(
            manager, "_show_combined_slot_picker", new_callable=AsyncMock
        ) as mock_combined, patch.object(
            manager, "_show_date_picker", new_callable=AsyncMock
        ) as mock_date_picker, patch(
            "app.services.conversation.get_patient_by_phone",
            new_callable=AsyncMock,
            return_value={"name": "Patient"},
        ), patch(
            "app.services.conversation.get_or_create_conversation",
            new_callable=AsyncMock,
            return_value={"id": "conv-1", "state": "main_menu", "context": {}},
        ), patch(
            "app.services.conversation.screen_message",
            return_value=(False, ""),
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value = mock_table
            mock_table.eq.return_value = mock_table
            mock_table.execute.return_value = mock_result

            await manager.handle_message(
                clinic,
                phone,
                "Dr. Test",
                message_type="interactive",
                interactive_data={"id": "view_doc_doc-1"},
            )

        mock_combined.assert_called_once()
        mock_date_picker.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_selecting_doctor_calls_combined_picker(self):
        manager = ConversationManager()
        clinic = {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "City Clinic",
            "whatsapp_number": "+911111111111",
        }
        phone = "+919876543210"
        context = {"patient_name": "Patient"}

        mock_doc = {
            "id": "doc-1",
            "name": "Dr. Test",
            "department": "General",
        }
        mock_result = MagicMock()
        mock_result.data = [mock_doc]

        with patch(
            "app.database.supabase"
        ) as mock_sb, patch.object(
            manager, "_show_combined_slot_picker", new_callable=AsyncMock
        ) as mock_combined, patch.object(
            manager, "_show_date_picker", new_callable=AsyncMock
        ) as mock_date_picker:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value = mock_table
            mock_table.eq.return_value = mock_table
            mock_table.execute.return_value = mock_result

            await manager._handle_selecting_doctor(
                clinic, phone, "Dr. Test", "select_doctor", context, "en"
            )

        mock_combined.assert_called_once()
        mock_date_picker.assert_not_called()


class TestHandleSelectingSlotCombined:
    @pytest.mark.asyncio
    async def test_select_datetime_intent_sets_both_date_and_time(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {"doctor_name": "Dr. Test"}

        with patch.object(
            manager, "_show_booking_confirmation", new_callable=AsyncMock
        ) as mock_show_confirmation:
            await manager._handle_selecting_slot(
                clinic, "+919876543210", "2026-08-24_10:00", "select_datetime", context, "en"
            )

        assert context["appointment_date"] == "2026-08-24"
        assert context["appointment_time"] == "10:00"
        mock_show_confirmation.assert_called_once()

    @pytest.mark.asyncio
    async def test_legacy_select_slot_intent_still_works(self):
        """Regression: the old single-day slot_ tap (reached only via the
        free-text-date fallback now) must keep working unchanged."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {"doctor_name": "Dr. Test", "appointment_date": "2026-08-24"}

        with patch.object(
            manager, "_show_booking_confirmation", new_callable=AsyncMock
        ) as mock_show_confirmation:
            await manager._handle_selecting_slot(
                clinic, "+919876543210", "10:00", "select_slot", context, "en"
            )

        assert context["appointment_time"] == "10:00"
        mock_show_confirmation.assert_called_once()

    @pytest.mark.asyncio
    async def test_free_text_delegates_to_handle_selecting_date(self):
        """A patient typing 'tomorrow' instead of tapping a button must
        still work — delegates to the existing free-text date parser."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {"doctor_name": "Dr. Test"}

        with patch.object(
            manager, "_handle_selecting_date", new_callable=AsyncMock
        ) as mock_handle_date:
            await manager._handle_selecting_slot(
                clinic, "+919876543210", "tomorrow", "unknown", context, "en"
            )

        mock_handle_date.assert_called_once_with(
            clinic, "+919876543210", "tomorrow", context, "en"
        )




