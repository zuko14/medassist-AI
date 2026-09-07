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


class TestProductionHardeningFixes:
    """Verifies the 4 production fixes across dispatcher, slots, lab context, and admin unassign."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("test_state", ["collecting_symptoms", "asking_symptoms"])
    async def test_dispatcher_routes_both_symptom_states(self, test_state):
        """Incoming messages in either collecting_symptoms or asking_symptoms route to _handle_collecting_symptoms."""
        cm = conversation_manager
        clinic = {"id": "9d9e9f12-c775-49c0-a326-98a59cdcc2e4", "name": "Dispatch Clinic", "is_active": True}
        phone = "+919876543210"

        with patch("app.services.conversation.get_or_create_conversation", new_callable=AsyncMock) as mock_get_conv, \
             patch("app.services.conversation.get_patient_by_phone", new_callable=AsyncMock) as mock_get_patient, \
             patch("app.services.conversation.get_lang", new_callable=AsyncMock) as mock_get_lang, \
             patch("app.services.conversation.update_conversation", new_callable=AsyncMock), \
             patch.object(cm, "_handle_collecting_symptoms", new_callable=AsyncMock) as mock_symptoms_handler, \
             patch("app.services.conversation.detect_intent", new_callable=AsyncMock) as mock_detect:

            mock_get_conv.return_value = {
                "state": test_state,
                "context": {"patient_name": "Test Patient"},
            }
            mock_patient = {"name": "Test", "language": "en", "opted_in": True, "data_consent": True}
            mock_get_patient.return_value = mock_patient
            mock_get_lang.return_value = "en"
            mock_detect.return_value = "general"

            await cm.handle_message(clinic, phone, "I have severe fever", message_type="text")

            mock_symptoms_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_date_selection_passes_branch_and_session(self):
        """_handle_selecting_date passes branch_id and branch_session into get_available_slots."""
        cm = conversation_manager
        clinic = {"id": "clinic-slots", "name": "Slots Clinic"}
        phone = "+919876543210"
        context = {
            "doctor_name": "Dr. Naidu",
            "branch_id": "branch-madv",
            "branch_session": "morning",
        }

        with patch("app.services.conversation.get_available_slots", new_callable=AsyncMock) as mock_get_slots, \
             patch.object(cm, "_show_slot_list", new_callable=AsyncMock):

            mock_get_slots.return_value = (["09:00", "09:30"], None)

            await cm._handle_selecting_date(clinic, phone, "tomorrow", context, "en")

            mock_get_slots.assert_awaited_once()
            _, kwargs = mock_get_slots.call_args
            assert kwargs.get("branch_id") == "branch-madv"
            assert kwargs.get("branch_session") == "morning"

    @pytest.mark.asyncio
    async def test_suggest_other_doctors_scopes_by_branch(self):
        """_suggest_other_doctors queries doctors and slots with branch_id and branch_session."""
        cm = conversation_manager
        clinic = {"id": "clinic-slots", "name": "Slots Clinic"}
        phone = "+919876543210"
        context = {
            "doctor_name": "Dr. Unavailable",
            "department": "Cardiology",
            "branch_id": "branch-madv",
            "branch_session": "morning",
        }

        alt_doc = {
            "name": "Dr. Alternate",
            "department": "Cardiology",
            "specialization": "Cardiologist",
            "session": "morning",
        }

        with patch("app.services.conversation.get_doctors", new_callable=AsyncMock) as mock_get_doctors, \
             patch("app.services.conversation.get_available_slots", new_callable=AsyncMock) as mock_get_slots, \
             patch.object(cm.whatsapp, "send_text", new_callable=AsyncMock):

            mock_get_doctors.return_value = [alt_doc]
            mock_get_slots.return_value = (["10:00"], None)

            await cm._suggest_other_doctors(clinic, phone, context, "en")

            # Check that get_doctors was filtered by branch_id
            mock_get_doctors.assert_awaited_once_with("clinic-slots", "Cardiology", branch_id="branch-madv")

            # Check that get_available_slots received branch_id and branch_session
            assert mock_get_slots.call_count >= 1
            _, slot_kwargs = mock_get_slots.call_args
            assert slot_kwargs.get("branch_id") == "branch-madv"
            assert slot_kwargs.get("branch_session") == "morning"

    @pytest.mark.asyncio
    async def test_start_lab_booking_atomically_sets_context_and_resets(self):
        """_start_lab_booking for single branch sets atomic branch context and calls update_state with reset_context=True."""
        cm = conversation_manager
        clinic = {"id": "clinic-lab", "name": "Lab Clinic"}
        phone = "+919876543210"

        with patch("app.services.tenant.get_clinic_branches", new_callable=AsyncMock) as mock_branches, \
             patch.object(cm, "update_state", new_callable=AsyncMock) as mock_update_state, \
             patch.object(cm, "_show_lab_test_list", new_callable=AsyncMock):

            mock_branches.return_value = [MADHURAWADA_BRANCH]

            await cm._start_lab_booking(clinic, phone, "en")

            mock_update_state.assert_awaited_once()
            call_args = mock_update_state.call_args
            assert call_args[0][2] == "browsing_lab_tests"
            ctx = call_args[0][3]
            assert ctx.get("branch_id") == MADHURAWADA_BRANCH["id"]
            assert ctx.get("branch_name") == MADHURAWADA_BRANCH["short_name"]
            assert ctx.get("branch_address") == MADHURAWADA_BRANCH["address"]
            assert call_args[1].get("reset_context") is True

    @pytest.mark.asyncio
    async def test_admin_update_doctor_explicit_unassign(self):
        """Passing branch_id='' to update_doctor unassigns the doctor from branches."""
        from app.routers.admin import update_doctor, DoctorUpdate
        from app.routers.admin import AdminUser

        admin_user = AdminUser(
            username="admin",
            role="admin",
            clinic_id="clinic-test",
            permissions=["DOCTORS_UPDATE"],
        )

        existing_doc = {
            "id": "doc-123",
            "name": "Dr. Test",
            "clinic_id": "clinic-test",
        }

        with patch("app.routers.admin.sb", new_callable=AsyncMock) as mock_sb, \
             patch("app.routers.admin.log_admin_action", new_callable=AsyncMock), \
             patch("app.routers.admin.invalidate_doctor_cache") as mock_inval:

            # Mock responses: 1. owner_query, 2. delete doctor_branches
            mock_sb.side_effect = [
                MagicMock(data=[existing_doc]),  # owner query
                MagicMock(data=[]),              # delete doctor_branches
            ]

            update_payload = DoctorUpdate(branch_id="")
            result = await update_doctor(
                doctor_id="doc-123",
                doctor=update_payload,
                clinic_id="clinic-test",
                user=admin_user,
            )

            assert result["branch_id"] is None
            assert result["branch_session"] is None
            mock_inval.assert_called_once_with(clinic_id="clinic-test")

