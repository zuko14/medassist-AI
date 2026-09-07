"""Tests for multi-branch context integrity and anti-cross-contamination.

Ensures:
1. Branch context fields (id, name, address, landmark, maps_link, session) are set atomically.
2. Ephemeral booking context is wiped when starting a fresh booking or finishing a confirmed booking.
3. Confirmation displays and location messages authoritatively resolve branch details by branch_id,
   preventing stale or cross-contaminated address/landmark data.
4. Doctor selection correctly respects branch assignments without leaking stale branch details.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.conversation import (
    conversation_manager,
    ConversationManager,
    ConversationState,
    BOOKING_CONTEXT_KEYS,
)


MADHURAWADA_BRANCH = {
    "id": "bdb74de5-3d50-4fb4-829f-78ef5d9197b0",
    "name": "MADHURAWADA",
    "short_name": "Madhurawada",
    "address": "6-107/1B,Krishna Nagar,Chandrampalem, Madhurawada, Visakhapatnam, Andhra Pradesh 530041",
    "landmark": "BESIDE MAX FASHIONS",
    "maps_link": "https://maps.google.com/?q=17.8189,83.3541",
    "is_active": True,
}

MAHARANIPETA_BRANCH = {
    "id": "d3b77ff8-7512-442d-8e63-2cefb07a9740",
    "name": "MAHARANIPETA",
    "short_name": "Maharanipeta",
    "address": "# 17-1-28, Opp KGH Clock Tower, Maharanipeta, Visakhapatnam, Andhra Pradesh 530002",
    "landmark": "Opp KGH Clock Towe",
    "maps_link": "https://maps.google.com/?q=17.7123,83.3056",
    "is_active": True,
}


class TestBranchContextAtomicOperations:
    """Verifies atomic context setter and cleanup helpers in ConversationManager."""

    def test_set_branch_context_atomically_updates_all_keys(self):
        """When switching branches, all 6 branch keys must be overwritten atomically."""
        cm = conversation_manager
        # Simulate tainted context with Madhurawada ID but Maharanipeta address & landmark
        context = {
            "branch_id": MADHURAWADA_BRANCH["id"],
            "branch_name": "MADHURAWADA",
            "branch_address": MAHARANIPETA_BRANCH["address"],
            "branch_landmark": MAHARANIPETA_BRANCH["landmark"],
            "branch_maps_link": MAHARANIPETA_BRANCH["maps_link"],
            "branch_session": "both",
        }

        # Atomically update to Madhurawada
        cm._set_branch_context(context, MADHURAWADA_BRANCH, session_val="morning")

        assert context["branch_id"] == MADHURAWADA_BRANCH["id"]
        assert context["branch_name"] == MADHURAWADA_BRANCH["short_name"]
        assert context["branch_address"] == MADHURAWADA_BRANCH["address"]
        assert context["branch_landmark"] == MADHURAWADA_BRANCH["landmark"]
        assert context["branch_maps_link"] == MADHURAWADA_BRANCH["maps_link"]
        assert context["branch_session"] == "morning"

    def test_set_branch_context_with_none_branch_clears_branch_keys(self):
        """Setting branch context to None must clear all branch keys."""
        cm = conversation_manager
        context = {
            "branch_id": MADHURAWADA_BRANCH["id"],
            "branch_name": "MADHURAWADA",
            "branch_address": MADHURAWADA_BRANCH["address"],
            "branch_landmark": MADHURAWADA_BRANCH["landmark"],
            "branch_maps_link": MADHURAWADA_BRANCH["maps_link"],
            "branch_session": "morning",
            "keep_me": "persists",
        }

        cm._set_branch_context(context, None)

        assert "branch_id" not in context
        assert "branch_name" not in context
        assert "branch_address" not in context
        assert "branch_landmark" not in context
        assert "branch_maps_link" not in context
        assert "branch_session" not in context
        assert context["keep_me"] == "persists"

    def test_clear_booking_context_preserves_persistent_metadata(self):
        """_clear_booking_context removes all ephemeral booking keys while preserving user/tenant keys."""
        cm = conversation_manager
        context = {
            # Booking keys
            "doctor": {"id": "doc-1", "name": "Dr. Test"},
            "doctor_id": "doc-1",
            "doctor_name": "Dr. Test",
            "selected_doctor_id": "doc-1",
            "department": "Cardiology",
            "appointment_date": "2026-09-10",
            "appointment_time": "10:00",
            "branch_id": "b-1",
            "branch_name": "Test Branch",
            "branch_address": "Test Road",
            "branch_landmark": "Near Test",
            "branch_maps_link": "https://maps.test",
            "branch_session": "both",
            "booking_name": "Patient Name",
            # Persistent keys
            "user_name": "Patient Name",
            "patient_phone": "+919876543210",
            "lang": "en",
        }

        cm._clear_booking_context(context)

        for key in BOOKING_CONTEXT_KEYS:
            assert key not in context, f"Key {key} was not cleared from booking context"

        assert context["user_name"] == "Patient Name"
        assert context["patient_phone"] == "+919876543210"
        assert context["lang"] == "en"


@pytest.mark.asyncio
class TestAuthoritativeBranchResolution:
    """Verifies authoritative DB resolution of branch details during confirmation & location messaging."""

    async def test_show_booking_confirmation_resolves_branch_authoritatively(self):
        """Even if context contains contaminated landmark/address, confirmation must resolve authoritative branch."""
        cm = conversation_manager
        phone = "+917981945956"
        clinic = {
            "id": "9d9e9f12-c775-49c0-a326-98a59cdcc2e4",
            "name": "Visakha Multispeciality Clinics",
        }
        # Tainted context: Madhurawada ID, but Maharanipeta landmark
        context = {
            "booking_name": "Test Patient",
            "doctor_name": "DR LATCHIREDDY S A NAIDU",
            "department": "Diabetology",
            "appointment_date": "2026-09-10",
            "appointment_time": "10:00",
            "branch_id": MADHURAWADA_BRANCH["id"],
            "branch_name": "MADHURAWADA",
            "branch_landmark": "Opp KGH Clock Towe",  # Stale Maharanipeta landmark!
            "branch_address": MAHARANIPETA_BRANCH["address"],
        }

        with patch(
            "app.services.tenant.get_branch_by_id",
            new_callable=AsyncMock,
            return_value=MADHURAWADA_BRANCH,
        ) as mock_get_branch, patch.object(
            cm.whatsapp, "send_interactive_buttons", new_callable=AsyncMock
        ) as mock_send_buttons, patch.object(
            cm, "update_state", new_callable=AsyncMock
        ):
            await cm._show_booking_confirmation(clinic, phone, context, "en")

        mock_get_branch.assert_awaited_once_with(MADHURAWADA_BRANCH["id"])
        mock_send_buttons.assert_awaited_once()

        body_text = mock_send_buttons.await_args.kwargs.get("body")

        # Must display authentic Madhurawada landmark and NOT Maharanipeta
        assert "BESIDE MAX FASHIONS" in body_text
        assert "Opp KGH Clock Towe" not in body_text

        # Context must be synchronized
        assert context["branch_landmark"] == "BESIDE MAX FASHIONS"
        assert context["branch_address"] == MADHURAWADA_BRANCH["address"]

    async def test_handle_confirming_booking_sends_authoritative_location(self):
        """On booking confirmation, the location message must resolve authoritative branch data."""
        cm = conversation_manager
        phone = "+917981945956"
        clinic = {
            "id": "9d9e9f12-c775-49c0-a326-98a59cdcc2e4",
            "name": "Visakha Multispeciality Clinics",
            "phone": "+919999999999",
            "payment_mode": "pay_at_clinic",
        }
        context = {
            "booking_name": "Test Patient",
            "doctor_name": "DR LATCHIREDDY S A NAIDU",
            "doctor_id": "705c33c2-3f1f-41a0-a899-2f7b4cacc2b0",
            "department": "Diabetology",
            "appointment_date": "2026-09-10",
            "appointment_time": "10:00",
            "branch_id": MADHURAWADA_BRANCH["id"],
            "branch_name": "MADHURAWADA",
            "branch_landmark": "Opp KGH Clock Towe",  # Tainted
            "branch_address": MAHARANIPETA_BRANCH["address"],  # Tainted
        }

        mock_appointment = {
            "id": "apt-12345",
            "booking_ref": "VMC-2026-001",
            "appointment_date": "2026-09-10",
            "appointment_time": "10:00",
            "patient_name": "Test Patient",
        }

        with patch(
            "app.services.tenant.get_branch_by_id",
            new_callable=AsyncMock,
            return_value=MADHURAWADA_BRANCH,
        ) as mock_get_branch, patch(
            "app.services.conversation.book_appointment",
            new_callable=AsyncMock,
            return_value={"success": True, "appointment": mock_appointment},
        ), patch(
            "app.database.get_doctor_by_name",
            new_callable=AsyncMock,
            return_value={"id": "doc-1", "is_active": True},
        ), patch.object(
            cm.whatsapp, "send_text", new_callable=AsyncMock
        ) as mock_send_text, patch.object(
            cm.whatsapp, "send_interactive_buttons", new_callable=AsyncMock
        ), patch.object(
            cm, "update_state", new_callable=AsyncMock
        ) as mock_update_state:
            await cm._handle_confirming_booking(
                clinic,
                phone,
                "confirm_yes",
                "confirm_booking",
                context,
                {"name": "Test Patient"},
                "en",
            )

        mock_get_branch.assert_awaited_with(MADHURAWADA_BRANCH["id"])

        # Check all sent messages to find the location message
        sent_messages = [call.args[2] for call in mock_send_text.await_args_list if len(call.args) > 2]
        location_msg = next((msg for msg in sent_messages if "📍 Location:" in msg), None)
        assert location_msg is not None, f"Location message was not sent. Sent messages: {sent_messages}"

        # The location message must contain Madhurawada address & landmark, not Maharanipeta
        assert "BESIDE MAX FASHIONS" in location_msg
        assert MADHURAWADA_BRANCH["address"] in location_msg
        assert "Opp KGH Clock Towe" not in location_msg

        # Must reset context upon successful booking
        mock_update_state.assert_awaited_with(
            clinic,
            phone,
            "main_menu",
            reset_context=True,
        )


@pytest.mark.asyncio
class TestDoctorSelectionBranchIntegrity:
    """Verifies branch handling during doctor selection."""

    async def test_handle_selecting_doctor_single_branch_auto_assigns_atomically(self):
        """When a doctor is assigned to a single branch, it is auto-assigned atomically."""
        cm = conversation_manager
        phone = "+917981945956"
        clinic = {"id": "9d9e9f12-c775-49c0-a326-98a59cdcc2e4", "name": "Test Clinic"}
        doctor_id = "doc-123"

        doc = {
            "id": doctor_id,
            "name": "Dr. Single Branch",
            "department": "Diabetology",
        }

        context = {"available_doctors": [doc], "doctor_page": 0}

        doc_branch_data = [{
            "branch_id": MADHURAWADA_BRANCH["id"],
            "session": "morning",
            "branches": MADHURAWADA_BRANCH,
        }]

        with patch(
            "app.services.conversation.sb", new_callable=AsyncMock
        ) as mock_sb, patch.object(
            cm, "_show_date_picker", new_callable=AsyncMock
        ) as mock_date_picker, patch.object(
            cm, "update_state", new_callable=AsyncMock
        ):
            mock_sb.side_effect = [
                MagicMock(data=[doc]),
                MagicMock(data=doc_branch_data),
            ]

            await cm._handle_selecting_doctor(
                clinic,
                phone,
                "1",
                "select_doctor",
                context,
                "en",
                interactive_data={"id": f"doc_{doctor_id}"},
            )

        # Context must have Madhurawada fully set
        assert context["branch_id"] == MADHURAWADA_BRANCH["id"]
        assert context["branch_name"] == MADHURAWADA_BRANCH["short_name"]
        assert context["branch_address"] == MADHURAWADA_BRANCH["address"]
        assert context["branch_landmark"] == MADHURAWADA_BRANCH["landmark"]
        assert context["branch_maps_link"] == MADHURAWADA_BRANCH["maps_link"]
        assert context["branch_session"] == "morning"
        mock_date_picker.assert_awaited_once()

    async def test_handle_selecting_doctor_multi_branch_prompts_selection(self):
        """When a doctor is assigned to multiple branches, bot prompts branch selection without leaking previous branch."""
        cm = conversation_manager
        phone = "+917981945956"
        clinic = {"id": "9d9e9f12-c775-49c0-a326-98a59cdcc2e4", "name": "Test Clinic"}
        doctor_id = "doc-multi"

        doc = {
            "id": doctor_id,
            "name": "Dr. Multi Branch",
            "department": "General Medicine",
        }

        # Stale context from earlier booking
        context = {
            "available_doctors": [doc],
            "doctor_page": 0,
            "branch_id": "stale-branch",
            "branch_name": "Stale Branch",
            "branch_landmark": "Stale Landmark",
        }

        doc_branch_data = [
            {
                "branch_id": MADHURAWADA_BRANCH["id"],
                "session": "morning",
                "branches": MADHURAWADA_BRANCH,
            },
            {
                "branch_id": MAHARANIPETA_BRANCH["id"],
                "session": "evening",
                "branches": MAHARANIPETA_BRANCH,
            },
        ]

        with patch(
            "app.services.conversation.sb", new_callable=AsyncMock
        ) as mock_sb, patch.object(
            cm, "_send_doctor_branch_selection", new_callable=AsyncMock
        ) as mock_send_branch_sel, patch.object(
            cm, "update_state", new_callable=AsyncMock
        ):
            mock_sb.side_effect = [
                MagicMock(data=[doc]),
                MagicMock(data=doc_branch_data),
            ]

            await cm._handle_selecting_doctor(
                clinic,
                phone,
                "1",
                "select_doctor",
                context,
                "en",
                interactive_data={"id": f"doc_{doctor_id}"},
            )

        mock_send_branch_sel.assert_awaited_once()
